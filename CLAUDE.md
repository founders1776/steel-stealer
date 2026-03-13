### CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Objective

Steel Stealer — Scraper pipeline to import vacuum parts from Steel City Vacuum into our Shopify store. We log into their website, scrape schematics + catalog for all parts, process images, generate descriptions, and prepare Shopify-ready data.

## User Info (Steel City Login):
- Account: REDACTED_ACCT
- Id: REDACTED_USER
- Password: REDACTED_PASS

## Shopify Store:
- Store: `1bb2a2-2.myshopify.com`
- Credentials in `.env` file (SHOPIFY_STORE, SHOPIFY_ACCESS_TOKEN)
- OAuth app Client ID: `REDACTED_CLIENT_ID`
- Token obtained via `shopify_oauth.py` (OAuth flow with localhost:9999 callback)
- Current store has **1,399 existing products** (exported to `output/shopify_current_products.csv`)

## AFTER EACH UPDATE TO THIS SCRIPT
Update `Schematics.md` with all wiring and relevant files. This is imperative for complex scripts.

## USEFUL INFO
Update this CLAUDE.md file to give yourself better instructions as we learn more about the project.

---

## Current State (as of 2026-03-11)

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
- **8,081 total products** in `product_names.json` (after Phase 2f full discovery)
- **7,974 products** have retail prices (107 skipped — no dealer cost)

### Product Filtering (IMPORTANT)
- Started with **5,926** unique products in `product_names.json`
- Removed **2** NLA products with no alternatives
- Removed **1,116** special order products (in_stock=0 with no alt items)
- **Kept 184** special order products that DO have alt items (NLA with alts are always kept)
- Phase 2f added **3,273** additional products → **8,081 total**

### Key Insight: Steel City Stock Status
The `product_info` API returns `in_stock` as `"1"` (in stock) or `"0"` (special order). The API does NOT use the text "special order" in name/description fields — you must check the `in_stock` field directly. Products with `in_stock=0` show as "Special Order" on the Steel City website.

### Rule: Always Keep NLA Items With Alternatives
When filtering products, NEVER remove NLA items that have alt_items. Those should always be kept — the user specifically requested this.

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
- `product_names.json` — **8,081 products** with: clean_name, description, brand, model, sku, price, retail_price, in_stock, raw_name
- `output/product_descriptions.xlsx` — Same data in Excel + Retail Price column
- `output/removed_products.json` — 1,118 excluded products for reference
- `stock_check_progress.json` — Full stock check results for all 5,926 original products
- `full_discovery_progress.json` — Full discovery pipeline checkpoint

### Phase 3c — Automated Stock & Price Sync (TESTED & LIVE)
- `build_shopify_map.py` — paginates Shopify → `shopify_product_map.json` (8,353 SKUs mapped)
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
- **~1,399 pre-existing products** from other vendors (untouched)
- `bulk_import_progress.json` — 6,965 entries, all "created"

### What's Next
- Push to GitHub, configure secrets, enable Actions for automated 12h sync
- Image sourcing strategy still needed (Steel City images cover ~43% of products)

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
