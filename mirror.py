#!/usr/bin/env python3
"""
mirror.py - Build a faithful STATIC MIRROR of the public hauscr.org site.

The live site is a Squarespace 7.1 (Fluid Engine) property. This tool crawls the
public pages, downloads every asset (CSS, images, fonts, PDFs, native videos),
strips all Squarespace runtime JavaScript, rewrites every URL to a self-hosted,
base-path-prefixed local path, and writes a browsable static tree into docs/ that
can be served from GitHub Pages as a development preview.

Usage:
    python mirror.py --base /hauscr-website --out docs

The run is idempotent: assets already present on disk are not re-downloaded, so
re-running only refreshes HTML/CSS rewriting and picks up new assets.

Nothing on the live Squarespace site is ever modified - this is read-only scraping.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SRC_ORIGIN = "https://www.hauscr.org"          # ALWAYS the www host (bare host has a bad cert)
PAGES_HOST = "https://harvard-auscr.github.io"  # GitHub Pages origin for canonical/og:url
SITE_ID = "63bbb9a897fc6b2ebb873111"

# Hosts whose assets we self-host.
SQSP_HOST_RE = re.compile(
    r"(images\.squarespace-cdn\.com|static1\.squarespace\.com|"
    r"static\.squarespace\.com|file\.squarespace-cdn\.com|"
    r"definitions\.sqspcdn\.com|assets\.squarespace\.com|"
    r"video\.squarespace-cdn\.com)",
    re.I,
)

# Seed pages (sitemap + nav). Folder roots /conferences and /about-us are handled
# as redirect stubs (they 302 on the live site).
SEED_PATHS = [
    "/", "/about-huauscr", "/about-us", "/advisory-board", "/apply",
    "/associate-team", "/bpc", "/conferences", "/contactus", "/ctb",
    "/deans-list", "/executives", "/hsylc", "/hweek", "/past-presidents", "/ysa",
]

# Folder roots that redirect to a first child on the live site.
FOLDER_REDIRECTS = {
    "/conferences": "/hsylc",
    "/about-us": "/about-huauscr",
}

# Paths we never crawl as pages.
SKIP_PAGE_PREFIXES = ("/cart", "/search", "/config", "/account", "/checkout", "/s/", "/api/")

MAX_PAGES = 40
MAX_WORKERS = 6
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

FONT_EXTS = {".woff2", ".woff", ".ttf", ".otf", ".eot"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".avif", ".bmp"}

# Typekit family (domain-locked, not downloadable) -> free Google Fonts substitute.
GOOGLE_FONTS_HREF = (
    "https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&display=swap"
)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


# --------------------------------------------------------------------------- #
# HTTP helpers (polite: retry with backoff)
# --------------------------------------------------------------------------- #

def fetch(url, binary=False, tries=4, timeout=40):
    """GET a URL with retry/backoff. Returns bytes (binary) or text, or None."""
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                return r.content if binary else r.text
            if r.status_code in (301, 302, 404, 410):
                return None
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1.5 * (i + 1))
    print(f"  ! fetch failed {url[:90]} ({last})", file=sys.stderr)
    return None


# --------------------------------------------------------------------------- #
# Asset registry + naming
# --------------------------------------------------------------------------- #

class Mirror:
    def __init__(self, base, out):
        self.base = base.rstrip("/")            # e.g. /hauscr-website
        self.out = out                          # e.g. docs
        # maps absolute source URL -> local repo-root-relative path (with base)
        self.asset_map = {}
        # queued downloads: local_disk_path -> (source_url, kind)
        self.downloads = {}
        self.substituted_fonts = set()
        self.video_notes = []
        for sub in ("css", "img", "fonts", "files", "js", "video"):
            os.makedirs(os.path.join(out, "assets", sub), exist_ok=True)

    # -- path helpers ------------------------------------------------------- #

    def local_url(self, disk_relpath):
        """docs/assets/img/x.jpg -> /hauscr-website/assets/img/x.jpg"""
        rel = disk_relpath.replace("\\", "/")
        if rel.startswith(self.out + "/"):
            rel = rel[len(self.out) + 1:]
        return f"{self.base}/{rel}"

    @staticmethod
    def _sanitize(name):
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
        return name[:80] or "asset"

    def _uuid_token(self, url):
        m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", url, re.I)
        if m:
            return m.group(0)[:8]
        return hashlib.md5(url.encode()).hexdigest()[:8]

    def _ext_from(self, path, default=".bin"):
        ext = os.path.splitext(urlsplit(path).path)[1].lower()
        return ext if ext else default

    # -- registration ------------------------------------------------------- #

    def register_asset(self, src_url, kind=None, variant=None):
        """
        Register an asset URL for download and return the local base-prefixed URL.
        kind: 'css' | 'img' | 'fonts' | 'files' | None(auto).
        variant: optional width tag for images (e.g. '1500w').
        """
        if not src_url or src_url.startswith("data:"):
            return src_url
        clean = src_url.split("#")[0]
        path_part = urlsplit(clean).path
        ext = self._ext_from(clean)

        if kind is None:
            if ext in FONT_EXTS:
                kind = "fonts"
            elif ext in IMG_EXTS:
                kind = "img"
            elif ext == ".css":
                kind = "css"
            else:
                kind = "files"

        token = self._uuid_token(clean)
        fname = self._sanitize(os.path.basename(path_part) or "asset")
        if not os.path.splitext(fname)[1] and ext not in ("", ".bin"):
            fname += ext

        if kind == "img" and variant:
            stem, e = os.path.splitext(fname)
            fname = f"{token}-{stem}-{variant}{e or '.jpg'}"
        else:
            fname = f"{token}-{fname}"

        disk = os.path.join(self.out, "assets", kind, fname)
        key = (clean, variant)
        if key not in self.downloads:
            self.downloads[key] = (clean, disk, kind, variant)
        self.asset_map[(clean, variant)] = self.local_url(disk)
        return self.local_url(disk)

    def register_image(self, src_url):
        """Register 1500w + 750w variants of a Squarespace image. Returns (src, srcset)."""
        base = src_url.split("?")[0].split("#")[0]
        u1500 = self.register_asset(base + "?format=1500w", kind="img", variant="1500w")
        u750 = self.register_asset(base + "?format=750w", kind="img", variant="750w")
        return u1500, u750

    # -- download drain ----------------------------------------------------- #

    def download_all(self):
        pending = [v for v in self.downloads.values()
                   if not os.path.exists(v[1]) or os.path.getsize(v[1]) == 0]
        print(f"  downloading {len(pending)} new assets "
              f"({len(self.downloads) - len(pending)} already cached)")

        def _dl(item):
            clean, disk, kind, variant = item
            data = fetch(clean, binary=True)
            if data is None:
                return (clean, False)
            os.makedirs(os.path.dirname(disk), exist_ok=True)
            with open(disk, "wb") as fh:
                fh.write(data)
            return (clean, True)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = [ex.submit(_dl, it) for it in pending]
            for f in as_completed(futs):
                pass

    # -- CSS post-processing ------------------------------------------------ #

    def rewrite_css_text(self, css_text, css_src_url):
        """Rewrite url(...) refs inside a CSS body to local base-prefixed assets."""
        def repl(m):
            raw = m.group(1).strip().strip('\'"')
            if raw.startswith("data:") or not raw:
                return m.group(0)
            # Already rewritten to a local base-prefixed asset: leave as-is
            # (prevents double-processing on repeated passes).
            if raw.startswith(self.base + "/"):
                return m.group(0)
            abs_url = urljoin(css_src_url, raw)
            if not SQSP_HOST_RE.search(abs_url):
                # leave non-squarespace url() (e.g. gstatic fonts) alone
                if abs_url.startswith(("http://", "https://", "//")):
                    return m.group(0)
                abs_url = urljoin(css_src_url, raw)
            local = self.register_asset(abs_url)
            return f"url({local})"

        return re.sub(r"url\(\s*([^)]+?)\s*\)", repl, css_text)

    def process_css_files(self):
        """After download, rewrite url() inside every downloaded .css to local assets,
        then drain any newly discovered assets (fonts/images referenced from CSS)."""
        css_dir = os.path.join(self.out, "assets", "css")
        # Build reverse map disk->source for css files
        src_by_disk = {v[1]: v[0] for v in self.downloads.values() if v[2] == "css"}
        for _ in range(3):  # a couple of passes to resolve nested @imports/newly found assets
            for disk, src_url in list(src_by_disk.items()):
                if not os.path.exists(disk):
                    continue
                with open(disk, "r", encoding="utf-8", errors="ignore") as fh:
                    txt = fh.read()
                new = self.rewrite_css_text(txt, src_url)
                if new != txt:
                    with open(disk, "w", encoding="utf-8") as fh:
                        fh.write(new)
            self.download_all()


# --------------------------------------------------------------------------- #
# Page routing
# --------------------------------------------------------------------------- #

def normalize_path(path):
    """Normalize an internal path to a canonical page key (leading slash, no trailing)."""
    path = path.split("#")[0].split("?")[0]
    if not path.startswith("/"):
        path = "/" + path
    if path == "/home":
        path = "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def route_for(page_path, base):
    """Base-prefixed route ending in '/' for a page path."""
    p = normalize_path(page_path)
    if p == "/":
        return base + "/"
    return f"{base}{p}/"


def out_file_for(page_path, out):
    p = normalize_path(page_path)
    if p == "/":
        return os.path.join(out, "index.html")
    return os.path.join(out, p.strip("/"), "index.html")


def canonical_for(page_path, base):
    return PAGES_HOST + route_for(page_path, base)


# --------------------------------------------------------------------------- #
# HTML processing
# --------------------------------------------------------------------------- #

def is_internal(href):
    if not href:
        return False
    if href.startswith("//"):
        return False
    if href.startswith("/"):
        return True
    for h in (SRC_ORIGIN, "https://hauscr.org", "http://www.hauscr.org", "http://hauscr.org"):
        if href.startswith(h):
            return True
    return False


def to_path(href):
    """Extract the path from an internal href."""
    if href.startswith("/"):
        return href
    sp = urlsplit(href)
    return sp.path or "/"


def rewrite_internal_href(href, mirror):
    """Rewrite an internal link to a base-prefixed, trailing-slash local route."""
    sp = urlsplit(href)
    path = sp.path or "/"
    frag = ("#" + sp.fragment) if sp.fragment else ""
    # File downloads (/s/<file>) -> local asset
    if path.startswith("/s/"):
        local = mirror.register_asset(urljoin(SRC_ORIGIN, path), kind="files")
        return local + frag
    return route_for(path, mirror.base) + frag


def discover_links(soup):
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not is_internal(href):
            continue
        path = normalize_path(to_path(href))
        if any(path.startswith(pre.rstrip("/")) for pre in SKIP_PAGE_PREFIXES):
            continue
        out.append(path)
    return out


def strip_runtime(soup):
    """Remove scripts and preconnect/preload style link hints."""
    for s in soup.find_all("script"):
        s.decompose()
    for l in soup.find_all("link"):
        rels = l.get("rel") or []
        rels = [r.lower() for r in rels]
        if any(r in ("preconnect", "dns-prefetch", "preload", "prefetch", "modulepreload") for r in rels):
            l.decompose()
    # Squarespace runtime-only chrome that renders wrong without JS.
    for sel in [
        ".sqs-cart-dropzone", ".sqs-announcement-bar-dropzone", "#sqs-announcement-bar-dropzone",
        ".sqs-cookie-banner-v2-container", "[data-test='cookie-banner']",
        ".sqs-mobile-info-bar", "#sqs-cmp-recommendations",
    ]:
        for el in soup.select(sel):
            el.decompose()


def process_images(soup, mirror):
    """Repoint every <img>/<source> and neutralize the JS loader so images render."""
    # <img>
    for img in soup.find_all("img"):
        cand = img.get("data-src") or img.get("data-image") or img.get("src")
        if cand and SQSP_HOST_RE.search(cand):
            u1500, u750 = mirror.register_image(cand)
            img["src"] = u1500
            img["srcset"] = f"{u750} 750w, {u1500} 1500w"
        elif img.get("src") and SQSP_HOST_RE.search(img["src"]):
            u1500, u750 = mirror.register_image(img["src"])
            img["src"] = u1500
            img["srcset"] = f"{u750} 750w, {u1500} 1500w"
        # Neutralize the Squarespace lazy-loader so images are visible without JS.
        for attr in ("data-src", "data-image", "data-srcset", "data-load", "data-loader",
                     "data-image-resolution"):
            if img.has_attr(attr):
                del img[attr]
        img["data-loaded"] = "true"
        cls = img.get("class", [])
        if "loaded" not in cls:
            cls.append("loaded")
        img["class"] = cls
        if img.get("loading") == "eager":
            pass

    # <source> inside <picture>
    for src in soup.find_all("source"):
        ss = src.get("srcset") or src.get("data-srcset")
        if ss and SQSP_HOST_RE.search(ss):
            first = ss.split(",")[0].strip().split(" ")[0]
            u1500, u750 = mirror.register_image(first)
            src["srcset"] = f"{u750} 750w, {u1500} 1500w"
            if src.has_attr("data-srcset"):
                del src["data-srcset"]


def process_inline_styles(soup, mirror, page_url):
    """Rewrite url() inside <style> blocks and style="" attributes."""
    for st in soup.find_all("style"):
        if st.string is None:
            txt = st.get_text()
        else:
            txt = st.string
        if txt and "url(" in txt:
            st.string = mirror.rewrite_css_text(txt, page_url)
    for el in soup.find_all(style=True):
        s = el["style"]
        if "url(" in s:
            el["style"] = mirror.rewrite_css_text(s, page_url)


def _rels(link):
    return [r.lower() for r in (link.get("rel") or [])]


def process_stylesheets(soup, mirror):
    """Download every <link rel=stylesheet> and repoint it locally; localize icons."""
    for l in soup.find_all("link", href=True):
        rels = _rels(l)
        href = l["href"]
        if "stylesheet" in rels:
            absu = "https:" + href if href.startswith("//") else urljoin(SRC_ORIGIN + "/", href)
            l["href"] = mirror.register_asset(absu, kind="css")
        elif any(r in ("icon", "shortcut", "apple-touch-icon", "image_src", "mask-icon") for r in rels):
            if SQSP_HOST_RE.search(href):
                l["href"] = mirror.register_asset(href.split("?")[0])


def process_block_css(soup, mirror):
    """Parse every data-block-css JSON list, download each unique CSS, and add a
    <link rel=stylesheet> per unique URL in <head> (normally injected by JS but
    required for video/form/button/html component blocks to look right)."""
    urls = []
    seen = set()
    for el in soup.find_all(attrs={"data-block-css": True}):
        raw = el.get("data-block-css")
        try:
            arr = json.loads(raw)
        except Exception:  # noqa: BLE001
            arr = [raw]
        for u in arr:
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
    head = soup.find("head")
    existing = {l.get("href") for l in soup.find_all("link", href=True)}
    for u in urls:
        local = mirror.register_asset(u, kind="css")
        if local in existing:
            continue  # already linked (e.g. also a page-level <link rel=stylesheet>)
        existing.add(local)
        link = soup.new_tag("link", rel="stylesheet", href=local)
        head.append(link)
    return urls


def rewrite_links(soup, mirror):
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if is_internal(href):
            a["href"] = rewrite_internal_href(href, mirror)
        # mailto/tel/#/external untouched


def process_video_blocks(soup, mirror):
    """Replace Squarespace native-video / embed blocks with self-hosted, JS-free players."""
    for block in soup.select(".sqs-block-video"):
        native = block.select_one(".sqs-native-video[data-config-video]")
        content = block.select_one(".sqs-block-content") or block
        if native:
            try:
                cfg = json.loads(native.get("data-config-video"))
            except Exception:  # noqa: BLE001
                cfg = {}
            thumb_url = None
            tb = native.get("data-config-thumbnail")
            if tb:
                try:
                    thumb_url = json.loads(tb).get("assetUrl")
                except Exception:  # noqa: BLE001
                    thumb_url = None
            settings = {}
            sset = native.get("data-config-settings")
            if sset:
                try:
                    settings = json.loads(sset)
                except Exception:  # noqa: BLE001
                    settings = {}
            local_mp4 = download_native_video(cfg, mirror)
            poster_local = None
            if thumb_url:
                poster_local, _ = mirror.register_image(thumb_url)
            new = build_video_html(soup, local_mp4, poster_local, settings, cfg)
            # Replace the intrinsic embed wrapper content
            inner = content.select_one(".intrinsic") or content
            inner.clear()
            inner.append(new)
            continue

        # oEmbed / YouTube / Vimeo path (data-block-json / data-html)
        embed = extract_oembed(block)
        if embed:
            inner = content.select_one(".intrinsic") or content
            inner.clear()
            inner.append(embed_iframe(soup, embed))


def download_native_video(cfg, mirror):
    """Download a Squarespace native (HLS) video's 360p stream to a local mp4 via ffmpeg.
    Returns the local base-prefixed URL, or None on failure (poster-only fallback)."""
    alex = cfg.get("alexandriaUrl")
    vid = cfg.get("systemDataId") or cfg.get("id") or "video"
    if not alex:
        return None
    token = re.search(r"[0-9a-f-]{36}", alex)
    stem = (token.group(0)[:8] if token else hashlib.md5(alex.encode()).hexdigest()[:8])
    disk = os.path.join(mirror.out, "assets", "video", f"{stem}.mp4")
    local_url = mirror.local_url(disk)
    if os.path.exists(disk) and os.path.getsize(disk) > 0:
        return local_url
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        mirror.video_notes.append(f"{vid}: ffmpeg unavailable, poster only")
        return None
    playlist = alex.replace("{variant}", "playlist.m3u8")
    master = fetch(playlist)
    if not master:
        mirror.video_notes.append(f"{vid}: HLS playlist unavailable, poster only")
        return None
    # Prefer the smallest (first) video variant for a lightweight preview.
    variant_url = None
    lines = master.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            variant_url = lines[i + 1].strip()
            break
    src = variant_url or playlist
    print(f"    ffmpeg fetching native video {stem} ...")
    # Squarespace HLS segment URIs carry no file extension, so ffmpeg's segment
    # extension whitelist must be relaxed. -an drops audio (blocks autoplay muted).
    base_cmd = [ffmpeg, "-y", "-loglevel", "error",
                "-user_agent", USER_AGENT,
                "-allowed_extensions", "ALL", "-extension_picky", "0"]
    try:
        subprocess.run(
            base_cmd + ["-i", src, "-c", "copy", "-an", disk],
            check=True, timeout=300,
        )
    except Exception:  # noqa: BLE001
        # fall back to re-encoding (handles odd container/codec edge cases)
        try:
            subprocess.run(
                base_cmd + ["-i", src, "-an", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "26", "-movflags", "+faststart", disk],
                check=True, timeout=420,
            )
        except Exception as e:  # noqa: BLE001
            mirror.video_notes.append(f"{vid}: ffmpeg failed ({e}), poster only")
            if os.path.exists(disk):
                os.remove(disk)
            return None
    if os.path.exists(disk) and os.path.getsize(disk) > 0:
        return local_url
    return None


def build_video_html(soup, mp4_url, poster_url, settings, cfg):
    """Responsive 16:9 wrapper containing a native <video> (JS-free)."""
    wrap = soup.new_tag("div", **{"class": "mirror-video-wrap"})
    if mp4_url:
        v = soup.new_tag("video")
        v["class"] = "mirror-video"
        v["controls"] = "controls" if settings.get("controls") else "controls"
        v["playsinline"] = "playsinline"
        v["preload"] = "metadata"
        if settings.get("muted", True):
            v["muted"] = "muted"
        if settings.get("autoPlay"):
            v["autoplay"] = "autoplay"
        if settings.get("loop"):
            v["loop"] = "loop"
        if poster_url:
            v["poster"] = poster_url
        s = soup.new_tag("source", src=mp4_url, type="video/mp4")
        v.append(s)
        wrap.append(v)
    elif poster_url:
        # Poster-only fallback (video could not be downloaded).
        img = soup.new_tag("img", src=poster_url)
        img["class"] = "mirror-video-poster loaded"
        img["alt"] = "Video preview"
        img["data-loaded"] = "true"
        wrap.append(img)
    return wrap


def extract_oembed(block):
    """Return an embed dict {provider,id/url} from a video block, or None."""
    for attr_el in block.find_all(attrs={"data-block-json": True}):
        try:
            d = json.loads(attr_el["data-block-json"])
        except Exception:  # noqa: BLE001
            continue
        url = d.get("url") or (d.get("video") or {}).get("url") if isinstance(d, dict) else None
        if url:
            return {"url": url}
    # look for youtube/vimeo urls anywhere in the block markup
    html = str(block)
    m = re.search(r"https?://[^\"' ]*(?:youtu\.be|youtube\.com|vimeo\.com)[^\"' ]*", html)
    if m:
        return {"url": m.group(0)}
    return None


def embed_iframe(soup, embed):
    url = embed["url"]
    wrap = soup.new_tag("div", **{"class": "mirror-video-wrap"})
    iframe = soup.new_tag("iframe")
    iframe["loading"] = "lazy"
    iframe["class"] = "mirror-video"
    iframe["frameborder"] = "0"
    iframe["allowfullscreen"] = "allowfullscreen"
    iframe["allow"] = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
    yt = re.search(r"(?:youtu\.be/|watch\?v=|embed/)([A-Za-z0-9_-]{6,})", url)
    vm = re.search(r"vimeo\.com/(?:video/)?(\d+)", url)
    if yt:
        iframe["src"] = f"https://www.youtube-nocookie.com/embed/{yt.group(1)}"
    elif vm:
        iframe["src"] = f"https://player.vimeo.com/video/{vm.group(1)}"
    else:
        iframe["src"] = url
    wrap.append(iframe)
    return wrap


def process_forms(soup, mirror):
    """Rebuild Squarespace form blocks (fields injected by JS) as a static, visually
    faithful, inactive form using the block's sqs-form-block-context JSON."""
    for block in soup.select(".sqs-block-form"):
        ctx = block.select_one("script.sqs-form-block-context")
        content = block.select_one(".sqs-block-content") or block
        fields = []
        submit_text = "Submit"
        if ctx and ctx.string:
            try:
                d = json.loads(ctx.string)
                fields = d.get("formFields", []) or []
                submit_text = d.get("formSubmitButtonText") or submit_text
            except Exception:  # noqa: BLE001
                pass
        form_wrap = content.select_one(".sqs-site-style-form") or content
        form_wrap.clear()
        form = soup.new_tag("form", **{"class": "mirror-form", "novalidate": "novalidate"})
        for fl in fields:
            form.append(build_form_field(soup, fl))
        # inactive submit
        btn_field = soup.new_tag("div", **{"class": "mirror-form-field mirror-form-button-wrap"})
        btn = soup.new_tag("button", type="button", disabled="disabled")
        btn["class"] = "mirror-form-submit"
        btn.string = submit_text
        btn_field.append(btn)
        note = soup.new_tag("p", **{"class": "mirror-form-note"})
        note.string = "Contact form is inactive on this preview."
        btn_field.append(note)
        form.append(btn_field)
        form_wrap.append(form)


