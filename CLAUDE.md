# GoDaddy-to-WordPress Automated Migration Pipeline

## What this project is

Carver (Principal Consultant, Stratecon Tech Advisors) is building a service that
automatically migrates small/medium GoDaddy Website Builder sites to WordPress,
using a team of AI agents to eliminate as much manual labor as possible. Packages
are priced by page count + detected features (payments, blogs, forums, knowledge
bases). **Current scope: informational sites only** — no ecommerce, no
login/account areas, no forums. Those are explicitly out of scope for now.

The test site is Carver's own: **stratecon.tech**, a ~16-page GoDaddy Website
Builder site (confirmed via its `meta-generator` tag) with a nested nav (AI /
Communications / Cybersecurity solution areas, each with sub-pages), a blog, an
FAQ accordion, a contact form, and a newsletter signup. No payments, no logins —
a clean in-scope example.

## The agent pipeline (design, from prior planning)

1. **Crawler Agent** (scripted) — headless browser, discovers + renders every page.
2. **Extraction Agent** (scripted) — pulls clean content per page into structured JSON.
3. **Qualification Agent** (LLM judgment) — flags anything out of scope (payments,
   logins, forums) before it's processed further. *This is the safety gate that
   makes "fully automated" an honest claim.*
4. **Brand Agent** (scripted) — extracts colors/fonts/logo from computed CSS.
5. **Content Structuring Agent** (LLM — Claude) — cleans raw extracted content into
   WP-ready structured blocks, generates meta titles/descriptions/alt text.
6. **Architecture Agent** (LLM + rules) — maps page inventory + nav into a WP
   page/menu hierarchy.
7. **Generator Agent** (scripted) — assembles a WXR file, Gutenberg block markup,
   theme tokens, and a plugin manifest.
8. **Redirect Agent** (scripted) — old-URL → new-URL map, exportable as CSV for
   the Redirection plugin.
9. **QA Agent** (scripted + LLM summary) — diffs old vs new, writes a plain-English
   report.
10. **Concierge Agent** (LLM) — the only agent-facing interface a non-technical
    client sees; translates everything above into plain language.

## Key technical decisions already made

- **Gutenberg blocks, not Divi**, for the automated-generation tier. Divi's
  shortcode format isn't reliably scriptable; Gutenberg's block markup is. Divi
  (or another builder) can stay an option for the higher-touch/custom tier where
  a human is building anyway — Carver is not married to any one builder; the
  automation is what matters, not the tool.
- **WXR (WordPress eXtended RSS)** is the output format for pages/posts — WordPress's
  native, stable import format.
- Generated pages import as **drafts**, never auto-published — human reviews before
  go-live, at least in this early phase.
- Redirects are generated as a CSV for the **Redirection** plugin.

## What's already built and proven (in this folder)

- `structured_content.json` — Content Structuring Agent output for 4 real pages
  (Home, About, Services, Contact), manually built from real fetched content in
  the planning conversation. **12 more pages are marked `not_yet_extracted`** in
  the nav section — the sub-pages under AI/Communications/Cybersecurity, plus
  the Blog.
- `generator_agent.py` — real, tested. Converts `structured_content.json` into
  `stratecon-migration.xml` (valid WXR, verified by parsing it), `redirects.csv`,
  and `qa_report.md`. Run it again on richer input and it scales.
- `crawler_agent.py` — **written but NOT yet run against a real site**. It compiles
  cleanly (verified with `py_compile`) but was built in a sandboxed environment
  with no internet access, so it's untested against real-world HTML. Expect
  issues on first real run — that's normal, not a design failure. It uses
  Playwright, does headless-browser rendering (needed since GoDaddy Website
  Builder pages are JS-rendered), and outputs directly into the same JSON schema
  `generator_agent.py` consumes. It also runs a first-pass Qualification Agent
  (regex-based) that flags/skips pages matching payment/login/forum patterns.

## Immediate next step

1. Install dependencies: `pip install playwright && playwright install chromium`
2. Run: `python3 crawler_agent.py https://stratecon.tech`
3. Debug whatever breaks — first crawls against a real site reliably surface
   issues (accordion markup, form detection, pagination, etc. can vary from what
   the extraction heuristics expect).
4. Feed the resulting `structured_content.json` into `generator_agent.py` and
   verify the output WXR file covers the whole site, not just the original 4 pages.
5. Once that works end to end for stratecon.tech, the next gaps to close (not yet
   built): image download/re-hosting, brand/style (CSS) extraction, and the
   Qualification Agent needs to move from regex heuristics to something more
   robust before this is trusted on client sites it hasn't been tuned against.

## Open questions / things flagged for review, not yet resolved

- Contact form's exact fields weren't fully visible in extracted content — needs
  confirmation against the live site before any real migration goes live.
- Newsletter signup is mapped to a placeholder shortcode — needs to be wired to
  whatever email tool the new site will actually use.
- No image handling built yet at all.
