"""
page_flow.py
------------
Spinny's page-state, lazy-load and pagination policy, shared by all three
engines.

Why this module exists, when part of this family keeps each engine
self-contained: Spinny answers a listing request in FIVE ways and four of
them want a different response.

    content     the grid painted — parse it
    empty       a real page with no cars on it, and there are THREE distinct
                ways to get one: a filter the inventory does not match
                (/used-volvo-cars-in-karnal/s/ answers 200 with a heading
                reading "0 Used Volvo cars in Karnal"), a CITYLESS listing
                URL (/used-cars/s/, which renders the grid container and no
                cars because Spinny scopes inventory by city), and a
                /used-cars/ SEO landing page, which has no grid at all. None
                is a fault, none should be retried, and none should send
                anyone looking for a proxy problem.
    notfound    Spinny's own branded 404. A typo in the argument — exit 2,
                not exit 3, and naming the URL.
    blocked     nothing arrived, or something Spinny did not build arrived.
    unknown     served by Spinny, no grid, no zero-count heading. On this
                site that is almost always "the XHR has not landed yet",
                which is why it is the one state that WAITS.

Five copies of that triage across three engines would drift, and the drift
would be silent — one engine reporting exit 3 where its twin reports exit 4
on the same URL. The family already shares `output_writer.finish_run()` for
exactly this reason; this is the same argument applied to the decisions that
come before it.

THE FIRST RESPONSE IS ALWAYS A SHELL
------------------------------------
Measured 2026-09-11 on /used-cars-in-delhi-ncr/s/: `domcontentloaded` at
1.1 s with **zero** cars in the DOM, first batch of 22 at 4.7 s. The grid
arrives from `api.spinny.com` after load and exists only in the rendered DOM.

So a run that classifies the first response naively gets "unknown", and
"unknown" in the rest of this family RETRIES — which would fetch the shell
again, scroll nothing, and report exit 4 on a listing holding 1559 cars.
`is_unpainted` exists to stop that: a page Spinny plainly served, with no
zero-count heading on it, is waiting to paint, and the answer is to WAIT.

THERE IS NO PAGINATION AND THAT CHANGES WHAT --pages MEANS
----------------------------------------------------------
`?page=N` on a Spinny listing is silently IGNORED — pages 1, 2 and 3 of one
URL returned byte-identical first cards under HTTP 200. A listing is one
infinitely scrolling page that hydrates 20 cars at a time.

So `--pages N` means **N batches of 20 cars**, which is the site's own page
size (its listing API takes `page=` with `size=20`, and the grid grew in
multiples of 20 on every round measured). The scroll stops at the target or
when the grid stops growing, whichever comes first, and the run says which.

The functions here are either pure or driven through small callables, so each
engine passes its own driver's primitives and keeps its browser plumbing to
itself:

    count(selector) -> int          how many elements match
    content() -> Optional[str]      current HTML, None if unavailable
    current_url() -> str            the URL the browser is on
    sleep(ms) -> None               the driver's own wait
    page_height() -> Optional[int]  document.body.scrollHeight
    scroll_to_bottom() -> None      scroll to that height

Deliberately no `evaluate(js)`: passing JavaScript from here would decide its
dialect for every driver, and they disagree — Playwright and pyppeteer take
`() => expr` while Selenium's `execute_script` takes a function body with an
explicit `return`. So the OPERATION is named and each engine spells it in its
own dialect.

Every value here is measured and the measurements are dated.
"""

import logging
from typing import Callable, Dict, List, Optional

from product_parser import (detect_page_state, listing_kind, page_url,
                            paginates_by_url, requested_page_in_url,
                            served_by_spinny, strip_tracking, total_available)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
# What "the page is ready" means, per mode. Both are the site's own container
# and URL markers rather than fields: every text node in a card wears the
# same `ds-body-small` utility class, so a field selector could not tell the
# fuel type from the RTO code, let alone survive a redesign.
READY_SELECTOR_LISTING = (
    '[data-id="landing-plp-container"] a[href^="/buy-used-cars/"]'
)
# A detail page's own price node, which the site labels. Not the `<h1>`: the
# page ships an `<h1>` in its shell before anything about the car has
# arrived, so waiting on it resolves on a page with no car in it.
READY_SELECTOR_DETAIL = '[data-base-component="Pricing"]'

