# Troubleshooting

Symptoms first, with what each one actually means. Everything here was
measured on **2026-09-11** against `spinny.com`; where a symptom has more than
one cause, the causes are ordered by how often they turned out to be the
answer.

The first thing worth knowing is what this site does **not** do: it does not
refuse anyone. A local Chromium on a home connection with no key and no proxy
returned 62 cars, and so did an Amsterdam datacentre exit, a Chennai
residential exit and the Scraping Browser API. So most of what follows is
about the URL you chose or the shape of the data — not about access.

---

## Zero rows, exit 4, and the log says "names no city"

**The URL is the problem, and the site will not tell you so.** Spinny scopes
its inventory by city. A listing path that names none serves the grid
container and leaves it empty — under a heading advertising the **national**
total:

```
/used-suv-cars/s/            heading: "2549 Used SUV cars in India"   rows: 0
/used-maruti-suzuki-cars/s/  heading: "1787 Used Maruti Suzuki cars"  rows: 0
/used-cars/s/                heading: "1546 Used cars in"             rows: 0
```

HTTP 200 every time. Add a city:

```
/used-suv-cars-in-delhi-ncr/s/
/used-maruti-suzuki-cars-in-bangalore/s/
```

`product_parser.CITIES` holds the 33 read off Spinny's own footer. An unknown
city is a **warning**, not a refusal — Spinny opens cities, and a scraper that
refuses a URL the site serves is worse than one that says it has not seen this
city before.

---

## Zero rows on `/used-cars` (no `/s/`)

**That is an SEO landing page, not a listing.** Banners, city links and FAQ
copy, with no grid on it at all. The run reports 0 rows and exit 4, and says
so by name. Listing URLs end in `/s/`.

---

## `?page=2` returns the same cars as page 1

**Working as measured. Spinny ignores `?page=N`.** Pages 1, 2 and 3 of one
listing URL returned byte-identical first cards under HTTP 200 — not an error,
not an empty result, page one three times.

A listing is one infinitely scrolling page that hydrates 20 cars at a time.
Use `--pages N`, which scrolls to N batches of 20. If you put `?page=4` in the
URL the run warns and tells you to use `--pages 4` instead; nothing else on
the site would ever tell you, because it answers 200.

---

## `--concurrency 4` is refused

**Deliberately, and with the reason.** Page 5 of an infinitely scrolling grid
has no address to hand a worker, so N workers would fetch page one N times
from N addresses. The flag is accepted and says no rather than appearing to
work.

To parallelise, split across Spinny's own filter URLs, which **are** separate
addresses:

```
/used-maruti-suzuki-cars-in-delhi-ncr/s/
/used-cars-under-5-lakh-rs-in-delhi-ncr/s/
```

Several runs across those parallelise properly.

---

## The run says "shell … has not painted" and then works

**Expected, on every listing.** Spinny's first response is a shell: 1.6 to
2 MB of navigation, filter rail and footer with the grid container present and
empty. `domcontentloaded` lands at about 1.1 s with zero cars in the DOM, and
the first batch of 22 arrives at about 4.7 s from an `api.spinny.com` XHR.

The engines wait for the grid rather than retrying, because a retry would
fetch another shell. If it stays a shell for the whole timeout, the run
reports 0 rows and exit 4 rather than pretending.

---

## Only 62 cars from a listing that advertises 1546

**Ask for more batches.** `--pages 3` asks for 60 cars, and the run stops once
it has them. The sidecar says so in arithmetic rather than in a guess:

```json
"completeness": {"total_available": 1546, "rows_collected": 62,
                 "short_by": 1484, "reached_target": true}
```

`--pages 50` scrolls to 1000. Reaching the whole of a large city listing takes
a while — the grid grows by 20 a round and each round waits for the XHR.

**The advertised total moves between runs**, because it is inventory: two runs
eleven minutes apart read 1546 and 1566 for the same listing. Do not pin a
test to it.

---

## `hub` is null on a few rows

**Correct.** 10 of 705 measured cards print no hub. This matters more than it
looks: anything read by POSITION in a card's span order shifts up by one on
those ten and silently reports the wrong field. This parser reads by pattern
inside a structurally-scoped card — the fuel type because it is one of five
words, the RTO because it matches `[A-Z]{2}\d{1,2}[A-Z]{0,3}`, the odometer
because it ends in "km" — so the other columns survive the gap.

---

## `original_price` and `discount_amount` are null on some rows

**Those cars are not discounted.** 20 of 705 cards carry no strike price, and
those same 20 carry no discount badge — the two travel together. So
`discount_pct` is null on exactly those rows, rather than 0.

A row with one and not the other WOULD be a fault, and the canary checks for
it.

---

## `rating` and `review_count` are null on every row

**Spinny publishes neither, anywhere.** The five inspection scores a detail
page shows are about different *parts* of one car, and the customer reviews
the site publishes are about Spinny rather than about a vehicle. The columns
stay because every scraper in this family carries them and consumers read them
by name.