def build_form_field(soup, fl):
    ftype = fl.get("type", "text")
    title = fl.get("title", "")
    required = fl.get("required", False)
    fid = "f-" + re.sub(r"[^A-Za-z0-9]+", "-", (fl.get("id") or title or "field"))[:40]
    wrap = soup.new_tag("div", **{"class": f"mirror-form-field mirror-form-field-{ftype}"})
    label = soup.new_tag("label")
    label["for"] = fid
    label.string = title + (" *" if required else "")
    wrap.append(label)
    if ftype == "textarea":
        el = soup.new_tag("textarea", id=fid, rows="6", disabled="disabled")
    elif ftype == "name":
        row = soup.new_tag("div", **{"class": "mirror-form-name-row"})
        for sub in ("First Name", "Last Name"):
            i = soup.new_tag("input", type="text", disabled="disabled",
                             placeholder=sub)
            row.append(i)
        wrap.append(row)
        return wrap
    elif ftype in ("email",):
        el = soup.new_tag("input", type="email", id=fid, disabled="disabled")
    elif ftype in ("phone",):
        el = soup.new_tag("input", type="tel", id=fid, disabled="disabled")
    elif ftype in ("select", "dropdown"):
        el = soup.new_tag("select", id=fid, disabled="disabled")
        for opt in (fl.get("options") or []):
            o = soup.new_tag("option")
            o.string = opt if isinstance(opt, str) else opt.get("value", "")
            el.append(o)
    else:
        el = soup.new_tag("input", type="text", id=fid, disabled="disabled")
    if fl.get("placeholder"):
        el["placeholder"] = fl["placeholder"]
    wrap.append(el)
    return wrap


