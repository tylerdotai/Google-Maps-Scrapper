# Google Maps Scraper

A Playwright-based scraper that extracts business listings from Google Maps — name, address, website, phone, reviews, services, hours, and more.

Forked from [zohaibbashir/Google-Maps-Scrapper](https://github.com/zohaibbashir/Google-Maps-Scrapper) with multiple selector strategies added for reliability against DOM changes.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python main.py -s "coffee shops in Austin TX" -t 20 -o results.csv

# Visible browser (debug)
python main.py -s "restaurants in Dallas" -t 10 --visible

# Append to existing CSV
python main.py -s "dentists in Houston" -t 50 -o results.csv --append

# Verbose output
python main.py -s "plumbers near me" -t 25 -v
```

## CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `-s, --search` | Search query for Google Maps | **required** |
| `-t, --total` | Max listings to scrape | 10 |
| `-o, --output` | Output CSV file | maps_results.csv |
| `--headless` | Run browser headless | True |
| `--visible` | Force visible browser (overrides headless) | False |
| `--no-stealth` | Disable automation detection bypass | False |
| `--append` | Append to existing CSV | False |
| `-v, --verbose` | DEBUG-level logging | False |

## Output Fields

Each row includes:

- **name** — Business name
- **address** — Full street address
- **website** — Business website URL
- **phone_number** — Phone number
- **reviews_count** — Total number of reviews
- **reviews_average** — Average star rating
- **store_shopping** — "Yes" if buy-in-store available
- **in_store_pickup** — "Yes" if pickup available
- **store_delivery** — "Yes" if delivery available
- **place_type** — Business category
- **opens_at** — Next opening time
- **introduction** — Business description

Columns where all values are identical (e.g., all "No") are automatically dropped.

## Selector Strategy

The scraper tries **multiple XPath strategies** for each field — if one fails, it falls back to the next. This makes the scraper more resilient against Google Maps DOM changes, which happen frequently.

If the scraper stops working, the issue is almost certainly a DOM change. Check:
1. Open Google Maps in a browser
2. Search for something
3. Inspect the DOM to find the new selectors
4. Update the `extract_place()` function in main.py

## Architecture

```
scrape_places() → launches Chromium → searches → scrolls → clicks listings → extract_place() → save_places_to_csv()
```

- `extract_place()`: pulls all fields from the detail panel
- `scroll_to_bottom()`: lazy-loads results by scrolling the results panel
- Multiple fallback selectors per field — no single point of failure