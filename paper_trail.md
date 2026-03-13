# Paper Trail — Deleted Artifacts

This file documents all files and directories that were deleted from this project after the pipeline was fully built and operational. These were one-time-use scripts, intermediate data files, and cached browser data from the build phases. They are no longer needed for the ongoing automated sync.

**Deleted on:** 2026-03-13
**Total space reclaimed:** ~2.2 GB

---

## Phase 1: Schematics Scraping

| File | Purpose | Output It Produced |
|---|---|---|
| `scraper.py` (24KB) | Selenium scraper that logged into Steel City, navigated brand/model schematics pages, and extracted part numbers + names for all 54 brands. Used `undetected-chromedriver` for Cloudflare bypass. | `output/steel_city_parts.xlsx` (12,576 unique parts) |
| `progress.json` | Checkpoint file for `scraper.py` — tracked which brands/models had been scraped so crashes could resume. | *(was already in .gitignore)* |

---

## Phase 2a: Google Images (ABANDONED)

| File | Purpose | Why Abandoned |
|---|---|---|
| `image_scraper.py` (16KB) | Downloaded product images from Google Images search results using Selenium. | Low quality results — blurry, wrong products, watermarked stock photos. Replaced by Phase 2b (scraping directly from Steel City). |

---

## Phase 2b: Steel City Product Images

| File | Purpose | Output It Produced |
|---|---|---|
| `steel_city_images.py` (24KB) | Logged into Steel City, downloaded product images from their CDN, applied watermark overlay (`VaM Watermark.png`), and saved processed images to `images/{sku}/1.jpg`. Ran in batches of 8 concurrent downloads. | `images/` directory (5,361 processed product images, ~1.1GB — deleted separately after Shopify upload confirmed) |
| `steel_city_image_progress.json` (1MB) | Checkpoint tracking which SKUs had been downloaded/processed. | *(intermediate, no longer needed)* |
| `image_progress.json` (104KB) | Earlier image download progress from Phase 2a that was reused as a starting point. | *(intermediate)* |

---

## Phase 2b-catalog: Catalog Discovery

| File | Purpose | Output It Produced |
|---|---|---|
| `catalog_scraper.py` (36KB) | Scraped Steel City's online catalog (browsing by category rather than by schematics) to discover products not found via schematics. Found 1,165 additional products. | Additional entries merged into `product_names.json` |
| `catalog_images.py` (8KB) | Downloaded images for the newly discovered catalog products. | Additional images merged into `images/` directory |
| `catalog_progress.json` (944KB) | Checkpoint for catalog scraping progress. | *(intermediate)* |

---

## Phase 2c: Image Quality Flagging & Replacement

| File | Purpose | Output It Produced |
|---|---|---|
| `flag_images.py` (12KB) | Analyzed all product images for quality issues (too small, too blurry, wrong aspect ratio, mostly white/blank). Used PIL/Pillow for image analysis. | `flag_results.json` (810 images flagged for review) |
| `flag_results.json` (136KB) | List of flagged images with quality scores and reasons. | *(input to manual review)* |
| `replace_flagged_images.py` (16KB) | Re-downloaded flagged images from Steel City with alternative URLs/sizes, attempting to get better quality versions. | Replaced ~400 images in `images/` |
| `replace_progress.json` (20KB) | Checkpoint for image replacement progress. | *(intermediate)* |
| `make_all_sheets.py` (4KB) | Generated contact sheet PDFs (grids of thumbnails) for visual review of product images, organized by brand. | `review_sheets/` directory (210 PDFs) |
| `visual_review_results.json` (12KB) | Results of manual visual review — which flagged images were acceptable vs. needed removal. | *(manual review complete)* |
| `test_inpaint.py` (8KB) | Experimental script testing OpenCV inpainting to remove watermarks from competitor images. Never used in production. | *(experiment, abandoned)* |

### Deleted Directories

| Directory | Size | Purpose |
|---|---|---|
| `thumbnails_review/` | 11MB | ~740 thumbnail images generated for quick visual scanning during image quality review. |
| `review_sheets/` | 53MB | ~210 contact sheet PDFs (one per brand) showing all product images in a grid layout for manual review. |

---

## Phase 2d: Product Name Cleaning & Descriptions

| File | Purpose | Output It Produced |
|---|---|---|
| `clean_names.py` (16KB) | Cleaned raw product names from Steel City (removed junk characters, standardized formatting, extracted brand/model info). Applied regex patterns and manual overrides. | `clean_name` field in `product_names.json` |
| `generate_descriptions.py` (20KB) | Generated product descriptions using template-based approach with 25+ product categories (belts, bags, filters, hoses, etc.). Matched products to categories via keyword analysis. | `description` field in `product_names.json` |
| `regenerate_descriptions.py` (40KB) | Enhanced version of description generator — re-ran on all products with improved templates and category matching after initial review showed some weak descriptions. | Updated `description` field in `product_names.json` |
| `merge_descriptions.py` (4KB) | Merged description output from temp files (`temp_output_*.json`) back into `product_names.json`. The description generator wrote batches to temp files for crash safety. | Merged data into `product_names.json` |
| `temp_output_68.json` through `temp_output_80.json` (10 files, ~300KB total) | Temporary batch output from `regenerate_descriptions.py`. Each file contained descriptions for a batch of ~800 products. | *(merged into product_names.json by merge_descriptions.py)* |

