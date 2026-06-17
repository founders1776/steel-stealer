# Steel Stealer — Operations Guide

Everything you need to know to manage the Shopify store and sync pipeline.

---

## Quick Reference

| What | Where |
|---|---|
| Shopify Admin | `1bb2a2-2.myshopify.com/admin` |
| Shopify credentials | `.env` file |
| Steel City login | See `.env` (SC_ACCOUNT, SC_USER, SC_PASSWORD) |
| Product data | `product_names.json` (8,081 products) |
| Shopify SKU map | `shopify_product_map.json` (8,353 SKUs) |
| Price locks (MAP) | `price_locks.json` (15 Titan vacuums) |
| Sync change log | `sync_log.json` |
| Import progress | `bulk_import_progress.json` (6,965 products imported) |

---

## 0. Pricing & Sync Architecture (read this first)

> **Paper trail for the 2026-06-16/17 repricing overhaul.** If a price looks
> wrong, or the sync is failing, start here. Designs:
> `docs/superpowers/specs/2026-06-16-competitive-reprice-all-skus-design.md` and
> `docs/superpowers/specs/2026-06-17-decouple-competitive-from-login-design.md`.
> Lessons: `tasks/lessons.md`.

### The incident that drove this
A Hoover harness (`440013719`) sold 5× at **$54.99** on a **$52.65** dealer cost
(~$0.46/unit). Three compounding faults:
1. **Stale competitor data** — the weekly scrape froze at 2026-03-18: the
   2026-04-29 security commit untracked `competitors.json` (moved to the
   `COMPETITORS_JSON` secret), but the secret wasn't created until 2026-06-11.
   For ~6 weeks the job ran, found no competitor list, skipped the scrape, and
   still reported success because `calculate … | tee` masks the exit code.
2. **Zero-profit floor** — undercut prices floored at `break_even` (≈cost), so
   beating a (stale, aftermarket) competitor by $1 could land $2 over cost.
3. **No push path for ungated SKUs** — competitive prices only reached Shopify
   via the sync's `bulk_import_progress` gate; `440013719` wasn't in it.

### How pricing works now
Two GitHub Actions:
- **Weekly scrape** (`scrape-competitor-prices.yml`, Sun 2am UTC) → refreshes
  `competitor_prices.json`. Pulls each competitor's **full Shopify catalog** via
  `/products.json` (`?page=N`, 250/page) and matches SKUs locally. (Replaced
  per-SKU `/search/suggest.json` which was ~160k requests and got rate-limited;
  catalog pull is a few hundred requests, ~2 min for all 12, no throttling.)
- **12h sync** (`sync-stock-prices.yml`, 6am/6pm UTC) → applies prices. Three
  passes, in `run_sync`:
  1. `run_reprice_targets` — SEBO/Miele/Lindhaus dual-source parts (§2b). No browser.
  2. **Steel City main loop** — logs in (see §1, Cloudflare-flaky), checks
     stock + cost, prices in-gate SKUs via `get_best_price` (competitor-aware:
     walk + 20% floor, else markup). Records `main_loop_skus`.
  3. `run_competitive_reprice` — the **whole catalog** (`product_names ∪
     desco_products`), undercut walk, **skips `main_loop_skus`** to avoid
     double-pricing (§2c). No browser.

**Login is decoupled from competitive pricing (2026-06-17):** the Steel City
browser login happens *inside* `run_sync`, non-fatally. If Cloudflare blocks it,
only the main loop is skipped — `run_reprice_targets` + `run_competitive_reprice`
still run, AND `main_loop_skus` is empty so the competitive pass covers in-gate
SKUs too (fallback). **Competitive undercut runs every 12h regardless of
Cloudflare; Steel City cost/stock runs when login works.**

### The pricing rule (one place: `competitive_target()` + `get_best_price()`)
- **Walk** competitors low→high; undercut the cheapest one we can beat by $1.
- **Margin gate = 20% gross** (`price ≥ cost / 0.80`). Skip competitors too cheap
  to beat at that floor; if none beatable → tiered markup.
- **$6.99 store floor** = a separate final clamp (`max(price, 6.99)`), NOT a
  margin requirement.
- `--up-only` (one-time / correction): never lower a price this run.
See §3 for the markup tiers (the fallback).

### Known gaps / fragilities (2026-06-17)
- **Steel City login is Cloudflare-flaky on CI (~50/50)** — datacenter IPs hit
  Turnstile intermittently. Works from a residential IP. Not a hard break;
  competitive pricing no longer depends on it. `SC_PROXY` secret support exists
  (dormant) for a residential proxy if reliability is needed.
- **Riccar / Simplicity / CleanMax (~398 parts) are priced by nothing** — they're
  in `dual_source_brands.json` but not `reprice_brands.json`, and
  `build_reprice_targets.py`'s machine-MAP patterns don't cover their families
  (adding them blindly risks repricing their machines below MAP). Needs a human
  to confirm which models are MAP-protected + their title patterns.
