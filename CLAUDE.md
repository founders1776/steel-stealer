### CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Objective

Steel Stealer — Scraper pipeline to import vacuum parts from Steel City Vacuum into our Shopify store. We log into their website, scrape schematics + catalog for all parts, process images, generate descriptions, and prepare Shopify-ready data.

## User Info (Steel City Login):
- Account: REDACTED_ACCT
- Id: REDACTED_USER
- Password: REDACTED_PASS
- **Login URL**: `https://www.steelcityvac.com/a/s/` (NOT `/a/si/` — that's dead)
- **Login approach**: Navigate to `/a/s/`, wait 6s, fill fields directly (no Cloudflare wait needed, no `--user-data-dir`)
- **Login form fields**: `scv_customer_number` (ID: scv_customer_number), `username` (ID: username_login_box), password (ID: password_login), submit (name: loginSubmit)
- **After login**: Navigate to base URL (`driver.get(base_url)`) so API calls work
- **Driver config**: Simple — just `--no-sandbox` flag, no `--user-data-dir` (causes issues)
- **API has 62 fields** — we originally only captured 10. Key missed fields: `category_names`, `categoryPath`, `product_code2`, `other_images`, `original_Price`, `qty_on_hand`

## Shopify Store:
- Store: `1bb2a2-2.myshopify.com`
- Credentials in `.env` file (SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN)
- OAuth app Client ID: `REDACTED_CLIENT_ID`
- Token obtained via `shopify_oauth.py` (OAuth flow with localhost:9999 callback)
- Current store has **1,399 existing products** (exported to `output/shopify_current_products.csv`)

## ⚠️ CRITICAL — Documentation After Every Change

**IMPORTANCE: HIGHEST — This rule is non-negotiable.**

After completing ANY code change (new feature, bug fix, refactor, config update), you MUST update the relevant documentation files before considering the task done. This project has many interconnected scripts and data files — without up-to-date docs, future agents will make incorrect assumptions and break things.

**What to update:**
1. **`CLAUDE.md`** — Update current state, product counts, phase status, data file descriptions, or any section affected by your change. If you added/removed/renamed a file, update the File Reference tables.
2. **`Schematics.md`** — Update wiring diagrams and file relationships whenever you modify how scripts connect to each other, change input/output files, or alter data flow.
3. **`OPERATIONS.md`** — Update if your change affects how the system is operated (new commands, changed workflows, new secrets needed, etc.).

**When in doubt, over-document.** A 2-minute doc update now saves 20 minutes of confused debugging later. If your change touched it, document it.

## USEFUL INFO
Update this CLAUDE.md file to give yourself better instructions as we learn more about the project.

---

## Current State (as of 2026-03-22)

### What's Done
- **Phase 1** — Schematics scraping: 54 brands, 12,576 unique parts → `output/steel_city_parts.xlsx`
- **Phase 2a** — Google Images approach ABANDONED (low quality)
- **Phase 2b** — Steel City images: 5,361 processed product images in `images/{sku}/1.jpg`
- **Phase 2b-catalog** — Catalog discovery: 1,165 additional products + 800 images
- **Phase 2c** — Image quality flagging: 810 flagged for review
- **Phase 2d** — Product name cleaning (`product_names.json`) + AI-generated descriptions (template-based, 25+ categories)
- **Phase 2e** — Stock check via API: all 5,926 products checked for in_stock status
- **Phase 2f** — Full product discovery: 55,998 products enumerated, 3,273 new products added with images
- **Phase 2g** — Competitive retail pricing: tiered markup engine, competitor-validated pricing

### Product Counts
- **8,520 total products** in `product_names.json` (after Phase 2f + missing product import)
- **8,520 products** have retail prices
- **8,788 SKUs** mapped in `shopify_product_map.json`

### Product Filtering (IMPORTANT)
- Started with **5,926** unique products in `product_names.json`
- Removed **2** NLA products with no alternatives
- Removed **1,116** special order products (in_stock=0 with no alt items)
- **Kept 184** special order products that DO have alt items (NLA with alts are always kept)
- Phase 2f added **3,273** additional products → **8,081 total**
- Missing product import added **260** products (empty product_code from API) → **8,520 total** (as of 2026-03-22)

### Key Insight: Steel City Stock Status
The `product_info` API returns `in_stock` as `"1"` (in stock) or `"0"` (special order). The `qty_on_hand` field exists but is **always 0** for all products — Steel City does not populate it with real inventory counts. Only `in_stock` is a reliable stock signal. Products with `in_stock=0` show as "Special Order" on the Steel City website.

### Key Insight: API in_stock Accuracy
Among 10,532 products verified with front-end qtyoh scraping, the API `in_stock` field has a **2.9% false positive rate** (not 12.6% as previously documented). 169/175 API in_stock=1 products confirmed by qtyoh. The 12h sync job catches false positives by drafting out-of-stock products.

### Key Insight: Empty product_code Products
**2,392 products** in discovery_v2 enrichment have an empty `product_code` from the API. These products exist and are real, but the `product_info` API returns no product_code for them. The discovery key (e.g. `26-1401-94`) should be used as the SKU instead. The `import_missing_products.py` script handles this — it found **260** such products that were in-stock with pictures but missing from the store.

### Key Insight: Steel City API Enumeration
- **Search API caps at ~486 results per query** — no pagination parameter (`page`, `offset`, `skip`) actually paginates past this limit.
- **Category-based search** (`categoryID` param) also caps at ~486 per category, even leaf subcategories.
- **The working approach**: progressive-depth prefix search — 2-char prefixes (aa, ab, ..., 99, 9-), drilling into 3-char and 4-char prefixes whenever a query hits the cap. This is how 29,302+ products were found originally.
- **Session is critical**: After login, MUST navigate to base URL (`steelcityvac.com`) then to `/a/s/` — otherwise the search API returns garbage data. The `login()` function handles this.
- **productID sweep does NOT work**: the `product_info` API only accepts product_code strings (like `38-2450-02`), not integer productIDs. Integer IDs that happen to be valid product codes return those products, but this is coincidental.

### Rule: Always Keep NLA Items With Alternatives
When filtering products, NEVER remove NLA items that have alt_items. Those should always be kept — the user specifically requested this.

### Rule: NLA Products Must Never Be Active on Shopify
Products with "NLA" or "No Longer Available" in their API name/description must be drafted on Shopify, never active. The 12h sync (`sync_stock_prices.py`) detects NLA markers in API responses and auto-drafts them before any stock/price logic runs. NLA products are also marked `in_stock: "0"` in `product_names.json`. (Added 2026-03-30 after a customer ordered NLA SKU 155458.)

### Pricing Strategy (Phase 2g)
- `price` field = **dealer cost** from Steel City (e.g., `"$4.05"`)
- `retail_price` field = **our selling price** after tiered markup + charm pricing
- Tiered markup: 8.0× ($0–1) → 4.5× ($1–3) → 3.2× ($3–7) → 2.5× ($7–15) → 2.2× ($15–30) → 1.9× ($30–60) → 1.7× ($60–120) → 1.5× ($120–300) → 1.4× ($300+)
- All prices end in `.99` (charm pricing)
- Minimum floor: **$6.99** — no product listed below this
- Avg retail: $45.64 | Median: $19.99 | Avg margin: 64.5%
- **Validated against 20 random competitors** — pricing is competitive with market rates
- Tiers were adjusted down after initial competitor analysis showed we were 30-60% too high

### Current Data Files
- `product_names.json` — **8,520 products** with: clean_name, description, brand, model, sku, price, retail_price, in_stock, raw_name
- `output/product_descriptions.xlsx` — Same data in Excel + Retail Price column
- `output/removed_products.json` — 1,118 excluded products for reference
- `stock_check_progress.json` — Full stock check results for all 5,926 original products
- `full_discovery_progress.json` — Full discovery pipeline checkpoint

### Phase 3c — Automated Stock & Price Sync (TESTED & LIVE)
- `build_shopify_map.py` — paginates Shopify → `shopify_product_map.json` (8,788 SKUs mapped)
- `sync_stock_prices.py` — polls Steel City API, compares stock/price, updates Shopify:
  - Drafts out-of-stock products, reactivates back-in-stock
  - Updates pricing only on cost **increases** (recalculates via tiered markup)
  - Cost decreases → no action (protect margins)
  - Resumable with checkpoint, `--dry-run` mode
  - **Safety: only touches products WE imported** (cross-refs bulk_import_progress.json, skips 372 pre-existing)
  - URL-encodes SKUs with special chars (`+`, `&`, etc.)
- `.github/workflows/sync-stock-prices.yml` — runs every 12h (6am/6pm UTC) + manual trigger
- `sync_log.json` — append-only change log (3 entries: 2 dry runs + 1 live)
- **First live sync**: 7,921 checked, 18 drafted (out of stock), 10 errors (SKUs with `+` chars), 0 price changes
- **GitHub Secrets needed**: SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN, EMAIL_USERNAME, EMAIL_PASSWORD, EMAIL_TO

### Shopify Upload Status
- **6,965 products imported** from `shopify_import.csv` (0 failures)
- **260 additional products imported** via `import_missing_products.py` (0 failures, 2026-03-22)
- **~1,399 pre-existing products** from other vendors (untouched)
- `bulk_import_progress.json` — 6,965 entries, all "created"
- `missing_import_progress.json` — 260 entries, all "created"

### Phase 3-desc — SEO Description Enhancement (COMPLETE)
- `build_compatibility_map.py` → `compatibility_map.json` (11,011 SKUs with brand/model data)
- `enhance_descriptions.py` — splits 8,081 products into 29 batches, merges agent outputs
- 29 Claude agents wrote SEO-optimized HTML descriptions (benefit-driven, 60-120 words + compat lists)
- 3,949 products have full compatibility model lists from schematics data
- `update_descriptions.py` — pushed `body_html` ONLY to Shopify (7,292 updated, 0 errors)
- **Safe**: only touches `body_html` — no prices, stock, status, or inventory fields affected
- **GitHub sync unaffected**: `sync_stock_prices.py` doesn't read/write description field

### Phase 4 — Competitive Pricing Overhaul (IN PROGRESS)
- `discover_competitors.py` — DuckDuckGo search to find competitor domains (1,250 SKU sample)
- `scrape_competitor_prices.py` — Multi-competitor price scraper (Shopify API + Playwright)
  - **Accessory detection**: `is_accessory_match()` skips results where title contains "fits", "for", "replacement", "compatible with" etc. near the SKU (prevents matching accessories FOR a model as the model itself)
  - **Price sanity check**: `price_is_sane()` rejects competitor prices outside 0.5x–5x of our dealer cost
  - **`--rebuild` flag**: Re-aggregates `competitor_prices.json` from progress data without re-scraping
  - Old median-based outlier filter REMOVED — it was counterproductive (removed correct prices when bad matches were the majority)
- `calculate_competitive_prices.py` — Pricing engine: undercut competitors, enforce break-even floor
  - **Per-competitor validation**: Filters individual competitor entries by 0.5x–5x dealer cost ratio before averaging
  - **Pricing strategy**: (1) Beat lowest valid competitor by $1, (2) if unprofitable, undercut average, (3) if still unprofitable, fall back to tiered markup
- `sync_stock_prices.py` — Modified to use competitor-aware pricing (both up AND down)
- `.github/workflows/scrape-competitor-prices.yml` — Weekly scrape (Sunday 2am UTC), calculates prices live and commits updated `product_names.json`
- **Break-even**: dealer_cost + Shopify fees (2.9% + $0.30)
- **MAP products**: 15 SEBO SKUs skipped (in price_locks.json)
- **No competitor data**: falls back to existing tiered markup
- **Workflow**: discover_competitors → user picks 20+ → scrape_competitor_prices → calculate_competitive_prices → sync

### Phase 5 — Full Product Discovery v2 (IN PROGRESS)
- `product_discovery_v2.py` — 11-step pipeline with verified stock checking + Claude agent descriptions
  - **Step 1 (discover)**: Multi-strategy search: 123 manufacturer names, 000-999 3-digit prefixes, 30 part terms. Seeds from `missing_products_enumeration.json` (26,406 pre-found products). Target: ~64,450 total products.
  - **Step 2 (enrich)**: Batch product_info API (8 concurrent), captures all 62 fields
  - **Step 3 (stock)**: Front-end stock verification via `/a/s/p/{sku}` → extract `var qtyoh`. API's `in_stock` field is unreliable (12.6% false positive rate). Only products with `qtyoh > 0` are truly in stock.
  - **Step 4 (filter)**: Keep verified in-stock with picture, NLA with alts (always keep), remove rest
  - **Step 5 (images)**: Download ALL images with retries + rembg + VaM watermark + 2048x2048 pad
  - **Step 6 (crossref)**: Cross-reference ezvacuum.com for product data (titles, descriptions, model lists, tags). Uses existing `ezvacuum_descriptions.json` first, then ezvac suggest API for new SKUs.
  - **Step 7 (names)**: Write enriched batch files to `discovery_v2_batches/` for Claude agents. Each batch includes Steel City API hierarchy, schematics compatibility data, and ezvacuum reference data. **NO templates** — agents write every name and description from scratch.
  - **Step 8 (names-merge)**: Merge agent-written `batch_N_output.json` files back into progress. Each output has `clean_name` + `description` (HTML body). Descriptions MUST include SKU and ALL compatible models.
  - **Step 9 (pricing)**: Competitor-aware pricing (beat lowest by $1) + tiered markup fallback
  - **Step 10 (merge)**: Merge into `product_names.json` + export spreadsheet
  - **Step 11 (report)**: Print stats
- `discovery_v2_progress.json` — Checkpoint file (fully resumable per step)
- `discovery_v2_batches/` — Batch JSON files for Claude agent description writing + `AGENT_INSTRUCTIONS.md`
- **Key improvements**:
  - Uses real `qtyoh` from product pages instead of unreliable API `in_stock` field
  - Image downloads have 3x retries, retry previously-failed images
  - Descriptions written by Claude agents (not templates) with SKU, all compatible models, benefit-driven SEO copy
  - ezvacuum cross-reference for factual enrichment (model lists, product type) — descriptions are original, not copied

### What's Next
- Run `product_discovery_v2.py` to find + import remaining products
- After import: run existing Shopify import pipeline (generate_shopify_csv → shopify_bulk_import → build_shopify_map)
- Image sourcing strategy still needed (Steel City images cover ~43% of products)

---

## File Reference

### Scripts — Shopify Sync & Upload
| File | Purpose |
|------|---------|
| `sync_stock_prices.py` | Automated 12h sync (stock status + pricing) via GitHub Actions. Logs into Steel City, batches API calls, drafts/activates products, recalculates prices on cost increases. **Detects NLA products in API responses and auto-drafts them.** Dry-run mode, resumable, respects price locks, only touches imported products. |
| `build_shopify_map.py` | Paginates all Shopify products → `shopify_product_map.json` (8,353 SKU → product_id/variant_id mappings). |
| `shopify_bulk_import.py` | Bulk import from `output/shopify_import.csv` to Shopify REST API. Rate limiting, error handling, progress tracking → `bulk_import_progress.json`. |
| `import_missing_products.py` | 7-step pipeline for 260 products with empty product_code: extract from discovery_v2, live Shopify dedup (GraphQL), images, ezvacuum cross-ref, agent description batches, pricing, Shopify upload. Resumable via `missing_import_progress.json`. |
| `generate_shopify_csv.py` | Generates Shopify-ready CSV from `product_names.json` with auto-tagging (Bags/Filters/Belts/Hoses/etc), dedup, excludes pre-existing products. |

### Scripts — Product Discovery & Enrichment
| File | Purpose |
|------|---------|
| `full_discovery.py` | 7-step pipeline: (1) search discovery a-z/0-9, (2) enrich via product_info API, (3) filter by stock/images, (4) download raw images, (5) process images (rembg + watermark + pad 2048x2048), (6) clean names + descriptions, (7) merge + export. Resumable via checkpoint. |
| `product_discovery_v2.py` | 11-step pipeline: multi-strategy search, enrich, **front-end stock verification (qtyoh)**, filter, images (with retries), ezvacuum cross-ref, **Claude agent description batches** (no templates), pricing (competitor-aware), merge + export. Resumable via `discovery_v2_progress.json`. Pipeline pauses after writing batches — agents must process them, then resume with `--step names-merge`. |
| `rescrape_full_api.py` | Re-scrape all 62 API fields (originally only had 10). Captures category_names, categoryPath, other_images, original_Price, alt_items, qty_on_hand → `full_api_data.json`. |
| `download_extra_images.py` | Fetch additional images from `other_images` field + missing primaries. Saves as `images/{sku}/2.jpg`, `3.jpg`, etc. |
| `ezvacuum_cross_ref.py` | Cross-ref SKUs against ezvacuum.com suggest API for descriptions, models, tags → `ezvacuum_descriptions.json`. |
| `check_qty_on_hand.py` | Diagnostic: queries Steel City API for in_stock vs qty_on_hand breakdown. Finding: qty_on_hand always 0, only in_stock is reliable. |

### Scripts — Description Enhancement
| File | Purpose |
|------|---------|
| `enhance_descriptions.py` | Splits 8,081 products into 29 batches (280/batch) with enriched data (categories, alt_items, compatibility). Includes `merge_results()` to fold agent outputs back into `product_names.json`. |
| `build_compatibility_map.py` | Reads `output/steel_city_parts.xlsx` (schematics) → `compatibility_map.json` (11,011 SKUs with brand/model data). |
| `update_descriptions.py` | Pushes `body_html` & title ONLY to Shopify. Safe alongside sync — doesn't touch pricing/inventory/status. |

### Scripts — Competitive Pricing (Phase 4)
| File | Purpose |
|------|---------|
| `discover_competitors.py` | DuckDuckGo search for competitor domains. Stratified sampling (30% top brands), ~1,250 SKUs, excludes 40+ marketplaces → `competitor_domains.json`. |
| `scrape_competitor_prices.py` | Two scraper types: Shopify sites (`/search/suggest.json` API) and non-Shopify (Playwright + stealth). Accessory detection (`is_accessory_match`) + price sanity filtering (0.5x-5x dealer cost). Supports `--limit`, `--competitor`, `--rebuild` flags. |
| `calculate_competitive_prices.py` | Beat lowest competitor by $1, fall back to avg undercut if unprofitable, then tiered markup. Per-competitor price validation (0.5x-5x dealer cost ratio). Break-even floor = dealer_cost + Shopify fees. Supports `--dry-run`. |

### Scripts — Batch Description Generation (One-time)
| File | Purpose |
|------|---------|
| `process_batch4.py` | Generate enhanced titles & HTML descriptions for batch 4. Category detection, abbreviation expansion, compatibility formatting. |
| `process_batch5.py` | Same as above for batch 5. |
| `gen_batch6.py` | Same as above for batch 6. |
| `gen_batch18.py` | Same as above for batch 18. |

### Data Files
| File | Purpose |
|------|---------|
| `product_names.json` | **Master database** — 8,081 products with clean_name, description, brand, model, sku, price (dealer cost), retail_price, in_stock, raw_name. |
| `shopify_product_map.json` | SKU → {product_id, variant_id} mapping for all 8,353 Shopify products. |
| `bulk_import_progress.json` | Import tracking — 6,965 entries, all "created". |
| `compatibility_map.json` | 11,011 SKUs with brand → [model list] from schematics data. |
| `full_api_data.json` | Enriched API responses (all 62 fields) for all products (~25MB). |
| `competitor_domains.json` | Domain frequency counts from competitor discovery. |
| `competitor_prices.json` | Scraped prices per competitor per SKU. |
| `competitors.json` | Selected competitor sites for scraping. |
| `pricing_decisions.json` | Competitive pricing calculations per SKU. |
| `price_locks.json` | 15 MAP-locked Titan SKUs — sync won't auto-update these prices. |
| `sync_log.json` | Append-only audit trail of all sync runs. |
| `missing_import_progress.json` | Progress for `import_missing_products.py` — 260 products extracted, checked, imported. |
| `ezvacuum_descriptions.json` | Harvested descriptions from ezvacuum.com cross-referencing. |

### Progress/Checkpoint Files (Resumable)
| File | Purpose |
|------|---------|
| `full_discovery_progress.json` | Checkpoint for `full_discovery.py` |
| `discovery_v2_progress.json` | Checkpoint for `product_discovery_v2.py` (search, enrich, stock, filter, images, pricing) |
| `competitor_discovery_progress.json` | Checkpoint for `discover_competitors.py` |
| `competitor_price_progress.json` | Checkpoint for `scrape_competitor_prices.py` |
| `ezvacuum_progress.json` | Checkpoint for `ezvacuum_cross_ref.py` |
| `rescrape_progress.json` | Checkpoint for `rescrape_full_api.py` |
| `extra_images_progress.json` | Checkpoint for `download_extra_images.py` |
| `desc_update_progress.json` | Checkpoint for `update_descriptions.py` |
| `qty_check_progress.json` | Checkpoint for `check_qty_on_hand.py` |
| `real_stock_check_progress.json` | Stock check results for active products |
| `removed_stock_check_progress.json` | Stock check results for removed products |

### Output Directory
| File | Purpose |
|------|---------|
| `output/steel_city_parts.xlsx` | Phase 1 schematics data — 54 brands, 12,576 parts. |
| `output/product_descriptions.xlsx` | Product data in Excel + Retail Price column. |
| `output/shopify_import.csv` | Shopify-ready CSV (6,965 products). |
| `output/shopify_current_products.csv` | Export of 1,399 pre-existing Shopify products. |
| `output/removed_products.json` | 1,118 excluded products for reference. |
| `output/catalog_new_products.xlsx` | Catalog discovery results. |

### Config & Documentation
| File | Purpose |
|------|---------|
| `.env` | Shopify credentials (SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN). |
| `.gitignore` | Excludes progress files, output/, images/, .venv/, .env, debug/. |
| `requirements.txt` | Dependencies: playwright, playwright-stealth, openpyxl, Pillow, ddgs, requests, rembg, opencv-python-headless, numpy. |
| `OPERATIONS.md` | Human ops guide: credentials, automated sync, MAP pricing, common tasks, safety guards. |
| `Schematics.md` | Architecture & wiring documentation for all scripts. |
| `paper_trail.md` | History of deleted one-time scripts (~2.2GB reclaimed). |
| `instructions.md` | Technical discoveries: site structure, API formats, anti-bot bypass, known issues. |
| `pricing.md` | Tiered markup strategy, charm pricing rules, margin stats. |

### GitHub Actions
| File | Purpose |
|------|---------|
| `.github/workflows/sync-stock-prices.yml` | Runs `sync_stock_prices.py` every 12h (6am/6pm UTC) + manual trigger. Pinned to Python 3.12 (3.13 removed distutils, breaks undetected_chromedriver). |
| `.github/workflows/scrape-competitor-prices.yml` | Runs competitor price scrape weekly (Sunday 2am UTC). |

### Directories
| Directory | Purpose |
|-----------|---------|
| `images/` | ~8,000+ processed product images organized as `images/{sku}/1.jpg`, `2.jpg`, etc. |
| `desc_batches/` through `desc_batches_v4/` | Batch JSON files for Claude agent description enhancement (29 batches × 4 versions). |
| `ebay_batches/` | eBay search target batches. |
| `missing_batches/` | Batch JSON files + agent outputs for the 260 missing product import (2 batches). |
| `fix_batches/` | Description fix batches. |
| `review_flags/` | Image quality review flags. |
| `debug/` | Debug outputs from various scripts. |
| `tasks/` | Task management (todo.md, description_enhancement_plan.md). |
| `__pycache__/` | Python bytecode cache. |

---

## Workflow Orchestration

1. **Plan Mode Default** — Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions). If something goes sideways, STOP and re-plan immediately — don't keep pushing. Use plan mode for verification steps, not just building. Write detailed specs upfront to reduce ambiguity.

