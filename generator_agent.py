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
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

# GoDaddy Website Builder can't do real nested nav menus, so some source
# sites fake a sub-item look by prefixing the page <title> itself with a
# dash (e.g. "- AI Strategy" under an "AI" category). The real WP site
# gets an actual parent/child menu, so that prefix is markup cruft, not
# content -- strip it before it lands in a migrated page title.
LEADING_DASH_TITLE = re.compile(r"^[-–—]\s+")


def clean_title(title):
    return LEADING_DASH_TITLE.sub("", title)


IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|svg|avif)$", re.I)


def image_slug(url):
    """Derive a filesystem-safe slug from an image URL's own filename.

    GoDaddy's CDN often appends a resize suffix after the real filename
    (e.g. ".../photo.jpg/:/rs=w:400,h:300"), so the last path segment
    isn't reliably the filename -- scan segments for one that actually
    ends in an image extension instead. Falls back to a stable hash if
    none is found, so re-runs on the same data produce the same slug.
    """
    for segment in urlparse(url).path.split("/"):
        if IMAGE_EXT_RE.search(segment):
            base = IMAGE_EXT_RE.sub("", segment)
            slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
            if slug:
                return slug
    return "image-" + hashlib.sha1(url.encode()).hexdigest()[:10]


def canonical_attachment_url(url):
    """The URL WordPress's importer can actually download, or None if
    there isn't one.

    WordPress's fetch_remote_file() rejects a download whose URL doesn't
    end in a recognized image extension -- it checks the URL string
    itself (basename of the path), not what the server actually returns.
    GoDaddy's CDN puts the real filename+extension mid-path, followed by
    resize/crop parameters (".../photo.png/:/rs=w:400,h:300"), so the
    last path segment is never a real extension and every one of these
    URLs fails that check as-is.

    Confirmed against the live CDN: truncating the URL right after the
    extension (dropping the transform suffix) still serves the correct,
    full-resolution image -- but only when an extension exists somewhere
    in the path to truncate at. Many of this CDN's stock-photo URLs
    (".../stock/10130/:/cr=...") have no filename or extension anywhere,
    just an opaque ID -- confirmed the CDN rejects any tampered path
    (400/404), so there's no way to construct a URL WordPress will
    accept for these; they can't be auto-imported via this mechanism at
    all and are the caller's responsibility to flag, not silently drop.
    """
    parsed = urlparse(url)
    segments = parsed.path.split("/")
    for i, segment in enumerate(segments):
        if IMAGE_EXT_RE.search(segment):
            truncated_path = "/".join(segments[: i + 1])
            return f"{parsed.scheme}://{parsed.netloc}{truncated_path}"
    return None


