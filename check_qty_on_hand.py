#!/usr/bin/env python3
"""
One-time check: query Steel City API for all products, report in_stock vs qty_on_hand breakdown.
Resumable via qty_check_progress.json.
"""

import json
import logging
import os
import random
import time
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "product_names.json"
PROGRESS_FILE = BASE_DIR / "qty_check_progress.json"
BATCH_SIZE = 8

CONFIG = {
    "base_url": "https://www.steelcityvac.com",
    "account": os.environ.get("SC_ACCOUNT", ""),
    "user_id": os.environ.get("SC_USER", ""),
    "password": os.environ.get("SC_PASSWORD", ""),
}

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")


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

    # Navigate to base URL so API calls work
    driver.get(CONFIG["base_url"])
    time.sleep(3)


def api_batch(driver, skus):
    """Call product_info API for multiple SKUs concurrently."""
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
        """, skus)
        if result:
            return json.loads(result)
    except Exception as e:
        log.debug(f"Batch API failed: {e}")
    return {s: None for s in skus}


def main():
    with open(PRODUCTS_FILE) as f:
        products = json.load(f)

    all_skus = [(key, p.get("sku", key), p) for key, p in products.items()]
    print(f"Total products: {len(all_skus)}")

    # Load progress for resume
    checked = {}
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            checked = json.load(f)
        print(f"Resuming — already checked: {len(checked)}")

    remaining = [(k, s, p) for k, s, p in all_skus if s not in checked]
    print(f"Remaining to check: {len(remaining)}")

    if remaining:
        driver = create_driver()
        try:
            login(driver)

            for i in range(0, len(remaining), BATCH_SIZE):
                batch = remaining[i:i + BATCH_SIZE]
                batch_skus = [s for _, s, _ in batch]

                results = api_batch(driver, batch_skus)

                for key, sku, product in batch:
                    api_data = results.get(sku)
                    if not api_data or not api_data.get("name"):
                        checked[sku] = {"in_stock": None, "qty_on_hand": None, "error": True}
                    else:
                        checked[sku] = {
                            "in_stock": api_data.get("in_stock", "?"),
                            "qty_on_hand": api_data.get("qty_on_hand", "?"),
                        }

                done = len(checked)
                total = len(all_skus)
                if done % 50 < BATCH_SIZE:
                    print(f"  Progress: {done}/{total} ({100*done/total:.1f}%)")
                    with open(PROGRESS_FILE, "w") as f:
                        json.dump(checked, f)

                time.sleep(random.uniform(0.3, 0.8))

            # Final save
            with open(PROGRESS_FILE, "w") as f:
                json.dump(checked, f)

        finally:
            driver.quit()

    # ── Report ──
    print("\n" + "=" * 60)
    print("RESULTS: in_stock vs qty_on_hand")
    print("=" * 60)

    categories = {
        "in_stock=1, qty>0": [],
        "in_stock=1, qty=0": [],
        "in_stock=0": [],
        "error": [],
    }

    for sku, data in checked.items():
        if data.get("error"):
            categories["error"].append(sku)
        elif data["in_stock"] == "0":
            categories["in_stock=0"].append(sku)
        elif data["in_stock"] == "1":
            qty = int(data.get("qty_on_hand", "0") or "0")
            if qty > 0:
                categories["in_stock=1, qty>0"].append(sku)
            else:
                categories["in_stock=1, qty=0"].append(sku)

    for cat, skus in categories.items():
        print(f"\n{cat}: {len(skus)}")

    # The key number — these are the ones that would get drafted
    problem = categories["in_stock=1, qty=0"]
    print(f"\n{'=' * 60}")
    print(f"PRODUCTS THAT WOULD BE NEWLY DRAFTED: {len(problem)}")
    print(f"(in_stock=1 but qty_on_hand=0 — appear available but aren't)")
    print(f"{'=' * 60}")

    if problem:
        for sku in problem[:30]:
            for key, p in products.items():
                if p.get("sku", key) == sku:
                    print(f"  {sku} — {p.get('clean_name', '?')[:60]}")
                    break
        if len(problem) > 30:
            print(f"  ... and {len(problem) - 30} more")


if __name__ == "__main__":
    main()
