# Schematics.md — Script Architecture

## Overview

```
Phase 1: scraper.py (single script, ~560 lines)
  |
  +-- create_driver()        → Launches undetected Chrome (v145)
  +-- login()                → Fills login form, handles Cloudflare
  +-- get_brands()           → Scrapes brand gallery (54 brands, 3 pages)
  |     +-- get_all_gallery_items()  → Generic paginated gallery scraper
  +-- get_models()           → Scrapes model gallery per brand (paginated)
  |     +-- get_all_gallery_items()
  +-- process_model()        → Routes folder vs direct schematic models
  |     +-- extract_schematic_parts()   → Gets <area> tags + calls API
  |           +-- get_part_info_via_api()  → JS $.ajax to product_info endpoint
  +-- export_to_excel()      → Writes .xlsx with openpyxl

Phase 2a: image_scraper.py (ABANDONED — Google Images quality too low)

Phase 2b: steel_city_images.py (~500 lines)
  |
  +-- build_sku_map()           → Reads spreadsheet, deduplicates by SKU
  +-- create_driver() / login() → Launches Chrome, authenticates (same as scraper.py)
  +-- Step 1: URL Discovery
  |     +-- get_image_url_from_api()  → Calls product_info API, extracts `picture` field
  |     +-- discover_urls()           → Iterates all SKUs, stores URLs in progress
  +-- Step 2: Download
  |     +-- download_image_requests() → Direct HTTP download (fast)
  |     +-- download_images()         → Saves to images_raw/
  +-- Step 3-5: Process
  |     +-- process_single_image()    → rembg bg removal → composite (watermark removal REMOVED)
  |     +-- load_logo()               → Loads VaM Watermark.png
  |     +-- create_background_with_logo() → White canvas + centered logo at 20% opacity
  |     +-- process_images()          → Batch processing with checkpointing
  +-- main()                    → CLI: --step [discover|download|process|all], --test N

Phase 2b-catalog: catalog_scraper.py (~550 lines)
  |
  +-- load_existing_part_numbers()  → Reads steel_city_parts.xlsx, returns set of 12,576 known parts
  +-- create_driver() / login()     → Same Chrome + Cloudflare bypass
  +-- Discovery Phase:
  |     +-- discover_categories()        → Navigates /a/s/, uses item_autocomplete API (161 categories)
  |     +-- scrape_category_pages()      → Visits /a/s/c/{id} pages, extracts product links
  |     +-- discover_products_via_search() → search API with 123 terms (take=500), autocomplete API
  +-- Enrichment Phase:
  |     +-- enrich_products()            → Calls product_info API for each new product
  |           Filters: NLA without alt → skip, special order → skip
  +-- Export Phase:
  |     +-- export_to_excel()            → Writes output/catalog_new_products.xlsx
  +-- main()                             → CLI: --step [discover|enrich|export|all]

Phase 2b-catalog-images: catalog_images.py (~120 lines)
  |
  +-- download_image()           → Direct HTTP download
  +-- process_single_image()     → rembg bg removal → logo composite → pad to 2048x2048
  +-- main()                     → Downloads + processes images for catalog products

Phase 2c-quality: flag_images.py (~230 lines)
  |
  +-- analyze_processed_image()  → Checks faint, small, cutoff on processed 2048x2048 images
  +-- scan_all()                 → Iterates all images/{part}/1.jpg, collects flags
  +-- make_contact_sheets()      → Generates thumbnail grids (6x5 = 30/sheet) per category
  +-- generate_all_sheets()      → Creates review_sheets/review_{category}_{nn}.jpg
  +-- main()                     → Scan → JSON → contact sheets

Phase 2d-regen: regenerate_descriptions.py (~830 lines)
  |
  +-- KNOWN_BRANDS             → 70+ brand names (multi-word first for longest match)
  +-- extract_brand_from_name()→ Matches brand in clean_name/raw_name for empty brand fields
  +-- CATEGORY_PATTERNS        → 55+ regex patterns (motor sub-cats, expanded machine exclusions)
  +-- detect_category()        → Routes product to most specific category
  +-- generate_description()   → Template: "Replacement {cat_name} {compat}. {benefit}. {details}."
  +-- clean_product_name()     → Abbreviation expansion, title case, restructuring
  +-- is_sku_like()            → Detects SKU-only names for fixing
  +-- main()                   → Fix SKU names → extract brands → regenerate descriptions → save + export

Phase 2g: generate_pricing.py (~160 lines)
  |
  +-- MARKUP_TIERS           → 9-tier config dict (cost_max, multiplier)
  +-- get_markup()           → Returns multiplier for a given dealer cost
  +-- charm_price()          → Rounds to nearest .99 (psychological pricing)
  +-- calculate_retail_price() → Applies markup + charm + $6.99 floor
  +-- parse_price()          → Extracts numeric price from "$11.92" format
  +-- update_spreadsheet()   → Adds Retail Price column to product_descriptions.xlsx
  +-- print_summary()        → Stats: margin %, distribution, sample prices by tier
  +-- main()                 → Loads product_names.json, prices all products, saves

Phase 3: shopify_upload.py (~300 lines)
  |
  +-- build_sku_map()        → Same dedup logic as image_scraper.py
  +-- shopify_graphql()      → Executes Shopify Admin GraphQL queries
  +-- upload_image_to_shopify()  → Staged upload flow (3-step GraphQL)
  +-- upload_all_images()    → Batch upload with checkpointing
  +-- slugify()              → Converts SKU to URL-friendly handle
  +-- parse_price()          → Extracts numeric price from "$11.92" format
  +-- generate_csv()         → Builds Shopify CSV; retail_price → Variant Price, cost → Variant Cost per item
  +-- main()                 → CLI: "csv" (default) or "upload" mode

Phase 3b: generate_shopify_csv.py (~250 lines)
  |
  +-- generate_tags()          → Auto-tags: broad (1 of 5) + specific + brand + model
  +-- BROAD_TAG_RULES          → Bags, Filters, Attachments, Vacuums, Parts (default)
  +-- SPECIFIC_TAG_RULES       → Belts, Hoses, Wands, Floor Nozzles, vacuum types
  +-- dedup_by_sku()           → Merges 678 duplicate SKU entries (combines brands, longest name)
  +-- load_existing_skus()     → Reads shopify_current_products.csv → 1,388 SKUs to exclude
  +-- slugify() / parse_price()→ Handle + price helpers
  +-- get_image_src()          → CDN URL or local image path
  +-- generate_csv()           → Full pipeline: load → dedup → exclude → filter → tag → write CSV
```

