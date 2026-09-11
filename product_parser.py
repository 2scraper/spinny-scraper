"""
product_parser.py
-----------------
Everything this repo knows about Spinny. The engines know how to drive a
browser; this file knows what a Spinny page says.

Read this before changing anything here
=======================================

**A listing page has no structured data about its cars.** Measured on five
live captures, 2026-09-11: a fully scrolled `/used-cars-in-delhi-ncr/s/`
carries five `application/ld+json` blocks and not one of them names a car —
a `BreadcrumbList`, a `LocalBusiness` for Spinny's hubs, a `FAQPage`, and a
marketing `Product` whose `aggregateRating` rates **Spinny** rather than any
vehicle. The grid is fetched by client-side XHR after load and exists only in
the rendered DOM.

So the JSON-LD-first design the rest of this family uses has nothing to be
first about on a listing, and §4's *fallback* — a URL pattern — is promoted
to primary because there is nothing above it. Every car in the grid is
wrapped in an `<a href="/buy-used-cars/{city}/{make}/{model}/{trim}-{year}/{id}/">`,
705 of 705 across three captures, and that path is a contract with search
engines rather than a build artefact.

A DETAIL page is different and much better off: it carries a real
`["Product","Car"]` block with the price, the currency, the exact odometer
reading, the previous-owner count, the colour and the seating capacity.
`parse_detail_page` reads it.

**The detail page's JSON-LD spells the price with a capital P.**

    "offers": {"@type": "Offer", "priceCurrency": "INR",
               "Price": 392000, ...}

schema.org's property is `price`. A parser reading `offers.price` — which is
what every other repo in this family reads — gets `None`, silently, on every
row of every detail run. Both spellings are accepted here and the capitalised
one is the only one that has ever been observed.

**Class names are a design-system vocabulary, and nothing anchors on them.**

    <span class="ds-font-text ds-body-small ds-font-regular
                 ds-text-surface-text-gray-normal">Petrol</span>

Those `ds-*` classes are utility classes shared by every text node on the
site — `ds-body-small` wraps the fuel type, the transmission, the RTO code
and the hub name identically. So a class cannot tell one field from another
here, and the reads below are **pattern-based inside a structurally-scoped
card**: the fuel type is recognised because it is one of five words, the RTO
because it matches `[A-Z]{2}\\d{1,2}[A-Z]{0,3}`, the odometer because it ends
in "km". That is not a stylistic choice — 10 of 705 tiles print no hub at
all, so anything read by position in the card's span order is wrong on those
ten and silently right-shifted on the fields after it.

What the site DOES label, and what the reads anchor on:

    a[href^="/buy-used-cars/"]          the car, and its id
    [data-id="landing-plp-container"]   the grid
    [data-base-component="card"]        one car's card
    [data-base-component="Pricing"]     the displayed price
    .ds-line-through                    the was-price
    #shortlist_icon[data-price]         the exact all-in price, and the id
    [data-componentname="HeadingContent"]  the listing's own total

**`data-label` is not a car id.** It is the shortlist button's label, and on
CTA buttons inside the same grid it holds `loan-learn-more`, `Buy Back` and
`notify-btn`. Anchoring on it would let a junk element steal a real car's row
(§4). The id comes from the URL; `data-label` is only ever used as a
cross-check, and it agreed on 705 of 705.

**Four prices per car, and they disagree by up to ₹300,000.** See the note at
the top of output_writer.py. The short version: the displayed pair
(headline + strike) is the only pair on one basis, `data-price` is the exact
figure on a different one, and mixing them computes a negative discount.

**Pagination does not exist.** `?page=N` is silently ignored — pages 1, 2
and 3 of `/used-cars-in-delhi-ncr/s/` returned byte-identical first cards.
See `paginates_by_url`, and the long comment above it, before writing
anything that builds a page URL.
"""

import json
import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin

from bs4 import BeautifulSoup

from output_writer import Product, SOURCE_DEFAULT

logger = logging.getLogger("product_parser")


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------
# Spinny is ONE storefront on ONE hostname, serving one country in one
# currency. There is no locale path prefix, no per-country domain, no second
# currency and no geo-redirect: the same listing URL fetched from an
# Amsterdam datacentre exit and from a Chennai residential exit returned the
# same page, the same title, the same advertised total and INR prices both
# times (measured 2026-09-11).
#
# Which is why this repo has NO --country flag: there would be nothing for it
# to select. What DOES vary is the CITY, and the city is part of the URL
# (`/used-cars-in-{city}/s/`), so it is an argument rather than a flag — see
# CITIES below.
HOSTS = ("spinny.com", "www.spinny.com")

# Hosts that look like they belong and do not. Refuse them WITH THE REASON:
# "is not a Spinny site" would be false for a Spinny subdomain that simply is
# not the storefront, and sends the reader hunting for a typo (§5).
UNSUPPORTED: Dict[str, str] = {
    "api.spinny.com":     "is Spinny's JSON API, not a page — this scraper "
                          "drives a browser and reads the rendered grid",
    "assets.spinny.com":  "is Spinny's image CDN",
    "spn-sta.spinny.com": "is Spinny's static-asset host, not the storefront",
    "mda.spinny.com":     "is Spinny's media host",
    "spn-mda.spinny.com": "is Spinny's media host",
    "blog.spinny.com":    "is Spinny's blog, which publishes articles rather "
                          "than car listings",
    "partners.spinny.com": "is the Spinny Partners dealer portal, a different "
                           "product behind a login",
}

# Kept for compatibility with the family's shared engine code, which reads a
# locale-to-currency map. Spinny has exactly one of each, so this is a
# one-entry table rather than a mechanism.
LOCALE_CURRENCY = {"en-IN": "INR"}
CURRENCY = "INR"

# The cities Spinny sells in, taken from the site's OWN footer rather than
# guessed — 33 `/used-cars-in-{city}/s/` links on a detail page captured
# 2026-09-11. §5 says to read this off the site and then CHECK it, and the
# check matters here: `delhi-ncr` is in the list and is a REGION, not a city.
# A /used-cars-in-delhi-ncr/ listing returned cars whose own city segments
# were delhi, gurgaon, ghaziabad, noida, faridabad, sonipat and karnal.
#
# The list is advisory. An unknown city slug is a WARNING and not a refusal:
# Spinny opens cities, and a scraper that refuses a URL the site serves
# perfectly well is worse than one that says "I have not seen this city
# before" and carries on.
CITIES = (
    "agra", "ahmedabad", "ambala", "bangalore", "chandigarh", "chennai",
    "coimbatore", "delhi", "delhi-ncr", "faridabad", "ghaziabad", "gurgaon",
    "hyderabad", "jaipur", "jodhpur", "kanpur", "karnal", "kochi", "kolkata",
    "lucknow", "ludhiana", "mangaluru", "mohali", "mumbai", "mysuru",
    "nagpur", "noida", "prayagraj", "pune", "ranchi", "sonipat", "vadodara",
    "visakhapatnam",
)


def site_host(url: str) -> Optional[str]:
    """The row's `source`: the bare hostname with `www.` stripped."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def unsupported_reason(url: str) -> Optional[str]:
    """Why this URL cannot be scraped, or None if it can be."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return "has no hostname"
    if host in UNSUPPORTED:
        return UNSUPPORTED[host]
    if host in HOSTS:
        return None
    if host.endswith(".spinny.com"):
        return ("is a Spinny subdomain but not the storefront — this scraper "
                "reads www.spinny.com")
    return "is not a Spinny address"


def is_supported_host(url: str) -> bool:
    return unsupported_reason(url) is None


def locale_of(url: str) -> str:
    """Spinny serves one locale. Present so shared code has it to call."""
    return "en-IN"


def host_currency(url: str) -> str:
    return CURRENCY


# ---------------------------------------------------------------------------
# PAGE KINDS
# ---------------------------------------------------------------------------
# Four shapes, and three of them want a different response.
#
#   listing   /used-{what}-in-{city}/s/   the grid. One page, infinite scroll.
#             /used-cars/s/               CITYLESS — serves an EMPTY grid
#   detail    /buy-used-cars/{city}/{make}/{model}/{trim}-{year}/{id}/
#   hub       /used-cars/                 an SEO landing page, no grid at all
#   unknown   anything else
#
# The cityless listing deserves its own note because it is the site's most
# obvious URL and it never works. Spinny scopes its inventory by city; with
# no city in the path it renders the grid container, no cars, and a header
# reading "1546 Used cars in" with the city name missing. Measured from both
# an Amsterdam and a Chennai exit, and the site's own
# `api.spinny.com/v3/api/user-info/city-from-ip/v2/` answered `{"city":[]}`
# from BOTH — so this is not a geo problem that an Indian proxy fixes, it is
# a URL that needs a city in it.
SELECTORS = {
    # The grid. `data-id` is the site's own container marker and has outlived
    # the `ds-*` utility classes around it. One per page on every capture.
    "grid": '[data-id="landing-plp-container"]',
    # One car's card inside the grid. Note the grid also holds SKELETON
    # cards — 6 of 188 in one Bangalore capture — which carry no link and no
    # text at all because the site has not hydrated them yet. They are not a
    # fault and are skipped by requiring the link below.
    "card": '[data-base-component="card"]',
    # THE anchor: the car, and its id. 705/705.
    "item_link": 'a[href^="/buy-used-cars/"]',
    # The displayed price ("3.88 Lakh"). The site labels this one, on both a
    # listing card and a detail page. 705/705 and 1/1.
    "price": '[data-base-component="Pricing"]',
    # The struck-through was-price, on the displayed basis. 685/705.
    "strike_price": '.ds-line-through',
    # The shortlist heart, which carries three attributes worth having:
    # data-label (the id, as a cross-check), data-price (the EXACT all-in
    # price) and data-category (Spinny's quality tier). 705/705.
    "shortlist": '#shortlist_icon[data-price]',
    # Every text node in a card, read by PATTERN rather than by position —
    # see the module docstring.
    "card_text": 'span[data-base-component="text"]',
    "card_heading": 'h3',
    # The listing's own advertised total: `<span>1559</span><h1>Used cars in
    # Delhi NCR</h1>`. This is the completeness oracle (§8) — 482 rows
    # against an advertised 1559 is arithmetic proof the scroll stopped
    # early, needing no threshold.
    "heading_content": '[data-componentname="HeadingContent"]',
}

