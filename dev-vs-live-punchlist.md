# dev.stratecon.tech vs stratecon.tech — punch list

Structured diff pass over all 38 migrated pages (heading outlines, image
counts + broken-image detection, form/placeholder flags, full-page
screenshots of every pair), run 2026-09-06 against the current dev site
(fresh reset → import → publish → repair plugin → front page set).

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

### 4. Contact page — real contact form gone

Live has a contact form (name/email/message) plus a newsletter form. Dev
keeps the text (phone, hours, "Drop us a line") but both forms are
`[contact-form-7 id="TBD"]` placeholders. Blocked on a **form-plugin
decision** (Contact Form 7 / Fluent Forms / WPForms / Gravity) plus real
per-field capture in the crawler.

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

- **7.** `ai-solutions` / `blog` "AI Insights" feed renders more images than
  live plus the known vertical-whitespace/layout issue.
- **8.** One broken image on `threat-id-%26-detection` — the only page with
  `&` / `%26` in its slug; likely a URL-encoding edge case in the repair
  plugin's URL matching or the WXR.
- **9.** "Contact Us us" double word; "Click here to take our free cyber
  risk assessment" bare-link phrasing.

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
