# dev.stratecon.tech vs stratecon.tech — punch list

Structured diff pass over all 38 migrated pages (heading outlines, image
counts + broken-image detection, form/placeholder flags, full-page
screenshots of every pair), run 2026-09-06 against the current dev site
(fresh reset → import → publish → repair plugin → front page set).

## Pass 6 — compare_agent.py: closing the tooling gap 2026-09-11

Pass 5 fixed the four regressions Carver found by eye but left the
comparison *tooling* itself unchanged -- still just headings/text-length/
image-counts, blind to layout. Built `compare_agent.py`, a permanent
pipeline tool (not a throwaway script), adding the checks that would
have caught Pass 5's regressions automatically:

- **media_text image side/width** — matched live-to-dev by heading text,
  compared by *rendered* position (GoDaddy's `flex-direction:row-reverse`
  alternation makes DOM order meaningless — confirmed the image is
  always the first DOM child regardless of which side it renders on).
- **hero/banner text-panel + photo-overlay** — generalized to recognize
  either GoDaddy's gradient-baked-into-background-image technique or
  WordPress core/cover's separate overlay-span-with-opacity technique
  (needed both: the first version only knew GoDaddy's, so it silently
  found "no overlay" on every migrated page and cried wolf on all 10
  banner pages until fixed).
- **form realness** — real input/textarea/select count vs an unswapped
  `.migration-form-placeholder` panel.
- **a full-page pixel diff**, vertically offset-aligned first (a naive
  top-crop badly false-positives on ordinary height differences), kept
  **informational/ranked only, not a flag** — even a page confirmed by
  eye to match live scores 20–40% different from ordinary cross-platform
  rendering noise (different font hinting, re-encoded stock photos,
  multi-point spacing drift down a long page); treating that as pass/fail
  would flag most of the site on noise alone.

**Caught a real bug in itself before trusting it:** a stray Python-style
`#` comment inside the JS template caused a silent `evaluate()` syntax
error, so an initial full-site run's "0/37 flagged" was meaningless — no
check had actually executed, and every field the diff logic read back was
just an empty default. Fixed, then written to distrust its own logic:
validated with a synthetic HTML fixture deliberately mismatched on every
check (image side, width, hero box, overlay strength, form realness) and
confirmed **all five fire correctly** before trusting a real run against
the site.

**That real run then found three genuine, previously-unknown issues**
(the actual proof this was worth building), now also fixed:
- `page_banner`'s overlay was hard-coded to 50%; the live banner is
  ~24%. `extract_page_banner()` now captures the real value, same
  pattern as the earlier hero `dim_ratio` fix.
- `risk-assessment-1` ("Take Free Assessment") and `cyber-risk-assessment`
  ("Get in Touch") were each missing a stand-alone CTA button that sits
  in its own text-only section, not inside any media_text pair — the
  crawler only captures buttons from media_text text-cells. Added by
  hand to `structured_content.json` (matched to its exact position on
  the live page) rather than building a whole new extraction path for
  what's currently a single-site, two-button case.
- `threat-protection` and `threat-id-%26-detection` had their Fluent
  Forms shortcode swap **silently reverted** back to the placeholder
  panel — a real self-inflicted regression: an earlier content PATCH in
  the same session (pushing the page_banner `dim_ratio` fix) overwrote
  those two pages' content from freshly regenerated (placeholder-shaped)
  XML, clobbering the shortcode swap done earlier in the same pass. Both
  the lesson and the fix: **content PATCHes must be applied in the right
  order relative to the Fluent Forms shortcode swap**, or re-apply the
  swap after any later content push — see CLAUDE.md's dev-site workflow
  step 11.

Verified: 0 invalid blocks across all touched pages before deploying;
full 37-page sweep on the dev site afterward — 24/37 pages still show a
flag, but every single one is one of the two already-documented,
non-actionable patterns (the intentional blog-listing-header strip on
all 16 blog posts, and body-text-ratio measurement noise on pages with
form placeholders). **Zero real regressions outstanding.**

