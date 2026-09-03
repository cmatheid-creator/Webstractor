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

# Set once by build_wxr() and read by block_to_gutenberg() and
# build_header_template_part_content() -- module-level rather than
# threaded as a parameter through build_item_xml()/every block builder,
# since only a couple of leaf functions actually need it and both are
# already only ever called from within one build_wxr() run.
_BRAND = None

# Same rationale as _BRAND: set once by build_wxr() (right after it
# assigns page post_ids), read by block_to_gutenberg() to resolve a
# card_group's CTA link -- e.g. "/ai-strategy-1" -- to the matching
# migrated page's real URL instead of leaving it pointed at the old site.
_PAGES_BY_SLUG = None

# Same rationale as _PAGES_BY_SLUG: set once by build_wxr(), read by
# block_to_gutenberg()'s "post_feed" handler as a fallback thumbnail when
# a post's own card image wasn't captured (that content loads via
# client-side JS the crawler doesn't always see) -- keyed by slug against
# the linked page's own real featured_image (og:image).
_FEATURED_IMAGE_BY_SLUG = None


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


def display_image_url(url):
    """The URL to actually embed in an <img src> for a re-hostable image
    -- canonical_attachment_url(url) when there is one, else the raw url
    unchanged (e.g. a stock-photo URL with no extension to truncate at,
    which never gets a WXR attachment item at all -- see
    canonical_attachment_url()'s docstring).

    Using anything other than the exact same URL string that ends up as
    the image's wp:attachment_url/guid creates real, confirmed problems:
    the SAME logical photo commonly appears across this site at several
    different GoDaddy CDN transform-suffixed URLs (a card thumbnail's
    resized crop, a blog post's full-size body copy, ...), and
    collect_unique_images() already collapses all of those down to one
    attachment item keyed by this same canonical URL. If the <img src>
    actually embedded in content still used the original, differently-
    suffixed URL, it would never literally match the one URL WordPress
    downloaded and is rewriting occurrences of -- confirmed on a real
    test import that the plain string-match rewrite this project relies
    on (see collect_unique_images()) only fires for an occurrence whose
    src is byte-for-byte identical to the downloaded URL. Every image-
    emitting block type uses this rather than embedding block/card/post
    data's own raw src, so every occurrence of the same photo -- however
    it was originally suffixed -- reliably resolves to the one real
    attachment WordPress actually created for it, not 2-3 duplicate
    downloads of which only some get rewritten.
    """
    return canonical_attachment_url(url) or url


SRC = "structured_content.json"
SRC_BRAND = "brand.json"
OUT_WXR = "stratecon-migration.xml"
OUT_REDIRECTS = "redirects.csv"
OUT_QA = "qa_report.md"
OUT_THEME = "theme.json"
OUT_APPLY_BRANDING = "apply_branding.php"

NEW_BASE_URL = "https://staging.stratecon-newsite.example"  # placeholder staging URL


def _brand_role_style(role, brand):
    """Look up one of brand_agent.py's extracted typography roles (e.g.
    "HeadingBeta", "NavAlpha") and resolve its font/color against the
    same palette and font-family slugs the brand's tokens were
    registered under (see _derive_brand_palette_and_fonts()) -- so a
    heading or the nav can reference the theme's own registered
    "playfair-display"/"link" entries instead of duplicating literal
    values, while still getting the *real* extracted size and weight
    the generic WordPress defaults don't know about.

    This is what closes a real visual gap, not a cosmetic one: brand.json
    already captures each role's actual font/size/weight/color from the
    live site's computed styles, but nothing was applying it -- a
    section heading rendered at WordPress's generic (much larger)
    default size, and the nav rendered in the body's muted gray instead
    of the site's actual navy nav color.

    Returns None if brand has no typography data for this role (brand.json
    is optional -- not every run has one). Otherwise a dict with
    font_family_slug/text_color_slug (None if the role's actual value
    doesn't match any registered palette/font entry) and the role's
    font_size/font_weight straight from brand.json.
    """
    if not brand:
        return None
    spec = (brand.get("typography") or {}).get(role)
    if not spec:
        return None
    palette, font_families, _ = _derive_brand_palette_and_fonts(brand)
    font_family_slug = next(
        (f["slug"] for f in font_families if f["fontFamily"] == spec.get("font_family")),
        None,
    )
    text_color_slug = next(
        (c["slug"] for c in palette if c["color"].lower() == (spec.get("color") or "").lower()),
        None,
    )
    return {
        "font_family_slug": font_family_slug,
        "text_color_slug": text_color_slug,
        "font_size": spec.get("font_size"),
        "font_weight": spec.get("font_weight"),
    }


def role_class_name(role):
    """The shared CSS class a role's font-size/weight rule lives under
    (see _extra_css_rules()) -- e.g. "has-role-bodyalpha" for "BodyAlpha".
    """
    return f"has-role-{role.lower()}"


def _role_style_bits(role, brand):
    """Like _brand_role_style(), but returns ready-to-use ("json_attrs",
    "classes") fragments for a block comment's attributes and its
    element's class list -- font family and color as Gutenberg's own
    native fontFamily/textColor attributes (safe: block validation can
    correctly reconstruct these), font-size/weight as a shared
    role_class_name() className instead of an inline style.

    That split matters: Gutenberg's editor re-validates every saved
    block by regenerating its expected HTML from ONLY the JSON
    attributes in its comment, and flags "Block contains unexpected or
    invalid content" on any byte mismatch against what's actually
    stored. An earlier version of this code appended "font-size:...;
    font-weight:..." straight into each element's inline style with no
    matching JSON attribute at all -- confirmed on a real test install,
    that produced exactly this warning on every heading/paragraph/list
    this project applies brand typography to. A className has no such
    reconstruction step (Gutenberg just appends whatever string is
    there), so pairing it with one real, shared CSS rule per role (
    written once in _extra_css_rules(), not duplicated inline per
    occurrence) sidesteps the mismatch entirely while still applying
    the same font-size/weight.

    Returns None if the role isn't in brand's typography at all.
    """
    hs = _brand_role_style(role, brand)
    if not hs:
        return None
    json_attrs = []
    classes = []
    if hs.get("font_family_slug"):
        json_attrs.append(f'"fontFamily":"{hs["font_family_slug"]}"')
        classes.append(f'has-{hs["font_family_slug"]}-font-family')
    if hs.get("text_color_slug"):
        json_attrs.append(f'"textColor":"{hs["text_color_slug"]}"')
        classes.append(f'has-{hs["text_color_slug"]}-color has-text-color')
    if hs.get("font_size") or hs.get("font_weight"):
        rc = role_class_name(role)
        json_attrs.append(f'"className":"{rc}"')
        classes.append(rc)
    return {"json_attrs": ",".join(json_attrs), "classes": " ".join(classes)}


