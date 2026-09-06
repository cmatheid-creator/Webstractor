# GoDaddy-to-WordPress Automated Migration Pipeline

## What this project is

Carver (Principal Consultant, Stratecon Tech Advisors) is building a service that
automatically migrates small/medium GoDaddy Website Builder sites to WordPress,
using a team of AI agents to eliminate as much manual labor as possible. Packages
are priced by page count + detected features (payments, blogs, forums, knowledge
bases). **Current scope: informational sites only** — no ecommerce, no
login/account areas, no forums. Those are explicitly out of scope for now.

The test site is Carver's own: **stratecon.tech**, a ~16-page GoDaddy Website
Builder site (confirmed via its `meta-generator` tag) with a nested nav (AI /
Communications / Cybersecurity solution areas, each with sub-pages), a blog, an
FAQ accordion, a contact form, and a newsletter signup. No payments, no logins —
a clean in-scope example.

## The agent pipeline (design, from prior planning)

1. **Crawler Agent** (scripted) — headless browser, discovers + renders every page.
2. **Extraction Agent** (scripted) — pulls clean content per page into structured JSON.
3. **Qualification Agent** (LLM judgment) — flags anything out of scope (payments,
   logins, forums) before it's processed further. *This is the safety gate that
   makes "fully automated" an honest claim.*
4. **Brand Agent** (scripted) — extracts colors/fonts/logo from computed CSS.
5. **Content Structuring Agent** (LLM — Claude) — cleans raw extracted content into
   WP-ready structured blocks, generates meta titles/descriptions/alt text.
6. **Architecture Agent** (LLM + rules) — maps page inventory + nav into a WP
   page/menu hierarchy.
7. **Generator Agent** (scripted) — assembles a WXR file, Gutenberg block markup,
   theme tokens, and a plugin manifest.
8. **Redirect Agent** (scripted) — old-URL → new-URL map, exportable as CSV for
   the Redirection plugin.
9. **QA Agent** (scripted + LLM summary) — diffs old vs new, writes a plain-English
   report.
10. **Concierge Agent** (LLM) — the only agent-facing interface a non-technical
    client sees; translates everything above into plain language.

## Key technical decisions already made

- **Gutenberg blocks, not Divi**, for the automated-generation tier. Divi's
  shortcode format isn't reliably scriptable; Gutenberg's block markup is. Divi
  (or another builder) can stay an option for the higher-touch/custom tier where
  a human is building anyway — Carver is not married to any one builder; the
  automation is what matters, not the tool.
- **WXR (WordPress eXtended RSS)** is the output format for pages/posts — WordPress's
  native, stable import format.
- Generated pages import as **drafts**, never auto-published — human reviews before
  go-live, at least in this early phase.
- Redirects are generated as a CSV for the **Redirection** plugin.

## What's already built and proven (in this folder)

Superseded the original prototype status below — this has been run against the
real site repeatedly, end to end, across many iteration rounds:

- `crawler_agent.py` — run for real against https://stratecon.tech (all ~16+
  pages, including AI/Communications/Cybersecurity sub-pages and the blog).
  Captures headings/paragraphs/lists with their real GoDaddy `data-typography`
  role and a safe inline-HTML fragment (bold/italic/links preserved via
  `element_inline_html()`), card groups, post-feed ("AI Insights") cards, and
  media-text pairs. `structured_content.json` in this folder is real crawled
  output, not the original 4-page hand-built sample.
- `brand_agent.py` — run for real; `brand.json` in this folder is real
  extracted logo/colors/typography from the live site's computed styles.
