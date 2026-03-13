#!/usr/bin/env python3
"""
Generate Shopify Import CSV with Intelligent Auto-Tagging

Reads product_names.json, applies auto-tagging by category/brand/model,
deduplicates by SKU, excludes existing store products, and outputs
a Shopify-formatted CSV ready for import.

Output: output/shopify_import.csv
"""

import csv
import json
import os
import re
import sys

# ── Config ──────────────────────────────────────────────────────────────────

PRODUCT_NAMES_FILE = "product_names.json"
IMAGE_URLS_FILE = "image_urls.json"
EXISTING_PRODUCTS_CSV = "output/shopify_current_products.csv"
OUTPUT_CSV = "output/shopify_import.csv"
IMAGES_DIR = "images"

# ── Tagging ─────────────────────────────────────────────────────────────────

# Broad tags — each product gets exactly one (checked in order, first match wins)
BROAD_TAG_RULES = [
    ("Bags", re.compile(r'\bbags?\b(?!less|ged)', re.IGNORECASE)),
    ("Filters", re.compile(r'\bfilters?\b|\bhepa\b', re.IGNORECASE)),
    ("Attachments", re.compile(
        r'\b(hose|wand|crevice|nozzle|floor\s*(brush|tool)|dust\s*brush|upholstery|turbo|attachment)\b',
        re.IGNORECASE
    )),
    ("Vacuums", None),  # Special — handled in code
]

# Keywords that indicate a part, not a full vacuum
PART_KEYWORDS = re.compile(
    r'\b(motor|belt|wheel|switch|cord|gasket|bearing|fan|brush\s*roll|seal|'
    r'spring|screw|clip|latch|handle|bag|filter|hose|wand|nozzle|cap|cover|'
    r'tube|duct|bumper|bracket|roller|axle|housing|board|sensor|valve|plug|'
    r'replacement|repair)\b',
    re.IGNORECASE
)

# Specific tags — multiple can apply
SPECIFIC_TAG_RULES = [
    ("Belts", re.compile(r'\bbelts?\b', re.IGNORECASE)),
    ("Hoses", re.compile(r'\bhoses?\b', re.IGNORECASE)),
    ("Wands", re.compile(r'\bwands?\b', re.IGNORECASE)),
    ("Floor Nozzles", re.compile(
        r'\b(power\s*?(head|nozzle)|floor\s*?(nozzle|brush|tool))\b', re.IGNORECASE
    )),
    ("Upright Vacuums", re.compile(r'\bupright\b', re.IGNORECASE)),
    ("Canister Vacuums", re.compile(r'\bcanister\b', re.IGNORECASE)),
    ("Central Vacuums", re.compile(r'\bcentral\b', re.IGNORECASE)),
    ("Stick Vacuums", re.compile(r'\bstick\b', re.IGNORECASE)),
    ("Handheld Vacuums", re.compile(r'\bhand\s*?held\b', re.IGNORECASE)),
    ("Backpack Vacuums", re.compile(r'\bback\s*?pack\b', re.IGNORECASE)),
]

# Brand-based specific tags
CENTRAL_VAC_BRANDS = {"beam", "nutone", "cen-tec", "centec"}
BACKPACK_BRANDS = {"proteam"}


def generate_tags(product):
    """Generate all tags for a product. Returns list of tag strings."""
    name = product.get("clean_name", "")
    brand_str = product.get("brand", "")
    model = product.get("model", "")

    tags = []

    # ── Broad tag (exactly one) ──
    broad = "Parts"  # default
    for tag_name, pattern in BROAD_TAG_RULES:
        if tag_name == "Vacuums":
            # Only if "vacuum" in name AND no part keywords
            if re.search(r'\bvacuum\b', name, re.IGNORECASE) and not PART_KEYWORDS.search(name):
                broad = "Vacuums"
                break
        elif pattern and pattern.search(name):
            broad = tag_name
            break
    tags.append(broad)

    # ── Specific tags (multiple) ──
    for tag_name, pattern in SPECIFIC_TAG_RULES:
        if pattern.search(name):
            if tag_name != broad:  # avoid duplicating broad tag concept
                tags.append(tag_name)

    # Brand-based specific tags
    brands_lower = {b.strip().lower() for b in brand_str.split(",") if b.strip()}
    if brands_lower & CENTRAL_VAC_BRANDS:
        if "Central Vacuums" not in tags:
            tags.append("Central Vacuums")
    if brands_lower & BACKPACK_BRANDS:
        if "Backpack Vacuums" not in tags:
            tags.append("Backpack Vacuums")

    # ── Brand tags ──
    for b in brand_str.split(","):
        b = b.strip()
        if b:
            tags.append(b)

    # ── Model tag ──
    if model:
        tags.append(model)

    return tags


