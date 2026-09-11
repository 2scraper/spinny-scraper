#!/usr/bin/env python3
"""
spinny-scraper — pyppeteer edition (secondary engine)
=====================================================

The same scrape as playwright_scraper.py, driven through pyppeteer. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money — the decisions that determine all three live in page_flow.py
and output_writer.finish_run(), so this file is browser plumbing and nothing
else. Read playwright_scraper.py's docstring for what is different about
Spinny; the two that shape this file are repeated here because they change
what the code looks like:

    --mode listing   (default)  a city or filtered listing
                                (/used-cars-in-{city}/s/)
    --mode detail               one /buy-used-cars/.../{id}/ page

  * **`--pages N` scrolls; it does not fetch N URLs.** `?page=N` on a Spinny
    listing is silently IGNORED — pages 1, 2 and 3 return byte-identical
    first cards under HTTP 200 — so a listing is ONE infinitely scrolling
    page that hydrates 20 cars at a time and `--pages N` means N batches.
  * **No --concurrency, in any engine.** Page 5 of an infinitely scrolling
    grid has no address to hand a worker, so the flag is accepted and
    refused with that reason rather than silently doing nothing.

And two things to know before choosing this engine:

  * **pyppeteer is effectively unmaintained** and its own README points at
    Playwright. It is here for parity, and for anyone who already has it.
  * pyppeteer cannot authenticate a proxy on the command line either, so
    credentials go through `page.authenticate` — never into argv, where
    anything that can run `ps` would read them.

Usage
-----
    python puppeteer_scraper.py \\
        --url "https://www.spinny.com/used-cars-in-delhi-ncr/s/" --pages 5

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
          (pyppeteer downloads its own Chromium on first run)
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional


# At module level, deliberately, and not inside the launch path where it
# started out. The offline suite guards `import puppeteer_scraper` behind
# try/except ImportError and REPORTS the skip, and CI's engine-smoke job fails
# on any reported skip — that whole mechanism only works if importing this
# module actually requires the driver. With the import hidden inside
# _Session.open(), the module imported cleanly with no pyppeteer installed at
# all, the group never skipped, and CI could not have noticed a broken import.
# It also let CI install pyppeteer 0.0.25 (a stub, resolved from an unpinned
# `pip install pyppeteer`) without anything failing, because nothing ever
# imported it.
from pyppeteer import launch, connect

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
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The lowest PRICE coverage that is still healthy. Not a structured-price
# confirmation share, because a Spinny LISTING carries no structured price to
# confirm against — five JSON-LD blocks and not one names a vehicle — so
# `price_source` is "dom+attr" on every listing row and a confirmation
# threshold would describe nothing. What IS worth a floor is the plain share
# of rows that got a price at all: 705 of 705 across three captures.
#
# Kept byte-identical to the Playwright engine's, and the offline suite
# asserts that: three engines quietly disagreeing about what "healthy" means
# is how one of them starts reporting a problem its twins do not.
PRICE_FLOOR = {"listing": 98, "cityless": 98, "detail": 100}

# The two price bases, checked against each other on every run. The all-in
# attribute figure was HIGHER than the displayed one on 482 of 482 Delhi
# tiles, because it includes RC transfer and insurance. A row where it is
# lower means the two reads have been crossed — the mistake that produces a
# negative discount.
ALL_IN_ABOVE_DISPLAYED_FLOOR = 98

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share (how long to wait for
    challenge, when to scroll, when only a fresh session helps) and it is
    written against plain synchronous callables — which is the right shape for
    two of the three drivers. Bridging here keeps the policy in one place
    rather than growing an async copy of it that would drift.

    The second benefit is the one the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which is not something
    pyppeteer's own API offers.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one as "Future exception was never retrieved:
        # NetworkError('Protocol error Target.sendMessageToTarget: Target
        # closed.')" — at ERROR level, AFTER a successful run has printed its
        # results. Five of those under a "Saved 48 products" line read as a
        # failed run. Only that shape is swallowed; anything else still gets
        # the default handler, because silencing the loop wholesale would hide
        # real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH, not one or the other. asyncio puts its own words in
        # `message` ("Future exception was never retrieved") and the library's
        # in `exception` (a NetworkError about a closed CDP session), and an
        # `or` between them looks at the exception and never sees the message
        # — which is why these kept printing after they were "handled".
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                # asyncio's own words when the loop stops with work in
                # flight. Emitted after a successful run; see close().
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                # A CDP message addressed to a session that has gone away.
                # Routine over a remote browser: three of six captures of
                # this site had their target closed mid-scroll and succeeded
                # on the next attempt.
                "No session with given id",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping the loop outright leaves pyppeteer's own background tasks
        pending — its websocket reader and keepalive — and asyncio then prints
        "Task was destroyed but it is pending!" plus a traceback for each of
        them. That happens AFTER the output has been written, so the run is
        fine and the log looks like a crash. Four tracebacks under a
        successful run is how a reader learns to ignore the log.

        Cancelling first is the fix, and it has to happen ON the loop thread —
        `call_soon_threadsafe` is what gets it there.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            if pending:
                logger.debug("Cancelled %d pending pyppeteer task(s) on "
                             "teardown.", len(pending))
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # What the listing itself says its catalogue size is, read off Spinny's
    # own heading ("1559 Used cars in Delhi NCR"). This site DOES publish
    # one, which makes the completeness check arithmetic rather than a
    # threshold.
    total_available: Optional[int] = None
    # That heading verbatim, for the sidecar.
    header: Optional[str] = None
    # What the lazy-load scroll did: rounds spent, cards reached, the count
    # after each round, and whether it SETTLED or merely reached the target.
    # A page whose grid was still growing when the budget ran out is partial,
    # and a run that reported it as complete would read as a shrinking
    # catalogue.
    scroll: Optional[dict] = None
    # The advertised total against what this run holds, from
    # page_flow.completeness.
    completeness: Optional[dict] = None
    # In --mode detail, the run-level facts about the one car this run
    # covers. Stored as the small dict rather than by keeping the page's HTML
    # around — a detail page is 2.1 MB and a scrolled listing 15.
    car_facts: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a driver's connection
# error can repeat the endpoint several times (the message plus a call log),
# so a masker that handled only the first occurrence would print the password
# the other times and look like it was working.
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


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded number: it drifts the moment a newer Chromium ships, and
    claiming an older Chrome than the JS engine and TLS handshake report is
    itself a mismatch a fingerprinter can key on. pyppeteer's
    `browser.version()` returns "HeadlessChrome/115.0.0.0"; the marketing
    part is what a real Chrome would send.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone, so the cookie jar goes with the exit.
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            self.browser = self.bridge.run(
                connect(browserWSEndpoint=self.args.cdp_endpoint,
                        ignoreHTTPSErrors=True), timeout=CONNECT_TIMEOUT)
            self.page = self.bridge.run(self.browser.newPage())
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the Chromium at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises
        # "signal only works in main thread of the main interpreter" because
        # the event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead, so nothing is
        # lost — the browser is still closed on both success and failure.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        self.bridge.run(self.page.setViewport({"width": 1600, "height": 1000}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        return self

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here; every decision about what to do
# with the answer is in page_flow.py so all three engines make it the same way.
def _driver(session):
    bridge, page = session.bridge, session.page

    def count(selector):
        return len(bridge.run(page.querySelectorAll(selector)))

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return bridge.run(page.content())
        except Exception as e:  # noqa: BLE001
            # A geo-redirect or the consent layer can navigate, so a
            # snapshot can land exactly on the document swap. None tells the
            # caller to skip a check rather than fail the run.
            logger.debug("content() unavailable (page navigating?): %s", e)
            return None

    def current_url():
        return page.url

    def page_height():
        try:
            return int(bridge.run(page.evaluate(
                "() => document.body.scrollHeight")))
        except Exception:  # noqa: BLE001 — a missing height is not fatal
            return None

    def scroll_to_bottom():
        # Scroll to the document's own bottom, not a fixed wheel distance: a
        # fixed distance falls behind a page that grows as it loads, and this
        # grid went from 8,004 to 32,008 px across ten rounds.
        try:
            bridge.run(page.evaluate(
                "() => window.scrollTo(0, document.body.scrollHeight)"))
        except Exception:  # noqa: BLE001
            pass

    # The scroll primitives are NAMED OPERATIONS rather than JavaScript
    # crossing the page_flow boundary: pyppeteer takes `() => expr` while
    # Selenium takes a function body with an explicit `return`, so a shared
    # module passing JS would acquire one driver's dialect.
    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url, "page_height": page_height,
            "scroll_to_bottom": scroll_to_bottom}


def _content(session) -> Optional[str]:
    return _driver(session)["content"]()


def _parse_for_mode(html: str, url: str, args, page_num: int = 1,
                    boundaries: Optional[List[int]] = None) -> List:
    # `boundaries` is how a run that scrolled ONE long page still reports a
    # meaningful `page` per row: the card count after each scroll round, so a
    # car at position 47 is known to have arrived in batch 3. Without it
    # every row is page 1 and `page`+`position` stops identifying a row.
    # Mirrors playwright_scraper._parse_for_mode exactly.
    if args.mode == "detail":
        return parse_detail_page(html, url, category=args.category)
    return parse_products(html, url, page=page_num, category=args.category,
                          page_boundaries=boundaries)


def _same_url(a: str, b: str) -> bool:
    """Whether two URLs address the same page.

    Delegates to page_flow rather than reimplementing the comparison, so all
    three engines cannot drift on it. Used to notice that the page answering
    is not the page that was asked for: a redirect to another city's listing
    would change the run's SUBJECT while every row in it stayed internally
    consistent.
    """
    return page_flow.comparable(a) == page_flow.comparable(b)


def _advertised_next_hrefs(session, page_num: int = 1) -> List[str]:
    """Every href on the page that could be a link to the next page.

    On Spinny this returns NOTHING, and that is measured rather than broken:
    a fully scrolled 15 MB listing capture publishes no `link[rel=next]`, no
    `a[rel=next]`, no href containing `page=` and no next-page button.

    Reads the DOM's `.href` property rather than the raw attribute, which the
    browser has already resolved — the opposite of Playwright's
    get_attribute("href"). Kept explicit because the two engines differ here
    and a hand-rolled join got it wrong once.
    """
    bridge, page = session.bridge, session.page
    hrefs = bridge.run(page.evaluate(
        "(selector) => Array.from(document.querySelectorAll(selector))"
        ".map(a => a.href || a.getAttribute('href')).filter(Boolean)",
        page_flow.next_page_selector(page_num)))
    return list(hrefs or [])


def _check_for_new_pagination(session, page_num: int = 1) -> None:
    """Log it if the site has grown the pagination markup it has never had.

    Not an error and not a behaviour change: the run continues to scroll. But
    a match here means the single most important assumption in this repo has
    changed, and finding that out from a log line beats finding it out from a
    run that silently takes a twentieth of a catalogue. Mirrors
    playwright_scraper._check_for_new_pagination.
    """
    hrefs = [h for h in _advertised_next_hrefs(session, page_num) if h]
    if not hrefs:
        return
    logger.warning(
        "This page advertises %d next-page link(s) — %s. Spinny has never "
        "published any (0 matches on a fully scrolled 15 MB capture), so if "
        "this is real, the site has grown pagination and "
        "product_parser.PAGINATED_KINDS / page_flow's pagination block need "
        "revisiting. This run still scrolls.",
        len(hrefs), ", ".join(hrefs[:3]))


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same two families, same order, same "detected is not blocking" rule as
    the Playwright engine — see its docstring for why the anchor count is
    checked here rather than after the readiness wait.
    """
    bridge, page = session.bridge, session.page
    html = _content(session)
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = len(bridge.run(page.querySelectorAll(selector)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: bridge.run(page.evaluate(js)), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False
    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it.", challenge.kind, challenge.source,
                    already_rendered)
        return False
    # Which task type a solve would actually buy, read off the SITE'S OWN
    # loader rather than guessed from the widget. Mirrors the Playwright
    # engine: sending v3 parameters for a v2 widget buys a token the site
    # rejects, so the flavour is logged before the spend.
    configured = recaptcha_config(html)
    if configured:
        logger.info("The site's own reCAPTCHA config says %s%s, sitekey %s.",
                    configured["version"],
                    " enterprise" if configured["enterprise"] == "true" else "",
                    configured["sitekey"] or "(explicit)")
    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s).", e)
        return False
    bridge.run(page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    bridge.run(page.reload({"waitUntil": "domcontentloaded", "timeout": 60000}))
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    The retry/rotate/wait policy is page_flow's and finish_run's; what differs
    here is only the driver calls. Kept structurally parallel on purpose —
    the two files are meant to be diffable, because "all three engines agree"
    is checked by reading them side by side as well as by the smoke suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    bridge, page = session.bridge, session.page
    d = _driver(session)

    # See the Playwright engine for the measurement: without a pool there is
    # no exit to rotate to, but a plain re-fetch is what clears a block on a
    # Scraping Browser profile, so the budget is not zero.
    has_pool = bool(pool and len(pool) > 1)
    # Computed by page_flow, not here, so the three engines cannot disagree
    # about how many attempts a blocked page is worth — and so that
    # `RETRY_ON_BLOCKED` has a READER rather than a paragraph of
    # justification nobody consults.
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
        load_failed = False
        for attempt in range(1, args.retries + 1):
            try:
                bridge.run(page.goto(url, {"waitUntil": "domcontentloaded",
                                           "timeout": 60000}))
                load_failed = False
                break
            except Exception as e:  # noqa: BLE001 — pyppeteer raises many types
                load_failed = True
                # pyppeteer surfaces a dead proxy as a page error whose text
                # carries Chromium's own name for it, exactly as Playwright
                # does; a timeout and an unusable exit want opposite
                # responses, so they are told apart by that text.
                text = str(e)
                if any(marker in text for marker in _PROXY_ERROR_MARKERS):
                    logger.warning("Exit %s is unusable (%s).",
                                   mask(pool.current) if pool else "(none)", text[:120])
                    break
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        # Guarded on there BEING a pool, which the copied version was not:
        # `pool.advance` on a run with no --proxy-file is an
        # AttributeError on None, on the failure path, at runtime — invisible
        # to import, --help and the offline suite. Mirrors the Playwright
        # engine, which rotates only when there is somewhere to rotate to.
        if load_failed and has_pool and block_attempt < block_retries:
            pool.advance("unusable exit or repeated load failure")
            session.relaunch()
            bridge, page = session.bridge, session.page
            d = _driver(session)
            continue
        if load_failed:
            break


        if handle_captcha_if_present(session, args):
            time.sleep(1)

        html = _content(session) or ""
        state = page_flow.classify(html, url=page.url)

        # "Not painted yet" is not a fault, and on this site it is the NORMAL
        # first response: domcontentloaded at 1.1 s with zero cars, the first
        # batch of 22 arriving at 4.7 s from an api.spinny.com XHR.
        # Classified naively that is "unknown", and "unknown" retries — so
        # every healthy listing would burn its retry budget on shells. Wait
        # for the anchor and re-classify BEFORE the retry decision. Mirrors
        # playwright_scraper exactly; see page_flow.is_unpainted.
        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is the shell Spinny serves first (%d bytes, "
                        "nothing to read yet) — waiting up to %.0fs for it "
                        "to hydrate rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            need = page_flow.min_matches(args.mode)
            found = page_flow.wait_for_count(
                d["count"], d["sleep"], page_flow.ready_selector(args.mode),
                need, wait_timeout)
            # `<` and not `<=`: wait_for_count returns as soon as the
            # count REACHES the threshold, so `found == need` is success.
            if found < need:
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content(session) or html
            state = page_flow.classify(html, url=page.url)

        # No interstitial-settling step, and its absence is measured rather
        # than an omission: no refusal of any kind was ever observed on this
        # site, from either a datacentre or a residential exit, so there is
        # no interstitial to wait out.
        #
        # The paid path is reached only for state "challenge", which no
        # capture of this site has ever produced — Spinny's reCAPTCHA is
        # Enterprise v3, which scores rather than challenges. Wired up
        # because a bot manager can be switched on between deploys, and
        # bounded by SOLVES_PER_PAGE so a speculative path cannot become a
        # bill.
        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                time.sleep(1)
                html = _content(session) or html
                state = page_flow.classify(html, url=page.url)
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — an SEO landing page has no grid, and a cityless
            # URL is served with an empty one — so retrying it would
            # re-confirm the same right answer, and rotating the exit would
            # blame an address for the URL it was given.
            break

        # Blocked or challenged. Whatever went wrong is a property of the
        # ADDRESS or the connection rather than of the URL, so a different
        # exit is the only thing that plausibly changes the outcome — and on
        # this site the likeliest cause is a dead proxy exit, not a score.
        if block_attempt < block_retries:
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
                bridge, page = session.bridge, session.page
                d = _driver(session)
            else:
                # No pool, so nowhere else to go — but a plain re-fetch is
                # what clears this on a Scraping Browser profile. The browser
                # is NOT relaunched: over --cdp-endpoint a profile allows one
                # live connection, so reconnecting risks profile_locked and
                # would lose the cookies the retry is meant to build on.
                pause = args.retry_delay * (block_attempt + 1)
                logger.warning("Page %d came back as %s — re-fetching through "
                               "the same access path in %.1fs (%d/%d).",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # Most likely NOT Spinny refusing: no refusal was ever observed on
        # this site, from an Amsterdam datacentre exit or a Chennai
        # residential one. Suspect the access path first. The dump is written
        # even when empty: "0 bytes" is itself the diagnosis, and a reader
        # who finds no file cannot tell that from a run that never got here.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error(
            "This response was not built by Spinny — %d bytes, %s the site's "
            "own asset hosts, saved to %s. That inverted check is the only "
            "one that classifies CHROMIUM'S OWN error page correctly: a proxy "
            "failure renders a page carrying the site's hostname in its "
            "<title> and no vendor marker anywhere. A page Spinny built "
            "references assets.spinny.com 267 to 1040 times; that one "
            "references it zero. Check the access path before blaming the "
            "site: is the --proxy exit alive and authenticating, and is "
            "another run holding this --cdp-endpoint pid? This is exit 3, "
            "distinct from a genuinely empty listing (exit 4).",
            len(html or ""),
            "which references" if served_by_spinny(html or "")
            else "with no reference to", debug_html)
        outcome.blocked_by = "no-response" if not html else "not-served"
        outcome.final_url = d["current_url"]()
        return outcome


    if state == "content":
        # Wait for paint, then SCROLL — and on this site the scroll is the
        # only way any car past the first batch is ever seen. Spinny hydrates
        # 20 cars at a time from an api.spinny.com XHR: 22 cards at round 0,
        # 302 after ten rounds, against an advertised 1559.
        selector = page_flow.ready_selector(args.mode)
        threshold = page_flow.min_matches(args.mode)
        # A POLL, not waitForFunction, which hands the browser a STRING to
        # evaluate and is refused outright by a site whose CSP has no
        # `unsafe-eval`. See page_flow.wait_for_count.
        found = page_flow.wait_for_count(
            d["count"], d["sleep"], selector, threshold,
            page_flow.content_timeout_ms(args.mode))
        time.sleep(0.5)
        if found < threshold:
            # Not an error on its own, and what it MEANS depends on the mode.
            # A listing page with no grid can be a correct answer (an SEO
            # landing page, a cityless URL, a filter nothing matches); a
            # detail page whose price block never painted is a different
            # thing, and usually just slow, because the row is read out of
            # the page's own ["Product","Car"] JSON-LD.
            if args.mode == "detail":
                logger.info("The price block did not paint in time. That is "
                            "not fatal: a detail row is read from the page's "
                            "own [\"Product\",\"Car\"] JSON-LD, which is in "
                            "the first response, so the parse below decides.")
            else:
                logger.info("No cars appeared in time. If this URL is a "
                            "/used-cars/ hub, a cityless listing or a filter "
                            "nothing matches, that is the expected answer and "
                            "the run will report 0 rows (exit 4).")

        if args.mode != "detail":
            # `--pages N` is a scroll budget here, not a URL plan: N batches
            # of 20 cars. The rounds budget is deliberately larger than N,
            # because a round can legitimately add nothing.
            outcome.scroll = page_flow.scroll_until_settled(
                count=d["count"], page_height=d["page_height"],
                scroll_to_bottom=d["scroll_to_bottom"], sleep=d["sleep"],
                selector=selector,
                rounds=page_flow.scroll_rounds_for(args.pages),
                want_cards=page_flow.target_cards(args.pages))
            if not (outcome.scroll["settled"] or outcome.scroll["reached_target"]):
                logger.warning(
                    "The grid was still growing after %d scroll rounds (%d "
                    "cards, height %s) and had not reached the %d cars "
                    "--pages %d asks for — this page is PARTIAL. Its row "
                    "count is a floor, not the listing.",
                    outcome.scroll["rounds"], outcome.scroll["cards"],
                    outcome.scroll["height"],
                    page_flow.target_cards(args.pages), args.pages)

        html = d["content"]() or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only for a state page_flow already counts as BLOCKED. An EMPTY page is
    # a correct answer, so this only ever refines the REASON for a page the
    # policy had already given up on. The marker set itself was cut down for
    # the same reason: `recaptcha` and `g-recaptcha` were inherited from the
    # sibling repos and REMOVED after counting them on a page known good,
    # because Spinny loads reCAPTCHA v3 on every page it serves. Mirrors
    # playwright_scraper exactly.
    vendor = (detect_bot_challenge(html, url=page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    # The scroll's record of the card count after each round is what lets a
    # run that scrolled ONE long page still report a meaningful `page` per
    # row. Without it every row is page 1 and `page`+`position` stops
    # identifying a row.
    boundaries = (outcome.scroll or {}).get("boundaries")
    products = _parse_for_mode(html, page.url, args, page_num,
                               boundaries=boundaries)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode == "detail":
        outcome.car_facts = car_metadata(html, page.url)

    if args.mode == "listing":
        # The tripwire for this engine's central assumption, run on every
        # listing fetch rather than documented and forgotten.
        _check_for_new_pagination(session, page_num)
        # Spinny DOES publish a total, beside its own heading — "1559 Used
        # cars in Delhi NCR" — which makes the completeness check arithmetic
        # rather than a threshold.
        outcome.completeness = page_flow.completeness(
            len(products), html, outcome.scroll)
        outcome.total_available = outcome.completeness["total_available"]
        outcome.header = listing_heading(html)
        if outcome.header:
            logger.info("The listing's own heading says: %s", outcome.header)

    if products and args.mode == "listing":
        priced = sum(1 for p in products if p.price is not None)
        share = 100.0 * priced / len(products)
        kind = listing_kind(page.url)
        floor = PRICE_FLOOR.get(kind, 0)
        logger.info("Price coverage on page %d (%s page): %d/%d (%.0f%%); the "
                    "floor for this page kind is %d%%.",
                    page_num, kind, priced, len(products), share, floor)
        if share < floor:
            logger.warning(
                "Only %.0f%% of page %d carries a price, against a measured "
                "floor of %d%% for a %s page. All 705 cards across the three "
                "captured listings had one, so this is the read breaking "
                "rather than the page being unusual.", share, page_num,
                floor, kind)

        # No structured-price confirmation share, and its absence is
        # measured: a Spinny LISTING carries five JSON-LD blocks and not one
        # names a car. What replaces it is the check this site needs — the
        # two price BASES against each other. The all-in attribute figure
        # includes RC transfer and insurance and was the HIGHER of the two on
        # 482 of 482 cards; a row where it is lower means the reads have been
        # crossed, which is what computes a negative discount.
        pairs = [(p.price, p.price_all_in) for p in products
                 if p.price is not None and p.price_all_in is not None]
        if pairs:
            consistent = sum(1 for shown, all_in in pairs if all_in >= shown)
            share = 100.0 * consistent / len(pairs)
            logger.info("All-in price at or above the displayed price on page "
                        "%d: %d/%d (%.0f%%).",
                        page_num, consistent, len(pairs), share)
            if share < ALL_IN_ABOVE_DISPLAYED_FLOOR:
                logger.warning(
                    "On %d row(s) of page %d the all-in price is BELOW the "
                    "displayed one. That was true of 0 of 482 measured cards, "
                    "so the two price bases have most likely been crossed.",
                    len(pairs) - consistent, page_num)

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = page.url
    return outcome


# Chromium's own names for "the proxy is the problem, not the site".
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


def scrape(args) -> int:
    # One entry per page attempted — exactly one on this site, in both modes.
    # The list and the page-order merge are kept anyway: they are what the
    # family's sidecar is built out of. Mirrors playwright_scraper.scrape.
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

    # Refused with the reason rather than ignored, in every engine: a flag
    # that appears to work and does nothing is worse than one that says no.
    if args.concurrency > 1:
        if args.mode == "detail":
            logger.info("--concurrency is ignored in --mode detail: there is "
                        "one page to fetch.")
        else:
            logger.warning("--concurrency %d is not honoured here: %s",
                           args.concurrency,
                           page_flow.concurrency_refusal(args.url)
                           or "this URL cannot be split across workers.")

    if args.twocaptcha_key or args.solve_captcha == "always":
        logger.info("%s", page_flow.recaptcha_note())

    bridge = _AsyncBridge()
    session = None
    outcome = None
    try:
        session = _Session(bridge, args, pool).open()
        outcome = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(outcome)

        # The tripwire for the assumption this whole engine rests on. False
        # for every URL on this site today; if a deploy ever makes it True,
        # this says so loudly and names what to bring back.
        if page_flow.pagination_is_addressable(
                outcome.final_url or args.url,
                _advertised_next_hrefs(session)):
            logger.warning(
                "This listing now looks addressable page by page, which "
                "Spinny has never been. product_parser.PAGINATED_KINDS and "
                "the multi-page loop the rest of this family ships need "
                "bringing back — this run still scrolls one page.")
    finally:
        if session is not None:
            session.close()
        bridge.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
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
        stop_reason = "single_page_mode"
    else:
        stop_reason = page_flow.listing_stop_reason(
            outcome.scroll, outcome.completeness, len(all_rows))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = outcome.final_url or args.url

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
        advertised = (outcome.completeness or {}).get("total_available")
        short_by = (outcome.completeness or {}).get("short_by")
        if advertised:
            logger.info("This listing advertises %d car(s); this run holds %d "
                        "(%.1f%%).", advertised, len(all_rows),
                        100.0 * len(all_rows) / advertised)
        if short_by:
            logger.info("Short by %d against the advertised total — ask for "
                        "more --pages, or narrow the listing with one of "
                        "Spinny's own filter URLs.", short_by)

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
        description="Spinny scraper (pyppeteer edition). pyppeteer is "
                    "effectively unmaintained — playwright_scraper.py is the "
                    "primary engine.")
    p.add_argument("--url", default=None,
                   help="Spinny URL: /used-cars-in-{city}/s/, a filtered "
                        "listing such as /used-{make}-cars-in-{city}/s/, or "
                        "/buy-used-cars/.../{id}/ with --mode detail. "
                        "Required, unless SPINNY_URL is set in the "
                        "environment or in .env.")
    p.add_argument("--mode", choices=["listing", "detail"],
                   default="listing",
                   help="listing (default) or detail. detail reads one "
                        "/buy-used-cars/.../{id}/ page out of its own "
                        "[\"Product\",\"Car\"] JSON-LD and overview table, "
                        "adding the EXACT odometer reading, the "
                        "previous-owner count, the colour, the seating "
                        "capacity, the registration year and month and the "
                        "insurance details — the columns a listing card "
                        "cannot carry. No --pages in detail mode.")
    p.add_argument("--category", default=None, help="Label to tag output rows with.")
    p.add_argument("--pages", type=int, default=1,
                   help="How many BATCHES of 20 cars to gather. A SCROLL "
                        "budget, not a URL plan: a Spinny listing has no "
                        "per-page addresses — ?page=N returns page 1 under "
                        "HTTP 200 — so batch N is reached by scrolling one "
                        "long page.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted and REFUSED with the reason, in every "
                        "engine: page 5 of an infinitely scrolling grid has "
                        "no address to hand a worker. Split the work across "
                        "Spinny's own filter URLs instead.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "A page that comes back EMPTY is not retried: an SEO "
                        "landing page or a cityless URL having no grid is a "
                        "correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="spinny_products", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999. "
                        "Credentials are sent over CDP (page.authenticate), "
                        "never on the browser's command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Note what "
                        "there is to buy on this site: NOTHING, so far. "
                        "Spinny loads reCAPTCHA Enterprise v3 (invisible) on "
                        "every page it serves — it SCORES the session rather "
                        "than challenging it — and no rendered challenge has "
                        "ever been observed.")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP, e.g. "
                        "ws://user:pass@host:port. pyppeteer authenticates on "
                        "the WebSocket upgrade, so a credentialed Scraping "
                        "Browser endpoint works here.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive, instead of the Chromium "
                        "pyppeteer downloads for itself. Needed where that "
                        "build will not start: on an Apple Silicon Mac "
                        "pyppeteer fetches an x86_64 Chromium 117, which runs "
                        "under Rosetta far enough to print --version and then "
                        "fails to open its DevTools socket (measured "
                        "2026-09-08; the same failure occurs with no wrapper "
                        "code at all, so it is the build, not this engine). "
                        "Point it at a Chrome or Chromium of your own — "
                        "Playwright's, if you have it installed.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and SPINNY_URL is not set in the environment "
                "or in .env.")
    if args.mode == "detail" and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read.", args.pages, args.mode)
        args.pages = 1
    if not is_supported_host(args.url):
        # Refused rather than attempted: the card scoping, the
        # /buy-used-cars/ path pattern, the lakh/crore price words and the
        # "there is no pagination" model are all Spinny's, so another site
        # would not fail loudly — it would return zero rows and read as an
        # empty listing.
        why = unsupported_reason(args.url)
        if why:
            # Named WITH THE REASON: "is not a Spinny site" would be false
            # for api.spinny.com and sends the reader hunting for a typo.
            p.error(f"{site_host(args.url)} {why}.")
        p.error(f"{site_host(args.url) or args.url!r} is not Spinny. This "
                f"scraper reads www.spinny.com, the site's only "
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
        logger.warning(
            "%s is an SEO LANDING PAGE, not a listing — it has no car grid "
            "on it, so this run will return 0 rows and exit 4. A listing URL "
            "ends in /s/ and names a city: "
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
            "%s does not match any Spinny page shape this parser knows. The "
            "run will go ahead and report what it finds, which may be "
            "nothing.", args.url)
    city_warning = unknown_city_warning(args.url)
    if city_warning:
        logger.warning("%s", city_warning)
    page_warning = page_flow.page_param_warning(args.url)
    if page_warning:
        logger.warning("%s", page_warning)
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except Exception as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest
        # one: `profile_locked` means another run still holds this `pid`, and
        # a harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
