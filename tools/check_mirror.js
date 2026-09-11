/*
 * check_mirror.js - self-verification for the hauscr.org static mirror.
 *
 * Serves docs/ under the GitHub Pages base path (/hauscr-website), then loads
 * every generated page in headless Chrome at desktop (1280x900) and mobile
 * (390x844) and asserts:
 *   - the page responds HTTP 200
 *   - zero failed network requests (no 404s)
 *   - zero requests to any squarespace.com / sqspcdn.com / squarespace-cdn.com /
 *     typekit.net host (allowed externals: youtube[-nocookie].com, vimeo.com,
 *     fonts.googleapis.com, fonts.gstatic.com)
 *   - every <img> in the viewport has naturalWidth > 0 after load
 *   - clicking the burger opens the mobile menu (the menu becomes visible)
 * A desktop screenshot per page is written to .shots/ (git-ignored; override with SHOTS_DIR).
 *
 * Playwright is resolved from NODE_PATH (see README) or a local install.
 * Usage: NODE_PATH=/path/to/node_modules node tools/check_mirror.js
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const BASE = '/hauscr-website';
const REPO = path.resolve(__dirname, '..');
const DOCS = path.join(REPO, 'docs');
const PORT = 8731;
const SHOTS = process.env.SHOTS_DIR ||
  path.join(REPO, '.shots');
require('fs').mkdirSync(SHOTS, { recursive: true });

const FORBIDDEN = /(squarespace\.com|sqspcdn\.com|squarespace-cdn\.com|typekit\.net)/i;
const ALLOWED_EXTERNAL = /(youtube-nocookie\.com|youtube\.com|youtu\.be|vimeo\.com|fonts\.googleapis\.com|fonts\.gstatic\.com)/i;

const MIME = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.json': 'application/json',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
  '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff',
  '.ttf': 'font/ttf', '.otf': 'font/otf', '.eot': 'application/vnd.ms-fontobject',
  '.mp4': 'video/mp4', '.avif': 'image/avif',
};

function resolveFile(urlPath) {
  let p = decodeURIComponent(urlPath.split('?')[0].split('#')[0]);
  if (p.startsWith(BASE)) p = p.slice(BASE.length);
  if (!p || p === '/') p = '/index.html';
  let disk = path.join(DOCS, p);
  if (!path.extname(disk)) disk = path.join(disk, 'index.html');
  return disk;
}

function startServer() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const disk = resolveFile(req.url);
      fs.readFile(disk, (err, data) => {
        if (err) {
          // serve 404.html with a 404 status for unknown routes
          const nf = path.join(DOCS, '404.html');
          if (fs.existsSync(nf)) {
            res.writeHead(404, { 'Content-Type': 'text/html; charset=utf-8' });
            res.end(fs.readFileSync(nf));
          } else {
            res.writeHead(404); res.end('not found');
          }
          return;
        }
        res.writeHead(200, { 'Content-Type': MIME[path.extname(disk).toLowerCase()] || 'application/octet-stream' });
        res.end(data);
      });
    });
    server.listen(PORT, () => resolve(server));
  });
}

function findPages() {
  // every docs/**/index.html -> its base-prefixed route
  const routes = [];
  (function walk(dir) {
    for (const name of fs.readdirSync(dir)) {
      const full = path.join(dir, name);
      const st = fs.statSync(full);
      if (st.isDirectory()) {
        if (name === 'assets') continue;
        walk(full);
      } else if (name === 'index.html') {
        const rel = path.relative(DOCS, path.dirname(full)).split(path.sep).join('/');
        routes.push(rel ? `${BASE}/${rel}/` : `${BASE}/`);
      }
    }
  })(DOCS);
  return routes.sort();
}

function slugOf(route) {
  const s = route.replace(BASE, '').replace(/^\/|\/$/g, '');
  return s || 'home';
}

