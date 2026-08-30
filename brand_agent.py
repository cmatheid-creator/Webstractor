#!/usr/bin/env python3
"""
Brand Agent (prototype)
------------------------
Visits a site's homepage with a real browser and extracts brand tokens --
logo, favicon, colors, and typography -- from the page's actually-rendered
(computed) styles.

Static HTML alone isn't enough for this: on GoDaddy Website Builder sites,
most colors and fonts are generated at runtime by a CSS-in-JS engine and
never appear as literal values in the server-rendered markup. This uses
getComputedStyle() in a real rendered page instead of guessing from raw
CSS/HTML.

GoDaddy Website Builder marks up semantic type roles via a
data-typography attribute (HeadingAlpha, BodyAlpha, ButtonAlpha,
LinkAlpha, NavAlpha, ...) and the header logo via data-ux="ImageLogo" --
this reads the computed style of the first element carrying each role
rather than guessing from CSS class names, which are usually
machine-generated and meaningless (e.g. "c1-2p c1-2q").

Run this somewhere with real internet access -- e.g. Claude Code on your
own machine -- not inside a locked-down sandbox.

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python3 brand_agent.py https://stratecon.tech

Output:
    brand.json -- feed this into generator_agent.py alongside
    structured_content.json to produce a WordPress theme.json with the
    extracted color palette and typography.
"""

import sys
import json
import re
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

TYPOGRAPHY_ROLES = [
    "HeadingAlpha",
    "HeadingBeta",
    "HeadingDelta",
    "BodyAlpha",
    "ButtonAlpha",
    "LinkAlpha",
    "NavAlpha",
]

RGB_RE = re.compile(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)")


def rgb_to_hex(value):
    """Convert a computed-style color string ("rgb(29, 43, 82)") to hex.
    Passes through unrecognized formats (e.g. the literal "transparent")
    unchanged. An explicit alpha of 0 -- e.g. "rgba(0, 0, 0, 0)", which is
    <body>'s default background before any site CSS touches it -- means
    "nothing painted here", not "opaque black"; naively dropping the
    alpha channel would misreport it as #000000."""
    m = RGB_RE.match(value or "")
    if not m:
        return value
    r, g, b, a = m.groups()
    if a is not None and float(a) == 0:
        return "transparent"
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def extract_typography(page):
    """For each known type role, read the computed font-family/size/weight
    of the first element carrying it. Roles that don't appear on this
    page (e.g. a role only used elsewhere on the site) are simply absent
    from the result rather than guessed at."""
    typography = {}
    for role in TYPOGRAPHY_ROLES:
        el = page.query_selector(f'[data-typography="{role}"]')
        if not el:
            continue
        style = el.evaluate(
            "e => { const s = getComputedStyle(e); "
            "return {fontFamily: s.fontFamily, fontSize: s.fontSize, "
            "fontWeight: s.fontWeight, color: s.color}; }"
        )
        typography[role] = {
            "font_family": style["fontFamily"],
            "font_size": style["fontSize"],
            "font_weight": style["fontWeight"],
            "color": rgb_to_hex(style["color"]),
        }
    return typography


def find_body_content_element(page):
    """The BodyAlpha-tagged element if the page has one, else <body>.

    NOT the same as page.query_selector('[data-typography="BodyAlpha"],
    body') -- a CSS selector list returns the first DOCUMENT-ORDER match
    across all of them, and <body> structurally precedes every one of
    its own descendants (including any BodyAlpha element), so that
    "fallback" selector actually always resolves to <body> itself. This
    does the fallback explicitly instead.
    """
    return page.query_selector('[data-typography="BodyAlpha"]') or page.query_selector("body")


