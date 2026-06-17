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
    "account": os.environ.get("SC_ACCOUNT", ""),
    "user_id": os.environ.get("SC_USER", ""),
    "password": os.environ.get("SC_PASSWORD", ""),
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
DUAL_SOURCE_FILE = BASE_DIR / "dual_source_skus.json"
DUAL_SOURCE_BRANDS_FILE = BASE_DIR / "dual_source_brands.json"
REPRICE_TARGETS_FILE = BASE_DIR / "reprice_targets.json"
DESCO_PRODUCTS_FILE = BASE_DIR / "desco_products.json"

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

MIN_PRICE = 6.99      # store display floor — nothing is ever listed below this
MIN_MARGIN = 0.20     # minimum gross margin: (price - cost) / price >= 20%


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


def calculate_break_even(cost):
    """Break-even = dealer cost + Shopify fees (2.9% + $0.30).

    Kept for run_reprice_targets (SEBO/dual-source), whose Steel City dealer
    cost is an inflated reseller estimate — a margin floor off it would be wrong.
    The general competitive pass uses margin_floor() instead.
    """
    return (cost + SHOPIFY_FEE_FIXED) / (1 - SHOPIFY_FEE_RATE)


def margin_floor(cost):
    """Lowest price holding the MIN_MARGIN gross margin.

    gross margin = (price - cost) / price >= MIN_MARGIN  ⟺  price >= cost / (1 - MIN_MARGIN)
    This is the gate on whether an undercut is allowed. The $6.99 store floor is
    applied separately as a final clamp — it is NOT a margin requirement.
    """
    return cost / (1 - MIN_MARGIN)


def filter_competitor_prices(sku, ref_price, comp_data):
    """Filter out mismatched competitor entries (accessories, wrong models).

    Only keep prices within 0.5x-5x of ref_price (our dealer cost, or current
    retail when no cost is known), and where our SKU actually appears in the
    matched product's title or URL path.
    """
    sku_lower = sku.lower()
    sku_norm = re.sub(r'[\-\s\.]', '', sku).lower()
    valid_prices = []
    for domain, cdata in comp_data.get("competitors", {}).items():
        if not cdata or not cdata.get("price"):
            continue
        # Price ratio check
        ratio = cdata["price"] / ref_price if ref_price and ref_price > 0 else 1
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
    return valid_prices


def competitive_target(valid_prices, floor):
    """Walk competitors low→high; undercut the cheapest one we can beat by $1
    while holding `floor`.

    For each competitor (lowest first) raw = charm(comp - $1). Return the first
    raw that clears `floor` — that is the cheapest price we can profitably offer.
    Competitors too cheap to beat at `floor` are skipped, not averaged in.
    Returns None if no competitor clears `floor`. The result is the pre-clamp
    undercut price; callers apply the $6.99 store floor separately.
    """
    for comp in sorted(valid_prices):
        raw = charm_price(comp - 1.00)
        if raw >= floor:
            return raw
    return None


def get_best_price(sku, dealer_cost, competitor_prices):
    """Return (price, method): undercut the cheapest beatable competitor by $1
    while holding the 20% margin floor; else tiered markup. The $6.99 store floor
    is a final clamp, applied after selection — never folded into the gate."""
    markup_price = calculate_retail_price(dealer_cost)

    comp_data = competitor_prices.get(sku)
    if not comp_data or comp_data.get("num_competitors", 0) == 0:
        return markup_price, "markup"

    valid_prices = filter_competitor_prices(sku, dealer_cost, comp_data)
    if not valid_prices:
        return markup_price, "markup"

    target = competitive_target(valid_prices, margin_floor(dealer_cost))
    if target is not None:
        return max(target, MIN_PRICE), "competitor"   # store display floor clamp

    return markup_price, "markup"


# Floor for reprice targets with no dealer cost: never drop below this
# fraction of ref_price (the store price when the SKU was first targeted).
REPRICE_NO_COST_FLOOR = 0.70


