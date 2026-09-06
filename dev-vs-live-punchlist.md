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

### 3. `cybersecurity-solutions` — "Cybersecurity Insights" card grid (~12 posts) missing

Replaced by the CF7 placeholder (same root cause as #1). ~60% of the
page's body text is gone. The "Free Cybersecurity eBook" form fields are
also not captured (only the surrounding text).

### 4. Contact page — real contact form gone

Live has a contact form (name/email/message) plus a newsletter form. Dev
keeps the text (phone, hours, "Drop us a line") but both forms are
`[contact-form-7 id="TBD"]` placeholders. Blocked on a **form-plugin
decision** (Contact Form 7 / Fluent Forms / WPForms / Gravity) plus real
per-field capture in the crawler.

### 5. `the-spider-mantm-dilemma-building-an-ai-strategy` — body content duplicated

Dev body ~1.5× live. Likely the FAQ-dupe / repeated-section bug. Needs a
targeted look.

### 6. Spot-check thin pages

`cyber-risk-assessment`, `ai-use-policy-template`, `ai-disclosure-template`
show notably less body text than live with a matching heading outline —
possible capture loss. `contact-us` is near-empty on live too (~580
chars), probably a non-issue.

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
