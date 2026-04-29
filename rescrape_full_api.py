#!/usr/bin/env python3
"""
Re-scrape Steel City API to collect FULL product data for all products.

The original scrape only captured 10 of 62 available fields. This script
collects everything — especially:
  - category_names: full category hierarchy (Brand > Part Type > Sub-type)
  - categoryPath: structured category tree
  - product_code2: secondary/cross-reference part number
  - other_images: additional product images we never downloaded
  - original_Price: Steel City MSRP
  - alt_items: replacement/substitute parts (was captured but discarded)
  - qty_on_hand: actual inventory count

Usage:
  python3 rescrape_full_api.py              # Full re-scrape (resumable)
  python3 rescrape_full_api.py --limit 10   # Test with 10 products
"""

import argparse
import json
import logging
import os
import random
import time
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

# ── Config ──────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "product_names.json"
PROGRESS_FILE = BASE_DIR / "rescrape_progress.json"
OUTPUT_FILE = BASE_DIR / "full_api_data.json"

CONFIG = {
    "base_url": "https://www.steelcityvac.com",
    "account": os.environ.get("SC_ACCOUNT", ""),
    "user_id": os.environ.get("SC_USER", ""),
    "password": os.environ.get("SC_PASSWORD", ""),
}

BATCH_SIZE = 10  # Concurrent API calls per batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── Browser helpers ─────────────────────────────────────────────────────────

def create_driver():
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    return uc.Chrome(options=options, version_main=145)


def login(driver):
    driver.get(f"{CONFIG['base_url']}/a/s/")
    time.sleep(6)

    driver.find_element(By.ID, "scv_customer_number").clear()
    driver.find_element(By.ID, "scv_customer_number").send_keys(CONFIG["account"])
    driver.find_element(By.ID, "username_login_box").clear()
    driver.find_element(By.ID, "username_login_box").send_keys(CONFIG["user_id"])
    driver.find_element(By.ID, "password_login").clear()
    driver.find_element(By.ID, "password_login").send_keys(CONFIG["password"])
    driver.find_element(By.NAME, "loginSubmit").click()
    time.sleep(5)
    log.info(f"Logged in. URL: {driver.current_url}")

    driver.get(CONFIG["base_url"])
    time.sleep(3)


def api_batch(driver, skus):
    """Call product_info API for multiple SKUs concurrently, return FULL responses."""
    sku_list_js = json.dumps(skus)
    js = f"""
        var callback = arguments[arguments.length - 1];
        var skus = {sku_list_js};
        var results = {{}};
        var done = 0;
        skus.forEach(function(sku) {{
            $.ajax({{
                type: 'POST',
                url: 'applications/shopping_cart/web_services.php?action=product_info&name=' + encodeURIComponent(sku),
                success: function(data) {{
                    if (typeof data === 'string') try {{ data = JSON.parse(data); }} catch(e) {{}}
                    results[sku] = data;
                    done++;
                    if (done === skus.length) callback(JSON.stringify(results));
                }},
                error: function() {{
                    results[sku] = null;
                    done++;
                    if (done === skus.length) callback(JSON.stringify(results));
                }}
            }});
        }});
    """
    driver.set_script_timeout(30)
    result = driver.execute_async_script(js)
    return json.loads(result)


# ── Progress ────────────────────────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {"completed": {}, "errors": []}


def save_progress(progress):
    PROGRESS_FILE.write_text(json.dumps(progress))


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    # Load products
    products = json.loads(PRODUCTS_FILE.read_text())
    log.info(f"Total products: {len(products)}")

    # Build SKU list — use the SKU field (what the API expects)
    # Some products use their key as the lookup, others use the sku field
    sku_map = {}  # api_lookup → product_key
    for key, prod in products.items():
        sku = prod.get("sku", key)
        # The API uses product_code OR name for lookup
        # Try the key first (often the part number), then the SKU
        sku_map[key] = key
        if sku != key:
            sku_map[sku] = key  # Also try by SKU

    # Load progress
    progress = load_progress()
    already_done = set(progress["completed"].keys())

    # Build work list — try each product key as the API lookup
    work = []
    for key in products:
        if key not in already_done:
            work.append(key)

    if args.limit:
        work = work[:args.limit]

    log.info(f"Already done: {len(already_done)}")
    log.info(f"Remaining: {len(work)}")

    if not work:
        log.info("Nothing to do!")
        return

    # Launch browser
    driver = create_driver()
    login(driver)

    # Verify login worked
    test_result = api_batch(driver, [work[0]])
    test_data = test_result.get(work[0])
    if isinstance(test_data, list) and test_data and test_data[0].get("name") == "Please Log In":
        log.error("NOT LOGGED IN! Aborting.")
        driver.quit()
        return
    log.info("Login verified — API responding")

    # Process in batches
    batch_num = 0
    for i in range(0, len(work), BATCH_SIZE):
        batch = work[i:i + BATCH_SIZE]
        try:
            results = api_batch(driver, batch)
        except Exception as e:
            log.error(f"Batch error: {e}")
            time.sleep(5)
            continue

        for sku in batch:
            data = results.get(sku)
            if data and not isinstance(data, list) and not data.get("error_code"):
                progress["completed"][sku] = data
            elif data and isinstance(data, dict) and data.get("error_code") == "product_not_found":
                # Try with the product's SKU field instead
                prod = products.get(sku, {})
                alt_sku = prod.get("sku", sku)
                if alt_sku != sku and alt_sku not in already_done:
                    try:
                        alt_results = api_batch(driver, [alt_sku])
                        alt_data = alt_results.get(alt_sku)
                        if alt_data and not isinstance(alt_data, list) and not alt_data.get("error_code"):
                            progress["completed"][sku] = alt_data
                            continue
                    except:
                        pass
                progress["errors"].append(sku)
            else:
                progress["errors"].append(sku)

        batch_num += 1
        done = len(progress["completed"])
        total = len(work) + len(already_done)

        if batch_num % 20 == 0:
            save_progress(progress)
            log.info(f"Progress: {done}/{total} ({100 * done // max(total, 1)}%) | Errors: {len(progress['errors'])}")

        time.sleep(random.uniform(0.3, 0.6))

    # Final save
    save_progress(progress)

    # Write cleaned output
    log.info(f"\nDone! Scraped: {len(progress['completed'])} | Errors: {len(progress['errors'])}")

    # Save as full_api_data.json for easy access
    OUTPUT_FILE.write_text(json.dumps(progress["completed"], indent=2))
    log.info(f"Full API data saved to {OUTPUT_FILE}")

    driver.quit()


if __name__ == "__main__":
    main()