def run_reprice_targets(competitor_prices, price_locks, products, progress,
                        changes, dry_run=False, up_only=False):
    """Competitor-undercut pass for reprice_targets.json (dual-source brands
    like SEBO whose parts follow competitor pricing but are never stock-synced
    against Steel City).

    Strictly competitor-driven: a SKU with no validated competitor price is
    left untouched — the tiered-markup fallback NEVER applies here, because
    these prices were not derived from Steel City costs to begin with.
    Machines stay frozen via price_locks. No stock/status changes ever.
    """
    if not REPRICE_TARGETS_FILE.exists():
        log.info("No reprice_targets.json — skipping reprice pass")
        return

    with open(REPRICE_TARGETS_FILE) as f:
        targets = {k: v for k, v in json.load(f).items() if not k.startswith("_")}

    sku_to_key = {}
    for key, product in products.items():
        sku_to_key.setdefault(product.get("sku", key), key)

    already = set(progress.setdefault("repriced_skus", []))
    log.info(f"Reprice targets: {len(targets)} ({len(already)} already done this run)")

    stats = {"repriced": 0, "no_data": 0, "below_floor": 0, "unchanged": 0, "locked": 0}
    dirty = False
    for sku, target in targets.items():
        if sku in already:
            continue
        progress["repriced_skus"].append(sku)

        if sku in price_locks:
            stats["locked"] += 1
            continue

        comp_data = competitor_prices.get(sku)
        if not comp_data or comp_data.get("num_competitors", 0) == 0:
            stats["no_data"] += 1
            continue

        dealer_cost = target.get("dealer_cost")
        ref_price = target.get("ref_price")
        if dealer_cost:
            floor = calculate_break_even(dealer_cost)
        elif ref_price:
            floor = ref_price * REPRICE_NO_COST_FLOOR
        else:
            stats["no_data"] += 1
            continue

        valid_prices = filter_competitor_prices(sku, dealer_cost or ref_price, comp_data)
        if not valid_prices:
            stats["no_data"] += 1
            continue

        new_retail = competitive_target(valid_prices, floor)
        if new_retail is None:
            stats["below_floor"] += 1
            continue
        new_retail = max(new_retail, MIN_PRICE)   # store display floor clamp

        old_retail = target.get("last_applied") or ref_price or 0
        if abs(new_retail - old_retail) < 0.01:
            stats["unchanged"] += 1
            continue

        if up_only and old_retail and new_retail <= old_retail:
            stats["unchanged"] += 1
            continue

        direction = "UP" if new_retail > old_retail else "DOWN"
        log.info(f"  REPRICE {direction} (competitor, SKU redacted)")
        if not dry_run:
            # cost=None: never write cost-per-item on these products. Their
            # Shopify costs are the brand's direct dealer costs; the
            # dealer_cost here is Steel City's marked-up reseller cost and
            # is only valid as a price floor, not as the item cost.
            ok = update_variant_price(target["variant_id"], new_retail, None)
            if not ok:
                changes["errors"].append({"sku": sku, "error": "reprice_failed"})
                continue
            target["last_applied"] = new_retail
            dirty = True
            key = sku_to_key.get(sku)
            if key:
                products[key]["retail_price"] = f"${new_retail:.2f}"
            time.sleep(0.55)

        changes["price_updated"].append({
            "sku": sku,
            "old_cost": "untouched",
            "new_cost": "untouched",
            "old_retail": f"${old_retail:.2f}" if old_retail else "?",
            "new_retail": f"${new_retail:.2f}",
            "method": "competitor_reprice",
        })
        stats["repriced"] += 1

        if stats["repriced"] % 50 == 0:
            save_sync_progress(progress)

    if dirty:
        out = {"_comment": (
            "Competitor-undercut allowlist built by build_reprice_targets.py. "
            "sync_stock_prices.py reprices these SKUs from competitor_prices.json "
            "only (never markup fallback); no stock changes. ref_price anchors the "
            "price floor for SKUs without dealer cost.")}
        out.update(dict(sorted(targets.items())))
        with open(REPRICE_TARGETS_FILE, "w") as f:
            json.dump(out, f, indent=2)

    save_sync_progress(progress)
    log.info(f"Reprice pass: {stats['repriced']} repriced, {stats['unchanged']} unchanged, "
             f"{stats['no_data']} no competitor data, {stats['below_floor']} below floor, "
             f"{stats['locked']} MAP-locked")


