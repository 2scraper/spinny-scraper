"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Two modes, one row shape
------------------------
    --mode listing   a city or filtered used-car listing -> Product
    --mode detail    one /buy-used-cars/.../{id}/ page -> Car, with the
                     trailing detail-only fields populated

Both modes yield the SAME class, because on Spinny a detail page is not a
different kind of object from a tile — it is the same car described more
fully. So there is no second dataclass here, and `diff_runs.py` can compare
a listing run against a detail run on the columns both populate.

THE PRICE COLUMNS ARE THE THING TO READ BEFORE ANYTHING ELSE
------------------------------------------------------------
Spinny publishes **four different figures** for one car, and they disagree by
up to ₹300,000. Measured on 482 Delhi tiles, 2026-09-11, for the Hyundai
Grand i10 with id 31483859:

    displayed price      "3.88 Lakh"   388,000   the card's headline, and the
                                                 detail page's "Car price"
    displayed strike     (absent here)           the was-price, when discounted
    discount badge       (absent here)           "₹13,000" — exact rupees
    data-price attribute "392000"      392,000   the site's own `price` field,
                                                 and the detail page's JSON-LD
                                                 `offers.Price`
    page title           "at 3.92 Lakh"          the same 392,000, rounded

The difference is NOT a discount. 392,000 is the car price **plus RC transfer
facilitation and insurance**; 388,000 is the same car **without** them. The
attribute figure was higher than the displayed one on **482 of 482** tiles —
median +₹17,000, maximum +₹299,596 — so the two are different definitions,
not two reads of one number.

And a discount moves a third axis. For the Mahindra XUV 300 id 31064888 the
card shows "8.41 Lakh" with "8.54 Lakh" struck through and a "₹13,000" badge,
while `data-price` is 862,000: the strike is the pre-discount price WITHOUT
add-ons (853,579 rounded), the headline is that minus the ₹13,000 discount,
and the attribute is the pre-discount price WITH add-ons.

So the only pair of figures on ONE basis is the displayed pair, and that is
what `price` / `original_price` / `discount_pct` carry — coherent, and the
same basis on a listing tile and on a detail page. The exact all-in figure
gets its own column, `price_all_in`, because it is exact where the displayed
one is quantised: Spinny renders a lakh to two decimals, so "8.41 Lakh" is
any amount from 840,500 to 841,499. A price monitor that needs rupee
precision should diff `price_all_in`; one that wants what a customer sees
should diff `price`.

`rating` and `review_count` are null on every row of every run
--------------------------------------------------------------
Kept anyway, and this is a deliberate exception to "a column that is null on
every row should not exist" — they are part of the family's column prefix and
consumers read these names across six repos.

