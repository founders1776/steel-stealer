"""
Steel Stealer — Scrapes parts data from Steel City Vacuum schematics portal.
Uses undetected-chromedriver to bypass Cloudflare Turnstile.
Outputs to Excel spreadsheet.

Site structure:
  Schematics main page → Brands (54, paginated 20/page)
    → Brand page → Models (paginated 20/page)
      → Model page → Schematic image with clickable parts OR sub-models
        → Parts data (part number, description, price, etc.)

Selectors discovered:
  Brands/Models: .gallery-product-wrapper[data-name], a.prod-name
  Pagination: .paging a (offset param)
  URL pattern: a/g/?t=1&gid=1&folder=/Brand/Model&image=Model-Page_P.jpg
  Folder models have "Schematics" button, direct schematics have "Displaying" button
"""

import json
import logging
import os
import random
import re
import time
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, StaleElementReferenceException,
    WebDriverException
)
import undetected_chromedriver as uc

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG = {
    "base_url": "https://www.steelcityvac.com",
    "schematics_url": "https://www.steelcityvac.com/a/g/?t=1&gid=1&folder=",
    "account": "REDACTED_ACCT",
    "user_id": "REDACTED_USER",
    "password": "REDACTED_PASS",
    "delay_min": 1.5,
    "delay_max": 4.0,
}

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
PROGRESS_FILE = BASE_DIR / "progress.json"
DEBUG_DIR = BASE_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("steel_stealer")


# ── Helpers ─────────────────────────────────────────────────────────────────

def random_delay(lo=None, hi=None):
    lo = lo or CONFIG["delay_min"]
    hi = hi or CONFIG["delay_max"]
    time.sleep(random.uniform(lo, hi))


def human_type(element, text, delay_range=(0.05, 0.12)):
    element.click()
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(*delay_range))


def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed_models": {}, "parts": []}


