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
import os
import re
import hashlib
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen
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

# {page_slug: [note, ...]} -- collected by build_item_xml() as it pulls
# every "<!-- QA FLAG: ... -->" comment back out of a page's generated
# block markup, and rendered as a per-page section in build_qa_report().
# Those comments must NOT stay in post_content: WordPress's block parser
# turns each comment sitting between two top-level blocks into its own
# core/freeform (Classic) block on import -- confirmed against the real
# dev site, every page carried one Classic block per QA-flag comment.
# The QA report is where a human reviewer looks for this anyway.
_QA_NOTES = {}

# Recognized inline QA-flag comment, e.g.
#   <!-- QA FLAG: card images still point at the original site ... -->
# Comment bodies never contain ">" so [^>] is a safe, greedy-free match.
QA_FLAG_COMMENT_RE = re.compile(r"[ \t]*<!-- QA FLAG:\s*(?P<note>[^>]*?)\s*-->\n?")


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
OUT_FF = "fluentforms-migration.json"

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


def _role_style_bits(role, brand, include_color=True):
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

    include_color=False drops the textColor attribute/class -- for a role
    rendered against a background the brand color wasn't chosen for (the
    hero cover's dark overlay, where the HeadingAlpha navy would be all
    but invisible), where the light text color is supplied by a scoped
    CSS rule instead.
    """
    hs = _brand_role_style(role, brand)
    if not hs:
        return None
    json_attrs = []
    classes = []
    if hs.get("font_family_slug"):
        json_attrs.append(f'"fontFamily":"{hs["font_family_slug"]}"')
        classes.append(f'has-{hs["font_family_slug"]}-font-family')
    if include_color and hs.get("text_color_slug"):
        json_attrs.append(f'"textColor":"{hs["text_color_slug"]}"')
        classes.append(f'has-{hs["text_color_slug"]}-color has-text-color')
    if hs.get("font_size") or hs.get("font_weight"):
        rc = role_class_name(role)
        json_attrs.append(f'"className":"{rc}"')
        classes.append(rc)
    return {"json_attrs": ",".join(json_attrs), "classes": " ".join(classes)}


def _form_placeholder(title, description, qa_note, anchor=None):
    """A clearly-labelled, self-explanatory placeholder panel for a form
    the pipeline hasn't wired to a plugin yet. Real Gutenberg blocks only
    -- a bordered group with a heading and an italic note -- never a
    literal `[contact-form-7 ...]` / `[..._form]` shortcode, which renders
    as raw bracket text to visitors on any site without that exact plugin
    installed. The QA note is stripped into qa_report.md like every other
    QA flag.

    When `anchor` is given the group carries it as its HTML id, so the
    migration repair plugin can find and replace this exact panel with a
    real `[fluentform id="N"]` shortcode once it has built the form (see
    build_repair_migration_php()'s Fluent Forms section)."""
    anchor_attr = f',"anchor":"{anchor}"' if anchor else ""
    anchor_id = f' id="{anchor}"' if anchor else ""
    return (
        f'<!-- wp:group {{"className":"migration-form-placeholder"{anchor_attr},'
        '"layout":{"type":"constrained"}} -->\n'
        f'<div class="wp-block-group migration-form-placeholder"{anchor_id}>\n'
        '<!-- wp:heading {"level":3} -->\n'
        f'<h3 class="wp-block-heading">{html.escape(title)}</h3>\n'
        '<!-- /wp:heading -->\n'
        '<!-- wp:paragraph {"className":"migration-form-note"} -->\n'
        f'<p class="migration-form-note"><em>{html.escape(description)}</em></p>\n'
        '<!-- /wp:paragraph -->\n'
        '</div>\n'
        '<!-- /wp:group -->\n'
        f'<!-- QA FLAG: {qa_note} -->'
    )


# ---- Fluent Forms ---------------------------------------------------------
# The migration repair plugin builds a real Fluent Forms form for each
# captured contact_form block (see build_repair_migration_php()). These
# field shapes are lifted verbatim from a real Fluent Forms 6.x export
# (the "Contact Form Demo" template) so the generated form_fields JSON is
# exactly what Fluent Forms itself writes -- no schema guesswork.

_FF_SUBMIT_BUTTON = {
    "uniqElKey": "el_migration_submit",
    "element": "button",
    "attributes": {"type": "submit", "class": ""},
    "settings": {
        "align": "left", "button_style": "default", "container_class": "",
        "help_message": "", "background_color": "#1a7efb", "button_size": "md",
        "color": "#ffffff",
        "button_ui": {"type": "default", "text": "Send", "img_url": ""},
    },
    "editor_options": {"title": "Submit Button"},
}


def _ff_name_field(idx, label):
    return {
        "index": idx, "element": "input_name",
        "attributes": {"name": "names", "data-type": "name-element"},
        "settings": {"container_class": "", "admin_field_label": label or "Name",
                     "conditional_logics": []},
        "fields": {
            "first_name": {"element": "input_text",
                "attributes": {"type": "text", "name": "first_name", "value": "", "id": "",
                               "class": "", "placeholder": "First Name"},
                "settings": {"container_class": "", "label": "First Name", "help_message": "",
                             "visible": True,
                             "validation_rules": {"required": {"value": True, "message": "This field is required"}},
                             "conditional_logics": []},
                "editor_options": {"template": "inputText"}},
            "middle_name": {"element": "input_text",
                "attributes": {"type": "text", "name": "middle_name", "value": "", "id": "",
                               "class": "", "placeholder": "", "required": False},
                "settings": {"container_class": "", "label": "Middle Name", "help_message": "",
                             "error_message": "", "visible": False,
                             "validation_rules": {"required": {"value": False, "message": "This field is required"}},
                             "conditional_logics": []},
                "editor_options": {"template": "inputText"}},
            "last_name": {"element": "input_text",
                "attributes": {"type": "text", "name": "last_name", "value": "", "id": "",
                               "class": "", "placeholder": "Last Name", "required": False},
                "settings": {"container_class": "", "label": "Last Name", "help_message": "",
                             "error_message": "", "visible": True,
                             "validation_rules": {"required": {"value": True, "message": "This field is required"}},
                             "conditional_logics": []},
                "editor_options": {"template": "inputText"}},
        },
        "editor_options": {"title": "Name Fields", "element": "name-fields",
                           "icon_class": "ff-edit-name", "template": "nameFields"},
        "uniqElKey": f"el_migration_name_{idx}",
    }


def _ff_email_field(idx, label):
    return {
        "index": idx, "element": "input_email",
        "attributes": {"type": "email", "name": "email", "value": "", "id": "", "class": "",
                       "placeholder": label or "Email"},
        "settings": {"container_class": "", "label": label or "Email", "label_placement": "",
                     "help_message": "", "admin_field_label": "",
                     "validation_rules": {
                         "required": {"value": True, "message": "This field is required"},
                         "email": {"value": True, "message": "This field must contain a valid email"}},
                     "conditional_logics": []},
        "editor_options": {"title": "Email Address", "icon_class": "ff-edit-email", "template": "inputText"},
        "uniqElKey": f"el_migration_email_{idx}",
    }


def _ff_text_field(idx, label, name, required=False, field_type="text"):
    return {
        "index": idx, "element": "input_text",
        "attributes": {"type": field_type, "name": name, "value": "", "class": "",
                       "placeholder": label},
        "settings": {"container_class": "", "label": label, "label_placement": "",
                     "admin_field_label": label, "help_message": "",
                     "validation_rules": {"required": {"value": required, "message": "This field is required"}},
                     "conditional_logics": {"type": "any", "status": False,
                                            "conditions": [{"field": "", "value": "", "operator": ""}]}},
        "editor_options": {"title": "Simple Text", "icon_class": "ff-edit-text", "template": "inputText"},
        "uniqElKey": f"el_migration_text_{idx}",
    }


def _ff_textarea_field(idx, label, name, required=True):
    return {
        "index": idx, "element": "textarea",
        "attributes": {"name": name, "value": "", "id": "", "class": "",
                       "placeholder": label, "rows": 4, "cols": 2},
        "settings": {"container_class": "", "label": label, "admin_field_label": "",
                     "label_placement": "", "help_message": "",
                     "validation_rules": {"required": {"value": required, "message": "This field is required"}},
                     "conditional_logics": {"type": "any", "status": False,
                                            "conditions": [{"field": "", "value": "", "operator": ""}]}},
        "editor_options": {"title": "Text Area", "icon_class": "ff-edit-textarea", "template": "inputTextarea"},
        "uniqElKey": f"el_migration_textarea_{idx}",
    }


def _ff_checkbox_field(idx, label, name):
    opt = label or "I agree"
    return {
        "index": idx, "element": "input_checkbox",
        "attributes": {"type": "checkbox", "name": name, "value": []},
        "options": {opt: opt},
        "settings": {"container_class": "", "label": label or "Consent", "admin_field_label": "",
                     "description": "", "label_placement": "",
                     "validation_rules": {"required": {"value": False, "message": "This field is required"}},
                     "conditional_logics": [], "layout_class": "",
                     "randomize_options": False, "enable_select_all": False},
        "editor_options": {"title": "Check Box", "element": "input-radio",
                           "icon_class": "ff-edit-checkbox-1", "template": "inputCheckable"},
        "uniqElKey": f"el_migration_checkbox_{idx}",
    }


def build_fluentform_form_fields(fields):
    """Crawler `[{label, type}]` -> a Fluent Forms `form_fields` object
    (fields[] + submitButton). Unmapped/odd fields become a simple text
    input so nothing captured is silently dropped."""
    ff_fields = []
    for i, f in enumerate(fields):
        label = (f.get("label") or "").strip()
        ftype = (f.get("type") or "text").lower()
        low = label.lower()
        name = re.sub(r"[^a-z0-9]+", "_", low).strip("_") or f"field_{i}"
        if ftype == "checkbox":
            ff_fields.append(_ff_checkbox_field(i, label or "Consent", name or "consent"))
        elif ftype == "textarea" or low in ("message", "your message", "comments"):
            ff_fields.append(_ff_textarea_field(i, label or "Message", name or "message"))
        elif ftype == "email" or low in ("email", "email address", "your email"):
            ff_fields.append(_ff_email_field(i, label or "Email"))
        elif low in ("name", "your name", "full name"):
            ff_fields.append(_ff_name_field(i, label or "Name"))
        elif "phone" in low or ftype in ("tel", "phone"):
            ff_fields.append(_ff_text_field(i, label or "Phone", name or "phone", field_type="tel"))
        else:
            ff_fields.append(_ff_text_field(i, label or f"Field {i + 1}", name))
    if not any(x["element"] == "input_email" for x in ff_fields):
        ff_fields.append(_ff_email_field(len(ff_fields), "Email"))
    for j, x in enumerate(ff_fields):
        x["index"] = j
    return {"fields": ff_fields, "submitButton": _FF_SUBMIT_BUTTON}


_FF_FORM_SETTINGS = {
    "confirmation": {"redirectTo": "samePage",
                     "messageToShow": "Thank you for your message. We'll be in touch shortly.",
                     "customPage": None, "samePageFormBehavior": "hide_form", "customUrl": None},
    "restrictions": {
        "limitNumberOfEntries": {"enabled": False, "numberOfEntries": None, "period": "total",
                                 "limitReachedMsg": "Maximum number of entries exceeded."},
        "scheduleForm": {"enabled": False, "start": None, "end": None,
                         "selectedDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                         "pendingMsg": "Form submission is not started yet.",
                         "expiredMsg": "Form submission is now closed."},
        "requireLogin": {"enabled": False, "requireLoginMsg": "You must be logged in to submit the form."},
        "denyEmptySubmission": {"enabled": False, "message": "Sorry, you cannot submit an empty form."}},
    "layout": {"labelPlacement": "top", "helpMessagePlacement": "with_label",
               "errorMessagePlacement": "inline", "cssClassName": "", "asteriskPlacement": "asterisk-right"},
    "delete_entry_on_submission": "no",
    "appendSurveyResult": {"enabled": False, "showLabel": False, "showCount": False},
}


def _ff_signature(block):
    return (
        (block.get("title") or "").strip().lower(),
        tuple((f.get("label", ""), f.get("type", "")) for f in block.get("fields") or []),
    )


def assign_ff_slots(data):
    """Stamp `_ff_slot` on every contact_form block, deduped: identical
    forms (same title + same fields, e.g. the "Free Cybersecurity eBook"
    form that appears on several pages) share one slot number, so the
    Fluent Forms export carries one form for them and every page with
    that form shows the same instructions. Returns {slot: [(page_slug,
    representative_block)]} keyed 1..N in first-seen order."""
    slots = {}
    by_sig = {}
    for page in data.get("pages", []):
        for block in page.get("blocks", []):
            if block.get("type") != "contact_form":
                continue
            sig = _ff_signature(block)
            if sig not in by_sig:
                by_sig[sig] = len(by_sig) + 1
                slots[by_sig[sig]] = {"block": block, "pages": []}
            slots[by_sig[sig]]["pages"].append(page.get("slug", ""))
            block["_ff_slot"] = by_sig[sig]
    return slots


def build_fluentforms_export(data):
    """A Fluent Forms native import file (`fluentforms-migration.json`).
    The client imports it via Fluent Forms -> Tools -> Import Forms -- one
    form per unique captured contact form, its fields already mapped to
    Fluent Forms' own field types. The format and field shapes are lifted
    verbatim from a real Fluent Forms 6.x export, so this is exactly what
    the plugin's own Export produces."""
    slots = assign_ff_slots(data)
    forms = []
    for slot, info in sorted(slots.items()):
        block = info["block"]
        forms.append({
            "title": block.get("title") or f"Migrated Contact Form {slot}",
            "status": "published",
            "appearance_settings": None,
            "form_fields": build_fluentform_form_fields(block.get("fields") or []),
            "has_payment": 0,
            "type": "",
            "conditions": None,
            "form_meta": [
                {"meta_key": "formSettings", "value": json.dumps(_FF_FORM_SETTINGS)},
                {"meta_key": "template_name", "value": "migrated_contact_form"},
            ],
        })
    return json.dumps(forms, indent=2) + "\n"


# ---- end Fluent Forms ---------------------------------------------------


def block_to_gutenberg(block):
    """Turn one structured content block into native Gutenberg block markup."""
    t = block["type"]

    if t == "hero":
        # GoDaddy Website Builder's header-widget hero -- see
        # crawler_agent.py's extract_hero(). Rendered as a full-width
        # core/cover: the background image, a dark overlay in the brand
        # primary color (dimRatio 60), and an inner container with the
        # page's real <h1>, the sub-tagline as a paragraph, and the CTA
        # as a button. Without this block the migrated home page has no
        # hero and no page-level <h1> at all -- it starts cold at its
        # first <h2> section heading.
        #
        # The overlay color is set as a *named* palette color
        # ("overlayColor":"primary"), the most stable core/cover
        # serialization -- no inline style to drift against block
        # validation. ".migration-hero" (see _extra_css_rules()) then
        # backs it with a real CSS rule for the overlay color, the light
        # text color the brand's own navy HeadingAlpha can't provide on a
        # dark overlay, and a min-height -- none of which depend on
        # whether the imported palette registered "primary".
        heading_text = html.escape(block["heading"])
        inner = []

        hb = _role_style_bits(block.get("heading_role"), _BRAND, include_color=False)
        if hb and hb["json_attrs"]:
            h_attrs = f'"level":1,{hb["json_attrs"]}'
            h_classes = f'wp-block-heading {hb["classes"]}'.strip()
        else:
            h_attrs = '"level":1'
            h_classes = "wp-block-heading"
        inner.append(
            f'<!-- wp:heading {{{h_attrs}}} -->\n'
            f'<h1 class="{h_classes}">{heading_text}</h1>\n'
            '<!-- /wp:heading -->'
        )

        subheading = block.get("subheading")
        if subheading:
            sb = _role_style_bits(
                block.get("subheading_role"), _BRAND, include_color=False
            )
            if sb and sb["json_attrs"]:
                attrs_block = f' {{{sb["json_attrs"]}}}'
                p_classes = f' class="{sb["classes"]}"'
            else:
                attrs_block = ""
                p_classes = ""
            inner.append(
                f'<!-- wp:paragraph{attrs_block} -->\n'
                f'<p{p_classes}>{html.escape(subheading)}</p>\n'
                '<!-- /wp:paragraph -->'
            )

        cta = block.get("cta")
        if cta and cta.get("text"):
            slug = href_to_slug(cta.get("href"))
            url = (
                f"{NEW_BASE_URL}/{slug}/"
                if _PAGES_BY_SLUG and slug in _PAGES_BY_SLUG
                else cta.get("href")
            )
            label = html.escape(cta.get("text"))
            url_escaped = xml_escape(url or "#")
            inner.append(
                '<!-- wp:buttons {"layout":{"type":"flex","justifyContent":"center"}} -->\n'
                '<div class="wp-block-buttons">\n'
                '<!-- wp:button -->\n'
                '<div class="wp-block-button"><a class="wp-block-button__link '
                f'wp-element-button" href="{url_escaped}">{label}</a></div>\n'
                '<!-- /wp:button -->\n'
                '</div>\n'
                '<!-- /wp:buttons -->'
            )

        inner_markup = "\n\n".join(inner)

        image = block.get("image")
        if not image or not image.get("src"):
            # No hero image captured -- fall back to a plain centered
            # group so the <h1>/sub-tagline/CTA are still emitted rather
            # than dropped.
            return (
                '<!-- wp:group {"align":"full","className":"migration-hero",'
                '"layout":{"type":"constrained"}} -->\n'
                '<div class="wp-block-group alignfull migration-hero">\n'
                f'{inner_markup}\n'
                '</div>\n'
                '<!-- /wp:group -->'
            )

        src = xml_escape(display_image_url(image["src"]))
        alt = xml_escape(image.get("alt", ""))
        return (
            '<!-- wp:cover {"url":"' + src + '","dimRatio":60,'
            '"overlayColor":"primary","align":"full","className":"migration-hero"} -->\n'
            '<div class="wp-block-cover alignfull migration-hero">\n'
            '<span aria-hidden="true" class="wp-block-cover__background '
            'has-primary-background-color has-background-dim-60 has-background-dim"></span>\n'
            f'<img class="wp-block-cover__image-background" alt="{alt}" src="{src}" '
            'data-object-fit="cover"/>\n'
            '<div class="wp-block-cover__inner-container">\n'
            f'{inner_markup}\n'
            '</div>\n'
            '</div>\n'
            '<!-- /wp:cover -->\n'
            '<!-- QA FLAG: hero background image still points at the original site '
            '(a GoDaddy stock photo with no importable URL) -- the Stratecon Migration '
            'Repair plugin sideloads it and repoints this reference. -->'
        )

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
        # Real click-to-expand accordion via core/details (WP 6.7+),
        # collapsed by default (no `open` attr). Restores the interaction
        # of the GoDaddy FAQ accordion the Content Structuring Agent
        # flattened into paired question/answer text.
        parts = []
        for item in block["items"]:
            q = html.escape(item["q"])
            a = html.escape(item["a"])
            parts.append(
                '<!-- wp:details {"className":"migration-faq-item"} -->\n'
                f'<details class="wp-block-details migration-faq-item"><summary>{q}</summary>\n'
                '<!-- wp:paragraph -->\n'
                f'<p>{a}</p>\n'
                '<!-- /wp:paragraph -->\n'
                '</details>\n'
                '<!-- /wp:details -->'
            )
        return "\n\n".join(parts)

    if t == "newsletter_signup":
        label = block.get("label") or "Newsletter signup"
        desc = (block.get("text") or "").strip()
        return _form_placeholder(
            label,
            (desc + " " if desc else "")
            + "Newsletter signup — connect this to the site's email/newsletter "
            "tool before go-live.",
            "newsletter signup -- wire to the real email/newsletter plugin",
        )

    if t == "contact_form":
        fields = block.get("fields") or []
        if fields:
            flist = ", ".join(
                f"{f.get('label') or 'field'} ({f.get('type', 'text')})" for f in fields
            )
        else:
            flist = block.get("note") or "fields not captured from the live site"
        slot = block.get("_ff_slot")
        anchor = f"migration-ff-{slot}" if slot else None
        return _form_placeholder(
            block.get("title") or "Contact form",
            f"Contact form — captured fields: {flist}. This form is in "
            "fluentforms-migration.json: import it via Fluent Forms → Tools → "
            "Import Forms, then replace this block with the form's "
            "[fluentform id=\"…\"] shortcode.",
            f"contact form \"{block.get('title', '')}\" (slot {slot}) -- fields: "
            f"{flist} -- import fluentforms-migration.json (Fluent Forms → Tools "
            "→ Import Forms), then swap this placeholder for [fluentform id=\"N\"] "
            "and add an email notification to the form",
            anchor=anchor,
        )

    if t == "forms_detected":
        # Legacy block shape (older structured_content.json); newer crawls
        # emit contact_form / newsletter_signup instead. Same clean
        # placeholder panel either way -- never a literal shortcode.
        parts = []
        for i, fields in enumerate(block.get("forms", []), 1):
            field_desc = ", ".join(
                f"{f.get('name') or '(unnamed)'} ({f.get('type', '')})" for f in fields
            ) or "no fields detected"
            parts.append(_form_placeholder(
                "Form",
                f"Form — fields: {field_desc}. Connect this to the site's form "
                "plugin before go-live.",
                f"form {i} on this page had fields: {field_desc} -- confirm "
                "against the live site and wire to the real form plugin",
            ))
        return "\n\n".join(parts)

    if t == "document_embed":
        # A GoDaddy "PDF" widget (see crawler_agent.py's
        # extract_pdf_widget()) -- the page's content is a downloadable
        # multi-page PDF shown in an in-page viewer. Rendered as a
        # heading + intro + a real Download button pointing at the file on
        # the old CDN (it stays reachable), flagged for a manual re-host
        # into the media library before go-live.
        title = html.escape(block.get("title") or "Document")
        heading = block.get("heading")
        desc = block.get("description")
        url = xml_escape(block.get("url") or "#")
        fname = block.get("filename") or "the document"
        # Level 1: on this site a document_embed is the whole page (the
        # GoDaddy PDF widget), so its title is the page's only real
        # heading -- without <h1> the migrated page has none.
        parts = [
            '<!-- wp:heading {"level":1} -->\n'
            f'<h1 class="wp-block-heading">{title}</h1>\n'
            '<!-- /wp:heading -->'
        ]
        if heading:
            parts.append(
                '<!-- wp:paragraph -->\n'
                f'<p><strong>{html.escape(heading)}</strong></p>\n'
                '<!-- /wp:paragraph -->'
            )
        if desc:
            parts.append(
                '<!-- wp:paragraph -->\n'
                f'<p>{html.escape(desc)}</p>\n'
                '<!-- /wp:paragraph -->'
            )
        parts.append(
            '<!-- wp:buttons -->\n'
            '<div class="wp-block-buttons">\n'
            '<!-- wp:button -->\n'
            '<div class="wp-block-button"><a class="wp-block-button__link '
            f'wp-element-button" href="{url}" download>Download PDF</a></div>\n'
            '<!-- /wp:button -->\n'
            '</div>\n'
            '<!-- /wp:buttons -->\n'
            f'<!-- QA FLAG: "{fname}" is linked straight from the old site\'s CDN -- '
            'download it, add it to the Media Library, and repoint this button before '
            'go-live. The original page showed it in an in-page PDF viewer; a '
            'viewer/embed block can be added if that presentation matters. -->'
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

        # Wrap the cards in rows of at most 3 columns -- matching the live
        # site (which lays 6 cards out 2x3) and post_feed below. A single
        # wp:columns row of 6 renders as 6 skinny columns: core/columns is
        # a non-wrapping flex row on desktop, so 6 children each declared
        # flex-basis:33.33% just shrink to share one line. Explicit
        # blockGap ("migration-columns-gap") widens the gutter to match
        # the live card row -- see the column fix above for why via
        # className.
        row_blocks = []
        for i in range(0, len(column_blocks), 3):
            columns_content = "\n\n".join(column_blocks[i:i + 3])
            row_blocks.append(
                '<!-- wp:columns {"align":"wide","className":"migration-columns-gap"} -->\n'
                '<div class="wp-block-columns alignwide migration-columns-gap">\n'
                f"{columns_content}\n"
                '</div>\n'
                '<!-- /wp:columns -->'
            )
        return (
            "\n\n".join(row_blocks)
            + "\n\n<!-- QA FLAG: card images still point at the original site -- "
            "swap to the re-hosted media-library copy after import. -->"
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
        # in rows of 2, matching the live site's "Insights" grid (its
        # section cards are 3-up, its post-preview grid is 2-up).
        posts = block.get("posts", [])
        row_blocks = []
        any_images = False
        # A section heading over the grid ("Cybersecurity Insights" /
        # "AI Insights" on the live landing pages), rendered the same way
        # as the page's other section headings (HeadingBeta + dividers).
        feed_heading = (block.get("heading") or "").strip()
        if feed_heading:
            row_blocks.append(block_to_gutenberg({
                "type": "heading", "text": feed_heading, "level": 2,
                "typography_role": "HeadingBeta",
            }))
        for i in range(0, len(posts), 2):
            column_blocks = []
            for post in posts[i:i + 2]:
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
                    # "Continue Reading" follows the excerpt naturally --
                    # confirmed against the live "Insights" grid, which
                    # does NOT pin it to the bottom of the card. The
                    # leftover space in an equal-height card sits below
                    # it, inside the card border (added by
                    # "migration-post-feed-card"), reading as card padding
                    # rather than a stray gap.
                    parts.append(
                        '<!-- wp:paragraph -->\n'
                        f'<p><a href="{url_escaped}">Continue Reading</a></p>\n'
                        '<!-- /wp:paragraph -->'
                    )

                # "migration-flex-column-half": explicit 50% width + flex
                # column (2-up grid), same "keep a lone trailing card the
                # same width" reasoning as card_group's 33.33% variant.
                # "migration-post-feed-card": the white background + 1px
                # #e2e2e2 border + padding the live "Insights" cards have
                # -- without it, the whitespace under a short card's text
                # (the cards are equal height) looked like a random gap
                # instead of card padding.
                column_content = "\n\n".join(parts)
                column_blocks.append(
                    '<!-- wp:column {"className":"migration-flex-column-half migration-post-feed-card"} -->\n'
                    f'<div class="wp-block-column migration-flex-column-half migration-post-feed-card">\n{column_content}\n</div>\n'
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
    slug = page["slug"]
    blocks_md = "\n\n".join(block_to_gutenberg(b) for b in page["blocks"])

    # Pull every "<!-- QA FLAG: ... -->" comment out of the block markup
    # and into the QA report (see _QA_NOTES). Left in post_content, each
    # one sitting between two top-level blocks becomes its own core/
    # freeform (Classic) block on import.
    notes = [m.group("note").strip() for m in QA_FLAG_COMMENT_RE.finditer(blocks_md)]
    if notes:
        _QA_NOTES.setdefault(slug, []).extend(notes)
        blocks_md = QA_FLAG_COMMENT_RE.sub("", blocks_md)
        blocks_md = re.sub(r"\n{3,}", "\n\n", blocks_md).strip()

    title = xml_escape(clean_title(page["title"]))
    meta_desc = xml_escape(page.get("meta_description", ""))
    # meta_title is set by the Content Structuring Agent (pipeline step 5);
    # absent on a raw crawl. Emitted as Yoast's SEO-title postmeta so it
    # doesn't disturb the WordPress page title / nav label (which stay
    # `title`).
    meta_title = xml_escape(page.get("meta_title", ""))
    meta_title_postmeta = (
        f"""
    <wp:postmeta>
      <wp:meta_key><![CDATA[_yoast_wpseo_title]]></wp:meta_key>
      <wp:meta_value><![CDATA[{meta_title}]]></wp:meta_value>
    </wp:postmeta>"""
        if meta_title
        else ""
    )
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
    </wp:postmeta>{meta_title_postmeta}
  </item>"""


def collect_unique_images(pages):
    """Dedupe image blocks across all pages and return {canonical_url:
    alt} pairs, one entry per real underlying photo. Covers plain
    "image" blocks, the image half of a "media_text" side-by-side pair,
    a "hero" block's background image, each card's image within a
    "card_group", and each post's thumbnail within a "post_feed" -- all
    carry a real image that needs its own WXR attachment item (or, when
    the URL has no importable form, a sideload by the Stratecon Migration
    Repair plugin).

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
            elif block["type"] == "hero":
                image = block.get("image")
                if image and image.get("src"):
                    url = display_image_url(image["src"])
                    if url not in images:
                        images[url] = image.get("alt", "")
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


def build_attachment_item_xml(url, alt, attachment_id, is_site_logo=False):
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

    # A stable marker on the logo attachment. The classic WP Importer
    # normally honours the WXR wp:post_id (via wp_insert_post's import_id),
    # so LOGO_ATTACHMENT_ID survives a fresh import -- but on a non-empty
    # DB, or a re-import over existing rows, that ID can be taken and the
    # logo lands on some other auto-increment id. This postmeta lets the
    # repair plugin find the logo attachment by identity, not by a guessed
    # id or by fuzzy alt-text matching.
    logo_meta = (
        "\n    <wp:postmeta>\n"
        "      <wp:meta_key><![CDATA[_webstractor_site_logo]]></wp:meta_key>\n"
        "      <wp:meta_value><![CDATA[1]]></wp:meta_value>\n"
        "    </wp:postmeta>"
        if is_site_logo
        else ""
    )

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
    </wp:postmeta>{logo_meta}
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
    _QA_NOTES.clear()  # per-run; build_qa_report() reads what this run collects

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

    # The site logo, at a fixed post_id (see LOGO_ATTACHMENT_ID) and with a
    # `_webstractor_site_logo` marker so the repair plugin (and
    # build_apply_branding_php()) can reference it directly without any
    # fuzzy matching-by-URL after import. WXR has no mechanism to set the
    # site_logo option/custom_logo theme mod itself -- the repair plugin's
    # step 4 does that on activation; apply_branding.php does it from a shell.
    logo = (brand or {}).get("logo") or {}
    if logo.get("url") and canonical_attachment_url(logo["url"]):
        items_xml.append(
            build_attachment_item_xml(
                logo["url"], logo.get("alt", ""), LOGO_ATTACHMENT_ID, is_site_logo=True
            )
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
        new = f"/{page['slug']}/" if not page.get("is_front_page") else "/"
        lines.append(f"{page['old_url']},{new}")
        # A GoDaddy blog post is served under every section that links to
        # it (/blog/f/x, /ai-solutions/f/x, ...); the crawler records the
        # non-canonical prefixes as alias_urls. The new site only knows
        # the canonical one, so 301 each alias to the post as well.
        for alias in page.get("alias_urls") or []:
            lines.append(f"{alias},{new}")
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
    faq_clean_count = count_blocks("faq")
    newsletter_count = count_blocks("newsletter_signup")
    contact_form_count = count_blocks("contact_form")
    hero_count = count_blocks("hero")

    # Content Structuring Agent (pipeline step 5) coverage. meta_title is
    # only ever set by step 5, so it's the honest "did step 5's LLM pass
    # run" signal; meta_description can also carry a weak value straight
    # from the crawl. Alt text absent here is an accessibility gap step 5
    # is meant to close.
    meta_title_count = sum(1 for p in pages if (p.get("meta_title") or "").strip())
    images_missing_alt = []
    for p in pages:
        for b in p["blocks"]:
            t = b["type"]
            if t in ("image", "media_text") and b.get("src") and not (b.get("alt") or "").strip():
                images_missing_alt.append(p["slug"])
            elif t == "hero" and (b.get("image") or {}).get("src") and not (b["image"].get("alt") or "").strip():
                images_missing_alt.append(p["slug"])
            elif t == "card_group":
                for c in b.get("cards", []) or []:
                    im = c.get("image") or {}
                    if im.get("src") and not (im.get("alt") or "").strip():
                        images_missing_alt.append(p["slug"])

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

    qual = data.get("qualification") or {}
    review_slugs = list(qual.get("review") or [])
    if flags:
        lines.append(
            f"- **Qualification gate: {len(flags)} page(s) BLOCKED** as out of scope "
            "(store/payment, login/account, forum, booking, or donation functionality "
            "this pipeline does not reproduce) and left out of this file — see below."
        )
    elif review_slugs:
        lines.append(
            f"- **Qualification gate: site in scope, {len(review_slugs)} page(s) flagged "
            f"for review** ({', '.join(review_slugs)}) — migrated, but confirm each is "
            "genuinely informational before go-live."
        )
    else:
        lines.append(
            "- **Qualification gate: site in scope.** No store/payment, login/account, "
            "forum, booking, or donation functionality detected on any page — an "
            "informational-site profile." if qual else
            "- **0 payment, login, or account features detected** on the pages processed "
            "— consistent with an informational-site profile."
        )
    lines.append("")
    lines.append("## Items flagged for human review before go-live")
    lines.append("")
    front_page = next((p for p in pages if p.get("is_front_page")), None)
    if front_page:
        lines.append(
            f"- **Homepage**: the front page imports as a normal page — titled "
            f"\"{clean_title(front_page['title'])}\", slug `{front_page['slug']}`. Which page "
            f"WordPress shows at `/` is a site option (Settings → Reading), not page content, "
            f"so no WXR import can set it. **The {REPAIR_PLUGIN_NAME} plugin sets it for "
            f"you** — {REPAIR_HOWTO}; or set it by hand via Settings → Reading → \"Your "
            f"homepage displays\" → a static page. Skip both and `/` shows the default blog "
            f"listing. (Publish the imported pages first — the plugin can only point `/` at a "
            f"page that exists.)"
        )
    if hero_count:
        lines.append(
            f"- **Hero section** ({hero_count} page(s), incl. the home page): the "
            "heading, sub-tagline, and call-to-action button were lifted from GoDaddy's "
            "header widget (which is otherwise treated as site chrome) and rebuilt as a "
            "full-width cover block — this is the migrated home page's only page-level "
            "`<h1>`. The background is a GoDaddy stock photo with no importable URL; "
            f"the {REPAIR_PLUGIN_NAME} plugin sideloads it with the rest of the stock "
            "images. Sanity-check the wording and the CTA target."
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
            "independent copies into your media library. Some of the original site's image "
            "URLs carry an extension that doesn't match the actual bytes (a `.webp`/`.png` "
            "URL that returns JPEG); WordPress saves those with the correct extension but the "
            f"importer leaves the page's `<img>` tag pointing at the old one, so it 404s. "
            f"**The {REPAIR_PLUGIN_NAME} plugin repoints every broken `wp-content/uploads/` "
            f"image URL** at the file WordPress actually created — {REPAIR_HOWTO}."
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
                f"- **{len(non_importable_images)} stock image(s) can't ride the WXR import**: "
                "their source URLs (the original site's stock-photo CDN) have no filename or "
                "extension for the importer's attachment mechanism to accept, just an opaque "
                "ID, so the WXR leaves them hotlinked to the old site. "
                f"**The {REPAIR_PLUGIN_NAME} plugin pulls independent copies** (downloads "
                "each, sniffs the real image type, then sideloads it) and repoints every "
                f"occurrence — {REPAIR_HOWTO}. Until then they display fine, just served from "
                "the old host."
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
    if faq_clean_count:
        lines.append(
            f"- **FAQ sections rebuilt** ({faq_clean_count} page(s)): the GoDaddy accordion "
            "renders its questions as toggle controls and its answers in separate panels, so "
            "the crawl captured them as loose text. The Content Structuring Agent (pipeline "
            "step 5) paired each question with its answer; the generator renders the pairs as "
            "a real click-to-expand accordion (`core/details` blocks, collapsed by default, "
            "WP 6.7+). Skim the pairings before publishing."
        )
    if meta_title_count == extracted:
        lines.append(
            f"- **SEO title + meta description set on all {extracted} page(s)** by the Content "
            "Structuring Agent (imported as the Yoast `_yoast_wpseo_title` / "
            "`_yoast_wpseo_metadesc` fields) — review the wording before go-live."
        )
    elif meta_title_count:
        lines.append(
            f"- **SEO titles set on {meta_title_count}/{extracted} page(s)**; the rest fall "
            "back to the page title. Re-run the Content Structuring Agent (step 5, needs an "
            "API key) to complete them, or set them per page in Yoast."
        )
    else:
        lines.append(
            "- **SEO titles / descriptions not generated yet**: the Content Structuring Agent "
            "(pipeline step 5) writes a per-page meta title and 150–160-char description but "
            "needs an Anthropic API key to run. Until it does, each page uses its own title "
            "and whatever `<meta name=\"description\">` the crawl captured. Run step 5, or set "
            "the fields in Yoast per page."
        )
    if images_missing_alt:
        uniq = sorted(set(images_missing_alt))
        lines.append(
            f"- **{len(images_missing_alt)} image(s) with no alt text** on {len(uniq)} page(s) "
            f"({', '.join(uniq)}): the crawl found no alt attribute. The Content Structuring "
            "Agent's image pass (step 5, needs an API key) writes literal alt text from the "
            "image itself; until it runs, add alt text by hand for accessibility."
        )
    if flags:
        lines.append(f"- **{len(flags)} page(s) excluded** by the qualification check (out of scope):")
        for url, reasons in flags.items():
            lines.append(f"  - {url} — {'; '.join(reasons)}")
    review_pages = [p for p in pages if (p.get("_qualification") or {}).get("verdict") == "review"]
    if review_pages:
        lines.append(
            "- **Qualification review** — these page(s) are migrated but a human should "
            "confirm they're genuinely informational (a moderate out-of-scope signal fired):"
        )
        for p in review_pages:
            reasons = (p.get("_qualification") or {}).get("reasons") or []
            lines.append(f"  - `{p['slug']}` — {reasons[0] if reasons else 'see qualification_report.md'}")
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
                f"library attachment (post_id {LOGO_ATTACHMENT_ID}, also stamped with a "
                "`_webstractor_site_logo` marker). Setting it as the site's active logo "
                "(the `site_logo` option/`custom_logo` theme mod) isn't something WXR can "
                f"do on its own -- **the {REPAIR_PLUGIN_NAME} plugin does it for you** on "
                f"activation ({REPAIR_HOWTO}); or, from a shell, `php {OUT_APPLY_BRANDING}` "
                "does the same. A full site reset wipes this option, so re-run whichever "
                "of the two you use after every reset."
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
    if _QA_NOTES:
        lines.append("")
        lines.append("## Per-page review notes")
        lines.append("")
        lines.append(
            "Formerly emitted as `<!-- QA FLAG -->` HTML comments inside each page's "
            "content. They're collected here instead: left in the page body, WordPress's "
            "block editor turns every one into a stray \"Classic\" block on import."
        )
        lines.append("")
        pages_by_slug_title = {p["slug"]: clean_title(p["title"]) for p in pages}
        for slug, notes in _QA_NOTES.items():
            lines.append(f"- **{pages_by_slug_title.get(slug, slug)}** (`{slug}`):")
            for note in dict.fromkeys(notes):  # dedupe, keep order
                lines.append(f"  - {note}")
    lines.append("")
    lines.append("## What's in the attached files")
    lines.append("")
    lines.append("- `stratecon-migration.xml` — import via **Tools → Import → WordPress** on any WordPress site (install the free WordPress Importer plugin if prompted). Pages import as **drafts** so nothing goes live automatically.")
    lines.append("- `redirects.csv` — import into the free **Redirection** plugin to preserve old URLs once the new site goes live.")
    lines.append(
        f"- `{OUT_REPAIR}` — the **{REPAIR_PLUGIN_NAME}** plugin. After importing and "
        f"publishing the pages, {REPAIR_HOWTO}. Repoints broken re-hosted image URLs at the "
        "file WordPress actually saved, pulls media-library copies of the stock images the "
        "importer couldn't, sets the static front page, sets the site logo, and reclaims the "
        "`/privacy-policy/` slug from WordPress's sample page. No "
        "server/shell access needed; safe to activate again — re-run it after any full "
        "site reset, which wipes the front-page and logo settings. (A shell, where "
        "available, can instead run "
        "`php wp-content/plugins/repair-migration/repair-migration.php` directly.)"
    )
    if brand:
        lines.append(f"- `{OUT_THEME}` — the extracted color palette and font list in WordPress's block-theme format.")
        if build_apply_branding_php(brand):
            lines.append(
                f"- `{OUT_APPLY_BRANDING}` — the shell-based equivalent of the repair "
                f"plugin's logo step plus brand-font loading (`php {OUT_APPLY_BRANDING}` "
                "from the WordPress root), for hosts where a shell is available. Run once "
                "after each fresh import; see the notes above."
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
        ".migration-flex-column-half{flex-basis:50%!important;flex-grow:0!important;"
        "display:flex!important;flex-direction:column!important}"
        ".migration-columns-gap{column-gap:2.5rem;row-gap:2.5rem}"
        ".migration-cta-buttons{margin-top:auto;padding-top:1.5rem}"
        ".migration-text-center{text-align:center}"
        # FAQ accordion (core/details): a divider between items, a little
        # breathing room, and a pointer cursor + weight on the question.
        ".migration-faq-item{border-top:1px solid #e2e2e2;padding:1rem 0}"
        ".migration-faq-item:last-of-type{border-bottom:1px solid #e2e2e2}"
        ".migration-faq-item>summary{cursor:pointer;font-weight:600;list-style-position:outside}"
        ".migration-faq-item>summary:focus-visible{outline:2px solid currentColor;outline-offset:2px}"
        ".migration-faq-item[open]>summary{margin-bottom:.75rem}"
    )

    # The hero cover (see block_to_gutenberg()'s "hero" branch). The
    # block markup already carries "overlayColor":"primary"; these rules
    # back it up independently of whether the imported palette registered
    # "primary" -- the overlay color, a real min-height, centered inner
    # content, and the light text color the brand's own navy HeadingAlpha
    # can't provide against a dark overlay (scoped to .migration-hero so
    # it never leaks into the rest of the page).
    primary = (brand or {}).get("colors", {}).get("button_background") or "#1d2b52"
    rules.append(
        ".migration-hero{min-height:460px}"
        ".migration-hero .wp-block-cover__background{background-color:" + primary + "}"
        ".migration-hero .wp-block-cover__inner-container{text-align:center;"
        "max-width:820px;margin-left:auto;margin-right:auto}"
        ".migration-hero .wp-block-cover__inner-container :where(h1,p){color:#ffffff}"
    )

    # Form placeholder panel (see _form_placeholder()) -- a form the
    # pipeline can't build until a form-plugin decision is made. Styled
    # as an obvious "to be wired up" box so it never reads as a broken
    # form or as stray bracket text. Theme-neutral (no brand token) since
    # it's scaffolding, not final design.
    rules.append(
        ".migration-form-placeholder{border:1px dashed rgba(120,120,120,.45);"
        "border-radius:6px;padding:1.25rem 1.5rem;background:rgba(120,120,120,.06);"
        "margin:1.5rem 0}"
        ".migration-form-placeholder .migration-form-note{opacity:.75;"
        "font-size:.95em;margin:.25rem 0 0}"
    )

    # post_feed ("AI Insights" / "Cybersecurity Insights") card grid --
    # match the live widget's bordered white cards so the equal-height
    # columns' leftover space reads as card padding, not a gap. The image
    # sits flush to the card's top edge (negative margins cancel the
    # card padding on three sides).
    rules.append(
        ".migration-post-feed-card{background:#ffffff;border:1px solid #e2e2e2;"
        "border-radius:4px;padding:1.25rem}"
        ".migration-post-feed-card .wp-block-image:first-child{margin:-1.25rem -1.25rem 1rem}"
        ".migration-post-feed-card .wp-block-image:first-child img{border-radius:4px 4px 0 0;width:100%}"
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

    # Attach real @font-face data (fetched from Google Fonts -- see
    # _fetch_google_font_faces()'s docstring for why this, not a CSS
    # @import, is what actually gets these fonts to load) to each
    # family entry via theme.json v2's native "fontFace" schema.
    # WordPress's own font-loading pipeline (WP_Font_Face_Resolver)
    # picks this straight out of settings.typography.fontFamilies and
    # prints the real @font-face CSS in wp_head -- no @import, no PHP
    # execution, and not dependent on anything this project's own
    # generated CSS controls. A family this couldn't fetch faces for
    # (no internet access when this script ran, or Google Fonts
    # unreachable) just keeps registering its name with no "fontFace",
    # same as before -- the font falls back to its stack's next entry,
    # exactly like today, rather than failing the whole build.
    faces_by_family = _fetch_google_font_faces(brand)
    for f in font_families:
        primary_name = f["fontFamily"].split(",")[0].strip().strip("\"'")
        faces = faces_by_family.get(primary_name)
        if faces:
            f["fontFace"] = faces

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


_FONT_FACE_RE = re.compile(
    r"font-family:\s*'([^']+)';\s*"
    r"font-style:\s*(\w+);\s*"
    r"font-weight:\s*(\d+);.*?"
    r"src:\s*url\(([^)]+)\)\s*format\('(\w+)'\)",
    re.DOTALL,
)


def _fetch_google_font_faces(brand):
    """Real @font-face src URLs (one per family+weight) for every font
    brand.json found, fetched from Google's own CSS2 endpoint -- not
    just the stylesheet *link* build_apply_branding_php() and the
    _google_fonts_href()-based @import both rely on, which need
    something to actually execute for the browser to fetch the font
    (server-side PHP for the first, and confirmed unreliable in
    practice for the second: on a real test site with CSS optimization/
    minification active -- a common WordPress performance-plugin
    feature, and SiteGround's own SG Optimizer plugin was active on the
    one this was tested against -- @import is a well-known casualty of
    that kind of processing, since combining/minifying stylesheets
    often drops or reorders it, and CSS requires @import to be the
    first rule in its stylesheet or browsers discard it outright).

    These URLs get embedded directly in build_global_styles_content()'s
    settings.typography.fontFamilies[].fontFace, WordPress's own native
    font-loading schema (see WP_Font_Face_Resolver::
    get_fonts_from_theme_json(), core since WP 6.4) -- the same
    mechanism Appearance > Editor > Styles > Fonts uses when a person
    installs a Google Font by hand, which is already confirmed to work
    on the real site this was built against. WordPress generates the
    real @font-face CSS from this itself and prints it in wp_head, no
    @import or PHP execution involved.

    Requires real internet access to Google Fonts -- unlike the rest of
    this script, which only ever reads local JSON. Returns {} (not an
    error) if the fetch fails for any reason, since a sandboxed/offline
    run of this script is a normal case, not a bug: build_global_styles_
    content() falls back to just registering the font names without
    "fontFace" entries, same as before this existed.
    """
    href = _google_fonts_href(brand)
    if not href:
        return {}

    try:
        req = Request(
            href,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                )
            },
        )
        with urlopen(req, timeout=15) as resp:
            css_text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return {}

    # Google's response repeats each family+weight once per unicode-range
    # subset (cyrillic, vietnamese, latin-ext, latin, ...); the broadest,
    # most essential "latin" block reliably comes last in every response
    # observed from this endpoint, so keeping the LAST match per
    # (family, weight, style) -- rather than the first -- lands on that
    # one instead of a narrow-coverage subset.
    faces_by_family = {}
    for family, style, weight, src, fmt in _FONT_FACE_RE.findall(css_text):
        faces_by_family.setdefault(family, {})[(weight, style)] = {
            "fontFamily": family,
            "fontStyle": style,
            "fontWeight": weight,
            "src": [src],
        }

    return {family: list(faces.values()) for family, faces in faces_by_family.items()}


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
        parts.append(
            f"\n"
            f"// Site logo -- the file itself already came in as an attachment\n"
            f"// via the WXR import, normally at post_id {LOGO_ATTACHMENT_ID} (the WXR\n"
            f"// wp:post_id, which the importer honours on a clean import). Fall back\n"
            f"// to the '_webstractor_site_logo' marker meta if that id isn't the logo.\n"
            f"$logo_id = {LOGO_ATTACHMENT_ID};\n"
            f"if (get_post_type($logo_id) !== 'attachment') {{\n"
            f"    $marked = get_posts(array(\n"
            f"        'post_type' => 'attachment', 'post_status' => 'inherit',\n"
            f"        'numberposts' => 1, 'fields' => 'ids',\n"
            f"        'meta_key' => '_webstractor_site_logo', 'meta_value' => '1',\n"
            f"    ));\n"
            f"    $logo_id = $marked ? (int) $marked[0] : 0;\n"
            f"}}\n"
            f"if ($logo_id && get_post_type($logo_id) === 'attachment') {{\n"
            f"    update_option('site_logo', $logo_id);       // block themes' core/site-logo\n"
            f"    set_theme_mod('custom_logo', $logo_id);     // classic-theme fallback\n"
            f"    echo \"Site logo set (attachment {{$logo_id}}).\\n\";\n"
            f"}} else {{\n"
            f"    echo \"Logo attachment not found -- import the WXR file first"
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


# The post-import repair is shipped as an installable WordPress plugin
# (upload the .zip via Plugins -> Add New -> Upload Plugin, then Activate)
# rather than a `php repair_migration.php` shell script -- SiteGround and
# most shared hosts give the client no shell to run one. OUT_REPAIR is the
# artifact the client actually handles (the .zip); OUT_REPAIR_PHP_REL is
# the plugin file inside it / on disk.
OUT_REPAIR_DIR = "repair-migration"
OUT_REPAIR_PHP_REL = os.path.join(OUT_REPAIR_DIR, "repair-migration.php")
OUT_REPAIR = "repair-migration.zip"
REPAIR_PLUGIN_NAME = "Stratecon Migration Repair"
# How the client runs the repair, for QA-report prose. It's a plugin now,
# not a shell script -- upload + activate, no server access needed.
REPAIR_HOWTO = (
    f"upload **`{OUT_REPAIR}`** via Plugins → Add New → Upload Plugin and click "
    f"Activate — it runs once, shows a report, then deactivates itself"
)


def _php_single_quoted(value):
    """A PHP single-quoted string literal for an arbitrary Python str --
    only \\ and ' need escaping inside PHP single quotes."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_repair_migration_php(data, brand=None):
    """The post-import repair, as an installable WordPress plugin -- the
    client uploads `repair-migration.zip` via Plugins -> Add New -> Upload
    Plugin and clicks Activate; it does its work once on activation,
    prints a report in an admin notice, then deactivates itself. No shell
    access needed (SiteGround and most shared hosts don't give the client
    one). The same file is still runnable straight from the CLI where a
    shell *is* available (`php wp-content/plugins/repair-migration/
    repair-migration.php`).

    It fixes the things WXR + the core importer provably get wrong, none
    of which any WXR item can express:

      1. Broken re-hosted image URLs. GoDaddy serves some images from a
         URL whose extension lies about the bytes (a `.webp`/`.png` URL
         returning JPEG). WordPress downloads the bytes, correctly saves
         the file as `.jpg`, and generates every sub-size -- but the
         importer's content URL-rewrite keeps the original `.webp`/`.png`
         extension, so every `<img>` referencing it 404s. Confirmed on
         the real dev site: `AI Customer Service`, `Cyber Training
         example`, the founder headshot, and others. This walks every
         imported page, finds `wp-content/uploads/...` image URLs with no
         file behind them, and repoints them at the real attachment (same
         filename stem, whatever extension WordPress actually used).

      2. Stock images that never got a media-library copy. GoDaddy's
         `isteam/stock/<id>/:/...` URLs have no filename or extension for
         the importer's attachment mechanism to accept, so they stayed
         hot-linked to the old site. media_sideload_image() sniffs the
         real type from the response and doesn't care about the URL
         shape, so it can pull independent copies; each occurrence in
         page content is then repointed at the new local URL.

      3. The static front page. "Which page shows at /" is the
         show_on_front / page_on_front option pair, not page content --
         no WXR item can set it, so a migrated home page otherwise
         imports as a normal page and `/` shows the blog listing.

      4. The site logo. The logo file rides in as a WXR attachment, but
         "which attachment is the site logo" is the site_logo option /
         custom_logo theme mod -- again not page content, and wiped by
         every full reset. The plugin re-points it at the logo
         attachment (found by the fixed WXR post id, falling back to the
         `_webstractor_site_logo` marker meta the generator writes on it).

      5. The /privacy-policy/ slug. WordPress (and a WP Reset) seed a
         sample "Privacy Policy" page, so the importer can't give the
         migrated one that slug -- it lands at /privacy-policy-2/ and
         /privacy-policy/ shows WordPress's boilerplate. The plugin
         trashes the sample (identified by its "Suggested text:"
         content), moves the migrated page onto /privacy-policy/, and
         repoints wp_page_for_privacy_policy.

    Idempotent: activating it again re-checks the same conditions and
    no-ops on anything already fixed (a stock URL no longer present in
    any post is skipped rather than re-downloaded).
    """
    front_page = next((p for p in data.get("pages", []) if p.get("is_front_page")), None)
    _, non_importable = partition_images_by_importability(
        collect_unique_images(data.get("pages", []))
    )

    php_stock_entries = ",\n".join(
        f"        {_php_single_quoted(url)} => {_php_single_quoted(alt or '')}"
        for url, alt in non_importable.items()
    ) or "        // (none -- every image had an importable URL)"
    front_slug_literal = (
        _php_single_quoted(front_page["slug"]) if front_page else "null"
    )
    logo = (brand or {}).get("logo") or {}
    has_logo = bool(logo.get("url") and canonical_attachment_url(logo["url"]))
    logo_id_literal = str(LOGO_ATTACHMENT_ID) if has_logo else "0"

    # Raw string: every backslash below is for PHP/PCRE, not Python. The
    # dynamic values are spliced in via sentinel replace so nothing needs
    # Python brace- or escape-handling.
    template = r'''<?php
/**
 * Plugin Name: __PLUGIN_NAME__
 * Description: One-time post-import cleanup the WXR import can't do itself -- repoints broken re-hosted image URLs, sideloads the GoDaddy stock images the importer can't take, sets the static front page, sets the site logo, and reclaims the /privacy-policy/ slug from WordPress's sample page. Runs once on activation, shows a report, then deactivates itself. Safe to activate again.
 * Version:     1.0.0
 * Author:      Webstractor migration pipeline (auto-generated)
 */

if (!defined('ABSPATH')) {
    // No WordPress around us -- this is a direct CLI run, e.g.
    //   php wp-content/plugins/repair-migration/repair-migration.php
    // (handy where a shell IS available; the plugin path is for hosts
    // where it isn't).
    if (PHP_SAPI !== 'cli') {
        exit;
    }
    $candidates = array(
        dirname(__FILE__, 4) . '/wp-load.php',  // wp-content/plugins/<dir>/<file>
        dirname(__FILE__, 3) . '/wp-load.php',
        dirname(__FILE__, 2) . '/wp-load.php',
        dirname(__FILE__) . '/wp-load.php',
    );
    $loaded = false;
    foreach ($candidates as $wp_load) {
        if (file_exists($wp_load)) {
            require_once $wp_load;
            $loaded = true;
            break;
        }
    }
    if (!$loaded) {
        fwrite(STDERR, "wp-load.php not found relative to " . __FILE__ . "\n");
        exit(1);
    }
    foreach (stratecon_migration_repair_run() as $line) {
        echo $line . "\n";
    }
    exit(0);
}

register_activation_hook(__FILE__, function () {
    // Clear any stale report first -- on hosts with a persistent object
    // cache (SiteGround's Memcached/Redis), a previous run's transient
    // can outlive the DB reset and be shown instead of this run's.
    delete_transient('stratecon_migration_repair_report');
    // Stash the report for the admin notice below. No echo here: any
    // output during activation trips WordPress's "plugin generated N
    // characters of unexpected output" warning.
    set_transient(
        'stratecon_migration_repair_report',
        stratecon_migration_repair_run(),
        10 * MINUTE_IN_SECONDS
    );
});

add_action('admin_notices', function () {
    $report = get_transient('stratecon_migration_repair_report');
    if ($report === false) {
        return;
    }
    delete_transient('stratecon_migration_repair_report');
    // Arm a one-request-later self-deactivate: the notice is shown once,
    // then the plugin bows out on its own on the next admin page load.
    // Nothing of it runs on a normal request, but no reason to leave it
    // sitting in the active list either.
    update_option('stratecon_migration_repair_cleanup', 1, false);
    echo '<div class="notice notice-success"><p><strong>' . esc_html('__PLUGIN_NAME__') . ' &mdash; done.</strong></p><ul style="list-style:disc;margin-left:2em">';
    foreach ((array) $report as $line) {
        echo '<li>' . esc_html($line) . '</li>';
    }
    echo '</ul><p>This plugin has finished its one-time job and will deactivate itself. You can delete it.</p></div>';
});

add_action('admin_init', function () {
    if (!get_option('stratecon_migration_repair_cleanup')) {
        return;
    }
    delete_option('stratecon_migration_repair_cleanup');
    require_once ABSPATH . 'wp-admin/includes/plugin.php';
    deactivate_plugins(plugin_basename(__FILE__));
});

// Sideload one image whose URL has no usable extension (GoDaddy's
// isteam/stock/<id> URLs): download, sniff the real type, name the temp
// file ourselves, then hand it to media_handle_sideload(). Declared
// unconditionally at file scope so PHP hoists it -- the CLI branch above
// calls run() before this point in the file is ever reached.
function stratecon_migration_repair_sideload($src, $alt) {
    $tmp = download_url($src, 30);
    if (is_wp_error($tmp)) {
        return $tmp;
    }
    $info = @getimagesize($tmp);
    $ext  = $info ? ltrim(image_type_to_extension($info[2]), '.') : 'jpg';
    $file_array = array(
        'name'     => 'stock-' . substr(md5($src), 0, 12) . '.' . $ext,
        'tmp_name' => $tmp,
    );
    $id = media_handle_sideload($file_array, 0, $alt !== '' ? $alt : null);
    if (is_wp_error($id)) {
        @unlink($tmp);
        return $id;
    }
    return wp_get_attachment_url($id);
}

function stratecon_migration_repair_run() {
    @set_time_limit(300);
    @ignore_user_abort(true);
    require_once ABSPATH . 'wp-admin/includes/image.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';

    $report = array();
    $uploads = wp_get_upload_dir();
    $all_posts = get_posts(array(
        'post_type'   => array('page', 'post'),
        'post_status' => 'any',
        'numberposts' => -1,
    ));

    // -----------------------------------------------------------------
    // 1. Repoint broken /wp-content/uploads/ image URLs at the real file.
    // -----------------------------------------------------------------
    $img_url_re = '~https?://[^\s\x22\x27<>()]+?/wp-content/uploads/[^\s\x22\x27<>()]+?\.(?:jpe?g|png|gif|webp|avif)(?=[\s\x22\x27>)]|$)~i';
    $fixed_refs = 0;
    foreach ($all_posts as $post) {
        $content = $post->post_content;
        if (strpos($content, '/wp-content/uploads/') === false) {
            continue;
        }
        $updated = $content;
        if (preg_match_all($img_url_re, $content, $m)) {
            foreach (array_unique($m[0]) as $url) {
                $rel  = ltrim(str_replace($uploads['baseurl'], '', $url), '/');
                $path = $uploads['basedir'] . '/' . $rel;
                if (file_exists($path)) {
                    continue;  // URL already resolves -- nothing to do
                }
                $dir  = dirname($path);
                $stem = preg_replace('/\.[a-z0-9]+$/i', '', basename($path));
                // Same stem, any real image extension; also tolerate
                // WordPress's -1/-2 filename-collision suffix on the real
                // file. Prefer an exact-stem match; fall back to a
                // suffixed one. Skip WordPress's own -WxH sub-sizes.
                $candidates = array_merge(
                    (array) glob($dir . '/' . $stem . '.*'),
                    (array) glob($dir . '/' . $stem . '-*.*')
                );
                $replacement = null;
                foreach ($candidates as $cand) {
                    if (preg_match('/-\d+x\d+\.[a-z0-9]+$/i', $cand)) {
                        continue;  // WordPress sub-size, not the original
                    }
                    if (preg_match('/\.(jpe?g|png|gif|webp|avif)$/i', $cand) && is_file($cand)) {
                        $replacement = $uploads['baseurl'] . '/' . ltrim(str_replace($uploads['basedir'], '', $cand), '/');
                        break;
                    }
                }
                if ($replacement && $replacement !== $url) {
                    $updated = str_replace($url, $replacement, $updated);
                    $fixed_refs++;
                }
            }
        }
        if ($updated !== $content) {
            wp_update_post(array('ID' => $post->ID, 'post_content' => $updated));
        }
    }
    $report[] = "Broken image URLs repointed: {$fixed_refs}";

    // -----------------------------------------------------------------
    // 2. Sideload stock images the importer couldn't, then repoint refs.
    // -----------------------------------------------------------------
    $stock = array(
__STOCK_ENTRIES__
    );
    // Longest URL first. Some of these keys are a strict prefix of
    // another (the same GoDaddy image referenced once with a
    // ".../rs=w:600,..." resize suffix and once without): matching the
    // shorter one first replaces only part of the longer one's <img
    // src>, leaving a dangling ".../rs=w:600,..." that 404s. Handling
    // the more specific URL first, then re-checking, avoids that.
    uksort($stock, function ($a, $b) { return strlen($b) - strlen($a); });
    $sideloaded = 0;
    $stock_skipped = 0;
    foreach ($stock as $src => $alt) {
        $still_used = false;
        foreach ($all_posts as $post) {
            if (strpos(get_post_field('post_content', $post->ID), $src) !== false) {
                $still_used = true;
                break;
            }
        }
        if (!$still_used) {
            $stock_skipped++;
            continue;  // already handled on a previous run, or never referenced
        }
        $new_url = stratecon_migration_repair_sideload($src, $alt);
        if (is_wp_error($new_url)) {
            $report[] = "  stock sideload failed ({$src}): " . $new_url->get_error_message();
            continue;
        }
        foreach ($all_posts as $post) {
            $content = get_post_field('post_content', $post->ID);
            if (strpos($content, $src) !== false) {
                wp_update_post(array('ID' => $post->ID, 'post_content' => str_replace($src, $new_url, $content)));
            }
        }
        $sideloaded++;
    }

    // A local uploads image URL is sometimes left with a trailing GoDaddy
    // transform suffix ("stock-x.jpeg/rs=w:600,..." / ".../:/cr=...") --
    // e.g. when a shorter stock URL matched as a prefix of a longer one,
    // or the importer sanitised the URL so the full stock key no longer
    // matched. Nothing legitimate follows an image extension with "/rs="
    // or "/cr=" or "/:", so chop any of that off.
    $trail_re = '~(/wp-content/uploads/[^\s\x22\x27<>()]+?\.(?:jpe?g|png|gif|webp|avif))/(?:rs=|cr=|:)[^\s\x22\x27<>()]*~i';
    $trimmed = 0;
    foreach ($all_posts as $post) {
        $content = get_post_field('post_content', $post->ID);
        $nc = preg_replace($trail_re, '$1', $content);
        if ($nc !== null && $nc !== $content) {
            wp_update_post(array('ID' => $post->ID, 'post_content' => $nc));
            $trimmed++;
        }
    }
    $report[] = "Stock images sideloaded: {$sideloaded} (skipped {$stock_skipped} already done/unused)"
        . ($trimmed ? "; trimmed a stray transform suffix on {$trimmed} page(s)" : "");

    // -----------------------------------------------------------------
    // 3. Static front page.
    // -----------------------------------------------------------------
    $front_slug = __FRONT_SLUG__;
    if ($front_slug) {
        $front = get_page_by_path($front_slug);
        if ($front) {
            update_option('show_on_front', 'page');
            update_option('page_on_front', $front->ID);
            $note = ($front->post_status === 'publish') ? '' : " (still a {$front->post_status} -- publish it so / resolves)";
            $report[] = "Front page set to \"{$front->post_title}\" (slug {$front_slug}, id {$front->ID}){$note}.";
        } else {
            $report[] = "Front-page slug \"{$front_slug}\" not found -- publish the imported pages first, then activate this plugin again.";
        }
    }

    // -----------------------------------------------------------------
    // 4. Site logo. The file came in as a WXR attachment; "which
    //    attachment is the logo" is the site_logo option + custom_logo
    //    theme mod, which no WXR item can carry and a full reset wipes.
    // -----------------------------------------------------------------
    $logo_id = __LOGO_ATTACHMENT_ID__;  // the WXR wp:post_id; 0 if the site has no logo
    if ($logo_id) {
        if (get_post_type($logo_id) !== 'attachment') {
            // The fixed WXR post id didn't survive import (id already
            // taken, or this wasn't a clean import over an empty DB).
            // Fall back to the marker the generator stamps on the logo.
            $marked = get_posts(array(
                'post_type'   => 'attachment',
                'post_status' => 'inherit',
                'numberposts' => 1,
                'fields'      => 'ids',
                'meta_key'    => '_webstractor_site_logo',
                'meta_value'  => '1',
            ));
            $logo_id = $marked ? (int) $marked[0] : 0;
        }
        if ($logo_id && get_post_type($logo_id) === 'attachment') {
            update_option('site_logo', $logo_id);    // block themes (core/site-logo)
            set_theme_mod('custom_logo', $logo_id);  // classic-theme fallback
            $report[] = "Site logo set (attachment {$logo_id}).";
        } else {
            $report[] = "Site logo NOT set -- logo attachment not found. Import the WXR with \"Download and import file attachments\" checked, then activate this plugin again, or set it by hand in Appearance -> Editor -> Styles.";
        }
    }

    // -----------------------------------------------------------------
    // 5. Reclaim the /privacy-policy/ slug. WordPress (and a WP Reset)
    //    seed a sample "Privacy Policy" page, so the WXR import can't
    //    claim that slug and the migrated page lands at
    //    /privacy-policy-2/. Detect that -- the sample page always carries
    //    the "Suggested text:" boilerplate -- and swap them: trash the
    //    sample (which frees the slug), move the migrated page onto
    //    /privacy-policy/, and repoint the privacy-policy option.
    //    Idempotent: once done there's no /privacy-policy-2/ to find.
    // -----------------------------------------------------------------
    $pp_pages = get_posts(array(
        'post_type'   => 'page',
        'post_status' => 'any',
        'title'       => 'Privacy Policy',
        'numberposts' => 10,
    ));
    $pp_sample = null;
    $pp_migrated = null;
    foreach ($pp_pages as $pp) {
        if (strpos((string) $pp->post_content, 'Suggested text:') !== false) {
            $pp_sample = $pp;
        } elseif (!$pp_migrated) {
            $pp_migrated = $pp;
        }
    }
    if ($pp_migrated && $pp_sample && (int) $pp_migrated->ID !== (int) $pp_sample->ID
        && $pp_migrated->post_name !== 'privacy-policy') {
        wp_trash_post($pp_sample->ID);                 // appends __trashed, frees the slug
        wp_update_post(array('ID' => $pp_migrated->ID, 'post_name' => 'privacy-policy'));
        if ((int) get_option('wp_page_for_privacy_policy') === (int) $pp_sample->ID) {
            update_option('wp_page_for_privacy_policy', $pp_migrated->ID);
        }
        $report[] = "Privacy Policy: trashed the WordPress sample page (id {$pp_sample->ID}); "
                  . "moved the migrated page to /privacy-policy/.";
    }

    return $report;
}
'''
    return (
        template
        .replace("__PLUGIN_NAME__", REPAIR_PLUGIN_NAME)
        .replace("__STOCK_ENTRIES__", php_stock_entries)
        .replace("__FRONT_SLUG__", front_slug_literal)
        .replace("__LOGO_ATTACHMENT_ID__", logo_id_literal)
    )


def write_repair_migration_plugin(data, brand=None):
    """Write the repair plugin to disk as both an unpacked
    `repair-migration/repair-migration.php` (for a shell / SFTP drop-in)
    and a `repair-migration.zip` the client uploads via Plugins -> Add
    New -> Upload Plugin. Returns the list of paths written."""
    php = build_repair_migration_php(data, brand)
    os.makedirs(OUT_REPAIR_DIR, exist_ok=True)
    with open(OUT_REPAIR_PHP_REL, "w") as f:
        f.write(php)
    # Fixed timestamp/permissions so the .zip only changes in git when the
    # plugin's contents actually change, not on every regeneration.
    info = zipfile.ZipInfo("repair-migration/repair-migration.php", (2026, 1, 1, 0, 0, 0))
    info.external_attr = 0o644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(OUT_REPAIR, "w") as z:
        z.writestr(info, php)
    return [OUT_REPAIR_PHP_REL, OUT_REPAIR]


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

    # Stamp `_ff_slot` on every contact_form block before anything renders
    # it, so the page markup, the QA report, and fluentforms-migration.json
    # all agree on which placeholder maps to which Fluent Forms form.
    ff_slots = assign_ff_slots(data)

    with open(OUT_WXR, "w") as f:
        f.write(build_wxr(data, brand))

    with open(OUT_REDIRECTS, "w") as f:
        f.write(build_redirects_csv(data))

    with open(OUT_QA, "w") as f:
        f.write(build_qa_report(data, brand))

    repair_paths = write_repair_migration_plugin(data, brand)

    outputs = [OUT_WXR, OUT_REDIRECTS, OUT_QA, *repair_paths]
    if ff_slots:
        with open(OUT_FF, "w") as f:
            f.write(build_fluentforms_export(data))
        outputs.append(OUT_FF)
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
