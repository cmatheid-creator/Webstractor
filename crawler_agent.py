#!/usr/bin/env python3
"""
Crawler Agent (prototype)
--------------------------
Discovers every page on a target site, renders it with a real browser
(so JS-built pages like GoDaddy Website Builder sites work), and extracts
structured content into the SAME schema generator_agent.py already reads.

Run this somewhere with real internet access -- e.g. Claude Code on your
own machine -- not inside a locked-down sandbox.

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python3 crawler_agent.py https://stratecon.tech

Output:
    structured_content.json  (same schema as the manually-built version
    already used by generator_agent.py -- feed this straight into it)
"""

import sys
import json
import re
from urllib.parse import urlparse, urljoin

from playwright.sync_api import sync_playwright


def same_domain(base, url):
    return urlparse(base).netloc == urlparse(url).netloc


def slugify(url, base):
    path = urlparse(url).path.strip("/")
    if not path:
        return "home"
    # Use the last path segment so nested paths (e.g. GoDaddy blog posts
    # at /blog/f/<slug>) produce a flat, valid WP post_name instead of a
    # slug containing "/".
    return path.rsplit("/", 1)[-1]


def element_text(el):
    """Get an element's text, tolerating nodes inner_text() rejects (e.g.
    an SVG icon matched by a broad selector isn't an HTMLElement)."""
    try:
        text = el.inner_text()
    except Exception:
        try:
            text = el.text_content()
        except Exception:
            text = ""
    return " ".join((text or "").split())


MIN_CONTENT_IMAGE_SIZE = 24  # px; filters tracking pixels and tiny UI icons


# Site-wide chrome to exclude from content extraction: any of the three
# <nav> elements on a GoDaddy Website Builder page (main nav, mobile nav
# drawer, footer nav), the [data-ux="Header"] wrapper around the whole
# header (which also contains the logo and hamburger icon, not just the
# nav), and the footer widget (marked with the standard ARIA landmark
# role="contentinfo", not a <footer> tag). Without this, the entire
# site navigation -- every heading, list item, and image in the header
# and footer -- gets extracted as if it were unique page content, since
# it's still just <ul>/<li>/<img> markup like everything else on the
# page. That's not a rare edge case here: it silently duplicated the
# same ~40-item nav list (twice -- once for the visible nav, once for
# the mobile drawer) onto the front of every single one of the 37
# extracted pages.
#
# The cookie-consent banner (data-aid="FOOTER_COOKIE_BANNER_RENDERED")
# is a separate case: despite the "FOOTER_" name it's its own floating
# widget elsewhere in the page, not nested inside the real
# role="contentinfo" footer -- confirmed by inspecting the live markup
# after its heading+paragraph ("This website uses cookies." / "We use
# cookies to analyze website traffic...") turned up as extracted
# content on every single page, sometimes in the middle of real content
# rather than at the end.
CHROME_SELECTOR = 'nav, [data-ux="Header"], [role="contentinfo"], [data-aid="FOOTER_COOKIE_BANNER_RENDERED"]'


def mark_media_text_pairs(page):
    """Tags DOM elements that form a GoDaddy Website Builder side-by-side
    image+text section, so extract_blocks() can group them into one
    "media_text" block (rendered as a real side-by-side WordPress Media
    & Text block) instead of extracting the image and text as separate,
    stacked blocks the way everything else on the page is handled.

    Confirmed via the live site's actual markup: a two-column layout is
    a <div data-ux="Grid"> with exactly two direct <div data-ux=
    "GridCell"> children -- one holding an <img> and little else, the
    other holding the real text content (a heading/paragraphs/list).
    This pattern is also used for other Grid layouts (e.g. wrapping a
    single column, or multi-column text-only sections), so it's only
    treated as an image+text pair when exactly one of the two cells is
    image-dominant and the other is clearly text-dominant -- not just
    "has a Grid with two GridCells".

    Marks the <img> with data-migration-media-text-image="<n>" and the
    text cell with data-migration-media-text-content="<n>" (DOM
    attributes, not Python-side element handles -- re-querying the same
    DOM node later, e.g. via extract_blocks()'s broader selector, would
    return a different ElementHandle object that doesn't compare equal
    to this pass's handles, so tagging the DOM itself is what makes the
    grouping visible to the later pass).
    """
    return page.evaluate(
        """() => {
            const grids = document.querySelectorAll('[data-ux="Grid"]');
            let pairCount = 0;
            for (const grid of grids) {
                const cells = Array.from(grid.children).filter(
                    c => c.matches('[data-ux="GridCell"]')
                );
                if (cells.length !== 2) continue;

                let imgCell = null, textCell = null;
                for (const cell of cells) {
                    const img = cell.querySelector('img');
                    const textLen = (cell.textContent || '').trim().length;
                    if (img && textLen < 40) imgCell = cell;
                    else if (textLen >= 40) textCell = cell;
                }
                if (!imgCell || !textCell) continue;

                const img = imgCell.querySelector('img');
                if (!img) continue;
                img.setAttribute('data-migration-media-text-image', String(pairCount));
                textCell.setAttribute('data-migration-media-text-content', String(pairCount));
                pairCount++;
            }
            return pairCount;
        }"""
    )


