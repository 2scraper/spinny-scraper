"""
Spinny.com Web Scraper — Playwright Implementation (v3)
========================================================
Dual mode: direct API (fast, no browser) + browser fallback with network interception.
Features: 2captcha CAPTCHA solving, 2prx.com proxy support, fingerprint evasion.

Repository : https://github.com/2scraper/spinny-scraper
License    : MIT

Usage:
    python spinny_playwright.py --output cars.json
    python spinny_playwright.py --output cars.csv --format csv --pages 5
    python spinny_playwright.py --proxy user:pass@gate.2prx.com:8080
    python spinny_playwright.py --mode api    # direct API, no browser
    python spinny_playwright.py --mode browser # full browser with interception
"""

from __future__ import annotations
import argparse, asyncio, csv, json, logging, os, random, re, sys, time
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urljoin, urlencode

try:
    from playwright.async_api import async_playwright, Page, BrowserContext, Response
except ImportError:
    sys.exit("pip install playwright && playwright install chromium")

try:
    from twocaptcha import TwoCaptcha
except ImportError:
    TwoCaptcha = None

# ---------------------------------------------------------------------------
BASE_URL = "https://www.spinny.com"
API_BASE = "https://api.spinny.com/v3/api/listing/light/v5"

# Discovered URL patterns for browser mode
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

# API filter values (for direct API mode)
API_FILTERS = {
    "suv": {"body_type": "SUV"}, "sedan": {"body_type": "Sedan"},
    "hatchback": {"body_type": "Hatchback"}, "muv": {"body_type": "MUV"},
    "petrol": {"fuel_type": "Petrol"}, "diesel": {"fuel_type": "Diesel"},
    "cng": {"fuel_type": "CNG"}, "electric": {"fuel_type": "Electric"},
    "automatic": {"transmission": "Automatic"}, "manual": {"transmission": "Manual"},
    "maruti-suzuki": {"make": "Maruti Suzuki"}, "hyundai": {"make": "Hyundai"},
    "tata": {"make": "Tata"}, "honda": {"make": "Honda"}, "kia": {"make": "Kia"},
    "mahindra": {"make": "Mahindra"}, "toyota": {"make": "Toyota"},
    "volkswagen": {"make": "Volkswagen"}, "ford": {"make": "Ford"},
    "renault": {"make": "Renault"}, "bmw": {"make": "BMW"},
    "mercedes-benz": {"make": "Mercedes-Benz"},
    "delhi-ncr": {"city": "delhi-ncr"}, "bangalore": {"city": "bangalore"},
    "hyderabad-city": {"city": "hyderabad"}, "mumbai": {"city": "mumbai"},
    "pune": {"city": "pune"}, "chennai": {"city": "chennai"},
    "kolkata": {"city": "kolkata"}, "ahmedabad": {"city": "ahmedabad"},
    "jaipur": {"city": "jaipur"}, "lucknow": {"city": "lucknow"},
}