They are null because Spinny publishes neither, measured rather than assumed:
0 of 705 listing tiles across three captures carry a rating or a review
count, and a detail page's `["Product","Car"]` JSON-LD has no
`aggregateRating` either. What a detail page DOES have is five named
INSPECTION-SECTION scores out of 10 ("Core systems 9.8", "Exteriors & lights
7.7") — five numbers about different parts of one car, which is not a
product rating and would be a fabrication squeezed into one column. The
listing page's only `aggregateRating` belongs to a marketing block rating
**Spinny**, not the car.

`Product` keeps the family's first sixteen columns in the family's order, with
the Spinny-specific ones appended after `price_source`, so a consumer written
against another repo in this family still reads the prefix unchanged.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The hostname a row came from. Spinny is ONE storefront on one hostname,
# serving one country in one currency — there is no locale path, no
# per-country domain and no second currency anywhere on the site. The column
# is `spinny.com` on every row of every run, and it is kept because the
# family's schema has it in this position and consumers read the columns by
# name across repos.
SOURCE_DEFAULT = "spinny.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # The car's numeric id, as a string. Spinny puts it in the last segment
    # of every product URL and in the `data-label` attribute of the tile's
    # shortlist button, and the two agreed on 705 of 705 tiles across three
    # captures. It is also the detail page's JSON-LD `productID`, so a
    # listing row and a detail row join on it exactly.
    #
    # A string rather than an int because every other repo in this family
    # has a string `sku`, and because a CSV consumer that reads it as a
    # number will eventually meet a leading zero.
    sku: Optional[str] = None
    # The card's own headline, e.g. "2019 Hyundai Grand i10" — year, make and
    # model as Spinny prints them. 705/705.
    title: Optional[str] = None
    # The manufacturer, and it comes from the URL rather than from the
    # headline. This is a real trap: **the headline abbreviates the make.**
    # 178 of 705 tiles print "Maruti Swift" for a maruti-suzuki, "Mercedes
    # CLA" for a mercedes-benz. The URL's own `/{make}/` segment does not,
    # so `brand` is built from that with a display map for the twenty makes
    # observed. See product_parser.MAKE_DISPLAY.
    brand: Optional[str] = None
    # The DISPLAYED price, in rupees — post-discount, excluding RC transfer
    # and insurance, and quantised to two decimals of a lakh by the site
    # itself. Read the price note at the top of this file before using it.
    price: Optional[float] = None
    # INR. A fact rather than a guess on this site: Spinny operates in one
    # country on one hostname, its discount badge writes "₹" explicitly, and
    # a detail page's JSON-LD states `"priceCurrency": "INR"` outright. Still
    # null rather than defaulted when no price was found — a row with no
    # price has no currency either.
    currency: Optional[str] = None
    # The struck-through was-price, on the same displayed basis as `price`.
    # 685 of 705 tiles carry one, and the 20 that do not also carry no
    # discount badge — the two travel together, so this is "not discounted"
    # rather than a parsing failure. Null in --mode detail: a detail page
    # prints no strike of its own (0 on the captured page), and the "Similar
    # cars" carousel's prices belong to other cars.
    original_price: Optional[float] = None
    # Computed from the two prices, never read off a badge — and
    # cross-checked against `discount_amount`, which is the site's own exact
    # rupee figure. None rather than 0 or a negative when the two figures are
    # not what they were taken for, so a canary can assert "no
    # original_price at or below its price" and mean it.
    discount_pct: Optional[float] = None
    # Null on every row of every run. See the note at the top of this file:
    # Spinny publishes no car-level rating anywhere, and the five inspection
    # scores a detail page does publish are about different parts of one car.
    rating: Optional[float] = None
    # Null on every row of every run — Spinny publishes no per-car review
    # count. The customer reviews it does publish are about Spinny.
    review_count: Optional[int] = None
    # Only a detail page states availability, as JSON-LD
    # `offers.availability`. Null on a listing row rather than assumed True:
    # the listing API is called with `include_booked=false`, so a tile's mere
    # presence says the default filter excluded booked cars, not that this
    # car is available.
    in_stock: Optional[bool] = None
    # The tile's own car photograph, from the site's asset host. 705/705 —
    # and unlike two sibling repos there is no lazy-load placeholder to guard
    # against here: Spinny renders a real `assets.spinny.com` URL into `src`
    # on every tile in the grid, painted or not.
    image_url: Optional[str] = None
    # What the listing URL selects, as a slug: "cars", "luxury-cars",
    # "volvo-cars", "cars-under-1-lakh-rs". The city is NOT part of it — it
    # has its own column — because "used-cars-in-delhi-ncr" and
    # "used-cars-in-bangalore" are the same category in two places.
    category: Optional[str] = None
    # Where `price` came from:
    #   "dom+attr"  the rendered tile, with the site's own `data-price`
    #               attribute present and consistent with it (the attribute
    #               being the higher, all-in figure). The normal case on a
    #               listing page: 705/705.
    #   "dom"       the rendered tile only — the attribute was missing.
    #   "dom+jsonld"  --mode detail: the displayed price from the page, with
    #               the JSON-LD `offers.Price` in `price_all_in`.
    # diff_runs.py reports a price change that comes with a price_source
    # change as `source_changed`, not `changed`: that says something about
    # our own two snapshots, not about Spinny.
    price_source: Optional[str] = None

    # ---- Spinny-specific, appended so the family prefix stays stable ----
    # Which batch of the infinite scroll this row came from (1-based) and its
    # position within the whole grid as Spinny ordered it.
    #
    # `page` is NOT a URL here, and that is the single most important thing
    # to know about this site's pagination: `?page=N` on a Spinny listing is
    # silently IGNORED — pages 1, 2 and 3 of /used-cars-in-delhi-ncr/s/
    # returned byte-identical first cards. The grid is one infinitely
    # scrolling page that hydrates ~20 cars at a time, so `page` is the
    # scroll batch the car first appeared in, derived from the card count
    # observed after each round. `position` is global, not per-batch, so the
    # pair is unique across a run — which the offline suite asserts, because
    # a `position` that restarts silently is worthless.
    page: Optional[int] = None
    position: Optional[int] = None
    # The model, from the URL's own `/{model}/` segment. Kept beside `brand`
    # for the same reason: the headline abbreviates.
    model: Optional[str] = None
    # The trim, as the tile prints it: "Sportz 1.2 Kappa VTVT", "W8 (O) 1.2
    # Petrol AMT". 705/705. Taken from the DOM and not from the URL slug —
    # the slug drops the decimal points and appends the hub name
    # ("sportz-12-kappa-vtvt", "vxi-sector-29"), so it disagreed with the
    # printed trim on 530 of 705 tiles.
    variant: Optional[str] = None
    # Manufacture year, from the headline's leading four digits. 705/705.
    # NOT the registration year, which a detail page states separately and
    # which differed from it on the car captured (Apr 2019 made, Aug 2019
    # registered).
    year: Optional[int] = None
    # Odometer reading in kilometres — and on a listing row this is the
    # site's OWN ROUNDED figure, not the exact one. A tile prints "63.5K km"
    # for a car whose detail page states 63,417, so `km_driven` is 63500
    # here and the exact number arrives in --mode detail. Rounded to the
    # nearest 500 km in practice; 705/705.
    km_driven: Optional[int] = None
    # "Petrol" · "Diesel" · "Cng" · "Hybrid" · "Electric", exactly as the
    # tile spells them (Spinny writes "Cng", not "CNG"). 705/705 across three
    # captures: 610 / 63 / 13 / 18 / 1.
    fuel_type: Optional[str] = None
    # "Manual" or "Automatic". 705/705 — 401 and 304.
    transmission: Optional[str] = None
    # The registering authority's code, e.g. "HR26", "DL8C", "UP16". 705/705,
    # 78 distinct. Worth a column on a used-car site: it says which state the
    # car is registered in, which decides the road-tax position of a buyer in
    # another one.
    rto: Optional[str] = None
    # The Spinny hub the car sits at, as printed: "Sector 27, Faridabad".
    # 695 of 705 — 10 tiles print none, which is why nothing here is read by
    # position in the tile's span order.
    hub: Optional[str] = None
    # The car's OWN city, from the URL's `/{city}/` segment — and not the
    # city in the listing URL, which is a REGION. A
    # /used-cars-in-delhi-ncr/ listing returned cars in delhi, gurgaon,
    # ghaziabad, noida, faridabad, sonipat and karnal.
    car_city: Optional[str] = None
    # Spinny's own quality tier, from the tile's `data-category` attribute:
    # "assured" · "budget" · "luxury". 705/705 — 351 / 264 / 90.
    assurance: Optional[str] = None
    # The EXACT all-in price: the site's own `price` field, read from the
    # tile's `data-price` attribute and from a detail page's JSON-LD
    # `offers.Price`. Pre-discount, and including RC transfer facilitation
    # and insurance. Read the price note at the top of this file.
    price_all_in: Optional[float] = None
    # The discount in exact rupees, from the tile's own "₹13,000" badge. 685
    # of 705. Exact where `discount_pct` is computed from two quantised
    # figures, so it is the one to trust when they disagree.
    discount_amount: Optional[float] = None
    # The monthly instalment the tile advertises ("EMI 6,675/m"), in rupees.
    # 700 of 705. A financing quote rather than a property of the car — kept
    # because it is what the tile leads with and because it is the figure a
    # competitor's listing is compared on in this market.
    emi_monthly: Optional[int] = None
    # Spinny's own one-line pitch for this car: "High quality, less driven",
    # "4-star NCAP rating", "City's most affordable". Not a spec, and useful
    # for exactly that reason — it is the site's editorial hook.
    tag: Optional[str] = None
    # ---- populated by --mode detail only; null on a listing run ----
    # The EXACT odometer reading, from JSON-LD `mileageFromOdometer.value`.
    # Distinct from `km_driven` on purpose: 63417 against the tile's 63500,
    # and one column holding both would silently mean two things.
    km_driven_exact: Optional[int] = None
    # `numberOfPreviousOwners`. A listing tile does not state it at all.
    owners: Optional[int] = None
    # From `additionalProperty`: the registration year and month, which are
    # not the manufacture year in `year` — Apr 2019 made, Aug 2019
    # registered, on the car captured.
    registration_year: Optional[int] = None
    registration_month: Optional[str] = None
    color: Optional[str] = None
    seating_capacity: Optional[int] = None
    # From the detail page's "Car Overview" table. Genuinely useful on a used
    # car and published nowhere else: when the cover runs out and whether it
    # is comprehensive or third-party only.
    insurance_validity: Optional[str] = None
    insurance_type: Optional[str] = None

# Row classes by --mode, so an engine maps its mode to a schema in one place.
# Both modes are Product here; the mapping exists so adding a mode later is a
# one-line change rather than a search for every place that assumed Product.
ROW_CLASS_BY_MODE = {"listing": Product, "detail": Product}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both of this repo's modes qualify: a listing page
# names each product once, and a product page IS one product.
UNIQUE_BY_SKU_MODES = ("listing", "detail")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. On Spinny this DOES fire on
    healthy runs, and for two distinct reasons. The grid repeats cars — 189
    ids across 184 distinct cars in one Bangalore capture, and 5 extra
    `data-label` elements inside the Delhi grid — because a "recommended"
    strip is inserted mid-grid carrying cars the grid already holds. And
    every scroll batch re-parses the whole DOM, so batch 2 sees batch 1's
    cards again by construction. So a large drop count here is normal and
    says nothing is wrong; a drop count of ZERO on a multi-batch run would
    mean the scroll never advanced.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On Spinny this code specifically does NOT cover the three ways to get a
# real page with no cars on it: a filter combination the inventory does not
# match (/used-volvo-cars-in-karnal/s/ answers 200 with "0 Used Volvo cars
# in Karnal" and an alert-signup card where the grid would be); a
# CITYLESS listing URL (/used-cars/s/, which renders an empty grid because
# Spinny scopes its inventory by city and could not pick one); and a
# `/used-cars/` SEO landing page, which has no grid on it at all. All three
# are EXIT_NO_PRODUCTS — the request was served exactly as asked and simply
# has no cars on it. Reporting any of them as blocked would send a user
# hunting for a proxy problem that does not exist.
#
# Nor does it cover a 404. Spinny answers a URL that does not exist with its
# own branded not-found page under HTTP 404, and that is a typo in the
# argument rather than anything to do with access — the engines report exit
# 2 (bad usage) for it, naming the URL. See page_flow.STATE_POLICY.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listing run or a product run,
    and those populate different columns — `sold` is a FLOOR on a listing
    row and exact on a product row, so diffing one against the other would
    report every row as changed. diff_runs.py refuses a pair whose modes or
    sources differ. `source` is `spinny.com` on every row of every run here,
    since the site has one hostname and one currency; it is kept because
    consumers read these columns by name across the family.

    `extra` carries facts about the run that are not about any single row.
    On Spinny that is the listing's own advertised total — the count Spinny
    prints beside its heading ("1559 Used cars in Delhi NCR") — together
    with the scroll's own report. Those belong to the run rather than
    repeated down a column, and the total is what makes completeness
    ARITHMETIC rather than a guess: 482 rows against an advertised 1559 is
    proof the scroll stopped early, with no threshold needed.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `total_available` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. On Spinny that ordering is not a preference, it is the only
# thing that works: the site publishes NO `link[rel=next]`, no numbered
# anchors and no next-page button anywhere on a listing, and **`?page=N` is
# silently ignored** — pages 1, 2 and 3 of one listing URL returned
# byte-identical first cards. There is exactly one page per listing URL and
# it grows by scrolling, so "no new cars in a scroll batch" is the ONE
# termination condition this site offers. `pagination_exhausted` is kept
# because the family's engines still look for a next-link, and it has never
# matched here. See page_flow.pagination_is_addressable.
#
# "single_page_mode" is complete by construction: --mode detail reads one
# page because one page is all there is.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
