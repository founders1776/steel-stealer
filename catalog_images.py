#!/usr/bin/env python3
"""
Downloads and processes images for catalog products (non-schematic).
Reuses the image processing pipeline from steel_city_images.py.

Reads catalog_progress.json for image URLs, downloads to images_raw/,
processes (bg removal + logo + pad) to images/.
"""

import json
import logging
import os
import random
import re
import time
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

# Lazy imports
rembg_remove = None

def get_rembg():
    global rembg_remove
    if rembg_remove is None:
        from rembg import remove
        rembg_remove = remove
    return rembg_remove

BASE_DIR = Path(__file__).parent
IMAGES_DIR = BASE_DIR / "images"
IMAGES_RAW_DIR = BASE_DIR / "images_raw"
CATALOG_PROGRESS = BASE_DIR / "catalog_progress.json"
LOGO_PATH = BASE_DIR / "VaM Watermark.png"

TARGET_SIZE = 2048
PRODUCT_MAX_SIZE = 1800
JPEG_QUALITY = 85
LOGO_OPACITY = 0.20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("catalog_images")


def load_logo():
    if not LOGO_PATH.exists():
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


def download_image(url):
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        if "image" not in resp.headers.get("Content-Type", ""):
            return None
        return resp.content
    except Exception:
        return None


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


def main():
    with open(CATALOG_PROGRESS) as f:
        progress = json.load(f)

    enriched = progress.get("enriched_products", {})
    products_with_images = {
        k: v for k, v in enriched.items()
        if v.get("image_url")
    }

    log.info(f"Catalog products with images: {len(products_with_images)}")

    IMAGES_RAW_DIR.mkdir(exist_ok=True)
    IMAGES_DIR.mkdir(exist_ok=True)

    # Download phase
    downloaded = 0
    skipped = 0
    for i, (code, product) in enumerate(products_with_images.items()):
        folder_name = re.sub(r'[^\w\-]', '_', product["part_number"])
        raw_path = IMAGES_RAW_DIR / f"{folder_name}.jpg"

        if raw_path.exists():
            skipped += 1
            continue

        data = download_image(product["image_url"])
        if data:
            try:
                img = Image.open(BytesIO(data))
                if img.width >= 50 and img.height >= 50:
                    with open(raw_path, "wb") as f:
                        f.write(data)
                    downloaded += 1
            except Exception:
                pass

        time.sleep(random.uniform(0.05, 0.15))
        if (i + 1) % 50 == 0:
            log.info(f"  Download [{i+1}/{len(products_with_images)}] Downloaded: {downloaded}, Skipped (exists): {skipped}")

    log.info(f"Download complete. New: {downloaded}, Already existed: {skipped}")

    # Process phase
    logo = load_logo()
    bg_canvas = create_background_with_logo(logo)
    processed = 0
    errors = 0

    for i, (code, product) in enumerate(products_with_images.items()):
        folder_name = re.sub(r'[^\w\-]', '_', product["part_number"])
        raw_path = IMAGES_RAW_DIR / f"{folder_name}.jpg"
        output_path = IMAGES_DIR / folder_name / "1.jpg"

        if output_path.exists():
            continue
        if not raw_path.exists():
            continue

        try:
            process_single_image(raw_path, output_path, bg_canvas)
            processed += 1
        except Exception as e:
            log.warning(f"  Failed to process {code}: {e}")
            errors += 1

        if (i + 1) % 25 == 0:
            log.info(f"  Process [{i+1}/{len(products_with_images)}] Processed: {processed}, Errors: {errors}")

    log.info(f"Processing complete. Processed: {processed}, Errors: {errors}")
    log.info(f"Total catalog images ready: {processed + skipped}")


if __name__ == "__main__":
    main()
