#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for spinny-scraper.

Run this FIRST, before touching a real browser or spinny.com, to confirm the
parsing, the output contract, the page-state policy and the shared engine
decisions still hold:

    python3 smoke_test.py        # exits non-zero if anything failed

One file of plain functions with inline fixtures — no pytest, no conftest, no
fixtures directory. `tests/test_smoke.py` wraps this as a single pytest test
so `pytest` works as an entry point without a second copy of the checks.

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is recorded. CI's `engine-smoke` job installs each engine
in its own virtualenv and fails if the corresponding group reports a skip —
"skipped, engine absent" reads identically to a real import error, so the two
have to be told apart somewhere.

WHAT THE FIXTURES ARE
---------------------
Real captures, taken 2026-09-11 through a browser from an Indian residential
exit, cut down to whole cards and then VERIFIED to parse identically to the
untrimmed original — every field of every row, compared before they were
committed. The trimming removed `<svg>` subtrees and presentational
attributes (`style`, `role`, `tabindex`, …) and nothing else; every
`data-*` attribute, every class and every text node the parser reads is
byte-for-byte what Spinny served.

Six cards, each here because it pins a specific behaviour, named above it.
One detail page, reduced from 2.1 MB to its own ["Product","Car"] JSON-LD,
its price node, its overview table and its inspection-report line.

Nothing needed scrubbing, and that is worth stating rather than assuming: a
Spinny card is written by the company, not by a person — there is no seller
name, no reviewer, no uploaded photo credit — and the captures carry no
cookie, no session id, no CSRF token and no expiring image signature. The
only opaque strings in them are the 32-hex asset ids in
`assets.spinny.com` photo paths, which are public and identify a
photograph rather than a visit. `test_no_capture_leaks` guards the NEXT
capture with patterns rather than with these literals.

ASSERT VALUES, NOT COVERAGE. A column can be 100% populated and entirely
wrong: a sibling repo shipped a `review_count` of 445279961 on every row of
every mode because it stripped the digits out of an aria-label, while its
coverage check happily said 100%. So the checks below pin the expected price,
strike price, hub, RTO, odometer and title for named cars.
"""

import io
import ast
import csv
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import builtins
from contextlib import redirect_stdout
from dataclasses import asdict as dataclasses_asdict, fields

import captcha_solver
from captcha_solver import (CaptchaChallenge, detect_recaptcha_v3,
                            reconcile_detections, _v2_task_for, _redact)
from diff_runs import diff_products
import env_config
import page_flow
from output_writer import (Product, save, finish_run, write_csv,
                           dedupe_by_key, dedupe_by_sku, run_meta,
                           ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES,
                           EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL,
                           EXIT_API_ERROR, COMPLETE_STOP_REASONS,
                           LIST_CSV_SEPARATOR)
import product_parser
from product_parser import (parse_products, parse_detail_page, car_metadata,
                            page_url, paginates_by_url, category_from_url,
                            listing_kind, is_listing, site_host,
                            is_supported_host, host_currency, locale_of,
                            city_from_url, car_city_from_url, make_from_url,
                            model_from_url, unknown_city_warning,
                            make_display, model_from_headline,
                            LOCALE_CURRENCY, HOSTS, CURRENCY, CITIES,
                            MAKE_DISPLAY, PAGINATED_KINDS, SELECTORS,
                            BOT_CHALLENGE_MARKERS, MIN_CARD_MATCHES,
                            detect_page_state, detect_bot_challenge,
                            served_by_spinny, is_no_results, is_not_found,
                            page_number_from_url, requested_page_in_url,
                            total_available, listing_heading, total_pages,
                            sku_from_url, car_path, unsupported_reason,
                            strip_tracking, prices_in, scaled_price,
                            rupee_amount, recaptcha_config, count_cards)
from proxy_pool import (ProxyPool, mask, to_playwright, split_credentials,
                        parse_proxy_line)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


# Ten references to Spinny's own asset hosts, and the number is not arbitrary:
# `product_parser._ASSET_MIN_MATCHES` is 10, because detection on this site is
# INVERTED. No Spinny refusal has ever been observed, so what the state
# machine actually has to recognise is a page Spinny did NOT build —
# Chromium's own network-error page, which carries the site's hostname in its
# <title> and no vendor marker anywhere. A real page references
# assets.spinny.com 267 to 1040 times; that one references it zero. A bare
# `<html><body>` wrapper would therefore classify every hand-built fixture
# below as "blocked", which is why this exists.
_ASSET_REFS = "".join(
    '<img src="https://assets.spinny.com/sp-file-system/public/x%d.jpg"/>' % i
    for i in range(10))


def page(*fragments):
    """Wrap fragments in a minimal document, as the engines hand it over."""
    return ('<html lang="en"><body>%s%s</body></html>'
            % (_ASSET_REFS, "".join(fragments)))


def grid(*cards):
    """Wrap cards in the site's own grid container, as a listing serves them."""
    return page('<div data-id="landing-plp-container">%s</div>'
                % "".join(cards))


def heading(count, what="Used cars in Delhi NCR"):
    """The site's own advertised total, in the markup it uses for it."""
    return ('<div data-componentname="HeadingContent">'
            '<span>%s</span><h1>%s</h1></div>' % (count, what))


# A 2Captcha Fingerprint API response, in the `full` shape it actually
# returns. Not markup, so it is deliberately outside the committed-capture
# privacy scan above, which collects by content: this is an API response
# with no site page in it.
#
# The country is IN and the timezone Asia/Kolkata, because that is what a
# fingerprint paired with an Indian exit should look like — the point of the
# flag is that the identity does not contradict the address.
FIX_FINGERPRINT = {
    "id": 1000000,
    "country": "IN",
    "userAgent": {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36"),
        "platform": "Windows",
        "mobile": False,
    },
    "intl": {
        "contentLocale": "en-IN",
        "languages": ["en-IN", "en", "hi-IN", "hi"],
        "timeZone": "Asia/Kolkata",
    },
    "screen": {"width": 1920, "height": 1080,
               "outerWidth": 1920, "outerHeight": 992,
               "deviceScaleFactor": 1},
}

# Wording the build fails on. The original 2scraper brief and this repo's own
# April 2026 prototype use several of these — they predate the naming — so
# the scan is what stops one being pasted back in.
BANNED_PHRASES = (
    "cloud browser",
    "antidetect browser",
    "anti-detect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "2prx.com",
)

# Flags that were removed and must stay removed. Scoped to the ENGINES:
# `--country` is banned on a scraper for this site — one storefront serving
# one country, so the flag could only disagree with the URL, and what varies
# is the CITY, which is part of the path — and legitimate on
# fingerprint_client.py, where it picks a fingerprint locale.
REMOVED_ENGINE_FLAGS = ("--antidetect", "--country")
ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py",
                "selenium_scraper.py")
# ---------------------------------------------------------------------------
# FIXTURES
# ---------------------------------------------------------------------------
# Cut from the real captures in ../captures/, trimmed to whole cards, and
# verified to parse identically to the untrimmed original before being
# committed. See the module docstring for what the trimming removed and why
# nothing needed scrubbing.

# 31483859 — the car this repo's DETAIL fixture is also of, so the two modes
# can be checked against each other on a real pair. Pins the NOT-discounted
# case: no strike price, no discount badge, and the two travel together (20
# of 705 cards are like this), so `original_price` and `discount_pct` must
# come back None rather than 0. Also pins the exact all-in attribute
# (392,000) against the displayed 3.88 Lakh — the two price bases.
# Captured from pw_delhi.html
CARD_GRAND_I10_URL = 'https://www.spinny.com/used-cars-in-delhi-ncr/s/'
CARD_GRAND_I10 = (
    '<div class="ds-rounded-medium ds-p-0 ds-bg-surface-background-gray-intense ds-mt-0 ds-mb-4 ds-mx-2 ds-relative ds-card-elev-none" data-base-component="card" data-componentname="CarListingDesktop"><div class="ds-flex ds-flex-col" data-base-component="box"><div class="ds-rounded-medium ds-relative ds-bg-interactive-background-staticwhite-default" data-base-component="box" data-id=""><div class="ds-absolute ds-overflow-hidden ds-interactive" data-base-component="box"></div><div class="ds-absolute" data-base-component="box"><div data-base-component="box"><div class="ds-rounded-xlarge ds-flex ds-justify-center ds-items-center ds-relative ds-interactive" data-base-component="box" data-category="assured" data-label="31483859" data-price="392000" id="shortlist_icon"><div></div></div></div></div><div class="ds-relative ds-flex ds-justify-end" data-base-component="box"><div class="ds-absolute" data-base-component="box"></div><div class="ds-img-wrapper ds-img-shape-square"><img alt="Used 2019 Hyundai Grand i10 Sportz 1.2 Kappa VTVT Petrol Manual Image" class="ds-img ds-img-fit-contain" src="//assets.spinny.com/sp-file-system/public/2026-09-09/7abfc170b0cf45c483f068d1610d16fb/raw/file.JPG"/></div></div><div><div class="ds-px-3 ds-pb-3" data-base-component="box" id="listing-detail-card-v2"><div class="ds-flex ds-justify-between ds-flex-col" data-base-component="box"><div class="ds-flex ds-justify-between ds-items-center" data-base-component="box"><a data-discover="true" href="/buy-used-cars/faridabad/hyundai/grand-i10/sportz-12-kappa-vtvt-2019/31483859/"><div class="ds-overflow-hidden" data-base-component="box"><h3 class="ds-heading ds-heading-small ds-heading-semibold ds-truncate-1 ds-text-surface-text-gray-normal" data-base-component="heading">2019 Hyundai Grand i10</h3></div></a><div class="ds-flex ds-items-end ds-justify-end ds-flex-row-reverse ds-ml-1 ds-gap-2" data-base-component="box"><span class="ds-heading ds-heading-small ds-heading-semibold ds-whitespace-nowrap ds-text-surface-text-gray-normal" data-base-component="Pricing">3.88 Lakh</span></div></div><div class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-mt-1" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-overflow-hidden" data-base-component="box" data-componentname="ListingModelEmiDetail"><span class="ds-font-text ds-body-medium ds-font-regular ds-truncate-1 ds-text-surface-text-gray-subtle" data-base-component="text">Sportz 1.2 Kappa VTVT</span></div><div class="ds-text-right ds-relative ds-ml-4 ds-flex ds-justify-end ds-items-end ds-flex-col ds-whitespace-nowrap" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-flex ds-items-center ds-flex-nowrap ds-whitespace-nowrap ds-gap-2 ds-rounded-tl-small ds-rounded-bl-small ds-border-w-none ds-relative" data-base-component="box" data-componentname="EmiSectionV2"><div class="ds-flex ds-items-center" data-base-component="box"><span class="ds-font-text ds-body-medium ds-font-regular ds-text-surface-text-gray-subtle" data-base-component="text">EMI <span class="ds-font-text ds-body-medium ds-font-regular ds-whitespace-nowrap ds-text-surface-text-gray-subtle" data-base-component="Pricing">6,675/m<span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">*</span></span></span></div></div></div></div></div><div class="ds-flex ds-flex-row ds-gap-3 ds-mt-2 ds-overflow-auto ds-relative" data-base-component="box"><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">63.5K km</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Petrol</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Manual</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">HR26</span></span></div><div class="ds-flex ds-items-center ds-mt-2" data-base-component="box"><section class="ds-flex ds-items-center ds-gap-2 ds-overflow-hidden" data-base-component="box" id="hub-details-card-v2"><div class="ds-bg-surface-text-gray-muted ds-rounded-round" data-base-component="box" id="dot-separator-card-v2"></div><div class="ds-overflow-hidden ds-whitespace-nowrap" data-base-component="box" id="car-hub-details"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">Sector 27, Faridabad</span></div></section></div></div></div><div aria-orientation="horizontal" class="ds-bg-surface-border-gray-subtle" data-base-component="box"></div><div class="ds-flex ds-rounded-bl-medium ds-rounded-br-medium ds-overflow-hidden ds-relative" data-base-component="box" id="card-footer"><div class="ds-flex ds-items-center ds-justify-between ds-py-3 ds-px-4 ds-box-border ds-bg-surface-background-gray-subtle" data-base-component="box"><div class="ds-overflow-hidden ds-whitespace-nowrap ds-ml-3" data-base-component="box"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-subtle" data-base-component="text">High quality, less driven</span></div></div></div></div></div></div>'
)

# 32071928 — a card with NO HUB on it. 10 of 705 print none, and this is the
# case that breaks anything read by position in the card's span order: the
# fields after the hub silently shift up by one. Also carries a strike price
# and the site's own exact discount in rupees, so `discount_pct` can be
# checked against `discount_amount` rather than against a badge.
# Captured from pw_delhi.html
CARD_CRETA_NO_HUB_URL = 'https://www.spinny.com/used-cars-in-delhi-ncr/s/'
CARD_CRETA_NO_HUB = (
    '<div class="ds-rounded-medium ds-p-0 ds-bg-surface-background-gray-intense ds-mt-0 ds-mb-4 ds-mx-2 ds-relative ds-card-elev-none" data-base-component="card" data-componentname="CarListingDesktop"><div class="ds-flex ds-flex-col" data-base-component="box"><div class="ds-rounded-medium ds-relative ds-bg-interactive-background-staticwhite-default" data-base-component="box" data-id=""><div class="ds-absolute ds-overflow-hidden ds-interactive" data-base-component="box"></div><div class="ds-absolute" data-base-component="box"><div data-base-component="box"><div class="ds-rounded-xlarge ds-flex ds-justify-center ds-items-center ds-relative ds-interactive" data-base-component="box" data-category="assured" data-label="32071928" data-price="607000" id="shortlist_icon"><div></div></div></div></div><div class="ds-relative ds-flex ds-justify-end" data-base-component="box"><div class="ds-absolute" data-base-component="box"></div><div class="ds-img-wrapper ds-img-shape-square"><img alt="Used 2017 Hyundai Creta SX Plus 1.6 AT Petrol Petrol Automatic Image" class="ds-img ds-img-fit-contain" src="//assets.spinny.com/sp-file-system/public/2026-09-10/e82c8bec7a5f4f13ba2669c2205e822c/raw/file.JPG"/></div></div><div><div class="ds-px-3 ds-pb-3" data-base-component="box" id="listing-detail-card-v2"><div class="ds-flex ds-justify-between ds-flex-col" data-base-component="box"><div class="ds-flex ds-justify-between ds-items-center" data-base-component="box"><a data-discover="true" href="/buy-used-cars/delhi/hyundai/creta/sx-plus-16-at-petrol-2017/32071928/"><div class="ds-overflow-hidden" data-base-component="box"><h3 class="ds-heading ds-heading-small ds-heading-semibold ds-truncate-1 ds-text-surface-text-gray-normal" data-base-component="heading">2017 Hyundai Creta</h3></div></a><div class="ds-flex ds-items-center ds-justify-end ds-flex-row-reverse ds-ml-1 ds-gap-2" data-base-component="box"><span class="ds-heading ds-heading-small ds-heading-semibold ds-whitespace-nowrap ds-text-surface-text-gray-normal" data-base-component="Pricing">5.86 Lakh</span><span class="ds-font-text ds-body-small ds-font-regular ds-line-through ds-whitespace-nowrap ds-text-surface-text-gray-muted" data-base-component="Pricing">5.93 Lakh</span></div></div><div class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-mt-1" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-overflow-hidden" data-base-component="box" data-componentname="ListingModelEmiDetail"><span class="ds-font-text ds-body-medium ds-font-regular ds-truncate-1 ds-text-surface-text-gray-subtle" data-base-component="text">SX Plus 1.6 AT Petrol</span></div><div class="ds-text-right ds-relative ds-ml-4 ds-flex ds-justify-end ds-items-end ds-flex-col ds-whitespace-nowrap" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-flex ds-items-center ds-flex-nowrap ds-whitespace-nowrap ds-gap-2 ds-rounded-tl-small ds-rounded-bl-small ds-border-w-none ds-relative" data-base-component="box" data-componentname="EmiSectionV2"><div class="ds-flex ds-items-center" data-base-component="box"><span class="ds-font-text ds-body-medium ds-font-regular ds-text-surface-text-gray-subtle" data-base-component="text">EMI <span class="ds-font-text ds-body-medium ds-font-regular ds-whitespace-nowrap ds-text-surface-text-gray-subtle" data-base-component="Pricing">10,336/m<span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">*</span></span></span></div></div></div></div></div><div class="ds-flex ds-flex-row ds-gap-3 ds-mt-2 ds-overflow-auto ds-relative" data-base-component="box"><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">48.5K km</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Petrol</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Automatic</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">DL8C</span></span></div><div class="ds-flex ds-items-center ds-justify-between ds-mt-3" data-base-component="box"><div class="ds-flex ds-items-center ds-gap-2 ds-overflow-hidden" data-base-component="box" id="highlights-card-v2"><img alt="awards" aria-hidden="true" class="Image__animateOpacity" src="https://mda.spinny.com/sp-file-system/public/2024-07-10/013b30540f784f578156bb67f043cdc5/raw/Frame 627321.svg.svg?q=85&amp;w=16"/><div class="ds-whitespace-nowrap ds-overflow-hidden" data-base-component="box"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal" data-base-component="text">Award winner</span></div></div></div></div></div><div aria-orientation="horizontal" class="ds-bg-surface-border-gray-subtle" data-base-component="box"></div><div class="ds-flex ds-rounded-bl-medium ds-rounded-br-medium ds-overflow-hidden ds-relative" data-base-component="box" id="card-footer"><section class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-py-2 ds-px-3 ds-box-border ds-bg-surface-background-gray-subtle" data-base-component="box"><div class="ds-flex ds-gap-3 ds-items-center ds-relative" data-base-component="box" id="availability-status-card-v2"><div class="ds-img-wrapper ds-img-shape-square"><img alt="upcoming" class="ds-img ds-img-fit-contain" src="https://spn-sta.spinny.com/spinny-web/static-images/assets/images/components/CarListingCardV2New/components/CardFooter/components/AvailabilityStatus/assets/upcoming.svg"/></div></div><div class="ds-flex ds-items-center" data-base-component="box" id="procurement-category-card-v2"></div></section></div><div class="ds-absolute" data-base-component="box"><div class="ds-flex ds-items-center ds-gap-2 ds-rounded-2xlarge ds-py-1 ds-px-2 ds-border-w-thick" data-base-component="box" id="GenericDiscountBadge"><span class="ds-font-text ds-body-small ds-font-regular" data-base-component="text">₹7,000</span></div></div></div></div></div>'
)

# 31419821 — "2016 Maruti Ciaz". The headline ABBREVIATES the make: 178 of
# 705 cards do, and the brand here has to come from the URL's own
# `/maruti-suzuki/` segment through MAKE_DISPLAY, not from the words on the
# card. An automatic, so the transmission read is exercised too.
# Captured from pw_delhi.html
CARD_CIAZ_URL = 'https://www.spinny.com/used-cars-in-delhi-ncr/s/'
CARD_CIAZ = (
    '<div class="ds-rounded-medium ds-p-0 ds-bg-surface-background-gray-intense ds-mt-0 ds-mb-4 ds-mx-2 ds-relative ds-card-elev-none" data-base-component="card" data-componentname="CarListingDesktop"><div class="ds-flex ds-flex-col" data-base-component="box"><div class="ds-rounded-medium ds-relative ds-bg-interactive-background-staticwhite-default" data-base-component="box" data-id=""><div class="ds-absolute ds-overflow-hidden ds-interactive" data-base-component="box"></div><div class="ds-absolute" data-base-component="box"><div data-base-component="box"><div class="ds-rounded-xlarge ds-flex ds-justify-center ds-items-center ds-relative ds-interactive" data-base-component="box" data-category="budget" data-label="31419821" data-price="388000" id="shortlist_icon"><div></div></div></div></div><div class="ds-relative ds-flex ds-justify-end" data-base-component="box"><div class="ds-absolute" data-base-component="box"></div><div class="ds-img-wrapper ds-img-shape-square"><img alt="Used 2016 Maruti Suzuki Ciaz ZXI+ AT Petrol Automatic Image" class="ds-img ds-img-fit-contain" src="//assets.spinny.com/sp-file-system/public/2026-09-01/d2c1ba4f25714d89b3e8d3988cafdd34/raw/file.JPG"/></div></div><div><div class="ds-px-3 ds-pb-3" data-base-component="box" id="listing-detail-card-v2"><div class="ds-flex ds-justify-between ds-flex-col" data-base-component="box"><div class="ds-flex ds-justify-between ds-items-center" data-base-component="box"><a data-discover="true" href="/buy-used-cars/faridabad/maruti-suzuki/ciaz/zxi-at-2016/31419821/"><div class="ds-overflow-hidden" data-base-component="box"><h3 class="ds-heading ds-heading-small ds-heading-semibold ds-truncate-1 ds-text-surface-text-gray-normal" data-base-component="heading">2016 Maruti Ciaz</h3></div></a><div class="ds-flex ds-items-end ds-justify-end ds-flex-row-reverse ds-ml-1 ds-gap-2" data-base-component="box"><span class="ds-heading ds-heading-small ds-heading-semibold ds-whitespace-nowrap ds-text-surface-text-gray-normal" data-base-component="Pricing">3.80 Lakh</span></div></div><div class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-mt-1" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-overflow-hidden" data-base-component="box" data-componentname="ListingModelEmiDetail"><span class="ds-font-text ds-body-medium ds-font-regular ds-truncate-1 ds-text-surface-text-gray-subtle" data-base-component="text">ZXI+ AT</span></div><div class="ds-text-right ds-relative ds-ml-4 ds-flex ds-justify-end ds-items-end ds-flex-col ds-whitespace-nowrap" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-flex ds-items-center ds-flex-nowrap ds-whitespace-nowrap ds-gap-2 ds-rounded-tl-small ds-rounded-bl-small ds-border-w-none ds-relative" data-base-component="box" data-componentname="EmiSectionV2"><div class="ds-flex ds-items-center" data-base-component="box"><span class="ds-font-text ds-body-medium ds-font-regular ds-text-surface-text-gray-subtle" data-base-component="text">EMI <span class="ds-font-text ds-body-medium ds-font-regular ds-whitespace-nowrap ds-text-surface-text-gray-subtle" data-base-component="Pricing">7,062/m<span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">*</span></span></span></div></div></div></div></div><div class="ds-flex ds-flex-row ds-gap-3 ds-mt-2 ds-overflow-auto ds-relative" data-base-component="box"><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">63.5K km</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Petrol</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Automatic</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">HR51</span></span></div><div class="ds-flex ds-items-center ds-mt-2" data-base-component="box"><section class="ds-flex ds-items-center ds-gap-2 ds-overflow-hidden" data-base-component="box" id="hub-details-card-v2"><div class="ds-bg-surface-text-gray-muted ds-rounded-round" data-base-component="box" id="dot-separator-card-v2"></div><div class="ds-overflow-hidden ds-whitespace-nowrap" data-base-component="box" id="car-hub-details"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">Sector 27, Faridabad</span></div></section></div></div></div><div aria-orientation="horizontal" class="ds-bg-surface-border-gray-subtle" data-base-component="box"></div><div class="ds-flex ds-rounded-bl-medium ds-rounded-br-medium ds-overflow-hidden ds-relative" data-base-component="box" id="card-footer"><div class="ds-flex ds-items-center ds-justify-between ds-py-3 ds-px-4 ds-box-border ds-bg-surface-background-gray-subtle" data-base-component="box"><div class="ds-overflow-hidden ds-whitespace-nowrap ds-ml-3" data-base-component="box"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-subtle" data-base-component="text">Highly affordable &amp; reliable</span></div></div></div></div></div></div>'
)