def run_competitive_reprice(competitor_prices, price_locks, shopify_map, products,
                            progress, changes, dry_run=False, up_only=False):
    """General-catalog competitor-undercut pass (every part/accessory we sell).

    Candidate set = union of product_names.json + desco_products.json. A SKU is
    repriced when it has a Shopify variant, a dealer cost, validated competitor
    data, is not MAP-locked, and is not dual-source (those follow
    run_reprice_targets). Price-only (cost-per-item untouched), resumable, and
    competitor-driven: a SKU with no competitor data is skipped — the markup
    fallback fires only for SKUs that have data but cannot be undercut at the
    20% margin floor. Honors up_only (never lowers a known price).
    """
    dual_source_skus = set()
    if DUAL_SOURCE_FILE.exists():
        with open(DUAL_SOURCE_FILE) as f:
            dual_source_skus = set(json.load(f))
    dual_source_brands = set()
    if DUAL_SOURCE_BRANDS_FILE.exists():
        with open(DUAL_SOURCE_BRANDS_FILE) as f:
            dual_source_brands = {b.strip().upper() for b in json.load(f) if b and b.strip()}

    desco = {}
    if DESCO_PRODUCTS_FILE.exists():
        with open(DESCO_PRODUCTS_FILE) as f:
            desco = json.load(f)

    # Build candidates {sku: {cost, brand, key}} from both distributors.
    # product_names wins on overlap (its key lets us write retail back locally).
    candidates = {}
    for key, product in products.items():
        sku = product.get("sku", key)
        cost = parse_price(product.get("price"))
        if cost:
            candidates.setdefault(sku, {"cost": cost,
                                        "brand": product.get("brand") or "",
                                        "key": key})
    for sku, d in desco.items():
        if sku in candidates:
            continue
        cost = d.get("dealer_cost")
        if cost:
            candidates[sku] = {"cost": float(cost),
                               "brand": d.get("brand") or "",
                               "key": None}

    already = set(progress.setdefault("competitive_repriced_skus", []))
    log.info(f"Competitive reprice candidates: {len(candidates)} "
             f"({len(already)} already done this run)")

    stats = {"repriced": 0, "no_variant": 0, "no_data": 0, "below_floor": 0,
             "unchanged": 0, "locked": 0, "dual_source": 0}
    for sku, info in candidates.items():
        if sku in already:
            continue
        progress["competitive_repriced_skus"].append(sku)

        if sku in price_locks:
            stats["locked"] += 1
            continue
        map_entry = shopify_map.get(sku)
        if not map_entry or not map_entry.get("variant_id"):
            stats["no_variant"] += 1
            continue
        if sku in dual_source_skus:
            stats["dual_source"] += 1
            continue
        if dual_source_brands:
            vendor = (map_entry.get("vendor") or "").strip().upper()
            brand = (info["brand"] or "").strip().upper()
            if vendor in dual_source_brands or brand in dual_source_brands:
                stats["dual_source"] += 1
                continue

        comp_data = competitor_prices.get(sku)
        if not comp_data or comp_data.get("num_competitors", 0) == 0:
            stats["no_data"] += 1
            continue

        cost = info["cost"]
        valid_prices = filter_competitor_prices(sku, cost, comp_data)
        if not valid_prices:
            stats["no_data"] += 1
            continue

        target = competitive_target(valid_prices, margin_floor(cost))
        if target is None:
            new_retail = calculate_retail_price(cost)   # markup fallback (>= floor)
            method = "markup"
        else:
            new_retail = max(target, MIN_PRICE)          # store display floor clamp
            method = "competitor"

        # Authoritative old price: product_names if we own the record, else the
        # live Shopify variant (Desco-only SKUs have no local retail).
        variant_id = map_entry["variant_id"]
        if info["key"] is not None:
            old_retail = parse_price(products[info["key"]].get("retail_price")) or 0
        else:
            old_retail = 0
            try:
                data, _ = shopify_get(f"variants/{variant_id}.json")
                if data and data.get("variant"):
                    old_retail = parse_price(data["variant"].get("price")) or 0
            except Exception:
                old_retail = 0

        if old_retail and abs(new_retail - old_retail) < 0.01:
            stats["unchanged"] += 1
            continue
        if up_only and old_retail and new_retail <= old_retail:
            stats["unchanged"] += 1
            continue

        direction = "UP" if (not old_retail or new_retail > old_retail) else "DOWN"
        log.info(f"  CReprice {direction} ({method}, SKU redacted)")
        if not dry_run:
            ok = update_variant_price(variant_id, new_retail, None)
            if not ok:
                changes["errors"].append({"sku": sku, "error": "competitive_reprice_failed"})
                continue
            if info["key"] is not None:
                products[info["key"]]["retail_price"] = f"${new_retail:.2f}"
            time.sleep(0.55)

        changes["price_updated"].append({
            "sku": sku,
            "old_cost": "untouched",
            "new_cost": "untouched",
            "old_retail": f"${old_retail:.2f}" if old_retail else "?",
            "new_retail": f"${new_retail:.2f}",
            "method": f"competitive_{method}",
        })
        stats["repriced"] += 1
        if stats["repriced"] % 50 == 0:
            save_sync_progress(progress)

    save_sync_progress(progress)
    log.info(f"Competitive reprice: {stats['repriced']} repriced, "
             f"{stats['unchanged']} unchanged, {stats['no_data']} no competitor data, "
             f"{stats['no_variant']} no variant, {stats['locked']} MAP-locked, "
             f"{stats['dual_source']} dual-source")