- **Ungated Steel City SKUs don't get daily cost refresh** — the main loop only
  covers `our_product_ids` (imported products). Ungated ones (e.g. `440013719`)
  are competitively priced by `run_competitive_reprice` off their *stored* cost,
  which isn't refreshed by the Steel City API.

---

## 1. Automated Stock & Price Sync

### What It Does
Every 12 hours (via GitHub Actions), the sync script:
- Logs into Steel City (mid-run, non-fatal — see §0), checks stock + dealer cost
- **Out of stock?** Variant set unbuyable (qty 0, deny) but kept active for SEO
- **Back in stock?** Inventory restored, product active
- **Cost changed?** Recalculates retail via `get_best_price` (competitor undercut
  walk + 20% margin floor, else markup tiers) and updates Shopify
- **Price-locked SKU?** Never touches the price (MAP)
- **Competitive undercut** runs every 12h for the whole catalog regardless of
  whether the Steel City login succeeds (see §0, §2c)

### GitHub Actions Setup
The workflow file is at `.github/workflows/sync-stock-prices.yml`.

**Secrets you need to configure in GitHub → Settings → Secrets:**

| Secret | Value |
|---|---|
| `SHOPIFY_STORE` | `1bb2a2-2.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | (from `.env` file) |
| `EMAIL_USERNAME` | Your Gmail address |
| `EMAIL_PASSWORD` | Gmail app password (not your regular password) |
| `EMAIL_TO` | Where to send sync reports |
| `SC_ACCOUNT` | Steel City account number |
| `SC_USER` | Steel City username |
| `SC_PASSWORD` | Steel City password |
| `SC_PROXY` | *(optional)* residential proxy for the Steel City login, `http://user:pass@host:port` — only needed to fix the Cloudflare flakiness (§0). Dormant if unset. |
| `COMPETITORS_JSON` | contents of local `competitors.json` (competitor domains; kept out of the public repo) |
| `REPRICE_BRANDS` | contents of local `reprice_brands.json` (dual-source brands that get competitor repricing) |

**Schedule:** Runs at 6am and 6pm UTC. Also has a manual trigger button in GitHub Actions.

### Running Locally
```bash
cd "/Users/jamesfeeney98/Desktop/Projects/Steel Stealer"
source .venv/bin/activate
export $(cat .env | xargs)

# Preview what would change (safe, no Shopify changes)
python3 sync_stock_prices.py --dry-run

# Actually apply changes
python3 sync_stock_prices.py
```

The sync is **resumable** — if it crashes mid-run, just re-run and it picks up where it left off.

---

## 2. MAP Pricing (Price Locks)

### What Are Price Locks?
Some products have Minimum Advertised Price (MAP) requirements from the manufacturer. The sync must never auto-update these prices.

### Currently Locked

**15 Titan vacuum machines** (manually verified MAP, 2026-03-19) plus **32 SEBO complete machines** auto-locked by `build_reprice_targets.py` at their store price (AIRBELT / AUTOMATIC X / FELIX / DART / MECHANICAL / ESSENTIAL G / DUO Brush Machine / DISCO Floor Polisher). SEBO machines are MAP-protected; their parts and accessories are NOT locked — they follow competitor repricing (see §2b).

Titan machines:

| SKU | Model | MAP Price |
|---|---|---|
| T1400 | Compact Canister | $169.00 |
| T3200 | HEPA Upright | $299.00 |
| T3600 | Upright | $349.00 |
| T750 | Backpack | $399.00 |
| TC6000.2 | Commercial Upright | $429.00 |
| T8000 | Bagless Canister | $449.00 |
| T9400 | Canister | $449.00 |
| T4000.2 | Heavy Duty Upright | $499.00 |
| T9500 | HEPA Canister | $599.00 |
| T500 | Cord-Free Upright | $699.00 |
| TCS-4792 | Central Vacuum | $799.00 |
| TCS-5525 | Central Vacuum | $799.00 |
| TCS-8575 | Central Vacuum | $995.00 |
| TCS-7702 | Central Vacuum | $999.00 |
| TCS-9902 | Central Vacuum | $1,199.00 |

### How to Add a New Price Lock
1. Set the correct price manually in Shopify admin
2. Add the SKU to `price_locks.json`:
```json
{
  "NEW-SKU-HERE": "$199.99"
}
```
The value is just for your reference — the sync reads the key (SKU) to know what to skip.

### How to Remove a Price Lock
Delete the SKU line from `price_locks.json`. The sync will start managing that SKU's price again. Note: SEBO machine locks are re-added automatically by `build_reprice_targets.py` (weekly in CI) — to permanently unlock a machine you'd have to change the `MACHINE_TITLE` pattern in that script.

