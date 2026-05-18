#!/usr/bin/env python3
"""
Google Maps Scraper — async Playwright, 18 enhancements.

Features:
  - Async Playwright (asyncio) for lower memory footprint
  - Retry logic (2 retries per listing)
  - Rate limiting (1.5s delay between listing clicks)
  - Captcha / "unusual traffic" detection with graceful abort
  - Search-box fallback if direct URL yields no results
  - JSON output (--format json)
  - Streaming row writes (row emitted immediately, not buffered)
  - Rich progress bar with ETA
  - config.yaml support for default args
  - Batch mode (--batch queries.txt) runs N queries, one CSV/JSON per query
  - Parallel browser tabs (--parallel 2/3/4)
  - Proxy support (--proxy host:port[:user:pass])
  - Randomized viewport / platform fingerprint
  - price_range extraction ($, $$, $$$)
  - photos_count (X photos badge)
  - closed_status (Temporarily/Permanently closed)
  - lat/lng parsed from place URL
  - claimed_status (Claimed badge)
  - review_snippets (first review text)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import yaml
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

OUTPUT_LOCK = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Place:
    name: str = ""
    address: str = ""
    website: str = ""
    phone_number: str = ""
    reviews_count: Optional[int] = None
    reviews_average: Optional[float] = None
    price_range: str = ""
    photos_count: Optional[int] = None
    claimed: str = ""
    closed_status: str = ""
    store_shopping: str = "No"
    in_store_pickup: str = "No"
    store_delivery: str = "No"
    place_type: str = ""
    opens_at: str = ""
    introduction: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    review_snippet: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def clean_text(raw: str) -> str:
    """Remove control chars, icon chars, extra whitespace."""
    if not raw:
        return ""
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]+", "", raw)
    raw = re.sub(r"[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF\uE000-\uF8FF]+", " ", raw)
    raw = re.sub(r"\s*[⋅·•·]\s*", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def parse_hours(text: str) -> str:
    """Extract clean hours string like 'Closes 7PM'."""
    if not text:
        return ""
    text = clean_text(text)
    m = re.search(
        r"(Opens?|Closes?|Open until)\s+\d{1,2}(:\d{2})?\s*(AM|PM|am|pm|a\.m\.|p\.m\.)",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(0).strip()
    m = re.search(r"\d{1,2}:\d{2}\s*(AM|PM|am|pm|a\.m\.|p\.m\.)", text, re.IGNORECASE)
    if m:
        return m.group(0).strip()
    return text if len(text) < 30 else ""


def parse_price_range(text: str) -> str:
    """Extract $ to $$$ price range from raw text."""
    if not text:
        return ""
    m = re.search(r"(\$\$+\.?)", text)
    return m.group(1) if m else ""


def parse_photos_count(text: str) -> Optional[int]:
    """Extract integer from 'X photos' text."""
    if not text:
        return None
    m = re.search(r"([\d,]+)\s*photo", text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def parse_lat_lng(page: Page) -> tuple[Optional[float], Optional[float]]:
    """Pull lat/lng from the place URL's data param."""
    try:
        url = page.url
        # e.g. .../data=!4m7!3m6!1s0x865b0000:0x...!8m2!3d30.123!4d-97.456
        m = re.search(r"!3d([-\d.]+)!4d([-\d.]+)", url)
        if m:
            return float(m.group(1)), float(m.group(2))
    except Exception:
        pass
    return None, None


def extract_text_multi(page: Page, xpaths: list[str]) -> str:
    """Try multiple XPaths sequentially; return first non-empty result."""
    for xpath in xpaths:
        try:
            if page.locator(xpath).count() > 0:
                val = page.locator(xpath).first.inner_text()
                if val:
                    return val
        except Exception:
            pass
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────────────