def mark_content_cards(page):
    """Tags GoDaddy Website Builder "ContentCard" components (confirmed via
    live markup: a <div data-ux="ContentCard"> holding a heading, an image,
    a paragraph, and a "Learn More" button) so extract_blocks() can group
    each row of them into one "card_group" block instead of extracting
    each card's heading/image/paragraph as separate, stacked blocks
    indistinguishable from ordinary page content.

    Confirmed the hard way: a 3-card row ("AI Strategy" / "AI for Sales" /
    "AI for Customer Service") was extracting as three unrelated
    heading+image+paragraph clusters, with no signal that they belonged
    side by side as cards, and silently dropping every card's CTA link
    (the query selector driving normal extraction never matches <a>).

    Also confirmed: each card's heading Block actually contains all of the
    row's headings (e.g. the "AI Strategy" card's DOM literally also
    contains hidden <h4>s for "AI for Sales" and "AI for Customer
    Service"), with only the one belonging to that card visible -- inert
    markup from whatever carousel/tab component GoDaddy builds this from.
    That's harmless here since heading extraction already goes through
    element_text() (Playwright's inner_text()), which returns "" for
    non-visible elements, so only the one real heading per card is ever
    picked up.

    Groups cards by walking each card up its ancestor chain (capped at 8
    levels to avoid over-grouping unrelated cards elsewhere on the page)
    to the closest ancestor that contains more than one ContentCard --
    that's the row wrapper, whatever GoDaddy happens to tag it with.
    Confirmed necessary against the real site: a 3-card row's cards each
    turned out to sit inside their own single-cell <div data-ux="Grid">
    (one GridCell each), not one shared Grid the way the two-column
    media_text pattern works -- grouping by nearest data-ux="Grid"
    ancestor alone split every card into its own one-card group instead
    of uniting the row. Falls back to the card's own parent if no such
    ancestor is found within the cap, so a lone card still gets a group
    of one rather than being skipped.

    Tags each card with data-migration-card-group="<n>" and
    data-migration-card-index="<i>" so extract_blocks() can pull the
    whole row together the first time it encounters any element inside
    any card belonging to that group.
    """
    return page.evaluate(
        """() => {
            const cards = document.querySelectorAll('[data-ux="ContentCard"]');
            const groupIds = new Map();
            let groupCounter = 0;
            let count = 0;
            for (const card of cards) {
                let node = card.parentElement;
                let rowAncestor = null;
                for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
                    if (node.querySelectorAll('[data-ux="ContentCard"]').length > 1) {
                        rowAncestor = node;
                        break;
                    }
                }
                const key = rowAncestor || card.parentElement;
                let gid = groupIds.get(key);
                if (gid === undefined) {
                    gid = groupCounter++;
                    groupIds.set(key, gid);
                }
                const idx = parseInt(
                    key.getAttribute('data-migration-card-next-index') || '0', 10
                );
                key.setAttribute('data-migration-card-next-index', String(idx + 1));
                card.setAttribute('data-migration-card-group', String(gid));
                card.setAttribute('data-migration-card-index', String(idx));
                count++;
            }
            return count;
        }"""
    )