Going forward: run `python3 compare_agent.py` after any dev-site deploy
(or `python3 compare_agent.py <slug> ...` for just the touched pages) —
output goes to `comparison-output/` (gitignored), read `report.md` for
the flagged pages + the visual-diff ranking, and write up anything real
into this file by hand, the way every pass so far has.

## Pass 5 — layout regressions the structural diff can't see 2026-09-11

Carver reported four concrete visual differences Pass 4's structural
diff (headings/text-length/image-counts) had no way to catch, because
none of them change the text or image *count* — they're layout and
functional-behavior regressions:

1. **Home hero** — no translucent white panel behind the heading/
   sub-tagline/CTA, and the background photo read noticeably more
   "faded"/muddy than live.
2. **Services page** — the "middle three" IT-service sections all had
   their image on the left; live alternates left/right per section.
3. **About page** — same left/right problem, plus the founder photo
   rendered oversized (stretched to a plain 50% column) instead of
   live's smaller, right-aligned, narrower-column treatment.
4. **Contact page** — no functional form, just the placeholder panel
   (by design at generation time, but never swapped for the real one).

Root-caused and fixed all four (see the two commits below); full
reset→import→publish→repair→Fluent-Forms-import→shortcode-swap→cache-purge
pass on the dev site, verified logged out against all four pages plus a
full 37-page block-validation sweep before deploying. Screenshots
confirm home/services/about/contact now match live's layout.