# 31749826 — the cheapest car in the Bangalore capture (1.76 Lakh), from a
# SECOND CITY. Pins that `car_city` comes from the car's own URL rather than
# from the listing's, and that a two-decimal lakh figure round-trips to an
# exact rupee integer.
# Captured from pw_bangalore.html
CARD_EON_URL = 'https://www.spinny.com/used-cars-in-bangalore/s/'
CARD_EON = (
    '<div class="ds-rounded-medium ds-p-0 ds-bg-surface-background-gray-intense ds-mt-0 ds-mb-4 ds-mx-2 ds-relative ds-card-elev-none" data-base-component="card" data-componentname="CarListingDesktop"><div class="ds-flex ds-flex-col" data-base-component="box"><div class="ds-rounded-medium ds-relative ds-bg-interactive-background-staticwhite-default" data-base-component="box" data-id=""><div class="ds-absolute ds-overflow-hidden ds-interactive" data-base-component="box"></div><div class="ds-absolute" data-base-component="box"><div data-base-component="box"><div class="ds-rounded-xlarge ds-flex ds-justify-center ds-items-center ds-relative ds-interactive" data-base-component="box" data-category="budget" data-label="31749826" data-price="187000" id="shortlist_icon"><div></div></div></div></div><div class="ds-relative ds-flex ds-justify-end" data-base-component="box"><div class="ds-absolute" data-base-component="box"></div><div class="ds-img-wrapper ds-img-shape-square"><img alt="Used 2014 Hyundai Eon Era + Petrol Manual Image" class="ds-img ds-img-fit-contain" src="//assets.spinny.com/sp-file-system/public/2026-09-10/1402109c6b1f483e812e283da3d6a4cc/raw/file.JPG"/></div></div><div><div class="ds-px-3 ds-pb-3" data-base-component="box" id="listing-detail-card-v2"><div class="ds-flex ds-justify-between ds-flex-col" data-base-component="box"><div class="ds-flex ds-justify-between ds-items-center" data-base-component="box"><a data-discover="true" href="/buy-used-cars/bangalore/hyundai/eon/era-2014/31749826/"><div class="ds-overflow-hidden" data-base-component="box"><h3 class="ds-heading ds-heading-small ds-heading-semibold ds-truncate-1 ds-text-surface-text-gray-normal" data-base-component="heading">2014 Hyundai Eon</h3></div></a><div class="ds-flex ds-items-center ds-justify-end ds-flex-row-reverse ds-ml-1 ds-gap-2" data-base-component="box"><span class="ds-heading ds-heading-small ds-heading-semibold ds-whitespace-nowrap ds-text-surface-text-gray-normal" data-base-component="Pricing">1.76 Lakh</span><span class="ds-font-text ds-body-small ds-font-regular ds-line-through ds-whitespace-nowrap ds-text-surface-text-gray-muted" data-base-component="Pricing">1.83 Lakh</span></div></div><div class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-mt-1" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-overflow-hidden" data-base-component="box" data-componentname="ListingModelEmiDetail"><span class="ds-font-text ds-body-medium ds-font-regular ds-truncate-1 ds-text-surface-text-gray-subtle" data-base-component="text">Era +</span></div><div class="ds-text-right ds-relative ds-ml-4 ds-flex ds-justify-end ds-items-end ds-flex-col ds-whitespace-nowrap" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-flex ds-items-center ds-flex-nowrap ds-whitespace-nowrap ds-gap-2 ds-rounded-tl-small ds-rounded-bl-small ds-border-w-none ds-relative" data-base-component="box" data-componentname="EmiSectionV2"><div class="ds-flex ds-items-center" data-base-component="box"><span class="ds-font-text ds-body-medium ds-font-regular ds-text-surface-text-gray-subtle" data-base-component="text">EMI <span class="ds-font-text ds-body-medium ds-font-regular ds-whitespace-nowrap ds-text-surface-text-gray-subtle" data-base-component="Pricing">4,725/m<span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">*</span></span></span></div></div></div></div></div><div class="ds-flex ds-flex-row ds-gap-3 ds-mt-2 ds-overflow-auto ds-relative" data-base-component="box"><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">89.5K km</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Petrol</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Manual</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">KA53</span></span></div><div class="ds-flex ds-items-center ds-justify-between ds-mt-3 ds-gap-2" data-base-component="box"><section class="ds-flex ds-items-center ds-gap-2 ds-overflow-hidden" data-base-component="box" id="hub-details-card-v2"><div class="ds-bg-surface-text-gray-muted ds-rounded-round" data-base-component="box" id="dot-separator-card-v2"></div><div class="ds-overflow-hidden ds-whitespace-nowrap" data-base-component="box" id="car-hub-details"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">Nexus Shanti Niketan Mall, Whitefield</span></div></section><div class="ds-flex ds-items-center" data-base-component="box" id="procurement-category-card-v2"></div></div></div></div><div aria-orientation="horizontal" class="ds-bg-surface-border-gray-subtle" data-base-component="box"></div><div class="ds-flex ds-rounded-bl-medium ds-rounded-br-medium ds-overflow-hidden ds-relative" data-base-component="box" id="card-footer"><section class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-py-2 ds-px-3 ds-box-border" data-base-component="box"><div class="ds-flex ds-items-center ds-gap-2 ds-overflow-hidden" data-base-component="box" id="highlights-card-v2"><img alt="most_affordable" aria-hidden="true" class="Image__animateOpacity styles__fallBackImgClass"/><div class="ds-whitespace-nowrap ds-overflow-hidden" data-base-component="box"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal" data-base-component="text">City\'s most affordable<span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal" data-base-component="text"> &amp; 2 more reasons to buy</span></span></div></div></section></div><div class="ds-absolute" data-base-component="box"><div class="ds-flex ds-items-center ds-gap-2 ds-rounded-2xlarge ds-py-1 ds-px-2 ds-border-w-thick" data-base-component="box" id="GenericDiscountBadge"><span class="ds-font-text ds-body-small ds-font-regular" data-base-component="text">₹7,000</span></div></div></div></div></div>'
)

# 30589928 — "2022 Mercedes E-Class", brand "Mercedes Benz". The other
# abbreviated-make shape, and the largest price in the Bangalore capture
# (48.68 Lakh), so the lakh multiplier is pinned well above the 1-10 range
# where a mis-scaled read would still look plausible.
# Captured from pw_bangalore.html
CARD_E_CLASS_URL = 'https://www.spinny.com/used-cars-in-bangalore/s/'
CARD_E_CLASS = (
    '<div class="ds-rounded-medium ds-p-0 ds-bg-surface-background-gray-intense ds-mt-0 ds-mb-4 ds-mx-2 ds-relative ds-card-elev-none" data-base-component="card" data-componentname="CarListingDesktop"><div class="ds-flex ds-flex-col" data-base-component="box"><div class="ds-rounded-medium ds-relative ds-bg-interactive-background-staticwhite-default" data-base-component="box" data-id=""><div class="ds-absolute ds-overflow-hidden ds-interactive" data-base-component="box"></div><div class="ds-absolute" data-base-component="box"><div data-base-component="box"><div class="ds-rounded-xlarge ds-flex ds-justify-center ds-items-center ds-relative ds-interactive" data-base-component="box" data-category="luxury" data-label="30589928" data-price="5150488" id="shortlist_icon"><div></div></div></div></div><div class="ds-relative ds-flex ds-justify-end" data-base-component="box"><div class="ds-absolute" data-base-component="box"></div><div class="ds-img-wrapper ds-img-shape-square"><img alt="Used 2022 Mercedes-Benz E-Class E 200 Exclusive Petrol Automatic Image" class="ds-img ds-img-fit-contain" src="//assets.spinny.com/sp-file-system/public/2026-07-20/c122abf960194020929613cfc0eccc24/raw/file.JPG"/></div></div><div><div class="ds-px-3 ds-pb-3" data-base-component="box" id="listing-detail-card-v2"><div class="ds-flex ds-justify-between ds-flex-col" data-base-component="box"><div class="ds-flex ds-justify-between ds-items-center" data-base-component="box"><a data-discover="true" href="/buy-used-cars/bangalore/mercedes-benz/e-class/e-200-exclusive-yelahanka-2022/30589928/"><div class="ds-overflow-hidden" data-base-component="box"><h3 class="ds-heading ds-heading-small ds-heading-semibold ds-truncate-1 ds-text-surface-text-gray-normal" data-base-component="heading">2022 Mercedes E-Class</h3></div></a><div class="ds-flex ds-items-center ds-justify-end ds-flex-row-reverse ds-ml-1 ds-gap-2" data-base-component="box"><span class="ds-heading ds-heading-small ds-heading-semibold ds-whitespace-nowrap ds-text-surface-text-gray-normal" data-base-component="Pricing">48.68 Lakh</span><span class="ds-font-text ds-body-small ds-font-regular ds-line-through ds-whitespace-nowrap ds-text-surface-text-gray-muted" data-base-component="Pricing">51 Lakh</span></div></div><div class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-mt-1" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-overflow-hidden" data-base-component="box" data-componentname="ListingModelEmiDetail"><span class="ds-font-text ds-body-medium ds-font-regular ds-truncate-1 ds-text-surface-text-gray-subtle" data-base-component="text">E 200 Exclusive</span></div><div class="ds-text-right ds-relative ds-ml-4 ds-flex ds-justify-end ds-items-end ds-flex-col ds-whitespace-nowrap" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-flex ds-items-center ds-flex-nowrap ds-whitespace-nowrap ds-gap-2 ds-rounded-tl-small ds-rounded-bl-small ds-border-w-none ds-relative" data-base-component="box" data-componentname="EmiSectionV2"><div class="ds-flex ds-items-center" data-base-component="box"><span class="ds-font-text ds-body-medium ds-font-regular ds-text-surface-text-gray-subtle" data-base-component="text">EMI <span class="ds-font-text ds-body-medium ds-font-regular ds-whitespace-nowrap ds-text-surface-text-gray-subtle" data-base-component="Pricing">87,709/m<span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">*</span></span></span></div></div></div></div></div><div class="ds-flex ds-flex-row ds-gap-3 ds-mt-2 ds-overflow-auto ds-relative" data-base-component="box"><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">40.5K km</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Petrol</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Automatic</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">KA01</span></span></div><div class="ds-flex ds-items-center ds-justify-between ds-mt-3 ds-gap-2" data-base-component="box"><section class="ds-flex ds-items-center ds-gap-2 ds-overflow-hidden" data-base-component="box" id="hub-details-card-v2"><div class="ds-bg-surface-text-gray-muted ds-rounded-round" data-base-component="box" id="dot-separator-card-v2"></div><div class="ds-overflow-hidden ds-whitespace-nowrap" data-base-component="box" id="car-hub-details"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">Hunasamaranahalli, Yelahanka</span></div></section><div class="ds-flex ds-items-center" data-base-component="box" id="procurement-category-card-v2"></div></div></div></div><div aria-orientation="horizontal" class="ds-bg-surface-border-gray-subtle" data-base-component="box"></div><div class="ds-flex ds-rounded-bl-medium ds-rounded-br-medium ds-overflow-hidden ds-relative" data-base-component="box" id="card-footer"><section class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-py-2 ds-px-3 ds-box-border" data-base-component="box"><div class="ds-flex ds-items-center ds-gap-2 ds-overflow-hidden" data-base-component="box" id="highlights-card-v2"><img alt="icon_fuel_efficiency" aria-hidden="true" class="Image__animateOpacity styles__fallBackImgClass"/><div class="ds-whitespace-nowrap ds-overflow-hidden" data-base-component="box"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal" data-base-component="text">200 petrol<span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal" data-base-component="text"> &amp; 2 more reasons to buy</span></span></div></div></section></div><div class="ds-absolute" data-base-component="box"><div class="ds-flex ds-items-center ds-gap-2 ds-rounded-2xlarge ds-py-1 ds-px-2 ds-border-w-thick" data-base-component="box" id="GenericDiscountBadge"><span class="ds-font-text ds-body-small ds-font-regular" data-base-component="text">₹2.32L</span></div></div></div></div></div>'
)

# 31981616 — "2022 Land Rover Range Rover Sport" at 85.90 Lakh, from the
# LUXURY listing. A two-word make whose slug is `land-rover`, and a model
# name that repeats the make's words — which is exactly what a naive
# "strip the brand off the headline" would mangle.
# Captured from pw_luxury.html
CARD_RANGE_ROVER_URL = 'https://www.spinny.com/used-luxury-cars-in-delhi-ncr/s/'
CARD_RANGE_ROVER = (
    '<div class="ds-rounded-medium ds-p-0 ds-bg-surface-background-gray-intense ds-mt-0 ds-mb-4 ds-mx-2 ds-relative ds-card-elev-none" data-base-component="card" data-componentname="CarListingDesktop"><div class="ds-flex ds-flex-col" data-base-component="box"><div class="ds-rounded-medium ds-relative ds-bg-interactive-background-staticwhite-default" data-base-component="box" data-id=""><div class="ds-absolute ds-overflow-hidden ds-interactive" data-base-component="box"></div><div class="ds-absolute" data-base-component="box"><div data-base-component="box"><div class="ds-rounded-xlarge ds-flex ds-justify-center ds-items-center ds-relative ds-interactive" data-base-component="box" data-category="luxury" data-label="31981616" data-price="8685788" id="shortlist_icon"><div></div></div></div></div><div class="ds-relative ds-flex ds-justify-end" data-base-component="box"><div class="ds-absolute" data-base-component="box"></div><div class="ds-img-wrapper ds-img-shape-square"><img alt="Used 2022 Land Rover Range Rover Sport HSE 2.0 Petrol Petrol Automatic Image" class="ds-img ds-img-fit-contain" src="//assets.spinny.com/sp-file-system/public/2026-09-04/e93341031f314200b9aa9a39d5a4b622/raw/file.JPG"/></div></div><div><div class="ds-px-3 ds-pb-3" data-base-component="box" id="listing-detail-card-v2"><div class="ds-flex ds-justify-between ds-flex-col" data-base-component="box"><div class="ds-flex ds-justify-between ds-items-center" data-base-component="box"><a data-discover="true" href="/buy-used-cars/gurgaon/land-rover/range-rover-sport/hse-20-petrol-sector-29-2022/31981616/"><div class="ds-overflow-hidden" data-base-component="box"><h3 class="ds-heading ds-heading-small ds-heading-semibold ds-truncate-1 ds-text-surface-text-gray-normal" data-base-component="heading">2022 Land Rover Range Rover Sport</h3></div></a><div class="ds-flex ds-items-center ds-justify-end ds-flex-row-reverse ds-ml-1 ds-gap-2" data-base-component="box"><span class="ds-heading ds-heading-small ds-heading-semibold ds-whitespace-nowrap ds-text-surface-text-gray-normal" data-base-component="Pricing">85.90 Lakh</span><span class="ds-font-text ds-body-small ds-font-regular ds-line-through ds-whitespace-nowrap ds-text-surface-text-gray-muted" data-base-component="Pricing">86 Lakh</span></div></div><div class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-mt-1" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-overflow-hidden" data-base-component="box" data-componentname="ListingModelEmiDetail"><span class="ds-font-text ds-body-medium ds-font-regular ds-truncate-1 ds-text-surface-text-gray-subtle" data-base-component="text">HSE 2.0 Petrol</span></div><div class="ds-text-right ds-relative ds-ml-4 ds-flex ds-justify-end ds-items-end ds-flex-col ds-whitespace-nowrap" data-base-component="box" data-componentname="ListingModelEmiDetail"><div class="ds-flex ds-items-center ds-flex-nowrap ds-whitespace-nowrap ds-gap-2 ds-rounded-tl-small ds-rounded-bl-small ds-border-w-none ds-relative" data-base-component="box" data-componentname="EmiSectionV2"><div class="ds-flex ds-items-center" data-base-component="box"><span class="ds-font-text ds-body-medium ds-font-regular ds-text-surface-text-gray-subtle" data-base-component="text">EMI <span class="ds-font-text ds-body-medium ds-font-regular ds-whitespace-nowrap ds-text-surface-text-gray-subtle" data-base-component="Pricing">1,47,913/m<span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">*</span></span></span></div></div></div></div></div><div class="ds-flex ds-flex-row ds-gap-3 ds-mt-2 ds-overflow-auto ds-relative" data-base-component="box"><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">39K km</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Petrol</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">Automatic</span></span><span class="ds-badge ds-badge-medium ds-badge-neutral-subtle"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal ds-badge-label" data-base-component="text">HR72</span></span></div><div class="ds-flex ds-items-center ds-justify-between ds-mt-3 ds-gap-2" data-base-component="box"><section class="ds-flex ds-items-center ds-gap-2 ds-overflow-hidden" data-base-component="box" id="hub-details-card-v2"><div class="ds-bg-surface-text-gray-muted ds-rounded-round" data-base-component="box" id="dot-separator-card-v2"></div><div class="ds-overflow-hidden ds-whitespace-nowrap" data-base-component="box" id="car-hub-details"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-muted" data-base-component="text">Sector 29, Gurgaon</span></div></section><div class="ds-flex ds-items-center" data-base-component="box" id="procurement-category-card-v2"></div></div></div></div><div aria-orientation="horizontal" class="ds-bg-surface-border-gray-subtle" data-base-component="box"></div><div class="ds-flex ds-rounded-bl-medium ds-rounded-br-medium ds-overflow-hidden ds-relative" data-base-component="box" id="card-footer"><section class="ds-flex ds-items-center ds-justify-between ds-gap-2 ds-py-2 ds-px-3 ds-box-border" data-base-component="box"><div class="ds-flex ds-items-center ds-gap-2 ds-overflow-hidden" data-base-component="box" id="highlights-card-v2"><img alt="vip_no" aria-hidden="true" class="Image__animateOpacity styles__fallBackImgClass"/><div class="ds-whitespace-nowrap ds-overflow-hidden" data-base-component="box"><span class="ds-font-text ds-body-small ds-font-regular ds-text-surface-text-gray-normal" data-base-component="text">VIP number</span></div></div></section></div><div class="ds-absolute" data-base-component="box"><div class="ds-flex ds-items-center ds-gap-2 ds-rounded-2xlarge ds-py-1 ds-px-2 ds-border-w-thick" data-base-component="box" id="GenericDiscountBadge"><span class="ds-font-text ds-body-small ds-font-regular" data-base-component="text">₹10,000</span></div></div></div></div></div>'
)

