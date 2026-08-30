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


def build_item_xml(page, post_id, parent_post_id=0):
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
    <wp:post_parent>{parent_post_id}</wp:post_parent>
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


def href_to_slug(href):
    """Match crawler_agent.py's own slugify(): the last path segment, or
    "home" for the front page. Keeps nav hrefs ("/ai-solutions",
    "/threat-id-%26-detection") matchable against page slugs without
    needing the crawler and generator to agree on a shared module."""
    if not href:
        return None
    path = href.rstrip("/")
    if not path:
        return "home"
    return path.rsplit("/", 1)[-1]


def build_page_parent_map(navigation, known_slugs):
    """{child_slug: parent_slug} for WXR wp:post_parent, derived from the
    nav's dropdown structure. GoDaddy's nav categories (e.g. "AI") have
    no page of their own (href="#") -- their first dropdown child (e.g.
    "AI Solutions") is the real hub page for that section, and the
    other children become its sub-pages. A category whose first child
    doesn't resolve to a known page is left alone rather than guessing
    a hierarchy from incomplete data -- its items just stay top-level.
    """
    parent_map = {}
    for item in navigation:
        children = item.get("children") or []
        if not children:
            continue
        hub_slug = href_to_slug(children[0].get("href"))
        if hub_slug not in known_slugs:
            continue
        for child in children[1:]:
            child_slug = href_to_slug(child.get("href"))
            if child_slug and child_slug in known_slugs and child_slug != hub_slug:
                parent_map[child_slug] = hub_slug
    return parent_map



# "main-menu" collides with common theme/demo-content menu slugs (Divi's
# demo layout packs, and plenty of others, ship a menu using exactly
# that name). WordPress's importer reuses an existing term for a
# matching wp:term_slug instead of creating a new one, so importing
# into a menu name this generic can silently merge our real menu items
# in among leftover demo content -- confirmed happening on a real test
# import, where several "(Invalid)" items turned out to be pre-existing
# Divi demo menu entries pointing at pages that don't exist on this
# site. A distinctive name makes that collision very unlikely.
NAV_MENU_SLUG = "migrated-site-menu"
NAV_MENU_NAME = "Migrated Site Menu"


def build_nav_menu_term_xml(term_id):
    return f"""  <wp:term>
    <wp:term_id>{term_id}</wp:term_id>
    <wp:term_taxonomy>nav_menu</wp:term_taxonomy>
    <wp:term_slug>{NAV_MENU_SLUG}</wp:term_slug>
    <wp:term_name><![CDATA[{NAV_MENU_NAME}]]></wp:term_name>
  </wp:term>"""


def build_nav_menu_item_xml(item_id, title, target_page_post_id, menu_order, parent_item_id=0):
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    post_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return f"""  <item>
    <title>{xml_escape(title)}</title>
    <link>{NEW_BASE_URL}/</link>
    <pubDate>{pub_date}</pubDate>
    <dc:creator><![CDATA[migration-agent]]></dc:creator>
    <guid isPermaLink="false">{NEW_BASE_URL}/?p={item_id}</guid>
    <description></description>
    <content:encoded><![CDATA[]]></content:encoded>
    <excerpt:encoded><![CDATA[]]></excerpt:encoded>
    <wp:post_id>{item_id}</wp:post_id>
    <wp:post_date><![CDATA[{post_date}]]></wp:post_date>
    <wp:post_date_gmt><![CDATA[{post_date}]]></wp:post_date_gmt>
    <wp:comment_status><![CDATA[closed]]></wp:comment_status>
    <wp:ping_status><![CDATA[closed]]></wp:ping_status>
    <wp:post_name><![CDATA[]]></wp:post_name>
    <wp:status><![CDATA[publish]]></wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>{menu_order}</wp:menu_order>
    <wp:post_type><![CDATA[nav_menu_item]]></wp:post_type>
    <wp:post_password><![CDATA[]]></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
    <category domain="nav_menu" nicename="{NAV_MENU_SLUG}"><![CDATA[{NAV_MENU_NAME}]]></category>
    <wp:postmeta>
      <wp:meta_key><![CDATA[_menu_item_type]]></wp:meta_key>
      <wp:meta_value><![CDATA[post_type]]></wp:meta_value>
    </wp:postmeta>
    <wp:postmeta>
      <wp:meta_key><![CDATA[_menu_item_object]]></wp:meta_key>
      <wp:meta_value><![CDATA[page]]></wp:meta_value>
    </wp:postmeta>
    <wp:postmeta>
      <wp:meta_key><![CDATA[_menu_item_object_id]]></wp:meta_key>
      <wp:meta_value><![CDATA[{target_page_post_id}]]></wp:meta_value>
    </wp:postmeta>
    <wp:postmeta>
      <wp:meta_key><![CDATA[_menu_item_menu_item_parent]]></wp:meta_key>
      <wp:meta_value><![CDATA[{parent_item_id}]]></wp:meta_value>
    </wp:postmeta>
    <wp:postmeta>
      <wp:meta_key><![CDATA[_menu_item_target]]></wp:meta_key>
      <wp:meta_value><![CDATA[]]></wp:meta_value>
    </wp:postmeta>
  </item>"""