# How many car links mean "the grid rendered". Must be > 1: waiting for a
# single match resolves on an unrelated link long before the grid paints
# (§5). Spinny hydrates ~20 cars per batch and 22 were present in the DOM
# the moment the first batch landed, so 4 is reached immediately on a real
# listing and never on an empty one.
MIN_CARD_MATCHES = 4


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
# INR as Spinny writes it, which is TWO conventions on one page.
#
# The displayed price is written in **lakhs**: "3.88 Lakh" is ₹388,000 and
# "1.02 Crore" would be ₹10,200,000. One lakh is 100,000 and one crore is
# 10,000,000 — the South Asian numbering system, and the single most
# dangerous thing in this file, because a general price pattern reads
# "3.88 Lakh" as **3.88**.
#
# Crore has NOT been observed: the highest displayed price across 705 tiles
# and three captures was 35.40 Lakh, and Spinny's own price filter tops out
# at 40,00,000 (₹4,000,000 — still lakhs). The unit is supported anyway
# because it costs one dictionary entry, and it is marked here as unobserved
# rather than implied to be tested.
#
# Exact rupee amounts appear elsewhere on the same page — the discount badge
# ("₹13,000"), a detail page's EMI breakdown ("₹3,13,600") — and those use
# **Indian digit grouping**: the last group is three digits and every group
# before it is TWO. So 1,26,950 is 126,950 and not 1,269.50, and the family's
# inherited "exactly three trailing digits means a thousands grouping" rule
# returns None on it rather than a wrong number. Both groupings are handled
# below.
_LAKH = 100_000
_CRORE = 10_000_000
_SCALE_WORDS = {
    "lakh": _LAKH, "lakhs": _LAKH, "lac": _LAKH, "lacs": _LAKH,
    # Unobserved on this site as of 2026-09-11 — see the note above.
    "crore": _CRORE, "crores": _CRORE, "cr": _CRORE,
}

# A written amount: digits with optional grouping separators and an optional
# decimal part. Space grouping is included with all four of the space
# characters a rendered page uses, because a no-break variant is what keeps a
# number from wrapping and missing them parses "1 234" as 234 (§4).
_GROUP_SPACES = "    "
_AMOUNT = r'\d[\d,.' + _GROUP_SPACES + r']*'

# "3.88 Lakh" / "1.02 Crore" — a number followed by a scale word. The scale
# word is required: a bare number in a card is an odometer reading, a year,
# an EMI or an RTO code, never a price.
_SCALED_PRICE_RE = re.compile(
    r'(' + _AMOUNT + r')\s*(' + "|".join(sorted(_SCALE_WORDS, key=len,
                                                reverse=True)) + r')\b',
    re.I)
# "₹13,000" / "₹ 3,13,600" / "₹1.64L" — an amount behind the symbol, with
# an optional scale SUFFIX.
#
# That suffix is not decoration. Spinny's discount badge writes small
# discounts in full rupees and large ones in abbreviated lakhs: "₹13,000" on
# a ₹8 lakh hatchback and **"₹2.50L"** on a ₹47 lakh Mercedes. Read without
# the suffix, a ₹250,000 discount comes back as 2.5 — which is not a wrong
# number so much as a number in the wrong unit, and it passed every coverage
# check while making `discount_amount` disagree with the strike-minus-price
# figure by ₹249,998 on 13 of 223 discounted cars across two captures.
#
# "L" is lakh, "Cr" is crore, "K" is thousand. L is measured; Cr and K are
# the shapes the same abbreviation takes and are unobserved on this site.
_RUPEE_SUFFIX = {"": 1, "k": 1_000, "l": _LAKH, "cr": _CRORE,
                 "lakh": _LAKH, "lakhs": _LAKH, "crore": _CRORE}
_RUPEE_RE = re.compile(
    r'[₹₹]\s*(' + _AMOUNT + r')\s*(Cr|Crores?|Lakhs?|L|K)?\b', re.I)
# There was an `_RS_RE` here for "Rs. 1.65 Lakh" in a page title, and it is
# gone: nothing read it, and `scaled_price` already returns 165000.0 for
# that string because the SCALE WORD is what it anchors on and the "Rs."
# lead-in is not in the way. A second pattern with no consumer is the defect
# §17 names — prose that reads like enforcement while nothing enforces it.

# Percentages come out of the text BEFORE prices are matched, never after. A
# rejected match has still consumed the currency symbol, so filtering
# afterwards loses the real price too (§4). Spinny has not been observed
# printing a percentage inside a card — its discount badge is in rupees —
# but the guard costs one substitution and the sibling repo that skipped it
# read a 25,999 TRY product as costing 10.34.
_PCT_RE = re.compile(r'-?\s*\d{1,3}(?:[.,]\d+)?\s*%')

# An EMI line lives INSIDE the card, right next to the price: "EMI6,675/m*".
# On a sibling site exactly this shape made a price read return a monthly
# payment (§4). Here the price is read from its own labelled node rather than
# from the card's text, so the EMI cannot be mistaken for it — but the EMI is
# also worth having, so it gets its own pattern instead of being stripped.
_EMI_RE = re.compile(r'EMI\s*(' + _AMOUNT + r')\s*/\s*m', re.I)

# "63.5K km" / "117K km" / "980 km". The K suffix is the site's own rounding
# and is why `km_driven` is documented as approximate.
_KM_RE = re.compile(r'^(' + _AMOUNT + r')\s*(K?)\s*km$', re.I)

# A registering-authority code: two letters, one or two digits, sometimes a
# letter series. "HR26", "DL8C", "UP16", "DL10". 78 distinct across 705
# tiles, and the shape is fixed by law rather than by a designer.
_RTO_RE = re.compile(r'^[A-Z]{2}\d{1,2}[A-Z]{0,3}$')

# The card's headline: "2019 Hyundai Grand i10". The year leads it on 705 of
# 705.
_HEADLINE_RE = re.compile(r'^\s*(\d{4})\s+(.+?)\s*$')

# The fuel types Spinny prints, exactly as it spells them — note "Cng"
# rather than "CNG". Measured across three captures: Petrol 610, Diesel 63,
# Hybrid 18, Cng 13, Electric 1, totalling 705 of 705. An unrecognised word
# in this slot leaves `fuel_type` null rather than guessing, and the coverage
# warning below says so.
FUEL_TYPES = ("Petrol", "Diesel", "Cng", "Electric", "Hybrid", "Lpg")
TRANSMISSIONS = ("Manual", "Automatic")


def _normalize_amount(raw: str) -> Optional[float]:
    """Turn a written amount into a number, honouring every grouping seen.

    Four conventions, all real on this site or in this family:

        1,234.56    Western, decimal point
        1.234,56    European, decimal comma
        1 234,56    space grouping (NBSP, narrow NBSP, thin space, space)
        1,26,950    **Indian** — last group three digits, the rest two

    The Indian form is the one the family's inherited version got wrong. It
    is recognised structurally rather than by counting trailing digits: a
    comma-grouped integer whose groups are all two or three digits and whose
    LAST group is three digits is a grouped integer, whichever sizes the
    earlier groups take.
    """
    s = raw.strip()
    for sp in _GROUP_SPACES:
        if sp != " ":
            s = s.replace(sp, " ")
    s = s.replace(" ", ",")
    if not s:
        return None
    has_dot, has_comma = "." in s, "," in s

    if has_dot and has_comma:
        # Whichever separator comes LAST is the decimal point; the other
        # groups. "1,26,950.50" and "1.234,56" both come out right.
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif has_comma:
        groups = s.split(",")
        head, tail = groups[0], groups[1:]
        # Western (1,234 / 1,234,567) and Indian (1,26,950 / 12,34,56,789)
        # both satisfy this: every group after the first is 2 or 3 digits and
        # the last is 3. Anything else is a decimal comma.
        grouped = (bool(tail) and len(head) in (1, 2, 3)
                   and all(g.isdigit() and len(g) in (2, 3) for g in tail)
                   and len(tail[-1]) == 3)
        if grouped:
            s = head + "".join(tail)
        else:
            s = s.replace(",", ".", 1) if len(tail) == 1 else s.replace(",", "")
    elif has_dot:
        head, _, tail = s.rpartition(".")
        if head and len(tail) == 3 and head.replace(".", "").isdigit():
            # "1.234" — a thousands grouping. No currency has a three-digit
            # subunit, so this is 1234 rather than 1.234.
            s = s.replace(".", "")
        # One dot with 1-2 trailing digits is a decimal point and is left
        # alone. That is the NORMAL case here: "3.88 Lakh" must stay 3.88.
    try:
        return float(s)
    except ValueError:
        return None


def scaled_price(text: str) -> Optional[float]:
    """A lakh/crore price in rupees: "3.88 Lakh" -> 388000.0.

    The scale word is mandatory. Without it this would match the "2019" in a
    headline and the "63" in an odometer reading.
    """
    if not text:
        return None
    m = _SCALED_PRICE_RE.search(_PCT_RE.sub(" ", text))
    if not m:
        return None
    val = _normalize_amount(m.group(1))
    if val is None:
        return None
    return round(val * _SCALE_WORDS[m.group(2).lower()], 2)


def rupee_amount(text: str) -> Optional[float]:
    """An amount behind a ₹ symbol, in rupees.

    "₹13,000" -> 13000.0 and "₹2.50L" -> 250000.0. The suffix is the part
    that matters — see the note above _RUPEE_RE.
    """
    if not text:
        return None
    m = _RUPEE_RE.search(_PCT_RE.sub(" ", text))
    if not m:
        return None
    val = _normalize_amount(m.group(1))
    if val is None:
        return None
    scale = _RUPEE_SUFFIX.get((m.group(2) or "").lower(), 1)
    return round(val * scale, 2)


