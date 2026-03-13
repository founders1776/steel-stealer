#!/usr/bin/env python3
"""
Steel City Images — Downloads product images from Steel City Vacuum's API,
removes backgrounds/watermarks, adds company logo, and pads for Shopify.

Pipeline:
  1. Discover image URLs via product_info API (picture field)
  2. Download raw images
  3. Remove background + watermark using rembg
  4. Composite with company logo (behind product)
  5. Pad to 2048x2048 and save as JPEG

Usage:
  python3 steel_city_images.py                    # Full pipeline
  python3 steel_city_images.py --step discover    # Only find image URLs
  python3 steel_city_images.py --step download    # Only download images
  python3 steel_city_images.py --step process     # Only process (no browser needed)
  python3 steel_city_images.py --test 10          # Test with first 10 SKUs
"""

import argparse
import base64
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

# Lazy imports — heavy libs only needed for processing step
rembg_remove = None
cv2_module = None

def get_rembg():
    global rembg_remove
    if rembg_remove is None:
        from rembg import remove
        rembg_remove = remove
    return rembg_remove

def get_cv2():
    global cv2_module
    if cv2_module is None:
        import cv2
        cv2_module = cv2
    return cv2_module


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
SPREADSHEET = OUTPUT_DIR / "steel_city_parts.xlsx"
PROGRESS_FILE = BASE_DIR / "steel_city_image_progress.json"
LOGO_PATH = BASE_DIR / "VaM Watermark.png"
DEBUG_DIR = BASE_DIR / "debug"

TARGET_SIZE = 2048
PRODUCT_MAX_SIZE = 1800  # Leave margin for logo visibility
JPEG_QUALITY = 85
LOGO_OPACITY = 0.20
CHECKPOINT_DISCOVER = 50
CHECKPOINT_DOWNLOAD = 10
CHECKPOINT_PROCESS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("steel_city_images")


# ── Progress Tracking ───────────────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {
        "image_urls": {},
        "downloaded": [],
        "processed": [],
        "failed": {},
        "stats": {
            "total_skus": 0,
            "urls_found": 0,
            "no_image": 0,
            "downloaded": 0,
            "processed": 0,
        },
    }


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


# ── Spreadsheet Reading ────────────────────────────────────────────────────

def build_sku_map():
    """Read spreadsheet, return {sku: {part_number, name, folder_name, ...}}."""
    log.info(f"Reading {SPREADSHEET}...")
    wb = openpyxl.load_workbook(str(SPREADSHEET), read_only=True)
    ws = wb.active

    sku_map = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        part_number = str(row[3] or "").strip()
        name = str(row[4] or "").strip()
        sku = str(row[5] or "").strip()

        if not part_number:
            continue

        # Use part_number as the key since the API uses part_number
        if part_number in sku_map:
            continue

        sku_map[part_number] = {
            "sku": sku or part_number,
            "part_number": part_number,
            "name": name,
            "folder_name": re.sub(r'[^\w\-]', '_', sku or part_number),
        }

    wb.close()
    log.info(f"  Found {len(sku_map)} unique SKUs")
    return sku_map


# ── Browser / Auth (reuses scraper.py patterns) ────────────────────────────

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
        if i % 15 == 14:
            log.info(f"Still waiting on Cloudflare... ({(i+1)*2}s)")
    raise TimeoutError("Cloudflare challenge not resolved.")


def login(driver):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    log.info("Navigating to Steel City Vacuum...")
    driver.get(CONFIG["base_url"])
    time.sleep(random.uniform(2, 4))
    wait_for_cloudflare(driver)

    # Check if already logged in
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

    def human_type(element, text):
        element.click()
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.05, 0.12))

    human_type(customer_field, CONFIG["account"])
    time.sleep(random.uniform(0.5, 1.0))
    human_type(driver.find_element(By.ID, "userNameBox"), CONFIG["user_id"])
    time.sleep(random.uniform(0.5, 1.0))
    human_type(driver.find_element(By.ID, "password"), CONFIG["password"])
    time.sleep(random.uniform(0.5, 1.0))

    try:
        driver.find_element(By.CSS_SELECTOR, "a.login-btn").click()
    except Exception:
        driver.execute_script("submitLoginForm()")

    time.sleep(5)
    wait_for_cloudflare(driver)
    log.info("Login complete.")


# ── Step 1: Discover Image URLs ────────────────────────────────────────────

IMAGE_BASE_URL = "https://www.steelcityvac.com/uploads/applications/shopping_cart/"

