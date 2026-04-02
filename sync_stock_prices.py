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
MISSING_IMPORT_PROGRESS_FILE = BASE_DIR / "missing_import_progress.json"
PRICE_LOCKS_FILE = BASE_DIR / "price_locks.json"
COMPETITOR_PRICES_FILE = BASE_DIR / "competitor_prices.json"

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


# ── Competitive pricing ────────────────────────────────────────────────────

SHOPIFY_FEE_RATE = 0.029
SHOPIFY_FEE_FIXED = 0.30

UNDERCUT_TIERS = [
    (20,   0.50),
    (50,   1.00),
    (100,  2.00),
    (300,  5.00),
    (float("inf"), 10.00),
]


def calculate_break_even(cost):
    """Break-even = dealer cost + Shopify fees (2.9% + $0.30)."""
    return (cost + SHOPIFY_FEE_FIXED) / (1 - SHOPIFY_FEE_RATE)


def get_undercut(competitor_avg):
    for max_price, undercut in UNDERCUT_TIERS:
        if competitor_avg < max_price:
            return undercut
    return UNDERCUT_TIERS[-1][1]


def get_best_price(sku, dealer_cost, competitor_prices):
    """Return best price: beat lowest competitor by $1 if profitable, else avg undercut, else markup."""
    markup_price = calculate_retail_price(dealer_cost)

    comp_data = competitor_prices.get(sku)
    if not comp_data or comp_data.get("num_competitors", 0) == 0:
        return markup_price, "markup"

    # Filter out mismatched competitor entries (accessories, wrong models)
    # Only keep prices within 0.5x-5x of our dealer cost, and where
    # our SKU actually appears in the matched product's title or URL path
    sku_lower = sku.lower()
    sku_norm = re.sub(r'[\-\s\.]', '', sku).lower()
    valid_prices = []
    for domain, cdata in comp_data.get("competitors", {}).items():
        if not cdata or not cdata.get("price"):
            continue
        # Price ratio check
        ratio = cdata["price"] / dealer_cost if dealer_cost > 0 else 1
        if not (0.50 <= ratio <= 5.0):
            continue
        # SKU presence check (title + URL path, not query string)
        comp_title = (cdata.get("title") or "").lower()
        comp_url = (cdata.get("url") or "").split("?")[0].lower()
        comp_text = comp_title + " " + comp_url
        comp_text_norm = re.sub(r'[\-\s\.]', '', comp_text)
        if sku_lower not in comp_text and sku_norm not in comp_text_norm:
            continue
        valid_prices.append(cdata["price"])

    if not valid_prices:
        return markup_price, "markup"

    break_even = calculate_break_even(dealer_cost)
    competitor_min = min(valid_prices)
    competitor_avg = sum(valid_prices) / len(valid_prices)

    # Try to beat lowest competitor by $1
    target_min = charm_price(competitor_min - 1.00)
    target_min = max(target_min, MIN_PRICE)
    if target_min >= break_even:
        return target_min, "competitor"

    # Can't beat lowest — try average with tiered undercut
    undercut = get_undercut(competitor_avg)
    target_avg = charm_price(competitor_avg - undercut)
    target_avg = max(target_avg, MIN_PRICE)
    if target_avg >= break_even:
        return target_avg, "competitor"

    return markup_price, "markup"


# ── Browser helpers (from check_stock.py) ───────────────────────────────────

def create_driver():
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    if os.environ.get("CI"):
        options.add_argument("--headless=new")
        options.add_argument("--disable-dev-shm-usage")
    return uc.Chrome(options=options, headless=bool(os.environ.get("CI")), version_main=130)


def login(driver):
    log.info("Navigating to Steel City login page...")
    driver.get(f"{CONFIG['base_url']}/a/s/")
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "scv_customer_number"))
        )
    except Exception:
        log.error(f"Login form not found. URL: {driver.current_url}")
        log.error(f"Page title: {driver.title}")
        log.error(f"Page source (first 2000 chars): {driver.page_source[:2000]}")
        raise

    driver.find_element(By.ID, "scv_customer_number").clear()
    driver.find_element(By.ID, "scv_customer_number").send_keys(CONFIG["account"])
    driver.find_element(By.ID, "username_login_box").clear()
    driver.find_element(By.ID, "username_login_box").send_keys(CONFIG["user_id"])
    driver.find_element(By.ID, "password_login").clear()
    driver.find_element(By.ID, "password_login").send_keys(CONFIG["password"])
    driver.find_element(By.NAME, "loginSubmit").click()
    time.sleep(5)
    log.info(f"Logged in. URL: {driver.current_url}")

    # Navigate to base URL so API calls work
    driver.get(CONFIG["base_url"])
    time.sleep(3)


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


