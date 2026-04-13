"""
Spinny.com Web Scraper — Puppeteer (Pyppeteer) Implementation (v3)
====================================================================
Dual mode: direct API (fast, no browser) + browser fallback with CDP interception.

Repository : https://github.com/2scraper/spinny-scraper
License    : MIT

Usage:
    python spinny_puppeteer.py --output cars.json
    python spinny_puppeteer.py --mode api --pages 5
    python spinny_puppeteer.py --mode browser --proxy user:pass@gate.2prx.com:8080
"""

from __future__ import annotations
import argparse, asyncio, csv, json, logging, os, random, re, sys, time
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urljoin, urlencode

try:
    from pyppeteer import launch
    from pyppeteer.page import Page
except ImportError:
    sys.exit("pyppeteer is required.  Install: pip install pyppeteer")

try:
    from twocaptcha import TwoCaptcha
except ImportError:
    TwoCaptcha = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("spinny-ppt")

STEALTH = """() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
    const gp = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return gp.call(this, p);
    };
}"""

# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.spinny.com"
API_BASE = "https://api.spinny.com/v3/api/listing/light/v5"

CATEGORIES = {
    "all":"/used-cars/s/","suv":"/used-suv-cars/s/","sedan":"/used-sedan-cars/s/",
    "hatchback":"/used-hatchback-cars/s/","muv":"/used-muv-cars/s/",
    "petrol":"/used-petrol-cars/s/","diesel":"/used-diesel-cars/s/",
    "cng":"/used-cng-cars/s/","electric":"/used-electric-cars/s/",
    "automatic":"/used-automatic-cars/s/","manual":"/used-manual-cars/s/",
    "under-3-lakh":"/used-cars-under-3-lakh-rs/s/",
    "3-to-4-lakh":"/used-cars-over-3-lakh-rs-under-4-lakh-rs/s/",
    "4-to-5-lakh":"/used-cars-over-4-lakh-rs-under-5-lakh-rs/s/",
    "5-to-6-lakh":"/used-cars-over-5-lakh-rs-under-6-lakh-rs/s/",
    "6-to-8-lakh":"/used-cars-over-6-lakh-rs-under-8-lakh-rs/s/",
    "8-to-10-lakh":"/used-cars-over-8-lakh-rs-under-10-lakh-rs/s/",
    "above-10-lakh":"/used-cars-over-10-lakh-rs/s/",
    "maruti-suzuki":"/used-maruti-suzuki-cars/s/","hyundai":"/used-hyundai-cars/s/",
    "tata":"/used-tata-cars/s/","honda":"/used-honda-cars/s/","kia":"/used-kia-cars/s/",
    "mahindra":"/used-mahindra-cars/s/","toyota":"/used-toyota-cars/s/",
    "volkswagen":"/used-volkswagen-cars/s/","ford":"/used-ford-cars/s/",
    "renault":"/used-renault-cars/s/","bmw":"/used-bmw-cars/s/",
    "mercedes-benz":"/used-mercedes-benz-cars/s/",
    "delhi-ncr":"/used-cars-in-delhi-ncr/s/","bangalore":"/used-cars-in-bangalore/s/",
    "hyderabad-city":"/used-cars-in-hyderabad/s/","mumbai":"/used-cars-in-mumbai/s/",
    "pune":"/used-cars-in-pune/s/","chennai":"/used-cars-in-chennai/s/",
    "kolkata":"/used-cars-in-kolkata/s/","ahmedabad":"/used-cars-in-ahmedabad/s/",
    "jaipur":"/used-cars-in-jaipur/s/","lucknow":"/used-cars-in-lucknow/s/",
}

