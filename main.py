#!/usr/bin/env python3
"""
Google Maps Scraper — async Playwright, full feature set.

Key features:
  - Async Playwright (asyncio)
  - Retry logic (2 retries per listing)
  - Rate limiting (1.5s between listing clicks)
  - Captcha / unusual-traffic detection
  - Search-box fallback
  - JSONL output
  - Streaming row writes (immediate, no buffering)
  - config.yaml support
  - Batch mode (--batch FILE)
  - Parallel tabs (--parallel 2/3/4)
  - Proxy support (--proxy HOST:PORT[:USER:PASS])
  - Randomized fingerprint per context
  - Field selection (--fields)
  - Deduplication (skip already-scraped places)
  - --dry-run (verify without writing)
  - --quiet (errors only)
  - Full reviews extraction (author, rating, date, text)
  - Service fields: wheelchair, restroom, parking, payment
  - place_id from URL
  - Per-query metadata (query, timestamp)
  - Crash recovery via --snapshot-dir
  - In-memory / stdout mode (--no-save)
  - Proxy rotation (--proxies FILE)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import datetime
import json
import logging
import os
import random
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Optional

import yaml
from playwright.async_api import async_playwright, Browser, Page

OUTPUT_LOCK = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Review:
    author: str = ""
    rating: Optional[float] = None   # 1.0–5.0 stars
    date: str = ""
    text: str = ""


@dataclass
class Place:
    # Identity / metadata
    place_id: str = ""          # Google CID parsed from URL
    query: str = ""             # Search query that found this place
    scraped_at: str = ""        # ISO timestamp of scrape

    # Basic info
    name: str = ""
    address: str = ""
    website: str = ""
    phone_number: str = ""

    # Reviews summary
    reviews_count: Optional[int] = None
    reviews_average: Optional[float] = None

    # Pricing / media
    price_range: str = ""       # $, $$, $$$  (empty = not known)
    photos_count: Optional[int] = None

    # Status
    claimed: str = "No"         # "Yes" or "No"
    closed_status: str = ""      # "Temporarily Closed" / "Permanently Closed"

    # Services  (Yes / No / empty)
    store_shopping: str = "No"
    in_store_pickup: str = "No"
    store_delivery: str = "No"
    wheelchair_accessible: str = "No"
    restroom: str = "No"
    parking: str = "No"
    payment_cards: str = "No"

    # Location / hours / description
    place_type: str = ""
    opens_at: str = ""
    introduction: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # Full reviews list — base64(JSON) in CSV, JSON in JSONL
    reviews: list = field(default_factory=list)

    # Backwards-compatible snippet (first review text only)
    review_snippet: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False, quiet: bool = False):
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    fmt = "%(asctime)s.%(msecs)03d,%(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


def clean_text(raw: str) -> str:
    """Strip control chars, Unicode icon noise, and collapse whitespace."""
    if not raw:
        return ""
    # C0/C1 control chars
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]+", "", raw)
    # Unicode private-use and decorative blocks
    raw = re.sub(r"[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF\uE000-\uF8FF]+", " ", raw)
    # Bullet dots used as separators in GMaps DOM
    raw = re.sub(r"\s*[⋅·•·]\s*", " ", raw)
    # Collapse whitespace
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def parse_price_range(text: str) -> str:
    """Return '$', '$$', '$$$', or ''."""
    if not text:
        return ""
    m = re.search(r"(\${1,4})", text)
    if m:
        return m.group(1)[:3]
    return ""


def parse_hours(text: str) -> str:
    """Pull a clean open/close time like 'Opens 9 AM' or 'Closes 10 PM'."""
    if not text:
        return ""
    text = clean_text(text)
    m = re.search(
        r"(Open[sd]?|Close[sd]?|Open until)\s+"
        r"\d{1,2}(:\d{2})?\s*(AM|PM|am|pm|a\.m\.|p\.m\.)",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(0).strip()
    # Fallback: time pattern alone
    m = re.search(r"\d{1,2}:\d{2}\s*(AM|PM|am|pm)", text, re.IGNORECASE)
    if m:
        return m.group(0).strip()
    return text if len(text) < 40 else ""


def parse_place_id(url: str) -> str:
    """Extract Google CID from a maps place URL.

    Google encodes places in URLs two ways:
      /place/NAME/!8m2!3dLAT!4dLNG          (short form)
      /place/!6sHASH!8m2!3dLAT!4dLNG        (hash form)
    The data= param also contains the CID.
    """
    for pattern in [
        r"!1s([^!]+)",      # data=!4m7!3m6!1sHASH
        r"/place/([^/?]+)",  # /place/HASH or /place/NAME
    ]:
        m = re.search(pattern, url)
        if m:
            val = m.group(1)
            if val and "!" in val or "/" in val:
                return val
            if val:
                return val
    return ""


def parse_lat_lng(url: str) -> tuple[Optional[float], Optional[float]]:
    """Extract lat/lng from a place URL.

    Google uses two formats:
      ...!3dLAT!4dLNG         (data param format, e.g. !3d30.2510458!4d-97.7493717)
      /place/.../@LAT,LNG,Z   (short URL format, e.g. @30.2510458,-97.7493717,17z)
    """
    try:
        # Format 1: !3dLAT!4dLNG
        m = re.search(r"!3d([-\d.]+)!4d([-\d.]+)", url)
        if m:
            v1, v2 = float(m.group(1)), float(m.group(2))
            # swap: lat is small positive in US, lng is large negative
            if v1 < -30 and -90 < v2 < 90:
                return v2, v1
            return v1, v2

        # Format 2: @LAT,LNG (e.g. @30.2510458,-97.7493717,17z)
        m = re.search(r"@([-\d.]+),([-\d.]+)", url)
        if m:
            v1, v2 = float(m.group(1)), float(m.group(2))
            # swap: the numeric pattern can't distinguish which is lat/lng by magnitude
            # US: lat 20-72 (positive), lng -60 to -130 (negative)
            if v1 < -30 and -90 < v2 < 90:
                return v2, v1
            if -90 < v1 < 90 and (v2 <= -60 or v2 >= 60):
                return v1, v2
            # Default: lat first
            return v1, v2

        return None, None
    except Exception:
        return None, None


def safe_filename(query: str) -> str:
    """Sanitize a query string into a safe filename component."""
    return re.sub(r"[^\w\-]", "_", query)[:60]


# ─────────────────────────────────────────────────────────────────────────────
# Extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _text(page: Page, xpath: str, limit: int = 0) -> str:
    """Return inner_text of first matching XPath, or ''."""
    try:
        if await page.locator(xpath).count() == 0:
            return ""
        val = await page.locator(xpath).first.inner_text()
        val = clean_text(val)
        if limit and len(val) > limit:
            val = val[:limit]
        return val
    except Exception:
        return ""


async def _attr(page: Page, xpath: str, attr: str) -> str:
    """Return an attribute value from the first matching XPath, or ''."""
    try:
        if await page.locator(xpath).count() == 0:
            return ""
        return (await page.locator(xpath).first.get_attribute(attr) or "").strip()
    except Exception:
        return ""


async def _count(page: Page, xpath: str) -> int:
    """Return count of matching elements."""
    try:
        return await page.locator(xpath).count()
    except Exception:
        return 0


async def _try_texts(page: Page, xpaths: list[str], limit: int = 0) -> str:
    """Try each XPath in order; return first non-empty result."""
    for xp in xpaths:
        val = await _text(page, xp, limit=limit)
        if val:
            return val
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Reviews extraction
# ─────────────────────────────────────────────────────────────────────────────

async def extract_reviews(page: Page, max_reviews: int = 20) -> list[Review]:
    """Extract up to max_reviews from the detail page.

    Each review is inside a container with class jxjCjc. The actual text is in
    a span.wiI7pd (or similar). We find each block, then query the text span
    within it directly.
    """
    reviews: list[Review] = []

    try:
        # Expand "More reviews" if present
        for btn_xp in [
            '//button[contains(text(),"More reviews")]',
            '//span[contains(text(),"More reviews")]',
        ]:
            if await _count(page, btn_xp) > 0:
                await page.locator(btn_xp).first.click()
                await page.wait_for_timeout(2000)
                break

        # Scroll reviews section into view
        await page.evaluate("""
            var s = document.querySelector('[aria-label*="Reviews"]') ||
                    document.querySelector('[class*="review"]');
            if (s) s.scrollIntoView();
        """)
        await page.wait_for_timeout(1000)

        # Find individual review blocks
        block_xpaths = [
            '//div[@class="jxjCjc"]',
            '//div[@data-review-id]',
            '//div[contains(@class,"review-main-section")]',
        ]
        block_sel = None
        for xp in block_xpaths:
            if await _count(page, xp) > 0:
                block_sel = xp
                break

        if not block_sel:
            return reviews

        n_blocks = await _count(page, block_sel)
        for i in range(min(n_blocks, max_reviews)):
            try:
                block = page.locator(block_sel).nth(i)
                rev = Review()

                # Author: look for the subtitle span that contains the name
                for author_xp in [
                    './/div[@class="section-review-subtitle"]//span[@class="fontBodyMedium"]',
                    './/*[contains(@class,"author")]',
                    './/span[contains(@aria-label," ")]',   # names often in aria-label
                ]:
                    try:
                        if await block.locator(author_xp).count() > 0:
                            author_text = await block.locator(author_xp).first.inner_text()
                            author_text = clean_text(author_text)
                            if author_text and len(author_text) < 100:
                                rev.author = author_text
                                break
                    except Exception:
                        pass

                # Rating: look for the star rating aria-label
                for rating_xp in [
                    './/meta[@itemprop="ratingValue"]',
                    './/span[@aria-label and contains(@aria-label,"star")]',
                    './/span[contains(@aria-label,".")]',   # "4.5 stars"
                ]:
                    try:
                        if await block.locator(rating_xp).count() > 0:
                            raw = await block.locator(rating_xp).first.get_attribute("aria-label") or ""
                            m = re.search(r"([\d.]+)", raw)
                            if m:
                                rev.rating = float(m.group(1))
                                break
                    except Exception:
                        pass

                # Date: look for relative date text
                for date_xp in [
                    './/span[@class="fontBodyMedium"]//span[last()]',
                    './/*[contains(@class,"date")]',
                    './/span[contains(@aria-label," ")]',
                ]:
                    try:
                        if await block.locator(date_xp).count() > 0:
                            date_text = await block.locator(date_xp).last.inner_text()
                            date_text = clean_text(date_text)
                            if date_text and 2 < len(date_text) < 60:
                                rev.date = date_text
                                break
                    except Exception:
                        pass

                # Review text: wiI7pd is the specific class for review body text
                for text_xp in ['.//span[@class="wiI7pd"]', './/*[contains(@class,"review-text")]']:
                    try:
                        if await block.locator(text_xp).count() > 0:
                            rev.text = clean_text(await block.locator(text_xp).first.inner_text())
                            break
                    except Exception:
                        pass

                if rev.text or rev.author:
                    reviews.append(rev)

            except Exception as e:
                logging.debug("Review block %d error: %s", i, e)

    except Exception as e:
        logging.debug("Reviews extraction failed: %s", e)

    return reviews


# ─────────────────────────────────────────────────────────────────────────────
# Place extraction
# ─────────────────────────────────────────────────────────────────────────────

async def extract_place(page: Page, query: str) -> Place:
    place = Place()
    place.query = query
    place.scraped_at = datetime.datetime.utcnow().isoformat() + "Z"
    place.place_id = parse_place_id(page.url)

    # ── Name ─────────────────────────────────────────────────────────────────
    place.name = await _try_texts(page, [
        '//h1[contains(@class,"DUwDvf")]',
        '//h1[@class="DUwDvf lfPIob"]',
        '//div[@class="TIHn2 "]/h1',
        '//*[contains(@class,"header-title")]/h1',
    ])

    # ── Address ───────────────────────────────────────────────────────────────
    # Must use @data-item-id="address" specifically, NOT just containing "address"
    place.address = await _try_texts(page, [
        '//button[@data-item-id="address"]//div[contains(@class,"fontBodyMedium")]',
        '//*[@data-item-id="address"]//div[contains(@class,"fontBodyMedium")]',
    ])

    # ── Website ───────────────────────────────────────────────────────────────
    place.website = await _try_texts(page, [
        '//a[@data-item-id="authority"]//div[contains(@class,"fontBodyMedium")]',
        '//a[contains(@href,"://") and not(contains(@href,"google.com"))]',
    ])

    # ── Phone ─────────────────────────────────────────────────────────────────
    # data-item-id may be "phone:tel:" or just "phone:" — use starts-with
    place.phone_number = await _try_texts(page, [
        '//button[starts-with(@data-item-id,"phone")]//div[contains(@class,"fontBodyMedium")]',
        '//a[starts-with(@data-item-id,"phone")]//div[contains(@class,"fontBodyMedium")]',
    ])

    # ── Place type / category ─────────────────────────────────────────────────
    place.place_type = await _try_texts(page, [
        '//div[@class="LBgpqf"]//button[contains(@class,"DkEaL")]',
        '//button[contains(@class,"DkEaL")]',
    ])

    # ── Introduction / description ─────────────────────────────────────────────
    # The section tab has aria-label="About NAME" (not "About this business")
    # The description text is in div.WeS02d > div > div.PYvSYb within that section
    place.introduction = await _try_texts(page, [
        '//div[starts-with(@aria-label,"About ")]//div[@class="WeS02d fontBodyMedium"]//div[@class="PYvSYb "]',
        '//div[starts-with(@aria-label,"About ")]//div[@class="PYvSYb "]',
        '//div[contains(@aria-label,"About ")]',
    ], limit=500)

    # ── Reviews count ──────────────────────────────────────────────────────────
    # The reviews count is in an aria-label like "4.5 stars, 1,952 reviews"
    rev_text = await _try_texts(page, [
        '//div[@class="TIHn2 "]/div[@class="fontBodyMedium dmRWX"]//span[@aria-label]',
        '//*[contains(@aria-label,"review") and contains(@aria-label,",")]',
    ])
    if rev_text:
        nums = re.sub(r"[^\d]", "", rev_text)
        if nums:
            try:
                place.reviews_count = int(nums)
            except ValueError:
                pass

    # ── Reviews average ────────────────────────────────────────────────────────
    # Stars rating — aria-label like "4.5 stars" — iterate all matches since
    # the first span's aria-label may be just " stars" (no numeric value)
    place.reviews_average = None
    for xp in [
        '//meta[@itemprop="ratingValue"]',
        '//div[@class="TIHn2 "]/div[@class="fontBodyMedium dmRWX"]//span[@aria-hidden]',
        '//span[contains(@aria-label,"star")]',
    ]:
        for el in await page.locator(xp).all():
            raw = (await el.get_attribute("aria-label") or "") or (await el.get_attribute("content") or "") or (await el.inner_text() or "")
            if not raw:
                continue
            m = re.search(r"([\d.]+)", raw)
            if m:
                try:
                    val = float(m.group(1))
                    if 0 < val <= 5.0:
                        place.reviews_average = val
                        break
                except ValueError:
                    pass
        if place.reviews_average is not None:
            break

    # ── Price range ───────────────────────────────────────────────────────────
    price_text = await _try_texts(page, [
        '//div[@class="price-and-availability"]//div[contains(@class,"price")]',
        '//div[contains(@class,"price") and contains(@class,"font")]',
    ])
    place.price_range = parse_price_range(price_text)

    # ── Photos count ────────────────────────────────────────────────────────────
    # "X photos" — aria-label on the photos link/button
    photos_text = await _attr(page, '//a[@data-item-id="photos"]', "aria-label")
    if not photos_text:
        photos_text = await _try_texts(page, [
            '//a[@data-item-id="photos"]//span',
            '//*[contains(@aria-label,"photo")]',
        ])
    if photos_text:
        m = re.search(r"([\d,]+)", photos_text)
        if m:
            try:
                place.photos_count = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

    # ── Claimed status ────────────────────────────────────────────────────────
    claimed_text = await _attr(page, '//*[contains(@aria-label,"Claimed")]', "aria-label")
    place.claimed = "Yes" if claimed_text and "claim" in claimed_text.lower() else "No"

    # ── Closed status ──────────────────────────────────────────────────────────
    closed_text = await _try_texts(page, [
        '//div[contains(@class,"closed")]//span',
        '//div[contains(@class,"status-closed")]',
    ])
    if closed_text:
        lower = closed_text.lower()
        if "temporarily" in lower:
            place.closed_status = "Temporarily Closed"
        elif "permanently" in lower:
            place.closed_status = "Permanently Closed"
        else:
            place.closed_status = closed_text

    # ── Service chips (shopping, pickup, delivery) ─────────────────────────────
    service_text = await _try_texts(page, [
        '//div[contains(@class,"LTs0Rc")]',
        '//div[@class="section-attribute-alt"]',
    ])
    if service_text:
        lower = service_text.lower()
        place.store_shopping = "Yes" if any(w in lower for w in ["shop", "buy", "purchase"]) else "No"
        place.in_store_pickup = "Yes" if any(w in lower for w in ["pickup", "collect", "curbside"]) else "No"
        place.store_delivery = "Yes" if any(w in lower for w in ["delivery", "deliver", "delivers"]) else "No"

    # ── Additional service fields ───────────────────────────────────────────────
    place.wheelchair_accessible = "Yes" if await _count(page, '//*[contains(text(),"Wheelchair accessible")]') > 0 else "No"
    place.restroom = "Yes" if await _count(page, '//*[contains(text(),"Restroom")]') > 0 else "No"
    place.parking = "Yes" if await _count(page, '//*[contains(text(),"Parking")]') > 0 else "No"
    place.payment_cards = "Yes" if await _count(page, '//*[contains(text(),"Credit card")]') > 0 else "No"

    # ── Open hours ─────────────────────────────────────────────────────────────
    place.opens_at = await _try_texts(page, [
        '//button[@data-item-id="oh"]//div[contains(@class,"fontBodyMedium")]',
        '//div[@class="MkV9"]//span[@class="ZDu9vd"]//span[last()]',
        '//*[contains(@data-item-id,"hours")]//div[contains(@class,"fontBodyMedium")]',
    ])

    # ── Lat / Lng ──────────────────────────────────────────────────────────────
    place.latitude, place.longitude = parse_lat_lng(page.url)

    # ── Full reviews + snippet ─────────────────────────────────────────────────
    place.reviews = await extract_reviews(page)
    if place.reviews:
        place.review_snippet = place.reviews[0].text[:300]

    return place


# ─────────────────────────────────────────────────────────────────────────────
# Captcha detection
# ─────────────────────────────────────────────────────────────────────────────

async def is_captcha_page(page: Page) -> bool:
    """Return True if the page is showing a captcha or 'unusual traffic' block."""
    try:
        url_lo = page.url.lower()
        title_lo = (await page.title()).lower()
        body = (await page.inner_text("body"))[:600].lower() if await _count(page, "body") > 0 else ""
        return any(s in body or s in url_lo for s in [
            "unusual traffic", "captcha", "systems have detected",
        ]) or ("sorry" in title_lo and "google" in title_lo)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Scroll helper
# ─────────────────────────────────────────────────────────────────────────────

async def scroll_to_bottom(page: Page, max_retries: int = 10) -> int:
    """Scroll the results panel until no new listings appear. Return final count."""
    total = 0
    last = 0
    no_change = 0

    for i in range(max_retries):
        await page.evaluate("""
            var p = document.querySelector('[aria-label="Results"]') ||
                    document.querySelector('[role="feed"]') ||
                    document.querySelector('[class*="results"]');
            if (p) p.scrollTop += 3000;
            else window.scrollBy(0, 2000);
        """)
        await page.wait_for_timeout(1500)

        n = await _count(page, '//a[contains(@href,"/place/")]')
        total = max(total, n)
        logging.info("Scroll %d: %d listings", i + 1, total)

        if n == last:
            no_change += 1
            if no_change >= 2:
                logging.info("Reached end of results after %d scrolls", i + 1)
                break
        else:
            no_change = 0
        last = n
        if total >= 100:
            break

    return total


# ─────────────────────────────────────────────────────────────────────────────
# Listing collector
# ─────────────────────────────────────────────────────────────────────────────

async def collect_listing_links(page: Page, total: int) -> list[tuple[str, str]]:
    """Collect up to `total` place links from the search results page."""
    await page.wait_for_timeout(2000)
    await scroll_to_bottom(page)

    sel = '//a[contains(@href,"/place/")]'
    if await _count(page, sel) == 0:
        return []

    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    all_els = await page.locator(sel).all()
    for el in all_els:
        try:
            href = await el.get_attribute("href") or ""
            if href and href not in seen:
                seen.add(href)
                links.append((href, ""))
        except Exception:
            pass

    return links[:total]


# ─────────────────────────────────────────────────────────────────────────────
# Output serialization
# ─────────────────────────────────────────────────────────────────────────────

async def serialize_place(place: Place, fmt: str) -> dict:
    """Convert Place to a flat dict ready for CSV/JSONL writing.

    In CSV mode reviews list is base64-encoded to avoid embedded newlines
    corrupting CSV row structure. In JSONL mode it stays as JSON.
    """
    row = asdict(place)
    if "reviews" in row and row["reviews"] is not None:
        rev_json = json.dumps(row["reviews"], ensure_ascii=False)
        if fmt == "csv":
            row["reviews"] = base64.b64encode(rev_json.encode("utf-8")).decode("ascii")
        else:
            row["reviews"] = rev_json
    # Drop None / empty terminal fields
    row = {k: v for k, v in row.items() if v is not None and v != ""}
    return row


async def write_row(place: Place, path: str, fmt: str, header_needed: bool):
    """Append one Place as a row to path. Thread-safe via OUTPUT_LOCK."""
    async with OUTPUT_LOCK:
        row = await serialize_place(place, fmt)
        if fmt == "jsonl":
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=row.keys())
                if header_needed:
                    w.writeheader()
                w.writerow(row)


async def write_snapshot(place: Place, snap_path: str):
    """Write a JSON snapshot after each listing (crash recovery)."""
    async with OUTPUT_LOCK:
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(asdict(place), f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────────────────────────────────────

def load_seen_ids(path: str, fmt: str) -> set[str]:
    """Load place IDs (or names) already in the output file."""
    seen: set[str] = set()
    if not os.path.exists(path):
        return seen
    try:
        if fmt == "jsonl":
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        obj = json.loads(line)
                        pid = obj.get("place_id") or obj.get("name") or ""
                        if pid:
                            seen.add(pid)
        else:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    pid = row.get("place_id") or row.get("name") or ""
                    if pid:
                        seen.add(pid)
    except Exception as e:
        logging.debug("Dedup load error: %s", e)
    return seen


# ─────────────────────────────────────────────────────────────────────────────
# Core scrape
# ─────────────────────────────────────────────────────────────────────────────

async def _scrape_query(
    browser: Optional[Browser],
    query: str,
    total: int,
    output_path: str,
    fmt: str,
    headless: bool,
    stealth: bool,
    rate_limit: float,
    retry_count: int,
    dry_run: bool,
    quiet: bool,
    no_save: bool,
    proxy: Optional[str],
    snapshot_path: Optional[str],
) -> int:
    """
    Scrape one query. Returns count of places extracted.
    Launches own browser if browser is None.
    """
    own_browser = browser is None

    async def do_scrape(br, proxy_str: Optional[str]) -> int:
        ctx_opts: dict = {
            "viewport": {"width": random.randint(1200, 1400), "height": random.randint(700, 900)},
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
        if proxy_str:
            parts = proxy_str.split(":")
            if len(parts) >= 2:
                ctx_opts["proxy"] = {"server": f"http://{parts[0]}:{parts[1]}"}
                if len(parts) == 4:
                    ctx_opts["proxy"]["username"] = parts[2]
                    ctx_opts["proxy"]["password"] = parts[3]

        ctx = await br.new_context(**ctx_opts)
        if stealth:
            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                delete window.cdc_adoQpoasnfa76fcZLmcfl_Set;
            """)
        page = await ctx.new_page()
        page.set_default_timeout(30000)

        seen_ids: set[str] = set()
        first_row = not dry_run and not no_save and not os.path.exists(output_path)

        try:
            if not dry_run and not no_save:
                seen_ids = load_seen_ids(output_path, fmt)

            # ── Navigate to search URL
            encoded_q = query.replace(" ", "+")
            url = f"https://www.google.com/maps/search/{encoded_q}"
            if not quiet:
                logging.info("[%s] Opening: %s", query, url)
            await page.goto(url, timeout=60000)
            await page.wait_for_timeout(4000)

            if await is_captcha_page(page):
                logging.error("[%s] Captcha / block — aborting", query)
                return 0

            links = await collect_listing_links(page, total)
            if not quiet:
                logging.info("[%s] Collected %d listing links", query, len(links))

            if not links:
                # ── Fallback: use search box
                logging.warning("[%s] No results via direct URL — trying search box", query)
                await page.goto("https://www.google.com/maps", timeout=30000)
                await page.wait_for_timeout(2000)
                try:
                    for ck_xp in ['//button[contains(@id,"L2AGLe")]', '//button[contains(text(),"Accept")]']:
                        if await _count(page, ck_xp) > 0:
                            await page.locator(ck_xp).first.click()
                            await page.wait_for_timeout(1000)
                            break
                    sb = page.locator('//input[@id="searchboxinput"]')
                    await sb.fill(query)
                    await sb.press("Enter")
                    await page.wait_for_timeout(4000)
                    if await is_captcha_page(page):
                        logging.error("[%s] Captcha after fallback — aborting", query)
                        return 0
                    links = await collect_listing_links(page, total)
                    if not quiet:
                        logging.info("[%s] After fallback: %d links", query, len(links))
                except Exception as e:
                    logging.warning("[%s] Search-box fallback failed: %s", query, e)

            scraped = 0
            for idx, (href, _) in enumerate(links):
                if idx > 0:
                    await asyncio.sleep(rate_limit)

                pid_candidate = parse_place_id(href)
                if not pid_candidate:
                    pid_candidate = href
                if pid_candidate in seen_ids:
                    if not quiet:
                        logging.info("[%s] [%d/%d] Skipping duplicate: %s",
                                     query, idx + 1, len(links), pid_candidate[:30])
                    continue

                place = None
                last_err: Optional[Exception] = None

                for attempt in range(retry_count + 1):
                    try:
                        detail_url = href if href.startswith("http") else f"https://www.google.com{href}"
                        # Always force navigation by going to blank first —
                        # avoids Playwright treating a no-op goto as already satisfied
                        await page.goto("about:blank")
                        await page.goto(detail_url, timeout=30000)
                        await page.wait_for_timeout(2000)

                        if await is_captcha_page(page):
                            logging.error("[%s] Captcha on listing %d — aborting", query, idx + 1)
                            break

                        for sel in ['//h1[contains(@class,"DUwDvf")]', '//h1']:
                            try:
                                await page.wait_for_selector(sel, timeout=5000)
                                break
                            except Exception:
                                pass
                        await page.wait_for_timeout(500)

                        place = await extract_place(page, query)
                        if place.name:
                            break
                    except Exception as e:
                        last_err = e
                        logging.debug("Attempt %d for %s: %s", attempt + 1, href, e)
                        await asyncio.sleep(1.5)

                if place and place.name:
                    if dry_run:
                        if not quiet:
                            logging.info("[%s] [DRY-RUN %d/%d] %s | %s | %s",
                                         query, idx + 1, len(links),
                                         place.name, place.address[:40], place.phone_number)
                    elif no_save:
                        # Stream JSON to stdout
                        row = await serialize_place(place, "jsonl")
                        print(json.dumps(row, ensure_ascii=False))
                    else:
                        await write_row(place, output_path, fmt, first_row)
                        if snapshot_path:
                            await write_snapshot(place, snapshot_path)
                        first_row = False
                        seen_ids.add(place.place_id or place.name)
                        scraped += 1
                        if not quiet:
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
            await ctx.close()

    launch_args = ["--no-sandbox"]
    if stealth:
        launch_args.extend(["--disable-blink-features=Automation", "--disable-dev-shm-usage"])

    if own_browser:
        async with async_playwright() as p:
            br = await p.chromium.launch(headless=headless, args=launch_args)
            return await do_scrape(br, proxy)
    else:
        return await do_scrape(browser, proxy)