# One /buy-used-cars/.../{id}/ page, reduced from 2.1 MB to the four things
# the parser reads: the car's own ["Product","Car"] JSON-LD, the displayed
# price node, the overview table and the inspection-report line. Note the
# JSON-LD spells the price `offers.Price` with a CAPITAL P — schema.org's
# property is `price`, so a parser reading the standard spelling returns None
# on every row of every detail run. Both spellings are accepted and the
# capitalised one is the only one ever observed.
DETAIL_URL = 'https://www.spinny.com/buy-used-cars/faridabad/hyundai/grand-i10/sportz-12-kappa-vtvt-2019/31483859/'
DETAIL_PAGE = (
    '<html lang="en"><head><script type="application/ld+json">{"@context":"https://schema.org","@type":["Product","Car"],"name":"Hyundai Grand i10 Sportz 1.2 Kappa VTVT","description":"Used 63,417 Kms Driven 2019 Hyundai Grand i10 Sportz 1.2 Kappa VTVT for 3.9 Lakh in Sector-27, Faridabad. SPINNY inspected, SPINNY Star Price, free test drive.","model":"Grand i10","fuelType":"petrol","numberOfPreviousOwners":1,"modelDate":"2019-04-01T00:00:00.000Z","itemCondition":"https://schema.org/UsedCondition","vehicleModelDate":2019,"vehicleSeatingCapacity":5,"mileageFromOdometer":{"@type":"QuantitativeValue","value":63417,"unitCode":"KMT"},"color":"grey","image":"http://assets.spinny.com/sp-file-system/public/2026-09-09/5edaaa7256f6415894b1310837915707/raw/file.JPG","url":"https://www.spinny.com/buy-used-cars/faridabad/hyundai/grand-i10/sportz-12-kappa-vtvt-2019/31483859/","vehicleTransmission":"manual","brand":"Hyundai","productID":31483859,"manufacturer":{"@type":"organization","name":"Hyundai"},"offers":{"@type":"Offer","priceCurrency":"INR","Price":392000,"url":"https://www.spinny.com/buy-used-cars/faridabad/hyundai/grand-i10/sportz-12-kappa-vtvt-2019/31483859/","availability":"http://schema.org/InStock"},"additionalProperty":[{"@type":"PropertyValue","name":"RTO","unitText":"HR26"},{"@type":"PropertyValue","name":"Registration Year","unitText":2019},{"@type":"PropertyValue","name":"Registration Month","unitText":"August"}]}</script></head><body><img src="https://assets.spinny.com/x.jpg"/><span class="ds-heading ds-heading-large ds-heading-semibold ds-whitespace-nowrap ds-text-surface-text-gray-normal" data-base-component="Pricing"><svg aria-hidden="true" fill="none" focusable="false" height="0.714em" style="margin-right: 2px; vertical-align: baseline;" viewbox="0 0 17 28.56" width="0.425em"><path d="M7.44 28.56L0 15.56V13.44H1.32C2.6 13.44 3.69333 13.3067 4.6 13.04C5.50667 12.7733 6.22667 12.3467 6.76 11.76C7.29333 11.1467 7.62667 10.36 7.76 9.4H0V6.72H7.72C7.53333 5.81333 7.18667 5.06667 6.68 4.48C6.17333 3.86667 5.49333 3.41333 4.64 3.12C3.78667 2.82667 2.76 2.68 1.56 2.68H0V0H17V2.68H9.8C10.36 3.18667 10.8267 3.77333 11.2 4.44C11.5733 5.10667 11.8133 5.86667 11.92 6.72H17V9.4H12C11.8133 11.3467 11.0667 12.8667 9.76 13.96C8.48 15.0533 6.8 15.76 4.72 16.08L12.44 28.56H7.44Z" fill="currentColor"></path></svg>3.88 Lakh</span><div class="ds-flex ds-flex-wrap ds-relative" data-base-component="box" style="z-index: 1;"><div class="ds-py-3 ds-border-w-b-thinner" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">Make Year</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-capitalize ds-text-surface-text-gray-normal" data-base-component="text">Apr 2019</span></div></div><div class="ds-py-3 ds-border-w-b-thinner" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">Registration Year</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-capitalize ds-text-surface-text-gray-normal" data-base-component="text">Aug 2019</span></div></div><div class="ds-py-3 ds-border-w-b-thinner" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">Fuel Type</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-capitalize ds-text-surface-text-gray-normal" data-base-component="text">petrol (BSIV)</span></div></div><div class="ds-py-3 ds-border-w-b-thinner" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">Km driven</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-normal-case ds-text-surface-text-gray-normal" data-base-component="text">63K km</span></div></div><div class="ds-py-3 ds-border-w-b-thinner" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">Transmission</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-capitalize ds-text-surface-text-gray-normal" data-base-component="text">manual (regular)</span><i class="DesktopOverview__infoIcon" role="none"><svg height="16" viewbox="0 0 24 24" width="16" xmlns="http://www.w3.org/2000/svg"><g fill="none" fill-rule="evenodd"><g><g><path d="M0 0L24 0 24 24 0 24z" transform="translate(-27 -373) translate(27 373)"></path><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 15c-.55 0-1-.45-1-1v-4c0-.55.45-1 1-1s1 .45 1 1v4c0 .55-.45 1-1 1zm1-8h-2V7h2v2z" fill="#2e054e" fill-rule="nonzero" transform="translate(-27 -373) translate(27 373)"></path></g></g></g></svg></i></div></div><div class="ds-py-3 ds-border-w-b-thinner" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">No. of Owner</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-capitalize ds-text-surface-text-gray-normal" data-base-component="text">1st Owner</span></div></div><div class="ds-py-3 ds-border-w-b-thinner" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">Insurance Validity</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-capitalize ds-text-surface-text-gray-normal" data-base-component="text">Jul  2027</span></div></div><div class="ds-py-3 ds-border-w-b-thinner" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">Insurance Type</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-capitalize ds-text-surface-text-gray-normal" data-base-component="text">Third Party</span></div></div><div class="ds-py-3 ds-border-w-b-thinner" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">RTO</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-capitalize ds-text-surface-text-gray-normal" data-base-component="text">HR26</span></div></div><div class="ds-py-3 ds-border-w-b-none" data-base-component="box" style="width: 33.3%; border-bottom-color: var(--ds-color-popup-border-subtle); border-bottom-style: solid;"><span class="ds-font-text ds-body-small ds-font-medium ds-text-surface-text-gray-muted" data-base-component="text">Car Location</span><div class="ds-flex ds-items-center ds-flex-wrap ds-gap-x-1" data-base-component="box" style="width: 90%;"><span class="ds-font-text ds-body-medium ds-font-medium ds-capitalize ds-text-surface-text-gray-normal" data-base-component="text">Sector-27, Faridabad</span></div></div></div><div class="ds-font-text ds-body-small ds-font-regular ds-my-3 ds-text-surface-text-gray-muted" data-base-component="text">1573 parts evaluated by 5 automotive experts</div></body></html>'
)
def _one(card_html, url):
    """Parse a single card fixture as the grid the site would serve it in."""
    rows = parse_products(grid(card_html), url)
    return rows[0] if rows else None


ALL_CARDS = [
    ("CARD_GRAND_I10", CARD_GRAND_I10, CARD_GRAND_I10_URL),
    ("CARD_CRETA_NO_HUB", CARD_CRETA_NO_HUB, CARD_CRETA_NO_HUB_URL),
    ("CARD_CIAZ", CARD_CIAZ, CARD_CIAZ_URL),
    ("CARD_EON", CARD_EON, CARD_EON_URL),
    ("CARD_E_CLASS", CARD_E_CLASS, CARD_E_CLASS_URL),
    ("CARD_RANGE_ROVER", CARD_RANGE_ROVER, CARD_RANGE_ROVER_URL),
]


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
def test_price_parsing():
    group("price parsing — lakh, crore, and the shapes that are not prices")
    ok = True

    # THE SITE'S OWN SCALE WORDS. Spinny writes every listing price as a
    # multiple of a lakh (100,000) or a crore (10,000,000), to two decimals,
    # and never as a plain rupee figure. A parser that read "3.88 Lakh" as
    # 3.88 would report a car costing 388,000 rupees as costing under four,
    # and every downstream comparison would still look internally consistent.
    for text, want in [("3.88 Lakh", 388000.0),
                       ("85.90 Lakh", 8590000.0),
                       ("1.76 Lakh", 176000.0),
                       ("48.68 Lakh", 4868000.0),
                       ("1.25 Crore", 12500000.0),
                       ("₹3.88 Lakh", 388000.0),
                       ("3.88 lakh", 388000.0),
                       ("1.25 Cr", 12500000.0),
                       # "Rs." in front changes nothing: the SCALE WORD is
                       # what this anchors on, which is why there is no
                       # separate "Rs." pattern in the parser.
                       ("Rs. 1.65 Lakh", 165000.0)]:
        ok &= check("scaled_price(%r) == %s" % (text, want),
                    scaled_price(text) == want)

    # A bare number with no scale word is NOT a price on this site, and
    # accepting one is how a card's odometer ("63.5K km"), its EMI figure or
    # its model year becomes its price.
    # A bare "L" suffix is deliberately not a scale word HERE — only behind
    # a ₹ symbol, where `rupee_amount` reads it. "3.88L" alone is as likely
    # to be a variant name as a price.
    for text in ("2019", "63.5K km", "5 Seater", "1 owner", "3.88L"):
        ok &= check("scaled_price(%r) is None" % text, scaled_price(text) is None)

    # The exact-rupee reads: the discount badge and the `data-price`
    # attribute both carry plain rupees, with Indian digit grouping.
    for text, want in [("₹7,000", 7000.0),
                       ("₹2,32,000", 232000.0),      # Indian lakh grouping
                       # The badge abbreviates large discounts: ₹2.50L is
                       # 250,000, not 2.5. Read without the suffix it is a
                       # number in the wrong unit, which passes every
                       # coverage check while disagreeing with
                       # strike-minus-price by ₹249,998.
                       ("₹2.50L", 250000.0),
                       ("₹1,47,913/m", 147913.0)]:
        ok &= check("rupee_amount(%r) == %s" % (text, want),
                    rupee_amount(text) == want)

    # Indian grouping is 2-2-3, not 3-3-3: 2,32,000 is two hundred and
    # thirty-two thousand. A parser that assumed thousands-grouping would
    # read the same string as 232 or refuse it.
    ok &= check("Indian 2-2-3 grouping: ₹1,00,00,000 is one crore",
                rupee_amount("₹1,00,00,000") == 10000000.0)

    # prices_in returns every scaled price in a blob of text, in order —
    # which is how a card's displayed price and its strike price are read
    # from one node without either stealing the other's value.
    ok &= check("prices_in reads both prices in order",
                prices_in("5.86 Lakh 5.93 Lakh") == [586000.0, 593000.0])
    ok &= check("prices_in ignores an unscaled number between two prices",
                prices_in("5.86 Lakh 48500 km 5.93 Lakh")
                == [586000.0, 593000.0])
    ok &= check("prices_in on text with no price is empty",
                prices_in("Sportz 1.2 Kappa VTVT") == [])
    return ok


# ---------------------------------------------------------------------------
# Listing rows — pinned VALUES, on real cards
# ---------------------------------------------------------------------------
def test_listing_values():
    group("listing rows: the exact values six real cards must produce")
    ok = True

    r = _one(CARD_GRAND_I10, CARD_GRAND_I10_URL)
    ok &= check("Grand i10: sku", r.sku == "31483859")
    ok &= check("Grand i10: title", r.title == "2019 Hyundai Grand i10")
    ok &= check("Grand i10: brand", r.brand == "Hyundai")
    ok &= check("Grand i10: model", r.model == "Grand i10")
    ok &= check("Grand i10: variant", r.variant == "Sportz 1.2 Kappa VTVT")
    ok &= check("Grand i10: year", r.year == 2019)
    ok &= check("Grand i10: displayed price is 3.88 Lakh, in rupees",
                r.price == 388000.0)
    ok &= check("Grand i10: currency INR", r.currency == "INR")
    ok &= check("Grand i10: all-in price is the attribute's exact figure",
                r.price_all_in == 392000.0)
    # THE NOT-DISCOUNTED CASE. No strike, no badge, and the two travel
    # together on all 20 of the 705 cards that lack them — so this must be
    # None rather than 0, or a diff reports a phantom 0% discount forever.
    ok &= check("Grand i10: no strike price -> original_price is None",
                r.original_price is None)
    ok &= check("Grand i10: no strike price -> discount_pct is None",
                r.discount_pct is None)
    ok &= check("Grand i10: no badge -> discount_amount is None",
                r.discount_amount is None)
    ok &= check("Grand i10: odometer, as the card rounds it",
                r.km_driven == 63500)
    ok &= check("Grand i10: RTO", r.rto == "HR26")
    ok &= check("Grand i10: hub", r.hub == "Sector 27, Faridabad")
    ok &= check("Grand i10: fuel", r.fuel_type == "Petrol")
    ok &= check("Grand i10: transmission", r.transmission == "Manual")
    ok &= check("Grand i10: assurance tier", r.assurance == "assured")
    ok &= check("Grand i10: EMI", r.emi_monthly == 6675)
    ok &= check("Grand i10: tag", r.tag == "High quality, less driven")
    ok &= check("Grand i10: price_source names both reads",
                r.price_source == "dom+attr")
    ok &= check("Grand i10: car_city from the car's own URL",
                r.car_city == "faridabad")

    # THE NO-HUB CARD. 10 of 705 print no hub, and everything read by
    # POSITION in the card's span order shifts up by one on those ten. Every
    # other field here has to survive the gap.
    r = _one(CARD_CRETA_NO_HUB, CARD_CRETA_NO_HUB_URL)
    ok &= check("Creta: no hub on the card -> hub is None", r.hub is None)
    ok &= check("Creta: RTO survives the missing hub", r.rto == "DL8C")
    ok &= check("Creta: fuel survives the missing hub", r.fuel_type == "Petrol")
    ok &= check("Creta: transmission survives", r.transmission == "Automatic")
    ok &= check("Creta: odometer survives", r.km_driven == 48500)
    ok &= check("Creta: price", r.price == 586000.0)
    ok &= check("Creta: strike price", r.original_price == 593000.0)
    # Computed from the two prices, never read off the badge — and the badge
    # is here to check it against: 593,000 - 586,000 = 7,000 exactly.
    ok &= check("Creta: discount_amount is the site's own rupee figure",
                r.discount_amount == 7000.0)
    ok &= check("Creta: discount_pct computed from the two prices",
                r.discount_pct == 1.18)
    ok &= check("Creta: the site's own figure agrees with the two prices",
                r.original_price - r.price == r.discount_amount)

    # THE ABBREVIATED MAKE. "2016 Maruti Ciaz" on the card; the make is
    # maruti-suzuki in the URL. 178 of 705 abbreviate, so the brand cannot
    # come from the headline.
    r = _one(CARD_CIAZ, CARD_CIAZ_URL)
    ok &= check("Ciaz: brand expanded from the URL, not the headline",
                r.brand == "Maruti Suzuki")
    ok &= check("Ciaz: the headline itself still says 'Maruti'",
                r.title == "2016 Maruti Ciaz")
    ok &= check("Ciaz: model", r.model == "Ciaz")
    ok &= check("Ciaz: automatic", r.transmission == "Automatic")
    ok &= check("Ciaz: budget tier", r.assurance == "budget")

    r = _one(CARD_EON, CARD_EON_URL)
    ok &= check("Eon: the cheapest car in the capture, 1.76 Lakh",
                r.price == 176000.0)
    ok &= check("Eon: from the Bangalore listing, city off the car's URL",
                r.car_city == "bangalore")
    ok &= check("Eon: hub", r.hub == "Nexus Shanti Niketan Mall, Whitefield")
    ok &= check("Eon: RTO", r.rto == "KA53")

    r = _one(CARD_E_CLASS, CARD_E_CLASS_URL)
    ok &= check("E-Class: brand expanded from mercedes-benz",
                r.brand == "Mercedes Benz")
    ok &= check("E-Class: 48.68 Lakh", r.price == 4868000.0)
    ok &= check("E-Class: strike at 51 Lakh", r.original_price == 5100000.0)
    ok &= check("E-Class: the site's own discount, 2,32,000 rupees",
                r.discount_amount == 232000.0)
    ok &= check("E-Class: model", r.model == "E-Class")

    r = _one(CARD_RANGE_ROVER, CARD_RANGE_ROVER_URL)
    ok &= check("Range Rover: two-word make", r.brand == "Land Rover")
    # The model repeats the make's words. A "strip the brand off the
    # headline" read would leave "Sport"; the model comes from the URL.
    ok &= check("Range Rover: model keeps the repeated words",
                r.model == "Range Rover Sport")
    ok &= check("Range Rover: 85.90 Lakh", r.price == 8590000.0)
    ok &= check("Range Rover: category is the listing's, city removed",
                r.category == "luxury-cars")
    ok &= check("Range Rover: luxury tier", r.assurance == "luxury")

    # INVARIANTS ACROSS ALL SIX, checked as a set rather than one at a time.
    rows = [_one(h, u) for _, h, u in ALL_CARDS]
    ok &= check("every card yields exactly one row", all(rows))
    ok &= check("every row has a price", all(r.price for r in rows))
    ok &= check("every row has currency INR",
                all(r.currency == "INR" for r in rows))
    ok &= check("every row has price_source dom+attr",
                all(r.price_source == "dom+attr" for r in rows))
    ok &= check("every row's sku is the last URL segment",
                all(r.sku == r.url.rstrip("/").rsplit("/", 1)[-1] for r in rows))
    ok &= check("every image is on the site's own asset host",
                all(r.image_url.startswith("https://assets.spinny.com/")
                    for r in rows))
    # THE TWO PRICE BASES. The attribute includes RC transfer and insurance,
    # so it is the higher of the two on every card — 482 of 482 measured.
    # A row where it is lower means the reads have been crossed, which is
    # what computes a negative discount and still looks plausible.
    ok &= check("all-in price is never below the displayed price",
                all(r.price_all_in >= r.price for r in rows))
    # Never zero, never negative: the two figures either are a discount or
    # are not one.
    ok &= check("no row has an original_price at or below its price",
                all(r.original_price is None or r.original_price > r.price
                    for r in rows))
    ok &= check("no row has a non-positive discount_pct",
                all(r.discount_pct is None or r.discount_pct > 0 for r in rows))
    ok &= check("rating is null on every row (Spinny publishes none)",
                all(r.rating is None for r in rows))
    ok &= check("review_count is null on every row",
                all(r.review_count is None for r in rows))
    return ok


def test_cards_are_scoped_to_one_car():
    group("card scoping: a card may not read its neighbour's price")
    ok = True

    # Two cards in one grid, adjacent, as the site serves them. Each row must
    # carry its OWN price — the failure this guards is a scope that widens
    # one level too far and reports the neighbour's figures, which looks
    # entirely plausible in the output.
    rows = parse_products(grid(CARD_GRAND_I10, CARD_CRETA_NO_HUB),
                          CARD_GRAND_I10_URL)
    ok &= check("two cards -> two rows", len(rows) == 2)
    by = {r.sku: r for r in rows}
    ok &= check("card 1 keeps its own price",
                by["31483859"].price == 388000.0)
    ok &= check("card 2 keeps its own price",
                by["32071928"].price == 586000.0)
    # The Grand i10 has NO strike price and its neighbour has one. This is
    # the exact shape of the theft: if the scope leaked, the un-discounted
    # car would acquire the discounted one's was-price.
    ok &= check("the un-discounted card does not acquire a strike price",
                by["31483859"].original_price is None)
    ok &= check("the discounted card keeps its own strike price",
                by["32071928"].original_price == 593000.0)
    ok &= check("positions are 1 and 2, in document order",
                [r.position for r in rows] == [1, 2])

    # A junk link matching the URL shape by coincidence must not become a
    # row, and must not steal a real card's data.
    junk = ('<div data-base-component="card">'
            '<a href="/buy-used-cars/delhi/">All used cars in Delhi</a>'
            '</div>')
    rows = parse_products(grid(junk, CARD_CIAZ), CARD_CIAZ_URL)
    ok &= check("a link with no car id is not a row", len(rows) == 1)
    ok &= check("...and the real card is unaffected", rows[0].sku == "31419821")

    # The grid also holds SKELETON cards — 6 of 188 in one Bangalore
    # capture — with no link and no text, because the site has not hydrated
    # them yet. Not a fault, and not a row.
    skeleton = '<div data-base-component="card"><div></div></div>'
    rows = parse_products(grid(skeleton, CARD_EON, skeleton), CARD_EON_URL)
    ok &= check("skeleton cards are skipped, not counted as rows",
                len(rows) == 1 and rows[0].sku == "31749826")

    ok &= check("count_cards counts cards, not links",
                count_cards(grid(CARD_GRAND_I10, CARD_CRETA_NO_HUB),
                            CARD_GRAND_I10_URL) == 2)
    return ok