**1 & 2 — `extract_hero()` / `mark_media_text_pairs()` fixes** (see the
"Fix hero white box/overlay and alternating media_text image position"
commit): the hero's dim overlay was hard-coded to 60% navy (live has
none — legibility comes from the white box) and every media_text pair
rendered image-left at a flat 50/50 split (GoDaddy alternates sides via
`flex-direction:row-reverse` on the Grid — the image is always the
*first* DOM child regardless of which side it renders on, so DOM order
can't detect it; About's founder photo also uses a ~33/67 split, not
50/50). Both are now captured from the live DOM (rendered position +
width share, and the hero's actual overlay alpha + box background) and
applied via `core/media-text`'s `mediaPosition`/`mediaWidth` and a new
`.migration-hero-box` group — markup verified against
`wp.blocks.getBlockContent()`, including two block-validation bugs this
surfaced (a `dimRatio:0` CSS-class regression, and a same-specificity
CSS rule losing the cascade) — see the two follow-up commits.

**4 — Contact / eBook / Cyber-Risk forms — DONE.** Imported
`fluentforms-migration.json`'s 3 forms via Fluent Forms → Tools → Import
Forms (new ids 3/4/5), then swapped each `.migration-form-placeholder`
panel for its real `[fluentform id="N"]` shortcode via a REST content
PATCH on `contact`, `cyber-risk-assessment`, `cybersecurity-solutions`,
`threat-protection`, `threat-id-%26-detection`. Verified: `/contact/`
renders a real First/Last Name, Email, Message, opt-in, Send form (8
inputs). **Not yet automated** — a fresh WP Reset wipes Fluent Forms
along with everything else, so this import + swap has to be redone by
hand (or scripted again) after every reset; the repair plugin doesn't
do it (dropped earlier as too fragile for a direct-DB-insert approach —
the sanctioned Import Forms UI path used here is reliable but still
manual).

**Comparison tooling gap, noted for next time:** every structural-diff
pass so far (heading text, body-text length ratio, image/CTA counts)
is blind to *how* content is laid out — side, width, overlay/box
styling, and whether a "form" placeholder is real. Catching this class
of regression earlier means either an actual visual/pixel diff between
matched live/dev screenshots, or explicit structural checks for the
specific things GoDaddy is known to vary (image side, column width,
overlay presence) rather than just counting elements.

## Pass 4 — full dev-vs-live re-diff + page banners / CTAs 2026-09-09

Re-ran the structured diff across all 37 pairs with **working text
extraction** (the first attempt read every live page as empty — it
called `innerText` on a detached DOM clone, which returns `""`). Result:
**no content loss anywhere** — every page's dev body text ≥ live (the
higher ratios are the newsletter/form placeholder panels plus
chrome-subtraction noise). The one flagged "missing heading" on 16 blog
pages is the crawler's *intentional* strip of the blog-listing header
("Stratecon Tech Insights", Tier 1 #2). The only real systemic gaps were
two widgets the crawler had been dropping:

### Page banners — DONE (10 pages)

GoDaddy's body-level `<div data-ux="WidgetBanner">` — a ~210px
full-width band with a stock background photo and the page title in
white — was reduced to a bare `<h1>` on `connectivity`, `services`,
`ai-for-sales-1`, `ai-strategy-1`, `risk-assessment-1`,
`threat-protection`, `threat-id-%26-detection`, `unified-communications`,
`customer-experience`, `ai-for-customer-service`. New
`extract_page_banner()` in `crawler_agent.py` pulls the title, the
`background-image` URL, and the `aria-label` as alt, and tags the
widget's subtree so the document-order pass skips the now-duplicate
heading; it emits a `page_banner` block. The generator renders a short
full-width `core/cover` (dimRatio 50, `.migration-page-banner`) carrying
the page `<h1>`; `collect_unique_images()` walks `page_banner` blocks so
the repair plugin sideloads the stock image (71 → 75 entries).
Verified logged out on all 10: banner cover present, `<h1>` correct,
background image re-hosted to `dev.stratecon.tech/wp-content/uploads/`
(not hot-linked), 0 broken images.

Opening the pages in the block editor first caught that `core/cover`
was `isValid=false` — a latent bug the home hero had all along, which
the new banner inherited. Checked the correct markup against
`wp.blocks.getBlockContent()` and fixed it in a shared `_cover_block()`
helper: the `<img class="wp-block-cover__image-background">` goes
*before* the `<span>` overlay (not after); the dim class is
`has-background-dim-N has-background-dim` only for a non-50 ratio; and
the `alt` must be in the block's JSON attributes, not only the `<img>`
tag. Re-verified: all 11 affected pages (10 banners + home hero) open in
the editor with **0 invalid blocks**.

### Section CTA buttons — DONE (~33 buttons, 10 pages)

The pill CTA that closes almost every side-by-side section on the
solution pages ("Get a Quote", "Let's Talk", "Schedule a Call", …),
`<a data-ux-btn="secondary">`, was dropped. The `media_text` text-cell
extractor now captures these (raw `textContent`, not `inner_text`, so
GoDaddy's `text-transform:uppercase` isn't baked into the label) as
`button` items; the generator renders a real left-aligned `core/button`,
its href remapped to the migrated slug when it targets another migrated
page, lowercase-authored labels smart-titled. Verified logged out: 3–7
CTA groups per page in the right sections.

`structured_content.json` was updated by a **surgical merge** over the 10
banner pages (re-crawl → replace the leading bare `<h1>` with the
`page_banner`, append matched CTA buttons to each `media_text` section by
heading text) — every other block and all prior hand edits (e.g. the
normalized "Free Cybersecurity eBook" title) left untouched.

## Pass 3 — full reset→import→verify pass 2026-09-09

Ran the whole dev-site workflow end to end against `dev.stratecon.tech`
(headed real Chrome): WP Reset (38→2 pages, 96→0 media, users wiped to
`claude-agent`) → permalinks → import `stratecon-migration.xml` with
attachments → publish → repair plugin → cache purge (WP-Optimize + SG) →
verify **logged out** (`x-proxy-cache: MISS` on every page) → recreate
`cmatheid` admin.

- **Repair plugin report:** 17 broken image URLs repointed, 71 stock
  images sideloaded (0 skipped), front page set (home, id 100), site
  logo set (attachment 40004).
- **Logged-out spot checks (9 pages):** all HTTP 200 except the expected
  404s; **0 broken images** anywhere; logo on every page; no shortcode
  leaks. Home `<title>` + FAQ (`<details>`×3) correct; About title
  correct; `ai-use-policy-template` renders the inline PDF `<object>`;
  `cybersecurity-solutions` renders the 10-card 2-up Insights grid +
  "Free Cybersecurity eBook"/"Cybersecurity Insights" headings + the
  eBook placeholder; `cyber-risk-assessment` renders the intro →
  "Cyber Risk Self Assessment" heading → the 23-field placeholder panel
  ("import fluentforms-migration.json, then swap the shortcode" — the
  Fluent Forms import + `[fluentform]` swap stays a manual go-live step,
  as designed) → "What Next?".
- **`cmatheid` admin recreated** (Administrator).
- **One issue found and fixed:** the WordPress sample Privacy Policy page
  was still live at `/privacy-policy-2/` serving core boilerplate —
  publish-order edge case in the repair plugin's step 5. Fixed and
  re-verified (see the privacy-policy item below).
- **Note:** the verification script bulk-published *every* draft,
  including WordPress's own "Sample Page" and sample "Privacy Policy".
  The documented workflow publishes only the imported pages; a careful
  operator should leave WordPress's two default drafts alone (or trash
  "Sample Page").