def prices_in(text: str) -> List[float]:
    """Every price in reading order, in rupees.

    Kept under the family's name because the shared smoke-test scaffolding
    and the sibling repos all call it. Reads scaled prices first (the
    displayed form) and falls back to ₹ amounts.
    """
    cleaned = _PCT_RE.sub(" ", text or "")
    out: List[float] = []
    for m in _SCALED_PRICE_RE.finditer(cleaned):
        val = _normalize_amount(m.group(1))
        if val is not None:
            out.append(round(val * _SCALE_WORDS[m.group(2).lower()], 2))
    if out:
        return out
    for m in _RUPEE_RE.finditer(cleaned):
        val = _normalize_amount(m.group(1))
        if val is not None:
            out.append(val)
    return out


def _discount_from(price: Optional[float],
                   original_price: Optional[float]) -> Optional[float]:
    """The discount COMPUTED from the two prices, never read off a badge.

    None rather than 0 or a negative when the figures are not what they were
    taken for, so a canary can assert "no original_price at or below its
    price" and mean it. That assertion is not theoretical here: mixing the
    displayed price with the `data-price` attribute — two different price
    DEFINITIONS, see output_writer.py — produces a negative discount on a
    car that is discounted, and it looks entirely plausible.
    """
    if price is None or original_price is None:
        return None
    if original_price <= 0 or original_price <= price:
        return None
    return round((original_price - price) / original_price * 100, 2)


# ---------------------------------------------------------------------------
# URLs, ids and pagination
# ---------------------------------------------------------------------------
# A car lives at
#   /buy-used-cars/{city}/{make}/{model}/{trim-slug}-{year}/{id}/
# — six segments, on 705 of 705 links across three captures. The id is the
# last one, and it is the ONLY segment that resolves: replacing the four
# middle segments with junk returned the same page, byte for byte
# (1,765,143 bytes both ways, measured 2026-09-11). So the slug is decorative
# and the id is the address.
#
# That is worth knowing and NOT worth exploiting. `url` on every row is the
# site's own href verbatim, because a canonical URL is what a consumer should
# open and what a search engine indexes; the id's sufficiency is recorded
# here only so a future edit knows the middle segments cannot be trusted to
# mean anything.
_CAR_PATH_RE = re.compile(
    r'^/buy-used-cars/(?P<city>[^/]+)/(?P<make>[^/]+)/(?P<model>[^/]+)/'
    r'(?P<trim>[^/]+)/(?P<id>\d+)$')

# A listing path: `used-{something}` then `s`. Every listing URL on the site
# takes this shape — /used-cars-in-delhi-ncr/s/,
# /used-luxury-cars-in-delhi-ncr/s/, /used-volvo-cars-in-karnal/s/,
# /used-cars-under-1-lakh-rs-in-delhi-ncr/s/, /used-automatic-cars/s/ — and
# the trailing `/s/` is what separates a listing from the SEO landing page at
# /used-cars/, which has no grid on it at all.
_LISTING_PATH_RE = re.compile(r'^/(?P<slug>used-[a-z0-9-]*)/s$')
_HUB_PATH_RE = re.compile(r'^/(?P<slug>used-[a-z0-9-]*)$')

# The `-in-{city}` tail a listing slug carries when it is scoped to a city.
# Anchored to the END of the slug so a model called "in-something" cannot be
# mistaken for it.
_CITY_TAIL_RE = re.compile(r'-in-(?P<city>[a-z0-9-]+)$')

# Query parameters that identify the visit rather than the page. Stripped
# before a URL becomes a row's `url` or is compared to a canonical.
TRACKING_PARAMS = frozenset("""
    utm_source utm_medium utm_campaign utm_term utm_content utm_id
    gclid fbclid gad_source gbraid wbraid referrer ref refer src
    cmpid campaign_id adgroup_id creative_id keyword matchtype device
    """.split())

# Makes as Spinny displays them, keyed by the slug its URLs use. The keys are
# the twenty makes observed across three captures; the values are the
# display names the site's own pages use.
#
# This map exists because THE HEADLINE ABBREVIATES THE MAKE — 178 of 705
# tiles print "Maruti Swift" for a maruti-suzuki and "Mercedes CLA" for a
# mercedes-benz — so the headline cannot be the source and the slug has to be
# turned back into a name. An unmapped slug falls through to title-casing,
# which is right for a single-word make and is why the map only needs the
# awkward ones.
MAKE_DISPLAY = {
    "maruti-suzuki": "Maruti Suzuki",
    "mercedes-benz": "Mercedes Benz",
    "land-rover": "Land Rover",
    "mg-motors": "MG Motors",
    "bmw": "BMW",
    "mg": "MG",
}


def _slug_display(slug: Optional[str]) -> Optional[str]:
    """A URL slug as a display name: "grand-i10" -> "Grand I10".

    Deliberately dumb, and deliberately not applied to the make (which has
    MAKE_DISPLAY). A model name's casing is genuinely inconsistent on the
    site itself — "Grand i10" in a headline, "grand-i10" in a URL, "Grand
    i10" in the API — so anything cleverer here would be inventing a
    convention the site does not have. §13 calls inconsistent brand casing a
    trap worth documenting rather than one worth fixing.
    """
    if not slug:
        return None
    return " ".join(w.capitalize() for w in slug.split("-")) or None


def make_display(slug: Optional[str]) -> Optional[str]:
    if not slug:
        return None
    return MAKE_DISPLAY.get(slug.lower()) or _slug_display(slug)


def model_from_headline(headline: Optional[str], brand: Optional[str],
                        model_slug: Optional[str] = None) -> Optional[str]:
    """The model as SPINNY spells it, out of the card's own headline.

    The headline is "{year} {Make} {Model}" — "2018 Mercedes CLA", "2022
    Maruti Wagon R" — so the model is what is left once the year and the make
    come off. That is worth the trouble because the alternative is
    title-casing the URL slug, and the slug's casing is not the site's:
    `cla` would become "Cla" and `grand-i10` would become "Grand I10", where
    the site writes "CLA" and "Grand i10".

    The make has to be stripped by TRYING ITS WORDS, longest prefix first,
    because the headline abbreviates it: "Mercedes Benz" is printed
    "Mercedes" and "Maruti Suzuki" is printed "Maruti" — 178 of 705 tiles.
    So "Mercedes Benz" is tried, fails, and "Mercedes" succeeds.

    Falls back to the slug when the headline is missing or does not start
    with any prefix of the make, which is better than nothing and is flagged
    by the `model` coverage floor if it ever becomes the common case.
    """
    if not headline:
        return _slug_display(model_slug)
    rest = headline.strip()
    m = _HEADLINE_RE.match(rest)
    if m:
        rest = m.group(2).strip()
    words = (brand or "").split()
    for take in range(len(words), 0, -1):
        prefix = " ".join(words[:take])
        if rest.lower().startswith(prefix.lower()):
            candidate = rest[len(prefix):].strip()
            if candidate:
                return candidate
            break
    return _slug_display(model_slug)


def strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), ""))


def car_path(href: str, base_url: str = "") -> Optional[str]:
    """The `/buy-used-cars/.../{id}/` path of a car link, or None.

    Returns the path WITHOUT its trailing slash, so it is comparable. The
    host is checked when the href carries one: a grid holds links to
    Spinny's own marketing pages and, in the "Similar cars" strip of a detail
    page, to other listings.
    """
    if not href:
        return None
    absolute = urljoin(base_url or "https://www.spinny.com/", href)
    parts = urlsplit(absolute)
    host = (parts.hostname or "").lower()
    if host and host not in HOSTS:
        return None
    path = parts.path.rstrip("/")
    return path if _CAR_PATH_RE.match(path) else None


def _car_parts(href: str, base_url: str = "") -> Optional[Dict[str, str]]:
    path = car_path(href, base_url)
    if not path:
        return None
    m = _CAR_PATH_RE.match(path)
    return m.groupdict() if m else None


def sku_from_url(url: str) -> Optional[str]:
    """The car's numeric id, as a string."""
    parts = _car_parts(url)
    return parts["id"] if parts else None


def car_city_from_url(url: str) -> Optional[str]:
    """The CAR's own city, from its detail-page path.

    Not the listing's city, which is often a region: a
    /used-cars-in-delhi-ncr/ run returned cars in seven different cities.
    """
    parts = _car_parts(url)
    return parts["city"] if parts else None


def make_from_url(url: str) -> Optional[str]:
    parts = _car_parts(url)
    return parts["make"] if parts else None


def model_from_url(url: str) -> Optional[str]:
    parts = _car_parts(url)
    return parts["model"] if parts else None


def listing_kind(url: str) -> str:
    """"listing" · "cityless" · "detail" · "hub" · "unknown".

    `cityless` is its own kind rather than a flavour of `listing` because it
    behaves differently and predictably: /used-cars/s/ is served with a grid
    container and NO cars, because Spinny scopes inventory by city and the
    path names none. Classified as an ordinary listing it would look like a
    site outage; classified as its own kind the engines can say what is
    actually wrong, which is the URL.
    """
    path = urlsplit(url).path.rstrip("/") or "/"
    if _CAR_PATH_RE.match(path):
        return "detail"
    m = _LISTING_PATH_RE.match(path)
    if m:
        return "listing" if _CITY_TAIL_RE.search(m.group("slug")) else "cityless"
    if _HUB_PATH_RE.match(path) or path == "/":
        return "hub"
    return "unknown"


def is_listing(url: str) -> bool:
    return listing_kind(url) in ("listing", "cityless")


def city_from_url(url: str) -> Optional[str]:
    """The city (or region) a listing URL is scoped to, or None."""
    path = urlsplit(url).path.rstrip("/") or "/"
    for rx in (_LISTING_PATH_RE, _HUB_PATH_RE):
        m = rx.match(path)
        if m:
            c = _CITY_TAIL_RE.search(m.group("slug"))
            return c.group("city") if c else None
    return None


def unknown_city_warning(url: str) -> Optional[str]:
    """A warning if the URL names a city this repo has not seen, else None.

    A WARNING and not a refusal, on purpose. Spinny opens cities, and CITIES
    is a snapshot of its footer taken on one day; refusing a URL the site
    serves would make this scraper stale faster than the site changes.
    """
    city = city_from_url(url)
    if city and city not in CITIES:
        return (f"{city!r} is not in the 33 cities read off Spinny's own "
                f"footer on 2026-09-11. If the site serves it, this run will "
                f"work — the list is a snapshot, not a gate.")
    return None