def build_nav_menu_items_xml(navigation, pages_by_slug):
    """WXR items for a real, importable WordPress navigation menu built
    from the site's actual scraped nav structure (see
    crawler_agent.py's extract_navigation()) -- not hardcoded per-site.

    A GoDaddy nav category like "AI" has no page of its own (href="#"),
    just a dropdown; that's not something a WP menu item can point at,
    so it's linked to its hub page instead (the same one
    build_page_parent_map() uses) -- the original nav left it
    unclickable, but pointing it somewhere real is more standard menu
    behavior and costs nothing.

    Returns (items_xml, term_xml, skipped_labels) -- skipped_labels are
    top-level or child entries whose href didn't match any crawled page
    (e.g. a nav link to a page that got excluded by the qualification
    check), reported by the caller rather than silently dropped.
    """
    known_slugs = set(pages_by_slug)
    items_xml = []
    skipped = []
    item_id = 20000
    menu_order = 1

    for top in navigation:
        children = top.get("children") or []
        top_slug = href_to_slug(top.get("href"))
        link_slug = top_slug if top_slug in known_slugs else None
        if link_slug is None and children:
            hub_slug = href_to_slug(children[0].get("href"))
            if hub_slug in known_slugs:
                link_slug = hub_slug

        if link_slug is None:
            skipped.append(top["label"])
            continue

        top_item_id = item_id
        items_xml.append(build_nav_menu_item_xml(
            item_id, clean_title(top["label"]), pages_by_slug[link_slug], menu_order,
        ))
        item_id += 1
        menu_order += 1

        for child in children:
            child_slug = href_to_slug(child.get("href"))
            if child_slug not in known_slugs:
                skipped.append(child["label"])
                continue
            items_xml.append(build_nav_menu_item_xml(
                item_id, clean_title(child["label"]), pages_by_slug[child_slug],
                menu_order, parent_item_id=top_item_id,
            ))
            item_id += 1
            menu_order += 1

    term_xml = build_nav_menu_term_xml(term_id=2) if items_xml else None
    return items_xml, term_xml, skipped


def _navigation_link_attrs(label, slug, pages_by_slug):
    return json.dumps(
        {
            "label": clean_title(label),
            "type": "page",
            "id": pages_by_slug[slug],
            "url": f"{NEW_BASE_URL}/{slug}/",
            "kind": "post-type",
        },
        separators=(",", ":"),
    )