def block_to_gutenberg(block):
    """Turn one structured content block into native Gutenberg block markup."""
    t = block["type"]

    if t == "heading":
        level = block.get("level", 2)
        text = html.escape(block["text"])

        # GoDaddy Website Builder's "SectionHeading" role -- confirmed in
        # the live site's own markup -- gets flanked with a horizontal
        # rule on each side, and it's the *role* that decides this, not
        # the HTML heading level: the same "HeadingBeta" role shows up as
        # a real <h1> on one page (GoDaddy promotes it there via a
        # data-promoted-from attribute) and a plain <h2> on another (e.g.
        # "AI Solutions"), but the original site renders both identically
        # with dividers. An earlier version keyed this off level==1
        # instead, confirmed wrong on a real test import: the <h1> case
        # got its dividers, but every "HeadingBeta" <h2> rendered as a
        # plain heading with none. Centered heading blocks alone lose the
        # divider treatment entirely, so it's rebuilt here with a flex
        # group and two separators sized to fill the remaining space via
        # an inline style -- core/separator has no "grow" attribute of
        # its own to reach for.
        if block.get("typography_role") == "HeadingBeta":
            rb = _role_style_bits("HeadingBeta", _BRAND) or {}
            extra_attrs = f',{rb["json_attrs"]}' if rb.get("json_attrs") else ""
            extra_classes = f' {rb["classes"]}' if rb.get("classes") else ""
            # Real per-section vertical spacing, confirmed against the
            # live site's own CSS (56px top+bottom padding per section):
            # this generated page has no per-"section" wrapper the way
            # the original site's markup does, so without an explicit
            # margin here these divider headings -- the actual visual
            # section boundaries -- were only getting WordPress's small
            # default block spacing (~1.5em) between them, far tighter
            # than the original site's rhythm. "migration-section-divider"/
            # "migration-divider-hr" carry that margin and the separators'
            # flex-grow via className + a real shared CSS rule (see
            # _extra_css_rules()) instead of an inline style with no
            # matching JSON attribute -- see _role_style_bits()'s
            # docstring for why that combination fails Gutenberg's block
            # validation.
            return (
                '<!-- wp:group {"align":"wide","className":"migration-section-divider",'
                '"layout":{"type":"flex","justifyContent":"center","verticalAlignment":"center"}} -->\n'
                '<div class="wp-block-group alignwide migration-section-divider">\n'
                '<!-- wp:separator {"className":"is-style-wide migration-divider-hr"} -->\n'
                '<hr class="wp-block-separator has-alpha-channel-opacity is-style-wide migration-divider-hr"/>\n'
                '<!-- /wp:separator -->\n'
                f'<!-- wp:heading {{"level":{level},"textAlign":"center"{extra_attrs}}} -->\n'
                f'<h{level} class="wp-block-heading has-text-align-center{extra_classes}">{text}</h{level}>\n'
                '<!-- /wp:heading -->\n'
                '<!-- wp:separator {"className":"is-style-wide migration-divider-hr"} -->\n'
                '<hr class="wp-block-separator has-alpha-channel-opacity is-style-wide migration-divider-hr"/>\n'
                '<!-- /wp:separator -->\n'
                '</div>\n'
                '<!-- /wp:group -->'
            )

        # Other heading roles: apply the real per-role font/size/weight/
        # color when the crawler captured which GoDaddy typography role
        # this specific heading used (data-typography) -- not just a
        # WordPress generic default. Older structured_content.json files
        # predate this field and simply won't have it; falls back to the
        # generic heading below exactly as before.
        rb = _role_style_bits(block.get("typography_role"), _BRAND)
        if rb:
            attrs = f'"level":{level}'
            if rb["json_attrs"]:
                attrs += f',{rb["json_attrs"]}'
            classes = f'wp-block-heading {rb["classes"]}'.strip()
            return (
                f'<!-- wp:heading {{{attrs}}} -->\n'
                f'<h{level} class="{classes}">{text}</h{level}>\n'
                f'<!-- /wp:heading -->'
            )

        return (
            f'<!-- wp:heading {{"level":{level}}} -->\n'
            f'<h{level} class="wp-block-heading">{text}</h{level}>\n'
            f'<!-- /wp:heading -->'
        )

    if t == "paragraph":
        # crawler_agent.py's element_inline_html() already produces a
        # small safe HTML fragment (bold/italic/links preserved, every
        # other tag/attribute stripped) -- not plain text, so this
        # doesn't re-escape it the way the heading branches above do for
        # their own plain-text "text" field.
        text = block["text"]
        rb = _role_style_bits(block.get("typography_role"), _BRAND)
        if rb:
            attrs_block = f' {{{rb["json_attrs"]}}}' if rb["json_attrs"] else ""
            classes = rb["classes"]
            return (
                f'<!-- wp:paragraph{attrs_block} -->\n'
                f'<p class="{classes}">{text}</p>\n'
                '<!-- /wp:paragraph -->'
            )
        return f'<!-- wp:paragraph -->\n<p>{text}</p>\n<!-- /wp:paragraph -->'

    if t == "list":
        # Same inline-HTML contract as "paragraph" above -- each item is
        # already a safe fragment, not plain text.
        items = "".join(f"<li>{i}</li>" for i in block["items"])
        rb = _role_style_bits(block.get("typography_role"), _BRAND)
        if rb:
            attrs_block = f' {{{rb["json_attrs"]}}}' if rb["json_attrs"] else ""
            classes = f'wp-block-list {rb["classes"]}'.strip()
            return (
                f'<!-- wp:list{attrs_block} -->\n'
                f'<ul class="{classes}">{items}</ul>\n'
                '<!-- /wp:list -->'
            )
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
        src = xml_escape(display_image_url(block["src"]))
        alt = xml_escape(block.get("alt", ""))
        return (
            '<!-- wp:image -->\n'
            f'<figure class="wp-block-image"><img src="{src}" alt="{alt}"/></figure>\n'
            '<!-- /wp:image -->\n'
            '<!-- QA FLAG: still points at the original site -- swap to the '
            're-hosted media-library copy after import. -->'
        )

    if t == "media_text":
        # Same original-site-URL caveat as the plain "image" block --
        # see its comment above.
        src = xml_escape(display_image_url(block["src"]))
        alt = xml_escape(block.get("alt", ""))
        content_blocks = "\n\n".join(
            block_to_gutenberg(b) for b in block.get("content", [])
        )
        return (
            '<!-- wp:media-text {"align":"wide","mediaType":"image"} -->\n'
            '<div class="wp-block-media-text alignwide is-stacked-on-mobile">'
            f'<figure class="wp-block-media-text__media"><img src="{src}" alt="{alt}"/></figure>'
            f'<div class="wp-block-media-text__content">\n{content_blocks}\n</div>'
            '</div>\n'
            '<!-- /wp:media-text -->\n'
            '<!-- QA FLAG: image still points at the original site -- swap to the '
            're-hosted media-library copy after import. -->'
        )

    if t == "card_group":
        # GoDaddy Website Builder's "ContentCard" component: a row of
        # equal cards, each with an image on top, a heading, a short
        # paragraph, and a "Learn More" button -- confirmed via live
        # markup (see crawler_agent.py's mark_content_cards()). Rendered
        # as a real core/columns row so cards sit side by side instead of
        # stacking as generic page content, which is what happened before
        # this block type existed (three unrelated-looking heading/image/
        # paragraph clusters, one after another, no CTA at all).
        column_blocks = []
        for card in block.get("cards", []):
            parts = []

            image = card.get("image")
            if image:
                src = xml_escape(display_image_url(image["src"]))
                alt = xml_escape(image.get("alt", ""))
                parts.append(
                    '<!-- wp:image {"sizeSlug":"large"} -->\n'
                    f'<figure class="wp-block-image size-large"><img src="{src}" alt="{alt}"/></figure>\n'
                    '<!-- /wp:image -->'
                )

            # Confirmed against the live site: each card's heading,
            # paragraph, and button are centered under its image, not
            # left-aligned the way a plain heading/paragraph/buttons
            # block defaults to.
            heading = card.get("heading")
            if heading:
                rb = _role_style_bits(card.get("heading_role"), _BRAND)
                text = html.escape(heading)
                if rb:
                    attrs = '"level":4,"textAlign":"center"'
                    if rb["json_attrs"]:
                        attrs += f',{rb["json_attrs"]}'
                    classes = f'wp-block-heading has-text-align-center {rb["classes"]}'.strip()
                    parts.append(
                        f'<!-- wp:heading {{{attrs}}} -->\n'
                        f'<h4 class="{classes}">{text}</h4>\n'
                        '<!-- /wp:heading -->'
                    )
                else:
                    parts.append(
                        '<!-- wp:heading {"level":4,"textAlign":"center"} -->\n'
                        f'<h4 class="wp-block-heading has-text-align-center">{text}</h4>\n'
                        '<!-- /wp:heading -->'
                    )

            text = card.get("text")
            if text:
                parts.append(
                    '<!-- wp:paragraph {"align":"center"} -->\n'
                    f'<p class="has-text-align-center">{html.escape(text)}</p>\n'
                    '<!-- /wp:paragraph -->'
                )

            cta = card.get("cta")
            if cta:
                slug = href_to_slug(cta.get("href"))
                url = (
                    f"{NEW_BASE_URL}/{slug}/"
                    if _PAGES_BY_SLUG and slug in _PAGES_BY_SLUG
                    else cta.get("href")
                )
                label = html.escape(cta.get("label") or "Learn More")
                url_escaped = xml_escape(url or "#")
                # margin-top:auto pins the button to the bottom of the
                # column regardless of how many lines the paragraph above
                # it wraps to -- confirmed a real gap without this: cards
                # with a shorter description had their button riding
                # noticeably higher than a neighboring card's, since
                # nothing tied the button's position to the column's own
                # bottom edge rather than wherever the text above happened
                # to end. padding-top adds a fixed minimum gap on top of
                # that auto push -- without it, the tallest card's own
                # text (the one that sets the row's height) butts right up
                # against its own button, since auto-push has nothing left
                # to push through for that card specifically. Confirmed on
                # a real test import. "migration-cta-buttons" carries both
                # via className + a real shared CSS rule (see
                # _extra_css_rules()) instead of an inline style with no
                # matching JSON attribute -- see _role_style_bits()'s
                # docstring for why that fails Gutenberg's block
                # validation.
                parts.append(
                    '<!-- wp:buttons {"layout":{"type":"flex","justifyContent":"center"},'
                    '"className":"migration-cta-buttons"} -->\n'
                    '<div class="wp-block-buttons migration-cta-buttons">\n'
                    '<!-- wp:button -->\n'
                    f'<div class="wp-block-button"><a class="wp-block-button__link '
                    f'wp-element-button" href="{url_escaped}">{label}</a></div>\n'
                    '<!-- /wp:button -->\n'
                    '</div>\n'
                    '<!-- /wp:buttons -->'
                )

            # display:flex (so margin-top:auto above has a flex container
            # to push against) and an explicit width are needed here, not
            # just the parent wp:columns block's own layout -- confirmed a
            # real gap without the explicit width: a trailing row with
            # fewer cards than earlier rows (e.g. 7 cards in rows of 3
            # leaves a lone card in the last row) has its column(s)
            # stretch to fill the *whole* row instead of staying the same
            # width as every other card, since a plain wp:column's width
            # is otherwise just an equal share of however many siblings
            # happen to be in that specific row. "migration-flex-column"
            # carries all of that (including the 33.33% width) via
            # className instead of a "width" attribute + inline style --
            # see _role_style_bits()'s docstring for why the inline-style
            # version fails Gutenberg's block validation.
            column_content = "\n\n".join(parts)
            column_blocks.append(
                '<!-- wp:column {"className":"migration-flex-column"} -->\n'
                f'<div class="wp-block-column migration-flex-column">\n{column_content}\n</div>\n'
                '<!-- /wp:column -->'
            )

        columns_content = "\n\n".join(column_blocks)
        # Explicit blockGap -- confirmed against the live site's own card
        # row: its gutters between cards are noticeably wider than
        # WordPress's small default column gap, which (combined with each
        # column's explicit 33.33% width leaving it almost no slack) made
        # columns here read as wider than the live site's and wrap its
        # paragraph text differently/less evenly. "migration-columns-gap"
        # carries this via className -- see the column fix above for why.
        return (
            '<!-- wp:columns {"align":"wide","className":"migration-columns-gap"} -->\n'
            '<div class="wp-block-columns alignwide migration-columns-gap">\n'
            f"{columns_content}\n"
            '</div>\n'
            '<!-- /wp:columns -->\n'
            '<!-- QA FLAG: card images still point at the original site -- swap to the '
            're-hosted media-library copy after import. -->'
        )

    if t == "post_feed":
        # GoDaddy Website Builder's "RSS Feed" widget -- confirmed via
        # live markup (see crawler_agent.py's mark_post_feeds()): a grid
        # of real blog-post previews (thumbnail, date, categories,
        # title, excerpt, "Continue Reading"), e.g. an "AI Insights"
        # section on a landing page. Every post it links to is itself a
        # page in this same crawl, so each preview points at the
        # *migrated* copy via the same href-to-slug lookup card_group's
        # CTA uses, not the original site -- falls back to the original
        # href only if that post genuinely isn't in this crawl. Rendered
        # in rows of 3 columns, matching this site's other card rows.
        posts = block.get("posts", [])
        row_blocks = []
        any_images = False
        for i in range(0, len(posts), 3):
            column_blocks = []
            for post in posts[i:i + 3]:
                parts = []

                href = post.get("href")
                slug = href_to_slug(href) if href else None
                url = (
                    f"{NEW_BASE_URL}/{slug}/"
                    if slug and _PAGES_BY_SLUG and slug in _PAGES_BY_SLUG
                    else href
                )
                url_escaped = xml_escape(url) if url else None

                # The card's own thumbnail (loaded via client-side JS on
                # the referring page -- see mark_post_feeds()) isn't
                # always captured; fall back to the linked post's own
                # real featured image (og:image) rather than showing no
                # image at all.
                image_src = post.get("image_src") or (
                    _FEATURED_IMAGE_BY_SLUG.get(slug) if slug and _FEATURED_IMAGE_BY_SLUG else None
                )
                if image_src:
                    any_images = True
                    img_html = f'<img src="{xml_escape(display_image_url(image_src))}" alt=""/>'
                    if url_escaped:
                        img_html = f'<a href="{url_escaped}">{img_html}</a>'
                    # "post-feed-thumbnail" is a hook for the hover-shadow
                    # CSS in build_global_styles_content() -- scoped to just
                    # these card thumbnails rather than every wp:image on
                    # the site, matching the live site's own hover treatment
                    # on its "AI Insights" blog cards specifically.
                    parts.append(
                        '<!-- wp:image {"sizeSlug":"large","className":"post-feed-thumbnail"} -->\n'
                        f'<figure class="wp-block-image size-large post-feed-thumbnail">{img_html}</figure>\n'
                        '<!-- /wp:image -->'
                    )

                meta_bits = [b for b in (post.get("date"), post.get("categories")) if b]
                if meta_bits:
                    meta_text = html.escape(" | ".join(meta_bits))
                    parts.append(
                        '<!-- wp:paragraph {"fontSize":"small"} -->\n'
                        f'<p class="has-small-font-size">{meta_text}</p>\n'
                        '<!-- /wp:paragraph -->'
                    )

                heading = post.get("heading")
                if heading:
                    text = html.escape(heading)
                    # Confirmed against the live site: a card title link
                    # has no underline (unlike a plain in-text link, e.g.
                    # the footer's legal links, which do) -- WordPress's
                    # own default styles underline every <a> with nothing
                    # to say otherwise, so this needs an explicit override
                    # rather than being left to inherit.
                    inner = (
                        f'<a href="{url_escaped}" style="text-decoration:none">{text}</a>'
                        if url_escaped else text
                    )
                    rb = _role_style_bits(post.get("heading_role"), _BRAND)
                    if rb:
                        attrs = '"level":4'
                        if rb["json_attrs"]:
                            attrs += f',{rb["json_attrs"]}'
                        classes = f'wp-block-heading {rb["classes"]}'.strip()
                        parts.append(
                            f'<!-- wp:heading {{{attrs}}} -->\n'
                            f'<h4 class="{classes}">{inner}</h4>\n'
                            '<!-- /wp:heading -->'
                        )
                    else:
                        parts.append(
                            '<!-- wp:heading {"level":4} -->\n'
                            f'<h4 class="wp-block-heading">{inner}</h4>\n'
                            '<!-- /wp:heading -->'
                        )

                excerpt = post.get("excerpt")
                if excerpt:
                    parts.append(
                        '<!-- wp:paragraph -->\n'
                        f'<p>{html.escape(excerpt)}</p>\n'
                        '<!-- /wp:paragraph -->'
                    )

                if url_escaped:
                    # margin-top:auto -- same reasoning as card_group's
                    # button: pins "Continue Reading" to the bottom of the
                    # column regardless of how long this particular post's
                    # excerpt happens to be, instead of it landing right
                    # after wherever the text above it ends.
                    # "migration-push-bottom" carries this via className --
                    # see _role_style_bits()'s docstring for why an inline
                    # style with no matching JSON attribute fails
                    # Gutenberg's block validation.
                    parts.append(
                        '<!-- wp:paragraph {"className":"migration-push-bottom"} -->\n'
                        f'<p class="migration-push-bottom"><a href="{url_escaped}">Continue Reading</a></p>\n'
                        '<!-- /wp:paragraph -->'
                    )

                # Explicit width + flex column, same reasoning as
                # card_group: without it, a trailing row with fewer posts
                # than a full row of 3 (7 posts = rows of 3, 3, 1) has its
                # last column stretch to the full row width instead of
                # staying the same size as every other card.
                # "migration-flex-column" carries this via className --
                # see card_group's identical fix for why.
                column_content = "\n\n".join(parts)
                column_blocks.append(
                    '<!-- wp:column {"className":"migration-flex-column"} -->\n'
                    f'<div class="wp-block-column migration-flex-column">\n{column_content}\n</div>\n'
                    '<!-- /wp:column -->'
                )

            columns_content = "\n\n".join(column_blocks)
            # Same blockGap fix as card_group's row -- see its comment.
            row_blocks.append(
                '<!-- wp:columns {"align":"wide","className":"migration-columns-gap"} -->\n'
                '<div class="wp-block-columns alignwide migration-columns-gap">\n'
                f"{columns_content}\n"
                '</div>\n'
                '<!-- /wp:columns -->'
            )

        result = "\n\n".join(row_blocks)
        if any_images:
            result += (
                '\n\n<!-- QA FLAG: post preview images still point at the original '
                'site -- swap to the re-hosted media-library copy after import. -->'
            )
        return result

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
    """Dedupe image blocks across all pages and return {canonical_url:
    alt} pairs, one entry per real underlying photo. Covers plain
    "image" blocks, the image half of a "media_text" side-by-side pair,
    each card's image within a "card_group", and each post's thumbnail
    within a "post_feed" -- all four carry a real image that needs its
    own WXR attachment item.

    Dedupes by display_image_url(url), not the raw url -- the same
    logical photo commonly shows up at several different GoDaddy CDN
    transform-suffixed URLs across the site (e.g. a card thumbnail's
    resized crop vs. that same post's own full-size body copy), and
    deduping by the raw string missed that, confirmed on a real test
    import: it produced 2-3 separate WXR attachment items -- and 2-3
    separate downloads -- for what was really one photo. Every image-
    emitting block in block_to_gutenberg() embeds the same canonical URL
    via display_image_url(), so this dict's keys are exactly the src
    strings that show up in content -- one real attachment per photo,
    reliably matched.
    """
    images = {}
    for page in pages:
        for block in page["blocks"]:
            if block["type"] in ("image", "media_text"):
                url = display_image_url(block["src"])
                if url not in images:
                    images[url] = block.get("alt", "")
            elif block["type"] == "card_group":
                for card in block.get("cards", []):
                    image = card.get("image")
                    if image:
                        url = display_image_url(image["src"])
                        if url not in images:
                            images[url] = image.get("alt", "")
            elif block["type"] == "post_feed":
                for post in block.get("posts", []):
                    image_src = post.get("image_src")
                    if image_src:
                        url = display_image_url(image_src)
                        if url not in images:
                            images[url] = ""
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
# below, which assumes TT4's block vocabulary: site-logo, a flex group
# for the header) needs to change to match.
THEME_SLUG = "twentytwentyfour"

