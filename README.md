# Google Maps Scraper

Async Playwright scraper that extracts business listings from Google Maps — name, address, phone, reviews, price range, photos, hours, coordinates, and more.

Forked from [zohaibbashir/Google-Maps-Scrapper](https://github.com/zohaibbashir/Google-Maps-Scrapper), rewritten with async Playwright and 18 enhancements.

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

Copy `config.yaml.example` to `config.yaml` to set default values for all CLI flags:

```bash
cp config.yaml.example config.yaml
# then edit config.yaml
```

CLI flags always override config.yaml values.

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

# Verbose DEBUG output
python main.py -s "gyms in Austin" -t 20 -v

# Proxy support
python main.py -s "coffee shops in Austin TX" -t 10 --proxy "123.45.67.89:8080:user:pass"

# Parallel tabs (2–4) for faster scraping
python main.py -s "coffee shops in Austin TX" -t 40 -p 3

# Batch mode — one file per query
python main.py --batch queries.txt -t 10 -o ./output_dir/
# queries.txt example:
#   coffee shops in Austin TX
#   restaurants in Dallas
#   gyms in Houston
```

## CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `-s, --search` | Single search query | — |
| `-t, --total` | Max listings per query | 10 |
| `-o, --output` | Output file base name (no extension) | maps_results |
| `-f, --format` | `csv` or `jsonl` | csv |
| `--headless` | Run browser headless | True |
| `--visible` | Force visible browser | False |
| `--no-stealth` | Disable stealth/automation bypass | False |
| `--append` | Append to existing output file | False |
| `-v, --verbose` | DEBUG-level logging | False |
| `--batch FILE` | One query per line; produces one file per query | — |
| `-p, --parallel N` | Parallel browser tabs (1–4) | 1 |
| `--proxy HOST:PORT[:USER:PASS]` | HTTP/SOCKS proxy | — |
| `--rate-limit SEC` | Seconds between listing clicks | 1.5 |
| `--retry N` | Retries per listing on failure | 2 |
| `--config FILE` | Path to config.yaml | ./config.yaml |

## Output Fields

Each row includes:

- **name** — Business name
- **address** — Full street address
- **website** — Business website URL
- **phone_number** — Phone number
- **reviews_count** — Total number of reviews
- **reviews_average** — Average star rating
- **price_range** — $, $$, $$$ (price tier)
- **photos_count** — Number of photos uploaded
- **claimed** — "Yes" if Google-verified claimed business
- **closed_status** — "Temporarily Closed" or "Permanently Closed" if applicable
- **store_shopping** — "Yes" if buy-in-store available
- **in_store_pickup** — "Yes" if pickup available
- **store_delivery** — "Yes" if delivery available
- **place_type** — Business category
- **opens_at** — Next opening time (e.g., "Closes 7PM")
- **introduction** — Business description
- **latitude** — Decimal degrees north
- **longitude** — Decimal degrees east
- **review_snippet** — First review text

Columns where all values are identical (e.g., all "No") are automatically dropped from CSV output.

## Features

- **Async Playwright** — asyncio-based, lower memory footprint at scale
- **Retry logic** — 2 retries per listing (configurable via `--retry`)
- **Rate limiting** — 1.5s between listing clicks to avoid bot detection
- **Captcha detection** — aborts gracefully if "unusual traffic" or captcha page appears
- **Search-box fallback** — if direct URL returns no results, falls back to filling the search box
- **Parallel tabs** — run 2–4 queries concurrently (use responsibly to avoid blocks)
- **Streaming writes** — each row written immediately, no buffering (safe for large scrapes)
- **JSONL output** — newline-delimited JSON for easy line-by-line processing
- **Config file** — `config.yaml` sets defaults, CLI flags override
- **Proxy support** — HTTP/SOCKS with optional auth
- **Randomized fingerprint** — randomized viewport, user agent, locale, timezone per context
- **Multi-strategy XPath** — multiple fallback selectors per field for DOM resilience

## Selector Strategy

The scraper tries **multiple XPath strategies** for each field — if one fails, it falls back to the next. This makes the scraper more resilient against Google Maps DOM changes, which happen frequently.

If the scraper stops working, the issue is almost certainly a DOM change:

1. Open Google Maps in a browser, search for something
2. Open DevTools (F12), inspect the detail panel HTML
3. Find the new selectors for each field
4. Update the `extract_place()` function in `main.py`