def extract_content_card(card, page_url, seen_image_urls):
    """Pull one ContentCard's heading/image/text/CTA out into a dict (see
    mark_content_cards()). Any piece that's missing or fails to resolve is
    simply omitted rather than dropping the whole card."""
    card_data = {}

    for h in card.query_selector_all("h1, h2, h3, h4, h5, h6"):
        text = element_text(h)
        if text:
            card_data["heading"] = text
            role = h.get_attribute("data-typography")
            if role:
                card_data["heading_role"] = role
            break

    img_el = card.query_selector(
        '[data-ux="ContentCardWrapperImage"] img'
    ) or card.query_selector("img")
    if img_el is not None:
        resolved = resolve_image_src(img_el, page_url, seen_image_urls)
        if resolved is not None:
            abs_src, alt = resolved
            card_data["image"] = {"src": abs_src, "alt": alt}

    text_el = card.query_selector('[data-ux="ContentCardText"]')
    if text_el is not None:
        text = element_text(text_el)
        if text:
            card_data["text"] = text

    cta_el = card.query_selector('[data-ux="ContentCardButton"]')
    if cta_el is not None:
        href = cta_el.get_attribute("href")
        label = element_text(cta_el)
        if href:
            card_data["cta"] = {"href": urljoin(page_url, href), "label": label}

    return card_data


def mark_post_feeds(page):
    """Tags GoDaddy Website Builder's "RSS Feed" widget -- confirmed via
    live markup: a <div data-ux="Grid" data-aid="RSS_FEEDS_RENDERED">
    listing real blog posts (title/excerpt/date/categories/link) as
    cards, used for things like an "AI Insights" section embedding
    recent blog posts on a landing page. Was previously invisible to
    extract_blocks() entirely: its thumbnail is a CSS background-image
    on a plain <div>, not an <img>, so the normal image selector never
    matched it, and while its heading/paragraph text would otherwise
    match the generic selector, doing so lost the post's link, date, and
    categories, and interleaved unrelated cards' text with no grouping.

    One of the widget's own GridCells is just category-filter tabs (a
    <nav>, no post card) -- confirmed real, not a bug in this markup;
    only cells that actually contain a `data-ux="Card"` are tagged.

    Tags each qualifying GridCell with data-migration-post-feed-group=
    "<n>" (shared per widget, since a page could in principle have more
    than one) so extract_blocks() can pull the whole feed together the
    first time it encounters any element inside any of its cards.
    """
    return page.evaluate(
        """() => {
            const grids = document.querySelectorAll(
                '[data-ux="Grid"][data-aid="RSS_FEEDS_RENDERED"]'
            );
            let count = 0;
            let gid = 0;
            for (const grid of grids) {
                const cells = grid.querySelectorAll('[data-ux="GridCell"]');
                let tagged = false;
                for (const cell of cells) {
                    if (cell.querySelector('[data-ux="Card"]')) {
                        cell.setAttribute('data-migration-post-feed-group', String(gid));
                        tagged = true;
                        count++;
                    }
                }
                if (tagged) gid++;
            }
            return count;
        }"""
    )


def extract_post_feed_card(cell, page_url, seen_image_urls):
    """Pull one blog-post preview out of a GoDaddy RSS feed widget card
    (see mark_post_feeds()) into a dict. Any missing piece is simply
    omitted rather than dropping the whole card."""
    card_data = {}

    link_el = cell.query_selector('a[data-ux="Link"]')
    href = link_el.get_attribute("href") if link_el else None
    if href:
        card_data["href"] = urljoin(page_url, href)

    # The thumbnail is a CSS background-image on a plain <div>, not an
    # <img> -- resolve_image_src() (built around an <img>'s src/
    # data-srclazy attributes) doesn't apply here at all.
    bg_el = cell.query_selector('[data-ux="Background"]')
    if bg_el is not None:
        bg_css = bg_el.evaluate("e => getComputedStyle(e).backgroundImage")
        m = re.search(r'url\(["\']?(.*?)["\']?\)', bg_css or "")
        if m and m.group(1) and m.group(1).lower() != "none":
            abs_src = urljoin(page_url, m.group(1))
            if abs_src not in seen_image_urls:
                seen_image_urls.add(abs_src)
                card_data["image_src"] = abs_src

    date_el = cell.query_selector('[data-aid="RSS_FEED_POST_DATE_RENDERED"]')
    if date_el is not None:
        text = element_text(date_el)
        if text:
            card_data["date"] = text

    cat_el = cell.query_selector('[data-aid="RSS_FEED_POST_CATEGORIES_RENDERED"]')
    if cat_el is not None:
        text = element_text(cat_el)
        if text:
            card_data["categories"] = text

    heading_el = cell.query_selector('h4[data-ux="CardHeading"]')
    if heading_el is not None:
        text = element_text(heading_el)
        if text:
            card_data["heading"] = text

    excerpt_el = cell.query_selector('p[data-aid="RSS_FEED_POST_CONTENT_RENDERED"]')
    if excerpt_el is not None:
        text = element_text(excerpt_el)
        if text:
            card_data["excerpt"] = text

    return card_data