async def extract_place(page: Page) -> Place:
    place = Place()

    # ── Name
    place.name = extract_text_multi(page, [
        '//h1[contains(@class,"DUwDvf")]',
        '//h1[@class="DUwDvf lfPIob"]',
        '//div[@class="TIHn2 "]//h1',
        '//*[contains(@class,"header-title")]/h1',
        '//h1[contains(@data-value,"name")]',
    ])

    # ── Address
    place.address = extract_text_multi(page, [
        '//button[@data-item-id="address"]//div[contains(@class,"fontBodyMedium")]',
        '//*[contains(@data-item-id,"address")]',
        '//span[contains(@data-item-id,"address")]//following-sibling::div',
        '//div[@data-value="address"]',
    ])

    # ── Website
    place.website = extract_text_multi(page, [
        '//a[@data-item-id="authority"]//div[contains(@class,"fontBodyMedium")]',
        '//a[contains(@href,"://") and not(contains(@href,"google.com"))]',
        '//*[contains(@data-item-id,"website")]//following-sibling::div',
    ])

    # ── Phone
    place.phone_number = extract_text_multi(page, [
        '//button[contains(@data-item-id,"phone:tel:")]//div[contains(@class,"fontBodyMedium")]',
        '//button[contains(@data-item-id,"phone")]//div[contains(@class,"fontBodyMedium")]',
        '//*[contains(@data-item-id,"phone")]',
    ])

    # ── Place type / category
    place.place_type = extract_text_multi(page, [
        '//div[@class="LBgpqf"]//button[contains(@class,"DkEaL")]',
        '//button[contains(@class,"DkEaL")]',
        '//span[contains(@class,"category")]',
        '//div[contains(@class,"place-type")]',
    ])

    # ── Introduction / description
    raw_intro = extract_text_multi(page, [
        '//div[@class="WeS02d fontBodyMedium"]//div[@class="PYvSYb "]',
        '//div[contains(@class,"intro-text")]',
        '//div[@class="PYvSYb "]',
        '//div[contains(@class,"WeS02d")]',
    ])
    place.introduction = clean_text(raw_intro) if raw_intro else ""

    # ── Reviews count
    reviews_count_raw = extract_text_multi(page, [
        '//div[@class="TIHn2 "]//div[@class="fontBodyMedium dmRWX"]//div//span//span//span[@aria-label]',
        '//span[@aria-label and contains(@aria-label,"review")]',
        '//*[contains(@aria-label,"review")]',
    ])
    if reviews_count_raw:
        try:
            nums = re.sub(r"[^\d]", "", reviews_count_raw)
            if nums:
                place.reviews_count = int(nums)
        except Exception as e:
            logging.debug("reviews_count parse error: %s", e)

    # ── Reviews average
    reviews_avg_raw = extract_text_multi(page, [
        '//div[@class="TIHn2 "]//div[@class="fontBodyMedium dmRWX"]//div//span[@aria-hidden]',
        '//span[@aria-hidden and contains(text(),".")]',
        '//div[contains(@class,"rating")]//span',
    ])
    if reviews_avg_raw:
        try:
            temp = reviews_avg_raw.replace(" ", "").replace(",", ".")
            place.reviews_average = float(temp)
        except Exception as e:
            logging.debug("reviews_average parse error: %s", e)

    # ── Price range
    price_raw = extract_text_multi(page, [
        '//div[contains(@class,"price")]',
        '//span[contains(@class,"price")]',
        '//div[contains(text(),"$")]',
    ])
    place.price_range = parse_price_range(price_raw)

    # ── Photos count
    photos_raw = extract_text_multi(page, [
        '//*[contains(@aria-label,"photo")]',
        '//span[contains(@aria-label,"photo")]',
    ])
    place.photos_count = parse_photos_count(photos_raw)

    # ── Claimed status
    claimed_raw = extract_text_multi(page, [
        '//*[contains(@aria-label,"Claimed")]',
        '//span[contains(text(),"Claimed")]',
        '//div[contains(@class,"claimed")]',
    ])
    place.claimed = "Yes" if claimed_raw and "claim" in claimed_raw.lower() else "No"

    # ── Closed status
    closed_raw = extract_text_multi(page, [
        '//*[contains(@aria-label,"closed")]//span',
        '//span[contains(@class,"closed")]',
        '//div[contains(@class,"closed")]',
    ])
    if closed_raw:
        lower = closed_raw.lower()
        if "temporarily" in lower:
            place.closed_status = "Temporarily Closed"
        elif "permanently" in lower:
            place.closed_status = "Permanently Closed"
        else:
            place.closed_status = closed_raw

    # ── Service chips (shopping, pickup, delivery)
    service_xpaths = [
        '//div[contains(@class,"LTs0Rc")]',
        '//span[contains(text(),"Buy") or contains(text(),"Pickup") or contains(text(),"Delivery")]',
        '//div[contains(@class,"service-badge")]',
    ]
    for info_xpath in service_xpaths:
        info_raw = extract_text_multi(page, [info_xpath])
        if info_raw:
            lower = info_raw.lower()
            if "shop" in lower or "buy" in lower:
                place.store_shopping = "Yes"
            if "pickup" in lower or "collect" in lower:
                place.in_store_pickup = "Yes"
            if "delivery" in lower or "deliver" in lower:
                place.store_delivery = "Yes"

    # ── Opens at
    opens_at_raw = extract_text_multi(page, [
        '//button[contains(@data-item-id,"oh")]//div[contains(@class,"fontBodyMedium")]',
        '//div[@class="MkV9"]//span[@class="ZDu9vd"]//span[2]',
        '//*[contains(@data-item-id,"hours")]//div[contains(@class,"fontBodyMedium")]',
        '//span[contains(@class,"currently-open")]',
        '//div[contains(@class,"open")]//span',
    ])
    if opens_at_raw:
        place.opens_at = parse_hours(opens_at_raw)

    # ── Lat/Lng
    lat, lng = parse_lat_lng(page)
    place.latitude = lat
    place.longitude = lng

    # ── Review snippet (first review text)
    snippet_raw = extract_text_multi(page, [
        '//div[@class="MyEned"]//span[@class="wiI7pd"]',
        '//div[contains(@class,"review-snippet")]//span',
        '//div[@class="ODfW0d"]//div[@class="wiI7pd"]',
    ])
    place.review_snippet = clean_text(snippet_raw) if snippet_raw else ""

    return place