def get_image_url_from_api(driver, part_id):
    """Call Steel City API and return the large image URL (or None).

    The API returns a `picture` field with just the filename (e.g. '107402274.jpg').
    The large version is at: uploads/applications/shopping_cart/{name}_large.jpg
    Falls back to the regular version if large doesn't exist.
    """
    try:
        result = driver.execute_script("""
            return new Promise((resolve) => {
                $.ajax({
                    type: 'POST',
                    url: 'applications/shopping_cart/web_services.php?action=product_info&name=' + arguments[0],
                    success: function(data) { resolve(JSON.stringify(data)); },
                    error: function() { resolve(null); }
                });
            });
        """, part_id)
        if result:
            data = json.loads(result)
            # Prefer big_picture (e.g. "107402274_large.jpg"), fall back to picture
            big_pic = data.get("big_picture", "")
            picture = data.get("picture", "")
            filename = (big_pic or picture or "").strip()
            if filename:
                return IMAGE_BASE_URL + filename
    except Exception as e:
        log.debug(f"API call failed for {part_id}: {e}")
    return None


def discover_urls(driver, sku_map, progress, limit=None):
    """Query API for each SKU to find image URLs."""
    # Navigate to the site so AJAX calls work (need to be on the same origin)
    driver.get(CONFIG["schematics_url"])
    time.sleep(3)
    wait_for_cloudflare(driver)

    already_discovered = set(progress["image_urls"].keys())
    to_discover = [(sku, info) for sku, info in sku_map.items() if sku not in already_discovered]

    if limit:
        to_discover = to_discover[:limit]

    log.info(f"Discovering image URLs: {len(to_discover)} remaining (of {len(sku_map)} total)")
    found = 0
    no_image = 0

    for i, (sku, info) in enumerate(to_discover):
        part_id = info["part_number"]
        picture = get_image_url_from_api(driver, part_id)

        if picture:
            progress["image_urls"][sku] = picture
            found += 1
        else:
            progress["image_urls"][sku] = None
            progress["failed"][sku] = "no_image"
            no_image += 1

        time.sleep(random.uniform(0.2, 0.5))

        if (i + 1) % CHECKPOINT_DISCOVER == 0:
            progress["stats"]["urls_found"] = sum(1 for v in progress["image_urls"].values() if v)
            progress["stats"]["no_image"] = sum(1 for v in progress["image_urls"].values() if v is None)
            save_progress(progress)
            log.info(f"  [{i+1}/{len(to_discover)}] Found: {found}, No image: {no_image}")

    progress["stats"]["urls_found"] = sum(1 for v in progress["image_urls"].values() if v)
    progress["stats"]["no_image"] = sum(1 for v in progress["image_urls"].values() if v is None)
    save_progress(progress)
    log.info(f"Discovery complete. Found: {found}, No image: {no_image}")


# ── Step 2: Download Images ────────────────────────────────────────────────

def download_image_requests(url):
    """Download image via direct HTTP request. Returns bytes or None."""
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type:
            return None
        return resp.content
    except Exception:
        return None


def download_images(progress, limit=None):
    """Download raw images via direct HTTP (no browser needed)."""
    IMAGES_RAW_DIR.mkdir(exist_ok=True)
    already_downloaded = set(progress["downloaded"])
    urls_with_images = {pn: url for pn, url in progress["image_urls"].items() if url}

    to_download = [(pn, url) for pn, url in urls_with_images.items()
                   if pn not in already_downloaded and pn not in progress.get("failed", {})]
    if limit:
        to_download = to_download[:limit]

    log.info(f"Downloading images: {len(to_download)} remaining")

    downloaded = 0
    for i, (part_number, full_url) in enumerate(to_download):
        folder_name = re.sub(r'[^\w\-]', '_', part_number)
        raw_path = IMAGES_RAW_DIR / f"{folder_name}.jpg"

        if raw_path.exists():
            progress["downloaded"].append(part_number)
            downloaded += 1
            continue

        data = download_image_requests(full_url)

        if data:
            try:
                img = Image.open(BytesIO(data))
                if img.width < 50 or img.height < 50:
                    progress["failed"][part_number] = "too_small"
                    continue
                with open(raw_path, "wb") as f:
                    f.write(data)
                progress["downloaded"].append(part_number)
                downloaded += 1
            except Exception:
                progress["failed"][part_number] = "invalid_image"
        else:
            progress["failed"][part_number] = "download_failed"

        time.sleep(random.uniform(0.05, 0.15))

        if (i + 1) % CHECKPOINT_DOWNLOAD == 0:
            progress["stats"]["downloaded"] = len(progress["downloaded"])
            save_progress(progress)
            log.info(f"  [{i+1}/{len(to_download)}] Downloaded: {downloaded}")

    progress["stats"]["downloaded"] = len(progress["downloaded"])
    save_progress(progress)
    log.info(f"Download complete. Downloaded: {downloaded}")


# ── Step 3-5: Process Images ────────────────────────────────────────────────