# ── Browser helpers (from check_stock.py) ───────────────────────────────────

def _detect_chrome_major():
    """Detect installed Chrome major version to pin chromedriver."""
    import subprocess
    for cmd in ["google-chrome --version", "chromium --version",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --version"]:
        try:
            out = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL)
            ver = re.search(r"(\d+)\.", out)
            if ver:
                return int(ver.group(1))
        except Exception:
            continue
    return None


def _build_proxy_auth_extension(host, port, user, password):
    """Chrome can't take inline proxy credentials on --proxy-server, so for an
    authenticated (user:pass) residential proxy we load a tiny MV3 extension
    that answers the auth challenge. Returns the extension dir, or None."""
    import tempfile
    ext_dir = tempfile.mkdtemp(prefix="sc_proxy_ext_")
    manifest = {
        "name": "sc-proxy-auth", "version": "1.0", "manifest_version": 3,
        "permissions": ["proxy", "webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "bg.js"},
    }
    bg = f"""
chrome.webRequest.onAuthRequired.addListener(
  () => ({{authCredentials: {{username: "{user}", password: "{password}"}}}}),
  {{urls: ["<all_urls>"]}}, ["blocking"]
);
"""
    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(ext_dir, "bg.js"), "w") as f:
        f.write(bg)
    return ext_dir


def create_driver():
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    # Optional residential proxy (SC_PROXY) — GitHub's datacenter IP is blocked
    # by Cloudflare Turnstile at Steel City login; a residential proxy passes it.
    # Format: host:port  OR  http://user:pass@host:port
    proxy = os.environ.get("SC_PROXY", "").strip()
    if proxy:
        from urllib.parse import urlparse
        p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        if p.username and p.password:
            ext = _build_proxy_auth_extension(p.hostname, p.port, p.username, p.password)
            options.add_argument(f"--load-extension={ext}")
            options.add_argument(f"--proxy-server={p.hostname}:{p.port}")
        else:
            options.add_argument(f"--proxy-server={p.hostname}:{p.port}")
        log.info(f"Using residential proxy {p.hostname}:{p.port}")

    version = _detect_chrome_major()
    log.info(f"Detected Chrome version: {version}")
    return uc.Chrome(options=options, version_main=version)


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
    """Call product_info API for multiple parts concurrently, with timeout + retry."""
    try:
        result = driver.execute_script("""
            var ids = arguments[0];
            var promises = ids.map(function(pid) {
                return new Promise(function(resolve) {
                    $.ajax({
                        type: 'POST',
                        url: 'applications/shopping_cart/web_services.php?action=product_info&name=' + encodeURIComponent(pid),
                        timeout: 10000,
                        success: function(data) {
                            if (typeof data === 'string') {
                                try { data = JSON.parse(data); } catch(e) {}
                            }
                            resolve({id: pid, data: data, status: 'ok'});
                        },
                        error: function(xhr, status) { resolve({id: pid, data: null, status: status || 'error'}); }
                    });
                });
            });
            return Promise.all(promises).then(function(results) {
                var out = {};
                results.forEach(function(r) { out[r.id] = {data: r.data, status: r.status}; });
                return JSON.stringify(out);
            });
        """, part_ids)
        if result:
            parsed = json.loads(result)
            # Extract data, retry any that failed
            out = {}
            retry_ids = []
            for pid, info in parsed.items():
                if isinstance(info, dict) and 'data' in info:
                    if info['data'] is not None:
                        out[pid] = info['data']
                    else:
                        retry_ids.append(pid)
                        log.debug(f"  API miss for {pid} (status={info.get('status','?')}), will retry")
                elif info is not None:
                    out[pid] = info  # legacy format (direct data)
                else:
                    retry_ids.append(pid)

            # Retry failed SKUs individually with longer timeout
            if retry_ids:
                log.info(f"  Retrying {len(retry_ids)} failed API lookups individually...")
                time.sleep(1)
                for pid in retry_ids:
                    try:
                        retry_result = driver.execute_script("""
                            var pid = arguments[0];
                            return new Promise(function(resolve) {
                                $.ajax({
                                    type: 'POST',
                                    url: 'applications/shopping_cart/web_services.php?action=product_info&name=' + encodeURIComponent(pid),
                                    timeout: 15000,
                                    success: function(data) {
                                        if (typeof data === 'string') {
                                            try { data = JSON.parse(data); } catch(e) {}
                                        }
                                        resolve(JSON.stringify({data: data, status: 'ok'}));
                                    },
                                    error: function(xhr, status) {
                                        resolve(JSON.stringify({data: null, status: (status || 'error') + ' http=' + (xhr.status || '?')}));
                                    }
                                });
                            });
                        """, pid)
                        if retry_result:
                            r = json.loads(retry_result)
                            if r.get('data'):
                                out[pid] = r['data']
                                log.info(f"  Retry SUCCESS for {pid}")
                            else:
                                log.info(f"  Retry FAILED for {pid} (status={r.get('status','?')})")
                                out[pid] = None
                        else:
                            out[pid] = None
                        time.sleep(0.5)
                    except Exception as e:
                        log.debug(f"  Retry exception for {pid}: {e}")
                        out[pid] = None

            return out
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