# ===========================================================================
# PAGINATION — read this whole block before writing anything that builds a
# page URL.
# ===========================================================================
# **A Spinny listing has exactly one page and `?page=N` is silently ignored.**
#
# Measured 2026-09-11, three fetches through a real browser:
#
#   /used-cars-in-delhi-ncr/s/           first ids: 31483859, 31064888, ...
#   /used-cars-in-delhi-ncr/s/?page=2    first ids: 31483859, 31064888, ...
#   /used-cars-in-delhi-ncr/s/?page=3    first ids: 31483859, 31064888, ...
#
# Identical. Not an error, not an empty result — page one, three times, under
# HTTP 200. And the page publishes no pagination markup of any kind: zero
# `link[rel=next]`, zero `page=` hrefs, no "Load more" and no numbered
# anchors on a fully scrolled 15 MB capture.
#
# This is the §7 failure in its most dangerous form. A `page_url()` that
# built `?page=N` unconditionally would fetch page one N times, find no new
# id after the first, conclude the listing was exhausted, and report a
# **complete** run holding one twentieth of a Delhi catalogue. Every layer of
# §7's defence except the last would pass: the selector layer finds nothing
# (correct), the constructed-URL layer builds something that returns 200
# (wrong), and only the data layer — "this page added no new sku" — notices.
#
# So `paginates_by_url` is False for every URL on this site, `page_url`
# returns None above page 1, and the engines scroll one long page instead.
# `--concurrency` above 1 is refused with that reason (§18): page 5 of an
# infinitely scrolling grid has no address to hand a worker.
PAGE_PARAM = "page"
PAGINATED_KINDS: Tuple[str, ...] = ()


def paginates_by_url(url: str) -> bool:
    """Whether page N of this listing has an address of its own.

    Always False on Spinny. See the block above — this is measured, not
    cautious, and the measurement is that `?page=2` returns page 1.
    """
    return listing_kind(url) in PAGINATED_KINDS


def page_url(url: str, page_num: int) -> Optional[str]:
    """The address of page `page_num`, or None if the listing has none.

    Page 1 is the URL itself with tracking stripped. Anything above 1 is
    None, because on this site it does not exist — and returning a
    constructed `?page=N` would be worse than returning nothing, since the
    site answers it with page 1 under HTTP 200.
    """
    if page_num <= 1:
        return strip_tracking(url)
    return None


def page_number_from_url(url: str) -> int:
    """Always 1: `?page=` in a Spinny URL does not select a page.

    The parameter is still READ rather than ignored, so an engine handed a
    `?page=4` URL by a user can say why it will not do what they expect.
    """
    return 1


def requested_page_in_url(url: str) -> Optional[int]:
    """A `?page=N` the caller put in the URL, so an engine can warn about it."""
    for k, v in parse_qsl(urlsplit(url).query):
        if k == PAGE_PARAM:
            try:
                n = int(v)
            except ValueError:
                return None
            return n if n > 1 else None
    return None


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------
# The category is what the listing URL SELECTS, with the city removed:
#
#   /used-cars-in-delhi-ncr/s/              -> "cars"
#   /used-luxury-cars-in-delhi-ncr/s/       -> "luxury-cars"
#   /used-volvo-cars-in-karnal/s/           -> "volvo-cars"
#   /used-cars-under-1-lakh-rs-in-delhi-ncr/s/ -> "cars-under-1-lakh-rs"
#   /used-automatic-cars/s/                 -> "automatic-cars"
#
# The city comes out because the same category in two cities is one category,
# and the city has its own column.
def category_from_url(url: str) -> Optional[str]:
    """The category slug a listing URL names, or None."""
    path = urlsplit(url).path.rstrip("/") or "/"
    for rx in (_LISTING_PATH_RE, _HUB_PATH_RE):
        m = rx.match(path)
        if not m:
            continue
        slug = m.group("slug")
        slug = _CITY_TAIL_RE.sub("", slug)
        slug = slug[len("used-"):] if slug.startswith("used-") else slug
        return slug or None
    return None


# ---------------------------------------------------------------------------
# Page state: served, refused, empty, not-found
# ---------------------------------------------------------------------------
# Spinny gives a refused request no branded page to detect — the development
# machine never met a refusal at all, from either exit tested — so detection
# here is INVERTED, per §8 and §18: look for a marker every REAL page carries
# and treat its absence as the signal. A served page is assembled out of the
# site's own assets; an interstitial, and Chromium's own network-error page,
# are not.
#
# Measured over seven captures, 2026-09-11:
#
#   fully scrolled Delhi listing      846 asset references
#   fully scrolled Bangalore listing  503
#   luxury listing                    327
#   cityless listing (empty grid)     267
#   detail page                      1040
#   zero-result listing               267
#   Spinny's own 404 page               3
#   Chromium's proxy-error page         0
#
# The gap between 267 and 3 is wide enough that the threshold is not doing
# delicate work. It is set at 10.
_ASSET_MARKER = re.compile(
    r'(?:spn-sta|spn-mda|assets|mda)\.spinny\.com', re.I)
_ASSET_MIN_MATCHES = 10

# Vendor challenge markers.
#
# **`recaptcha` is NOT in this list, and that is the most important line in
# this file's detection half.** Spinny loads reCAPTCHA Enterprise v3 on
# EVERY page it serves — `www.google.com/recaptcha/enterprise.js?render=6Lc_rqYo…`,
# an invisible widget whose badge is hidden by the site's own CSS, and a
# `g-recaptcha-response` textarea in the DOM. There were 31 occurrences of
# the string "recaptcha" and 3 of "g-recaptcha" on a page that had just
# served 482 cars.
#
# So `recaptcha`, `g-recaptcha`, `grecaptcha-badge` and
# `recaptcha/api.js`-style loader markers are facts about the site, not
# markers of a block — and a marker that matches every page is worse than no
# marker (§18, proven twice on a sibling site in two consecutive releases).
# The sibling repos' lists contain `g-recaptcha` and `recaptcha/api.js` and
# were copied here; both were REMOVED after counting them on a page known
# good, which is the check §18 asks for.
#
# What IS a signal is a challenge actually RENDERED, which v3-invisible never
# is on a served page: the anchor and bframe iframes of a v2 or of an
# executed enterprise challenge, and any other vendor's widget.
BOT_CHALLENGE_MARKERS = (
    "recaptcha/api2/anchor",             # a RENDERED reCAPTCHA widget
    "recaptcha/api2/bframe",             # ...and its challenge frame
    "recaptcha/enterprise/bframe",       # the enterprise equivalent
    "hcaptcha.com",
    "challenges.cloudflare.com",
    "px-captcha",
    "_Incapsula_Resource",
    "datadome",
    "awswaf",
    # Akamai's own refusal page, which is what a sibling site answers with.
    # Spinny is not behind Akamai — zero `akamai` references on any capture —
    # so these are listed as shapes a refusal could take and are marked
    # unobserved rather than implied to be tested.
    "Request unsuccessful",
    "Reference #",
)

# THE SITE'S OWN reCAPTCHA, read structurally rather than as a substring.
#
# Spinny wires reCAPTCHA Enterprise v3 with sitekey
# `6Lc_rqYoAAAAAHwcTbMntlDkC52H6QAYgYE7eUKp`, loaded as
# `enterprise.js?render=<sitekey>`. `render=<sitekey>` means v3;
# `render=explicit` would mean v2 (§8). It is present and invisible on every
# page, and no challenge has ever been rendered into it.
#
# So the machinery is configured everywhere and fires nowhere, which is §18's
# "no challenge rendered is not no captcha configured" exactly. What this
# function is for is telling the solver WHICH task type to buy if one ever
# appears: an enterprise v3 score task, not a v2 checkbox, and the two are
# priced and solved differently.
_RECAPTCHA_LOADER_RE = re.compile(
    r'recaptcha/(?P<flavour>enterprise|api)\.js\?[^"\']*\brender=(?P<render>[^"&\']+)',
    re.I)


def recaptcha_config(html: str) -> Optional[Dict[str, str]]:
    """The site's own reCAPTCHA configuration, or None.

    Returns {"sitekey", "version", "enterprise"}. Present on every page
    Spinny serves; `version` is "v3" when the loader carries a sitekey in
    `render=` and "v2" when it carries `explicit`, which is the loader-wins
    reconciliation §8 describes.
    """
    for m in _RECAPTCHA_LOADER_RE.finditer(html or ""):
        render = m.group("render")
        return {
            "sitekey": "" if render == "explicit" else render,
            "version": "v2" if render == "explicit" else "v3",
            "enterprise": "true" if m.group("flavour") == "enterprise" else "false",
        }
    return None


_EXTENSION_TAG_RE = re.compile(
    r'<script\b[^>]*\bsrc\s*=\s*["\'](?:chrome|moz)-extension://[^"\']*["\'][^>]*>'
    r'(?:\s*</script>)?', re.I)


def _strip_extension_tags(html: str) -> str:
    """Remove browser-extension script tags before looking for markers.

    The Scraping Browser API ships an auto-solve extension that injects its
    own captcha hunters into every page it loads, and its markup mentions the
    vendors it hunts — including `cf-turnstile`, which is why a sibling
    repo's first live run reported exit 3 on a 1.8 MB page holding the full
    catalogue (§8).
    """
    return _EXTENSION_TAG_RE.sub(" ", html or "")


