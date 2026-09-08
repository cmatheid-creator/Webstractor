#!/usr/bin/env python3
"""
Content Structuring Agent  (pipeline step 5)
-------------------------------------------
Reads the raw crawl (`structured_content.json`) and cleans it into
WP-ready structured content before `generator_agent.py` turns it into a
WXR file. Three jobs:

  1. FAQ restructuring. GoDaddy's FAQ accordion renders each question as a
     click-to-toggle control (not an <h*>/<p>) and each answer in a
     separate panel, so `crawler_agent.py` captures the questions in a
     `faq_raw_unverified` block and the answers as loose paragraphs under
     the "Frequently Asked Questions" heading -- the migrated page ends up
     with orphan answer paragraphs and a duplicated blob of question
     text. This agent pairs each question with its answer and emits a
     single clean `faq` block (which the generator renders as <h3>/<p>
     pairs).

  2. Meta title + description. Generates a concise SEO title and a
     150-160 character meta description for every page from its actual
     content, filling the gap where the crawl found a weak or missing
     <meta name="description">.

  3. Image alt text. Any image the crawl left without alt text (standalone
     image, media+text, hero, or card image) is sent to Claude with the
     page for context, and concise literal alt text is written back onto
     the block -- accessibility the crawl couldn't supply.

Everything else in `structured_content.json` is passed through unchanged.
The pristine crawl is copied to `structured_content.raw.json` (refreshed
whenever the input is an unstructured fresh crawl) so re-running is always
safe.

Re-runs are incremental: each page carries a `_structured_blocks_hash`
and `_structured_agent_version`. A page is re-structured only if its
blocks changed since the last run (a re-crawl or a hand edit) or this
agent's logic version moved on; `--force` overrides both.

LLM: the semantic work (pairing questions to answers, writing meta text,
describing images) goes to Claude via the Anthropic SDK. Set
`ANTHROPIC_API_KEY` (or run `ant auth login`). `--offline` skips the API
and does only the parts that don't need it -- deterministic FAQ region
detection with naive in-order pairing -- which is enough to smoke-test
the pipeline.

Setup:
    pip install anthropic

Usage:
    python3 content_structuring_agent.py                 # structure new/changed pages
    python3 content_structuring_agent.py --dry-run       # show changes, write nothing
    python3 content_structuring_agent.py --offline       # no API; FAQ region + naive pairing only
    python3 content_structuring_agent.py --pages home,about   # just these, always
    python3 content_structuring_agent.py --model claude-sonnet-5
    python3 content_structuring_agent.py --force         # every page, ignore the staleness check

Output:
    structured_content.json      -- rewritten in place, ready for generator_agent.py
    structured_content.raw.json  -- the untouched crawl (refreshed on a fresh-crawl input)
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from urllib.request import Request, urlopen

SRC = "structured_content.json"
RAW_BACKUP = "structured_content.raw.json"
MODEL_DEFAULT = "claude-opus-5"

# Bump when the structuring logic changes in a way that should re-process
# pages an older run already marked done (new prompt rules, a new job,
# a schema change). Pages carry the version they were structured under.
AGENT_VERSION = 2

FAQ_HEADING_RE = re.compile(r"\b(faq|frequently asked questions)\b", re.I)


# --------------------------------------------------------------------------
# Helpers that never touch the network
# --------------------------------------------------------------------------
def block_plain_text(block):
    """A readable plain-text rendering of one content block, for prompts
    and for hashing. Inline HTML fragments (paragraph/list text) are
    stripped back to text."""
    t = block.get("type")
    if t == "heading":
        return f"{'#' * block.get('level', 2)} {_strip_tags(block.get('text', ''))}"
    if t == "paragraph":
        return _strip_tags(block.get("text", ""))
    if t == "list":
        return "\n".join(f"- {_strip_tags(i)}" for i in block.get("items", []))
    if t == "faq":
        return "\n".join(f"Q: {i['q']}\nA: {i['a']}" for i in block.get("items", []))
    if t == "faq_raw_unverified":
        return "FAQ (unpaired): " + " | ".join(block.get("raw_text_blocks", []))
    if t == "hero":
        return f"[hero] {block.get('heading', '')} / {block.get('subheading', '')}"
    if t == "card_group":
        return "[cards] " + " | ".join(
            (c.get("heading") or "") for c in block.get("cards", [])
        )
    if t == "media_text":
        return "[media+text] " + " ".join(
            _strip_tags(x.get("text", "")) for x in block.get("content", [])
            if x.get("type") in ("paragraph", "heading")
        )
    if t == "post_feed":
        return "[post feed]"
    if t in ("contact_form", "newsletter_signup", "document_embed", "image"):
        return f"[{t}]"
    return f"[{t}]"


def _strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def page_blocks_hash(page):
    """A fingerprint of the page's content blocks (ignoring `_`-prefixed
    bookkeeping keys). Stored after structuring and recomputed on the next
    run: an unchanged hash means nothing has touched the page since we
    last structured it, so it can be skipped. Recomputed *after* this
    run's edits, so a clean re-run of an already-structured page is a
    no-op rather than a perpetual "stale"."""
    payload = json.dumps(
        [
            {k: v for k, v in b.items() if not k.startswith("_")}
            for b in page.get("blocks", [])
        ],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Image alt text
# --------------------------------------------------------------------------
def images_needing_alt(page):
    """Every image on the page with no alt text, as (ref, src) pairs where
    `ref` is the dict that carries the `alt` key (the block itself for
    `image`/`media_text`, the nested image dict for `hero`/`card`).
    Writing to `ref["alt"]` updates the page in place."""
    out = []
    for b in page.get("blocks", []):
        t = b.get("type")
        if t == "image" and b.get("src") and not (b.get("alt") or "").strip():
            out.append((b, b["src"]))
        elif t == "media_text" and b.get("src") and not (b.get("alt") or "").strip():
            out.append((b, b["src"]))
        elif t == "hero":
            im = b.get("image") or {}
            if im.get("src") and not (im.get("alt") or "").strip():
                out.append((im, im["src"]))
        elif t == "card_group":
            for c in b.get("cards", []) or []:
                im = c.get("image") or {}
                if im.get("src") and not (im.get("alt") or "").strip():
                    out.append((im, im["src"]))
    return out


def _sniff_image_media_type(data):
    """The image media_type from magic bytes -- more reliable than the
    CDN's Content-Type or the URL extension, which for GoDaddy often lie
    (a `.webp` URL returning JPEG bytes). Returns None for anything the
    Messages API can't take."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def fetch_image_as_block(url):
    """Download an image and return a Messages API image content block
    (base64 source), or None if it can't be fetched or isn't a supported
    type. Base64 rather than a URL source so it works regardless of
    whether Anthropic's fetcher can reach the GoDaddy CDN."""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Webstractor content-structuring agent)"})
    with urlopen(req, timeout=20) as r:
        data = r.read()
    media_type = _sniff_image_media_type(data)
    if not media_type:
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def find_faq_region(blocks):
    """Locate the FAQ on a page. Returns a dict with the question list,
    the answer-paragraph block indices, the intro-paragraph index (if
    any), the FAQ heading index, and the faq_raw_unverified index -- or
    None if the page has no FAQ.

    Shape on the live site: a heading matching /faq|frequently asked/,
    then an optional intro paragraph, then one loose paragraph per
    answer, then (appended at the end of the page by the crawler) a
    faq_raw_unverified block holding the questions.
    """
    raw_idx = next(
        (i for i, b in enumerate(blocks) if b.get("type") == "faq_raw_unverified"),
        None,
    )
    if raw_idx is None:
        return None

    raw = blocks[raw_idx]
    questions = [
        q.strip()
        for q in raw.get("raw_text_blocks", [])
        if q.strip().endswith("?") and len(q.strip()) < 200
    ]
    # de-dupe, keep order
    seen = set()
    questions = [q for q in questions if not (q in seen or seen.add(q))]
    if not questions:
        return None

    heading_idx = next(
        (
            i
            for i, b in enumerate(blocks)
            if b.get("type") == "heading" and FAQ_HEADING_RE.search(b.get("text", ""))
        ),
        None,
    )
    if heading_idx is None:
        return None

    # Paragraphs from just after the FAQ heading up to the next heading.
    answer_idxs, intro_idx = [], None
    for i in range(heading_idx + 1, len(blocks)):
        b = blocks[i]
        if b.get("type") == "heading":
            break
        if b.get("type") != "paragraph":
            continue
        txt = _strip_tags(b.get("text", ""))
        # The lead-in line ("Please contact us if you can't find an
        # answer...") is not an answer.
        if intro_idx is None and answer_idxs == [] and re.search(
            r"\b(cannot find|can'?t find|contact us|below)\b", txt, re.I
        ):
            intro_idx = i
            continue
        answer_idxs.append(i)

    return {
        "heading_idx": heading_idx,
        "intro_idx": intro_idx,
        "answer_idxs": answer_idxs,
        "raw_idx": raw_idx,
        "questions": questions,
        "answers": [_strip_tags(blocks[i].get("text", "")) for i in answer_idxs],
    }


