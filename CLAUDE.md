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

## Known issues carried forward from prior sessions

- **Two specific re-hosted images intermittently fail to import** on Carver's
  real SiteGround host ("Cyber Training example.webp" / "AI Customer
  Service.webp" source filenames) despite every diagnostic available from a
  sandboxed session coming back clean: the source URLs are valid/reachable,
  the downloaded bytes are valid JPEGs despite the `.webp` URL extension,
  WordPress's own type-sniffing correctly retypes them, and a full local
  production-equivalent reimport succeeds every time. Suspected but
  unconfirmed: an interaction with SiteGround Optimizer's own WebP
  image-conversion feature, since these are the only two images on the site
  sourced from `.webp`-extensioned URLs. If this session has real site
  access, this is worth investigating directly (check the plugin's settings,
  the Media Library state, and the site's own error log) rather than
  continuing to reason about it from a sandbox.
- **Font loading was fixed twice.** A CSS `@import` of the Google Fonts
  stylesheet (in the `custom_css`/global-styles content) looked correct in a
  sandboxed test but didn't survive on the real host — @import needs to be
  the literal first rule in its stylesheet or browsers discard it, and
  CSS-combining/minifying plugins (SiteGround Optimizer is active on the dev
  site) are a well-known way that breaks. Replaced with real `@font-face`
  data embedded in `settings.typography.fontFamilies[].fontFace` (theme.json
  v2's native schema, read by `WP_Font_Face_Resolver`, core since WP 6.4) —
  confirmed via a local WordPress install that this produces real
  `@font-face` CSS in `wp_head`, not yet confirmed against the real dev site
  as of the last handoff.
- Contact form's exact fields weren't fully visible in extracted content —
  needs confirmation against the live site before any real migration goes
  live.
- Newsletter signup is mapped to a placeholder shortcode — needs to be wired
  to whatever email tool the new site will actually use.
- The Qualification Agent is still regex-based (see the pipeline design
  above) — fine for stratecon.tech, which this project has been tuned
  against, but needs to move to something more robust before this is trusted
  on a client site it hasn't seen.