def _strip_scripts(html: str) -> str:
    """The markup with `<script>` and `<style>` bodies removed.

    Load-bearing, not tidy. **Spinny's no-results copy ships inside
    `window.__INITIAL_STATE__` on every listing page**, as configuration:

        no_car_found_data: "Oops. No cars found for your search"

    So a text search for that sentence matches a page holding 482 cars — the
    §18 trap again, arriving through a JS blob rather than a vendor script.
    Measured: "No cars found" appears once in the raw markup of every listing
    capture including the full Delhi one, and ZERO times in any of them once
    scripts are removed.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.decode()


# Spinny's own not-found page. It answers HTTP 404, carries its own branding
# and three asset references, and says so in plain English. Detected by its
# OWN copy rather than by the asset threshold, because it IS a Spinny page —
# §17's ordering rule: a marker only the real page can carry outranks a
# threshold, whichever is cheaper to check.
_NOT_FOUND_MARKERS = (
    "Spinny | Not Found Page",
    "the page you're looking for doesn't exist",
    "Take a U-turn to get back on the right track",
)

# The zero-result listing, which is a SUCCESS and not a block. A real
# example: /used-volvo-cars-in-karnal/s/ answers 200 with the filter rail,
# the footer, a heading reading "0 Used Volvo cars in Karnal" and an
# alert-signup card where the grid would be.
#
# **There is exactly one signal for it, and it is arithmetic: the heading's
# own count reading 0.** No text marker survives §18's test.
#
# The obvious candidates were tried and all three failed it. Spinny's
# no-results sentence ("Oops. No cars found for your search") ships inside
# `window.__INITIAL_STATE__` as configuration on EVERY listing page, so a
# text search for it matches a page holding 482 cars. And the alert-signup
# card — "Do you wish to setup an alert?", "We'll notify you when similar
# cars are added to our inventory", "Notify me" — turned out to be a marker
# of a FILTERED listing rather than an empty one: it appears once on the
# zero-result Volvo page and once on the luxury listing that rendered 41
# cars, and not at all on the two unfiltered city listings. Counted on a page
# known good, exactly as §18 requires, and discarded on the strength of that
# count.
#
# So the count it is. It is the site's own arithmetic, it cannot appear on a
# page that has cars, and it is checked against the script-stripped markup
# so a template string cannot reach it.
_NO_RESULTS_MARKERS: Tuple[str, ...] = ()

_HEADING_COUNT_RE = re.compile(
    r'^\s*([\d,]+)\s+Used\b(?P<what>[^|]*?)\bcars?\s+in\b', re.I)


def total_available(html: str, shown: int = 0) -> Optional[int]:
    """The total Spinny itself advertises for this listing, or None.

    Read from the site's own heading — `<span>1559</span><h1>Used cars in
    Delhi NCR</h1>` — and this is the completeness oracle §8 asks for. A run
    that gathered 482 rows against an advertised 1559 is PARTIAL by
    arithmetic, with no threshold and no guess involved.

    Three warnings that cost time to establish:

      * It is not the number in the `<title>`. The title said 1546 where the
        heading said 1559 on the same capture, and the API's own
        `total_available_cars_in_city` agreed with the heading.
      * A listing can advertise MORE than it will render. The luxury listing
        advertised 41 and rendered 41; the Delhi listing advertised 1559 and
        the grid stopped growing long before that.
      * The heading is present on a zero-result page, reading 0. That is the
        state signal, so the 0 has to be returned as 0 and not folded into
        None.

    `shown` is accepted so the family's shared signature holds.
    """
    if not html:
        return None
    soup = BeautifulSoup(_strip_scripts(html), "html.parser")
    for node in soup.select(SELECTORS["heading_content"]):
        m = _HEADING_COUNT_RE.match(
            re.sub(r"\s{2,}", " ", node.get_text(" ", strip=True)))
        if m:
            val = _normalize_amount(m.group(1))
            if val is not None:
                return int(val)
    return None


def listing_heading(html: str) -> Optional[str]:
    """The listing's own heading, verbatim, for the sidecar.

    Worth recording beside the total because it names what the listing
    actually selected — "Used Luxury cars in Delhi NCR" — which is the
    site's own description of the run's subject and is not always what the
    URL slug suggests.
    """
    if not html:
        return None
    soup = BeautifulSoup(_strip_scripts(html), "html.parser")
    for node in soup.select(SELECTORS["heading_content"]):
        text = re.sub(r"\s{2,}", " ", node.get_text(" ", strip=True))
        if _HEADING_COUNT_RE.match(text):
            return text[:200]
    return None


def total_pages(html: str) -> Optional[int]:
    """Always None: there is no pagination markup on this site at all.

    Kept because the family's engines call it.
    """
    return None


def detect_bot_challenge(html: str, url: str = "") -> Optional[str]:
    """The name of a challenge vendor found in the markup, or None.

    Broad on purpose (§8) — different geos and scenarios surface different
    challenges, and a detection is only a warning unless the page also has no
    cars on it. But NOT so broad that it matches the site's own invisible
    reCAPTCHA, which is on every page: see BOT_CHALLENGE_MARKERS.
    """
    if not html:
        return None
    lowered = _strip_extension_tags(html).lower()
    for marker in BOT_CHALLENGE_MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


def served_by_spinny(html: str) -> bool:
    """Whether this markup is a page Spinny itself built.

    Inverted detection (§8, §18). Worth having even where no block page has
    been observed, because it is the only thing that classifies **Chromium's
    own network-error page** correctly — on a sibling site that page carried
    `<title>www.tokopedia.com</title>`, so a title check called it real, and
    it held no vendor marker anywhere. Spinny's asset hosts appear 267 to
    1040 times on a real page and 0 times on a browser error page.
    """
    if not html:
        return False
    return len(_ASSET_MARKER.findall(html)) >= _ASSET_MIN_MATCHES


def is_not_found(html: str, status: Optional[int] = None) -> bool:
    """Whether this is Spinny's own 404 page.

    The status is the primary signal where a driver exposes one — Playwright
    does, pyppeteer and Selenium do not at the point this is called — and the
    page's own copy is the structural fallback, so all three engines agree.
    """
    if status == 404:
        return True
    if not html:
        return False
    text = _strip_scripts(html)
    return any(m.lower() in text.lower() for m in _NOT_FOUND_MARKERS)


def is_no_results(html: str) -> bool:
    """Whether a listing page was served and has no cars to put on it.

    The site's own heading count reading exactly 0 — see the note above
    `_NO_RESULTS_MARKERS` for why nothing textual is used here. The empty
    marker tuple is still consulted so that a future marker which DOES pass
    §18's count-it-on-a-good-page test can be added in one place.
    """
    if not html:
        return False
    if total_available(html) == 0:
        return True
    if not _NO_RESULTS_MARKERS:
        return False
    text = _strip_scripts(html).lower()
    return any(m.lower() in text for m in _NO_RESULTS_MARKERS)


def detect_page_state(html: Optional[str], status: Optional[int] = None,
                      url: str = "") -> str:
    """One of "content" · "empty" · "notfound" · "blocked" · "challenge" ·
    "unknown".

    The engines must not each re-derive this: three copies of the triage
    across three engines drift, and the drift is silent — one engine
    reporting exit 3 where its twin reports exit 0 on the same page. The
    retry/solve/blocked decision that follows lives in
    `page_flow.STATE_POLICY` as data.

    **The order of these checks is the whole design**, per §17: signals are
    ordered by how much they PROVE, not by how cheap they are. Spinny's own
    404 copy and its own zero-count heading are unambiguous positives that
    only a real Spinny page can carry, so they outrank the asset-reference
    threshold — which is a heuristic, and which classified a minimal real
    page as blocked on a sibling site when it was checked first.
    """
    if html is None:
        # No markup reached us at all — a dead proxy, a reset connection, a
        # navigation that never committed. Reported as blocked rather than
        # crashed so the run can rotate an exit and try again.
        return "blocked"
    if not html.strip():
        return "blocked"

    # 1. Spinny's own not-found page, which IS a Spinny page and has to be
    #    recognised before any threshold: it carries three asset references
    #    where a real page carries 267 to 1040, so the inverted check below
    #    would call it blocked and send the reader hunting for a proxy
    #    problem that is really a typo. The engines turn this into exit 2 and
    #    name the URL.
    if is_not_found(html, status):
        return "notfound"

    if status is not None and status not in (200, 304):
        # A non-200 that is not the 404 handled above. Never observed from
        # this host; reported as blocked because whatever it is, it is not a
        # page with cars on it.
        return "blocked"

    kind = listing_kind(url)
    soup = BeautifulSoup(html, "html.parser")

    # 2. THE GRID, WITH CARS IN IT — and this comes before every
    #    emptiness check AND before the asset-reference threshold below.
    #
    #    The ordering is not cosmetic. `/used-luxury-cars-in-delhi-ncr/s/` is
    #    a filtered listing, so it carries the alert-signup card that the
    #    zero-result page carries, and an earlier draft of this function
    #    reported it EMPTY while holding 41 cars. Cars on the page outrank
    #    every signal that the page might not have any.
    if kind in ("listing", "cityless", "unknown") and _count_cards(soup, url):
        return "content"

    # 3. A detail page has no grid; what it has is a car. Its own
    #    ["Product","Car"] block is the content signal, so an engine in
    #    --mode detail is not told "unknown" about a page it parsed fine.
    if kind == "detail" and _car_jsonld(html) is not None:
        return "content"

    # 4. A served listing with nothing on it, by the site's own arithmetic:
    #    a heading reading "0 Used Volvo cars in Karnal".
    if is_no_results(html):
        return "empty"

    # 5. ONLY NOW the heuristic: was this built out of Spinny's own assets?
    #    If not, Spinny did not build it — an interstitial, or the browser's
    #    own error page.
    #
    #    It sits BELOW the three checks above because of §17's ordering rule:
    #    signals go in order of how much they PROVE, and a threshold is
    #    always weaker than a marker only the real page can carry. Cars in
    #    the site's own grid container, the site's own car JSON-LD and the
    #    site's own zero-count heading are each unforgeable by an
    #    interstitial; "at least ten references to assets.spinny.com" is a
    #    count, and a real page that happens to carry nine would be reported
    #    blocked — exit 3 for a correct answer, sending the reader hunting
    #    for a proxy problem that does not exist. This repo's own trimmed
    #    detail fixture is exactly that page, and a sibling repo shipped the
    #    same mistake live.
    if not served_by_spinny(html):
        return "blocked"

    # 6. An SEO landing page (/used-cars/). It answers 200 with banners,
    #    city links and FAQ copy, and has no grid on it at all — "empty"
    #    rather than "unknown", so the engines report exit 4 instead of
    #    retrying a page that will never have cars on it.
    if kind == "hub":
        return "empty"

    # 7. A listing URL with no city in it. Spinny scopes inventory by city;
    #    with none in the path it renders the grid container, no cars, and a
    #    heading whose city name is simply missing ("1546 Used cars in").
    #    Reached only once step 3 has established there are no cars, so a
    #    cityless URL that DOES serve cars from some exit is still read
    #    correctly.
    if kind == "cityless":
        return "empty"

    # A challenge only counts once everything better has been ruled out.
    challenge = detect_bot_challenge(html, url)
    if challenge:
        return "challenge"

    # Served by Spinny, no grid, no no-results heading. On this site that is
    # almost always "the XHR has not landed yet" — the first response to a
    # listing URL is a 1.6-2 MB SHELL with no cards in it at all, and the
    # grid arrives afterwards. WAIT, do not retry, and do not rotate the
    # exit. See page_flow.is_unpainted.
    return "unknown"


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------
def _grid_of(soup):
    return soup.select_one(SELECTORS["grid"])


def _cards(soup, url: str = "") -> List:
    """Every car card in the grid, grid-scoped and link-anchored.

    Both halves of that are load-bearing:

      * **Grid-scoped**, because a detail page's "Similar cars" strip and a
        listing's own recommendation carousels hold car links too, and a
        document-wide anchor sweep would report them as rows of the listing.
      * **Link-anchored**, because the grid also holds SKELETON cards the
        site has not hydrated — 6 of 188 in one Bangalore capture, with no
        link and no text — and CTA cards whose `data-label` reads
        `loan-learn-more` or `notify-btn`. Requiring the car link is what
        separates a car from both.

    Cards are returned in document order, which is the order Spinny laid the
    grid out.
    """
    grid = _grid_of(soup)
    if grid is None:
        return []
    out = []
    for card in grid.select(SELECTORS["card"]):
        link = card.select_one(SELECTORS["item_link"])
        if link is None or not car_path(link.get("href"), url):
            continue
        # A card that CONTAINS another card would double-count its child.
        # Not observed on any capture — Spinny's cards are siblings — but one
        # nested card would otherwise add a row holding its parent's data,
        # which is §4's tile-scoping failure from the other direction.
        if card.find(attrs={"data-base-component": "card"}) is not None:
            continue
        out.append(card)
    return out


def _count_cards(soup, url: str = "") -> int:
    return len(_cards(soup, url))


def count_cards(html: str, url: str = "") -> int:
    """How many cars the grid holds. Used by the engines' readiness wait."""
    return _count_cards(BeautifulSoup(html or "", "html.parser"), url)


def _card_text_nodes(card) -> List[str]:
    """The card's labelled text nodes, in document order, de-duplicated.

    Nested spans repeat their parent's text — "4-star NCAP rating & 1 more
    reason to buy" contains "& 1 more reason to buy" — so a node whose text
    is a suffix of the previous one is dropped. 104 of 705 tiles have that
    shape.

    Joined with a SPACE rather than with nothing, because the site nests
    those spans without whitespace between them and `get_text("")` welds the
    two into "Audi pre sense& 2 more reasons to buy". The patterns below are
    written to tolerate the space either way ("EMI 6,675 /m" and
    "EMI6,675/m" both match), so this only affects the two free-text
    fields — which is where it matters.
    """
    out: List[str] = []
    for node in card.select(SELECTORS["card_text"]):
        text = re.sub(r"\s{2,}", " ", node.get_text(" ", strip=True))
        if not text or text == "*":
            continue
        if out and out[-1].endswith(text) and out[-1] != text:
            continue
        out.append(text)
    return out


def _card_fields(card) -> Dict[str, object]:
    """The card's specs, recognised BY PATTERN rather than by position.

    Position does not work here and the measurement says why: 10 of 705 tiles
    print no hub at all, so the field after it shifts up by one on those ten
    and every position-based read is silently wrong. The patterns are
    disjoint — a fuel type is one of six words, a transmission one of two, an
    RTO code matches a legally-fixed shape, an odometer ends in "km", an EMI
    starts with "EMI" — so each node lands in at most one slot.

    Whatever is left over, in order, is the trim and then the editorial tag.
    """
    found: Dict[str, object] = {
        "fuel_type": None, "transmission": None, "rto": None,
        "km_driven": None, "emi_monthly": None, "discount_amount": None,
        "hub": None, "variant": None, "tag": None,
    }
    leftover: List[str] = []
    for text in _card_text_nodes(card):
        if text in FUEL_TYPES and found["fuel_type"] is None:
            found["fuel_type"] = text
            continue
        if text in TRANSMISSIONS and found["transmission"] is None:
            found["transmission"] = text
            continue
        if _RTO_RE.match(text) and found["rto"] is None:
            found["rto"] = text
            continue
        km = _KM_RE.match(text)
        if km and found["km_driven"] is None:
            val = _normalize_amount(km.group(1))
            if val is not None:
                found["km_driven"] = int(round(val * (1000 if km.group(2) else 1)))
            continue
        emi = _EMI_RE.search(text)
        if emi and found["emi_monthly"] is None:
            val = _normalize_amount(emi.group(1))
            if val is not None:
                found["emi_monthly"] = int(round(val))
            continue
        if text.lstrip().startswith(("₹", "₹")) and found["discount_amount"] is None:
            found["discount_amount"] = rupee_amount(text)
            continue
        leftover.append(text)

    # The trim comes first in the card's reading order, right under the
    # headline: "Sportz 1.2 Kappa VTVT". 705/705.
    if leftover:
        found["variant"] = leftover[0]
    # The hub is the one that names a place, and a place has a comma in it —
    # "Sector 27, Faridabad", "Raj Nagar Extension, Ghaziabad". The
    # editorial tag does not ("High quality, less driven" does, which is why
    # the hub is taken as the LAST comma-bearing leftover rather than the
    # first: the tag is printed after the hub on every tile, and the trim is
    # excluded by starting at index 1).
    #
    # 695 of 705. The 10 without one get None rather than the tag.
    place = [t for t in leftover[1:] if "," in t]
    if place:
        found["hub"] = place[0]
    rest = [t for t in leftover[1:] if t != found["hub"]]
    if rest:
        found["tag"] = rest[-1]
    return found


def _card_price(card) -> Tuple[Optional[float], Optional[float]]:
    """(displayed price, displayed strike price) in rupees.

    Both come from LABELLED nodes rather than from the card's text, which is
    what keeps the EMI line — "EMI6,675/m", inside the same card — from ever
    being read as the price. A sibling site's price node contained its own
    instalment line and that is exactly the bug it caused (§4).

    The strike is taken from inside the card only. A detail page's "Similar
    cars" strip carries strikes belonging to other cars, and scoping is the
    only thing that keeps them out.
    """
    node = card.select_one(SELECTORS["price"])
    price = scaled_price(node.get_text("", strip=True)) if node is not None else None
    strike_node = card.select_one(SELECTORS["strike_price"])
    strike = (scaled_price(strike_node.get_text("", strip=True))
              if strike_node is not None else None)
    return price, strike


def _https(src: str) -> str:
    """A Spinny asset URL as https, however the site wrote it.

    The site writes the same image three ways: `//assets.spinny.com/…` in a
    card's `src`, `http://assets.spinny.com/…` in a detail page's JSON-LD,
    and `https://…` elsewhere. One column holding all three would report a
    changed image on every row of a listing-versus-detail diff.
    """
    src = (src or "").strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http://"):
        return "https://" + src[len("http://"):]
    return src


def _card_image(card) -> Optional[str]:
    """The card's car photograph.

    Unlike two sibling repos there is no lazy-load placeholder to guard
    against: Spinny renders a real `assets.spinny.com` URL into `src` on
    every card in the grid, 705 of 705, painted or not. The host is still
    checked positively — a populated column of icon URLs is the failure mode
    §10 warns about — and a protocol-relative `//assets.spinny.com/...` is
    made absolute, because that is how the site writes it.
    """
    img = card.select_one("img[src]")
    if img is None:
        return None
    src = _https(img.get("src") or "")
    if not src:
        return None
    if not _ASSET_MARKER.search(src):
        return None
    return src


def _page_of(position: int, boundaries: Optional[Sequence[int]],
             default: int) -> int:
    """Which scroll batch a card at `position` first appeared in.

    `boundaries[i]` is how many cards the grid held after batch i+1 — a count
    the engine takes through the browser after every scroll round, which is a
    cheap `querySelectorAll().length` rather than a re-parse of a 15 MB
    document.

    A card at position p belongs to the FIRST batch whose count reached p.
    With no boundaries (a single-shot parse, or --mode detail) every row gets
    `default`.
    """
    if not boundaries:
        return default
    for i, count in enumerate(boundaries, start=1):
        if position <= count:
            return i
    return len(boundaries)


def parse_products(html: str, url: str, page: int = 1,
                   category: Optional[str] = None,
                   page_boundaries: Optional[Sequence[int]] = None) -> List[Product]:
    """Every car in the grid, in the order Spinny laid them out.

    One path, not two: there is no structured data on a listing page to
    prefer. See the module docstring.

    `page_boundaries` is how a run that scrolled one long page still reports
    a meaningful `page` per row — see `_page_of`. It is optional so the
    signature stays compatible with the rest of the family, and so a caller
    with a single snapshot can pass nothing.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    cards = _cards(soup, url)
    if not cards:
        return []

    cat = category or category_from_url(url)
    host = site_host(url) or SOURCE_DEFAULT
    rows: List[Product] = []
    attr_mismatches = 0

    for position, card in enumerate(cards, start=1):
        link = card.select_one(SELECTORS["item_link"])
        parts = _car_parts(link.get("href"), url)
        if not parts:
            continue

        price, strike = _card_price(card)
        fields = _card_fields(card)

        # The shortlist heart carries the EXACT all-in price and Spinny's own
        # quality tier. `data-label` on the same element is the car id, and
        # it is used ONLY as a cross-check: it agreed with the URL on 705 of
        # 705, and a disagreement means the card's scope is wrong, which is
        # worth a warning rather than a silent row.
        shortlist = card.select_one(SELECTORS["shortlist"])
        all_in = assurance = None
        if shortlist is not None:
            all_in = _normalize_amount(shortlist.get("data-price") or "")
            assurance = (shortlist.get("data-category") or "").strip() or None
            label = (shortlist.get("data-label") or "").strip()
            if label and label != parts["id"]:
                attr_mismatches += 1

        headline = card.select_one(SELECTORS["card_heading"])
        title = headline.get_text(" ", strip=True) if headline is not None else None
        year = None
        if title:
            m = _HEADLINE_RE.match(title)
            if m:
                year = int(m.group(1))

        rows.append(Product(
            source=host,
            url=strip_tracking(urljoin(url, link.get("href") or "")),
            sku=parts["id"],
            title=title,
            brand=make_display(parts["make"]),
            price=price,
            # A fact on this site rather than a guess: one country, one
            # hostname, a "₹" written on the card's own discount badge, and
            # a detail page stating `"priceCurrency": "INR"` outright. Still
            # null when no price was found.
            currency=CURRENCY if price is not None else None,
            original_price=strike,
            discount_pct=_discount_from(price, strike),
            rating=None,          # Spinny publishes none — see output_writer
            review_count=None,    # ...nor one of these
            in_stock=None,        # only a detail page states availability
            image_url=_card_image(card),
            category=cat,
            price_source="dom+attr" if all_in is not None else "dom",
            page=_page_of(position, page_boundaries, page),
            position=position,
            model=model_from_headline(title, make_display(parts["make"]),
                                      parts["model"]),
            variant=fields["variant"],
            year=year,
            km_driven=fields["km_driven"],
            fuel_type=fields["fuel_type"],
            transmission=fields["transmission"],
            rto=fields["rto"],
            hub=fields["hub"],
            car_city=parts["city"],
            assurance=assurance,
            price_all_in=all_in,
            discount_amount=fields["discount_amount"],
            emi_monthly=fields["emi_monthly"],
            tag=fields["tag"],
        ))

    if attr_mismatches:
        logger.warning(
            "%d of %d cards had a data-label that disagreed with the id in "
            "their own URL — the card scope may be wrong, which is how one "
            "car reports its neighbour's price: %s",
            attr_mismatches, len(rows), url)
    _report_coverage(rows, url)
    return rows