## Pass 2 — full dev-vs-live pass 2026-09-08

Re-ran the structured + visual diff across all 37 pairs (logged out,
cache purged) after step 5, the FAQ accordion, and the card-row wrap.
**0 broken images on any page**; body text 90–98% of live everywhere;
logo/nav/fonts/footer/brand consistent; FAQ accordion and the
Communications 2×3 card layout now match live. Two real gaps and a set
of by-design differences:

### Open — real gaps

- **`/privacy-policy/` slug collision — DONE.** WP Reset re-seeds a
  sample "Privacy Policy" page every cycle, so the WXR import can't claim
  the slug and the migrated page lands at `/privacy-policy-2/`. The
  repair plugin's step 5 identifies the sample page by its
  "Suggested text:" boilerplate, trashes it (freeing the slug), moves
  the migrated page onto `/privacy-policy/`, and repoints
  `wp_page_for_privacy_policy`. Idempotent.
  **Hardened 2026-09-09** after the full verification pass surfaced an
  edge case: if the operator publishes WordPress's own sample "Privacy
  Policy" draft alongside the imported pages, `wp_unique_post_slug()` on
  publish can hand the *migrated* page `/privacy-policy/` directly and
  bump the *sample* to `/privacy-policy-2/`. The old code gated the whole
  cleanup on `migrated->post_name !== 'privacy-policy'`, so in that
  ordering it skipped everything and left WordPress's boilerplate live at
  `/privacy-policy-2/`. Now the sample is trashed whenever a distinct
  migrated Privacy Policy page exists; the re-slug is the only
  slug-gated step. Verified on a full fresh reset→import→publish→repair
  pass: `/privacy-policy/` serves the real Stratecon policy (9,761
  chars, `suggested_text=false`), `/privacy-policy-2/` 404s (it was
  never a real crawled URL; core's `_wp_old_slug` redirect doesn't apply
  across two different posts for a bare page URL, so a 404 is the
  outcome).

- **`cyber-risk-assessment` self-assessment form — DONE (rebuilt in
  Fluent Forms).** The live page embeds a 20-question Cognito Forms
  questionnaire (Network Security / Endpoint Protection / … rated 1–5,
  a free-text concerns box, a follow-up dropdown, plus name/email) — the
  whole point of the page, and the crawler never saw the cross-origin
  embed. Scraped the live Cognito form's structure, modelled it as a
  `contact_form` block (heading + 37 fields: 4 custom-HTML notes, name,
  email, 10 section breaks, 19 rating radios, 1 textarea, 1 dropdown)
  inserted between the intro and "What Next?" in `structured_content.json`.
  `build_fluentform_form_fields()` gained `radio` / `select` /
  `section` / `html` field types; `build_fluentforms_export()` now emits
  the exact FF export shape including a `metas` array (FF's import tool
  ignores the legacy `form_meta`-only shape, which is why the migrated
  forms had been importing with no `formSettings` and their
  `[fluentform]` shortcode rendered nothing). Verified on the dev site:
  imported all 3 forms, `[fluentform]` renders the full assessment
  (95 radio inputs = 19×5, the dropdown, the textarea, section headers,
  consent, Submit); placed on `/cyber-risk-assessment/` it matches the
  live page's structure. Import ships a default admin-email
  notification.

