# HAUSCR website — static development mirror

This repository holds a **faithful static mirror** of the public
[hauscr.org](https://www.hauscr.org) website (Harvard Undergraduate Association
for U.S. Cross-Cultural Relations), snapshotted **2026-09-10**, generated so the
site can be served from **GitHub Pages as a development preview**.

> **Source of truth:** the live Squarespace site at `https://www.hauscr.org`
> remains authoritative until an eventual cutover. This mirror is a point-in-time
> snapshot for development/preview only. It carries `noindex, nofollow` on every
> page so it never competes with the live site in search.

**Preview URL:**
`https://harvard-auscr.github.io/hauscr-website/`

The generated site lives in [`docs/`](docs/) and is served from `main:/docs`.

---

## What's in here

| Path | What it is |
|------|------------|
| `mirror.py` | The generator. Crawls the live site, downloads assets, strips Squarespace runtime JS, rewrites every URL to a local base-prefixed path, and writes `docs/`. |
| `requirements.txt` | Python dependencies for `mirror.py`. |
| `docs/` | The generated static site (this is what GitHub Pages serves). |
| `docs/assets/` | Self-hosted `css/`, `img/`, `fonts/`, `files/`, `video/`, and `js/site.js`. |
| `docs/.nojekyll` | Tells GitHub Pages to serve the tree verbatim (no Jekyll processing). |
| `docs/404.html` | Simple not-found page linking back to the preview home. |
| `tools/check_mirror.js` | Playwright self-verification (serves `docs/` and audits every page). |

---

## Regenerating the mirror

```bash
# 1. install Python deps
python -m pip install -r requirements.txt
#    (ffmpeg must be on PATH to re-port the native/HLS video blocks)

# 2. regenerate docs/ (idempotent — already-downloaded assets are skipped)
python mirror.py --base /hauscr-website --out docs
```

On Windows **Git Bash**, prefix the command with `MSYS_NO_PATHCONV=1` so the
leading-slash `--base /hauscr-website` argument is not rewritten into a Windows
path:

```bash
MSYS_NO_PATHCONV=1 python mirror.py --base /hauscr-website --out docs
```

### Serving from a custom domain

GitHub Pages serves a custom domain at the domain root, so the base path must be
empty. `--cname` writes `docs/CNAME` (Pages reads that file to bind the domain)
and switches the canonical / `og:url` origin to that host:

```bash
python mirror.py --base "" --cname dev.hauscr.org --out docs
```

