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
import html
from urllib.parse import urlparse, urljoin

from playwright.sync_api import sync_playwright

# The out-of-scope safety gate (payments, accounts, forums, bookings,
# donations) is pipeline step 3, kept in its own module so it can be
# re-run and reasoned about independently. crawl() calls scan_page()
# while it holds the live DOM.
from qualification_agent import scan_page


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


# GoDaddy Website Builder serves a blog post at `/<section>/f/<slug>`,
# where `<section>` is *any* page that links to it -- the blog index
# (/blog/f/<slug>) and every landing page whose "Insights" feed lists it
# (/ai-solutions/f/<slug>, /cybersecurity-solutions/f/<slug>, ...). The
# content is identical under every prefix (the `/f/` route is a dedicated
# post view; the section is cosmetic). A post's identity is therefore its
# slug alone. Without collapsing these, every re-crawl saves the same
# post 2-3 times with the same slug and different old_urls, and someone
# has to merge them back down by hand before regenerating.
BLOG_FEED_ITEM_RE = re.compile(r"^/[^/]+/f/([^/]+)/?$")


def blog_feed_slug(path):
    """The <slug> of a GoDaddy blog-feed-item path (`/<section>/f/<slug>`),
    or None if `path` isn't one."""
    m = BLOG_FEED_ITEM_RE.match(path)
    return m.group(1) if m else None


def canonical_feed_item_url(url):
    """Rewrite any `/<section>/f/<slug>` URL to the canonical
    `/blog/f/<slug>` (the form redirects.csv and real bookmarks use);
    returns `url` unchanged if it isn't a feed-item URL."""
    parts = urlparse(url)
    slug = blog_feed_slug(parts.path)
    if not slug:
        return url
    return f"{parts.scheme}://{parts.netloc}/blog/f/{slug}"


# Third-party form / survey / questionnaire embeds. GoDaddy Website
# Builder's "Embed" / "Custom HTML" widget lets an editor drop an
# <iframe> or loader <script> from an external form builder straight into
# a page -- the live stratecon.tech /cyber-risk-assessment page does
# exactly this with a 20-question Cognito Forms questionnaire, and that
# was the whole point of the page. These embeds are cross-origin, so
# their fields are invisible to the crawler; all we can capture is that
# one exists, which provider serves it, and its URL. The generator turns
# each into a labelled placeholder carrying a QA flag that tells the
# operator to rebuild the form in Fluent Forms (or re-embed it via the
# provider's own WordPress block) before go-live -- the generic form of
# the manual cyber-risk-assessment rebuild.
#
# Matched as case-folded substrings, so a bare registrable domain covers
# its subdomains and paths. GoDaddy's Custom-HTML widget renders the
# pasted embed code inside an <iframe srcdoc="..."> (a nested browsing
# context), so the needle has to be searched in the srcdoc HTML string
# and the page's child frames too, not just top-level <iframe src> /
# <script src>. The live cyber-risk-assessment page is exactly this
# shape: <iframe srcdoc='... <script src="https://www.cognitoforms.com/
# f/seamless.js" data-key="..." data-form="1"></script> ...'>.
EMBED_FORM_PROVIDERS = (
    ("cognitoforms.com", "Cognito Forms"),
    ("jotform.com", "JotForm"),
    ("jotform.co", "JotForm"),
    ("form.jotform.com", "JotForm"),
    ("typeform.com", "Typeform"),
    ("docs.google.com/forms", "Google Forms"),
    ("forms.gle", "Google Forms"),
    ("forms.office.com", "Microsoft Forms"),
    ("wufoo.com", "Wufoo"),
    ("formstack.com", "Formstack"),
    ("js.hsforms.net", "HubSpot Forms"),
    ("hsforms.com", "HubSpot Forms"),
    ("hsforms.net", "HubSpot Forms"),
    ("airtable.com/embed", "Airtable Form"),
    ("paperform.co", "Paperform"),
    ("tally.so", "Tally"),
    ("zohopublic.com/forms", "Zoho Forms"),
    ("forms.zohopublic", "Zoho Forms"),
    ("surveymonkey.com", "SurveyMonkey"),
    ("123formbuilder.com", "123FormBuilder"),
    ("formsite.com", "Formsite"),
    ("gravityforms.com", "Gravity Forms"),
    ("involve.me", "involve.me"),
    ("feathery.io", "Feathery"),
    ("fillout.com", "Fillout"),
    ("getform.io", "Getform"),
)


