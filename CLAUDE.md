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
3. **Qualification Agent** (`qualification_agent.py`) — the safety gate that keeps
   "fully automated" honest. A deterministic signal catalog (DOM selectors, GoDaddy
   `data-ux` widget names, script/`<form action>` hosts, URL-path patterns) tagged
   by category (payments, accounts, forum, booking, donation) and weight; strong →
   page blocked, moderate → migrated but flagged for review, weak → noted only.
   Optional Claude judgement layer for false negatives the DOM misses. The crawler
   calls `scan_page()` inline and stashes the evidence; the module also re-runs
   standalone over `structured_content.json`.
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
   report. `qa_report.md` (build_qa_report() in generator_agent.py) covers the
   static content inventory (forms/images/FAQ/meta coverage); the live
   dev-vs-live comparison itself is `compare_agent.py` — structural checks
   (headings/text-length/image counts) plus layout checks a purely
   structural diff can't see (media_text image side/width, hero/banner
   text-panel + overlay, real-form-vs-placeholder) and an informational
   full-page pixel diff. Run it after any dev-site deploy; see
   dev-vs-live-punchlist.md "Pass 6" for why the layout checks exist.
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
   Automating it: Tools → WP Reset → Site Reset — tick "Reactivate
   current theme" and "Reactivate all currently active plugins" (so the
   importer / Fluent Forms / Yoast come back), type `reset` into the
   confirm field, click "Reset Site". That opens WP Reset's **own
   in-page modal** ("Are you sure…?" with a red **Reset WordPress**
   button) — it is *not* a native `confirm()` dialog, so a Playwright
   `page.on("dialog")` handler will not dismiss it; click the modal
   button explicitly. The reset then logs out + back in and lands on a
   plugin's post-activation screen (e.g. Yoast's welcome page), not a
   "site has been reset" notice — verify success by the page/media
   counts collapsing, not by on-screen text.
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
9. **Purge the cache, then verify logged out.** SiteGround serves a
   server-side page cache to anonymous visitors (`x-proxy-cache: HIT`
   in the response headers); a logged-in admin bypasses it entirely.
   So a Playwright pass run while logged in as `claude-agent` will show
   the new logo / meta / content while a logged-out visitor (Carver in
   a normal browser) still sees the pre-change cached HTML — this bit us
   on 2026-09-08 (home page showed no logo and the old generic
   `<title>` for days). After any import / repair-plugin run / edit:
   purge WP-Optimize's cache **and** SiteGround's cache (the SG
   Optimizer plugin `sg-cachepress` is normally inactive here — briefly
   activate it, hit "Purge SG Cache" from the admin bar, deactivate it
   again; re-saving a page also busts that one URL), then verify in a
   **fresh, logged-out** browser context and confirm `x-proxy-cache` is
   `MISS`. A browser hard-refresh does not touch the server cache.
10. Recreate Carver's admin account, destroyed by the step-1 reset:
    Users → Add New → `cmatheid` / `cmatheid@gmail.com`, role
    Administrator. Ask him for the password to set, or use the
    "send the user a set-password link" option. `claude-agent` working
    is not evidence that Carver still has access — his account is a
    separate row and it is gone after every reset.
11. Wire up the real forms (still manual — a reset wipes Fluent Forms
    too): Fluent Forms → Tools → Import Forms → upload
    `fluentforms-migration.json` (file input `#fileUpload`, button
    `.el-button--primary` "Import Forms" — the URL is
    `admin.php?page=fluent_forms_transfer`, not discoverable by guessing
    a route). Note the new form ids it creates (FF numbers its own demo
    forms 1-2 first, so the imported ones land at 3+), then in each
    page that carries a `.migration-form-placeholder` panel, replace
    that `<!-- wp:group {"className":"migration-form-placeholder",
    "anchor":"migration-ff-N"} -->...<!-- /wp:group -->` block with
    `<!-- wp:shortcode -->[fluentform id="<the real id>"]<!-- /wp:shortcode -->`
    (a REST `POST wp/v2/pages/<id>` with the edited `content` is the
    reliable way to do this precisely, rather than hand-editing in the
    block editor). Without this step every "form" on the site is the
    dashed placeholder panel, not a working form.

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

