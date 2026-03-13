#!/usr/bin/env python3
"""
Shopify Upload & CSV Generator for Steel Stealer
- Uploads images to Shopify Files via Admin API
- Generates Shopify-formatted product import CSV
"""

import csv
import json
import os
import re
import sys
import time

import openpyxl
import requests

# ── Config ──────────────────────────────────────────────────────────────────

SPREADSHEET = "output/steel_city_parts.xlsx"
IMAGES_DIR = "images"
IMAGE_URLS_FILE = "image_urls.json"
OUTPUT_CSV = "output/shopify_import.csv"

# Shopify API — set via environment variables
# export SHOPIFY_STORE="your-store.myshopify.com"
# export SHOPIFY_ACCESS_TOKEN="shpat_xxxxx"
SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION = "2024-10"


# ── Spreadsheet Reading (same dedup logic as image_scraper.py) ─────────────

def build_sku_map():
    """Read spreadsheet and build deduplicated SKU map."""
    print(f"Reading {SPREADSHEET}...")
    wb = openpyxl.load_workbook(SPREADSHEET, read_only=True)
    ws = wb.active

    sku_map = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        brand = str(row[0] or "").strip()
        model = str(row[1] or "").strip()
        part_number = str(row[3] or "").strip()
        name = str(row[4] or "").strip()
        sku = str(row[5] or "").strip()
        description = str(row[6] or "").strip()
        price = str(row[7] or "").strip()

        if not sku:
            # No SKU — use part_number as identifier
            if part_number:
                key = f"NOSKU-{part_number}-{brand}-{model}"
                if key not in sku_map:
                    sku_map[key] = {
                        "sku": key,
                        "name": name or description or part_number,
                        "description": description,
                        "price": price,
                        "brand": brand,
                        "brands": {brand},
                        "models": {model},
                        "folder_name": re.sub(r'[^\w\-]', '_', key),
                    }
            continue

        if sku in sku_map:
            entry = sku_map[sku]
            if brand:
                entry["brands"].add(brand)
            if model:
                entry["models"].add(model)
            if name and len(name) > len(entry.get("name", "")):
                entry["name"] = name
            if description and len(description) > len(entry.get("description", "")):
                entry["description"] = description
            if price and not entry["price"]:
                entry["price"] = price
        else:
            clean_name = name
            if not clean_name and description:
                clean_name = description
                if " - " in clean_name:
                    clean_name = clean_name.split(" - ", 1)[1]

            sku_map[sku] = {
                "sku": sku,
                "name": clean_name or sku,
                "description": description,
                "price": price,
                "brand": brand,
                "brands": {brand} if brand else set(),
                "models": {model} if model else set(),
                "folder_name": re.sub(r'[^\w\-]', '_', sku),
            }

    wb.close()
    print(f"  {len(sku_map)} unique products")
    return sku_map


# ── Shopify File Upload ────────────────────────────────────────────────────