If either ever fills up, something is reading Spinny's own aggregate rating —
the one in the marketing `Product` block — into a car's row. The canary fails
on that.

---

## The discount looks wrong, or a price does not match the site

**There are four prices per car and they are on two different bases.** Read
this before comparing anything:

| Column | Basis |
|---|---|
| `price` | displayed, excludes RC transfer and insurance |
| `original_price` | displayed, the struck-through was-price |
| `price_all_in` | the site's own `data-price`: includes those fees |
| `discount_amount` | the site's own exact rupee figure, off the badge |

`price` and `original_price` are one basis; `price_all_in` is the other.
Mixing them computes a negative discount that looks entirely plausible. The
all-in figure was the higher of the two on 482 of 482 measured cards, and
every run reports that share — if it drops, the two reads have been crossed.

`discount_pct` is computed from the displayed pair and cross-checked against
`discount_amount`. It is `null` — never 0, never negative — when the two
figures are not what they were taken for.

---

## `km_driven` disagrees with the car's own page

**The card rounds it and the detail page does not.** Cards print "63.5K km",
so `km_driven` is 63500; `--mode detail` reads 63,417 into `km_driven_exact`
out of the car's structured data. Both are correct; only one is exact.

---

## `image_url` differs between a listing row and a detail row

**Spinny names a different photograph of the same car** in its structured data
than on its card (`5edaaa72…` against `7abfc170…` for one car). It is the site
publishing two pictures, not the parser reading one badly. Worth knowing
before reading a listing-versus-detail diff.

---

## `brand` says "Maruti Suzuki" but the title says "Maruti"

**The headline abbreviates the make, on 178 of 705 cards.** "Maruti Swift" for
a maruti-suzuki, "Mercedes CLA" for a mercedes-benz. The URL's own make
segment does not abbreviate, so `brand` is built from that through a display
map; `title` stays exactly what the card said.

---

## `price_source`

- `dom+attr` — the rendered card, with the site's own `data-price` attribute
  present and consistent. The normal case on a listing: 705 of 705.
- `dom` — the card only; the attribute was missing.
- `dom+jsonld` — `--mode detail`: the displayed price from the page, with the
  structured `offers.Price` in `price_all_in`.

**There is no `jsonld` value for a listing, and that is measured.** A Spinny
listing page carries five `application/ld+json` blocks and not one of them
names a car — a BreadcrumbList, a LocalBusiness for the hubs, a FAQPage, and a
marketing Product rating Spinny itself. There is nothing to confirm a listing
price against.

---

## Exit 3, and the log says the response "was not built by Spinny"

**Suspect the access path before the site.** No Spinny refusal has ever been
observed, from any exit tested. What the check actually asks is whether the
markup references Spinny's own asset hosts: a real page does so 267 to 1040
times, and the two things that do not are an interstitial and **Chromium's own
network-error page** — which carries `<title>www.spinny.com</title>`, so a
title check would call it real, and no vendor marker anywhere.

In order of likelihood:

1. **A dead or unauthenticated proxy exit.** Look at the byte count in the
   log: a Chromium error page is ~180 KB of the browser's own markup.
2. **A `--cdp-endpoint` profile another run still holds** — that is exit 5,
   not 3, and says `profile_locked`.
3. **The site really has started scoring addresses.** That would be new, and
   the debug dump is what proves it. Try another exit.

---

## `--proxy` is refused with exit 2 and a message about SOCKS5

**Chromium cannot authenticate a SOCKS5 proxy.** A `socks5://user:pass@…` URL
would have its credentials silently dropped, so it is refused rather than
sent. Use an `http://` proxy entry for an authenticated exit.

Note also that **Selenium cannot authenticate a proxy at all** — it strips the
credentials and warns — and that with `--cdp-endpoint` set, `--proxy` is
ignored entirely (and no longer refused, because there is nothing for it to be
wrong about).

---

## `500 Internal Server Error` on connecting, exit 5

**A Scraping Browser profile allows one live connection.** Another run still
holds this `pid`. Wait, or use a different one. Give CI its own.

Exit 5 is deliberately distinct from exit 1: a harness that sees 1 goes
looking for a bug in the scraper instead of waiting.

---

## pyppeteer exits immediately with "Browser closed unexpectedly"

**Its bundled Chromium, not this code.** On Apple Silicon pyppeteer downloads
an x86_64 Chromium that runs under Rosetta far enough to print `--version` and
then fails to open its DevTools socket. Point it at a browser you already
have:

```bash
python puppeteer_scraper.py --chromium-path \
  "$(python3 -c 'from playwright.sync_api import sync_playwright
with sync_playwright() as p: print(p.chromium.executable_path)')" \
  --url "https://www.spinny.com/used-cars-in-delhi-ncr/s/"
```

Verified working that way on 2026-09-11: 62 cars, identical to the Playwright
run.

---

## pyppeteer prints a wall of asyncio tracebacks after a successful run

