import logging
import argparse
import platform
import time
import os
from typing import List, Optional

from playwright.sync_api import sync_playwright, Page
from dataclasses import dataclass, asdict
import pandas as pd


@dataclass
class Place:
    name: str = ""
    address: str = ""
    website: str = ""
    phone_number: str = ""
    reviews_count: Optional[int] = None
    reviews_average: Optional[float] = None
    store_shopping: str = "No"
    in_store_pickup: str = "No"
    store_delivery: str = "No"
    place_type: str = ""
    opens_at: str = ""
    introduction: str = ""


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
    )


def extract_text(page: Page, xpath: str) -> str:
    try:
        if page.locator(xpath).count() > 0:
            return page.locator(xpath).first.inner_text()
    except Exception as e:
        logging.debug(f"XPath extract failed for {xpath}: {e}")
    return ""


def extract_text_multi(page: Page, xpaths: List[str]) -> str:
    """Try multiple xpaths until one succeeds."""
    for xpath in xpaths:
        result = extract_text(page, xpath)
        if result:
            return result
    return ""


def extract_place(page: Page) -> Place:
    place = Place()

    # Name — multiple selector strategies
    place.name = extract_text_multi(page, [
        '//h1[contains(@class,"DUwDvf")]',
        '//h1[@class="DUwDvf lfPIob"]',
        '//div[@class="TIHn2 "]//h1',
        '//*[contains(@class,"header-title")]/h1',
        '//h1[contains(@data-value,"name")]',
    ])

    # Address — data-item-id is the most stable Google Maps selector
    place.address = extract_text_multi(page, [
        '//button[@data-item-id="address"]//div[contains(@class,"fontBodyMedium")]',
        '//*[contains(@data-item-id,"address")]',
        '//span[contains(@data-item-id,"address")]//following-sibling::div',
        '//div[@data-value="address"]',
    ])

    # Website
    place.website = extract_text_multi(page, [
        '//a[@data-item-id="authority"]//div[contains(@class,"fontBodyMedium")]',
        '//a[contains(@href,"://") and not(contains(@href,"google.com"))]',
        '//*[contains(@data-item-id,"website")]//following-sibling::div',
    ])

    # Phone
    place.phone_number = extract_text_multi(page, [
        '//button[contains(@data-item-id,"phone:tel:")]//div[contains(@class,"fontBodyMedium")]',
        '//button[contains(@data-item-id,"phone")]//div[contains(@class,"fontBodyMedium")]',
        '//*[contains(@data-item-id,"phone")]',
    ])

    # Place type / category
    place.place_type = extract_text_multi(page, [
        '//div[@class="LBgpqf"]//button[contains(@class,"DkEaL")]',
        '//button[contains(@class,"DkEaL")]',
        '//span[contains(@class,"category")]',
        '//div[contains(@class,"place-type")]',
    ])

    # Introduction / description
    place.introduction = extract_text_multi(page, [
        '//div[@class="WeS02d fontBodyMedium"]//div[@class="PYvSYb "]',
        '//div[contains(@class,"intro-text")]',
        '//div[@class="PYvSYb "]',
        '//div[contains(@class,"WeS02d")]',
    ]) or "None Found"

    # Reviews count (aria-label is stable across localizations)
    reviews_count_raw = extract_text_multi(page, [
        '//div[@class="TIHn2 "]//div[@class="fontBodyMedium dmRWX"]//div//span//span//span[@aria-label]',
        '//span[@aria-label and contains(@aria-label,"review")]',
        '//*[contains(@aria-label,"review")]',
    ])
    if reviews_count_raw:
        try:
            import re
            nums = re.sub(r'[^\d]', '', reviews_count_raw)
            if nums:
                place.reviews_count = int(nums)
        except Exception as e:
            logging.debug(f"Failed to parse reviews count: {e}")

    # Reviews average
    reviews_avg_raw = extract_text_multi(page, [
        '//div[@class="TIHn2 "]//div[@class="fontBodyMedium dmRWX"]//div//span[@aria-hidden]',
        '//span[@aria-hidden and contains(text(),".")]',
        '//div[contains(@class,"rating")]//span',
    ])
    if reviews_avg_raw:
        try:
            temp = reviews_avg_raw.replace(' ', '').replace(',', '.')
            place.reviews_average = float(temp)
        except Exception as e:
            logging.debug(f"Failed to parse reviews average: {e}")

    # Store services (shopping, pickup, delivery) — look for service chips
    service_xpaths = [
        '//div[contains(@class,"LTs0Rc")]',
        '//span[contains(text(),"Buy") or contains(text(),"Pickup") or contains(text(),"Delivery")]',
        '//div[contains(@class,"service-badge")]',
    ]
    for info_xpath in service_xpaths:
        info_raw = extract_text(page, info_xpath)
        if info_raw:
            lower = info_raw.lower()
            if 'shop' in lower or 'buy' in lower:
                place.store_shopping = "Yes"
            if 'pickup' in lower or 'collect' in lower:
                place.in_store_pickup = "Yes"
            if 'delivery' in lower or 'deliver' in lower:
                place.store_delivery = "Yes"

    # Opens at — multiple selector strategies
    opens_at_raw = extract_text_multi(page, [
        '//button[contains(@data-item-id,"oh")]//div[contains(@class,"fontBodyMedium")]',
        '//div[@class="MkV9"]//span[@class="ZDu9vd"]//span[2]',
        '//*[contains(@data-item-id,"hours")]//div[contains(@class,"fontBodyMedium")]',
        '//span[contains(@class,"currently-open")]',
        '//div[contains(@class,"open")]//span',
    ])
    if opens_at_raw:
        parts = opens_at_raw.split('⋅')
        if len(parts) > 1:
            place.opens_at = parts[1].replace("\u202f", "").replace("\n", "").strip()
        else:
            place.opens_at = opens_at_raw.replace("\u202f", "").replace("\n", "").strip()

    return place


