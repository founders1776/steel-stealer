# Steel Stealer — Session Instructions

Feed this file to a new Claude session to pick up where we left off.

## Project Goal

1. **Phase 1 (COMPLETE)**: Scrape all parts data from Steel City Vacuum's dealer portal schematics section and export to Excel.
2. **Phase 2 — Images (COMPLETE)**: Download product images from Steel City's API, remove backgrounds, add company branding, pad for Shopify.
3. **Phase 2b — Catalog Discovery (COMPLETE)**: Find non-schematic products (bags, cleaners, whole units, etc.) via search/category APIs.
4. **Phase 3 (NEXT)**: Upload images to Shopify and generate a Shopify product import CSV.

## Phase 1 — Parts Scraping (COMPLETE)

Single script `scraper.py` that:
1. Uses `undetected-chromedriver` (Selenium-based) to bypass Cloudflare Turnstile
2. Logs in with dealer credentials (account REDACTED_ACCT, ID REDACTED_USER, Password REDACTED_PASS)
3. Navigates the 3-level hierarchy: Brands (54) → Models (paginated) → Schematics
4. Extracts part IDs from `<area>` image map hotspots on schematic pages
5. Calls the internal API for each part to get full details (name, SKU, description, price, alt items)
6. Saves progress to `progress.json` after each model (resumable)
7. Exports to `output/steel_city_parts.xlsx`

**Results**: 53,365 rows, 12,576 unique part numbers, 54 brands.

## Phase 2 — Image Pipeline (COMPLETE)

### Overview

`steel_city_images.py` — pipeline that sources images directly from Steel City's product API:

1. **Discover** — Queries `product_info` API for each unique part number, extracts `big_picture` / `picture` field
2. **Download** — Direct HTTP download from `https://www.steelcityvac.com/uploads/applications/shopping_cart/{filename}` (no browser needed for downloads)
3. **Background removal** — `rembg` (U2-Net neural network) removes colored backgrounds (teal, gray, etc.)
4. **Composite + Pad** — Places product on white 2048x2048 canvas with VaM "Vacuums and More" logo centered behind product at 20% opacity

**NOTE**: Watermark removal step (OpenCV inpainting) was REMOVED — it was destroying product label text by misidentifying it as the Steel City watermark. The `rembg` background removal handles the watermark sufficiently since it's usually on the background.

### Results

| Metric | Count |
|--------|-------|
| Total part numbers (schematics) | 12,576 |
| Images found & processed | **5,361** (43%) |
| No image available | 7,215 (57% — mostly "special order" items) |
| Processing errors | **0** |

### Key Technical Details

- Steel City API: `POST web_services.php?action=product_info&name={PART_ID}` returns `picture` and `big_picture` fields
- Parts with `picture='0'` have no image (special order items)
- Image URLs: `https://www.steelcityvac.com/uploads/applications/shopping_cart/{filename}_large.jpg` (600x400px)
- Images are downloadable via direct HTTP — no Cloudflare protection on the uploads directory
- Browser session only needed for API discovery (Cloudflare protects the API endpoint)
- Product scaled UP to fill 1800x1800 within the 2048x2048 canvas
- Logo (`VaM Watermark.png`) scaled to 80% of canvas, centered behind product

### Previous Approach — Google Images (ABANDONED)

The Google Images scraper (`image_scraper.py`) completed ~5,500 SKUs and downloaded ~14,000 images. Quality was too low for e-commerce use. All images were deleted.

---

## Phase 2b — Catalog Discovery (COMPLETE)

### Overview

`catalog_scraper.py` — Finds Steel City products that are NOT listed in the schematics section (bags, cleaning products, whole vacuum units, accessories, etc.).

### Strategy

1. **Category Discovery** — Navigates the store section (`/a/s/`), uses `item_autocomplete` API with 18 search terms to find all product categories (161 found)
2. **Category Page Scraping** — Visits each category page at `/a/s/c/{categoryID}` to extract product links (with pagination)
3. **Search API Discovery** — Uses `search` API with 123 search terms (product types, brand names, single letters/digits) with `take=500` to find products. Response format: `{"results": [...], "total": N}` where each result has indexed fields (`"2"` = product_code, `"3"` = name, `"5"` = price)
4. **Enrichment** — Calls `product_info` API for each new product to get full details (name, SKU, description, price, image URL, alt items, stock status)
5. **Filtering** — Removes NLA items without alternatives, special order items
6. **Export** — Writes to `output/catalog_new_products.xlsx`