def build_wp_navigation_content(navigation, pages_by_slug):
    """Gutenberg block markup (wp:navigation-link / wp:navigation-submenu)
    for a wp_navigation post -- the object type modern block themes
    actually use, as opposed to build_nav_menu_items_xml()'s classic
    nav_menu. A classic menu only shows up in a theme's Navigation block
    picker via a "convert existing menu" bridge that depends on the
    theme having registered a classic menu location -- confirmed absent
    on a real block theme (Twenty Twenty-Four registers none, so
    Appearance > Menus doesn't even exist while it's active) --
    while a wp_navigation post is what that same picker lists
    natively, regardless of classic menu locations, since it's the same
    object type the Site Editor itself creates when a person builds a
    Navigation block by hand.

    Same resolution rules as build_nav_menu_items_xml() (category
    headers link to their hub page, unresolvable hrefs are skipped, not
    guessed at) -- kept as separate, independent logic rather than
    shared, since these two produce fundamentally different output
    (nav_menu_item posts vs. inline block markup) from the same input.
    """
    known_slugs = set(pages_by_slug)
    blocks = []
    skipped = []

    for top in navigation:
        children = top.get("children") or []
        top_slug = href_to_slug(top.get("href"))
        link_slug = top_slug if top_slug in known_slugs else None
        if link_slug is None and children:
            hub_slug = href_to_slug(children[0].get("href"))
            if hub_slug in known_slugs:
                link_slug = hub_slug

        if link_slug is None:
            skipped.append(top["label"])
            continue

        top_attrs = _navigation_link_attrs(top["label"], link_slug, pages_by_slug)

        if not children:
            blocks.append(f"<!-- wp:navigation-link {top_attrs} /-->")
            continue

        child_blocks = []
        for child in children:
            child_slug = href_to_slug(child.get("href"))
            if child_slug not in known_slugs:
                skipped.append(child["label"])
                continue
            child_attrs = _navigation_link_attrs(child["label"], child_slug, pages_by_slug)
            child_blocks.append(f"<!-- wp:navigation-link {child_attrs} /-->")

        blocks.append(
            f"<!-- wp:navigation-submenu {top_attrs} -->\n"
            + "\n".join(child_blocks)
            + "\n<!-- /wp:navigation-submenu -->"
        )

    return "\n".join(blocks), skipped


def build_wp_navigation_item_xml(content, post_id):
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    post_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return f"""  <item>
    <title>{xml_escape(NAV_MENU_NAME)}</title>
    <link>{NEW_BASE_URL}/</link>
    <pubDate>{pub_date}</pubDate>
    <dc:creator><![CDATA[migration-agent]]></dc:creator>
    <guid isPermaLink="false">{NEW_BASE_URL}/?p={post_id}</guid>
    <description></description>
    <content:encoded><![CDATA[{content}]]></content:encoded>
    <excerpt:encoded><![CDATA[]]></excerpt:encoded>
    <wp:post_id>{post_id}</wp:post_id>
    <wp:post_date><![CDATA[{post_date}]]></wp:post_date>
    <wp:post_date_gmt><![CDATA[{post_date}]]></wp:post_date_gmt>
    <wp:comment_status><![CDATA[closed]]></wp:comment_status>
    <wp:ping_status><![CDATA[closed]]></wp:ping_status>
    <wp:post_name><![CDATA[{NAV_MENU_SLUG}]]></wp:post_name>
    <wp:status><![CDATA[publish]]></wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>0</wp:menu_order>
    <wp:post_type><![CDATA[wp_navigation]]></wp:post_type>
    <wp:post_password><![CDATA[]]></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
  </item>"""


# The block theme currently being tested against (Twenty Twenty-Four).
# wp_template_part posts only override a theme's own header/footer when
# this taxonomy term matches the theme actually active on import -- if a
# client site ends up on a different block theme, this (and the markup
# below, which assumes TT4's block vocabulary: site-logo, site-title,
# a flex group for the header) needs to change to match.
THEME_SLUG = "twentytwentyfour"