# Below these shares something is wrong with the READ rather than with the
# page, and a warning naming the number is what turned two silent regressions
# in this family into five-minute fixes.
#
# Every floor here is measured over 705 cards from three captures
# (Delhi 482, Bangalore 182, Delhi luxury 41) taken 2026-09-11. The fields
# that came back 705/705 get 0.98 rather than 1.00, because a single
# hydrating card at the bottom of a grid should not fire a warning; `hub` and
# `discount_amount` get floors below their measured shares because their
# absence is a property of the car (10 tiles print no hub, 20 carry no
# discount) rather than of the read.
_COVERAGE_FLOORS = {
    "price": 0.98,            # measured 705/705
    "price_all_in": 0.98,     # measured 705/705
    "title": 0.98,            # measured 705/705
    "brand": 0.98,            # measured 705/705
    "km_driven": 0.98,        # measured 705/705
    "fuel_type": 0.98,        # measured 705/705
    "transmission": 0.98,     # measured 705/705
    "rto": 0.98,              # measured 705/705
    "variant": 0.98,          # measured 705/705
    "model": 0.98,            # measured 705/705
    "image_url": 0.98,        # measured 705/705
    "assurance": 0.98,        # measured 705/705
    "emi_monthly": 0.95,      # measured 700/705
    "hub": 0.90,              # measured 695/705
    "original_price": 0.80,   # measured 685/705
    "discount_amount": 0.80,  # measured 685/705
}


