"""
spinny-scraper — Playwright edition (primary engine)
====================================================

Scrapes Spinny used-car listings and car detail pages.

    --mode listing   (default)  a city or filtered listing
                                (/used-cars-in-{city}/s/,
                                 /used-{make}-cars-in-{city}/s/, ...)
    --mode detail               one /buy-used-cars/.../{id}/ page, read from
                                the page's own ["Product","Car"] JSON-LD: the
                                EXACT odometer reading, the previous-owner
                                count, the colour, the seating capacity, the
                                registration year and month, and the
                                insurance validity and type

There is deliberately no `--country` flag. Spinny is one storefront on one
hostname serving one country in one currency — the same listing URL fetched
from an Amsterdam datacentre exit and from a Chennai residential exit
returned the same page, the same advertised total and INR prices both times
(measured 2026-09-11) — so a country flag could only disagree with the URL.
What varies is the CITY, and the city is part of the URL.

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money; the shared decisions live
in output_writer.finish_run() and page_flow.py so they cannot drift apart.

What is different about Spinny
------------------------------
* **`--pages N` scrolls; it does not fetch N URLs.** `?page=N` on a Spinny
  listing is silently IGNORED — pages 1, 2 and 3 of
  /used-cars-in-delhi-ncr/s/ returned byte-identical first cards under HTTP
  200, and the page publishes no rel=next, no numbered anchors and no "load
  more" button anywhere in a fully scrolled 15 MB capture. A listing is ONE
  infinitely scrolling page that hydrates 20 cars at a time, so `--pages N`
  means N batches of 20 and the engine reaches them by scrolling. A planner
  that built `?page=N` would fetch page one N times, find no new id, and
  report a COMPLETE run holding a twentieth of the catalogue — §7's
  silent-single-page failure with the site answering 200 the whole way.
* **`--concurrency` above 1 is refused, and this engine ships no worker
  pool.** Page 5 of an infinitely scrolling grid has no address to hand a
  worker, so the machinery would be dead code that looks load-bearing. The
  flag is accepted and says no, naming what to do instead: Spinny's own
  filter URLs (/used-{make}-cars-in-{city}/s/) ARE separate addresses and
  several runs across them parallelise properly.
* **The first response is always a shell.** domcontentloaded at 1.1 s with
  ZERO cars in the DOM; the first batch of 22 arrives at 4.7 s from an
  `api.spinny.com` XHR. So the scroll is not an optimisation, it is the only
  way any car is ever seen — and "no grid yet" has to mean WAIT rather than
  retry. See page_flow.is_unpainted.
* **A listing page has no structured data about its cars.** Five JSON-LD
  blocks and not one names a vehicle: a BreadcrumbList, a LocalBusiness for
  the hubs, a FAQPage, and a marketing Product whose aggregateRating rates
  SPINNY. So `price_source` is "dom+attr" on every listing row. A DETAIL page
  is different and carries a real ["Product","Car"] block — which spells the
  price `offers.Price`, with a capital P, so the standard reader returns None.
* **Four prices per car, disagreeing by up to ₹300,000.** The card's
  headline ("3.88 Lakh") excludes RC transfer and insurance; the
  `data-price` attribute (392,000) includes them; the strike price is the
  pre-discount figure on the headline's basis; the badge ("₹13,000") is the
  discount in exact rupees. Read the note at the top of output_writer.py
  before touching any of them — mixing two bases computes a negative
  discount that looks entirely plausible.
* **reCAPTCHA Enterprise v3 loads on every page and challenges nothing.**
  31 occurrences of "recaptcha" on a page that had just served 482 cars, an
  invisible widget with a hidden badge. v3 scores rather than challenges, so
  there is nothing to solve and `--solve-captcha` has nothing to buy — and
  `recaptcha` is deliberately NOT a block marker, because a marker matching
  every page is worse than no marker (§18).
* **No refusal was ever observed.** An Amsterdam datacentre address and a
  Chennai residential address both got the full grid with no key and no
  proxy. So exit 3 on this site is far more likely to be a dead proxy exit
  than a scored address, and `served_by_spinny` — the inverted check — is
  what tells Chromium's own error page apart from a real one.
* **Class names are a design-system vocabulary.** `ds-body-small` wraps the
  fuel type, the transmission, the RTO code and the hub name identically, so
  a class cannot tell one field from another. Read the top of
  product_parser.py before touching any extraction.

Usage
-----
    python playwright_scraper.py \\
        --url "https://www.spinny.com/used-cars-in-delhi-ncr/s/" \\
        --pages 5 \\
        --format both

    python playwright_scraper.py \\
        --url "https://www.spinny.com/used-luxury-cars-in-delhi-ncr/s/"

    python playwright_scraper.py --mode detail \\
        --url "https://www.spinny.com/buy-used-cars/faridabad/hyundai/grand-i10/sportz-12-kappa-vtvt-2019/31483859/"

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urljoin

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from product_parser import (parse_products, parse_detail_page,
                            car_metadata, detect_bot_challenge,
                            listing_kind, site_host, is_supported_host,
                            listing_heading, unsupported_reason,
                            served_by_spinny, unknown_city_warning,
                            recaptcha_config)
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chromium
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all actually report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes. Two reasons, and the second is the point:
    dedupe that mutates a running set inside the loop makes the OUTPUT depend
    on the order pages happen to arrive in — fine while that order is fixed,
    wrong the moment pages are fetched concurrently, because which page
    "claims" a duplicate sku (and so which `scraped_at` the row carries)
    would vary between runs of the same command. Merging afterwards in page
    order is deterministic regardless of arrival order.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The page_flow state this page came back as ("content", "blocked",
    # "challenge", "empty", "unknown"). Carried so the caller can tell an
    # EMPTY page — a /p/<slug> hub, a no-match query, or one page past the
    # end of a listing — from a page that failed. Both produce zero rows and
    # they mean opposite things.
    state: Optional[str] = None
    # What the listing itself says its catalogue size is, read off Spinny's
    # own heading ("1559 Used cars in Delhi NCR"). This site DOES publish
    # one, which makes §8's completeness check arithmetic rather than a
    # threshold: 482 rows against an advertised 1559 is proof the scroll
    # stopped early. Note the number in the <title> disagrees (1546 against
    # 1559 on one capture) — the heading is the one the site's own API
    # agrees with.
    total_available: Optional[int] = None
    # That heading verbatim, for the sidecar. Worth recording beside the
    # count because it names what the listing actually selected — "Used
    # Luxury cars in Delhi NCR" — which is not always what the URL slug
    # suggests.
    header: Optional[str] = None
    # What the lazy-load scroll did: rounds spent, cards reached, the card
    # count after each round, and whether it SETTLED or merely reached the
    # target. Those two flags are the important ones — a page whose grid was
    # still growing when the round budget ran out is partial, and a run that
    # reported it as complete would read as a shrinking catalogue.
    scroll: Optional[dict] = None
    # The advertised total against what this run holds, from
    # page_flow.completeness. Kept whole rather than reduced to a boolean:
    # "482 of 1559" is actionable and "incomplete" is not.
    completeness: Optional[dict] = None
    # In --mode detail, the run-level facts about the one car this run
    # covers: its canonical URL, the size of its inspection report and the
    # insurance validity and type off the overview table. Stored as the
    # small dict rather than by keeping the page's HTML around — a detail
    # page is 2.1 MB and a scrolled listing 15.
    car_facts: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The lowest PRICE coverage that is still healthy. Not a structured-price
# confirmation share, because a Spinny LISTING carries no structured price to
# confirm against — five JSON-LD blocks and not one names a vehicle — so
# `price_source` is "dom+attr" on every listing row and a confirmation
# threshold would describe nothing.
#
# What IS worth a floor is the plain share of rows that got a price at all:
# 705 of 705 across three captures, because every card prints one. Anything
# below 98% means the read broke rather than the page being unusual.
PRICE_FLOOR = {"listing": 98, "cityless": 98, "detail": 100}

# The two price bases, checked against each other on every run. The all-in
# attribute figure was HIGHER than the displayed one on 482 of 482 Delhi
# tiles, because it includes RC transfer and insurance. A row where it is
# lower means the two reads have been crossed — which is the mistake that
# produces a negative discount, and the one worth a warning on every run
# rather than only in the offline suite.
ALL_IN_ABOVE_DISPLAYED_FLOOR = 98


# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a fresh session is the only fix — lives in page_flow.py so all
# three engines make it identically. What lives here is only HOW to ask this
# particular driver. See page_flow's docstring for why that split exists.
def _driver(page):
    # The scroll primitives are NAMED OPERATIONS rather than JavaScript, and
    # that is the point of the split. Selenium's execute_script takes a
    # function BODY with an explicit `return` while Playwright and pyppeteer
    # take `() => expr`, so a shared module handing JS across this boundary
    # would quietly acquire one driver's dialect.
    return {
        "count": lambda selector: len(page.query_selector_all(selector)),
        "sleep": page.wait_for_timeout,
        "content": lambda: _content_when_settled(page),
        "current_url": lambda: page.url,
        "page_height": lambda: _page_height(page),
        "scroll_to_bottom": lambda: _scroll_to_bottom(page),
    }


def _page_height(page) -> Optional[int]:
    try:
        return int(page.evaluate("() => document.body.scrollHeight"))
    except (PWError, PWTimeout, TypeError, ValueError):
        return None


def _scroll_to_bottom(page) -> None:
    """Scroll to the document's own bottom, not a fixed wheel distance.

    A fixed distance falls behind a page that grows as it loads, and this
    grid grows a lot: a Delhi listing scrolled from one screen to a 15 MB
    document over 60 rounds, adding roughly 20 cars each time. A sibling
    repo's 2400px wheel stopped three rounds short of a 7600px grid and
    never reached the trigger; there is no fixed distance that would have
    worked here at all.
    """
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    except (PWError, PWTimeout):
        pass


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args) -> int:
    return page_flow.min_matches(args.mode)


def _classify(page, html: str, status=None) -> str:
    return page_flow.classify(html, status=status, url=page.url)

# Every readiness constant, every pagination selector and every state policy
# lives in page_flow.py, with its measurement beside it. Nothing about WHAT
# to do with a page is duplicated here — this file only knows HOW to ask
# Playwright.


def _advertised_next_hrefs(page, page_num: int = 1) -> List[str]:
    """Every href on the page that could be a link to the next page.

    On Spinny this returns NOTHING, and that is measured rather than broken:
    a fully scrolled 15 MB listing capture publishes no `link[rel=next]`, no
    `a[rel=next]`, no href containing `page=` and no next-page button — the
    grid is one infinitely scrolling page.

    The function is kept, and CALLED on every listing fetch, for one reason:
    it turns `page_flow.NEXT_PAGE_SELECTOR` from a documented constant that
    nothing reads into a live check. If Spinny ever grows real pagination
    markup, this logs it loudly and names the module to revisit — which is
    better than a constant with a paragraph of justification and no reader
    (§17 found exactly that in a sibling repo).
    """
    selector = page_flow.next_page_selector(page_num)
    return [el.get_attribute("href")
            for el in page.query_selector_all(selector)]


def _check_for_new_pagination(page, page_num: int = 1) -> None:
    """Log it if the site has grown the pagination markup it has never had.

    Not an error and not a behaviour change: the run continues to scroll.
    But a match here means the single most important assumption in this repo
    has changed, and finding that out from a log line beats finding it out
    from a run that silently takes a twentieth of a catalogue.
    """
    hrefs = [h for h in _advertised_next_hrefs(page, page_num) if h]
    if not hrefs:
        return
    logger.warning(
        "This page advertises %d next-page link(s) — %s. Spinny has never "
        "published any (0 matches on a fully scrolled 15 MB capture), so if "
        "this is real, the site has grown pagination and "
        "product_parser.PAGINATED_KINDS / page_flow's pagination block need "
        "revisiting. This run still scrolls.",
        len(hrefs), ", ".join(hrefs[:3]))


def _same_url(a: str, b: str) -> bool:
    """Whether two URLs address the same page.

    Delegates to page_flow rather than reimplementing the comparison, so all
    three engines cannot drift on it. An engine that carried its own copy of
    this in a sibling repo went stale and silently fell back to sequential
    fetching — the exact divergence page_flow.py exists to prevent,
    reproduced inside one engine.

    Used to notice that the page answering is not the page that was asked
    for. Both sides are stripped of tracking parameters first, so a
    `?utm_source=…` tail is not mistaken for a different listing — and on
    this site the check matters more than it looks, because a redirect to
    another city's listing would change the run's SUBJECT while every row in
    it stayed internally consistent.
    """
    return page_flow.comparable(a) == page_flow.comparable(b)


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",     # nothing listening / refused
    "ERR_TUNNEL_CONNECTION_FAILED",    # CONNECT rejected by the proxy
    "ERR_PROXY_AUTH_UNSUPPORTED",      # auth scheme we cannot satisfy
    "ERR_PROXY_AUTH_REQUESTED",        # credentials missing or wrong
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different exit — retrying it unchanged just
    spends the retry budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    Factored out of scrape() so a proxy rotation can tear the whole browser
    down and call this again. Swapping the proxy under a live session would
    be cheaper and wrong: cookies a bot manager issued against one exit,
    replayed from another, are a stronger signal than either address alone.
    A rotation therefore means a genuinely fresh browser — new cookie jar,
    new storage — which is what an ordinary user on a different network
    looks like.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    # Only override the UA when we launched our own bundled Chromium.
    # Forcing a UA on a page reached via --cdp-endpoint mismatches the remote
    # browser's real TLS/JS fingerprint on purpose-matched values.
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        # Must be installed on the context, before any page script runs.
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale. It also gives a worker thread a single object to own: with
    Playwright's sync API, a browser and everything reachable from it belong
    to the thread that created them, so each worker builds its own.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    # Explicit timeout. Playwright defaults to 30s here, but stating it makes
    # the contract visible next to the pyppeteer twin, which has no connect
    # timeout at all. A Scraping Browser session that is still held answers
    # with HTTP 500 rather than stalling, so this mostly guards against the
    # endpoint going quiet.
    try:
        browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # the endpoint is a URL with the password in it. Unmasked, that
        # password lands in the terminal, in CI output and in any log the run
        # is piped to — which is the one thing this project promises does not
        # happen ("credentials never reach argv or logs"). The message is
        # rewritten with the credentials masked and the host and port kept,
        # because WHICH endpoint failed is the useful half and is not the
        # secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so a 500 here usually means another run still holds this "
            f"`pid`. Wait for it to finish, or use a different pid."
        ) from None
    # Reuse the remote browser's existing context so its
    # fingerprint/session/proxy settings stay intact.
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser: https://2captcha.com/scraper/browser-api/api
    # Tried first when --cdp-endpoint is set; this script's own detect+solve
    # logic still runs as a fallback if the endpoint does not support it.
    # Note what it cannot cover. The reCAPTCHA Enterprise v3 that loads on
    # every Spinny page SCORES rather than challenges — there is no widget
    # to click and nothing to solve — and a dead proxy exit, which is the
    # likeliest cause of a blocked run here, is not a challenge either. No
    # challenge has ever been observed on this site; this is wired up
    # because one can appear between deploys.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info("[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled — supported "
                    "challenge types will be solved automatically if this "
                    "--cdp-endpoint is a Scraping Browser API session.")
    except Exception as e:
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


def _resolve_pagination_url(base_url: str, href: str) -> str:
    """Resolve a pagination link's raw href against the page it came from.

    Playwright's get_attribute("href") returns the raw HTML attribute,
    unresolved — unlike the DOM .href property Puppeteer/Selenium read for
    the same purpose in this project, which the browser resolves for you.
    urljoin handles every shape correctly — absolute, protocol-relative,
    absolute-path, and page-relative hrefs alike.
    """
    return urljoin(base_url, href)


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a Playwright connection
# error repeats the endpoint five times (the message plus a four-line call
# log), so a masker that handled only the first occurrence would print the
# password four times and look like it was working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Takes arbitrary text, not just a URL, because the strings that most need
    this are exception messages with a URL inside them. The host and port are
    KEPT — which endpoint or exit a run used is the useful half of the line
    and is not the secret.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright raises `Page.content: Unable to retrieve content because the
    page is navigating and changing the content` if the document swaps under
    it. Spinny does not geo-redirect — one storefront, measured identical
    from an Amsterdam and a Chennai exit — but every listing arrives as a
    shell and hydrates itself from an api.spinny.com XHR, so a snapshot
    taken right after goto() can land exactly on a swap.

    Retries briefly and returns None if the page won't hold still, so the
    caller can skip a check instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating (a geo-redirect or the consent "
                        "layer?) — retrying content() in %dms (%d/%d).",
                        pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page — not scoped to one URL. The
    static-HTML and runtime reCAPTCHA detectors are run and reconciled
    against each other rather than short-circuited, because they can disagree
    about the variant and the parameters for one are rejected for the other.

    NOTE what this cannot help with, because on this site that is almost
    everything. Spinny loads reCAPTCHA Enterprise **v3** on every page — 31
    occurrences of "recaptcha" on a capture that had just served 482 cars,
    an invisible widget with a hidden badge — and v3 scores traffic rather
    than challenging it. There is no widget to solve and buying a token for
    one would buy nothing. What a blocked run here most likely met is a dead
    proxy exit or Chromium's own error page, which `detect_page_state`
    reports as "blocked" rather than "challenge" precisely so no solve is
    attempted or billed. No rendered challenge has ever been observed on
    this site. This path exists because a bot manager can be switched on
    between deploys, and because the family's rule is that detection stays
    broad: different geos and scenarios surface different challenges.
    """
    html = _content_when_settled(page)
    if html is None:
        # Couldn't get a stable snapshot — skip detection for this navigation
        # rather than taking the whole run down. The next navigation gets
        # another chance, and the parse below reads its own copy of the DOM.
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # products are already rendered guards nothing, and counting the anchors
    # is instant — no wait_for_function, no 20s — which is why this check
    # sits here rather than after the readiness wait. Doing it the other way
    # round would cost 20 wasted seconds on a page the captcha genuinely
    # gates, where solving FIRST is what makes the content appear.
    already_rendered = len(page.query_selector_all(_ready_selector(args)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it. Pass --solve-captcha always to "
                    "solve it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    # Which task type a solve would actually buy, read off the SITE'S OWN
    # loader rather than guessed from the widget. Spinny wires reCAPTCHA
    # Enterprise v3, and an enterprise v3 score task is priced and solved
    # differently from a v2 checkbox — sending v3 parameters for a v2 widget
    # buys a token the site rejects (§8). Logged before the spend, so a
    # rejected token has an explanation next to it in the log.
    configured = recaptcha_config(html)
    if configured:
        logger.info("The site's own reCAPTCHA config says %s%s, sitekey %s.",
                    configured["version"],
                    " enterprise" if configured["enterprise"] == "true" else "",
                    configured["sitekey"] or "(explicit)")
    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page already "
                       "holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int = 1,
                    boundaries: Optional[List[int]] = None) -> List:
    """Rows for this mode, always as a list even when the mode yields one.

    `boundaries` is how a run that scrolled ONE long page still reports a
    meaningful `page` per row: it is the card count observed after each
    scroll round, so a card at position 47 is known to have arrived in batch
    3 without re-parsing a 15 MB document once per round. Without it every
    row would be labelled page 1 — and `position` restarts nowhere, so 482
    rows would carry 482 distinct positions and one meaningless page number.
    A sibling repo shipped exactly that and 60 of 119 rows silently claimed a
    position another row already had.
    """
    if args.mode == "detail":
        return parse_detail_page(html, url, category=args.category)
    return parse_products(html, url, page=page_num, category=args.category,
                          page_boundaries=boundaries)


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a 403 refusal, a captcha page, a dead exit are all recorded on the
    outcome instead. What the run should do about them differs between the
    sequential and concurrent paths, so that decision belongs to the caller
    rather than to a raised exception unwinding through it.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url)

    # How many times a blocked page may be retried.
    #
    # With a pool, each retry moves to a DIFFERENT exit and the budget is the
    # user's `--proxy-block-retries`. WITHOUT one — the ordinary case here,
    # because `--cdp-endpoint` brings its own exit — the retry re-fetches
    # through the same access path, and that is worth doing on this site
    # rather than giving up: a Scraping Browser profile was measured refusing
    # two requests and serving the third. Zero was the family default and it
    # made the first live run of this engine abandon page 1 on its first
    # block without retrying once.
    has_pool = bool(pool and len(pool) > 1)
    # Computed by page_flow, not here, so the three engines cannot disagree
    # about how many attempts a blocked page is worth — and so that
    # `RETRY_ON_BLOCKED` has a READER rather than a paragraph of
    # justification nobody consults. §17 found exactly that constant, unread,
    # in a sibling repo: setting it False changed nothing. Setting it False
    # here really does stop the retry loop.
    block_retries = page_flow.block_retry_budget(
        has_pool, args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        # Retry a navigation timeout rather than ending the run on it. One
        # network flap on page 12 of 50 should not break the loop.
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                # A dead or misconfigured proxy raises PWError
                # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                # catching only the latter lets it escape as a traceback,
                # which is the likeliest failure the first time anyone points
                # --proxy-file at a real list.
                reason = _proxy_failure(e)
                if reason:
                    exit_failed = reason
                    load_failed = True
                    break  # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session.page, args):
            # A solve navigated the page. Give the destination a moment
            # before judging what came back.
            session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        state = _classify(session.page, html)

        # "Not painted yet" is not a fault, and on this site it is not an
        # edge case either: **Spinny's first response is ALWAYS a shell.**
        # Measured 2026-09-11 — domcontentloaded on /used-cars-in-delhi-ncr/s/
        # at 1.1 s with ZERO cars in the DOM, the first batch of 22 arriving
        # at 4.7 s from an api.spinny.com XHR. So every listing fetch passes
        # through here, and classifying that shell naively would make
        # "unknown" the normal state of a healthy page: "unknown" retries,
        # and the run would re-fetch shells until the budget ran out and
        # report 0 rows with exit 4 against a listing that was working.
        #
        # So wait for the anchor and re-classify BEFORE the retry decision.
        # See page_flow.is_unpainted.
        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is the shell Spinny serves first (%d bytes, "
                        "nothing to read yet) — waiting up to %.0fs for it "
                        "to hydrate rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: len(session.page.query_selector_all(sel)),
                session.page.wait_for_timeout,
                _ready_selector(args), _min_matches(args), wait_timeout)
            # `<` and not `<=`. page_flow.wait_for_count returns as soon as
            # the count REACHES the threshold, so `found == threshold` is
            # the success case — reporting it as a timeout printed "still
            # had not painted after 30s" on a detail page that had painted
            # in 1.5 seconds, which sends the reader looking for a fault
            # that is not there.
            if found < _min_matches(args):
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content_when_settled(session.page) or html
            state = _classify(session.page, html)

        # No interstitial-settling step here, and its absence is measured
        # rather than an omission: no refusal of any kind was ever observed
        # on this site. An Amsterdam datacentre exit and a Chennai
        # residential exit both got the full grid, with no key and no proxy,
        # so there is no interstitial to wait out and nothing to reclassify.
        #
        # The paid path is reached only for state "challenge", which NO
        # capture of this site has ever produced — Spinny's reCAPTCHA is
        # Enterprise v3, which scores rather than challenges. It is wired up
        # because a bot manager can be switched on between deploys and a
        # scraper that cannot name what stopped it is much harder to fix —
        # and bounded by SOLVES_PER_PAGE so a speculative path cannot become
        # a bill.
        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _content_when_settled(session.page) or html
                state = _classify(session.page, html)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works. This
                # line is what says whether the money bought anything.
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning(
                        "The solve was NOT accepted: page %d is still %s. The "
                        "purchase is spent.", page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — an SEO landing page has no grid, and a cityless
            # listing URL serves its grid empty — so retrying it would
            # spend the user's budget re-confirming the same right answer,
            # and rotating the exit would blame an address for the URL it was
            # given.
            break

        # Blocked or challenged. A different exit is the one thing that
        # plausibly changes the outcome: whatever went wrong here is a
        # property of the ADDRESS or the connection, not of the URL, so
        # retrying it unchanged would only confirm it. Note that on this
        # site the likeliest cause is not a score at all — no Spinny refusal
        # has ever been observed — but a dead or unauthenticated proxy exit,
        # which is exactly what rotating away from is for.
        if block_attempt < block_retries:
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
            else:
                # No pool, so nowhere else to go — but a plain re-fetch is
                # what clears this on a Scraping Browser profile. The browser
                # is NOT relaunched: over `--cdp-endpoint` a profile allows
                # one live connection, so tearing the session down and
                # reconnecting risks `profile_locked` and would lose the very
                # cookies the retry is meant to build on.
                pause = args.retry_delay * (block_attempt + 1)
                logger.warning("Page %d came back as %s — re-fetching through "
                               "the same access path in %.1fs (%d/%d). On this "
                               "site that is often what clears it.",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # What a caller needs to know here is that this is most likely NOT
        # Spinny refusing them. No refusal was ever observed on this site: an
        # Amsterdam datacentre exit and a Chennai residential exit both got
        # the full grid, with no key and no proxy. So the first thing to
        # suspect is the access path itself — a dead proxy exit, an
        # unauthenticated one, or a Scraping Browser profile another run
        # still holds — and the byte count below is what tells those apart.
        #
        # The dump is written even when it is empty, because "0 bytes" is
        # itself the diagnosis and a reader who finds no file at all cannot
        # tell that from a run that never got here.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        served = served_by_spinny(html or "")
        logger.error(
            "This response was not built by Spinny — %d bytes, %s the site's "
            "own asset hosts, saved to %s. That is the inverted check, and it "
            "is the only one that classifies CHROMIUM'S OWN error page "
            "correctly: a proxy failure renders a page carrying the site's "
            "hostname in its <title> and no vendor marker anywhere, which a "
            "text check calls real. A page Spinny built references "
            "assets.spinny.com 267 to 1040 times; that one references it "
            "zero. No Spinny refusal has ever been observed — a datacentre "
            "and a residential exit both got the full grid with no key and "
            "no proxy — so check the access path before blaming the site: is "
            "the --proxy exit alive and authenticating, and is another run "
            "holding this --cdp-endpoint pid? This is exit 3, distinct from a "
            "genuinely empty listing (exit 4).%s",
            len(html or ""), "which references" if served else "with no "
            "reference to", debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        outcome.blocked_by = "no-response" if not html else "not-served"
        outcome.final_url = session.page.url
        return outcome

    if state == "content":
        # Don't wait for network idle (a listing page never goes fully quiet)
        # and don't accept a single selector match as "ready".
        #
        # Then SCROLL, and on this site the scroll is not an optimisation —
        # it is the only way any car past the first batch is ever seen.
        # Spinny hydrates 20 cars at a time from an api.spinny.com XHR, so
        # without the loop below a listing run returns the 22 cards that
        # happened to arrive first. Measured 2026-09-11: 22 cards at round 0,
        # 302 after ten rounds, against an advertised 1559.
        selector, threshold = _ready_selector(args), _min_matches(args)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        # A POLL, not wait_for_function, which hands the browser a STRING to
        # evaluate and is refused outright by a site whose CSP has no
        # `unsafe-eval` — that took a sibling repo's run down with exit 1 on
        # the site's most obvious URL. Counting through the protocol works
        # under any CSP and spells the same in all three drivers. See
        # page_flow.wait_for_count.
        found = page_flow.wait_for_count(
            lambda sel: len(session.page.query_selector_all(sel)),
            session.page.wait_for_timeout, selector, threshold, content_timeout)
        session.page.wait_for_timeout(500)
        if found < threshold:
            # Not an error on its own, and what it MEANS depends on the
            # mode — which is why the message does too. A listing page with
            # no grid can be a correct answer (a /used-cars/ hub, a cityless
            # URL, a filter nothing matches); a detail page whose price block
            # never painted is a different thing, and on this site it is
            # usually just slow rather than absent, because the row is read
            # out of the page's own ["Product","Car"] JSON-LD rather than out
            # of the rendered buy box.
            if args.mode == "detail":
                logger.info("The price block did not paint within %.0fs. That "
                            "is not fatal: a detail row is read from the "
                            "page's own [\"Product\",\"Car\"] JSON-LD, which "
                            "is in the first response, so the parse below "
                            "decides.", content_timeout / 1000)
            else:
                logger.info("No cars appeared within %.0fs. If this URL is a "
                            "/used-cars/ hub, a cityless listing or a filter "
                            "nothing matches, that is the expected answer and "
                            "the run will report 0 rows (exit 4).",
                            content_timeout / 1000)

        if args.mode != "detail":
            # `--pages N` is a scroll budget on this site, not a URL plan:
            # N batches of 20 cars, reached by scrolling. The rounds budget
            # is deliberately larger than N — a round can add nothing (round
            # 7 of the measurement above added zero and round 8 added forty),
            # so budgeting one round per batch would stop short of what was
            # asked for.
            outcome.scroll = page_flow.scroll_until_settled(
                count=lambda sel: len(session.page.query_selector_all(sel)),
                page_height=lambda: _page_height(session.page),
                scroll_to_bottom=lambda: _scroll_to_bottom(session.page),
                sleep=session.page.wait_for_timeout,
                selector=selector,
                rounds=page_flow.scroll_rounds_for(args.pages),
                want_cards=page_flow.target_cards(args.pages))
            if not (outcome.scroll["settled"] or outcome.scroll["reached_target"]):
                # The round budget ran out with the page still growing and
                # the target not reached. That is a PARTIAL page, not an
                # exhausted one, and saying so is what stops a consumer
                # reading the missing tail as delisted cars.
                logger.warning(
                    "The grid was still growing after %d scroll rounds (%d "
                    "cards, height %s) and had not reached the %d cars "
                    "--pages %d asks for — this page is PARTIAL. Its row "
                    "count is a floor, not the listing.",
                    outcome.scroll["rounds"], outcome.scroll["cards"],
                    outcome.scroll["height"],
                    page_flow.target_cards(args.pages), args.pages)

        html = _content_when_settled(session.page) or html

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is to inspect the exact
    # bytes the parser was given.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only for a state page_flow already counts as BLOCKED, and that
    # narrowing was earned twice.
    #
    # A marker on a page whose cars have rendered guards nothing — that is
    # the "detected is not blocking" rule the captcha default follows,
    # applied to the blocking decision instead of the spending one. But
    # `state != "content"` would still be too wide: an EMPTY page is a
    # correct answer, so this only ever refines the REASON for a page the
    # policy had already given up on.
    #
    # The marker set itself was cut down for the same reason. `recaptcha`
    # and `g-recaptcha` were inherited from the sibling repos and both were
    # REMOVED after counting them on a page known good: Spinny loads
    # reCAPTCHA Enterprise v3 on every page it serves — 31 occurrences on a
    # capture that had just delivered 482 cars — so as markers they would
    # have made every healthy page blocked. See
    # product_parser.BOT_CHALLENGE_MARKERS.
    vendor = (detect_bot_challenge(html, url=session.page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png",
                                    full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s%s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html,
                     (f" (tried {block_retries + 1} exit(s))" if has_pool
                      else f" (re-fetched {block_retries + 1} time(s))"))
        outcome.blocked_by = vendor
        return outcome

    # The scroll's own record of how many cards were in the DOM after each
    # round is what lets a run that scrolled ONE long page still report a
    # meaningful `page` per row: a car at position 47 is known to have
    # arrived in batch 3. Without it every row would be labelled page 1,
    # `position` would run 1..482, and the two columns together would stop
    # identifying a row — which a sibling repo shipped, with 60 of 119 rows
    # claiming a position another row already had.
    boundaries = (outcome.scroll or {}).get("boundaries")
    # Through the POLICY rather than unconditionally. `STATE_POLICY` is the
    # one place that says which states are worth reading, and until now
    # nothing consulted its `parse` column: the engines parsed whatever
    # reached this line, so every state without an earlier `return` was
    # read regardless of what the table said.
    #
    # # Latent here rather than live — measured on this repo's own fixtures on
    # 2026-09-23: no shipped fixture of a parse:False state yields a row,
    # because the parser is independently defensive. The gate is wired
    # anyway, because "the parser happens to return nothing" is not the
    # same guarantee as "the policy says do not read this", and two
    # siblings had exactly this shape turn into phantom rows.
    #
    # Measured across the family on 2026-09-23 by counting definitions
    # against readers: 7 of 24 repos defined `should_parse` and none of
    # them called it.
    products = (_parse_for_mode(html, session.page.url, args, page_num,
                                boundaries=boundaries)
                if page_flow.should_parse(state) else [])
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode == "detail":
        outcome.car_facts = car_metadata(html, session.page.url)

    # Spinny DOES publish a total, beside its own heading — "1559 Used cars
    # in Delhi NCR" — which makes §8's completeness check arithmetic rather
    # than a threshold. Read it here so the sidecar can carry the gap.
    #
    # Two traps, both measured: the number in the <title> disagrees (1546
    # against 1559 on one capture, and the site's own API agrees with the
    # heading), and a listing can advertise more than it will ever render.
    # So the figure goes in the sidecar as what the site CLAIMS, next to
    # what the run actually holds.
    if args.mode == "listing":
        # The tripwire for this engine's central assumption, run on every
        # listing fetch rather than documented and forgotten.
        _check_for_new_pagination(session.page, page_num)
        outcome.completeness = page_flow.completeness(
            len(products), html, outcome.scroll)
        outcome.total_available = outcome.completeness["total_available"]
        outcome.header = listing_heading(html)
        if outcome.header:
            logger.info("The listing's own heading says: %s", outcome.header)

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        kind = listing_kind(session.page.url)
        floor = PRICE_FLOOR.get(kind, 0)
        # Reported every time, not only when it looks wrong, so a consumer
        # gets the number rather than a threshold someone guessed. On Spinny
        # the healthy figure is 100%: 705 of 705 cards across three captured
        # listings carried a price, because every card prints one.
        logger.info("Price coverage on page %d (%s page): %d/%d (%.0f%%); the "
                    "floor for this page kind is %d%%.",
                    page_num, kind, priced, len(products), share, floor)
        if share < floor:
            logger.warning(
                "Only %.0f%% of page %d carries a price, against a measured "
                "floor of %d%% for a %s page. Every row of every captured "
                "page had one, so this is the read breaking rather than the "
                "page being unusual — re-run with --dump-html.",
                share, page_num, floor, kind)

        # There is deliberately NO structured-price confirmation share here,
        # and its absence is measured rather than an omission: a Spinny
        # LISTING page carries five JSON-LD blocks and not one of them names
        # a car, so `price_source` is "dom+attr" on every listing row and a
        # confirmation threshold would describe nothing. Porting the sibling
        # repo's overlay would be dead code that looks load-bearing (§4).
        #
        # What replaces it is the check this site actually needs. There are
        # two price BASES here — the displayed figure excludes RC transfer
        # and insurance, the site's own `data-price` attribute includes them
        # — and the attribute was the HIGHER of the two on 482 of 482 Delhi
        # cards. A row where it is lower means the two reads have been
        # crossed, which is exactly the mistake that computes a negative
        # discount and looks entirely plausible in the output. Checked on
        # every run rather than only in the offline suite, because it is the
        # one that would be silent.
        pairs = [(p.price, p.price_all_in) for p in products
                 if p.price is not None and p.price_all_in is not None]
        if pairs:
            consistent = sum(1 for shown, all_in in pairs if all_in >= shown)
            share = 100.0 * consistent / len(pairs)
            logger.info("All-in price at or above the displayed price on page "
                        "%d: %d/%d (%.0f%%). The attribute includes RC "
                        "transfer and insurance, so it should never be the "
                        "lower of the two.",
                        page_num, consistent, len(pairs), share)
            if share < ALL_IN_ABOVE_DISPLAYED_FLOOR:
                logger.warning(
                    "On %d row(s) of page %d the all-in price is BELOW the "
                    "displayed one. That was true of 0 of 482 measured cards, "
                    "so the two price bases have most likely been crossed — "
                    "re-run with --dump-html before trusting discount_pct.",
                    len(pairs) - consistent, page_num)

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=debug_png, full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s and %s. Open the .png to see it.", debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    if not _same_url(url, outcome.final_url):
        # Not an error — Spinny normalises a listing path and adds its own
        # parameters — but worth saying, because a redirect that lands on a
        # DIFFERENT listing changes what the run is about while every row
        # stays internally consistent and nothing else would notice.
        logger.info("The page that answered is %s, not the URL asked for. "
                    "Check it is still the listing you meant.",
                    outcome.final_url)
    return outcome


# ---------------------------------------------------------------------------
# There is no worker pool in this engine, and its absence is a decision
# ---------------------------------------------------------------------------
# The rest of this family ships `_worker_pool` and
# `_fetch_pages_concurrently`: page N gets handed to a worker holding its own
# browser and its own proxy exit, which is what makes `--concurrency` mean
# anything.
#
# On Spinny there is nothing to hand a worker. `?page=N` is silently ignored
# — pages 1, 2 and 3 of one listing URL return byte-identical first cards
# under HTTP 200 — so every worker would fetch page one, from a different
# address, and the run would pay N times for one page. Reaching the 300th car
# means scrolling past the first 299 in one session.
#
# So the machinery is not here. Shipping it would be dead code that looks
# load-bearing, which §17 names as the same defect as an unenforced policy
# constant — and a `--concurrency 8` that quietly did nothing useful would be
# worse than one that says no. The flag is still accepted, because the
# family's flag contract has it, and `page_flow.concurrency_refusal` explains
# what to do instead: Spinny's own filter URLs
# (/used-{make}-cars-in-{city}/s/, /used-cars-under-{n}-lakh-rs-in-{city}/s/)
# ARE separate addresses, and several runs across them parallelise properly.
#
# If Spinny ever grows real pagination, `product_parser.PAGINATED_KINDS` is
# the one place to change and `_check_for_new_pagination` is what will say so.


def scrape(args) -> int:
    # One entry per page attempted — which is exactly one on this site, in
    # both modes. The list, the page-order merge and the per-page bookkeeping
    # are kept anyway: they are what the family's sidecar is built out of,
    # and they are what would have to come back the day Spinny grows real
    # pagination. See the block above for why the WORKERS did not survive
    # that reasoning and this did.
    outcomes: List[PageOutcome] = []
    blocked = False
    # Both modes are one row per car, so `sku` is the key for both.
    dedupe_key = "sku"

    # The proxy is discarded BEFORE the pool is built, not after. Building it
    # first meant a --proxy this run was about to ignore could still end the
    # run: an unusable entry — a socks5:// URL with credentials, which
    # Chromium cannot authenticate — is a usage error, and
    # `proxy_pool_from_args` correctly refuses it. But with --cdp-endpoint set
    # there is nothing for it to be an error ABOUT, and a `.env` holding a
    # proxy for another engine then blocked every remote run. Found on the
    # first live run over the Scraping Browser API.
    if args.cdp_endpoint and (args.proxy or args.proxy_file):
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        args.proxy, args.proxy_file = None, None
    pool = proxy_pool_from_args(args)

    # --concurrency is accepted and refused, with the reason. Refused rather
    # than ignored because a flag that appears to work and does nothing is
    # worse than one that says no, and the reason names what to do instead.
    if args.concurrency > 1:
        if args.mode == "detail":
            logger.info("--concurrency is ignored in --mode detail: there is "
                        "one page to fetch.")
        else:
            logger.warning("--concurrency %d is not honoured here: %s",
                           args.concurrency,
                           page_flow.concurrency_refusal(args.url)
                           or "this URL cannot be split across workers.")

    # Said once, where someone asking "why didn't it solve the captcha" will
    # see it, rather than buried in a docstring. Only when a key or an
    # explicit --solve-captcha says the user expects to spend something.
    if args.twocaptcha_key or args.solve_captcha == "always":
        logger.info("%s", page_flow.recaptcha_note())

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            outcome = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(outcome)

            # `pagination_is_addressable` is CONSULTED rather than assumed,
            # and it is the tripwire for the one assumption this whole engine
            # rests on. It is False for every URL on this site today. If a
            # deploy ever makes it True, this says so loudly and names what
            # to bring back — which beats discovering it from a run that
            # quietly took one page.
            if page_flow.pagination_is_addressable(
                    outcome.final_url or args.url,
                    _advertised_next_hrefs(session.page)
                    if session.page else None):
                logger.warning(
                    "This listing now looks addressable page by page, which "
                    "Spinny has never been. product_parser.PAGINATED_KINDS "
                    "and the multi-page loop the rest of this family ships "
                    "need bringing back — this run still scrolls one page.")
        finally:
            session.close()

    # Merged in page order rather than arrival order. With one page the two
    # are identical — which is the point of doing it here rather than inside
    # the fetch: the row order is a property of the merge, not of timing.
    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            # Worth a line rather than silence: one scrolled page CAN repeat
            # a car — the grid re-renders as it hydrates — and a large count
            # here means the parse is picking up something twice.
            logger.info("Dropped %d duplicate row(s) by %s.",
                        len(oc.products) - len(fresh), dedupe_key)
        all_rows.extend(fresh)

    # THE RUN STATUS, decided after the merge because one of its cases needs
    # the row count that the merge produces.
    if not outcome.ok:
        stop_reason = ("page_load_timeout" if outcome.load_failed
                       else f"blocked_{outcome.blocked_by}")
        blocked = outcome.blocked_by is not None
    elif args.mode == "detail":
        # A detail page has no page 2, so this is complete by construction.
        stop_reason = "single_page_mode"
    else:
        stop_reason = page_flow.listing_stop_reason(
            outcome.scroll, outcome.completeness, len(all_rows))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = outcome.final_url or args.url

    # How much of the catalogue this run actually holds. Arithmetic, not a
    # threshold: Spinny prints its own total beside its heading, so 482 rows
    # against an advertised 1559 is proof rather than suspicion. The numbers
    # go in the sidecar so a canary can assert on them.
    extra = None
    if args.mode == "detail":
        facts = next((o.car_facts for o in outcomes if o.ok and o.car_facts), None)
        if facts:
            extra = dict(facts)
            if facts.get("parts_evaluated"):
                logger.info("Inspection report: %s parts evaluated by %s "
                            "inspector(s).", facts["parts_evaluated"],
                            facts.get("inspectors") or "?")
    else:
        extra = {"scroll": outcome.scroll,
                 "listing_heading": outcome.header,
                 "completeness": outcome.completeness}
        short_by = (outcome.completeness or {}).get("short_by")
        advertised = (outcome.completeness or {}).get("total_available")
        if advertised:
            logger.info("This listing advertises %d car(s); this run holds %d "
                        "(%.1f%%).", advertised, len(all_rows),
                        100.0 * len(all_rows) / advertised)
        if short_by:
            logger.info("Short by %d against the advertised total. That is "
                        "arithmetic rather than a guess: ask for more --pages, "
                        "or narrow the listing with one of Spinny's own filter "
                        "URLs.", short_by)

    # `pages_completed` counts the BATCHES OF 20 THIS RUN HOLDS — derived
    # from the rows in the file, not from the scroll's own last count and not
    # from the URLs fetched (there is only ever one of those). It can
    # legitimately exceed `pages_requested`, because a single scroll round
    # sometimes delivers two or three batches; the sidecar's `scroll` block
    # is what says how it got there.
    pages_completed = (len(ok_pages) if args.mode == "detail"
                       else page_flow.batches_delivered(len(all_rows)))

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages,
                      pages_completed=pages_completed,
                      pages_failed=failed_pages, mode=args.mode,
                      source=site_host(final_url),
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Spinny scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="Spinny URL: a city listing "
                        "(/used-cars-in-{city}/s/), a filtered listing "
                        "(/used-{make}-cars-in-{city}/s/, "
                        "/used-cars-under-{n}-lakh-rs-in-{city}/s/) or one "
                        "car (/buy-used-cars/.../{id}/) with --mode detail. "
                        "One storefront, one language, one currency — there "
                        "is no locale prefix and no per-country host, and "
                        "the CITY is part of the path rather than a flag. "
                        "Required, unless SPINNY_URL is set in the "
                        "environment or in .env.")
    p.add_argument("--mode", choices=["listing", "detail"],
                   default="listing",
                   help="listing (default): a city or filtered listing, read "
                        "from the rendered grid — 20 cars per batch. detail: "
                        "one /buy-used-cars/.../{id}/ page, read from the "
                        "page's own [\"Product\",\"Car\"] JSON-LD and its "
                        "overview table, which add the EXACT odometer "
                        "reading, the previous-owner count, the colour, the "
                        "seating capacity, the registration year and month, "
                        "and the insurance validity and type. --pages applies "
                        "to listing only; there is one page to read in detail "
                        "mode.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to what the "
                        "listing URL selects with the city removed — \"cars\", "
                        "\"luxury-cars\", \"volvo-cars\", "
                        "\"cars-under-1-lakh-rs\" — so the column is rarely "
                        "empty just because the flag was omitted.")
    p.add_argument("--pages", type=int, default=1,
                   help="How many BATCHES of 20 cars to gather. Applies to "
                        "--mode listing; ignored in --mode detail. This is a "
                        "SCROLL budget, not a URL plan: a Spinny listing has "
                        "no per-page addresses at all — ?page=N is silently "
                        "ignored and returns page 1 under HTTP 200 — so the "
                        "engine reaches batch N by scrolling one long page. "
                        "--pages 5 asks for 100 cars.")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Delay between pages, seconds. Nothing to pace on "
                        "this site, where a run fetches one URL; kept because "
                        "the family's flag contract has it.")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted and REFUSED on this site, with the reason. "
                        "A worker cannot be handed \"page 5\" because page 5 "
                        "has no address, so N workers would fetch page one N "
                        "times from N exits. Split the work across Spinny's "
                        "own filter URLs instead — those ARE separate "
                        "addresses and several runs across them parallelise "
                        "properly.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried — see "
                        "page_flow.STATE_POLICY — because an SEO landing page "
                        "or a cityless URL having no grid is a correct "
                        "answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="spinny_products", help="Output file prefix")
    p.add_argument("--locale", default="en-IN",
                   help="Browser locale (default en-IN, the site's only "
                        "one). It does NOT decide the language or the "
                        "currency: Spinny served the same page and INR prices "
                        "to an Amsterdam and a Chennai exit alike, so this "
                        "only affects what the browser claims about itself.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and blank "
                        "lines skipped) to rotate across. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. per-page: "
                        "a new exit for every page — this is what spreads volume, "
                        "and it relaunches the browser each time so the session "
                        "does not follow the IP around.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do not "
                        "all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back unusable, retry it from this "
                        "many OTHER exits before giving up (default 2). Needs "
                        "a pool of more than one; ignored otherwise. No Spinny "
                        "refusal has ever been observed, so what this most "
                        "often rotates away from here is a dead or "
                        "unauthenticated proxy exit rather than a scored one.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off by "
                        "default so a failed run can't overwrite a good result "
                        "with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's Fingerprint "
                        "API and apply it to the launched browser. Needs "
                        "--twocaptcha-key. Ignored with --cdp-endpoint, where the "
                        "Scraping Browser supplies its own.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" in
    # this family, which the API rejects with HTTP 400 ("Request parameters
    # are invalid"), so --fingerprint failed on every invocation. Measured
    # 2026-09-10: `Windows` succeeds, and `Windows,Chrome,Desktop`, `Chrome`
    # and `Desktop` each 400. fingerprint_client.py's own --tags help has
    # said so all along; the engines' default contradicted it.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400, and no combination is accepted. Use "
                        "--fp-country to narrow further. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair. Applies to both the image "
                        "captcha and reCAPTCHA.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Note what "
                        "there is to buy on this site: NOTHING, so far. "
                        "Spinny loads reCAPTCHA Enterprise v3 (invisible) on "
                        "every page it serves — it SCORES the session rather "
                        "than challenging it — and no rendered challenge has "
                        "ever been observed. The path is wired up because a "
                        "bot manager can be switched on between deploys.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9 "
                        "— the API only accepts these three). Ignored for v2 "
                        "widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP instead "
                        "of launching Playwright's bundled Chromium, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy and --headless/--headful are ignored when this "
                        "is set.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure. Useful when the row count is right but "
                        "a column comes back empty — see TROUBLESHOOTING.md.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and SPINNY_URL is not set in the "
                "environment or in .env.")
    if not is_supported_host(args.url):
        # Refused rather than attempted. The parser's card scoping, its
        # /buy-used-cars/ path pattern, its lakh/crore price words and its
        # "there is no pagination" model are all Spinny's, so pointing this
        # at another site would not fail loudly — it would return zero rows
        # and look like an empty listing.
        why = unsupported_reason(args.url)
        if why:
            # Named WITH THE REASON (§5). "is not a Spinny site" would be
            # false for api.spinny.com and sends the reader hunting for a
            # typo that is not there.
            p.error(f"{site_host(args.url)} {why}.")
        p.error(f"{site_host(args.url) or args.url!r} is not Spinny. This "
                f"scraper reads www.spinny.com, which is the site's only "
                f"storefront — one country, one language, one currency, no "
                f"per-country hostname and no locale prefix.")
    kind = listing_kind(args.url)
    if args.mode == "detail" and kind != "detail":
        p.error(f"--mode detail expects a /buy-used-cars/.../{{id}}/ car URL; "
                f"{args.url!r} is a {kind} page.")
    if args.mode == "listing" and kind == "detail":
        p.error(f"{args.url!r} is a single car page. Use --mode detail for "
                f"it, or pass a listing URL such as "
                f"/used-cars-in-delhi-ncr/s/.")
    if args.mode == "listing" and kind == "hub":
        # A warning rather than an error: it IS a Spinny URL and the run will
        # honestly report zero rows (exit 4). But a reader who tries the
        # obvious URL first would otherwise conclude the tool is broken.
        logger.warning(
            "%s is an SEO LANDING PAGE, not a listing — it has no car grid "
            "on it, so this run will return 0 rows and exit 4. A listing "
            "URL ends in /s/ and names a city: "
            "https://www.spinny.com/used-cars-in-delhi-ncr/s/", args.url)
    if args.mode == "listing" and kind == "cityless":
        # Its own kind rather than a flavour of listing, because it fails in
        # a specific and predictable way: the grid CONTAINER is served and
        # stays empty, since Spinny scopes inventory by city and this path
        # names none. Classified as an ordinary listing it would look like a
        # site outage.
        #
        # And it is worse than empty — it is CONFIDENTLY empty. Measured
        # 2026-09-11: /used-suv-cars/s/ advertises "2549 Used SUV cars in
        # India" and renders none of them; /used-maruti-suzuki-cars/s/
        # advertises 1787 and renders none. So the completeness arithmetic
        # below will report the run as short by the whole national figure,
        # which is true and useless. Add a city.
        logger.warning(
            "%s names no city, and Spinny scopes its inventory by city: the "
            "grid container is served and stays EMPTY, under a heading "
            "advertising the NATIONAL total (2549 for /used-suv-cars/s/, of "
            "which it renders zero). That is the URL rather than a fault, and "
            "this run will report 0 rows (exit 4). Add a city — "
            "/used-suv-cars-in-delhi-ncr/s/ — or pick one of the 33 in "
            "product_parser.CITIES.", args.url)
    if args.mode == "listing" and kind == "unknown":
        logger.warning(
            "%s does not match any Spinny page shape this parser knows "
            "(/used-...-in-{city}/s/ for a listing, /buy-used-cars/.../{id}/ "
            "for a car). The run will go ahead and report what it finds, "
            "which may be nothing.", args.url)
    city_warning = unknown_city_warning(args.url)
    if city_warning:
        logger.warning("%s", city_warning)
    page_warning = page_flow.page_param_warning(args.url)
    if page_warning:
        # The single most likely user error on this site, and the site
        # answers it with HTTP 200, so nothing else would ever say so.
        logger.warning("%s", page_warning)
    if args.mode == "detail" and args.pages != 1:
        # Said out loud rather than silently ignored: a user who passed
        # --pages 5 expects five pages of something.
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read. The run status will say single_page_mode.",
                       args.pages, args.mode)
        args.pages = 1
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch rather "
                       "than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest one:
        # `profile_locked` means another run still holds this `pid`, and a
        # harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