def load_logo():
    """Load and prepare the company logo for compositing."""
    if not LOGO_PATH.exists():
        log.warning(f"Logo not found at {LOGO_PATH} — skipping logo step")
        return None
    logo = Image.open(LOGO_PATH).convert("RGBA")
    return logo


def create_background_with_logo(logo, target_size=TARGET_SIZE):
    """Create a white canvas with the logo centered at reduced opacity."""
    canvas = Image.new("RGBA", (target_size, target_size), (255, 255, 255, 255))

    if logo is None:
        return canvas

    # Scale logo to ~80% of canvas size
    logo_scale = 0.80
    logo_w = int(target_size * logo_scale)
    ratio = logo_w / logo.width
    logo_h = int(logo.height * ratio)
    scaled_logo = logo.resize((logo_w, logo_h), Image.LANCZOS)

    # Apply opacity
    r, g, b, a = scaled_logo.split()
    a = a.point(lambda p: int(p * LOGO_OPACITY))
    scaled_logo.putalpha(a)

    # Center on canvas
    x = (target_size - logo_w) // 2
    y = (target_size - logo_h) // 2
    canvas.paste(scaled_logo, (x, y), scaled_logo)

    return canvas


def remove_watermark(img_array):
    """Remove 'www.steelcityvac.com' watermark from image using inpainting.

    Strategy: The watermark is white/light text on a colored background.
    We detect light-colored text regions (especially in the lower portion
    where the watermark typically appears) and inpaint them away.
    """
    cv2 = get_cv2()
    h, w = img_array.shape[:2]

    # Convert to grayscale and HSV for analysis
    gray = cv2.cvtColor(img_array, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img_array, cv2.COLOR_BGR2HSV)

    # The watermark is typically white/very light text on colored backgrounds
    # Detect high-brightness, low-saturation pixels (white/light text)
    # Focus on the lower 40% of the image where watermark usually appears
    lower_region_start = int(h * 0.6)
    mask = np.zeros((h, w), dtype=np.uint8)

    # In the lower region, find bright, low-saturation pixels (watermark text)
    lower_gray = gray[lower_region_start:, :]
    lower_hsv = hsv[lower_region_start:, :]

    # Watermark text: high value (bright), low saturation
    bright_mask = (lower_hsv[:, :, 2] > 200) & (lower_hsv[:, :, 1] < 50)

    # Also check for semi-transparent watermark (slightly lighter than surroundings)
    # Use local contrast: watermark text has edges against the background
    lower_edges = cv2.Canny(lower_gray, 50, 150)

    # Dilate to connect nearby edge components (text characters)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 5))
    dilated_edges = cv2.dilate(lower_edges, kernel, iterations=2)

    # Find contours that look like text lines (wide, short)
    contours, _ = cv2.findContours(dilated_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    text_mask = np.zeros_like(lower_gray)

    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        aspect_ratio = cw / max(ch, 1)
        # Watermark text is typically wide and short (aspect ratio > 3)
        # and covers a reasonable width (> 10% of image width)
        if aspect_ratio > 3 and cw > w * 0.10 and ch < h * 0.08:
            # This looks like a text line — include it in the mask
            cv2.rectangle(text_mask, (x, y), (x + cw, y + ch), 255, -1)

    # Combine: bright text pixels OR detected text-line regions
    combined_lower = np.zeros_like(lower_gray)
    combined_lower[bright_mask] = 255
    combined_lower[text_mask > 0] = 255

    # Only keep connected components that are reasonably sized (not noise)
    # Dilate slightly to connect characters, then erode to clean up
    kernel_small = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
    combined_lower = cv2.dilate(combined_lower, kernel_small, iterations=1)
    combined_lower = cv2.erode(combined_lower, kernel_small, iterations=1)

    mask[lower_region_start:, :] = combined_lower

    # Also check the full image for any bright text that might overlap the product
    # Use a more conservative threshold for the upper portion
    full_bright = (hsv[:, :, 2] > 230) & (hsv[:, :, 1] < 30)
    full_edges = cv2.Canny(gray, 80, 200)
    kernel_text = cv2.getStructuringElement(cv2.MORPH_RECT, (12, 4))
    full_dilated = cv2.dilate(full_edges, kernel_text, iterations=1)

    full_contours, _ = cv2.findContours(full_dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in full_contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        aspect_ratio = cw / max(ch, 1)
        if aspect_ratio > 4 and cw > w * 0.15 and ch < h * 0.06:
            # High-confidence text line anywhere in the image
            region = full_bright[y:y+ch, x:x+cw]
            if np.mean(region) > 0.3:  # At least 30% bright pixels
                cv2.rectangle(mask, (x, y), (x + cw, y + ch), 255, -1)

    # If no watermark detected, return original
    if np.sum(mask) == 0:
        return img_array

    # Dilate mask slightly so inpainting covers edges
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)), iterations=1)

    # Inpaint the watermark regions
    result = cv2.inpaint(img_array, mask, inpaintRadius=7, flags=cv2.INPAINT_TELEA)
    return result