def scroll_to_bottom(page: Page, max_retries: int = 10) -> int:
    """Scroll to bottom of results list. Returns number of results loaded."""
    total_found = 0
    last_count = 0
    no_change_count = 0

    for i in range(max_retries):
        # Scroll down using evaluate to trigger lazy loading
        page.evaluate("""
            const panel = document.querySelector('[aria-label="Results"]') ||
                          document.querySelector('[role="feed"]') ||
                          document.querySelector('[class*="result"]');
            if (panel) panel.scrollTop += 3000;
            else window.scrollBy(0, 2000);
        """)
        page.wait_for_timeout(1500)

        # Count listings using multiple selector strategies
        selectors = [
            '//a[contains(@href,"/place/")]',
            '//div[contains(@class,"result")]//a',
            '//*[contains(@class,"place-card")]',
        ]
        found = 0
        for sel in selectors:
            try:
                found = page.locator(sel).count()
                if found > 0:
                    break
            except Exception:
                pass

        logging.info(f"Scroll {i+1}: found {found} listings")
        total_found = max(total_found, found)

        if found == last_count:
            no_change_count += 1
            if no_change_count >= 2:
                logging.info("No new results after 2 scrolls — reached end")
                break
        else:
            no_change_count = 0

        last_count = found
        if found >= 100:  # reasonable cap
            break

    return total_found


