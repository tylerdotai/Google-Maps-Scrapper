# Google Maps Scraper

Async Playwright scraper that extracts business listings from Google Maps — name, address, phone, reviews, price range, coordinates, services, and more.

Forked from [zohaibbashir/Google-Maps-Scrapper](https://github.com/zohaibbashir/Google-Maps-Scrapper), fully rewritten with async Playwright and 18 enhancements.

## Install

```bash
git clone https://github.com/tylerdotai/Google-Maps-Scrapper
cd Google-Maps-Scrapper

uv venv .venv --python 3.11
. .venv/bin/activate
uv pip install -r requirements.txt
playwright install chromium
```

## Config (optional)

Copy `config.yaml.example` to `config.yaml` to set default values for all CLI flags. CLI flags always override config values.

```bash
cp config.yaml.example config.yaml
```

## Usage

```bash
# Single query
python main.py -s "coffee shops in Austin TX" -t 20 -o results.csv

# JSONL output (newline-delimited JSON)
python main.py -s "restaurants in Dallas" -t 10 -f jsonl -o results.jsonl

# Visible browser (debugging)
python main.py -s "dentists in Houston" -t 10 --visible

# Append to existing file
python main.py -s "plumbers near me" -t 25 -o results.csv --append

# Verbose logging
python main.py -s "gyms in Austin" -t 20 -v

# Proxy support
python main.py -s "coffee shops in Austin TX" -t 10 --proxy "123.45.67.89:8080:user:pass"

# Parallel tabs (2-4) for faster scraping
python main.py -s "coffee shops in Austin TX" -t 40 -p 3

# Batch mode — one output file per query line
python main.py --batch queries.txt -t 10 -o ./output_dir/
# queries.txt:
#   coffee shops in Austin TX
#   restaurants in Dallas

# Field selection — output only what you need
python main.py -s "coffee shops in Austin TX" -t 20 -o results.csv \
  --fields name,phone_number,address,reviews_count,reviews_average

# Dry run — verify selectors without writing output
python main.py -s "coffee shops in Austin TX" -t 3 --dry-run -v

# Stream to stdout as JSON (pipe into other tools)
python main.py -s "coffee shops in Austin TX" -t 20 --no-save | jq .

# Snapshot recovery — partial results saved per listing
python main.py -s "coffee shops in Austin TX" -t 50 -o results.csv --snapshot-dir ./snapshots/

# Multiple proxies (round-robin)
python main.py -s "coffee shops in Austin TX" -t 50 -o results.csv \
  --proxies "proxy1:1234,proxy2:5678"
```

## CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `-s, --search` | Single search query | — |
| `-t, --total` | Max listings to scrape per query | 10 |
| `-o, --output` | Output file path (auto-adds .csv/.jsonl) | maps_results.csv |
| `-f, --format` | `csv` or `jsonl` | csv |
| `-p, --parallel N` | Parallel browser tabs (1-4) | 1 |
| `--fields F1,F2` | Comma-separated field list — output only these | all fields |
| `--batch FILE` | Text file, one query per line | — |
| `--append` | Append to existing output file | False |
| `--headless` | Run browser headless (default: True) | True |
| `--visible` | Force visible browser | False |
| `--no-stealth` | Disable stealth mode | False |
| `--proxy HOST:PORT[:USER:PASS]` | HTTP/SOCKS proxy with optional auth | — |
| `--proxies P1,P2` | Comma-separated proxy list, round-robin per query | — |
| `--rate-limit SEC` | Seconds between listing clicks | 1.5 |
| `--retry N` | Retries per listing on failure | 2 |
| `--dry-run` | Verify selectors only, no output | False |
| `--no-save` | Stream results to stdout as JSON lines | False |
| `--quiet` | Suppress all but errors | False |
| `--snapshot-dir DIR` | Save a JSON snapshot per listing for crash recovery | — |
| `--config FILE` | Path to config.yaml | ./config.yaml |
| `-v, --verbose` | DEBUG-level logging | False |

## Output Fields

Each row includes:

- **place_id** — Google Maps place identifier (parsed from URL)
- **query** — Search string used
- **scraped_at** — ISO 8601 timestamp of scrape
- **name** — Business name
- **address** — Full street address
- **website** — Business website URL
- **phone_number** — Phone number
- **reviews_count** — Total number of reviews
- **reviews_average** — Average star rating (0-5)
- **price_range** — $, $$, $$$ (price tier)
- **photos_count** — Number of photos uploaded
- **claimed** — "Yes" if Google-verified claimed business
- **closed_status** — "Temporarily Closed" or "Permanently Closed"
- **store_shopping** — "Yes" if buy-in-store available
- **in_store_pickup** — "Yes" if local pickup available
- **store_delivery** — "Yes" if delivery available
- **wheelchair_accessible** — "Yes" if wheelchair accessible
- **restroom** — "Yes" if restroom available
- **parking** — "Yes" if parking available
- **payment_cards** — "Yes" if card payment accepted
- **place_type** — Business category (e.g., "Coffee shop", "Pizza restaurant")
- **opens_at** — Next opening/closing time
- **latitude** — Decimal degrees north
- **longitude** — Decimal degrees east
- **introduction** — Business description (first paragraph)
- **reviews** — Base64-encoded JSON array of review objects: `{"author","rating","date","text"}`
- **review_snippet** — First review text (legacy alias)

CSV columns where all values are empty or identical are dropped. The `reviews` field is base64-encoded JSON to avoid embedded newlines corrupting CSV rows.

## Features

**Reliability**
- **Retry logic** — 2 retries per listing on failure (configurable via `--retry`)
- **Rate limiting** — 1.5s between listing clicks to avoid bot detection
- **Captcha detection** — aborts gracefully if "unusual traffic" or captcha page appears
- **Search-box fallback** — if direct URL returns no listings, falls back to filling the search box
- **`about:blank` force-nav** — avoids Playwright treating no-op navigations as already satisfied

**Data**
- **Multi-strategy XPath** — multiple fallback selectors per field; if one fails, the next is tried
- **Reviews array** — base64-encoded JSON array of `{author, rating, date, text}` per listing
- **Service fields** — wheelchair, restroom, parking, payment cards
- **Place ID** — extracted from Google Maps URL for dedup across scrapes
- **Per-query metadata** — `place_id`, `query`, `scraped_at` timestamp in every row

**Output / UX**
- **Streaming writes** — each row written immediately, no buffering (safe for large scrapes)
- **JSONL output** — newline-delimited JSON for line-by-line processing
- **Field selection** — `--fields name,phone,address` output only what you need
- **Deduplication** — skips places already in output (by place_id or name)
- **Crash recovery** — `--snapshot-dir` saves a JSON snapshot after each listing
- **Dry-run mode** — verify selectors without writing output
- **Quiet mode** — suppress all log output except errors
- **Config file** — `config.yaml` sets defaults, CLI flags override

**Speed / Stealth**
- **Async Playwright** — asyncio-based throughout, lower memory footprint
- **Parallel tabs** — 2-4 concurrent browser contexts for batch queries
- **Proxy rotation** — `--proxies p1,p2,p3` round-robins per query
- **Randomized fingerprint** — random viewport, user agent, locale, timezone per context

## Selector Strategy

The scraper uses **multiple XPath fallback selectors** per field. Google Maps DOM changes frequently, so if the scraper stops working:

1. Open Google Maps in a browser, search for a business
2. Open DevTools (F12), inspect the detail panel HTML
3. Find the current selectors for each field
4. Update `extract_place()` in `main.py` with the new paths

Common patterns that change: `data-item-id` values, CSS class names, `aria-label` text, section structure.