_EMBED_SCAN_JS = r"""(args) => {
    const providers = args.providers;
    const rootIsFrame = args.rootIsFrame;
    const CH = 'nav,[data-ux="Header"],[role="contentinfo"],[data-aid="FOOTER_COOKIE_BANNER_RENDERED"]';
    const match = (u) => {
        if (!u) return null;
        const low = ('' + u).toLowerCase();
        for (const nn of providers) {
            if (low.indexOf(nn[0]) !== -1) return nn[1];
        }
        return null;
    };
    // Pull the first provider-matching URL out of a blob of embed HTML
    // (a srcdoc string, an innerHTML), so a QA note can point somewhere.
    const urlFromHtml = (html, name) => {
        const re = /(?:src|href|data-[a-z-]+)\s*=\s*["']([^"']+)["']/gi;
        let m;
        while ((m = re.exec(html)) !== null) {
            if (match(m[1]) === name) return m[1];
        }
        return '';
    };
    const hits = [];
    const push = (name, src, title) => { if (name) hits.push({ provider: name, src: src || '', title: (title || '').trim() }); };

    for (const f of document.querySelectorAll('iframe')) {
        if (!rootIsFrame && f.closest(CH)) continue;
        const src = f.getAttribute('src') || f.getAttribute('data-src') || '';
        let name = match(src);
        if (name) { push(name, src, f.getAttribute('title')); continue; }
        // GoDaddy's Custom-HTML widget: the real embed is in srcdoc.
        const sd = f.getAttribute('srcdoc') || '';
        if (sd) {
            name = match(sd);
            if (name) push(name, urlFromHtml(sd, name), f.getAttribute('title'));
        }
    }
    for (const s of document.querySelectorAll('script[src]')) {
        if (!rootIsFrame && s.closest(CH)) continue;
        const name = match(s.getAttribute('src'));
        if (name) push(name, s.getAttribute('src'), '');
    }
    // Some builders inject the form from a placeholder element + their
    // loader script rather than a bare <iframe>.
    const DIVS = 'div[class*="cognito"], [data-paperform-id], [data-tally-src], [data-tf-widget], [data-tf-live], [data-hs-forms-root], [data-region][data-form-id], .jotform-form';
    for (const d of document.querySelectorAll(DIVS)) {
        if (!rootIsFrame && d.closest(CH)) continue;
        const src = d.getAttribute('data-tally-src') || d.getAttribute('data-tf-widget') || d.getAttribute('data-tf-live') || d.getAttribute('data-paperform-id') || '';
        let name = match(src) || match(d.getAttribute('src'));
        if (!name) {
            const cls = ('' + (d.className || '')).toLowerCase();
            if (cls.indexOf('cognito') !== -1) name = 'Cognito Forms';
            else if (d.hasAttribute('data-paperform-id')) name = 'Paperform';
            else if (d.hasAttribute('data-tally-src')) name = 'Tally';
            else if (d.hasAttribute('data-tf-widget') || d.hasAttribute('data-tf-live')) name = 'Typeform';
            else if (d.hasAttribute('data-hs-forms-root')) name = 'HubSpot Forms';
            else if (cls.indexOf('jotform') !== -1) name = 'JotForm';
        }
        push(name, src, '');
    }
    return hits;
}"""


def detect_embedded_forms(page):
    """Find third-party form / survey embeds (<iframe>, loader <script>,
    a builder's placeholder <div>, or an embed pasted into GoDaddy's
    Custom-HTML widget, which wraps it in an <iframe srcdoc>) from an
    external form builder -- see EMBED_FORM_PROVIDERS. Site chrome is
    excluded. Returns a list of {"provider", "src", "title"} dicts,
    collapsed to one per provider."""
    args = {"providers": [list(p) for p in EMBED_FORM_PROVIDERS], "rootIsFrame": False}
    raw = list(page.evaluate(_EMBED_SCAN_JS, args))
    # Also scan every child browsing context (GoDaddy's Custom-HTML
    # widget renders into an about:srcdoc frame; some builders nest a
    # real provider iframe). Cross-origin frames throw on DOM access --
    # skip those, the srcdoc-attribute pass above already covered them.
    frame_args = {"providers": args["providers"], "rootIsFrame": True}
    for fr in page.frames:
        if fr is page.main_frame:
            continue
        try:
            raw.extend(fr.evaluate(_EMBED_SCAN_JS, frame_args))
        except Exception:
            continue
    # Collapse to one hit per provider. A builder's embed routinely shows
    # up two or three times on one page -- its loader <script>, a
    # placeholder <div>, and the <iframe> it injects all match -- and a
    # single page embedding two *different* forms from the same builder
    # is vanishingly rare. Keep the most informative row: prefer a real
    # URL over a bare widget id, and a titled row over an untitled one.
    def _score(h):
        src = h.get("src") or ""
        first = h["provider"].split()[0].lower()
        return (
            1 if ("/" in src and first in src.lower()) else 0,
            1 if "/" in src else 0,
            1 if src else 0,
            1 if h.get("title") else 0,
        )

    best = {}
    for h in raw:
        h = {
            "provider": h["provider"],
            "src": (h.get("src") or "").strip(),
            "title": (h.get("title") or "").strip(),
        }
        cur = best.get(h["provider"])
        if cur is None or _score(h) > _score(cur):
            best[h["provider"]] = h
    return [best[k] for k in sorted(best)]


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