def scrape_places(search_for: str, total: int, headless: bool = True,
                  stealth: bool = True, output_path: str = "result.csv",
                  append: bool = False, verbose: bool = False) -> List[Place]:
    setup_logging(verbose)
    places: List[Place] = []

    with sync_playwright() as p:
        # Browser launch args
        launch_args = ["--no-sandbox"]
        if stealth:
            launch_args.extend([
                "--disable-blink-features=Automation",
                "--disable-dev-shm-usage",
            ])

        browser = p.chromium.launch(headless=headless, args=launch_args)

        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        # Stealth: block detection scripts
        if stealth:
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Set;
            """)

        page = context.new_page()
        page.set_default_timeout(30000)

        try:
            logging.info(f"Opening Google Maps with search: {search_for}")
            page.goto("https://www.google.com/maps", timeout=60000)
            page.wait_for_timeout(1500)

            # Accept cookies if prompt appears
            try:
                page.locator('button:has-text("Accept all"), button:has-text("Reject")').first.click()
                page.wait_for_timeout(500)
            except Exception:
                pass

            # Fill search box
            search_selectors = [
                '//input[@id="searchboxinput"]',
                '//input[@placeholder="Search Google Maps"]',
                '//input[contains(@class,"searchbox")]',
            ]
            for sel in search_selectors:
                if page.locator(sel).count() > 0:
                    page.locator(sel).fill(search_for)
                    page.keyboard.press("Enter")
                    break

            page.wait_for_timeout(3000)

            # Scroll to load results
            found = scroll_to_bottom(page)
            logging.info(f"Found {found} total listings for '{search_for}'")

            # Get all listing links
            listing_selectors = [
                '//a[contains(@href,"/place/")]',
            ]
            listing_locator = None
            for sel in listing_selectors:
                if page.locator(sel).count() > 0:
                    listing_locator = page.locator(sel)
                    break

            if not listing_locator:
                logging.warning("No listings found on page")
                return places

            # Deduplicate and cap
            seen = set()
            listing_elements = []
            for el in listing_locator.all():
                try:
                    href = el.get_attribute('href')
                    if href and href not in seen:
                        seen.add(href)
                        listing_elements.append(el)
                        if len(listing_elements) >= total:
                            break
                except Exception:
                    pass

            listing_elements = listing_elements[:total]
            logging.info(f"Clicking {len(listing_elements)} listings")

            for idx, listing in enumerate(listing_elements):
                try:
                    listing.scroll_into_view_if_needed()
                    listing.click()
                    page.wait_for_timeout(2000)

                    # Wait for detail panel to load
                    page.wait_for_selector(
                        '//h1[contains(@class,"DUwDvf")], //h1[contains(@class,"header")]',
                        timeout=8000
                    )
                    page.wait_for_timeout(500)

                    place = extract_place(page)
                    if place.name:
                        places.append(place)
                        logging.info(f"[{idx+1}/{len(listing_elements)}] {place.name}")
                    else:
                        logging.warning(f"No name for listing {idx+1}, skipping")
                except Exception as e:
                    logging.warning(f"Failed listing {idx+1}: {e}")
                    # Try pressing Escape to close any popup
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(500)
                    except Exception:
                        pass

        finally:
            browser.close()

    return places


def save_places_to_csv(places: List[Place], output_path: str = "result.csv", append: bool = False):
    df = pd.DataFrame([asdict(place) for place in places])
    if not df.empty:
        # Drop columns where all values are the same (e.g., "No" for all services)
        for column in df.columns:
            if df[column].nunique() == 1:
                df.drop(column, axis=1, inplace=True)

        file_exists = os.path.isfile(output_path)
        mode = "a" if append else "w"
        header = not (append and file_exists)
        df.to_csv(output_path, index=False, mode=mode, header=header)
        logging.info(f"Saved {len(df)} places to {output_path} (append={append})")
    else:
        logging.warning("No data to save. DataFrame is empty.")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape business data from Google Maps using Playwright.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-s", "--search", type=str, required=True,
                        help="Search query for Google Maps (e.g., 'coffee shops in Austin TX')")
    parser.add_argument("-t", "--total", type=int, default=10,
                        help="Total number of listings to scrape (default: 10)")
    parser.add_argument("-o", "--output", type=str, default="maps_results.csv",
                        help="Output CSV file path (default: maps_results.csv)")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run browser in headless mode (default: True)")
    parser.add_argument("--visible", action="store_true",
                        help="Run browser in visible mode (overrides --headless)")
    parser.add_argument("--no-stealth", action="store_true",
                        help="Disable stealth mode (automation detection bypass)")
    parser.add_argument("--append", action="store_true",
                        help="Append results to the output file instead of overwriting")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose (DEBUG) logging")

    args = parser.parse_args()

    search_for = args.search
    total = args.total
    output_path = args.output
    append = args.append
    headless = False if args.visible else args.headless
    stealth = not args.no_stealth
    verbose = args.verbose

    places = scrape_places(
        search_for=search_for,
        total=total,
        headless=headless,
        stealth=stealth,
        output_path=output_path,
        append=append,
        verbose=verbose,
    )
    save_places_to_csv(places, output_path, append=append)


if __name__ == "__main__":
    main()