#!/usr/bin/env python3
"""
Image Scraper for Steel Stealer
Reads cleaned spreadsheet, deduplicates by SKU, searches Google Images
in a VISIBLE browser, downloads top 3 results, pads to 2048x2048 white
background for Shopify.
"""

import json
import os
import random
import re
import sys
import time
from io import BytesIO
from urllib.parse import quote_plus

import openpyxl
import requests
from PIL import Image
from playwright.sync_api import sync_playwright

# ── Config ──────────────────────────────────────────────────────────────────

SPREADSHEET = "output/steel_city_parts.xlsx"
IMAGES_DIR = "images"
PROGRESS_FILE = "image_progress.json"
IMAGES_PER_SKU = 3
TARGET_SIZE = 2048
MIN_IMAGE_SIZE = 300
JPEG_QUALITY = 85
MIN_DELAY = 2.0
MAX_DELAY = 4.0
REQUEST_TIMEOUT = 15

# Domains to skip (stock photo sites, competitor watermarks)
SKIP_DOMAINS = [
    "shutterstock.com", "istockphoto.com", "gettyimages.com",
    "dreamstime.com", "123rf.com", "stock.adobe.com",
    "depositphotos.com", "alamy.com",
    "sweepscrub.com", "appliancefactoryparts.com", "scrubbercity",
    "partswarehouse.com",
    "usa-clean.com",
]


# ── Spreadsheet Reading ────────────────────────────────────────────────────

def build_sku_map():
    """Read spreadsheet and build deduplicated SKU map."""
    print(f"Reading {SPREADSHEET}...")
    wb = openpyxl.load_workbook(SPREADSHEET, read_only=True)
    ws = wb.active

    sku_map = {}
    no_sku_parts = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        brand = str(row[0] or "").strip()
        model = str(row[1] or "").strip()
        part_number = str(row[3] or "").strip()
        name = str(row[4] or "").strip()
        sku = str(row[5] or "").strip()
        description = str(row[6] or "").strip()
        price = str(row[7] or "").strip()

        if not sku:
            if part_number:
                key = f"NOSKU-{part_number}-{brand}-{model}"
                no_sku_parts.append({
                    "sku": key,
                    "name": name or description or part_number,
                    "description": description,
                    "price": price,
                    "brand": brand,
                    "brands": {brand},
                    "models": {model},
                    "search_query": f"{part_number} {brand} {model} vacuum part",
                    "folder_name": re.sub(r'[^\w\-]', '_', key),
                })
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
            if clean_name:
                clean_name = re.sub(r'<[^>]+>', ' ', clean_name).strip()
                clean_name = re.sub(r'\s+', ' ', clean_name)

            sku_map[sku] = {
                "sku": sku,
                "name": clean_name or sku,
                "description": description,
                "price": price,
                "brand": brand,
                "brands": {brand} if brand else set(),
                "models": {model} if model else set(),
                "search_query": f"{sku} {clean_name}".strip(),
                "folder_name": re.sub(r'[^\w\-]', '_', sku),
            }

    wb.close()

    for part in no_sku_parts:
        if part["sku"] not in sku_map:
            sku_map[part["sku"]] = part

    print(f"  Total unique products: {len(sku_map)}")
    print(f"  With SKU: {len(sku_map) - len(no_sku_parts)}")
    print(f"  Without SKU (using part number): {len(no_sku_parts)}")

    return sku_map


# ── Progress / Checkpoint ──────────────────────────────────────────────────

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {"completed": [], "failed": [], "no_results": []}


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


# ── Browser-Based Google Image Search ──────────────────────────────────────

def get_thumbnail_elements(page):
    """Get clickable thumbnail image elements from Google Images results."""
    thumbs = page.query_selector_all("img.YQ4gaf")
    result_imgs = []
    for t in thumbs:
        cls = t.get_attribute("class") or ""
        if "zr758c" in cls:
            continue  # site favicon, not a result
        src = t.get_attribute("src") or ""
        alt = t.get_attribute("alt") or ""
        # Real result images have alt text and are base64 encoded or http
        if alt and (src.startswith("data:image/") or src.startswith("http")):
            result_imgs.append(t)
    return result_imgs


def get_fullres_url_after_click(page):
    """After clicking a thumbnail, extract the full-res image URL from the detail panel."""
    try:
        # Wait for the full-res image to load in the side panel
        # It has class 'sFlh5c' and 'iPVvYb' and an http src
        page.wait_for_selector("img.sFlh5c.iPVvYb[src^='http']", timeout=5000)
        time.sleep(0.5)

        # Get the full-res image
        full_imgs = page.query_selector_all("img.sFlh5c.iPVvYb[src^='http']")
        for img in full_imgs:
            src = img.get_attribute("src") or ""
            if src.startswith("http") and "google" not in src and not src.endswith(".svg"):
                # Skip stock photo sites
                if any(domain in src.lower() for domain in SKIP_DOMAINS):
                    continue
                return src
    except Exception:
        pass

    # Fallback: try any non-google http image in the panel
    try:
        all_http_imgs = page.query_selector_all("img.sFlh5c[src^='http']")
        for img in all_http_imgs:
            src = img.get_attribute("src") or ""
            if "google" not in src and "gstatic" not in src:
                if any(domain in src.lower() for domain in SKIP_DOMAINS):
                    continue
                return src
    except Exception:
        pass

    return None