def element_inline_html(el, base_url):
    """An element's content as a small, safe HTML fragment -- preserves
    bold (<strong>/<b>), italic (<em>/<i>), super/subscript (<sup>/<sub>),
    and links (<a href>) instead of flattening everything to plain text
    like element_text() does. GoDaddy Website Builder content commonly
    leans on exactly this kind of inline formatting to carry meaning the
    plain text alone loses -- e.g. a bolded stat plus a "source" citation
    link inside one list item. Everything else (spans, classes, inline
    styles -- GoDaddy's own generated wrapper markup) is unwrapped down
    to its own inner content rather than kept, since none of it is
    meaningful here.

    A trailing citation link -- an <a> whose visible text is just the
    word "source" -- is wrapped in <sup> so it reads as a superscript
    reference instead of a stray word dangling off the end of the
    sentence ("...in the previous year source"). The live site renders
    these at body size inline; superscript is the small, safe
    presentation fix a human doing this migration would make.

    Falls back to plain text (via element_text()) if this fails for any
    reason -- some formatting lost beats losing the block entirely."""
    try:
        raw = el.evaluate(
            """(el, baseUrl) => {
                function esc(s) {
                    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                }
                function walk(node) {
                    if (node.nodeType === Node.TEXT_NODE) {
                        return esc(node.textContent);
                    }
                    if (node.nodeType !== Node.ELEMENT_NODE) {
                        return '';
                    }
                    const tag = node.tagName.toLowerCase();
                    const inner = Array.from(node.childNodes).map(walk).join('');
                    if (tag === 'strong' || tag === 'b') {
                        return `<strong>${inner}</strong>`;
                    }
                    if (tag === 'em' || tag === 'i') {
                        return `<em>${inner}</em>`;
                    }
                    if (tag === 'sup' || tag === 'sub') {
                        return `<${tag}>${inner}</${tag}>`;
                    }
                    if (tag === 'a') {
                        const href = node.getAttribute('href');
                        if (!href) return inner;
                        let abs;
                        try { abs = new URL(href, baseUrl).href; } catch (e) { abs = href; }
                        const link = `<a href="${esc(abs)}">${inner}</a>`;
                        return /^\\s*source\\s*$/i.test(node.textContent) ? `<sup>${link}</sup>` : link;
                    }
                    if (tag === 'br') {
                        return ' ';
                    }
                    return inner;
                }
                return Array.from(el.childNodes).map(walk).join('');
            }""",
            base_url,
        )
    except Exception:
        return html.escape(element_text(el))
    # Collapse whitespace the same way element_text() does, without
    # touching the tags themselves (none of the tags above ever contain
    # a run of literal whitespace).
    return " ".join((raw or "").split())


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

                // GoDaddy alternates which side the image sits on with
                // flex-direction:row-reverse on the Grid -- confirmed via
                // the live site's own markup on /services and /about,
                // where every image is the *first* DOM child regardless
                // of which side it renders on. DOM order is therefore
                // useless for detecting the side; compare rendered
                // horizontal position instead. Also capture the image
                // cell's share of the row's width -- most pairs split
                // 50/50, but a section like About's founder photo uses a
                // narrower image column (confirmed ~33/67), and stretching
                // it to a plain 50/50 WordPress Media & Text block makes
                // the photo look oversized next to the live page.
                const ir = imgCell.getBoundingClientRect();
                const tr = textCell.getBoundingClientRect();
                const side = ir.left < tr.left ? 'left' : 'right';
                const totalW = ir.width + tr.width;
                const widthPct = totalW > 0 ? Math.round((ir.width / totalW) * 100) : 50;
                img.setAttribute('data-migration-media-text-image', String(pairCount));
                img.setAttribute('data-migration-media-text-side', side);
                img.setAttribute('data-migration-media-text-width', String(widthPct));
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


def settle_lazy_widgets(page):
    """GoDaddy's "RSS Feed" widget mounts its post cards only when it
    scrolls into view, via an IntersectionObserver -- and on a long page
    crawl()'s single step-scroll pass isn't always enough settle time.
    Confirmed on cybersecurity-solutions: its "Cybersecurity Insights"
    feed (10 post cards) sits ~3000px down and came back completely empty
    (no post_feed block at all), so the page lost a whole section and the
    generator rendered nothing there. Scroll each feed grid into view and
    wait for its cards to render before mark_post_feeds()/extract_blocks()
    read the DOM.
    """
    grids = page.query_selector_all('[data-aid="RSS_FEEDS_RENDERED"]')
    if not grids:
        # The RSS_FEEDS_RENDERED container itself can be lazy -- a slower,
        # finer scroll pass gives it a chance to appear before we give up.
        try:
            page.evaluate(
                """async () => {
                    const step = window.innerHeight * 0.5;
                    for (let y = 0; y < document.body.scrollHeight; y += step) {
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 300));
                    }
                }"""
            )
            page.wait_for_timeout(500)
        except Exception:
            pass
        grids = page.query_selector_all('[data-aid="RSS_FEEDS_RENDERED"]')
    for grid in grids:
        try:
            grid.scroll_into_view_if_needed(timeout=3000)
            grid.wait_for_selector('[data-ux="Card"]', timeout=5000)
        except Exception:
            pass
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    page.wait_for_timeout(300)


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
            role = heading_el.get_attribute("data-typography")
            if role:
                card_data["heading_role"] = role

    excerpt_el = cell.query_selector('p[data-aid="RSS_FEED_POST_CONTENT_RENDERED"]')
    if excerpt_el is not None:
        text = element_text(excerpt_el)
        if text:
            card_data["excerpt"] = text

    return card_data


def mark_pdf_widgets(page):
    """Tag GoDaddy's "PDF" widget (class widget-pdf) so extract_blocks()
    pulls it as one document_embed block instead of scattering its
    title/heading/description across the page and leaking its pdf.js
    viewer chrome -- the page-counter ("1/5"), "Next"/"Previous"
    buttons -- into the content as stray paragraphs. Confirmed on
    /ai-use-policy-template and /ai-disclosure-template, whose entire
    body is one of these widgets wrapping a multi-page PDF."""
    return page.evaluate(
        """() => {
            let n = 0;
            for (const w of document.querySelectorAll('[class*="widget-pdf"]')) {
                w.setAttribute('data-migration-pdf-group', String(n++));
            }
            return n;
        }"""
    )


def extract_pdf_widget(widget, page_url):
    """One GoDaddy PDF widget -> a document_embed dict: its title,
    heading, description, and the real PDF URL from the "Download PDF"
    link (data-aid="PDF_DOWNLOAD_LINK_RENDERED"). The pdf.js <canvas>
    preview and the page-counter / Next-Previous nav are deliberately
    left out -- they're viewer UI, not content."""
    def txt(sel):
        el = widget.query_selector(sel)
        return element_text(el) if el else ""

    data = {"type": "document_embed"}
    title = txt('[data-aid="PDF_SECTION_TITLE_RENDERED"]')
    if title:
        data["title"] = title
    heading = txt('[data-aid="PDF_HEADING_RENDERED"]')
    if heading:
        data["heading"] = heading
    desc = txt('[data-aid="PDF_DESCRIPTION_RENDERED"]')
    if desc:
        data["description"] = desc

    link = widget.query_selector('a[data-aid="PDF_DOWNLOAD_LINK_RENDERED"]') \
        or widget.query_selector('a[href*=".pdf"], a[href*="/blobby/"]')
    href = link.get_attribute("href") if link else None
    if href:
        data["url"] = urljoin(page_url, href)
        data["filename"] = urlparse(data["url"]).path.rsplit("/", 1)[-1] or "document.pdf"
    return data if (data.get("url") or data.get("title")) else None


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


