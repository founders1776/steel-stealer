#!/usr/bin/env python3
"""
Sync stock status and pricing from Steel City → Shopify.

Polls Steel City API for stock/price changes and updates Shopify:
  - Drafts products when out of stock → reactivates when back in stock
  - Updates pricing only on cost INCREASES (recalculates via tiered markup)
  - Logs all changes to sync_log.json
  - Outputs email summary for GitHub Actions

Usage:
  python3 sync_stock_prices.py            # Full sync
  python3 sync_stock_prices.py --dry-run  # Preview changes without touching Shopify
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
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
SHOPIFY_MAP_FILE = BASE_DIR / "shopify_product_map.json"
SYNC_LOG_FILE = BASE_DIR / "sync_log.json"
SYNC_PROGRESS_FILE = BASE_DIR / "sync_progress.json"
BULK_IMPORT_PROGRESS_FILE = BASE_DIR / "bulk_import_progress.json"
PRICE_LOCKS_FILE = BASE_DIR / "price_locks.json"

SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = "2024-10"

BATCH_SIZE = 8  # Concurrent Steel City API calls per batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Pricing (from generate_pricing.py) ──────────────────────────────────────

MARKUP_TIERS = [
    (1.00,    8.0),
    (3.00,    4.5),
    (7.00,    3.2),
    (15.00,   2.5),
    (30.00,   2.2),
    (60.00,   1.9),
    (120.00,  1.7),
    (300.00,  1.5),
    (float("inf"), 1.4),
]

MIN_PRICE = 6.99


def get_markup(cost):
    for max_cost, multiplier in MARKUP_TIERS:
        if cost <= max_cost:
            return multiplier
    return MARKUP_TIERS[-1][1]


def charm_price(raw_price):
    dollar = int(raw_price)
    cents = raw_price - dollar
    if cents < 0.50:
        return float(dollar - 1) + 0.99 if dollar > 0 else 0.99
    else:
        return float(dollar) + 0.99


def calculate_retail_price(cost):
    markup = get_markup(cost)
    raw = cost * markup
    retail = charm_price(raw)
    return max(retail, MIN_PRICE)


def parse_price(price_str):
    if not price_str:
        return None
    match = re.search(r'[\d]+\.?\d*', str(price_str))
    return float(match.group()) if match else None


# ── Browser helpers (from check_stock.py) ───────────────────────────────────

def create_driver():
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    # Use temp profile in CI (no persistent browser_data)
    if os.environ.get("CI"):
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
    else:
        options.add_argument(f"--user-data-dir={BASE_DIR / 'browser_data'}")
    return uc.Chrome(options=options, headless=bool(os.environ.get("CI")), version_main=145)


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


def api_product_info_batch(driver, part_ids):
    """Call product_info API for multiple parts concurrently."""
    try:
        result = driver.execute_script("""
            var ids = arguments[0];
            var promises = ids.map(function(pid) {
                return new Promise(function(resolve) {
                    $.ajax({
                        type: 'POST',
                        url: 'applications/shopping_cart/web_services.php?action=product_info&name=' + encodeURIComponent(pid),
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


# ── Shopify API helpers ─────────────────────────────────────────────────────

def shopify_api_url(path):
    return f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/{path}"


def shopify_headers():
    return {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


def shopify_put(path, payload, retries=3):
    """PUT to Shopify REST API with retry."""
    for attempt in range(retries):
        try:
            resp = requests.put(
                shopify_api_url(path), json=payload,
                headers=shopify_headers(), timeout=30,
            )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2))
                time.sleep(retry_after)
                continue
            if resp.status_code in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep((attempt + 1) * 5)
                continue
            resp.raise_for_status()
            return resp.json(), resp.status_code
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                time.sleep((attempt + 1) * 5)
            else:
                raise
    return None, None


def set_product_status(product_id, status):
    """Set Shopify product status to 'active' or 'draft'."""
    payload = {"product": {"id": int(product_id), "status": status}}
    data, code = shopify_put(f"products/{product_id}.json", payload)
    return data is not None


def update_variant_price(variant_id, retail_price, cost):
    """Update variant price and cost on Shopify."""
    payload = {"variant": {
        "id": int(variant_id),
        "price": f"{retail_price:.2f}",
        "cost": f"{cost:.2f}",
    }}
    data, code = shopify_put(f"variants/{variant_id}.json", payload)
    return data is not None


# ── Progress helpers ────────────────────────────────────────────────────────

def load_sync_progress():
    if SYNC_PROGRESS_FILE.exists():
        with open(SYNC_PROGRESS_FILE) as f:
            return json.load(f)
    return {"checked_skus": [], "changes": {
        "drafted": [], "activated": [], "price_updated": [], "errors": [],
    }}


def save_sync_progress(progress):
    with open(SYNC_PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


# ── Main sync logic ────────────────────────────────────────────────────────

def run_sync(driver, dry_run=False):
    # Load data
    with open(PRODUCTS_FILE) as f:
        products = json.load(f)

    if not SHOPIFY_MAP_FILE.exists():
        log.error(f"{SHOPIFY_MAP_FILE} not found. Run build_shopify_map.py first.")
        sys.exit(1)

    with open(SHOPIFY_MAP_FILE) as f:
        shopify_map = json.load(f)

    # Load price locks (MAP pricing — never auto-update these prices)
    price_locks = set()
    if PRICE_LOCKS_FILE.exists():
        with open(PRICE_LOCKS_FILE) as f:
            locks_data = json.load(f)
        price_locks = {k for k in locks_data.keys() if not k.startswith("_")}
        if price_locks:
            log.info(f"Price-locked SKUs (MAP): {len(price_locks)}")

    # Load bulk import progress to identify OUR products (avoid touching pre-existing ones)
    our_product_ids = set()
    if BULK_IMPORT_PROGRESS_FILE.exists():
        with open(BULK_IMPORT_PROGRESS_FILE) as f:
            bulk_progress = json.load(f)
        our_product_ids = {v["id"] for v in bulk_progress.values() if v.get("status") == "created"}
        log.info(f"Products we imported (from bulk_import_progress): {len(our_product_ids)}")

    # Only sync SKUs that are in both product_names and shopify_map,
    # AND whose Shopify product_id is one we imported (not pre-existing)
    sync_skus = []
    skipped_preexisting = 0
    for key, product in products.items():
        sku = product.get("sku", key)
        if sku in shopify_map:
            product_id = shopify_map[sku]["product_id"]
            if our_product_ids and product_id not in our_product_ids:
                skipped_preexisting += 1
                continue
            sync_skus.append((key, sku, product))

    log.info(f"Products in Shopify map: {len(shopify_map)}")
    log.info(f"Skipped (pre-existing, not our import): {skipped_preexisting}")
    log.info(f"Products to sync: {len(sync_skus)}")

    # Load progress for resume
    progress = load_sync_progress()
    already_checked = set(progress["checked_skus"])
    remaining = [(k, s, p) for k, s, p in sync_skus if s not in already_checked]
    log.info(f"Already checked: {len(already_checked)}, remaining: {len(remaining)}")

    changes = progress["changes"]

    # Process in batches
    for i in range(0, len(remaining), BATCH_SIZE):
        batch = remaining[i:i + BATCH_SIZE]
        batch_skus = [sku for _, sku, _ in batch]

        results = api_product_info_batch(driver, batch_skus)

        for key, sku, product in batch:
            api_data = results.get(sku)
            shop_info = shopify_map[sku]
            product_id = shop_info["product_id"]
            variant_id = shop_info["variant_id"]

            old_in_stock = product.get("in_stock", "1")
            old_cost = parse_price(product.get("price"))

            # Get new values from API
            if not api_data or not api_data.get("name"):
                changes["errors"].append({"sku": sku, "error": "no_api_data"})
                progress["checked_skus"].append(sku)
                continue

            new_in_stock = api_data.get("in_stock", "1")
            new_cost_raw = api_data.get("price", "")
            new_cost = parse_price(new_cost_raw)

            # ── Stock status changes ──
            if old_in_stock == "1" and new_in_stock == "0":
                # In stock → out of stock: draft the product
                log.info(f"  DRAFT: {sku} ({product.get('clean_name', '')[:40]})")
                if not dry_run:
                    ok = set_product_status(product_id, "draft")
                    if not ok:
                        changes["errors"].append({"sku": sku, "error": "draft_failed"})
                    time.sleep(0.55)
                changes["drafted"].append(sku)
                products[key]["in_stock"] = "0"

            elif old_in_stock == "0" and new_in_stock == "1":
                # Out of stock → back in stock: reactivate
                log.info(f"  ACTIVATE: {sku} ({product.get('clean_name', '')[:40]})")
                if not dry_run:
                    ok = set_product_status(product_id, "active")
                    if not ok:
                        changes["errors"].append({"sku": sku, "error": "activate_failed"})
                    time.sleep(0.55)
                changes["activated"].append(sku)
                products[key]["in_stock"] = "1"

            # ── Price changes (increase only, skip MAP-locked SKUs) ──
            if old_cost is not None and new_cost is not None and new_cost > old_cost and sku not in price_locks:
                new_retail = calculate_retail_price(new_cost)
                old_retail_str = product.get("retail_price", "")
                old_retail = parse_price(old_retail_str) or 0

                log.info(f"  PRICE UP: {sku} cost ${old_cost:.2f}→${new_cost:.2f}, "
                         f"retail ${old_retail:.2f}→${new_retail:.2f}")

                if not dry_run:
                    ok = update_variant_price(variant_id, new_retail, new_cost)
                    if not ok:
                        changes["errors"].append({"sku": sku, "error": "price_update_failed"})
                    time.sleep(0.55)

                changes["price_updated"].append({
                    "sku": sku,
                    "old_cost": product.get("price", ""),
                    "new_cost": f"${new_cost:.2f}",
                    "old_retail": old_retail_str,
                    "new_retail": f"${new_retail:.2f}",
                })
                products[key]["price"] = f"${new_cost:.2f}"
                products[key]["retail_price"] = f"${new_retail:.2f}"

            progress["checked_skus"].append(sku)

        # Checkpoint every 50 SKUs
        if len(progress["checked_skus"]) % 50 < BATCH_SIZE:
            save_sync_progress(progress)
            checked = len(progress["checked_skus"])
            total = len(sync_skus)
            log.info(f"Progress: {checked}/{total} ({100*checked/total:.1f}%) | "
                     f"drafted={len(changes['drafted'])} activated={len(changes['activated'])} "
                     f"price_up={len(changes['price_updated'])} errors={len(changes['errors'])}")

        time.sleep(random.uniform(0.3, 0.8))

    # Save final progress
    save_sync_progress(progress)

    # Update product_names.json
    if not dry_run:
        with open(PRODUCTS_FILE, "w") as f:
            json.dump(products, f, indent=2)
        log.info("Updated product_names.json")

    # Append to sync log
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "drafted": changes["drafted"],
        "activated": changes["activated"],
        "price_updated": changes["price_updated"],
        "errors": changes["errors"],
        "total_checked": len(progress["checked_skus"]),
    }

    sync_log = []
    if SYNC_LOG_FILE.exists():
        with open(SYNC_LOG_FILE) as f:
            sync_log = json.load(f)
    sync_log.append(log_entry)
    with open(SYNC_LOG_FILE, "w") as f:
        json.dump(sync_log, f, indent=2)

    # Clean up progress file on successful completion
    if SYNC_PROGRESS_FILE.exists():
        SYNC_PROGRESS_FILE.unlink()

    # Print summary
    summary = build_summary(log_entry)
    print(summary)

    # Output summary for GitHub Actions
    if os.environ.get("GITHUB_OUTPUT"):
        # Write multiline output
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"summary<<EOF\n{summary}\nEOF\n")

    return log_entry


def build_summary(log_entry):
    """Build human-readable email summary."""
    lines = [
        "=" * 50,
        "STEEL STEALER SYNC REPORT",
        f"Time: {log_entry['timestamp']}",
        f"{'DRY RUN — no changes made' if log_entry.get('dry_run') else 'LIVE RUN'}",
        "=" * 50,
        f"Total checked: {log_entry['total_checked']}",
        "",
    ]

    drafted = log_entry["drafted"]
    if drafted:
        lines.append(f"DRAFTED (out of stock): {len(drafted)}")
        for sku in drafted[:20]:
            lines.append(f"  - {sku}")
        if len(drafted) > 20:
            lines.append(f"  ... and {len(drafted) - 20} more")
        lines.append("")

    activated = log_entry["activated"]
    if activated:
        lines.append(f"REACTIVATED (back in stock): {len(activated)}")
        for sku in activated[:20]:
            lines.append(f"  - {sku}")
        if len(activated) > 20:
            lines.append(f"  ... and {len(activated) - 20} more")
        lines.append("")

    price_updated = log_entry["price_updated"]
    if price_updated:
        lines.append(f"PRICE INCREASED: {len(price_updated)}")
        for p in price_updated[:20]:
            lines.append(f"  - {p['sku']}: cost {p['old_cost']}→{p['new_cost']}, "
                        f"retail {p['old_retail']}→{p['new_retail']}")
        if len(price_updated) > 20:
            lines.append(f"  ... and {len(price_updated) - 20} more")
        lines.append("")

    errors = log_entry["errors"]
    if errors:
        lines.append(f"ERRORS: {len(errors)}")
        for e in errors[:10]:
            lines.append(f"  - {e.get('sku', '?')}: {e.get('error', '?')}")
        lines.append("")

    if not drafted and not activated and not price_updated:
        lines.append("No changes detected.")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Sync stock & prices from Steel City to Shopify")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without updating Shopify")
    args = parser.parse_args()

    if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
        if not args.dry_run:
            print("ERROR: Set SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN environment variables.")
            sys.exit(1)

    driver = None
    try:
        driver = create_driver()
        login(driver)
        run_sync(driver, dry_run=args.dry_run)
    except KeyboardInterrupt:
        log.info("\nInterrupted — progress saved. Re-run to resume.")
    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