DEFAULT_CATEGORIES = ["all"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("spinny-pw")

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
# Parser matched to actual Spinny API v5 field names
# ---------------------------------------------------------------------------

def parse_spinny_results(results: list[dict], category: str) -> list[CarListing]:
    """Parse car objects from api.spinny.com/v3/api/listing/light/v5 response."""
    out = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    for c in results:
        if not isinstance(c, dict):
            continue
        L = CarListing(category=category, scraped_at=now)
        L.spinny_id = str(c.get("id", ""))
        L.make = c.get("make", "")
        L.model = c.get("model", "")
        L.variant = c.get("variant", "")
        L.title = f"{c.get('make_year', '')} {L.make} {L.model} {L.variant}".strip()

        # Price
        price = c.get("price")
        if isinstance(price, (int, float)):
            lakh = price / 100000
            L.price = f"₹ {lakh:.2f} Lakh" if lakh < 100 else f"₹ {price:,.0f}"
        elif price:
            L.price = str(price)

        L.year = str(c.get("make_year") or c.get("registration_year") or "")
        L.fuel_type = c.get("fuel_type", "")
        L.transmission = c.get("transmission", "")
        if c.get("transmission_sub_type"):
            L.transmission += f" ({c['transmission_sub_type']})"

        # Mileage / kilometers
        km = c.get("mileage") or c.get("round_off_mileage") or ""
        if isinstance(km, (int, float)):
            L.kilometers = f"{km:,.0f} km"
        elif km:
            L.kilometers = str(km)

        L.owner_type = str(c.get("no_of_owners", ""))
        if L.owner_type and L.owner_type.isdigit():
            n = int(L.owner_type)
            L.owner_type = f"{n}{'st' if n==1 else 'nd' if n==2 else 'rd' if n==3 else 'th'} Owner"

        L.location = c.get("city", "")
        hub = c.get("hub")
        if hub and hub != L.location:
            L.location = f"{L.location} ({hub})" if L.location else hub

        # EMI
        emi = c.get("emi")
        if isinstance(emi, (int, float)):
            L.emi = f"₹ {emi:,.0f}/mo"
        elif isinstance(emi, dict):
            L.emi = f"₹ {emi.get('amount', '')}/mo"
        elif emi:
            L.emi = str(emi)

        # URL
        purl = c.get("permanent_url", "")
        if purl:
            L.url = urljoin(BASE_URL, purl) if not purl.startswith("http") else purl
        elif L.spinny_id:
            L.url = f"{BASE_URL}/used-car/{L.spinny_id}/s/"

        # Image
        images = c.get("images") or []
        if images and isinstance(images, list) and isinstance(images[0], dict):
            f_obj = images[0].get("file", {})
            img_url = f_obj.get("absurl") or f_obj.get("taburl") or f_obj.get("moburl") or ""
            if img_url and not img_url.startswith("http"):
                img_url = "https:" + img_url
            L.image_url = img_url

        L.color = c.get("color", "")
        L.body_type = c.get("body_type", "")
        L.sold = "yes" if c.get("sold") else "no"

        if L.title:
            out.append(L)
    return out


def extract_results(body: dict) -> tuple[list[dict], int, str | None]:
    """Extract results list, total count, and next page URL from API response."""
    results = body.get("results", [])
    count = body.get("count", 0)
    next_url = body.get("next")
    return results, count, next_url


# ---------------------------------------------------------------------------
# 2captcha
# ---------------------------------------------------------------------------

class CaptchaSolver:
    def __init__(self, api_key=None):
        self.key = api_key or os.getenv("TWOCAPTCHA_API_KEY", "")
        self.solver = TwoCaptcha(self.key) if self.key and TwoCaptcha else None
        if self.solver: log.info("2captcha solver initialized")

    async def detect(self, page: Page) -> Optional[str]:
        for name, sel in [("turnstile","iframe[src*='challenges.cloudflare.com']"),
                          ("hcaptcha","iframe[src*='hcaptcha.com']"),
                          ("recaptcha","iframe[src*='google.com/recaptcha']")]:
            if await page.query_selector(sel): return name
        return None

    async def solve(self, page: Page, ctype: str) -> bool:
        if not self.solver:
            log.warning("No 2captcha solver"); return False
        sk = await self._sk(page, ctype)
        if not sk: log.error("No sitekey for %s", ctype); return False
        log.info("Solving %s via 2captcha …", ctype)
        try:
            if ctype == "turnstile": r = self.solver.turnstile(sitekey=sk, url=page.url)
            elif ctype == "recaptcha": r = self.solver.recaptcha(sitekey=sk, url=page.url)
            elif ctype == "hcaptcha": r = self.solver.hcaptcha(sitekey=sk, url=page.url)
            else: return False
            token = r.get("code", "")
            await self._inject(page, ctype, token)
            await page.wait_for_timeout(5000)
            return True
        except Exception as e:
            log.error("2captcha: %s", e); return False

    async def _sk(self, page, kind):
        smap = {"turnstile":"[data-sitekey]","recaptcha":".g-recaptcha[data-sitekey]","hcaptcha":".h-captcha[data-sitekey]"}
        el = await page.query_selector(smap.get(kind,"[data-sitekey]"))
        if el:
            sk = await el.get_attribute("data-sitekey")
            if sk: return sk
        if kind == "recaptcha":
            iframe = await page.query_selector("iframe[src*='google.com/recaptcha']")
            if iframe:
                src = await iframe.get_attribute("src") or ""
                m = re.search(r'k=([A-Za-z0-9_-]+)', src)
                if m: return m.group(1)
        content = await page.content()
        m = re.search(r'(?:data-sitekey|sitekey)["\s:=]+["\']([A-Za-z0-9_-]+)["\']', content)
        return m.group(1) if m else ""

    async def _inject(self, page, kind, token):
        if kind == "recaptcha":
            await page.evaluate("""(t)=>{
                document.querySelectorAll('#g-recaptcha-response,textarea[name="g-recaptcha-response"]')
                    .forEach(el=>{el.value=t;el.innerHTML=t});
                try{if(typeof ___grecaptcha_cfg!=='undefined'){
                    const find=(o)=>{if(!o||typeof o!=='object')return null;
                    if(typeof o.callback==='function')return o.callback;
                    for(const v of Object.values(o)){const c=find(v);if(c)return c}return null};
                    Object.values(___grecaptcha_cfg.clients).forEach(v=>{const c=find(v);if(c)c(t)})}}catch(e){}
            }""", token)
        elif kind == "turnstile":
            await page.evaluate("(t)=>{const i=document.querySelector('[name=\"cf-turnstile-response\"]');if(i)i.value=t;const cb=window.turnstileCallback||window.__cfCallback;if(typeof cb==='function')cb(t);}", token)
        elif kind == "hcaptcha":
            await page.evaluate("(t)=>{document.querySelectorAll('[name=\"h-captcha-response\"]').forEach(el=>{el.value=t;el.innerHTML=t});}", token)


# ---------------------------------------------------------------------------
# MODE 1: Direct API (no browser needed!)
# ---------------------------------------------------------------------------

async def scrape_api_direct(categories, max_pages, proxy=None, delay=(1.5, 3.0)):
    """Hit api.spinny.com directly — no browser, no CAPTCHA."""
    import httpx  # lightweight async HTTP

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Referer": "https://www.spinny.com/used-cars/s/",
        "Origin": "https://www.spinny.com",
    }
    proxy_url = f"http://{proxy}" if proxy else None
    all_results: list[CarListing] = []

    async with httpx.AsyncClient(headers=headers, proxy=proxy_url, timeout=30, follow_redirects=True) as client:
        for cat_name in categories:
            filters = API_FILTERS.get(cat_name, {})
            log.info("API mode: category=%s  filters=%s", cat_name, filters)

            for page_num in range(1, max_pages + 1):
                params = {"page": page_num, "city": filters.get("city", ""),
                          "o": "popular", "include_booked": "false"}
                for k in ("body_type", "fuel_type", "transmission", "make"):
                    if k in filters:
                        params[k] = filters[k]

                log.info("  page %d: %s?%s", page_num, API_BASE, urlencode(params))
                try:
                    resp = await client.get(API_BASE, params=params)
                    resp.raise_for_status()
                    body = resp.json()
                except Exception as e:
                    log.error("  API error: %s", e)
                    break

                results, count, next_url = extract_results(body)
                if not results:
                    log.info("  No more results at page %d", page_num)
                    break

                listings = parse_spinny_results(results, cat_name)
                all_results.extend(listings)
                log.info("  → %d listings (total: %d / %d)", len(listings), len(all_results), count)

                if not next_url:
                    log.info("  Last page reached")
                    break
                await asyncio.sleep(random.uniform(*delay))

    return all_results