def extract_element_content(container, page_url):
    """Headings/paragraphs/lists inside one element, in document order --
    the same block types and rules extract_blocks() applies at the page
    level, scoped to a single container. Used to pull the text side of a
    detected media_text pair (see mark_media_text_pairs()) into that
    block's own "content" list."""
    content = []
    for el in container.query_selector_all(
        "h1, h2, h3, h4, h5, h6, p, ul, ol, "
        "a[data-ux-btn], a[data-ux='ButtonSecondary'], a[data-ux='ButtonPrimary']"
    ):
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        if tag == "a":
            # GoDaddy "Button" widget inside a content section (data-ux-btn
            # "secondary"/"primary") -- the pill CTA that closes almost
            # every media_text block on the solution pages ("Get a Quote",
            # "Let's Talk", ...). Captured as its own button item so the
            # generator can render a real core/button, not lose it. Use the
            # raw textContent, not inner_text(): GoDaddy styles these
            # labels with CSS text-transform:uppercase, and inner_text()
            # would bake the ALL-CAPS rendering into the stored label.
            label = " ".join((el.text_content() or "").split())
            if not label:
                continue
            href = el.get_attribute("href")
            content.append({
                "type": "button",
                "text": label,
                "href": urljoin(page_url, href) if href else "",
            })
            continue
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
            # "text" holds a small safe inline-HTML fragment (bold/italic/
            # links preserved -- see element_inline_html()), not plain
            # text -- generator_agent.py embeds it directly rather than
            # HTML-escaping it.
            para = {"type": "paragraph", "text": element_inline_html(el, page_url)}
            role = el.get_attribute("data-typography")
            if role:
                para["typography_role"] = role
            content.append(para)
        elif tag in ("ul", "ol"):
            items = [
                element_inline_html(li, page_url)
                for li in el.query_selector_all("li")
                if element_text(li)
            ]
            if items:
                lst = {"type": "list", "items": items}
                # GoDaddy tags the role on the list widget itself, not
                # each individual <li> -- confirmed on the live site's
                # own markup (a "BodyAlpha"-tagged <ul> wrapping plain
                # <li>s with no attribute of their own).
                role = el.get_attribute("data-typography")
                if role:
                    lst["typography_role"] = role
                content.append(lst)
    return content