---

## 2b. Competitor Repricing for Dual-Source Brands (SEBO)

Dual-source brands (SEBO etc., see `dual_source_brands.json`) are skipped by the stock sync entirely — their inventory is never tracked against Steel City. But their **parts/accessories/attachments still follow competitor pricing**, via a separate price-only pass:

- **`reprice_brands.json`** (`["SEBO"]`) — which dual-source brands get competitor repricing. In CI this comes from the `REPRICE_BRANDS` secret.
- **`build_reprice_targets.py`** — pulls the brand's full catalog from Shopify by vendor, auto-locks complete machines into `price_locks.json` (MAP), writes everything else to `reprice_targets.json`. Runs weekly in CI; run locally after SEBO catalog changes.
- **`reprice_targets.json`** — SEBO/Miele/Lindhaus SKUs. The 12h sync reprices them from `competitor_prices.json`: **walk** competitors low→high, undercut the cheapest beatable by $1 (above the floor). **Strictly competitor-driven** — no competitor data means no change (the markup tiers never apply here). *(The old "tiered average undercut" fallback was removed 2026-06-17; all passes now share `competitive_target()`'s walk.)*
- **Price floor**: `break_even(dealer_cost)` for the ~282 SKUs Steel City carries; for the rest, **70% of `ref_price`** (the store price when the SKU was first targeted — preserved across rebuilds, so repeated undercutting can't ratchet prices to zero). Steel City's cost is an over-estimate of the true direct dealer cost, so this floor is conservative — it can only skip an undercut, never price below cost.
- **Never touches stock, status, inventory tracking, or cost-per-item** on these products. The Shopify cost field holds the brand's direct dealer cost (what the store actually pays); Steel City's marked-up reseller cost must never overwrite it. Decision 2026-06-12: SEBO costs are maintained manually in Shopify admin — revisit if a SEBO dealer price feed (portal export / price list) becomes available, which would also enable a tighter price floor than Steel City's inflated cost.

Run just this pass (fast, no Steel City browser):
```bash
python3 sync_stock_prices.py --reprice-only --dry-run   # preview (SEBO/Miele/Lindhaus only)
python3 sync_stock_prices.py --reprice-only             # apply
```

## 2c. Whole-Catalog Competitor Reprice (`run_competitive_reprice`)

Added 2026-06-17. Every sellable part/accessory — **both distributors** — gets
competitively priced, not just imported/in-gate SKUs.

- **Candidate set:** `product_names.json ∪ desco_products.json` with a Shopify
  variant + dealer cost + validated competitor data; minus `price_locks` and
  dual-source brands (those go through §2b).
- **Pricing:** the walk + 20% margin floor + $6.99 store floor (see §0/§3).
- **Price-only** — never touches stock/cost-per-item (`cost=None` on the PUT).
  Resumable (`competitive_repriced_skus` progress key).
- **Dedup:** in a full sync it skips `main_loop_skus` (the Steel City main loop
  already priced those off fresh cost) → no double-write. When the login fails,
  `main_loop_skus` is empty so this pass covers in-gate SKUs too (fallback).
- Old price for the up-only check: `product_names` retail, or a live Shopify GET
  for Desco-only SKUs (no local retail).

Run just this pass (no browser; the one-time stale-data correction used it):
```bash
python3 sync_stock_prices.py --competitive-reprice --up-only --dry-run  # preview, raises only
python3 sync_stock_prices.py --competitive-reprice --up-only            # apply
python3 sync_stock_prices.py --competitive-reprice                      # ongoing, both directions
```

> `calculate_competitive_prices.py` is **report-only** (writes
> `pricing_decisions.json` for the weekly email). It no longer writes
> `product_names`/Shopify — the sync owns that.

The weekly scrape workflow (`scrape-competitor-prices.yml`, Sun 2am UTC) now decrypts/re-encrypts the data bundle like the sync does, so competitor data actually refreshes in CI. It needs the `COMPETITORS_JSON` and `REPRICE_BRANDS` secrets set once (contents of the local `competitors.json` / `reprice_brands.json`).

**A full sweep spans ~2 weekly runs.** GitHub hard-kills jobs at 6h and the full sweep (8,743 SKUs × 12 domains) takes ~9h, so the scrape runs with `--budget-minutes 300` and checkpoints `competitor_price_progress.json` into the data bundle. Each run resumes where the last stopped; when a sweep completes, the next run starts a fresh cycle. Mid-cycle, `competitor_prices.json` covers only the domains scraped so far in that cycle — fresh beats complete-but-stale.

---

## 3. Pricing Tiers (the markup *fallback*)

**Primary pricing is competitor-driven** (the walk + 20% margin floor — see §0).
These markup tiers are the **fallback**, used only when a SKU has no validated
competitor data, or when no competitor can be beaten while holding 20% gross
margin. Every markup tier clears 20% (lowest is 1.4× = 28.6% gross), so the
fallback never violates the floor.

Markup from dealer cost:

| Dealer Cost | Markup | Example: Cost → Retail |
|---|---|---|
| $0 – $1 | 8.0x | $0.50 → $6.99 |
| $1 – $3 | 4.5x | $2.00 → $8.99 |
| $3 – $7 | 3.2x | $5.00 → $15.99 |
| $7 – $15 | 2.5x | $10.00 → $24.99 |
| $15 – $30 | 2.2x | $20.00 → $43.99 |
| $30 – $60 | 1.9x | $45.00 → $84.99 |
| $60 – $120 | 1.7x | $80.00 → $135.99 |
| $120 – $300 | 1.5x | $200.00 → $299.99 |
| $300+ | 1.4x | $500.00 → $699.99 |

- All prices end in **.99** (charm pricing)
- **$6.99** store display floor — nothing listed below it (a final clamp, NOT a margin rule)
- **20% gross margin gate** (`cost / 0.80`) — the floor that decides whether a competitor undercut is allowed; below it, fall back to these markup tiers
- Average margin: ~64.5%

---

## 4. Common Tasks

### "I need to add new products"
1. Add them to Steel City's system first
2. Run the full discovery pipeline: `python3 full_discovery.py`
3. Generate a new CSV: `python3 generate_shopify_csv.py`
4. Import via: `python3 shopify_bulk_import.py`
5. Rebuild the Shopify map: `python3 build_shopify_map.py`

### "I need to rebuild the Shopify SKU map"
Run this after adding new products to Shopify:
```bash
python3 build_shopify_map.py
```

### "A product is showing as draft but it's actually in stock"
The sync may have drafted it because Steel City's API showed it as out of stock. Either:
- Wait for the next sync (if it's back in stock, it'll auto-reactivate)
- Manually set it to "active" in Shopify admin

### "I want to change a product's price manually"
1. Change it in Shopify admin
2. If you want the sync to leave it alone permanently, add it to `price_locks.json`
3. If you don't lock it, the sync will only overwrite it if the dealer cost increases

### "I want to check what the last sync did"
```bash
python3 -c "
import json
with open('sync_log.json') as f:
    log = json.load(f)
entry = log[-1]
print(f'Time: {entry[\"timestamp\"]}')
print(f'Checked: {entry[\"total_checked\"]}')
print(f'Drafted: {len(entry[\"drafted\"])} | Activated: {len(entry[\"activated\"])} | Price updates: {len(entry[\"price_updated\"])} | Errors: {len(entry[\"errors\"])}')
if entry['drafted']: print(f'Drafted SKUs: {entry[\"drafted\"]}')
if entry['activated']: print(f'Activated SKUs: {entry[\"activated\"]}')
if entry['price_updated']:
    for p in entry['price_updated']:
        print(f'  {p[\"sku\"]}: {p[\"old_cost\"]} -> {p[\"new_cost\"]} (retail {p[\"old_retail\"]} -> {p[\"new_retail\"]})')
"
```

### "The sync is erroring on some SKUs"
10 SKUs with special characters (`+`, `&`) in their names can't be resolved by Steel City's API. These are logged as errors but cause no harm. The affected products just won't have their stock/price checked.

---

## 5. Safety Guards

The sync has several protections built in:

1. **Pre-existing products are never touched** — Only products we imported (tracked in `bulk_import_progress.json`) are synced. The ~1,399 products that existed in Shopify before our import are completely ignored.

2. **Prices only go up, never down** — If Steel City lowers a cost, we keep our higher price.

3. **Price locks** — MAP products in `price_locks.json` are never price-updated.

4. **Dry run mode** — Always test with `--dry-run` first if you're unsure.

5. **Resumable** — Crashes don't lose progress. Re-run to continue.

6. **Full audit trail** — Every sync run is logged to `sync_log.json`.

---

## 6. File Map

| File | What It Is |
|---|---|
| `sync_stock_prices.py` | Main sync script (Steel City → Shopify) |
| `build_shopify_map.py` | Builds SKU → Shopify ID mapping |
| `shopify_bulk_import.py` | Bulk imports products from CSV to Shopify |
| `generate_shopify_csv.py` | Generates Shopify import CSV from product data |
| `product_names.json` | Master product database (8,081 products) |
| `shopify_product_map.json` | SKU → {product_id, variant_id} mapping |
| `price_locks.json` | MAP-priced SKUs that sync won't touch |
| `sync_log.json` | History of all sync runs |
| `bulk_import_progress.json` | Tracks which products were imported to Shopify |
| `.env` | Shopify API credentials (never commit this) |
| `.github/workflows/sync-stock-prices.yml` | GitHub Actions cron job |
| `output/shopify_import.csv` | The CSV that was imported (6,965 products) |