- `generator_agent.py` — heavily extended well past the original prototype.
  Applies real brand typography (font/size/weight/color) to headings,
  paragraphs, lists, nav, and buttons; generates a real header/footer template
  part, global styles (colors, fonts, logo sizing), a `custom_css` WXR item as
  a resilient second delivery path for CSS overrides, and — as of the most
  recent round — real `@font-face` data fetched live from Google Fonts and
  embedded via WordPress's native font-loading schema (`settings.typography.
  fontFamilies[].fontFace`), so brand fonts load without needing shell access
  to run `apply_branding.php`. Also dedupes re-hosted images by canonical URL
  (`display_image_url()`) so the same photo doesn't get downloaded 2-3 times
  under different GoDaddy CDN resize-suffixed URLs. All hand-authored Gutenberg
  markup has been checked against real block-editor validation (not just
  front-end rendering) — every custom style/class hook uses `className` +
  a real CSS rule instead of an untracked inline `style=`, since the latter
  reliably produces "Block contains unexpected or invalid content" in the
  editor.
- A local WordPress test install (fresh installs + the actual WordPress
  Importer plugin) has been used repeatedly this project to verify generated
  WXR files end to end — real import, real Site Editor, real block-editor
  validation — before ever handing a file to Carver. Prefer this over
  reasoning about markup from first principles when something looks wrong;
  guessing has produced real regressions before (see "flex-grow" note below).

## Dev site workflow (dev.stratecon.tech)

Carver's real WordPress dev/staging site, hosted on SiteGround, used to
validate each generated WXR before anything goes near the real
stratecon.tech. The manual reset→reimport→verify loop, in order:

1. Full WP Reset (WP Reset plugin)
2. Reactivate theme (Twenty Twenty-Four) — usually already stays active
3. Reactivate WordPress Importer plugin — usually already stays active
4. Settings → Permalinks → **Post name** → Save Changes (a full reset drops
   this back to "Plain", which 404s every non-homepage URL — this step is not
   optional)
5. Import the generated `stratecon-migration.xml`
6. Publish all imported pages (they land as drafts)
7. Set Site Logo: Appearance → Editor → Styles → (or the identity/logo
   picker) → select the already-imported logo attachment from Media
   Library — **do this only after step 5**, never before. Touching the Site
   Editor before importing has previously caused WordPress to lazily create a
   stub row for the theme's own header/footer template parts or global
   styles, which collides with the real imported ones on a slug/singleton
   basis and silently keeps the stub active instead. Import first, always.
8. Verify — screenshots or, if this session has direct site access (see
   below), a real Playwright pass across the pages that changed.

If this Claude Code session has real credentials for dev.stratecon.tech (a
dedicated `claude-agent` WordPress admin account — check for a local,
git-ignored credentials file in this environment before asking Carver for
one), drive this workflow directly with Playwright instead of walking Carver
through it by hand: log in, run the WP Reset, reimport, publish, set the
logo, and take real screenshots/read real computed styles/open the real
block editor to check for validation warnings, the same way this project's
local WordPress test install has been used throughout. That closes the loop
directly instead of round-tripping every change through Carver's own manual
clicking and screenshots.

**Never commit real WordPress credentials to this repo.** If Carver hasn't
already set up a local credentials file outside git, ask where one should
live (e.g. `~/dev-site-credentials.txt` in this account's home directory,
outside the repo) rather than writing a password into any tracked file.

## Verified against the real dev site (2026-09-05, direct-access session)

A full reset → import → publish → repair → verify pass ran directly against
dev.stratecon.tech (headed real Chrome — headless Chromium gets a hard 403
from SiteGround's bot rule; only a claimed-browser UA triggers it, plain
`curl` is fine). Results:

- **Fonts: confirmed working on the real host.** Playfair Display + Cabin
  render (not a fallback serif) on every migrated page —
  `document.fonts.check()` true, correct `#1d2b52`/sizes. The theme.json
  `fontFace` mechanism holds up on SiteGround. Note SiteGround's Speed
  Optimizer (the renamed SG Optimizer) is currently **inactive** on the dev
  site; WP-Optimize's cache/minify plugin is active instead. Re-test with
  Speed Optimizer on before trusting fonts in production.
- **The "two intermittently-missing images" were misdiagnosed.** They import
  fine — WordPress downloads the JPEG bytes behind the `.webp` URL, saves the
  file as `.jpg`, generates every sub-size. They rendered *broken* because
  the importer's content URL-rewrite keeps the original `.webp`/`.png`
  extension, so the `<img>` 404s. Same failure hits any image whose GoDaddy
  URL extension lies about its bytes (the founder headshot `.png`, a `blob-*`
  `.png`, several `.webp`). Not a SiteGround problem, not intermittent — a
  pipeline problem. Fixed: see `repair_migration.php` below.

## Fixes landed this session (generator_agent.py)