def _report_coverage(rows: Sequence[Product], url: str) -> None:
    if not rows:
        return
    total = len(rows)
    for field, floor in _COVERAGE_FLOORS.items():
        got = sum(1 for r in rows if getattr(r, field, None) is not None)
        share = got / total
        if share < floor:
            logger.warning(
                "%s coverage %.0f%% (%d/%d), below the %.0f%% this site "
                "normally gives — the markup may have changed: %s",
                field, share * 100, got, total, floor * 100, url)

    # The two price bases, checked against each other. `price_all_in` was
    # HIGHER than `price` on 482 of 482 Delhi tiles, because it includes RC
    # transfer and insurance; one that is lower means the two reads have been
    # crossed, which is the mistake that produces a negative discount.
    crossed = sum(1 for r in rows
                  if r.price is not None and r.price_all_in is not None
                  and r.price_all_in < r.price)
    if crossed:
        logger.warning(
            "%d of %d rows have price_all_in BELOW price. Those are two "
            "different price definitions (see output_writer.py) and the "
            "all-in one was higher on 482 of 482 measured tiles, so this "
            "means the two reads have been crossed: %s",
            crossed, total, url)

    # The computed discount against Spinny's own exact rupee badge. They are
    # on the same basis, so they should agree to within the quantisation of a
    # displayed lakh (±₹500 on each of two figures, so ₹1000).
    checked = disagreed = 0
    for r in rows:
        if (r.price is None or r.original_price is None
                or r.discount_amount is None):
            continue
        checked += 1
        if abs((r.original_price - r.price) - r.discount_amount) > 1000:
            disagreed += 1
    if checked and disagreed / checked > 0.02:
        logger.warning(
            "%d of %d rows disagree by more than ₹1000 between the strike "
            "minus the price and the site's own discount badge — one of the "
            "three is being read from the wrong node: %s",
            disagreed, checked, url)
    logger.info("parsed %d cars from %s", total, url)


# ---------------------------------------------------------------------------
# Detail pages: the site's own structured data
# ---------------------------------------------------------------------------
# A detail page is the one place on Spinny with real structured data about a
# car, and it is good: a `["Product","Car"]` block with the price, the
# currency, the exact odometer reading, the previous-owner count, the colour,
# the seating capacity, the transmission, the fuel type and the registration
# year and month.
#
# Two things about it will break a naive reader, and the first is the reason
# this function exists rather than a generic JSON-LD helper being reused:
#
#   1. **`offers.Price`, with a capital P.** schema.org says `price`. Every
#      other repo in this family reads `price`. Here that returns None on
#      every row of every run, silently.
#   2. `@type` is a LIST — `["Product", "Car"]` — so a reader comparing
#      `@type == "Product"` finds nothing.
#
# And the §4 shapes that are legal schema.org and crash a naive parser apply
# here too: `offers` can legally be null or a list, `image` can be an
# ImageObject, and products can sit under `@graph`. All are handled.
_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)

_CAR_TYPES = frozenset({"car", "vehicle", "product"})


def _jsonld_blocks(html: str) -> List[object]:
    out = []
    for m in _JSONLD_RE.finditer(html or ""):
        try:
            out.append(json.loads(m.group(1)))
        except (ValueError, TypeError):
            # A malformed block is not a reason to lose the good ones.
            logger.debug("unparseable ld+json block")
    return out


def _types_of(node) -> List[str]:
    """The `@type`s of a node, lowercased. Handles the list form."""
    if not isinstance(node, dict):
        return []
    t = node.get("@type")
    if isinstance(t, str):
        return [t.lower()]
    if isinstance(t, list):
        return [x.lower() for x in t if isinstance(x, str)]
    return []


def _car_jsonld(html: str) -> Optional[dict]:
    """The detail page's `["Product","Car"]` block, or None.

    A block is the car if its types include `car` or `vehicle`. A bare
    `Product` is accepted only when it also carries a `productID` — the
    LISTING pages carry a marketing `Product` block whose `aggregateRating`
    rates Spinny rather than a vehicle, and accepting that would put a
    5-star, price-less, id-less row in the output.
    """
    for block in _jsonld_blocks(html):
        candidates = []
        if isinstance(block, list):
            candidates = block
        elif isinstance(block, dict):
            graph = block.get("@graph")
            candidates = graph if isinstance(graph, list) else [block]
        for node in candidates:
            types = set(_types_of(node))
            if not types & _CAR_TYPES:
                continue
            if types & {"car", "vehicle"}:
                return node
            if node.get("productID") is not None:
                return node
    return None


def _offer_of(node: dict) -> Optional[dict]:
    """The car's Offer, whatever legal shape it takes.

    `"offers": null` is legal schema.org and an explicit null is NOT covered
    by a `.get()` default — that only applies to a MISSING key (§4). A list
    of offers, possibly containing non-dicts, is legal too.
    """
    offers = node.get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        for item in offers:
            if isinstance(item, dict):
                return item
    return None


def _offer_price(offer: Optional[dict]) -> Optional[float]:
    """The offer's price, under either spelling.

    `Price` is the only spelling ever observed on this site. `price` is tried
    too, and tried FIRST, so that a future Spinny deploy which fixes the
    capitalisation does not break this and so that the standard spelling is
    what a reader sees.
    """
    if not offer:
        return None
    for key in ("price", "Price", "lowPrice"):
        if key in offer and offer[key] is not None:
            return _to_float(offer[key])
    return None


def _jsonld_image(node: dict) -> Optional[str]:
    """The car's image, in any of the four legal shapes (§4).

    Normalised to https, because the JSON-LD writes `http://assets.spinny.com/…`
    where a listing card writes `//assets.spinny.com/…`. Left as two
    spellings, the same image would read as a changed field on every row of
    a listing-versus-detail diff.
    """
    img = node.get("image")
    if isinstance(img, str):
        return _https(img) or None
    if isinstance(img, dict):
        for key in ("url", "contentUrl"):
            if isinstance(img.get(key), str):
                return _https(img[key]) or None
        return None
    if isinstance(img, list):
        for item in img:
            got = _jsonld_image({"image": item})
            if got:
                return got
    return None


def _additional_properties(node: dict) -> Dict[str, str]:
    """`additionalProperty` as a flat {name: value} map.

    Spinny puts the RTO code, the registration year and the registration
    month here, and it writes the value under `unitText` rather than the
    `value` schema.org expects — which is why this reads both.
    """
    out: Dict[str, str] = {}
    props = node.get("additionalProperty")
    if isinstance(props, dict):
        props = [props]
    if not isinstance(props, list):
        return out
    for prop in props:
        if not isinstance(prop, dict):
            continue
        name = prop.get("name")
        if not isinstance(name, str):
            continue
        for key in ("value", "unitText"):
            if prop.get(key) is not None:
                out[name.strip().lower()] = str(prop[key]).strip()
                break
    return out


