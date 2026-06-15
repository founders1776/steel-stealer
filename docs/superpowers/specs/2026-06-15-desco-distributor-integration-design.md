# Desco Vacs Distributor Integration — Design

**Date:** 2026-06-15
**Status:** Approved pending Phase 0 + James's spec review

## Purpose

Add **Desco Vacs** as a second product distributor alongside Steel City, mirroring the Steel City pipeline: pull catalog + costs + stock + images, write SEO-rich descriptions (optimized for agentic storefronts + Google), publish to all sales channels (except Point of Sale), and apply the same competitor-undercut/tiered-markup pricing and 12h price sync. No duplicate SKUs/products. Every product is explicitly source-tagged (Steel City vs Desco) on the backend.

## Decisions (from discovery)

- **Desco access:** API preferred if it exposes accurate/complete catalog, dealer cost, stock, images. **Phase 0 investigates and confirms** before ingestion is finalized; fall back to site scrape if no real API.
- **Source identifier:** `custom.source` **metafield** (`steel_city` | `desco`) on every product; **backfill** existing Steel City + Miele/Lindhaus products. Mirrored locally in `source_map.json`.
- **SKU collision:** if a SKU already exists in the store (any source), **keep existing, skip Desco**. One listing per SKU.
- **Pricing:** **same engine as Steel City** — tiered markup on dealer cost + competitor-undercut + 12h sync (stock draft/reactivate, cost-rise repricing). MAP brands route through existing `dual_source_brands`/`reprice_brands`.
- **Images:** same Steel City treatment — rembg background removal → 2048×2048 white canvas → VaM watermark.
- **Channels:** Online Store, Shop, Agentic, Google & YouTube (exclude Point of Sale).

## Approach (C — thin adapter + reuse shared downstream)

One new ingestion adapter for Desco; everything downstream is reused because it already keys on SKU + product_id, not on distributor. A `source` dimension is layered on top. The live Steel City path is left untouched.

## Architecture

### Phase 0 — Desco access investigation (GATE)
Research Desco's dealer portal/site (candidate domains incl. descovac.com / descovacs.com). Determine whether a complete API exists (catalog list, per-product dealer cost, stock/availability, image URLs, auth model). Output: `docs/desco_access_findings.md` documenting endpoints/auth OR a "no API → scrape" recommendation. **The ingestion section below finalizes after Phase 0.** No build past this gate without confirmed access shape.

### Source identifier
- **Metafield** `custom.source` (single_line_text), values `steel_city` | `desco`, on every product. Definition created once via GraphQL `metafieldDefinitionCreate`.
- **Backfill pass** (`backfill_source.py`, one-time): sets `custom.source` on all existing imported products. Steel City + sheet-import (Miele/Lindhaus) → derive source from the existing progress files (`bulk_import_progress.json`, `missing_import_progress.json` → steel_city; sheet_import runs → their brand's distributor). Pre-existing ~1,399 store products: leave untagged (not ours) or tag `steel_city` only if in our progress files.
- **Local mirror** `source_map.json` (SKU → source) so offline tooling (sync, reprice) resolves source without Shopify calls.

### Ingestion — `desco_ingest.py` (new)
- Pull Desco catalog via the Phase-0-confirmed API.
- Normalize each record to the existing `product_names.json` shape: `{sku, brand, clean_name, dealer_cost, in_stock, image_urls[], description?, source:"desco"}`.
- Write `desco_products.json` (Desco's source-of-truth file, sibling to `product_names.json`).
- Resumable, paginated, rate-limited (mirror existing script conventions).

### Dedup — reuse `sheet_import` matcher
- For every Desco SKU: match against the live store (exact + O↔0 fuzzy within vendor), double-checked live via GraphQL — the existing `sheet_import.step_match` logic.
- Buckets: `new` (proceed) / `existing` (skip + log) / `ambiguous` (report). Per the decision, `existing` is skipped (Steel City/whoever is already there wins).

### Downstream reuse (new Desco SKUs only)
- **Descriptions:** Claude research agents, batched ~10/agent. Prompt tuned for **agentic-storefront + Google** optimization: entity-dense, explicit compatible-model lists, structured spec facts, the SKU verbatim, meta_title ≤60 / meta_description ≤160. Same content-file contract + validation as `sheet_import` (`content/<sku>.json`).
- **Images:** download Desco image URLs → existing `images` step (rembg + 2048 + VaM logo) → upload. Same accuracy rule: better empty than wrong.
- **Create:** drafts via `import_missing_products`/`sheet_import` machinery — `vendor`=brand, auto-tags, SEO metafields, **`custom.source=desco`**, untracked-or-tracked inventory per Desco stock model (TBD Phase 0).
- **Channels:** publish to Online Store, Shop, Agentic, Google & YouTube (not POS) via `publishablePublish` (the publication IDs already captured).
- **Activation:** first batch lands as drafts for James's review, then `--activate` (the sheet_import gate pattern).

### Pricing + sync (source-neutral)
- Desco new SKUs enter the **same** pricing: tiered markup on Desco dealer cost (`generate_pricing`/`calculate_markup_price`), competitor-undercut via `competitor_prices.json` + `reprice_targets.json`, MAP locks for any MAP brands Desco carries (`dual_source_brands`/`reprice_brands`).
- **12h sync extension:** `sync_stock_prices.py`'s "our products" gate currently unions `bulk_import_progress.json` + `missing_import_progress.json`. Extend it to also include Desco's progress file so the sync drafts/reactivates Desco products on stock changes and repriced on cost rises — keyed by product_id, source-neutral. Desco stock polled from its API in the sync (or a Desco-specific stock step), mirroring the Steel City stock check.
- **CI data bundle:** add `desco_products.json`, `source_map.json`, and Desco progress file to the encrypted bundle tar lists in **both** workflows (kept identical). Add any Desco API secret to GitHub Secrets.

## Module boundaries
- `desco_ingest.py` — Desco API → `desco_products.json` (only Desco-specific logic; one clear job).
- `backfill_source.py` — one-time `custom.source` metafield backfill + `source_map.json` build.
- Reused unchanged where possible: `sheet_import.py` (match/research/images/create/register/activate patterns), `import_missing_products.py` (Shopify helpers, tags, markup), `generate_pricing.py`, `build_reprice_targets.py`, `sync_stock_prices.py` (one gate extension).
- The Desco run uses a per-run dir like `sheet_imports/` → `desco_imports/<date>/` with manifest/content/images/progress, reusing the sheet_import step machinery where it fits.

## Error handling
- Phase 0 gates everything; if Desco data is incomplete/inaccurate, stop and report rather than import bad data.
- Per-SKU failures logged + skipped, never abort a run (existing pattern). Dry-run on all mutating steps. Drafts-first for the first batch.
- Source metafield write failures recorded per-SKU; `source_map.json` is the fallback truth.

## Testing / verification
- No test suite (project convention): `--dry-run`, report files, live spot-checks in Shopify admin.
- Phase 0 deliverable reviewed before build.
- First Desco batch: drafts → spot-check → activate, then verify channel placement + source metafield on a sample.

## Out of scope
- Refactoring Steel City scripts into a generalized engine (approach B) — deferred.
- Changing existing Steel City pricing/sync behavior beyond the additive gate extension.
- POS channel.

## Open items resolved in Phase 0
- Desco auth model + exact endpoints (catalog, cost, stock, images).
- Whether Desco stock is real-time (tracked) or always-available (untracked).
- Desco catalog size (drives batch counts + effort estimate).
- Which Desco brands are MAP-protected (route to locks).
