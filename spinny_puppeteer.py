"""
Spinny.com Web Scraper — Puppeteer (Pyppeteer) Implementation (v3)
====================================================================
Dual mode: direct API (fast, no browser) + browser fallback.

Repository : https://github.com/2scraper/spinny-scraper
License    : MIT

Usage:  python spinny_puppeteer.py --output cars.json
        python spinny_puppeteer.py --mode api --pages 5
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
    sys.exit("pip install pyppeteer")

try:
    from twocaptcha import TwoCaptcha
except ImportError:
    TwoCaptcha = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("spinny-ppt")

# Shared config, model, parser
exec(open(os.path.join(os.path.dirname(__file__) or ".", "_shared.py")).read())

STEALTH = """() => {
    Object.defineProperty(navigator,'webdriver',{get:()=>false});
    Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
    window.chrome={runtime:{}};
}"""

async def scrape_api_direct(cats, max_pages, proxy=None):
    import httpx
    headers = {"User-Agent":UA[0],"Referer":"https://www.spinny.com/used-cars/s/","Accept":"application/json"}
    proxy_url = f"http://{proxy}" if proxy else None
    all_r = []
    async with httpx.AsyncClient(headers=headers, proxy=proxy_url, timeout=30, follow_redirects=True) as cl:
        for cat in cats:
            filt = API_FILTERS.get(cat, {})
            for pg in range(1, max_pages+1):
                params = {"page":pg,"city":filt.get("city",""),"o":"popular","include_booked":"false"}
                for k in ("body_type","fuel_type","transmission","make"):
                    if k in filt: params[k] = filt[k]
                log.info("API %s page=%d", cat, pg)
                try:
                    r = await cl.get(API_BASE, params=params); r.raise_for_status(); body = r.json()
                except Exception as e: log.error("  %s", e); break
                res,cnt,nxt = extract_results(body)
                if not res: break
                lst = parse_spinny_results(res, cat); all_r.extend(lst)
                log.info("  → %d (total %d/%d)", len(lst), len(all_r), cnt)
                if not nxt: break
                await asyncio.sleep(random.uniform(1.5, 3.0))
    return all_r


class SpinnyScraper:
    def __init__(self, proxy=None, captcha_key=None, headless=True, max_pages=10):
        self.proxy=proxy; self.headless=headless; self.max_pages=max_pages
        self.results=[]; self._buf=[]
        self.captcha_key = captcha_key or os.getenv("TWOCAPTCHA_API_KEY","")

    async def scrape_all(self, cats):
        args = ["--no-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"]
        if self.proxy:
            srv = self.proxy.rsplit("@",1)[-1] if "@" in self.proxy else self.proxy
            args.append(f"--proxy-server=http://{srv}")
        browser = await launch(headless=self.headless, args=args, ignoreHTTPSErrors=True)
        try:
            page = await browser.newPage()
            await page.setUserAgent(random.choice(UA))
            await page.setViewport({"width":1440,"height":900})
            await page.evaluateOnNewDocument(STEALTH)
            if self.proxy and "@" in self.proxy:
                auth=self.proxy.rsplit("@",1)[0]
                if ":" in auth: u,p=auth.split(":",1); await page.authenticate({"username":u,"password":p})

            # CDP interception
            cdp = await page.target.createCDPSession()
            await cdp.send("Network.enable")
            async def on_resp(ev):
                url=ev.get("response",{}).get("url","")
                if ev.get("response",{}).get("status")==200 and "api.spinny.com" in url and "listing" in url:
                    try:
                        br=await cdp.send("Network.getResponseBody",{"requestId":ev["requestId"]})
                        b=json.loads(br.get("body",""))
                        if "results" in b:
                            log.info("  ✓ CDP: %s (%d)", url[:100], len(b["results"]))
                            self._buf.append(b)
                    except: pass
            cdp.on("Network.responseReceived", lambda e: asyncio.ensure_future(on_resp(e)))

            for cat in cats:
                path = CATEGORIES.get(cat, f"/used-{cat}-cars/s/")
                url = f"{BASE_URL}{path}"
                for pg in range(1, self.max_pages+1):
                    purl = url if pg==1 else f"{url}?page={pg}"
                    log.info("browser %s page=%d %s", cat, pg, purl)
                    self._buf.clear()
                    await page.goto(purl, {"waitUntil":"domcontentloaded","timeout":30000})
                    await asyncio.sleep(random.uniform(4,7))
                    for _ in range(3):
                        await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                        await asyncio.sleep(2)
                    if not self._buf: await asyncio.sleep(5)
                    found=[]
                    for b in self._buf:
                        res,_,_ = extract_results(b); found.extend(parse_spinny_results(res, cat))
                    if found: self.results.extend(found); log.info("  → %d", len(found))
                    else: log.info("  No data"); break
                    await asyncio.sleep(random.uniform(2,4))
        finally: await browser.close()
        seen=set(); uniq=[]
        for r in self.results:
            k=r.spinny_id or r.url
            if k and k not in seen: seen.add(k); uniq.append(r)
        self.results=uniq; return self.results

async def main():
    p = argparse.ArgumentParser(description="Spinny.com scraper (Pyppeteer)")
    p.add_argument("-o","--output",default="spinny_cars.json")
    p.add_argument("-f","--format",choices=["json","csv"],default="json")
    p.add_argument("-p","--pages",type=int,default=10)
    p.add_argument("--mode",choices=["api","browser","auto"],default="auto")
    p.add_argument("--proxy",default=None); p.add_argument("--captcha-key",default=None)
    p.add_argument("--categories",nargs="*",default=None)
    p.add_argument("--list-categories",action="store_true")
    p.add_argument("--headless",action="store_true",default=True)
    p.add_argument("--no-headless",dest="headless",action="store_false")
    args = p.parse_args()
    if args.list_categories:
        for n,pa in sorted(CATEGORIES.items()): print(f"  {n:20s} → {pa}")
        return
    cats = args.categories or DEFAULT_CATEGORIES; results=[]
    if args.mode in ("api","auto"):
        try:
            import httpx; results = await scrape_api_direct(cats, args.pages, args.proxy)
        except ImportError:
            if args.mode=="api": sys.exit("pip install httpx")
            args.mode="browser"
        except Exception as e:
            log.warning("API: %s",e)
            if args.mode=="auto": args.mode="browser"
    if args.mode=="browser" or (args.mode=="auto" and not results):
        s=SpinnyScraper(proxy=args.proxy,captcha_key=args.captcha_key,headless=args.headless,max_pages=args.pages)
        results=await s.scrape_all(cats)
    seen=set();uniq=[]
    for r in results:
        k=r.spinny_id or r.url; 
        if k and k not in seen: seen.add(k);uniq.append(r)
    results=uniq; log.info("Final: %d",len(results))
    ext="csv" if args.format=="csv" else "json"
    out=args.output if args.output.endswith(f".{ext}") else args.output.rsplit(".",1)[0]+f".{ext}"
    if ext=="csv":
        if results:
            with open(out,"w",newline="",encoding="utf-8") as f:
                w=csv.DictWriter(f,fieldnames=list(asdict(results[0]).keys()));w.writeheader()
                for r in results: w.writerow(asdict(r))
    else:
        with open(out,"w",encoding="utf-8") as f: json.dump([asdict(d) for d in results],f,indent=2,ensure_ascii=False)
    log.info("Saved %d → %s",len(results),out)

if __name__=="__main__": asyncio.run(main())