2. **Subagent Strategy** — Use subagents liberally to keep main context window clean. Offload research, exploration, and parallel analysis to subagents. For complex problems, throw more compute at it via subagents. One task per subagent for focused execution.

3. **Self-Improvement Loop** — After ANY correction from the user: update `tasks/lessons.md` with the pattern. Write rules for yourself that prevent the same mistake. Ruthlessly iterate on these lessons until mistake rate drops. Review lessons at session start for relevant project.

4. **Verification Before Done** — Never mark a task complete without proving it works. Diff behavior between main and your changes when relevant. Ask yourself: "Would a staff engineer approve this?" Run tests, check logs, demonstrate correctness.

5. **Demand Elegance (Balanced)** — For non-trivial changes: pause and ask "is there a more elegant way?" If a fix feels hacky: "Knowing everything I know now, implement the elegant solution." Skip this for simple, obvious fixes — don't over-engineer. Challenge your own work before presenting it.

6. **Autonomous Bug Fixing** — When given a bug report: just fix it. Don't ask for hand-holding. Point at logs, errors, failing tests — then resolve them. Zero context switching required from the user. Go fix failing CI tests without being told how.

## Task Management

- **Plan First**: Write plan to `tasks/todo.md` with checkable items
- **Verify Plan**: Check in before starting implementation
- **Track Progress**: Mark items complete as you go
- **Explain Changes**: High-level summary at each step
- **Document Results**: Add review section to `tasks/todo.md`
- **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.