- **Generic third-party form-embed detection — DONE.** The
  cyber-risk-assessment fix above was per-site and manual; the crawler
  now auto-detects the class of problem. `EMBED_FORM_PROVIDERS` +
  `detect_embedded_forms()` in `crawler_agent.py` scan every page for an
  `<iframe>` / loader `<script>` / builder placeholder `<div>` from ~20
  external form builders (Cognito Forms, JotForm, Typeform, Google Forms,
  Microsoft Forms, HubSpot, Wufoo, Formstack, Tally, Paperform, …), skip
  site chrome, and collapse to one hit per provider per page (a builder's
  embed matches two–three times on one page — loader, placeholder, and
  injected iframe). `extract_blocks()` emits an `embedded_form` block
  (`provider`, `src`, `title`); the generator renders it as a labelled
  `_form_placeholder` panel with a QA flag saying to rebuild the form in
  Fluent Forms (the cyber-risk-assessment questionnaire is the worked
  example) or re-embed via the provider's own block, and `qa_report.md`
  gets a "Third-party form embeds" callout listing the pages + providers.
  Verified: unit test over a fixture (Cognito iframe + Typeform
  script/div + JotForm + Google Forms detected; nav/footer embeds and a
  non-provider iframe correctly ignored; three Cognito matches collapse
  to one, keeping the real iframe URL + title), and the `embedded_form`
  block renders a clean placeholder with no literal shortcode. The
  current `structured_content.json` isn't re-crawled, so
  `cyber-risk-assessment` keeps its hand-built Fluent Forms model; a
  future re-crawl of this site (or a first crawl of the next client's)
  now surfaces every such embed instead of dropping it silently.

### By-design / cosmetic — confirm acceptable

- **Blog posts (16) lose the right sidebar — ACCEPTED (Carver, 2026-09-09).**
  GoDaddy's Categories nav, Recent Posts widget, and inline "Sign up for
  blog updates" form are intentionally not reproduced. Body content is
  complete and correct, and the blog posts import as WordPress *pages*
  (not posts), so core Recent Posts/Categories blocks wouldn't populate
  anyway — reproducing the rail would mean a hand-maintained link list.
  Carver's call: drop it. "Share this post:" stays as text (no social
  icons on dev). If a real blog is stood up later on the WP `post` type,
  the theme's own sidebar/widgets cover this.
- **Newsletter / "Stay Informed" is a placeholder** on the home page,
  contact, and 17 blog posts — live has a designed inline signup (the
  home one sits in a bokeh-background band). Blocked on the ESP choice.
- **Contact / eBook forms are dashed placeholder panels** on the 4
  form pages; their instruction text literally contains
  `[fluentform id="…"]` (by design, punchlist #4 — one-time shortcode
  swap at go-live). On `cybersecurity-solutions` the eBook placeholder
  was **orphaned at the very bottom** of the page instead of in the
  "Free Cybersecurity eBook" section. **DONE** — reordered the blocks in
  `structured_content.json` so `contact_form` sits before `post_feed`;
  the placeholder now renders in the eBook section, Insights feed after.
  Verified on the dev site (section order:  Free Cybersecurity eBook →
  eBook placeholder → post-feed grid).