def save_progress(data):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def export_to_excel(parts, filename="steel_city_parts.xlsx"):
    OUTPUT_DIR.mkdir(exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "All Parts"

    headers = ["Brand", "Model", "Schematic Page", "Part Number", "SKU",
               "Description", "Price", "Notes"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for part in parts:
        ws.append([
            part.get("brand", ""),
            part.get("model", ""),
            part.get("schematic_page", ""),
            part.get("part_number", ""),
            part.get("sku", ""),
            part.get("description", ""),
            part.get("price", ""),
            part.get("notes", ""),
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)
    ws.freeze_panes = "A2"

    filepath = OUTPUT_DIR / filename
    wb.save(filepath)
    log.info(f"Excel saved: {filepath} ({len(parts)} parts)")


def retry_on_error(func, *args, retries=MAX_RETRIES, **kwargs):
    """Retry a function on WebDriverException (network errors, etc.)."""
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except (WebDriverException, TimeoutError) as e:
            err_msg = str(e)
            if attempt < retries - 1 and any(kw in err_msg for kw in [
                "DISCONNECTED", "timeout", "ERR_NAME", "ERR_CONNECTION",
                "ERR_INTERNET", "net::", "chrome not reachable"
            ]):
                wait_time = RETRY_DELAY * (2 ** attempt)
                log.warning(f"Network error (attempt {attempt+1}/{retries}), retrying in {wait_time}s: {err_msg[:100]}")
                time.sleep(wait_time)
            else:
                raise


def screenshot(driver, name):
    path = str(DEBUG_DIR / f"{name}.png")
    try:
        driver.save_screenshot(path)
    except Exception:
        pass


def save_html(driver, name):
    path = str(DEBUG_DIR / f"{name}.html")
    try:
        with open(path, "w") as f:
            f.write(driver.page_source)
    except Exception:
        pass


# ── Cloudflare ──────────────────────────────────────────────────────────────

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


# ── Browser & Login ─────────────────────────────────────────────────────────

def create_driver():
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument(f"--user-data-dir={BASE_DIR / 'browser_data'}")
    return uc.Chrome(options=options, headless=False, version_main=145)


def login(driver):
    log.info("Navigating to Steel City Vacuum...")
    driver.get(CONFIG["base_url"])
    random_delay()
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

    # Reveal login panel
    try:
        driver.find_element(By.CSS_SELECTOR, "a.nav-login-btn").click()
        time.sleep(1)
    except Exception:
        pass

    wait = WebDriverWait(driver, 10)
    customer_field = wait.until(EC.visibility_of_element_located((By.ID, "scvCustomerNumber")))
    human_type(customer_field, CONFIG["account"])
    random_delay(0.5, 1.0)

    human_type(driver.find_element(By.ID, "userNameBox"), CONFIG["user_id"])
    random_delay(0.5, 1.0)

    human_type(driver.find_element(By.ID, "password"), CONFIG["password"])
    random_delay(0.5, 1.0)

    # Submit
    try:
        driver.find_element(By.CSS_SELECTOR, "a.login-btn").click()
    except Exception:
        driver.execute_script("submitLoginForm()")

    time.sleep(5)
    wait_for_cloudflare(driver)

    screenshot(driver, "post_login")
    log.info(f"Post-login URL: {driver.current_url}")

    try:
        nav = driver.find_element(By.ID, "main-nav")
        if "notlogged" not in (nav.get_attribute("class") or "").lower():
            log.info("Login successful!")
        else:
            log.error("Login may have failed.")
    except Exception:
        log.warning("Could not verify login.")


# ── Gallery Navigation ──────────────────────────────────────────────────────

def get_all_gallery_items(driver, base_url):
    """Get all items from a paginated gallery page. Returns list of {name, url, type}."""
    items = []
    seen_names = set()

    retry_on_error(driver.get, base_url)
    random_delay(1.0, 2.0)
    wait_for_cloudflare(driver)

    while True:
        wrappers = driver.find_elements(By.CSS_SELECTOR, ".gallery-product-wrapper")
        for wrapper in wrappers:
            try:
                name = wrapper.get_attribute("data-name")
                if not name or name in seen_names:
                    continue
                seen_names.add(name)

                # Determine type: "Schematics" button = subfolder, "Displaying" = direct schematic
                link_el = wrapper.find_element(By.CSS_SELECTOR, "a.prod-name")
                url = link_el.get_attribute("href")

                try:
                    quick_btn = wrapper.find_element(By.CSS_SELECTOR, ".gallery-quick-tools a.frequent-buy")
                    btn_text = quick_btn.text.strip().lower()
                except Exception:
                    btn_text = "unknown"

                item_type = "folder" if "schematic" in btn_text else "schematic"
                items.append({"name": name, "url": url, "type": item_type})
            except (StaleElementReferenceException, NoSuchElementException):
                continue

        # Check for next page
        try:
            paging = driver.find_element(By.CSS_SELECTOR, ".paging")
            next_links = paging.find_elements(By.LINK_TEXT, "Next")
            if next_links:
                next_url = next_links[0].get_attribute("href")
                log.info(f"  Navigating to next page...")
                retry_on_error(driver.get, next_url)
                random_delay(1.0, 2.0)
                wait_for_cloudflare(driver)
            else:
                break
        except NoSuchElementException:
            break

    return items


def get_brands(driver):
    """Get all brands from the schematics main page."""
    log.info("Getting all brands...")
    brands = get_all_gallery_items(driver, CONFIG["schematics_url"])
    log.info(f"Found {len(brands)} brands")
    for b in brands:
        log.info(f"  {b['name']}")
    return brands


def get_models(driver, brand):
    """Get all models for a brand."""
    log.info(f"Getting models for: {brand['name']}...")
    models = get_all_gallery_items(driver, brand["url"])
    log.info(f"  Found {len(models)} models for {brand['name']}")
    return models


# ── Schematic Parts Extraction ──────────────────────────────────────────────

def get_part_info_via_api(driver, part_id):
    """Call the Steel City product info API to get full part details.

    API: POST applications/shopping_cart/web_services.php?action=product_info&name={part_id}
    Returns JSON with: name, product_code, description, productID, Price_1, etc.
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
            return json.loads(result)
    except Exception as e:
        log.debug(f"    API call failed for {part_id}: {e}")
    return None


def extract_schematic_parts(driver, brand_name, model_name, url, page_num=1):
    """Extract parts from a single schematic page by reading area tags and calling the API."""
    retry_on_error(driver.get, url)
    random_delay(1.0, 2.0)
    wait_for_cloudflare(driver)

    parts = []
    safe = f"{brand_name}_{model_name}_p{page_num}".replace("/", "_").replace(" ", "_")

    # Get all part IDs from image map areas
    areas = driver.find_elements(By.TAG_NAME, "area")
    part_ids = []
    for area in areas:
        title = area.get_attribute("title") or ""
        if title:
            pid = title
        else:
            # Some pages have empty title but part ID in href: schematicPartClicked('PART_ID', ...)
            href = area.get_attribute("href") or ""
            m = re.search(r"schematicPartClicked\('([^']+)'", href)
            pid = m.group(1) if m else ""
        if pid and pid != "nohref" and pid not in part_ids:
            part_ids.append(pid)

    if not part_ids:
        log.info(f"    No hotspots found for {brand_name}/{model_name} page {page_num}")
        # Check for sub-schematics
        sub_schematics = _get_sub_schematics(driver)
        return parts, sub_schematics

    log.info(f"    Found {len(part_ids)} unique parts on {brand_name}/{model_name} page {page_num}")

    # Call API for each part to get full details
    for part_id in part_ids:
        part = {
            "brand": brand_name,
            "model": model_name,
            "schematic_page": page_num,
            "part_number": part_id,
            "sku": "",
            "description": "",
            "price": "",
            "notes": "",
        }

        # Handle special status codes
        if part_id == "NLA":
            part["description"] = "No Longer Available"
            part["price"] = "NLA"
            parts.append(part)
            continue
        if part_id == "NOF":
            part["description"] = "Not in system"
            part["price"] = "N/A"
            parts.append(part)
            continue

        data = get_part_info_via_api(driver, part_id)
        time.sleep(random.uniform(0.2, 0.5))  # Small delay between API calls

        if data and data.get("name"):
            part["sku"] = data.get("product_code", "")
            part["description"] = data.get("name", "")
            if data.get("description"):
                desc = data["description"]
                part["description"] += f" - {desc}"
                # Check if description indicates NLA
                if "NLA" in desc.upper() or "NO LONGER AVAILABLE" in desc.upper():
                    part["price"] = "NLA"
                    # Check for alternate items
                    alt_items = data.get("alt_items", [])
                    if alt_items:
                        alt_strs = []
                        for alt in alt_items:
                            if not alt or not alt.get("name"):
                                continue
                            alt_str = alt["name"]
                            if alt.get("product_code"):
                                alt_str += f" ({alt['product_code']})"
                            alt_strs.append(alt_str)
                        if alt_strs:
                            part["notes"] = "ALT: " + "; ".join(alt_strs)
                    parts.append(part)
                    continue

            price = data.get("Price_1")
            if price:
                try:
                    part["price"] = f"${float(price):.2f}"
                except (ValueError, TypeError):
                    part["price"] = str(price)

            # Volume pricing
            vol_prices = []
            for qty in ["5", "10", "25", "50"]:
                p = data.get(f"Price_{qty}")
                if p:
                    try:
                        vol_prices.append(f"{qty}+: ${float(p):.2f}")
                    except (ValueError, TypeError):
                        pass
            if vol_prices:
                part["notes"] = " | ".join(vol_prices)

            # Check for alternate items on non-NLA parts too
            alt_items = data.get("alt_items", [])
            if alt_items:
                alt_strs = []
                for alt in alt_items:
                    if not alt or not alt.get("name"):
                        continue
                    alt_str = alt["name"]
                    if alt.get("product_code"):
                        alt_str += f" ({alt['product_code']})"
                    alt_strs.append(alt_str)
                if alt_strs:
                    alt_note = "ALT: " + "; ".join(alt_strs)
                    part["notes"] = (part["notes"] + " | " + alt_note) if part["notes"] else alt_note
        else:
            part["description"] = "Not found in system"
            part["price"] = "N/A"

        parts.append(part)

    log.info(f"    Extracted {len(parts)} parts from {brand_name}/{model_name} page {page_num}")

    sub_schematics = _get_sub_schematics(driver)
    return parts, sub_schematics


def _get_sub_schematics(driver):
    """Check for sub-schematics (multiple pages within a model)."""
    sub_schematics = []
    wrappers = driver.find_elements(By.CSS_SELECTOR, ".gallery-product-wrapper")
    for wrapper in wrappers:
        try:
            name = wrapper.get_attribute("data-name")
            link = wrapper.find_element(By.CSS_SELECTOR, "a.prod-name")
            href = link.get_attribute("href")
            try:
                btn = wrapper.find_element(By.CSS_SELECTOR, ".gallery-quick-tools a.frequent-buy")
                btn_text = btn.text.strip().lower()
            except Exception:
                btn_text = ""
            if "displaying" in btn_text and href:
                sub_schematics.append({"name": name, "url": href})
        except Exception:
            continue
    return sub_schematics


def process_model(driver, brand_name, model):
    """Process a model — might be a direct schematic or a subfolder with multiple schematics."""
    all_parts = []

    if model["type"] == "schematic":
        # Direct schematic image — extract parts
        parts, _ = extract_schematic_parts(driver, brand_name, model["name"], model["url"])
        all_parts.extend(parts)
    else:
        # Folder — contains sub-models or sub-schematics
        sub_items = get_all_gallery_items(driver, model["url"])

        for i, sub in enumerate(sub_items):
            if sub["type"] == "schematic":
                parts, _ = extract_schematic_parts(
                    driver, brand_name, model["name"],
                    sub["url"], page_num=i+1
                )
                all_parts.extend(parts)
            else:
                # Nested subfolder — go one more level deep
                log.info(f"    Nested subfolder: {sub['name']}")
                nested_items = get_all_gallery_items(driver, sub["url"])
                for j, nested in enumerate(nested_items):
                    if nested["type"] == "schematic":
                        parts, _ = extract_schematic_parts(
                            driver, brand_name, f"{model['name']}/{sub['name']}",
                            nested["url"], page_num=j+1
                        )
                        all_parts.extend(parts)
            random_delay(0.5, 1.5)

    return all_parts


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    log.info("=== Steel Stealer starting ===")

    progress = load_progress()
    all_parts = progress.get("parts", [])

    driver = create_driver()

    try:
        login(driver)

        # Navigate to Schematics
        log.info("Navigating to Schematics...")
        try:
            schematics_link = driver.find_element(By.LINK_TEXT, "SCHEMATICS")
            schematics_link.click()
            time.sleep(3)
            wait_for_cloudflare(driver)
        except Exception:
            driver.get(CONFIG["schematics_url"])
            random_delay()
            wait_for_cloudflare(driver)

        screenshot(driver, "schematics_main")

        # Get all brands
        brands = get_brands(driver)
        if not brands:
            log.error("No brands found.")
            return

        # Process each brand
        for brand in brands:
            brand_key = brand["name"]
            completed_models = progress["completed_models"].get(brand_key, [])

            try:
                models = get_models(driver, brand)
            except (WebDriverException, Exception) as e:
                log.error(f"Error getting models for {brand_key}: {e}")
                log.info("Waiting 10s and retrying...")
                time.sleep(10)
                try:
                    models = get_models(driver, brand)
                except Exception as e2:
                    log.error(f"Retry failed for {brand_key}, skipping: {e2}")
                    continue

            for model in models:
                model_key = model["name"]
                if model_key in completed_models:
                    log.info(f"  Skipping completed: {brand_key}/{model_key}")
                    continue

                log.info(f"  Processing: {brand_key}/{model_key} ({model['type']})")
                try:
                    parts = process_model(driver, brand_key, model)
                    all_parts.extend(parts)
                except (WebDriverException, Exception) as e:
                    err_msg = str(e)
                    if any(kw in err_msg for kw in ["DISCONNECTED", "ERR_INTERNET", "net::"]):
                        log.warning(f"  Network error on {brand_key}/{model_key}, waiting 15s...")
                        time.sleep(15)
                        try:
                            parts = process_model(driver, brand_key, model)
                            all_parts.extend(parts)
                        except Exception as e2:
                            log.error(f"  Retry failed for {brand_key}/{model_key}: {e2}")
                            screenshot(driver, f"error_{brand_key}_{model_key}")
                    else:
                        log.error(f"  Error processing {brand_key}/{model_key}: {e}")
                        screenshot(driver, f"error_{brand_key}_{model_key}")

                # Checkpoint
                if brand_key not in progress["completed_models"]:
                    progress["completed_models"][brand_key] = []
                progress["completed_models"][brand_key].append(model_key)
                progress["parts"] = all_parts
                save_progress(progress)

                random_delay()

            # Export after each brand
            if all_parts:
                export_to_excel(all_parts)
            log.info(f"Completed brand: {brand_key}")

        if all_parts:
            export_to_excel(all_parts)
            log.info(f"=== Done! {len(all_parts)} total parts exported. ===")
        else:
            log.warning("No parts extracted. Check debug/ folder.")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
