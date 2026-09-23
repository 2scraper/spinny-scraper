#!/usr/bin/env python3
"""
spinny-scraper — Selenium edition (secondary engine)
====================================================

The same scrape as playwright_scraper.py, driven through Selenium. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money — the decisions that determine all three live in page_flow.py
and output_writer.finish_run(), so this file is browser plumbing and nothing
else. Read playwright_scraper.py's docstring for what is different about
Spinny; the one that shapes this file is that **`--pages N` scrolls rather
than fetching N URLs** — `?page=N` on a Spinny listing is silently ignored
and returns page 1 under HTTP 200, so a listing is one infinitely scrolling
page that hydrates 20 cars at a time.

    --mode listing   (default)  a city or filtered listing
                                (/used-cars-in-{city}/s/)
    --mode detail               one /buy-used-cars/.../{id}/ page

Two limits of this engine, stated here rather than left to be discovered.
Neither is a bug in this code and neither can be fixed from here:

  * **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
    `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put a password. So --cdp-endpoint here works only for an endpoint that
    needs no credentials; a credentialed one is refused with exit 2 rather
    than connected to and silently failing.
  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=` accepts
    no credentials, and there is no equivalent of pyppeteer's
    `page.authenticate`. Credentials are stripped and a warning says so, so
    nobody believes a `user:pass` URL is doing something.

There is no --concurrency in ANY engine here, and that is a property of the
site rather than of this one: page 5 of an infinitely scrolling grid has no
address to hand a worker.

Usage
-----
    python selenium_scraper.py \\
        --url "https://www.spinny.com/used-cars-in-delhi-ncr/s/" --pages 5

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          Selenium 4 fetches a matching chromedriver itself; a local Chrome
          or Chromium must be installed.
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

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
logger = logging.getLogger("selenium_scraper")

ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The lowest PRICE coverage that is still healthy. Not a structured-price
# confirmation share, because a Spinny LISTING carries no structured price to
# confirm against — five JSON-LD blocks and not one names a vehicle — so
# `price_source` is "dom+attr" on every listing row and a confirmation
# threshold would describe nothing. What IS worth a floor is the plain share
# of rows that got a price at all: 705 of 705 across three captures.
#
# Kept byte-identical to the Playwright engine's, and the offline suite
# asserts that.
PRICE_FLOOR = {"listing": 98, "cityless": 98, "detail": 100}

# The two price bases, checked against each other on every run. The all-in
# attribute figure was HIGHER than the displayed one on 482 of 482 Delhi
# tiles, because it includes RC transfer and insurance. A row where it is
# lower means the two reads have been crossed — the mistake that produces a
# negative discount.
ALL_IN_ABOVE_DISPLAYED_FLOOR = 98

PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Chromium's own names for "the proxy is the problem, not the site". A dead
# proxy and a slow page want opposite responses — a different exit versus
# another try at the same one — so they are told apart by the error text.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


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
    # one, which makes the completeness check arithmetic.
    total_available: Optional[int] = None
    # That heading verbatim, for the sidecar.
    header: Optional[str] = None
    # What the lazy-load scroll did: rounds, cards, the count after each
    # round, and whether it SETTLED or merely reached the target. A page
    # whose grid was still growing when the budget ran out is partial.
    scroll: Optional[dict] = None
    # The advertised total against what this run holds, from
    # page_flow.completeness.
    completeness: Optional[dict] = None
    # In --mode detail, the run-level facts about the one car this run
    # covers.
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

    `driver.capabilities["browserVersion"]` is the installed Chrome's version,
    so the claim matches what the JS engine and the TLS handshake report. A
    hardcoded number drifts the moment Chrome updates, and claiming an older
    Chrome than everything else reports is itself a signal.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason.

    chromedriver takes a bare address here and cannot send credentials, so an
    endpoint that carries them cannot work through this engine. Refused up
    front: connecting anyway would fail somewhere further in with an error
    that names none of this.
    """
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser — and a
    fresh browser is also the only thing that re-rolls the served page
    fresh cookie jar is what an ordinary user on another network looks like.
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover.
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        # Not a fingerprint measure, a correctness one: without it Chrome
        # advertises "HeadlessChrome", which is a giveaway on any site with
        # a bot manager in front of it.
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            # Set over CDP rather than as a launch switch, so it can use the
            # version the driver actually reports.
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # Explicit, because a driver that stops answering otherwise hangs the
        # run: "every remote call is bounded" applies to this engine too.
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            # The same patch script the Playwright engine installs on its
            # context. Shared deliberately: two engines applying different
            # halves of one fingerprint would be a contradiction of exactly
            # the kind a fingerprint is meant to avoid.
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is why page_flow names
# operations instead of passing JavaScript.
def _driver(session):
    driver = session.driver

    def count(selector):
        try:
            return len(driver.find_elements(By.CSS_SELECTOR, selector))
        except WebDriverException as e:
            logger.debug("count(%s) failed: %s", selector, e)
            return 0

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return driver.page_source
        except WebDriverException as e:
            # A geo-redirect or the consent layer can navigate, so a
            # snapshot can land on the document swap. None tells the caller to
            # skip a check rather than fail the run.
            logger.debug("page_source unavailable (page navigating?): %s", e)
            return None

    def current_url():
        try:
            return driver.current_url
        except WebDriverException:
            return ""

    def page_height():
        # Selenium's execute_script takes a function BODY with an explicit
        # `return` — not `() => expr`, which is what the other two drivers
        # take. That disagreement is exactly why page_flow names the
        # OPERATION and each engine spells it in its own dialect.
        try:
            return int(driver.execute_script(
                "return document.body.scrollHeight;"))
        except (WebDriverException, TypeError, ValueError):
            return None

    def scroll_to_bottom():
        # To the document's own bottom, not a fixed wheel distance: a fixed
        # distance falls behind a page that grows as it loads, and this grid
        # went from 8,004 to 32,008 px across ten rounds.
        try:
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);")
        except WebDriverException:
            pass

    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url, "page_height": page_height,
            "scroll_to_bottom": scroll_to_bottom}


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

    Reads the DOM's `.href` property, which is already absolute — the
    opposite of Playwright's get_attribute("href"), which returns the raw
    attribute. Kept explicit because the engines differ here.

    Note the JS is a function BODY with an explicit `return`, not the arrow
    expression the other two engines pass. That difference is exactly why no
    JavaScript crosses the page_flow boundary.
    """
    try:
        hrefs = session.driver.execute_script(
            "return Array.from(document.querySelectorAll(arguments[0]))"
            ".map(a => a.href || a.getAttribute('href')).filter(Boolean);",
            page_flow.next_page_selector(page_num))
    except WebDriverException:
        return []
    return list(hrefs or [])


def _check_for_new_pagination(session, page_num: int = 1) -> None:
    """Log it if the site has grown the pagination markup it has never had.

    Not an error and not a behaviour change: the run continues to scroll. But
    a match here means the single most important assumption in this repo has
    changed. Mirrors playwright_scraper._check_for_new_pagination.
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

    Same detectors, same reconciliation and the same "detected is not
    blocking" rule as the Playwright engine — the three must agree about
    when a run spends money.

    NOTE what this cannot help with, because on this site that is almost
    everything. Spinny loads reCAPTCHA Enterprise **v3** on every page it
    serves, and v3 scores traffic rather than challenging it — there is no
    widget to solve. What a blocked run here most likely met is a dead proxy
    exit or Chromium's own error page, which detect_page_state reports as
    "blocked" rather than "challenge" precisely so no solve is attempted or
    billed.
    """
    driver = session.driver
    d = _driver(session)
    html = d["content"]()
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = d["count"](selector)
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, d["current_url"]())
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=d["current_url"]())
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
    try:
        driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
    except WebDriverException as e:
        logger.error("Could not inject the token (%s).", e)
        return False
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    driver.refresh()
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    Kept structurally parallel to its twins on purpose — "all three engines
    agree" is checked by reading them side by side as well as by the smoke
    suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    d = _driver(session)
    html, state, load_failed = None, "ok", False

    # See the Playwright engine for the measurement: without a pool there is
    # no exit to rotate to, but a plain re-fetch is what clears a block on a
    # Scraping Browser profile, so the budget is not zero.
    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not just documented. It was a
    # constant with a paragraph of justification that no engine read — a
    # policy statement nothing enforced, which is the same defect as dead
    # code that looks load-bearing. Setting it False now really does stop
    # the retry loop.
    block_retries = page_flow.block_retry_budget(
        has_pool, args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.driver.get(url)
                load_failed = False
                break
            except (TimeoutException, WebDriverException) as e:
                text = str(e)
                reason = next((m for m in _PROXY_ERROR_MARKERS if m in text), "")
                load_failed = True
                if reason:
                    exit_failed = reason
                    break  # a different exit is the only thing that helps
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            d = _driver(session)
            continue
        if load_failed:
            break


        if handle_captcha_if_present(session, args):
            time.sleep(1)

        html = d["content"]() or ""
        state = page_flow.classify(html, url=d["current_url"]())

        # "Not painted yet" is not a fault, and on this site it is the NORMAL
        # first response: domcontentloaded at 1.1 s with zero cars, the first
        # batch of 22 arriving at 4.7 s from an api.spinny.com XHR.
        # Classified naively that is "unknown", and "unknown" retries — so
        # every healthy listing would burn its retry budget on shells. Wait
        # for the anchor and re-classify BEFORE the retry decision. Mirrors
        # playwright_scraper exactly; see page_flow.is_unpainted.
        if page_flow.is_unpainted(state, html):
            wait_s = page_flow.content_timeout_ms(args.mode) / 1000.0
            sel = page_flow.ready_selector(args.mode)
            need = page_flow.min_matches(args.mode)
            logger.info("Page %d is the shell Spinny serves first (%d bytes, "
                        "nothing to read yet) — waiting up to %.0fs for it "
                        "to hydrate rather than spending a retry.",
                        page_num, len(html), wait_s)
            found = page_flow.wait_for_count(d["count"], d["sleep"], sel,
                                             need, int(wait_s * 1000))
            # `<` and not `<=`: wait_for_count returns as soon as the
            # count REACHES the threshold, so `found == need` is success.
            if found < need:
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_s, found)
            html = d["content"]() or html
            state = page_flow.classify(html, url=d["current_url"]())

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
                html = d["content"]() or html
                state = page_flow.classify(html, url=d["current_url"]())
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
        # ADDRESS or the connection rather than of the URL — and on this site
        # the likeliest cause is a dead proxy exit, not a score.
        if block_attempt < block_retries:
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
                d = _driver(session)
            else:
                # No pool, so nowhere else to go — but a plain re-fetch is
                # what clears this on a Scraping Browser profile. Guarded on
                # there BEING a pool, which the copied version was not:
                # `pool.advance` on a run with no --proxy-file is an
                # AttributeError on None, on the failure path, at runtime.
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
        # residential one. Suspect the access path first — and on THIS engine
        # suspect it harder, because Selenium can authenticate neither a
        # remote CDP endpoint nor a proxy, so an unauthenticated exit here is
        # a configuration this engine cannot express rather than a fault.
        # The dump is written even when empty: "0 bytes" is itself the
        # diagnosis.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error(
            "This response was not built by Spinny — %d bytes, %s the site's "
            "own asset hosts, saved to %s. That inverted check is the only "
            "one that classifies CHROMIUM'S OWN error page correctly, and on "
            "this engine that is the likely case: a Selenium run through an "
            "unauthenticatable proxy produced 187,799 bytes of Chromium's "
            "network-error page on a sibling site, carrying the site's own "
            "hostname in its <title> and no vendor marker anywhere. A page "
            "Spinny built references assets.spinny.com 267 to 1040 times; "
            "that one references it zero. Note this engine cannot use an "
            "authenticated remote CDP endpoint or an authenticated proxy; see "
            "the README's engine limits. This is exit 3, distinct from a "
            "genuinely empty listing (exit 4).",
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
        timeout_s = page_flow.content_timeout_ms(args.mode) / 1000.0
        # A POLL through the shared helper, so all three engines wait the
        # same way. This engine could use WebDriverWait — it never evaluates
        # a string — but a shared wait is one fewer thing to drift on, and
        # the other two cannot use the driver's own predicate wait: it hands
        # the browser a STRING to evaluate, which a site whose CSP has no
        # `unsafe-eval` refuses outright.
        found = page_flow.wait_for_count(d["count"], d["sleep"], selector,
                                         threshold,
                                         int(timeout_s * 1000))
        time.sleep(0.5)
        if found < threshold:
            # Not an error on its own, and what it MEANS depends on the mode.
            # A listing page with no grid can be a correct answer (an SEO
            # landing page, a cityless URL, a filter nothing matches); a
            # detail page whose price block never painted is a different
            # thing, and usually just slow, because the row is read out of
            # the page's own ["Product","Car"] JSON-LD.
            if args.mode == "detail":
                logger.info("The price block did not paint within %.0fs. That "
                            "is not fatal: a detail row is read from the "
                            "page's own [\"Product\",\"Car\"] JSON-LD, which "
                            "is in the first response, so the parse below "
                            "decides.", timeout_s)
            else:
                logger.info("No cars appeared within %.0fs. If this URL is a "
                            "/used-cars/ hub, a cityless listing or a filter "
                            "nothing matches, that is the expected answer and "
                            "the run will report 0 rows (exit 4).", timeout_s)

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
    vendor = (detect_bot_challenge(html, url=d["current_url"]())
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    final_url = d["current_url"]() or url
    # The scroll's record of the card count after each round is what lets a
    # run that scrolled ONE long page still report a meaningful `page` per
    # row. Without it every row is page 1 and `page`+`position` stops
    # identifying a row.
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
    products = (_parse_for_mode(html, final_url, args, page_num,
                                boundaries=boundaries)
                if page_flow.should_parse(state) else [])
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode == "detail":
        outcome.car_facts = car_metadata(html, final_url)

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
        kind = listing_kind(d["current_url"]())
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
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = final_url
    return outcome


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

    session = None
    outcome = None
    try:
        session = _Session(args, pool).open()
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
        description="Spinny scraper (Selenium edition). Cannot authenticate a "
                    "proxy or a remote CDP endpoint — see the module "
                    "docstring; playwright_scraper.py is the primary engine.")
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
                   help="Proxy URL. NOTE: Selenium cannot authenticate a "
                        "proxy; credentials are stripped and a warning says "
                        "so. Use the Playwright or pyppeteer engine for an "
                        "authenticated exit.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from 2captcha's Fingerprint API "
                        "and apply it over CDP. Needs --twocaptcha-key. "
                        "Ignored with --cdp-endpoint.")
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
                        "your proxy's exit country.")
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
                   help="Attach to a running browser at host:port. Must NOT "
                        "carry credentials — chromedriver's debuggerAddress "
                        "cannot send them, so a credentialed endpoint is "
                        "refused with exit 2 rather than silently failing.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
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
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "remote browser supplies its own.")
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