def apply_faq(page, region, items):
    """Replace the FAQ region's loose answer paragraphs + the
    faq_raw_unverified block with one `faq` block, in place. Keeps the
    "Frequently Asked Questions" heading; drops the orphan intro line."""
    blocks = page["blocks"]
    faq_block = {"type": "faq", "items": items}

    drop = set(region["answer_idxs"]) | {region["raw_idx"]}
    if region["intro_idx"] is not None:
        drop.add(region["intro_idx"])
    insert_at = region["answer_idxs"][0] if region["answer_idxs"] else region["raw_idx"]

    new_blocks = []
    for i, b in enumerate(blocks):
        if i == insert_at:
            new_blocks.append(faq_block)
        if i in drop:
            continue
        new_blocks.append(b)
    page["blocks"] = new_blocks


def naive_pairing(region):
    """Pair question N with answer paragraph N, in order -- correct when
    the page lists them 1:1 in the same order (the common case, and true
    for stratecon.tech). Used by --offline."""
    qs, as_ = region["questions"], region["answers"]
    n = min(len(qs), len(as_))
    return [{"q": qs[i], "a": as_[i]} for i in range(n)]


# --------------------------------------------------------------------------
# LLM pass
# --------------------------------------------------------------------------
STRUCT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "meta_title": {"type": "string"},
        "meta_description": {"type": "string"},
        "faq": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"q": {"type": "string"}, "a": {"type": "string"}},
                "required": ["q", "a"],
            },
        },
        "image_alt": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"src": {"type": "string"}, "alt": {"type": "string"}},
                "required": ["src", "alt"],
            },
        },
    },
    "required": ["meta_title", "meta_description"],
}