# ─────────────────────────────────────────────────────────────────────────────
# Batch runner
# ─────────────────────────────────────────────────────────────────────────────

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
    dry_run: bool,
    quiet: bool,
    proxies: Optional[list[str]],
    snapshot_dir: Optional[str],
) -> None:
    os.makedirs(output_dir, exist_ok=True)
    if snapshot_dir:
        os.makedirs(snapshot_dir, exist_ok=True)

    launch_args = ["--no-sandbox"]
    if stealth:
        launch_args.extend(["--disable-blink-features=Automation", "--disable-dev-shm-usage"])

    async with async_playwright() as p:
        br = await p.chromium.launch(headless=headless, args=launch_args)
        sem = asyncio.Semaphore(parallel)
        tasks = []

        for i, q in enumerate(queries):
            safe = safe_filename(q)
            out = os.path.join(output_dir, f"{safe}.{fmt}")
            snap = os.path.join(snapshot_dir, f"{safe}.partial.json") if snapshot_dir else None
            prx = proxies[i % len(proxies)] if proxies else None

            async def run(query: str, path: str, sp: Optional[str], px: Optional[str]):
                async with sem:
                    return await _scrape_query(
                        browser=br, query=query, total=total,
                        output_path=path, fmt=fmt,
                        headless=headless, stealth=stealth,
                        rate_limit=rate_limit, retry_count=retry_count,
                        dry_run=dry_run, quiet=quiet, no_save=False,
                        proxy=px, snapshot_path=sp,
                    )

            tasks.append(run(q, out, snap, prx))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        await br.close()

        for q, r in zip(queries, results):
            if isinstance(r, Exception):
                logging.error("[%s] Failed: %s", q, r)
            else:
                logging.info("[%s] Done — %d scraped", q, r)


