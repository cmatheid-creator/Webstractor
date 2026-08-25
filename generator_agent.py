#!/usr/bin/env python3
"""
Generator Agent (prototype)
----------------------------
Consumes the Content Structuring Agent's output (structured_content.json)
and produces:
  1. stratecon-migration.xml  -> a standard WordPress WXR file, ready to
     import via Tools > Import > WordPress on any WP install.
  2. redirects.csv            -> old-URL -> new-URL map, ready to import
     into the free "Redirection" plugin.
  3. qa_report.md             -> plain-English summary a non-technical
     client could read before go-live.

This is a working, standalone script -- not a mockup. Run it again on a
richer structured_content.json (more pages, images, etc.) and it produces
a bigger, still-valid WXR file. The block-building logic below is the
seed of the real "Generator Agent" in the multi-agent pipeline.
"""

import json
import html
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

# GoDaddy Website Builder can't do real nested nav menus, so some source
# sites fake a sub-item look by prefixing the page <title> itself with a
# dash (e.g. "- AI Strategy" under an "AI" category). The real WP site
# gets an actual parent/child menu, so that prefix is markup cruft, not
# content -- strip it before it lands in a migrated page title.
LEADING_DASH_TITLE = re.compile(r"^[-–—]\s+")


def clean_title(title):
    return LEADING_DASH_TITLE.sub("", title)


SRC = "structured_content.json"
OUT_WXR = "stratecon-migration.xml"
OUT_REDIRECTS = "redirects.csv"
OUT_QA = "qa_report.md"

NEW_BASE_URL = "https://staging.stratecon-newsite.example"  # placeholder staging URL


def block_to_gutenberg(block):
    """Turn one structured content block into native Gutenberg block markup."""
    t = block["type"]

    if t == "heading":
        level = block.get("level", 2)
        text = html.escape(block["text"])
        return (
            f'<!-- wp:heading {{"level":{level}}} -->\n'
            f'<h{level} class="wp-block-heading">{text}</h{level}>\n'
            f'<!-- /wp:heading -->'
        )

    if t == "paragraph":
        text = html.escape(block["text"])
        return f'<!-- wp:paragraph -->\n<p>{text}</p>\n<!-- /wp:paragraph -->'

    if t == "list":
        items = "".join(f"<li>{html.escape(i)}</li>" for i in block["items"])
        return (
            '<!-- wp:list -->\n'
            f'<ul class="wp-block-list">{items}</ul>\n'
            '<!-- /wp:list -->'
        )

    if t == "faq":
        parts = []
        for item in block["items"]:
            q = html.escape(item["q"])
            a = html.escape(item["a"])
            parts.append(
                '<!-- wp:heading {"level":3} -->\n'
                f'<h3 class="wp-block-heading">{q}</h3>\n'
                '<!-- /wp:heading -->\n'
                '<!-- wp:paragraph -->\n'
                f'<p>{a}</p>\n'
                '<!-- /wp:paragraph -->'
            )
        return "\n\n".join(parts)

    if t == "newsletter_signup":
        label = html.escape(block.get("label", "Newsletter"))
        text = html.escape(block.get("text", ""))
        return (
            '<!-- wp:heading {"level":2} -->\n'
            f'<h2 class="wp-block-heading">{label}</h2>\n'
            '<!-- /wp:heading -->\n'
            '<!-- wp:paragraph -->\n'
            f'<p>{text}</p>\n'
            '<!-- /wp:paragraph -->\n'
            '<!-- wp:shortcode -->[newsletter_signup_form]<!-- /wp:shortcode -->'
            '\n<!-- QA FLAG: replace shortcode with the real WP newsletter/email plugin block -->'
        )

    if t == "contact_form":
        note = html.escape(block.get("note", ""))
        return (
            '<!-- wp:shortcode -->[contact-form-7 id="TBD" title="Contact form"]<!-- /wp:shortcode -->\n'
            f'<!-- QA FLAG: {note} -->'
        )

    if t == "forms_detected":
        # Real per-field form data the crawler found -- surface it as a QA
        # note with a placeholder shortcode, same shape as contact_form,
        # rather than falling through to the generic unhandled-type
        # placeholder (which would show raw "[Unhandled block type]" text
        # to site visitors on every page with a form).
        parts = []
        for i, fields in enumerate(block.get("forms", []), 1):
            field_desc = ", ".join(
                f"{f['name'] or '(unnamed)'} ({f['type']})" for f in fields
            ) or "no fields detected"
            parts.append(
                '<!-- wp:shortcode -->[contact-form-7 id="TBD" title="Form"]<!-- /wp:shortcode -->\n'
                f'<!-- QA FLAG: form {i} on this page had fields: {field_desc} -- '
                'confirm against the live site and wire to the real form plugin. -->'
            )
        return "\n\n".join(parts)

    if t == "images_detected":
        # Image download/re-hosting isn't built yet (see CLAUDE.md). Note
        # it in an HTML comment for QA instead of leaking visible
        # placeholder text into the page -- missing images are silent by
        # default until that agent exists, not a visible defect on every
        # migrated page.
        count = len(block.get("images", []))
        return (
            f'<!-- QA FLAG: {count} image(s) detected on the source page '
            'but not migrated -- image download/re-hosting not yet built. -->'
        )

    if t == "faq_raw_unverified":
        note = html.escape(block.get("note", ""))
        parts = [f'<!-- QA FLAG: {note} -->']
        for raw in block.get("raw_text_blocks", []):
            text = html.escape(raw)
            parts.append(f'<!-- wp:paragraph -->\n<p>{text}</p>\n<!-- /wp:paragraph -->')
        return "\n\n".join(parts)

    return f'<!-- wp:paragraph --><p>[Unhandled block type: {t}]</p><!-- /wp:paragraph -->'