API_FILTERS = {
    "suv":{"body_type":"SUV"},"sedan":{"body_type":"Sedan"},"hatchback":{"body_type":"Hatchback"},
    "muv":{"body_type":"MUV"},"petrol":{"fuel_type":"Petrol"},"diesel":{"fuel_type":"Diesel"},
    "cng":{"fuel_type":"CNG"},"electric":{"fuel_type":"Electric"},
    "automatic":{"transmission":"Automatic"},"manual":{"transmission":"Manual"},
    "maruti-suzuki":{"make":"Maruti Suzuki"},"hyundai":{"make":"Hyundai"},
    "tata":{"make":"Tata"},"honda":{"make":"Honda"},"kia":{"make":"Kia"},
    "mahindra":{"make":"Mahindra"},"toyota":{"make":"Toyota"},
    "volkswagen":{"make":"Volkswagen"},"ford":{"make":"Ford"},
    "renault":{"make":"Renault"},"bmw":{"make":"BMW"},"mercedes-benz":{"make":"Mercedes-Benz"},
    "delhi-ncr":{"city":"delhi-ncr"},"bangalore":{"city":"bangalore"},
    "hyderabad-city":{"city":"hyderabad"},"mumbai":{"city":"mumbai"},
    "pune":{"city":"pune"},"chennai":{"city":"chennai"},
    "kolkata":{"city":"kolkata"},"ahmedabad":{"city":"ahmedabad"},
    "jaipur":{"city":"jaipur"},"lucknow":{"city":"lucknow"},
}

DEFAULT_CATEGORIES = ["all"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>false});window.chrome={runtime:{}};"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CarListing:
    title: str = ""; price: str = ""; year: str = ""; fuel_type: str = ""
    transmission: str = ""; kilometers: str = ""; owner_type: str = ""
    location: str = ""; emi: str = ""; url: str = ""; image_url: str = ""
    category: str = ""; scraped_at: str = ""; make: str = ""; model: str = ""
    variant: str = ""; color: str = ""; body_type: str = ""; sold: str = ""
    spinny_id: str = ""


# ---------------------------------------------------------------------------
# Spinny API v5 parser
# ---------------------------------------------------------------------------

def parse_spinny_results(results: list[dict], category: str) -> list[CarListing]:
    out = []; now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    for c in results:
        if not isinstance(c, dict): continue
        L = CarListing(category=category, scraped_at=now)
        L.spinny_id = str(c.get("id", ""))
        L.make = c.get("make", ""); L.model = c.get("model", ""); L.variant = c.get("variant", "")
        L.title = f"{c.get('make_year', '')} {L.make} {L.model} {L.variant}".strip()
        price = c.get("price")
        if isinstance(price, (int, float)):
            lakh = price / 100000
            L.price = f"₹ {lakh:.2f} Lakh" if lakh < 100 else f"₹ {price:,.0f}"
        elif price: L.price = str(price)
        L.year = str(c.get("make_year") or c.get("registration_year") or "")
        L.fuel_type = c.get("fuel_type", ""); L.transmission = c.get("transmission", "")
        if c.get("transmission_sub_type"): L.transmission += f" ({c['transmission_sub_type']})"
        km = c.get("mileage") or c.get("round_off_mileage") or ""
        L.kilometers = f"{km:,.0f} km" if isinstance(km, (int, float)) else str(km)
        n = c.get("no_of_owners", "")
        if isinstance(n, int):
            L.owner_type = f"{n}{'st' if n==1 else 'nd' if n==2 else 'rd' if n==3 else 'th'} Owner"
        else: L.owner_type = str(n)
        L.location = c.get("city", "")
        hub = c.get("hub")
        if hub and hub != L.location: L.location = f"{L.location} ({hub})" if L.location else hub
        emi = c.get("emi")
        if isinstance(emi, (int, float)): L.emi = f"₹ {emi:,.0f}/mo"
        elif isinstance(emi, dict): L.emi = f"₹ {emi.get('amount', '')}/mo"
        elif emi: L.emi = str(emi)
        purl = c.get("permanent_url", "")
        L.url = urljoin(BASE_URL, purl) if purl else (f"{BASE_URL}/used-car/{L.spinny_id}/s/" if L.spinny_id else "")
        imgs = c.get("images") or []
        if imgs and isinstance(imgs[0], dict):
            f_ = imgs[0].get("file", {}); iu = f_.get("absurl") or f_.get("taburl") or ""
            L.image_url = ("https:" + iu) if iu and not iu.startswith("http") else iu
        L.color = c.get("color", ""); L.body_type = c.get("body_type", "")
        L.sold = "yes" if c.get("sold") else "no"
        if L.title: out.append(L)
    return out