SYSTEM_PROMPT = (
    "You are the Content Structuring Agent in an automated website-migration "
    "pipeline. You clean crawled content into publish-ready form WITHOUT "
    "inventing facts. Rules:\n"
    "- meta_title: <= 60 characters, specific to this page, no site-name "
    "boilerplate unless it already reads that way.\n"
    "- meta_description: 150-160 characters, plain sentence(s) drawn only from "
    "the page's own content, no clickbait, no ellipsis padding.\n"
    "- faq (only when the page has an FAQ): pair each supplied question with "
    "the answer paragraph that actually answers it. Use the supplied answer "
    "text close to verbatim -- light copy-editing only (fix an obvious typo "
    "such as a doubled word, tighten a broken link phrase). Never write an "
    "answer that isn't supported by the supplied text. Keep the questions "
    "verbatim. Omit the faq field entirely if no questions were supplied.\n"
    "- image_alt (only when images are attached): for each attached image, "
    "return an object with its exact `src` string (as listed) and `alt` "
    "text -- a concise literal description of what is visibly in the image, "
    "<= 125 characters, no leading \"image of\"/\"photo of\", no invented "
    "detail. Omit the image_alt field entirely if no images were attached.\n"
    "Respond with a single JSON object and nothing else."
)


def build_user_prompt(page, region, image_srcs=()):
    lines = [
        f"PAGE TITLE (from crawl): {page.get('title', '')}",
        f"PAGE SLUG: {page.get('slug', '')}",
        f"EXISTING META DESCRIPTION (may be weak/empty): {page.get('meta_description', '') or '(none)'}",
        "",
        "PAGE CONTENT (in order):",
    ]
    for b in page.get("blocks", []):
        txt = block_plain_text(b)
        if txt:
            lines.append(txt)
    if region:
        lines += [
            "",
            "FAQ QUESTIONS TO PAIR (verbatim, keep order unless an answer clearly matches a different one):",
        ]
        lines += [f"  {i+1}. {q}" for i, q in enumerate(region["questions"])]
        lines += ["", "CANDIDATE ANSWER PARAGRAPHS (already on the page, under the FAQ heading):"]
        lines += [f"  - {a}" for a in region["answers"]]
    else:
        lines += ["", "This page has no FAQ. Omit the faq field."]
    if image_srcs:
        lines += [
            "",
            "IMAGES ATTACHED ABOVE, IN THIS ORDER -- each needs alt text. Return an "
            "image_alt entry per image, echoing the src string exactly:",
        ]
        lines += [f"  {i+1}. {s}" for i, s in enumerate(image_srcs)]
    else:
        lines += ["", "No images attached. Omit the image_alt field."]
    return "\n".join(lines)