# ── Helpers ─────────────────────────────────────────────────────────────────

def slugify(text):
    """Convert text to URL-friendly handle."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')


def parse_price(price_str):
    """Extract numeric price from '$11.92' format."""
    if not price_str:
        return ""
    match = re.search(r'[\d]+\.?\d*', str(price_str))
    return match.group() if match else ""


def load_existing_skus():
    """Load SKUs from existing Shopify store export."""
    skus = set()
    if not os.path.exists(EXISTING_PRODUCTS_CSV):
        print(f"  Warning: {EXISTING_PRODUCTS_CSV} not found — skipping dedup")
        return skus

    with open(EXISTING_PRODUCTS_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = row.get("Variant SKU", "").strip()
            if sku:
                skus.add(sku)

    print(f"  Loaded {len(skus)} existing store SKUs")
    return skus


def load_image_urls():
    """Load Shopify CDN image URLs if available."""
    if not os.path.exists(IMAGE_URLS_FILE):
        return {}
    with open(IMAGE_URLS_FILE, "r") as f:
        data = json.load(f)
    # Only return entries with actual URLs
    return {k: v for k, v in data.items() if v}


def get_image_src(sku, image_urls):
    """Get image source for a SKU — CDN URL or local path."""
    if sku in image_urls and image_urls[sku]:
        return image_urls[sku][0]

    # Check for local processed image
    folder = re.sub(r'[^\w\-]', '_', sku)
    img_path = os.path.join(IMAGES_DIR, folder, "1.jpg")
    if os.path.exists(img_path):
        return img_path

    # Also check with original SKU as folder name
    img_path2 = os.path.join(IMAGES_DIR, sku, "1.jpg")
    if os.path.exists(img_path2):
        return img_path2

    return ""


# ── Deduplication ───────────────────────────────────────────────────────────

def dedup_by_sku(products):
    """Deduplicate products by SKU. Merge: combine brands, keep longest name."""
    sku_map = {}

    for key, prod in products.items():
        sku = prod.get("sku", "")
        if not sku:
            continue

        if sku in sku_map:
            existing = sku_map[sku]
            # Merge brands
            existing_brands = {b.strip() for b in existing.get("brand", "").split(",") if b.strip()}
            new_brands = {b.strip() for b in prod.get("brand", "").split(",") if b.strip()}
            combined = sorted(existing_brands | new_brands)
            existing["brand"] = ",".join(combined)
            # Keep longest clean_name
            if len(prod.get("clean_name", "")) > len(existing.get("clean_name", "")):
                existing["clean_name"] = prod["clean_name"]
            # Keep description if longer
            if len(prod.get("description", "")) > len(existing.get("description", "")):
                existing["description"] = prod["description"]
        else:
            sku_map[sku] = dict(prod)  # copy

    return sku_map


# ── CSV Generation ──────────────────────────────────────────────────────────

SHOPIFY_HEADERS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Product Type", "Tags",
    "Published", "Option1 Name", "Option1 Value", "Variant SKU",
    "Variant Price", "Variant Cost per item", "Variant Compare At Price",
    "Variant Inventory Policy", "Variant Fulfillment Service",
    "Variant Requires Shipping", "Image Src", "Image Position",
    "Variant Weight Unit", "Status",
]


def generate_csv():
    """Main CSV generation pipeline."""
    print("=" * 60)
    print("Shopify Import CSV Generator")
    print("=" * 60)

    # 1. Load products
    print(f"\n1. Loading {PRODUCT_NAMES_FILE}...")
    with open(PRODUCT_NAMES_FILE, "r") as f:
        all_products = json.load(f)
    print(f"   {len(all_products)} total entries")

    # 2. Dedup by SKU
    print("\n2. Deduplicating by SKU...")
    sku_products = dedup_by_sku(all_products)
    dup_count = len(all_products) - len(sku_products)
    print(f"   {len(sku_products)} unique SKUs ({dup_count} duplicates merged)")

    # 3. Exclude existing store SKUs
    print("\n3. Excluding existing store products...")
    existing_skus = load_existing_skus()
    before = len(sku_products)
    sku_products = {s: p for s, p in sku_products.items() if s not in existing_skus}
    excluded = before - len(sku_products)
    print(f"   Excluded {excluded} already in store → {len(sku_products)} remaining")

    # 4. Skip products without retail price
    print("\n4. Filtering products without retail price...")
    before = len(sku_products)
    sku_products = {s: p for s, p in sku_products.items() if parse_price(p.get("retail_price", ""))}
    skipped_price = before - len(sku_products)
    print(f"   Skipped {skipped_price} with no retail price → {len(sku_products)} remaining")

    # 4b. Exclude SEBO products (MAP pricing — imported separately from dealer spreadsheet)
    before = len(sku_products)
    sku_products = {s: p for s, p in sku_products.items()
                    if (p.get("brand", "") or "").upper().strip() != "SEBO"}
    sebo_excluded = before - len(sku_products)
    if sebo_excluded:
        print(f"   Excluded {sebo_excluded} SEBO products (MAP pricing) → {len(sku_products)} remaining")

    # 5. Load image URLs
    print("\n5. Loading image URLs...")
    image_urls = load_image_urls()
    print(f"   {len(image_urls)} SKUs with CDN URLs")

    # 6. Generate tags + build rows
    print("\n6. Generating tags and building CSV rows...")
    tag_counts = {}
    broad_counts = {}
    rows = []
    with_images = 0

    for sku, prod in sorted(sku_products.items()):
        tags = generate_tags(prod)

        # Track tag stats
        broad_counts[tags[0]] = broad_counts.get(tags[0], 0) + 1
        for t in tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1

        # Parse prices
        retail = parse_price(prod.get("retail_price", ""))
        cost = parse_price(prod.get("price", ""))

        # Vendor = first brand
        brand_str = prod.get("brand", "")
        brands = [b.strip() for b in brand_str.split(",") if b.strip()]
        vendor = brands[0] if brands else "Vacuums and More"

        # Body HTML
        desc = prod.get("description", "")
        model = prod.get("model", "")
        body_parts = []
        if desc:
            body_parts.append(f"<p>{desc}</p>")
        if model:
            body_parts.append(f"<p><strong>Compatible models:</strong> {model}</p>")
        body_html = "\n".join(body_parts)

        # Image
        img_src = get_image_src(sku, image_urls)
        if img_src:
            with_images += 1

        row = {
            "Handle": slugify(f"{prod.get('clean_name', sku)} {sku}"),
            "Title": prod.get("clean_name", sku),
            "Body (HTML)": body_html,
            "Vendor": vendor,
            "Product Type": "Vacuum Parts",
            "Tags": ", ".join(tags),
            "Published": "TRUE",
            "Option1 Name": "Title",
            "Option1 Value": "Default Title",
            "Variant SKU": sku,
            "Variant Price": retail,
            "Variant Cost per item": cost,
            "Variant Compare At Price": "",
            "Variant Inventory Policy": "deny",
            "Variant Fulfillment Service": "manual",
            "Variant Requires Shipping": "TRUE",
            "Image Src": img_src,
            "Image Position": "1" if img_src else "",
            "Variant Weight Unit": "lb",
            "Status": "active",
        }
        rows.append(row)

    # 7. Write CSV
    print(f"\n7. Writing CSV to {OUTPUT_CSV}...")
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SHOPIFY_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    # 8. Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Products in CSV:     {len(rows)}")
    print(f"  With images:         {with_images} ({100*with_images/max(len(rows),1):.1f}%)")
    print(f"  Without images:      {len(rows) - with_images}")
    print()

    print("  Broad Tag Distribution:")
    for tag in ["Parts", "Bags", "Filters", "Attachments", "Vacuums"]:
        count = broad_counts.get(tag, 0)
        print(f"    {tag:<15} {count:>5}  ({100*count/max(len(rows),1):.1f}%)")
    print()

    print("  Top Specific Tags:")
    specific_tags = {t: c for t, c in tag_counts.items()
                     if t not in ("Parts", "Bags", "Filters", "Attachments", "Vacuums")}
    for tag, count in sorted(specific_tags.items(), key=lambda x: -x[1])[:15]:
        print(f"    {tag:<25} {count:>5}")
    print()

    print(f"  Output: {OUTPUT_CSV}")
    print("  DO NOT IMPORT — review first!")


if __name__ == "__main__":
    generate_csv()
