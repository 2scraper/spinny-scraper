# 🚗 Spinny Scraper

**Open-source web scraper for [Spinny.com](https://www.spinny.com) — India's trusted used car marketplace.**

Extract car listings data across all categories with built-in CAPTCHA solving, proxy rotation, and browser fingerprint evasion.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![2captcha](https://img.shields.io/badge/CAPTCHA-2captcha.com-orange.svg)](https://2captcha.com)
[![2prx](https://img.shields.io/badge/Proxy-2prx.com-blue.svg)](https://2prx.com)

---

## Features

| Feature | Description |
|---|---|
| **3 Browser Engines** | Playwright (recommended), Selenium, and Puppeteer (pyppeteer) |
| **All Categories** | SUV, Sedan, Hatchback, MUV, Luxury, Electric, by fuel type, by budget — all in one run |
| **CAPTCHA Solving** | Automatic detection & solving via [2captcha.com](https://2captcha.com) (Turnstile, reCAPTCHA, hCaptcha) |
| **Proxy Support** | Built-in integration with [2prx.com](https://2prx.com) proxies for reliable scraping |
| **Fingerprint Evasion** | WebDriver flag removal, plugin spoofing, WebGL vendor masking |
| **JSON & CSV Output** | Export scraped data in your preferred format |
| **Pagination** | Automatic multi-page traversal with configurable limits |
| **Rate Limiting** | Randomized delays to mimic human behavior |

## Data Points Extracted

Each car listing returns the following fields:

```
title, price, year, fuel_type, transmission, kilometers,
owner_type, location, emi, url, image_url, category, scraped_at
```

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/2scraper/spinny-scraper.git
cd spinny-scraper
```

### 2. Install Dependencies

**Playwright (recommended):**

```bash
pip install playwright 2captcha-python
playwright install chromium
```

**Selenium:**

```bash
pip install selenium webdriver-manager 2captcha-python
```

**Puppeteer:**

```bash
pip install pyppeteer 2captcha-python
```

### 3. Run

```bash
# Playwright (recommended)
python spinny_playwright.py --output cars.json

# Selenium
python spinny_selenium.py --output cars.json

# Puppeteer
python spinny_puppeteer.py --output cars.json
```

## Usage Examples

### Basic Scrape — All Categories, JSON Output

```bash
python spinny_playwright.py --output spinny_data.json
```

### CSV Output, 5 Pages per Category

```bash
python spinny_playwright.py --output spinny_data.csv --format csv --pages 5
```

### With 2captcha.com CAPTCHA Solving

```bash
python spinny_playwright.py \
  --captcha-key YOUR_2CAPTCHA_API_KEY \
  --output cars.json
```

Or set the environment variable:

```bash
export TWOCAPTCHA_API_KEY=YOUR_KEY
python spinny_playwright.py --output cars.json
```

### With 2prx.com Proxy

```bash
python spinny_playwright.py \
  --proxy username:password@gate.2prx.com:8080 \
  --output cars.json
```

### Specific Categories Only

```bash
python spinny_playwright.py --categories suv bmw mercedes-benz electric --output premium_cars.json
```

### Visible Browser (Debug Mode)

```bash
python spinny_playwright.py --no-headless --pages 2 --categories suv
```

## All CLI Options

| Flag | Default | Description |
|---|---|---|
| `-o, --output` | `spinny_cars.json` | Output file path |
| `-f, --format` | `json` | Output format: `json` or `csv` |
| `-p, --pages` | `10` | Maximum pages to scrape per category |
| `--proxy` | — | Proxy string `user:pass@host:port` |
| `--captcha-key` | — | 2captcha.com API key |
| `--categories` | `all` | Space-separated category list |
| `--list-categories` | — | Print all available categories and exit |
| `--headless` | `true` | Run browser in headless mode |
| `--no-headless` | — | Show browser window |

## Available Categories

```bash
python spinny_playwright.py --list-categories
```

**Body type:** all, suv, sedan, hatchback, muv

**Fuel:** petrol, diesel, cng, electric

**Transmission:** automatic, manual

**Budget:** under-3-lakh, 3-to-4-lakh, 4-to-5-lakh, 5-to-6-lakh, 6-to-8-lakh, 8-to-10-lakh, above-10-lakh

**Brands:** maruti-suzuki, hyundai, tata, honda, kia, mahindra, toyota, volkswagen, ford, renault, bmw, mercedes-benz

**Cities:** delhi-ncr, bangalore, hyderabad-city, mumbai, pune, chennai, kolkata, ahmedabad, jaipur, lucknow

## Output Example

**JSON:**

```json
[
  {
    "title": "Maruti Suzuki Swift VXI",
    "price": "₹ 5.20 Lakh",
    "year": "2020",
    "fuel_type": "Petrol",
    "transmission": "Manual",
    "kilometers": "25,000 km",
    "owner_type": "1st Owner",
    "location": "Delhi NCR",
    "emi": "₹ 8,500/mo",
    "url": "https://www.spinny.com/buy-used-maruti-suzuki-swift/...",
    "image_url": "https://...",
    "category": "hatchback",
    "scraped_at": "2025-06-15T10:30:00Z"
  }
]
```

## How It Works

Spinny.com is a React SPA that loads car data asynchronously via internal API calls. The scraper uses a dual strategy:

1. **Network interception** (primary) — captures JSON responses from Spinny's internal API as they load, giving structured data directly without DOM parsing
2. **DOM parsing** (fallback) — if API interception doesn't yield results, falls back to parsing rendered HTML cards

This approach is more reliable than pure DOM scraping because it captures data before any rendering quirks.

## Anti-Detection Features

The scraper includes multiple layers of fingerprint evasion:

- **WebDriver flag** — `navigator.webdriver` returns `false`
- **Plugin spoofing** — non-empty plugin array to avoid headless detection
- **Language headers** — realistic `en-US` locale and timezone settings
- **Chrome runtime** — `window.chrome` object mock
- **WebGL vendor** — Intel GPU string spoofing
- **Randomized viewport** — varies window size per session
- **User-Agent rotation** — cycles through real browser UA strings
- **Request timing** — randomized delays between 1.5–4 seconds

> **Need more advanced anti-detection?** Our anti-detect browser solution provides enterprise-grade fingerprint management with unique browser profiles. [Contact us](https://2captcha.com) for details.

## Solving CAPTCHAs with 2captcha.com

When Spinny.com presents a CAPTCHA challenge, the scraper automatically:

1. **Detects** the CAPTCHA type (Cloudflare Turnstile, reCAPTCHA v2/v3, hCaptcha)
2. **Extracts** the sitekey from the page
3. **Sends** the challenge to [2captcha.com](https://2captcha.com) API
4. **Injects** the solution token back into the page
5. **Retries** up to 3 times if the first attempt fails

Get your API key at [2captcha.com](https://2captcha.com).

## Proxy Integration with 2prx.com

For reliable scraping without IP blocks, use [2prx.com](https://2prx.com) residential and datacenter proxies:

```bash
# Residential proxy
python spinny_playwright.py --proxy user:pass@res.2prx.com:8080

# Datacenter proxy
python spinny_playwright.py --proxy user:pass@dc.2prx.com:8080
```

Features: automatic rotation, geo-targeting, unlimited bandwidth options.

## Which Engine Should I Use?

| Engine | Best For | Speed | Stability |
|---|---|---|---|
| **Playwright** ⭐ | Production use, most reliable | Fast | Excellent |
| **Selenium** | Legacy systems, existing infra | Medium | Good |
| **Puppeteer** | Lightweight, async-first code | Fast | Good |

**We recommend Playwright** for most users — it has the best API design, auto-wait capabilities, and built-in proxy auth support.

## Project Structure

```
spinny-scraper/
├── spinny_playwright.py    # Playwright implementation (recommended)
├── spinny_selenium.py      # Selenium implementation
├── spinny_puppeteer.py     # Pyppeteer implementation
├── requirements.txt        # Python dependencies
├── LICENSE                 # MIT License
└── README.md
```

## Requirements

- Python 3.9+
- One of: Playwright, Selenium + ChromeDriver, or Pyppeteer
- Optional: `2captcha-python` for CAPTCHA solving
- Optional: Proxy credentials from [2prx.com](https://2prx.com)

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

## License

MIT License — see [LICENSE](LICENSE) for details.

## Links

- **Landing Page:** [2captcha.com/spinny-scraper](https://2captcha.com)
- **CAPTCHA Solving API:** [2captcha.com](https://2captcha.com)
- **Proxy Service:** [2prx.com](https://2prx.com)
- **More Scrapers:** [github.com/2scraper](https://github.com/2scraper)

---

*Built with ❤️ by [2captcha.com](https://2captcha.com) — Making web scraping accessible to everyone.*