### Search API Response Format
```
POST web_services.php?action=search&take=500&searchstring={query}
Response: {"results": [{"0": internal_id, "1": productID, "2": product_code, "3": name, "5": price, "7": in_stock}, ...], "total": N, "cat_brands": [...], "cat_cats": [...]}
```

### Results

| Metric | Count |
|--------|-------|
| Existing schematic parts | 12,576 |
| New products discovered | 1,708 |
| NLA filtered (no alternative) | 407 |
| **New products exported** | **1,165** |
| With images | 801 |
| Without images | 364 |
| NLA kept (has alternative) | 56 |
| Categories discovered | 161 |

### Image Processing for Catalog Products

`catalog_images.py` — Downloads and processes images for the 801 catalog products with image URLs. Uses same pipeline as schematic images (rembg bg removal → logo composite → pad to 2048x2048).

**Results**: 800 images downloaded and processed, 0 errors.

---

## Phase 2c — Image Quality Flagging (COMPLETE)

`flag_images.py` — Scans all 6,107 processed images for quality issues. Generates contact sheets for visual review.

### Checks Performed

| Check | Threshold | Flagged |
|-------|-----------|---------|
| Faint product | Mean brightness > 200 | 211 |
| Small product | Product area < 2.5% of canvas | 646 |
| Cutoff at edges | Product within 50px of edge | 0 |
| **Total unique** | | **810 (13%)** |

### What Was Tested and Discarded

- **Watermark overlap on raw images**: rembg removes the "www.steelcityvac.com" watermark along with the background in virtually all cases, even when the product physically overlaps the watermark zone. Verified visually on highest-risk candidates (light-colored products with 100% watermark zone overlap). Not useful.
- **Color saturation check**: Flagged genuinely colorful products (blue covers, yellow filters, orange cords) as "background residue." 100% false positive rate. Not useful.

### Outputs

- `flag_results.json` — All flagged parts with scores and categories
- `review_sheets/review_faint_*.jpg` — 8 contact sheets (211 faint images)
- `review_sheets/review_small_*.jpg` — 22 contact sheets (646 small images)

---

## Phase 3 — Shopify Import (NEXT — DO NOT START WITHOUT USER APPROVAL)

### shopify_upload.py

Two modes:
- `python shopify_upload.py csv` — Generate Shopify CSV only (uses local image paths)
- `python shopify_upload.py upload` — Upload images to Shopify Files via API, then generate CSV with CDN URLs

**Shopify Credentials**:
- Store: `1bb2a2-2.myshopify.com` (Vacuums and More / evacuumsandmore.com)
- Access token: set via `SHOPIFY_ACCESS_TOKEN` env var
- Uses GraphQL staged uploads (stagedUploadsCreate → file upload → fileCreate)

### Shopify CSV Format

- Handle: slugified SKU
- Title: product name
- Body (HTML): description + compatible models
- Vendor: first brand
- Type: "Vacuum Parts"
- Tags: all compatible model numbers
- Variant SKU, Variant Price, Image Src, Image Position
- One main row per product + additional rows for extra images (same handle)

