# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Steel Stealer scrapes products from Steel City Vacuum's dealer portal and syncs them into the Shopify store at `1bb2a2-2.myshopify.com` (Vacuums and More / evacuumsandmore.com). Phases 1–3 of the import (schematics scrape, image pipeline, catalog discovery, Shopify CSV import, image upload) are complete. The active loop is **`sync_stock_prices.py`** — runs every 12h via GitHub Actions, polls Steel City for stock + dealer cost changes, drafts/reactivates products, and bumps retail when costs rise.

## Where to Look First

Before changing anything, read the doc that matches the layer you're touching:

| If you're working on... | Read |
|---|---|
| The 12h sync, MAP locks, pricing tiers, GitHub Actions ops | `OPERATIONS.md` |
| Script wiring, data flow, API endpoints, file map | `Schematics.md` |
| Phase 1–3 history + what was abandoned and why | `instructions.md` |
| Past mistakes to avoid | `tasks/lessons.md` (create on first correction) |

`Schematics.md` is a hard requirement: **update it after any change to a pipeline script.** Mentioned explicitly in this file's predecessor and worth keeping — this codebase has a lot of moving scripts and the diagram is the only thing that keeps them coherent.

## Commands

```bash
# Activate venv first
source .venv/bin/activate
export $(cat .env | xargs)   # Local only; CI uses GitHub Secrets

# The sync (main production loop)
python3 sync_stock_prices.py --dry-run     # Always preview first
python3 sync_stock_prices.py               # Apply changes (resumable)

# Pricing
python3 calculate_competitive_prices.py --dry-run   # Competitor-driven repricing
python3 generate_pricing.py                          # Tiered markup (fallback)

# Discovery / re-scrape
python3 full_discovery.py                  # 7-step pipeline (resumable per step)
python3 full_discovery.py --step discover  # See Schematics.md for step list

# Shopify infra
python3 build_shopify_map.py               # Rebuild SKU → {product_id, variant_id}
python3 generate_shopify_csv.py            # Generate import CSV (auto-tagged)
```

There is no test suite. Verify by running with `--dry-run`, reading `sync_log.json`, or spot-checking Shopify admin.

## Architecture Highlights (the non-obvious bits)

- **`product_names.json` is the source of truth** for 8,081 products (clean_name, brand, sku, dealer cost, retail_price, in_stock, description). The Shopify store is downstream of it.
- **`shopify_product_map.json`** maps SKU → `{product_id, variant_id}`. Rebuild via `build_shopify_map.py` whenever new products land in Shopify.
- **`bulk_import_progress.json`** is the gate for the sync: only SKUs in this file are touched. The ~1,399 pre-existing Shopify products are never modified.
- **`price_locks.json`** is the MAP allowlist (15 Titan models + 32 SEBO machines, the latter auto-locked by `build_reprice_targets.py`). Sync skips price updates for any SKU listed here — value is just a human note, the key is what matters.
- **Dual-source exclusions: `dual_source_skus.json` + `dual_source_brands.json`.** Products in either list are skipped by the sync's stock/cost loop — no stock check, no Steel City cost updates. The brand file is the right place when a *whole brand* is available from a direct distributor (e.g. `["SEBO"]`). If a brand belongs there, every SKU under it should be untracked in Shopify (`inventory_management=null`, `inventory_policy=continue`) — `restore_dual_source.py --dry-run` reports which Shopify variants drifted into stock-tracking by accident.
- **Dual-source parts still get competitor repricing.** `reprice_brands.json` (`["SEBO"]`) → `build_reprice_targets.py` → `reprice_targets.json`: the sync runs a price-only competitor-undercut pass over these (`--reprice-only` to run just that). Strictly competitor-driven — no competitor data, no change; machines are MAP-locked. See `OPERATIONS.md` §2b.
- **Prices only go up** for cost-driven (markup) pricing — the sync ignores Steel City cost decreases. Competitor-driven prices move both directions (down to undercut, back up when competitors raise). Documented in `OPERATIONS.md` §5.
- **CI data bundle.** Sensitive JSON (product_names, shopify_product_map, price_locks, competitor_prices, bulk_import_progress, missing_import_progress, dual_source_skus, dual_source_brands, competitors, reprice_brands, reprice_targets) is gitignored locally but ships to CI encrypted as `data.tar.gz.gpg`. GitHub Actions decrypts with `DATA_PASSPHRASE`, runs the sync, re-encrypts, and commits if the SHA changed. If you change which files the sync reads/writes, update the tar list in **both** `.github/workflows/sync-stock-prices.yml` and `.github/workflows/scrape-competitor-prices.yml` — they must stay identical or one workflow will drop the other's files from the bundle.
- **Chrome version is pinned at `version_main=145`** in every Selenium script (`sync_stock_prices.py`, `full_discovery.py`, etc.). Bump in lockstep when Chrome updates — Cloudflare detection breaks otherwise.
- **Cloudflare bypass = undetected-chromedriver.** Playwright (stealth included) gets caught by Turnstile. The `uploads/` CDN does NOT need a browser — plain `requests` works.
- **Watermark removal (OpenCV inpainting) was removed** — it destroyed label text. rembg's bg removal handles the Steel City watermark adequately.

## Critical Safety Rules

- **Never trust local progress files for Shopify state.** Before creating products, uploading images, or running anything destructive, check the live Shopify store. Progress JSONs drift.
- **Never run `shopify_upload.py upload` or `import_missing_products.py` without explicit user approval** — they mutate the production store.
- **Sync is the only thing that should auto-commit to git** (re-encrypted bundle only). Don't `git push` from a session unless James asks.
- **57% of schematic parts have `picture='0'`** (special order, no image). This is expected, not a bug.

## Workflow

1. **Plan mode for non-trivial work** (3+ steps or architectural decisions). If something goes sideways mid-task, stop and re-plan — don't push through.
2. **Use subagents** for research, exploration, and parallel analysis to keep the main context clean. One focused task per subagent.
3. **After any correction from James**, append the pattern to `tasks/lessons.md` so the same mistake doesn't repeat. Review lessons at session start.
4. **Verification before "done"**: dry-run, check logs, demonstrate the change works. Never claim success on a script you didn't run.
5. **Autonomous bug fixing**: if given a failure (CI log, error, draft product), just fix it — don't ask for hand-holding.
6. **Demand elegance, balanced**: for non-trivial changes, ask "is there a simpler way?" Skip for obvious fixes — don't over-engineer.

## Core Principles

- **Simplicity first** — touch the minimum code that solves the problem.
- **No bandaid scripts** — fix root causes. Don't write a new one-off `fix_X.py` when an existing pipeline can be re-run or extended.
- **Senior developer standards** — would a staff engineer approve this diff?