def build_item_xml(page, post_id):
    blocks_md = "\n\n".join(block_to_gutenberg(b) for b in page["blocks"])
    title = xml_escape(clean_title(page["title"]))
    slug = page["slug"]
    meta_desc = xml_escape(page.get("meta_description", ""))
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    post_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    return f"""  <item>
    <title>{title}</title>
    <link>{NEW_BASE_URL}/{slug}/</link>
    <pubDate>{pub_date}</pubDate>
    <dc:creator><![CDATA[migration-agent]]></dc:creator>
    <guid isPermaLink="false">{NEW_BASE_URL}/?page_id={post_id}</guid>
    <description></description>
    <content:encoded><![CDATA[{blocks_md}]]></content:encoded>
    <excerpt:encoded><![CDATA[]]></excerpt:encoded>
    <wp:post_id>{post_id}</wp:post_id>
    <wp:post_date><![CDATA[{post_date}]]></wp:post_date>
    <wp:post_date_gmt><![CDATA[{post_date}]]></wp:post_date_gmt>
    <wp:comment_status><![CDATA[closed]]></wp:comment_status>
    <wp:ping_status><![CDATA[closed]]></wp:ping_status>
    <wp:post_name><![CDATA[{slug}]]></wp:post_name>
    <wp:status><![CDATA[draft]]></wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>0</wp:menu_order>
    <wp:post_type><![CDATA[page]]></wp:post_type>
    <wp:post_password><![CDATA[]]></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
    <wp:postmeta>
      <wp:meta_key><![CDATA[_yoast_wpseo_metadesc]]></wp:meta_key>
      <wp:meta_value><![CDATA[{meta_desc}]]></wp:meta_value>
    </wp:postmeta>
  </item>"""