async function checkPage(browser, origin, route, viewport, doShot) {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  const failed = [];
  const forbidden = [];

  page.on('requestfailed', (r) => {
    const errText = (r.failure() && r.failure().errorText) || 'failed';
    // Browsers abort <video> range/preload requests once metadata is read; these
    // ERR_ABORTED media cancellations are benign and not missing-asset errors.
    if (errText.includes('ERR_ABORTED') && (r.resourceType() === 'media' || /\.mp4($|\?)/i.test(r.url()))) return;
    failed.push(`${r.url()} (${errText})`);
  });
  page.on('response', (resp) => {
    const u = resp.url();
    if (FORBIDDEN.test(u)) forbidden.push(`${resp.status()} ${u}`);
    if (resp.status() >= 400 && u.startsWith(origin)) failed.push(`${resp.status()} ${u}`);
  });
  page.on('request', (r) => {
    const u = r.url();
    if (FORBIDDEN.test(u) && !ALLOWED_EXTERNAL.test(u)) forbidden.push(`REQ ${u}`);
  });

  // domcontentloaded first (redirect stubs call location.replace and never reach
  // networkidle on the original URL); then a bounded idle wait, tolerating redirects.
  const resp = await page.goto(origin + route, { waitUntil: 'domcontentloaded', timeout: 45000 });
  const status = resp ? resp.status() : 0;
  try { await page.waitForLoadState('networkidle', { timeout: 12000 }); } catch (e) { /* redirect or slow media */ }
  await page.waitForTimeout(400);

  // Scroll through the full page so native lazy-loaded images decode, then
  // return to top. This makes the screenshot complete and lets us verify every
  // image (in-viewport and below-fold), not just those above the initial fold.
  await page.evaluate(async () => {
    const step = Math.round(window.innerHeight * 0.8);
    for (let y = 0; y <= document.body.scrollHeight; y += step) {
      window.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 120));
    }
    window.scrollTo(0, 0);
  });
  await page.waitForTimeout(500);

  // every rendered <img> must have decoded (naturalWidth > 0)
  const badImgs = await page.evaluate(() => {
    const out = [];
    for (const img of document.querySelectorAll('img')) {
      const r = img.getBoundingClientRect();
      const rendered = r.width > 1 && r.height > 1;
      if (rendered && img.naturalWidth === 0) out.push(img.currentSrc || img.src);
    }
    return out;
  });

  // mobile menu open test (only meaningful at mobile width where burger shows)
  let menuOk = null;
  if (viewport.width < 800) {
    const hasEls = await page.evaluate(() => {
      const burger = document.querySelector('.header-burger-btn');
      const menu = document.querySelector('.header-menu');
      if (!burger || !menu) return false;
      burger.click();
      return true;
    });
    if (!hasEls) {
      menuOk = 'no-burger-or-menu';
    } else {
      await page.waitForTimeout(800); // allow the 600ms visibility/opacity transition
      menuOk = await page.evaluate(() => {
        const menu = document.querySelector('.header-menu');
        const cs = getComputedStyle(menu);
        const visible = cs.visibility === 'visible' && parseFloat(cs.opacity) > 0.5
          && document.body.classList.contains('header--menu-open');
        return visible ? 'ok' : `not-visible(vis=${cs.visibility},op=${cs.opacity},body=${document.body.className.includes('menu-open')})`;
      });
    }
  }

  if (doShot) {
    fs.mkdirSync(SHOTS, { recursive: true });
    await page.screenshot({ path: path.join(SHOTS, `${slugOf(route)}.png`), fullPage: true });
  }

  await ctx.close();
  return { status, failed, forbidden, badImgs, menuOk };
}

(async () => {
  const server = await startServer();
  const origin = `http://localhost:${PORT}`;
  const routes = findPages();
  console.log(`Serving ${DOCS} at ${origin}${BASE}/  (${routes.length} pages)`);

  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  let problems = 0;

  for (const route of routes) {
    const d = await checkPage(browser, origin, route, { width: 1280, height: 900 }, true);
    const m = await checkPage(browser, origin, route, { width: 390, height: 844 }, false);

    const errs = [];
    if (d.status !== 200) errs.push(`desktop status ${d.status}`);
    if (m.status !== 200) errs.push(`mobile status ${m.status}`);
    const forb = [...new Set([...d.forbidden, ...m.forbidden])];
    if (forb.length) errs.push(`FORBIDDEN host requests: ${forb.slice(0, 4).join(' | ')}`);
    const fails = [...new Set([...d.failed, ...m.failed])];
    if (fails.length) errs.push(`failed requests: ${fails.slice(0, 4).join(' | ')}`);
    if (d.badImgs.length) errs.push(`broken imgs(desktop): ${d.badImgs.slice(0, 3).join(', ')}`);
    if (m.menuOk && m.menuOk !== 'ok') errs.push(`mobile menu: ${m.menuOk}`);

    if (errs.length) {
      problems++;
      console.log(`  FAIL ${route}`);
      errs.forEach((e) => console.log(`       - ${e}`));
    } else {
      console.log(`  ok   ${route}  (menu=${m.menuOk})`);
    }
  }

  await browser.close();
  server.close();

  if (problems) {
    console.log(`\n${problems} page(s) with problems.`);
    process.exit(1);
  }
  console.log('\nAll pages passed.');
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(2); });