def resolve_image_src(el, page_url, seen_image_urls):
    """Shared by the normal per-image extraction and the media_text
    pair extraction below, so both apply the exact same lazy-load/
    size-filter/dedup rules. Returns (abs_src, alt) or None if this
    image shouldn't be extracted at all."""
    # GoDaddy Website Builder lazy-loads below-the-fold images: src
    # holds a 1x1 transparent GIF placeholder until the image actually
    # scrolls into view (which headless crawling never triggers), and
    # the real URL sits in data-srclazy. naturalWidth/Height can't be
    # used to size-filter these -- the placeholder is the only thing
    # ever loaded into the element, so it always reads as 1x1
    # regardless of what the real image is.
    lazy_src = el.get_attribute("data-srclazy")
    if lazy_src:
        src = lazy_src
    else:
        src = el.get_attribute("src")
        if not src:
            return None
        dims = el.evaluate("e => ({w: e.naturalWidth, h: e.naturalHeight})")
        if dims["w"] < MIN_CONTENT_IMAGE_SIZE or dims["h"] < MIN_CONTENT_IMAGE_SIZE:
            return None  # likely a tracking pixel or decorative icon

    abs_src = urljoin(page_url, src)
    if abs_src in seen_image_urls:
        return None  # e.g. duplicate desktop/mobile logo markup
    seen_image_urls.add(abs_src)
    return abs_src, (el.get_attribute("alt") or "")


def extract_element_content(container):
    """Headings/paragraphs/lists inside one element, in document order --
    the same block types and rules extract_blocks() applies at the page
    level, scoped to a single container. Used to pull the text side of a
    detected media_text pair (see mark_media_text_pairs()) into that
    block's own "content" list."""
    content = []
    for el in container.query_selector_all("h1, h2, h3, h4, h5, h6, p, ul, ol"):
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        text = element_text(el)
        if not text:
            continue
        if tag.startswith("h"):
            heading = {"type": "heading", "level": int(tag[1]), "text": text}
            role = el.get_attribute("data-typography")
            if role:
                heading["typography_role"] = role
            content.append(heading)
        elif tag == "p":
            content.append({"type": "paragraph", "text": text})
        elif tag in ("ul", "ol"):
            items = [
                element_text(li)
                for li in el.query_selector_all("li")
                if element_text(li)
            ]
            if items:
                content.append({"type": "list", "items": items})
    return content