## Data Flow

```
Phase 1: Steel City Schematics Scraping
──────────────────────────────────────────
Steel City Website
       |
  [Login via form submit to admin.php]
       |
  [Cloudflare Turnstile - bypassed by undetected-chromedriver]
       |
  /a/g/?t=1&gid=1&folder=   ← Schematics main page
       |
  .gallery-product-wrapper   ← 54 brand cards (20/page, 3 pages)
       |
  /a/g/?t=1&gid=1&folder=/BrandName  ← Brand page
       |
  .gallery-product-wrapper   ← Model cards (20/page, varies)
       |                        Two types: "folder" or "schematic"
       |
  /a/g/?t=1&gid=1&folder=/Brand/Model  ← Schematic viewer
       |
  <area title="PART_ID">    ← Image map hotspots on schematic diagram
       |
  POST web_services.php?action=product_info&name=PART_ID  ← API call
       |
  JSON: {name, product_code, description, Price_1, ...}
       |
  progress.json              ← Checkpoint after each model
       |
  output/steel_city_parts.xlsx  ← Final Excel export (53,365 rows, 12,576 unique parts)

Phase 2: Steel City Images (Schematic Products)
─────────────────────────────────────────────────
output/steel_city_parts.xlsx
       |
  [build_sku_map() — dedup by SKU]
       |
  12,576 unique part numbers
       |
  [Step 1: Query product_info API for `picture` field]
       |
  [Step 2: Download raw images to images_raw/{part}.jpg]
       |
  [Step 3: rembg background removal (U2-Net)]
       |
  [Step 4: Composite with VaM logo behind product]
       |
  [Step 5: Pad to 2048x2048, save as JPEG]
       |
  images/{part}/1.jpg  ← 5,361 images (43% have images)
       |
  steel_city_image_progress.json  ← Checkpoint (resumable)

Phase 2b: Catalog Discovery (Non-Schematic Products)
──────────────────────────────────────────────────────
  [Load 12,576 existing part numbers from steel_city_parts.xlsx]
       |
  [Login + navigate to /a/s/ (store section)]
       |
  [item_autocomplete API with 18 category terms → 161 categories]
       |
  [Visit /a/s/c/{id} for each category → extract product links]
       |
  [search API with 123 terms (take=500) → product_code directly from results]
       |
  [1,708 new products discovered]
       |
  [product_info API for each → full details + filtering]
       |
  [Filter: NLA without alt (407 removed), special order (0 removed)]
       |
  [1,165 new products enriched]
       |
  catalog_progress.json  ← Checkpoint
       |
  output/catalog_new_products.xlsx  ← New products Excel

  [catalog_images.py → download 800 images + process same as schematic images]
       |
  images/{part}/1.jpg  ← 800 additional processed images

Phase 2c: Image Quality Flagging
─────────────────────────────────
  images/{part}/1.jpg  ← 6,107 processed images
       |
  [Faint check: mean brightness of product pixels > 200]
  [Small check: product area < 2.5% of canvas]
  [Cutoff check: product within 50px of canvas edge]
       |
  flag_results.json  ← All flagged parts with scores + categories
       |
  review_sheets/review_faint_*.jpg   ← 8 contact sheets (211 faint images)
  review_sheets/review_small_*.jpg   ← 22 contact sheets (646 small images)
       |
  [Manual visual review via contact sheets]
       |
  810 total flagged (13% of images) for manual review

Phase 2d: Product Description Generation + Data Quality
─────────────────────────────────────────────────────────
  product_names.json  ← 8,081 products with clean_name, brand, model, sku, price
       |
  [regenerate_descriptions.py — advanced description regenerator]
       |
  [extract_brand_from_name() — fills empty brand field from KNOWN_BRANDS list (~2,452 extracted)]
       |
  [detect_category() — 55+ categories from clean_name keywords]
       |   Motor sub-categories: motor_wire, motor_cover, motor_gasket, motor_bearing, motor_bracket, motor_fan
       |   New categories: cleaning_solution, tank, tube_duct, wire_cable, connector, pump,
       |                   light_bulb, knob_dial, clip, roller, label_decal, sensor, door_lid
       |   Machine category: expanded exclusion list (~70 part keywords) to prevent false positives
       |
  [extract attributes: brand, model, quantity, length, color]
       |
  [Select template for category → fill with attributes]
       |
  product_names.json  ← Updated with `description` + `brand` fields per product
       |
  Stats: generic category ~11.8% (down from ~20.4%), 2,452 brands extracted

Phase 2e: Stock Check & Filtering
───────────────────────────────────
  product_names.json  ← 5,926 products
       |
  [check_stock.py — batch API calls (8 concurrent) via browser JS]
       |
  [product_info API → in_stock field: "1" = in stock, "0" = special order]
       |
  [Classify: ok / special_order / nla_no_alt / nla_has_alt / no_data]
       |
  stock_check_progress.json  ← Checkpoint (resumable)
       |
  Results: 4,620 in_stock=1 | 1,301 in_stock=0 | 5 no_data
    Of the 1,301 out-of-stock:
      184 have alt items → KEPT
      1,116 no alt items → REMOVED (special order, unavailable)
    Also removed: 2 NLA without alts
       |
  output/removed_products.json  ← 1,118 excluded products for reference
       |
  product_names.json  ← 4,808 products remaining (with in_stock + description fields)
       |
  output/product_descriptions.xlsx  ← Final spreadsheet with In Stock column

Phase 2f: Full Product Discovery & Pipeline
─────────────────────────────────────────────
  [full_discovery.py — 7-step pipeline]
       |
  Step 1: Search API enumeration (a-z, 0-9 + progressive depth 2→3→4 chars)
       |
  55,998 total products discovered across Steel City catalog
       |
  Step 2: Batch product_info API enrichment (BATCH_SIZE=10, concurrent JS)
       |
  55,998 products enriched (36 errors/no data)
       |
  Step 3: Filter + Alt Tracing
       |
  Filter: in_stock with picture → KEEP
          NLA/OOS with alt → trace alt, pick up if in-stock with picture
          Already in product_names.json → SKIP (dedup)
       |
  Run 1: 2,587 new products (from initial 55,998 enrichment)
  Run 2: 686 new products (after dedup against 8,154 existing)
  Total alt items traced: 2,108
       |
  Step 4: Download raw images via HTTP → images_raw/
  Step 5: Process images (rembg bg removal → VaM watermark 20% → 2048x2048 pad)
       |
  3,273 total images downloaded + processed (0 failures)
       |
  Step 6: Clean names (abbreviation expansion, smart title case, 120 char cap)
         + Template-based SEO descriptions (25+ categories)
       |
  Step 7: Merge into product_names.json + export spreadsheet
       |
  product_names.json  ← 8,081 total products (no duplicates)
       |
  output/product_descriptions.xlsx  ← Final spreadsheet
       |
  full_discovery_progress.json  ← Checkpoint (fully resumable per step)

Phase 2g: Competitive Retail Pricing
──────────────────────────────────────
  product_names.json  ← 8,081 products with dealer cost in `price` field
       |
  [generate_pricing.py — tiered markup engine]
       |
  [9 cost tiers (adjusted after competitor analysis):
    $0-1 (8.0×), $1-3 (4.5×), $3-7 (3.2×), $7-15 (2.5×),
    $15-30 (2.2×), $30-60 (1.9×), $60-120 (1.7×),
    $120-300 (1.5×), $300+ (1.4×)]
  [Charm pricing: round to nearest .99]
  [Floor: $6.99 minimum retail price]
       |
  [Validated: 20 random products compared against competitors online]
  [Initial tiers were 30-60% above market → lowered all multipliers]
       |
  product_names.json  ← Updated with `retail_price` field (7,974 priced, 107 skipped)
       |
  output/product_descriptions.xlsx  ← Added Retail Price column
       |
  Stats: avg retail $45.64, median $19.99, avg margin 64.5%

Phase 3: Shopify Import
──────────────────────────────
  [Both Excel files → combine product data]
       |
  [shopify_upload.py uses retail_price for Variant Price, dealer cost for Variant Cost per item]
       |
  [Optional: Upload to Shopify Files via GraphQL API]
       |
  image_urls.json  ← SKU → [cdn_url]
       |
  output/shopify_import.csv  ← Shopify product import CSV

Phase 3c: Automated Stock & Price Sync
────────────────────────────────────────
  [build_shopify_map.py — one-time setup]
       |
  [Paginates ALL Shopify products → extracts SKU + product_id + variant_id]
       |
  shopify_product_map.json  ← SKU → {product_id, variant_id}
       |
  [sync_stock_prices.py — runs every 12 hours via GitHub Actions]
       |
  [Launch browser → login to Steel City → batch API calls (8 concurrent)]
       |
  [Compare current vs stored: stock status + dealer cost]
       |
  [in_stock 1→0 → PUT product status="draft" (hide from store)]
  [in_stock 0→1 → PUT product status="active" (show in store)]
  [cost increased → recalculate retail via markup tiers → PUT variant price + cost]
  [cost decreased → NO ACTION (protect margins)]
       |
  [Update product_names.json + append to sync_log.json]
       |
  [GitHub Actions: commit changes + email summary]
       |
  sync_log.json  ← Append-only change log
       |
  .github/workflows/sync-stock-prices.yml  ← Cron: 6am + 6pm UTC
```