A run without `--cname` deletes a stale `docs/CNAME`, so a plain regeneration
always puts the site back on the `github.io` sub-path. `--pages-host` sets the
canonical origin on its own if it ever differs from the CNAME host. Binding the
domain also needs a DNS record (`dev` CNAME → `harvard-auscr.github.io`) and the
repository's Pages setting (`gh api -X PUT repos/harvard-auscr/hauscr-website/pages
-f cname=dev.hauscr.org`). Pages are `noindex` on either host.

The run is **idempotent**: assets already present on disk are not re-downloaded,
so re-running only refreshes the HTML/CSS rewriting and picks up anything new.
Cached stylesheets are re-prefixed for the current `--base`, and asset files the
snapshot no longer references (Squarespace rotates its CSS hashes) are deleted at
the end of the run and listed in the output.
The scrape is strictly **read-only** toward Squarespace (polite: ≤6 concurrent
downloads, browser User-Agent, retry with backoff — nothing on the live site is
ever modified).

### How the base path works

GitHub Pages serves this project at a sub-path (`/hauscr-website/`), not at a
domain root. So **every** root-relative URL in the output is prefixed with that
base path — links become `/hauscr-website/executives/`, assets become
`/hauscr-website/assets/img/...`, etc. Change the prefix with `--base` if the
repository (and therefore the Pages sub-path) is ever renamed, or pass an empty
`--base ""` when the site is served from a domain root (see custom domain above).

Internal links are normalized to end in `/` (so `/about-us` →
`/hauscr-website/about-us/`), which resolves to that folder's `index.html`.

---

## Verifying the mirror

`tools/check_mirror.js` starts a tiny static server that maps
`/hauscr-website/*` → `docs/*`, then loads **every** generated page in headless
Chrome at desktop (1280×900) and mobile (390×844) and asserts:

- the page responds **HTTP 200**;
- **zero failed network requests** (no 404s);
- **zero requests** to any `squarespace.com` / `sqspcdn.com` /
  `squarespace-cdn.com` / `typekit.net` host (allowed externals:
  `youtube[-nocookie].com`, `vimeo.com`, `fonts.googleapis.com`,
  `fonts.gstatic.com`);
- every rendered `<img>` decoded (`naturalWidth > 0`) after a full-page scroll;
- the burger button opens the mobile menu.

```bash
# Playwright is not vendored here (node_modules is git-ignored). Either install it:
npm init -y && npm install playwright   # then: node tools/check_mirror.js
# ...or point NODE_PATH at an existing Playwright install:
NODE_PATH=/path/to/node_modules node tools/check_mirror.js
```

The checker launches system Google Chrome via
`chromium.launch({ channel: 'chrome' })`.

---

## Known gaps

Everything below is either impossible to port faithfully from a Squarespace
snapshot, or is runtime-only behavior that was intentionally dropped. Content is
always kept visible in a reasonable form rather than dropped silently.

**Fonts**
- **Headings** use `acumin-pro`, an **Adobe Typekit** font served from a
  domain-locked kit (`use.typekit.net`) that **cannot be rehosted**. It is
  substituted with the closest free **Google Fonts** equivalent, **Archivo**
  (loaded from `fonts.googleapis.com`, mapped in
  `docs/assets/css/mirror-overrides.css`). Headings therefore look very close
  but not pixel-identical to the live site.
- **Body text** uses **Poppins**, which Squarespace serves from a downloadable
  library — it is **self-hosted faithfully** in `docs/assets/fonts/`.

**Contact form** (`/contactus`)
- Squarespace forms POST to Squarespace and cannot function on a static host.
  The form is rebuilt with its real field layout (Name / Email / Message) but
  the inputs are **disabled**, the submit button is inert, and a line reads
  *"Contact form is inactive on this preview."*

**Video blocks**
- **HSYLC / Conferences** videos are **YouTube** embeds, ported to responsive
  16:9 `youtube-nocookie.com` `<iframe>`s. (One HSYLC embed's `?start=2` offset
  is not preserved.)
- **BPC** and **CTB** use Squarespace **native (HLS) video**. These are
  downloaded with `ffmpeg` to a self-hosted **360p MP4** and played with a
  native `<video>` element (muted, autoplay, loop — matching the live site's
  settings) over the original thumbnail poster. **Audio is dropped** (these
  videos autoplay muted on the live site regardless). If `ffmpeg` is
  unavailable at generation time, the block degrades to a poster image.

**Scrolling ticker & photo reel** (Fluid Engine components)
- The **Marquee** scrolling-ticker blocks (homepage "6 Annual Conferences",
  `/ctb`, `/hsylc`) ship an empty SVG `<path>` whose geometry Squarespace's
  runtime JS computes from container/font metrics; without that JS the block
  collapses to zero height. `mirror.py` rebuilds each ticker from its
  `data-marquee-items` text as a duplicated, **CSS-animated** scrolling track
  (`mirror-overrides.css`), so the heading text is visible and scrolls. The
  exact JS-driven wave path and per-block speed easing are **not** reproduced.
- The **gallery-reel** photo slideshows (`/apply`) rely on runtime JS to lay out
  their absolutely-stacked, `display:none` items into a draggable one-at-a-time
  reel. `mirror-overrides.css` re-flows them into a **horizontally-scrollable
  filmstrip** with every photo visible; the prev/next arrows scroll it one photo
  per click (`site.js`, wrapping at either end). The
  drag/one-slide-at-a-time interaction and the click-to-zoom **lightbox** overlay
  are dropped (the lightbox markup stays hidden, matching its default state).

**Text highlights** (curved / scribble underlines, circles, markers)
- Squarespace draws these at runtime from a per-block `TextAttributes-props`
  JSON blob (e.g. the red curved underline under "Applications for 2026-2027
  have opened!" on `/apply`). `mirror.py` turns each one into a static CSS
  rule on the `.sqsrte-text-highlight` span: an SVG background underline in the
  site accent color that repeats per line box. Shape, thickness and color are
  honored; the draw-on animation is not.

**Folder pages**
- `/conferences` and `/about-us` are Squarespace **nav folders** that 302-redirect
  to their first child on the live site. They are reproduced as **redirect
  stubs** (`meta refresh` + `location.replace`) pointing at
  `/hauscr-website/hsylc/` and `/hauscr-website/about-huauscr/` respectively.

**Runtime-only features dropped** (all Squarespace runtime JS is stripped)
- Site search, shopping cart, cookie-consent banner, and announcement bar.
- Image hover / scroll / parallax effects (the Fluid image "effects" engine),
  image lightbox zoom, and animated section reveals — images render statically.
- Auto-fitting "scaled text" — text renders at its normal wrapped size.
- All analytics and the Adobe Typekit loader.

**Scope**
- Crawl is capped at 40 pages (16 real pages were captured). `/cart`, `/search`,
  query-string variants, and pure anchors are excluded.
- Content is a **snapshot as of 2026-09-10**; anything edited on the live
  Squarespace site after that date will not appear here until the mirror is
  regenerated.