def extract_blocks(page, page_url):
    """Turn a rendered page's DOM into structured content blocks."""
    blocks = []

    mark_media_text_pairs(page)
    mark_content_cards(page)
    mark_post_feeds(page)

    # Headings + paragraphs + lists + images, in document order. Images
    # used to be collected in a separate pass at the end of the function
    # and dumped into one page-level block, disconnected from where they
    # actually appeared -- every page ended up with all its images
    # bunched at the bottom regardless of layout. Including "img" in the
    # same document-order query keeps each image roughly where it
    # belongs in the content.
    seen_image_urls = set()
    emitted_card_groups = set()
    emitted_post_feed_groups = set()
    elements = page.query_selector_all("h1, h2, h3, h4, h5, h6, p, ul, ol, img")
    for el in elements:
        if el.evaluate("(e, sel) => !!e.closest(sel)", CHROME_SELECTOR):
            continue  # inside the header, footer, or a nav -- not page content

        # Anything inside a detected ContentCard is pulled in as part of
        # that card's group (below, the first time we hit any element
        # belonging to any card in the group) rather than extracted again
        # here as a separate, stacked heading/image/paragraph.
        card_group_id = el.evaluate(
            """e => {
                const card = e.closest('[data-migration-card-group]');
                return card ? card.getAttribute('data-migration-card-group') : null;
            }"""
        )
        if card_group_id is not None:
            if card_group_id not in emitted_card_groups:
                emitted_card_groups.add(card_group_id)
                cards = page.query_selector_all(
                    f'[data-migration-card-group="{card_group_id}"]'
                )
                card_dicts = [
                    extract_content_card(c, page_url, seen_image_urls)
                    for c in cards
                ]
                card_dicts = [c for c in card_dicts if c]
                if card_dicts:
                    blocks.append({"type": "card_group", "cards": card_dicts})
            continue

        # Same idea for a detected RSS feed widget (e.g. an "AI Insights"
        # recent-posts section) -- pull the whole feed together the
        # first time we hit any element inside any of its post cards.
        post_feed_group_id = el.evaluate(
            """e => {
                const cell = e.closest('[data-migration-post-feed-group]');
                return cell ? cell.getAttribute('data-migration-post-feed-group') : null;
            }"""
        )
        if post_feed_group_id is not None:
            if post_feed_group_id not in emitted_post_feed_groups:
                emitted_post_feed_groups.add(post_feed_group_id)
                cells = page.query_selector_all(
                    f'[data-migration-post-feed-group="{post_feed_group_id}"]'
                )
                posts = [
                    extract_post_feed_card(c, page_url, seen_image_urls)
                    for c in cells
                ]
                posts = [p for p in posts if p]
                if posts:
                    blocks.append({"type": "post_feed", "posts": posts})
            continue

        # Anything inside a detected side-by-side text column is pulled
        # in as part of that media_text block (below, when we hit its
        # paired image) rather than extracted again here as a separate,
        # stacked block.
        if el.evaluate("e => !!e.closest('[data-migration-media-text-content]')"):
            continue

        tag = el.evaluate("e => e.tagName.toLowerCase()")

        if tag == "img":
            pair_id = el.get_attribute("data-migration-media-text-image")
            if pair_id is not None:
                # Resolve the text side first and independently of the
                # image. Confirmed as a real content-loss bug on a live
                # crawl: an earlier version bailed out of this whole
                # branch the moment resolve_image_src() returned None
                # (e.g. this image has no data-srclazy fallback and its
                # bare src failed to load/size-check), silently dropping
                # its paired heading and paragraphs along with it -- text
                # that a plain, ungrouped image+paragraph pair elsewhere
                # on the same page would never have lost. Whatever
                # happens to the image, the real text content the pair
                # was tagged with is never thrown away.
                text_cell = page.query_selector(
                    f'[data-migration-media-text-content="{pair_id}"]'
                )
                content = extract_element_content(text_cell) if text_cell else []
                resolved = resolve_image_src(el, page_url, seen_image_urls)

                if resolved is not None and content:
                    abs_src, alt = resolved
                    blocks.append({
                        "type": "media_text",
                        "src": abs_src,
                        "alt": alt,
                        "content": content,
                    })
                elif resolved is not None:
                    # Text side had nothing extractable after all --
                    # fall back to a plain image rather than losing it.
                    abs_src, alt = resolved
                    blocks.append({"type": "image", "src": abs_src, "alt": alt})
                else:
                    # Image didn't resolve -- keep the real text content
                    # as normal top-level blocks instead of losing it too.
                    blocks.extend(content)
                continue

            resolved = resolve_image_src(el, page_url, seen_image_urls)
            if resolved is None:
                continue
            abs_src, alt = resolved
            blocks.append({"type": "image", "src": abs_src, "alt": alt})
            continue

        text = element_text(el)
        if not text:
            continue

        if tag.startswith("h"):
            heading = {"type": "heading", "level": int(tag[1]), "text": text}
            role = el.get_attribute("data-typography")
            if role:
                heading["typography_role"] = role
            blocks.append(heading)
        elif tag == "p":
            blocks.append({"type": "paragraph", "text": text})
        elif tag in ("ul", "ol"):
            items = [
                element_text(li)
                for li in el.query_selector_all("li")
                if element_text(li)
            ]
            if items:
                blocks.append({"type": "list", "items": items})

    # Best-effort FAQ / accordion detection -- flagged low-confidence
    # since accordion markup varies a lot site to site.
    faq_candidates = page.query_selector_all(
        "[class*='faq'], [class*='accordion'], details"
    )
    faq_items = []
    for el in faq_candidates:
        text = element_text(el)
        if "?" in text and len(text) < 2000:
            faq_items.append(text)
    if faq_items:
        blocks.append({
            "type": "faq_raw_unverified",
            "note": "Low-confidence FAQ extraction -- review before publishing.",
            "raw_text_blocks": faq_items,
        })

    # Forms -- capture field names/types so the Generator Agent can flag
    # the right plugin (contact form vs newsletter signup) for review.
    forms = []
    for form in page.query_selector_all("form"):
        fields = []
        for inp in form.query_selector_all("input, textarea, select"):
            fields.append({
                "name": inp.get_attribute("name") or "",
                "type": inp.get_attribute("type") or inp.evaluate("e => e.tagName.toLowerCase()"),
            })
        forms.append(fields)
    if forms:
        blocks.append({"type": "forms_detected", "forms": forms})

    return blocks