# Reserved for the site logo attachment -- past every other fixed ID
# range (wp_navigation at 30000, template parts/global styles/page
# template at 40000-40003) so it can never collide.
LOGO_ATTACHMENT_ID = 40004


def build_header_template_part_content(wp_navigation_post_id):
    """Gutenberg block markup for a header template part: logo and the
    real migrated nav menu (referenced by ID, not duplicated -- editing
    the Navigation block anywhere updates both).

    No site-title block: the original site's header shows only the logo
    image (its wordmark is baked into the graphic itself), confirmed
    against the live site -- adding one back here would just print
    whatever site title the target WordPress install happens to have
    (e.g. a placeholder typed in at install time) next to the real logo.

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

    Wrapped in an outer "constrained"-layout group with "has-global-
    padding" -- confirmed in a real test import's rendered page: without
    this, the inner flex row has the *entire* browser viewport to work
    with (unlike the page's main content, which the theme already caps
    to a centered, readable max-width), so on a wide screen
    "justifyContent":"space-between" flings the logo and nav to opposite
    edges of a much wider space than the rest of the page uses, leaving
    a large empty gap in the middle and cramming the nav into a narrow
    strip on the right. The outer group's "constrained" layout centers
    the header, and the inner flex row is explicitly "align":"wide" --
    the theme's *wide* content width (theme.json's --wide-size, e.g.
    1280px), not the narrower default *reading* width ("--content-size",
    e.g. 620px) a plain "constrained" child would otherwise inherit,
    which is comfortable for body paragraph text but too narrow for a
    logo plus an 8-item nav, causing exactly the same awkward wrapping
    a too-wide header does. "has-global-padding" is a real WordPress
    core utility class (confirmed present in this site's own global-
    styles-inline-css) that applies the theme's configured root padding
    -- the same mechanism the theme's own default header and the page's
    main content area both already rely on.
    """
    # The nav's real per-role style (NavAlpha) -- confirmed in a real
    # test import: without this, the nav inherits the header's/body's
    # muted text color and whatever generic size the "small" preset
    # happens to be, rendering visibly smaller and the wrong color
    # compared to the original site's actual (usually bolder, brand-
    # colored) nav treatment.
    nav_style = _brand_role_style("NavAlpha", _BRAND) or {}
    nav_attrs = f'"ref":{wp_navigation_post_id},"overlayMenu":"mobile"'
    typography = {"textTransform": "uppercase", "letterSpacing": "0.05em"}
    if nav_style.get("font_family_slug"):
        nav_attrs += f',"fontFamily":"{nav_style["font_family_slug"]}"'
    if nav_style.get("text_color_slug"):
        nav_attrs += f',"textColor":"{nav_style["text_color_slug"]}"'
    if nav_style.get("font_size"):
        typography["fontSize"] = nav_style["font_size"]
    if nav_style.get("font_weight"):
        typography["fontWeight"] = nav_style["font_weight"]
    nav_attrs += ',"style":' + json.dumps({"typography": typography}, separators=(",", ":"))

    # The logo's real size is applied via a global CSS override (see
    # build_global_styles_content()) matching the live site's own
    # height-constrained/auto-width sizing -- not a block attribute here,
    # since core/site-logo's own "width" attribute only supports the
    # opposite (fixed width, auto height).
    return (
        '<!-- wp:group {"align":"full","className":"has-global-padding","layout":{"type":"constrained"}} -->\n'
        '<div class="wp-block-group alignfull has-global-padding">\n'
        '<!-- wp:group {"align":"wide","layout":{"type":"flex","justifyContent":"space-between"}} -->\n'
        '<div class="wp-block-group alignwide">\n'
        "<!-- wp:site-logo /-->\n"
        f'<!-- wp:navigation {{{nav_attrs}}} /-->\n'
        "</div>\n"
        "<!-- /wp:group -->\n"
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

    Wrapped the same two-level way as the header (an outer full-width
    group, an inner "align":"wide" one) and for the same reason,
    confirmed on a real test import: without an explicit wide inner
    width, the nav links wrap onto a second line that the original site
    never does, because a plain "constrained" child inherits the
    theme's much narrower default *reading* width, not its *wide* one.
    The outer group is also where the footer's real background color
    goes (brand.json's "footer_background", when the crawl captured
    one) -- confirmed distinct from the page's own background on the
    live site, not something a plain content-width group could paint
    edge to edge on its own.

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
            '<!-- wp:navigation {"align":"wide","layout":{"type":"flex","justifyContent":"center"},'
            '"overlayMenu":"never"} -->\n'
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
            '<!-- wp:social-links {"className":"is-style-logos-only",'
            '"layout":{"type":"flex","justifyContent":"center"}} -->\n'
            '<ul class="wp-block-social-links is-style-logos-only is-content-justification-center '
            'is-layout-flex wp-block-social-links-is-layout-flex">\n'
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
        # core/paragraph's "align" attribute is overloaded -- it can hold
        # EITHER a text-alignment value ("center", rendered as a
        # has-text-align-center class) OR a layout-width value ("wide"/
        # "full", rendered as an alignwide/alignfull class), never both.
        # Wrapping the paragraph in its own extra "align":"wide" group
        # (an earlier attempt at this fix) doesn't work either: confirmed
        # via a real test import's computed layout that the WRAPPER group
        # does become 1232px wide, but the plain paragraph inside it is
        # still just an unmarked child of an ".is-layout-constrained"
        # group, so WordPress's generic nested-layout CSS rule clamps it
        # right back down to the theme's narrow *content* width (620px)
        # -- only a child actually carrying the "alignwide" class of its
        # own escapes that rule (the same reason nav_block above declares
        # "align":"wide" on itself rather than relying on its parent).
        # So: give the paragraph "alignwide" directly, and do the
        # centering via "migration-text-center" (className + a real
        # shared CSS rule, see _extra_css_rules()) instead of an inline
        # style, since the "align" attribute is already spoken for and
        # an inline style here with no matching JSON attribute fails
        # Gutenberg's block validation -- see _role_style_bits()'s
        # docstring for the same reasoning applied elsewhere.
        copyright_block = (
            '<!-- wp:paragraph {"align":"wide","fontSize":"small","className":"migration-text-center"} -->\n'
            f'<p class="alignwide has-small-font-size migration-text-center">{copyright_html}</p>\n'
            "<!-- /wp:paragraph -->\n"
        )

    footer_bg_slug = None
    if _BRAND and (_BRAND.get("colors") or {}).get("footer_background"):
        footer_bg_slug = "footer-background"
    outer_attrs = '"align":"full","layout":{"type":"constrained"}'
    outer_classes = "wp-block-group alignfull"
    outer_style = {"spacing": {"padding": {"top": "var:preset|spacing|50", "bottom": "var:preset|spacing|50"}}}
    if footer_bg_slug:
        outer_attrs += f',"backgroundColor":"{footer_bg_slug}"'
        outer_classes += f" has-{footer_bg_slug}-background-color has-background"
    outer_attrs += ',"style":' + json.dumps(outer_style, separators=(",", ":"))
    outer_style_css = "padding-top:var(--wp--preset--spacing--50);padding-bottom:var(--wp--preset--spacing--50)"

    content = (
        f'<!-- wp:group {{{outer_attrs}}} -->\n'
        f'<div class="{outer_classes}" style="{outer_style_css}">\n'
        '<!-- wp:group {"align":"wide","style":{"spacing":{"blockGap":"1.5rem"}},"layout":{"type":"constrained"}} -->\n'
        '<div class="wp-block-group alignwide">\n'
        f"{nav_block}{social_block}{copyright_block}"
        "</div>\n"
        "<!-- /wp:group -->\n"
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


def build_page_template_content():
    """Override for the target theme's default "page" template --
    same overall structure (header template part, the real post
    content, footer template part) but without the wp:post-title and
    wp:post-featured-image blocks Twenty Twenty-Four's own page.html
    always includes, and with no added spacer between the header and
    the content.

    WordPress's generic page template shows the post's title
    prominently above its content on every page -- reasonable for a
    blank new WP page, but redundant here: confirmed on a real test
    import, a page titled "AI Solutions" rendered that title twice --
    once as this generic banner, and again moments later as the page's
    own in-content heading, which the original site never did. No
    featured images are set on any imported page either, so that block
    would only ever render empty space; dropped for the same reason.

    No wp:spacer between the header and wp:post-content either -- the
    live site's first headline sits right under the nav bar with no
    extra gap, but an explicit spacer here stacked on top of the
    header's own bottom padding and the content's default block gap,
    confirmed on a real test import to visibly widen that gap versus
    the live site.
    """
    return (
        '<!-- wp:template-part {"slug":"header","area":"header","tagName":"header"} /-->\n\n'
        '<!-- wp:group {"tagName":"main"} -->\n'
        '<main class="wp-block-group">\n'
        '<!-- wp:post-content {"lock":{"move":false,"remove":true},"layout":{"type":"constrained"}} /-->\n'
        '</main>\n'
        '<!-- /wp:group -->\n\n'
        '<!-- wp:template-part {"slug":"footer","area":"footer","tagName":"footer"} /-->'
    )


def build_page_template_item_xml(post_id, content):
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    post_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return f"""  <item>
    <title>{xml_escape('Page')}</title>
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
    <wp:post_name><![CDATA[page]]></wp:post_name>
    <wp:status><![CDATA[publish]]></wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>0</wp:menu_order>
    <wp:post_type><![CDATA[wp_template]]></wp:post_type>
    <wp:post_password><![CDATA[]]></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
    <category domain="wp_theme" nicename="{THEME_SLUG}"><![CDATA[{THEME_SLUG}]]></category>
  </item>"""


def build_wxr(data, brand=None):
    global _BRAND
    _BRAND = brand

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

    global _PAGES_BY_SLUG
    _PAGES_BY_SLUG = pages_by_slug

    # A post_feed card's own thumbnail is only capturable when the crawler
    # happens to see it (see mark_post_feeds() -- it's loaded via
    # client-side JS on the *referring* page, not always present). Every
    # post this widget links to is itself a crawled page, though, and
    # crawler_agent.py's og:image extraction gives that page's own real
    # featured image regardless -- a reliable fallback keyed by slug.
    global _FEATURED_IMAGE_BY_SLUG
    _FEATURED_IMAGE_BY_SLUG = {
        page["slug"]: page["featured_image"]
        for page in data["pages"]
        if page.get("featured_image")
    }

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

    if brand:
        global_styles_content = build_global_styles_content(brand)
        if global_styles_content:
            items_xml.append(build_global_styles_item_xml(40002, global_styles_content))

        # A second, independent copy of the same CSS rules via WordPress's
        # Additional CSS mechanism -- see build_custom_css_content()'s
        # docstring for why relying on wp_global_styles alone isn't
        # reliable enough for rules this visible to silently drop.
        custom_css_content = build_custom_css_content(brand)
        if custom_css_content:
            items_xml.append(build_custom_css_item_xml(40005, custom_css_content))

    items_xml.append(build_page_template_item_xml(40003, build_page_template_content()))

    # The site logo, at a fixed post_id (see LOGO_ATTACHMENT_ID) so
    # build_apply_branding_php() can reference it directly without any
    # fuzzy matching-by-URL after import. WXR has no mechanism to set the
    # site_logo option/custom_logo theme mod itself -- that's what the
    # generated apply_branding.php companion script is for.
    logo = (brand or {}).get("logo") or {}
    if logo.get("url") and canonical_attachment_url(logo["url"]):
        items_xml.append(
            build_attachment_item_xml(logo["url"], logo.get("alt", ""), LOGO_ATTACHMENT_ID)
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
    card_group_image_instances = sum(
        1
        for p in pages
        for b in p["blocks"]
        if b["type"] == "card_group"
        for card in b.get("cards", [])
        if card.get("image")
    )
    post_feed_image_instances = sum(
        1
        for p in pages
        for b in p["blocks"]
        if b["type"] == "post_feed"
        for post in b.get("posts", [])
        if post.get("image_src")
    )
    image_instances = (
        count_blocks("image") + count_blocks("media_text")
        + card_group_image_instances + post_feed_image_instances
    )
    media_text_count = count_blocks("media_text")
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
        if media_text_count:
            lines.append(
                f"- **Side-by-side layout preserved** ({media_text_count} section(s)): the "
                "original site's two-column image+text sections (detected from its real Grid/"
                "GridCell markup) are generated as WordPress Media & Text blocks instead of a "
                "plain stacked image and paragraph, matching the original layout rather than "
                "flattening it."
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
            "below) -- and WordPress's Navigation block correctly hides any menu link that "
            "points to a page still in draft, the same way it would for any other unpublished "
            "page. Confirmed with a full local WordPress + Twenty Twenty-Four reproduction: "
            "with only one page published, the nav showed only that page's own branch (e.g. "
            "just \"AI\" > \"AI Solutions\"); publishing every page made the complete nav -- all "
            "top-level items, all category dropdowns, every child link -- render correctly in "
            "both the header and footer. This is expected, correct WordPress behavior, not a "
            "defect in this file. It also means a *sparse-looking* nav while reviewing in draft "
            "isn't a red flag by itself -- it's just reflecting how much of the site is "
            "published so far. To see the complete nav before committing to a real go-live, "
            "temporarily publish all pages, review, then set them back to Draft if you're not "
            "ready to launch. WordPress's own draft-preview mode (`?preview=true`) has also been "
            "observed failing to render the Navigation block's menu items at all, even for "
            "published targets -- don't trust a preview link's nav either; check a real "
            "published URL."
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
            f"- **Brand tokens applied automatically**: {len(brand.get('typography', {}))} "
            f"typography role(s), colors ({color_list or 'none found'}). The WXR file includes a "
            "\"Custom Styles\" entry (a real WordPress `wp_global_styles` post -- the same object "
            "the Site Editor's own Styles panel creates when a person sets colors/fonts by hand) "
            "that applies the extracted background, text, link, and button colors plus the body "
            "font sitewide on import -- no manual Site Editor configuration needed. Also included "
            f"as `{OUT_THEME}`, a standalone theme.json fragment, for reference or for merging "
            "into a theme's own theme.json directly."
        )
        if logo and logo.get("url") and canonical_attachment_url(logo["url"]):
            lines.append(
                f"- **Logo** found at {logo['url']} -- included in the WXR as a real media-"
                f"library attachment (post_id {LOGO_ATTACHMENT_ID}). Setting it as the site's "
                "active logo (the `site_logo` option/`custom_logo` theme mod) isn't something "
                f"WXR can do on its own, though -- run `php {OUT_APPLY_BRANDING}` once after "
                "importing (from the WordPress root) to finish the job."
            )
        elif logo:
            lines.append(
                f"- **Logo** found at {logo['url']} -- its URL has no filename/extension "
                "WordPress's importer can download (see the image note above), so it couldn't "
                "be included as a real attachment. Download it from the original site and set it "
                "via Appearance → Editor → Site Identity."
            )
        if _google_fonts_href(brand):
            lines.append(
                f"- **Brand fonts loaded for real**: `theme.json`/\"Custom Styles\" only "
                "*register* the extracted font-family names -- nothing else fetches the actual "
                "font files, so every role using one would otherwise silently fall back to its "
                f"generic fallback (e.g. Georgia/serif). `php {OUT_APPLY_BRANDING}` (see above) "
                "also writes a small must-use plugin that loads the real fonts from Google Fonts "
                "on every page, sitewide. Without file access to run that script, WordPress's "
                "built-in Font Library (Appearance → Editor → Styles → Typography, WP 6.5+) is "
                "the no-code alternative -- but confirmed a real gotcha there: **installing** a "
                "font only adds it to the library, each individual weight/style face still needs "
                "to be **activated** separately (checked on) before it actually loads. A font "
                "showing e.g. \"1 of 8 active\" in the Fonts screen means only one weight is live "
                "-- headings/nav using a different weight will silently fall back to the generic "
                "font until every face that role needs is checked on too. Also survives a database "
                "reset worse than the must-use-plugin route: Font Library's installed fonts are "
                "database entries, wiped by a full reset, and need reinstalling+reactivating "
                "afterward -- the must-use plugin is a file on disk that a DB reset doesn't touch."
            )
    lines.append("")
    lines.append("## What's in the attached files")
    lines.append("")
    lines.append("- `stratecon-migration.xml` — import via **Tools → Import → WordPress** on any WordPress site (install the free WordPress Importer plugin if prompted). Pages import as **drafts** so nothing goes live automatically.")
    lines.append("- `redirects.csv` — import into the free **Redirection** plugin to preserve old URLs once the new site goes live.")
    if brand:
        lines.append(f"- `{OUT_THEME}` — the extracted color palette and font list in WordPress's block-theme format.")
        if build_apply_branding_php(brand):
            lines.append(
                f"- `{OUT_APPLY_BRANDING}` — run once after each fresh import "
                f"(`php {OUT_APPLY_BRANDING}` from the WordPress root) to set the site logo and "
                "load the real brand fonts; see the notes above."
            )
    return "\n".join(lines) + "\n"


def _derive_brand_palette_and_fonts(brand):
    """Shared by build_theme_json() (a standalone reference file) and
    build_global_styles_content() (the WXR item that makes these tokens
    actually apply on import) so the two never drift apart. Returns
    (palette, font_families, body_font_slug) -- body_font_slug is the
    slug of whichever font family carries the BodyAlpha role (falling
    back to the first font family found, or None if brand.json had no
    typography at all), used to set the sitewide base font."""
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
    add_color("footer-background", "Footer Background", colors.get("footer_background"))

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
    body_font_slug = None
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
        if "BodyAlpha" in roles:
            body_font_slug = slug

    if body_font_slug is None and font_families:
        body_font_slug = font_families[0]["slug"]

    return palette, font_families, body_font_slug


def build_theme_json(brand):
    """A WordPress block-theme theme.json fragment (settings.color.palette
    and settings.typography.fontFamilies) built from brand_agent.py's
    extracted tokens. Not a complete theme.json -- WP block themes need
    more than colors/fonts to function -- this is the piece a human (or
    a future Architecture Agent) merges into one, or uses as a reference
    when setting the palette/fonts by hand in the Site Editor. See
    build_global_styles_content() for the WXR item that actually applies
    these on import, rather than just registering them as available."""
    palette, font_families, _ = _derive_brand_palette_and_fonts(brand)
    theme = {
        "$schema": "https://schemas.wp.org/trunk/theme.json",
        "version": 2,
        "settings": {
            "color": {"palette": palette},
            "typography": {"fontFamilies": font_families},
        },
    }
    return json.dumps(theme, indent=2) + "\n"


def _darken_hex(hex_color, factor=0.82):
    """A simple RGB-scaled darker shade of a "#rrggbb" color, for a
    button hover state -- brand.json/getComputedStyle() has no way to
    capture a live site's actual :hover color (that only exists on
    mouseover, which a static crawl never triggers), so this derives a
    plausible one from the button's own resting color instead of
    leaving hover unstyled. Returns the input unchanged if it isn't a
    recognizable "#rrggbb" hex string.
    """
    if not hex_color or not re.fullmatch(r"#[0-9a-fA-F]{6}", hex_color):
        return hex_color
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return "#%02x%02x%02x" % (
        max(0, min(255, round(r * factor))),
        max(0, min(255, round(g * factor))),
        max(0, min(255, round(b * factor))),
    )


def _extra_css_rules(brand):
    """Raw CSS rules -- Google Fonts import, logo height/width override,
    button hover, blog card thumbnail hover -- shared between
    build_global_styles_content() (its "styles.css" field) and
    build_custom_css_content() (a fully separate WordPress "Additional
    CSS" post). Kept as one shared list so the two don't drift out of
    sync, but written to WordPress as two independent copies -- see
    build_custom_css_content()'s docstring for why relying on
    wp_global_styles alone isn't reliable enough for rules this
    important to drop silently."""
    rules = []

    # theme.json/global-styles only ever registers a font-family's *name*
    # (settings.typography.fontFamilies) -- it never fetches the font
    # file itself, confirmed on a real test import: heading/nav text got
    # the "has-cabin-font-family" class exactly as intended but rendered
    # in Georgia/serif anyway, since nothing ever actually loaded "Cabin"
    # from anywhere. This project's only other fix for that,
    # build_apply_branding_php(), needs to be run from a shell on the
    # server -- not an option without cPanel/SSH access. A CSS @import
    # of the same Google Fonts stylesheet URL, right here in the same
    # custom_css/global-styles content this function already feeds,
    # loads the real font with zero server-side execution: WordPress
    # just writes this CSS into a <style> tag in wp_head, and @import
    # needs nothing more than that to fetch and apply the real font
    # files. Must stay the very first rule -- CSS requires @import to
    # precede every other rule in its stylesheet or browsers discard it.
    fonts_href = _google_fonts_href(brand or {})
    if fonts_href:
        rules.append(f'@import url("{fonts_href}");')

    # The logo's real rendered box, confirmed against the live site's own
    # <img> via getBoundingClientRect() in brand_agent.py -- not just a
    # height to scale from. WordPress's importer can only ever download
    # GoDaddy's *uncropped* original asset here: canonical_attachment_url()
    # has to truncate the URL right after its file extension for
    # WordPress's fetch_remote_file() to accept it at all, which throws
    # away the "cg:true" crop-guide directive in brand.json's logo URL --
    # confirmed via a real site's DevTools inspector, the imported file's
    # natural size (887x204, aspect ~4.35) is measurably wider/squatter
    # than the live header's actual rendered logo (278x88, aspect
    # ~3.16). A plain "height:88px;width:auto" scaled that extra width
    # right along with it -- still 88px tall as intended, but ~380px
    # wide instead of ~278px, visibly oversized/off-brand.
    #
    # Tried object-fit:cover first (crop to exactly fill 278x88) on the
    # assumption the uncropped asset just had extra padding around a
    # smaller mark -- confirmed WRONG by directly viewing the downloaded
    # file: it's a tightly-fitted two-line lockup (icon + "STRATECON" /
    # "TECH ADVISORS") with the text running edge to edge, no slack to
    # crop into. cover clipped real letters off both lines. object-
    # fit:contain instead scales the whole, undistorted image down to
    # fit within 278x88 (letterboxed on whichever axis has slack, here
    # top/bottom, landing around 278x64) -- smaller than the live site's
    # actual box, but shows the complete, legible logo rather than a
    # mangled crop. Best available fix without the actual crop GoDaddy's
    # CDN applied, which WordPress's importer has no way to request.
    logo = (brand or {}).get("logo") or {}
    if logo.get("height") and logo.get("width"):
        rules.append(
            (
                ".wp-block-site-logo img{height:%dpx!important;width:%dpx!important;"
                "object-fit:contain!important;object-position:center!important;"
                "max-height:none!important;max-width:none!important}"
            ) % (logo["height"], logo["width"])
        )
    elif logo.get("height"):
        rules.append(
            (
                ".wp-block-site-logo img{height:%dpx!important;width:auto!important;"
                "max-height:none!important;max-width:none!important}"
            ) % logo["height"]
        )

    # The live site's buttons visibly change color on hover; a plain
    # WordPress button with no explicit hover style just sits static.
    # No live :hover color to copy (see _darken_hex()'s docstring), so
    # this darkens the button's own resting background instead of
    # leaving hover unstyled. Duplicates build_global_styles_content()'s
    # elements.button:hover as a raw rule -- see _extra_css_rules()'s
    # own docstring for why.
    palette = {c["slug"]: c for c in _derive_brand_palette_and_fonts(brand)[0]} if brand else {}
    if "primary" in palette:
        hover_bg = _darken_hex(palette["primary"]["color"])
        if hover_bg != palette["primary"]["color"]:
            rules.append(
                ".wp-element-button:hover,.wp-block-button__link:hover{"
                f"background-color:{hover_bg}!important}}"
            )

    # A drop-shadow behind each "AI Insights" blog card thumbnail on
    # hover, matching the live site's own hover treatment there (see the
    # "post-feed-thumbnail" class added in block_to_gutenberg()'s
    # post_feed renderer). Scoped to that class rather than every image
    # on the site, since ordinary content images don't get this
    # treatment on the live site.
    rules.append(
        ".post-feed-thumbnail img{transition:box-shadow 0.2s ease}"
        ".post-feed-thumbnail img:hover{box-shadow:0 8px 24px rgba(0,0,0,0.18)}"
    )

    # One real CSS rule per typography role's font-size/weight -- see
    # _role_style_bits()'s docstring for why this lives here (a shared
    # class) instead of inline on each element.
    for role, spec in (brand or {}).get("typography", {}).items():
        size = spec.get("font_size")
        weight = spec.get("font_weight")
        if not size and not weight:
            continue
        decls = []
        if size:
            decls.append(f"font-size:{size}")
        if weight:
            decls.append(f"font-weight:{weight}")
        rules.append(f".{role_class_name(role)}{{{';'.join(decls)}}}")

    # Structural utility classes used in place of untracked inline
    # styles across block_to_gutenberg() -- see the same validation-
    # mismatch reasoning as _role_style_bits()'s docstring. Written here
    # unconditionally (not brand-dependent) since these blocks render
    # the same way regardless of whether brand.json was supplied.
    rules.append(
        ".migration-divider-hr{flex:1 1 auto}"
        ".migration-section-divider{margin-top:56px;margin-bottom:56px}"
        # flex-grow:0 is load-bearing, not decorative: core/columns'
        # own default layout is a flex row that stretches every child
        # equally (flex-grow:1, flex-basis:0) via a per-instance
        # generated class/selector more specific than a single plain
        # class -- confirmed on a real test import: without !important,
        # this rule was present in the stylesheet but computed style
        # still showed flex-grow:1/flex-basis:0, i.e. core's own rule
        # was winning outright, so a trailing row with just one card
        # was still free to grow and fill the whole row. !important
        # sidesteps that specificity fight instead of trying to out-rank
        # a per-instance selector this code doesn't control the name of.
        ".migration-flex-column{flex-basis:33.33%!important;flex-grow:0!important;"
        "display:flex!important;flex-direction:column!important}"
        ".migration-columns-gap{column-gap:2.5rem;row-gap:2.5rem}"
        ".migration-cta-buttons{margin-top:auto;padding-top:1.5rem}"
        ".migration-push-bottom{margin-top:auto}"
        ".migration-text-center{text-align:center}"
    )
    return rules


def build_custom_css_content(brand):
    """Raw CSS for a WordPress "custom_css" post -- the same storage
    Appearance > Customize > Additional CSS writes to, always output in
    wp_head() via wp_custom_css_cb() regardless of the active theme's
    block-editor state. This is a second, independent path to the same
    rules build_global_styles_content() already carries in its
    "styles.css" field, not a replacement for it.

    That redundancy is deliberate: wp_global_styles is a *singleton*
    custom post per theme (post_name "wp-global-styles-{theme}"), and
    confirmed on this project's own header/footer template parts (a
    real, previously-fixed bug) -- WordPress lazily creates a real row
    for a theme's own global styles/template parts the moment a person
    so much as opens Appearance > Editor and saves anything, even
    something unrelated like the site logo. If that happens before a
    WXR import runs, or if an earlier import's wp_global_styles row is
    already sitting there, later re-imports are not guaranteed to
    overwrite its content -- exactly the kind of silent, hard-to-diagnose
    failure this project has already hit once for template parts, and
    the reported symptom here (color/palette overrides visibly active,
    but this "styles.css" field's own rules -- like the logo height cap
    -- not taking effect) matches it. "custom_css" is a completely
    separate post type with no relationship to wp_global_styles, so it
    isn't exposed to that same collision/staleness risk.

    Returns None if there are no rules to write (mirrors
    build_global_styles_content()'s "empty override is worse than
    nothing" reasoning).
    """
    rules = _extra_css_rules(brand)
    if not rules:
        return None
    return "".join(rules)


def build_global_styles_content(brand):
    """JSON content for a wp_global_styles post -- the same object
    WordPress's own Site Editor > Styles panel creates and edits when a
    person customizes colors/fonts by hand. Unlike build_theme_json()'s
    output (a standalone reference file nothing applies automatically),
    importing this WXR item makes the extracted palette and fonts the
    site's live, active styles immediately -- no manual Site Editor
    configuration needed.

    "settings.color.palette"/"settings.typography.fontFamilies" only
    register the tokens as available (e.g. in the color picker); the
    "styles" section below is what actually paints them on -- background/
    text/link/button colors and the base font -- referencing the
    palette/font slugs via the standard "var:preset|..." token so they
    stay in sync with the palette entries rather than duplicating literal
    values. Returns None if brand has no usable colors or fonts, since an
    empty override is worse than leaving the theme's own defaults alone.
    """
    palette = {c["slug"]: c for c in _derive_brand_palette_and_fonts(brand)[0]}
    _, font_families, body_font_slug = _derive_brand_palette_and_fonts(brand)
    if not palette and not font_families:
        return None

    styles = {}
    color_styles = {}
    if "background" in palette:
        color_styles["background"] = "var:preset|color|background"
    if "foreground" in palette:
        color_styles["text"] = "var:preset|color|foreground"
    if color_styles:
        styles["color"] = color_styles

    if body_font_slug:
        styles["typography"] = {"fontFamily": f"var:preset|font-family|{body_font_slug}"}

    elements = {}
    if "link" in palette:
        elements["link"] = {"color": {"text": "var:preset|color|link"}}
    if "primary" in palette or "primary-text" in palette:
        button_colors = {}
        if "primary" in palette:
            button_colors["background"] = "var:preset|color|primary"
        if "primary-text" in palette:
            button_colors["text"] = "var:preset|color|primary-text"
        button_el = {"color": button_colors}
        # The live site's buttons visibly change color on hover; a plain
        # WordPress button with no explicit hover style just sits static.
        # No live :hover color to copy (see _darken_hex()'s docstring),
        # so this darkens the button's own resting background instead of
        # leaving hover unstyled.
        if "primary" in palette:
            hover_bg = _darken_hex(palette["primary"]["color"])
            if hover_bg != palette["primary"]["color"]:
                button_el[":hover"] = {"color": {"background": hover_bg}}
        elements["button"] = button_el
    if elements:
        styles["elements"] = elements

    css_rules = _extra_css_rules(brand)
    if css_rules:
        styles["css"] = "".join(css_rules)

    global_styles = {
        "version": 2,
        "isGlobalStylesUserThemeJSON": True,
        "settings": {
            "color": {"palette": list(palette.values())},
            "typography": {"fontFamilies": font_families},
        },
        "styles": styles,
    }
    return json.dumps(global_styles, separators=(",", ":"))


def build_global_styles_item_xml(post_id, content):
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    post_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    slug = f"wp-global-styles-{THEME_SLUG}"
    return f"""  <item>
    <title>{xml_escape('Custom Styles')}</title>
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
    <wp:post_type><![CDATA[wp_global_styles]]></wp:post_type>
    <wp:post_password><![CDATA[]]></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
    <category domain="wp_theme" nicename="{THEME_SLUG}"><![CDATA[{THEME_SLUG}]]></category>
  </item>"""


def build_custom_css_item_xml(post_id, css):
    """A WXR item for the "custom_css" post type -- WordPress's Additional
    CSS storage (see build_custom_css_content()'s docstring for why this
    exists as a second, independent copy of the same rules). Its
    post_name has to be exactly the target theme's stylesheet slug --
    that's the literal lookup key wp_get_custom_css() uses to find it,
    not just a label like the other post types here use their slugs for."""
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    post_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return f"""  <item>
    <title>{xml_escape(THEME_SLUG)}</title>
    <link>{NEW_BASE_URL}/</link>
    <pubDate>{pub_date}</pubDate>
    <dc:creator><![CDATA[migration-agent]]></dc:creator>
    <guid isPermaLink="false">{NEW_BASE_URL}/?p={post_id}</guid>
    <description></description>
    <content:encoded><![CDATA[{css}]]></content:encoded>
    <excerpt:encoded><![CDATA[]]></excerpt:encoded>
    <wp:post_id>{post_id}</wp:post_id>
    <wp:post_date><![CDATA[{post_date}]]></wp:post_date>
    <wp:post_date_gmt><![CDATA[{post_date}]]></wp:post_date_gmt>
    <wp:comment_status><![CDATA[closed]]></wp:comment_status>
    <wp:ping_status><![CDATA[closed]]></wp:ping_status>
    <wp:post_name><![CDATA[{THEME_SLUG}]]></wp:post_name>
    <wp:status><![CDATA[publish]]></wp:status>
    <wp:post_parent>0</wp:post_parent>
    <wp:menu_order>0</wp:menu_order>
    <wp:post_type><![CDATA[custom_css]]></wp:post_type>
    <wp:post_password><![CDATA[]]></wp:post_password>
    <wp:is_sticky>0</wp:is_sticky>
  </item>"""


def _google_fonts_href(brand):
    """A Google Fonts CSS2 stylesheet URL requesting every font family
    brand.json found, at the actual weights its typography roles use --
    not just weight 400. Returns None if brand has no typography.

    WordPress's theme.json only registers a font-family's *name*
    (settings.typography.fontFamilies) -- it never fetches or serves the
    font file itself. Confirmed on a real test import: heading/nav text
    got the "has-cabin-font-family"/"has-playfair-display-font-family"
    classes exactly as intended, but rendered in Georgia/serif anyway --
    every browser silently falls through to theme.json's own fallback
    stack because "Cabin"/"Playfair Display" were never actually loaded
    from anywhere. build_apply_branding_php() wires this URL into a
    <link> via wp_head so the fonts genuinely load, not just get
    referenced by class name.
    """
    _, font_families, _ = _derive_brand_palette_and_fonts(brand)
    if not font_families:
        return None

    weights_by_family = {}
    for info in brand.get("typography", {}).values():
        family = info.get("font_family")
        weight = info.get("font_weight")
        if family and weight:
            weights_by_family.setdefault(family, set()).add(str(weight))

    family_params = []
    for f in font_families:
        primary_name = f["fontFamily"].split(",")[0].strip().strip("\"'")
        name_param = primary_name.replace(" ", "+")
        weights = sorted(weights_by_family.get(f["fontFamily"], {"400"}))
        family_params.append(f"family={name_param}:wght@{';'.join(weights)}")

    return "https://fonts.googleapis.com/css2?" + "&".join(family_params) + "&display=swap"


def build_apply_branding_php(brand):
    """A companion PHP script -- run once after each fresh WXR import,
    the same way this project's other one-off setup scripts work --
    that finishes what WXR itself can't: setting the site logo and
    loading the real brand fonts. Both are WordPress *options*/theme
    mods (site_logo, custom_logo) or a wp_head-enqueued stylesheet, not
    posts or terms, so there's no WXR item that can carry them; the
    generated WXR gets the logo file into the media library as a normal
    attachment (see LOGO_ATTACHMENT_ID) and the font CSS2 URL is
    computed here, but something still has to flip those switches after
    import. Written to run from the WordPress root (next to wp-load.php)
    via `php apply_branding.php` -- confirmed against a real local
    WordPress install.

    Returns None if brand has neither a usable logo nor any typography,
    since there'd be nothing for the script to do.
    """
    logo = (brand or {}).get("logo") or {}
    has_logo = bool(logo.get("url") and canonical_attachment_url(logo["url"]))
    fonts_href = _google_fonts_href(brand or {})

    if not has_logo and not fonts_href:
        return None

    parts = [
        "<?php\n"
        "// Run once after each fresh WXR import: php apply_branding.php\n"
        "// (from the WordPress root, next to wp-load.php). Finishes what\n"
        "// WXR itself has no mechanism for -- the site logo and real\n"
        "// brand fonts are WordPress options/theme mods and a wp_head\n"
        "// stylesheet link, not posts or terms.\n"
        "require_once(__DIR__ . '/wp-load.php');\n"
    ]

    if has_logo:
        alt = logo.get("alt", "").replace("'", "\\'")
        parts.append(
            f"\n"
            f"// Site logo -- the file itself already came in as attachment\n"
            f"// post_id {LOGO_ATTACHMENT_ID} via the WXR import.\n"
            f"$logo_id = {LOGO_ATTACHMENT_ID};\n"
            f"if (get_post($logo_id)) {{\n"
            f"    update_option('site_logo', $logo_id);       // block themes' core/site-logo\n"
            f"    set_theme_mod('custom_logo', $logo_id);     // classic-theme fallback\n"
            f"    echo \"Site logo set (attachment {LOGO_ATTACHMENT_ID}).\\n\";\n"
            f"}} else {{\n"
            f"    echo \"Attachment {LOGO_ATTACHMENT_ID} not found -- import the WXR file first"
            f" (with 'Download and import file attachments' checked) before running this"
            f" script.\\n\";\n"
            f"}}\n"
        )

    if fonts_href:
        fonts_href_escaped = fonts_href.replace("'", "\\'")
        parts.append(
            f"\n"
            f"// Real brand fonts -- theme.json/wp_global_styles only *register*\n"
            f"// font-family names; nothing else loads the actual font files, so\n"
            f"// every role using one silently falls back to its generic fallback\n"
            f"// (e.g. Georgia/serif) without this. Written as a must-use plugin\n"
            f"// so it keeps loading on every future request, not just this run.\n"
            f"$mu_dir = WPMU_PLUGIN_DIR;\n"
            f"if (!file_exists($mu_dir)) {{\n"
            f"    wp_mkdir_p($mu_dir);\n"
            f"}}\n"
            f"$mu_plugin = <<<'PHP'\n"
            f"<?php\n"
            f"/* Plugin Name: Migration Brand Fonts (auto-generated) */\n"
            f"add_action('wp_head', function () {{\n"
            f"    echo '<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">' . PHP_EOL;\n"
            f"    echo '<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>' . PHP_EOL;\n"
            f"    echo '<link rel=\"stylesheet\" href=\"{fonts_href_escaped}\">' . PHP_EOL;\n"
            f"}}, 1);\n"
            f"PHP;\n"
            f"file_put_contents($mu_dir . '/migration-brand-fonts.php', $mu_plugin);\n"
            f"echo \"Brand fonts wired up via a must-use plugin (\" . $mu_dir . \"/migration-brand-fonts.php).\\n\";\n"
        )

    return "".join(parts)


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
        f.write(build_wxr(data, brand))

    with open(OUT_REDIRECTS, "w") as f:
        f.write(build_redirects_csv(data))

    with open(OUT_QA, "w") as f:
        f.write(build_qa_report(data, brand))

    outputs = [OUT_WXR, OUT_REDIRECTS, OUT_QA]
    if brand:
        with open(OUT_THEME, "w") as f:
            f.write(build_theme_json(brand))
        outputs.append(OUT_THEME)

        apply_branding_php = build_apply_branding_php(brand)
        if apply_branding_php:
            with open(OUT_APPLY_BRANDING, "w") as f:
                f.write(apply_branding_php)
            outputs.append(OUT_APPLY_BRANDING)
    else:
        print(f"({SRC_BRAND} not found -- skipping {OUT_THEME}; run brand_agent.py first to include it)")

    print(f"Wrote {', '.join(outputs)}")


if __name__ == "__main__":
    main()