def shopify_get(path, retries=3):
    """GET from Shopify REST API with retry."""
    for attempt in range(retries):
        try:
            resp = requests.get(
                shopify_api_url(path), headers=shopify_headers(), timeout=30,
            )
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)))
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


def shopify_post(path, payload, retries=3):
    """POST to Shopify REST API with retry."""
    for attempt in range(retries):
        try:
            resp = requests.post(
                shopify_api_url(path), json=payload,
                headers=shopify_headers(), timeout=30,
            )
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)))
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


# ── OOS-but-published helpers (SEO-friendly: keep URL indexable) ────────────

_PRIMARY_LOCATION_ID = None


def get_primary_location_id():
    """Cache and return the primary Shopify location id."""
    global _PRIMARY_LOCATION_ID
    if _PRIMARY_LOCATION_ID is not None:
        return _PRIMARY_LOCATION_ID
    data, _ = shopify_get("locations.json")
    if not data:
        return None
    locs = data.get("locations", [])
    primary = next((l for l in locs if l.get("primary")), None) or (locs[0] if locs else None)
    if primary:
        _PRIMARY_LOCATION_ID = primary["id"]
    return _PRIMARY_LOCATION_ID


def get_inventory_item_id(variant_id):
    data, _ = shopify_get(f"variants/{variant_id}.json")
    if not data:
        return None
    return data.get("variant", {}).get("inventory_item_id")


def set_oos_unbuyable(product_id, variant_id):
    """
    SEO-correct OOS flow: keep the product Active so its URL stays indexed
    by Google, but make it unbuyable:
      - inventory_management = shopify (track stock)
      - inventory_policy = deny (no overselling)
      - inventory level = 0 at primary location
      - status = active (in case it was previously drafted)
    """
    try:
        # 1. Enable tracking + deny overselling on the variant
        variant_payload = {"variant": {
            "id": int(variant_id),
            "inventory_management": "shopify",
            "inventory_policy": "deny",
        }}
        shopify_put(f"variants/{variant_id}.json", variant_payload)
        time.sleep(0.55)

        # 2. Zero out inventory at the primary location
        iid = get_inventory_item_id(variant_id)
        loc_id = get_primary_location_id()
        if iid and loc_id:
            shopify_post("inventory_levels/set.json", {
                "location_id": loc_id,
                "inventory_item_id": iid,
                "available": 0,
            })
            time.sleep(0.55)

        # 3. Make sure status is active (so the storefront URL renders, indexable)
        shopify_put(f"products/{product_id}.json",
                    {"product": {"id": int(product_id), "status": "active"}})
        time.sleep(0.55)
        return True
    except Exception as e:
        # One bad variant must not abort the whole sync; caller records the error.
        log.warning(f"  OOS update failed (status {getattr(getattr(e, 'response', None), 'status_code', '?')}) — continuing")
        return False


def set_in_stock(product_id, variant_id, qty):
    """
    Bump stock back to a positive number when product comes back in stock.
    Status should already be active under the new flow, but we set it as
    a safety net in case a legacy product is still in draft.
    """
    try:
        iid = get_inventory_item_id(variant_id)
        loc_id = get_primary_location_id()
        if iid and loc_id:
            shopify_post("inventory_levels/set.json", {
                "location_id": loc_id,
                "inventory_item_id": iid,
                "available": int(qty),
            })
            time.sleep(0.55)
        shopify_put(f"products/{product_id}.json",
                    {"product": {"id": int(product_id), "status": "active"}})
        time.sleep(0.55)
        return True
    except Exception as e:
        # A single bad variant (e.g. 422 from inventory_levels/set when the item
        # isn't stocked at the location) must not abort the whole sync. The caller
        # records this SKU under errors and continues.
        log.warning(f"  restock failed (status {getattr(getattr(e, 'response', None), 'status_code', '?')}) — continuing")
        return False