def discover_links(page, base_url):
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    links = set()
    for href in hrefs:
        if not href or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        full = urljoin(base_url, href)
        # Strip only the fragment, not the query string. Query-string
        # variants of a listing page (e.g. /blog?blogcategory=X) render
        # different post links than the bare page -- an earlier version
        # stripped them here to avoid re-extracting the same content
        # under multiple URLs, but that also meant those variants were
        # never *visited*, so posts only linked from a filtered view were
        # never discovered at all. Content-level dedup is handled in
        # crawl() instead, keyed on the query-stripped path, so we still
        # visit every variant for link discovery without saving duplicate
        # pages.
        full = full.split("#")[0].rstrip("/")
        if same_domain(base_url, full):
            links.add(full)
    return links


# ---- Qualification Agent hook -------------------------------------------
# Structural/DOM checks to flag pages that fall outside the
# informational-site scope (payments, logins, forums). This is NOT a
# substitute for the real Qualification Agent -- it's a first-pass filter
# so the crawler can flag risk early rather than silently processing
# something out of scope.
#
# Earlier this matched keywords anywhere in the page's raw HTML text
# ("password", "login", "community", "cart", ...). On a cybersecurity
# advisory site, ordinary blog prose about password hygiene or online
# communities constantly tripped that -- silently dropping real,
# in-scope content instead of flagging actual functionality. These checks
# look for concrete signals (form fields, URL path, known SDK scripts)
# instead of topic vocabulary.
URL_PATH_RISK_PATTERN = re.compile(
    r"/(login|sign-?in|my-?account|register|cart|checkout|forum)(/|$)", re.I
)


def flag_risks(page, url):
    flags = []

    path = urlparse(url).path
    if URL_PATH_RISK_PATTERN.search(path):
        flags.append(f"URL path suggests account/cart/forum area: {path}")

    if page.query_selector("input[type='password']"):
        flags.append("possible login/account area (password field present)")

    if page.query_selector(
        "[class*='add-to-cart' i], [class*='shopping-cart' i], "
        "a[href*='/cart'], a[href*='/checkout'], "
        "script[src*='stripe.com'], script[src*='paypal.com']"
    ):
        flags.append("possible ecommerce/payment (cart/checkout element or payment SDK present)")

    if page.query_selector("[class*='phpbb' i], [class*='discourse-forum' i], a[href*='/forum']"):
        flags.append("possible forum/community feature (forum software marker present)")

    return flags


def extract_navigation(page):
    """The site's real top-level navigation, as a nested tree:
    [{"label": ..., "href": ..., "children": [{"label": ..., "href": ...}, ...]}, ...]

    GoDaddy Website Builder's header nav (found by inspecting the live
    site's markup) is a <nav data-aid="HEADER_NAV_RENDERED"> containing
    a flat <ul> of top-level <li data-ux="NavListItemInline"> items.
    A plain link (e.g. "Home") has a single <a> with a real href. A
    category with a dropdown (e.g. "AI") has an <a data-ux=
    "NavLinkDropdown" href="#"> -- not a real destination -- followed
    by a sibling <ul data-ux="Dropdown"> of <li data-ux="ListItem">
    children, each a real link.

    The nav also renders a second copy of the same top-level items
    under a "More" overflow dropdown (data-aid="NAV_MORE") for
    responsive collapse -- confirmed by inspecting the live page, this
    duplicates the visible items rather than containing anything
    unique, so it's skipped entirely rather than needing to be merged
    or deduped against the real one.
    """
    return page.evaluate(
        """() => {
            const nav = document.querySelector('nav[data-aid="HEADER_NAV_RENDERED"]');
            if (!nav) return [];
            const topUl = nav.querySelector('ul[data-ux="List"]');
            if (!topUl) return [];

            const topItems = Array.from(topUl.children).filter(
                el => el.matches('li[data-ux="NavListItemInline"]')
            );
            const result = [];
            for (const li of topItems) {
                const firstA = li.querySelector('a');
                if (!firstA || firstA.dataset.aid === 'NAV_MORE') continue;

                const href = firstA.getAttribute('href');
                const item = {
                    label: firstA.textContent.trim(),
                    href: (href && href !== '#') ? href : null,
                };

                const dropdown = li.querySelector('ul[data-ux="Dropdown"]');
                if (dropdown) {
                    item.children = Array.from(
                        dropdown.querySelectorAll('li[data-ux="ListItem"] a')
                    ).map(a => ({
                        label: a.textContent.trim(),
                        href: a.getAttribute('href'),
                    }));
                }
                result.push(item);
            }
            return result;
        }"""
    )