def extract_colors(page):
    colors = {}
    content_el = find_body_content_element(page)

    if content_el:
        # <body> itself is usually unstyled on these sites -- the actual
        # page background lives on a wrapper div somewhere between
        # <body> and the real content, and an untouched <body> computes
        # to the browser default (fully transparent) rather than
        # anything the site's design actually specifies. Walking up
        # from a real content element -- not <body>, which would skip
        # right past any wrapper div sitting below it -- to the nearest
        # ancestor with a non-transparent background finds the color a
        # visitor actually sees behind that content. If nothing up the
        # chain ever paints a background, the page is relying on the
        # browser's plain white canvas, which is the honest answer here
        # (not "transparent", which isn't a usable palette color).
        bg = content_el.evaluate(
            """e => {
                let node = e;
                while (node) {
                    const bg = getComputedStyle(node).backgroundColor;
                    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
                        return bg;
                    }
                    node = node.parentElement;
                }
                return 'rgb(255, 255, 255)';
            }"""
        )
        colors["background"] = rgb_to_hex(bg)

        text_color = content_el.evaluate("e => getComputedStyle(e).color")
        colors["text"] = rgb_to_hex(text_color)

    button = page.query_selector('[data-typography="ButtonAlpha"]')
    if button:
        style = button.evaluate(
            "e => { const s = getComputedStyle(e); "
            "return {bg: s.backgroundColor, text: s.color}; }"
        )
        colors["button_background"] = rgb_to_hex(style["bg"])
        colors["button_text"] = rgb_to_hex(style["text"])

    # Explicit fallback, not a combined selector -- see
    # find_body_content_element() for why: <a> tags near the top of the
    # DOM (skip links, nav, a logo wrapper) would otherwise always win
    # over the actual LinkAlpha-styled content link.
    link = page.query_selector('[data-typography="LinkAlpha"]') or page.query_selector("a")
    if link:
        style = link.evaluate("e => getComputedStyle(e).color")
        colors["link"] = rgb_to_hex(style)

    return colors


def extract_logo(page, base_url):
    # Explicit fallback chain, not a combined selector -- see
    # find_body_content_element() for why that matters: a generic
    # "header img" appearing earlier in the DOM than the real
    # data-ux="ImageLogo" element would otherwise silently win.
    el = (
        page.query_selector('[data-ux="ImageLogo"]')
        or page.query_selector("header img")
        or page.query_selector('img[alt*="logo" i]')
    )
    if not el:
        return None
    src = el.get_attribute("src")
    if not src:
        return None
    logo = {
        "url": urljoin(base_url, src),
        "alt": el.get_attribute("alt") or "",
    }
    # The logo's actual rendered size on the live page -- not its natural
    # image dimensions, which are usually much larger (GoDaddy serves a
    # high-DPI srcset) than how big it's actually displayed. Confirmed a
    # real gap without this: WordPress's core/site-logo block defaults to
    # a small fixed width with no signal to size it correctly, rendering
    # noticeably smaller than the original site's header.
    box = el.evaluate(
        "e => ({w: Math.round(e.getBoundingClientRect().width), "
        "h: Math.round(e.getBoundingClientRect().height)})"
    )
    if box["w"] > 0 and box["h"] > 0:
        logo["width"] = box["w"]
        logo["height"] = box["h"]
    return logo


def extract_favicon(page, base_url):
    el = page.query_selector(
        'link[rel="icon"], link[rel="shortcut icon"], link[rel="apple-touch-icon"]'
    )
    if not el:
        return None
    href = el.get_attribute("href")
    return urljoin(base_url, href) if href else None


def extract_brand(url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # Same rationale as crawler_agent.py: "networkidle" false-negatives
        # on sites with persistent background connections.
        page.goto(url, wait_until="load", timeout=30000)
        page.wait_for_timeout(1000)

        brand = {
            "source_url": url,
            "logo": extract_logo(page, url),
            "favicon_url": extract_favicon(page, url),
            "colors": extract_colors(page),
            "typography": extract_typography(page),
        }

        browser.close()
    return brand


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 brand_agent.py https://example.com")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Extracting brand tokens from {url} ...")
    brand = extract_brand(url)

    with open("brand.json", "w") as f:
        json.dump(brand, f, indent=2)

    print("Wrote brand.json:")
    print(json.dumps(brand, indent=2))
    print("\nFeed this into generator_agent.py alongside structured_content.json")
    print("to produce a WordPress theme.json with the extracted palette.")


if __name__ == "__main__":
    main()