**Note**: `shopify_upload.py` expects images at `images/{SKU}/1.jpg` — this matches what `steel_city_images.py` outputs. However, the upload script may need updating since image folders are now keyed by part_number instead of SKU (they're the same for most parts, but differ for some).

## File Structure

```
Steel Stealer/
  scraper.py                # Phase 1 — Steel City parts scraper (COMPLETE)
  steel_city_images.py      # Phase 2 — Image pipeline: API → download → bg removal → logo → pad (COMPLETE)
  catalog_scraper.py        # Phase 2b — Catalog discovery: search/category APIs → enrich → filter → Excel (COMPLETE)
  catalog_images.py         # Phase 2b — Image download + processing for catalog products (COMPLETE)
  flag_images.py            # Phase 2c — Flag faint/small images for manual review (COMPLETE)
  image_scraper.py          # Phase 2a — Google Images search (ABANDONED)
  shopify_upload.py         # Phase 3 — Shopify upload + CSV generation (NEXT)
  VaM Watermark.png         # Company logo — "Vacuums and More" badge (used as background watermark)
  progress.json             # Phase 1 checkpoint (completed models + raw parts)
  steel_city_image_progress.json  # Phase 2 checkpoint (URLs, downloaded, processed status per part)
  catalog_progress.json     # Phase 2b checkpoint (discovered products, enriched products, filters)
  flag_results.json         # Phase 2c output — flagged parts with scores and categories
  review_sheets/            # Phase 2c output — contact sheet thumbnails for visual review
  image_progress.json       # Phase 2a checkpoint (Google Images — obsolete)
  instructions.md           # This file — session continuity
  Schematics.md             # Architecture doc — script wiring and data flow
  CLAUDE.md                 # Claude Code instructions (READ THIS FIRST)
  requirements.txt          # Python deps
  images/                   # 6,161 processed product images (5,361 schematic + 800 catalog)
  images_raw/               # 6,161 raw downloaded images from Steel City
  output/
    steel_city_parts.xlsx           # Schematics Excel (53,365 rows, 12,576 unique parts)
    steel_city_parts backup.xlsx    # Untouched backup
    catalog_new_products.xlsx       # Non-schematic products (1,165 new products)
    shopify_import.csv              # Generated Shopify CSV
  image_urls.json           # Shopify CDN URLs per SKU (after upload)
  browser_data/             # Chrome persistent profile (keeps session cookies)
  debug/                    # Screenshots and HTML dumps
  .venv/                    # Python virtual environment
```

## CLI Commands

```bash
# Phase 2 — Image pipeline (already complete, but can re-run steps)
python3 steel_city_images.py                    # Full pipeline
python3 steel_city_images.py --step discover    # Only query API for image URLs
python3 steel_city_images.py --step download    # Only download raw images
python3 steel_city_images.py --step process     # Only process images (no browser needed)
python3 steel_city_images.py --test 10          # Test with first 10 parts

# Phase 2b — Catalog discovery
python3 catalog_scraper.py                      # Full pipeline (discover + enrich + export)
python3 catalog_scraper.py --step discover      # Only discover categories + search
python3 catalog_scraper.py --step enrich        # Only enrich discovered products via API
python3 catalog_scraper.py --step export        # Only export to Excel
python3 catalog_images.py                       # Download + process images for catalog products

# Phase 3 — Shopify (DO NOT RUN WITHOUT USER APPROVAL)
python3 shopify_upload.py csv                   # Generate CSV only
python3 shopify_upload.py upload                # Upload to Shopify + generate CSV
```

## Dependencies

```
openpyxl==3.1.5
Pillow==12.1.1
requests==2.32.5
rembg[cpu]>=2.0.50
numpy>=1.24.0
```

Also: `undetected-chromedriver`, `selenium` (for Cloudflare bypass)

## Key Technical Discoveries

### Site Structure
- CognitiveSOUL v4.0 PHP framework behind Cloudflare
- Schematics URL: `https://www.steelcityvac.com/a/g/?t=1&gid=1&folder=`
- Store/Catalog URL: `https://www.steelcityvac.com/a/s/`
- Category page: `https://www.steelcityvac.com/a/s/c/{categoryID}`
- Search: `#cognitiveSOULQuickSearchTermField` (name=searchstring)
- Product page: `https://www.steelcityvac.com/a/s/pid/{productID}`
- Image CDN: `https://www.steelcityvac.com/uploads/applications/shopping_cart/{filename}`

### Parts Data API
- `POST web_services.php?action=product_info&name=PART_ID`
- Returns JSON: {name, product_code, description, Price_1, in_stock, picture, big_picture, thumbnail, alt_items, productID, ...}
- `picture='0'` means no image (special order items)

### Search API
- `POST web_services.php?action=search&take=500&searchstring={query}`
- Returns: `{"results": [{indexed fields}], "total": N, "cat_brands": [...], "cat_cats": [...]}`
- Result fields: `"0"` = internal_id, `"1"` = productID, `"2"` = product_code, `"3"` = name/description, `"5"` = price, `"7"` = in_stock

### Autocomplete API
- `POST web_services.php?action=item_autocomplete` with data `{name: query, maxRows: N}`
- Returns array of `{name, type, categoryID, productID, manufacturer, products_count, exact_match}`
- Types: "product", "category", "schematic", "wizard"

### Anti-Bot Bypass
- Cloudflare WAF + Turnstile → undetected-chromedriver v145
- Playwright (regular + stealth) FAILED — detected by Turnstile
- Image downloads do NOT need Cloudflare bypass (direct HTTP works)

## Known Issues

1. **Chrome version hardcoded** — `version_main=145` in `scraper.py`, `steel_city_images.py`, and `catalog_scraper.py`. Update if Chrome updates.
2. **Part number vs SKU mismatch** — Most part numbers equal the SKU, but some differ. Image folders use part_number. `shopify_upload.py` may need updating to handle this.
3. **57% of schematic parts have no image** — These are special order items. Steel City's API returns `picture='0'` for them.
4. **Watermark removal removed** — The OpenCV inpainting step was destroying product label text. Removed entirely; rembg handles the watermark via background removal.
5. **Search API take limit** — The search API returns max ~500 results per query. Some terms may have more products than this. If more products are needed, try more specific sub-queries.
