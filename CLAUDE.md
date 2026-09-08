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

1. Full WP Reset (WP Reset plugin). **This deletes every WordPress user
   except the account running the reset** (`claude-agent`). Carver's own
   admin account (`cmatheid` / `cmatheid@gmail.com`) is wiped every cycle
   and must be recreated — see step 10 — or he can't log in.
2. Reactivate theme (Twenty Twenty-Four) — usually already stays active
3. Reactivate WordPress Importer plugin — usually already stays active
4. Settings → Permalinks → **Post name** → Save Changes (a full reset drops
   this back to "Plain", which 404s every non-homepage URL — this step is not
   optional)
5. Import the generated `stratecon-migration.xml`
6. Publish all imported pages (they land as drafts)
7. Install the **Stratecon Migration Repair** plugin — Plugins → Add New →
   Upload Plugin → `repair-migration.zip` → Activate. It runs once on
   activation (repoints broken re-hosted image URLs, sideloads the ~70
   GoDaddy stock images the importer can't take, **sets the static
   front page** from the crawler's `is_front_page` flag, and **sets the
   site logo** — `site_logo` option + `custom_logo` theme mod — from the
   imported logo attachment, found by its fixed WXR post id 40004 or, as
   a fallback, its `_webstractor_site_logo` marker meta), shows a report
   in an admin notice, then deactivates itself. Must run *after* step 6 —
   it can only point `/` at a page that already exists and is published.
   Idempotent; safe to activate again. A full reset wipes both the
   front-page and the logo settings, so this step is not optional on a
   re-run.
8. Confirm the logo — the repair plugin's report line should read
   "Site logo set (attachment N)". Only if it says "Site logo NOT set"
   do it by hand: Appearance → Editor → Styles → (or the identity/logo
   picker) → select the imported logo attachment from the Media Library.
   Never touch the Site Editor *before* step 5 — it makes WordPress
   lazily create a stub row for the theme's own header/footer template
   parts or global styles, which then collides with the real imported
   ones on a slug/singleton basis and silently stays active instead.
   Import first, always.
9. Verify — screenshots or, if this session has direct site access (see
   below), a real Playwright pass across the pages that changed.
10. Recreate Carver's admin account, destroyed by the step-1 reset:
    Users → Add New → `cmatheid` / `cmatheid@gmail.com`, role
    Administrator. Ask him for the password to set, or use the
    "send the user a set-password link" option. `claude-agent` working
    is not evidence that Carver still has access — his account is a
    separate row and it is gone after every reset.

If this Claude Code session has real credentials for dev.stratecon.tech (a
dedicated `claude-agent` WordPress admin account — check for a local,
git-ignored credentials file in this environment before asking Carver for
one), drive this workflow directly with Playwright instead of walking Carver
through it by hand: log in, run the WP Reset, reimport, publish, upload +
activate `repair-migration.zip`, set the logo, and take real screenshots/
read real computed styles/open the real block editor to check for
validation warnings, the same way this project's
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
  pipeline problem. Fixed: see the repair plugin (`repair-migration.zip`) below.

## Fixes landed this session (generator_agent.py)

- **`repair-migration.zip` — post-import repair, now an installable plugin**
  (was `repair_migration.php`, a `php` shell script — SiteGround gives the
  client no shell). Upload via Plugins → Add New → Upload Plugin, click
  Activate: it runs once on activation, shows a report in an admin notice,
  then deactivates itself. Does the three things no WXR item can: (1)
  repoints broken re-hosted image URLs at the file WordPress actually saved;
  (2) sideloads the GoDaddy `isteam/stock/...` images the importer can't
  take (opaque IDs, no extension) and repoints every reference — ~70 on this
  site; (3) sets the static front page from the crawler's `is_front_page`
  flag. Idempotent. The same file still runs straight from a shell where one
  exists (`php wp-content/plugins/repair-migration/repair-migration.php`).
  The generator emits both the unpacked `repair-migration/repair-migration.php`
  and the `.zip`. Verified end-to-end on the dev site by activating it:
  report read "Broken image URLs repointed: 13 / Stock images sideloaded: 70
  / Front page set to … id 100", 0 broken images afterward, hero background
  now served from `dev.stratecon.tech`, plugin self-deactivated on the next
  admin page load.
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
  the hero image so it flows into the repair plugin's stock-sideload list
  automatically.
- **Verified on the real dev site** (full WP Reset → import → publish →
  activate `repair-migration.zip` → verify, headed Chrome): the migrated
  home now has exactly **one `<h1>`** ("Trusted Technology Advice", was
  zero); the cover renders full-bleed with the `#1d2b52` overlay, white
  Playfair Display heading, Cabin sub-tagline, and a "Contact Us" button
  linking to `/contact/`; **0 block-editor validation warnings** on the page
  (cover + inner blocks all parse clean); Playfair Display + Cabin still
  load, no regression to the rest of the page. After the repair plugin ran,
  the hero background is served from `dev.stratecon.tech` (sideloaded), not
  hot-linked.

## dev-vs-live punch list — CLOSED (see `dev-vs-live-punchlist.md`)