# How many matches mean "the grid rendered". Must be > 1: waiting for a
# single match resolves on an unrelated link long before the grid paints
# (§5). Spinny's first batch is 22 cars and arrives whole, so 4 is reached
# the moment it lands and is nowhere near a batch boundary.
MIN_CARD_MATCHES = 4
MIN_CARD_MATCHES_DETAIL = 1

# Spinny's first batch landed 3.6 s after domcontentloaded on a local
# Chromium over an Amsterdam datacentre exit (1.1 s to DCL, 22 cars at
# 4.7 s), measured 2026-09-11. 45 s leaves room for a remote browser on a
# residential exit without letting a genuinely dead page hold a worker.
CONTENT_TIMEOUT_MS = 45_000
CONTENT_TIMEOUT_MS_DETAIL = 30_000


def ready_selector(mode: str) -> str:
    return READY_SELECTOR_DETAIL if mode == "detail" else READY_SELECTOR_LISTING


def min_matches(mode: str) -> int:
    return MIN_CARD_MATCHES_DETAIL if mode == "detail" else MIN_CARD_MATCHES


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS_DETAIL if mode == "detail" else CONTENT_TIMEOUT_MS


# ---------------------------------------------------------------------------
# The lazy-load scroll — the only way any car is ever seen
# ---------------------------------------------------------------------------
# §8's rule, and every clause of it is confirmed by the measurement below.
# Ten scroll rounds on /used-cars-in-delhi-ncr/s/, 2026-09-11:
#
#   cards   22  42  82 122 142 162 182 182 222 242 302
#   added       +20 +40 +40 +20 +20 +20  +0 +40 +20 +60
#   height    8004 11936 15037 17434 18933 20770 21328 24427 27064 32008
#
#   * Scroll to `document.body.scrollHeight`, never a fixed wheel distance.
#     The page grew from 8,004 to 32,008 px in ten rounds; a fixed distance
#     would fall behind on the third.
#   * **Require the count AND the height to hold still for THREE rounds.**
#     Round 7 added ZERO cards and round 8 added 40. A one-round stability
#     test would have stopped at 182 of 1559.
#   * Growth is in multiples of 20 — the site's own batch size.
SCROLL_PAUSE_MS = 2_200
SCROLL_STABLE_ROUNDS = 3
# The site's own listing page size: its API is called with `size=20`, and
# every non-zero round above added a multiple of 20.
CARDS_PER_PAGE = 20
# A ceiling on rounds, so a page that grows forever cannot hold a run
# forever. Generous rather than tight: reaching 1559 cars at 20 a round
# needs about 78, and a caller asking for that many batches is asking for
# that many rounds.
SCROLL_ROUNDS_CAP = 240


def scroll_rounds_for(pages: int) -> int:
    """How many scroll rounds a `--pages N` request is worth.

    Two per batch plus the stability tail, because a round can legitimately
    add nothing (round 7 above) and because a round sometimes adds two
    batches at once. Capped, so a malformed page cannot run forever.
    """
    want = max(1, pages) * 2 + SCROLL_STABLE_ROUNDS + 2
    return min(want, SCROLL_ROUNDS_CAP)


def target_cards(pages: int) -> int:
    """How many cars `--pages N` is asking for."""
    return max(1, pages) * CARDS_PER_PAGE


def wait_for_count(count: Callable[[str], int],
                   sleep: Callable[[int], None],
                   selector: str,
                   want: int,
                   timeout_ms: int,
                   poll_ms: int = 500) -> int:
    """Poll until `count(selector) >= want`, or the timeout. Returns the count.

    A POLL rather than the driver's own wait-for-predicate, and this is not a
    style choice. Playwright's `wait_for_function` hands the browser a STRING
    to evaluate, which a site whose Content-Security-Policy has no
    `unsafe-eval` refuses outright — on a sibling site that took a live run
    down with exit 1 on the site's most obvious URL. Counting elements goes
    over CDP instead (`querySelectorAll` through the protocol, not through
    eval), so it works under any CSP and spells the same in all three
    drivers.

    The count is returned rather than a bool so a caller can say how close it
    got, and a timeout is not an error: a listing with genuinely no cars on
    it never reaches `want`, and that is exit 4 rather than a fault.
    """
    waited = 0
    found = 0
    while True:
        try:
            found = count(selector)
        except Exception as exc:                    # a driver-level fault
            logger.warning("could not count %r: %s", selector, exc)
            return found
        if found >= want:
            return found
        if waited >= timeout_ms:
            return found
        sleep(poll_ms)
        waited += poll_ms