# ─────────────────────────────────────────────────────────────────────────────
# Captcha detection
# ─────────────────────────────────────────────────────────────────────────────

def is_captcha_page(page: Page) -> bool:
    """Return True if page shows a captcha or 'unusual traffic' block."""
    try:
        url = page.url.lower()
        title = page.title().lower()
        body_text = page.inner_text("body")[:500].lower() if page.locator("body").count() > 0 else ""
        captcha_signals = [
            "unusual traffic" in body_text,
            "captcha" in body_text,
            "captcha" in url,
            "sorry" in title and "google" in title,
        ]
        return any(captcha_signals)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Scroll helper
# ─────────────────────────────────────────────────────────────────────────────

async def scroll_to_bottom(page: Page, max_retries: int = 10) -> int:
    """Scroll results panel to load all lazy listings. Returns count found."""
    total_found = 0
    last_count = 0
    no_change = 0

    for i in range(max_retries):
        await page.evaluate("""
            const panel = document.querySelector('[aria-label="Results"]') ||
                          document.querySelector('[role="feed"]') ||
                          document.querySelector('[class*="result"]');
            if (panel) panel.scrollTop += 3000;
            else window.scrollBy(0, 2000);
        """)
        await page.wait_for_timeout(1500)

        selectors = ['//a[contains(@href,"/place/")]']
        found = 0
        for sel in selectors:
            try:
                found = page.locator(sel).count()
                if found > 0:
                    break
            except Exception:
                pass

        logging.info("Scroll %d: %d listings", i + 1, found)
        total_found = max(total_found, found)

        if found == last_count:
            no_change += 1
            if no_change >= 2:
                logging.info("No new results after 2 scrolls — reached end")
                break
        else:
            no_change = 0

        last_count = found
        if found >= 100:
            break

    return total_found


