# spinny-scraper

[![release](https://img.shields.io/github/v/release/2scraper/spinny-scraper?sort=semver)](https://github.com/2scraper/spinny-scraper/releases)
[![tests](https://github.com/2scraper/spinny-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/spinny-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/spinny-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/spinny-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.12-blue)](https://www.python.org/)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20puppeteer-lightgrey)](#which-engine)
[![runs without an account](https://img.shields.io/badge/runs%20without%20an%20account-yes-brightgreen)](#do-you-need-anything-paid)

Scrapes **Spinny** used-car listings and car detail pages into JSON or CSV,
through Playwright, Selenium, pyppeteer, or a remote browser over CDP.

```
python playwright_scraper.py \
    --url "https://www.spinny.com/used-cars-in-delhi-ncr/s/" \
    --pages 5 --format both
```

---

## Do you need anything paid?

**No.** Measured 2026-09-11: an ordinary local Chromium on a plain home
connection, with no API key and no proxy, returned **62 cars from
`/used-cars-in-delhi-ncr/s/` with `--pages 3`**, every one of them priced,
and exit 0. No refusal of any kind was observed on this site — from that
connection or from an Amsterdam datacentre exit.

That is unusual in this family of scrapers, and it is worth saying before the
credentials section rather than after it.

What the paid products buy here is [further down](#what-the-paid-products-buy-here);
the short version is volume from many addresses and no browser to install,
not access.

---

## Read this first: `--pages` SCROLLS, it does not fetch pages

**A Spinny listing has exactly one page, and `?page=N` is silently ignored.**
Measured 2026-09-11 through a real browser:

```
/used-cars-in-delhi-ncr/s/           first ids: 31483859, 31064888, ...
/used-cars-in-delhi-ncr/s/?page=2    first ids: 31483859, 31064888, ...
/used-cars-in-delhi-ncr/s/?page=3    first ids: 31483859, 31064888, ...
```

Identical. Not an error, not an empty result — page one, three times, under
HTTP 200. And the page publishes no pagination markup at all: zero
`link[rel=next]`, zero `page=` hrefs, no "Load more" and no numbered anchors
on a fully scrolled 15 MB capture.

So a listing is **one infinitely scrolling page** that hydrates 20 cars at a
time from an `api.spinny.com` XHR, and `--pages N` means *N batches of 20,
reached by scrolling*. `--pages 5` asks for 100 cars.

Two consequences:

* **`--concurrency` above 1 is refused**, in every engine, with that reason.
  Page 5 of an infinitely scrolling grid has no address to hand a worker. To
  parallelise, run several scrapes across Spinny's own filter URLs —
  `/used-maruti-suzuki-cars-in-delhi-ncr/s/`,
  `/used-cars-under-5-lakh-rs-in-delhi-ncr/s/` — which **are** separate
  addresses.
* **`pages_completed` in the sidecar counts batches of 20, not URLs**, and it
  can legitimately exceed `pages_requested`: one scroll round sometimes
  delivers two or three batches.

---

## What a healthy run looks like

All measured 2026-09-11, local Chromium, no key, no proxy:

| Run | Cars | Priced | All-in ≥ displayed | Site's own total | Status |
|---|---|---|---|---|---|
| `/used-cars-in-delhi-ncr/s/ --pages 3` | 62 | 62/62 (100%) | 62/62 (100%) | 1546 | complete |
| `/used-cars-in-bangalore/s/ --pages 2` | 62 | 62/62 (100%) | 62/62 (100%) | 822 | complete |
| `/used-luxury-cars-in-delhi-ncr/s/ --pages 4` | 40 | 40/40 (100%) | 40/40 (100%) | 40 | complete |
| `--mode detail` on one car | 1 | 1/1 | 1/1 | — | complete |

The luxury run is the useful one to look at: it took **40 of the 40 cars the
site advertises**, so the scroll ran to the end of a real listing rather than
to a threshold.

**Completeness is arithmetic here, not a guess.** Spinny prints its own total
beside the listing heading — "1546 Used cars in Delhi NCR" — so a run holding
62 of them is short by 1484, and the sidecar says so:

```json
"completeness": {"total_available": 1546, "rows_collected": 62,
                 "short_by": 1484, "scroll_settled": false,
                 "reached_target": true}
```

**That total moves between runs.** Two runs eleven minutes apart read 1546 and
1566 for the same listing. It is inventory, not a constant — do not pin a
test to it.

### All three engines agree

The same URL through all three, 2026-09-11: **62 rows each, the same 62 car
ids, and zero differing columns** on a car compared field by field across
them.

---

## Install

One engine, in its own virtualenv. The three engines' pins are mutually
unsatisfiable — playwright and pyppeteer disagree on `pyee`, pyppeteer and
selenium on `urllib3` — so installing all three into one environment lets pip
resolve the conflict by downgrading something you wanted.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium        # not needed with --cdp-endpoint
```

Swap `requirements-playwright.txt` for `requirements-selenium.txt` or
`requirements-puppeteer.txt` for the other two. Python 3.9+.

---

## Credentials go in `.env`, never on the command line

A secret in `argv` is readable by anything that can run `ps`, and it lands in
your shell history.

```bash
cp .env.example .env      # then fill in what you use
python3 env_config.py     # prints what was picked up, without printing secrets
```

Precedence, highest first: **explicit flag → exported variable → `.env` →
default**. A `.env` never overrides something you typed.

`.env.example` documents exactly the four variables the code reads, and the
offline suite asserts the two sets are equal in both directions.

---

## Run

```bash
# a city listing, five batches of 20 cars, JSON and CSV
python playwright_scraper.py \
    --url "https://www.spinny.com/used-cars-in-delhi-ncr/s/" \
    --pages 5 --format both

# a filtered listing — these ARE separate addresses, so several run in parallel
python playwright_scraper.py \
    --url "https://www.spinny.com/used-luxury-cars-in-delhi-ncr/s/"

# one car, with the exact odometer, the owner count, the colour and the insurance
python playwright_scraper.py --mode detail \
    --url "https://www.spinny.com/buy-used-cars/gurgaon/nissan/magnite/xv-premium-2021/31276728/"
```

### Every flag

| Flag | What it does |
|---|---|
| `--url` | The listing or car URL. Or `SPINNY_URL` in `.env`. |
| `--mode` | `listing` (default) or `detail`. |
| `--pages` | How many **batches of 20** to scroll to. Not URLs — see above. |
| `--category` | Label for the `category` column. Defaults to what the URL selects. |
| `--format` | `json`, `csv` or `both`. |
| `--out` | Output prefix (default `spinny_products`). |
| `--delay` | Seconds between pages. Nothing to pace here; kept for family parity. |
| `--retries` | Attempts per page load (default 3), pause doubling. |
| `--retry-delay` | Seconds before the first retry (default 2). |
| `--concurrency` | Accepted and **refused** with the reason. See above. |
| `--proxy` | One proxy URL. Credentials go through the driver, never argv. |
| `--proxy-file` | One proxy URL per line, to rotate across. |
| `--proxy-rotate` | `per-run` (default) or `per-page`. |
| `--proxy-shuffle` | Shuffle the pool at startup. |
| `--proxy-block-retries` | Retries from OTHER exits when a page comes back unusable (default 2). |
| `--twocaptcha-key` | 2Captcha key. Prefer `.env`. |
| `--captcha-api` | `v2` (default) or the legacy `v1`. |
| `--solve-captcha` | `when-blocked` (default) or `always`. Buys nothing here — see below. |
| `--min-score` | reCAPTCHA v3 score to request (0.3 / 0.7 / 0.9). |
| `--cdp-endpoint` | Drive a remote browser over CDP instead of launching one. |
| `--allow-empty` | Write files even with 0 rows. Off by default. |
| `--dump-html` | Save the exact HTML the parser saw — on success too. |
| `--headless` / `--headful` | Default headless. |

Engine-specific, and the differences are asserted by the offline suite so
this table cannot go stale:

| Flag | playwright | selenium | pyppeteer |
|---|---|---|---|
| `--locale` | yes | — | — |
| `--fingerprint`, `--fp-tags`, `--fp-country` | yes | yes | — |
| `--chromium-path` | — | — | yes |

<a name="which-engine"></a>
### Which engine

**Playwright is the primary engine.** The other two are kept at parity and
must agree with it on exit codes, run status, and whether a run crashes or
spends money — the decisions all three share live in `page_flow.py` and
`output_writer.finish_run()`.

Known limits, stated here rather than left to be discovered:

* **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port`; chromedriver's `debuggerAddress` takes a bare
  `host:port` with nowhere to put a password. A credentialed endpoint is
  refused with exit 2 rather than connected to and silently failing.
* **Selenium cannot authenticate a proxy at all.** Credentials are stripped
  and a warning says so.
* **pyppeteer is effectively unmaintained**, and its own README points at
  Playwright. On Apple Silicon it downloads an x86_64 Chromium that will not
  open its DevTools socket — pass `--chromium-path` at a Chrome you already
  have. Verified working that way on 2026-09-11.
* **Chromium cannot authenticate a SOCKS5 proxy.** A `socks5://user:pass@…`
  entry is refused with exit 2 rather than being sent with the credentials
  silently dropped. Use an `http://` proxy entry.

### Modes

`--mode listing` reads the rendered grid. `--mode detail` reads one car out
of its own `["Product","Car"]` JSON-LD and its overview table, which adds
nine columns a card cannot carry: the **exact** odometer reading, the
previous-owner count, the registration year and month, the colour, the
seating capacity, the insurance validity and type, and the availability.

`sku` is the same id in both modes, so a listing row and a detail row join
exactly — and they were checked against each other on a real pair. For car
`31276728`, both modes report `price` 545,000 and `price_all_in` 560,000; the
card says `km_driven` 23,500 and the detail page says `km_driven_exact`
23,675, which is the site's own rounding rather than a disagreement.

---

## Output

One row per car, same field order in JSON and CSV. `sample_output.json` and
`sample_output.csv` are cut from the runs above.

The first sixteen columns are the family's, byte-identical across every
scraper in this org: `source`, `scraped_at`, `url`, `sku`, `title`, `brand`,
`price`, `currency`, `original_price`, `discount_pct`, `rating`,
`review_count`, `in_stock`, `image_url`, `category`, `price_source`.

Then Spinny's own: `page`, `position`, `model`, `variant`, `year`,
`km_driven`, `fuel_type`, `transmission`, `rto`, `hub`, `car_city`,
`assurance`, `price_all_in`, `discount_amount`, `emi_monthly`, `tag`, and the
detail-only `km_driven_exact`, `owners`, `registration_year`,
`registration_month`, `color`, `seating_capacity`, `insurance_validity`,
`insurance_type`.

### Four prices per car, and they disagree

This is the thing to understand before using the numbers:

| Column | What it is |
|---|---|
| `price` | The **displayed** price. Excludes RC transfer and insurance. Quantised to two decimals of a lakh by the site itself ("3.88 Lakh"). |
| `price_all_in` | The site's own `data-price` attribute — the exact figure **including** those fees (392,000 for that same car). |
| `original_price` | The struck-through was-price, on the **displayed** basis. |
| `discount_amount` | Spinny's own exact rupee discount, off its badge. |

`price` and `original_price` are one basis; `price_all_in` is another. Mixing
them computes a negative discount that looks entirely plausible.
`discount_pct` is computed from the displayed pair and cross-checked against
`discount_amount`; it is `null` — never 0, never negative — when the two
figures are not what they were taken for. The all-in figure was the higher of
the two on **482 of 482** measured cards, and every run reports that share.

### Exit codes

`0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero cars ·
`5` remote API error · `6` partial.

A run that finds nothing **writes nothing**, so last night's good output is
never replaced by `[]`. Pass `--allow-empty` if an empty result is the answer
you want recorded. An empty CSV still carries its header.

Every run writes `<out>.meta.json` beside the data: `status`, `stop_reason`,
`pages_requested`, `pages_completed`, the scroll trace, the listing's own
heading and the completeness arithmetic. A **failed** run writes no sidecar —
it would contradict the previous good data still sitting there.

---

## Traps that look like bugs

**`hub` is null on ~2% of rows, and that is correct.** 10 of 705 measured
cards print no hub. Everything read by position in a card's span order breaks
on those ten; this parser reads by pattern instead, so the other fields
survive the gap.

**`original_price` is null on ~8% of rows, and that is correct.** 20 of 705
cards carry no strike price — and those same 20 carry no discount badge, so
they are *not discounted* rather than *not parsed*. `discount_pct` is null on
exactly those rows.

**`rating` and `review_count` are null on every row of every run.** Spinny
publishes no car-level rating anywhere. The five inspection scores a detail
page shows are about different *parts* of one car, and the customer reviews
it publishes are about Spinny. The columns stay because consumers across this
family read them by name.

**The headline abbreviates the make.** 178 of 705 cards print "Maruti Swift"
for a maruti-suzuki and "Mercedes CLA" for a mercedes-benz. `brand` comes
from the URL's own make segment, so it reads "Maruti Suzuki" while `title`
still says what the card said.

**`km_driven` is the site's own rounding.** Cards print "63.5K km";
`--mode detail` gives you 63,417 in `km_driven_exact`.

**A listing URL with no city in it returns zero cars, confidently.** Spinny
scopes inventory by city, so a path that names none serves the grid container
and leaves it empty — while the heading above it advertises the national
total. Measured 2026-09-11: `/used-suv-cars/s/` says "2549 Used SUV cars in
India" and renders none of them; `/used-maruti-suzuki-cars/s/` says 1787 and
renders none. Exit 4, and the URL rather than a fault. Add a city:
`/used-suv-cars-in-delhi-ncr/s/`. `/used-cars` (no `/s/`) is an SEO landing
page with no grid at all — same answer.

**`delhi-ncr` is a region, not a city.** Its listing returns cars whose own
city segments are delhi, gurgaon, ghaziabad, noida, faridabad, sonipat and
karnal. That is why `car_city` comes from the car's URL rather than the
listing's.

**A detail page's `image_url` differs from its card's.** Spinny names a
different photograph of the same car in its structured data. It is the site
publishing two pictures, not the parser reading one badly.

---

## What the paid products buy here

All four are separately-billed [2Captcha](https://2captcha.com) products
behind one key.

**Captcha solving buys nothing on this site, and the reason is worth knowing.**
Spinny *has* reCAPTCHA configured on every page it serves — 31 occurrences of
"recaptcha" on a capture that had just delivered 482 cars — but it is
**Enterprise v3, invisible**, with a badge the site's own CSS hides. v3 scores
a session in the background rather than challenging it, so there is nothing
rendered to solve. No rendered challenge was observed from any exit tested.
The solving path is wired up and capped at one purchase per page, because a
bot manager can be switched on between deploys.

Because of that, `recaptcha` and `g-recaptcha` are deliberately **not** block
markers here: a marker that matches every page of the site it is meant to
guard is worse than no marker.

**One thing to watch over `--cdp-endpoint`:** the Scraping Browser's own
auto-solve extension noticed Spinny's invisible v3 widget and logged
`CAPTCHA detected` / `sent to 2captcha for solving` on a page that had
already delivered 62 cars (2026-09-11; no `solveFinished` followed within the
run). Nothing on this site needs solving, so if you are watching your bill,
that is where to look.

**The Scraping Browser API** (`--cdp-endpoint`) is a remote browser with a
persistent profile and a chosen exit country — no Chromium to install and no
infrastructure to patch. Verified 2026-09-11: 62 cars from
`/used-cars-in-delhi-ncr/s/`, identical to the local run. One live connection
per `pid`; a second concurrent run on the same profile gets a 500
(`profile_locked`, exit 5).

**Proxies** buy volume from many addresses. You are unlikely to need one for a
single run. `--proxy-file` with `--proxy-rotate per-page` relaunches the
browser on each rotation, because replaying a bot manager's cookies from a
second address is a stronger signal than either address alone.

**Fingerprints** (`--fingerprint`) buy a consistent device identity — the UA,
locale, timezone, screen and device pixel ratio all agreeing with one another
and with your exit country. Verified 2026-09-11: fingerprint 3088655 (IN)
applied to a live run.

**The Scraper API** (`scraper_api_client.py`) is a fourth, browserless
engine — and on this site it returns **nothing**, by construction. Measured
2026-09-11: HTTP 200, 1,873,849 bytes, **0 cars**, $0.0005, exit 4. The first
response is a shell; the grid arrives by XHR after load. Use a browser engine
here. The client ships because it is family core and is genuinely the right
tool on sites whose grid is server-rendered.

---

## Comparing two runs

```bash
python diff_runs.py --old monday.json --new tuesday.json --fail-on-change
```

Reports added, removed and changed cars by `sku`. It **refuses** to compare
two runs that are not both `complete`, or that were taken in different modes:
a partial run's un-fetched cars would otherwise read as sold.

Tracked: `price`, `original_price`, `discount_pct`, `currency`, `in_stock`,
`price_all_in`, `discount_amount`. Both price bases, on purpose — a car whose
displayed price is unchanged while its all-in figure rose has had a *fee*
changed, not a price cut.

A price difference that comes with a `price_source` difference is reported as
`source_changed`, not `changed`, and `--fail-on-change` ignores it: it says
something about our two snapshots, not about the site.

---

## Tests

```bash
python3 smoke_test.py     # 600+ offline checks, no network, no browser
pytest                    # the same suite, wrapped as one test
```

The suite passes with **no engine library installed at all** — each engine
group reports a skip instead, and CI's `engine-smoke` job installs all three
in separate virtualenvs and fails if any group skips, because "skipped,
engine absent" reads exactly like a passing run.

Fixtures are real captures, trimmed to whole cards and verified to parse
identically to the untrimmed original before being committed.

`canary.yml` runs one real listing daily and asserts the row floor, the price
coverage, the two price bases and the run status.

---

## Not supported

* **Sign-in, checkout, or anything behind an account.** This reads public
  listing and car pages.
* **`api.spinny.com`.** The site has a JSON API; this scraper drives a
  browser and reads the rendered grid. Pointing it at the API host is refused
  with that reason.
* **A `--country` flag.** Spinny is one storefront serving one country in one
  currency, so a country flag could only disagree with the URL. What varies is
  the **city**, and the city is part of the path.
* **Scraping at a volume Spinny has not agreed to.** Read the site's terms.
  This exists for price monitoring and market research at a human scale.

---

## Licence and scope

MIT. Open source, no telemetry, no account required to run it.

Built by [2Captcha](https://2captcha.com). Integrates 2Captcha's captcha
solving, Scraping Browser API, proxies and fingerprints — and, as the
measurements above say plainly, needs none of them for a single run against
this site.