# ---------------------------------------------------------------------------
# MODE 2: Browser with network interception
# ---------------------------------------------------------------------------

class SpinnyScraper:
    def __init__(self, proxy=None, captcha_api_key=None, headless=True, max_pages=10, delay=(2.0, 5.0)):
        self.proxy = proxy; self.headless = headless; self.max_pages = max_pages
        self.delay = delay; self.captcha = CaptchaSolver(captcha_api_key)
        self.results: list[CarListing] = []; self._api_buf: list[dict] = []

    def _launch(self):
        o = {"headless": self.headless, "args": ["--disable-blink-features=AutomationControlled","--no-sandbox","--disable-dev-shm-usage"]}
        if self.proxy:
            auth, server = (self.proxy.rsplit("@",1) if "@" in self.proxy else (None, self.proxy))
            o["proxy"] = {"server": f"http://{server}"}
            if auth and ":" in auth:
                u,p = auth.split(":",1); o["proxy"]["username"]=u; o["proxy"]["password"]=p
        return o

    async def _on_resp(self, resp: Response):
        if resp.status != 200: return
        url = resp.url
        if "api.spinny.com" in url and "listing" in url:
            try:
                body = await resp.json()
                if isinstance(body, dict) and "results" in body and isinstance(body["results"], list):
                    log.info("  ✓ Intercepted API: %s (%d results)", url[:100], len(body["results"]))
                    self._api_buf.append(body)
            except Exception: pass

    async def _goto(self, page, url) -> bool:
        try:
            r = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if r and r.status == 404: return False
            await page.wait_for_timeout(random.randint(3000, 6000))
            # Try solving CAPTCHA but don't fail if it doesn't work
            ct = await self.captcha.detect(page)
            if ct:
                log.warning("CAPTCHA: %s (attempting solve, API data may already be captured)", ct)
                for i in range(2):
                    if await self.captcha.solve(page, ct):
                        await page.wait_for_timeout(5000)
                        if not await self.captcha.detect(page):
                            log.info("CAPTCHA solved"); break
                    await page.wait_for_timeout(2000)
            return True
        except Exception as e:
            log.warning("Nav error: %s", e); return False

    async def _scroll(self, page):
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)

    async def scrape_all(self, categories=None):
        cats = categories or DEFAULT_CATEGORIES
        cmap = {n: CATEGORIES.get(n, f"/used-{n}-cars/s/") for n in cats}
        log.info("Browser mode: %s, max %d pages", list(cmap), self.max_pages)

        async with async_playwright() as pw:
            br = await pw.chromium.launch(**self._launch())
            ctx = await br.new_context(user_agent=random.choice(USER_AGENTS),
                viewport={"width":1440,"height":900}, locale="en-US", timezone_id="Asia/Kolkata")
            await ctx.add_init_script(STEALTH)
            page = await ctx.new_page()
            page.on("response", self._on_resp)

            for cat_name, cat_path in cmap.items():
                url = f"{BASE_URL}{cat_path}"
                for pg in range(1, self.max_pages + 1):
                    purl = url if pg == 1 else f"{url}?page={pg}"
                    log.info("category=%s  page=%d  url=%s", cat_name, pg, purl)

                    self._api_buf.clear()  # clear ONCE per page, before navigation
                    await self._goto(page, purl)
                    await self._scroll(page)

                    # Wait a bit more for API to arrive
                    if not self._api_buf:
                        log.info("  Waiting 5s more for API…")
                        await page.wait_for_timeout(5000)

                    # Parse captured API data
                    page_listings = []
                    for body in self._api_buf:
                        results, count, next_url = extract_results(body)
                        page_listings.extend(parse_spinny_results(results, cat_name))

                    if page_listings:
                        self.results.extend(page_listings)
                        log.info("  → %d listings (total: %d)", len(page_listings), len(self.results))
                    else:
                        log.info("  No listings captured — stopping %s", cat_name)
                        break

                    await asyncio.sleep(random.uniform(*self.delay))

            await br.close()

        # Dedup
        seen = set(); uniq = []
        for r in self.results:
            k = r.spinny_id or r.url or r.title
            if k and k not in seen: seen.add(k); uniq.append(r)
        self.results = uniq
        log.info("Done — %d unique listings", len(self.results))
        return self.results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_json(data, path):
    with open(path,"w",encoding="utf-8") as f: json.dump([asdict(d) for d in data], f, indent=2, ensure_ascii=False)
    log.info("Saved %d → %s", len(data), path)