- **Post-feed grids 2-up + section heading — DONE.** `post_feed` now
  renders rows of 2 (matching live's Insights grid) and carries a
  "Cybersecurity Insights" / "AI Insights" HeadingBeta section heading
  on the two landing pages. Verified on a fresh dev import. Still not
  reproduced: the live "All Posts | <category>" filter tabs above the
  grid (a GoDaddy widget control).
- **PDF-widget pages — DONE.** `ai-use-policy-template` and
  `ai-disclosure-template` now render a `core/file` block with
  `displayPreview` — an inline `<object>` PDF viewer (760px) plus a
  Download button, matching the live page's in-page viewer. Verified on
  the dev site: 0 block-editor validation warnings; the cross-origin
  GoDaddy CDN PDF loads and paints the full Chrome PDF viewer
  (thumbnails, page nav, zoom). QA flag still asks for a Media Library
  re-host before go-live (repoint both `href` and the `<object data>`).
- **Hero** rebuilt as a full-bleed navy cover with centered text vs
  live's translucent white box over the globe image. Deliberate.

## Tier 1 — systemic, many pages

### 1. `[contact-form-7 id="TBD" title="Form"]` renders as literal body text (~20 pages)

Contact (×2), every blog post, `cybersecurity-solutions`, and several
solution pages show the raw shortcode as visible copy. Root cause is in
`crawler_agent.py`: the `forms` loop does `page.query_selector_all("form")`
with no chrome filter, so it captures the **sitewide footer newsletter
`<form>`** on nearly every page, and it also mis-reads the GoDaddy
"Insights" post-feed on `cybersecurity-solutions` as a form. The generator
then emits a `forms_detected` / `contact_form` block that renders the CF7
placeholder (no CF7 plugin installed → shortcode prints literally).

Fix direction:
- Exclude `CHROME_SELECTOR` (footer/header/nav) from form detection.
- The footer newsletter belongs to the `newsletter_signup` path only —
  don't also emit `forms_detected` for it.
- The "Cybersecurity Insights" block on `cybersecurity-solutions` is a
  `post_feed`, not a form.

### 2. Every blog post is topped with a bogus `<h1>Stratecon Tech Insights</h1>` + blog-index intro

16 posts. The heading "Stratecon Tech Insights" plus the paragraph
"Please check back here often for our most recent insights. If there is a
topic you would like us to see cover, please Contact Us to submit your
ideas." appear at the top of every individual post; the real post title
renders below as a smaller (h3) heading. Crawling `/blog/f/<slug>` is
pulling in the blog *listing* section header/intro.

Fix direction:
- Strip the listing header + intro paragraph when extracting an individual
  post.
- Promote the post's own title to the page-level `<h1>`.

## Tier 2 — page-specific content gaps

### 3. `cybersecurity-solutions` — "Cybersecurity Insights" card grid (~12 posts) missing — FIXED

Was replaced by the CF7 placeholder (same root cause as #1) and ~60% of
the page's body text was gone. Root cause was a second bug: the RSS-feed
widget mounts its post cards only on scroll-into-view, and on this long
page `crawl()`'s single step-scroll pass didn't give it enough settle
time, so `mark_post_feeds()` saw an empty grid. Fixed with
`settle_lazy_widgets()` in `crawler_agent.py` — scrolls each
`RSS_FEEDS_RENDERED` grid into view and waits for its `[data-ux="Card"]`
children before extraction. Verified on the dev site: all 10 Insights
cards (heading, link, thumbnail, date, categories, excerpt) now render as
a 3-column grid matching live. The eBook form is a clean placeholder
panel (per #1); its Name/Email/Company fields are captured.

### 4. Contact forms — Fluent Forms — DONE

Form plugin chosen: **Fluent Forms**. The crawler already captures each
contact form's real fields (Name / Email / Message / Company / opt-in).
The generator now:

- writes **`fluentforms-migration.json`** — a Fluent Forms native import
  file, one form per unique captured contact form, fields already mapped
  to Fluent Forms' own field types. Verified on the dev site: imports
  clean via Fluent Forms → Tools → Import Forms and produces valid,
  correctly-fielded forms (First/Last Name, Email, Message, Email opt-in
  checkbox, Send).
- keeps the clean placeholder panel on each page, now worded for the
  Fluent Forms workflow: import the JSON, then replace the block with the
  form's `[fluentform id="N"]` shortcode (Fluent Forms shows the
  shortcode on its Forms list). QA report lists each form and its pages.

Two remaining manual touches per go-live, both one-click: paste the
shortcode in place of each placeholder, and add an email notification to
each form in Fluent Forms. (An attempt to have the repair plugin create
the forms and swap the shortcodes automatically was dropped — building
Fluent Forms' form rows by direct DB insert from an activation hook was
too fragile; the sanctioned Import Forms path is reliable.)

### 5. `the-spider-mantm-dilemma-building-an-ai-strategy` — body content duplicated — FIXED

Every bulleted item in the "AI Use Policies" and "AI Disclosure
Statements" sections appeared twice — once as a flattened paragraph, once
as a list. GoDaddy wraps a `<ul>` *inside* a `<p>` (invalid HTML, still
rendered); the crawler's document-order loop emitted the `<p>` (list text
flattened to prose) and then the nested `<ul>` (as a proper list). The
`<p>` handler in `crawler_agent.py` now skips when it contains a nested
list, keeping only any real lead-in text; the list is still captured on
its own iteration. Verified on the dev site: each bullet appears once, as
a list.

### 6. Spot-check thin pages — DONE

- **`cyber-risk-assessment`** — not a real gap. The "53% of live" flag was
  the diff heuristic counting the live site's cookie-consent banner; the
  migrated content matches live. No change.
- **`contact-us`** — the live URL is a hard **HTTP 404** ("Page Not
  Found"). The crawler had followed a dead link and saved GoDaddy's 404
  page as a migrated page. Fixed: `crawl()` now checks `response.status`
  and skips 4xx; `contact-us` removed from the data (38 → 37 pages).
- **`ai-use-policy-template` / `ai-disclosure-template`** — real gap.
  These are a GoDaddy "PDF" widget (`widget-pdf`); only the intro text
  plus a stray "1/5" / "1/4" page-counter got migrated. Fixed:
  `mark_pdf_widgets()` / `extract_pdf_widget()` in the crawler emit a
  `document_embed` block (title, sub-heading, description, and the real
  PDF URL from the "Download PDF" link); the pdf.js viewer chrome is
  dropped. The generator renders it as an `<h1>` + intro + a **Download
  PDF** button pointing at the file on the old CDN (still reachable),
  QA-flagged for a manual re-host into the Media Library before go-live.
  Verified on the dev site: both pages have their title as the page
  `<h1>`, no "1/5" artifact, working download button.
  (An attempt to have the repair plugin sideload the PDFs automatically
  was dropped — `download_url()` to the GoDaddy CDN from SiteGround's
  datacenter IP is blocked/hangs and was killing the activation request.)

## Tier 3 — cosmetic / low

- **7. Post-feed layout — DONE.** The live "Insights" grid uses equal-height
  cards *with a white background + 1px `#e2e2e2` border*, so the space
  under a short card's text reads as card padding. Ours had no card
  styling and also pinned "Continue Reading" to the bottom with
  `margin-top:auto`, doubling the gap. Fixed: added
  `.migration-post-feed-card` (white bg, `#e2e2e2` border, radius,
  padding, image flush to the top edge) and dropped the bottom-pin on
  "Continue Reading". Verified on the dev site (`ai-solutions`,
  `cybersecurity-solutions`): bordered cards matching live, 0
  block-editor validation warnings.
- **8. Broken image on `threat-id-%26-detection` — DONE.** Nothing to do
  with the `%26` slug. Two `$stock` entries existed for the same GoDaddy
  image — one with a `/rs=w:600,…` resize suffix, one without — and the
  repair plugin matched the shorter URL as a substring of the longer
  one's `<img src>`, replacing only part of it and leaving a dangling
  `.../rs=w:600,…` that 404s. Fixed: the repair plugin now `uksort`s
  `$stock` by URL length descending so the most-specific URL is
  repointed first. Simulated against the real page content — all 4
  images repoint cleanly.
- **9. Text artifacts — DONE (mostly not bugs).**
  - "Contact Us us" — the doubled "us" is **a typo in the live GoDaddy
    content** ("Please Contact Us us if you cannot find an answer…"), not
    a migration artifact. Faithfully reproduced. Fix at the source, or
    the LLM Content Structuring Agent (step 5) would catch it.
  - Trailing "source" citation links — the live site renders these inline
    at body size too, so it's faithful. Small polish applied: the crawler
    now wraps a link whose text is just "source" in `<sup>` so it reads
    as a superscript reference. Also added `<sup>`/`<sub>` to the
    preserved inline tags generally. Verified on the dev site.
  - "Click here to take our free cyber risk assessment" — no longer
    present in the data (gone after the #5 re-crawl).

## Already solid (verified this pass)

- 0 broken images on 37/38 pages; **0 offsite hot-linked images anywhere**
  (the repair plugin worked — every image served from `dev.stratecon.tech`).
- Home hero + single correct page-level `<h1>`.
- Solution pages (`connectivity`, `ai-for-sales-1`, `threat-protection`,
  `unified-communications`, `customer-experience`, `services`,
  `communications-solutions`, …) match live closely on headings, image
  count, and layout. **About is essentially pixel-faithful.**
- Nav, footer, fonts, brand colours all consistent.

## Recommended order

#1 + #2 together — both are crawler-extraction fixes, both sitewide, and
they account for most of the remaining gap. #3 and #5 fall out of the same
"post-feed vs form" confusion. #4 is blocked on the plugin choice.

(All of #1–#9 are since closed — see the per-item "DONE" notes above.)

## Content Structuring Agent (step 5) — status

`content_structuring_agent.py` runs after the crawl, before the
generator. Where it stands against this list:

- **FAQ restructuring — done, verified offline.** The home page's
  accordion (questions as toggles, answers in loose paragraphs) is now
  one clean `faq` block, no orphan paragraphs, no duplicated blob.
  `--offline` does deterministic in-order pairing, which is correct for
  stratecon.tech; the LLM pass additionally handles reordered /
  multi-paragraph answers and copy-edits obvious typos (e.g. the
  "Contact Us us" doubling noted in #9).
- **Incremental re-runs — done.** Each page carries
  `_structured_agent_version` + `_structured_blocks_hash`; a normal run
  re-processes only pages whose blocks changed or that an older agent
  version structured. `--force` overrides; `--pages` always re-does the
  named pages.
- **Meta title + description — done.** All 37 pages now carry a
  hand-reviewed SEO title (≤60 chars) and a 147–164-char meta
  description drawn from the page's own content, imported as Yoast
  `_yoast_wpseo_title` / `_yoast_wpseo_metadesc`. Because this
  environment has no Anthropic API key, the semantic pass was run *in
  the Claude Code session* (Pro subscription covers it) rather than by
  `content_structuring_agent.py`'s API call — output is identical, just
  produced by hand this once. The automated agent takes over for the
  next (unknown) client site once an API key is available.
- **Image alt text — done.** The three `image` blocks that lacked alt
  (`ai-and-data-analytics…`, `top-5-security-considerations…`,
  `how-ai-can-transform…`) now have literal alt text written from the
  actual images, viewed in-session. Cards, hero, and media+text images
  already carried GoDaddy's stock-photo alt.

Verified: `generator_agent.py` regenerated, XML well-formed, 37/37
`_yoast_wpseo_title` + `_yoast_wpseo_metadesc` items, 0 images missing
alt, `&` correctly escaped in titles.

**Verified on the dev site (2026-09-08).** Full reset → import → publish
→ activate Yoast SEO → repair plugin → verify. On the live front end:
`<title>` and `<meta name="description">` match the generated values on
every sampled page (8/8, incl. the `&`-escaped "AI & Data Analytics"
title); all three images render with their generated `alt`; the home
FAQ renders as heading + three question `<h3>`s. No regressions — repair
report: 17 broken image URLs repointed, 71 stock images sideloaded,
front page set, site logo set (attachment 40004); 0 broken images on
the home page; `cmatheid` admin recreated. (Incidental: Yoast warns
"you're blocking access to robots" — that's the staging site's
deliberate "Discourage search engines" setting, not a migration issue.)