**Step 5 run once, in-session (2026-09-08).** This environment has no
`ANTHROPIC_API_KEY` / `ant auth login` (the Claude Code login is a Pro
subscription, which doesn't grant API access), so the semantic pass was
done *by this Claude Code session* instead of by
`content_structuring_agent.py`'s API call: all 37 pages got a
hand-reviewed `meta_title` (≤60 chars) + 147–164-char `meta_description`
from their own content, and the three alt-less `image` blocks
(`ai-and-data-analytics…`, `top-5-security-considerations…`,
`how-ai-can-transform…`) got alt text written from the actual images.
Applied via a scratch script that reuses the agent's own
`structure_page()` / marker logic; `structured_content.json` now carries
`_structured_agent_version: 2` + `_structured_blocks_hash` on every page.
Output is byte-compatible with what the automated agent would produce —
the agent takes over for the next client site once an API key exists.
The call shape (SDK 1.x `messages.create` with `output_config.format`
json_schema + adaptive thinking) is verified against the installed
`anthropic` package but the agent's *own* API path is still unexercised.

**Verified on the dev site (2026-09-08, direct-access session).** Full
reset → import → publish → activate Yoast SEO (`wordpress-seo`, needed
so the meta tags render) → repair plugin → verify. On the live front
end: `<title>` and `<meta name="description">` match the generated
values on every sampled page (8/8), all three re-described images render
with their `alt`. No regressions: repair report repointed 17 broken image URLs,
sideloaded 71 stock images, set the front page and the logo (attachment
40004); 0 broken images on the home page. `cmatheid` recreated (the
reset wiped it, per the workflow note). Yoast's "you're blocking access
to robots" warning is just the staging site's deliberate "Discourage
search engines" setting.

The Spider-Man post's image `alt` was the trademark-disclaimer caption
the crawler grabbed (that image already had *an* alt, so the step-5
image pass skipped it). Fixed 2026-09-08: re-described in
`structured_content.json` and pushed to the live dev page (id 135, a
`page` — note the migration imports blog posts as pages) via the REST
API. The disclaimer text stays on the page as its own paragraph.

## FAQ accordion + card-row wrap (2026-09-08)

Two rendering fixes in `generator_agent.py`, both verified on the dev
site (logged out, cache purged, `x-proxy-cache: MISS`, 0 block-editor
validation warnings):

- **FAQ renders as a real accordion.** The `faq` block now emits
  `core/details` blocks (WP 6.7+), collapsed by default, instead of
  flat `<h3>`/`<p>` pairs — restoring the click-to-expand behaviour of
  the original GoDaddy accordion. `.migration-faq-item` CSS adds the
  dividers / pointer cursor / focus ring. Verified: 3 `<details>` on the
  home page, all closed on load, first one opens on click.
- **`card_group` wraps at 3 columns per row.** It was emitting one
  `wp:columns` row with every card, which renders as N skinny columns
  (`core/columns` is a non-wrapping flex row on desktop) — the
  Communications Solutions page showed one row of 6 where the live site
  has 2×3. Now chunked into rows of ≤3, like `post_feed`. Verified:
  `communications-solutions` renders two rows of three equal 399px
  columns; pages with ≤3 cards per group (home, ai-solutions) unchanged.

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

## Session handoff — 2026-09-08

**State:** All work is committed and pushed to
`origin/claude/stratecon-crawler-generator-faki7f` (working tree clean).
The dev site (dev.stratecon.tech) reflects the latest: step-5 meta/alt,
FAQ accordion, 2×3 card rows, logo, privacy-policy slug reclaimed, eBook
placeholder reordered. A full dev-vs-live visual pass was run 2026-09-08
— results in `dev-vs-live-punchlist.md` ("Pass 2"). 0 broken images on
any page; body text 90–98% of live everywhere.

**Blocked on a decision from Carver:**
1. `cyber-risk-assessment` Cognito Forms embed — **RESOLVED 2026-09-09**:
   rebuilt in Fluent Forms (see "Done" below).
2. Blog posts (×16) drop the GoDaddy right sidebar (Categories, Recent
   Posts, blog-signup) and social-share icons — **RESOLVED 2026-09-09**:
   Carver's call is to drop it. Blog posts import as WP *pages*, not
   posts, so core Recent Posts/Categories blocks wouldn't populate
   anyway; reproducing the rail would be a hand-maintained link list.
   Accepted as-is; "Share this post:" stays text-only.
3. **Newsletter / "Stay Informed"** — still a placeholder on the home
   page, contact, and 17 blog posts. Needs an ESP choice (Mailchimp,
   etc.) before it can be wired. **Still open** — the last remaining
   punch-list item, and it can't move without the ESP name.

**Done 2026-09-11:**
- **`compare_agent.py` — closed the tooling gap the layout regressions
  below exposed.** New permanent pipeline tool adding the checks a
  headings/text-length/image-count diff can't provide: media_text
  image side/width (matched live-to-dev by heading, compared by
  *rendered* position since GoDaddy's row-reverse alternation makes DOM
  order meaningless), hero/banner text-panel + photo-overlay (recognizes
  both GoDaddy's and WordPress core/cover's overlay techniques), and
  real-form-vs-placeholder. A full-page pixel diff (vertically
  offset-aligned first) is informational/ranked only, never a flag —
  cross-platform rendering noise alone scores 20-40% "different" on a
  page confirmed by eye to match. Caught a real bug in itself (a stray
  Python `#` inside the JS template silently broke every check) before
  trusting it, via a synthetic fixture that deliberately mismatches
  every check. The real run that followed found three genuine,
  previously-unknown issues, now fixed too: `page_banner`'s overlay was
  hard-coded to 50% instead of the live ~24%; `risk-assessment-1` and
  `cyber-risk-assessment` were each missing a stand-alone CTA button
  outside any media_text pair; and threat-protection/threat-id's Fluent
  Forms shortcode swap had been silently reverted by an out-of-order
  content PATCH earlier in the same session. Full 37-page sweep
  afterward: 0 real regressions, only the two already-documented
  non-actionable patterns still flag. See dev-vs-live-punchlist.md
  "Pass 6".
- **Four layout regressions the structural dev-vs-live diff couldn't
  see, reported by Carver directly.** Home hero was missing its
  translucent white text panel and had a hard-coded 60% dark overlay
  (live has none); Services/About media_text sections were all
  image-left instead of alternating like live; About's founder photo
  was stretched to a plain 50% column instead of live's narrower,
  right-aligned treatment; Contact page showed the dashed form
  placeholder instead of a working form. `extract_hero()` now captures
  the real overlay alpha and the white-box background/padding from the
  live DOM; `mark_media_text_pairs()` now compares *rendered* left
  position (not DOM order — GoDaddy alternates sides via
  `flex-direction:row-reverse`, DOM order never changes) and the image
  cell's width share. Imported the 3 Fluent Forms + swapped every
  placeholder for its real `[fluentform id]` shortcode (see the "Dev
  site workflow" step 11 above — still a manual step after every
  reset). Two block-validation bugs surfaced and fixed along the way
  (a `dimRatio:0` CSS-class edge case, and a same-specificity CSS rule
  silently losing the cascade). Verified: full reset→import→publish→
  repair→FF-import→shortcode-swap→cache-purge pass, logged out,
  screenshots confirm home/services/about/contact now match live.
  See `dev-vs-live-punchlist.md` "Pass 5" for the noted gap in the
  comparison tooling itself (structural diffs can't see layout/overlay/
  form-functionality regressions — only a real visual diff or
  explicit structural checks for these specific GoDaddy behaviors would
  have caught this ahead of Carver spotting it).

**Done 2026-09-09:**
- **`cyber-risk-assessment` self-assessment form rebuilt in Fluent
  Forms.** The live page's 20-question Cognito Forms embed (cross-origin,
  never crawled) is now a `contact_form` block on the page — name, email,
  10 section breaks, 19 five-point rating radios, a concerns textarea,
  and a follow-up dropdown, plus the intro/disclaimer/consent as
  custom-HTML notes. `build_fluentform_form_fields()` grew `radio` /
  `select` / `section` / `html` types; `build_fluentforms_export()` now
  emits FF's exact export shape with a **`metas` array** — FF's Import
  tool ignores a `form_meta`-only export, so the migrated forms had been
  importing with no `formSettings` and rendering nothing via
  `[fluentform]`. Verified end to end on the dev site (import → shortcode
  renders the full 95-radio form → matches live).