def scrape_real_stock_batch(driver, skus):
    """Fetch product pages and extract real qty from server-rendered var qtyoh."""
    try:
        result = driver.execute_async_script("""
            var skus = arguments[0];
            var callback = arguments[arguments.length - 1];
            var results = {};
            var done = 0;
            skus.forEach(function(sku) {
                $.ajax({
                    type: 'GET',
                    url: 'a/s/p/' + encodeURIComponent(sku),
                    success: function(html) {
                        var match = html.match(/var\\s+qtyoh\\s*=\\s*'([^']*)'/);
                        results[sku] = match ? match[1] : null;
                        done++;
                        if (done === skus.length) callback(JSON.stringify(results));
                    },
                    error: function() {
                        results[sku] = null;
                        done++;
                        if (done === skus.length) callback(JSON.stringify(results));
                    }
                });
            });
        """, skus)
        if result:
            return json.loads(result)
    except Exception as e:
        log.debug(f"Batch page scrape failed for {len(skus)} SKUs: {e}")
    return {sku: None for sku in skus}


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

    # Load competitor prices for competitive pricing
    competitor_prices = {}
    if COMPETITOR_PRICES_FILE.exists():
        with open(COMPETITOR_PRICES_FILE) as f:
            comp_data = json.load(f)
        competitor_prices = comp_data.get("prices", {})
        log.info(f"Competitor price data: {len(competitor_prices)} SKUs")

    # Load bulk import progress to identify OUR products (avoid touching pre-existing ones)
    our_product_ids = set()
    if BULK_IMPORT_PROGRESS_FILE.exists():
        with open(BULK_IMPORT_PROGRESS_FILE) as f:
            bulk_progress = json.load(f)
        our_product_ids = {v["id"] for v in bulk_progress.values() if v.get("status") == "created"}
        log.info(f"Products we imported (from bulk_import_progress): {len(our_product_ids)}")

    # Also include missing_import_progress.json (belt and suspenders)
    if MISSING_IMPORT_PROGRESS_FILE.exists():
        with open(MISSING_IMPORT_PROGRESS_FILE) as f:
            missing_progress = json.load(f)
        before = len(our_product_ids)
        for v in missing_progress.get("uploaded", {}).values():
            if v.get("status") == "created" and v.get("id"):
                our_product_ids.add(v["id"])
        added = len(our_product_ids) - before
        if added:
            log.info(f"  + {added} from missing_import_progress → {len(our_product_ids)} total")

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

        # Fetch API data (for prices) and real stock (from product pages) in sequence
        results = api_product_info_batch(driver, batch_skus)
        stock_results = scrape_real_stock_batch(driver, batch_skus)

        for key, sku, product in batch:
            api_data = results.get(sku)
            shop_info = shopify_map[sku]
            product_id = shop_info["product_id"]
            variant_id = shop_info["variant_id"]

            old_qty = product.get("qty_on_hand")
            old_cost = parse_price(product.get("price"))

            # Get new values from API
            if not api_data or not api_data.get("name"):
                changes["errors"].append({"sku": sku, "error": "no_api_data"})
                progress["checked_skus"].append(sku)
                continue

            # ── NLA detection: draft products marked No Longer Available ──
            api_name = (api_data.get("name") or "").upper()
            api_desc = (api_data.get("description") or "").upper()
            is_nla = "NLA" in api_name or "NLA" in api_desc or \
                     "NO LONGER AVAILABLE" in api_name or "NO LONGER AVAILABLE" in api_desc
            if is_nla:
                log.info(f"  DRAFT (NLA): {sku} ({product.get('clean_name', '')[:40]})")
                if not dry_run:
                    set_product_status(product_id, "draft")
                    time.sleep(0.55)
                changes["drafted"].append(sku)
                products[key]["in_stock"] = "0"
                progress["checked_skus"].append(sku)
                save_sync_progress(progress)
                continue

            new_cost_raw = api_data.get("price", "")
            new_cost = parse_price(new_cost_raw)

            # Real stock from product page (qtyoh)
            qtyoh_raw = stock_results.get(sku)
            if qtyoh_raw is None:
                # Page scrape failed — skip stock check for this SKU
                changes["errors"].append({"sku": sku, "error": "no_stock_data"})
                progress["checked_skus"].append(sku)
                continue
            new_qty = int(qtyoh_raw)

            # ── Stock status changes (based on real qty from product page) ──
            was_in_stock = old_qty is None or old_qty > 0  # assume in-stock if no prior data
            now_in_stock = new_qty > 0

            if was_in_stock and not now_in_stock:
                # Was available → now out of stock: draft the product
                log.info(f"  DRAFT: {sku} (qty=0) ({product.get('clean_name', '')[:40]})")
                if not dry_run:
                    ok = set_product_status(product_id, "draft")
                    if not ok:
                        changes["errors"].append({"sku": sku, "error": "draft_failed"})
                    time.sleep(0.55)
                changes["drafted"].append(sku)

            elif not was_in_stock and now_in_stock:
                # Was out → now back in stock: reactivate
                log.info(f"  ACTIVATE: {sku} (qty={new_qty}) ({product.get('clean_name', '')[:40]})")
                if not dry_run:
                    ok = set_product_status(product_id, "active")
                    if not ok:
                        changes["errors"].append({"sku": sku, "error": "activate_failed"})
                    time.sleep(0.55)
                changes["activated"].append(sku)

            # Always store real qty for next run's comparison
            products[key]["qty_on_hand"] = new_qty

            # ── Price changes (competitor-aware, skip MAP-locked SKUs) ──
            if new_cost is not None and sku not in price_locks:
                # Use competitor-based price if available, else tiered markup
                new_retail, price_method = get_best_price(sku, new_cost, competitor_prices)
                old_retail_str = product.get("retail_price", "")
                old_retail = parse_price(old_retail_str) or 0

                # Update cost in product_names if it changed
                cost_changed = old_cost is not None and abs(new_cost - old_cost) > 0.005

                # Update price if retail changed or cost changed
                price_changed = abs(new_retail - old_retail) >= 0.01

                if cost_changed or price_changed:
                    direction = "UP" if new_retail > old_retail else "DOWN" if new_retail < old_retail else "COST"
                    log.info(f"  PRICE {direction}: {sku} cost ${old_cost or 0:.2f}→${new_cost:.2f}, "
                             f"retail ${old_retail:.2f}→${new_retail:.2f} ({price_method})")

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
                        "method": price_method,
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
