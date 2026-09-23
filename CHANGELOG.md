# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. In practice that means: **a patch release fixes things**
— it does not promise that every flag and every default is frozen. Where a
patch changes behaviour an existing user would notice, the release notes lead
with it, so nobody discovers it from a bill or from a diff.

---

## [Unreleased]

### Fixed

- Leftovers from the repos this one was bootstrapped from:
  `playwright_scraper.py --retries` help said an empty "hub category" is a
  correct answer, and its retry comment spoke of "one page past the end of a
  listing", which cannot happen on a site that ignores `?page=N`. Both now
  say what the pyppeteer and Selenium engines already said: an SEO landing
  page or a cityless URL. `CONTRIBUTING.md` said the same thing the same
  way and is fixed too.
- The bug-report template's "What you expected" placeholder was another
  site's ("96 products ... a category page"); it now uses this repo's
  20-cars-per-batch figure.
- `captcha_solver.py` no longer points at a "No DataDome solver" section that
  does not exist.
- `SECURITY.md` said this project has no releases or tags; it has both.

## [0.1.1] — 2026-09-11

### Fixed

- **`fingerprint_client.py` read `--key`'s default straight from
  `os.environ`, which skips the placeholder rule.** The `.env` half of this
  was already fixed in 0.1.0 — found the first time `--fingerprint` was run
  live here — but with `env_config.apply()` AFTER parsing, which leaves the
  argparse default reading the environment directly. So an exported
  placeholder still reached the API.

  Measured with `TWOCAPTCHA_KEY=your_2captcha_api_key_here` exported: the old
  path reported "Fingerprint API rejected the key (401) — note this is a
  separate subscription", sending the reader off to check a subscription they
  never needed; the loader says "still set to the placeholder from
  .env.example" instead.

  This converges the repo onto the shape the whole family now shares. The
  same defect, in its `.env` form, was found in five sibling repos in the
  same pass and fixed there — farfetch 0.4.3, amazon 0.1.3, mediamarkt
  0.1.8, etsy 0.2.2, tokopedia 0.1.4 — and the check that pins it is
  byte-identical in all seven.

---

## [0.1.0] — 2026-09-11