- **Full dev-site verification pass (2026-09-09).** Ran the whole
  workflow end to end against dev.stratecon.tech (headed real Chrome):
  WP Reset (38→2 pages, 96→0 media) → permalinks → import with
  attachments → publish → repair plugin (17 image URLs repointed, 71
  stock images sideloaded, front page + logo set) → cache purge →
  verify logged out (`x-proxy-cache: MISS` everywhere) → recreate
  `cmatheid` admin. 9-page logged-out spot check: 0 broken images, logo
  on every page, no shortcode leaks, FAQ `<details>` / inline PDF
  `<object>` / 2-up Insights grid / cyber-risk placeholder all render.
  Found + fixed one issue: WordPress's sample Privacy Policy page was
  still live at `/privacy-policy-2/` with core boilerplate — the repair
  plugin's step 5 gated the sample-trash on the migrated page's slug,
  which fails when publish-order hands the migrated page
  `/privacy-policy/` directly. Now the sample is trashed whenever a
  distinct migrated Privacy Policy page exists; re-verified
  (`/privacy-policy/` = real policy, `/privacy-policy-2/` 404s). Also
  noted: WP Reset's confirm is a custom in-page modal ("Reset
  WordPress" button), not a native `confirm()` dialog — a
  `page.on("dialog")` handler alone doesn't dismiss it.