def set_product_status(product_id, status):
    """Set Shopify product status to 'active' or 'draft'."""
    payload = {"product": {"id": int(product_id), "status": status}}
    try:
        data, code = shopify_put(f"products/{product_id}.json", payload)
        return data is not None
    except Exception as e:
        log.warning(f"  status update failed (status {getattr(getattr(e, 'response', None), 'status_code', '?')}) — continuing")
        return False


def update_variant_price(variant_id, retail_price, cost):
    """Update variant price (and cost, when known) on Shopify."""
    payload = {"variant": {
        "id": int(variant_id),
        "price": f"{retail_price:.2f}",
    }}
    if cost is not None:
        payload["variant"]["cost"] = f"{cost:.2f}"
    try:
        data, code = shopify_put(f"variants/{variant_id}.json", payload)
        return data is not None
    except Exception as e:
        # Don't let one variant's 4xx abort the sync; caller records the error.
        log.warning(f"  price update failed (status {getattr(getattr(e, 'response', None), 'status_code', '?')}) — continuing")
        return False


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

def run_sync(driver, dry_run=False, reprice_only=False, up_only=False,
             competitive_only=False):
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

    # Desco-distributor imports (second supplier) — markup-priced + stock-tracked
    # like Steel City, so the sync owns them too. Created product ids live in the
    # per-run sheet-import progress under desco_imports/<date>/progress.json.
    before = len(our_product_ids)
    for desco_prog in BASE_DIR.glob("desco_imports/*/progress.json"):
        try:
            with open(desco_prog) as f:
                created = json.load(f).get("created", {})
        except (json.JSONDecodeError, OSError):
            continue
        for pid in created.values():
            pid = pid if isinstance(pid, str) else (pid or {}).get("id")
            if pid:
                our_product_ids.add(str(pid))
    added = len(our_product_ids) - before
    if added:
        log.info(f"  + {added} from Desco imports → {len(our_product_ids)} total")

    # Load dual-source exclusion list (products available from both Steel City
    # AND the store's direct dealers — inventory should NOT be tracked against SC)
    dual_source_skus = set()
    if DUAL_SOURCE_FILE.exists():
        with open(DUAL_SOURCE_FILE) as f:
            dual_source_skus = set(json.load(f))
        log.info(f"Dual-source SKUs to skip (available from other dealers): {len(dual_source_skus)}")

    # Brand-level dual-source: any product whose brand matches is skipped.
    # Brands we always have direct access to (e.g. Sebo) should never have
    # their stock tracked against Steel City.
    dual_source_brands = set()
    if DUAL_SOURCE_BRANDS_FILE.exists():
        with open(DUAL_SOURCE_BRANDS_FILE) as f:
            dual_source_brands = {b.strip().upper() for b in json.load(f) if b and b.strip()}
        log.info(f"Dual-source brands to skip: {len(dual_source_brands)} (names redacted — public CI log)")

    # Only sync SKUs that are in both product_names and shopify_map,
    # AND whose Shopify product_id is one we imported (not pre-existing),
    # AND that are NOT dual-source (available from other dealers)
    sync_skus = []
    skipped_preexisting = 0
    skipped_dual_source = 0
    skipped_dual_brand = 0
    for key, product in products.items():
        sku = product.get("sku", key)
        if sku in dual_source_skus:
            skipped_dual_source += 1
            continue
        map_entry = shopify_map.get(sku)
        # Brand-level dual-source skip. The Shopify product vendor is authoritative
        # (it's what the storefront groups by); fall back to product_names' brand
        # for SKUs whose map entry predates vendor capture.
        if dual_source_brands:
            vendor = (map_entry.get("vendor") if map_entry else "") or ""
            brand = product.get("brand") or ""
            if vendor.strip().upper() in dual_source_brands or brand.strip().upper() in dual_source_brands:
                skipped_dual_brand += 1
                continue
        if map_entry:
            product_id = map_entry["product_id"]
            if our_product_ids and product_id not in our_product_ids:
                skipped_preexisting += 1
                continue
            sync_skus.append((key, sku, product))

    log.info(f"Products in Shopify map: {len(shopify_map)}")
    log.info(f"Skipped (pre-existing, not our import): {skipped_preexisting}")
    log.info(f"Skipped (dual-source SKU): {skipped_dual_source}")
    log.info(f"Skipped (dual-source brand/vendor): {skipped_dual_brand}")
    log.info(f"Products to sync: {len(sync_skus)}")

    # Load progress for resume
    progress = load_sync_progress()
    already_checked = set(progress["checked_skus"])
    remaining = [(k, s, p) for k, s, p in sync_skus if s not in already_checked]
    log.info(f"Already checked: {len(already_checked)}, remaining: {len(remaining)}")

    changes = progress["changes"]

    # Competitor-undercut pass for dual-source brand parts (SEBO etc.) —
    # price-only, driven by reprice_targets.json, no Steel City involved.
    run_reprice_targets(competitor_prices, price_locks, products, progress,
                        changes, dry_run=dry_run, up_only=up_only)

    # General-catalog competitor reprice (all parts/accessories, both
    # distributors). Price-only; skipped in legacy SEBO-only --reprice-only mode.
    if not reprice_only:
        run_competitive_reprice(competitor_prices, price_locks, shopify_map,
                                products, progress, changes, dry_run=dry_run,
                                up_only=up_only)

    if reprice_only or competitive_only:
        log.info("Price-only mode — skipping Steel City stock/price sync")
        remaining = []

    # Process in batches
    for i in range(0, len(remaining), BATCH_SIZE):
        batch = remaining[i:i + BATCH_SIZE]
        batch_skus = [sku for _, sku, _ in batch]

        # Fetch API data (for prices) and real stock (from product pages) in sequence
        results = api_product_info_batch(driver, batch_skus)
        stock_results = scrape_real_stock_batch(driver, batch_skus)

        for key, sku, product in batch:
            api_data = results.get(sku)
            # Steel City returns a list when a SKU lookup is ambiguous; pick the first dict.
            if isinstance(api_data, list):
                api_data = next((x for x in api_data if isinstance(x, dict)), None)
            shop_info = shopify_map[sku]
            product_id = shop_info["product_id"]
            variant_id = shop_info["variant_id"]

            old_qty = product.get("qty_on_hand")
            old_cost = parse_price(product.get("price"))

            # Get new values from API — fall back to cached data if API fails
            if not api_data or not api_data.get("name"):
                # Try cached data from product_names.json (from a previous successful run)
                cached_name = product.get("raw_name") or product.get("clean_name")
                cached_desc = product.get("raw_description", "")
                cached_price = product.get("price")
                if cached_name and cached_price:
                    log.info("  API miss, using cached data (SKU redacted — public CI log)")
                    api_data = {"name": cached_name, "description": cached_desc, "price": cached_price}
                else:
                    changes["errors"].append({"sku": sku, "error": "no_api_data"})
                    progress["checked_skus"].append(sku)
                    continue

            # ── NLA detection: draft products marked No Longer Available ──
            api_name = (api_data.get("name") or "").upper()
            api_desc = (api_data.get("description") or "").upper()
            is_nla = "NLA" in api_name or "NLA" in api_desc or \
                     "NO LONGER AVAILABLE" in api_name or "NO LONGER AVAILABLE" in api_desc
            if is_nla:
                log.info("  NLA: keep active, mark discontinued")
                if not dry_run:
                    # Keep product active for SEO indexing but make unbuyable
                    if variant_id:
                        set_oos_unbuyable(product_id, variant_id)
                    else:
                        set_product_status(product_id, "active")
                    # Set NLA metafield so the theme renders a "Discontinued" badge
                    try:
                        nla_payload = {"product": {"id": int(product_id), "metafields": [
                            {"namespace": "custom", "key": "product_status", "value": "nla", "type": "single_line_text_field"}
                        ]}}
                        shopify_put(f"products/{product_id}.json", nla_payload)
                    except Exception as e:
                        log.debug(f"  NLA metafield failed: {e}")
                    time.sleep(0.55)
                changes["drafted"].append(sku)  # log key kept for back-compat
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
                # Was available → now out of stock.
                # SEO-friendly: keep product Active but unbuyable so the URL
                # stays indexed by Google. Customer cannot purchase (deny + 0 qty).
                log.info("  OOS: qty=0, keep published, unbuyable")
                if not dry_run:
                    if variant_id:
                        ok = set_oos_unbuyable(product_id, variant_id)
                        if not ok:
                            changes["errors"].append({"sku": sku, "error": "oos_failed"})
                    else:
                        changes["errors"].append({"sku": sku, "error": "no_variant_id_for_oos"})
                changes["drafted"].append(sku)  # log key kept as "drafted" for back-compat with email summary

            elif not was_in_stock and now_in_stock:
                # Was out → now back in stock: bump inventory level
                log.info(f"  RESTOCK: qty={new_qty}")
                if not dry_run:
                    if variant_id:
                        ok = set_in_stock(product_id, variant_id, new_qty)
                        if not ok:
                            changes["errors"].append({"sku": sku, "error": "restock_failed"})
                    else:
                        changes["errors"].append({"sku": sku, "error": "no_variant_id_for_restock"})
                changes["activated"].append(sku)

            # Always store real qty for next run's comparison
            products[key]["qty_on_hand"] = new_qty

            # ── Price changes (competitor-aware, skip MAP-locked SKUs) ──
            if new_cost is not None and sku not in price_locks:
                # Use competitor-based price if available, else tiered markup
                new_retail, price_method = get_best_price(sku, new_cost, competitor_prices)
                old_retail_str = product.get("retail_price", "")
                old_retail = parse_price(old_retail_str) or 0

                # up_only: never lower the listed price this run (cost-per-item
                # may still update below — that only corrects margin upward).
                if up_only and old_retail and new_retail < old_retail:
                    new_retail = old_retail

                # Update cost in product_names if it changed
                cost_changed = old_cost is not None and abs(new_cost - old_cost) > 0.005

                # Update price if retail changed or cost changed
                price_changed = abs(new_retail - old_retail) >= 0.01

                if cost_changed or price_changed:
                    direction = "UP" if new_retail > old_retail else "DOWN" if new_retail < old_retail else "COST"
                    log.info(f"  PRICE {direction} ({price_method})")

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

    # Clean up progress file on successful completion. A reprice-only run
    # must not clobber a full sync's resume state (checked_skus), but if
    # there is none, leaving the file would make the next run skip every
    # reprice target.
    if SYNC_PROGRESS_FILE.exists() and not (reprice_only and progress["checked_skus"]):
        SYNC_PROGRESS_FILE.unlink()

    # Full summary (with SKUs) goes to the email only. stdout is the public CI
    # log, so print a counts-only version with SKUs redacted.
    summary = build_summary(log_entry)
    print(build_summary(log_entry, redact=True))

    # Output full summary for GitHub Actions → email (private to the owner)
    if os.environ.get("GITHUB_OUTPUT"):
        # Write multiline output
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"summary<<EOF\n{summary}\nEOF\n")

    return log_entry


