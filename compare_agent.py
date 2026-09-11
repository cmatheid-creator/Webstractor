"""Dev-vs-live comparison agent (pipeline step 9, QA) -- catches both
content-level regressions (missing headings/text/images) and the class of
*layout* regression a purely structural diff is blind to: which side an
image sits on, whether a hero/banner's text panel is there, and whether a
"form" is real inputs or a still-unswapped placeholder panel.

Why this exists: three earlier passes (see dev-vs-live-punchlist.md
"Pass 2"/"Pass 4") compared heading text, body-text length, and image/
button *counts* between the live GoDaddy site and the migrated WP dev
site. That approach never once flagged four real regressions Carver found
by eye in one pass: the home hero's white text panel and overlay strength,
image-left/right alternation on /services and /about, the About founder
photo's column width, and the Contact page's form being a placeholder
panel instead of real inputs. None of those change a heading, a body-text
length, or an image count -- the content is identical, only its *layout*
or *functional behavior* differs. This script adds the checks that would
have caught them, plus a real pixel-level screenshot diff as a catch-all
for whatever the structural checks still don't think to name.

Requires Pillow (`pip install pillow`) for the visual diff; everything
else is Playwright, already a project dependency.

Usage:
    python3 compare_agent.py                  # all pages in structured_content.json
    python3 compare_agent.py services about    # just these slugs

Output goes to ./comparison-output/ (gitignored -- screenshots and diff
images are large, regenerated binary artifacts, not something to commit;
write up real findings into dev-vs-live-punchlist.md by hand, the way
every prior pass has):
    compare_result.json   -- full structured data for every pair
    report.md             -- flagged-pages summary + a visual-diff ranking
                             (informational, see VISUAL_DIFF_FLAG_THRESHOLD)
    shots/<slug>__live.png, <slug>__dev.png
    diff/<slug>.png        -- every pair: [live | dev | red-highlighted
                              diff] side by side, vertically aligned first
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    from PIL import Image, ImageChops
except ImportError:
    Image = None  # visual diff step is skipped with a warning; see main()

LIVE_BASE = "https://stratecon.tech"
DEV_BASE = "https://dev.stratecon.tech"
STRUCTURED_CONTENT = Path(__file__).parent / "structured_content.json"
OUT_DIR = Path(__file__).parent / "comparison-output"
SHOT_DIR = OUT_DIR / "shots"
DIFF_DIR = OUT_DIR / "diff"

VIEWPORT = {"width": 1440, "height": 1600}

# Purely a report-ranking cutoff, NOT a pass/fail gate -- see diff_page()'s
# note on why raw pixel-diff percentages run high even on pages confirmed
# by eye to match (cross-platform rendering never lines up exactly, and a
# single global vertical alignment can't correct for spacing that diverges
# at more than one point down a page). Bolds a page in the visual-diff
# ranking table above this score so it's easy to spot; every page's score
# is listed and every diff image saved regardless.
VISUAL_DIFF_FLAG_THRESHOLD = 0.30  # fraction of compared pixels

# Chrome (nav/header/footer) on either platform -- GoDaddy Website
# Builder's markup (see crawler_agent.py's CHROME_SELECTOR) and
# WordPress/Twenty Twenty-Four's. A single selector string works on
# whichever page evaluate() currently runs against.
CHROME_SELECTOR = (
    'nav, header, footer, [data-ux="Header"], [role="contentinfo"], '
    '[role="navigation"], [data-aid="FOOTER_COOKIE_BANNER_RENDERED"], '
    ".wp-block-template-part, #wpadminbar"
)


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


def dev_path(page_record):
    """The migrated page's path on dev.stratecon.tech for a
    structured_content.json page record -- see generator_agent.py's
    slugify(): the last path segment becomes the WP slug, home is `/`."""
    slug = page_record["slug"]
    if page_record.get("is_front_page") or slug == "home":
        return "/"
    return f"/{slug}/"


# ---------------------------------------------------------------------
# In-page extraction. Runs identically against the live and dev DOM --
# every selector here has to make sense on both a GoDaddy Website Builder
# page and a WordPress/Twenty-Four page, which is why chrome exclusion,
# heading/image counting, and the layout checks below all key off generic
# rendered geometry (bounding rects, computed style) rather than either
# platform's own markup vocabulary.
# ---------------------------------------------------------------------
EXTRACT_JS = r"""(chromeSel) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const chromeEls = [...document.querySelectorAll(chromeSel)];
  const inChrome = el => chromeEls.some(c => c.contains(el));

  const bodyText = norm(document.body.innerText);
  let chromeText = '';
  for (const c of chromeEls) chromeText += ' ' + norm(c.innerText || '');
  const contentLen = Math.max(0, bodyText.length - norm(chromeText).length);

  const heads = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
    .filter(h => !inChrome(h))
    .map(h => ({ l: +h.tagName[1], t: norm(h.textContent) }))
    .filter(h => h.t && h.t.length < 200);

  const imgs = [...document.querySelectorAll('img')].filter(i => !inChrome(i));
  const bgImgs = [...document.querySelectorAll('[data-ux="Background"], [style*="background-image"], [data-aid*="IMAGE_RENDERED"], .wp-block-cover')]
    .filter(e => !inChrome(e) && /url\(|wp-block-cover/.test((getComputedStyle(e).backgroundImage || '') + ' ' + e.className));

  const buttons = [...document.querySelectorAll('a[data-ux*="Button"], a[data-ux-btn], [data-ux="ContentCardButton"], .wp-block-button__link, a.wp-element-button')]
    .filter(b => !inChrome(b)).map(b => norm(b.textContent)).filter(t => t && t.length < 40);

  // --- layout check 1: side-by-side image+text sections ---
  // GoDaddy: a <div data-ux="Grid"> with exactly two <div data-ux=
  // "GridCell"> children, one image-dominant, one text-dominant (see
  // crawler_agent.py's mark_media_text_pairs()). WordPress: a
  // core/media-text block. Both end up as a plain two-column flex/grid
  // row at render time, so rather than maintaining two DOM matchers this
  // finds two-child rows generically and classifies each child by
  // content, then reads the *rendered* geometry -- not DOM order, which
  // GoDaddy's flex-direction:row-reverse makes meaningless (confirmed:
  // the image is always the first DOM child regardless of which side it
  // renders on).
  const rowCandidates = [
    ...document.querySelectorAll('[data-ux="Grid"], .wp-block-media-text'),
  ];
  const mediaText = [];
  for (const row of rowCandidates) {
    if (inChrome(row)) continue;
    let cells;
    if (row.matches('.wp-block-media-text')) {
      const fig = row.querySelector(':scope > .wp-block-media-text__media');
      const content = row.querySelector(':scope > .wp-block-media-text__content');
      cells = [fig, content].filter(Boolean);
    } else {
      cells = [...row.children].filter(c => c.matches('[data-ux="GridCell"]'));
    }
    if (cells.length !== 2) continue;
    let imgCell = null, textCell = null;
    for (const cell of cells) {
      const img = cell.querySelector('img');
      const textLen = norm(cell.textContent).length;
      if (img && textLen < 40) imgCell = cell;
      else if (textLen >= 40) textCell = cell;
    }
    if (!imgCell || !textCell) continue;
    const heading = (textCell.querySelector('h1,h2,h3,h4,h5,h6') || {}).textContent || '';
    const firstPara = (textCell.querySelector('p') || {}).textContent || '';
    const key = norm(heading).toLowerCase() || norm(firstPara).toLowerCase().slice(0, 60);
    if (!key) continue;
    const ir = imgCell.getBoundingClientRect(), tr = textCell.getBoundingClientRect();
    if (ir.width < 20 || tr.width < 20) continue;  // hidden/mobile-only duplicate markup
    const totalW = ir.width + tr.width;
    mediaText.push({
      key,
      side: ir.left < tr.left ? 'left' : 'right',
      width_pct: totalW > 0 ? Math.round((ir.width / totalW) * 100) : 50,
    });
  }

  // --- layout check 2: hero/banner text panel + photo overlay ---
  // Generalized version of crawler_agent.py's extract_hero()/
  // extract_page_banner() box + dim detection, run against whichever
  // <h1> is the page's real content heading (not inside chrome).
  // GoDaddy Website Builder bundles the home page's real hero -- its
  // only true page-level <h1> -- inside the same header widget
  // ([data-ux="Header"]) that also holds the logo and nav (confirmed in
  // crawler_agent.py's extract_hero(), which has to dig it out
  // explicitly for exactly this reason). Excluding all of chromeSel
  // here would silently find no <h1> at all on the home page and skip
  // this whole check there -- so an <h1> inside [data-ux="Header"]
  // specifically is allowed through; everything else chrome still
  // excludes (nav links, footer, cookie banner, ...).
  const mainH1 = [...document.querySelectorAll('h1')].find(h => {
    if (!inChrome(h)) return true;
    return h.closest('[data-ux="Header"]') && !h.closest('nav, footer, [role="contentinfo"]');
  });
  let hero = null;
  if (mainH1) {
    // Two different techniques render "a photo behind this heading",
    // confirmed across the live GoDaddy pages and their migrated
    // WordPress equivalents, and a hero/banner has to be recognized
    // under *either* one or every dev page silently reads as
    // "no photo background at all": GoDaddy bakes the photo into a CSS
    // background-image (optionally with a leading
    // linear-gradient(rgba(...)) dim baked into that same property);
    // WordPress's core/cover instead renders a plain <img> sibling for
    // the photo and applies the dim as a *separate* sibling
    // <span class="wp-block-cover__background"> with a solid
    // background-color and its own low CSS opacity. Whichever is found
    // first walking up from the heading wins; both are checked at each
    // level before moving up, so a WP cover one level up doesn't lose to
    // a coincidental unrelated background-image two levels further up.
    let bgPhoto = false, dimAlpha = null, photoContainer = null;
    let el = mainH1;
    for (let i = 0; i < 8 && el; i++) {
      const bi = getComputedStyle(el).backgroundImage;
      if (bi && bi !== 'none' && /url\(/.test(bi)) {
        bgPhoto = true;
        photoContainer = el;
        const dm = bi.match(/rgba?\([^)]*,\s*([\d.]+)\)/);
        if (dm) dimAlpha = parseFloat(dm[1]);
        break;
      }
      const er = el.getBoundingClientRect();
      const coverImg = [...el.querySelectorAll('img')].find(img => {
        const ir = img.getBoundingClientRect();
        return er.width > 0 && er.height > 0
          && ir.width >= er.width * 0.9 && ir.height >= er.height * 0.9;
      });
      if (coverImg) {
        bgPhoto = true;
        photoContainer = el;
        break;
      }
      el = el.parentElement;
    }
    if (bgPhoto && dimAlpha === null && photoContainer) {
      // The WP-style overlay span isn't nested inside the <img> itself,
      // so it has to be searched for around photoContainer rather than
      // inside it -- try photoContainer first, then its parent.
      for (const scope of [photoContainer, photoContainer.parentElement].filter(Boolean)) {
        for (const cand of scope.querySelectorAll('*')) {
          const cs = getComputedStyle(cand);
          const op = parseFloat(cs.opacity);
          if (op < 1 && cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && !/url\(/.test(cs.backgroundImage || '')) {
            dimAlpha = op;
            break;
          }
        }
        if (dimAlpha !== null) break;
      }
    }
    // A real designed text panel (confirmed on the live home hero:
    // rgba(255,255,255,0.9)) sits *inside* the photo cover, wrapping just
    // the heading cluster -- so the walk stops at photoContainer rather
    // than continuing out into the page body, whose own plain opaque
    // white background (extremely common -- most themes set one
    // explicitly) would otherwise get misread as a panel on every single
    // page. A solid, fully-opaque color matching the page's own base
    // background is the same false-positive shape one level removed, so
    // that's excluded too even when found before reaching photoContainer.
    const pageBg = getComputedStyle(document.body).backgroundColor;
    let boxBg = null;
    el = mainH1.parentElement;
    for (let i = 0; i < 8 && el && el !== photoContainer; i++) {
      const bg = getComputedStyle(el).backgroundColor;
      const m = bg && bg.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/);
      const alpha = m && m[4] !== undefined ? parseFloat(m[4]) : 1;
      if (bg && alpha > 0 && bg !== pageBg && !(alpha >= 1 && /^rgb\(255, 255, 255\)$|^rgba\(255, 255, 255, 1\)$/.test(bg))) {
        boxBg = bg;
        break;
      }
      el = el.parentElement;
    }
    hero = { text: norm(mainH1.textContent).slice(0, 40), has_photo_bg: bgPhoto, dim_alpha: dimAlpha, has_box: !!boxBg, box_bg: boxBg };
  }

  // --- layout check 3: is a "form" real, or a placeholder? ---
  const realFields = [...document.querySelectorAll('input,textarea,select')]
    .filter(e => !inChrome(e) && e.type !== 'hidden').length;
  const placeholderPanels = document.querySelectorAll('.migration-form-placeholder').length;

  return {
    title: document.title,
    h1: norm((heads.find(h => h.l === 1) || {}).t || ''),
    headings: heads, n_headings: heads.length,
    content_len: contentLen, body_len: bodyText.length,
    img_count: imgs.length, bg_img_count: bgImgs.length, visual_imgs: imgs.length + bgImgs.length,
    broken_imgs: imgs.filter(i => i.complete && i.naturalWidth === 0).map(i => i.currentSrc || i.src).slice(0, 12),
    buttons: [...new Set(buttons)],
    media_text: mediaText,
    hero,
    real_form_fields: realFields,
    form_placeholder_panels: placeholderPanels,
  };
}"""


def grab(page, url, shot_path):
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        return {"error": f"goto: {e}"}
    page.wait_for_timeout(3000)
    for _ in range(6):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(350)
    page.wait_for_timeout(700)
    try:
        data = page.evaluate(EXTRACT_JS, CHROME_SELECTOR)
    except Exception as e:
        data = {"error": f"evaluate: {e}"}
    try:
        page.screenshot(path=str(shot_path), full_page=True)
    except Exception:
        pass
    data["http"] = resp.status if resp else None
    return data


def norm_key(s):
    return "".join(c for c in (s or "").lower() if c.isalnum() or c == " ").strip()


def _best_vertical_offset(a, b, max_shift=900, step=8, probe_width=220):
    """How many pixels to shift `b` down (negative: up) relative to `a`
    before diffing, found by brute-force search rather than assumed zero.

    Two full-page screenshots of the same page essentially never have the
    same total height -- a taller/shorter hero, a placeholder panel with
    different copy length, a dropped sidebar -- so a naive top-aligned
    crop compares content that's really just vertically shifted and scores
    it as "totally different": confirmed on this project's own hero fix,
    where a ~40px hero height difference smeared a false-positive red
    diff across the entire rest of an otherwise-identical page. Searches
    on small grayscale thumbnails (cheap without numpy) and returns the
    offset (in the *original* images' coordinate space) whose overlap has
    the lowest mean difference.
    """
    scale = probe_width / a.width
    pw, ph_a, ph_b = probe_width, int(a.height * scale), int(b.height * scale)
    ta = a.resize((pw, ph_a)).convert("L")
    tb = b.resize((pw, ph_b)).convert("L")
    max_shift_probe = int(max_shift * scale)
    step_probe = max(1, int(step * scale))
    best_offset, best_score = 0, None
    for dy in range(-max_shift_probe, max_shift_probe + 1, step_probe):
        # crop the overlapping window from each thumbnail after shifting b by dy
        a_top, b_top = max(0, -dy), max(0, dy)
        h = min(ph_a - a_top, ph_b - b_top)
        if h < 40:
            continue
        wa = ta.crop((0, a_top, pw, a_top + h))
        wb = tb.crop((0, b_top, pw, b_top + h))
        d = ImageChops.difference(wa, wb)
        score = sum(px * n for px, n in enumerate(d.histogram())) / (pw * h)
        if best_score is None or score < best_score:
            best_score, best_offset = score, dy
    return int(best_offset / scale)


def visual_diff(live_shot, dev_shot, out_path):
    """A full-page screenshot diff, vertically aligned first (see
    _best_vertical_offset()) so it isn't dominated by "the whole page
    shifted down 40px" false positives. Reports the fraction of the
    aligned overlap whose per-channel brightness delta exceeds a small
    antialiasing-noise threshold, and always saves a side-by-side
    [live | dev | red-highlighted diff] image for a human to look at.

    Returns the differing-pixel fraction, or None if Pillow isn't
    installed or either screenshot is missing/corrupt.
    """
    if Image is None:
        return None
    try:
        a = Image.open(live_shot).convert("RGB")
        b = Image.open(dev_shot).convert("RGB")
    except Exception:
        return None
    if a.width != b.width:
        b = b.resize((a.width, int(b.height * a.width / b.width)))
    if a.width < 10 or a.height < 10 or b.height < 10:
        return None

    dy = _best_vertical_offset(a, b)
    a_top, b_top = max(0, -dy), max(0, dy)
    h = min(a.height - a_top, b.height - b_top)
    w = a.width
    if h < 10:
        return None
    a = a.crop((0, a_top, w, a_top + h))
    b = b.crop((0, b_top, w, b_top + h))

    diff = ImageChops.difference(a, b)
    # collapse to per-pixel brightness so one histogram covers all 3
    # channels -- exact color isn't the point, "did something change here"
    # is, and this is cheap without a numpy dependency.
    gray = diff.convert("L")
    hist = gray.histogram()
    total = w * h
    differing = sum(hist[40:])  # ignore small antialiasing deltas
    frac = differing / total if total else 0.0

    # Always saved (not gated on the fraction) -- see diff_page()'s note
    # on why this score alone isn't a reliable pass/fail signal; a human
    # skimming the ranked report needs the image regardless of the number.
    mask = gray.point(lambda p: 255 if p >= 40 else 0)
    red = Image.new("RGB", (w, h), (255, 0, 60))
    highlighted = Image.composite(red, b, mask)
    strip = Image.new("RGB", (w * 3 + 20, h), (255, 255, 255))
    strip.paste(a, (0, 0))
    strip.paste(b, (w + 10, 0))
    strip.paste(highlighted, (2 * w + 20, 0))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    strip.save(out_path)
    return frac


def diff_page(slug, live, dev, live_shot, dev_shot):
    flags = []
    lc, dc = live.get("content_len", 0), dev.get("content_len", 0)
    ratio = round(dc / lc, 3) if lc else None
    if ratio is not None and (ratio < 0.85 or ratio > 1.35):
        flags.append(f"text {ratio}x (dev {dc} / live {lc})")

    lh = {norm_key(h["t"]) for h in live.get("headings", [])}
    dh = {norm_key(h["t"]) for h in dev.get("headings", [])}
    missing_h = sorted(h for h in lh - dh if h)
    if missing_h:
        flags.append(f"{len(missing_h)} headings missing on dev")

    if dev.get("broken_imgs"):
        flags.append(f"{len(dev['broken_imgs'])} broken imgs on dev")
    lv, dv = live.get("visual_imgs", 0), dev.get("visual_imgs", 0)
    if lv and (lv - dv) >= 2 and (lv - dv) / lv > 0.34:
        flags.append(f"images live {lv} vs dev {dv}")

    lb = {b.lower().rstrip("!. ") for b in live.get("buttons", [])}
    db = {b.lower().rstrip("!. ") for b in dev.get("buttons", [])}
    missing_b = sorted(b for b in lb - db if b and b != "learn more")
    if missing_b:
        flags.append(f"CTA buttons missing on dev: {missing_b}")

    # --- layout: media_text side/width, matched by heading key ---
    live_mt = {m["key"]: m for m in live.get("media_text", [])}
    dev_mt = {m["key"]: m for m in dev.get("media_text", [])}
    mismatched_side, mismatched_width, unmatched = [], [], []
    for key, lm in live_mt.items():
        dm = dev_mt.get(key)
        if not dm:
            unmatched.append(key)
            continue
        if lm["side"] != dm["side"]:
            mismatched_side.append(f"{key!r} live={lm['side']} dev={dm['side']}")
        if abs(lm["width_pct"] - dm["width_pct"]) >= 10:
            mismatched_width.append(f"{key!r} live={lm['width_pct']}% dev={dm['width_pct']}%")
    if mismatched_side:
        flags.append(f"image side mismatch: {'; '.join(mismatched_side)}")
    if mismatched_width:
        flags.append(f"image width mismatch: {'; '.join(mismatched_width)}")

    # --- layout: hero/banner box + overlay ---
    lhero, dhero = live.get("hero"), dev.get("hero")
    if lhero and dhero and lhero.get("has_photo_bg"):
        if lhero.get("has_box") != dhero.get("has_box"):
            flags.append(f"hero text-box mismatch: live={lhero.get('has_box')} dev={dhero.get('has_box')}")
        # None (no overlay mechanism found at all) and 0 (a mechanism was
        # found and measured at zero strength) mean the same thing here --
        # no darkening -- confirmed on the home hero, whose live markup
        # has no gradient/opacity overlay to find (None) while the
        # migrated version explicitly carries dimRatio 0 (0); treating
        # those as a mismatch would flag a page that's actually correct.
        la = 0 if lhero.get("dim_alpha") is None else lhero.get("dim_alpha")
        da = 0 if dhero.get("dim_alpha") is None else dhero.get("dim_alpha")
        if abs(la - da) >= 0.15:
            flags.append(f"hero overlay strength mismatch: live={la} dev={da}")

    # --- form realness ---
    l_fields = live.get("real_form_fields", 0)
    d_fields = dev.get("real_form_fields", 0)
    d_placeholders = dev.get("form_placeholder_panels", 0)
    if l_fields >= 2 and d_fields == 0 and d_placeholders:
        flags.append(f"form not wired up: live has {l_fields} real fields, dev shows a placeholder panel")
    elif l_fields >= 2 and d_fields == 0:
        flags.append(f"form missing entirely: live has {l_fields} real fields, dev has none")

    # --- visual pixel diff: informational only, NOT a flag ---
    # Deliberately not folded into `flags`. Empirically (see the commit
    # that added this file) even a page confirmed by eye to match live
    # closely scores 20-40% different: cross-platform rendering (GoDaddy
    # vs WordPress/Twenty-Four) never lines up exactly -- different exact
    # font hinting, stock-photo re-encoding on sideload, and a single
    # global vertical alignment can't correct for spacing that diverges
    # at more than one point down a long page (the hero, then a card
    # row, then a form section can each be a little taller or shorter
    # independently). Treating a raw percentage as pass/fail would flag
    # most of the site on noise alone. It's kept as a sortable score in
    # the report instead -- "which pages look most different overall,
    # worth a human's eyes first" -- with an image always saved so
    # that's a fast visual scan, not a re-run.
    vdiff = visual_diff(live_shot, dev_shot, DIFF_DIR / f"{slug}.png")

    return flags, ratio, vdiff, missing_h, mismatched_side, mismatched_width, unmatched


def run(target_slugs=None):
    OUT_DIR.mkdir(exist_ok=True)
    SHOT_DIR.mkdir(exist_ok=True)
    DIFF_DIR.mkdir(exist_ok=True)
    if Image is None:
        log("!! Pillow not installed -- visual pixel diff will be skipped (pip install pillow)")

    pages = json.load(open(STRUCTURED_CONTENT))["pages"]
    if target_slugs:
        pages = [p for p in pages if p["slug"] in target_slugs]

    results = []
    with sync_playwright() as pw:
        lb = pw.chromium.launch(headless=True)
        lp = lb.new_context(viewport=VIEWPORT).new_page()
        # dev.stratecon.tech gives headless Chromium a hard 403 from
        # SiteGround's bot rule -- only a claimed-browser UA triggers it,
        # so this side needs headed real Chrome (see CLAUDE.md).
        db = pw.chromium.launch(channel="chrome", headless=False)
        dp = db.new_context(viewport=VIEWPORT).new_page()

        for i, p in enumerate(pages, 1):
            slug = p["slug"]
            live_url = p["old_url"]
            dev_url = DEV_BASE + dev_path(p)
            live_shot = SHOT_DIR / f"{slug}__live.png"
            dev_shot = SHOT_DIR / f"{slug}__dev.png"
            live = grab(lp, live_url, live_shot)
            dev = grab(dp, dev_url, dev_shot)
            flags, ratio, vdiff, missing_h, mside, mwidth, unmatched = diff_page(
                slug, live, dev, live_shot, dev_shot
            )
            results.append({
                "slug": slug, "live_url": live_url, "dev_url": dev_url,
                "text_ratio": ratio, "visual_diff": vdiff, "flags": flags,
                "missing_headings": missing_h,
                "media_text_side_mismatch": mside,
                "media_text_width_mismatch": mwidth,
                "media_text_unmatched": unmatched,
                "live": live, "dev": dev,
            })
            log(f"{i}/{len(pages)} {slug:<40} vdiff={vdiff if vdiff is None else f'{vdiff:.0%}'}  "
                f"{'; '.join(flags) if flags else 'ok'}")
            json.dump(results, open(OUT_DIR / "compare_result.json", "w"), indent=2)

        lb.close()
        db.close()

    write_report(results)
    flagged = [r for r in results if r["flags"]]
    log(f"\n{len(flagged)}/{len(results)} pages flagged -- see {OUT_DIR / 'report.md'}")


def write_report(results):
    lines = [
        f"# Dev-vs-live comparison — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"{sum(1 for r in results if r['flags'])}/{len(results)} pages flagged.",
        "",
        "Structural checks: heading presence, body-text length ratio, image/CTA-button ",
        "counts. Layout checks: which side each side-by-side section's image sits on and ",
        "how wide its column is, a hero/banner's text panel + photo-overlay presence, and ",
        "whether a \"form\" is real inputs or an unswapped placeholder. These are the",
        "checks that count for pass/fail -- act on these.",
        "",
    ]
    for r in results:
        if not r["flags"]:
            continue
        lines.append(f"## {r['slug']}")
        lines.append(f"- live: {r['live_url']}")
        lines.append(f"- dev: {r['dev_url']}")
        for f in r["flags"]:
            lines.append(f"- **{f}**")
        lines.append("")

    lines += [
        "## Visual diff ranking (informational — not pass/fail)",
        "",
        "Full-page screenshot pixel diff, vertically aligned first, every page listed",
        "worst-first. Cross-platform rendering (GoDaddy vs WordPress) never lines up",
        "exactly -- fonts, stock-photo re-encoding, and multi-point spacing drift down a",
        "long page all inflate this even on pages that match by eye -- so a high score",
        f"here is a prompt to go look at `diff/<slug>.png`, not a confirmed bug. Rows at or",
        f"above {VISUAL_DIFF_FLAG_THRESHOLD:.0%} are **bolded** as worth a look first.",
        "",
        "| page | diff | image |",
        "|---|---|---|",
    ]
    ranked = sorted(
        (r for r in results if r.get("visual_diff") is not None),
        key=lambda r: r["visual_diff"], reverse=True,
    )
    for r in ranked:
        pct = f"{r['visual_diff']:.0%}"
        row = f"| {r['slug']} | {pct} | `diff/{r['slug']}.png` |"
        if r["visual_diff"] >= VISUAL_DIFF_FLAG_THRESHOLD:
            row = f"| **{r['slug']}** | **{pct}** | `diff/{r['slug']}.png` |"
        lines.append(row)

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    targets = sys.argv[1:] or None
    run(targets)