def extract_hero(page, page_url):
    """The page's hero / banner section, or None if it doesn't have one.

    GoDaddy Website Builder bundles the hero into the same header widget
    it uses for the logo and nav (data-ux="Header"), which CHROME_SELECTOR
    otherwise excludes wholesale as site chrome -- so extract_blocks()'s
    main pass never sees it, and the migrated page starts cold at its
    first <h2> section heading with no page-level <h1> at all. Confirmed
    against the live site's own markup, the hero lives in
    <section data-aid="HEADER_SECTION"> and carries:

      * <h1 data-aid="HEADER_TAGLINE_RENDERED"> -- the only real
        page-level <h1> anywhere on this site (data-typography
        "HeadingAlpha");
      * <div data-aid="HEADER_TAGLINE2_RENDERED"> -- a sub-tagline
        (data-typography "HeadingDelta");
      * <a data-aid="HEADER_CTA_BTN"> -- a call-to-action button
        (data-typography "ButtonAlpha");
      * <div data-aid="BACKGROUND_IMAGE_RENDERED"> -- a full-bleed
        background image set via a CSS background-image (not an <img>),
        whose aria-label is a real human-written description that doubles
        as alt text.

    On this site only the home page has a hero; every other page has the
    same header widget with no HEADER_SECTION tagline, so this returns
    None there and the caller simply prepends nothing. Keyed purely on
    the presence of the <h1> tagline, so any other page that grows one
    would pick it up too.
    """
    data = page.evaluate(
        r"""(baseUrl) => {
            const sec = document.querySelector('[data-aid="HEADER_SECTION"]');
            if (!sec) return null;

            const h1 = sec.querySelector('h1[data-aid="HEADER_TAGLINE_RENDERED"]');
            const headingText = h1 ? h1.textContent.trim() : '';
            if (!headingText) return null;  // no real hero on this page

            const sub = sec.querySelector('[data-aid="HEADER_TAGLINE2_RENDERED"]');
            const cta = sec.querySelector('a[data-aid="HEADER_CTA_BTN"]');
            const bg = sec.querySelector('[data-aid="BACKGROUND_IMAGE_RENDERED"]');

            // The background image is a CSS background-image, and it may
            // sit on the marked element itself or on a nested slideshow
            // container -- walk the marked element and its descendants and
            // take the first real url(...) found. The same walk picks up
            // a dark overlay baked into that same background-image as a
            // leading linear-gradient(rgba(0,0,0,N) ...) -- confirmed on
            // this site's home hero, N is 0 (no overlay at all; the text
            // stays legible via the white box below, not by darkening the
            // whole photo). Earlier versions hard-coded a 60% navy tint
            // here regardless of what the live page actually used, which
            // made the migrated hero image look muddy/over-darkened next
            // to a live page with none.
            let imgUrl = '';
            let dimAlpha = 0;
            if (bg) {
                const cands = [bg, ...bg.querySelectorAll('*')];
                for (const el of cands) {
                    const bi = getComputedStyle(el).backgroundImage;
                    if (bi && bi !== 'none') {
                        const m = bi.match(/url\((['"]?)(.*?)\1\)/);
                        if (m && m[2]) {
                            imgUrl = m[2];
                            const dm = bi.match(/rgba?\([^)]*,\s*([\d.]+)\)/);
                            if (dm) dimAlpha = parseFloat(dm[1]) || 0;
                            break;
                        }
                    }
                }
            }

            // A translucent panel behind the heading/sub-tagline/CTA --
            // confirmed on the live home page: rgba(255,255,255,0.9),
            // padding 40px 56px, no border-radius, wrapping just that
            // text cluster (not the full hero width). Walk up from the
            // <h1> looking for the first ancestor with a real background
            // color, stopping at the section boundary.
            let boxBg = '', boxPadding = '';
            let boxEl = h1.parentElement;
            for (let i = 0; i < 8 && boxEl && boxEl !== sec; i++) {
                const cs = getComputedStyle(boxEl);
                if (cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && cs.backgroundColor !== 'transparent') {
                    boxBg = cs.backgroundColor;
                    boxPadding = cs.padding;
                    break;
                }
                boxEl = boxEl.parentElement;
            }

            const abs = (u) => {
                if (!u) return '';
                try { return new URL(u, baseUrl).href; } catch (e) { return u; }
            };

            return {
                heading: headingText,
                heading_role: h1.getAttribute('data-typography') || '',
                subheading: sub ? sub.textContent.trim() : '',
                subheading_role: sub ? (sub.getAttribute('data-typography') || '') : '',
                cta: cta ? {
                    text: cta.textContent.trim(),
                    href: abs(cta.getAttribute('href')),
                } : null,
                image: imgUrl ? {
                    src: abs(imgUrl),
                    alt: (bg && bg.getAttribute('aria-label')) || '',
                } : null,
                dim_ratio: Math.round(dimAlpha * 100),
                heading_box: boxBg ? { background: boxBg, padding: boxPadding } : null,
            };
        }""",
        page_url,
    )
    if not data or not data.get("heading"):
        return None
    hero = {"type": "hero", "heading": data["heading"]}
    if data.get("heading_role"):
        hero["heading_role"] = data["heading_role"]
    if data.get("subheading"):
        hero["subheading"] = data["subheading"]
        if data.get("subheading_role"):
            hero["subheading_role"] = data["subheading_role"]
    if data.get("cta") and data["cta"].get("text"):
        hero["cta"] = {
            "text": data["cta"]["text"],
            "href": data["cta"].get("href") or "",
        }
    if data.get("image") and data["image"].get("src"):
        hero["image"] = {
            "src": data["image"]["src"],
            "alt": data["image"].get("alt") or "",
        }
    hero["dim_ratio"] = data.get("dim_ratio") or 0
    if data.get("heading_box"):
        hero["heading_box"] = data["heading_box"]
    return hero


def extract_page_banner(page, page_url):
    """GoDaddy Website Builder's "Banner" widget, or None.

    Distinct from the header-widget hero (extract_hero()): this is a
    body-level <div data-ux="WidgetBanner"> -- a ~210px full-width band
    with a stock background photo (a CSS background-image on a nested
    <div data-aid="BACKGROUND_IMAGE_RENDERED">, whose aria-label is a
    human-written description that doubles as alt) and the page title as
    an <h1 data-aid="SECTION_TITLE_RENDERED"> centred in white over a
    dark scrim. Present on the solution/landing pages (connectivity,
    services, threat-protection, ...), absent on about/blog/legal.

    The widget's <h1> is NOT inside CHROME_SELECTOR, so extract_blocks()'s
    main pass would otherwise emit it as a bare "CONNECTIVITY" heading
    with no image -- this tags every element inside the widget with
    data-migration-banner so that pass skips it, and returns the banner
    as its own block instead.
    """
    data = page.evaluate(
        r"""(baseUrl) => {
            const wb = document.querySelector('[data-ux="WidgetBanner"], [data-ux="SectionBanner"]');
            if (!wb) return null;
            wb.querySelectorAll('*').forEach(e => e.setAttribute('data-migration-banner', '1'));
            wb.setAttribute('data-migration-banner', '1');

            const titleEl = wb.querySelector('[data-aid="SECTION_TITLE_RENDERED"]')
                || wb.querySelector('h1, h2');
            const title = titleEl ? titleEl.textContent.replace(/\s+/g, ' ').trim() : '';
            if (!title) return null;

            const bg = wb.querySelector('[data-aid="BACKGROUND_IMAGE_RENDERED"], [data-ux="Background"]') || wb;
            let imgUrl = '';
            for (const el of [bg, ...bg.querySelectorAll('*')]) {
                const bi = getComputedStyle(el).backgroundImage;
                if (bi && bi !== 'none') {
                    // the value is usually "linear-gradient(...), url('...')"
                    const m = bi.match(/url\((['"]?)(.*?)\1\)/);
                    if (m && m[2]) { imgUrl = m[2]; break; }
                }
            }
            const abs = (u) => { try { return new URL(u, baseUrl).href; } catch (e) { return u || ''; } };
            return {
                title: title,
                title_role: titleEl.getAttribute('data-typography') || '',
                image: imgUrl ? {
                    src: abs(imgUrl),
                    alt: (bg && bg.getAttribute('aria-label')) || '',
                } : null,
            };
        }""",
        page_url,
    )
    if not data or not data.get("title"):
        return None
    banner = {"type": "page_banner", "title": data["title"]}
    if data.get("title_role"):
        banner["title_role"] = data["title_role"]
    if data.get("image") and data["image"].get("src"):
        banner["image"] = {
            "src": data["image"]["src"],
            "alt": data["image"].get("alt") or "",
        }
    return banner