## Key Files

| File | Purpose |
|------|---------|
| `scraper.py` | Phase 1 — login, navigation, extraction, export |
| `progress.json` | Phase 1 checkpoint — completed models + all extracted parts |
| `output/steel_city_parts.xlsx` | Schematics Excel output (53,365 rows, 12,576 unique parts) |
| `image_scraper.py` | Phase 2a — ABANDONED (Google Images, low quality) |
| `steel_city_images.py` | Phase 2b — Steel City API images: download, bg removal, logo, pad |
| `steel_city_image_progress.json` | Phase 2b checkpoint — URL discovery, download, processing status |
| `catalog_scraper.py` | Phase 2b-catalog — search/category discovery, enrichment, filtering, export |
| `catalog_images.py` | Phase 2b-catalog — image download + processing for catalog products |
| `catalog_progress.json` | Phase 2b-catalog checkpoint — discovered/enriched products |
| `output/catalog_new_products.xlsx` | Non-schematic products (1,165 new, filtered) |
| `images_raw/` | Raw downloaded images from Steel City (before processing) |
| `images/` | Processed product images (bg removed, logo, padded) — subfolder per part |
| `VaM Watermark.png` | Company logo (Vacuums and More) used as background watermark |
| `flag_images.py` | Phase 2c — flag faint/small/cutoff processed images for manual review |
| `flag_results.json` | Phase 2c output — flagged parts with scores and categories |
| `review_sheets/` | Phase 2c output — contact sheet thumbnails for visual review |
| `generate_descriptions.py` | Phase 2d — original template-based product description generator (25+ categories) |
| `regenerate_descriptions.py` | Phase 2d — advanced description regenerator: brand extraction, 55+ categories, motor sub-cats, expanded machine exclusions |
| `check_stock.py` | Phase 2e — batch stock check via API, filters special order/NLA products |
| `stock_check_progress.json` | Phase 2e checkpoint — stock status per product (resumable) |
| `output/removed_products.json` | Phase 2e — 1,118 excluded products (special order + NLA no alt) |
| `full_discovery.py` | Phase 2f — 7-step pipeline: discover, enrich, filter, images, names, merge, report |
| `full_discovery_progress.json` | Phase 2f checkpoint — all discovery/enrichment/filter/image/name state |
| `output/product_descriptions.xlsx` | Final spreadsheet: 8,081 products with all fields |
| `product_names.json` | 8,081 products: clean_name, description, brand, model, sku, price, retail_price, in_stock |
| `generate_pricing.py` | Phase 2g — tiered markup engine with charm pricing + $6.99 floor |
| `shopify_upload.py` | Phase 3 — upload images to Shopify Files, generate CSV (uses retail_price) |
| `generate_shopify_csv.py` | Phase 3b — auto-tagged Shopify CSV from product_names.json (dedup + exclude existing) |
| `build_shopify_map.py` | Phase 3c — builds SKU → {product_id, variant_id} mapping from Shopify (one-time) |
| `sync_stock_prices.py` | Phase 3c — automated stock/price sync: Steel City → Shopify (12h cron) |
| `shopify_product_map.json` | Phase 3c — SKU → {product_id, variant_id} mapping |
| `sync_log.json` | Phase 3c — append-only log of all sync runs |
| `.github/workflows/sync-stock-prices.yml` | Phase 3c — GitHub Actions cron (6am + 6pm UTC) |
| `image_urls.json` | Shopify CDN URLs per SKU (after upload) |
| `output/shopify_import.csv` | Shopify product import CSV |
| `browser_data/` | Chrome persistent profile (keeps session cookies) |
| `debug/` | Screenshots + HTML dumps for troubleshooting |