def extract_results(body: dict):
    return body.get("results", []), body.get("count", 0), body.get("next")


# ---------------------------------------------------------------------------
# MODE 1: Direct API (sync, uses requests)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# MODE 1: Direct API (async, uses httpx)
# ---------------------------------------------------------------------------

async def scrape_api_direct(cats, max_pages, proxy=None):
    import httpx
    headers = {"User-Agent": USER_AGENTS[0], "Referer": f"{BASE_URL}/used-cars/s/", "Accept": "application/json"}
    proxy_url = f"http://{proxy}" if proxy else None
    all_r = []
    async with httpx.AsyncClient(headers=headers, proxy=proxy_url, timeout=30, follow_redirects=True) as cl:
        for cat in cats:
            filt = API_FILTERS.get(cat, {})
            for pg in range(1, max_pages + 1):
                params = {"page": pg, "city": filt.get("city", ""), "o": "popular", "include_booked": "false"}
                for k in ("body_type", "fuel_type", "transmission", "make"):
                    if k in filt: params[k] = filt[k]
                log.info("API %s page=%d", cat, pg)
                try:
                    r = await cl.get(API_BASE, params=params); r.raise_for_status(); body = r.json()
                except Exception as e: log.error("  %s", e); break
                res, cnt, nxt = extract_results(body)
                if not res: break
                lst = parse_spinny_results(res, cat); all_r.extend(lst)
                log.info("  → %d (total %d/%d)", len(lst), len(all_r), cnt)
                if not nxt: break
                await asyncio.sleep(random.uniform(1.5, 3.0))
    return all_r


# ---------------------------------------------------------------------------
# MODE 2: Browser with CDP network interception
# ---------------------------------------------------------------------------

class SpinnyScraper:
    def __init__(self, proxy=None, captcha_key=None, headless=True, max_pages=10):
        self.proxy = proxy; self.headless = headless; self.max_pages = max_pages
        self.results = []; self._buf = []

    async def scrape_all(self, cats):
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        if self.proxy:
            srv = self.proxy.rsplit("@", 1)[-1] if "@" in self.proxy else self.proxy
            args.append(f"--proxy-server=http://{srv}")
        browser = await launch(headless=self.headless, args=args, ignoreHTTPSErrors=True)
        try:
            page = await browser.newPage()
            await page.setUserAgent(random.choice(USER_AGENTS))
            await page.setViewport({"width": 1440, "height": 900})
            await page.evaluateOnNewDocument(STEALTH)
            if self.proxy and "@" in self.proxy:
                auth = self.proxy.rsplit("@", 1)[0]
                if ":" in auth:
                    u, p = auth.split(":", 1)
                    await page.authenticate({"username": u, "password": p})

            # CDP network interception
            cdp = await page.target.createCDPSession()
            await cdp.send("Network.enable")

            async def on_resp(ev):
                url = ev.get("response", {}).get("url", "")
                if ev.get("response", {}).get("status") == 200 and "api.spinny.com" in url and "listing" in url:
                    try:
                        br = await cdp.send("Network.getResponseBody", {"requestId": ev["requestId"]})
                        b = json.loads(br.get("body", ""))
                        if "results" in b:
                            log.info("  ✓ CDP: %s (%d results)", url[:100], len(b["results"]))
                            self._buf.append(b)
                    except Exception: pass
            cdp.on("Network.responseReceived", lambda e: asyncio.ensure_future(on_resp(e)))

            for cat in cats:
                path = CATEGORIES.get(cat, f"/used-{cat}-cars/s/")
                url = f"{BASE_URL}{path}"
                for pg in range(1, self.max_pages + 1):
                    purl = url if pg == 1 else f"{url}?page={pg}"
                    log.info("browser %s page=%d %s", cat, pg, purl)
                    self._buf.clear()
                    await page.goto(purl, {"waitUntil": "domcontentloaded", "timeout": 30000})
                    await asyncio.sleep(random.uniform(4, 7))
                    for _ in range(3):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(2)
                    if not self._buf: await asyncio.sleep(5)

                    found = []
                    for b in self._buf:
                        res, _, _ = extract_results(b)
                        found.extend(parse_spinny_results(res, cat))
                    if found:
                        self.results.extend(found)
                        log.info("  → %d listings", len(found))
                    else:
                        log.info("  No data — stopping %s", cat); break
                    await asyncio.sleep(random.uniform(2, 4))
        finally:
            await browser.close()

        seen = set(); uniq = []
        for r in self.results:
            k = r.spinny_id or r.url
            if k and k not in seen: seen.add(k); uniq.append(r)
        self.results = uniq
        return self.results