A structured diff of all ~37 migrated pages against the live site drove a
nine-item punch list; **#1–#9 are all done and verified on the dev site**.
Headlines: sitewide `[contact-form-7]` placeholder text removed; blog
posts now carry their real title as the only page `<h1>`; the missing
"Cybersecurity Insights" post-feed recovered (lazy-load settle); the
"Spider-Man" post's duplicated bullets fixed (`<ul>`-inside-`<p>`); the
`/contact-us` 404 dropped; GoDaddy PDF-widget pages captured as a
`document_embed` with a real download button; post-feed cards styled to
match live; citation links wrapped in `<sup>`; contact forms delivered as
`fluentforms-migration.json` (a Fluent Forms import file); and the one
broken image on `threat-id-%26-detection` (a `$stock` prefix-match bug)
fixed. Full detail and the "not a bug, faithful to the live site" notes
(the "Contact Us us" typo, the inline "source" links) are in
`dev-vs-live-punchlist.md`.

## Content Structuring Agent — pipeline step 5

`content_structuring_agent.py` — runs after the crawl, before the
generator, rewriting `structured_content.json` in place. Three jobs:

- **FAQ restructuring.** GoDaddy's FAQ accordion renders questions as
  toggle controls and answers in separate panels, so the crawl leaves a
  `faq_raw_unverified` block (questions) plus loose answer paragraphs
  under the "Frequently Asked Questions" heading. The agent detects that
  region deterministically and pairs each question with its answer into a
  single `faq` block (generator renders it as `<h3>`/`<p>` pairs). With
  the LLM (Claude via the Anthropic SDK) it also handles reordered or
  multi-paragraph answers and fixes obvious typos; `--offline` does naive
  in-order pairing, which is correct for stratecon.tech. Verified offline:
  the home page's FAQ is now one clean `faq` block, no orphan paragraphs,
  no duplicated blob.
- **Meta title + description.** Per-page LLM generation of an SEO title
  (`meta_title` -> Yoast `_yoast_wpseo_title` postmeta) and a 150–160-char
  meta description drawn only from the page's own content.
- **Image alt text.** Any image the crawl left without alt text (only
  three across this site, all `image` blocks on blog posts — cards, hero,
  and media+text images already carry GoDaddy's stock-photo alt) is
  fetched, sent to Claude with the page for context, and given concise
  literal alt text written back onto the block. Handled inside the same
  per-page call — the images are added as content blocks and the JSON
  schema gains an `image_alt: [{src, alt}]` field.

**Staleness check (done).** Each page carries `_structured_agent_version`
(constant `AGENT_VERSION` in the script, bump on a logic change) and
`_structured_blocks_hash` (a hash of its blocks *after* structuring). A
normal run skips a page only when both still match — so a re-crawl, a
hand edit, or a version bump re-processes just the affected pages, and a
clean re-run is a no-op. `--force` ignores the check; `--pages` always
re-does the named pages. `structured_content.raw.json` is refreshed
whenever the input is an unstructured fresh crawl.

**Prompt caching:** the system prompt is sent as a `cache_control`
prefix. It's short enough now that it won't clear the model's minimum
cacheable size, so it's a no-op until the instructions grow — the hook
is in place.

**Not yet exercised end to end:** everything that needs the LLM (meta
text, image alt) — this environment has no `ANTHROPIC_API_KEY` /
`ant auth login`. The call shape (SDK 1.x `messages.create` with
`output_config.format` json_schema + adaptive thinking) is verified
against the installed `anthropic` package; the offline path and the
generator hand-off are verified.

## Site logo now applied by the repair plugin (no manual step, no shell)

Setting the active site logo (`site_logo` option + `custom_logo` theme
mod) used to be a manual Site Editor click, or a shell run of
`apply_branding.php` — and it is wiped by every full WP Reset, so it had
to be redone by hand each dev-site cycle. `repair-migration.zip` now
does it as step 4 of its activation run, alongside the front-page fix:

- `generator_agent.py` stamps the logo attachment with a
  `_webstractor_site_logo` = `1` postmeta in the WXR (in addition to its
  fixed `wp:post_id` 40004, which the classic WP Importer normally
  honours via `wp_insert_post`'s `import_id` on a clean import).
- The repair plugin resolves the logo attachment by that fixed id, falls
  back to the marker meta if the id isn't an attachment, and then sets
  both `site_logo` and `custom_logo`. Its report gains a "Site logo set
  (attachment N)" / "Site logo NOT set …" line. Idempotent.
- `apply_branding.php` got the same fixed-id-then-marker fallback so the
  two paths stay equivalent.

**Verified on the real dev site (2026-09-08, direct-access session).** A
full WP Reset → reactivate importer → permalinks → import
`stratecon-migration.xml` (with attachments) → publish all 39 pages + 1
post → upload/activate `repair-migration.zip` cycle, driven headless-free
via real Chrome. The plugin's activation report read: "Front page set to
… id 100" and "**Site logo set (attachment 40004)**" — the fixed WXR post
id resolved on the clean import, so the marker fallback wasn't exercised
this run but is in place. Front end: `img.custom-logo` renders from a
local `wp-content/uploads/` copy, 0 broken images on the home page,
Settings → Reading shows the static front page set. Carver's `cmatheid`
admin account (wiped by the reset, per the workflow note above) was
recreated in the same run.

## Still open

- **Newsletter signup** still renders as a placeholder panel — needs an
  email-tool / ESP decision (separate from the Fluent Forms contact-form
  work).
- The **Qualification Agent** is still regex-based — fine for
  stratecon.tech, needs hardening before an unseen client site.
- Crawler nondeterminism: a full re-crawl re-discovers duplicate
  `/ai-solutions/f/…` and `/cybersecurity-solutions/f/…` paths for the
  same blog posts, so every regeneration this project has needed a manual
  merge back down to the canonical page set.
- Minor: the "FREE CYBERSECURITY EBOOK" all-caps form title (crawler
  grabbed a CSS-uppercased hero heading as the form's region title).
