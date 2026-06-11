#!/usr/bin/env python3
"""
Build reprice_targets.json — the competitor-undercut allowlist for brands the
sync otherwise skips (dual-source brands like SEBO whose stock is never
tracked against Steel City, but whose parts/accessories should still follow
competitor pricing).

For each brand in reprice_brands.json this script:
  1. Fetches every product with that vendor from Shopify.
  2. Classifies complete machines (MAP-protected) by title pattern and merges
     their SKUs into price_locks.json at their current store price.
  3. Writes every other SKU into reprice_targets.json with:
       - variant_id / product_id   (price update handles)
       - ref_price                 (store price when first targeted — the
                                    floor anchor for SKUs with no dealer cost;
                                    preserved across rebuilds so repeated
                                    undercutting can't ratchet the floor down)
       - dealer_cost               (from product_names.json when Steel City
                                    carries the SKU, else null)

Idempotent: existing targets keep their ref_price; existing price locks are
never overwritten. Safe to re-run after the brand's catalog changes.

Usage:
  python3 build_reprice_targets.py            # Build/refresh targets
  python3 build_reprice_targets.py --dry-run  # Report without writing
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ── Config ──────────────────────────────────────────────────────────────────

SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = "2024-10"

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "product_names.json"
REPRICE_BRANDS_FILE = BASE_DIR / "reprice_brands.json"
PRICE_LOCKS_FILE = BASE_DIR / "price_locks.json"
TARGETS_FILE = BASE_DIR / "reprice_targets.json"

# Complete machines (MAP-protected) — title starts with a machine family name
# and the price is in machine territory. Parts that mention a family mid-title
# ("Filter Bag Box FELIX and DART") or cheap branded accessories ("AIRBELT
# Textile", $44.99) don't match. Verified against the full SEBO catalog:
# matches exactly the 32 complete units, zero parts.
MACHINE_TITLE = re.compile(
    r'^(?:SEBO\s+)?('
    r'AIRBELT\s+[DEKC]\d|AUTOMATIC\s+X\d|FELIX|DART\b|'
    r'\d{3}\s+MECHANICAL|ESSENTIAL\s+G\d|'
    r'DUO\s+Brush\s+Machine|DISCO\s+Floor\s+Polisher'
    r')', re.I)
MACHINE_MIN_PRICE = 380.0


def api_get(path, params=None, retries=3):
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/{path}"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
    for attempt in range(retries):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 2)))
            continue
        if resp.status_code in (500, 502, 503, 504) and attempt < retries - 1:
            time.sleep((attempt + 1) * 5)
            continue
        resp.raise_for_status()
        return resp
    return None


def fetch_vendor_products(vendor):
    """All products for a vendor: (product_id, variant_id, sku, title, price)."""
    items = []
    params = {"vendor": vendor, "limit": 250,
              "fields": "id,title,status,variants"}
    while True:
        resp = api_get("products.json", params=params)
        for product in resp.json().get("products", []):
            for variant in product.get("variants", []):
                sku = (variant.get("sku") or "").strip()
                if sku:
                    items.append({
                        "sku": sku,
                        "product_id": str(product["id"]),
                        "variant_id": str(variant["id"]),
                        "title": product.get("title") or "",
                        "price": float(variant.get("price") or 0),
                        "status": product.get("status"),
                    })
        match = re.search(r'<([^>]+)>;\s*rel="next"', resp.headers.get("Link", ""))
        if not match:
            break
        page_info = re.search(r'page_info=([^&]+)', match.group(1))
        if not page_info:
            break
        params = {"limit": 250, "page_info": page_info.group(1)}
        time.sleep(0.55)
    return items


def main():
    parser = argparse.ArgumentParser(description="Build competitor-reprice target list")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing files")
    args = parser.parse_args()

    if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
        print("ERROR: Set SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN environment variables.")
        sys.exit(1)

    if not REPRICE_BRANDS_FILE.exists():
        print(f"{REPRICE_BRANDS_FILE.name} not found — nothing to do.")
        return
    brands = [b.strip() for b in json.loads(REPRICE_BRANDS_FILE.read_text()) if b and b.strip()]
    if not brands:
        print("reprice_brands.json is empty — nothing to do.")
        return

    # Dealer costs from product_names.json (Steel City SKUs only)
    dealer_costs = {}
    if PRODUCTS_FILE.exists():
        products = json.loads(PRODUCTS_FILE.read_text())
        for key, prod in products.items():
            sku = prod.get("sku", key)
            match = re.search(r'[\d]+\.?\d*', str(prod.get("price") or ""))
            if match:
                dealer_costs[sku] = float(match.group())

    locks = {}
    if PRICE_LOCKS_FILE.exists():
        locks = json.loads(PRICE_LOCKS_FILE.read_text())

    existing_targets = {}
    if TARGETS_FILE.exists():
        existing_targets = {k: v for k, v in json.loads(TARGETS_FILE.read_text()).items()
                            if not k.startswith("_")}

    targets = {}
    new_locks = 0
    machines = []
    for brand in brands:
        items = fetch_vendor_products(brand)
        print(f"{len(items)} variants fetched (vendor redacted — may run in public CI)")
        for item in items:
            sku = item["sku"]
            if MACHINE_TITLE.search(item["title"]) and item["price"] >= MACHINE_MIN_PRICE:
                machines.append(item)
                if sku not in locks:
                    locks[sku] = f"${item['price']:.2f}"
                    new_locks += 1
                continue
            if sku in locks or sku in targets:
                continue
            prior = existing_targets.get(sku) or {}
            targets[sku] = {
                "product_id": item["product_id"],
                "variant_id": item["variant_id"],
                "title": item["title"],
                # Floor anchor: keep the price from when the SKU was FIRST
                # targeted, so successive undercuts can't lower the floor.
                "ref_price": prior.get("ref_price", item["price"]),
                "dealer_cost": dealer_costs.get(sku),
                "last_applied": prior.get("last_applied"),
            }

    print(f"Machines (MAP-locked): {len(machines)} ({new_locks} newly locked)")
    print(f"Reprice targets: {len(targets)} "
          f"({sum(1 for t in targets.values() if t['dealer_cost'] is not None)} with dealer cost)")

    if args.dry_run:
        print("Dry run — not writing files.")
        return

    locks.setdefault("_comment_reprice", (
        "Complete machines auto-locked by build_reprice_targets.py at their "
        "store price (MAP). Parts/accessories for these brands are repriced "
        "from competitor data via reprice_targets.json."))
    PRICE_LOCKS_FILE.write_text(json.dumps(locks, indent=2))
    out = {"_comment": (
        "Competitor-undercut allowlist built by build_reprice_targets.py. "
        "sync_stock_prices.py reprices these SKUs from competitor_prices.json "
        "only (never markup fallback); no stock changes. ref_price anchors the "
        "price floor for SKUs without dealer cost.")}
    out.update(dict(sorted(targets.items())))
    TARGETS_FILE.write_text(json.dumps(out, indent=2))
    print(f"Wrote {TARGETS_FILE.name} and updated {PRICE_LOCKS_FILE.name}")


if __name__ == "__main__":
    main()