## Dependencies

**Phase 1:**
- `undetected-chromedriver` — Bypasses Cloudflare bot detection
- `selenium` — Browser automation
- `openpyxl` — Excel file creation
- Google Chrome v145 — System browser

**Phase 2 & 2b:**
- `rembg[cpu]` — Background removal using U2-Net neural network
- `numpy` — Image array processing
- `Pillow` — Image compositing, resize, pad, save JPEG
- `requests` — HTTP image downloads + Shopify API calls
- `openpyxl` — Read/write spreadsheets
- `undetected-chromedriver` / `selenium` — Steel City API access (Cloudflare bypass)

## API Endpoints

### Steel City — Product Info (Phase 1 & 2)
```
POST https://www.steelcityvac.com/applications/shopping_cart/web_services.php
  ?action=product_info&name={PART_ID}

Response JSON fields:
  name           — Product name (e.g., "LID,CLARKE COMFORT VAC 10")
  product_code   — SKU (e.g., "1471080510")
  description    — Extended description
  productID      — Internal ID
  Price_1        — Unit price
  Price_5/10/25/50 — Volume pricing tiers
  in_stock       — Stock status
  picture        — Image filename (or "0" for no image)
  big_picture    — Large image filename
  alt_items      — Array of {name, product_code} alternatives
  manufacturer   — Brand/manufacturer name
```