def scroll_until_settled(count: Callable[[str], int],
                         page_height: Callable[[], Optional[int]],
                         scroll_to_bottom: Callable[[], None],
                         sleep: Callable[[int], None],
                         selector: str = READY_SELECTOR_LISTING,
                         rounds: int = SCROLL_ROUNDS_CAP,
                         pause_ms: int = SCROLL_PAUSE_MS,
                         stable_rounds: int = SCROLL_STABLE_ROUNDS,
                         want_cards: Optional[int] = None) -> dict:
    """Scroll a listing until it stops growing or the target is reached.

    Pure policy: every browser operation arrives as a callable, so this runs
    identically under all three drivers and is testable with the browser
    stubbed out.

    Returns, for the sidecar:

        settled       the grid stopped growing — the listing is exhausted
        reached_target  `want_cards` was reached; there is probably more
        rounds        how many rounds were spent
        cards         how many cars ended up in the DOM
        boundaries    the card count after each round, which is what lets
                      `product_parser.parse_products` say which BATCH each
                      row came from without re-parsing a 15 MB document
                      once per round

    `settled` False and `reached_target` False together mean the round budget
    ran out with the page still growing — the run is PARTIAL and must say so,
    because a listing that was still loading when we stopped is not an
    exhausted one.
    """
    boundaries: List[int] = []
    stable = 0
    prev = None
    reached = False
    for i in range(rounds):
        try:
            found = count(selector)
        except Exception as exc:                    # a driver-level fault
            logger.warning("scroll round %d could not count: %s", i, exc)
            break
        height = page_height()
        boundaries.append(found)
        same = prev is not None and found == prev[0] and height == prev[1]
        stable = stable + 1 if same else 0
        logger.info("scroll round %d: %d cards, height %s, stable %d",
                    i, found, height, stable)
        prev = (found, height)
        if want_cards is not None and found >= want_cards:
            reached = True
            break
        if stable >= stable_rounds and found >= MIN_CARD_MATCHES:
            return {"settled": True, "reached_target": False,
                    "rounds": i + 1, "cards": found, "height": height,
                    "boundaries": boundaries}
        scroll_to_bottom()
        sleep(pause_ms)
    return {"settled": False, "reached_target": reached,
            "rounds": len(boundaries),
            "cards": prev[0] if prev else 0,
            "height": prev[1] if prev else None,
            "boundaries": boundaries}