**The run succeeded; this is teardown noise, and one line of it cannot be
suppressed from inside the process.**

pyppeteer's websocket connection is torn down after the event loop has
stopped, so its `_recv_loop` coroutine raises into a loop that is no longer
running. Most of that noise is caught by an exception handler this engine
installs, which inspects the asyncio *message* as well as the exception —
several of these arrive with no exception object at all, so a handler looking
only at `context["exception"]` lets them through.

The one that gets past everything looks like this:

```
Exception ignored in: <coroutine object Connection._recv_loop at 0x...>
Traceback (most recent call last):
  ...
```

CPython's garbage collector prints it at **interpreter shutdown**, after the
loop is gone and after the exit code has already been decided. No loop
handler can reach it, and the only thing that could — a global unraisable
hook — would swallow real bugs with it. So it is documented rather than
hidden. Check the exit code and the output file; both are correct.

---

## Selenium refuses a `ws://user:pass@…` endpoint with exit 2

**chromedriver cannot send credentials over CDP.** Its `debuggerAddress` takes
a bare `host:port` with nowhere to put a password, unlike Playwright's
`connect_over_cdp` and pyppeteer's `browserWSEndpoint`, which authenticate on
the WebSocket upgrade. Refusing is the honest answer; use another engine for a
credentialed endpoint.

---

## `scraper_api_client.py` returns 0 cars

**By construction, and it is not broken.** Measured 2026-09-11: HTTP 200,
1,873,849 bytes, 0 cars, $0.0005, exit 4. The first response is a shell and the
grid arrives by XHR after load, so a single browserless fetch cannot see a
listing here whatever it is routed through. Use a browser engine.

---

## `fingerprint_client.py` says "No API key" while everything else works

Fixed in 0.1.0 — it now reads `.env` like every other CLI here. If you are on
an older checkout, export the key:

```bash
export TWOCAPTCHA_KEY=...
```

And note that `--fp-tags` takes **one** OS-family tag: `Windows`,
`Microsoft Windows` or `Android`. `Chrome`, `Desktop`, `Mobile` and every
combination are rejected by the API with HTTP 400.

---

## `--solve-captcha` never solves anything

**There is nothing to solve.** Spinny loads reCAPTCHA **Enterprise v3,
invisible**, on every page it serves — 31 occurrences of "recaptcha" on a
capture that had just delivered 482 cars — and v3 scores a session rather than
challenging it. Nothing is rendered for a visitor to solve.

Two consequences worth knowing:

- `recaptcha` and `g-recaptcha` are deliberately **not** block markers here. A
  marker that matches every page of the site it guards is worse than no
  marker.
- Over `--cdp-endpoint`, the Scraping Browser's own auto-solve extension
  noticed that invisible widget and logged `CAPTCHA detected` / `sent to
  2captcha for solving` on a page that had already delivered 62 cars. If you
  are watching your bill, that is where to look.

---

## The parser used to work and now returns empty columns

Something on the site moved. In the order worth checking:

1. **Run with `--dump-html`** and open the snapshot. The engines write it on
   success too, precisely so a right-count-wrong-columns run can be told from
   a too-early snapshot.
2. **Check the card anchor.** Everything is scoped to
   `[data-id="landing-plp-container"]` and `a[href^="/buy-used-cars/"]`. The
   `ds-*` classes around them are a design-system vocabulary — `ds-body-small`
   wraps the fuel type, the transmission, the RTO code and the hub name
   identically — so nothing anchors on them and nothing should.
3. **Check the price node**, `[data-base-component="Pricing"]`, and the
   shortlist heart, `#shortlist_icon[data-price]`, which carries the all-in
   figure and the id.
4. **Run the offline suite.** Its fixtures are real captures; if they still
   pass and a live run does not, the site changed rather than the code.

---

## `pytest` or `python3 smoke_test.py` fails after an edit

The suite is one file of plain functions with inline fixtures. A few checks
exist to catch specific classes of drift and are worth reading rather than
silencing:

- **"every page_flow call matches its signature"** — an engine is calling a
  shared function with the wrong arguments. That crashes on the first fetch
  and is invisible to `--help` and to `compileall`.
- **"all three engines share the same coverage thresholds"** — one engine
  would start reporting a problem its twins call healthy.
- **"ships no worker pool"** — the concurrency machinery is deliberately
  absent on this site; adding it back has to be a decision taken together
  with `product_parser.PAGINATED_KINDS`.
- **"no shipped file says …"** — the wording rules. See CONTRIBUTING.md.

---

## Where to look next

- `product_parser.py` — everything this repo knows about Spinny. Its docstring
  is the fastest way to understand the site.
- `page_flow.py` — every decision all three engines must make identically,
  with the measurement beside each one.
- `output_writer.py` — the row schema, the exit codes, and the note on the
  four prices.
- `<out>.meta.json` — what the last run actually did: status, stop reason,
  the scroll trace and the completeness arithmetic.