### Steel City — Search (Phase 2b)
```
POST https://www.steelcityvac.com/applications/shopping_cart/web_services.php
  ?action=search&take=500&searchstring={query}

Response JSON:
  results    — Array of product dicts with indexed keys:
               "0": internal_id, "1": productID, "2": product_code/SKU,
               "3": name/description, "5": price, "7": in_stock
  total      — Total matching results (may exceed `take`)
  cat_brands — Array of brand names matching query
  cat_cats   — Array of category names matching query
```

### Steel City — Autocomplete (Phase 2b)
```
POST https://www.steelcityvac.com/applications/shopping_cart/web_services.php
  ?action=item_autocomplete
  Data: {name: query, maxRows: N}

Response: Array of {name, type, categoryID, productID, manufacturer, products_count, exact_match}
  type: "product" | "category" | "schematic" | "wizard"
```

### Shopify Admin GraphQL (Phase 3)
```
POST https://{store}.myshopify.com/admin/api/2024-01/graphql.json
  Header: X-Shopify-Access-Token: shpat_xxxxx

Mutations used:
  stagedUploadsCreate  — Get presigned upload URL
  fileCreate           — Register uploaded file in Shopify Files

Environment variables:
  SHOPIFY_STORE          — e.g., "your-store.myshopify.com"
  SHOPIFY_ACCESS_TOKEN   — e.g., "shpat_xxxxx"
```