def process_single_image(raw_path, output_path, bg_canvas):
    """Remove background, composite with logo, pad, and save."""
    remove = get_rembg()

    # Read raw image
    with open(raw_path, "rb") as f:
        raw_data = f.read()

    # Remove background
    result_data = remove(raw_data)
    product = Image.open(BytesIO(result_data)).convert("RGBA")

    # Resize product to fit within PRODUCT_MAX_SIZE (scale up or down)
    ratio = min(PRODUCT_MAX_SIZE / product.width, PRODUCT_MAX_SIZE / product.height)
    if ratio != 1.0:
        new_w = int(product.width * ratio)
        new_h = int(product.height * ratio)
        product = product.resize((new_w, new_h), Image.LANCZOS)

    # Composite: logo background + product on top, centered
    composite = bg_canvas.copy()
    x = (TARGET_SIZE - product.width) // 2
    y = (TARGET_SIZE - product.height) // 2
    composite.paste(product, (x, y), product)

    # Convert to RGB and save as JPEG
    final = composite.convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final.save(str(output_path), "JPEG", quality=JPEG_QUALITY)


def process_images(progress, limit=None):
    """Process all downloaded images (bg removal + logo + pad)."""
    IMAGES_DIR.mkdir(exist_ok=True)
    already_processed = set(progress["processed"])
    to_process = [sku for sku in progress["downloaded"] if sku not in already_processed]

    if limit:
        to_process = to_process[:limit]

    log.info(f"Processing images: {len(to_process)} remaining")

    # Pre-load logo and create background canvas
    logo = load_logo()
    bg_canvas = create_background_with_logo(logo)
    log.info(f"Background canvas ready (logo: {'yes' if logo else 'no'})")

    processed = 0
    errors = 0

    for i, sku in enumerate(to_process):
        folder_name = re.sub(r'[^\w\-]', '_', sku)
        raw_path = IMAGES_RAW_DIR / f"{folder_name}.jpg"
        output_path = IMAGES_DIR / folder_name / "1.jpg"

        if not raw_path.exists():
            progress["failed"][sku] = "raw_missing"
            errors += 1
            continue

        try:
            process_single_image(raw_path, output_path, bg_canvas)
            progress["processed"].append(sku)
            processed += 1
        except Exception as e:
            log.warning(f"  Failed to process {sku}: {e}")
            progress["failed"][sku] = f"process_error: {str(e)[:100]}"
            errors += 1

        if (i + 1) % CHECKPOINT_PROCESS == 0:
            progress["stats"]["processed"] = len(progress["processed"])
            save_progress(progress)
            log.info(f"  [{i+1}/{len(to_process)}] Processed: {processed}, Errors: {errors}")

    progress["stats"]["processed"] = len(progress["processed"])
    save_progress(progress)
    log.info(f"Processing complete. Processed: {processed}, Errors: {errors}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Steel City Image Pipeline")
    parser.add_argument("--step", choices=["discover", "download", "process", "all"], default="all",
                        help="Which pipeline step to run")
    parser.add_argument("--test", type=int, default=None,
                        help="Test mode: only process N SKUs")
    args = parser.parse_args()

    progress = load_progress()
    sku_map = build_sku_map()
    progress["stats"]["total_skus"] = len(sku_map)

    needs_browser = args.step in ("discover", "all")
    driver = None

    try:
        if needs_browser:
            driver = create_driver()
            login(driver)

        if args.step in ("discover", "all"):
            log.info("=" * 60)
            log.info("STEP 1: Discovering image URLs")
            log.info("=" * 60)
            discover_urls(driver, sku_map, progress, limit=args.test)

        if args.step in ("download", "all"):
            log.info("=" * 60)
            log.info("STEP 2: Downloading images (direct HTTP)")
            log.info("=" * 60)
            download_images(progress, limit=args.test)

        if args.step in ("process", "all"):
            log.info("=" * 60)
            log.info("STEP 3-5: Processing images (bg removal + logo + pad)")
            log.info("=" * 60)
            process_images(progress, limit=args.test)

    except KeyboardInterrupt:
        log.info("Interrupted — progress saved.")
        save_progress(progress)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    # Print summary
    s = progress["stats"]
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info(f"  Total SKUs:    {s.get('total_skus', 0)}")
    log.info(f"  URLs found:    {s.get('urls_found', 0)}")
    log.info(f"  No image:      {s.get('no_image', 0)}")
    log.info(f"  Downloaded:    {s.get('downloaded', 0)}")
    log.info(f"  Processed:     {s.get('processed', 0)}")
    log.info(f"  Failed:        {len(progress.get('failed', {}))}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