def extract_blocks(page, page_url):
    """Turn a rendered page's DOM into structured content blocks."""
    blocks = []

    # The hero/banner lives inside the header widget CHROME_SELECTOR
    # excludes, so it's pulled in explicitly here and prepended -- ahead
    # of the first section heading the main document-order pass starts
    # from. Its own <h1>/sub-tagline/CTA are all inside data-ux="Header",
    # so that same pass still skips them: no double extraction.
    try:
        hero = extract_hero(page, page_url)
    except Exception as e:
        print(f"  [warn] hero extraction failed: {e}")
        hero = None
    if hero:
        blocks.append(hero)

    # GoDaddy "Banner" widget (body-level title-over-photo band) -- see
    # extract_page_banner(). Tags its own subtree with data-migration-banner
    # so the document-order pass below skips the bare title heading.
    try:
        banner = extract_page_banner(page, page_url)
    except Exception as e:
        print(f"  [warn] page-banner extraction failed: {e}")
        banner = None
    if banner:
        blocks.append(banner)

    settle_lazy_widgets(page)
    mark_media_text_pairs(page)
    mark_content_cards(page)
    mark_post_feeds(page)
    mark_pdf_widgets(page)

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
    emitted_pdf_groups = set()
    elements = page.query_selector_all("h1, h2, h3, h4, h5, h6, p, ul, ol, img")
    for el in elements:
        if el.evaluate("(e, sel) => !!e.closest(sel)", CHROME_SELECTOR):
            continue  # inside the header, footer, or a nav -- not page content

        # Inside the GoDaddy "Banner" widget -- already emitted as a
        # single page_banner block (see extract_page_banner()); skip so
        # its title isn't re-extracted as a bare heading.
        if el.evaluate("e => !!e.closest('[data-migration-banner]')"):
            continue

        # A GoDaddy PDF widget (see mark_pdf_widgets()) -- emit it once as
        # a single document_embed block; skip every element inside it so
        # its title/heading/description aren't re-extracted and its pdf.js
        # viewer chrome ("1/5", "Next") doesn't leak in as stray text.
        pdf_group_id = el.evaluate(
            "e => { const w = e.closest('[data-migration-pdf-group]');"
            " return w ? w.getAttribute('data-migration-pdf-group') : null; }"
        )
        if pdf_group_id is not None:
            if pdf_group_id not in emitted_pdf_groups:
                emitted_pdf_groups.add(pdf_group_id)
                widget = page.query_selector(
                    f'[data-migration-pdf-group="{pdf_group_id}"]'
                )
                doc = extract_pdf_widget(widget, page_url) if widget else None
                if doc:
                    blocks.append(doc)
            continue

        # A single GoDaddy blog post (/blog/f/<slug>) renders inside the
        # blog feed widget, which repeats the feed's own section title
        # ("Stratecon Tech Insights") and standing intro blurb ("Please
        # check back here often...") above every individual post. Those
        # are feed chrome, not this post's content -- drop them. The
        # post's real title carries data-ux="BlogMainHeading" and is
        # promoted to the page <h1> in the heading branch below.
        if el.evaluate(
            "e => !!e.closest('[data-aid=\"RSS_SECTION_TITLE_RENDERED\"],"
            " [data-aid=\"RSS_SECTION_INTRO_RENDERED\"]')"
        ):
            continue

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
                content = extract_element_content(text_cell, page_url) if text_cell else []
                resolved = resolve_image_src(el, page_url, seen_image_urls)

                if resolved is not None and content:
                    abs_src, alt = resolved
                    mt_block = {
                        "type": "media_text",
                        "src": abs_src,
                        "alt": alt,
                        "content": content,
                    }
                    side = el.get_attribute("data-migration-media-text-side")
                    if side:
                        mt_block["image_side"] = side
                    width = el.get_attribute("data-migration-media-text-width")
                    if width:
                        mt_block["image_width_pct"] = int(width)
                    blocks.append(mt_block)
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
            # GoDaddy renders a single blog post's own title as an <h3>
            # (data-ux="BlogMainHeading") under the feed's section title.
            # With that feed title now dropped (above), this is the
            # page's real headline -- promote it to <h1> so the migrated
            # post isn't left with no page-level heading.
            level = 1 if el.get_attribute("data-ux") == "BlogMainHeading" else int(tag[1])
            heading = {"type": "heading", "level": level, "text": text}
            role = el.get_attribute("data-typography")
            if role:
                heading["typography_role"] = role
            blocks.append(heading)
        elif tag == "p":
            # GoDaddy sometimes wraps a <ul>/<ol> *inside* a <p> (invalid
            # HTML, but browsers render it). The loop reaches that nested
            # list on its own iteration and emits it as a proper "list"
            # block; without this guard the same items also come out here
            # as one flattened paragraph -- a real duplication confirmed
            # on the live "Spider-Man Dilemma" post (every bulleted
            # benefit appeared twice, once as prose, once as a list).
            # Keep only any real text that precedes the nested list.
            if el.query_selector("ul, ol") is not None:
                lead = el.evaluate(
                    """e => {
                        const c = e.cloneNode(true);
                        c.querySelectorAll('ul, ol').forEach(x => x.remove());
                        return c.textContent.replace(/\\s+/g, ' ').trim();
                    }"""
                )
                if lead:
                    para = {"type": "paragraph", "text": html.escape(lead)}
                    role = el.get_attribute("data-typography")
                    if role:
                        para["typography_role"] = role
                    blocks.append(para)
                continue  # the nested <ul>/<ol> is emitted on its own turn

            # "text" holds a small safe inline-HTML fragment (bold/
            # italic/links preserved -- see element_inline_html()), not
            # plain text -- generator_agent.py embeds it directly rather
            # than HTML-escaping it.
            para = {"type": "paragraph", "text": element_inline_html(el, page_url)}
            role = el.get_attribute("data-typography")
            if role:
                para["typography_role"] = role
            blocks.append(para)
        elif tag in ("ul", "ol"):
            items = [
                element_inline_html(li, page_url)
                for li in el.query_selector_all("li")
                if element_text(li)
            ]
            if items:
                lst = {"type": "list", "items": items}
                # GoDaddy tags the role on the list widget itself, not
                # each individual <li> -- see extract_element_content().
                role = el.get_attribute("data-typography")
                if role:
                    lst["typography_role"] = role
                blocks.append(lst)

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

    # Forms. GoDaddy wraps each real form in a widget-<type> div and tags
    # its fields with data-aid. Three kinds show up on this site:
    #   widget-contact   -> a real contact form (CONTACT_FORM_* fields)
    #   widget-subscribe -> a newsletter email-capture box
    #   widget-rss       -> the blog feed's own "subscribe for updates" box
    # Anything in the site chrome (the sitewide footer newsletter, a nav
    # search box) is excluded. Classifying every <form> the same way and
    # emitting one "forms_detected" block put a literal
    # "[contact-form-7 id=\"TBD\"]" placeholder on ~20 pages whose only
    # form was a subscribe box -- the generator now renders contact forms
    # and newsletter boxes as distinct, clearly-labelled placeholders.
    form_info = page.evaluate(
        r"""() => {
            const CH = 'nav,[data-ux="Header"],[role="contentinfo"],[data-aid="FOOTER_COOKIE_BANNER_RENDERED"]';
            const KNOWN = {
                CONTACT_FORM_NAME: 'Name', CONTACT_FORM_EMAIL: 'Email',
                CONTACT_FORM_MESSAGE: 'Message', CONTACT_FORM_PHONE: 'Phone',
                CONTACT_FORM_EMAIL_OPT_IN: 'Email opt-in', Company: 'Company',
            };
            const label = aid => {
                if (!aid) return null;
                if (KNOWN[aid]) return KNOWN[aid];
                return aid.replace(/^CONTACT_FORM_/, '').replace(/_RENDERED?$/, '')
                          .replace(/_/g, ' ').trim().toLowerCase()
                          .replace(/\b\w/g, c => c.toUpperCase());
            };
            const out = { contact: [], newsletter: 0 };
            for (const f of document.querySelectorAll('form')) {
                if (f.closest(CH)) continue;
                const w = f.closest('[class*="widget-"]');
                const wc = w ? [...w.classList].find(c => /^widget-[a-z]+$/.test(c)) : '';
                const contactAids = !!f.querySelector('[data-aid^="CONTACT_FORM_"]');
                const visible = [...f.querySelectorAll('input,textarea,select')]
                    .filter(i => i.type !== 'hidden' && i.getAttribute('name') !== '_app_id');
                let kind = 'contact';
                if (wc === 'widget-subscribe' || wc === 'widget-rss') kind = 'newsletter';
                else if (!contactAids && wc !== 'widget-contact'
                         && visible.length === 1 && /^(email|text)$/.test(visible[0].type || '')) kind = 'newsletter';
                if (kind === 'newsletter') { out.newsletter++; continue; }
                const fields = visible.map(i => {
                    const wrapAid = i.closest('[data-aid]') ? i.closest('[data-aid]').getAttribute('data-aid') : null;
                    return {
                        label: label(i.getAttribute('data-aid') || wrapAid)
                            || i.getAttribute('placeholder') || i.getAttribute('aria-label') || '',
                        type: (i.getAttribute('type') || i.tagName.toLowerCase()),
                    };
                });
                const region = f.closest('[role="region"], section');
                let title = '';
                const th = region && region.querySelector(
                    '[data-aid="CONTACT_FORM_TITLE_REND"], [data-aid="CONTACT_FORM_TITLE_RENDERED"], h1, h2, h3');
                if (th) title = th.textContent.replace(/\s+/g, ' ').trim();
                out.contact.push({ title, fields });
            }
            return out;
        }"""
    )
    for cf in form_info.get("contact", []):
        blocks.append({
            "type": "contact_form",
            "title": cf.get("title") or "Contact form",
            "fields": cf.get("fields", []),
        })
    if form_info.get("newsletter"):
        blocks.append({
            "type": "newsletter_signup",
            "label": "Newsletter signup",
            "text": "Sign up to receive updates and blog posts by email.",
        })

    # Third-party form / survey embeds (Cognito Forms, JotForm, Typeform,
    # ...). Cross-origin, so only the fact of the embed and its provider
    # are visible -- the generator renders a placeholder + QA flag. See
    # detect_embedded_forms() / EMBED_FORM_PROVIDERS.
    for emb in detect_embedded_forms(page):
        blocks.append({
            "type": "embedded_form",
            "provider": emb["provider"],
            "src": emb["src"],
            "title": emb["title"] or f"{emb['provider']} form",
        })

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
                // Removing N anchors leaves N leftover "|" separators behind
                // (one preceded each removed link) -- a single non-global
                // trailing-pipe strip only ate the last one, leaving one
                // behind to collide with the "|" the generator prepends to
                // each legal link it re-adds. Strip all of them.
                const clone = copyrightBlock.cloneNode(true);
                clone.querySelectorAll('a').forEach(a => a.remove());
                copyright_text = clone.textContent.replace(/(\\|\\s*)+$/, '').trim();
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
    # slug -> {every `/<section>/f/<slug>` URL seen for that post}. Used
    # both to dedupe (one saved page per slug) and, after the crawl, to
    # attach the non-canonical prefixes as `alias_urls` so redirects.csv
    # can 301 them to the new post too.
    feed_item_urls = {}
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
                response = page.goto(url, wait_until="load", timeout=30000)
                page.wait_for_timeout(1000)
                # A dead link elsewhere on the site (an old "/contact-us"
                # that's really "/contact" now) resolves to GoDaddy's 404
                # page. Without this it got crawled and saved as a real
                # migrated page whose entire body was "Page Not Found /
                # We can't seem to find the page you're looking for."
                if response is not None and response.status >= 400:
                    print(f"  [skip] {url} -- HTTP {response.status}")
                    continue
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
            #
            # A GoDaddy blog post is served under every section that
            # links to it (/blog/f/x, /ai-solutions/f/x, ...) with
            # identical content, so its dedupe key is the slug alone, not
            # the full path -- otherwise every re-crawl saves it 2-3
            # times. Every prefix seen is remembered for redirects.csv.
            canonical_path = urlparse(url).path.rstrip("/") or "/"
            stripped_url = url.split("?")[0].rstrip("/")
            feed_slug = blog_feed_slug(canonical_path)
            if feed_slug:
                feed_item_urls.setdefault(feed_slug, set()).add(stripped_url)
            dedupe_key = f"blogpost:{feed_slug}" if feed_slug else canonical_path
            if dedupe_key not in extracted_paths:
                qual = scan_page(page, url)
                if qual["verdict"] == "block":
                    risk_flags[url] = qual["reasons"]
                    print(f"  [BLOCKED] {url} -- out of scope "
                          f"({', '.join(qual['categories'])}); not migrated")
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
                        # A page's own hero background image is a far better
                        # "featured image" than og:image, which GoDaddy sets
                        # to the same generic stock photo on every page --
                        # so when the crawler captured a real hero (see
                        # extract_hero()), prefer its image here.
                        hero_image = ""
                        if blocks and blocks[0].get("type") == "hero":
                            hero_image = (blocks[0].get("image") or {}).get("src", "")
                        extracted_paths.add(dedupe_key)
                        # Record the canonical, query-stripped URL, not
                        # whichever query-string variant happened to be
                        # the first one visited -- that variant is an
                        # implementation detail of how this page's links
                        # were discovered, not the URL real backlinks or
                        # bookmarks would use for redirects. For a blog
                        # post, canonicalise the section prefix to /blog/
                        # too, whichever prefix served this crawl.
                        canonical_url = (
                            canonical_feed_item_url(stripped_url)
                            if feed_slug else stripped_url
                        )
                        page_dict = {
                            "old_url": canonical_url,
                            "slug": slugify(canonical_url, start_url),
                            "title": title,
                            "meta_description": meta_desc or "",
                            "featured_image": hero_image or og_image or "",
                            "type": "page",
                            "is_front_page": (canonical_url == start_url.rstrip("/")),
                            "blocks": blocks,
                        }
                        # Stash the qualification verdict + evidence so the
                        # standalone qualification_agent.py can re-judge
                        # (and run its LLM layer) without a re-crawl.
                        page_dict["_qualification"] = {
                            k: qual[k] for k in ("verdict", "categories", "reasons")
                        }
                        page_dict["_qualification_evidence"] = qual["evidence"]
                        if qual["verdict"] == "review":
                            print(f"  [REVIEW] {url} -- {', '.join(qual['categories'])}: "
                                  f"{qual['reasons'][0] if qual['reasons'] else ''}")
                        pages.append(page_dict)
                        print(f"  [ok] {url} -- {len(blocks)} blocks")

            new_links = discover_links(page, start_url)
            to_visit |= (new_links - visited)

        browser.close()

    # Attach the non-canonical section prefixes a blog post was also
    # served under as `alias_urls`, so the generator can 301 each of them
    # to the new post (they'd otherwise 404 on the new site, which only
    # knows /blog/f/<slug> -> /<slug>/).
    for pg in pages:
        fs = blog_feed_slug(urlparse(pg["old_url"]).path)
        if not fs:
            continue
        canon = pg["old_url"].rstrip("/")
        aliases = sorted(
            u for u in feed_item_urls.get(fs, set()) if u.rstrip("/") != canon
        )
        if aliases:
            pg["alias_urls"] = aliases

    review_slugs = [p["slug"] for p in pages
                    if (p.get("_qualification") or {}).get("verdict") == "review"]
    if risk_flags:
        gate = f"OUT OF SCOPE -- {len(risk_flags)} page(s) blocked, not migrated"
    elif review_slugs:
        gate = f"REVIEW REQUIRED -- {len(review_slugs)} page(s) flagged, migrated"
    else:
        gate = "SITE IN SCOPE -- no out-of-scope functionality detected"

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
        "qualification": {
            "gate": ("out" if risk_flags else "review" if review_slugs else "in"),
            "summary": gate,
            "blocked": sorted(risk_flags.keys()),
            "review": review_slugs,
            "scanned": len(pages) + len(risk_flags),
            "llm_layer": False,
            "evidence": "crawl",
        },
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

    q = result["qualification"]
    print(f"\nDone. {len(result['pages'])} pages extracted; "
          f"{len(result['qualification_flags'])} blocked, {len(q['review'])} to review.")
    print(f"Qualification gate: {q['summary']}")
    print("Wrote structured_content.json -- feed this into generator_agent.py")
    print("Run `python3 qualification_agent.py` for the full report "
          "(and the optional LLM judgement layer).")


if __name__ == "__main__":
    main()