def shopify_graphql(query, variables=None, retries=3):
    """Execute a Shopify Admin GraphQL query with retry on transient errors."""
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = (attempt + 1) * 5
                print(f"\n    HTTP {resp.status_code}, retrying in {wait}s...", end=" ", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                wait = (attempt + 1) * 5
                print(f"\n    Connection error, retrying in {wait}s...", end=" ", flush=True)
                time.sleep(wait)
            else:
                raise


def upload_image_to_shopify(filepath, filename):
    """Upload a single image to Shopify Files via staged upload.

    Returns the Shopify CDN URL or None on failure.
    """
    # Step 1: Create staged upload
    staged_query = """
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets {
          url
          resourceUrl
          parameters {
            name
            value
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {
        "input": [{
            "resource": "FILE",
            "filename": filename,
            "mimeType": "image/jpeg",
            "httpMethod": "POST",
        }]
    }

    result = shopify_graphql(staged_query, variables)
    targets = result.get("data", {}).get("stagedUploadsCreate", {}).get("stagedTargets", [])
    if not targets:
        errors = result.get("data", {}).get("stagedUploadsCreate", {}).get("userErrors", [])
        print(f"    Staged upload error: {errors}")
        return None

    target = targets[0]
    upload_url = target["url"]
    resource_url = target["resourceUrl"]
    params = {p["name"]: p["value"] for p in target["parameters"]}

    # Step 2: Upload file to staged URL (with retry)
    for attempt in range(3):
        try:
            with open(filepath, "rb") as f:
                files = {"file": (filename, f, "image/jpeg")}
                resp = requests.post(upload_url, data=params, files=files, timeout=60)
            if resp.status_code in (200, 201, 204):
                break
            if attempt < 2:
                print(f"\n    Upload HTTP {resp.status_code}, retrying...", end=" ", flush=True)
                time.sleep((attempt + 1) * 5)
            else:
                print(f"    Upload failed: {resp.status_code}")
                return None
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            if attempt < 2:
                print(f"\n    Connection error, retrying...", end=" ", flush=True)
                time.sleep((attempt + 1) * 5)
            else:
                print(f"    Upload failed: {e}")
                return None

    # Step 3: Create file in Shopify
    create_query = """
    mutation fileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          ... on MediaImage {
            id
            image {
              url
            }
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    file_variables = {
        "files": [{
            "originalSource": resource_url,
            "contentType": "IMAGE",
        }]
    }

    result = shopify_graphql(create_query, file_variables)
    files_created = result.get("data", {}).get("fileCreate", {}).get("files", [])

    if files_created and files_created[0]:
        image = files_created[0].get("image")
        if image and image.get("url"):
            return image["url"]

    # Image may not be ready yet — return resource_url as placeholder
    # The URL will be available after Shopify processes it
    return resource_url


def upload_all_images(sku_map):
    """Upload all downloaded images to Shopify Files."""
    if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
        print("ERROR: Set SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN environment variables.")
        print("  export SHOPIFY_STORE='your-store.myshopify.com'")
        print("  export SHOPIFY_ACCESS_TOKEN='shpat_xxxxx'")
        sys.exit(1)

    # Load existing URL mapping
    url_map = {}
    if os.path.exists(IMAGE_URLS_FILE):
        with open(IMAGE_URLS_FILE, "r") as f:
            url_map = json.load(f)

    skus_to_upload = []
    for sku, data in sku_map.items():
        if sku in url_map:
            continue
        folder = data.get("folder_name", re.sub(r'[^\w\-]', '_', sku))
        sku_dir = os.path.join(IMAGES_DIR, folder)
        if os.path.exists(sku_dir):
            images = sorted([f for f in os.listdir(sku_dir) if f.endswith(".jpg")])
            if images:
                skus_to_upload.append((sku, data, sku_dir, images))

    total = len(skus_to_upload)
    print(f"\nUploading images for {total} SKUs to Shopify...")

    for i, (sku, data, sku_dir, images) in enumerate(skus_to_upload):
        print(f"[{i+1}/{total}] {sku}...", end=" ", flush=True)

        urls = []
        for img_file in images:
            filepath = os.path.join(sku_dir, img_file)
            filename = f"{data['folder_name']}_{img_file}"
            url = upload_image_to_shopify(filepath, filename)
            if url:
                urls.append(url)
            time.sleep(0.5)  # Rate limit

        url_map[sku] = urls
        print(f"✓ {len(urls)} uploaded")

        # Checkpoint every 10
        if (i + 1) % 10 == 0:
            with open(IMAGE_URLS_FILE, "w") as f:
                json.dump(url_map, f, indent=2)

        time.sleep(0.5)

    # Final save
    with open(IMAGE_URLS_FILE, "w") as f:
        json.dump(url_map, f, indent=2)

    print(f"\nDone! {len(url_map)} SKUs with uploaded images.")
    return url_map


# ── Shopify CSV Generation ─────────────────────────────────────────────────

def slugify(text):
    """Convert text to URL-friendly handle."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')


def parse_price(price_str):
    """Extract numeric price from string like '$11.92'."""
    if not price_str:
        return ""
    match = re.search(r'[\d]+\.?\d*', price_str)
    return match.group() if match else ""


def generate_csv(sku_map, url_map=None):
    """Generate Shopify product import CSV."""
    if url_map is None:
        url_map = {}
        if os.path.exists(IMAGE_URLS_FILE):
            with open(IMAGE_URLS_FILE, "r") as f:
                url_map = json.load(f)

    # Also check for local images if no Shopify URLs
    def get_image_sources(sku, data):
        """Get image URLs/paths for a SKU."""
        if sku in url_map and url_map[sku]:
            return url_map[sku]
        # Check for local images (user can host these elsewhere)
        folder = data.get("folder_name", re.sub(r'[^\w\-]', '_', sku))
        sku_dir = os.path.join(IMAGES_DIR, folder)
        if os.path.exists(sku_dir):
            images = sorted([f for f in os.listdir(sku_dir) if f.endswith(".jpg")])
            # Return relative paths — user will need to upload these
            return [os.path.join(sku_dir, img) for img in images]
        return []

    # Shopify CSV headers
    headers = [
        "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags",
        "Published", "Option1 Name", "Option1 Value", "Variant SKU",
        "Variant Price", "Variant Cost per item", "Variant Inventory Policy",
        "Variant Fulfillment Service", "Variant Requires Shipping",
        "Image Src", "Image Position", "Variant Weight Unit", "Status",
    ]

    # Load product_names.json for retail prices
    product_data = {}
    if os.path.exists("product_names.json"):
        with open("product_names.json", "r") as f:
            product_data = json.load(f)

    rows = []
    for sku, data in sku_map.items():
        cost = parse_price(data["price"])
        if not cost:
            continue  # Skip products with no price (NLA, etc.)

        # Use retail_price from product_names.json if available
        pdata = product_data.get(sku, {})
        retail = parse_price(pdata.get("retail_price", ""))
        if not retail:
            retail = cost  # Fallback to cost if no retail price generated

        # Clean up brand — use first brand
        brands = sorted(data["brands"]) if isinstance(data["brands"], set) else [data.get("brand", "")]
        vendor = brands[0] if brands else ""
        # Clean comma-separated brand names
        if "," in vendor:
            vendor = vendor.split(",")[0].strip()

        # Models for tags and description
        models = sorted(data["models"]) if isinstance(data["models"], set) else []
        models = [m for m in models if m]  # Remove empty
        tags = ", ".join(models) if models else ""

        # Build body HTML
        name = data["name"]
        desc = data.get("description", "")
        body_parts = []
        if desc and desc != name:
            body_parts.append(f"<p>{desc}</p>")
        if models:
            body_parts.append(f"<p><strong>Compatible with:</strong> {', '.join(models)}</p>")
        if len(brands) > 1:
            body_parts.append(f"<p><strong>Brands:</strong> {', '.join(brands)}</p>")
        body_html = "\n".join(body_parts)

        handle = slugify(sku)
        image_sources = get_image_sources(sku, data)

        # First row: main product + first image
        row = {
            "Handle": handle,
            "Title": name,
            "Body (HTML)": body_html,
            "Vendor": vendor,
            "Type": "Vacuum Parts",
            "Tags": tags,
            "Published": "TRUE",
            "Option1 Name": "Title",
            "Option1 Value": "Default Title",
            "Variant SKU": sku if not sku.startswith("NOSKU-") else "",
            "Variant Price": retail,
            "Variant Cost per item": cost,
            "Variant Inventory Policy": "deny",
            "Variant Fulfillment Service": "manual",
            "Variant Requires Shipping": "TRUE",
            "Image Src": image_sources[0] if image_sources else "",
            "Image Position": "1" if image_sources else "",
            "Variant Weight Unit": "lb",
            "Status": "active",
        }
        rows.append(row)

        # Additional rows for extra images (same handle, only image fields)
        for idx, img_src in enumerate(image_sources[1:], start=2):
            img_row = {h: "" for h in headers}
            img_row["Handle"] = handle
            img_row["Image Src"] = img_src
            img_row["Image Position"] = str(idx)
            rows.append(img_row)

    # Write CSV
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

    # Count products vs total rows
    products = len([r for r in rows if r["Title"]])
    print(f"\nCSV generated: {OUTPUT_CSV}")
    print(f"  Products: {products}")
    print(f"  Total rows (including extra image rows): {len(rows)}")

    return rows


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "csv"

    sku_map = build_sku_map()

    if command == "upload":
        # Upload images to Shopify Files, then generate CSV
        url_map = upload_all_images(sku_map)
        generate_csv(sku_map, url_map)

    elif command == "csv":
        # Just generate CSV (with local image paths or existing Shopify URLs)
        generate_csv(sku_map)

    else:
        print("Usage:")
        print("  python shopify_upload.py csv      — Generate Shopify CSV (default)")
        print("  python shopify_upload.py upload    — Upload images to Shopify, then generate CSV")


if __name__ == "__main__":
    main()
