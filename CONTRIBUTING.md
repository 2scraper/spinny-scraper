# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Spinny changing its markup is the normal way this stops working, and it has
its own issue template. The detail that saves the most time is WHICH anchor
broke, because a listing page has **no structured data about its cars** to
fall back on: five `application/ld+json` blocks on a fully scrolled capture
and not one of them names a vehicle. So the DOM is not the primary path by
preference here, it is the only one.

1. **The grid container**, `[data-id="landing-plp-container"]`. If it moves
   the run reports 0 rows and exit 4, which is loud.
2. **The card**, `[data-base-component="card"]`, and inside it **the car
   link**, `a[href^="/buy-used-cars/"]` — which is also where the id comes
   from. That path is a contract with search engines rather than a build
   artefact, which is why everything anchors on it.
3. **The price nodes**: `[data-base-component="Pricing"]` for the displayed
   price, `.ds-line-through` for the strike, and
   `#shortlist_icon[data-price]` for the exact all-in figure and the id.
4. **The heading**, `[data-componentname="HeadingContent"]`, which carries
   the site's own advertised total and is what makes the completeness check
   arithmetic rather than a threshold.

Note what is NOT on that list: the `ds-*` classes. They are a design-system
vocabulary — `ds-body-small` wraps the fuel type, the transmission, the RTO
code and the hub name identically — so a class cannot tell one field from
another, and the reads are pattern-based inside a structurally-scoped card
instead. Nothing is read by POSITION either: 10 of 705 cards print no hub,
and anything positional is silently right-shifted on those ten.

The one place structured data does exist is a DETAIL page's own
`["Product","Car"]` block, which is where `--mode detail` reads the exact
odometer, the owner count, the colour, the seating capacity and the
insurance details. It spells the price `offers.Price`, with a capital P.

A third thing can break without any path failing: the **join** between the
tiles and the structured data. When it breaks, the row count and the prices
stay healthy while `in_stock` and part of `brand` quietly empty out — so
every run logs its structured-price confirmation share per page and warns
below a floor set PER PAGE KIND (search 8%, category 70%, shop 80%; the
achievable share differs by a factor of eight between them). If you are
reporting a change, that percentage and the page kind are the numbers to
include.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once by
   hand before the badge is trusted. Unlike its siblings this canary needs
   **no credential** — the site serves a bare GitHub runner, measured — so it
   is scheduled daily as well. The optional `SPINNY_CDP_ENDPOINT` secret,
   when set, routes the same run through the Scraping Browser API; no step is
   gated on it, so the canary keeps working the day it expires.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Five properties in this repo exist because they were once absent and cost real
time. Tests pin all five, so a PR that breaks one will fail rather than
silently regress:

- **`--pages N` is a SCROLL budget, not a URL plan.** `?page=N` is silently
  ignored by this site — pages 1, 2 and 3 of one listing URL return
  byte-identical first cards under HTTP 200 — so a planner that built page
  URLs would fetch page one N times, find no new car id, conclude the listing
  was exhausted and report a COMPLETE run holding a twentieth of the
  catalogue. `product_parser.paginates_by_url` is False everywhere, `page_url`
  returns None above page 1, and `--concurrency` above 1 is refused with that
  reason. The worker pool the rest of this family ships is deliberately
  absent rather than present and inert.
- **`price` and `price_all_in` are two different BASES, not two reads of one
  number.** The displayed figure excludes RC transfer and insurance; the
  site's own `data-price` attribute includes them, and was the higher of the
  two on 482 of 482 measured cards. Mixing them computes a negative discount
  that looks entirely plausible, so `discount_pct` is derived from the
  displayed pair alone and cross-checked against the site's own
  `discount_amount`.
- **`brand` comes from the URL, not from the headline.** The card abbreviates
  the make on 178 of 705 cards — "Maruti Swift" for a maruti-suzuki. The
  URL's own make segment does not, and `product_parser.MAKE_DISPLAY` turns it
  into a column value.
- **Nothing is read by POSITION inside a card.** 10 of 705 cards print no
  hub, so anything positional is silently right-shifted on those ten. Fields
  are recognised by pattern — one of five fuel words, an RTO code shaped by
  law, a value ending in "km" — inside a card scoped to exactly one car.
- **A block is almost certainly not the site.** No Spinny refusal has ever
  been observed, from a home connection, a datacentre exit, a residential
  exit or the Scraping Browser API. Block detection is INVERTED: a served
  page is recognised by the site's own asset hosts, and the absence of them
  is the signal — which is the only thing that classifies Chromium's own
  network-error page correctly, since it carries the site's hostname in its
  `<title>` and no vendor marker anywhere. `page_flow.STATE_POLICY` holds the
  retry/solve/blocked decision as data so the three engines cannot disagree.
- **A run that finds nothing writes nothing.** It must not replace a good output
  file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked (the 403 refusal, or a challenge), `4` zero rows —
  including a hub category, which is a correct answer — `5` remote API error,
  `6` partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** An SEO
  landing page has no car grid, and a listing URL that names no city is
  served with an empty one because Spinny scopes inventory by city; both are
  correct answers to the question that was asked.
  Retrying them spends the user's budget re-confirming the same answer, and
  rotating the exit blames an address for the URL it was given.
  `page_flow.STATE_POLICY` holds that for all three engines so they cannot
  disagree about it.
- **A challenge marker is only consulted for a state already counted as
  blocked**, and a marker that matches every page of the site is not a marker
  at all. That is why `recaptcha` and `g-recaptcha`, both inherited from
  sibling repos, are NOT in the list here: Spinny loads reCAPTCHA Enterprise
  v3 on every page it serves — 31 occurrences on a capture that had just
  delivered 482 cars — so as markers they would have made every healthy page
  blocked. Count a marker on a page you know is good before adding it. The
  same applies to `cf-turnstile`, which the Scraping Browser API's own
  auto-solve extension injects into every page it loads.
- **A sku already written earlier in the same run is dropped, not
  duplicated.** One scrolled page can re-render a card as it hydrates, so a
  small non-zero drop count is expected and a large one is not. See
  `dedupe_by_key` in `output_writer.py`.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
spinny.com, say in the PR what you ran, which URL and page kind, from which
exit, and what you got — including the price coverage and the all-in-versus-
displayed share the run prints, and the scroll trace and completeness block
from the sidecar. Car counts differ by city, by filter and by how far the
scroll got, and the site's own advertised total moves between runs minutes
apart, so a bare "worked for me" is not reproducible.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

Do not add anything that submits the registration form. This project
deliberately never does, and a captcha token proved valid by creating a real
account is not a result worth having.

## Scope

This repo scrapes **public pages** on Spinny: city listings, filtered
listings and car detail pages, exactly as an anonymous visitor is served
them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