def rewrite_head_meta(soup, page_path, mirror):
    head = soup.find("head")
    # robots noindex
    if not soup.find("meta", attrs={"name": "robots"}):
        m = soup.new_tag("meta")
        m["name"] = "robots"
        m["content"] = "noindex, nofollow"
        head.insert(0, m)
    # canonical
    can = canonical_for(page_path, mirror.base)
    link_can = soup.find("link", rel="canonical")
    if link_can:
        link_can["href"] = can
    else:
        lc = soup.new_tag("link", rel="canonical", href=can)
        head.append(lc)
    # og:url
    og = soup.find("meta", attrs={"property": "og:url"})
    if og:
        og["content"] = can


def inject_head_extras(soup, mirror):
    """Add Google Fonts substitute + mirror overrides CSS (loaded last) + base note."""
    head = soup.find("head")
    gf = soup.new_tag("link", rel="stylesheet", href=GOOGLE_FONTS_HREF)
    head.append(gf)
    ov = soup.new_tag("link", rel="stylesheet",
                      href=f"{mirror.base}/assets/css/mirror-overrides.css")
    head.append(ov)
    mirror.substituted_fonts.add("acumin-pro -> Archivo (Google Fonts)")


def ensure_body_classes(soup):
    """Squarespace adds runtime body state classes; add the closed-menu defaults
    statically so header CSS lays out correctly without JS."""
    body = soup.find("body")
    if not body:
        return
    cls = body.get("class", [])
    for c in ("header--menu-closed",):
        if c not in cls:
            cls.append(c)
    body["class"] = cls