def extract_footer(page):
    """The site's real footer content, as rendered -- not the target
    theme's own placeholder footer, which is what a WXR import without
    this leaves in place. GoDaddy Website Builder's footer widget
    (found by inspecting the live site's markup) is the element bearing
    role="contentinfo", containing: a flat <ul data-ux="NavFooter"> of
    top-level page links (no dropdown/category nesting the way the
    header nav has), a data-aid="FOOTER_SOCIAL_LINKS" block of social
    icon links (each with an aria-label like "Facebook Social Link"),
    and a data-aid="FOOTER_COPYRIGHT_RENDERED" paragraph containing the
    copyright line plus inline Privacy Policy / Terms of Service links.

    Returns {"links": [...], "social_links": [...], "legal_links": [...],
    "copyright_text": "..."} -- any piece that isn't found on this site
    is simply omitted/empty rather than guessed at.
    """
    return page.evaluate(
        """() => {
            const footer = document.querySelector('[role="contentinfo"]');
            if (!footer) return null;

            const links = Array.from(
                footer.querySelectorAll('ul[data-ux="NavFooter"] a')
            ).map(a => ({
                label: a.textContent.trim(),
                href: a.getAttribute('href'),
            })).filter(l => l.label && l.href);

            const socialBlock = footer.querySelector('[data-aid="FOOTER_SOCIAL_LINKS"]');
            const social_links = socialBlock
                ? Array.from(socialBlock.querySelectorAll('a[href]')).map(a => {
                    const label = (a.getAttribute('aria-label') || '').replace(/\\s*Social Link$/i, '').trim();
                    return {
                        platform: label.toLowerCase(),
                        label: label,
                        href: a.getAttribute('href'),
                    };
                })
                : [];

            const copyrightBlock = footer.querySelector('[data-aid="FOOTER_COPYRIGHT_RENDERED"]');
            let copyright_text = '';
            let legal_links = [];
            if (copyrightBlock) {
                legal_links = Array.from(copyrightBlock.querySelectorAll('a[href]')).map(a => ({
                    label: a.textContent.trim(),
                    href: a.getAttribute('href'),
                }));
                // The copyright line and the trailing "| Privacy Policy |
                // Terms of Service" links share one text node -- clone the
                // block and strip the <a> tags to isolate just the prose.
                const clone = copyrightBlock.cloneNode(true);
                clone.querySelectorAll('a').forEach(a => a.remove());
                copyright_text = clone.textContent.replace(/\\|\\s*$/, '').trim();
            }

            return { links, social_links, legal_links, copyright_text };
        }"""
    )