# ─────────────────────────────────────────────────────────────────────────────
# Config
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
    p = argparse.ArgumentParser(
        description="Scrape Google Maps with async Playwright.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-s", "--search", type=str, help="Single search query")
    p.add_argument("-t", "--total", type=int, default=10, help="Max listings per query (default: 10)")
    p.add_argument("-o", "--output", type=str, default="maps_results",
                   help="Output file path (single) or directory (batch). Default: maps_results.csv")
    p.add_argument("-f", "--format", choices=["csv", "jsonl"], default="csv")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--visible", action="store_true", help="Show browser window")
    p.add_argument("--no-stealth", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true", help="Errors only")
    p.add_argument("--batch", type=str, metavar="FILE", help="One query per line; one output per query")
    p.add_argument("-p", "--parallel", type=int, default=1, choices=[1, 2, 3, 4])
    p.add_argument("--proxy", type=str, metavar="HOST:PORT[:USER:PASS]")
    p.add_argument("--rate-limit", type=float, default=1.5)
    p.add_argument("--retry", type=int, default=2)
    p.add_argument("--config", type=str, default="config.yaml")
    p.add_argument("--fields", type=str, help="Comma-separated fields, e.g. name,phone,address")
    p.add_argument("--dry-run", action="store_true", help="Verify selectors without writing output")
    p.add_argument("--no-save", action="store_true", help="Stream JSON to stdout instead of file")
    p.add_argument("--snapshot-dir", type=str, metavar="DIR")
    p.add_argument("--proxies", type=str, metavar="FILE", help="One proxy per line; round-robins in batch")
    return p.parse_args()


def main():
    setup_logging()
    args = parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.ERROR)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(args.config)

    search = args.search
    total = args.total
    headless = False if args.visible else config.get("headless", True)
    stealth = not args.no_stealth
    parallel = args.parallel
    rate_limit = args.rate_limit
    retry_count = args.retry
    dry_run = args.dry_run
    quiet = args.quiet
    no_save = args.no_save

    proxies: Optional[list[str]] = None
    if args.proxies and os.path.isfile(args.proxies):
        with open(args.proxies, encoding="utf-8") as f:
            proxies = [ln.strip() for ln in f if ln.strip()]
        if proxies and not quiet:
            logging.info("Loaded %d proxies from %s", len(proxies), args.proxies)

    fmt = args.format
    output_base = args.output

    # Normalise output path
    if fmt == "jsonl" and not output_base.endswith(".jsonl"):
        output_base += ".jsonl"
    elif fmt == "csv" and not output_base.endswith(".csv"):
        output_base += ".csv"

    if not search and not args.batch:
        print("Error: provide -s QUERY or --batch FILE", file=sys.stderr)
        sys.exit(1)

    if args.batch:
        queries = [ln.strip() for ln in open(args.batch, encoding="utf-8") if ln.strip()]
        if not queries:
            print(f"Error: no queries in {args.batch}", file=sys.stderr)
            sys.exit(1)
        out_dir = output_base if os.path.isdir(output_base) else os.path.dirname(output_base) or "."
        asyncio.run(scrape_batch(
            queries=queries, total=total, output_dir=out_dir,
            fmt=fmt, headless=headless, stealth=stealth,
            parallel=parallel, rate_limit=rate_limit, retry_count=retry_count,
            dry_run=dry_run, quiet=quiet,
            proxies=proxies, snapshot_dir=args.snapshot_dir,
        ))
    else:
        out_path = "/dev/null" if dry_run else output_base
        snap = None
        if args.snapshot_dir:
            os.makedirs(args.snapshot_dir, exist_ok=True)
            snap = os.path.join(args.snapshot_dir, f"{safe_filename(search)}.partial.json")

        n = asyncio.run(_scrape_query(
            browser=None, query=search, total=total,
            output_path=out_path, fmt=fmt,
            headless=headless, stealth=stealth,
            rate_limit=rate_limit, retry_count=retry_count,
            dry_run=dry_run, quiet=quiet, no_save=no_save,
            proxy=args.proxy, snapshot_path=snap,
        ))
        if not quiet:
            logging.info("Done — %d places scraped", n)


if __name__ == "__main__":
    main()
