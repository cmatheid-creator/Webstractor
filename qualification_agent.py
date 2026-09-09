#!/usr/bin/env python3
"""
Qualification Agent  (pipeline step 3)
-------------------------------------
The safety gate that keeps "fully automated migration" an honest claim.
It decides, per page and for the site as a whole, whether the content is
inside the supported scope:

  IN SCOPE  : informational / marketing sites -- pages, blog posts,
              FAQs, contact forms, knowledge-base articles, PDF handouts.
  OUT       : anything stateful or transactional the pipeline does not
              faithfully reproduce -- an online store or any payment
              flow, a login / account / members area, a forum or
              threaded community, an appointment/booking system, a
              donation/fundraising flow.

Two layers:

  1. Deterministic scan (no network, no API). A catalog of concrete
     signals -- DOM selectors, GoDaddy `data-ux` widget names, script
     hosts, `<form action>` hosts, URL-path patterns -- each tagged with
     a category and a weight (strong / moderate / weak). This is the
     backbone and runs on every page.

       - a STRONG signal  -> verdict "block"   (page is excluded from the
                             migration; site gate trips)
       - a MODERATE signal -> verdict "review" (page is still migrated,
                             but flagged for a human to confirm)
       - only WEAK / none  -> verdict "pass"

     Weak signals never decide anything on their own -- they exist so a
     reviewer sees "we noticed X but judged it in scope". This is a
     deliberate reaction to an earlier keyword-matching version that
     silently dropped real blog prose about password hygiene or online
     communities.

  2. LLM judgement (optional). For pages that pass the deterministic
     scan, Claude re-reads the extracted content against the strict
     scope definition and can escalate a false negative the DOM missed
     ("this page walks the reader through creating an account", "this is
     a checkout confirmation"). `--offline` skips it. No API key in this
     environment -> run the equivalent judgement inside the Claude Code
     session instead (same as the Content Structuring Agent's meta pass).

How it is wired in:

  - `crawler_agent.py` calls `scan_page(page, url)` on the live page
    while it has the DOM, stores the evidence on the page dict
    (`_qualification_evidence` / `_qualification`), and skips content
    extraction only on a "block".
  - This module, run standalone (`python qualification_agent.py`),
    re-judges from the stored evidence + extracted text, applies the
    optional LLM layer, writes `qualification_report.md`, and stamps a
    site-level `data["qualification"]` summary the generator surfaces in
    the QA report.

Usage:
    python3 qualification_agent.py                 # re-judge, write report
    python3 qualification_agent.py --offline       # deterministic only
    python3 qualification_agent.py --dry-run       # print, write nothing
    python3 qualification_agent.py --pages contact,store
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import urlparse

SRC = "structured_content.json"
OUT_REPORT = "qualification_report.md"
MODEL_DEFAULT = "claude-opus-5"

CATEGORIES = ("payments", "accounts", "forum", "booking", "donation")

# --------------------------------------------------------------------------
# Signal catalog
# --------------------------------------------------------------------------
# Each rule matches against the `evidence` dict produced by scan_page():
#   scripts    : list of <script src> URLs
#   form_acts  : list of <form action> URLs (absolute)
#   data_ux    : set of GoDaddy data-ux / data-aid attribute values (lower)
#   classes    : one big lowercased string of every element class on page
#   has_password: bool  -- an <input type=password> exists
#   text       : a bounded sample of visible page text (lowercased)
#   path       : URL path (lowercased)
#
# A rule fires when ANY of its predicates matches. `weight`:
#   "strong"   -> real out-of-scope functionality -> block
#   "moderate" -> probable, needs a human look     -> review
#   "weak"     -> mentioned/embedded but likely fine -> note only
class Rule:
    def __init__(self, id, category, weight, *, hosts=(), classes=(),
                 data_ux=(), path_re=None, text_re=None, needs_password=False,
                 require_all=False, detail=""):
        self.id = id
        self.category = category
        self.weight = weight
        self.hosts = tuple(h.lower() for h in hosts)
        self.classes = tuple(c.lower() for c in classes)
        self.data_ux = tuple(d.lower() for d in data_ux)
        self.path_re = re.compile(path_re, re.I) if path_re else None
        self.text_re = re.compile(text_re, re.I) if text_re else None
        self.needs_password = needs_password
        # By default any one configured predicate firing = a match (OR).
        # `require_all` makes every configured predicate mandatory (AND) --
        # e.g. "a password field AND login copy", not either alone.
        self.require_all = require_all
        self.detail = detail

    def match(self, ev):
        # per-predicate: None = not configured, [] = configured but no hit,
        # [..] = configured and hit
        checks = {}
        if self.hosts:
            h = []
            for u in ev.get("scripts", []) + ev.get("form_acts", []):
                lu = (u or "").lower()
                for host in self.hosts:
                    if host in lu:
                        h.append(f"{host} in {u[:120]}")
            checks["hosts"] = h
        if self.classes:
            blob = ev.get("classes", "")
            checks["classes"] = [f"class ~ {c!r}" for c in self.classes if c in blob]
        if self.data_ux:
            dux = ev.get("data_ux", set())
            checks["data_ux"] = [f"data-ux/aid {d!r}" for d in self.data_ux if d in dux]
        if self.path_re:
            checks["path"] = ([f"URL path {ev.get('path')!r}"]
                              if self.path_re.search(ev.get("path", "")) else [])
        if self.needs_password:
            checks["password"] = (["<input type=password> present"]
                                  if ev.get("has_password") else [])
        if self.text_re:
            m = self.text_re.search(ev.get("text", ""))
            checks["text"] = [f"text {m.group(0)!r}"] if m else []

        if self.require_all:
            if not checks or any(not v for v in checks.values()):
                return []
        return [hit for v in checks.values() for hit in v]


RULES = [
    # ---- payments / ecommerce ---------------------------------------
    Rule("gd-online-store", "payments", "strong",
         data_ux=("store", "productlist", "productwidget", "product",
                  "ols", "onlinestore", "cart", "checkout", "buynow"),
         classes=("add-to-cart", "shopping-cart", "product-list", "sqs-add-to-cart"),
         detail="GoDaddy Online Store / cart / product widget"),
    Rule("payment-sdk", "payments", "strong",
         hosts=("js.stripe.com", "checkout.stripe.com", "square.site",
                "squareup.com", "js.squareup.com", "www.paypal.com/sdk",
                "paypalobjects.com", "checkout.razorpay.com",
                "pay.google.com", "app.link/checkout"),
         detail="third-party payment SDK loaded"),
    Rule("commerce-platform", "payments", "strong",
         hosts=("cdn.shopify.com", "checkout.shopify", "ecwid.com",
                "app.ecwid.com", "bigcommerce.com", "snipcart.com",
                "gumroad.com/js", "foxycart.com"),
         classes=("woocommerce", "wc-block", "shopify-section", "ecwid",
                  "snipcart", "foxycart"),
         detail="external commerce platform (WooCommerce / Shopify / Ecwid / ...)"),
    Rule("checkout-path", "payments", "strong",
         path_re=r"/(cart|checkout|shop|store|products?)(/|$)",
         detail="URL path is a store/cart/checkout route"),
    Rule("buy-donate-button", "payments", "moderate",
         data_ux=("paypalbutton", "paymentbutton", "donatebutton"),
         text_re=r"\b(add to cart|buy now|proceed to checkout|complete purchase)\b",
         detail="buy / checkout call-to-action"),
    Rule("price-widget", "payments", "weak",
         classes=("price", "product-price", "sqs-money"),
         detail="price styling present (often just marketing copy)"),

    # ---- accounts / login / members --------------------------------
    Rule("members-path", "accounts", "strong",
         path_re=r"/(login|log-?in|sign-?in|signin|my-?account|account|members?|"
                 r"customer|dashboard|portal|register|signup|sign-?up)(/|$)",
         detail="URL path is an auth / account / members route"),
    Rule("gd-members-area", "accounts", "strong",
         data_ux=("login", "loginwidget", "membersarea", "memberarea",
                  "signin", "account", "memberslogin"),
         detail="GoDaddy Members Area / login widget"),
    Rule("auth-form", "accounts", "strong",
         hosts=("accounts.google.com/gsi", "auth0.com", "okta.com",
                "login.microsoftonline.com", "memberstack", "memberspace.com",
                "outseta.com", "wix.com/_api/wix-sm"),
         detail="hosted auth provider embedded"),
    Rule("password-plus-username", "accounts", "moderate",
         needs_password=True, require_all=True,
         text_re=r"\b(sign in|log in|username|email address and password|"
                 r"forgot (your )?password|member login)\b",
         detail="password field alongside login / account-creation copy"),
    Rule("lone-password", "accounts", "weak",
         needs_password=True,
         detail="a password input exists (could be a demo/how-to)"),

    # ---- forum / threaded community -------------------------------
    Rule("forum-software", "forum", "strong",
         hosts=("discourse", "flarum", "nodebb", "vanillaforums.com",
                "muut.com", "vbulletin", "phpbb"),
         classes=("phpbb", "discourse", "flarum", "nodebb", "bbpress",
                  "vanilla-forums"),
         detail="forum software fingerprint"),
    Rule("forum-path", "forum", "strong",
         path_re=r"/(forum|forums|community|discussion|board|topic|thread)(/|$)",
         detail="URL path is a forum / community route"),
    Rule("gd-forum-widget", "forum", "moderate",
         data_ux=("forum", "community", "discussion"),
         detail="GoDaddy community/forum widget"),
    Rule("threaded-comments", "forum", "weak",
         hosts=("disqus.com/embed", "commento", "hyvor",
                "graphcomment", "utteranc.es"),
         detail="third-party comment thread (Disqus/etc.) -- usually fine"),

    # ---- appointments / booking ---------------------------------
    Rule("booking-embed", "booking", "moderate",
         hosts=("calendly.com", "acuityscheduling.com", "app.acuityscheduling",
                "squarespace-scheduling", "youcanbook.me", "simplybook.me",
                "setmore.com", "cal.com/embed", "book.timify.com"),
         detail="scheduling / appointment widget embedded"),
    Rule("gd-appointments", "booking", "moderate",
         data_ux=("appointments", "booking", "bookings", "scheduler"),
         path_re=r"/(book|booking|bookings|appointments?|schedule|reserve)(/|$)",
         detail="GoDaddy Appointments widget / booking route"),

    # ---- donations / fundraising -------------------------------
    Rule("donation-platform", "donation", "moderate",
         hosts=("donorbox.org", "gofundme.com", "givebutter.com",
                "classy.org", "fundly.com", "every.org", "donately.com"),
         data_ux=("donation", "donate", "donatewidget"),
         detail="donation / fundraising widget embedded"),
    Rule("donate-cta", "donation", "weak",
         text_re=r"\b(donate now|make a donation|support our cause)\b",
         detail="donation call-to-action text"),
]

_WEIGHT_ORDER = {"strong": 3, "moderate": 2, "weak": 1}


# --------------------------------------------------------------------------
# Evidence gathering (live page) + matching
# --------------------------------------------------------------------------
_EVIDENCE_JS = r"""
() => {
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return u || ''; } };
  const scripts = [...document.querySelectorAll('script[src]')].map(s => abs(s.getAttribute('src')));
  const form_acts = [...document.querySelectorAll('form[action]')].map(f => abs(f.getAttribute('action')));
  const dux = new Set();
  document.querySelectorAll('[data-ux],[data-aid]').forEach(el => {
    if (el.dataset.ux) dux.add(String(el.dataset.ux).toLowerCase());
    if (el.dataset.aid) dux.add(String(el.dataset.aid).toLowerCase());
  });
  let classes = '';
  document.querySelectorAll('[class]').forEach(el => { classes += ' ' + el.className; });
  const has_password = !!document.querySelector('input[type="password"]');
  const text = (document.body ? document.body.innerText : '').slice(0, 20000);
  return {
    scripts, form_acts,
    data_ux: [...dux],
    classes: classes.toLowerCase().slice(0, 60000),
    has_password,
    text: text.toLowerCase(),
  };
}
"""


def gather_evidence(page, url):
    """Collect raw out-of-scope evidence from a live Playwright page.
    One DOM round-trip; no judgement."""
    try:
        ev = page.evaluate(_EVIDENCE_JS)
    except Exception:
        ev = {"scripts": [], "form_acts": [], "data_ux": [],
              "classes": "", "has_password": False, "text": ""}
    ev["data_ux"] = set(ev.get("data_ux") or [])
    ev["path"] = (urlparse(url).path or "/").lower()
    return ev


def match_signals(ev):
    """Run the rule catalog against an evidence dict. Returns a list of
    {id, category, weight, detail, hits}."""
    ev = dict(ev)
    ev["data_ux"] = set(ev.get("data_ux") or [])
    signals = []
    for rule in RULES:
        hits = rule.match(ev)
        if hits:
            signals.append({
                "id": rule.id, "category": rule.category, "weight": rule.weight,
                "detail": rule.detail, "hits": hits[:4],
            })
    return signals


def verdict(signals):
    """Reduce a signal list to a per-page verdict."""
    if not signals:
        return {"verdict": "pass", "categories": [], "reasons": []}
    top = max(_WEIGHT_ORDER[s["weight"]] for s in signals)
    if top == 3:
        v = "block"
    elif top == 2:
        v = "review"
    else:
        v = "pass"
    cats = sorted({s["category"] for s in signals
                   if _WEIGHT_ORDER[s["weight"]] >= (2 if v != "pass" else 1)})
    reasons = [f"[{s['weight']}] {s['category']}: {s['detail']} ({'; '.join(s['hits'])})"
               for s in sorted(signals, key=lambda s: -_WEIGHT_ORDER[s["weight"]])]
    return {"verdict": v, "categories": cats, "reasons": reasons}


def scan_page(page, url):
    """Full deterministic scan of a live page. Returns
    {evidence, signals, verdict, categories, reasons}. Called by the
    crawler while it holds the DOM."""
    ev = gather_evidence(page, url)
    signals = match_signals(ev)
    v = verdict(signals)
    # Store a compact evidence copy so a later offline re-judge is possible
    # without a re-crawl (drop the big text/classes blobs, keep the facts).
    compact = {
        "scripts": ev["scripts"][:40],
        "form_acts": ev["form_acts"][:20],
        "data_ux": sorted(ev["data_ux"]),
        "has_password": ev["has_password"],
        "path": ev["path"],
        # a short text window is enough for the weak text_re rules
        "text": ev["text"][:4000],
        "classes": "",  # too large to keep; class rules re-run only live
    }
    return {"evidence": compact, "signals": signals, **v}


# --------------------------------------------------------------------------
# Offline re-judge (from stored evidence + extracted content)
# --------------------------------------------------------------------------
def _text_from_blocks(page):
    out = []
    for b in page.get("blocks", []):
        for k in ("text", "heading", "subheading"):
            if b.get(k):
                out.append(re.sub("<[^>]+>", " ", str(b[k])))
        for it in b.get("items", []) or []:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                out.append(" ".join(str(v) for v in it.values() if isinstance(v, str)))
    return " ".join(out).lower()


def qualify_offline(page):
    """Re-judge a page dict without a live browser. Uses the crawl-time
    evidence the crawler stored, plus the extracted block text and the
    URL path. Degrades gracefully (path + content only) when a page has
    no stored evidence -- an older structured_content.json from before
    this agent existed."""
    ev = dict(page.get("_qualification_evidence") or {})
    ev.setdefault("path", (urlparse(page.get("old_url", "")).path or "/").lower())
    if not ev.get("text"):
        ev["text"] = _text_from_blocks(page)
    ev.setdefault("scripts", [])
    ev.setdefault("form_acts", [])
    ev.setdefault("data_ux", [])
    ev.setdefault("classes", "")
    signals = match_signals(ev)
    v = verdict(signals)
    v["evidence_source"] = "crawl" if page.get("_qualification_evidence") else "content-only"
    v["signals"] = signals
    return v


# --------------------------------------------------------------------------
# Optional LLM judgement layer
# --------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are the Qualification Agent in an automated website-migration "
    "pipeline. The pipeline ONLY supports informational / marketing sites: "
    "pages, blog posts, FAQs, contact forms, knowledge-base articles, PDFs. "
    "It does NOT reproduce: an online store or any payment/checkout flow; a "
    "login / account / members-only area; a forum or threaded community; an "
    "appointment / booking system; a donation / fundraising flow.\n"
    "You are given one page's extracted text. A deterministic DOM scan "
    "already ran; your job is to catch functionality it may have missed by "
    "reading the copy. Judge ONLY what the page itself provides -- a blog "
    "post that discusses passwords, online communities, or e-commerce is "
    "still in scope. Reply with a single JSON object: "
    '{"scope": "in" | "review" | "out", "reason": "<= 30 words"}.'
)

STRUCT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "scope": {"type": "string", "enum": ["in", "review", "out"]},
        "reason": {"type": "string"},
    },
    "required": ["scope", "reason"],
}


def judge_page_llm(client, model, page):
    body = _text_from_blocks(page)[:8000]
    prompt = (
        f"PAGE TITLE: {page.get('title', '')}\n"
        f"URL: {page.get('old_url', '')}\n\n"
        f"EXTRACTED TEXT:\n{body or '(no text extracted)'}"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=1000,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": STRUCT_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return json.loads(text)


# --------------------------------------------------------------------------
# Report + site gate
# --------------------------------------------------------------------------
def site_gate(page_verdicts):
    blocked = [p for p, v in page_verdicts if v["verdict"] == "block"]
    review = [p for p, v in page_verdicts if v["verdict"] == "review"]
    if blocked:
        line = (f"**OUT OF SCOPE — {len(blocked)} page(s) blocked.** The site "
                "has functionality this pipeline does not reproduce; a human "
                "must handle those pages (or the whole site) before go-live.")
    elif review:
        line = (f"**REVIEW REQUIRED — {len(review)} page(s) flagged.** Migrated, "
                "but a human should confirm each is genuinely in scope.")
    else:
        line = "**SITE IN SCOPE for automated migration.** No out-of-scope functionality detected."
    return {"verdict": ("out" if blocked else "review" if review else "in"),
            "blocked": [p["slug"] for p in blocked],
            "review": [p["slug"] for p in review],
            "summary": line}


def build_report(pages, page_verdicts, gate, llm_used):
    L = ["# Qualification Report — pipeline step 3", ""]
    L.append(gate["summary"])
    L.append("")
    L.append(f"- Pages scanned: **{len(pages)}**")
    L.append(f"- Categories checked: {', '.join(CATEGORIES)}")
    L.append(f"- LLM judgement layer: {'ran' if llm_used else 'not run (deterministic only)'}")
    n_pass = sum(1 for _, v in page_verdicts if v["verdict"] == "pass")
    L.append(f"- Verdicts: {n_pass} pass · {len(gate['review'])} review · {len(gate['blocked'])} block")
    L.append("")

    flagged = [(p, v) for p, v in page_verdicts if v["verdict"] != "pass"]
    if flagged:
        L.append("## Pages needing a human look")
        L.append("")
        for p, v in flagged:
            mark = "🚫 BLOCK" if v["verdict"] == "block" else "⚠️ REVIEW"
            L.append(f"### {mark} — `{p['slug']}`  ({p.get('old_url', '')})")
            for r in v["reasons"]:
                L.append(f"- {r}")
            if v.get("llm"):
                L.append(f"- LLM: **{v['llm']['scope']}** — {v['llm']['reason']}")
            L.append("")
    else:
        L.append("Every page passed. Nothing to review.\n")

    weak_notes = [(p, v) for p, v in page_verdicts
                  if v["verdict"] == "pass" and v.get("signals")]
    if weak_notes:
        L.append("## Noticed but judged in scope (weak signals only)")
        L.append("")
        for p, v in weak_notes:
            bits = "; ".join(f"{s['category']}: {s['detail']}" for s in v["signals"])
            L.append(f"- `{p['slug']}` — {bits}")
        L.append("")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true",
                    help="deterministic scan only; skip the LLM layer")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--pages", help="comma-separated slugs to limit to")
    args = ap.parse_args()

    if not os.path.exists(SRC):
        sys.exit(f"{SRC} not found -- run crawler_agent.py first.")
    with open(SRC) as f:
        data = json.load(f)

    only = {s.strip() for s in args.pages.split(",")} if args.pages else None
    pages = [p for p in data.get("pages", []) if not only or p.get("slug") in only]

    client = None
    if not args.offline:
        try:
            import anthropic
            client = anthropic.Anthropic()
            client.models.list(limit=1)
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "anthropic" in msg and "not installed" in msg:
                print("  [note] `anthropic` not installed -- deterministic scan only.")
            elif "auth" in msg or "api_key" in msg or "credential" in msg:
                print("  [note] no Anthropic credentials -- deterministic scan only. "
                      "(Run the LLM judgement in-session instead; see module docstring.)")
            else:
                print(f"  [note] LLM layer unavailable ({e}); deterministic scan only.")
            client = None

    page_verdicts = []
    any_content_only = False
    for p in pages:
        v = qualify_offline(p)
        if v.get("evidence_source") == "content-only":
            any_content_only = True
        if client is not None and v["verdict"] == "pass":
            try:
                lj = judge_page_llm(client, args.model, p)
                v["llm"] = lj
                if lj["scope"] == "out":
                    v["verdict"] = "block"
                    v["reasons"].append(f"[llm] {lj['reason']}")
                elif lj["scope"] == "review":
                    v["verdict"] = "review"
                    v["reasons"].append(f"[llm] {lj['reason']}")
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] LLM judge failed for {p.get('slug')}: {e}")
        p["_qualification"] = {k: v[k] for k in ("verdict", "categories", "reasons")}
        page_verdicts.append((p, v))

    gate = site_gate(page_verdicts)
    data["qualification"] = {
        "gate": gate["verdict"],
        "summary": gate["summary"],
        "blocked": gate["blocked"],
        "review": gate["review"],
        "scanned": len(pages),
        "llm_layer": client is not None,
        "evidence": "content-only (re-crawl for a full DOM scan)" if any_content_only else "crawl",
    }
    # keep the legacy key the generator already reads: block verdicts only
    data["qualification_flags"] = {
        p.get("old_url", p.get("slug")): v["reasons"]
        for p, v in page_verdicts if v["verdict"] == "block"
    }

    report = build_report(pages, page_verdicts, gate, client is not None)
    print(report)
    print(gate["summary"])
    if any_content_only:
        print("\n(note: this structured_content.json predates crawl-time evidence "
              "capture -- judged from URL paths + extracted text only. Re-crawl "
              "for the full DOM scan.)")

    if args.dry_run:
        print("\n--dry-run: not writing.")
        return
    with open(OUT_REPORT, "w") as f:
        f.write(report)
    with open(SRC, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nWrote {OUT_REPORT} and updated {SRC}.")


if __name__ == "__main__":
    main()