def build_header_template_part_content(wp_navigation_post_id):
    """Gutenberg block markup for a header template part: logo, site
    title, and the real migrated nav menu (referenced by ID, not
    duplicated -- editing the Navigation block anywhere updates both).

    Imported as a wp_template_part with post_name "header" plus the
    wp_theme taxonomy term above, this transparently replaces the
    target theme's own header.html -- which every block theme's
    page/single templates pull in via {"slug":"header"} -- for every
    page, site-wide, with no manual Site Editor work. This is exactly
    the mechanism the Site Editor itself uses when a person edits the
    header by hand; generating it here just does that step for them.

    Uses a plain (untagged) group, not {"tagName":"header"} -- WordPress
    already wraps a template part's rendered content in a <header> tag
    based on its area (the wp_template_part_area taxonomy term below),
    so tagging the inner group too produced invalid, doubly-nested
    <header><header>...</header></header> markup, confirmed in a real
    test import's page source.
    """
    return (
        '<!-- wp:group {"layout":{"type":"flex","justifyContent":"space-between"}} -->\n'
        '<div class="wp-block-group">\n'
        "<!-- wp:site-logo /-->\n"
        "<!-- wp:site-title /-->\n"
        f'<!-- wp:navigation {{"ref":{wp_navigation_post_id}}} /-->\n'
        "</div>\n"
        "<!-- /wp:group -->"
    )


def build_footer_template_part_content(footer, pages_by_slug):
    """Gutenberg block markup for a footer template part, built from the
    site's real extracted footer content (crawler_agent.py's
    extract_footer()) rather than left as the target theme's own
    placeholder/demo footer -- which is what silently stays in place
    without this. Same resolution rules as build_wp_navigation_content()
    for links: unresolvable hrefs are skipped, not guessed at.

    Uses a plain (untagged) group, not {"tagName":"footer"} -- same
    double-wrapping issue as build_header_template_part_content(); see
    its docstring.

    Returns (content, skipped_labels).
    """
    known_slugs = set(pages_by_slug)
    skipped = []

    link_blocks = []
    for link in footer.get("links") or []:
        slug = href_to_slug(link.get("href"))
        if slug not in known_slugs:
            skipped.append(link.get("label"))
            continue
        attrs = _navigation_link_attrs(link["label"], slug, pages_by_slug)
        link_blocks.append(f"<!-- wp:navigation-link {attrs} /-->")

    nav_block = ""
    if link_blocks:
        nav_block = (
            '<!-- wp:navigation {"layout":{"type":"flex","justifyContent":"center"},"overlayMenu":"never"} -->\n'
            + "\n".join(link_blocks)
            + "\n<!-- /wp:navigation -->\n"
        )

    social_links = [s for s in (footer.get("social_links") or []) if s.get("href")]
    social_block = ""
    if social_links:
        items = "\n".join(
            '<!-- wp:social-link {"url":%s,"service":%s} /-->'
            % (json.dumps(s["href"]), json.dumps(s.get("platform") or ""))
            for s in social_links
        )
        social_block = (
            '<!-- wp:social-links {"className":"is-style-logos-only"} -->\n'
            '<ul class="wp-block-social-links is-style-logos-only">\n'
            f"{items}\n"
            "</ul>\n"
            "<!-- /wp:social-links -->\n"
        )

    copyright_html = xml_escape(footer.get("copyright_text") or "")
    for legal in footer.get("legal_links") or []:
        legal_slug = href_to_slug(legal.get("href"))
        url = f"{NEW_BASE_URL}/{legal_slug}/" if legal_slug in known_slugs else legal.get("href")
        if url:
            copyright_html += f' | <a href="{xml_escape(url)}">{xml_escape(legal.get("label", ""))}</a>'

    copyright_block = ""
    if copyright_html:
        copyright_block = (
            '<!-- wp:paragraph {"align":"center","fontSize":"small"} -->\n'
            f'<p class="has-text-align-center has-small-font-size">{copyright_html}</p>\n'
            "<!-- /wp:paragraph -->\n"
        )

    content = (
        '<!-- wp:group {"style":{"spacing":{"blockGap":"1rem"}},"layout":{"type":"constrained"}} -->\n'
        '<div class="wp-block-group">\n'
        f"{nav_block}{social_block}{copyright_block}"
        "</div>\n"
        "<!-- /wp:group -->"
    )
    return content, skipped