def inject_site_js(soup, mirror):
    body = soup.find("body")
    tag = soup.new_tag("script", src=f"{mirror.base}/assets/js/site.js")
    tag["defer"] = "defer"
    body.append(tag)


# Runtime-only data attributes that carry squarespace URLs but are never fetched
# once the JS is gone. We strip them so the output holds zero live squarespace URLs.
_RUNTIME_ATTR_PREFIXES = ("data-block-css", "data-block-scripts", "data-block-json",
                          "data-block-field-json", "data-controller",
                          "data-image", "data-src", "data-srcset")


def scrub_residual(soup, mirror):
    """Final sweep: rewrite leftover asset URLs (og:image, image_src, poster, etc.)
    to local files and delete any remaining attribute that still holds a live
    squarespace/sqspcdn URL (inert runtime data attributes)."""
    # meta image tags + image_src link -> local image
    for meta in soup.find_all("meta", attrs={"content": True}):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image",
                    "twitter:image:src") and SQSP_HOST_RE.search(meta["content"]):
            meta["content"] = PAGES_HOST + mirror.register_asset(meta["content"].split("?")[0])
    for link in soup.find_all("link", href=True):
        if SQSP_HOST_RE.search(link["href"]):
            rels = [r.lower() for r in (link.get("rel") or [])]
            if any(r in ("image_src", "icon", "shortcut", "apple-touch-icon") for r in rels):
                link["href"] = mirror.register_asset(link["href"].split("?")[0])

    # delete inert runtime data attributes and any attribute still holding a sqsp URL
    for el in soup.find_all(True):
        for attr in list(el.attrs.keys()):
            val = el.attrs[attr]
            sval = " ".join(val) if isinstance(val, list) else str(val)
            if attr in ("src", "href", "poster", "srcset"):
                continue  # real, already-rewritten references
            if attr.startswith(_RUNTIME_ATTR_PREFIXES) or SQSP_HOST_RE.search(sval):
                del el.attrs[attr]


