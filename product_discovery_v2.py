#!/usr/bin/env python3
"""
product_discovery_v2.py — Full product discovery v2 with verified stock checking.

Multi-strategy search to maximize coverage of Steel City's ~64,450 products,
front-end stock verification (qtyoh), image processing, and pricing.

Steps:
  1. discover   — Multi-strategy search (manufacturers, 3-digit prefixes, part terms)
  2. enrich     — product_info API for full details (batched)
  3. stock      — Front-end stock verification via /a/s/p/{sku} (qtyoh scrape)
  4. filter     — Keep verified in-stock with pics, NLA with alts, remove the rest
  5. images     — Download + process ALL images (rembg + watermark + 2048x2048)
  6. crossref   — Cross-reference ezvacuum + eBay for product data
  7. names      — Write enriched batches for Claude agents (NO templates)
  8. names merge — Merge agent-written descriptions back
  9. pricing    — Tiered markup + competitor pricing
  10. merge     — Merge into product_names.json + export spreadsheet
  11. report    — Print stats

Usage:
  python3 product_discovery_v2.py                       # Full pipeline (stops at names for agent work)
  python3 product_discovery_v2.py --step discover       # Single step
  python3 product_discovery_v2.py --step crossref       # Cross-reference ezvacuum + eBay
  python3 product_discovery_v2.py --step names          # Write batches for Claude agents
  python3 product_discovery_v2.py --step names-merge    # Merge agent outputs back
  python3 product_discovery_v2.py --step report         # Stats (no browser)
"""

import argparse
import json
import logging
import os
import random
import re
import time
from io import BytesIO
from pathlib import Path

import openpyxl
import requests
from PIL import Image

# Lazy imports for heavy libs
rembg_remove = None

def get_rembg():
    global rembg_remove
    if rembg_remove is None:
        from rembg import remove
        rembg_remove = remove
    return rembg_remove


# ── Config ──────────────────────────────────────────────────────────────────

CONFIG = {
    "base_url": "https://www.steelcityvac.com",
    "account": os.environ.get("SC_ACCOUNT", ""),
    "user_id": os.environ.get("SC_USER", ""),
    "password": os.environ.get("SC_PASSWORD", ""),
}

BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "images"
IMAGES_RAW_DIR = BASE_DIR / "images_raw"
OUTPUT_DIR = BASE_DIR / "output"
PROGRESS_FILE = BASE_DIR / "discovery_v2_progress.json"
PRODUCTS_FILE = BASE_DIR / "product_names.json"
FULL_API_DATA_FILE = BASE_DIR / "full_api_data.json"
MISSING_ENUM_FILE = BASE_DIR / "missing_products_enumeration.json"
COMPETITOR_PRICES_FILE = BASE_DIR / "competitor_prices.json"
PRICE_LOCKS_FILE = BASE_DIR / "price_locks.json"
COMPAT_MAP_FILE = BASE_DIR / "compatibility_map.json"
EZVAC_FILE = BASE_DIR / "ezvacuum_descriptions.json"
LOGO_PATH = BASE_DIR / "VaM Watermark.png"

# Cross-reference + batch directories
CROSSREF_PROGRESS_FILE = BASE_DIR / "discovery_v2_crossref_progress.json"
DESC_BATCH_DIR = BASE_DIR / "discovery_v2_batches"
DESC_BATCH_SIZE = 200  # Products per batch for Claude agents

# ezvacuum suggest API (Shopify store)
EZVAC_SUGGEST_URL = "https://www.ezvacuum.com/search/suggest.json"
EZVAC_PRODUCT_BASE = "https://www.ezvacuum.com"
CROSSREF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

IMAGE_BASE_URL = "https://www.steelcityvac.com/uploads/applications/shopping_cart/"
TARGET_SIZE = 2048
PRODUCT_MAX_SIZE = 1800
JPEG_QUALITY = 85
LOGO_OPACITY = 0.20
BATCH_SIZE = 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Progress ────────────────────────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {
        "search_completed": [],
        "all_products": {},
        "enriched": {},
        "enrich_errors": [],
        "stock_checked": {},     # sku → qtyoh (int or null)
        "filtered": {},
        "downloaded": [],
        "processed": [],
        "failed": {},
        "alt_traced": {},
        "crossref_done": [],     # SKUs that have been cross-referenced
        "crossref_data": {},     # sku → {ezvac: {...}, ebay: {...}}
        "names_batched": False,  # Whether batches have been written
        "names_merged": False,   # Whether agent outputs have been merged
        "priced": [],
    }


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


# ── Browser helpers ─────────────────────────────────────────────────────────

def create_driver():
    import undetected_chromedriver as uc
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    if os.environ.get("CI"):
        options.add_argument("--headless=new")
        options.add_argument("--disable-dev-shm-usage")
    return uc.Chrome(options=options, headless=bool(os.environ.get("CI")), version_main=145)


def login(driver):
    from selenium.webdriver.common.by import By

    log.info("Navigating to Steel City login page...")
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


# ── API Calls ───────────────────────────────────────────────────────────────

def api_search(driver, query, take=500, page=1):
    """Search API with pagination."""
    try:
        result = driver.execute_script("""
            return new Promise((resolve) => {
                $.ajax({
                    type: 'POST',
                    url: 'applications/shopping_cart/web_services.php?action=search&take='
                         + arguments[1] + '&searchstring=' + encodeURIComponent(arguments[0])
                         + '&page=' + arguments[2],
                    success: function(data) {
                        if (typeof data === 'string') {
                            try { data = JSON.parse(data); } catch(e) {}
                        }
                        resolve(JSON.stringify(data));
                    },
                    error: function() { resolve(null); }
                });
            });
        """, query, take, page)
        if result:
            return json.loads(result)
    except Exception as e:
        log.debug(f"Search API failed for '{query}' page {page}: {e}")
    return None


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
        log.debug(f"Batch API failed: {e}")
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


# ── Pricing ─────────────────────────────────────────────────────────────────

MARKUP_TIERS = [
    (1.00, 8.0), (3.00, 4.5), (7.00, 3.2), (15.00, 2.5),
    (30.00, 2.2), (60.00, 1.9), (120.00, 1.7), (300.00, 1.5),
    (float("inf"), 1.4),
]
MIN_PRICE = 6.99
SHOPIFY_FEE_RATE = 0.029
SHOPIFY_FEE_FIXED = 0.30
UNDERCUT_TIERS = [
    (20, 0.50), (50, 1.00), (100, 2.00), (300, 5.00),
    (float("inf"), 10.00),
]


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


def calculate_break_even(cost):
    return (cost + SHOPIFY_FEE_FIXED) / (1 - SHOPIFY_FEE_RATE)


def get_undercut(competitor_avg):
    for max_price, undercut in UNDERCUT_TIERS:
        if competitor_avg < max_price:
            return undercut
    return UNDERCUT_TIERS[-1][1]


