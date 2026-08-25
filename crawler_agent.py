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
    return path if path else "home"


def extract_blocks(page):
    """Turn a rendered page's DOM into structured content blocks."""
    blocks = []

    # Headings + paragraphs + lists, in document order
    elements = page.query_selector_all("h1, h2, h3, h4, h5, h6, p, ul, ol")
    for el in elements:
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        text = (el.inner_text() or "").strip()
        if not text:
            continue

        if tag.startswith("h"):
            blocks.append({"type": "heading", "level": int(tag[1]), "text": text})
        elif tag == "p":
            blocks.append({"type": "paragraph", "text": text})
        elif tag in ("ul", "ol"):
            items = [
                li.inner_text().strip()
                for li in el.query_selector_all("li")
                if li.inner_text().strip()
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
        text = (el.inner_text() or "").strip()
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

    # Images
    images = []
    for img in page.query_selector_all("img"):
        src = img.get_attribute("src")
        alt = img.get_attribute("alt") or ""
        if src:
            images.append({"src": src, "alt": alt})
    if images:
        blocks.append({"type": "images_detected", "images": images})

    return blocks


def discover_links(page, base_url):
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    links = set()
    for href in hrefs:
        if not href or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        full = urljoin(base_url, href)
        full = full.split("#")[0].rstrip("/")
        if same_domain(base_url, full):
            links.add(full)
    return links


# ---- Qualification Agent hook -------------------------------------------
# Cheap keyword/DOM checks to flag pages that fall outside the
# informational-site scope (payments, logins, forums). This is NOT a
# substitute for the real Qualification Agent -- it's a first-pass filter
# so the crawler can flag risk early rather than silently processing
# something out of scope.
RISK_PATTERNS = [
    (re.compile(r"stripe|paypal|checkout|add.to.cart|shopping.cart", re.I), "possible ecommerce/payment"),
    (re.compile(r"login|sign.?in|my.account|password", re.I), "possible login/account area"),
    (re.compile(r"forum|community.board|discourse|phpbb", re.I), "possible forum/community feature"),
]


def flag_risks(html_text):
    flags = []
    for pattern, label in RISK_PATTERNS:
        if pattern.search(html_text):
            flags.append(label)
    return flags


def crawl(start_url, max_pages=100):
    visited = set()
    to_visit = {start_url.rstrip("/")}
    pages = []
    risk_flags = {}

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
            except Exception as e:
                print(f"  [skip] {url} -- {e}")
                continue

            title = page.title()
            meta_desc_el = page.query_selector("meta[name='description']")
            meta_desc = meta_desc_el.get_attribute("content") if meta_desc_el else ""

            html_text = page.content()
            risks = flag_risks(html_text)
            if risks:
                risk_flags[url] = risks
                print(f"  [FLAGGED] {url} -- {risks} (skipping content extraction)")
                continue  # out of scope for this pipeline -- human review

            blocks = extract_blocks(page)
            pages.append({
                "old_url": url,
                "slug": slugify(url, start_url),
                "title": title,
                "meta_description": meta_desc or "",
                "type": "page",
                "is_front_page": (url.rstrip("/") == start_url.rstrip("/")),
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
        "navigation": [],
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
