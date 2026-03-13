#!/usr/bin/env python3
"""
Bulk import products to Shopify via REST Admin API.
Reads output/shopify_import.csv and creates all products.
Resumable — checkpoints progress to bulk_import_progress.json.
"""

import csv
import json
import os
import sys
import time
from collections import OrderedDict

import requests

# ── Config ──────────────────────────────────────────────────────────────────

CSV_FILE = "output/shopify_import.csv"
PROGRESS_FILE = "bulk_import_progress.json"

SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = "2024-10"


def api_url(path):
    return f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/{path}"


def api_headers():
    return {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


def api_post(path, payload, retries=3):
    """POST to Shopify REST API with retry on transient errors."""
    for attempt in range(retries):
        try:
            resp = requests.post(api_url(path), json=payload, headers=api_headers(), timeout=60)

            # Rate limiting
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2))
                print(f"\n  Rate limited, waiting {retry_after}s...", end=" ", flush=True)
                time.sleep(retry_after)
                continue

            # Transient server errors
            if resp.status_code in (500, 502, 503, 504) and attempt < retries - 1:
                wait = (attempt + 1) * 5
                print(f"\n  HTTP {resp.status_code}, retrying in {wait}s...", end=" ", flush=True)
                time.sleep(wait)
                continue

            if resp.status_code == 422:
                # Validation error — return it, don't retry
                return resp.json(), resp.status_code

            resp.raise_for_status()
            return resp.json(), resp.status_code

        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                wait = (attempt + 1) * 5
                print(f"\n  Connection error, retrying in {wait}s...", end=" ", flush=True)
                time.sleep(wait)
            else:
                raise
        except requests.exceptions.ReadTimeout:
            if attempt < retries - 1:
                wait = (attempt + 1) * 5
                print(f"\n  Timeout, retrying in {wait}s...", end=" ", flush=True)
                time.sleep(wait)
            else:
                raise

    return None, None


def read_csv_products():
    """Read CSV and group rows by Handle into product dicts."""
    products = OrderedDict()

    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            handle = row["Handle"]
            if handle not in products:
                products[handle] = {
                    "handle": handle,
                    "title": row["Title"],
                    "body_html": row["Body (HTML)"],
                    "vendor": row["Vendor"],
                    "product_type": row["Product Type"],
                    "tags": row["Tags"],
                    "published": row["Published"] == "TRUE",
                    "sku": row["Variant SKU"],
                    "price": row["Variant Price"],
                    "cost": row["Variant Cost per item"],
                    "compare_at_price": row["Variant Compare At Price"],
                    "inventory_policy": row["Variant Inventory Policy"],
                    "requires_shipping": row["Variant Requires Shipping"] == "TRUE",
                    "weight_unit": row["Variant Weight Unit"] or "lb",
                    "status": row["Status"],
                    "images": [],
                }
            # Collect images
            img_src = row.get("Image Src", "").strip()
            if img_src and img_src.startswith("http"):
                products[handle]["images"].append(img_src)

    return list(products.values())


def create_product(product):
    """Create a single product via REST API. Returns (product_id, error)."""
    variant = {
        "sku": product["sku"],
        "price": product["price"],
        "inventory_policy": product["inventory_policy"],
        "requires_shipping": product["requires_shipping"],
        "weight_unit": product["weight_unit"],
        "fulfillment_service": "manual",
    }
    if product["cost"]:
        variant["cost"] = product["cost"]
    if product["compare_at_price"]:
        variant["compare_at_price"] = product["compare_at_price"]

    images = [{"src": url} for url in product["images"]]

    payload = {
        "product": {
            "handle": product["handle"],
            "title": product["title"],
            "body_html": product["body_html"],
            "vendor": product["vendor"],
            "product_type": product["product_type"],
            "tags": product["tags"],
            "status": product["status"],
            "variants": [variant],
            "images": images,
        }
    }

    data, status = api_post("products.json", payload)

    if data is None:
        return None, "No response"

    if "product" in data:
        return data["product"]["id"], None

    if "errors" in data:
        err = data["errors"]
        if isinstance(err, dict):
            msgs = []
            for field, errs in err.items():
                if isinstance(errs, list):
                    msgs.append(f"{field}: {'; '.join(errs)}")
                else:
                    msgs.append(f"{field}: {errs}")
            return None, " | ".join(msgs)
        return None, str(err)

    return None, f"HTTP {status}: {json.dumps(data)[:200]}"


def main():
    if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
        print("ERROR: Set SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN environment variables.")
        sys.exit(1)

    print("=" * 60)
    print("Shopify Bulk Product Import (REST API)")
    print("=" * 60)

    # Read CSV
    print(f"\nReading {CSV_FILE}...")
    products = read_csv_products()
    print(f"  {len(products)} products to import")

    # Load progress (for resume)
    progress = {}
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            progress = json.load(f)
        created = sum(1 for v in progress.values() if v.get("status") == "created")
        failed = sum(1 for v in progress.values() if v.get("status") == "failed")
        print(f"  Resuming — {created} created, {failed} failed previously")

    # Filter out already-imported (only skip successful ones)
    to_import = [p for p in products if p["handle"] not in progress or progress[p["handle"]].get("status") == "failed"]
    print(f"  {len(to_import)} remaining to import\n")

    if not to_import:
        print("Nothing to import!")
        return

    success = 0
    failed = 0
    total = len(to_import)

    for i, product in enumerate(to_import):
        title_display = product['title'][:55]
        print(f"[{i+1}/{total}] {title_display}...", end=" ", flush=True)

        product_id, error = create_product(product)

        if product_id:
            progress[product["handle"]] = {"id": str(product_id), "status": "created"}
            success += 1
            print("✓")
        else:
            progress[product["handle"]] = {"error": error, "status": "failed"}
            failed += 1
            print(f"✗ {error}")

        # Checkpoint every 25
        if (i + 1) % 25 == 0:
            with open(PROGRESS_FILE, "w") as f:
                json.dump(progress, f, indent=2)
            print(f"  — Checkpoint: {success} created, {failed} failed —")

        # Rate limiting — stay under Shopify's 2 req/sec bucket
        time.sleep(0.55)

    # Final save
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)

    total_created = sum(1 for v in progress.values() if v.get("status") == "created")
    total_failed = sum(1 for v in progress.values() if v.get("status") == "failed")

    print(f"\n{'=' * 60}")
    print(f"IMPORT COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Created:  {total_created}")
    print(f"  Failed:   {total_failed}")
    print(f"  Total:    {total_created + total_failed}")
    print(f"\nProgress saved to {PROGRESS_FILE}")


if __name__ == "__main__":
    main()