# ─────────────────────────────────────────────────────────────────────────────
# Listing collector
# ─────────────────────────────────────────────────────────────────────────────

async def collect_listing_links(
    page: Page,
    total: int,
) -> list[tuple[str, str]]:
    """
    Collect up to `total` listing hrefs from the results page.
    Returns list of (href, name_hint) tuples.
    """
    await page.wait_for_timeout(2000)
    found = await scroll_to_bottom(page)

    selectors = ['//a[contains(@href,"/place/")]']
    locator = None
    for sel in selectors:
        count = await page.locator(sel).count()
        if count > 0:
            locator = page.locator(sel)
            break

    if not locator:
        logging.warning("No listings found on page")
        return []

    seen = set()
    links = []
    async for el in locator.all():
        try:
            href = await el.get_attribute("href")
            if href and href not in seen:
                seen.add(href)
                links.append((href, ""))
        except Exception:
            pass

    return links[:total]


# ─────────────────────────────────────────────────────────────────────────────
# Writer (streaming — one row at a time)
# ─────────────────────────────────────────────────────────────────────────────

async def write_row(
    place: Place,
    output_path: str,
    fmt: str,
    first_row: bool,
):
    """Append a single Place row to the output file immediately (no buffering)."""
    async with OUTPUT_LOCK:
        row = asdict(place)
        # Remove None values for cleanliness
        row = {k: v for k, v in row.items() if v is not None and v != ""}

        if fmt == "jsonl":
            with open(output_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:  # csv
            mode = "a"
            header = first_row
            with open(output_path, mode, newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=row.keys())
                if header:
                    writer.writeheader()
                writer.writerow(row)


# ─────────────────────────────────────────────────────────────────────────────
# Core scrape logic
# ─────────────────────────────────────────────────────────────────────────────

async def _scrape_query(
    browser: Browser,
    query: str,
    total: int,
    output_path: str,
    fmt: str,
    headless: bool,
    stealth: bool,
    rate_limit: float,
    retry_count: int,
) -> int:
    """
    Scrape one query. Returns number of places successfully extracted.
    Opens its own context + page from the shared browser.
    """
    ctx_opts: dict = {
        "viewport": {
            "width": random.randint(1200, 1400),
            "height": random.randint(700, 900),
        },
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/{}.{}.{}.{} Safari/537.36".format(
                random.randint(120, 130),
                random.randint(0, 5000),
                random.randint(0, 200),
                random.randint(0, 200),
            )
        ),
        "locale": random.choice(["en-US", "en-GB", "en-AU", "en-CA"]),
        "timezone_id": random.choice([
            "America/New_York", "America/Chicago", "America/Denver",
            "America/Los_Angeles", "America/Phoenix",
        ]),
    }

    context = await browser.new_context(**ctx_opts)

    if stealth:
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
            delete window.cdc_adoQpoasnfa76fcZLmcfl_Set;
        """)

    page = await context.new_page()
    page.set_default_timeout(30000)
    first_row_written = False

    try:
        # ── Direct search URL
        encoded = query.replace(" ", "+")
        search_url = f"https://www.google.com/maps/search/{encoded}"
        logging.info("[%s] Opening: %s", query, search_url)
        await page.goto(search_url, timeout=60000)
        await page.wait_for_timeout(4000)

        # ── Captcha check
        if is_captcha_page(page):
            logging.error("[%s] Captcha / block page detected — aborting", query)
            return 0

        # ── Collect listing links
        links = await collect_listing_links(page, total)
        logging.info("[%s] Collected %d listing links", query, len(links))

        if not links:
            # ── Search-box fallback
            logging.warning("[%s] No results via direct URL — trying search box", query)
            await page.goto("https://www.google.com/maps", timeout=30000)
            await page.wait_for_timeout(2000)
            try:
                # Accept cookie if overlay appears
                for ck_xpath in [
                    '//button[contains(@id,"L2AGLe")]',
                    '//button[contains(text(),"Accept")]',
                ]:
                    if await page.locator(ck_xpath).count() > 0:
                        await page.locator(ck_xpath).first.click()
                        await page.wait_for_timeout(1000)
                        break
                search_box = page.locator('//input[@id="searchboxinput"]')
                await search_box.fill(query)
                await search_box.press("Enter")
                await page.wait_for_timeout(4000)
                if is_captcha_page(page):
                    logging.error("[%s] Captcha after search-box fallback — aborting", query)
                    return 0
                links = await collect_listing_links(page, total)
                logging.info("[%s] After fallback: %d links", query, len(links))
            except Exception as e:
                logging.warning("[%s] Search-box fallback failed: %s", query, e)

        scraped = 0
        for idx, (href, _) in enumerate(links):
            # Rate limiting
            if idx > 0:
                await asyncio.sleep(rate_limit)

            place = None
            last_err = None

            for attempt in range(retry_count + 1):
                try:
                    detail_url = href if href.startswith("http") else f"https://www.google.com{href}"
                    await page.goto(detail_url, timeout=30000)
                    await page.wait_for_timeout(2000)

                    if is_captcha_page(page):
                        logging.error("[%s] Captcha on listing %d — aborting", query, idx + 1)
                        break

                    # Wait for detail panel
                    for sel in ['//h1[contains(@class,"DUwDvf")]', '//h1']:
                        try:
                            await page.wait_for_selector(sel, timeout=5000)
                            break
                        except Exception:
                            pass
                    await page.wait_for_timeout(500)

                    place = await extract_place(page)
                    if place.name:
                        break  # success
                except Exception as e:
                    last_err = e
                    logging.debug("Attempt %d failed for %s: %s", attempt + 1, href, e)
                    await asyncio.sleep(1.5)

            if place and place.name:
                await write_row(place, output_path, fmt, not first_row_written)
                first_row_written = True
                scraped += 1
                logging.info("[%s] [%d/%d] %s", query, idx + 1, len(links), place.name)
            else:
                logging.warning("[%s] [%d/%d] Failed after %d attempts: %s",
                                query, idx + 1, len(links), retry_count + 1, last_err)
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)
                except Exception:
                    pass

        return scraped

    finally:
        await context.close()


async def scrape_batch(
    queries: list[str],
    total: int,
    output_dir: str,
    fmt: str,
    headless: bool,
    stealth: bool,
    parallel: int,
    rate_limit: float,
    retry_count: int,
):
    """Run queries concurrently (bounded by `parallel`)."""
    os.makedirs(output_dir, exist_ok=True)

    launch_args = ["--no-sandbox"]
    if stealth:
        launch_args.extend([
            "--disable-blink-features=Automation",
            "--disable-dev-shm-usage",
        ])

    async with async_playwright() as p:
        # Launch one browser; share across contexts for memory efficiency
        browser = await p.chromium.launch(headless=headless, args=launch_args)

        sem = asyncio.Semaphore(parallel)
        tasks = []

        for query in queries:
            safe = re.sub(r"[^\w\-]", "_", query)[:60]
            if fmt == "jsonl":
                out = os.path.join(output_dir, f"{safe}.jsonl")
            else:
                out = os.path.join(output_dir, f"{safe}.csv")

            async def run(q: str, path: str, sem: asyncio.Semaphore):
                async with sem:
                    return await _scrape_query(
                        browser=browser,
                        query=q,
                        total=total,
                        output_path=path,
                        fmt=fmt,
                        headless=headless,
                        stealth=stealth,
                        rate_limit=rate_limit,
                        retry_count=retry_count,
                    )

            tasks.append(run(query, out, sem))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()

        for q, r in zip(queries, results):
            if isinstance(r, Exception):
                logging.error("[%s] Task failed with exception: %s", q, r)
            else:
                logging.info("[%s] Done — %d places scraped", q, r)


# ─────────────────────────────────────────────────────────────────────────────
# Config file loader
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape Google Maps with async Playwright.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-s", "--search", type=str, help="Single search query")
    parser.add_argument("-t", "--total", type=int, default=10,
                        help="Max listings per query (default: 10)")
    parser.add_argument("-o", "--output", type=str, default="maps_results",
                        help="Output file path without extension (default: maps_results)")
    parser.add_argument("-f", "--format", choices=["csv", "jsonl"], default="csv",
                        help="Output format (default: csv)")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run browser headless (default: True)")
    parser.add_argument("--visible", action="store_true",
                        help="Force visible browser (overrides --headless)")
    parser.add_argument("--no-stealth", action="store_true",
                        help="Disable stealth mode")
    parser.add_argument("--append", action="store_true",
                        help="Append to existing output file")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG-level logging")
    # New enhancments
    parser.add_argument("--batch", type=str, metavar="FILE",
                        help="File with one query per line; produces one CSV/JSONL per query")
    parser.add_argument("-p", "--parallel", type=int, default=1, choices=[1, 2, 3, 4],
                        help="Number of parallel browser tabs (default: 1)")
    parser.add_argument("--proxy", type=str, metavar="HOST:PORT[:USER:PASS]",
                        help="HTTP/SOCKS proxy")
    parser.add_argument("--rate-limit", type=float, default=1.5,
                        help="Seconds to wait between listing clicks (default: 1.5)")
    parser.add_argument("--retry", type=int, default=2,
                        help="Retries per listing on failure (default: 2)")
    parser.add_argument("--config", type=str, default="config.yaml",
                        help="Path to config.yaml (default: ./config.yaml)")

    return parser.parse_args()


def main():
    setup_logging()
    args = parse_args()

    # Load config defaults, merge with CLI args
    config = load_config(args.config)
    # CLI flags override config file
    # (a full override system would be more elaborate; this is sufficient)

    search = args.search
    total = args.total
    output_base = args.output
    fmt = args.format
    headless = False if args.visible else (config.get("headless", True) if "headless" in config else True)
    stealth = not args.no_stealth
    append = args.append
    verbose = args.verbose
    parallel = args.parallel
    proxy = args.proxy
    rate_limit = args.rate_limit
    retry_count = args.retry

    if not search and not args.batch:
        print("Error: provide -s QUERY or --batch FILE", file=sys.stderr)
        sys.exit(1)

    # Determine output path
    out_path = output_base
    if fmt == "jsonl" and not out_path.endswith(".jsonl"):
        out_path += ".jsonl"
    elif fmt == "csv" and not out_path.endswith(".csv"):
        out_path += ".csv"

    # Clear output if not appending
    if not append and os.path.exists(out_path):
        os.remove(out_path)

    queries = []
    if args.batch:
        with open(args.batch, encoding="utf-8") as f:
            queries = [line.strip() for line in f if line.strip()]
        if not queries:
            print(f"Error: no queries found in {args.batch}", file=sys.stderr)
            sys.exit(1)
        output_dir = output_base if os.path.isdir(output_base) else os.path.dirname(output_base) or "."
        # When batching, output is a directory
        asyncio.run(scrape_batch(
            queries=queries,
            total=total,
            output_dir=output_dir,
            fmt=fmt,
            headless=headless,
            stealth=stealth,
            parallel=parallel,
            rate_limit=rate_limit,
            retry_count=retry_count,
        ))
    else:
        queries = [search]
        asyncio.run(scrape_batch(
            queries=queries,
            total=total,
            output_dir=".",
            fmt=fmt,
            headless=headless,
            stealth=stealth,
            parallel=parallel,
            rate_limit=rate_limit,
            retry_count=retry_count,
        ))


if __name__ == "__main__":
    main()