def call_llm(client, model, page, region, alt_targets):
    # Attach any images that need alt text, in a stable order; keep the
    # srcs list aligned so the response can be mapped back.
    image_blocks, image_srcs = [], []
    for ref, src in alt_targets:
        try:
            blk = fetch_image_as_block(src)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] could not fetch image for alt text ({src[:70]}): {e}")
            continue
        if blk is None:
            print(f"  [warn] unsupported image type, skipping alt text: {src[:70]}")
            continue
        image_blocks.append(blk)
        image_srcs.append(src)

    content = image_blocks + [
        {"type": "text", "text": build_user_prompt(page, region, image_srcs)}
    ]

    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        # List form + cache_control marks the stable instructions as a
        # cache prefix. It only starts actually caching once that prefix
        # clears the model's minimum (~1KB) -- i.e. when the rules here
        # grow -- but costs nothing to leave in place now.
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": STRUCT_SCHEMA},
        },
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return json.loads(text)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def structure_page(page, result, region, alt_targets, offline):
    """Apply one page's structuring result (from the LLM, or naive when
    offline) to the page dict in place. Returns a list of change notes."""
    notes = []

    if region:
        items = None
        if not offline and result.get("faq"):
            items = [
                {"q": i["q"].strip(), "a": i["a"].strip()}
                for i in result["faq"]
                if i.get("q") and i.get("a")
            ]
        if not items:
            items = naive_pairing(region)
        if items:
            apply_faq(page, region, items)
            notes.append(f"FAQ: paired {len(items)} Q&A -> one faq block "
                         f"(removed {len(region['answer_idxs'])} loose paragraph(s) "
                         f"+ the unverified block)")

    if not offline and result:
        mt = (result.get("meta_title") or "").strip()
        md = (result.get("meta_description") or "").strip()
        if mt and mt != page.get("meta_title"):
            page["meta_title"] = mt
            notes.append(f'meta_title -> "{mt}"')
        if md and md != page.get("meta_description"):
            old = page.get("meta_description", "")
            page["meta_description"] = md
            notes.append(f'meta_description -> "{md[:70]}..." (was {len(old)} chars)')

    if not offline and alt_targets and result.get("image_alt"):
        by_src = {
            a["src"]: a["alt"].strip()
            for a in result["image_alt"]
            if a.get("src") and (a.get("alt") or "").strip()
        }
        applied = 0
        for ref, src in alt_targets:
            alt = by_src.get(src)
            if alt and not (ref.get("alt") or "").strip():
                ref["alt"] = alt
                applied += 1
        if applied:
            notes.append(f"image alt: wrote alt text for {applied} image(s) "
                         f"that had none")

    return notes


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    ap.add_argument("--offline", action="store_true", help="no API; FAQ region + naive pairing only")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--pages", help="comma-separated slugs to limit to")
    ap.add_argument("--force", action="store_true",
                    help="re-structure every page, ignoring the staleness check")
    args = ap.parse_args()

    if not os.path.exists(SRC):
        sys.exit(f"{SRC} not found -- run crawler_agent.py first.")
    with open(SRC) as f:
        data = json.load(f)

    # Keep structured_content.raw.json a true copy of the *current* crawl:
    # write it whenever the input is an unstructured fresh crawl (no page
    # carries a structured marker), or when no backup exists yet.
    STRUCT_MARKERS = ("_structured_blocks_hash", "_structured_raw_hash")
    is_fresh_crawl = not any(
        any(m in p for m in STRUCT_MARKERS) for p in data.get("pages", [])
    )
    if is_fresh_crawl or not os.path.exists(RAW_BACKUP):
        with open(RAW_BACKUP, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Saved the raw crawl to {RAW_BACKUP}")

    client = None
    if not args.offline:
        try:
            import anthropic
        except ImportError:
            sys.exit("The `anthropic` package is not installed. `pip install anthropic`, "
                     "or run with --offline.")
        client = anthropic.Anthropic()
        # The SDK 1.x constructor doesn't validate credentials, so probe
        # once up front rather than fail identically on all 37 pages.
        try:
            client.models.list(limit=1)
        except anthropic.AuthenticationError:
            sys.exit("Anthropic auth failed. Set ANTHROPIC_API_KEY, run `ant auth login`, "
                     "or use --offline (FAQ region + naive pairing only).")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "authentication" in msg.lower() or "api_key" in msg.lower():
                sys.exit(f"Anthropic auth not configured ({msg}). Set ANTHROPIC_API_KEY, "
                         "run `ant auth login`, or use --offline.")
            print(f"  [warn] could not pre-check the API ({e}); continuing anyway")

    only = set(s.strip() for s in args.pages.split(",")) if args.pages else None
    changed_pages = 0
    skipped = 0

    for page in data.get("pages", []):
        slug = page.get("slug", "")
        if only and slug not in only:
            continue

        # Staleness check: skip a page only if its blocks are byte-identical
        # to what this agent last produced for it AND it was structured by
        # the current AGENT_VERSION. A changed hash means a re-crawl or a
        # hand edit; an older version means the logic moved on. --force
        # overrides both. (`--pages` implies re-doing the named pages.)
        prev_hash = page.get("_structured_blocks_hash")
        prev_ver = page.get("_structured_agent_version")
        cur_hash = page_blocks_hash(page)
        if not args.force and not (only and slug in only) and prev_hash is not None:
            if prev_hash == cur_hash and prev_ver == AGENT_VERSION:
                skipped += 1
                continue
            if prev_hash != cur_hash:
                print(f"[{slug or '(home)'}] blocks changed since last run -- re-structuring")
            elif prev_ver != AGENT_VERSION:
                print(f"[{slug or '(home)'}] structured by agent v{prev_ver} "
                      f"(now v{AGENT_VERSION}) -- re-structuring")

        region = find_faq_region(page.get("blocks", []))
        alt_targets = images_needing_alt(page)
        result = {}
        if client is not None:
            try:
                result = call_llm(client, args.model, page, region, alt_targets)
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] {slug}: LLM call failed ({e}); "
                      f"{'pairing FAQ naively' if region else 'passing through'}")
        notes = structure_page(page, result, region, alt_targets, offline=(client is None))

        if notes:
            changed_pages += 1
            print(f"[{slug or '(home)'}]")
            for n in notes:
                print(f"   - {n}")
        # Mark the page done: version + a hash of the blocks as they now
        # stand, so a clean re-run skips it.
        page.pop("_structured_raw_hash", None)  # retire the old marker
        page["_structured_agent_version"] = AGENT_VERSION
        page["_structured_blocks_hash"] = page_blocks_hash(page)

    print(f"\n{changed_pages} page(s) structured, {skipped} unchanged/skipped.")

    if args.dry_run:
        print("--dry-run: not writing.")
        return
    with open(SRC, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {SRC} -- feed this into generator_agent.py")


if __name__ == "__main__":
    main()