def build_summary(log_entry, redact=False):
    """Build human-readable sync summary.

    redact=True omits the per-SKU detail (and dealer costs) for the PUBLIC CI
    log; the full version with SKUs goes only to the owner's email.
    """
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
        if not redact:
            for sku in drafted[:20]:
                lines.append(f"  - {sku}")
            if len(drafted) > 20:
                lines.append(f"  ... and {len(drafted) - 20} more")
        lines.append("")

    activated = log_entry["activated"]
    if activated:
        lines.append(f"REACTIVATED (back in stock): {len(activated)}")
        if not redact:
            for sku in activated[:20]:
                lines.append(f"  - {sku}")
            if len(activated) > 20:
                lines.append(f"  ... and {len(activated) - 20} more")
        lines.append("")

    price_updated = log_entry["price_updated"]
    if price_updated:
        lines.append(f"PRICE INCREASED: {len(price_updated)}")
        if not redact:
            for p in price_updated[:20]:
                lines.append(f"  - {p['sku']}: cost {p['old_cost']}→{p['new_cost']}, "
                            f"retail {p['old_retail']}→{p['new_retail']}")
            if len(price_updated) > 20:
                lines.append(f"  ... and {len(price_updated) - 20} more")
        lines.append("")

    errors = log_entry["errors"]
    if errors:
        lines.append(f"ERRORS: {len(errors)}")
        if not redact:
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
    parser.add_argument("--reprice-only", action="store_true",
                        help="Run only the SEBO/dual-source reprice_targets pass (no Steel City browser)")
    parser.add_argument("--competitive-reprice", action="store_true",
                        help="Run only the general-catalog competitor reprice pass (no Steel City browser)")
    parser.add_argument("--up-only", action="store_true",
                        help="Never lower a listed price this run (for the stale-data correction)")
    args = parser.parse_args()

    if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
        if not args.dry_run:
            print("ERROR: Set SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN environment variables.")
            sys.exit(1)

    price_only = args.reprice_only or args.competitive_reprice
    driver = None
    try:
        if not price_only:
            driver = create_driver()
            login(driver)
        run_sync(driver, dry_run=args.dry_run, reprice_only=args.reprice_only,
                 up_only=args.up_only, competitive_only=args.competitive_reprice)
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
