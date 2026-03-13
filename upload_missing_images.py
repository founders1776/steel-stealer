#!/usr/bin/env python3
"""Upload images for product_names.json SKUs that aren't yet in image_urls.json."""

import json
import os
import re
import sys
import time

# Reuse the upload functions from shopify_upload
os.environ.setdefault("SHOPIFY_STORE", "")
os.environ.setdefault("SHOPIFY_ACCESS_TOKEN", "")

from shopify_upload import (
    upload_image_to_shopify,
    IMAGES_DIR,
    IMAGE_URLS_FILE,
    SHOPIFY_STORE,
    SHOPIFY_ACCESS_TOKEN,
)

def main():
    if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
        print("ERROR: Set SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN environment variables.")
        sys.exit(1)

    # Load products
    with open("product_names.json") as f:
        products = json.load(f)

    # Load existing uploads
    url_map = {}
    if os.path.exists(IMAGE_URLS_FILE):
        with open(IMAGE_URLS_FILE) as f:
            url_map = json.load(f)

    # Find SKUs with images on disk but not uploaded
    to_upload = []
    for sku in products:
        if sku in url_map and url_map[sku]:
            continue
        folder = re.sub(r'[^\w\-]', '_', sku)
        sku_dir = os.path.join(IMAGES_DIR, folder)
        if os.path.exists(sku_dir):
            images = sorted([f for f in os.listdir(sku_dir) if f.endswith(".jpg")])
            if images:
                to_upload.append((sku, folder, sku_dir, images))

    total = len(to_upload)
    print(f"Found {total} SKUs with images to upload (from product_names.json)")
    if not total:
        print("Nothing to do!")
        return

    for i, (sku, folder, sku_dir, images) in enumerate(to_upload):
        print(f"[{i+1}/{total}] {sku}...", end=" ", flush=True)

        urls = []
        for img_file in images:
            filepath = os.path.join(sku_dir, img_file)
            filename = f"{folder}_{img_file}"
            url = upload_image_to_shopify(filepath, filename)
            if url:
                urls.append(url)
            time.sleep(0.5)

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

    print(f"\nDone! {len(url_map)} total SKUs with uploaded images.")


if __name__ == "__main__":
    main()
