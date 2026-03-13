#!/usr/bin/env python3
"""
full_discovery.py — Comprehensive Steel City product discovery pipeline.

Enumerates ALL products from the Steel City API using broad search queries
with full pagination, then runs the complete pipeline:
  1. Search Discovery — a-z, 0-9 queries with pagination to find ALL products
  2. Enrich — product_info API (batched) for full details: pictures, alts, manufacturer
  3. Filter — in_stock=1 with pictures, plus NLA/out-of-stock with alts traced
  4. Download — Raw product images via HTTP
  5. Process — Background removal + VaM watermark + pad to 2048x2048
  6. Clean Names + Descriptions — Shopify-ready titles + SEO descriptions
  7. Merge + Export — Update product_names.json + spreadsheet (no duplicates)

Usage:
  python3 full_discovery.py                      # Full pipeline
  python3 full_discovery.py --step discover      # Only search API discovery
  python3 full_discovery.py --step enrich        # Only enrich new products
  python3 full_discovery.py --step filter        # Only filter + trace alts
  python3 full_discovery.py --step images        # Only download + process images
  python3 full_discovery.py --step names         # Only clean names + descriptions
  python3 full_discovery.py --step merge         # Only merge + export spreadsheet
  python3 full_discovery.py --step report        # Report stats (no browser)
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

import numpy as np
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
    "schematics_url": "https://www.steelcityvac.com/a/g/?t=1&gid=1&folder=",
    "account": "REDACTED_ACCT",
    "user_id": "REDACTED_USER",
    "password": "REDACTED_PASS",
}

BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "images"
IMAGES_RAW_DIR = BASE_DIR / "images_raw"
OUTPUT_DIR = BASE_DIR / "output"
PROGRESS_FILE = BASE_DIR / "full_discovery_progress.json"
PRODUCTS_FILE = BASE_DIR / "product_names.json"
LOGO_PATH = BASE_DIR / "VaM Watermark.png"

IMAGE_BASE_URL = "https://www.steelcityvac.com/uploads/applications/shopping_cart/"
TARGET_SIZE = 2048
PRODUCT_MAX_SIZE = 1800
JPEG_QUALITY = 85
LOGO_OPACITY = 0.20
BATCH_SIZE = 10  # Concurrent API calls

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
        "search_completed": [],     # Search queries that are done
        "all_products": {},         # product_code → basic search data
        "enriched": {},             # product_code → full API data
        "enrich_errors": [],
        "filtered": {},             # product_code → final product data (passes filters)
        "downloaded": [],
        "processed": [],
        "failed": {},
        "alt_traced": {},           # product_code → alt product_code it points to
    }

def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


# ── Browser helpers ─────────────────────────────────────────────────────────

def create_driver():
    import undetected_chromedriver as uc
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
    raise TimeoutError("Cloudflare challenge not resolved.")


def login(driver):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

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


def navigate_to_site(driver):
    """Ensure we're on Steel City's domain for AJAX calls."""
    driver.get(CONFIG["schematics_url"])
    time.sleep(3)
    wait_for_cloudflare(driver)


# ── API Calls ───────────────────────────────────────────────────────────────

def api_search(driver, query, take=500, page=1):
    """Search API with pagination. Returns {results: [...], total: N}."""
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
        log.debug(f"Batch API failed: {e}")
    return {pid: None for pid in part_ids}


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: Search Discovery
# ══════════════════════════════════════════════════════════════════════════════

def step_discover(driver, progress):
    """Use search API with broad queries + full pagination to find ALL products."""
    log.info("=" * 60)
    log.info("STEP 1: Search Discovery (broad queries + pagination)")
    log.info("=" * 60)

    navigate_to_site(driver)

    # Search queries: single characters a-z, 0-9, plus some broader terms
    queries = list("abcdefghijklmnopqrstuvwxyz0123456789")
    # Add two-char combos for any single chars that had total > take
    # (we'll check after first pass)

    already_done = set(progress["search_completed"])
    all_products = progress["all_products"]
    initial_count = len(all_products)

    for query in queries:
        if query in already_done:
            continue

        page = 1
        query_total = 0
        query_new = 0

        while True:
            data = api_search(driver, query, take=500, page=page)
            if not data:
                break

            total = int(data.get("total", 0))
            results = data.get("results", [])

            if not results:
                break

            for item in results:
                # item keys: "0"=internal_id, "1"=productID, "2"=product_code,
                #            "3"=name, "5"=price, "7"=in_stock
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

                query_total += 1

            # Check if we need more pages
            if page * 500 >= total or len(results) < 500:
                break
            page += 1
            time.sleep(random.uniform(0.2, 0.4))

        progress["search_completed"].append(query)
        save_progress(progress)
        log.info(f"  Query '{query}': total={query_total}, new={query_new}, "
                 f"pages={page}, catalog={len(all_products)}")

        time.sleep(random.uniform(0.3, 0.6))

    new_found = len(all_products) - initial_count
    log.info(f"\nDiscovery complete. Total unique products: {len(all_products)} "
             f"(+{new_found} new)")
    save_progress(progress)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: Enrich
# ══════════════════════════════════════════════════════════════════════════════

def step_enrich(driver, progress):
    """Call product_info API for all discovered products to get full details."""
    log.info("=" * 60)
    log.info("STEP 2: Enriching products via product_info API")
    log.info("=" * 60)

    navigate_to_site(driver)

    all_products = progress["all_products"]
    enriched = progress["enriched"]

    # Only enrich products we haven't already
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
            if not data or not data.get("name"):
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
            }

        batch_num += 1
        if batch_num % 20 == 0:
            save_progress(progress)
            done = len(enriched)
            total = len(all_products)
            log.info(f"  Progress: {done}/{total} ({100*done//total}%)")

        time.sleep(random.uniform(0.2, 0.5))

    save_progress(progress)
    log.info(f"Enrichment complete. Enriched: {len(enriched)}, "
             f"Errors: {len(progress['enrich_errors'])}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: Filter + Alt Tracing
# ══════════════════════════════════════════════════════════════════════════════

def step_filter(progress):
    """Filter to in-stock products with pictures. Trace alt items for OOS/NLA."""
    log.info("=" * 60)
    log.info("STEP 3: Filtering + Alt Tracing")
    log.info("=" * 60)

    enriched = progress["enriched"]
    filtered = {}
    alt_traced = {}

    # Load existing products to avoid duplicates
    existing_codes = set()
    if PRODUCTS_FILE.exists():
        with open(PRODUCTS_FILE) as f:
            existing = json.load(f)
        for key, prod in existing.items():
            existing_codes.add(key)
            existing_codes.add(prod.get("sku", ""))
    log.info(f"Existing products to deduplicate against: {len(existing_codes)}")

    stats = {
        "total_enriched": 0,
        "no_data": 0,
        "already_have": 0,
        "in_stock_with_pic": 0,
        "in_stock_no_pic": 0,
        "oos_with_alt": 0,
        "oos_no_alt": 0,
        "nla_with_alt": 0,
        "nla_no_alt": 0,
        "special_order": 0,
    }

    for pc, data in enriched.items():
        stats["total_enriched"] += 1

        if data is None:
            stats["no_data"] += 1
            continue

        product_code = data.get("product_code", pc)

        # Deduplicate
        if pc in existing_codes or product_code in existing_codes:
            stats["already_have"] += 1
            continue

        in_stock = str(data.get("in_stock", ""))
        name = data.get("name", "")
        description = data.get("description", "")
        picture = data.get("picture", "")
        big_picture = data.get("big_picture", "")
        alt_items = data.get("alt_items", [])

        has_picture = bool(picture and picture != "0") or bool(big_picture and big_picture != "0")
        has_alt = bool(alt_items and isinstance(alt_items, list) and len(alt_items) > 0)

        # Detect NLA
        is_nla = False
        for field in [name, description]:
            if field and ("NLA" in field.upper() or "NO LONGER AVAILABLE" in field.upper()):
                is_nla = True
                break

        # Detect special order text (rare but check)
        is_special_order = False
        for field in [name, description]:
            if field and "SPECIAL ORDER" in field.upper():
                is_special_order = True
                break

        # ── Decision logic ──
        if in_stock == "1" and has_picture:
            # In stock with picture → KEEP
            stats["in_stock_with_pic"] += 1
            img_filename = (big_picture or picture or "").strip()
            image_url = IMAGE_BASE_URL + img_filename if img_filename and img_filename != "0" else ""

            # Format price
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
                "in_stock": in_stock,
                "image_url": image_url,
                "manufacturer": data.get("manufacturer", ""),
                "alt_items": alt_items,
                "source": "full_discovery",
            }
            # Also add the search-key mapping
            if pc != product_code:
                existing_codes.add(product_code)
            existing_codes.add(pc)

        elif in_stock == "1" and not has_picture:
            stats["in_stock_no_pic"] += 1

        elif is_nla and has_alt:
            # NLA but has alt → trace the alt and keep if alt is in stock with pic
            stats["nla_with_alt"] += 1
            for alt in alt_items:
                if isinstance(alt, dict) and alt.get("product_code"):
                    alt_code = alt["product_code"]
                    alt_traced[pc] = alt_code

        elif is_nla and not has_alt:
            stats["nla_no_alt"] += 1

        elif in_stock == "0" and has_alt:
            # Out of stock (special order) with alt → trace
            stats["oos_with_alt"] += 1
            for alt in alt_items:
                if isinstance(alt, dict) and alt.get("product_code"):
                    alt_code = alt["product_code"]
                    alt_traced[pc] = alt_code

        elif in_stock == "0" and not has_alt:
            if is_special_order:
                stats["special_order"] += 1
            else:
                stats["oos_no_alt"] += 1

        else:
            stats["oos_no_alt"] += 1

    # ── Trace alts: check if the alt products are already in our filtered set or enriched ──
    log.info(f"\nTracing {len(alt_traced)} alt items...")
    alt_additions = 0
    for original_pc, alt_code in alt_traced.items():
        # Skip if alt is already in filtered or existing
        if alt_code in filtered or alt_code in existing_codes:
            continue

        # Check if alt was enriched
        alt_data = enriched.get(alt_code)
        if alt_data and alt_data.get("in_stock") == "1":
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
                    "image_url": image_url,
                    "manufacturer": alt_data.get("manufacturer", ""),
                    "alt_items": alt_data.get("alt_items", []),
                    "source": "full_discovery_alt_trace",
                    "alt_for": original_pc,
                }
                existing_codes.add(alt_product_code)
                alt_additions += 1

    progress["filtered"] = filtered
    progress["alt_traced"] = alt_traced
    save_progress(progress)

    log.info(f"\nFilter Results:")
    for k, v in sorted(stats.items()):
        log.info(f"  {k:25s}: {v}")
    log.info(f"  alt_traced_additions    : {alt_additions}")
    log.info(f"\n  TOTAL NEW PRODUCTS: {len(filtered)}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: Download Images
# ══════════════════════════════════════════════════════════════════════════════

def step_download(progress):
    """Download raw images for filtered products."""
    log.info("=" * 60)
    log.info("STEP 4: Downloading images")
    log.info("=" * 60)

    IMAGES_RAW_DIR.mkdir(exist_ok=True)
    filtered = progress["filtered"]
    already_downloaded = set(progress["downloaded"])

    to_download = []
    for pc, prod in filtered.items():
        if pc in already_downloaded:
            continue
        url = prod.get("image_url", "")
        if url:
            to_download.append((pc, url))

    log.info(f"To download: {len(to_download)} (already done: {len(already_downloaded)})")

    downloaded = 0
    for i, (pc, url) in enumerate(to_download):
        folder_name = re.sub(r'[^\w\-]', '_', pc)
        raw_path = IMAGES_RAW_DIR / f"{folder_name}.jpg"

        if raw_path.exists():
            progress["downloaded"].append(pc)
            downloaded += 1
            continue

        try:
            resp = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
            })
            resp.raise_for_status()
            if "image" not in resp.headers.get("Content-Type", ""):
                progress["failed"][pc] = "not_image"
                continue

            img = Image.open(BytesIO(resp.content))
            if img.width < 50 or img.height < 50:
                progress["failed"][pc] = "too_small"
                continue

            with open(raw_path, "wb") as f:
                f.write(resp.content)
            progress["downloaded"].append(pc)
            downloaded += 1
        except Exception as e:
            progress["failed"][pc] = f"download: {str(e)[:80]}"

        if (i + 1) % 50 == 0:
            save_progress(progress)
            log.info(f"  [{i+1}/{len(to_download)}] Downloaded: {downloaded}")

        time.sleep(random.uniform(0.05, 0.15))

    save_progress(progress)
    log.info(f"Download complete. Downloaded: {downloaded}, "
             f"Failed: {len(progress['failed'])}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: Process Images
# ══════════════════════════════════════════════════════════════════════════════

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
    log.info("STEP 5: Processing images (bg removal + logo + pad)")
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
# STEP 6: Clean Names + Descriptions
# ══════════════════════════════════════════════════════════════════════════════

# ── Name cleaning (from clean_names.py) ──

ABBREVIATIONS = {
    'ASSY': 'Assembly', 'ASSEM': 'Assembly', 'ASSEMBLE': 'Assembly',
    'ASY': 'Assembly', 'MTR': 'Motor', 'BRG': 'Bearing',
    'BLK': 'Black', 'WHT': 'White', 'GRN': 'Green', 'BLU': 'Blue',
    'GRY': 'Gray', 'SLVR': 'Silver', 'CLR': 'Clear', 'ORG': 'Orange',
    'PNK': 'Pink', 'PUR': 'Purple', 'YEL': 'Yellow', 'BRN': 'Brown',
    'REFL': 'Reflector', 'HNDL': 'Handle', 'VAC': 'Vacuum',
    'UPRT': 'Upright', 'UPRI': 'Upright', 'CLNR': 'Cleaner',
    'DIAM': 'Diameter', 'SQ': 'Square', 'PR': 'Pair',
    'COMMERICIAL': 'Commercial', 'CLOTHBAG': 'Cloth Bag',
}

KEEP_UPPER = {
    'HEPA', 'LED', 'UV', 'AC', 'DC', 'USA', 'XL', 'II', 'III', 'IV',
    'V', 'VI', 'VII', 'VIII', 'IX', 'RH', 'LH', 'PWR',
}


def is_model_number(word):
    clean = word.strip(',-/()')
    if not clean:
        return False
    has_digit = bool(re.search(r'\d', clean))
    has_alpha = bool(re.search(r'[A-Za-z]', clean))
    if has_digit and has_alpha and len(clean) >= 3:
        return True
    if clean.isdigit() and len(clean) >= 4:
        return True
    return False


def restructure_name(text):
    text = text.strip(' ,-/')
    if not text:
        return ""
    if ',' not in text and '-' not in text:
        return text
    parts = [p.strip() for p in text.split(',') if p.strip()]
    if len(parts) <= 1:
        return text
    product_type = parts[0].strip()
    modifiers = parts[1:]
    short_mods = []
    long_mods = []
    for mod in modifiers:
        mod = mod.strip()
        if mod.lower().startswith('fits'):
            long_mods.append(mod)
        elif is_model_number(mod.split()[0] if mod.split() else mod):
            long_mods.append(mod)
        elif len(mod.split()) <= 2 and not any(c.isdigit() for c in mod):
            short_mods.append(mod)
        else:
            long_mods.append(mod)
    result_parts = []
    if short_mods:
        result_parts.extend(short_mods)
    result_parts.append(product_type)
    result = ' '.join(result_parts)
    if long_mods:
        result += ' - ' + ', '.join(long_mods)
    return result


def smart_title_case(text):
    words = text.split()
    result = []
    small_words = {'a', 'an', 'the', 'and', 'or', 'for', 'of', 'with', 'in', 'on', 'to', 'at', 'by'}
    for i, word in enumerate(words):
        stripped = word.strip(',-/()"\'')
        if not stripped:
            result.append(word)
            continue
        if is_model_number(stripped):
            result.append(word.upper())
            continue
        if stripped.upper() in KEEP_UPPER:
            result.append(word.upper())
            continue
        if re.match(r'^[\d\'"/.]+$', stripped):
            result.append(word)
            continue
        if stripped.lower() in small_words and i > 0:
            prev = words[i - 1] if i > 0 else ''
            if not prev.endswith('-'):
                result.append(word.lower())
                continue
        result.append(word.capitalize())
    return ' '.join(result)


def clean_product_name(raw_name, brand, part_number):
    name = raw_name
    if not name:
        return f"{brand} Replacement Part" if brand else "Replacement Part"

    first_brand = brand.split(",")[0].strip() if brand else ""
    all_brands = [b.strip() for b in brand.split(",") if b.strip()] if brand else []

    name = re.sub(r'<br\s*/?>', ' | ', name, flags=re.IGNORECASE)
    name = re.sub(r'<[^>]+>', '', name)
    name = re.sub(r'\bNLA\b[\s\d/ -]*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'PLEASE USE ALT.*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'DO NOT USE.*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'USE ALT\s*#?\s*.*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\bDISCONTINUED\b[^|]*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\bOEM\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\b(REPL|REPLACES?)\s+[\w-]+', '', name, flags=re.IGNORECASE)
    name = re.sub(r'SAME AS\s+[\w-]+', '', name, flags=re.IGNORECASE)
    name = re.sub(r'SPARE PARTS', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\d+[A-Z]?\s*:\s*', '', name)

    for b in all_brands:
        bp = re.escape(b)
        name = re.sub(rf'[-/,]\s*{bp}\b', ',', name, flags=re.IGNORECASE)
        name = re.sub(rf'^\s*{bp}\b\s*[,]?\s*', '', name, flags=re.IGNORECASE)
        name = re.sub(rf'\b{bp}\b\s*,?\s*', '', name, flags=re.IGNORECASE)

    name = re.sub(r'\bW\s*/\s*', 'with ', name, flags=re.IGNORECASE)
    name = re.sub(r'\bW/(?=\w)', 'with ', name, flags=re.IGNORECASE)
    name = re.sub(r'\((\d+)\s*PK\)', r'(\1 Pack)', name, flags=re.IGNORECASE)
    name = re.sub(r'\((\d+)\s*PAK\)', r'(\1 Pack)', name, flags=re.IGNORECASE)
    name = re.sub(r'\b(\d+)\s*PK\b', r'\1 Pack', name, flags=re.IGNORECASE)
    name = re.sub(r'\b(\d+)\s*PAK\b', r'\1 Pack', name, flags=re.IGNORECASE)
    name = re.sub(r'PACKS? OF (\d+)', r'\1 Pack', name, flags=re.IGNORECASE)

    for abbr, expansion in ABBREVIATIONS.items():
        name = re.sub(rf'\b{abbr}\b', expansion, name, flags=re.IGNORECASE)

    name = re.sub(r'\bALSO FITS?\b', 'Fits', name, flags=re.IGNORECASE)

    segments = [s.strip() for s in name.split('|') if s.strip()]
    main = segments[0] if segments else name
    extra = segments[1:] if len(segments) > 1 else []
    main = restructure_name(main)
    if extra:
        extra_clean = [restructure_name(e) for e in extra]
        extra_clean = [e for e in extra_clean if e and len(e) > 2]
        if extra_clean:
            main = main + " - " + ", ".join(extra_clean)
    name = main

    name = smart_title_case(name)

    if first_brand:
        if not name.lower().startswith(first_brand.lower()):
            name = f"{first_brand} {name}"

    name = re.sub(r'\s{2,}', ' ', name)
    name = re.sub(r',\s*,', ',', name)
    name = re.sub(r'\(\s*\)', '', name)
    name = re.sub(r'^\s*[-,/]\s*', '', name)
    name = re.sub(r'\s*[-,/]\s*$', '', name)
    name = re.sub(r'\s*,\s*$', '', name)
    name = re.sub(r'\s+-\s*$', '', name)
    name = name.strip()

    if len(name) > 120:
        for sep in [' - ', ', ']:
            cut = name[:120].rfind(sep)
            if cut > 50:
                name = name[:cut]
                break
        else:
            cut = name[:120].rfind(' ')
            if cut > 50:
                name = name[:cut]
        name = name.strip(' ,-/')

    return name


# ── Description generation (from generate_descriptions.py) ──

CATEGORY_PATTERNS = [
    ("bag_paper", re.compile(r"\bpaper\s*bag", re.I)),
    ("bag_cloth", re.compile(r"\bcloth\s*bag", re.I)),
    ("bag", re.compile(r"\bbags?\b", re.I)),
    ("filter_hepa", re.compile(r"\bhepa\b.*\bfilter\b|\bfilter\b.*\bhepa\b", re.I)),
    ("filter_foam", re.compile(r"\bfoam\b.*\bfilter\b|\bfilter\b.*\bfoam\b", re.I)),
    ("filter_exhaust", re.compile(r"\bexhaust\b.*\bfilter\b|\bfilter\b.*\bexhaust\b", re.I)),
    ("filter_pre_motor", re.compile(r"\bpre[\s-]?motor\b.*\bfilter\b|\bfilter\b.*\bpre[\s-]?motor\b", re.I)),
    ("filter", re.compile(r"\bfilters?\b", re.I)),
    ("belt_geared", re.compile(r"\bgeared\b.*\bbelt\b|\bbelt\b.*\bgeared\b", re.I)),
    ("belt_cogged", re.compile(r"\bcogged\b.*\bbelt\b|\bbelt\b.*\bcogged\b", re.I)),
    ("belt", re.compile(r"\bbelts?\b", re.I)),
    ("carbon_brush", re.compile(r"\bcarbon\s*brush", re.I)),
    ("brush_roll", re.compile(r"\bbrush\s*roll\b|\broller\s*brush\b|\bagitator\b", re.I)),
    ("brush_strip", re.compile(r"\bbrush\s*strip", re.I)),
    ("brush", re.compile(r"\bbrush\b", re.I)),
    ("motor", re.compile(r"\bmotor\b", re.I)),
    ("hose", re.compile(r"\bhose\b", re.I)),
    ("cord", re.compile(r"\bcords?\b|\bpower\s*cord\b", re.I)),
    ("wheel", re.compile(r"\bwheels?\b|\bcastors?\b|\bcasters?\b", re.I)),
    ("axle", re.compile(r"\baxle\b", re.I)),
    ("switch", re.compile(r"\bswitch\b", re.I)),
    ("handle", re.compile(r"\bhandle\b", re.I)),
    ("nozzle", re.compile(r"\bnozzle\b", re.I)),
    ("fan", re.compile(r"\bfan\b", re.I)),
    ("spring", re.compile(r"\bspring\b", re.I)),
    ("hardware", re.compile(r"\bscrews?\b|\bnuts?\b|\bbolts?\b|\brivets?\b|\bwashers?\b|\bhardware\b", re.I)),
    ("dust_cup", re.compile(r"\bdust\s*(cup|bin|container)\b", re.I)),
    ("wand", re.compile(r"\bwands?\b", re.I)),
    ("bearing", re.compile(r"\bbearings?\b", re.I)),
    ("gasket", re.compile(r"\bgaskets?\b|\bseals?\b|\bo[\s-]?ring\b", re.I)),
    ("bumper", re.compile(r"\bbumpers?\b", re.I)),
    ("attachment", re.compile(r"\battachment\b|\bcrevice\s*tool\b|\bupholstery\s*tool\b|\bdusting\s*brush\b|\bfloor\s*tool\b", re.I)),
    ("cover", re.compile(r"\bcover\b|\bplate\b|\bhousing\b|\bshroud\b", re.I)),
    ("pedal", re.compile(r"\bpedal\b", re.I)),
    ("latch", re.compile(r"\blatch\b|\bcatch\b|\bclasp\b|\block\b", re.I)),
    ("valve", re.compile(r"\bvalve\b", re.I)),
    ("power_head", re.compile(r"\bpower\s*(head|nozzle)\b", re.I)),
    ("machine", re.compile(r"\b(vacuum|cleaner|steamer|extractor|spot\s*cleaner)\b(?!.*\b(filter|belt|bag|hose|cord|motor|brush|wheel|switch|handle|nozzle|fan|spring|screw|wand|bearing|gasket|bumper|attachment|cover|plate|pedal|latch|valve)\b)", re.I)),
]


def detect_category(clean_name):
    for cat, pattern in CATEGORY_PATTERNS:
        if pattern.search(clean_name):
            return cat
    return "generic"


def compat_phrase(brand, model):
    parts = []
    if brand:
        parts.append(brand)
    if model:
        parts.append(model)
    if parts:
        return f"compatible with {' '.join(parts)} vacuum models"
    return "compatible with select vacuum models"


def extract_quantity(clean_name):
    m = re.search(r"(\d+)\s*[-]?\s*pack", clean_name, re.I)
    return m.group(1) if m else ""


def extract_length(clean_name):
    m = re.search(r"(\d+['\u2019]\d*[\"'\u2019]*\s*(?:long)?)", clean_name, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"(\d+\s*(?:1/\d+\s*)?(?:inch(?:es)?|feet|foot|ft))", clean_name, re.I)
    return m.group(1).strip() if m else ""


TEMPLATES = {
    "filter_hepa": [
        lambda b, m, **kw: f"Replacement HEPA filter {compat_phrase(b, m)}. Captures fine dust, allergens, and microscopic particles for cleaner air output.",
    ],
    "filter_foam": [
        lambda b, m, **kw: f"Replacement foam filter {compat_phrase(b, m)}. Washable foam design traps fine particles and helps maintain strong suction performance.",
    ],
    "filter_exhaust": [
        lambda b, m, **kw: f"Replacement exhaust filter {compat_phrase(b, m)}. Filters outgoing air to reduce dust recirculation.{' Sold in ' + kw.get('qty','') + '-pack.' if kw.get('qty') else ''}",
    ],
    "filter_pre_motor": [
        lambda b, m, **kw: f"Replacement pre-motor filter {compat_phrase(b, m)}. Protects the motor from dust and debris, helping extend the life of your vacuum.",
    ],
    "filter": [
        lambda b, m, **kw: f"Replacement filter {compat_phrase(b, m)}. Helps maintain optimal suction and air filtration for effective cleaning performance.",
    ],
    "bag_paper": [
        lambda b, m, **kw: f"Replacement paper vacuum bags {compat_phrase(b, m)}. Disposable design for quick, hygienic dust disposal.{' Pack of ' + kw.get('qty','') + '.' if kw.get('qty') else ''}",
    ],
    "bag_cloth": [
        lambda b, m, **kw: f"Reusable cloth vacuum bag {compat_phrase(b, m)}. Durable cloth construction can be emptied and reused, reducing ongoing replacement costs.",
    ],
    "bag": [
        lambda b, m, **kw: f"Replacement vacuum bags {compat_phrase(b, m)}. Designed for a secure fit to maximize dust capture and maintain suction.{' Pack of ' + kw.get('qty','') + '.' if kw.get('qty') else ''}",
    ],
    "belt_geared": [
        lambda b, m, **kw: f"Replacement geared belt {compat_phrase(b, m)}. Geared design provides consistent brush roll speed for reliable carpet agitation.",
    ],
    "belt_cogged": [
        lambda b, m, **kw: f"Replacement cogged belt {compat_phrase(b, m)}. Cogged teeth prevent slipping for consistent brush roll performance.",
    ],
    "belt": [
        lambda b, m, **kw: f"Replacement drive belt {compat_phrase(b, m)}. Restores proper brush roll spin for effective carpet cleaning and debris pickup.",
    ],
    "carbon_brush": [
        lambda b, m, **kw: f"Replacement carbon motor brushes {compat_phrase(b, m)}. Essential for maintaining electrical contact within the motor.{' Pack of ' + kw.get('qty','') + '.' if kw.get('qty') else ''}",
    ],
    "brush_roll": [
        lambda b, m, **kw: f"Replacement brush roll {compat_phrase(b, m)}. Agitates carpet fibers to loosen dirt and debris for deeper cleaning.{' ' + kw.get('length','') + ' length.' if kw.get('length') else ''}",
    ],
    "brush_strip": [
        lambda b, m, **kw: f"Replacement brush strip {compat_phrase(b, m)}. Attaches to the brush roll to sweep and agitate carpet fibers during cleaning.",
    ],
    "brush": [
        lambda b, m, **kw: f"Replacement brush {compat_phrase(b, m)}. Maintains effective sweeping and agitation for thorough cleaning results.",
    ],
    "motor": [
        lambda b, m, **kw: f"Replacement motor assembly {compat_phrase(b, m)}. Restores full suction power and performance to your vacuum.",
    ],
    "hose": [
        lambda b, m, **kw: f"Replacement hose {compat_phrase(b, m)}. Restores strong suction and flexible reach for above-floor cleaning tasks.",
    ],
    "cord": [
        lambda b, m, **kw: f"Replacement power cord {compat_phrase(b, m)}.{' ' + kw.get('length','') + ' length provides' if kw.get('length') else ' Provides'} extended reach for larger cleaning areas.",
    ],
    "wheel": [
        lambda b, m, **kw: f"Replacement wheel {compat_phrase(b, m)}. Restores smooth rolling and easy maneuverability across floors and carpets.",
    ],
    "axle": [
        lambda b, m, **kw: f"Replacement axle {compat_phrase(b, m)}. Ensures smooth, stable wheel rotation for easy vacuum movement.",
    ],
    "switch": [
        lambda b, m, **kw: f"Replacement switch {compat_phrase(b, m)}. Restores reliable on/off or speed control functionality.",
    ],
    "handle": [
        lambda b, m, **kw: f"Replacement handle assembly {compat_phrase(b, m)}. Restores comfortable grip and full control while vacuuming.",
    ],
    "nozzle": [
        lambda b, m, **kw: f"Replacement nozzle assembly {compat_phrase(b, m)}. Provides effective suction contact with floors for optimal dirt pickup.",
    ],
    "fan": [
        lambda b, m, **kw: f"Replacement fan {compat_phrase(b, m)}. Restores proper airflow and suction power for effective cleaning.",
    ],
    "spring": [
        lambda b, m, **kw: f"Replacement spring {compat_phrase(b, m)}. Restores proper tension and mechanical function to your vacuum.",
    ],
    "hardware": [
        lambda b, m, **kw: f"Replacement hardware {compat_phrase(b, m)}. Ensures a secure, factory-spec fit for reliable assembly.{' Pack of ' + kw.get('qty','') + '.' if kw.get('qty') else ''}",
    ],
    "dust_cup": [
        lambda b, m, **kw: f"Replacement dust cup {compat_phrase(b, m)}. Easy-empty design for quick, hygienic disposal of collected dirt and debris.",
    ],
    "wand": [
        lambda b, m, **kw: f"Replacement wand {compat_phrase(b, m)}. Extends your reach for cleaning above-floor surfaces, ceilings, and tight spaces.",
    ],
    "bearing": [
        lambda b, m, **kw: f"Replacement bearing {compat_phrase(b, m)}. Ensures smooth, quiet rotation of moving parts for reliable operation.",
    ],
    "gasket": [
        lambda b, m, **kw: f"Replacement gasket/seal {compat_phrase(b, m)}. Provides an airtight seal to maintain strong suction and prevent air leaks.",
    ],
    "bumper": [
        lambda b, m, **kw: f"Replacement bumper {compat_phrase(b, m)}. Protects furniture and baseboards from scuffs during vacuuming.",
    ],
    "attachment": [
        lambda b, m, **kw: f"Replacement attachment tool {compat_phrase(b, m)}. Extends your vacuum's versatility for upholstery, crevices, and hard-to-reach areas.",
    ],
    "cover": [
        lambda b, m, **kw: f"Replacement cover/plate {compat_phrase(b, m)}. Restores a secure, factory-fit closure for proper vacuum operation.",
    ],
    "pedal": [
        lambda b, m, **kw: f"Replacement pedal {compat_phrase(b, m)}. Restores proper foot-operated control for height adjustment or drive engagement.",
    ],
    "latch": [
        lambda b, m, **kw: f"Replacement latch/catch {compat_phrase(b, m)}. Ensures a secure closure for reliable vacuum operation.",
    ],
    "valve": [
        lambda b, m, **kw: f"Replacement valve {compat_phrase(b, m)}. Restores proper airflow control for consistent suction performance.",
    ],
    "power_head": [
        lambda b, m, **kw: f"Replacement power head {compat_phrase(b, m)}. Features a motorized brush roll for deep carpet cleaning.",
    ],
    "machine": [
        lambda b, m, **kw: f"{b + ' ' if b else ''}{m + ' ' if m else ''}vacuum cleaner. Powerful suction and reliable performance for thorough cleaning.",
    ],
    "generic": [
        lambda b, m, **kw: f"Replacement part {compat_phrase(b, m)}. Restores your vacuum to optimal working condition with a factory-spec fit.",
    ],
}


def generate_description(clean_name, brand, model=""):
    qty = extract_quantity(clean_name)
    length = extract_length(clean_name)
    category = detect_category(clean_name)
    templates = TEMPLATES.get(category, TEMPLATES["generic"])
    template_fn = templates[0]
    desc = template_fn(brand, model, qty=qty, length=length)
    return re.sub(r"  +", " ", desc).strip()


def step_names_and_descriptions(progress):
    """Generate clean names and SEO descriptions for filtered products."""
    log.info("=" * 60)
    log.info("STEP 6: Clean Names + Descriptions")
    log.info("=" * 60)

    filtered = progress["filtered"]
    processed_set = set(progress["processed"])

    # Only generate for products that have processed images
    count = 0
    for pc, prod in filtered.items():
        if pc not in processed_set:
            continue

        raw_name = prod.get("name", "")
        brand = prod.get("manufacturer", "")
        clean_name = clean_product_name(raw_name, brand, pc)
        description = generate_description(clean_name, brand.split(",")[0].strip() if brand else "")

        prod["clean_name"] = clean_name
        prod["description_generated"] = description
        count += 1

    save_progress(progress)
    log.info(f"Generated names + descriptions for {count} products")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7: Merge + Export
# ══════════════════════════════════════════════════════════════════════════════

def step_merge(progress):
    """Merge new products into product_names.json and re-export spreadsheet."""
    log.info("=" * 60)
    log.info("STEP 7: Merge + Export")
    log.info("=" * 60)

    # Load existing products
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
        # Must have processed image
        if pc not in processed_set:
            skipped_no_image += 1
            continue

        product_code = prod.get("product_code", pc)

        # Dedup check
        if pc in existing_codes or product_code in existing_codes:
            skipped_dup += 1
            continue

        # Format price
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
            "source": prod.get("source", "full_discovery"),
            "description": prod.get("description_generated", ""),
            "in_stock": prod.get("in_stock", "1"),
        }
        existing_codes.add(product_code)
        existing_codes.add(pc)
        added += 1

    log.info(f"Added: {added}")
    log.info(f"Skipped (duplicate): {skipped_dup}")
    log.info(f"Skipped (no image): {skipped_no_image}")
    log.info(f"Total products: {len(products)}")

    # Save updated products
    with open(PRODUCTS_FILE, "w") as f:
        json.dump(products, f, indent=2)

    # Export spreadsheet
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

    out_path = OUTPUT_DIR / "product_descriptions.xlsx"
    wb.save(str(out_path))
    log.info(f"Spreadsheet exported to {out_path}")


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def step_report(progress):
    log.info("=" * 60)
    log.info("FULL DISCOVERY REPORT")
    log.info("=" * 60)
    log.info(f"Search queries completed: {len(progress['search_completed'])}")
    log.info(f"Total products discovered: {len(progress['all_products'])}")
    log.info(f"Products enriched: {len(progress['enriched'])}")
    log.info(f"Enrich errors: {len(progress['enrich_errors'])}")
    log.info(f"Products passing filter: {len(progress['filtered'])}")
    log.info(f"Alt items traced: {len(progress['alt_traced'])}")
    log.info(f"Images downloaded: {len(progress['downloaded'])}")
    log.info(f"Images processed: {len(progress['processed'])}")
    log.info(f"Failures: {len(progress['failed'])}")

    if PRODUCTS_FILE.exists():
        with open(PRODUCTS_FILE) as f:
            products = json.load(f)
        log.info(f"\nFinal product_names.json: {len(products)} products")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Full Steel City Product Discovery")
    parser.add_argument("--step", choices=[
        "discover", "enrich", "filter", "images", "names", "merge", "report"
    ], default=None, help="Run a specific step only")
    args = parser.parse_args()

    progress = load_progress()
    needs_browser = args.step in ("discover", "enrich", None)
    driver = None

    try:
        if needs_browser:
            driver = create_driver()
            login(driver)

        if args.step in ("discover", None):
            step_discover(driver, progress)

        if args.step in ("enrich", None):
            step_enrich(driver, progress)

        if args.step in ("filter", None):
            step_filter(progress)

        if args.step in ("images", None):
            step_download(progress)
            step_process_images(progress)

        if args.step in ("names", None):
            step_names_and_descriptions(progress)

        if args.step in ("merge", None):
            step_merge(progress)

        step_report(progress)

    except KeyboardInterrupt:
        log.info("\nInterrupted — progress saved. Re-run to resume.")
        save_progress(progress)
    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
        save_progress(progress)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