# ---------------------------------------------------------------------------
# The detail page
# ---------------------------------------------------------------------------
def test_detail_page():
    group("--mode detail: one car, from its own structured data")
    ok = True
    rows = parse_detail_page(DETAIL_PAGE, DETAIL_URL)
    ok &= check("a detail page yields exactly one row", len(rows) == 1)
    if not rows:
        return ok
    r = rows[0]

    ok &= check("detail: sku", r.sku == "31483859")
    ok &= check("detail: title", r.title == "2019 Hyundai Grand i10")
    ok &= check("detail: brand", r.brand == "Hyundai")
    # THE CAPITAL P. The page spells it `offers.Price`, not `offers.price`.
    # A parser reading the schema.org spelling gets None here, silently, on
    # every row of every detail run — which is why both are accepted and why
    # this check pins the value rather than the coverage.
    ok &= check("detail: all-in price read from offers.Price (capital P)",
                r.price_all_in == 392000.0)
    # `price` still means the DISPLAYED figure in both modes, or a diff
    # between a listing run and a detail run reports every row as changed.
    ok &= check("detail: price is the displayed figure, as on a card",
                r.price == 388000.0)
    ok &= check("detail: price_source names both reads",
                r.price_source == "dom+jsonld")
    ok &= check("detail: currency from the structured data", r.currency == "INR")
    ok &= check("detail: availability", r.in_stock is True)
    # The columns a listing card cannot carry.
    ok &= check("detail: EXACT odometer, not the card's rounding",
                r.km_driven_exact == 63417)
    ok &= check("detail: previous owners", r.owners == 1)
    ok &= check("detail: registration year", r.registration_year == 2019)
    ok &= check("detail: registration month", r.registration_month == "August")
    ok &= check("detail: colour", r.color == "grey")
    ok &= check("detail: seating capacity", r.seating_capacity == 5)
    ok &= check("detail: insurance validity", r.insurance_validity == "Jul 2027")
    ok &= check("detail: insurance type", r.insurance_type == "Third Party")
    # A detail page prints no strike of its own — 0 on the captured page —
    # and the "Similar cars" carousel's prices belong to other cars.
    ok &= check("detail: no original_price (the page prints no strike)",
                r.original_price is None)

    # THE PAIR. The listing card and the detail page are of the SAME car, so
    # the two modes can be checked against each other on real data rather
    # than on an assumption that they agree.
    card = _one(CARD_GRAND_I10, CARD_GRAND_I10_URL)
    same = ["sku", "title", "brand", "price", "currency", "model", "variant",
            "year", "fuel_type", "transmission", "rto", "hub", "car_city",
            "price_all_in", "url"]
    for field_name in same:
        ok &= check("listing and detail agree on %s" % field_name,
                    getattr(card, field_name) == getattr(r, field_name))
    # And the one that legitimately differs: the card's odometer is the
    # site's own rounding of the exact figure.
    ok &= check("the card rounds the odometer the detail page states exactly",
                card.km_driven == 63500 and r.km_driven_exact == 63417)
    # Spinny names a DIFFERENT photograph of the same car in its structured
    # data than on its card. Worth pinning so a listing-versus-detail diff is
    # not read as a parsing bug.
    ok &= check("the two modes name different photos of the same car",
                card.image_url != r.image_url
                and r.image_url.startswith("https://assets.spinny.com/"))

    meta = car_metadata(DETAIL_PAGE, DETAIL_URL)
    ok &= check("car_metadata: inspection report size",
                meta["parts_evaluated"] == "1573")
    ok &= check("car_metadata: inspector count", meta["inspectors"] == "5")
    ok &= check("car_metadata: canonical URL is tracking-free",
                meta["car_url"] == DETAIL_URL)

    # A page with neither structured data nor a recognisable car URL yields
    # nothing rather than an empty row that looks like a car.
    ok &= check("a page with no car in it yields no rows",
                parse_detail_page(page("<h1>Spinny</h1>"),
                                  "https://www.spinny.com/") == [])
    return ok


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def test_urls():
    group("URLs: hosts, kinds, ids, cities and categories")
    ok = True

    ok &= check("www.spinny.com is supported",
                is_supported_host("https://www.spinny.com/used-cars-in-delhi-ncr/s/"))
    ok &= check("spinny.com without www is supported",
                is_supported_host("https://spinny.com/used-cars-in-delhi-ncr/s/"))
    ok &= check("another site is refused",
                not is_supported_host("https://www.cars24.com/buy-used-car"))
    ok &= check("source is the bare hostname",
                site_host("https://www.spinny.com/used-cars-in-pune/s/")
                == "spinny.com")

    # Refused WITH THE REASON. "is not a Spinny site" would be FALSE for
    # api.spinny.com and would send the reader hunting for a typo that is not
    # there (§5).
    why = unsupported_reason("https://api.spinny.com/v3/api/listing/v3/")
    ok &= check("api.spinny.com is refused with its own reason",
                why is not None and "API" in why)
    ok &= check("...and that reason does not claim it is another company",
                why is not None and "not a Spinny" not in why)
    ok &= check("blog.spinny.com is refused with its own reason",
                (unsupported_reason("https://blog.spinny.com/x") or "")
                .startswith("is Spinny's blog"))
    ok &= check("a host that is not Spinny at all says so plainly",
                unsupported_reason("https://www.cars24.com/")
                == "is not a Spinny address")

    for url, kind in [
            ("https://www.spinny.com/used-cars-in-delhi-ncr/s/", "listing"),
            ("https://www.spinny.com/used-luxury-cars-in-delhi-ncr/s/", "listing"),
            ("https://www.spinny.com/used-volvo-cars-in-karnal/s/", "listing"),
            ("https://www.spinny.com/used-cars/s/", "cityless"),
            ("https://www.spinny.com/used-automatic-cars/s/", "cityless"),
            ("https://www.spinny.com/used-cars", "hub"),
            ("https://www.spinny.com/", "hub"),
            (DETAIL_URL, "detail"),
            ("https://www.spinny.com/sell-used-car/", "unknown")]:
        ok &= check("listing_kind(%s) == %s" % (url.split(".com")[1], kind),
                    listing_kind(url) == kind)

    # `cityless` is its own kind rather than a flavour of `listing`, because
    # it fails predictably: the grid container is served and stays empty,
    # since Spinny scopes inventory by city. Classified as an ordinary
    # listing it would look like a site outage.
    ok &= check("a cityless URL still counts as a listing to fetch",
                is_listing("https://www.spinny.com/used-cars/s/"))
    ok &= check("a detail URL is not a listing", not is_listing(DETAIL_URL))

    ok &= check("sku from a car URL", sku_from_url(DETAIL_URL) == "31483859")
    ok &= check("no sku from a listing URL",
                sku_from_url("https://www.spinny.com/used-cars-in-pune/s/") is None)
    ok &= check("car_city from a car URL",
                car_city_from_url(DETAIL_URL) == "faridabad")
    # These return the URL's own SLUGS, not display names. The slug is the
    # trustworthy source — the headline abbreviates the make on 178 of 705
    # cards — and MAKE_DISPLAY turns it into a column value separately, so
    # the two steps stay visible.
    ok &= check("make slug from a car URL", make_from_url(DETAIL_URL) == "hyundai")
    ok &= check("model slug from a car URL",
                model_from_url(DETAIL_URL) == "grand-i10")
    ok &= check("...and the display name is a separate step",
                make_display(make_from_url(DETAIL_URL)) == "Hyundai")
    ok &= check("car_path recognises a car URL",
                car_path(DETAIL_URL) is not None)
    ok &= check("car_path refuses a listing URL",
                car_path("https://www.spinny.com/used-cars-in-pune/s/") is None)

    # delhi-ncr is in the site's own footer list and is a REGION, not a city:
    # its listing returns cars whose own city segments are delhi, gurgaon,
    # ghaziabad, noida, faridabad, sonipat and karnal. Which is exactly why
    # `car_city` comes from the CAR's URL and `city_from_url` from the
    # listing's — they are different questions.
    ok &= check("city from a listing URL",
                city_from_url("https://www.spinny.com/used-cars-in-delhi-ncr/s/")
                == "delhi-ncr")
    ok &= check("the listing's city and the car's city are different reads",
                city_from_url(CARD_GRAND_I10_URL) == "delhi-ncr"
                and car_city_from_url(DETAIL_URL) == "faridabad")
    ok &= check("delhi-ncr is in the site's own city list",
                "delhi-ncr" in CITIES)

    # An unknown city is a WARNING, not a refusal: Spinny opens cities, and
    # a scraper that refuses a URL the site serves is worse than one that
    # says it has not seen this city before.
    warn = unknown_city_warning("https://www.spinny.com/used-cars-in-shimla/s/")
    ok &= check("an unknown city warns", warn is not None and "shimla" in warn)
    ok &= check("...and says the list is a snapshot, not a gate",
                warn is not None and "snapshot" in warn)
    ok &= check("a known city does not warn",
                unknown_city_warning(
                    "https://www.spinny.com/used-cars-in-pune/s/") is None)

    for url, cat in [
            ("https://www.spinny.com/used-cars-in-delhi-ncr/s/", "cars"),
            ("https://www.spinny.com/used-luxury-cars-in-delhi-ncr/s/", "luxury-cars"),
            ("https://www.spinny.com/used-volvo-cars-in-karnal/s/", "volvo-cars"),
            ("https://www.spinny.com/used-cars-under-1-lakh-rs-in-delhi-ncr/s/",
             "cars-under-1-lakh-rs"),
            ("https://www.spinny.com/used-automatic-cars/s/", "automatic-cars")]:
        ok &= check("category_from_url(%s) == %s"
                    % (url.split(".com")[1], cat),
                    category_from_url(url) == cat)

    ok &= check("tracking parameters are stripped",
                strip_tracking("https://www.spinny.com/used-cars-in-pune/s/"
                               "?utm_source=x&utm_medium=y&gclid=z")
                == "https://www.spinny.com/used-cars-in-pune/s/")
    ok &= check("a meaningful parameter survives stripping",
                "sort=" in strip_tracking(
                    "https://www.spinny.com/used-cars-in-pune/s/?sort=price&utm_source=x"))

    ok &= check("one currency, and it is a fact rather than a guess",
                host_currency("https://www.spinny.com/used-cars-in-pune/s/")
                == CURRENCY == "INR")
    ok &= check("one locale", locale_of("https://www.spinny.com/") == "en-IN")
    ok &= check("LOCALE_CURRENCY has exactly one entry",
                LOCALE_CURRENCY == {"en-IN": "INR"})
    ok &= check("HOSTS is the one storefront, with and without www",
                set(HOSTS) == {"spinny.com", "www.spinny.com"})

    # MAKE_DISPLAY exists because the URL slug is the trustworthy source and
    # its spelling is not what a reader wants in a column.
    ok &= check("make_display expands maruti-suzuki",
                make_display("maruti-suzuki") == "Maruti Suzuki")
    ok &= check("make_display expands land-rover",
                make_display("land-rover") == "Land Rover")
    ok &= check("make_display title-cases a make it has not met",
                make_display("some-new-make") == "Some New Make")
    ok &= check("model_from_headline drops the year and the make",
                model_from_headline("2019 Hyundai Grand i10", "Hyundai",
                                    "grand-i10") == "Grand i10")
    return ok


# ---------------------------------------------------------------------------
# Pagination — the single most important assumption in this repo
# ---------------------------------------------------------------------------
def test_pagination():
    group("pagination: there is none, and that has to be enforced")
    ok = True

    # MEASURED, not cautious: pages 1, 2 and 3 of one listing URL returned
    # byte-identical first cards under HTTP 200. A page_url() that built
    # ?page=N would fetch page one N times, find no new sku, conclude the
    # listing was exhausted and report a COMPLETE run holding a twentieth of
    # the catalogue.
    listing = "https://www.spinny.com/used-cars-in-delhi-ncr/s/"
    ok &= check("PAGINATED_KINDS is empty", PAGINATED_KINDS == ())
    for url in (listing, "https://www.spinny.com/used-cars/s/",
                "https://www.spinny.com/used-cars", DETAIL_URL):
        ok &= check("paginates_by_url is False for %s" % url.split(".com")[1],
                    paginates_by_url(url) is False)

    ok &= check("page 1 is the URL itself, tracking stripped",
                page_url(listing + "?utm_source=x", 1) == listing)
    for n in (2, 3, 50):
        ok &= check("page_url(..., %d) is None" % n,
                    page_url(listing, n) is None)
    ok &= check("page_number_from_url is always 1",
                page_number_from_url(listing + "?page=4") == 1)
    ok &= check("total_pages is always None", total_pages(grid(CARD_EON)) is None)

    # The parameter is still READ, so an engine handed a ?page=4 URL can say
    # why it will not do what the user expects. The site answers it 200, so
    # nothing else would ever say so.
    ok &= check("a ?page=N in the URL is noticed",
                requested_page_in_url(listing + "?page=4") == 4)
    ok &= check("?page=1 is not worth warning about",
                requested_page_in_url(listing + "?page=1") is None)
    warn = page_flow.page_param_warning(listing + "?page=4")
    ok &= check("...and the warning names --pages instead",
                warn is not None and "--pages 4" in warn)
    ok &= check("...and says the site IGNORES the parameter",
                warn is not None and "IGNORES" in warn)
    ok &= check("no warning when there is no ?page",
                page_flow.page_param_warning(listing) is None)

    # `pagination_is_addressable` is the tripwire the engines consult. False
    # everywhere today; if a deploy ever makes it True the engines say so
    # loudly rather than quietly taking one page.
    ok &= check("pagination is not addressable",
                page_flow.pagination_is_addressable(listing) is False)
    ok &= check("...not even when a next-link is advertised",
                page_flow.pagination_is_addressable(
                    listing, ["https://www.spinny.com/used-cars-in-delhi-ncr/s/?page=2"])
                is False)

    # --concurrency is refused with the reason, per URL kind.
    ok &= check("concurrency is capped at 1", page_flow.concurrency_limit(listing) == 1)
    refusal = page_flow.concurrency_refusal(listing)
    ok &= check("the refusal explains that ?page=N is ignored",
                refusal is not None and "IGNORED" in refusal)
    ok &= check("...and names what to do instead",
                refusal is not None and "--pages" in refusal
                and "filter URLs" in refusal)
    ok &= check("a detail URL is refused for its own reason",
                "one car" in (page_flow.concurrency_refusal(DETAIL_URL) or ""))
    ok &= check("a hub URL is refused for its own reason",
                "no car grid" in (page_flow.concurrency_refusal(
                    "https://www.spinny.com/used-cars") or ""))

    # The selector list has never matched anything, and every entry says so.
    # It is kept so a deploy that grows real pagination is noticed.
    ok &= check("the next-page selector list is non-empty",
                len(page_flow.NEXT_PAGE_SELECTOR) > 0)
    ok &= check("it leads with the standards-based signal",
                page_flow.NEXT_PAGE_SELECTOR[0] == 'link[rel="next"]')
    ok &= check("next_page_selector joins them for a query",
                "," in page_flow.next_page_selector(1))
    return ok


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------
LISTING_URL = "https://www.spinny.com/used-cars-in-delhi-ncr/s/"

# Chromium's own network-error page, hand-built to the shape a real one takes
# — this is the ONE fixture here that is not a capture, and it is labelled as
# such. A Selenium run through an unauthenticatable proxy produced 187,799
# bytes of this on a sibling site: it carries the SITE'S OWN HOSTNAME in its
# <title>, so a title check calls it a real page, and no vendor marker of any
# kind. Only "was this built out of the site's own assets?" answers it.
CHROMIUM_ERROR_PAGE = (
    '<html><head><title>www.spinny.com</title></head><body>'
    '<div id="main-frame-error"><span jscontent="heading.msg">'
    'www.spinny.com refused to connect.</span>'
    '<div class="error-code">ERR_PROXY_CONNECTION_FAILED</div></div>'
    '</body></html>')


def test_page_state():
    group("page state: content, empty, notfound, blocked — in that order")
    ok = True

    # 1. CONTENT. Cars on the page outrank every signal that the page might
    #    not have any — see the luxury-listing trap below.
    ok &= check("a grid with cars is content",
                detect_page_state(grid(CARD_EON), 200, LISTING_URL) == "content")
    ok &= check("a detail page with its own JSON-LD is content",
                detect_page_state(DETAIL_PAGE, 200, DETAIL_URL) == "content")

    # THE ORDERING TRAP, from a real draft of this function. A FILTERED
    # listing carries the alert-signup card that the zero-result page also
    # carries; an earlier version checked emptiness first and reported the
    # luxury listing EMPTY while it held 41 cars.
    ok &= check("cars outrank a zero-result marker on the same page",
                detect_page_state(
                    grid(CARD_RANGE_ROVER) + heading(41, "Used Luxury cars in Delhi NCR"),
                    200, "https://www.spinny.com/used-luxury-cars-in-delhi-ncr/s/")
                == "content")

    # 2. EMPTY — a real answer to the question that was asked, and NOT
    #    retried: retrying re-confirms the same right answer and rotating the
    #    exit blames an address for the URL it was given.
    ok &= check("the site's own zero-count heading is empty, not blocked",
                detect_page_state(
                    page('<div data-id="landing-plp-container"></div>')
                    + heading(0, "Used Volvo cars in Karnal"),
                    200, "https://www.spinny.com/used-volvo-cars-in-karnal/s/")
                == "empty")
    ok &= check("an SEO landing page is empty, not unknown",
                detect_page_state(page("<h1>Used cars</h1>"), 200,
                                  "https://www.spinny.com/used-cars") == "empty")
    ok &= check("a cityless listing is empty, not a site outage",
                detect_page_state(page('<div data-id="landing-plp-container"></div>'),
                                  200, "https://www.spinny.com/used-cars/s/")
                == "empty")
    ok &= check("is_no_results reads the site's own arithmetic",
                is_no_results(page(heading(0, "Used Volvo cars in Karnal"))))
    ok &= check("...and 41 results is not no results",
                not is_no_results(page(heading(41, "Used Luxury cars in Delhi NCR"))))

    # 3. NOTFOUND — the caller's problem, not an address problem. It IS a
    #    Spinny page and carries only three asset references where a real one
    #    carries hundreds, so it has to be recognised BEFORE the inverted
    #    check or the reader goes hunting for a proxy fault.
    ok &= check("a 404 status is notfound",
                detect_page_state("<html><body>nope</body></html>", 404,
                                  "https://www.spinny.com/nope/s/") == "notfound")
    ok &= check("is_not_found does not need the status",
                is_not_found("<html><body>nope</body></html>", 404))

    # 4. BLOCKED — and on this site that is almost always the access path.
    ok &= check("no markup at all is blocked", detect_page_state(None) == "blocked")
    ok &= check("empty markup is blocked", detect_page_state("") == "blocked")
    ok &= check("a 503 is blocked",
                detect_page_state(grid(CARD_EON), 503, LISTING_URL) == "blocked")
    # THE CASE INVERTED DETECTION EXISTS FOR.
    ok &= check("Chromium's own error page is blocked, not content",
                detect_page_state(CHROMIUM_ERROR_PAGE, 200, LISTING_URL) == "blocked")
    ok &= check("...even though it carries the site's own hostname",
                "www.spinny.com" in CHROMIUM_ERROR_PAGE)
    ok &= check("...and no vendor marker matches it",
                detect_bot_challenge(CHROMIUM_ERROR_PAGE, LISTING_URL) is None)
    ok &= check("served_by_spinny is False for it",
                not served_by_spinny(CHROMIUM_ERROR_PAGE))
    ok &= check("served_by_spinny is True for a page built from its assets",
                served_by_spinny(grid(CARD_EON)))

    # 5. UNKNOWN — the shell. Spinny's FIRST RESPONSE to every listing URL
    #    is this, so it must not be a fault and must not be retried.
    shell = page('<div data-id="landing-plp-container"></div>'
                 + heading(1559))
    ok &= check("a served shell with a non-zero heading is unknown",
                detect_page_state(shell, 200, LISTING_URL) == "unknown")
    ok &= check("...and page_flow calls that 'not painted yet'",
                page_flow.is_unpainted("unknown", shell))
    ok &= check("a genuinely empty page is NOT 'not painted yet'",
                not page_flow.is_unpainted("empty", shell))
    return ok


def test_recaptcha_is_not_a_block_marker():
    group("the site's own reCAPTCHA is a fact about the site, not a marker")
    ok = True

    # §18, and this is the check that rule asks for: COUNT A MARKER ON A PAGE
    # YOU KNOW IS GOOD before adding it. Spinny loads reCAPTCHA Enterprise v3
    # on every page it serves — 31 occurrences of "recaptcha" on a capture
    # that had just delivered 482 cars — so `recaptcha` and `g-recaptcha` as
    # markers would have made every healthy page blocked. Both were inherited
    # from the sibling repos and both were removed.
    for banned in ("recaptcha", "g-recaptcha", "grecaptcha", "recaptcha/api.js"):
        ok &= check("%r is NOT a challenge marker" % banned,
                    banned not in BOT_CHALLENGE_MARKERS)

    loader = ('<script src="https://www.google.com/recaptcha/enterprise.js'
              '?render=6Lc_rqYoAAAAAHwcTbMntlDkC52H6QAYgYE7eUKp"></script>'
              '<div class="grecaptcha-badge"></div>'
              '<textarea id="g-recaptcha-response"></textarea>')
    good = grid(CARD_EON) + loader
    ok &= check("a good page carrying the v3 loader is still content",
                detect_page_state(good, 200, LISTING_URL) == "content")
    ok &= check("...and no vendor marker fires on it",
                detect_bot_challenge(good, LISTING_URL) is None)

    # "No challenge rendered" is not "no captcha configured" (§18). What IS
    # read off the page is which task type a solve would have to buy — an
    # enterprise v3 score task, not a v2 checkbox, and the two are priced
    # and solved differently.
    cfg = recaptcha_config(good)
    ok &= check("the site's own config is read", cfg is not None)
    ok &= check("...as v3", cfg and cfg["version"] == "v3")
    ok &= check("...enterprise", cfg and cfg["enterprise"] == "true")
    ok &= check("...with its sitekey",
                cfg and cfg["sitekey"].startswith("6Lc_rqYo"))
    # render=explicit means v2, and the LOADER wins over anything the
    # wrapper declares: v3 parameters bought for a v2 widget produce a token
    # the site rejects (§8).
    v2 = recaptcha_config('<script src="https://www.google.com/recaptcha/'
                          'api.js?render=explicit"></script>')
    ok &= check("render=explicit reads as v2", v2 and v2["version"] == "v2")
    ok &= check("...and not as enterprise", v2 and v2["enterprise"] == "false")

    # A RENDERED widget is a different thing and IS a marker.
    rendered = grid(CARD_EON) + ('<iframe src="https://www.google.com/'
                                 'recaptcha/api2/anchor?k=x"></iframe>')
    ok &= check("a rendered anchor iframe IS a marker",
                detect_bot_challenge(rendered, LISTING_URL) is not None)

    # The reason a solve buys nothing here, said once where a user asking
    # "why didn't it solve the captcha" will see it.
    note = page_flow.recaptcha_note()
    ok &= check("recaptcha_note explains that v3 scores rather than challenges",
                "scores" in note and "v3" in note)
    return ok


def test_extension_markers_are_not_the_sites():
    group("the Scraping Browser's own auto-solve extension is not a challenge")
    ok = True
    # The 2Captcha Scraping Browser API injects its own hunters into every
    # page it loads, so `cf-turnstile` appears in the markup of a perfectly
    # good listing fetched over --cdp-endpoint. A sibling repo's first live
    # run reported exit 3 on a 1.8 MB page holding the full catalogue for
    # exactly this reason.
    injected = ('<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenoo'
                'emhbpbo/content/captcha/turnstile/hunter.js" '
                'data-ts-input="cf-turnstile-response"></script>')
    ok &= check("an extension-injected marker does not block a good page",
                detect_page_state(grid(CARD_EON) + injected, 200, LISTING_URL)
                == "content")
    ok &= check("...and no vendor is reported for it",
                detect_bot_challenge(grid(CARD_EON) + injected, LISTING_URL)
                is None)
    # A challenge from the SITE, in a page the site served, still fires.
    real = ('<iframe src="https://challenges.cloudflare.com/turnstile/x">'
            '</iframe>')
    ok &= check("a first-party challenge iframe still fires",
                detect_bot_challenge(page(real), LISTING_URL) is not None)
    return ok