First release as a member of the [2scraper](https://github.com/2scraper)
family. The repository previously held an April 2026 prototype; **nothing of
its behaviour is preserved** — see "Replaced, not extended" below before
upgrading anything that consumed its output.

### What it does

Scrapes Spinny used-car city listings, filtered listings and car detail pages
through Playwright, Selenium or pyppeteer, with JSON/CSV output, a
run-metadata sidecar and the family's exit-code contract.

Everything in the README is measured and dated. The headline numbers, all
2026-09-11: `--pages 3` against `/used-cars-in-delhi-ncr/s/` returns 62 cars
at 100% price coverage from a local Chromium with **no API key and no
proxy**; all three engines return the same 62 car ids with zero differing
columns; and the luxury listing returns 40 of the 40 cars the site itself
advertises.

### The one thing to understand: `--pages` scrolls

**A Spinny listing has exactly one page.** `?page=N` is silently ignored —
pages 1, 2 and 3 of one listing URL returned byte-identical first cards under
HTTP 200 — and the site publishes no `rel=next`, no numbered anchors and no
"load more" anywhere in a fully scrolled 15 MB capture. A listing is one
infinitely scrolling page that hydrates 20 cars at a time from an
`api.spinny.com` XHR.

So `--pages N` means *N batches of 20, reached by scrolling*, and
`--concurrency` above 1 is **refused** in every engine with that reason: page
5 of an infinitely scrolling grid has no address to hand a worker. The worker
pool the rest of this family ships is deliberately absent rather than present
and inert.

A planner that built `?page=N` would have fetched page one N times, found no
new car id, concluded the listing was exhausted, and reported a **complete**
run holding a twentieth of the catalogue.

### Replaced, not extended

The April prototype and this release share no data contract. If anything
consumed the old output, it needs rewriting rather than adjusting:

- **Prices are numbers.** `price` was the string the site prints ("3.88
  Lakh"); it is now `388000.0`, with `currency` as the ISO code `INR`. There
  are four prices per car and they disagree by design — see the README.
- **`kilometers` is `km_driven`, and it is an integer.** The card's own
  rounding ("63.5K km") becomes `63500`; `--mode detail` adds
  `km_driven_exact`, which was 63,417 for that car.
- **`spinny_id` is `sku`**, a string, and it is the same id in both modes, so
  a listing row and a detail row join exactly.
- **`make` is `brand`, and it no longer comes from the headline.** The card
  ABBREVIATES the make on 178 of 705 measured cards — "Maruti Swift" for a
  maruti-suzuki, "Mercedes CLA" for a mercedes-benz — so the brand is built
  from the URL's own make segment instead.
- **There are exit codes and a sidecar.** 0 ok · 1 crash · 2 bad usage ·
  3 blocked · 4 zero cars · 5 remote API error · 6 partial, plus
  `<out>.meta.json` per run. A run that finds nothing now writes nothing,
  rather than replacing good output with an empty file.
- **There is no `--mode api`.** The prototype called
  `api.spinny.com/v3/api/listing/light/v5` directly. This repo drives a
  browser and reads the rendered grid; pointing it at the API host is refused
  with that reason rather than with a generic "not a Spinny site", which
  would be false.
- **The prototype's category list was almost entirely cityless, and cityless
  listings render nothing.** `/used-suv-cars/s/`, `/used-maruti-suzuki-cars/s/`
  and the other 30-odd paths it shipped serve the grid container empty,
  because Spinny scopes inventory by city — while the heading above them
  advertises the national total. Measured 2026-09-11: `/used-suv-cars/s/`
  says "2549 Used SUV cars in India" and renders none of them. Those URLs now
  report 0 rows and exit 4 with the reason named, and the city forms
  (`/used-suv-cars-in-delhi-ncr/s/`) are what work.
- **No `--country` flag.** Spinny sells in one country from one hostname in
  one currency, so a country flag could only disagree with the URL. What
  varies is the **city**, and the city is part of the path.
- **The prototype's proxy host and its marketing wording for the remote
  browser are gone**, along with everything else the family's naming rules
  ban — the
  offline suite fails the build on those phrases, so they cannot come back by
  paste. Proxies are `2captcha.com/proxy`; the remote browser is the
  **Scraping Browser API**.

### What the live runs found, in this repo's own new code

Every one of these was invisible to a green offline suite, and each is pinned
by a check now.

- **`--fingerprint` could not read the key from `.env`.**
  `fingerprint_client.py` read only the exported environment, so a user who
  put `TWOCAPTCHA_KEY` where the README tells them to got "No API key" from
  that one command and nowhere else. Found the first time the fingerprint
  path was run live.
- **A `--proxy` the run was about to ignore could still end the run.** The
  proxy pool was built before `--cdp-endpoint` was checked, so a `.env`
  holding a SOCKS5 proxy — which Chromium cannot authenticate, and which the
  pool correctly refuses — blocked every remote run. Found on the first live
  run over the Scraping Browser API.
- **The readiness wait reported a timeout on a page that had painted.**
  `wait_for_count` returns as soon as the count REACHES the threshold, and
  the engines tested `found <= threshold`, so a detail page that painted in
  1.5 seconds logged "still had not painted after 30s". Off by one, in all
  three engines.
- **`pages_completed` disagreed between engines on identical runs.** It was
  derived from the scroll's last card count, which stops the moment the
  target is reached while the page keeps hydrating; three engines that all
  wrote 62 rows had counters reading 62, 42 and 62. It is now derived from
  the rows in the file.
- **A stalled lazy load reported a COMPLETE run holding 22 of 1546.** Found
  by the last live run of the release, through a rotating residential
  gateway: every request leaves from a different address, so the hydration
  XHR for the second batch never landed and the grid stopped growing. The
  scroll did exactly what it is told to do — three rounds with no new cards
  means "the listing ran out" — and the sidecar said `status: complete` with
  `short_by: 1524` beside it, contradicting itself.

  "The grid stopped growing" is a statement about our session; "the listing
  ran out" is one about the catalogue, and only the site's own advertised
  total turns one into the other. A settled scroll far below that total is
  now `scroll_stalled` — status `partial`, exit 6 — and
  `page_flow.listing_stop_reason` makes that decision once for all three
  engines.

### Two findings that shaped the parser

- **A listing page has no structured data about its cars.** Five
  `application/ld+json` blocks on a fully scrolled listing and not one names
  a vehicle: a BreadcrumbList, a LocalBusiness for the hubs, a FAQPage, and a
  marketing Product whose `aggregateRating` rates **Spinny**. So the
  URL-pattern path is promoted to primary, and `price_source` is `dom+attr`
  on every listing row. A DETAIL page is different and carries a real
  `["Product","Car"]` block — which spells the price **`offers.Price`, with a
  capital P**, so the schema.org spelling returns `None` on every row of
  every detail run. Both are accepted; only the capitalised one has ever been
  observed.
- **reCAPTCHA Enterprise v3 is on every page and challenges nothing.** 31
  occurrences of "recaptcha" on a capture that had just delivered 482 cars,
  an invisible widget with a hidden badge. v3 scores rather than challenges,
  so `--solve-captcha` has nothing to buy — and `recaptcha` and
  `g-recaptcha`, both inherited from sibling repos, were REMOVED from the
  block-marker set after being counted on a page known good. A marker that
  matches every page of the site it guards is worse than no marker.

  Worth watching over `--cdp-endpoint`: the Scraping Browser's own auto-solve
  extension noticed that invisible widget and logged `CAPTCHA detected` /
  `sent to 2captcha for solving` on a page that had already delivered 62
  cars.

### Family core, fixed here

- **`detect_page_state` ordered a heuristic above two unambiguous markers.**
  The "was this built out of the site's own assets?" threshold ran before the
  car's own JSON-LD and before the site's own zero-count heading, so a real
  page carrying fewer than ten asset references came back **blocked** — exit
  3 for a correct answer. Signals are now ordered by how much they prove.
- **Dead code removed rather than inherited.** `pagination_agrees`,
  `next_page_candidates` and an unused `Rs.` price pattern had no consumer on
  this site and are gone; `RETRY_ON_BLOCKED`, `pagination_is_addressable`,
  `page_param_warning`, `recaptcha_note`, `block_retry_budget` and
  `concurrency_refusal` are each read by all three engines, and the suite
  asserts it.

### The paid paths, each run live

- **Scraping Browser API** (`--cdp-endpoint`): 62 cars, identical to the
  direct run. Its own auto-solve extension noticed Spinny's invisible v3
  widget and logged `CAPTCHA detected` / `sent to 2captcha for solving` on a
  page that had already delivered those 62 cars — worth knowing if you are
  watching a bill, since nothing on this site needs solving.
- **Residential proxy**: verified on an Indian exit (Palghar, Maharashtra),
  62 cars at 100% price coverage. Note that Chromium cannot authenticate a
  SOCKS5 proxy — such a URL is refused with exit 2 rather than sent with its
  credentials dropped — and that a rotating gateway can stall the lazy load,
  which is what surfaced the `scroll_stalled` defect above.
- **Fingerprint API**: fingerprint 3088655 (IN) fetched and applied to a live
  run. `fingerprint_client.py` reading `.env` was fixed in the process.

### The Scraper API engine returns nothing here, by construction

`scraper_api_client.py` ships and is measured: HTTP 200, 1,873,849 bytes,
**0 cars**, $0.0005, exit 4. Spinny's first response is a shell and the grid
arrives by XHR afterwards, so a single browserless fetch cannot see a listing
at all. It is kept because it is family core and is the right tool on sites
whose grid is server-rendered — and because family core that exists in five
repos and is exercised in four is how untested inheritance ships.

[0.1.1]: https://github.com/2scraper/spinny-scraper/releases/tag/v0.1.1
[0.1.0]: https://github.com/2scraper/spinny-scraper/releases/tag/v0.1.0