---

## Phase 2e: Stock Check

| File | Purpose | Output It Produced |
|---|---|---|
| `check_stock.py` (16KB) | Logged into Steel City and batch-queried the `product_info` API for all 5,926 products to determine in_stock status (1=in stock, 0=special order). Used same browser setup + batch API pattern later reused by `sync_stock_prices.py`. | `in_stock` field in `product_names.json`, `stock_check_progress.json` |
| `stock_check_progress.json` (828KB) | Full API response data from stock check — every product's stock status, price, description, alt items. | *(data merged into product_names.json)* |

---

## Phase 2f: Full Product Discovery

| File | Purpose | Output It Produced |
|---|---|---|
| `full_discovery_progress.json` (34MB) | Checkpoint from `full_discovery.py` — tracked enumeration of all 55,998 Steel City product IDs, which ones were new, which had images downloaded. This was the largest single file. `full_discovery.py` itself is KEPT because it can be re-run to find new products. | *(intermediate — full_discovery.py can regenerate if needed)* |

---

## Phase 2g: Pricing

| File | Purpose | Output It Produced |
|---|---|---|
| `generate_pricing.py` (8KB) | Tiered markup engine that calculated retail prices from dealer costs. Contains `MARKUP_TIERS`, `get_markup()`, `charm_price()`, and `calculate_retail_price()` functions. These same pricing functions were copied into `sync_stock_prices.py` for ongoing use. | `retail_price` field in `product_names.json` |

**Note:** The pricing logic now lives in `sync_stock_prices.py` (lines ~30-70). If pricing tiers need to change, edit them there.

---

## Phase 3: Shopify Import & Upload

| File | Purpose | Output It Produced |
|---|---|---|
| `shopify_oauth.py` (4KB) | One-time OAuth flow to obtain Shopify access token. Set up localhost:9999 callback, opened browser for authorization, exchanged code for token. | `shopify_token.json` (token now in `.env` instead) |
| `shopify_token.json` (4KB) | OAuth token response from Shopify. Superseded by `SHOPIFY_ACCESS_TOKEN` in `.env`. | *(credentials moved to .env)* |
| `shopify_upload.py` (20KB) | Earlier version of Shopify product upload script. Created products with images via the Shopify REST API. Superseded by `shopify_bulk_import.py` which was faster and more reliable. | Products in Shopify store |
| `upload_images.py` (8KB) | Uploaded product images to Shopify for products that were imported via CSV (which doesn't support images). Used the Shopify product images API endpoint. | Images attached to Shopify products (now on Shopify CDN) |
| `upload_missing_images.py` (4KB) | Backfill script that found products missing images in Shopify and re-uploaded them. Ran after `upload_images.py` to catch any that failed. | Additional images on Shopify CDN |
| `image_urls.json` (2MB) | Mapping of SKU → Shopify CDN image URL, built during image upload to track which products had images. | *(reference only, images are on Shopify CDN)* |

---

## Deleted Directories (Infrastructure/Cache)

| Directory | Size | Purpose |
|---|---|---|
| `images_raw/` | 422MB | Raw image files downloaded directly from Steel City before processing (resizing, watermark overlay). 9,310 files. The processed versions were in `images/` (already deleted after Shopify upload). |
| `browser_data/` | 1.5GB | Selenium/undetected-chromedriver browser profile cache. Stored cookies, Cloudflare clearance tokens, and session data to avoid re-solving Turnstile challenges. A new profile is created automatically when any script runs. |
| `debug/` | 137MB | ~200 screenshot PNGs and HTML dumps captured during scraper development for debugging Cloudflare bypass, login issues, and page navigation. |

---

## What's Still Here (and Why)

| File | Why It's Kept |
|---|---|
| `sync_stock_prices.py` | **Active** — runs every 12h via GitHub Actions cron |
| `build_shopify_map.py` | **Active** — needed after adding new products |
| `generate_shopify_csv.py` | **Active** — generates import CSV for new products |
| `shopify_bulk_import.py` | **Active** — imports new products to Shopify |
| `full_discovery.py` | **Active** — discovers new products from Steel City |
| `product_names.json` | **Active** — master product database (8,081 products) |
| `shopify_product_map.json` | **Active** — SKU → {product_id, variant_id} mapping (8,353 SKUs) |
| `bulk_import_progress.json` | **Active** — sync uses this to skip pre-existing Shopify products |
| `price_locks.json` | **Active** — 15 Titan vacuum MAP-priced SKUs |
| `sync_log.json` | **Active** — audit trail of all sync runs |
| `requirements.txt` | **Active** — Python dependencies for GitHub Actions |
| `.env` | **Active** — Shopify API credentials |
| `.github/workflows/sync-stock-prices.yml` | **Active** — 12h cron job |
| `VaM Watermark.png` | **Kept** — needed if images ever need to be reprocessed |
| `CLAUDE.md` | **Context** — project state and instructions for Claude agents |
| `OPERATIONS.md` | **Context** — human-readable operations guide |
| `Schematics.md` | **Context** — architecture and wiring documentation |
| `instructions.md` | **Context** — original project instructions |
| `pricing.md` | **Context** — pricing strategy notes |
| `output/` directory | **Reference** — contains shopify_import.csv (source of truth for what was imported), product_descriptions.xlsx, removed_products.json, steel_city_parts.xlsx |