def get_best_price(sku, dealer_cost, competitor_prices):
    """Beat lowest competitor by $1 if profitable, else avg undercut, else markup."""
    markup_price = calculate_retail_price(dealer_cost)

    comp_data = competitor_prices.get(sku)
    if not comp_data or comp_data.get("num_competitors", 0) == 0:
        return markup_price, "markup"

    sku_lower = sku.lower()
    sku_norm = re.sub(r'[\-\s\.]', '', sku).lower()
    valid_prices = []
    for domain, cdata in comp_data.get("competitors", {}).items():
        if not cdata or not cdata.get("price"):
            continue
        ratio = cdata["price"] / dealer_cost if dealer_cost > 0 else 1
        if not (0.50 <= ratio <= 5.0):
            continue
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

    target_min = charm_price(competitor_min - 1.00)
    target_min = max(target_min, MIN_PRICE)
    if target_min >= break_even:
        return target_min, "competitor"

    undercut = get_undercut(competitor_avg)
    target_avg = charm_price(competitor_avg - undercut)
    target_avg = max(target_avg, MIN_PRICE)
    if target_avg >= break_even:
        return target_avg, "competitor"

    return markup_price, "markup"


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: Multi-Strategy Search Discovery
# ══════════════════════════════════════════════════════════════════════════════

def get_manufacturer_names():
    """Extract all 123 categoryName values from full_api_data.json."""
    if not FULL_API_DATA_FILE.exists():
        log.warning(f"{FULL_API_DATA_FILE} not found, skipping manufacturer strategy")
        return []
    with open(FULL_API_DATA_FILE) as f:
        data = json.load(f)
    names = set()
    for v in data.values():
        if v and v.get("categoryName"):
            names.add(v["categoryName"])
    return sorted(names)


PART_TERMS = [
    "belt", "bag", "filter", "hose", "motor", "brush", "cord", "switch",
    "wheel", "fan", "gasket", "bearing", "handle", "nozzle", "wand",
    "spring", "bumper", "latch", "valve", "pedal", "cover", "plate",
    "axle", "carbon brush", "dust cup", "power head", "roller",
    "attachment", "agitator", "seal", "housing",
]


def _run_search_query(driver, query, all_products):
    """Run a single search query with full pagination. Returns count of new products found."""
    page = 1
    query_new = 0

    while True:
        data = api_search(driver, query, take=500, page=page)
        if not data:
            break

        # API sometimes returns a list instead of a dict
        if isinstance(data, list):
            results = data
            total = len(results)
        else:
            total = int(data.get("total", 0))
            results = data.get("results", [])

        if not results:
            break

        for item in results:
            product_code = str(item.get("2", "")).strip()
            if not product_code:
                continue
            if product_code not in all_products:
                all_products[product_code] = {
                    "product_code": product_code,
                    "product_id": str(item.get("1", "")),
                    "name": str(item.get("3", "")),
                    "price": str(item.get("5", "")),
                    "in_stock": str(item.get("7", "")),
                }
                query_new += 1

        if page * 500 >= total or len(results) < 500:
            break
        page += 1
        time.sleep(random.uniform(0.2, 0.4))

    return query_new, page


