#!/usr/bin/env python3
"""
check_stock.py — Check stock status for all products via Steel City API.
Removes special order and NLA (without alternatives) products from product_names.json.

Usage:
  python3 check_stock.py            # Full run (resumable)
  python3 check_stock.py --report   # Just report from existing progress (no browser)
"""

import argparse
import json
import logging
import random
import time
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG = {
    "base_url": "https://www.steelcityvac.com",
    "account": "REDACTED_ACCT",
    "user_id": "REDACTED_USER",
    "password": "REDACTED_PASS",
}

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "product_names.json"
PROGRESS_FILE = BASE_DIR / "stock_check_progress.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Browser helpers (same as catalog_scraper.py) ────────────────────────────

def create_driver():
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument(f"--user-data-dir={BASE_DIR / 'browser_data'}")
    return uc.Chrome(options=options, headless=False, version_main=145)


def wait_for_cloudflare(driver, max_wait=120):
    if "just a moment" not in driver.title.lower():
        return
    log.info("Cloudflare challenge detected — waiting...")
    for i in range(max_wait // 2):
        time.sleep(2)
        if "just a moment" not in driver.title.lower():
            log.info(f"Cloudflare resolved after ~{(i+1)*2}s")
            time.sleep(2)
            return
        if i % 15 == 14:
            log.info(f"Still waiting on Cloudflare... ({(i+1)*2}s)")
    raise TimeoutError("Cloudflare challenge not resolved.")


def login(driver):
    log.info("Navigating to Steel City Vacuum...")
    driver.get(CONFIG["base_url"])
    time.sleep(3)
    wait_for_cloudflare(driver)

    try:
        nav = driver.find_element(By.ID, "main-nav")
        if "notlogged" not in (nav.get_attribute("class") or "").lower():
            log.info("Already logged in!")
            return
    except Exception:
        pass

    log.info("Logging in...")
    try:
        driver.find_element(By.CSS_SELECTOR, "a.nav-login-btn").click()
        time.sleep(1)
    except Exception:
        pass

    wait = WebDriverWait(driver, 10)
    customer_field = wait.until(EC.visibility_of_element_located((By.ID, "scvCustomerNumber")))
    for field_id, value in [
        ("scvCustomerNumber", CONFIG["account"]),
        ("userNameBox", CONFIG["user_id"]),
        ("password", CONFIG["password"]),
    ]:
        el = driver.find_element(By.ID, field_id) if field_id != "scvCustomerNumber" else customer_field
        el.clear()
        for ch in value:
            el.send_keys(ch)
            time.sleep(random.uniform(0.02, 0.08))
        time.sleep(random.uniform(0.3, 0.6))

    try:
        driver.find_element(By.CSS_SELECTOR, "a.login-btn").click()
    except Exception:
        driver.execute_script("submitLoginForm()")

    time.sleep(5)
    wait_for_cloudflare(driver)
    log.info("Login complete.")


BATCH_SIZE = 8  # Concurrent API calls per batch


def api_product_info_batch(driver, part_ids):
    """Call product_info API for multiple parts concurrently. Returns dict of {part_id: data}."""
    try:
        result = driver.execute_script("""
            var ids = arguments[0];
            var promises = ids.map(function(pid) {
                return new Promise(function(resolve) {
                    $.ajax({
                        type: 'POST',
                        url: 'applications/shopping_cart/web_services.php?action=product_info&name=' + pid,
                        success: function(data) {
                            if (typeof data === 'string') {
                                try { data = JSON.parse(data); } catch(e) {}
                            }
                            resolve({id: pid, data: data});
                        },
                        error: function() { resolve({id: pid, data: null}); }
                    });
                });
            });
            return Promise.all(promises).then(function(results) {
                var out = {};
                results.forEach(function(r) { out[r.id] = r.data; });
                return JSON.stringify(out);
            });
        """, part_ids)
        if result:
            return json.loads(result)
    except Exception as e:
        log.debug(f"Batch API failed for {len(part_ids)} parts: {e}")
    return {pid: None for pid in part_ids}


# ── Progress helpers ────────────────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"checked": {}, "errors": []}


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


# ── Main logic ──────────────────────────────────────────────────────────────

def classify_product(data):
    """Classify a product based on API response."""
    if not data or not data.get("name"):
        return {"in_stock": "UNKNOWN", "status": "no_data"}

    in_stock = data.get("in_stock", "")
    description = data.get("description", "")
    name = data.get("name", "")

    is_special_order = False
    for field in [description, name]:
        if field and "SPECIAL ORDER" in field.upper():
            is_special_order = True
            break

    is_nla = False
    for field in [description, name]:
        if field and ("NLA" in field.upper() or "NO LONGER AVAILABLE" in field.upper()):
            is_nla = True
            break

    alt_items = data.get("alt_items", [])
    has_alt = bool(alt_items and isinstance(alt_items, list) and len(alt_items) > 0)

    status = "ok"
    if is_special_order:
        status = "special_order"
    elif is_nla and not has_alt:
        status = "nla_no_alt"
    elif is_nla and has_alt:
        status = "nla_has_alt"

    return {
        "in_stock": in_stock,
        "status": status,
        "special_order": is_special_order,
        "nla": is_nla,
        "has_alt": has_alt,
    }


def check_all(driver):
    with open(PRODUCTS_FILE) as f:
        products = json.load(f)

    progress = load_progress()
    checked = progress["checked"]

    skus = list(products.keys())
    remaining = [s for s in skus if s not in checked]
    log.info(f"Total products: {len(skus)}")
    log.info(f"Already checked: {len(checked)}")
    log.info(f"Remaining: {len(remaining)}")

    # Build lookup: key -> sku to query
    key_to_query = {}
    for key in remaining:
        product = products[key]
        key_to_query[key] = product.get("sku", key)

    # Process in batches
    batch_num = 0
    for i in range(0, len(remaining), BATCH_SIZE):
        batch_keys = remaining[i:i + BATCH_SIZE]
        batch_queries = [key_to_query[k] for k in batch_keys]

        results = api_product_info_batch(driver, batch_queries)

        # Process results, retry with key if SKU didn't work
        retry_keys = []
        for key, query in zip(batch_keys, batch_queries):
            data = results.get(query)
            if (not data or not data.get("name")) and query != key:
                retry_keys.append(key)
            else:
                checked[key] = classify_product(data)
                if checked[key]["status"] == "no_data":
                    progress["errors"].append(key)

        # Retry failed ones with the raw key
        if retry_keys:
            retry_results = api_product_info_batch(driver, retry_keys)
            for key in retry_keys:
                data = retry_results.get(key)
                checked[key] = classify_product(data)
                if checked[key]["status"] == "no_data":
                    progress["errors"].append(key)

        batch_num += 1

        # Checkpoint every 10 batches (~80 products)
        if batch_num % 10 == 0:
            save_progress(progress)
            done = len(checked)
            total = len(skus)
            pct = 100 * done / total
            statuses = {}
            for v in checked.values():
                s = v.get("status", "ok")
                statuses[s] = statuses.get(s, 0) + 1
            log.info(f"Progress: {done}/{total} ({pct:.1f}%) | "
                     f"ok={statuses.get('ok',0)} special_order={statuses.get('special_order',0)} "
                     f"nla={statuses.get('nla_no_alt',0)} errors={len(progress['errors'])}")

        # Small delay between batches to be respectful
        time.sleep(random.uniform(0.3, 0.8))

    save_progress(progress)
    log.info(f"Stock check complete. Checked {len(checked)}/{len(skus)} products.")


def generate_report():
    """Report on stock check results and filter product_names.json."""
    progress = load_progress()
    checked = progress["checked"]

    with open(PRODUCTS_FILE) as f:
        products = json.load(f)

    # Tally
    statuses = {}
    for v in checked.values():
        s = v.get("status", "ok")
        statuses[s] = statuses.get(s, 0) + 1

    log.info(f"\n{'='*50}")
    log.info(f"STOCK CHECK REPORT")
    log.info(f"{'='*50}")
    log.info(f"Total checked: {len(checked)}")
    for s, count in sorted(statuses.items(), key=lambda x: -x[1]):
        log.info(f"  {s:20s}: {count}")
    log.info(f"  errors (no data)   : {len(progress.get('errors', []))}")

    # Remove special order and NLA (no alt) from product_names.json
    to_remove = []
    for key, info in checked.items():
        status = info.get("status", "ok")
        if status in ("special_order", "nla_no_alt"):
            to_remove.append(key)

    log.info(f"\nProducts to remove: {len(to_remove)}")
    log.info(f"  Special order: {statuses.get('special_order', 0)}")
    log.info(f"  NLA (no alt):  {statuses.get('nla_no_alt', 0)}")

    # Also add in_stock field to remaining products
    before = len(products)
    removed_list = []
    for key in to_remove:
        if key in products:
            removed_list.append({
                "key": key,
                "sku": products[key].get("sku", key),
                "clean_name": products[key].get("clean_name", ""),
                "status": checked[key].get("status", ""),
            })
            del products[key]

    # Add in_stock to remaining
    for key, product in products.items():
        if key in checked:
            product["in_stock"] = checked[key].get("in_stock", "")

    after = len(products)
    log.info(f"\nProducts before: {before}")
    log.info(f"Products after:  {after}")
    log.info(f"Removed:         {before - after}")

    # Save updated products
    with open(PRODUCTS_FILE, "w") as f:
        json.dump(products, f, indent=2)
    log.info(f"Updated {PRODUCTS_FILE}")

    # Save removed list for reference
    removed_file = BASE_DIR / "output" / "removed_products.json"
    with open(removed_file, "w") as f:
        json.dump(removed_list, f, indent=2)
    log.info(f"Removed products saved to {removed_file}")

    # Re-export spreadsheet
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Products"
        headers = ["SKU", "Brand", "Model", "Clean Name", "Description", "Price", "In Stock", "Raw Name"]
        ws.append(headers)
        for key, p in products.items():
            ws.append([
                p.get("sku", key),
                p.get("brand", ""),
                p.get("model", ""),
                p.get("clean_name", ""),
                p.get("description", ""),
                p.get("price", ""),
                p.get("in_stock", ""),
                p.get("raw_name", ""),
            ])
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)
        out_path = BASE_DIR / "output" / "product_descriptions.xlsx"
        wb.save(str(out_path))
        log.info(f"Spreadsheet re-exported to {out_path}")
    except ImportError:
        log.warning("openpyxl not available — skipping spreadsheet export")


def main():
    parser = argparse.ArgumentParser(description="Check stock status for all products")
    parser.add_argument("--report", action="store_true",
                        help="Generate report and filter products (no browser needed)")
    args = parser.parse_args()

    if args.report:
        generate_report()
        return

    driver = None
    try:
        driver = create_driver()
        login(driver)
        check_all(driver)
        generate_report()
    except KeyboardInterrupt:
        log.info("\nInterrupted — progress saved. Re-run to resume.")
    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