def build_template_part_item_xml(post_id, slug, area, title, content):
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    post_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return f"""  <item>
    <title>{xml_escape(title)}</title>
    <link>{NEW_BASE_URL}/</link>
    <pubDate>{pub_date}</pubDate>
    <dc:creator><![CDATA[migration-agent]]></dc:creator>
    <guid isPermaLink="false">{NEW_BASE_URL}/?p={post_id}</guid>
    <description></description>
    <content:encoded><![CDATA[{content}]]></content:encoded>
    <excerpt:encoded><![CDATA[]]></excerpt:encoded>
    <wp:post_id>{post_id}</wp:post_id>
    <wp:post_date><![CDATA[{post_date}]]></wp:post_date>
    <wp:post_date_gmt><![CDATA[{post_date}]]></wp:post_date_gmt>
    <wp:comment_status><![CDATA[closed]]></wp:comment_status>
    <wp:ping_status><![CDATA[closed]]></wp:ping_status>
    <wp:post_name><![CDATA[{slug}]]></wp:post_name>
    <wp:status><![CDATA[publish]]></wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>0</wp:menu_order>
    <wp:post_type><![CDATA[wp_template_part]]></wp:post_type>
    <wp:post_password><![CDATA[]]></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
    <category domain="wp_theme" nicename="{THEME_SLUG}"><![CDATA[{THEME_SLUG}]]></category>
    <category domain="wp_template_part_area" nicename="{area}"><![CDATA[{area}]]></category>
  </item>"""