def save_csv(data, path):
    if not data: return
    with open(path,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(data[0]).keys())); w.writeheader()
        for r in data: w.writerow(asdict(r))
    log.info("Saved %d → %s", len(data), path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Spinny.com scraper (Playwright)",
        epilog="Categories: " + ", ".join(sorted(CATEGORIES)))
    p.add_argument("-o","--output", default="spinny_cars.json")
    p.add_argument("-f","--format", choices=["json","csv"], default="json")
    p.add_argument("-p","--pages", type=int, default=10)
    p.add_argument("--mode", choices=["api","browser","auto"], default="auto",
                   help="api=direct HTTP (fast), browser=Playwright+interception, auto=try api first")
    p.add_argument("--proxy", default=None, help="user:pass@host:port (2prx.com)")
    p.add_argument("--captcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--categories", nargs="*", default=None)
    p.add_argument("--list-categories", action="store_true")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--no-headless", dest="headless", action="store_false")
    return p.parse_args()


async def main():
    args = parse_args()
    if args.list_categories:
        for n,p in sorted(CATEGORIES.items()): print(f"  {n:20s} → {p}")
        return

    cats = args.categories or DEFAULT_CATEGORIES
    mode = args.mode

    results = []

    if mode in ("api", "auto"):
        log.info("Trying direct API mode…")
        try:
            import httpx  # noqa
            results = await scrape_api_direct(cats, args.pages, proxy=args.proxy)
        except ImportError:
            log.warning("httpx not installed (pip install httpx). Falling back to browser mode.")
            if mode == "api":
                sys.exit("Direct API mode requires httpx: pip install httpx")
            mode = "browser"
        except Exception as e:
            log.warning("API mode failed: %s", e)
            if mode == "auto":
                log.info("Falling back to browser mode…")
                mode = "browser"
            else:
                raise

    if mode == "browser" or (mode == "auto" and not results):
        s = SpinnyScraper(proxy=args.proxy, captcha_api_key=args.captcha_key,
                          headless=args.headless, max_pages=args.pages)
        results = await s.scrape_all(cats)

    # Dedup final
    seen = set(); uniq = []
    for r in results:
        k = r.spinny_id or r.url or r.title
        if k and k not in seen: seen.add(k); uniq.append(r)
    results = uniq
    log.info("Final: %d unique listings", len(results))

    ext = "csv" if args.format == "csv" else "json"
    out = args.output if args.output.endswith(f".{ext}") else args.output.rsplit(".",1)[0] + f".{ext}"
    (save_csv if ext == "csv" else save_json)(results, out)


if __name__ == "__main__":
    asyncio.run(main())
