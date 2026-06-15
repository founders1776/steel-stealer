#!/usr/bin/env python3
"""
Desco Vacs dealer-site ingest — scrape descovac.com (CIMcloud/classic-ASP B2B
store) into desco_products.json.

Plain `requests` only. No Selenium, no Cloudflare. Auth mechanics were mapped by
a live login spike and are implemented here exactly as confirmed.

Steps:
  1. login     — authenticate the session against descovac.com
  2. discover  — enumerate category ids from the homepage
  3. enrich    — crawl each category's embedded product JSON (paginated)
  4. export    — write desco_products.json from progress + print a summary
  all          — run every step in order

Usage:
  python3 desco_ingest.py --step all                # full crawl
  python3 desco_ingest.py --step all --limit 30     # cap categories (testing)
  python3 desco_ingest.py --step discover           # just enumerate categories
  python3 desco_ingest.py --step export             # rebuild output from progress
  python3 desco_ingest.py --dry-run                 # crawl but don't write output

Resumable: state lives in desco_ingest_progress.json. Re-running skips
categories already processed. --step export reads progress only (no network).
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests

# ── Config ──────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
PROGRESS_FILE = BASE_DIR / "desco_ingest_progress.json"
OUTPUT_FILE = BASE_DIR / "desco_products.json"

SITE = "https://www.descovac.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

DESCO_EMAIL = os.environ.get("DESCO_EMAIL", "")
DESCO_PASSWORD = os.environ.get("DESCO_PASSWORD", "")

SIGNIN_URL = f"{SITE}/signin.asp"
LOGON_URL = (
    f"{SITE}/security_logonscript_sitefront.asp"
    "?action=logon&parent_c_id=&returnpage=signin%2Easp%3F"
    "&pageredir=%2Fsignin%2Easp"
)
MY_ACCOUNT_URL = f"{SITE}/my_account.asp"
HOME_URL = f"{SITE}/"
CATEGORY_URL = f"{SITE}/pc_combined_results.asp"

# 32-hex category / product ids
CATEGORY_RE = re.compile(r"pc_combined_results\.asp\?pc_id=([0-9A-Fa-f]{32})")

REQUEST_DELAY = 0.4            # seconds between requests
MAX_PAGES_PER_CATEGORY = 20    # defensive pagination cap
SAVE_EVERY_N_CATEGORIES = 25

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("desco")


# ── Progress ────────────────────────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        data = json.loads(PROGRESS_FILE.read_text())
        data.setdefault("category_ids", [])
        data.setdefault("products", {})
        data.setdefault("skipped", [])
        data.setdefault("steps_done", [])
        data.setdefault("processed_categories", [])
        return data
    return {
        "category_ids": [],
        "products": {},
        "skipped": [],
        "steps_done": [],
        "processed_categories": [],
    }


def save_progress(progress):
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


# ── HTTP session ──────────────────────────────────────────────────────────────

def get_session():
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return sess


def fetch(sess, url, method="GET", data=None, retries=4):
    """GET/POST with backoff on transient 429/5xx and connection errors."""
    for attempt in range(retries):
        try:
            if method == "POST":
                resp = sess.post(url, data=data, timeout=30)
            else:
                resp = sess.get(url, timeout=30)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
                log.warning(f"  429 throttled — sleeping {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep((attempt + 1) * 3)
                continue
            resp.raise_for_status()
            return resp
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as exc:
            if attempt < retries - 1:
                time.sleep((attempt + 1) * 3)
                continue
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc
    raise RuntimeError(f"Request failed for {url} after {retries} attempts")


# ── STEP 1: Login ─────────────────────────────────────────────────────────────

def step_login(sess):
    log.info("=" * 60)
    log.info("STEP 1: Login to descovac.com")
    log.info("=" * 60)

    if not DESCO_EMAIL or not DESCO_PASSWORD:
        raise RuntimeError(
            "DESCO_EMAIL and DESCO_PASSWORD must be set in the environment "
            "(export $(grep -E '^DESCO_(EMAIL|PASSWORD)=' .env | xargs))."
        )

    # Init cookies
    fetch(sess, SIGNIN_URL)
    time.sleep(REQUEST_DELAY)

    # Logon POST
    fetch(sess, LOGON_URL, method="POST", data={
        "username": DESCO_EMAIL,
        "password": DESCO_PASSWORD,
        "logontype": "customer",
    })
    time.sleep(REQUEST_DELAY)

    # Verify: my_account.asp returns 200 and is NOT redirected to signin
    resp = fetch(sess, MY_ACCOUNT_URL)
    if resp.status_code != 200 or "signin" in resp.url.lower():
        raise RuntimeError(
            f"Login failed — my_account.asp resolved to {resp.url} "
            f"(status {resp.status_code}). Check DESCO_EMAIL / DESCO_PASSWORD."
        )

    log.info(f"Login OK (account page: {resp.url})")
    return True


# ── STEP 2: Discover categories ───────────────────────────────────────────────

def step_discover(sess, progress, limit=None):
    log.info("=" * 60)
    log.info("STEP 2: Discover category ids from homepage")
    log.info("=" * 60)

    resp = fetch(sess, HOME_URL)
    time.sleep(REQUEST_DELAY)

    # Dedupe preserving first-seen order
    seen = set()
    ids = []
    for match in CATEGORY_RE.finditer(resp.text):
        cid = match.group(1)
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)

    log.info(f"Found {len(ids)} unique category ids")
    if limit:
        ids = ids[:limit]
        log.info(f"Limited to first {len(ids)} categories (--limit {limit})")

    progress["category_ids"] = ids
    if "discover" not in progress["steps_done"]:
        progress["steps_done"].append("discover")
    save_progress(progress)
    return ids


# ── Product JSON extraction ───────────────────────────────────────────────────

def _extract_product_arrays(html):
    """Find every `"products":[ ... ]` block, bracket-balance to its matching
    `]`, json.loads each, and return the list of parsed arrays."""
    arrays = []
    marker = '"products":['
    pos = 0
    while True:
        idx = html.find(marker, pos)
        if idx == -1:
            break
        # Position of the opening '['
        start = idx + len(marker) - 1
        depth = 0
        in_str = False
        escape = False
        end = None
        for i in range(start, len(html)):
            ch = html[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
        if end is None:
            # Unbalanced — give up on this occurrence, move past the marker
            pos = idx + len(marker)
            continue
        raw = html[start:end + 1]
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                arrays.append(arr)
        except (json.JSONDecodeError, ValueError):
            pass
        pos = end + 1
    return arrays


def _largest_array(arrays):
    if not arrays:
        return []
    return max(arrays, key=len)


def _abs_image_url(rel):
    if not rel:
        return None
    if "placeholder" in rel.lower():
        return None
    rel = rel.lstrip("/")
    return f"{SITE}/{rel}"


def _parse_product(obj):
    """Map a Desco product JSON object to our record shape, or return
    (None, reason) if it should be skipped."""
    sku = (obj.get("sku") or "").strip()
    if not sku:
        return None, "no_sku"

    # Dealer cost = uomPrice[0].price; suggested = uomPrice[0].suggestedPrice
    # (there is no top-level suggestedPrice — confirmed against live data).
    dealer_cost = None
    suggested = None
    uom = obj.get("uomPrice")
    if isinstance(uom, list) and uom:
        first = uom[0] or {}
        try:
            dealer_cost = float(first.get("price"))
        except (TypeError, ValueError):
            dealer_cost = None
        try:
            suggested = float(first.get("suggestedPrice"))
        except (TypeError, ValueError):
            suggested = None

    if dealer_cost is None or dealer_cost <= 0:
        return None, "no_price"

    # Inventory → in_stock
    inv = obj.get("inventory") or {}
    is_inv_item = bool(inv.get("isInventoryItem"))
    if is_inv_item:
        stock = inv.get("stock")
        try:
            stock_n = int(stock) if stock is not None else 0
        except (TypeError, ValueError):
            stock_n = 0
        in_stock = stock_n > 0
    else:
        # Non-tracked item — always orderable (special-order style)
        in_stock = True

    # Image candidates: prefer large, then normal. (thumb not stored.)
    image_urls = []
    for field in ("largePic", "pic"):
        url = _abs_image_url(obj.get(field))
        if url and url not in image_urls:
            image_urls.append(url)

    record = {
        "sku": sku,
        "clean_name": obj.get("name") or "",
        "brand": (obj.get("brand") or "").strip(),
        "dealer_cost": dealer_cost,
        "suggested_price": suggested,
        "in_stock": in_stock,
        "image_urls": image_urls,
        "key": obj.get("key") or "",
        "source": "desco",
    }
    return record, None


# ── STEP 3: Enrich (crawl categories) ─────────────────────────────────────────

def _fetch_category_page(sess, cid, page):
    """Return (product_array, raw_html). The html is returned so the caller can
    harvest subcategory pc_ids for recursive discovery."""
    url = f"{CATEGORY_URL}?pc_id={cid}"
    if page > 1:
        url += f"&page={page}"
    resp = fetch(sess, url)
    return _largest_array(_extract_product_arrays(resp.text)), resp.text


def step_enrich(sess, progress, limit=None):
    log.info("=" * 60)
    log.info("STEP 3: Enrich — crawl category product JSON")
    log.info("=" * 60)

    category_ids = progress["category_ids"]
    if limit:
        category_ids = category_ids[:limit]
    processed = set(progress["processed_categories"])
    products = progress["products"]            # keyed by product `key`
    skipped = progress["skipped"]

    # Recursion only on full crawls — a --limit run must NOT expand its scope.
    recurse = limit is None

    # Drive off a growing work-list. `queued` dedupes by 32-hex id; the list
    # naturally bounds itself once every category has been seen.
    queue = [c for c in category_ids if c not in processed]
    queued = set(category_ids)
    discovered_new = 0
    log.info(f"Categories: {len(category_ids)} total, "
             f"{len(processed)} already done, {len(queue)} to process"
             f"{' (recursive discovery ON)' if recurse else ' (limit set, recursion OFF)'}")

    done_count = 0
    while queue:
        cid = queue.pop(0)
        done_count += 1
        new_keys = 0
        seen_keys_this_cat = set()
        full_page_size = None
        first_page_html = None

        for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
            try:
                arr, html = _fetch_category_page(sess, cid, page)
            except RuntimeError as exc:
                log.warning(f"  [{cid}] page {page} failed: {exc}")
                break
            time.sleep(REQUEST_DELAY)

            if page == 1:
                first_page_html = html

            if not arr:
                break

            page_new = 0
            for obj in arr:
                key = obj.get("key") or ""
                if key:
                    if key in seen_keys_this_cat:
                        continue
                    seen_keys_this_cat.add(key)

                record, reason = _parse_product(obj)
                if record is None:
                    skipped.append({
                        "sku": (obj.get("sku") or ""),
                        "key": key,
                        "name": obj.get("name") or "",
                        "reason": reason,
                        "category": cid,
                    })
                    continue

                rec_key = record["key"] or record["sku"]
                if rec_key not in products:
                    products[rec_key] = record
                    new_keys += 1
                    page_new += 1

            # Pagination heuristic: page 1 sets the "full page" size. If a later
            # page yields no NEW keys at all, stop (single-page or exhausted).
            if page == 1:
                full_page_size = len(arr)
                # If page 1 wasn't a "full" page, no point paginating — but we
                # always try page 2 once to confirm (cheap), unless page 1 tiny.
                if full_page_size < 12:
                    break
            else:
                if page_new == 0:
                    break

        # Recursive discovery: harvest subcategory pc_ids from page 1's html.
        # Page 1 carries the same category nav as later pages, so it's enough.
        if recurse and first_page_html:
            for new_cid in CATEGORY_RE.findall(first_page_html):
                if new_cid not in queued:
                    queued.add(new_cid)
                    queue.append(new_cid)
                    progress["category_ids"].append(new_cid)
                    discovered_new += 1

        if done_count % 10 == 0 or new_keys:
            log.info(f"  [{done_count} done, {len(queue)} queued] {cid}: "
                     f"+{new_keys} new (total products: {len(products)})")

        processed.add(cid)
        progress["processed_categories"].append(cid)

        if done_count % SAVE_EVERY_N_CATEGORIES == 0:
            save_progress(progress)
            log.info(f"  — checkpoint saved ({len(products)} products, "
                     f"{len(skipped)} skipped, {discovered_new} subcats found) —")

    if recurse and discovered_new:
        log.info(f"Discovered {discovered_new} new subcategories during crawl")

    if "enrich" not in progress["steps_done"]:
        progress["steps_done"].append("enrich")
    save_progress(progress)
    log.info(f"Enrich complete: {len(products)} unique products, "
             f"{len(skipped)} skipped")


# ── STEP 4: Export ────────────────────────────────────────────────────────────

# ── Brand derivation ──────────────────────────────────────────────────────────
#
# Desco's source JSON leaves `brand` blank on ~80% of products. For vacuum parts,
# the brand the part FITS (the leading token of the product name) is the correct
# Shopify vendor. We normalise the name to uppercase tokens, check the two-token
# slash combo first (e.g. ROYAL/DD, EUREKA/SANITAIRE), then the leading token,
# against a curated alias map. Anything we can't map (hoses keyed only by length,
# generic measurements, importer codes) falls back to "Universal" — every product
# needs a vendor, so we never leave it blank.

BRAND_ALIASES = {
    # Major vacuum brands
    "BISSELL": "Bissell",
    "BISSEL": "Bissell",
    "HOOVER": "Hoover",
    "EUREKA": "Eureka",
    "KIRBY": "Kirby",
    "SEBO": "Sebo",
    "ORECK": "Oreck",
    "DYSON": "Dyson",
    "ROYAL": "Royal",
    "ROYAL/DD": "Royal",
    "LUX": "Lux",
    "ELECTROLUX": "Electrolux",
    "PROTEAM": "ProTeam",
    "PROTEAM/PROFORCE": "ProTeam",
    "REXAIR": "Rexair",
    "RAINBOW": "Rainbow",
    "PANASONIC": "Panasonic",
    "MIELE": "Miele",
    "RICCAR": "Riccar",
    "RICCAR/SIMPLICITY": "Riccar",
    "SIMPLICITY": "Simplicity",
    "TITAN": "Titan",
    "SANITAIRE": "Sanitaire",
    "EUREKA/SANITAIRE": "Eureka",
    "EUREKA/ELECTROLUX": "Eureka",
    "ROYAL/HOOVER": "Royal",
    "ELECTROLUX/BEAM": "Electrolux",
    "PANASONIC/KENMORE": "Panasonic",
    "PAN": "Panasonic",          # "PAN MCUG... " / "PAN / KEN ..."
    "LINDHAUS": "Lindhaus",
    "EUROCLEAN": "Euroclean",
    "HAKO": "Hako",
    "TORNADO": "Tornado",
    "SHARK": "Shark",
    "KENMORE": "Kenmore",
    "KOBLENZ": "Koblenz",
    "WINDSOR": "Windsor",
    "FANTOM": "Fantom",
    "REGINA": "Regina",
    "METROPOLITAN": "Metropolitan",
    "DUSTCARE": "DustCare",
    "VAPAMORE": "Vapamore",
    "READIVAC": "ReadiVac",

    # Motors / powerhead lines
    "LAMB": "Lamb Ametek",
    "AMETEK": "Lamb Ametek",
    "WESSEL": "Wessel-Werk",
    "WESSEL-WERK": "Wessel-Werk",
    "WESSELL": "Wessel-Werk",
    "EBK360": "Wessel-Werk",
    "EBK340": "Wessel-Werk",
    "HEB160": "Wessel-Werk",
    "TURBOCAT": "Turbocat",
    "T210": "Turbocat",
    "HP": "Turbocat",
    "PLASTIFLEX": "Plastiflex",
    "PLSTFLX": "Plastiflex",

    # Commercial / cleaning machine brands
    "NUMATIC": "NaceCare",
    "NACECARE": "NaceCare",
    "NUMATIC/NACECARE": "NaceCare",
    "NUMATIC/NACE": "NaceCare",
    "NSS": "NSS",
    "NILFISK": "Nilfisk",
    "ADVANCE": "Advance",
    "CASTEX": "Castex",
    "TENNANT": "Tennant",
    "NOBLES": "Nobles",
    "PULLMAN": "Pullman",
    "MASTERCRAFT": "Mastercraft",
    "INTERVAC": "Intervac",
    "ASCENDANT": "Ascendant",

    # Central-vac / hose-system brands
    "VACUMAID": "VacuMaid",
    "VACULINE": "Vaculine",
    "HAYDEN": "Hayden",
    "HIDE-A-HOSE": "Hide-A-Hose",
    "HIDE": "Hide-A-Hose",
    "HAH": "Hide-A-Hose",
    "RETRACTABLE": "Hide-A-Hose",
    "NADAIR": "Nadair",
    "AIR-WAY": "Air-Way",
    "VACUFLO": "VacuFlo",
    "BEAM": "Beam",
    "NUTONE": "NuTone",
    "CANAVAC": "Canavac",
    "DUOVAC": "Duovac",
    "VROOM": "Vroom",
    "WALLY": "Wally",
    "DECO": "Deco",
    "VEX": "Vex",
    "VACPORT": "VacPort",
    "GENIE": "Vacu-Genie",
    "CEN-TEC": "Cen-Tec",
    "CENTEC": "Cen-Tec",
    "CENTRAL": "Central Vac",
    "GRAND": "Grand",
    "SPOTTY": "Spotty",
    "VAC": "Vac N Clean",         # "VAC N CLEAN ..."

    # Importer / private-label part lines
    "JOHNNY": "Johnny Vac",
    "JV": "Johnny Vac",
    "FITALL": "Fit All",
    "FILTER": "Filter Queen",     # "FILTER QUEEN ..."
    "FILTERQUEEN": "Filter Queen",
    "DIRT": "Dirt Devil",         # "DIRT DEVIL ..."
    "DIRTDEVIL": "Dirt Devil",
    "PERFECT": "Perfect Fit Hoses",
    "CLEAN": "CleanMax",          # CLEAN MAX / CLEAN OBSESSED
    "CLEAN-OBSESSED": "CleanMax",
    "SHOP": "Shop-Vac",           # "SHOP-VAC ..." / "SHOP VAC ..."
    "STAIN-X": "Stain-X",
    "ROGERS": "Rogers",
    "VACYUM": "Vacyum",
    "JANITIZED": "Janitized",
    "CLICK": "Click",
    "MD": "MD",
}


def _lookup_alias(text):
    """Run a string's leading token(s) through BRAND_ALIASES.

    Tries the two-token slash combo first ("ROYAL/DD", "EUREKA/SANITAIRE"),
    then the leading token (which may itself embed a slash combo such as
    "NUMATIC/NACECARE"). Returns the canonical vendor string, or None if no
    leading token maps. Case-insensitive — BRAND_ALIASES keys are uppercase.
    """
    if not text:
        return None
    tokens = re.sub(r"[^A-Za-z0-9/-]", " ", text.upper()).split()
    if not tokens:
        return None

    first = tokens[0]
    if len(tokens) >= 2:
        combo = f"{first}/{tokens[1]}"
        if combo in BRAND_ALIASES:
            return BRAND_ALIASES[combo]
    if first in BRAND_ALIASES:
        return BRAND_ALIASES[first]
    return None


def _title_case_brand(raw):
    """Title-case an unmapped source brand, e.g. "PERFECT FIT HOSES" →
    "Perfect Fit Hoses". Only used as a fallback when a source brand's leading
    token is absent from BRAND_ALIASES — known stylizations live in the alias
    VALUES and resolve via _lookup_alias before we ever reach here.
    """
    return " ".join(w.capitalize() for w in raw.split())


def derive_brand(name):
    """Return a canonical Shopify vendor for a (usually brand-less) product name.

    Matches the leading token(s) against BRAND_ALIASES. Unmappable items
    (length-only hoses, measurements, importer codes) fall back to "Universal"
    so no product is left without a vendor.
    """
    return _lookup_alias(name) or "Universal"


def canonicalize_brand(raw_brand, name):
    """Resolve any product to a single canonical Shopify vendor.

    Source brands and brand-less names flow through the SAME alias lookup so
    "EUREKA" (source) and a name starting "EUREKA ..." (derived) both resolve
    to "Eureka". BRAND_ALIASES values are the single source of truth for
    canonical casing.

    - Non-empty source brand: look it up in the alias map; if it maps, return
      the canonical value. If it does not map (an unusual source brand), return
      it Title-Cased.
    - Empty source brand: derive from the product name (existing behavior).
    """
    if raw_brand:
        return _lookup_alias(raw_brand) or _title_case_brand(raw_brand)
    return derive_brand(name)


def step_export(progress, dry_run=False):
    log.info("=" * 60)
    log.info("STEP 4: Export desco_products.json")
    log.info("=" * 60)

    products = progress["products"]

    # Re-key by SKU for the output file
    by_sku = {}
    dup_skus = 0
    for record in products.values():
        sku = record["sku"]
        if sku in by_sku:
            dup_skus += 1
            # Keep the in-stock / cheaper one deterministically: prefer in_stock,
            # then lower dealer cost. Mostly there are no real dup SKUs.
            existing = by_sku[sku]
            if record["in_stock"] and not existing["in_stock"]:
                by_sku[sku] = record
            continue
        by_sku[sku] = record

    # Canonicalize EVERY product's brand through one normalizer so source
    # brands ("EUREKA") and name-derived brands both resolve to a single
    # canonical vendor ("Eureka") — no case-duplicate Shopify vendors.
    # Work on shallow copies so we never write canonical brands back into the
    # progress file (which would erase the source-vs-derived distinction and
    # break re-runs).
    derived_brand = 0
    for sku, record in list(by_sku.items()):
        had_source = bool(record["brand"])
        record = dict(record)
        record["brand"] = canonicalize_brand(record["brand"], record["clean_name"])
        by_sku[sku] = record
        if not had_source:
            derived_brand += 1

    total = len(by_sku)
    with_image = sum(1 for r in by_sku.values() if r["image_urls"])
    in_stock = sum(1 for r in by_sku.values() if r["in_stock"])
    empty_brand = sum(1 for r in by_sku.values() if not r["brand"])
    universal_brand = sum(1 for r in by_sku.values() if r["brand"] == "Universal")

    brand_counts = Counter(r["brand"] or "(empty)" for r in by_sku.values())
    top_brands = brand_counts.most_common(20)
    distinct_vendors = len(brand_counts)

    log.info(f"Total products (by SKU):  {total}")
    log.info(f"  with image_urls:        {with_image}")
    log.info(f"  in stock:               {in_stock}")
    log.info(f"  brands derived:         {derived_brand} (filled from name)")
    log.info(f"  empty brand:            {empty_brand}")
    log.info(f"  Universal (generic):    {universal_brand} "
             f"(un-mappable: length-only hoses, measurements, importer codes)")
    if dup_skus:
        log.info(f"  duplicate SKUs merged:  {dup_skus}")
    log.info(f"  skipped (no sku/price): {len(progress['skipped'])}")
    log.info(f"  distinct vendors:       {distinct_vendors}")
    log.info("Top 20 brands:")
    for brand, count in top_brands:
        log.info(f"    {count:>5}  {brand}")

    if dry_run:
        log.info("[DRY RUN] Not writing desco_products.json")
        return by_sku

    OUTPUT_FILE.write_text(json.dumps(by_sku, indent=2))
    log.info(f"Wrote {OUTPUT_FILE.name} ({total} products)")

    if "export" not in progress["steps_done"]:
        progress["steps_done"].append("export")
    save_progress(progress)
    return by_sku


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ingest descovac.com catalog")
    parser.add_argument("--step", choices=["login", "discover", "enrich", "export", "all"],
                        default="all", help="Run a specific step (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap number of categories (testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Crawl but do not write desco_products.json")
    args = parser.parse_args()

    progress = load_progress()

    # export-only does not need the network/session
    if args.step == "export":
        step_export(progress, dry_run=args.dry_run)
        log.info("\nDone!")
        return

    sess = get_session()

    if args.step in ("login", "all"):
        step_login(sess)

    if args.step in ("discover", "all"):
        # login required for an authenticated homepage
        if args.step == "discover":
            step_login(sess)
        step_discover(sess, progress, limit=args.limit)

    if args.step in ("enrich", "all"):
        if args.step == "enrich":
            step_login(sess)
            if not progress["category_ids"]:
                step_discover(sess, progress, limit=args.limit)
        if not progress["category_ids"]:
            log.error("No category ids — run --step discover first.")
            return
        step_enrich(sess, progress, limit=args.limit)

    if args.step == "all":
        step_export(progress, dry_run=args.dry_run)

    log.info("\nDone!")


if __name__ == "__main__":
    main()