def process_page(html, page_path, mirror):
    soup = BeautifulSoup(html, "lxml")
    links = discover_links(soup)

    rewrite_head_meta(soup, page_path, mirror)
    process_stylesheets(soup, mirror)
    process_block_css(soup, mirror)
    process_images(soup, mirror)
    process_video_blocks(soup, mirror)
    process_forms(soup, mirror)          # must read block-context JSON BEFORE scripts are stripped
    strip_runtime(soup)                  # now remove all runtime JS + chrome
    process_inline_styles(soup, mirror, SRC_ORIGIN + page_path)
    rewrite_links(soup, mirror)
    ensure_body_classes(soup)
    inject_head_extras(soup, mirror)
    inject_site_js(soup, mirror)
    scrub_residual(soup, mirror)         # rewrite/strip any leftover squarespace URLs

    return soup, links


# --------------------------------------------------------------------------- #
# Static support files
# --------------------------------------------------------------------------- #

SITE_JS = """\
/* hauscr.org static mirror - minimal vanilla header/mobile-nav behavior.
   Squarespace's runtime JS is stripped; this reproduces only what the header
   needs: burger toggle, folder drill-down, back control, and Escape-to-close.
   Desktop folder dropdowns are pure CSS :hover and need no JS. */
(function () {
  var body = document.body;
  var header = document.querySelector('.header');

  function openMenu() {
    body.classList.add('header--menu-open');
    body.classList.remove('header--menu-closed');
    if (header) header.classList.add('header--menu-open');
    resetFolders();
  }
  function closeMenu() {
    body.classList.remove('header--menu-open');
    body.classList.add('header--menu-closed');
    if (header) header.classList.remove('header--menu-open');
    resetFolders();
  }
  function isOpen() { return body.classList.contains('header--menu-open'); }

  function resetFolders() {
    var panes = document.querySelectorAll('.header-menu-nav-folder');
    panes.forEach(function (p) {
      if (p.getAttribute('data-folder') === 'root') p.classList.add('header-menu-nav-folder--active');
      else p.classList.remove('header-menu-nav-folder--active');
    });
  }
  function openFolder(id) {
    var pane = document.querySelector('.header-menu-nav-folder[data-folder="' + id + '"]');
    if (!pane) return;
    var root = document.querySelector('.header-menu-nav-folder[data-folder="root"]');
    if (root) root.classList.remove('header-menu-nav-folder--active');
    pane.classList.add('header-menu-nav-folder--active');
  }

  document.querySelectorAll('.header-burger-btn').forEach(function (b) {
    b.addEventListener('click', function (e) {
      e.preventDefault();
      isOpen() ? closeMenu() : openMenu();
    });
  });

  // Folder drill-down (mobile overlay): intercept folder-title links.
  document.querySelectorAll('.header-menu a[data-folder-id]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      openFolder(a.getAttribute('data-folder-id'));
    });
  });

  // Back controls return to the root pane.
  document.querySelectorAll('.header-menu [data-action="back"]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      resetFolders();
    });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && isOpen()) closeMenu();
  });

  resetFolders();
})();
"""

