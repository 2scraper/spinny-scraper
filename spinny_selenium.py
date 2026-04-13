"""
Spinny.com Web Scraper — Selenium Implementation (v3)
======================================================
Dual mode: direct API (fast, no browser) + browser fallback with selenium-wire interception.

Repository : https://github.com/2scraper/spinny-scraper
License    : MIT

Usage:  python spinny_selenium.py --output cars.json
        python spinny_selenium.py --mode api --pages 5
        python spinny_selenium.py --mode browser --proxy user:pass@gate.2prx.com:8080
"""

from __future__ import annotations
import argparse, csv, json, logging, os, random, re, sys, time
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urljoin, urlencode

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
except ImportError:
    sys.exit("pip install selenium")

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    ChromeDriverManager = None
try:
    from twocaptcha import TwoCaptcha
except ImportError:
    TwoCaptcha = None
try:
    from seleniumwire import webdriver as wire_webdriver; HAS_WIRE = True
except ImportError:
    HAS_WIRE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("spinny-sel")

# --- Shared config, model, parser (identical to Playwright version) ---
exec(open(os.path.join(os.path.dirname(__file__) or ".", "_shared.py")).read())  # noqa — will create below

def main():
    p = argparse.ArgumentParser(description="Spinny.com scraper (Selenium)")
    p.add_argument("-o","--output", default="spinny_cars.json")
    p.add_argument("-f","--format", choices=["json","csv"], default="json")
    p.add_argument("-p","--pages", type=int, default=10)
    p.add_argument("--mode", choices=["api","browser","auto"], default="auto")
    p.add_argument("--proxy", default=None)
    p.add_argument("--captcha-key", default=None)
    p.add_argument("--categories", nargs="*", default=None)
    p.add_argument("--list-categories", action="store_true")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--no-headless", dest="headless", action="store_false")
    args = p.parse_args()

    if args.list_categories:
        for n,pa in sorted(CATEGORIES.items()): print(f"  {n:20s} → {pa}")
        return

    cats = args.categories or DEFAULT_CATEGORIES
    results = []

    if args.mode in ("api","auto"):
        log.info("Trying direct API mode…")
        try:
            import requests as req
            results = scrape_api_sync(cats, args.pages, args.proxy)
        except ImportError:
            log.warning("requests not installed. Falling back to browser.")
            if args.mode == "api": sys.exit("pip install requests")
            args.mode = "browser"
        except Exception as e:
            log.warning("API failed: %s", e)
            if args.mode == "auto": args.mode = "browser"
            else: raise

    if args.mode == "browser" or (args.mode == "auto" and not results):
        results = scrape_browser_selenium(cats, args)

    # Dedup
    seen = set(); uniq = []
    for r in results:
        k = r.spinny_id or r.url or r.title
        if k and k not in seen: seen.add(k); uniq.append(r)
    results = uniq
    log.info("Final: %d unique", len(results))

    ext = "csv" if args.format == "csv" else "json"
    out = args.output if args.output.endswith(f".{ext}") else args.output.rsplit(".",1)[0]+f".{ext}"
    if ext == "csv":
        if results:
            with open(out,"w",newline="",encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys())); w.writeheader()
                for r in results: w.writerow(asdict(r))
    else:
        with open(out,"w",encoding="utf-8") as f: json.dump([asdict(d) for d in results], f, indent=2, ensure_ascii=False)
    log.info("Saved %d → %s", len(results), out)


def scrape_api_sync(cats, max_pages, proxy=None):
    import requests as req
    s = req.Session()
    s.headers.update({"User-Agent": UA[0], "Referer": "https://www.spinny.com/used-cars/s/", "Accept": "application/json"})
    if proxy: s.proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
    all_r = []
    for cat in cats:
        filt = API_FILTERS.get(cat, {})
        for pg in range(1, max_pages+1):
            params = {"page":pg,"city":filt.get("city",""),"o":"popular","include_booked":"false"}
            for k in ("body_type","fuel_type","transmission","make"):
                if k in filt: params[k] = filt[k]
            log.info("API %s page=%d", cat, pg)
            try:
                r = s.get(API_BASE, params=params, timeout=20)
                r.raise_for_status(); body = r.json()
            except Exception as e: log.error("  %s", e); break
            res, cnt, nxt = extract_results(body)
            if not res: break
            lst = parse_spinny_results(res, cat); all_r.extend(lst)
            log.info("  → %d (total %d/%d)", len(lst), len(all_r), cnt)
            if not nxt: break
            time.sleep(random.uniform(1.5, 3.0))
    return all_r


def scrape_browser_selenium(cats, args):
    opts = Options()
    if args.headless: opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox"); opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(f"--user-agent={random.choice(UA)}")
    opts.add_experimental_option("excludeSwitches",["enable-automation"])

    wire_opts = {}
    if args.proxy:
        srv = args.proxy.rsplit("@",1)[-1] if "@" in args.proxy else args.proxy
        if HAS_WIRE and "@" in args.proxy:
            wire_opts["proxy"] = {"http": f"http://{args.proxy}", "https": f"http://{args.proxy}"}
        else:
            opts.add_argument(f"--proxy-server=http://{srv}")

    if HAS_WIRE:
        drv = wire_webdriver.Chrome(options=opts, seleniumwire_options=wire_opts or {})
    elif ChromeDriverManager:
        drv = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    else:
        drv = webdriver.Chrome(options=opts)

    STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>false});window.chrome={runtime:{}};"
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    results = []

    try:
        for cat in cats:
            path = CATEGORIES.get(cat, f"/used-{cat}-cars/s/")
            url = f"{BASE_URL}{path}"
            for pg in range(1, args.pages+1):
                purl = url if pg == 1 else f"{url}?page={pg}"
                log.info("browser %s page=%d %s", cat, pg, purl)
                if HAS_WIRE: del drv.requests
                drv.get(purl); time.sleep(random.uniform(4,7))
                for _ in range(3):
                    drv.execute_script("window.scrollTo(0,document.body.scrollHeight)"); time.sleep(2)

                found = []
                if HAS_WIRE:
                    for req in drv.requests:
                        if req.response and req.response.status_code==200 and "listing" in req.url and "api.spinny.com" in req.url:
                            try:
                                b = json.loads(req.response.body.decode("utf-8"))
                                res,_,_ = extract_results(b)
                                found.extend(parse_spinny_results(res, cat))
                            except: pass
                if found:
                    results.extend(found); log.info("  → %d", len(found))
                else:
                    log.info("  No data — stopping %s", cat); break
                time.sleep(random.uniform(2,4))
    finally:
        drv.quit()
    return results


if __name__ == "__main__":
    main()
