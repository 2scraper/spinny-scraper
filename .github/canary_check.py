#!/usr/bin/env python3
"""
The canary's data assertions, run against a finished scrape:

    python3 .github/canary_check.py canary_run

reads `canary_run.json` and `canary_run.meta.json` and exits 1 with one
`::error::` line per failed assertion.

It lives in a file rather than in the workflow so it can be RUN outside CI:
against a downloaded canary artefact, or by the offline suite. A heredoc in a
workflow is executed nowhere else, which makes it the least-tested code in
the repository, and it is exactly the code that decides whether the badge is
red.

WHAT IS ASSERTED, AND WHAT IS DELIBERATELY NOT
----------------------------------------------
Every check here tests the PARSER or the RUN — row count, fields every card
prints, invariants between two reads of the same card, the scroll and the
sidecar. None of them tests what Spinny chooses to show this week.

Two floors used to, and both turned the badge red on runs that were complete
and fully priced (2026-10-08, -09, -10 and -15):

* "emi_monthly on at least 90% of rows". The site changed the EMI line's
  wording on those days, and the parser missed the new wording, so the floor
  did catch a real parser miss, but it fails just as hard on a day the site
  stops printing an EMI at all. It is replaced by the invariant that matters
  to the parser: an EMI line never lands in another column (hub or tag),
  which is what the miss actually did to the rows.
* "a strike price always travels with the site's own discount figure". On
  2026-10-10 the site printed a strike price without its rupee badge on 20
  of 82 cards. Whether a badge is printed is a merchandising choice. What is
  kept is the arithmetic: where both ARE printed, they must agree.
"""

import json
import re
import sys
from pathlib import Path

BATCHES_REQUESTED = 3
# Measured 2026-09-11: --pages 3 returned 62 cars, three times, through three
# different engines; 60 to 102 on the canary runs of October. The floor is
# two batches' worth, so a slow round cannot trip it.
MIN_ROWS = 40
# Every card prints a price: 705 of 705 across three captures, and every row
# of every canary run.
MIN_PRICED_SHARE = 0.98

# Fields every card prints, so any of them collapsing means an anchor moved.
# Each is a fact about the CAR (or the row's own bookkeeping), not a
# promotion. `emi_monthly`, `original_price` and `discount_amount` are not
# here on purpose: see the module docstring.
CORE_FIELDS = (("title", 0.98), ("sku", 1.0), ("url", 1.0),
               ("brand", 0.95), ("model", 0.95), ("variant", 0.95),
               ("year", 0.95), ("currency", 0.98), ("category", 1.0),
               ("km_driven", 0.95), ("fuel_type", 0.95),
               ("transmission", 0.95), ("rto", 0.95),
               ("assurance", 0.95), ("image_url", 0.98),
               ("page", 1.0), ("position", 1.0))

DETAIL_ONLY = ("km_driven_exact", "owners", "registration_year",
               "registration_month", "color", "seating_capacity",
               "insurance_validity", "insurance_type", "in_stock")

# Text that belongs to the EMI box. If either shape turns up as a hub or a
# tag, the EMI read has missed a wording again.
_EMI_LIKE = re.compile(r"^(EMI\b|Save\s+extra\b)", re.I)