- **`repair_migration.php`** — new generator output, run once from the WP
  root after import (same pattern as `apply_branding.php`). Does the three
  things no WXR item can: (1) repoints broken re-hosted image URLs at the
  file WordPress actually saved; (2) sideloads the GoDaddy `isteam/stock/...`
  images the importer can't take (opaque IDs, no extension) and repoints
  every reference — ~70 on this site; (3) sets the static front page from
  the crawler's `is_front_page` flag. Idempotent. Verified end-to-end on the
  dev site: 0 broken images, 0 remaining `img1.wsimg.com` hot-links, `/`
  serves the migrated home.
- **`core/freeform` (Classic) blocks eliminated.** The generator was emitting
  `<!-- QA FLAG -->` HTML comments *between* top-level blocks; WordPress's
  parser turns each stray comment into a Classic block on import (1–4 per
  page). They're now stripped from `post_content` and collected into a
  "Per-page review notes" section in `qa_report.md`. Verified: 0 freeform,
  0 validation warnings on every page checked in the real block editor.

## Hero / `<h1>` capture — landed and verified (2026-09-05, direct-access session)

The "no hero, no `<h1>` on the home page" issue below is **fixed**:

- **`crawler_agent.py` — `extract_hero()`.** GoDaddy Website Builder bundles
  the hero into the same header widget as the logo/nav (`data-ux="Header"`),
  which `CHROME_SELECTOR` excludes wholesale as chrome — so the main pass
  never saw it. `extract_hero()` reaches in explicitly for
  `<h1 data-aid="HEADER_TAGLINE_RENDERED">` (the only real page-level `<h1>`
  on the site), the `HEADER_TAGLINE2_RENDERED` sub-tagline, the
  `HEADER_CTA_BTN` call-to-action, and the `BACKGROUND_IMAGE_RENDERED` CSS
  background image (its `aria-label` doubles as alt text). Emitted as a
  `hero` block prepended in `extract_blocks()`; returns `None` on pages with
  no hero (home-only here). `featured_image` now prefers the hero image over
  `og:image` (which GoDaddy sets to the same generic `stock/2646` everywhere).
- **`generator_agent.py` — `hero` → `core/cover`.** Full-width cover: brand
  primary overlay at 60% dim (`overlayColor:"primary"`), inner `<h1>` in the
  brand HeadingAlpha font (no navy text colour on the dark overlay — new
  `_role_style_bits(include_color=False)`), sub-tagline paragraph, centred
  CTA button routed through the same slug resolution the card CTAs use. New
  `.migration-hero` CSS backs the overlay colour / min-height / white text
  independently of the imported palette. `collect_unique_images()` picks up
  the hero image so it flows into `repair_migration.php`'s stock-sideload
  list automatically.
- **Verified on the real dev site** (full WP Reset → import → publish →
  set front page → verify, headed Chrome): the migrated home now has exactly
  **one `<h1>`** ("Trusted Technology Advice", was zero); the cover renders
  full-bleed with the `#1d2b52` overlay, white Playfair Display heading,
  Cabin sub-tagline, and a "Contact Us" button linking to `/contact/`;
  **0 block-editor validation warnings** on the page (cover + inner blocks
  all parse clean); Playfair Display + Cabin still load, no regression to
  the rest of the page. `repair_migration.php` was **not** run this pass (no
  shell on SiteGround), so the hero background — like the other ~70 stock
  images — still hot-links to `img1.wsimg.com` until it is.

## Known issues still open (crawler / extraction side — next batch)

These are all in `crawler_agent.py` / the extraction step, not the
generator: the generator faithfully renders an incomplete capture.

- **FAQ accordion is flattened.** `extract_blocks()`'s FAQ detection emits a
  `faq_raw_unverified` block: questions captured, **no answers paired**, and
  the whole thing concatenated once then repeated. Renders on the page as
  duplicated FAQ text plus orphan questions. This is what the LLM Content
  Structuring Agent (pipeline step 5) is for.
- **Contact form / newsletter.** `forms_detected` captured one empty field.
  Contact page shows no real form and a literal `[contact-form-7 id="TBD"]`
  placeholder. Needs a form-plugin decision + real field capture.
- **`post_feed` ("AI Insights") layout.** Large vertical whitespace in the
  card grid; thumbnails often absent (client-JS-loaded on the source, not
  seen by the crawler; the og:image fallback is the bogus `stock/2646`).
- Minor text artifacts from link extraction: "Please Contact Us us if…"
  (double "us"), trailing literal "source" after each stat on AI Solutions.
- The Qualification Agent is still regex-based — fine for stratecon.tech but
  needs hardening before an unseen client site.
