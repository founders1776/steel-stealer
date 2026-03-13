#!/usr/bin/env python3
"""
Build SKU → {product_id, variant_id} mapping from Shopify.

Paginates through ALL products in Shopify and saves the mapping to
shopify_product_map.json. Only needs to run once (or after new products added).

Usage:
  python3 build_shopify_map.py
"""

import json
import os
import sys
import time

import requests

# ── Config ──────────────────────────────────────────────────────────────────

SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = "2024-10"
MAP_FILE = "shopify_product_map.json"


def api_url(path):
    return f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/{path}"


def api_headers():
    return {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


def api_get(path, params=None, retries=3):
    """GET from Shopify REST API with retry on transient errors."""
    for attempt in range(retries):
        try:
            resp = requests.get(
                api_url(path), headers=api_headers(), params=params, timeout=30
            )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2))
                print(f"\n  Rate limited, waiting {retry_after}s...", end=" ", flush=True)
                time.sleep(retry_after)
                continue
            if resp.status_code in (500, 502, 503, 504) and attempt < retries - 1:
                wait = (attempt + 1) * 5
                print(f"\n  HTTP {resp.status_code}, retrying in {wait}s...", end=" ", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                time.sleep((attempt + 1) * 5)
            else:
                raise
    return None


def build_map():
    """Paginate all Shopify products and build SKU → IDs mapping."""
    product_map = {}
    params = {"limit": 250, "fields": "id,variants"}
    page = 0

    print("Fetching products from Shopify...")

    while True:
        resp = api_get("products.json", params=params)
        if resp is None:
            print("ERROR: Failed to fetch products")
            break

        data = resp.json()
        products = data.get("products", [])
        if not products:
            break

        for product in products:
            product_id = str(product["id"])
            for variant in product.get("variants", []):
                sku = (variant.get("sku") or "").strip()
                if sku:
                    product_map[sku] = {
                        "product_id": product_id,
                        "variant_id": str(variant["id"]),
                    }

        page += 1
        print(f"  Page {page}: {len(products)} products (total SKUs mapped: {len(product_map)})")

        # Pagination via Link header
        link_header = resp.headers.get("Link", "")
        if 'rel="next"' not in link_header:
            break

        # Extract next page URL
        import re
        match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
        if not match:
            break

        next_url = match.group(1)
        # Extract page_info param from next URL
        page_info_match = re.search(r'page_info=([^&]+)', next_url)
        if page_info_match:
            params = {"limit": 250, "page_info": page_info_match.group(1)}
        else:
            break

        time.sleep(0.55)

    return product_map


def main():
    if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
        print("ERROR: Set SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN environment variables.")
        sys.exit(1)

    product_map = build_map()

    with open(MAP_FILE, "w") as f:
        json.dump(product_map, f, indent=2)

    print(f"\nDone! {len(product_map)} SKUs mapped → {MAP_FILE}")


if __name__ == "__main__":
    main()