def check(rows, meta):
    fail = []
    n = len(rows)

    if n < MIN_ROWS:
        fail.append(f"only {n} rows, expected at least {MIN_ROWS} "
                    f"from {BATCHES_REQUESTED} batches of 20")

    skus = [r.get("sku") for r in rows]
    if len(set(skus)) != len(skus):
        fail.append(f"{len(skus) - len(set(skus))} duplicate sku(s): one "
                    f"scrolled page can re-render a card as it hydrates, and "
                    f"the writer is meant to drop the repeats")

    priced = sum(1 for r in rows if r.get("price") is not None)
    if n and priced / n < MIN_PRICED_SHARE:
        fail.append(f"only {priced}/{n} rows have a price "
                    f"(floor {MIN_PRICED_SHARE:.0%}): every card prints one, "
                    f"so the price node moved")

    # A listing page carries no structured price for a car, so the price
    # source is always the card's own node, with or without the attribute.
    sources = {r.get("price_source") for r in rows}
    if sources - {"dom+attr", "dom"}:
        fail.append(f"listing rows carry price_source {sorted(sources)}; only "
                    f"'dom+attr' and 'dom' are possible on a listing page")

    # Two price BASES: the displayed figure excludes RC transfer and
    # insurance, the site's own data-price attribute includes them, so the
    # attribute is the higher of the two (482 of 482 measured cards).
    pairs = [(r["price"], r["price_all_in"]) for r in rows
             if r.get("price") is not None
             and r.get("price_all_in") is not None]
    crossed_basis = [p for p, a in pairs if a < p]
    if pairs and len(crossed_basis) / len(pairs) > 0.02:
        fail.append(f"{len(crossed_basis)}/{len(pairs)} rows have an all-in "
                    f"price BELOW the displayed one: the two price bases have "
                    f"been crossed")
    if len(pairs) < 0.9 * n:
        fail.append(f"only {len(pairs)}/{n} rows carry both price bases: the "
                    f"data-price attribute is on every card, so it moved")

    for field, floor in CORE_FIELDS:
        got = sum(1 for r in rows if r.get(field) not in (None, ""))
        if n and got / n < floor:
            fail.append(f"{field} present on only {got}/{n} rows "
                        f"(floor {floor:.0%})")

    # `hub` is sparse by nature (10 of 705 cards print none), so the floor
    # is well under 100%.
    hubs = sum(1 for r in rows if r.get("hub"))
    if n and hubs / n < 0.80:
        fail.append(f"hub present on only {hubs}/{n} rows: about 2% of cards "
                    f"legitimately print none, so the hub read has broken")

    # The EMI box must not leak into the free-text columns.
    leaked = [(r.get("sku"), col, r.get(col)) for r in rows
              for col in ("hub", "tag", "variant")
              if r.get(col) and _EMI_LIKE.match(str(r.get(col)))]
    if leaked:
        fail.append(f"{len(leaked)} row(s) carry EMI-box text in another "
                    f"column, e.g. {leaked[:2]}: the EMI read missed a wording")

    # Where the site prints BOTH a strike price and its own rupee discount,
    # strike - price must equal the discount (the badge rounds at most to a
    # thousand). Whether it prints them is the site's choice; that they agree
    # when it does is the parser's job.
    disagree = [r.get("sku") for r in rows
                if r.get("price") is not None
                and r.get("original_price") is not None
                and r.get("discount_amount") is not None
                and abs((r["original_price"] - r["price"])
                        - r["discount_amount"]) > 1000]
    if disagree:
        fail.append(f"{len(disagree)} row(s) where strike minus price does "
                    f"not match the site's own discount figure: "
                    f"{disagree[:3]}")

    # No car-level rating or review count is published; if either fills up,
    # Spinny's OWN aggregate rating is being read into a car's row.
    for field in ("rating", "review_count"):
        got = sum(1 for r in rows if r.get(field) is not None)
        if got:
            fail.append(f"{got}/{n} rows have `{field}`: the site publishes "
                        f"none per car")

    for field in DETAIL_ONLY:
        got = sum(1 for r in rows if r.get(field) is not None)
        if got:
            fail.append(f"{got}/{n} listing rows have `{field}`, which only "
                        f"--mode detail can know")

    crossed = [r["sku"] for r in rows
               if r.get("original_price") and r.get("price")
               and r["original_price"] <= r["price"]]
    if crossed:
        fail.append(f"{len(crossed)} row(s) have an original_price at or below "
                    f"their price: {crossed[:3]}")
    bad_discount = [r["sku"] for r in rows
                    if r.get("discount_pct") is not None
                    and not (0 < r["discount_pct"] < 100)]
    if bad_discount:
        fail.append(f"{len(bad_discount)} row(s) have a discount outside "
                    f"(0, 100): {bad_discount[:3]}")

    bad_sku = [r.get("sku") for r in rows
               if not (r.get("sku") or "").isdigit()
               or (r.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
               != r.get("sku")]
    if bad_sku:
        fail.append(f"{len(bad_sku)} sku(s) are not the car's numeric id from "
                    f"its own URL: {bad_sku[:3]}")

    keys = [(r.get("page"), r.get("position")) for r in rows]
    if len(set(keys)) != len(keys):
        fail.append(f"{len(keys) - len(set(keys))} row(s) share a (page, "
                    f"position) pair: the scroll's batch boundaries are not "
                    f"reaching the parser")

    currencies = {r.get("currency") for r in rows if r.get("currency")}
    if currencies - {"INR"}:
        fail.append(f"rows carry a currency other than INR "
                    f"({sorted(currencies)}); this site quotes one")

    if meta.get("pages_completed", 0) < BATCHES_REQUESTED:
        fail.append(f"the run holds {meta.get('pages_completed')} batches of "
                    f"the {BATCHES_REQUESTED} requested (stop_reason="
                    f"{meta.get('stop_reason')!r}): the lazy-load scroll is "
                    f"not reaching further into the grid")
    batches_seen = {r.get("page") for r in rows}
    if len(batches_seen) < 2:
        fail.append(f"every row claims batch {sorted(batches_seen)}: the "
                    f"scroll's boundaries are not being recorded")
    if meta.get("status") != "complete":
        fail.append(f"run status is {meta.get('status')!r}, not 'complete'")
    if meta.get("mode") != "listing":
        fail.append(f"run mode is {meta.get('mode')!r}, not 'listing'")

    scroll = meta.get("scroll") or {}
    if not scroll:
        fail.append("the sidecar records no scroll at all: the lazy-load loop "
                    "did not run, and on this site the grid then never "
                    "hydrated")
    elif not (scroll.get("settled") or scroll.get("reached_target")):
        fail.append("the scroll neither settled nor reached its target, so "
                    "this run is a floor rather than the listing")

    # The advertised total moves between runs, so what is asserted is that
    # it EXISTS and is sane, not its value.
    comp = meta.get("completeness") or {}
    advertised = comp.get("total_available")
    if advertised is None:
        fail.append("the listing's own advertised total was not read: the "
                    "heading node ([data-componentname=HeadingContent]) has "
                    "moved")
    elif advertised < n:
        fail.append(f"the site advertises {advertised} cars and this run holds "
                    f"{n}: the total is being read from the wrong node")

    # Reported, never asserted: these follow the site's merchandising.
    emi = sum(1 for r in rows if r.get("emi_monthly") is not None)
    struck = sum(1 for r in rows if r.get("original_price") is not None)
    summary = (f"[canary] {n} rows, {priced} priced, {hubs} with a hub, "
               f"{emi} with an EMI, {struck} with a strike price, advertised "
               f"{advertised}, status {meta.get('status')!r}, stop_reason "
               f"{meta.get('stop_reason')!r}")
    return fail, summary


def main(argv):
    prefix = argv[1] if len(argv) > 1 else "canary_run"
    try:
        rows = json.loads(Path(prefix + ".json").read_text(encoding="utf-8"))
        meta = json.loads(Path(prefix + ".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"::error::the run's output could not be read ({e}): a run that "
              f"exited 0 must have written both files")
        return 1
    fail, summary = check(rows, meta)
    for line in fail:
        print(f"::error::{line}")
    print(summary)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
