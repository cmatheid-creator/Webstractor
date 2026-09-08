# Migration QA Report — Trusted Technology Advisers | Cybersecurity Solutions

Generated: 2026-09-08 16:23 UTC

## Summary

- **37 pages** fully extracted, structured, and converted to a ready-to-import WordPress file.
- **0 payment, login, or account features detected** on the pages processed — consistent with an informational-site profile.

## Items flagged for human review before go-live

- **Homepage**: the front page imports as a normal page — titled "Trusted Technology Advisers | Cybersecurity Solutions", slug `home`. Which page WordPress shows at `/` is a site option (Settings → Reading), not page content, so no WXR import can set it. **The Stratecon Migration Repair plugin sets it for you** — upload **`repair-migration.zip`** via Plugins → Add New → Upload Plugin and click Activate — it runs once, shows a report, then deactivates itself; or set it by hand via Settings → Reading → "Your homepage displays" → a static page. Skip both and `/` shows the default blog listing. (Publish the imported pages first — the plugin can only point `/` at a page that exists.)
- **Hero section** (1 page(s), incl. the home page): the heading, sub-tagline, and call-to-action button were lifted from GoDaddy's header widget (which is otherwise treated as site chrome) and rebuilt as a full-width cover block — this is the migrated home page's only page-level `<h1>`. The background is a GoDaddy stock photo with no importable URL; the Stratecon Migration Repair plugin sideloads it with the rest of the stock images. Sanity-check the wording and the CTA target.
- **Contact form fields** (4 page(s)): the exact fields on the live contact form weren't fully visible in the extracted content. The generated page includes a placeholder form block — confirm the real field set before publishing.
- **Newsletter signup** (17 page(s)): mapped to a placeholder shortcode. Needs to be wired to whichever email tool (Mailchimp, etc.) the new site will use.
- **Images** (94 unique, 122 placements across the crawled pages): 23 included as WXR attachment items pointing at the original site's URLs. Check **"Download and import file attachments"** during import (the default) so WordPress fetches real, independent copies into your media library. Some of the original site's image URLs carry an extension that doesn't match the actual bytes (a `.webp`/`.png` URL that returns JPEG); WordPress saves those with the correct extension but the importer leaves the page's `<img>` tag pointing at the old one, so it 404s. **The Stratecon Migration Repair plugin repoints every broken `wp-content/uploads/` image URL** at the file WordPress actually created — upload **`repair-migration.zip`** via Plugins → Add New → Upload Plugin and click Activate — it runs once, shows a report, then deactivates itself.
- **Side-by-side layout preserved** (56 section(s)): the original site's two-column image+text sections (detected from its real Grid/GridCell markup) are generated as WordPress Media & Text blocks instead of a plain stacked image and paragraph, matching the original layout rather than flattening it.
- **71 stock image(s) can't ride the WXR import**: their source URLs (the original site's stock-photo CDN) have no filename or extension for the importer's attachment mechanism to accept, just an opaque ID, so the WXR leaves them hotlinked to the old site. **The Stratecon Migration Repair plugin pulls independent copies** (downloads each, sniffs the real image type, then sideloads it) and repoints every occurrence — upload **`repair-migration.zip`** via Plugins → Add New → Upload Plugin and click Activate — it runs once, shows a report, then deactivates itself. Until then they display fine, just served from the old host.
- **Navigation menu** (20 item(s), matching the site's real nav structure including page hierarchy) is included **twice**, in two different WordPress formats, so it works automatically regardless of which kind of theme the target site uses:
  - A classic menu named "Migrated Site Menu" (for classic/hybrid themes — Appearance → Menus, assign it to a menu location).
  - A block-theme navigation entry (`wp_navigation`, also named "Migrated Site Menu") for block themes like Twenty Twenty-Four. This one is wired in automatically (see the header/footer bullet below) — nothing to click for it specifically.
- **Header and footer**: this file also replaces the target theme's own header/footer (currently generated for **twentytwentyfour** — see note below if the target site uses a different block theme) with real ones built from the site's actual content: the header gets the site logo/title plus the migrated nav menu above, already linked by reference — nothing to assign by hand; the footer is rebuilt from the original site's real footer (its own nav links, social icons, and copyright/legal text), not the theme's generic demo footer. This is what the page layout in earlier test imports was missing — WordPress's importer has no way to override a theme's header/footer templates on its own, so without this the pages rendered inside whatever blank/demo chrome the theme shipped with. If the target site is on a **different block theme than twentytwentyfour**, this override won't take (WordPress scopes it to the specific theme) — the header/footer will need to be rebuilt by hand once, or regenerated by changing `THEME_SLUG` in generator_agent.py to match and re-running it.
- **Reviewing the nav before go-live**: pages import as drafts by design (see below) -- and WordPress's Navigation block correctly hides any menu link that points to a page still in draft, the same way it would for any other unpublished page. Confirmed with a full local WordPress + Twenty Twenty-Four reproduction: with only one page published, the nav showed only that page's own branch (e.g. just "AI" > "AI Solutions"); publishing every page made the complete nav -- all top-level items, all category dropdowns, every child link -- render correctly in both the header and footer. This is expected, correct WordPress behavior, not a defect in this file. It also means a *sparse-looking* nav while reviewing in draft isn't a red flag by itself -- it's just reflecting how much of the site is published so far. To see the complete nav before committing to a real go-live, temporarily publish all pages, review, then set them back to Draft if you're not ready to launch. WordPress's own draft-preview mode (`?preview=true`) has also been observed failing to render the Navigation block's menu items at all, even for published targets -- don't trust a preview link's nav either; check a real published URL.
- **FAQ sections rebuilt** (1 page(s)): the GoDaddy accordion renders its questions as toggle controls and its answers in separate panels, so the crawl captured them as loose text. The Content Structuring Agent (pipeline step 5) paired each question with its answer; the generator renders the pairs as a real click-to-expand accordion (`core/details` blocks, collapsed by default, WP 6.7+). Skim the pairings before publishing.
- **SEO title + meta description set on all 37 page(s)** by the Content Structuring Agent (imported as the Yoast `_yoast_wpseo_title` / `_yoast_wpseo_metadesc` fields) — review the wording before go-live.
- **Brand tokens applied automatically**: 7 typography role(s), colors (background: #ffffff, text: #5e5e5e, button_background: #1d2b52, button_text: #fafafa, link: #1d2b52, footer_background: #f6f6f6). The WXR file includes a "Custom Styles" entry (a real WordPress `wp_global_styles` post -- the same object the Site Editor's own Styles panel creates when a person sets colors/fonts by hand) that applies the extracted background, text, link, and button colors plus the body font sitewide on import -- no manual Site Editor configuration needed. Also included as `theme.json`, a standalone theme.json fragment, for reference or for merging into a theme's own theme.json directly.
- **Logo** found at https://img1.wsimg.com/isteam/ip/65839fec-72de-412d-8280-f55f4e3087d0/22a28f51-fa97-43af-906c-309373c738aa.png/:/rs=h:88,cg:true,m/qt=q:95 -- included in the WXR as a real media-library attachment (post_id 40004, also stamped with a `_webstractor_site_logo` marker). Setting it as the site's active logo (the `site_logo` option/`custom_logo` theme mod) isn't something WXR can do on its own -- **the Stratecon Migration Repair plugin does it for you** on activation (upload **`repair-migration.zip`** via Plugins → Add New → Upload Plugin and click Activate — it runs once, shows a report, then deactivates itself); or, from a shell, `php apply_branding.php` does the same. A full site reset wipes this option, so re-run whichever of the two you use after every reset.
- **Brand fonts loaded for real**: `theme.json`/"Custom Styles" only *register* the extracted font-family names -- nothing else fetches the actual font files, so every role using one would otherwise silently fall back to its generic fallback (e.g. Georgia/serif). `php apply_branding.php` (see above) also writes a small must-use plugin that loads the real fonts from Google Fonts on every page, sitewide. Without file access to run that script, WordPress's built-in Font Library (Appearance → Editor → Styles → Typography, WP 6.5+) is the no-code alternative -- but confirmed a real gotcha there: **installing** a font only adds it to the library, each individual weight/style face still needs to be **activated** separately (checked on) before it actually loads. A font showing e.g. "1 of 8 active" in the Fonts screen means only one weight is live -- headings/nav using a different weight will silently fall back to the generic font until every face that role needs is checked on too. Also survives a database reset worse than the must-use-plugin route: Font Library's installed fonts are database entries, wiped by a full reset, and need reinstalling+reactivating afterward -- the must-use plugin is a file on disk that a DB reset doesn't touch.

## Per-page review notes

Formerly emitted as `<!-- QA FLAG -->` HTML comments inside each page's content. They're collected here instead: left in the page body, WordPress's block editor turns every one into a stray "Classic" block on import.

- **Trusted Technology Advisers | Cybersecurity Solutions** (`home`):
  - hero background image still points at the original site (a GoDaddy stock photo with no importable URL) -- the Stratecon Migration Repair plugin sideloads it and repoints this reference.
  - card images still point at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Connectivity** (`connectivity`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **Blog** (`blog`):
  - post preview images still point at the original site -- swap to the re-hosted media-library copy after import.
- **Cybersecurity Insurance: Is It Right for Your SMB?** (`cybersecurity-insurance-is-it-right-for-your-smb`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **AI for Sales** (`ai-for-sales-1`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **Cybersecurity Solutions** (`cybersecurity-solutions`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
  - card images still point at the original site -- swap to the re-hosted media-library copy after import.
  - post preview images still point at the original site -- swap to the re-hosted media-library copy after import.
  - contact form "Free Cybersecurity eBook" (slot 1) -- fields: Name (text), Email (text), Company (text) -- import fluentforms-migration.json (Fluent Forms → Tools → Import Forms), then swap this placeholder for [fluentform id="N"] and add an email notification to the form
- **AI Strategy** (`ai-strategy-1`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **Risk Assessment** (`risk-assessment-1`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **Creating and Testing a Business Continuity/Disaster Recovery Plan** (`creating-and-testing-a-business-continuitydisaster-recovery-plan`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Threat Protection** (`threat-protection`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
  - contact form "FREE CYBERSECURITY EBOOK" (slot 1) -- fields: Name (text), Email (text), Company (text) -- import fluentforms-migration.json (Fluent Forms → Tools → Import Forms), then swap this placeholder for [fluentform id="N"] and add an email notification to the form
- **The Importance of Regular Cybersecurity Audits for SMBs** (`the-importance-of-regular-cybersecurity-audits-for-smbs`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Threat ID & Detection** (`threat-id-%26-detection`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
  - contact form "FREE CYBERSECURITY EBOOK" (slot 1) -- fields: Name (text), Email (text), Company (text) -- import fluentforms-migration.json (Fluent Forms → Tools → Import Forms), then swap this placeholder for [fluentform id="N"] and add an email notification to the form
- **Services** (`services`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **AI Solutions** (`ai-solutions`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
  - card images still point at the original site -- swap to the re-hosted media-library copy after import.
  - post preview images still point at the original site -- swap to the re-hosted media-library copy after import.
- **Stratecon Tech Advisors - Unified Communications, Contact Center, Workforce Management** (`communications-solutions`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
  - card images still point at the original site -- swap to the re-hosted media-library copy after import.
- **AI and Data Analytics: Uncovering Business Insights for SMBs** (`ai-and-data-analytics-uncovering-business-insights-for-smbs`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Supporting Hybrid Work with Effective Collaboration Tools** (`supporting-hybrid-work-with-effective-collaboration-tools`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Preventing Data Breaches: 8 Essential Strategies for SMBs** (`preventing-data-breaches-8-essential-strategies-for-smbs`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Unified Communications** (`unified-communications`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **Building a Cybersecurity-Aware Culture in Your Business** (`building-a-cybersecurity-aware-culture-in-your-business`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Leveraging AI to Optimize Your Marketing Strategies** (`leveraging-ai-to-optimize-your-marketing-strategies`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Customer Experience** (`customer-experience`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **Cybersecurity Compliance for SMBs: What You Need to Know** (`cybersecurity-compliance-for-smbs-what-you-need-to-know`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Managing Communications in a Multi-Generational Workforce** (`managing-communications-in-a-multi-generational-workforce`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Leveraging AI for Enhanced Customer Service** (`leveraging-ai-for-enhanced-customer-service`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **Stratecon Tech Advisors | Technology Advisory Services** (`about`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **10 Essential Cybersecurity Practices for SMBs in 2024** (`10-essential-cybersecurity-practices-for-smbs-in-2024`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **IT Advisor Services** (`contact`):
  - contact form "Contact Us" (slot 2) -- fields: Name (text), Email (text), Message (textarea), Email opt-in (checkbox) -- import fluentforms-migration.json (Fluent Forms → Tools → Import Forms), then swap this placeholder for [fluentform id="N"] and add an email notification to the form
  - newsletter signup -- wire to the real email/newsletter plugin
- **AI for Customer Service** (`ai-for-customer-service`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **Cyber Risk Assessment** (`cyber-risk-assessment`):
  - image still points at the original site -- swap to the re-hosted media-library copy after import.
- **Top 5 Security Considerations When Utilizing Generative AI** (`top-5-security-considerations-when-utilizing-generative-ai`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **AI Use Policy Template** (`ai-use-policy-template`):
  - "Stratecon%20Tech%20Advisors_Company%20AI%20Use%20Policy%20.pdf" is linked straight from the old site's CDN -- download it, add it to the Media Library, and repoint this button before go-live. The original page showed it in an in-page PDF viewer; a viewer/embed block can be added if that presentation matters.
- **AI Disclosure Template** (`ai-disclosure-template`):
  - "Stratecon%20Tech%20Advisors_AI%20Vendor%20Disclosure%20S.pdf" is linked straight from the old site's CDN -- download it, add it to the Media Library, and repoint this button before go-live. The original page showed it in an in-page PDF viewer; a viewer/embed block can be added if that presentation matters.
- **The Spider-Man* Dilemma: Building an AI Strategy** (`the-spider-mantm-dilemma-building-an-ai-strategy`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin
- **How AI Can Transform Your Small to Medium Business** (`how-ai-can-transform-your-small-to-medium-business`):
  - still points at the original site -- swap to the re-hosted media-library copy after import.
  - newsletter signup -- wire to the real email/newsletter plugin

## What's in the attached files

- `stratecon-migration.xml` — import via **Tools → Import → WordPress** on any WordPress site (install the free WordPress Importer plugin if prompted). Pages import as **drafts** so nothing goes live automatically.
- `redirects.csv` — import into the free **Redirection** plugin to preserve old URLs once the new site goes live.
- `repair-migration.zip` — the **Stratecon Migration Repair** plugin. After importing and publishing the pages, upload **`repair-migration.zip`** via Plugins → Add New → Upload Plugin and click Activate — it runs once, shows a report, then deactivates itself. Repoints broken re-hosted image URLs at the file WordPress actually saved, pulls media-library copies of the stock images the importer couldn't, sets the static front page, and sets the site logo. No server/shell access needed; safe to activate again — re-run it after any full site reset, which wipes the front-page and logo settings. (A shell, where available, can instead run `php wp-content/plugins/repair-migration/repair-migration.php` directly.)
- `theme.json` — the extracted color palette and font list in WordPress's block-theme format.
- `apply_branding.php` — the shell-based equivalent of the repair plugin's logo step plus brand-font loading (`php apply_branding.php` from the WordPress root), for hosts where a shell is available. Run once after each fresh import; see the notes above.