# ---------------------------------------------------------------------------
# page_flow — the policy all three engines share
# ---------------------------------------------------------------------------
def test_page_flow():
    group("page_flow: the decisions all three engines must make identically")
    ok = True

    # The policy is DATA, so three engines cannot quietly disagree about
    # whether a page is worth retrying or worth paying for.
    for state, retry, solve, blocked, parse in [
            ("content",   False, False, False, True),
            ("empty",     False, False, False, True),
            ("notfound",  False, False, False, False),
            ("blocked",   True,  False, True,  False),
            ("challenge", True,  True,  True,  False),
            ("unknown",   True,  False, False, False)]:
        ok &= check("%s: retry=%s" % (state, retry),
                    page_flow.should_retry(state) is retry)
        ok &= check("%s: solve=%s" % (state, solve),
                    page_flow.should_solve(state) is solve)
        ok &= check("%s: blocked=%s" % (state, blocked),
                    page_flow.counts_as_blocked(state) is blocked)
        ok &= check("%s: parse=%s" % (state, parse),
                    page_flow.should_parse(state) is parse)
    ok &= check("an unrecognised state falls back to the unknown policy",
                page_flow.should_retry("something-new") is True)
    ok &= check("notfound is the caller's problem, not the site's",
                page_flow.is_usage_error("notfound") is True)
    ok &= check("...and blocked is not",
                page_flow.is_usage_error("blocked") is False)

    # THE SHELL. Spinny's first response is always one, so this decides
    # whether the primary path works at all.
    shell = page('<div data-id="landing-plp-container"></div>' + heading(1559))
    ok &= check("a served shell waits", page_flow.is_unpainted("unknown", shell))
    ok &= check("nothing at all does not wait",
                not page_flow.is_unpainted("unknown", None))
    ok &= check("a page with cars does not wait",
                not page_flow.is_unpainted("content", grid(CARD_EON)))

    # wait_for_count polls through the protocol rather than evaluating a
    # STRING, which a site whose CSP has no unsafe-eval refuses outright —
    # that took a sibling repo's run down with exit 1 on its most obvious URL.
    counts = iter([0, 1, 3, 5, 9])
    slept = []
    found = page_flow.wait_for_count(lambda sel: next(counts), slept.append,
                                     "a", 4, 10_000, poll_ms=100)
    ok &= check("wait_for_count returns as soon as the threshold is met",
                found == 5)
    ok &= check("...having slept once per round below it", len(slept) == 3)
    # A timeout is not an error: a listing with genuinely no cars never
    # reaches the threshold, and that is exit 4 rather than a fault.
    found = page_flow.wait_for_count(lambda sel: 0, lambda ms: None,
                                     "a", 4, 300, poll_ms=100)
    ok &= check("a timeout returns the count rather than raising", found == 0)
    ok &= check("a driver fault returns rather than taking the run down",
                page_flow.wait_for_count(
                    lambda sel: (_ for _ in ()).throw(RuntimeError("gone")),
                    lambda ms: None, "a", 4, 300) == 0)

    # THE SCROLL. Round 7 of the real measurement added ZERO cars and round 8
    # added forty, so a one-round stability test would have stopped at 182 of
    # 1559. Three rounds is the measured requirement.
    seq = [22, 42, 82, 122, 142, 162, 182, 182, 222, 242, 302, 302, 302, 302]
    heights = [8004, 11936, 15037, 17434, 18933, 20770, 21328, 21328,
               24427, 27064, 32008, 32008, 32008, 32008]
    state = {"i": 0}

    def count(sel):
        return seq[min(state["i"], len(seq) - 1)]

    def height():
        return heights[min(state["i"], len(heights) - 1)]

    def scroll():
        state["i"] += 1

    res = page_flow.scroll_until_settled(count=count, page_height=height,
                                         scroll_to_bottom=scroll,
                                         sleep=lambda ms: None,
                                         selector="a")
    ok &= check("the scroll settles once the grid stops growing",
                res["settled"] is True)
    ok &= check("...at the full 302 cards, not at the zero-growth round 7",
                res["cards"] == 302)
    ok &= check("...and records the count after every round",
                res["boundaries"][:3] == [22, 42, 82])
    ok &= check("a one-round stability test would have stopped at 182",
                seq[6] == seq[7] == 182 and seq[8] == 222)

    # `--pages N` is a scroll budget: N batches of 20.
    ok &= check("target_cards(5) is 100", page_flow.target_cards(5) == 100)
    ok &= check("the rounds budget exceeds the batch count",
                page_flow.scroll_rounds_for(5) > 5)
    ok &= check("...and is capped", page_flow.scroll_rounds_for(10_000)
                == page_flow.SCROLL_ROUNDS_CAP)
    state["i"] = 0
    res = page_flow.scroll_until_settled(count=count, page_height=height,
                                         scroll_to_bottom=scroll,
                                         sleep=lambda ms: None, selector="a",
                                         want_cards=100)
    ok &= check("asking for 100 cars stops once 122 have arrived",
                res["reached_target"] is True and res["cards"] == 122)
    ok &= check("...and does not claim the listing was exhausted",
                res["settled"] is False)

    # A budget that runs out with the page still growing is PARTIAL, and the
    # run has to say so or the missing tail reads as delisted cars.
    state["i"] = 0
    res = page_flow.scroll_until_settled(count=count, page_height=height,
                                         scroll_to_bottom=scroll,
                                         sleep=lambda ms: None, selector="a",
                                         rounds=3)
    ok &= check("a spent round budget is neither settled nor on target",
                res["settled"] is False and res["reached_target"] is False)

    # pages_completed counts BATCHES the RUN HOLDS — from the rows in the
    # file, not from the scroll's own last count. The two differ: the scroll
    # stops the moment its target is reached and the page keeps hydrating
    # before the snapshot, so three engines that all wrote 62 rows on
    # 2026-09-11 had scroll counters reading 62, 42 and 62. A sidecar built
    # on the counter reported "3 batches" beside four batches' worth of cars,
    # and the three engines disagreed about an identical run.
    ok &= check("302 rows is 16 batches of 20",
                page_flow.batches_delivered(302) == 16)
    ok &= check("62 rows is 4 batches, whatever the scroll counter said",
                page_flow.batches_delivered(62) == 4)
    ok &= check("20 rows is 1 batch", page_flow.batches_delivered(20) == 1)
    ok &= check("no rows is no batches", page_flow.batches_delivered(0) == 0)

    # COMPLETENESS IS ARITHMETIC on this site, because Spinny prints its own
    # total. 482 rows against an advertised 1559 is proof, not suspicion.
    comp = page_flow.completeness(482, page(heading(1559)),
                                  {"settled": True, "reached_target": False})
    ok &= check("the advertised total is read", comp["total_available"] == 1559)
    ok &= check("...and the gap is arithmetic", comp["short_by"] == 1077)
    comp = page_flow.completeness(41, page(heading(41, "Used Luxury cars in Delhi NCR")),
                                  {"settled": True})
    ok &= check("a listing that was fully taken is short by nothing",
                comp["short_by"] == 0)
    comp = page_flow.completeness(20, page("<div></div>"), None)
    ok &= check("no advertised total means short_by is None, not zero",
                comp["total_available"] is None and comp["short_by"] is None)

    # THE RUN STATUS, in one place for all three engines — and the case that
    # matters was found by a live run rather than by reading the code.
    #
    # Through a rotating residential gateway the grid stopped growing at 22
    # cars, because every request leaves from a different address and the
    # hydration XHR for the second batch never landed. The scroll did exactly
    # what it is told to do, and the run reported COMPLETE while holding 22
    # of an advertised 1546 — with `short_by: 1524` in the same sidecar
    # contradicting it.
    settled = {"settled": True, "reached_target": False}
    ok &= check("a settled scroll the site's own count agrees with is "
                "complete",
                page_flow.listing_stop_reason(settled, {"total_available": 40},
                                              40) == "no_new_products")
    ok &= check("...but a settled scroll far below the advertised total is a "
                "STALL, not an exhausted listing",
                page_flow.listing_stop_reason(settled,
                                              {"total_available": 1546}, 22)
                == "scroll_stalled")
    ok &= check("...and scroll_stalled is NOT a complete stop reason",
                "scroll_stalled" not in COMPLETE_STOP_REASONS)
    ok &= check("where the site advertises no total, a settled scroll still "
                "reads as exhausted — there is nothing better to judge by",
                page_flow.listing_stop_reason(settled, {"total_available": None},
                                              22) == "no_new_products")
    ok &= check("reaching the batches that were asked for is complete",
                page_flow.listing_stop_reason(
                    {"settled": False, "reached_target": True},
                    {"total_available": 1546}, 62) == "completed")
    ok &= check("a spent round budget is partial",
                page_flow.listing_stop_reason(
                    {"settled": False, "reached_target": False},
                    {"total_available": 1546}, 182)
                == "scroll_budget_exhausted")
    ok &= check("no scroll at all is not silently complete",
                page_flow.listing_stop_reason(None, None, 0)
                == "scroll_budget_exhausted")

    # The retry budget, computed in ONE place so the engines cannot disagree
    # — and so RETRY_ON_BLOCKED has a reader rather than a paragraph of
    # justification nobody consults.
    ok &= check("with a pool, the budget is the caller's",
                page_flow.block_retry_budget(True, 5) == 5)
    ok &= check("without one, it is the measured single retry",
                page_flow.block_retry_budget(False, 5)
                == page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    ok &= check("a negative budget floors at zero",
                page_flow.block_retry_budget(True, -1) == 0)
    ok &= check("RETRY_ON_BLOCKED is True and is consulted here",
                page_flow.RETRY_ON_BLOCKED is True)
    ok &= check("a speculative solve is capped at one per page",
                page_flow.SOLVES_PER_PAGE == 1)

    ok &= check("the readiness anchor differs by mode",
                page_flow.ready_selector("listing")
                != page_flow.ready_selector("detail"))
    ok &= check("a listing needs more than one match to count as rendered",
                page_flow.min_matches("listing") > 1)
    ok &= check("a detail page needs one", page_flow.min_matches("detail") == 1)
    ok &= check("MIN_CARD_MATCHES agrees with the parser's",
                page_flow.MIN_CARD_MATCHES == MIN_CARD_MATCHES)
    ok &= check("comparable() strips tracking on both sides",
                page_flow.comparable(LISTING_URL + "?utm_source=x")
                == page_flow.comparable(LISTING_URL))
    return ok
# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------
def test_output_contract():
    group("the output contract shared across this scraper family")
    ok = True
    names = [f.name for f in fields(Product)]
    # The family prefix, byte-identical and in order, so a consumer written
    # against another repo in this family reads the first sixteen columns
    # unchanged. Site-specific columns go AFTER it.
    family_prefix = ["source", "scraped_at", "url", "sku", "title", "brand",
                     "price", "currency", "original_price", "discount_pct",
                     "rating", "review_count", "in_stock", "image_url",
                     "category", "price_source"]
    ok &= check("the family field prefix is present and in order",
                names[:len(family_prefix)] == family_prefix)
    ok &= check("Spinny's own columns come after it, in order",
                names[len(family_prefix):] ==
                ["page", "position", "model", "variant", "year", "km_driven",
                 "fuel_type", "transmission", "rto", "hub", "car_city",
                 "assurance", "price_all_in", "discount_amount",
                 "emi_monthly", "tag", "km_driven_exact", "owners",
                 "registration_year", "registration_month", "color",
                 "seating_capacity", "insurance_validity", "insurance_type"])
    ok &= check("both modes map to a row class",
                ROW_CLASS_BY_MODE == {"listing": Product, "detail": Product})
    # A column that is null on every row of every run should not exist (§9).
    # These eight are null on every LISTING row and populated in --mode
    # detail, which is a different thing and is why they are kept — pinned
    # so that removing them needs a measurement rather than a hunch.
    ok &= check("the detail-only columns are declared",
                {"km_driven_exact", "owners", "registration_year",
                 "registration_month", "color", "seating_capacity",
                 "insurance_validity", "insurance_type"} <= set(names))
    # And these two are null on every row of BOTH modes, on purpose: Spinny
    # publishes no car-level rating and no per-car review count anywhere.
    # The columns stay because the family's consumers read them by name, and
    # the measurement that says they are always null is written down here so
    # a future capture can overturn it.
    ok &= check("rating and review_count are declared and always null",
                {"rating", "review_count"} <= set(names)
                and all(getattr(_one(h, u), "rating") is None
                        and getattr(_one(h, u), "review_count") is None
                        for _, h, u in ALL_CARDS))
    ok &= check("both modes are one row per sku",
                set(UNIQUE_BY_SKU_MODES) == {"listing", "detail"})

    ok &= check("the exit codes are the family's",
                (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL) == (3, 4, 6))
    ok &= check("an exhausted listing counts as complete",
                "no_new_products" in COMPLETE_STOP_REASONS
                and "pagination_exhausted" in COMPLETE_STOP_REASONS)
    ok &= check("a single-page mode is complete by construction",
                "single_page_mode" in COMPLETE_STOP_REASONS)

    # No defaulted currency anywhere: a row that could not establish one says
    # None rather than claiming EUR, which would be wrong for the four
    # non-euro country sites.
    ok &= check("Product defaults currency to None, not a guess",
                Product().currency is None)
    ok &= check("Product defaults price_source to None",
                Product().price_source is None)
    return ok


def test_writers():
    group("writers, dedupe and the refusal to overwrite good data")
    ok = True
    rows = [Product(sku="1", url="u1", price=1.0),
            Product(sku="2", url="u2", price=2.0)]
    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "out")

        # A run that finds nothing writes NOTHING: a consumer cannot tell an
        # empty category from a failed run, and the failure destroys the last
        # known good data.
        save(rows, prefix, "json", allow_empty=False)
        ok &= check("a good run writes its output",
                    os.path.exists(prefix + ".json"))
        before = open(prefix + ".json").read()
        save([], prefix, "json", allow_empty=False)
        ok &= check("an empty run does NOT overwrite the previous good output",
                    open(prefix + ".json").read() == before)
        save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out and does overwrite",
                    json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(d, "empty.csv")
        write_csv([], csv_path, row_cls=Product)
        header = open(csv_path).read().strip().split("\n")[0]
        ok &= check("an empty CSV still carries its header",
                    header.split(",")[:4] == ["source", "scraped_at", "url", "sku"])

        # A list column has to survive CSV without becoming a Python repr.
        #
        # NO Spinny column is a list — the site publishes no image gallery
        # and no attribute list this repo reads — so this is checked with a
        # local row class rather than with Product. The joining is kept in
        # write_csv because it is generic and because a future column may
        # need it; pinning the CURRENT behaviour is what stops it being
        # deleted as dead or reappearing as a repr() by accident.
        ok &= check("no Product column is a list today",
                    not [f for f in fields(Product)
                         if "List" in str(f.type)])

        from dataclasses import dataclass as _dataclass
        from typing import List as _List, Optional as _Optional

        @_dataclass
        class _WithList:
            sku: _Optional[str] = None
            things: _Optional[_List[str]] = None

        csv_path = os.path.join(d, "list.csv")
        write_csv([_WithList(sku="1", things=["a", "b"])], csv_path,
                  row_cls=_WithList)
        body = open(csv_path).read()
        ok &= check("a list column is joined, not repr()d in CSV",
                    ("a" + LIST_CSV_SEPARATOR + "b") in body and "['a'" not in body)

    seen = set()
    ok &= check("dedupe drops a repeated sku",
                len(dedupe_by_sku([Product(sku="a"), Product(sku="a")], seen)) == 1)
    # A row with no key is always KEPT: there is nothing to check a duplicate
    # against, and dropping it is a silent data loss rather than a dedupe.
    ok &= check("a row with no sku is kept, not dropped",
                len(dedupe_by_key([Product(sku=None), Product(sku=None)],
                                  set())) == 2)

    meta = run_meta("complete", "completed", 3, 3, "u", "u", 36,
                    pages_failed=[], mode="listing", source="spinny.com")
    ok &= check("the sidecar records status, mode and source",
                meta["status"] == "complete" and meta["mode"] == "listing"
                and meta["source"] == "spinny.com")
    # A count stops being a description once a page can fail while later ones
    # succeed, so the sidecar names WHICH pages failed.
    meta = run_meta("partial", "blocked", 5, 3, "u", "u", 12,
                    pages_failed=[2, 4], mode="listing", source="spinny.com")
    ok &= check("the sidecar names which pages failed, by number",
                meta["pages_failed"] == [2, 4])
    return ok