def crawl(start_url, max_pages=100):
    visited = set()
    to_visit = {start_url.rstrip("/")}
    pages = []
    risk_flags = {}
    extracted_paths = set()
    navigation = []
    footer = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        page = ctx.new_page()

        while to_visit and len(visited) < max_pages:
            url = to_visit.pop()
            if url in visited:
                continue
            visited.add(url)

            try:
                # "networkidle" waits for zero in-flight requests for 500ms,
                # which many real sites (analytics beacons, chat widgets,
                # font loading) never reach -- causing false-negative
                # timeouts even though the page rendered fine. "load" plus
                # a short settle delay is more reliable in practice.
                page.goto(url, wait_until="load", timeout=30000)
                page.wait_for_timeout(1000)
                # Some GoDaddy Website Builder widgets (confirmed for the
                # "RSS Feed" widget, see mark_post_feeds()) lazy-mount
                # their real content only once scrolled into view --
                # never triggered by a plain page.goto(), which leaves
                # the initial viewport rendered and nothing below it.
                # Confirmed as the actual cause of a real gap: the same
                # widget rendered its posts fine on a page where it sits
                # near the top, but came up completely empty on a longer
                # page where it sits well below the fold. Scrolling
                # through the whole page in steps (not straight to the
                # bottom, which can skip past an IntersectionObserver's
                # trigger point for content still off-screen mid-jump)
                # gives every section a chance to mount before extraction.
                page.evaluate(
                    """async () => {
                        const step = window.innerHeight * 0.8;
                        let y = 0;
                        const height = () => document.body.scrollHeight;
                        while (y < height()) {
                            y += step;
                            window.scrollTo(0, y);
                            await new Promise(r => setTimeout(r, 250));
                        }
                        window.scrollTo(0, 0);
                    }"""
                )
                page.wait_for_timeout(500)
            except Exception as e:
                print(f"  [skip] {url} -- {e}")
                continue

            if not navigation and url.split("?")[0].rstrip("/") == start_url.rstrip("/"):
                try:
                    navigation = extract_navigation(page)
                except Exception as e:
                    print(f"  [warn] nav extraction failed: {e}")
                try:
                    footer = extract_footer(page) or {}
                except Exception as e:
                    print(f"  [warn] footer extraction failed: {e}")

            # Query-string variants of the same page (e.g. blog category
            # filters) are still visited -- below, discover_links() reads
            # their hrefs, since some linked posts only surface on a
            # filtered view -- but content is only extracted and saved
            # once per canonical (query-stripped) path, so we don't end
            # up with duplicate "pages" for the same content.
            canonical_path = urlparse(url).path.rstrip("/") or "/"
            if canonical_path not in extracted_paths:
                risks = flag_risks(page, url)
                if risks:
                    risk_flags[url] = risks
                    print(f"  [FLAGGED] {url} -- {risks} (skipping content extraction)")
                else:
                    try:
                        blocks = extract_blocks(page, url)
                    except Exception as e:
                        print(f"  [skip] {url} -- extraction failed: {e}")
                        blocks = None

                    if blocks is not None:
                        title = page.title()
                        meta_desc_el = page.query_selector("meta[name='description']")
                        meta_desc = meta_desc_el.get_attribute("content") if meta_desc_el else ""
                        # og:image is a far more reliable "this page's real
                        # featured image" signal than trying to guess one
                        # from the DOM -- confirmed useful for post_feed
                        # cards (see mark_post_feeds()), whose own
                        # thumbnail is loaded via client-side JS the
                        # crawler never sees, but which links to a post
                        # page like this one that always carries its own
                        # correct og:image regardless.
                        og_image_el = page.query_selector('meta[property="og:image"]')
                        og_image = og_image_el.get_attribute("content") if og_image_el else ""
                        extracted_paths.add(canonical_path)
                        # Record the canonical, query-stripped URL, not
                        # whichever query-string variant happened to be
                        # the first one visited -- that variant is an
                        # implementation detail of how this page's links
                        # were discovered, not the URL real backlinks or
                        # bookmarks would use for redirects.
                        canonical_url = url.split("?")[0].rstrip("/")
                        pages.append({
                            "old_url": canonical_url,
                            "slug": slugify(canonical_url, start_url),
                            "title": title,
                            "meta_description": meta_desc or "",
                            "featured_image": og_image or "",
                            "type": "page",
                            "is_front_page": (canonical_url == start_url.rstrip("/")),
                            "blocks": blocks,
                        })
                        print(f"  [ok] {url} -- {len(blocks)} blocks")

            new_links = discover_links(page, start_url)
            to_visit |= (new_links - visited)

        browser.close()

    return {
        "site": {
            "title": pages[0]["title"] if pages else "",
            "tagline": "",
            "theme_color": "",
            "old_domain": urlparse(start_url).netloc,
        },
        "pages": pages,
        "navigation": navigation,
        "footer": footer,
        "qualification_flags": risk_flags,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 crawler_agent.py https://example.com")
        sys.exit(1)

    start_url = sys.argv[1]
    print(f"Crawling {start_url} ...")
    result = crawl(start_url)

    with open("structured_content.json", "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nDone. {len(result['pages'])} pages extracted, "
          f"{len(result['qualification_flags'])} pages flagged for review.")
    print("Wrote structured_content.json -- feed this into generator_agent.py")


if __name__ == "__main__":
    main()