- **Page banners + section CTA buttons (full dev-vs-live re-diff).** A
  clean re-diff over all 37 pairs (the first attempt's text extraction
  was broken — `innerText` on a detached clone returns `""`) confirmed
  **no content loss**; the only systemic gaps were two widgets the
  crawler dropped across the solution/landing pages. (1) GoDaddy's
  body-level `data-ux="WidgetBanner"` — a title-over-photo band —
  became a bare `<h1>`; `extract_page_banner()` now emits a
  `page_banner` block (title, bg-image URL, aria-label alt), rendered as
  a short full-width `core/cover`, its stock image sideloaded by the
  repair plugin (71 → 75). (2) The pill CTA closing most `media_text`
  sections (`<a data-ux-btn="secondary">`) was dropped; the text-cell
  extractor captures it as a `button` item (raw `textContent`, not
  `inner_text`), rendered as a real `core/button` with slug-remapped
  href. `structured_content.json` merged surgically over the 10 banner
  pages (page_banner replaces the leading `<h1>`, CTAs appended to
  media_text by heading) — no other block touched. Verified logged out
  on all 10: banner cover + `<h1>` + re-hosted image + 3–7 CTA groups,
  0 broken images.
- **Generic third-party form-embed detection.** The crawler now
  auto-detects the class of problem the cyber-risk-assessment fix solved
  by hand. `EMBED_FORM_PROVIDERS` + `detect_embedded_forms()` in
  `crawler_agent.py` scan each page for an `<iframe>` / loader `<script>`
  / builder placeholder `<div>` from ~20 external form builders (Cognito
  Forms, JotForm, Typeform, Google/Microsoft Forms, HubSpot, Wufoo,
  Formstack, Tally, Paperform, …), skip site chrome, and collapse to one
  hit per provider per page. `extract_blocks()` emits an `embedded_form`
  block; the generator renders it as a labelled placeholder with a QA
  flag (rebuild in Fluent Forms — cyber-risk-assessment is the worked
  example — or re-embed via the provider's own block), and `qa_report.md`
  gains a "Third-party form embeds" callout. Verified by a fixture unit
  test (correct providers detected, chrome + non-provider iframes
  ignored, multi-match collapse) and the block render. The current
  `structured_content.json` is not re-crawled, so `cyber-risk-assessment`
  keeps its hand-built model; a future re-crawl surfaces every embed
  instead of silently dropping it.
- **PDF-widget pages** (`ai-use-policy-template`, `ai-disclosure-template`)
  now render `document_embed` as a `core/file` block with
  `displayPreview` — an inline `<object>` PDF viewer + Download button,
  matching the live page's in-page viewer. Verified on the dev site: 0
  block-editor validation warnings; the cross-origin GoDaddy CDN PDF
  loads (PDFium takes a few seconds to paint). QA flag still asks for a
  Media Library re-host before go-live.
- Post-feed grids are now 2-up (match live) with a "Cybersecurity
  Insights" / "AI Insights" section heading on the two landing pages —
  verified on a fresh dev-site pass, which also re-confirmed last
  session's privacy-policy step 5 and eBook reorder held through a real
  import. The live "All Posts | <category>" filter tabs above the
  Insights grid are still not reproduced.
- Normalized the "FREE CYBERSECURITY EBOOK" all-caps heading + form
  title (crawler had grabbed CSS-uppercased text) to "Free Cybersecurity
  eBook" on `threat-protection` and `threat-id-%26-detection`, matching
  `cybersecurity-solutions`. Side effect: `fluentforms-migration.json`
  dedup'd from 3 forms to 2, and all three eBook pages now reference the
  same form. Verified logged out on the dev site.
- **Crawler blog-post dedup.** GoDaddy serves each post under every
  section that links to it (`/blog/f/x`, `/ai-solutions/f/x`,
  `/cybersecurity-solutions/f/x`) with identical content, so every
  re-crawl was saving each post 2-3 times with the same slug and needing
  a manual merge. `crawl()` now keys its dedup on the slug alone for
  `/<section>/f/<slug>` paths (`blog_feed_slug()` / `canonical_feed_item_url()`
  in `crawler_agent.py`), canonicalises `old_url` to `/blog/f/<slug>`
  whichever prefix served the crawl, and records the other prefixes as
  `page["alias_urls"]`. `build_redirects_csv()` emits a 301 row per
  alias, so those URLs no longer 404 on the new site. Verified by unit
  test + a crawl-loop simulation; the current `structured_content.json`
  was back-filled with `alias_urls` from the existing `post_feed` hrefs,
  so `redirects.csv` grew 37 → 55 rows now (18 alias redirects) without
  a re-crawl.
- **Qualification Agent hardened** into its own `qualification_agent.py`
  (pipeline step 3). Replaced the four inline regex checks in the crawler
  with a weighted signal catalog across five categories (payments,
  accounts, forum, booking, donation) — GoDaddy Online Store / Members
  Area / Appointments `data-ux` widgets, Stripe/Square/PayPal/Shopify/
  Ecwid/WooCommerce hosts, Calendly/Acuity/Donorbox/GoFundMe embeds,
  auth providers, forum software, plus URL-path and (weak, guarded) text
  patterns. `require_all` rules combine predicates (a password field AND
  login copy). strong→block (excluded), moderate→review (migrated +
  flagged), weak→noted. Optional Claude judgement layer for DOM-invisible
  cases; `--offline` or in-session when there's no API key. The crawler
  calls `scan_page()` and stashes `_qualification` + `_qualification_evidence`
  per page so the module re-runs standalone; the generator surfaces the
  gate in the QA report; a `qualification_report.md` is written.
  Verified: unit tests (verdict reduction, per-rule matching, blog-prose
  false-positive guard, synthetic store/members/booking pages) + a live
  `scan_page` pass over 8 real stratecon.tech pages (forms, Cognito embed,
  blog posts, PDFs) → **0 false positives, site in scope**.

**Can be done without Carver (offered, not yet greenlit):**
- Not a bug: the comparison flagged "Cybersecurity Primer – Securing …"
  as an en-dash on dev vs a hyphen on live. The stored text is a plain
  hyphen on all three pages; WordPress's `wptexturize` renders " - " as
  " – " on output. Consistent, standard WP behaviour — nothing to fix.
  (The 2023 vs 2026 year difference between the pages is faithful to
  live.)
- The **Content Structuring Agent's own API path** has still never run
  end to end (this environment is Claude Pro, no API key — step 5 was
  done in-session). Exercise it whenever an `ANTHROPIC_API_KEY` exists.

**To resume:** `cd ~/Webstractor`, start Claude Code (`claude --continue`
to reattach this session, or a fresh `claude`), and read this file +
`dev-vs-live-punchlist.md`. The Playwright dev-site automation scripts
from this session are gone (scratchpad is session-scoped) but the
patterns are documented in the "Dev site workflow" section above.