# ---------------------------------------------------------------------------
# Output + CLI
# ---------------------------------------------------------------------------

def save(results, path, fmt):
    if not results: log.warning("No data"); return
    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys())); w.writeheader()
            for r in results: w.writerow(asdict(r))
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump([asdict(d) for d in results], f, indent=2, ensure_ascii=False)
    log.info("Saved %d → %s", len(results), path)


async def main():
    p = argparse.ArgumentParser(description="Spinny.com scraper (Pyppeteer)",
        epilog="Categories: " + ", ".join(sorted(CATEGORIES)))
    p.add_argument("-o", "--output", default="spinny_cars.json")
    p.add_argument("-f", "--format", choices=["json", "csv"], default="json")
    p.add_argument("-p", "--pages", type=int, default=10)
    p.add_argument("--mode", choices=["api", "browser", "auto"], default="auto")
    p.add_argument("--proxy", default=None, help="user:pass@host:port (2prx.com)")
    p.add_argument("--captcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--categories", nargs="*", default=None)
    p.add_argument("--list-categories", action="store_true")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--no-headless", dest="headless", action="store_false")
    args = p.parse_args()

    if args.list_categories:
        for n, pa in sorted(CATEGORIES.items()): print(f"  {n:20s} → {pa}")
        return

    cats = args.categories or DEFAULT_CATEGORIES
    results = []

    if args.mode in ("api", "auto"):
        log.info("Trying direct API mode…")
        try:
            import httpx  # noqa
            results = await scrape_api_direct(cats, args.pages, args.proxy)
        except ImportError:
            log.warning("httpx not installed (pip install httpx). Falling back to browser.")
            if args.mode == "api": sys.exit("pip install httpx")
            args.mode = "browser"
        except Exception as e:
            log.warning("API failed: %s", e)
            if args.mode == "auto": args.mode = "browser"
            else: raise

    if args.mode == "browser" or (args.mode == "auto" and not results):
        s = SpinnyScraper(proxy=args.proxy, captcha_key=args.captcha_key,
                          headless=args.headless, max_pages=args.pages)
        results = await s.scrape_all(cats)

    # Dedup
    seen = set(); uniq = []
    for r in results:
        k = r.spinny_id or r.url or r.title
        if k and k not in seen: seen.add(k); uniq.append(r)
    results = uniq
    log.info("Final: %d unique listings", len(results))

    ext = "csv" if args.format == "csv" else "json"
    out = args.output if args.output.endswith(f".{ext}") else args.output.rsplit(".", 1)[0] + f".{ext}"
    save(results, out, ext)


if __name__ == "__main__":
    asyncio.run(main())