def _to_int(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        got = _normalize_amount(value)
        return int(got) if got is not None else None
    return None


def _to_float(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _normalize_amount(value)
    return None


# The detail page's "Car Overview" table, which carries two facts published
# nowhere else and genuinely useful on a used car: when the insurance runs
# out and whether it is comprehensive or third-party only. Rendered as a
# label/value pair, so the value is the text that FOLLOWS the label.
_OVERVIEW_LABELS = {
    "insurance validity": "insurance_validity",
    "insurance type": "insurance_type",
    # The hub, which a listing card prints in its own span and a detail page
    # prints only here. Spinny writes it with a hyphen on a detail page
    # ("Sector-27, Faridabad") and with a space on a card ("Sector 27,
    # Faridabad") — see `_normalise_hub`, because one column spelling the
    # same hub two ways would make every row read as changed between the
    # two modes.
    "car location": "hub",
}


_HUB_HYPHEN_RE = re.compile(r'(?<=[A-Za-z])-(?=\d)|(?<=\d)-(?=[A-Za-z])')


def _normalise_hub(value: Optional[str]) -> Optional[str]:
    """A hub name spelled the way a listing card spells it.

    A detail page writes "Sector-27, Faridabad" where a card writes
    "Sector 27, Faridabad". The hyphen is replaced only where it joins a word
    to a digit, so a genuinely hyphenated place name is left alone.
    """
    if not value:
        return None
    return _HUB_HYPHEN_RE.sub(" ", value).strip() or None


def _overview_pairs(soup) -> Dict[str, str]:
    """The Car Overview table as {label: value}, for the labels we want.

    Read from the page's visible text rather than from a selector, because
    the table is built out of the same `ds-body-*` utility classes as
    everything else on the page and there is nothing to select on. Each label
    is looked for as an exact text node and the value is its next non-empty
    sibling text.
    """
    out: Dict[str, str] = {}
    for node in soup.find_all(string=True):
        label = str(node).strip().lower()
        field = _OVERVIEW_LABELS.get(label)
        if not field or field in out:
            continue
        parent = node.parent
        sibling = parent.find_next(string=True) if parent is not None else None
        hops = 0
        while sibling is not None and hops < 6:
            value = str(sibling).strip()
            if value and value.lower() != label:
                out[field] = re.sub(r"\s{2,}", " ", value)[:100]
                break
            sibling = sibling.find_next(string=True)
            hops += 1
    return out


_MONTH_NAMES = ("january february march april may june july august "
                "september october november december").split()


def parse_detail_page(html: str, url: str,
                      category: Optional[str] = None) -> List[Product]:
    """One car, from its own detail page. A list so callers stay uniform.

    Structured-data first and DOM second, which is the family's normal order
    and is possible here because a detail page — unlike a listing — actually
    has structured data.

    The DISPLAYED price still comes from the DOM, and that is deliberate: the
    JSON-LD price is the all-in figure (392,000 for a car whose page shows
    "3.88 Lakh"), and `price` has to mean the same thing in both modes or a
    diff between a listing run and a detail run reports every row as changed.
    So the displayed price goes in `price` and the structured one in
    `price_all_in`, exactly as on a listing card.

    Verified against a real pair: the listing row and the detail row for car
    31483859 agree on sku, title, brand, price, currency, model, variant,
    year, fuel type, transmission, RTO, hub, city, all-in price and URL —
    16 of 17 columns both modes populate. The one that differs is
    `image_url`, and it differs legitimately: Spinny's structured data names
    a DIFFERENT photograph of the same car than the card does
    (`5edaaa72…` against `7abfc170…`). Worth knowing before reading a
    listing-versus-detail diff, because it is the site publishing two
    pictures rather than this parser reading one badly.
    """
    node = _car_jsonld(html)
    soup = BeautifulSoup(html or "", "html.parser")
    parts = _car_parts(url) or {}

    price_node = soup.select_one(SELECTORS["price"])
    price = (scaled_price(price_node.get_text("", strip=True))
             if price_node is not None else None)

    if node is None and not parts:
        return []

    node = node or {}
    offer = _offer_of(node)
    props = _additional_properties(node)
    overview = _overview_pairs(soup)

    odometer = node.get("mileageFromOdometer")
    km_exact = None
    if isinstance(odometer, dict):
        km_exact = _to_int(odometer.get("value"))
    elif odometer is not None:
        km_exact = _to_int(odometer)

    availability = None
    if offer and isinstance(offer.get("availability"), str):
        availability = "instock" in offer["availability"].lower()

    make_slug = parts.get("make")
    brand = node.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    brand = brand if isinstance(brand, str) and brand.strip() else None

    reg_month = props.get("registration month")
    if reg_month and reg_month.strip().lower() not in _MONTH_NAMES:
        # A month Spinny wrote as something other than a name. Kept verbatim
        # rather than dropped — it is the site's own value — but not parsed
        # into a number, because guessing the convention is how a column
        # starts meaning two things.
        reg_month = reg_month.strip()

    # The headline on a detail page is the car's full name without the year
    # ("Hyundai Grand i10 Sportz 1.2 Kappa VTVT"), while a listing card's is
    # "{year} {Make} {Model}". They are different strings for the same car,
    # so `title` is built to the LISTING's shape here — that is what a diff
    # between the two modes compares.
    year = _to_int(node.get("vehicleModelDate"))
    if year is None:
        model_date = node.get("modelDate")
        if isinstance(model_date, str) and len(model_date) >= 4:
            year = _to_int(model_date[:4])
    model = node.get("model")
    model = model if isinstance(model, str) and model.strip() else _slug_display(
        parts.get("model"))
    title = None
    if year and brand and model:
        title = f"{year} {brand} {model}"

    currency = None
    if offer and isinstance(offer.get("priceCurrency"), str):
        currency = offer["priceCurrency"].strip().upper() or None

    return [Product(
        source=site_host(url) or SOURCE_DEFAULT,
        url=strip_tracking(url),
        sku=(str(_to_int(node.get("productID"))) if node.get("productID") is not None
             else parts.get("id")),
        title=title,
        brand=brand or make_display(make_slug),
        price=price,
        # From the site's own `priceCurrency` when it states one — a FACT at
        # the top of §4's trustworthiness order, never overwritten from the
        # DOM. Falls back to INR only when a price was found and the page
        # stated no currency.
        currency=currency or (CURRENCY if price is not None else None),
        # A detail page prints no strike of its own — 0 line-through nodes on
        # the captured page — and the "Similar cars" strip's strikes belong
        # to other cars. So this is null in detail mode rather than borrowed.
        original_price=None,
        discount_pct=None,
        rating=None,
        review_count=None,
        in_stock=availability,
        image_url=_jsonld_image(node),
        category=category or category_from_url(url),
        price_source="dom+jsonld" if price is not None else "jsonld",
        page=None,
        position=None,
        model=model,
        # A detail page's JSON-LD `name` is "{Brand} {Model} {Trim}", so the
        # trim is what is left once the brand and model are taken off.
        variant=_trim_from_name(node.get("name"), brand, model),
        year=year,
        # The tile's rounded figure is not available here; the exact one is,
        # and putting the exact number in the rounded column would make the
        # two modes' `km_driven` mean different things. So `km_driven` stays
        # null in detail mode and `km_driven_exact` carries the truth.
        km_driven=None,
        fuel_type=_title_word(node.get("fuelType")),
        transmission=_title_word(node.get("vehicleTransmission")),
        rto=props.get("rto") or None,
        hub=_normalise_hub(overview.get("hub")),
        car_city=parts.get("city"),
        assurance=None,
        price_all_in=_offer_price(offer),
        discount_amount=None,
        emi_monthly=None,
        tag=None,
        km_driven_exact=km_exact,
        owners=_to_int(node.get("numberOfPreviousOwners")),
        registration_year=_to_int(props.get("registration year")),
        registration_month=reg_month,
        color=node.get("color") if isinstance(node.get("color"), str) else None,
        seating_capacity=_to_int(node.get("vehicleSeatingCapacity")),
        insurance_validity=overview.get("insurance_validity"),
        insurance_type=overview.get("insurance_type"),
    )]


def _title_word(value) -> Optional[str]:
    """A JSON-LD lowercase enum as the site's own display casing.

    Spinny's structured data writes "petrol" and "manual" where its cards
    write "Petrol" and "Manual". Capitalising here is what lets a listing row
    and a detail row for the same car compare equal on `fuel_type` — without
    it, every row would read as changed between the two modes.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().capitalize()


def _trim_from_name(name, brand: Optional[str],
                    model: Optional[str]) -> Optional[str]:
    """The trim, from JSON-LD `name` with the brand and model removed.

    "Hyundai Grand i10 Sportz 1.2 Kappa VTVT" minus "Hyundai" and "Grand
    i10" is "Sportz 1.2 Kappa VTVT", which is byte-identical to what the
    listing card prints for the same car.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    rest = name.strip()
    for prefix in (brand, model):
        if prefix and rest.lower().startswith(prefix.lower()):
            rest = rest[len(prefix):].strip()
    return rest or None


def car_metadata(html: str, url: str) -> Dict[str, Optional[str]]:
    """Facts about the RUN rather than about any row, for the sidecar.

    In --mode detail that is the car's own hub and its quality report's
    headline, which describe one car and are therefore run-level when the run
    covers exactly one car.
    """
    soup = BeautifulSoup(_strip_scripts(html or ""), "html.parser")
    text = re.sub(r"\s{2,}", " ", soup.get_text(" ", strip=True))
    report = re.search(r'(\d[\d,]*)\s+parts evaluated by\s+(\d+)', text, re.I)
    overview = _overview_pairs(soup)
    return {
        "car_url": strip_tracking(url),
        "parts_evaluated": report.group(1).replace(",", "") if report else None,
        "inspectors": report.group(2) if report else None,
        "insurance_validity": overview.get("insurance_validity"),
        "insurance_type": overview.get("insurance_type"),
    }