SRC = "structured_content.json"
SRC_BRAND = "brand.json"
OUT_WXR = "stratecon-migration.xml"
OUT_REDIRECTS = "redirects.csv"
OUT_QA = "qa_report.md"
OUT_THEME = "theme.json"

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

    if t == "image":
        # The <img> src here still points at the original site's CDN --
        # see build_attachment_items()/build_wxr() for how the actual
        # file gets re-hosted into the new site's media library via a
        # WXR attachment item. Rewriting this src to the new site's
        # eventual upload URL isn't reliable to predict in advance (it
        # depends on WordPress's own filename-collision handling at
        # import time), so this is flagged for a manual swap once the
        # real media-library copy exists after import.
        src = xml_escape(block["src"])
        alt = xml_escape(block.get("alt", ""))
        return (
            '<!-- wp:image -->\n'
            f'<figure class="wp-block-image"><img src="{src}" alt="{alt}"/></figure>\n'
            '<!-- /wp:image -->\n'
            '<!-- QA FLAG: still points at the original site -- swap to the '
            're-hosted media-library copy after import. -->'
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


def collect_unique_images(pages):
    """Dedupe image blocks across all pages by URL (the same logo/icon
    typically repeats on every page) and return {url: alt} pairs."""
    images = {}
    for page in pages:
        for block in page["blocks"]:
            if block["type"] == "image" and block["src"] not in images:
                images[block["src"]] = block.get("alt", "")
    return images


def partition_images_by_importability(images):
    """Split {url: alt} into (importable, not_importable) based on
    whether canonical_attachment_url() found a usable URL. Images in
    not_importable still display fine in page content (hotlinked to the
    original site), they just can't get a real WXR attachment item --
    the caller is responsible for surfacing that, not silently dropping
    them."""
    importable, not_importable = {}, {}
    for url, alt in images.items():
        target = importable if canonical_attachment_url(url) else not_importable
        target[url] = alt
    return importable, not_importable


def build_attachment_item_xml(url, alt, attachment_id):
    """A WXR attachment item pointing at the original image URL. This is
    WordPress's own native mechanism for re-hosting external media: when
    "Download and import file attachments" is checked during import (the
    default), the importer fetches the file from wp:attachment_url itself
    and creates a real, independent copy in the new site's media library
    -- no custom download/hosting code needed here.

    Uses canonical_attachment_url(), not the raw url, as the actual
    wp:attachment_url -- WordPress's importer rejects a download whose
    URL doesn't end in a recognized image extension, and this CDN's
    transform-suffixed URLs never do. Caller must only pass urls that
    canonical_attachment_url() resolves; see
    partition_images_by_importability()."""
    slug = image_slug(url)
    title = xml_escape(alt or slug)
    alt_escaped = xml_escape(alt)
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    post_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    src = xml_escape(canonical_attachment_url(url))

    return f"""  <item>
    <title>{title}</title>
    <link>{NEW_BASE_URL}/{slug}/</link>
    <pubDate>{pub_date}</pubDate>
    <dc:creator><![CDATA[migration-agent]]></dc:creator>
    <guid isPermaLink="false">{src}</guid>
    <description></description>
    <content:encoded><![CDATA[]]></content:encoded>
    <excerpt:encoded><![CDATA[]]></excerpt:encoded>
    <wp:post_id>{attachment_id}</wp:post_id>
    <wp:post_date><![CDATA[{post_date}]]></wp:post_date>
    <wp:post_date_gmt><![CDATA[{post_date}]]></wp:post_date_gmt>
    <wp:comment_status><![CDATA[closed]]></wp:comment_status>
    <wp:ping_status><![CDATA[closed]]></wp:ping_status>
    <wp:post_name><![CDATA[{slug}]]></wp:post_name>
    <wp:status><![CDATA[inherit]]></wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>0</wp:menu_order>
    <wp:post_type><![CDATA[attachment]]></wp:post_type>
    <wp:post_password><![CDATA[]]></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
    <wp:attachment_url><![CDATA[{src}]]></wp:attachment_url>
    <wp:postmeta>
      <wp:meta_key><![CDATA[_wp_attachment_image_alt]]></wp:meta_key>
      <wp:meta_value><![CDATA[{alt_escaped}]]></wp:meta_value>
    </wp:postmeta>
  </item>"""


def build_wxr(data):
    site = data["site"]
    items_xml = []
    post_id = 100
    for page in data["pages"]:
        items_xml.append(build_item_xml(page, post_id))
        post_id += 1

    # Attachment IDs start well past the highest possible page post_id
    # (100 + one per page) so the two ranges can never collide.
    attachment_id = 10000
    importable, _ = partition_images_by_importability(collect_unique_images(data["pages"]))
    for url, alt in importable.items():
        items_xml.append(build_attachment_item_xml(url, alt, attachment_id))
        attachment_id += 1

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


def build_qa_report(data, brand=None):
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
    image_instances = count_blocks("image")
    importable_images, non_importable_images = partition_images_by_importability(
        collect_unique_images(pages)
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
    total_unique_images = len(importable_images) + len(non_importable_images)
    if total_unique_images:
        lines.append(
            f"- **Images** ({total_unique_images} unique, {image_instances} placements across "
            f"the crawled pages): {len(importable_images)} included as WXR attachment items "
            "pointing at the original site's URLs. Check **\"Download and import file "
            "attachments\"** during import (the default) so WordPress fetches real, "
            "independent copies into your media library. The inline image blocks on each "
            "page still reference the *original* site's URL, though — swap those to the new "
            "media-library copies before decommissioning the old site."
        )
        if non_importable_images:
            lines.append(
                f"- **{len(non_importable_images)} image(s) can't be auto-imported into the "
                "media library**: their source URLs (this site's stock-photo CDN links) have "
                "no filename or extension anywhere in the path, just an opaque ID -- "
                "WordPress's importer requires a recognized image extension in the URL itself "
                "and rejects these regardless of what the server actually returns. They still "
                "display correctly on the migrated pages (hotlinked to the original site), "
                "they just won't get an independent media-library copy automatically -- "
                "save them from the browser and upload manually if you want copies before "
                "decommissioning the old site."
            )
    if faq_unverified_count:
        lines.append(f"- **Low-confidence FAQ/accordion extraction** ({faq_unverified_count} page(s)): pulled via a broad DOM selector rather than verified Q&A structure — review before publishing.")
    if flags:
        lines.append(f"- **{len(flags)} page(s) excluded** by the qualification check:")
        for url, reasons in flags.items():
            lines.append(f"  - {url} — {'; '.join(reasons)}")
    if pending:
        lines.append(f"- **{pending} page(s)** in the site navigation were not yet crawled — flagged as pending, not dropped.")
    if brand:
        logo = brand.get("logo")
        colors = brand.get("colors", {})
        color_list = ", ".join(f"{k}: {v}" for k, v in colors.items() if v)
        lines.append(
            f"- **Brand tokens extracted**: {len(brand.get('typography', {}))} typography role(s), "
            f"colors ({color_list or 'none found'}). Included as `{OUT_THEME}` -- a WordPress "
            "block-theme color palette and font list, ready to drop into a block theme's "
            "theme.json (or use as a reference when configuring Site Editor colors/fonts by hand)."
        )
        if logo:
            lines.append(
                f"- **Logo** found at {logo['url']} -- not set automatically (that's done via "
                "Appearance → Editor → Site Identity in WordPress, not theme.json); download it "
                "from the original site and upload it there."
            )
    lines.append("")
    lines.append("## What's in the attached files")
    lines.append("")
    lines.append("- `stratecon-migration.xml` — import via **Tools → Import → WordPress** on any WordPress site (install the free WordPress Importer plugin if prompted). Pages import as **drafts** so nothing goes live automatically.")
    lines.append("- `redirects.csv` — import into the free **Redirection** plugin to preserve old URLs once the new site goes live.")
    if brand:
        lines.append(f"- `{OUT_THEME}` — the extracted color palette and font list in WordPress's block-theme format.")
    return "\n".join(lines) + "\n"


def build_theme_json(brand):
    """A WordPress block-theme theme.json fragment (settings.color.palette
    and settings.typography.fontFamilies) built from brand_agent.py's
    extracted tokens. Not a complete theme.json -- WP block themes need
    more than colors/fonts to function -- this is the piece a human (or
    a future Architecture Agent) merges into one, or uses as a reference
    when setting the palette/fonts by hand in the Site Editor."""
    colors = brand.get("colors", {})
    palette = []

    def add_color(slug, name, value):
        if value and value.startswith("#"):
            palette.append({"slug": slug, "color": value, "name": name})

    add_color("background", "Background", colors.get("background"))
    add_color("foreground", "Text", colors.get("text"))
    add_color("primary", "Primary (Button)", colors.get("button_background"))
    add_color("primary-text", "Primary Button Text", colors.get("button_text"))
    add_color("link", "Link", colors.get("link"))

    role_names = {
        "HeadingAlpha": "Heading",
        "HeadingBeta": "Heading (Secondary)",
        "HeadingDelta": "Heading (Tertiary)",
        "BodyAlpha": "Body",
        "ButtonAlpha": "Button",
        "LinkAlpha": "Link",
        "NavAlpha": "Navigation",
    }
    # When several roles share one font family -- typically a workhorse
    # font used for body text, nav, links, and buttons, plus a separate
    # display font just for large headings -- name it after whichever
    # role best represents how it's actually used, not whichever role
    # happened to be listed first (e.g. a font shared by BodyAlpha and
    # HeadingDelta is "Body", not "Heading (Tertiary)").
    name_priority = [
        "BodyAlpha", "HeadingAlpha", "HeadingBeta", "HeadingDelta",
        "NavAlpha", "LinkAlpha", "ButtonAlpha",
    ]
    roles_by_family = {}
    for role, info in brand.get("typography", {}).items():
        family = info.get("font_family")
        if family:
            roles_by_family.setdefault(family, []).append(role)

    font_families = []
    for family, roles in roles_by_family.items():
        best_role = min(
            roles,
            key=lambda r: name_priority.index(r) if r in name_priority else len(name_priority),
        )
        primary_name = family.split(",")[0].strip().strip("\"'")
        slug = re.sub(r"[^a-z0-9]+", "-", primary_name.lower()).strip("-") or best_role.lower()
        font_families.append({
            "slug": slug,
            "fontFamily": family,
            "name": role_names.get(best_role, best_role),
        })

    theme = {
        "$schema": "https://schemas.wp.org/trunk/theme.json",
        "version": 2,
        "settings": {
            "color": {"palette": palette},
            "typography": {"fontFamilies": font_families},
        },
    }
    return json.dumps(theme, indent=2) + "\n"


def main():
    with open(SRC) as f:
        data = json.load(f)

    # brand.json is optional -- produced by the separate brand_agent.py,
    # not required for the core WXR/redirects/QA output.
    brand = None
    try:
        with open(SRC_BRAND) as f:
            brand = json.load(f)
    except FileNotFoundError:
        pass

    with open(OUT_WXR, "w") as f:
        f.write(build_wxr(data))

    with open(OUT_REDIRECTS, "w") as f:
        f.write(build_redirects_csv(data))

    with open(OUT_QA, "w") as f:
        f.write(build_qa_report(data, brand))

    outputs = [OUT_WXR, OUT_REDIRECTS, OUT_QA]
    if brand:
        with open(OUT_THEME, "w") as f:
            f.write(build_theme_json(brand))
        outputs.append(OUT_THEME)
    else:
        print(f"({SRC_BRAND} not found -- skipping {OUT_THEME}; run brand_agent.py first to include it)")

    print(f"Wrote {', '.join(outputs)}")


if __name__ == "__main__":
    main()