def batches_delivered(rows_collected: int) -> int:
    """How many batches of 20 a finished run actually holds.

    `pages_completed` in the sidecar, and it is derived from the ROWS IN THE
    FILE rather than from the scroll's own last count. That distinction is
    not pedantry: the scroll stops the moment its target is reached, and the
    page keeps hydrating for the fraction of a second before the snapshot is
    taken, so the two numbers differ. Three engines run against the same
    listing on 2026-09-11 all wrote 62 rows, and their scroll counters read
    62, 42 and 62 — so a sidecar built on the counter reported "3 batches"
    beside a file holding four batches' worth of cars, and the three engines
    disagreed about a run they had otherwise performed identically.

    Not derived from the ROUNDS either: a round can add nothing, one batch,
    two or three, so counting rounds reports a number with no relation to
    how much of the catalogue is in the file.
    """
    if rows_collected <= 0:
        return 0
    return max(1, -(-rows_collected // CARDS_PER_PAGE))      # ceil


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """The page's state, in one place, for all three engines.

    `status` is OPTIONAL, and that default is load-bearing rather than tidy.
    Playwright hands back a response object with a status on it; pyppeteer
    and Selenium do not expose one at the point this is called, so they pass
    only the markup. When `status` was required positionally in a sibling
    repo, both of those engines crashed with `TypeError` on their FIRST
    fetch — and that was invisible to import, to `--help`, to `compileall`,
    to the AST undefined-name walk and to 426 green offline checks, because
    none of them calls a function the way a live run does.

    `test_engines` checks every `page_flow.*` call in every engine against
    this module's real signatures, which is the general form of that bug.
    """
    if html is None:
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as five copies of an
# if-chain, so an engine cannot quietly disagree with its twins about whether
# a page is worth retrying or worth paying for.
#
#   retry     fetch it again — a different exit if there is a pool
#   solve     spend money on a captcha here
#   blocked   contributes to exit 3
#   parse     hand the html to the parser
#   usage     the caller's URL is wrong — exit 2, naming it
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"retry": False, "solve": False, "blocked": False,
                  "parse": True,  "usage": False},
    # A zero-result filter, a cityless URL, an SEO landing page. A real
    # answer to the question that was asked.
    "empty":     {"retry": False, "solve": False, "blocked": False,
                  "parse": True,  "usage": False},
    # Spinny's own 404. Retrying it from another exit cannot help — the URL
    # does not exist — so this is the caller's problem and is reported as
    # one.
    "notfound":  {"retry": False, "solve": False, "blocked": False,
                  "parse": False, "usage": True},
    # Nothing arrived, or something Spinny did not build arrived. No page to
    # solve, but a different exit is worth a try.
    "blocked":   {"retry": True,  "solve": False, "blocked": True,
                  "parse": False, "usage": False},
    # Never observed on this site — see `recaptcha_note` below. If Spinny
    # ever renders the widget it already loads, this is the state that would
    # pay for it, and `--solve-captcha when-blocked` still gates the spend
    # on there being no cars on the page.
    "challenge": {"retry": True,  "solve": True,  "blocked": True,
                  "parse": False, "usage": False},
    # Served by Spinny, no grid, no zero-count heading. On this site that is
    # the SHELL — the first response to every listing URL — and it wants a
    # wait, not a retry and not a rotation. See `is_unpainted`.
    "unknown":   {"retry": True,  "solve": False, "blocked": False,
                  "parse": False, "usage": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


def is_usage_error(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["usage"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is "not painted yet" rather than actually wrong.

    This is the distinction that decides whether the primary path works at
    all on this site, because **Spinny's first response is ALWAYS a shell.**
    Measured 2026-09-11: `domcontentloaded` on /used-cars-in-delhi-ncr/s/ at
    1.1 s with zero cars in the DOM, first batch of 22 at 4.7 s, delivered by
    an `api.spinny.com` XHR after load. The shell is 1.6 to 2 MB of
    navigation, filter rail and footer with a grid container and nothing in
    it.

    §8 says missing content is one of three things — not painted,
    lazy-loaded, or a different page served to this session. On Spinny every
    listing starts as the first and continues as the second, and neither
    wants a retry: a page the site plainly served, with no zero-count
    heading on it, is waiting for its XHR. Retrying spends the user's retry
    budget on a page that would have painted, and rotating the exit throws
    away a session that was working.
    """
    return state == "unknown" and bool(html) and served_by_spinny(html)


def recaptcha_note() -> str:
    """Why a reCAPTCHA on the page is not a reason to spend anything.

    Spinny loads **reCAPTCHA Enterprise v3, invisible**, on every page it
    serves — `recaptcha/enterprise.js?render=6Lc_rqYo…`, a badge the site's
    own CSS hides, and a `g-recaptcha-response` textarea in the DOM. There
    were 31 occurrences of "recaptcha" on a page that had just served 482
    cars.

    v3 does not challenge; it scores. Nothing is rendered for a visitor to
    solve and there is nothing for a solver to buy unless the site starts
    gating the listing on a score — which it has not been observed doing from
    either exit tested. So `recaptcha` and `g-recaptcha` are deliberately NOT
    in `product_parser.BOT_CHALLENGE_MARKERS`: a marker that matches every
    page of the site it is meant to guard is worse than no marker (§18).

    Returned as a string rather than logged here so an engine can print it
    once, where a user asking "why didn't it solve the captcha" will see it.
    """
    return ("Spinny loads reCAPTCHA Enterprise v3 (invisible) on every page "
            "it serves — it scores the session rather than challenging it, so "
            "there is nothing rendered to solve and --solve-captcha has "
            "nothing to buy. A v3 score task is what a 2Captcha key would be "
            "spent on IF the site ever starts gating the grid on one; it has "
            "not been observed doing so.")


# A blocked page is worth retrying, and on this site that is a statement
# about proxies rather than about Spinny: no refusal was ever observed from
# either exit tested (an Amsterdam datacentre address and a Chennai
# residential one both got the full grid), so the `blocked` state here is
# far more likely to be a dead proxy exit than a scored address. Rotating is
# exactly the right response to that.
#
# CONSULTED BY THE ENGINES, not decorative — §17 found a sibling repo's
# identical constant with a paragraph of justification and no reader. The
# offline suite asserts that every engine reads this one.
RETRY_ON_BLOCKED = True
# Without a pool there is only one address to try, so retrying it mostly
# spends time. One extra attempt, because a single failed navigation is often
# the session rather than the address.
BLOCK_RETRIES_WITHOUT_POOL = 1

# Never spend more than this on one page, whatever the retry budget says. No
# challenge has ever been observed on this site, so any solve here is
# speculative and the cap keeps a speculative path from becoming a bill.
SOLVES_PER_PAGE = 1


def block_retry_budget(has_pool: bool, retries: int) -> int:
    """How many extra attempts a blocked page gets.

    With a pool, the user's `--retries` budget, because each attempt is a
    different exit and a different exit is the fix. Without one,
    `BLOCK_RETRIES_WITHOUT_POOL`, because the same address will answer the
    same way.

    A function rather than an engine-local expression so all three engines
    cannot disagree, and so `RETRY_ON_BLOCKED` has a reader.
    """
    if not RETRY_ON_BLOCKED:
        return 0
    return max(0, retries) if has_pool else BLOCK_RETRIES_WITHOUT_POOL


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# The most important thing in this file, and the one a reader is most likely
# to "fix" wrongly.
#
# **A Spinny listing has one page and `?page=N` is silently ignored.**
# Measured 2026-09-11 through a real browser:
#
#   /used-cars-in-delhi-ncr/s/           first ids 31483859, 31064888, ...
#   /used-cars-in-delhi-ncr/s/?page=2    first ids 31483859, 31064888, ...
#   /used-cars-in-delhi-ncr/s/?page=3    first ids 31483859, 31064888, ...
#
# Identical, under HTTP 200. Not an error and not an empty result — page one,
# three times. And the page publishes no pagination markup at all: zero
# `link[rel=next]`, zero `page=` hrefs, no "Load more" and no numbered
# anchors on a fully scrolled 15 MB capture.
#
# This is §7's silent-single-page failure in its most dangerous form. A
# `page_url()` used unconditionally would fetch page one N times, find no new
# id after the first, conclude the listing was exhausted, and report a
# COMPLETE run holding a twentieth of a Delhi catalogue. Three of §7's four
# layers pass it: the selector layer finds nothing (correct), the constructed
# -URL layer builds something that returns 200 (wrong), and only "this page
# added no new sku" notices.
#
# So the engines scroll one long page, and `--concurrency` above 1 is
# refused with that reason.
NEXT_PAGE_SELECTOR: List[str] = [
    # Ordered most-durable first, per §5 — standards-based signals before
    # build artefacts. NONE of these has ever matched on Spinny: the list is
    # here so that if the site ever grows real pagination markup the engines
    # pick it up, and every entry is honestly marked as unobserved.
    'link[rel="next"]',            # unobserved: 0 on all captures
    'a[rel="next"]',               # unobserved
    '[data-id="next-page"]',       # unobserved; named after the site's own
                                   # data-id convention, so it is the shape a
                                   # real one would take
]


def next_page_selector(page_num: int = 1) -> str:
    return ", ".join(NEXT_PAGE_SELECTOR)


def pagination_is_addressable(page1_url: str,
                              advertised_hrefs: Optional[List[str]] = None) -> bool:
    """Whether page N of this listing can be fetched without fetching N-1.

    Always False on Spinny. See the block above — measured, not cautious, and
    the measurement is that `?page=2` returns page 1 under HTTP 200.

    An engine that gets False here must fetch one long page and scroll it,
    must not plan page URLs, and must not run workers concurrently.
    """
    if not paginates_by_url(page1_url):
        return False
    # Unreachable on this site today. Kept, with its agreement check intact,
    # so that a Spinny deploy which introduces real pagination is picked up
    # by changing `product_parser.PAGINATED_KINDS` alone.
    if not advertised_hrefs:
        return True
    built = page_url(page1_url, 2)
    if built is None:
        return False
    want = strip_tracking(built)
    return any(strip_tracking(h) == want for h in advertised_hrefs if h)


# `pagination_agrees` and `next_page_candidates` were HERE and are gone on
# purpose. They are the family's "follow the site's own next-link, but only
# when it agrees with the URL convention" pair, and on this site neither can
# ever return anything: `page_url` yields None above page 1, and no capture
# of Spinny has ever carried a next-link to filter. Kept, they would have
# been two policy functions with no consumer — the defect §17 names, where
# the prose reads like enforcement and nothing enforces it.
#
# What survives is the pair that DOES have a reader in all three engines:
# `pagination_is_addressable` above, which the engines consult before
# assuming they may only scroll, and `next_page_selector`, which each engine
# runs against the live page so that markup Spinny has never published would
# be noticed the day it appears.


def page_param_warning(url: str) -> Optional[str]:
    """A warning if the caller put `?page=N` in the URL, else None.

    Worth saying out loud rather than ignoring: a user who has read any other
    scraper's README will try it, the site will answer 200, and without this
    they will believe they got page 4.
    """
    n = requested_page_in_url(url)
    if n is None:
        return None
    return (f"the URL carries ?page={n}, and Spinny IGNORES it — pages 1, 2 "
            f"and 3 of one listing URL return byte-identical first cards "
            f"under HTTP 200. Use --pages {n} instead, which scrolls "
            f"{target_cards(n)} cars into one page.")


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------
def completeness(rows_collected: int, html: Optional[str],
                 scroll: Optional[dict] = None) -> dict:
    """Whether the run got everything the listing says it has.

    §8's "where the site publishes totals, a gap is arithmetic" — and Spinny
    does publish one, beside its own heading: "1559 Used cars in Delhi NCR".
    So 482 rows against an advertised 1559 is PROOF the scroll stopped early,
    with no threshold and no guess involved.

    Returns the numbers rather than a verdict, so the engine can put them in
    the sidecar and a canary can assert on them. `short_by` is None when the
    site advertised no total, which is not the same as zero.
    """
    advertised = total_available(html or "")
    out = {
        "total_available": advertised,
        "rows_collected": rows_collected,
        "short_by": None if advertised is None else max(0, advertised - rows_collected),
        "scroll_settled": (scroll or {}).get("settled"),
        "reached_target": (scroll or {}).get("reached_target"),
    }
    if advertised is not None and out["short_by"]:
        logger.info(
            "the listing advertises %d cars and this run holds %d — short by "
            "%d. That is arithmetic, not a guess: ask for more --pages, or "
            "narrow the listing with one of Spinny's own filter URLs.",
            advertised, rows_collected, out["short_by"])
    return out


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str) -> Optional[int]:
    """The most workers this URL can usefully be given, or None for no limit.

    1 for every URL on this site, and the reason is structural rather than
    cautious: a worker cannot be handed "page 5" because page 5 has no
    address — `?page=5` returns page 1. Reaching the 300th car means
    scrolling past the first 299 in one session, so there is nothing to
    parallelise and N workers would fetch page 1 N times from N addresses.

    Refuse it with that reason rather than accepting it silently — a flag
    that appears to work and does nothing is worse than one that says no.
    """
    return None if paginates_by_url(url) else 1


def concurrency_refusal(url: str) -> Optional[str]:
    """Why --concurrency cannot be honoured for this URL, or None if it can."""
    if concurrency_limit(url) != 1:
        return None
    kind = listing_kind(url)
    if kind in ("listing", "cityless"):
        what = ("a Spinny listing has no per-page addresses — it is one "
                "infinitely scrolling page, and ?page=N on it is silently "
                "IGNORED: pages 1, 2 and 3 of the same listing URL return "
                "byte-identical first cards under HTTP 200")
    elif kind == "hub":
        what = ("/used-cars/ is an SEO landing page with no car grid on it, "
                "so there are no pages to divide")
    elif kind == "detail":
        what = "a detail page is one car, so there is nothing to divide"
    else:
        what = ("this URL does not paginate by address, so page N cannot be "
                "fetched without fetching N-1 first")
    return (what + ". Workers would each re-fetch the same page. Ask for more "
            "--pages instead, which scrolls further into the one page Spinny "
            "serves, or split the work across Spinny's own filter URLs "
            "(/used-{make}-cars-in-{city}/s/, /used-cars-under-{n}-lakh-rs-in-"
            "{city}/s/), which ARE separate addresses.")


def comparable(url: str) -> str:
    """A URL reduced to what identifies the page, for dedupe and comparison."""
    return strip_tracking(url)