MIRROR_OVERRIDES_CSS = """\
/* mirror-overrides.css - fixes for serving the Squarespace markup without its
   runtime JavaScript. Loaded last so these rules win the cascade. */

/* 1. Substitute the domain-locked Typekit heading font (acumin-pro) with a free
      Google Fonts equivalent (Archivo). Body text (Poppins) is self-hosted. */
:root {
  --heading-font-font-family: 'Archivo', 'Helvetica Neue', Helvetica, Arial, sans-serif !important;
}

/* 2. Squarespace hides images until its lazy-loader marks them .loaded / [data-loaded].
      We set those statically in mirror.py; force visibility here as a safety net. */
img { opacity: 1 !important; }
.sqs-gallery-block-grid img,
.sqs-gallery-design-autocolumns-slide img,
[data-loader],
img:not(.loaded) { opacity: 1 !important; }
[data-loaded] { opacity: 1 !important; }

/* 3. Full-bleed section backgrounds render even without the JS background loader. */
.section-background img,
.section-background .content-fill {
  opacity: 1 !important;
  display: block;
}

/* 4. Responsive 16:9 video/embed wrapper for ported video blocks. */
.mirror-video-wrap {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  background: #000;
  overflow: hidden;
}
.mirror-video-wrap .mirror-video,
.mirror-video-wrap iframe,
.mirror-video-wrap .mirror-video-poster {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: 0;
  object-fit: cover;
  display: block;
}

/* 5. Inactive contact form: keep the visual layout, show it is disabled. */
.mirror-form { display: flex; flex-direction: column; gap: 1.1em; width: 100%; }
.mirror-form-field { display: flex; flex-direction: column; gap: .4em; }
.mirror-form-field label { font-weight: 600; font-size: .95em; }
.mirror-form-field input,
.mirror-form-field textarea,
.mirror-form-field select {
  width: 100%;
  padding: .7em .8em;
  border: 1px solid rgba(0,0,0,.35);
  background: rgba(0,0,0,.02);
  font: inherit;
  color: inherit;
  box-sizing: border-box;
}
.mirror-form-name-row { display: flex; gap: .8em; }
.mirror-form-name-row input { flex: 1; }
.mirror-form-submit {
  padding: .8em 2em;
  border: 0;
  background: #999;
  color: #fff;
  font: inherit;
  cursor: not-allowed;
  opacity: .8;
}
.mirror-form-note { font-size: .85em; opacity: .75; margin: .5em 0 0; font-style: italic; }
"""