def test_finish_run():
    group("finish_run: the exit codes all three engines must agree on")
    ok = True
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "run")
        rows = [Product(sku="1", url="u")]

        code = finish_run(rows, p, "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="spinny.com", start_url="u", final_url="u")
        ok &= check("a complete run exits 0", code == 0)

        code = finish_run([], p + "b", "json", False, blocked=True,
                          stop_reason="blocked_no-response",
                          pages_requested=1, pages_completed=0,
                          pages_failed=[1], mode="listing",
                          source="spinny.com", start_url="u", final_url="u")
        ok &= check("a blocked run exits 3, not 4", code == EXIT_BLOCKED)
        # A FAILED run writes no sidecar: `save` leaves the previous good
        # output in place, and a "failed" sidecar beside good data would
        # contradict it.
        ok &= check("a failed run writes no sidecar beside older good data",
                    not os.path.exists(p + "b.meta.json"))

        code = finish_run([], p + "c", "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="spinny.com", start_url="u", final_url="u")
        ok &= check("a genuinely empty result exits 4, not 3",
                    code == EXIT_NO_PRODUCTS)

        code = finish_run(rows, p + "d", "json", False, blocked=False,
                          stop_reason="page_load_timeout", pages_requested=5,
                          pages_completed=2, pages_failed=[3], mode="listing",
                          source="spinny.com", start_url="u", final_url="u")
        ok &= check("a run with data that stopped early exits 6 (partial)",
                    code == EXIT_PARTIAL)
        ok &= check("a partial run still writes what it got",
                    os.path.exists(p + "d.json"))
    return ok


def test_diff():
    group("diff_runs")
    ok = True
    # `price_source` on this site is "dom" for a listing row and
    # "meta+apollo" for a detail row — there is no structured price on a
    # listing page to confirm against — so the source-change case uses those
    # two values rather than the sibling repos' jsonld pair.
    old = [{"sku": "1", "price": 10.0, "price_source": "dom"},
           {"sku": "2", "price": 20.0, "price_source": "dom"},
           {"sku": "3", "price": 30.0, "price_source": "dom"}]
    new = [{"sku": "1", "price": 11.0, "price_source": "dom"},
           {"sku": "3", "price": 30.5, "price_source": "meta+apollo"},
           {"sku": "4", "price": 40.0, "price_source": "dom"}]
    d = diff_products(old, new)
    ok &= check("a real price move is reported as changed",
                any(c["sku"] == "1" for c in d["changed"]))
    ok &= check("a delisted product is reported as removed",
                [r["sku"] for r in d["removed"]] == ["2"])
    ok &= check("a new product is reported as added",
                [r["sku"] for r in d["added"]] == ["4"])
    # A price difference that comes with a price_source difference says
    # something about OUR two snapshots, not about the shop.
    ok &= check("a price move with a source change is not 'changed'",
                not any(c["sku"] == "3" for c in d["changed"]))
    ok &= check("...it is reported separately as source_changed",
                any(c["sku"] == "3" for c in d.get("source_changed", [])))

    # `price` and `price_all_in` are BOTH tracked, and the pair is the point
    # on this site: the displayed figure excludes RC transfer and insurance
    # while the site's own attribute includes them, so a car whose displayed
    # price is unchanged while its all-in figure rose has had a FEE changed,
    # not a price cut. A monitor watching one of them would report the wrong
    # thing in both directions.
    tracked = __import__("diff_runs").TRACKED_FIELDS
    ok &= check("both price bases are tracked",
                "price" in tracked and "price_all_in" in tracked)
    # The site's own exact rupee discount is tracked as a cross-check on the
    # percentage this repo computes: the two moving apart means the read
    # broke rather than the price having changed.
    ok &= check("the site's own discount figure is tracked",
                "discount_amount" in tracked)
    # And these are NOT tracked, because Spinny publishes neither anywhere —
    # measured across three captures, 705 cards, and one detail page. Two
    # columns that can never differ would be two lines of noise in every
    # diff. Pinned so that adding one needs a measurement.
    ok &= check("rating and review_count are not tracked (the site has none)",
                not {"rating", "review_count"} & set(tracked))
    # The sibling repos' columns, pinned absent in both directions so that
    # porting one is a decision rather than a paste.
    ok &= check("no sibling-repo columns are tracked",
                not {"price_is_from", "price_max", "sold", "sold_is_floor"}
                & set(tracked))
    ok &= check("...and Product does not declare them either",
                not {"price_is_from", "price_max", "sold", "sold_is_floor"}
                & {f.name for f in fields(Product)})
    # Every tracked field must actually EXIST on the row class, or the diff
    # silently compares None to None forever.
    ok &= check("every tracked field is a real column",
                set(tracked) <= {f.name for f in fields(Product)})
    return ok


def test_pyppeteer_teardown_noise():
    group("pyppeteer teardown noise is suppressed, and its limit is pinned")
    ok = True
    try:
        import puppeteer_scraper as pyp
    except ImportError:
        return check("pyppeteer engine present (skipped: library absent)", True)

    handler = pyp._AsyncBridge._on_loop_exception.__func__ if hasattr(
        pyp._AsyncBridge._on_loop_exception, "__func__") else pyp._AsyncBridge._on_loop_exception

    class _Loop:
        def __init__(self): self.passed_through = []
        def default_exception_handler(self, context):
            self.passed_through.append(context)

    # Each of these arrives on a run that SUCCEEDED, after the output is
    # written, and four tracebacks under a healthy run is how a reader learns
    # to ignore the log.
    swallowed = [
        {"message": "Task was destroyed but it is pending"},
        {"message": "Future exception was never retrieved",
         "exception": RuntimeError("Protocol error (Target.sendMessageToTarget): "
                                   "No session with given id")},
        {"exception": RuntimeError("Target closed")},
        {"exception": RuntimeError("Connection closed")},
        {"message": "Event loop is closed"},
    ]
    for context in swallowed:
        loop = _Loop()
        handler(loop, context)
        label = (context.get("message") or str(context.get("exception")))[:44]
        ok &= check("teardown noise suppressed: %s" % label,
                    not loop.passed_through)

    # A REAL error must still get through, or the suppression has become a
    # blindfold.
    loop = _Loop()
    handler(loop, {"exception": ValueError("something actually went wrong")})
    ok &= check("a real exception is NOT swallowed", len(loop.passed_through) == 1)

    # The handler reads BOTH fields. It used to read `exception or message`,
    # which meant a context carrying both never had its message inspected —
    # so the asyncio-worded ones kept printing after they were "handled".
    src = inspect.getsource(handler)
    ok &= check("the handler inspects the message as well as the exception",
                'for k in ("exception", "message")' in src)

    # PINNED LIMITATION, not a guard: `Exception ignored in: <coroutine
    # object Connection._recv_loop>` is printed by CPython's garbage
    # collector at interpreter shutdown, after the loop is gone and after the
    # exit code is decided. No loop handler can reach it, and catching it
    # would mean a global unraisable hook that swallows real bugs too. It is
    # documented in TROUBLESHOOTING.md instead; this check makes sure that
    # documentation stays there.
    doc = open(os.path.join(REPO_ROOT, "TROUBLESHOOTING.md"),
               encoding="utf-8").read()
    ok &= check("the shutdown-time traceback is documented rather than hidden",
                "Exception ignored in" in doc and "The run succeeded" in doc)
    return ok


def test_canary_separates_access_from_defect():
    group("the canary fails on defects and only WARNS on access conditions")
    ok = True
    wf_path = os.path.join(REPO_ROOT, ".github", "workflows", "canary.yml")
    wf = open(wf_path, encoding="utf-8").read()

    # The data checks must not run on a blocked or refused run: there is no
    # output file, and a missing file would fail for the wrong reason.
    ok &= check("the data checks are gated on the run having got in",
                "steps.verdict.outputs.tested == 'true'" in wf)

    # Extract the real interpret-the-exit-code script and run it under bash
    # for every code, rather than asserting on the YAML text. What matters is
    # whether the JOB FAILS, and only running it answers that.
    try:
        start = wf.index('          set -e\n          code=')
        end = wf.index('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
        end += len('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
    except ValueError:
        return check("the canary's exit-code script could be located", False)
    script = "\n".join(line[10:] if line.startswith(" " * 10) else line
                        for line in wf[start:end].splitlines())

    # WHY EACH CODE LANDS WHERE IT DOES, and this repo's table differs from
    # its siblings' because the SITE differs. Every other canary in this
    # family treats "blocked" as an access condition to be warned about,
    # because its site refuses datacentre addresses as a matter of course.
    # Spinny has never been observed refusing anything — a home connection,
    # an Amsterdam datacentre exit, a Chennai residential exit and the
    # Scraping Browser API all got the full grid — so a blocked run HERE is
    # new information and is failed rather than excused.
    #
    #   0  got in and parsed         -> pass, and the assertions then run
    #   5  endpoint refused          -> ACCESS, and only possible when the
    #      OPTIONAL Scraping Browser secret is set. `profile_locked` and an
    #      expired credential both say nothing about the site, and this
    #      canary does not need the secret at all.
    #   3  blocked before parsing    -> DEFECT, or news. See above.
    #   6  partial                   -> DEFECT: the target is 60 cars and the
    #      measured run passes it in two scroll rounds, so a partial means
    #      the lazy-load loop stopped making progress.
    #   1  crashed                   -> DEFECT.
    #   2  bad arguments             -> DEFECT (in the workflow itself).
    #   4  served a page, ZERO rows  -> DEFECT, and precisely the regression
    #      this canary exists to catch: the card anchor moved.
    expected = {0: "pass", 5: "warn",
                1: "fail", 2: "fail", 3: "fail", 4: "fail", 6: "fail",
                99: "fail"}
    for code, want in sorted(expected.items()):
        body = script.replace('code="${{ steps.run.outputs.exit_code }}"',
                              'code="%d"' % code)
        with tempfile.TemporaryDirectory() as td:
            out_file = os.path.join(td, "gh_output")
            summary = os.path.join(td, "gh_summary")
            open(out_file, "w").close()
            open(summary, "w").close()
            done = subprocess.run(
                ["bash", "-c", body], capture_output=True, text=True,
                env=dict(os.environ, GITHUB_OUTPUT=out_file,
                         GITHUB_STEP_SUMMARY=summary))
            failed = done.returncode != 0
            warned = "::warning::" in done.stdout
            errored = "::error::" in done.stdout
            wrote_summary = bool(open(summary, encoding="utf-8").read().strip())
            tested = "tested=true" in open(out_file, encoding="utf-8").read()

        if want == "pass":
            got = not failed and not warned and not errored and tested
        elif want == "warn":
            # A warning must NOT read as a pass: it also has to say in the
            # step summary that nothing was actually tested, and it must not
            # claim `tested`.
            got = not failed and warned and wrote_summary and not tested
        else:
            got = failed and errored
        ok &= check("exit %-2d is treated as %s" % (code, want), got)

    ok &= check("the reason access is not a defect is written down",
                "ACCESS CONDITIONS ARE NOT DEFECTS" in wf)

    # THIS CANARY IS SCHEDULED, and that too differs from its siblings for a
    # measured reason: it needs no credential, so there is none to expire.
    # A sibling repo's canary is dispatch-only because a daily cron against a
    # credential that does not survive a day gives either a permanently red
    # badge or a permanently green one that tested nothing. Here the run
    # works from the runner's own address, so the schedule is honest.
    ok &= check("the canary runs on a schedule",
                re.search(r"^\s*-\s*cron:", wf, re.M) is not None)
    ok &= check("it is also dispatchable by hand", "workflow_dispatch:" in wf)
    ok &= check("and the reason it can run unattended is written down",
                "RUNS FROM A BARE RUNNER" in wf)
    # The credential is OPTIONAL and must stay optional: no step may be gated
    # on it, or the canary quietly stops running the day it expires.
    ok &= check("no step is gated on the optional secret",
                "HAVE_CDP" not in wf)
    ok &= check("...and the secret still reaches the run through the "
                "environment, never argv",
                "SPINNY_CDP_ENDPOINT: ${{ secrets.SPINNY_CDP_ENDPOINT }}" in wf
                and "--cdp-endpoint" not in wf.split("run:")[-3])
    return ok


def test_ci_checks_is_actually_wired_up():
    group("the repo's own checks are RUN, and still catch a real secret")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    if not os.path.exists(script):
        return ok

    # IT HAS TO BE INVOKED BY A WORKFLOW. It was not — for the whole of
    # v0.1.0 it sat there implementing three checks that nothing ran, while a
    # second, LOOSER copy of one of them lived inline in tests.yml. Dead code
    # that looks load-bearing is worse than no code, and this is the check
    # that keeps it alive.
    wf_dir = os.path.join(REPO_ROOT, ".github", "workflows")
    workflows = "\n".join(
        open(os.path.join(wf_dir, f), encoding="utf-8").read()
        for f in sorted(os.listdir(wf_dir)) if f.endswith((".yml", ".yaml")))
    ok &= check("a workflow runs ci_checks.py", "ci_checks.py" in workflows)
    ok &= check("the secret check specifically is run",
                "--secret-check" in workflows or "--all" in workflows)

    # AND IT PASSES ON THIS REPO. A check that is always red teaches everyone
    # to ignore checks; this one WAS red, on six documented placeholders.
    done = subprocess.run([sys.executable, script, "--all"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("ci_checks.py --all passes on this repo (exit %d)" % done.returncode,
                done.returncode == 0)
    if done.returncode != 0:
        print("        " + (done.stdout or done.stderr).strip()[-400:])

    # AND IT STILL CATCHES A REAL ONE. Loosening an allowlist until the check
    # passes is the failure mode here, so both directions are asserted: a
    # planted CDP endpoint, a planted 32-hex key and a planted http proxy URL
    # must all be found. The http one matters most — the inline grep this
    # replaced covered only ws:// and would have missed a committed proxy.
    planted = os.path.join(REPO_ROOT, "_secret_probe_delete_me.py")
    # The key is ASSEMBLED rather than written as a literal, because a
    # 32-character hex string sitting in this file is exactly what the check
    # under test flags — and it did, on the first run of this test. The file
    # it writes still gets the whole thing, which is what the probe needs.
    planted_key = "3f8a1c9e4b7d2065" + "af13ce88b409d752"
    try:
        with open(planted, "w", encoding="utf-8") as f:
            f.write(
                'CDP = "ws://acct-zone-scraping_browser-pid-x:'
                'S3cretPassw0rd@cb.2captcha.com:9222"\n'
                'KEY = "%s"\n'
                'PROXY = "http://acct-zone-custom:S3cretPassw0rd'
                '@na.proxy.2captcha.com:2334"\n' % planted_key)
        caught = subprocess.run([sys.executable, script, "--secret-check"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        out = caught.stdout + caught.stderr
        ok &= check("a planted secret fails the check", caught.returncode != 0)
        ok &= check("the planted ws:// CDP endpoint is named",
                    "_secret_probe_delete_me.py:1" in out)
        ok &= check("the planted 32-hex key is named",
                    "_secret_probe_delete_me.py:2" in out)
        ok &= check("the planted http:// PROXY url is named (the grep this "
                    "replaced missed those)",
                    "_secret_probe_delete_me.py:3" in out)
    finally:
        # Never leave it behind: a test that mutates the working tree is its
        # own defect, and this one would plant a fake secret.
        if os.path.exists(planted):
            os.remove(planted)
    ok &= check("the probe file is cleaned up", not os.path.exists(planted))

    # The pre-publication scan: the same rules over every blob that has EVER
    # existed. A later commit cannot remove what a published tag and a merged
    # PR's refs already hold, so this has to be runnable BEFORE the repo goes
    # public — and it has to be findable, which a check makes it.
    hist = subprocess.run([sys.executable, script, "--history-check"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--history-check runs and this history is clean",
                hist.returncode == 0)
    ok &= check("it says how many objects it looked at",
                "ever existed" in hist.stdout)
    # NOT in --all, on purpose: it shells out to git once per object, and a
    # dirty history needs a decision rather than a red check on every push.
    every = subprocess.run([sys.executable, script, "--all"],
                           cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--all deliberately excludes the history scan",
                "history check" not in every.stdout)
    return ok


def test_no_capture_leaks():
    group("no credentials or personal data in the committed fixtures")
    ok = True
    # Collected by CONTENT, not by a naming convention. An earlier version in
    # this family asked for a "FIX_" PREFIX, matched nothing, and every check
    # below passed against an empty string — 150 KB of committed real
    # captures went unexamined while twelve checks reported green. A suffix
    # convention has the same failure mode one rename later, so what is
    # collected here is "every module-level string that looks like captured
    # markup", and the non-empty assertion underneath is what makes the
    # collector itself testable: a corpus check that can silently scan
    # nothing is worse than no corpus check at all.
    names = [k for k, v in sorted(globals().items())
             if isinstance(v, str) and len(v) > 1000
             and ("<div" in v or "<html" in v)]
    fixtures = "\n".join(globals()[k] for k in names)
    ok &= check("the privacy checks below have fixtures to scan "
                "(%d fixtures, %d chars)" % (len(names), len(fixtures)),
                len(names) >= 3 and len(fixtures) > 30000)
    # Guarded with PATTERNS rather than with the literals a previous capture
    # happened to contain, so the NEXT capture is checked too. MediaMarkt's
    # pages embed a front-end configuration blob — a Sentry DSN, a Woosmap
    # public key, a store-code JWT — none of which is needed to test a
    # parser, and none of which belongs in a public repository.
    patterns = {
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an access token": r"(?:access|auth|bearer)[_\-]?[Tt]oken\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "an API key": r"(?:api|public|secret|private)[_\-]?[Kk]ey\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "a Sentry DSN": r"https://[0-9a-f]{16,}@[\w.]*ingest",
        "a session id": r"session[_\-]?[Ii]d\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}",
        "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
    }
    for label, pattern in patterns.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures" % label, not hits)

    # The SITE's OWN per-impression material, which a fresh capture brings with
    # it: a click-tracking key, its checksum, and the logging key that ties an
    # impression to a session. Anonymous and expired, and still not something
    # to commit — and a 40-character hex-ish blob in a public repo reads as a
    # credential to every scanner that looks, including this repo's own CI
    # grep. Matched as PATTERNS rather than as the values one capture
    # happened to hold, so the NEXT capture is checked too.
    site_session = {
        "a click-tracking key": r"click_key=(?!PLACEHOLDER)[A-Za-z0-9%.-]{12,}",
        "a click checksum": r"click_sum=(?!PLACEHOLDER)[A-Za-z0-9]{6,}",
        "an impression logging key":
            r'data-logging-key="(?!PLACEHOLDER)[A-Za-z0-9:-]{12,}"',
        "a content-source token":
            r"content_source=(?!PLACEHOLDER)[A-Za-z0-9%.-]{12,}",
        "a DataDome session blob": r"'(?:cid|hsh|e|cookie)':'(?!PLACEHOLDER)[^']{16,}'",
    }
    for label, pattern in site_session.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures (scrub a new capture before "
                    "committing it)" % label, not hits)

    # The repo-wide grep CI runs, applied here too so a failure is local.
    # Asked of GIT, not of the filesystem. A developer's own `.env` beside
    # the scripts is EXPECTED — it is how the local runs get their key — and
    # `.gitignore` is what keeps it out of the repo. Checking for the file's
    # existence made this red on every machine that had ever run the scraper
    # for real, which is the machine most likely to be running the suite.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT, capture_output=True, text=True).returncode == 0
    ok &= check("no .env file is tracked by git", not tracked)
    return ok


def test_wording():
    group("wording and removed flags")
    ok = True
    # Asked of GIT, so the scan reaches the workflows and the issue
    # templates under .github/ — eight shipped files that an os.listdir of
    # the repo ROOT silently missed, including the four a contributor is
    # most likely to paste marketing wording into. Untracked scratch files
    # and .pytest_cache/ are excluded for free by asking git.
    # Asked of the FILESYSTEM, walked, rather than of `git ls-files`. Using
    # git meant the scan silently shrank to a handful of root files whenever
    # the shipped files were not yet committed — which is exactly when a new
    # repo is being written and a phrase is most likely to be pasted in. The
    # noisy directories are excluded by name; a scratch file that survives
    # that is scanned too, which is the harmless direction to be wrong in.
    _SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache",
                  "live_results", "node_modules", ".fingerprint_cache"}
    shipped = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if not f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml")):
                continue
            if f == os.path.basename(__file__):
                continue
            shipped.append(os.path.relpath(os.path.join(root, f), REPO_ROOT))
    ok &= check("the wording scan reaches beyond the repo root",
                any(os.sep in f or "/" in f for f in shipped))
    for phrase in BANNED_PHRASES:
        offenders = []
        for f in shipped:
            try:
                text = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if phrase.lower() in text.lower():
                offenders.append(f)
        ok &= check("no shipped file says %r" % phrase, not offenders)

    for flag in REMOVED_ENGINE_FLAGS:
        offenders = []
        for f in ENGINE_FILES:
            path = os.path.join(REPO_ROOT, f)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            # A prose mention explaining why the flag does NOT exist is fine
            # and is worth keeping; an argparse registration is not.
            if ('add_argument("%s"' % flag) in text or \
                    ("add_argument('%s'" % flag) in text:
                offenders.append(f)
        ok &= check("no engine registers the removed flag %s" % flag,
                    not offenders)

    # The product this repo integrates with, named correctly.
    readme = os.path.join(REPO_ROOT, "README.md")
    if os.path.exists(readme):
        text = open(readme, encoding="utf-8").read()
        ok &= check("the README names the Scraping Browser API",
                    "Scraping Browser API" in text)
        ok &= check("the README does not name a competitor",
                    not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com",
                                  text, re.IGNORECASE))
    return ok


def test_fingerprint_client_reads_env():
    group("fingerprint_client resolves its key the way the docs promise")
    ok = True
    import fingerprint_client as fpc

    # THE DEFECT THIS PINS, found the first time --fingerprint was run live
    # here and confirmed present in five sibling repos: `--key` defaulted to
    # `os.environ.get("TWOCAPTCHA_KEY")` alone. So a key put in `.env` —
    # which is exactly what §3, the README and .env.example instruct — worked
    # for every engine and failed HERE with "No API key". A documented
    # mechanism not applied on one path, which is the shape of half the
    # defects §16 lists.
    src = inspect.getsource(fpc.main)
    ok &= check("it loads .env itself, rather than hoping an engine did",
                "env_config.load_env()" in src)
    ok &= check("...and reads the key through the family's loader",
                'env_config.env_value("TWOCAPTCHA_KEY")' in src)
    # Through `env_value` and NOT `os.environ.get`, because only the former
    # applies the placeholder rule. Measured both ways with
    # TWOCAPTCHA_KEY=your_2captcha_api_key_here exported: os.environ.get
    # sends the placeholder to the API and the run reports "Fingerprint API
    # rejected the key (401) — note this is a separate subscription", which
    # sends the reader to check a subscription they never needed.
    ok &= check("...not straight from os.environ, which skips the "
                "placeholder rule",
                'os.environ.get("TWOCAPTCHA_KEY")' not in src)
    # Behaviourally, not just by reading the source — and written
    # self-contained so this check is byte-identical in every repo of the
    # family rather than depending on a local helper.
    saved = os.environ.get("TWOCAPTCHA_KEY")
    try:
        os.environ["TWOCAPTCHA_KEY"] = "your_2captcha_api_key_here"
        read_back = env_config.env_value("TWOCAPTCHA_KEY")
    finally:
        if saved is None:
            os.environ.pop("TWOCAPTCHA_KEY", None)
        else:
            os.environ["TWOCAPTCHA_KEY"] = saved
    ok &= check("a placeholder still reads as unset on this path",
                read_back is None)

    # The default must never reach `--help`. argparse prints a default only
    # when the help string asks for it, so this is one substring away from
    # printing a live credential to anyone who types --help.
    ok &= check("the --key help text does not interpolate its default",
                "%(default)s" not in src)
    return ok


def test_fingerprint_application():
    group("a fingerprint is applied as the fingerprint describes it")
    ok = True
    import fingerprint_client as fpc

    ua = fpc.fingerprint_user_agent(FIX_FINGERPRINT)
    # The UA used to be read from `userAgent.value`, a key the API returns in
    # NEITHER format. So --fingerprint silently set no user agent at all and
    # the browser kept its own: a German fingerprint's screen and locale
    # wearing a local Chromium's UA, which is precisely the identity mismatch
    # the flag exists to avoid.
    ok &= check("the user agent is found in the shape the API returns",
                ua and ua.startswith("Mozilla/5.0 (Windows NT 10.0"))
    ok &= check("the `raw` format's ua key is understood too",
                fpc.fingerprint_user_agent({"data": {"ua": "UA/1.0"}}) == "UA/1.0")
    ok &= check("a fingerprint with no user agent yields None, not a crash",
                fpc.fingerprint_user_agent({"country": "DE"}) is None)

    kw = fpc.playwright_context_kwargs(FIX_FINGERPRINT)
    ok &= check("the context carries the fingerprint's user agent",
                kw.get("user_agent") == ua)
    # `locale` used to be built as f"en-{country}", giving "en-ID" for an
    # Indonesian fingerprint. An English-speaking visitor in Indonesia is
    # possible, but it is not what this fingerprint describes, and a locale
    # that contradicts the rest of the identity is the mismatch again. This
    # was one of the six defects a sibling repo inherited from copied core
    # and never ran (§16).
    ok &= check("the locale is the fingerprint's own, not en-<country>",
                kw.get("locale") == "en-IN")
    ok &= check("the timezone is carried, so the browser cannot contradict it",
                kw.get("timezone_id") == "Asia/Kolkata")
    # The device pixel ratio, which Playwright takes as its own option and
    # which was dropped on the floor until a live browser was compared
    # against the fingerprint: one stating 1.25 produced a browser reporting
    # `devicePixelRatio === 1`, so the identity contradicted itself on an
    # axis a fingerprinter reads for free.
    ok &= check("fingerprint: the device scale factor is carried",
                kw.get("device_scale_factor") == 1)
    # A viewport exactly equal to the screen is itself a signal, and the
    # fingerprint states its own window size rather than needing one guessed.
    ok &= check("the viewport is the fingerprint's window, not its screen",
                kw.get("viewport") == {"width": 1920, "height": 992}
                and kw.get("screen") == {"width": 1920, "height": 1080})

    # Falling back sensibly when a field is absent, rather than dropping it.
    bare = fpc.playwright_context_kwargs({"country": "FR", "screen":
                                          {"width": 1280, "height": 800}})
    ok &= check("a fingerprint with no intl block still gets a locale",
                bare.get("locale") == "en-FR")
    ok &= check("...and a window smaller than the screen",
                bare["viewport"]["height"] < bare["screen"]["height"])
    ok &= check("a fingerprint with nothing usable yields no kwargs",
                fpc.playwright_context_kwargs({}) == {})

    # Every key this produces must be one Playwright's new_context accepts;
    # an unknown one is a TypeError at launch, on the paid path, at runtime.
    accepted = {"user_agent", "viewport", "screen", "locale", "timezone_id",
                "geolocation", "permissions", "extra_http_headers",
                "device_scale_factor", "is_mobile", "has_touch", "color_scheme"}
    ok &= check("every context kwarg is one Playwright accepts",
                set(kw) <= accepted)
    return ok


def test_credentials_never_reach_a_log():
    group("an API key never reaches a log or an exception message")
    ok = True
    import fingerprint_client as fpc
    import captcha_solver as cs

    # requests puts the FULL URL — query string included — into the text of
    # HTTPError and of every connection error. Both of these modules have an
    # endpoint that takes the key as a query parameter, so an error there
    # echoed a live key to the terminal. It did, once, on a real call.
    # An obviously fake key, and NOT a real one even a revoked one: a
    # 32-hex string in a public repo reads as a live credential to every
    # scanner that looks, including this repo's own CI grep. The word
    # "example" in the name is what tells that grep this line is a fixture.
    example_key = "0123456789abcdef0123456789abcdef"
    for name, module in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        redacted = module._redact(
            "400 Client Error: Bad Request for url: "
            "https://api.2captcha.com/fingerprint/random?format=chromium&"
            "key=%s" % example_key)
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in redacted)
        ok &= check("...and keeps the endpoint, which is the useful half",
                    "api.2captcha.com/fingerprint/random" in redacted)
        ok &= check("%s redacts clientKey too" % name,
                    example_key not in module._redact("clientKey=%s" % example_key))
        ok &= check("%s leaves ordinary text alone" % name,
                    module._redact("upstream status 403") == "upstream status 403")
    return ok


def test_single_fetch_run(skips):
    group("a run is ONE fetch, and the scroll decides what it reports")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError as e:
        skips.append("single-fetch run (%s)" % e)
        return ok

    # The rest of this family tests its THREAD FAN-OUT here, because a live
    # run cannot always reach it. This site has no fan-out to test — page 5
    # of an infinitely scrolling grid has no address to hand a worker — so
    # what is driven with the browser stubbed out instead is the thing that
    # replaced it: `scrape()` must fetch exactly ONE page, whatever --pages
    # and --concurrency say, and must turn the SCROLL's outcome into the run
    # status. That mapping is what a consumer branches on, and it is the part
    # a live run exercises only one branch of at a time.
    original = (eng.sync_playwright, eng._BrowserSession, eng._fetch_one_page,
                eng._advertised_next_hrefs)

    class _FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeSession:
        def __init__(self, pool=None):
            self.pool = pool
            self.page = None
            self.closed = False

        def open(self):
            return self

        def close(self):
            self.closed = True

    class Args:
        url = LISTING_URL
        mode = "listing"
        pages = 5
        concurrency = 1
        format = "json"
        allow_empty = False
        twocaptcha_key = None
        solve_captcha = "when-blocked"
        cdp_endpoint = None
        proxy = None
        proxy_file = None
        proxy_rotate = "per-run"
        proxy_shuffle = False
        category = None
        delay = 0

    def run(scroll, rows=3, mode="listing", concurrency=1, ok_page=True,
            advertised=3):
        fetched = []
        sessions = []

        def fake_fetch(session, args, pool, page_num, url):
            fetched.append((page_num, url))
            outcome = eng.PageOutcome(page_num=page_num, url=url)
            outcome.final_url = url
            outcome.state = "content"
            outcome.scroll = scroll
            outcome.completeness = {"total_available": advertised,
                                    "rows_collected": rows,
                                    "short_by": max(0, advertised - rows)}
            outcome.header = "%d Used cars in Delhi NCR" % advertised
            if not ok_page:
                outcome.blocked_by = "not-served"
                return outcome
            outcome.products = [
                Product(sku=str(i), url="u%d" % i, price=100.0 + i)
                for i in range(rows)]
            return outcome

        def fake_session(pw, args, pool, **kw):
            s = _FakeSession(pool)
            sessions.append(s)
            return s

        eng.sync_playwright = lambda: _FakePlaywright()
        eng._BrowserSession = fake_session
        eng._fetch_one_page = fake_fetch
        eng._advertised_next_hrefs = lambda page, page_num=1: []
        args = Args()
        args.mode = mode
        args.concurrency = concurrency
        with tempfile.TemporaryDirectory() as d:
            args.out = os.path.join(d, "out")
            try:
                with io.StringIO() as buf, redirect_stdout(buf):
                    code = eng.scrape(args)
                meta_path = args.out + ".meta.json"
                meta = (json.load(open(meta_path, encoding="utf-8"))
                        if os.path.exists(meta_path) else None)
            finally:
                (eng.sync_playwright, eng._BrowserSession,
                 eng._fetch_one_page,
                 eng._advertised_next_hrefs) = original
        return fetched, code, meta, sessions

    # 1. ONE fetch, however many --pages were asked for. This is the whole
    #    design: a planner that built ?page=N would fetch page one five times
    #    and report a complete run holding a twentieth of the catalogue.
    settled = {"settled": True, "reached_target": False, "rounds": 11,
               "cards": 302, "height": 32008, "boundaries": [22, 302]}
    fetched, code, meta, sessions = run(settled)
    ok &= check("--pages 5 still fetches exactly one URL", len(fetched) == 1)
    ok &= check("...and it is the URL that was asked for",
                fetched[0] == (1, LISTING_URL))
    ok &= check("the browser session is always closed",
                all(s.closed for s in sessions))

    # 2. THE STATUS MAPPING. A grid that stopped growing WITH THE SITE'S OWN
    #    COUNT AGREEING is the listing running out — the DATA-based
    #    terminating condition — and that is a COMPLETE run. The run stubbed
    #    here advertises 1559 and holds 1559.
    ok &= check("a settled scroll reports no_new_products",
                meta and meta["stop_reason"] == "no_new_products")
    ok &= check("...and the run is complete", meta and meta["status"] == "complete")
    ok &= check("...and exits 0", code == 0)
    # pages_completed counts BATCHES delivered, not URLs fetched.
    ok &= check("pages_completed counts the batches the run holds",
                meta and meta["pages_completed"] == 1)
    ok &= check("the sidecar carries the arithmetic completeness check",
                meta and meta["completeness"]["short_by"] == 0)
    ok &= check("...and the listing's own heading",
                meta and meta["listing_heading"].endswith("Used cars in Delhi NCR"))

    # THE STALL, which a live run through a rotating residential gateway
    # produced: the grid stops growing because the hydration XHR never lands,
    # and without this the run reports COMPLETE while holding 22 of 1546.
    fetched, code, meta, _ = run(settled, rows=3, advertised=1546)
    ok &= check("a settled scroll far below the advertised total is a stall",
                meta and meta["stop_reason"] == "scroll_stalled")
    ok &= check("...and the run is PARTIAL, not complete",
                meta and meta["status"] == "partial")
    ok &= check("...and exits 6", code == EXIT_PARTIAL)
    ok &= check("...with the contradiction visible in the sidecar",
                meta and meta["completeness"]["short_by"] == 1543)

    # 3. Reaching the target the caller asked for is ALSO complete: the
    #    request was satisfied, and `short_by` is what says there is more.
    target = {"settled": False, "reached_target": True, "rounds": 6,
              "cards": 122, "height": 17434, "boundaries": [22, 122]}
    fetched, code, meta, _ = run(target, advertised=1546)
    ok &= check("reaching --pages worth of cars reports completed",
                meta and meta["stop_reason"] == "completed")
    ok &= check("...and is a complete run", meta and meta["status"] == "complete")

    # 4. A spent round budget with the page still growing is PARTIAL, and has
    #    to say so — the missing tail would otherwise read as delisted cars
    #    in the next diff.
    growing = {"settled": False, "reached_target": False, "rounds": 15,
               "cards": 182, "height": 21328, "boundaries": [22, 182]}
    fetched, code, meta, _ = run(growing)
    ok &= check("a spent scroll budget reports scroll_budget_exhausted",
                meta and meta["stop_reason"] == "scroll_budget_exhausted")
    ok &= check("...and the run is PARTIAL, not complete",
                meta and meta["status"] == "partial")
    ok &= check("...and exits 6", code == EXIT_PARTIAL)
    ok &= check("scroll_budget_exhausted is not a complete stop reason",
                "scroll_budget_exhausted" not in COMPLETE_STOP_REASONS)

    # 5. --mode detail is complete by construction: there is no page 2.
    fetched, code, meta, _ = run(None, rows=1, mode="detail")
    ok &= check("a detail run reports single_page_mode",
                meta and meta["stop_reason"] == "single_page_mode")
    ok &= check("...with one page completed",
                meta and meta["pages_completed"] == 1)

    # 6. A blocked page writes NO output and NO sidecar — `save` leaves the
    #    previous good run in place, and a "failed" sidecar beside good data
    #    would contradict it.
    fetched, code, meta, _ = run(settled, ok_page=False)
    ok &= check("a blocked page exits 3", code == EXIT_BLOCKED)
    ok &= check("...and writes no sidecar next to the last good output",
                meta is None)

    # 7. --concurrency is accepted and refused, and the run is unchanged.
    fetched, code, meta, _ = run(settled, concurrency=8)
    ok &= check("--concurrency 8 still fetches exactly one URL",
                len(fetched) == 1)
    ok &= check("...and the run is otherwise identical",
                meta and meta["stop_reason"] == "no_new_products")
    return ok


def test_no_undefined_names():
    group("no engine references a name that does not exist")
    ok = True
    # This exists because of a bug that got all the way to a live run.
    # puppeteer_scraper.py called `detect_page_state(...)` on a line reached
    # only while fetching a page, after the import of that name had been
    # removed. The module imported fine, `--help` worked, `compileall`
    # passed, the whole offline suite passed and CI was green — and the
    # engine died with NameError on its first real page.
    #
    # Byte-compiling proves a file PARSES. It says nothing about whether the
    # names in it resolve, and the paths where they do not are exactly the
    # ones an offline suite cannot execute.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        missing = _undefined_names(os.path.join(REPO_ROOT, name))
        detail = ", ".join("%s (line %d)" % (k, v[0])
                           for k, v in sorted(missing.items()))
        ok &= check("%s references no undefined name%s"
                    % (name, ": " + detail if missing else ""), not missing)
    return ok


def test_no_unused_imports():
    group("no module imports a name it does not use")
    ok = True
    # The small end of the same family of defects as the undefined-name walk
    # below: a name imported and never used is usually the FOSSIL of a
    # removed feature, and it is how a reader concludes a module still does
    # something it no longer does. Six of them were left behind here by the
    # port from a sibling repo — a whole `urllib.parse` line in
    # captcha_solver.py, and a marker set scraper_api_client.py had stopped
    # consulting.
    #
    # Deliberately coarse: a name is "used" if it appears anywhere else in
    # the file, so this under-reports rather than inventing problems.
    for name in sorted(f for f in os.listdir(REPO_ROOT)
                       if f.endswith(".py")
                       and f != os.path.basename(__file__)):
        tree = ast.parse(open(os.path.join(REPO_ROOT, name),
                              encoding="utf-8").read())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [(a.asname or a.name).split(".")[0]
                             for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # `from __future__ import annotations` is a compiler
                # directive rather than a name anything refers to.
                if node.module == "__future__":
                    continue
                imported += [a.asname or a.name for a in node.names]
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.value.id for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute)
                 and isinstance(n.value, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        unused = sorted(set(i for i in imported if i not in used))
        ok &= check("%s imports nothing it does not use" % name, not unused)
        if unused:
            print("        unused: %s" % unused)
    return ok


def test_dockerfile_copies_what_it_runs():
    group("the Docker image contains every module its entrypoint imports")
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("Dockerfile exists", False)

    # The Dockerfile COPYs an explicit list rather than the whole directory,
    # which is right — the image should not carry the test suite, the
    # fixtures or a stray .env. The cost is that the list can fall behind the
    # imports, and NOTHING else in this repo would notice: CI never builds
    # the image, so a missing module ships and the container dies with
    # ModuleNotFoundError on every invocation, `--help` included.
    #
    # That is not hypothetical. `proxy_pool.py` was missing from this list,
    # and playwright_scraper.py imports it at module level.
    raw = open(path, encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)          # fold line continuations
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(tok for tok in line.split() if tok.endswith(".py"))

    entrypoint = None
    m = re.search(r'ENTRYPOINT\s*\[([^\]]*)\]', joined)
    if m:
        parts = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        entrypoint = next((x for x in parts if x.endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entrypoint))
    if not entrypoint:
        return False
    ok &= check("the entrypoint itself is copied into the image",
                entrypoint in copied)

    # Every LOCAL module the entrypoint reaches, transitively.
    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(module, seen=None):
        seen = seen if seen is not None else set()
        if module in seen:
            return seen
        seen.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, module + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local:
                    reached(name, seen)
        return seen

    needed = reached(entrypoint[:-3])
    missing = sorted(m + ".py" for m in needed if (m + ".py") not in copied)
    ok &= check("every module the entrypoint imports is COPYed (%s)"
                % (", ".join(missing) if missing else "none missing"),
                not missing)

    # The other direction is a warning, not a failure: diff_runs.py is copied
    # deliberately as a companion tool even though the engine never imports
    # it. But anything copied must at least still EXIST.
    gone = sorted(f for f in copied
                  if not os.path.exists(os.path.join(REPO_ROOT, f)))
    ok &= check("the Dockerfile copies no file that has been deleted (%s)"
                % (", ".join(gone) if gone else "none"), not gone)
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    path = os.path.join(REPO_ROOT, "sample_output.json")
    if not os.path.exists(path):
        return check("sample_output.json exists", False)
    rows = json.load(open(path, encoding="utf-8"))
    ok &= check("the sample has rows", len(rows) > 0)
    names = [f.name for f in fields(Product)]
    ok &= check("its columns match the Product schema exactly",
                all(set(r) == set(names) for r in rows))
    text = json.dumps(rows, ensure_ascii=False)
    ok &= check("the sample carries no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|FIXME|TODO|XXXX",
                              text, re.IGNORECASE))
    # `sku` is the car's numeric id, the last segment of its own URL.
    ok &= check("every sample row's sku is the car's numeric id",
                all(re.fullmatch(r"\d+", r.get("sku") or "") for r in rows))
    ok &= check("...and is the last segment of its own URL",
                all((r.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
                    == r.get("sku") for r in rows))
    ok &= check("every sample row names the storefront it came from",
                all((r.get("source") or "") in HOSTS for r in rows))
    ok &= check("every sample row's URL is on the storefront",
                all((r.get("url") or "").startswith(
                    "https://www.spinny.com/buy-used-cars/") for r in rows))
    ok &= check("no sample URL carries a tracking tail",
                not [r for r in rows if "utm_" in (r.get("url") or "")])
    # A sample cut from ONE page kind would hide half the schema: a listing
    # card prints no odometer to the unit, no owner count and no insurance
    # details, and only a detail page states them. So the sample has to span
    # both modes, or a reader judges the output by its sparsest or its
    # fullest rows alone.
    ok &= check("the sample spans both modes",
                any(r.get("km_driven_exact") for r in rows)
                and any(r.get("page") for r in rows))
    ok &= check("the sample shows a discounted row and an undiscounted one",
                any(r.get("original_price") for r in rows)
                and any(r.get("original_price") is None for r in rows))
    ok &= check("the sample shows a card with a hub and one without",
                any(r.get("hub") for r in rows)
                and any(r.get("hub") is None for r in rows))
    ok &= check("the sample shows more than one assurance tier",
                len({r.get("assurance") for r in rows if r.get("assurance")}) > 1)
    # The sample is what a reader judges the output by, so it has to show the
    # provenance column doing its job rather than a column of nulls.
    ok &= check("the sample shows a real price_source",
                all(r.get("price_source") in ("dom+attr", "dom", "dom+jsonld")
                    for r in rows))
    # The two invariants a canary asserts, asserted on the committed sample
    # too — a sample that violated them would be teaching the wrong shape.
    ok &= check("no sample row has an original_price at or below its price",
                all(r.get("original_price") is None
                    or r["original_price"] > (r.get("price") or 0)
                    for r in rows))
    ok &= check("no sample row's all-in price is below its displayed price",
                all(r.get("price_all_in") is None or r.get("price") is None
                    or r["price_all_in"] >= r["price"] for r in rows))
    # `page` and `position` together have to IDENTIFY a listing row. A run
    # that scrolled one long page reports batches through `page`, and a
    # sibling repo that failed to thread the page number in shipped 60 of
    # 119 rows claiming a position another row already had.
    listing_rows = [r for r in rows if r.get("position")]
    seen_pos = [(r.get("page"), r.get("position")) for r in listing_rows]
    ok &= check("page+position is unique across the sample's listing rows",
                len(set(seen_pos)) == len(seen_pos))

    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = open(csv_path, encoding="utf-8").read().split("\n")[0]
        ok &= check("the sample CSV header matches the schema",
                    header.strip().split(",") == names)
    return ok


def test_captcha():
    group("captcha detection and reconciliation")
    ok = True
    from captcha_solver import CaptchaChallenge

    # Format 2: the site's own wrapper element carries the config as
    # attributes, with the execute() call inside a bundled file that never
    # appears as readable inline script.
    widget = ('<captcha-widget data-captcha-type="recaptcha" data-version="v3" '
              'data-sitekey="6LcABCDEFGHIJKLMNOPQRSTUVWXYZ0123" '
              'data-action="submit"></captcha-widget>')
    c = detect_recaptcha_v3(widget, "https://www.spinny.com/")
    ok &= check("a captcha-widget declaring v3 is detected",
                c is not None and c.kind == "recaptcha_v3")

    # A sitekey is at least 20 characters; a short string next to
    # data-sitekey is not one, and treating it as one would send a malformed
    # task to the API and bill for the answer.
    ok &= check("a too-short sitekey is not accepted as a challenge",
                detect_recaptcha_v3('<div data-sitekey="short" '
                                    'class="g-recaptcha"></div>',
                                    "https://www.spinny.com/") is None)
    ok &= check("a page with no reCAPTCHA at all is not a challenge",
                detect_recaptcha_v3(grid(CARD_EON), LISTING_URL) is None)

    # THE LOADER WINS. A site's own wrapper can declare v3 while the Google
    # loader it actually ships is the v2-invisible signature
    # (render=explicit, size=invisible, a bframe challenge iframe). v3
    # parameters sent for a v2-invisible widget buy a token the site
    # rejects — so the runtime reading is authoritative and the two
    # detectors are reconciled rather than short-circuited.
    static_v3 = CaptchaChallenge(kind="recaptcha_v3", sitekey="6LcABC" + "X" * 20,
                                 action="submit", source="html")
    runtime_v2 = CaptchaChallenge(kind="recaptcha_v2_invisible",
                                  sitekey="6LcABC" + "X" * 20,
                                  source="runtime", size="invisible")
    merged = reconcile_detections(static_v3, runtime_v2)
    ok &= check("when the detectors disagree, the live loader wins",
                merged is not None and merged.kind == "recaptcha_v2_invisible")
    ok &= check("...and the real action from the static markup is kept",
                merged.action == "submit")
    ok &= check("one detector alone is still used when only it fires",
                reconcile_detections(static_v3, None) is static_v3
                and reconcile_detections(None, runtime_v2) is runtime_v2)
    ok &= check("neither firing means no challenge",
                reconcile_detections(None, None) is None)

    # Deliberately absent: no solver for a first-party image captcha. This
    # site has no such page — measured 2026-09-10, its refusal is not a page
    # at all: the HTTP/2 stream is reset and nothing arrives — so a solver
    # for one would be dead code that looks load-bearing. Pinned so that
    # reintroducing it is a decision rather than a drift.
    import captcha_solver
    ok &= check("no first-party image-captcha solver was ported",
                not [n for n in dir(captcha_solver)
                     if "image" in n.lower() and "captcha" in n.lower()])
    # ...while the DETECTORS stay broad, which is the family's standing
    # policy: which challenge a visitor meets depends on the exit country and
    # on what the address has been doing.
    # The marker set is deliberately SHORT on this site, and shorter than the
    # sibling repos'. No refusal of any kind has ever been observed on
    # Spinny, so every entry is a guess about a bot manager that might be
    # switched on between deploys, and a long list of guesses is not better
    # than a short one.
    #
    # Two exclusions are pinned in BOTH directions, because both would fire
    # on healthy pages:
    #
    #   `recaptcha` / `g-recaptcha` — inherited from the sibling repos and
    #   REMOVED here. Spinny loads reCAPTCHA Enterprise v3 on every page it
    #   serves, so as markers they would make every good page blocked. §18's
    #   rule, and the check that rule asks for: count it on a page you know
    #   is good before adding it.
    #
    #   `cf-turnstile` — the Scraping Browser's own auto-solve extension
    #   injects a cf-turnstile hunter into every page it loads, so it would
    #   fire on good pages fetched over --cdp-endpoint.
    markers = {m.lower() for m in product_parser.BOT_CHALLENGE_MARKERS}
    ok &= check("detection covers the vendors a page could carry",
                {"hcaptcha.com", "datadome", "awswaf",
                 "request unsuccessful"} <= markers)
    ok &= check("a RENDERED reCAPTCHA widget is covered",
                {"recaptcha/api2/anchor", "recaptcha/enterprise/bframe"}
                <= markers)
    ok &= check("...and the bare loader is NOT, because every page has it",
                not [m for m in markers
                     if m in ("recaptcha", "g-recaptcha", "recaptcha/api.js")])
    ok &= check("...and does NOT include cf-turnstile, which our own "
                "extension injects",
                not [m for m in markers if "turnstile" in m])
    return ok


def _placeholder_reads_unset(raw):
    """Whether env_config would treat `raw` as "not configured".

    Goes through the real rule — `env_config.env_value`, which is where the
    placeholder logic lives — rather than reimplementing it, because a
    reimplementation is what drifts. The variable is set in os.environ
    directly and restored afterwards: `load_env` only fills variables that
    are not already set, so writing a temporary .env would be shadowed by
    whatever the suite has already loaded.
    """
    name = "SPINNY_CDP_ENDPOINT"
    saved = os.environ.get(name)
    try:
        os.environ[name] = raw
        with io.StringIO() as buf, redirect_stdout(buf):
            value = env_config.env_value(name)
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved
    return value is None


def test_env_config():
    group("env_config")
    ok = True
    ok &= check("the env keys are this site's, not another repo's",
                set(env_config.ENV_KEYS) ==
                {"TWOCAPTCHA_KEY", "SPINNY_CDP_ENDPOINT",
                 "SPINNY_PROXY", "SPINNY_URL"})

    # .env.example must document exactly the variables the code reads, in
    # both directions. It drifts otherwise, and a documented-but-unread
    # variable is worse than an undocumented one.
    example = os.path.join(REPO_ROOT, ".env.example")
    documented = set()
    if os.path.exists(example):
        for line in open(example, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                documented.add(line.split("=", 1)[0].strip())
    ok &= check(".env.example documents exactly the variables the code reads",
                documented == set(env_config.ENV_KEYS))

    # A variable mapped onto a flag with a non-empty default would be
    # silently inert, because the loader only fills UNSET values: a setting
    # that looks configurable and is not.
    ok &= check("no env variable is mapped onto --out (it has a default)",
                "out" not in env_config.ENV_KEYS.values())

    # A COPIED .env.example MUST READ AS UNSET, and a literal-only check is
    # not enough to make that true. This repo documents its two credentialled
    # URLs the way the vendor does, with the parts you fill in written in
    # braces:
    #
    #     ws://{login}-zone-scraping_browser-…-pid-{profileId}:{password}@…
    #     http://{user}:{password}@ap.proxy.2captcha.com:2334
    #
    # Before the brace check existed the loader reported both of those as
    # CONFIGURED, so `cp .env.example .env` and a run connected to
    # cb.2captcha.com with the string `{login}-zone-…` as its username and
    # got a 401 — a confusing failure a long way from its cause, which is
    # what §3's rule exists to prevent.
    for raw in ('ws://{login}-zone-scraping_browser-country-id-pid-'
                '{profileId}:{password}@cb.2captcha.com:9222',
                'http://{user}:{password}@ap.proxy.2captcha.com:2334',
                'your_2captcha_api_key_here'):
            ok &= check("a placeholder value reads as unset: %s..." % raw[:34],
                        _placeholder_reads_unset(raw))
    # ...and a REAL value still reads as set, or the guard has eaten the
    # feature it was protecting.
    ok &= check("a real value is not mistaken for a placeholder",
                _placeholder_reads_unset(
                    "ws://acct1-zone-scraping_browser-country-in-pid-p1:"
                    "secret@cb.2captcha.com:9222") is False)
    # The one variable a copied example leaves USABLE is the target URL,
    # which carries no credential and is a working default.
    ok &= check("the example's default URL is usable as-is",
                _placeholder_reads_unset(
                    "https://www.spinny.com/used-cars-in-delhi-ncr/s/")
                is False)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("TWOCAPTCHA_KEY=fromfile\n")
            f.write("SPINNY_URL=https://www.spinny.com/used-cars-in-pune/s/\n")
            f.write("NOT_A_REAL_KEY=1\n")

        class A:
            twocaptcha_key = None
            url = None
            cdp_endpoint = None
            proxy = None

        a = A()
        env_config.load_env(path)
        env_config.apply(a, quiet=True)
        ok &= check("a value in .env fills an unset flag",
                    a.twocaptcha_key == "fromfile")

        b = A()
        b.twocaptcha_key = "fromflag"
        env_config.apply(b, quiet=True)
        # A .env must never override something the caller typed.
        ok &= check("an explicit flag beats .env", b.twocaptcha_key == "fromflag")
        # A typo is REPORTED rather than silently ignored.
        ok &= check("an unrecognised variable in .env is reported",
                    "NOT_A_REAL_KEY" in env_config.unknown_keys(path))
    return ok


def test_proxy_pool():
    group("proxy_pool: credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2captcha.com:2334"
    masked = mask(url)
    ok &= check("credentials are masked in logs", "secret" not in masked)
    # The host and port are KEPT: which exit a run used is the point of the
    # log and is not the secret.
    ok &= check("...but the host and port survive masking",
                "eu.proxy.2captcha.com:2334" in masked)

    pw = to_playwright(url)
    # A `--proxy-server=` value becomes part of the browser's command line,
    # readable by anything that can run `ps`. The credentials go through the
    # driver's own fields instead.
    ok &= check("the server string handed to the browser has no credentials",
                "secret" not in pw["server"])
    ok &= check("credentials go through the driver's own fields",
                pw["username"] == "user" and pw["password"] == "secret")

    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates the two",
                scrubbed == "http://eu.proxy.2captcha.com:2334"
                and creds == ("user", "secret"))

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("a pool reports its size", len(pool) == 3)
    first = pool.current
    pool.advance("test")
    ok &= check("advancing moves to another exit", pool.current != first)
    # `.proxies` hands back a COPY, so a worker building its own pool from it
    # cannot mutate the parent's list. Two threads sharing one mutable list
    # is the bug that makes concurrency stop being worth it.
    copy = pool.proxies
    copy.append("http://d:4")
    ok &= check("the pool hands out a copy of its exits, not the list itself",
                len(pool) == 3)

    # The family's `_worker_pool` — one pool object per worker, each rotated
    # to a different offset so no thread needs a lock — is NOT in this repo,
    # and the check that used to live here went with it. `--concurrency` is
    # refused on this site (page 5 of an infinitely scrolling grid has no
    # address to hand a worker), so a per-worker pool would be machinery with
    # no caller: the defect §17 names, tested into looking load-bearing.
    #
    # What replaces it is the rotation a SEQUENTIAL run actually uses. A pool
    # here spreads several runs, or several block-retries within one, rather
    # than parallel workers — and rotating must hand back a genuinely
    # different exit each time rather than appearing to.
    seen = {pool.current}
    for i in range(2):
        pool.advance("block retry %d" % i)
        seen.add(pool.current)
    ok &= check("rotating a three-exit pool visits all three", len(seen) == 3)
    pool.advance("wrap around")
    ok &= check("...and then wraps rather than running out",
                pool.current in seen)

    # A pool of one is legal and must not rotate itself into an index error.
    one = ProxyPool(["http://only:1"])
    one.advance("nowhere else to go")
    ok &= check("a single-exit pool survives a rotation",
                one.current == "http://only:1")
    ok &= check("an empty pool is refused rather than silently accepted",
                _raises(lambda: ProxyPool([])))

    # This used to assert that "http://host:port:login:pass" — a line from a
    # proxy LIST FILE — "is understood", checking only that parse_proxy_line
    # did not reject it. It returned the string unchanged, so the check
    # passed; the value was never usable, and it blew up several calls later.
    # A test that asserts a function did not complain is not a test that its
    # answer was right.
    #
    # A proxy LIST FILE line pasted where a proxy URL belongs. This is the
    # mistake a new user makes — the file format is
    # scheme://host:port:login:password and the flag wants
    # http://login:password@host:port — and it reached a real CI run.
    #
    # It used to sail through parse_proxy_line (which never looked at the
    # port) and blow up much later inside to_playwright as an uncaught
    # ValueError: exit 1, a crash, where it should be exit 2, bad usage. And
    # the traceback printed the login AND the password into a public CI log.
    from proxy_pool import ProxyError
    pasted = ("http://eu.proxy.2captcha.com:2334:"
              "SOMELOGIN-zone-custom-region-de:SOMEPASSWORD")
    raised = None
    try:
        parse_proxy_line(pasted, source="SPINNY_PROXY")
    except ProxyError as exc:
        raised = str(exc)
    ok &= check("a proxy-list line pasted as a URL is refused, not crashed on",
                raised is not None)
    ok &= check("...and the refusal says what the value should look like",
                raised is not None and "login:password@host:port" in raised)
    ok &= check("...and neither the login nor the password is in the message",
                raised is not None
                and "SOMEPASSWORD" not in raised and "SOMELOGIN" not in raised)

    # mask() is the last thing standing between a password and a log, and it
    # is called precisely when the value is already wrong. It read
    # `parsed.port`, which urlparse computes lazily and which RAISES on a
    # malformed authority — so the masker blew up on exactly the input that
    # most needed masking. A masker that raises is worse than a vague one.
    ok &= check("mask() does not raise on a malformed URL",
                "SOMEPASSWORD" not in mask(pasted))
    for junk in ("::::", "not a url", "http://", "://x", ""):
        try:
            mask(junk)
            raised_here = False
        except Exception:
            raised_here = True
        ok &= check("mask(%r) does not raise" % junk, not raised_here)
    ok &= check("mask() still keeps host and port on a good URL",
                mask("http://u:p@h.example:8080") == "http://***:***@h.example:8080")

    # PINNED LIMITATION, not a defence. mask() takes a bare URL; given a
    # SENTENCE containing one it returns "?://?" — the password is gone,
    # which is the property that matters, but so is the host and port the log
    # was written to show. Every caller here passes the URL as its own `%s`
    # argument for that reason, and `_mask_credentials()` is what handles
    # arbitrary text. Asserting the CURRENT behaviour makes a future swap a
    # failing check rather than an unreadable log (§10).
    sentence = "a http://u:supersecret@h.example:8080 b"
    ok &= check("mask() on a sentence loses the host — the documented limit",
                mask(sentence) == "?://?")
    ok &= check("...but never the password", "supersecret" not in mask(sentence))
    ok &= check("every mask() call site passes a bare URL, not a sentence",
                not [ln for f in ("playwright_scraper.py", "puppeteer_scraper.py",
                                  "selenium_scraper.py", "proxy_pool.py")
                     for ln in open(os.path.join(REPO_ROOT, f),
                                    encoding="utf-8").read().split("\n")
                     if re.search(r'[^_]mask\(f?["\']', ln)])
    # ...and the engines' own masker handles a sentence, globally. A masker
    # that fixes the first occurrence and prints the password the other four
    # times looks exactly like one that works.
    for name in _ENGINE_MODULES:
        try:
            mod = __import__(name)
        except ImportError:
            continue
        many = ("x ws://u:supersecret@h:1 y ws://u:supersecret@h:1 "
                "z http://u:supersecret@h:2")
        out = mod._mask_credentials(many)
        ok &= check("%s._mask_credentials masks EVERY occurrence" % name,
                    "supersecret" not in out)
        ok &= check("%s._mask_credentials keeps the surrounding text" % name,
                    out.startswith("x ") and out.endswith(":2")
                    and "h:1" in out)
    return ok


# The three engines. Playwright is primary; the other two exist for parity
# and are demoted in priority, not in correctness — all three must agree on
# exit codes, run status, and whether a run crashes or spends money.
_ENGINE_MODULES = ("playwright_scraper", "puppeteer_scraper",
                   "selenium_scraper")


STATE_POLICY_NAMES = ("content", "empty", "notfound", "blocked", "challenge",
                      "unknown")


def test_engines(skips):
    group("engines: all three must behave identically")
    ok = True
    loaded = {}
    for name in _ENGINE_MODULES:
        try:
            loaded[name] = __import__(name)
        except ImportError as e:
            # Reported, never swallowed: "skipped, engine absent" reads
            # exactly like a passing run, and CI's engine-smoke job fails if
            # this list is non-empty.
            skips.append("%s (%s)" % (name, e))

    for name, mod in loaded.items():
        ok &= check("%s exposes scrape() and parse_args()" % name,
                    hasattr(mod, "scrape") and hasattr(mod, "parse_args"))
        # The engines must reach the shared policy rather than carry copies.
        src = inspect.getsource(mod)
        ok &= check("%s takes its readiness policy from page_flow" % name,
                    "page_flow.ready_selector" in src)
        ok &= check("%s takes its state policy from page_flow" % name,
                    "page_flow.should_retry" in src or "page_flow.classify" in src)
        # The scroll POLICY lives in page_flow and the DRIVER primitives
        # live here — the opposite of a sibling repo, which has no scroll at
        # all. An engine that grew its own loop would drift on the settle
        # rule, so the shared call is asserted and a local copy is banned.
        ok &= check("%s drives the shared scroll rather than its own" % name,
                    "page_flow.scroll_until_settled" in src)
        ok &= check("%s names the scroll operations, and passes no JS to "
                    "page_flow" % name,
                    "page_height" in src and "scroll_to_bottom" in src)
        # "Not painted yet" is not a fault, and every engine has to make that
        # distinction the same way — the first live search run of the
        # Playwright engine reported 0 rows and exit 4 because it did not.
        ok &= check("%s waits for an unpainted page instead of retrying it"
                    % name, "page_flow.is_unpainted" in src)
        # Credentials never reach a log, in any engine.
        ok &= check("%s masks credentials globally, not just once" % name,
                    "pass@" not in mod._mask_credentials(
                        "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
        ok &= check("%s refuses a host that is not Spinny" % name,
                    "is_supported_host" in src)
        # The same modes in every engine — a mode one engine offers and
        # another does not is the drift page_flow.py and finish_run() exist
        # to prevent, one level up. There are exactly two here.
        ok &= check("%s offers exactly the listing and detail modes" % name,
                    '"listing", "detail"]' in src)
        ok &= check("%s has no shop mode" % name, '"shop"' not in src)
        # --concurrency is REFUSED on this site rather than ignored, in every
        # engine, and with the reason. A flag that appears to work and does
        # nothing is worse than one that says no.
        ok &= check("%s refuses --concurrency with page_flow's reason" % name,
                    "concurrency_refusal" in src)
        # ...and the worker machinery the rest of this family ships is NOT
        # here. Page 5 of an infinitely scrolling grid has no address to hand
        # a worker, so shipping the pool would be dead code that looks
        # load-bearing. Pinned in both directions: adding it back has to be a
        # decision, taken together with product_parser.PAGINATED_KINDS.
        # Checked as a DEFINITION rather than as a mention: the comment that
        # explains why the pool is absent names both functions, and a
        # substring check would fail on its own explanation.
        ok &= check("%s ships no worker pool" % name,
                    "def _fetch_pages_concurrently" not in src
                    and "def _worker_pool" not in src)
        # `--pages N` is a SCROLL budget on this site, so every engine has to
        # pass the budget into the shared scroll rather than scrolling until
        # it settles regardless of what was asked for.
        ok &= check("%s turns --pages into a scroll budget" % name,
                    "scroll_rounds_for" in src and "target_cards" in src)
        # The scroll's per-round card counts are what let a run that fetched
        # ONE page still report a meaningful `page` per row.
        ok &= check("%s threads the scroll boundaries into the parse" % name,
                    "boundaries" in src)
        # Spinny publishes its own total, so completeness is arithmetic.
        ok &= check("%s records the arithmetic completeness check" % name,
                    "page_flow.completeness" in src)
        # And the pagination tripwire, which is what would notice the site
        # growing the markup this whole design assumes it does not have.
        ok &= check("%s checks for pagination the site has never had" % name,
                    "_check_for_new_pagination" in src
                    or "pagination_is_addressable" in src)

    # THE FLAG CONTRACT, and the exact ways the engines differ from it.
    #
    # §9 lists the flags every engine must offer. Checked rather than
    # trusted, because a flag one engine has and another does not is the
    # drift page_flow.py and finish_run() exist to prevent, one level up —
    # and because the README documents these differences by name, so a new
    # divergence has to update the README or fail here.
    contract = ("--url --pages --category --format --out --delay --retries "
                "--retry-delay --concurrency --proxy --proxy-file "
                "--proxy-rotate --proxy-shuffle --proxy-block-retries "
                "--twocaptcha-key --captcha-api --solve-captcha --min-score "
                "--cdp-endpoint --allow-empty --dump-html").split()
    flags = {}
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        flags[name] = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', src))
        missing = [f for f in contract if f not in flags[name]]
        ok &= check("%s offers every flag in the family contract" % name,
                    not missing)
        if missing:
            print("        missing: %s" % missing)
        ok &= check("%s offers --headless and --headful" % name,
                    "--headless" in flags[name] and "--headful" in flags[name])
    if len(flags) == 3:
        pw = flags["playwright_scraper"]
        # The differences the README states, pinned in both directions: a NEW
        # divergence fails here, and closing one of these also fails here, so
        # the README cannot quietly go stale either way.
        ok &= check("pyppeteer differs from playwright by exactly the "
                    "documented four flags",
                    sorted(pw - flags["puppeteer_scraper"])
                    == ["--fingerprint", "--fp-country", "--fp-tags",
                        "--locale"])
        ok &= check("selenium differs from playwright by exactly --locale",
                    sorted(pw - flags["selenium_scraper"]) == ["--locale"])

    # `--fp-tags` MUST DEFAULT TO ONE OS-FAMILY TAG. It shipped in this
    # family as "Windows,Chrome,Desktop", which the fingerprint API rejects
    # with HTTP 400 — so --fingerprint failed on every invocation, which is
    # one of the six defects §16 of the family notes lists. Measured
    # 2026-09-10 against the live API: `Windows` succeeds;
    # `Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400.
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        m = re.search(r'--fp-tags"\s*,\s*default="([^"]*)"', src)
        if m is None:
            continue          # pyppeteer has no fingerprint flags
        ok &= check("%s's --fp-tags default is ONE tag the API accepts"
                    % name,
                    "," not in m.group(1)
                    and m.group(1) in ("Windows", "Microsoft Windows",
                                       "Android"))

    # EVERY page_flow CALL IN EVERY ENGINE, CHECKED AGAINST THE REAL
    # SIGNATURE. This is the general form of a bug the first live run of the
    # pyppeteer engine found: `classify(html, status, url)` took `status`
    # positionally, and two of the three engines called it as
    # `classify(html, url=…)` because they have no response object to read a
    # status from. Both crashed with TypeError on their FIRST fetch — and
    # that was invisible to import, to --help, to compileall, to the AST
    # undefined-name walk and to 400+ green offline checks, because none of
    # those calls a function the way a live run does.
    #
    # An offline suite cannot execute a fetch. It CAN bind every call's
    # arguments to the callee's signature, which is the same check the
    # interpreter does at the moment of the call, minus the browser.
    import inspect as _inspect
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "page_flow"):
                continue
            target = getattr(page_flow, fn.attr, None)
            if not callable(target):
                bad.append("%s: page_flow has no %s()" % (name, fn.attr))
                continue
            try:
                sig = _inspect.signature(target)
            except (TypeError, ValueError):
                continue
            # Bind PLACEHOLDERS, not values: this checks arity and keyword
            # names, which is what drifts. `*args` in the call (none today)
            # would make the binding unknowable, so it is skipped rather
            # than guessed at.
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                sig.bind(*[object()] * len(node.args),
                         **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d page_flow.%s(...) — %s"
                           % (name, node.lineno, fn.attr, exc))
        ok &= check("every page_flow call in %s matches its signature" % name,
                    not bad)
        for line in bad:
            print("        %s" % line)

    # The same check for product_parser, which the engines call as often.
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "product_parser":
                imported.update(a.asname or a.name for a in node.names)
        bad = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in imported):
                continue
            target = getattr(product_parser, node.func.id, None)
            if not callable(target):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                _inspect.signature(target).bind(
                    *[object()] * len(node.args),
                    **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d %s(...) — %s"
                           % (name, node.lineno, node.func.id, exc))
        ok &= check("every product_parser call in %s matches its signature"
                    % name, not bad)
        for line in bad:
            print("        %s" % line)

    # And the two-argument call itself, pinned: `status` must stay optional,
    # because two of the three engines have no status to pass.
    ok &= check("page_flow.classify works with no status, as two engines "
                "call it",
                page_flow.classify("<html>x</html>",
                                   url=LISTING_URL) in STATE_POLICY_NAMES)

    # THE ENGINES' OWN CONSTANTS, compared against each other. A threshold
    # one engine reports a problem against and its twins do not is the same
    # class of drift as a policy constant, one level down — and it would show
    # up as one engine warning about a page the others call healthy.
    consts = {}
    for name in _ENGINE_MODULES:
        mod = loaded.get(name)
        if mod is None:
            continue
        consts[name] = (getattr(mod, "PRICE_FLOOR", None),
                        getattr(mod, "ALL_IN_ABOVE_DISPLAYED_FLOOR", None),
                        getattr(mod, "ITEM_LINK_SELECTOR", None))
    if len(consts) == 3:
        values = list(consts.values())
        ok &= check("all three engines share the same coverage thresholds "
                    "and readiness anchor", values[0] == values[1] == values[2])
        ok &= check("...and the price floor is keyed by the parser's own "
                    "page kinds",
                    set(values[0][0]) == {"listing", "cityless", "detail"})

    # For "it must pass with no engine installed" to mean anything, each
    # engine has to import its driver at MODULE level — otherwise the module
    # imports cleanly with the library absent, the group never skips, and the
    # CI job that exists to catch that cannot. This drifts back silently, so
    # it is asserted rather than trusted.
    driver_imports = {"playwright_scraper": "playwright",
                      "puppeteer_scraper": "pyppeteer",
                      "selenium_scraper": "selenium"}
    for name, lib in driver_imports.items():
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        ok &= check("%s imports %s at module level, so an absent library skips"
                    % (name, lib), lib in top_level)
    return ok


def _raises_type(fn, exc_type) -> bool:
    """True if `fn()` raises exactly `exc_type` (or a subclass)."""
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 — a different type is a failed check
        return False
    return False


def _no_secret_in(fn, secret: str) -> bool:
    """True if `fn()` raises and the secret is absent from the message."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return secret not in str(e)
    return False


# Names Python provides that are not imports and not assignments.
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    """Names loaded in `path` that are never imported, defined or assigned.

    A deliberately coarse approximation — it pools every binding in the file
    rather than tracking scopes, so it under-reports and never invents a
    problem. That is the right trade here: this exists to catch a name that
    is nowhere at all, and a false positive would be worse than a miss.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound |= set(node.names)
    missing = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            missing.setdefault(node.id, []).append(node.lineno)
    return missing


def test_x_debug_header_is_redacted():
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim.

    The fixtures are assembled from pieces rather than written out whole,
    because this file is scanned by the credential check like every other
    and a fixture that LOOKS like a live key fails it. They are the SHAPES a
    credential takes, not the literals this repo happens to contain today.
    """
    try:
        import scraper_api_client as sac
    except ImportError:
        return False

    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    ok = True
    ok &= check("x-debug: the credential and the key are gone",
                      pw not in out and key not in out)
    ok &= check("x-debug: the cost, host and status survive",
                      "cost=0.00145" in out and "cb.2captcha.com:9222" in out
                      and "status=200" in out)

    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    ok &= check("x-debug: both credentials are masked, not just the first",
                      s1 not in two and s2 not in two)

    src = inspect.getsource(sac)
    ok &= check("x-debug: the log line calls the redactor",
                      'logger.info("x-debug: %s", _redact_debug_header(debug))' in src)
    return ok


def test_scraper_api_waitfor_is_an_object():
    """Both Scraper API defects measured 2026-09-23, through the real
    parse_args() and fetch_html(), with requests.post captured (no network).

    waitFor went out as a JSON-encoded STRING, which the live API refuses
    with HTTP 422 and still bills; and the target's status was read from
    `status`, which is the API's own verdict string ("success"), so a
    target 403/503 never reached detect_page_state. The real field is
    `http_code`."""
    group("Scraper API: waitFor is an object, the target status is http_code")
    try:
        import scraper_api_client as sac
    except ImportError as e:
        return check("scraper_api_client imports (%s)" % e, False)

    captured = {}

    class _Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "headers": {},
                    "body": "<html></html>"}

    def _post(url, **kw):
        captured.update(kw)
        return _Resp()

    real_post, real_argv = sac.requests.post, sys.argv
    sac.requests.post = _post
    sys.argv = ["scraper_api_client.py", "--key", "k" * 8,
                "--url", "https://www.spinny.com/used-cars-in-delhi-ncr/s/", "--wait-text", "Maruti"]
    status = None
    try:
        args = sac.parse_args()
        got = sac.fetch_html(args)
        status = got[1] if isinstance(got, tuple) else None
    finally:
        sac.requests.post, sys.argv = real_post, real_argv
    wf = (captured.get("json") or {}).get("waitFor")
    ok = check("Scraper API: --wait-text sends waitFor as an OBJECT, not a "
               "JSON-encoded string (HTTP 422 and still billed, 2026-09-23)",
               isinstance(wf, dict) and wf.get("text") == "Maruti")
    ok &= check("Scraper API: the status handed onward is the target's "
                "http_code (403, an int), not the API's verdict 'success'",
                isinstance(status, int) and status == 403)
    return ok


def test_parse_is_gated_on_the_policy():
    group("the engines read STATE_POLICY's parse column")
    ok = True
    # `should_parse` existed, was correct, and had NO consumer: every
    # engine parsed whatever reached the parse line, so the `parse` column
    # of STATE_POLICY decided nothing and an engine could disagree with the
    # table — and with its twins — without anything noticing. That is the
    # defect CLAUDE.md §17 names for constants, with a function instead.
    #
    # Measured across the family on 2026-09-23 by counting definitions
    # against readers: 7 of 24 repos defined it and none called it.
    import glob as _glob
    engines = sorted(_glob.glob(os.path.join(REPO_ROOT, "*_scraper.py")))
    ok &= check("there are engines to check (%d)" % len(engines), engines)
    for path in engines:
        name = os.path.basename(path)
        text = open(path, encoding="utf-8").read()
        if "_parse_for_mode" not in text:
            continue
        ok &= check("%s reads the parse decision from the policy" % name,
                    "should_parse(" in text)
        # And nothing parses unconditionally any more: a bare
        # `products = _parse_for_mode(` is the shape that ignored the table.
        ok &= check("%s does not parse unconditionally" % name,
                    not re.search(r"products = _parse_for_mode\(", text))
    # Every state the policy names must be answerable — a typo'd state name
    # would make should_parse fall through to its default for ever.
    for state in page_flow.STATE_POLICY:
        ok &= check("should_parse answers for %r" % state,
                    isinstance(page_flow.should_parse(state), bool))

    return ok


def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports success
    # without checking that what it wanted actually happened.
    skips = []

    ok &= test_parse_is_gated_on_the_policy()
    ok &= test_price_parsing()
    ok &= test_listing_values()
    ok &= test_cards_are_scoped_to_one_car()
    ok &= test_detail_page()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_recaptcha_is_not_a_block_marker()
    ok &= test_extension_markers_are_not_the_sites()
    ok &= test_page_flow()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_finish_run()
    ok &= test_diff()
    ok &= test_captcha()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_engines(skips)
    ok &= test_pyppeteer_teardown_noise()
    ok &= test_canary_separates_access_from_defect()
    ok &= test_ci_checks_is_actually_wired_up()
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_fingerprint_client_reads_env()
    ok &= test_fingerprint_application()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_single_fetch_run(skips)
    ok &= test_no_undefined_names()
    ok &= test_no_unused_imports()
    ok &= test_dockerfile_copies_what_it_runs()
    ok &= test_sample_output()
    ok &= test_x_debug_header_is_redacted()
    ok &= test_scraper_api_waitfor_is_an_object()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs all three and fails if "
              "this list is non-empty, because a skip reads exactly like a "
              "passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