def search_images_browser(page, query):
    """Search Google Images and return full-res URLs for first 3 results."""
    try:
        search_url = f"https://www.google.com/search?q={quote_plus(query)}&tbm=isch"
        page.goto(search_url, timeout=15000)
        time.sleep(2)

        # Get thumbnail elements
        thumbs = get_thumbnail_elements(page)
        if not thumbs:
            return []

        urls = []
        # Click each of the first few thumbnails to get full-res URLs
        for thumb in thumbs[:IMAGES_PER_SKU + 3]:
            if len(urls) >= IMAGES_PER_SKU:
                break
            try:
                thumb.click()
                time.sleep(1)
                url = get_fullres_url_after_click(page)
                if url and url not in urls:
                    urls.append(url)
            except Exception:
                continue

        return urls

    except Exception as e:
        print(f"    Search error: {e}")
        return []


# ── Image Download & Processing ────────────────────────────────────────────

def download_image(url):
    """Download image from URL, return PIL Image or None."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "svg" in content_type or "html" in content_type:
            return None

        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > 20_000_000:
            return None

        img = Image.open(BytesIO(resp.content))

        if img.width < MIN_IMAGE_SIZE or img.height < MIN_IMAGE_SIZE:
            return None

        return img
    except Exception:
        return None


def pad_to_square(img, target_size=TARGET_SIZE):
    """Resize and pad image to target_size x target_size with white background."""
    if img.mode != "RGB":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "RGBA":
            bg.paste(img, mask=img.split()[3])
        else:
            img = img.convert("RGB")
            bg = img
        img = bg

    ratio = min(target_size / img.width, target_size / img.height)
    if ratio < 1:
        new_w = int(img.width * ratio)
        new_h = int(img.height * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    padded = Image.new("RGB", (target_size, target_size), (255, 255, 255))
    x_offset = (target_size - img.width) // 2
    y_offset = (target_size - img.height) // 2
    padded.paste(img, (x_offset, y_offset))

    return padded


def process_sku(page, sku_data, images_dir):
    """Search, download, and pad images for one SKU. Returns number of images saved."""
    sku = sku_data["sku"]
    folder_name = sku_data.get("folder_name", re.sub(r'[^\w\-]', '_', sku))
    sku_dir = os.path.join(images_dir, folder_name)

    # Check if already has enough images
    if os.path.exists(sku_dir):
        existing = [f for f in os.listdir(sku_dir) if f.endswith(".jpg")]
        if len(existing) >= IMAGES_PER_SKU:
            return len(existing)

    query = sku_data["search_query"]
    urls = search_images_browser(page, query)

    if not urls:
        # Retry with just the product name
        name = sku_data.get("name", "")
        if name and name != sku:
            urls = search_images_browser(page, f"{name} vacuum part")

    if not urls:
        return 0

    os.makedirs(sku_dir, exist_ok=True)
    saved = 0

    for url in urls:
        if saved >= IMAGES_PER_SKU:
            break

        img = download_image(url)
        if img is None:
            continue

        padded = pad_to_square(img)
        filepath = os.path.join(sku_dir, f"{saved + 1}.jpg")
        padded.save(filepath, "JPEG", quality=JPEG_QUALITY)
        saved += 1

    return saved


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    test_mode = False
    test_count = 10
    start_from = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--test" and i + 1 < len(args):
            test_mode = True
            test_count = int(args[i + 1])
            i += 2
        elif args[i] == "--start-from" and i + 1 < len(args):
            start_from = args[i + 1]
            i += 2
        else:
            i += 1

    sku_map = build_sku_map()

    progress = load_progress()
    done_set = set(progress["completed"]) | set(progress["failed"]) | set(progress["no_results"])

    all_skus = list(sku_map.keys())
    remaining = [s for s in all_skus if s not in done_set]

    if start_from:
        try:
            idx = remaining.index(start_from)
            remaining = remaining[idx:]
            print(f"Starting from SKU: {start_from} (index {idx})")
        except ValueError:
            print(f"SKU {start_from} not found in remaining list")

    if test_mode:
        remaining = remaining[:test_count]
        print(f"\n=== TEST MODE: Processing {test_count} SKUs ===\n")

    total = len(remaining)
    print(f"\nProgress: {len(progress['completed'])}/{len(all_skus)} completed, {len(progress['no_results'])} no results, {len(progress['failed'])} failed")
    print(f"Remaining: {total} SKUs to process")
    print(f"Images to download: ~{total * IMAGES_PER_SKU}\n")

    if total == 0:
        print("All SKUs already processed!")
        return

    os.makedirs(IMAGES_DIR, exist_ok=True)

    success_count = 0
    fail_count = 0
    no_results_count = 0

    print("Launching browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        for i, sku in enumerate(remaining):
            sku_data = sku_map[sku]
            prefix = f"[{i+1}/{total}]"
            print(f"{prefix} {sku} — \"{sku_data['search_query'][:60]}\"...", end=" ", flush=True)

            try:
                saved = process_sku(page, sku_data, IMAGES_DIR)

                if saved > 0:
                    print(f"✓ {saved} images")
                    progress["completed"].append(sku)
                    success_count += 1
                else:
                    print("✗ no results")
                    progress["no_results"].append(sku)
                    no_results_count += 1

            except KeyboardInterrupt:
                print("\n\nInterrupted! Saving progress...")
                save_progress(progress)
                print(f"Progress saved. {len(progress['completed'])} completed.")
                browser.close()
                sys.exit(0)
            except Exception as e:
                print(f"✗ error: {e}")
                progress["failed"].append(sku)
                fail_count += 1

            if (i + 1) % 10 == 0:
                save_progress(progress)

            if i < total - 1:
                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                time.sleep(delay)

        browser.close()

    save_progress(progress)

    print(f"\n{'='*50}")
    print(f"Done!")
    print(f"  Success: {success_count}")
    print(f"  No results: {no_results_count}")
    print(f"  Failed: {fail_count}")
    print(f"  Total completed: {len(progress['completed'])}")


if __name__ == "__main__":
    main()