def write_support_files(mirror):
    out = mirror.out
    with open(os.path.join(out, "assets", "js", "site.js"), "w", encoding="utf-8") as f:
        f.write(SITE_JS)
    with open(os.path.join(out, "assets", "css", "mirror-overrides.css"), "w", encoding="utf-8") as f:
        f.write(MIRROR_OVERRIDES_CSS)
    # .nojekyll so GitHub Pages serves _-prefixed and asset dirs verbatim
    open(os.path.join(out, ".nojekyll"), "w").close()
    # 404
    with open(os.path.join(out, "404.html"), "w", encoding="utf-8") as f:
        f.write(NOT_FOUND_HTML.replace("__BASE__", mirror.base))


NOT_FOUND_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Page not found - HAUSCR (preview)</title>
<style>
  body{font-family:'Archivo','Helvetica Neue',Arial,sans-serif;background:#111;color:#fff;
       display:flex;min-height:100vh;margin:0;align-items:center;justify-content:center;text-align:center}
  a{color:#ffd45e}
  .box{padding:2rem}
  h1{font-size:3rem;margin:0 0 .5rem}
</style></head>
<body><div class="box">
  <h1>404</h1>
  <p>This page does not exist in the HAUSCR preview mirror.</p>
  <p><a href="__BASE__/">Return to the home page</a></p>
</div></body></html>
"""


REDIRECT_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta http-equiv="refresh" content="0; url=__TARGET__">
<link rel="canonical" href="__CANON__">
<title>Redirecting…</title>
</head><body>
<p>This section opens at <a href="__TARGET__">its first page</a>.</p>
<script>location.replace("__TARGET__");</script>
</body></html>
"""


def write_redirect_page(page_path, target_path, mirror):
    target_route = route_for(target_path, mirror.base)
    html = (REDIRECT_HTML
            .replace("__TARGET__", target_route)
            .replace("__CANON__", canonical_for(page_path, mirror.base)))
    dest = out_file_for(page_path, mirror.out)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(html)


# --------------------------------------------------------------------------- #
# Crawl orchestration
# --------------------------------------------------------------------------- #

def crawl(mirror):
    queue = [normalize_path(p) for p in SEED_PATHS]
    seen = set()
    ordered = []
    # seed order first, then discovered
    for p in queue:
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    processed = []
    i = 0
    while i < len(ordered) and len(processed) < MAX_PAGES:
        path = ordered[i]
        i += 1

        # Folder redirects: write a stub, do not fetch content.
        if path in FOLDER_REDIRECTS:
            write_redirect_page(path, FOLDER_REDIRECTS[path], mirror)
            processed.append(path)
            print(f"  [redirect] {path} -> {FOLDER_REDIRECTS[path]}")
            continue

        url = SRC_ORIGIN + (path if path != "/" else "/")
        print(f"  [{len(processed)+1}] fetching {url}")
        html = fetch(url)
        if html is None:
            print(f"    ! skipped (no content) {path}")
            continue

        soup, links = process_page(html, path, mirror)

        dest = out_file_for(path, mirror.out)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write("<!doctype html>\n" + str(soup))
        processed.append(path)

        # enqueue newly discovered internal links
        for lk in links:
            if lk in FOLDER_REDIRECTS or lk not in seen:
                if lk not in seen:
                    seen.add(lk)
                    ordered.append(lk)

        time.sleep(0.25)  # be polite

    return processed


def main():
    ap = argparse.ArgumentParser(description="Static mirror of hauscr.org for GitHub Pages preview.")
    ap.add_argument("--base", default="/hauscr-website", help="GitHub Pages base path prefix")
    ap.add_argument("--out", default="docs", help="output directory")
    args = ap.parse_args()

    mirror = Mirror(args.base, args.out)
    print(f"Mirroring {SRC_ORIGIN} -> {args.out}/ (base={args.base})")

    print("Crawling pages...")
    pages = crawl(mirror)

    print("Downloading assets...")
    mirror.download_all()

    print("Rewriting CSS url() references...")
    mirror.process_css_files()

    print("Writing support files (.nojekyll, 404, site.js, overrides)...")
    write_support_files(mirror)

    print(f"\nDone: {len(pages)} pages, {len(mirror.downloads)} unique assets.")
    if mirror.substituted_fonts:
        print("Substituted fonts:", ", ".join(sorted(mirror.substituted_fonts)))
    if mirror.video_notes:
        print("Video notes:", "; ".join(mirror.video_notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