def build_wxr(data):
    site = data["site"]
    navigation = data.get("navigation") or []

    # Assign page post_ids first and build a slug lookup before anything
    # that needs to reference "the page for this slug" -- post_parent
    # assignment and nav menu item targets both do.
    post_id = 100
    pages_by_slug = {}
    for page in data["pages"]:
        pages_by_slug[page["slug"]] = post_id
        post_id += 1

    parent_map = build_page_parent_map(navigation, set(pages_by_slug))

    items_xml = []
    post_id = 100
    for page in data["pages"]:
        parent_slug = parent_map.get(page["slug"])
        parent_post_id = pages_by_slug.get(parent_slug, 0) if parent_slug else 0
        items_xml.append(build_item_xml(page, post_id, parent_post_id))
        post_id += 1

    # Attachment IDs start well past the highest possible page post_id
    # (100 + one per page) so the two ranges can never collide.
    attachment_id = 10000
    importable, _ = partition_images_by_importability(collect_unique_images(data["pages"]))
    for url, alt in importable.items():
        items_xml.append(build_attachment_item_xml(url, alt, attachment_id))
        attachment_id += 1

    # Nav menu item IDs (20000+) are a third range, past attachments,
    # so none of the three can ever collide. Generates both a classic
    # nav_menu (for classic/hybrid themes) and a wp_navigation post (for
    # modern block themes, which is what a Navigation block's own "use
    # existing" picker actually lists) from the same source data, so the
    # imported site works regardless of which kind of theme it ends up
    # using.
    menu_items_xml, menu_term_xml, _skipped_nav_labels = build_nav_menu_items_xml(
        navigation, pages_by_slug
    )
    items_xml.extend(menu_items_xml)

    wp_navigation_content, _skipped_wp_nav_labels = build_wp_navigation_content(
        navigation, pages_by_slug
    )
    wp_navigation_post_id = 30000
    if wp_navigation_content:
        items_xml.append(build_wp_navigation_item_xml(wp_navigation_content, post_id=wp_navigation_post_id))

        # Template part IDs (40000+) are a fourth range, past the
        # wp_navigation post, so none of the four can ever collide. The
        # header only makes sense once there's a real wp_navigation post
        # for it to reference -- skipped otherwise rather than emitting
        # a header with a dangling nav reference.
        header_content = build_header_template_part_content(wp_navigation_post_id)
        items_xml.append(
            build_template_part_item_xml(40000, "header", "header", "Header", header_content)
        )

    footer_data = data.get("footer") or {}
    if footer_data.get("links") or footer_data.get("copyright_text") or footer_data.get("social_links"):
        footer_content, _skipped_footer_labels = build_footer_template_part_content(
            footer_data, pages_by_slug
        )
        items_xml.append(
            build_template_part_item_xml(40001, "footer", "footer", "Footer", footer_content)
        )

    channel_title = xml_escape(site["title"])
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    term_block = f"\n{menu_term_xml}" if menu_term_xml else ""

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
  <wp:base_blog_url>{NEW_BASE_URL}</wp:base_blog_url>{term_block}
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

    known_slugs = {p["slug"] for p in pages}
    menu_items_xml, _, skipped_nav_labels = build_nav_menu_items_xml(
        data.get("navigation") or [], {slug: 0 for slug in known_slugs}
    )

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
    front_page = next((p for p in pages if p.get("is_front_page")), None)
    if front_page:
        lines.append(
            f"- **Set the homepage** (one-time, unavoidable manual step): the front page "
            f"imports as a normal page — titled \"{clean_title(front_page['title'])}\", slug "
            f"`{front_page['slug']}` — like any other. Which page WordPress actually shows at "
            f"`/` is a site option (Settings → Reading → \"Your homepage displays\" → set it to "
            f"a static page → choose this one), not page content, so no WXR import can set it "
            f"automatically. Skip this and `/` shows the default blog post listing instead."
        )
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
    if menu_items_xml:
        lines.append(
            f"- **Navigation menu** ({len(menu_items_xml)} item(s), matching the site's real "
            f"nav structure including page hierarchy) is included **twice**, in two different "
            f"WordPress formats, so it works automatically regardless of which kind of theme "
            f"the target site uses:\n"
            f"  - A classic menu named \"{NAV_MENU_NAME}\" (for classic/hybrid themes — "
            f"Appearance → Menus, assign it to a menu location).\n"
            f"  - A block-theme navigation entry (`wp_navigation`, also named "
            f"\"{NAV_MENU_NAME}\") for block themes like Twenty Twenty-Four. This one is wired "
            f"in automatically (see the header/footer bullet below) — nothing to click for it "
            f"specifically."
        )
        lines.append(
            f"- **Header and footer**: this file also replaces the target theme's own "
            f"header/footer (currently generated for **{THEME_SLUG}** — see note below if the "
            f"target site uses a different block theme) with real ones built from the site's "
            f"actual content: the header gets the site logo/title plus the migrated nav menu "
            f"above, already linked by reference — nothing to assign by hand; the footer is "
            f"rebuilt from the original site's real footer (its own nav links, social icons, "
            f"and copyright/legal text), not the theme's generic demo footer. This is what the "
            f"page layout in earlier test imports was missing — WordPress's importer has no way "
            f"to override a theme's header/footer templates on its own, so without this the "
            f"pages rendered inside whatever blank/demo chrome the theme shipped with. If the "
            f"target site is on a **different block theme than {THEME_SLUG}**, this override "
            f"won't take (WordPress scopes it to the specific theme) — the header/footer will "
            f"need to be rebuilt by hand once, or regenerated by changing `THEME_SLUG` in "
            f"generator_agent.py to match and re-running it."
        )
        lines.append(
            "- **Reviewing the nav before go-live**: pages import as drafts by design (see "
            "below), but WordPress's own draft-preview mode (the \"Preview\" link/button, URLs "
            "with `?preview=true`) has been observed failing to render the Navigation block's "
            "menu items at all -- confirmed on a real test import: the exact same page showed a "
            "completely empty nav in preview but rendered correctly, submenus and all, the "
            "moment it was published. This is a WordPress preview-mode limitation, not a defect "
            "in this file -- don't take an empty-looking nav on a draft preview at face value. "
            "To actually check it before the real go-live, temporarily publish the page, look, "
            "then set it back to Draft."
        )
    if skipped_nav_labels:
        lines.append(
            f"- **{len(skipped_nav_labels)} nav item(s) skipped**: linked to a page that wasn't "
            f"in this crawl ({', '.join(skipped_nav_labels)}) — added to the site's nav after "
            "the crawl, or excluded by the qualification check. Add manually if needed."
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