def step_discover(driver, progress):
    """Multi-strategy search: manufacturers, 3-digit prefixes, part terms."""
    log.info("=" * 60)
    log.info("STEP 1: Multi-Strategy Search Discovery")
    log.info("=" * 60)

    # Must navigate: base URL first (establishes session), THEN /a/s/ for search API
    driver.get(CONFIG["base_url"])
    time.sleep(2)
    driver.get(f"{CONFIG['base_url']}/a/s/")
    time.sleep(3)

    all_products = progress["all_products"]
    already_done = set(progress["search_completed"])
    initial_count = len(all_products)

    # Seed from missing_products_enumeration.json
    if MISSING_ENUM_FILE.exists() and "_seeded" not in already_done:
        with open(MISSING_ENUM_FILE) as f:
            enum_data = json.load(f)
        missing = enum_data.get("missing", {})
        seeded = 0
        for pc, info in missing.items():
            if pc not in all_products:
                all_products[pc] = {
                    "product_code": pc,
                    "product_id": "",
                    "name": info.get("name", ""),
                    "price": "",
                    "in_stock": info.get("in_stock", ""),
                }
                seeded += 1
        progress["search_completed"].append("_seeded")
        save_progress(progress)
        log.info(f"Seeded {seeded} products from missing_products_enumeration.json")

    # Strategy 0: Progressive-depth prefix search (2-char → 3-char → 4-char)
    # The search API caps at ~486 results per query. For any prefix that hits
    # the cap, we drill deeper by adding more characters.
    CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"
    PREFIX_CAP = 480  # If results >= this, the query is likely capped

    def _get_2char_prefixes():
        """Generate all 2-char prefix combos."""
        prefixes = []
        for a in CHARS:
            for b in CHARS:
                prefixes.append(a + b)
        return prefixes

    if "prefix_deep" not in already_done:
        log.info(f"\nStrategy 0: Progressive-depth prefix search")
        pre_prefix = len(all_products)

        # Start with 2-char prefixes
        prefixes_to_search = _get_2char_prefixes()
        depth = 2
        max_depth = 4
        capped_queries = []

        while prefixes_to_search and depth <= max_depth:
            log.info(f"\n  Depth {depth}: {len(prefixes_to_search)} prefixes to search")
            next_level = []
            searched = 0

            for prefix in prefixes_to_search:
                query_key = f"pfx:{prefix}"
                if query_key in already_done:
                    continue

                new, pages = _run_search_query(driver, prefix, all_products)
                progress["search_completed"].append(query_key)
                searched += 1

                # Check if we hit the cap (need to go deeper)
                # We detect cap by checking if the search returned exactly take=500 results
                if pages > 1 or new >= PREFIX_CAP:
                    # This prefix has more results than the cap — drill deeper
                    for c in CHARS:
                        next_level.append(prefix + c)

                if new > 0:
                    log.info(f"    '{prefix}': +{new} new, total={len(all_products)}"
                             f"{' (CAPPED, will drill deeper)' if pages > 1 or new >= PREFIX_CAP else ''}")

                if searched % 100 == 0:
                    save_progress(progress)
                    log.info(f"    Progress: {searched}/{len(prefixes_to_search)}, total={len(all_products)}")

                time.sleep(random.uniform(0.15, 0.35))

            save_progress(progress)
            added = len(all_products) - pre_prefix
            log.info(f"  Depth {depth} complete: searched {searched}, +{added} total new so far")

            prefixes_to_search = next_level
            depth += 1

        progress["search_completed"].append("prefix_deep")
        save_progress(progress)
        prefix_new = len(all_products) - pre_prefix
        log.info(f"  Progressive prefix search complete: +{prefix_new} new, total={len(all_products)}")

    # Strategy A: Manufacturer/category names (123 queries)
    manufacturer_names = get_manufacturer_names()
    log.info(f"\nStrategy A: {len(manufacturer_names)} manufacturer/category names")

    for name in manufacturer_names:
        query_key = f"mfr:{name}"
        if query_key in already_done:
            continue

        new, pages = _run_search_query(driver, name, all_products)
        progress["search_completed"].append(query_key)

        if new > 0 or pages > 1:
            log.info(f"  '{name}': +{new} new, {pages} pages, total={len(all_products)}")

        # Checkpoint every 10 queries
        if len(progress["search_completed"]) % 10 == 0:
            save_progress(progress)

        time.sleep(random.uniform(0.3, 0.6))

    save_progress(progress)
    log.info(f"After manufacturers: {len(all_products)} products (+{len(all_products) - initial_count} new)")

    # Strategy B: 3-digit numeric prefixes (000-999)
    log.info(f"\nStrategy B: 3-digit numeric prefixes (000-999)")
    pre_b_count = len(all_products)

    for i in range(1000):
        prefix = f"{i:03d}"
        query_key = f"3d:{prefix}"
        if query_key in already_done:
            continue

        new, pages = _run_search_query(driver, prefix, all_products)
        progress["search_completed"].append(query_key)

        if new > 0:
            log.info(f"  '{prefix}': +{new} new, {pages} pages, total={len(all_products)}")

        if i % 50 == 0:
            save_progress(progress)
            log.info(f"  Progress: {i}/1000 prefixes, total products={len(all_products)}")

        time.sleep(random.uniform(0.2, 0.4))

    save_progress(progress)
    log.info(f"After 3-digit prefixes: {len(all_products)} products (+{len(all_products) - pre_b_count} from this strategy)")

    # Strategy C: Generic part terms (~30 queries)
    log.info(f"\nStrategy C: {len(PART_TERMS)} generic part terms")
    pre_c_count = len(all_products)

    for term in PART_TERMS:
        query_key = f"term:{term}"
        if query_key in already_done:
            continue

        new, pages = _run_search_query(driver, term, all_products)
        progress["search_completed"].append(query_key)

        if new > 0:
            log.info(f"  '{term}': +{new} new, {pages} pages, total={len(all_products)}")

        time.sleep(random.uniform(0.3, 0.6))

    save_progress(progress)
    total_new = len(all_products) - initial_count
    log.info(f"\nDiscovery complete. Total unique: {len(all_products)} (+{total_new} new)")
    log.info(f"  Strategy A (manufacturers): {pre_b_count - initial_count}")
    log.info(f"  Strategy B (3-digit):       {pre_c_count - pre_b_count}")
    log.info(f"  Strategy C (part terms):    {len(all_products) - pre_c_count}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: Enrich via product_info API
# ══════════════════════════════════════════════════════════════════════════════

def step_enrich(driver, progress):
    """Call product_info API for all discovered products."""
    log.info("=" * 60)
    log.info("STEP 2: Enriching products via product_info API")
    log.info("=" * 60)

    driver.get(CONFIG["base_url"])
    time.sleep(2)
    driver.get(f"{CONFIG['base_url']}/a/s/")
    time.sleep(3)

    all_products = progress["all_products"]
    enriched = progress["enriched"]

    to_enrich = [pc for pc in all_products if pc not in enriched]
    log.info(f"Total discovered: {len(all_products)}")
    log.info(f"Already enriched: {len(enriched)}")
    log.info(f"Remaining: {len(to_enrich)}")

    batch_num = 0
    for i in range(0, len(to_enrich), BATCH_SIZE):
        batch = to_enrich[i:i + BATCH_SIZE]
        results = api_product_info_batch(driver, batch)

        for pc in batch:
            data = results.get(pc)
            # API sometimes returns a list — take first element if so
            if isinstance(data, list):
                data = data[0] if data else None
            if not data or not isinstance(data, dict) or not data.get("name"):
                progress["enrich_errors"].append(pc)
                enriched[pc] = None
                continue

            enriched[pc] = {
                "name": data.get("name", ""),
                "product_code": data.get("product_code", pc),
                "description": data.get("description", ""),
                "in_stock": data.get("in_stock", ""),
                "picture": data.get("picture", ""),
                "big_picture": data.get("big_picture", ""),
                "Price_1": data.get("Price_1", ""),
                "manufacturer": data.get("manufacturer", ""),
                "alt_items": data.get("alt_items", []),
                "productID": str(data.get("productID", "")),
                "category_names": data.get("category_names", ""),
            }

        batch_num += 1
        if batch_num % 25 == 0:
            save_progress(progress)
            done = len(enriched)
            total = len(all_products)
            pct = 100 * done // total if total else 0
            log.info(f"  Progress: {done}/{total} ({pct}%)")

        time.sleep(random.uniform(0.2, 0.5))

    save_progress(progress)
    log.info(f"Enrichment complete. Enriched: {len(enriched)}, Errors: {len(progress['enrich_errors'])}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: Front-End Stock Verification
# ══════════════════════════════════════════════════════════════════════════════

def step_stock(driver, progress):
    """Verify real stock via front-end qtyoh scrape for enriched products."""
    log.info("=" * 60)
    log.info("STEP 3: Front-End Stock Verification (qtyoh)")
    log.info("=" * 60)

    driver.get(CONFIG["base_url"])
    time.sleep(2)
    driver.get(f"{CONFIG['base_url']}/a/s/")
    time.sleep(3)

    enriched = progress["enriched"]
    stock_checked = progress["stock_checked"]

    # Load existing products to know what we already have
    existing_codes = set()
    if PRODUCTS_FILE.exists():
        with open(PRODUCTS_FILE) as f:
            existing = json.load(f)
        for key, prod in existing.items():
            existing_codes.add(key)
            existing_codes.add(prod.get("sku", ""))

    # Only stock-check products that:
    # 1. Were successfully enriched
    # 2. Haven't been stock-checked yet
    # 3. Aren't already in our product_names.json
    # 4. Have a picture (no point checking stock on pictureless products)
    to_check = []
    for pc, data in enriched.items():
        if data is None:
            continue
        if pc in stock_checked:
            continue
        if pc in existing_codes or data.get("product_code", pc) in existing_codes:
            continue
        # Check if has picture
        picture = data.get("picture", "")
        big_picture = data.get("big_picture", "")
        has_picture = bool(picture and picture != "0") or bool(big_picture and big_picture != "0")
        # Also check NLA with alts (always need to verify these)
        name = data.get("name", "")
        desc = data.get("description", "")
        is_nla = "NLA" in (name + " " + desc).upper() or "NO LONGER AVAILABLE" in (name + " " + desc).upper()
        has_alt = bool(data.get("alt_items"))

        if has_picture or (is_nla and has_alt):
            to_check.append(pc)

    log.info(f"Total enriched: {len(enriched)}")
    log.info(f"Already stock-checked: {len(stock_checked)}")
    log.info(f"Already in product_names: {len(existing_codes)}")
    log.info(f"To check: {len(to_check)}")

    for i in range(0, len(to_check), BATCH_SIZE):
        batch = to_check[i:i + BATCH_SIZE]
        results = scrape_real_stock_batch(driver, batch)

        for sku in batch:
            raw = results.get(sku)
            if raw is not None:
                try:
                    stock_checked[sku] = int(raw)
                except (ValueError, TypeError):
                    stock_checked[sku] = None
            else:
                stock_checked[sku] = None

        if (i // BATCH_SIZE + 1) % 50 == 0:
            save_progress(progress)
            done = len(stock_checked)
            total_to_check = len(to_check) + len([k for k in stock_checked if k not in set(to_check)])
            log.info(f"  Progress: {done} checked, at batch {i // BATCH_SIZE + 1}")

        time.sleep(random.uniform(0.3, 0.8))

    save_progress(progress)

    # Stats
    verified_in_stock = sum(1 for v in stock_checked.values() if v is not None and v > 0)
    verified_oos = sum(1 for v in stock_checked.values() if v is not None and v == 0)
    check_failed = sum(1 for v in stock_checked.values() if v is None)
    log.info(f"\nStock verification complete:")
    log.info(f"  In stock (qtyoh > 0): {verified_in_stock}")
    log.info(f"  Out of stock (qtyoh = 0): {verified_oos}")
    log.info(f"  Check failed: {check_failed}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: Filter
# ══════════════════════════════════════════════════════════════════════════════

def step_filter(progress):
    """Filter using verified stock data. Keep in-stock+pic, NLA with alts."""
    log.info("=" * 60)
    log.info("STEP 4: Filtering (verified stock)")
    log.info("=" * 60)

    enriched = progress["enriched"]
    stock_checked = progress["stock_checked"]
    filtered = {}
    alt_traced = {}

    existing_codes = set()
    if PRODUCTS_FILE.exists():
        with open(PRODUCTS_FILE) as f:
            existing = json.load(f)
        for key, prod in existing.items():
            existing_codes.add(key)
            existing_codes.add(prod.get("sku", ""))
    log.info(f"Existing products to dedup against: {len(existing_codes)}")

    stats = {
        "total_enriched": 0, "no_data": 0, "already_have": 0,
        "verified_in_stock_with_pic": 0, "verified_in_stock_no_pic": 0,
        "nla_with_alt": 0, "nla_no_alt": 0,
        "oos_with_alt": 0, "oos_no_alt": 0,
        "stock_unknown": 0,
    }

    for pc, data in enriched.items():
        stats["total_enriched"] += 1

        if data is None:
            stats["no_data"] += 1
            continue

        product_code = data.get("product_code", pc)
        if pc in existing_codes or product_code in existing_codes:
            stats["already_have"] += 1
            continue

        name = data.get("name", "")
        description = data.get("description", "")
        picture = data.get("picture", "")
        big_picture = data.get("big_picture", "")
        alt_items = data.get("alt_items", [])

        has_picture = bool(picture and picture != "0") or bool(big_picture and big_picture != "0")
        has_alt = bool(alt_items and isinstance(alt_items, list) and len(alt_items) > 0)

        is_nla = False
        for field in [name, description]:
            if field and ("NLA" in field.upper() or "NO LONGER AVAILABLE" in field.upper()):
                is_nla = True
                break

        # Get verified stock
        qtyoh = stock_checked.get(pc)

        # If stock wasn't checked (no pic and not NLA with alt), skip
        if qtyoh is None and pc not in stock_checked:
            stats["stock_unknown"] += 1
            continue

        verified_in_stock = qtyoh is not None and qtyoh > 0

        # Decision logic
        if verified_in_stock and has_picture:
            stats["verified_in_stock_with_pic"] += 1
            img_filename = (big_picture or picture or "").strip()
            image_url = IMAGE_BASE_URL + img_filename if img_filename and img_filename != "0" else ""

            price_str = ""
            price_raw = data.get("Price_1", "")
            if price_raw:
                try:
                    price_str = f"${float(price_raw):.2f}"
                except (ValueError, TypeError):
                    price_str = str(price_raw)

            filtered[product_code] = {
                "product_code": product_code,
                "name": name,
                "description": description,
                "price": price_str,
                "in_stock": "1",
                "qtyoh": qtyoh,
                "image_url": image_url,
                "manufacturer": data.get("manufacturer", ""),
                "alt_items": alt_items,
                "source": "discovery_v2",
            }
            existing_codes.add(product_code)
            existing_codes.add(pc)

        elif verified_in_stock and not has_picture:
            stats["verified_in_stock_no_pic"] += 1

        elif is_nla and has_alt:
            stats["nla_with_alt"] += 1
            for alt in alt_items:
                if isinstance(alt, dict) and alt.get("product_code"):
                    alt_traced[pc] = alt["product_code"]

        elif is_nla and not has_alt:
            stats["nla_no_alt"] += 1

        elif not verified_in_stock and has_alt:
            stats["oos_with_alt"] += 1
            for alt in alt_items:
                if isinstance(alt, dict) and alt.get("product_code"):
                    alt_traced[pc] = alt["product_code"]

        elif not verified_in_stock:
            stats["oos_no_alt"] += 1

        else:
            stats["oos_no_alt"] += 1

    # Trace alts
    log.info(f"\nTracing {len(alt_traced)} alt items...")
    alt_additions = 0
    for original_pc, alt_code in alt_traced.items():
        if alt_code in filtered or alt_code in existing_codes:
            continue
        alt_data = enriched.get(alt_code)
        if not alt_data:
            continue
        # Check verified stock for the alt
        alt_qtyoh = stock_checked.get(alt_code)
        if alt_qtyoh is not None and alt_qtyoh > 0:
            alt_pic = alt_data.get("picture", "")
            alt_big_pic = alt_data.get("big_picture", "")
            has_pic = bool(alt_pic and alt_pic != "0") or bool(alt_big_pic and alt_big_pic != "0")
            if has_pic:
                img_filename = (alt_big_pic or alt_pic or "").strip()
                image_url = IMAGE_BASE_URL + img_filename if img_filename and img_filename != "0" else ""
                price_str = ""
                price_raw = alt_data.get("Price_1", "")
                if price_raw:
                    try:
                        price_str = f"${float(price_raw):.2f}"
                    except (ValueError, TypeError):
                        price_str = str(price_raw)

                alt_product_code = alt_data.get("product_code", alt_code)
                filtered[alt_product_code] = {
                    "product_code": alt_product_code,
                    "name": alt_data.get("name", ""),
                    "description": alt_data.get("description", ""),
                    "price": price_str,
                    "in_stock": "1",
                    "qtyoh": alt_qtyoh,
                    "image_url": image_url,
                    "manufacturer": alt_data.get("manufacturer", ""),
                    "alt_items": alt_data.get("alt_items", []),
                    "source": "discovery_v2_alt_trace",
                    "alt_for": original_pc,
                }
                existing_codes.add(alt_product_code)
                alt_additions += 1

    progress["filtered"] = filtered
    progress["alt_traced"] = alt_traced
    save_progress(progress)

    log.info(f"\nFilter Results:")
    for k, v in sorted(stats.items()):
        log.info(f"  {k:35s}: {v}")
    log.info(f"  alt_traced_additions              : {alt_additions}")
    log.info(f"\n  TOTAL NEW PRODUCTS: {len(filtered)}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: Download + Process Images
# ══════════════════════════════════════════════════════════════════════════════

def step_download(progress):
    """Download raw images for ALL filtered products. Retries failures."""
    log.info("=" * 60)
    log.info("STEP 5: Downloading ALL images (with retries)")
    log.info("=" * 60)

    IMAGES_RAW_DIR.mkdir(exist_ok=True)
    filtered = progress["filtered"]
    already_downloaded = set(progress["downloaded"])

    # Collect ALL products with image URLs, including previously failed ones (retry them)
    to_download = []
    for pc, prod in filtered.items():
        if pc in already_downloaded:
            continue
        url = prod.get("image_url", "")
        if url:
            to_download.append((pc, url))

    log.info(f"To download: {len(to_download)} (already done: {len(already_downloaded)})")

    downloaded = 0
    still_failed = 0
    for i, (pc, url) in enumerate(to_download):
        folder_name = re.sub(r'[^\w\-]', '_', pc)
        raw_path = IMAGES_RAW_DIR / f"{folder_name}.jpg"

        if raw_path.exists():
            progress["downloaded"].append(pc)
            # Remove from failed if it was there from a previous run
            progress["failed"].pop(pc, None)
            downloaded += 1
            continue

        # Try up to 3 times
        success = False
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=20, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                })
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")
                if "image" not in content_type and "octet-stream" not in content_type:
                    progress["failed"][pc] = "not_image"
                    break

                img = Image.open(BytesIO(resp.content))
                if img.width < 50 or img.height < 50:
                    progress["failed"][pc] = "too_small"
                    break

                with open(raw_path, "wb") as f:
                    f.write(resp.content)
                progress["downloaded"].append(pc)
                progress["failed"].pop(pc, None)
                downloaded += 1
                success = True
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                progress["failed"][pc] = f"download: {str(e)[:80]}"
                still_failed += 1

        if (i + 1) % 50 == 0:
            save_progress(progress)
            log.info(f"  [{i+1}/{len(to_download)}] Downloaded: {downloaded}, Failed: {still_failed}")

        time.sleep(random.uniform(0.05, 0.15))

    save_progress(progress)
    log.info(f"Download complete. Downloaded: {downloaded}, Failed: {still_failed}")


def load_logo():
    if not LOGO_PATH.exists():
        log.warning(f"Logo not found at {LOGO_PATH}")
        return None
    return Image.open(LOGO_PATH).convert("RGBA")


def create_background_with_logo(logo):
    canvas = Image.new("RGBA", (TARGET_SIZE, TARGET_SIZE), (255, 255, 255, 255))
    if logo is None:
        return canvas
    logo_scale = 0.80
    logo_w = int(TARGET_SIZE * logo_scale)
    ratio = logo_w / logo.width
    logo_h = int(logo.height * ratio)
    scaled_logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
    r, g, b, a = scaled_logo.split()
    a = a.point(lambda p: int(p * LOGO_OPACITY))
    scaled_logo.putalpha(a)
    x = (TARGET_SIZE - logo_w) // 2
    y = (TARGET_SIZE - logo_h) // 2
    canvas.paste(scaled_logo, (x, y), scaled_logo)
    return canvas


def process_single_image(raw_path, output_path, bg_canvas):
    remove = get_rembg()
    with open(raw_path, "rb") as f:
        raw_data = f.read()
    result_data = remove(raw_data)
    product = Image.open(BytesIO(result_data)).convert("RGBA")
    ratio = min(PRODUCT_MAX_SIZE / product.width, PRODUCT_MAX_SIZE / product.height)
    if ratio != 1.0:
        new_w = int(product.width * ratio)
        new_h = int(product.height * ratio)
        product = product.resize((new_w, new_h), Image.LANCZOS)
    composite = bg_canvas.copy()
    x = (TARGET_SIZE - product.width) // 2
    y = (TARGET_SIZE - product.height) // 2
    composite.paste(product, (x, y), product)
    final = composite.convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final.save(str(output_path), "JPEG", quality=JPEG_QUALITY)


def step_process_images(progress):
    """Process downloaded images: bg removal + watermark + pad."""
    log.info("=" * 60)
    log.info("STEP 5b: Processing images (bg removal + logo + pad)")
    log.info("=" * 60)

    IMAGES_DIR.mkdir(exist_ok=True)
    already_processed = set(progress["processed"])
    to_process = [pc for pc in progress["downloaded"] if pc not in already_processed]

    log.info(f"To process: {len(to_process)} (already done: {len(already_processed)})")

    logo = load_logo()
    bg_canvas = create_background_with_logo(logo)
    log.info(f"Background canvas ready (logo: {'yes' if logo else 'no'})")

    processed = 0
    errors = 0

    for i, pc in enumerate(to_process):
        folder_name = re.sub(r'[^\w\-]', '_', pc)
        raw_path = IMAGES_RAW_DIR / f"{folder_name}.jpg"
        output_path = IMAGES_DIR / folder_name / "1.jpg"

        if output_path.exists():
            progress["processed"].append(pc)
            processed += 1
            continue

        if not raw_path.exists():
            progress["failed"][pc] = "raw_missing"
            errors += 1
            continue

        try:
            process_single_image(raw_path, output_path, bg_canvas)
            progress["processed"].append(pc)
            processed += 1
        except Exception as e:
            log.warning(f"  Failed to process {pc}: {e}")
            progress["failed"][pc] = f"process: {str(e)[:80]}"
            errors += 1

        if (i + 1) % 25 == 0:
            save_progress(progress)
            log.info(f"  [{i+1}/{len(to_process)}] Processed: {processed}, Errors: {errors}")

    save_progress(progress)
    log.info(f"Processing complete. Processed: {processed}, Errors: {errors}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6: Cross-Reference (ezvacuum first, then eBay via browser)
# ══════════════════════════════════════════════════════════════════════════════

def _search_ezvac(sku):
    """Search ezvacuum.com suggest API. Returns product data dict or None."""
    params = {
        "q": sku,
        "resources[type]": "product",
        "resources[limit]": 5,
    }
    try:
        resp = requests.get(EZVAC_SUGGEST_URL, params=params, headers=CROSSREF_HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        products = data.get("resources", {}).get("results", {}).get("products", [])
        sku_lower = sku.lower()
        sku_clean = sku_lower.replace("-", "").replace(" ", "")
        for p in products:
            title = p.get("title", "").lower()
            url = p.get("url", "").lower()
            title_clean = title.replace("-", "").replace(" ", "")
            if sku_lower in title or sku_lower in url or sku_clean in title_clean:
                # Fetch full product JSON
                product_url = f"{EZVAC_PRODUCT_BASE}{p['url']}.json"
                resp2 = requests.get(product_url, headers=CROSSREF_HEADERS, timeout=15)
                if resp2.status_code == 200:
                    pdata = resp2.json().get("product", {})
                    return {
                        "title": pdata.get("title", ""),
                        "body_html": pdata.get("body_html", ""),
                        "vendor": pdata.get("vendor", ""),
                        "product_type": pdata.get("product_type", ""),
                        "tags": pdata.get("tags", ""),
                    }
        return None
    except Exception:
        return None


def step_crossref(driver, progress):
    """Cross-reference filtered products against ezvacuum for description reference data."""
    log.info("=" * 60)
    log.info("STEP 6: Cross-Reference (ezvacuum.com)")
    log.info("=" * 60)

    filtered = progress["filtered"]
    processed_set = set(progress["processed"])
    crossref_done = set(progress["crossref_done"])
    crossref_data = progress["crossref_data"]

    # Load existing ezvacuum data from prior cross-ref runs
    existing_ezvac = {}
    if EZVAC_FILE.exists():
        with open(EZVAC_FILE) as f:
            existing_ezvac = json.load(f)
        log.info(f"Loaded {len(existing_ezvac)} existing ezvacuum matches")

    # Only cross-ref products that have images processed
    to_check = []
    for pc, prod in filtered.items():
        if pc not in processed_set:
            continue
        if pc in crossref_done:
            continue
        to_check.append(pc)

    log.info(f"To cross-reference: {len(to_check)} (already done: {len(crossref_done)})")

    ezvac_hits = 0
    no_match = 0

    for i, pc in enumerate(to_check):
        prod = filtered[pc]
        sku = prod.get("product_code", pc)

        # Check existing ezvacuum data first (from prior ezvacuum_cross_ref.py run)
        if sku in existing_ezvac:
            crossref_data[pc] = {"ezvac": existing_ezvac[sku], "source": "ezvac_existing"}
            ezvac_hits += 1
            progress["crossref_done"].append(pc)
            continue

        # Try ezvacuum suggest API
        ezvac_result = _search_ezvac(sku)
        if ezvac_result and ezvac_result.get("body_html"):
            crossref_data[pc] = {"ezvac": ezvac_result, "source": "ezvac_api"}
            ezvac_hits += 1
        else:
            crossref_data[pc] = {"source": "none"}
            no_match += 1

        progress["crossref_done"].append(pc)

        if (i + 1) % 100 == 0:
            save_progress(progress)
            log.info(f"  [{i+1}/{len(to_check)}] ezvac={ezvac_hits}, none={no_match}")

        time.sleep(random.uniform(0.4, 0.8))

    save_progress(progress)
    log.info(f"\nCross-reference complete:")
    log.info(f"  ezvacuum matches: {ezvac_hits}")
    log.info(f"  No match (agents will use Steel City data only): {no_match}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7: Write Batches for Claude Agents (NO templates — agents write everything)
# ══════════════════════════════════════════════════════════════════════════════

def step_names(progress):
    """Write enriched batch files for Claude agents to write names + descriptions."""
    log.info("=" * 60)
    log.info("STEP 7: Write Batches for Claude Agent Description Writing")
    log.info("=" * 60)

    filtered = progress["filtered"]
    processed_set = set(progress["processed"])
    crossref_data = progress.get("crossref_data", {})

    # Load compatibility map (schematics data: SKU → {brand: [models]})
    compat_map = {}
    if COMPAT_MAP_FILE.exists():
        with open(COMPAT_MAP_FILE) as f:
            compat_map = json.load(f)
        log.info(f"Loaded compatibility map: {len(compat_map)} SKUs")

    # Load full API data for category hierarchy
    full_api = {}
    if FULL_API_DATA_FILE.exists():
        with open(FULL_API_DATA_FILE) as f:
            full_api = json.load(f)
        log.info(f"Loaded full API data: {len(full_api)} products")

    # Build batch items — only products with processed images
    items = []
    for pc, prod in filtered.items():
        if pc not in processed_set:
            continue
        items.append((pc, prod))

    log.info(f"Products to batch: {len(items)}")

    DESC_BATCH_DIR.mkdir(exist_ok=True)

    batch_num = 0
    for i in range(0, len(items), DESC_BATCH_SIZE):
        batch = {}
        for pc, prod in items[i:i + DESC_BATCH_SIZE]:
            sku = prod.get("product_code", pc)

            # Steel City API hierarchy (category path)
            api_data = full_api.get(pc) or full_api.get(sku) or {}
            category_path = api_data.get("category_names", "")  # e.g. "Clarke,Switches & Electrial Parts,On Off Switches"

            # Alt items from enrichment
            alt_items_raw = prod.get("alt_items", [])
            alt_items = []
            for alt in alt_items_raw:
                if isinstance(alt, dict):
                    alt_items.append({
                        "name": alt.get("name", ""),
                        "product_code": alt.get("product_code", ""),
                    })

            # Compatibility data from schematics
            compat = compat_map.get(sku) or compat_map.get(pc) or {}

            # Cross-reference data (ezvacuum or eBay)
            xref = crossref_data.get(pc, {})
            ezvac_data = xref.get("ezvac", {})
            ebay_data = xref.get("ebay", {})

            batch[pc] = {
                "sku": sku,
                "raw_name": prod.get("name", ""),
                "brand": prod.get("manufacturer", ""),
                "dealer_cost": prod.get("price", ""),
                # Steel City hierarchy
                "steel_city_category_path": category_path,
                "steel_city_description": api_data.get("description", prod.get("description", "")),
                "steel_city_product_code2": api_data.get("product_code2", ""),
                # Compatibility (from schematics — brand → [model list])
                "compatible_models": compat,
                # Alt/replacement items
                "alt_items": alt_items,
                # ezvacuum cross-reference (for factual reference ONLY — do NOT copy)
                "reference_title": ezvac_data.get("title", ""),
                "reference_description": ezvac_data.get("body_html", ""),
                "reference_vendor": ezvac_data.get("vendor", ""),
                "reference_product_type": ezvac_data.get("product_type", ""),
                "reference_tags": ezvac_data.get("tags", ""),
            }

        batch_path = DESC_BATCH_DIR / f"batch_{batch_num}.json"
        with open(batch_path, "w") as f:
            json.dump(batch, f, indent=2)
        batch_num += 1

    # Write the agent instructions file
    instructions_path = DESC_BATCH_DIR / "AGENT_INSTRUCTIONS.md"
    instructions = """# Claude Agent Instructions — Product Name & Description Writing

## CRITICAL: Quality Is Everything
These names and descriptions are what customers see when deciding to buy. They must be:
- Written like a professional e-commerce copywriter
- Completely free of warehouse jargon, internal codes, and abbreviations
- Unique to each product — NO cookie-cutter templates or repetitive phrasing
- SEO-optimized with natural keyword usage
- Ready to publish on Shopify with zero editing needed

## Your Task
For each product in the batch JSON, write:
1. **`clean_name`** — A Shopify-ready product title (max 120 chars)
2. **`description`** — SEO-optimized HTML description for Shopify `body_html`

## Output Format
Write a JSON file named `batch_N_output.json` with this structure:
```json
{
  "PRODUCT_KEY": {
    "clean_name": "Your Shopify Title Here",
    "description": "<p>Your HTML description here...</p>"
  }
}
```

## Rules for `clean_name` (Product Title)
- **Consumer-friendly language ONLY** — write what a shopper would search for on Google
- Format: `[Brand] [Part Type] [Key Detail] - [Fits Model Info]`
- Good examples:
  - `Hoover WindTunnel Replacement Belt - 2 Pack`
  - `Miele S7000 Series HEPA Exhaust Filter`
  - `Dyson DC07 Brushroll Assembly - Genuine OEM`
  - `Kenmore Progressive HEPA Vacuum Bags - 6 Pack`
- Expand ALL abbreviations: ASSY→Assembly, MTR→Motor, VAC→Vacuum, UPRT→Upright, BLK→Black, WHT→White, GRN→Green, GRY→Gray, HNDL→Handle, BRG→Bearing, CLNR→Cleaner, CLR→Clear, DIAM→Diameter, COMMERICIAL→Commercial
- Remove ALL of these: NLA notices, "USE ALT", "REPLACES #xxx", "SAME AS", "OEM" (the label), "SPARE PARTS", "DO NOT USE", "PLEASE USE", "DISCONTINUED", internal part cross-references
- Remove warehouse codes and internal numbering (e.g., "32-0000-02:", "107:")
- Title Case: capitalize main words, lowercase: a, an, the, and, or, for, of, with, in, on, to, at, by
- Keep model numbers and acronyms UPPERCASE (e.g., XL, S7260, CT700, HEPA, LED, UV)
- Max 120 characters — if too long, cut at a natural break point (prefer cutting model lists over part description)

## Rules for `description` (HTML Body)

### MUST INCLUDE:
- **The SKU/part number** — e.g., "Part #1405496510" or "Part number 38528034"
- **ALL compatible models from ALL data sources** — merge `compatible_models`, `reference_description`, and `reference_tags`. List EVERY model number you can find. This is critical for SEO and for customers finding the right part.

### WRITING STYLE:
- Write 80-150 words of **benefit-driven, specific copy** unique to THIS product
- Address the customer's problem: why are they searching for this part? What broke? What will this fix?
- Be specific about what the part does — not generic filler
- Vary your sentence structure and vocabulary between products. If you wrote "Restores peak performance" for the last product, use different language for this one
- Write for a regular person — homeowner, not a technician. Explain what things are if they're not obvious
- DO NOT use these overused phrases: "Restores your vacuum to optimal working condition", "factory-spec fit", "compatible with select vacuum models", "for effective cleaning performance"

### FORMATTING:
- Use `<p>` tags for paragraphs (1-2 paragraphs of descriptive copy)
- Use `<ul><li>` for compatibility lists when there are 3+ models
- Group compatible models by brand in the list
- Structure: (1) What it is + why you need it + what it fixes, (2) Part number, (3) Full compatibility list

### ORIGINALITY REQUIREMENT:
- The `reference_description` and `reference_tags` fields contain data from a competitor store. Use this ONLY to extract factual information (model numbers, compatibility, product specs).
- **DO NOT copy, paraphrase, or closely mirror the competitor's description.** Write completely original copy in your own voice. The descriptions must be distinct enough that no one would think they came from the same source.
- Extract model numbers and compatibility data from reference fields — that's factual info, not copyrightable. But the actual description prose must be 100% yours.

### DECODING THE RAW DATA:
- `raw_name` is Steel City's internal product name — it's full of abbreviations and jargon. Decode it:
  - "BELT,HOOVER UPRT VAC" → This is a belt for Hoover upright vacuums
  - "SWITCH,NILFISK COMMERCIAL BACK PACK" → This is an on/off switch for Nilfisk commercial backpack vacuums
  - "BAG,PAPER,MIELE S227-S282,5PK" → These are paper vacuum bags for Miele S227-S282, sold in a 5-pack
- `steel_city_category_path` shows the hierarchy, e.g., "Hoover,Belts - Flat, Round, V, Grooved" means this is a belt in the Hoover section
- Text after `<br />` in raw names usually contains "FITS [model list]" info

## Data Fields Available Per Product
- `sku` — The product's part number / SKU (**MUST appear in description**)
- `raw_name` — Steel City's internal name (abbreviated, jargon-heavy — decode it)
- `brand` — Manufacturer(s), comma-separated
- `dealer_cost` — Our wholesale cost (**DO NOT include in description**)
- `steel_city_category_path` — Category hierarchy (e.g., "Miele,Bags - Paper, Cloth, & Parts")
- `steel_city_description` — Raw API description (may duplicate raw_name)
- `compatible_models` — Dict of {brand: [model1, model2, ...]} from schematics
- `alt_items` — Replacement/alternative product codes
- `reference_title` — Competitor product title (for factual reference only — model numbers, specs)
- `reference_description` — Competitor HTML description (**extract facts, DO NOT copy prose**)
- `reference_vendor` — Competitor's listed brand
- `reference_product_type` — Product category from competitor
- `reference_tags` — Competitor tags (often contain additional model numbers — extract them)

## Examples of GOOD Descriptions

**Example 1 — Belt with model list:**
```html
<p>When your Hoover WindTunnel stops agitating carpet or leaves debris behind, a stretched or broken belt is almost always the cause. This flat drive belt connects the motor to the brush roll, keeping it spinning at the right speed for deep carpet cleaning. A quick 5-minute swap brings back the cleaning power you remember. Part #38528034.</p>
<p><strong>Fits these Hoover models:</strong></p>
<ul>
<li>WindTunnel T-Series: UH70100, UH70105, UH70106, UH70107, UH70110</li>
<li>WindTunnel 2: UH70800, UH70801, UH70805, UH70810, UH70811</li>
<li>Rewind: UH71013</li>
</ul>
```

**Example 2 — Filter with minimal data:**
```html
<p>Keep your Miele vacuum's air filtration system working at full capacity with this replacement exhaust filter. Positioned after the motor, it catches fine particles that slip past the bag, so the air leaving your vacuum is as clean as possible. Especially important for allergy sufferers and pet owners who need the cleanest possible indoor air. Part #SF-SAC20/30.</p>
<p><strong>Compatible with:</strong></p>
<ul>
<li>Miele S300–S899 Series canister vacuums</li>
<li>Miele S7000 Series upright vacuums</li>
</ul>
```

**Example 3 — Switch with no competitor data:**
```html
<p>If your Clarke or Advance commercial vacuum won't turn on — or turns off unexpectedly mid-job — this replacement on/off switch is the fix. Designed for heavy-duty commercial backpack and upright models, it handles the demands of daily professional use. Part #1405496510.</p>
<p><strong>Compatible with:</strong></p>
<ul>
<li>Advance: Comfort Pak 6, Comfort Pak 10, Adgility 6XP</li>
<li>Clarke: CarpetMaster 112, 115, 212, 215, 218</li>
<li>Nilfisk: GD 5 Back, GD 10 Back</li>
<li>Kent: UZ 934, UZ 964</li>
</ul>
```
"""
    with open(instructions_path, "w") as f:
        f.write(instructions)

    progress["names_batched"] = True
    save_progress(progress)
    log.info(f"\nCreated {batch_num} batches of ~{DESC_BATCH_SIZE} products in {DESC_BATCH_DIR}/")
    log.info(f"Agent instructions written to {instructions_path}")
    log.info(f"\n*** NEXT STEP: Run Claude agents on each batch, then run --step names-merge ***")


def step_names_merge(progress):
    """Merge Claude agent outputs back into progress filtered data."""
    log.info("=" * 60)
    log.info("STEP 7b: Merge Agent-Written Names + Descriptions")
    log.info("=" * 60)

    filtered = progress["filtered"]
    merged = 0
    missing = 0

    if not DESC_BATCH_DIR.exists():
        log.error(f"{DESC_BATCH_DIR} not found. Run --step names first.")
        return

    for fname in sorted(os.listdir(DESC_BATCH_DIR)):
        if not fname.startswith("batch_") or not fname.endswith("_output.json"):
            continue
        path = DESC_BATCH_DIR / fname
        with open(path) as f:
            results = json.load(f)

        for key, data in results.items():
            if key in filtered:
                if isinstance(data, dict):
                    filtered[key]["clean_name"] = data.get("clean_name", "")
                    filtered[key]["description_generated"] = data.get("description", "")
                elif isinstance(data, str):
                    # Backwards compat: old format was just the description string
                    filtered[key]["description_generated"] = data
                merged += 1
            else:
                missing += 1

    progress["names_merged"] = True
    save_progress(progress)
    log.info(f"Merged {merged} agent descriptions ({missing} keys not found in filtered)")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7: Pricing
# ══════════════════════════════════════════════════════════════════════════════

def step_pricing(progress):
    """Apply tiered markup + competitor pricing to filtered products."""
    log.info("=" * 60)
    log.info("STEP 9: Pricing (tiered markup + competitor)")
    log.info("=" * 60)

    filtered = progress["filtered"]
    already_priced = set(progress["priced"])

    # Load competitor prices
    competitor_prices = {}
    if COMPETITOR_PRICES_FILE.exists():
        with open(COMPETITOR_PRICES_FILE) as f:
            comp_data = json.load(f)
        competitor_prices = comp_data.get("prices", {})
        log.info(f"Competitor price data: {len(competitor_prices)} SKUs")

    # Load price locks
    price_locks = set()
    if PRICE_LOCKS_FILE.exists():
        locks_data = json.loads(PRICE_LOCKS_FILE.read_text())
        price_locks = {k for k in locks_data.keys() if not k.startswith("_")}

    priced = 0
    skipped = 0
    methods = {"markup": 0, "competitor": 0, "map_locked": 0}

    for pc, prod in filtered.items():
        if pc in already_priced:
            continue

        dealer_cost = parse_price(prod.get("price"))
        if not dealer_cost:
            skipped += 1
            continue

        product_code = prod.get("product_code", pc)

        if product_code in price_locks:
            methods["map_locked"] += 1
            progress["priced"].append(pc)
            continue

        retail, method = get_best_price(product_code, dealer_cost, competitor_prices)
        prod["retail_price"] = f"${retail:.2f}"
        methods[method] = methods.get(method, 0) + 1
        progress["priced"].append(pc)
        priced += 1

    save_progress(progress)
    log.info(f"Priced: {priced}, Skipped (no cost): {skipped}")
    log.info(f"Methods: {methods}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 8: Merge + Export
# ══════════════════════════════════════════════════════════════════════════════

def step_merge(progress):
    """Merge new products into product_names.json and export spreadsheet."""
    log.info("=" * 60)
    log.info("STEP 10: Merge + Export")
    log.info("=" * 60)

    if PRODUCTS_FILE.exists():
        with open(PRODUCTS_FILE) as f:
            products = json.load(f)
    else:
        products = {}

    existing_codes = set(products.keys())
    for prod in products.values():
        existing_codes.add(prod.get("sku", ""))

    filtered = progress["filtered"]
    processed_set = set(progress["processed"])

    added = 0
    skipped_dup = 0
    skipped_no_image = 0

    for pc, prod in filtered.items():
        # Only merge products that have processed images
        if pc not in processed_set:
            skipped_no_image += 1
            continue

        product_code = prod.get("product_code", pc)
        if pc in existing_codes or product_code in existing_codes:
            skipped_dup += 1
            continue

        price = prod.get("price", "")
        if not price.startswith("$") and price:
            try:
                price = f"${float(price):.2f}"
            except (ValueError, TypeError):
                pass

        products[product_code] = {
            "raw_description": prod.get("name", ""),
            "raw_name": prod.get("name", ""),
            "clean_name": prod.get("clean_name", product_code),
            "brand": prod.get("manufacturer", ""),
            "sku": product_code,
            "price": price,
            "model": "",
            "source": prod.get("source", "discovery_v2"),
            "description": prod.get("description_generated", ""),
            "in_stock": prod.get("in_stock", "1"),
            "retail_price": prod.get("retail_price", ""),
            "qty_on_hand": prod.get("qtyoh", 0),
        }
        existing_codes.add(product_code)
        existing_codes.add(pc)
        added += 1

    log.info(f"Added: {added}")
    log.info(f"Skipped (duplicate): {skipped_dup}")
    log.info(f"Skipped (no image): {skipped_no_image}")
    log.info(f"Total products: {len(products)}")

    with open(PRODUCTS_FILE, "w") as f:
        json.dump(products, f, indent=2)

    # Export spreadsheet
    OUTPUT_DIR.mkdir(exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Products"
    headers = ["SKU", "Brand", "Model", "Clean Name", "Description", "Price",
               "Retail Price", "In Stock", "Raw Name"]
    ws.append(headers)
    for key, p in products.items():
        ws.append([
            p.get("sku", key),
            p.get("brand", ""),
            p.get("model", ""),
            p.get("clean_name", ""),
            p.get("description", ""),
            p.get("price", ""),
            p.get("retail_price", ""),
            p.get("in_stock", ""),
            p.get("raw_name", ""),
        ])
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)

    out_path = OUTPUT_DIR / "product_descriptions.xlsx"
    wb.save(str(out_path))
    log.info(f"Spreadsheet exported to {out_path}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 9: Report
# ══════════════════════════════════════════════════════════════════════════════

def step_report(progress):
    log.info("=" * 60)
    log.info("PRODUCT DISCOVERY V2 REPORT")
    log.info("=" * 60)
    log.info(f"Search queries completed: {len(progress['search_completed'])}")
    log.info(f"Total products discovered: {len(progress['all_products'])}")
    log.info(f"Products enriched: {len(progress['enriched'])}")
    log.info(f"Enrich errors: {len(progress['enrich_errors'])}")
    log.info(f"Stock checked: {len(progress['stock_checked'])}")

    stock = progress["stock_checked"]
    in_stock = sum(1 for v in stock.values() if v is not None and v > 0)
    oos = sum(1 for v in stock.values() if v is not None and v == 0)
    failed = sum(1 for v in stock.values() if v is None)
    log.info(f"  Verified in stock: {in_stock}")
    log.info(f"  Verified OOS: {oos}")
    log.info(f"  Stock check failed: {failed}")

    log.info(f"Products passing filter: {len(progress['filtered'])}")
    log.info(f"Alt items traced: {len(progress['alt_traced'])}")
    log.info(f"Images downloaded: {len(progress['downloaded'])}")
    log.info(f"Images processed: {len(progress['processed'])}")
    log.info(f"Products priced: {len(progress['priced'])}")
    log.info(f"Failures: {len(progress['failed'])}")

    if PRODUCTS_FILE.exists():
        with open(PRODUCTS_FILE) as f:
            products = json.load(f)
        log.info(f"\nFinal product_names.json: {len(products)} products")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Full Product Discovery v2")
    parser.add_argument("--step", choices=[
        "discover", "enrich", "stock", "filter", "images", "crossref",
        "names", "names-merge", "pricing", "merge", "report"
    ], default=None, help="Run a specific step only")
    args = parser.parse_args()

    progress = load_progress()
    needs_browser = args.step in ("discover", "enrich", "stock", "crossref", None)
    driver = None

    try:
        if needs_browser:
            driver = create_driver()
            login(driver)

        if args.step == "discover" or args.step is None:
            step_discover(driver, progress)

        if args.step == "enrich" or args.step is None:
            step_enrich(driver, progress)

        if args.step == "stock" or args.step is None:
            step_stock(driver, progress)

        if args.step == "filter" or args.step is None:
            step_filter(progress)

        if args.step == "images" or args.step is None:
            step_download(progress)
            step_process_images(progress)

        if args.step == "crossref" or args.step is None:
            step_crossref(driver, progress)

        if args.step == "names" or args.step is None:
            step_names(progress)
            # Full pipeline stops here — agents must process batches before continuing
            if args.step is None:
                log.info("\n" + "=" * 60)
                log.info("PIPELINE PAUSED — Agent work required")
                log.info("=" * 60)
                log.info(f"Batch files written to {DESC_BATCH_DIR}/")
                log.info("Run Claude agents on each batch_N.json → batch_N_output.json")
                log.info("Then resume with: python3 product_discovery_v2.py --step names-merge")
                return

        if args.step == "names-merge":
            step_names_merge(progress)

        if args.step == "pricing" or (args.step == "names-merge"):
            step_pricing(progress)

        if args.step == "merge" or (args.step == "names-merge"):
            step_merge(progress)

        if args.step == "report" or (args.step == "names-merge"):
            step_report(progress)

    except KeyboardInterrupt:
        save_progress(progress)
        log.info("\nInterrupted — progress saved. Re-run to resume.")
    except Exception as e:
        save_progress(progress)
        log.error(f"Error: {e}", exc_info=True)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