## CLI Usage

```bash
# Phase 1 — Schematics scraping
python3 scraper.py                             # Full scrape (resumable)

# Phase 2 — Image pipeline (schematic products)
python3 steel_city_images.py                    # Full pipeline
python3 steel_city_images.py --step discover    # Only query API for image URLs
python3 steel_city_images.py --step download    # Only download raw images
python3 steel_city_images.py --step process     # Only process (no browser needed)
python3 steel_city_images.py --test 10          # Test with first 10 SKUs

# Phase 2b — Catalog discovery (non-schematic products)
python3 catalog_scraper.py                      # Full pipeline
python3 catalog_scraper.py --step discover      # Only discover categories + search
python3 catalog_scraper.py --step enrich        # Only enrich via product_info API
python3 catalog_scraper.py --step export        # Only export to Excel
python3 catalog_images.py                       # Download + process catalog images

# Phase 2c — Image quality flagging
python3 flag_images.py                          # Scan all images → flag_results.json + contact sheets

# Phase 2d — Product descriptions
python3 generate_descriptions.py              # Generate descriptions → updates product_names.json
python3 regenerate_descriptions.py            # Full regeneration: brand extraction + 55+ categories + save
python3 regenerate_descriptions.py --dry      # Preview changes without saving

# Phase 2e — Stock check & filtering
python3 check_stock.py                        # Check all products (resumable, ~10 min)
python3 check_stock.py --report               # Report + filter without re-checking (no browser)

# Phase 2f — Full product discovery & pipeline
python3 full_discovery.py                     # Full 7-step pipeline (resumable)
python3 full_discovery.py --step discover     # Search API enumeration (needs browser)
python3 full_discovery.py --step enrich       # Batch product_info API (needs browser)
python3 full_discovery.py --step filter       # Filter + alt tracing (no browser)
python3 full_discovery.py --step images       # Download + process images (no browser)
python3 full_discovery.py --step names        # Clean names + descriptions (no browser)
python3 full_discovery.py --step merge        # Merge + export spreadsheet (no browser)
python3 full_discovery.py --step report       # Print stats only

# Phase 2g — Retail pricing
python3 generate_pricing.py              # Apply tiered markup → updates product_names.json + spreadsheet

# Phase 3 — Shopify (DO NOT RUN WITHOUT USER APPROVAL)
python3 generate_shopify_csv.py            # Generate auto-tagged Shopify CSV (7,009 products)
python3 shopify_upload.py csv               # Generate CSV only (default, from spreadsheet)
python3 shopify_upload.py upload            # Upload to Shopify + generate CSV

# Phase 3c — Stock & Price Sync
python3 build_shopify_map.py               # One-time: build SKU → Shopify ID mapping
python3 sync_stock_prices.py               # Full sync (resumable)
python3 sync_stock_prices.py --dry-run     # Preview changes without touching Shopify
```
