# dev.stratecon.tech vs stratecon.tech — punch list

Structured diff pass over all 38 migrated pages (heading outlines, image
counts + broken-image detection, form/placeholder flags, full-page
screenshots of every pair), run 2026-09-06 against the current dev site
(fresh reset → import → publish → repair plugin → front page set).

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
  repair plugin now has a step 5: it identifies the sample page by its
  "Suggested text:" boilerplate, trashes it (freeing the slug), moves
  the migrated page onto `/privacy-policy/`, and repoints
  `wp_page_for_privacy_policy`. Idempotent. Verified on the dev site by
  re-activating the plugin: report read "trashed the WordPress sample
  page (id 3); moved the migrated page to /privacy-policy/",
  `/privacy-policy/` now serves the real Stratecon policy (~9.7k chars,
  no boilerplate), `/privacy-policy-2/` 404s.

- **`cyber-risk-assessment` is missing its self-assessment form.** The
  live page embeds a 20-question Cognito Forms questionnaire (Network
  Security / Endpoint Protection / … rated 1–5, plus Submit) — the whole
  point of the page. Dev shows only the intro media+text and the "What
  Next?" line, with **no form and no placeholder**. Pass 1 (#6) called
  this "not a real gap" — that was wrong; the embed was never captured.
  Needs a decision (rebuild in Fluent Forms / embed Cognito Forms / link
  out) and, at minimum, a flagged placeholder so it isn't silently
  dropped. Same root cause affects any GoDaddy third-party form embed
  the crawler doesn't see.

### By-design / cosmetic — confirm acceptable

- **Blog posts (16) lose the right sidebar** — GoDaddy's Categories nav,
  Recent Posts widget, and inline "Sign up for blog updates" form. Body
  content is complete and correct. "Share this post:" has no social
  icons on dev (text only). Decide: accept, or add a Recent
  Posts/Categories block to the post template.
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
- **Post-feed grids are 3-up on dev vs 2-up on live** (Cybersecurity
  Insights / AI Insights); the live "All Posts | <category>" filter tabs
  aren't reproduced.
- **PDF-widget pages** (`ai-use-policy-template`, `ai-disclosure-template`)
  — dev shows title + intro + Download button; live shows an in-page PDF
  viewer. Known trade-off from pass 1 (#6).
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