def build_wxr(data):
    site = data["site"]
    items_xml = []
    post_id = 100
    for page in data["pages"]:
        items_xml.append(build_item_xml(page, post_id))
        post_id += 1

    channel_title = xml_escape(site["title"])
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:wfw="http://wellformedweb.org/CommentAPI/"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
  <title>{channel_title}</title>
  <link>{NEW_BASE_URL}</link>
  <description>Migrated from {site['old_domain']} by the automated migration pipeline (prototype)</description>
  <pubDate>{now}</pubDate>
  <language>en-US</language>
  <wp:wxr_version>1.2</wp:wxr_version>
  <wp:base_site_url>{NEW_BASE_URL}</wp:base_site_url>
  <wp:base_blog_url>{NEW_BASE_URL}</wp:base_blog_url>
{chr(10).join(items_xml)}
</channel>
</rss>
"""


def build_redirects_csv(data):
    lines = ["Source URL,Target URL"]
    for page in data["pages"]:
        old = page["old_url"]
        new = f"/{page['slug']}/" if not page.get("is_front_page") else "/"
        lines.append(f"{old},{new}")
    for item in data["navigation"]:
        if "old_url" in item and item.get("status") == "not_yet_extracted":
            lines.append(f"{item['old_url']},/PENDING-EXTRACTION/")
    return "\n".join(lines) + "\n"


def build_qa_report(data):
    pages = data["pages"]
    extracted = len(pages)
    flags = data.get("qualification_flags", {})
    pending = sum(
        1
        for item in data.get("navigation", [])
        for child in item.get("children", [item] if "old_url" in item else [])
        if child.get("status") == "not_yet_extracted"
    )

    def count_blocks(block_type):
        return sum(1 for p in pages for b in p["blocks"] if b["type"] == block_type)

    forms_count = count_blocks("forms_detected")
    images_count = sum(
        len(b.get("images", []))
        for p in pages
        for b in p["blocks"]
        if b["type"] == "images_detected"
    )
    faq_unverified_count = count_blocks("faq_raw_unverified")
    newsletter_count = count_blocks("newsletter_signup")
    contact_form_count = count_blocks("contact_form")

    lines = []
    lines.append(f"# Migration QA Report — {data['site']['title']}")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **{extracted} pages** fully extracted, structured, and converted to a ready-to-import WordPress file.")
    if pending:
        lines.append(f"- **{pending} pages** in the site navigation were not yet crawled and are not included in this file.")
    if flags:
        lines.append(f"- **{len(flags)} pages** were flagged by the qualification check (possible login/payment/forum area) and excluded from this file — see below.")
    else:
        lines.append("- **0 payment, login, or account features detected** on the pages processed — consistent with an informational-site profile.")
    lines.append("")
    lines.append("## Items flagged for human review before go-live")
    lines.append("")
    if contact_form_count:
        lines.append(f"- **Contact form fields** ({contact_form_count} page(s)): the exact fields on the live contact form weren't fully visible in the extracted content. The generated page includes a placeholder form block — confirm the real field set before publishing.")
    if forms_count:
        lines.append(f"- **Forms detected** ({forms_count} page(s)): field names/types were captured from the live DOM and noted in an HTML comment on each generated page — confirm against the live site and wire to the real form plugin before publishing.")
    if newsletter_count:
        lines.append(f"- **Newsletter signup** ({newsletter_count} page(s)): mapped to a placeholder shortcode. Needs to be wired to whichever email tool (Mailchimp, etc.) the new site will use.")
    if images_count:
        lines.append(f"- **Images** ({images_count} detected across the crawled pages): not migrated in this run — image download/re-hosting isn't built yet. Noted per-page in an HTML comment so nothing is silently missing, but no images will appear on the imported pages until that's built.")
    if faq_unverified_count:
        lines.append(f"- **Low-confidence FAQ/accordion extraction** ({faq_unverified_count} page(s)): pulled via a broad DOM selector rather than verified Q&A structure — review before publishing.")
    if flags:
        lines.append(f"- **{len(flags)} page(s) excluded** by the qualification check:")
        for url, reasons in flags.items():
            lines.append(f"  - {url} — {'; '.join(reasons)}")
    if pending:
        lines.append(f"- **{pending} page(s)** in the site navigation were not yet crawled — flagged as pending, not dropped.")
    lines.append("")
    lines.append("## What's in the attached files")
    lines.append("")
    lines.append("- `stratecon-migration.xml` — import via **Tools → Import → WordPress** on any WordPress site (install the free WordPress Importer plugin if prompted). Pages import as **drafts** so nothing goes live automatically.")
    lines.append("- `redirects.csv` — import into the free **Redirection** plugin to preserve old URLs once the new site goes live.")
    return "\n".join(lines) + "\n"


def main():
    with open(SRC) as f:
        data = json.load(f)

    with open(OUT_WXR, "w") as f:
        f.write(build_wxr(data))

    with open(OUT_REDIRECTS, "w") as f:
        f.write(build_redirects_csv(data))

    with open(OUT_QA, "w") as f:
        f.write(build_qa_report(data))

    print(f"Wrote {OUT_WXR}, {OUT_REDIRECTS}, {OUT_QA}")


if __name__ == "__main__":
    main()
