# Steel Stealer — Operations Guide

Everything you need to know to manage the Shopify store and sync pipeline.

---

## Quick Reference

| What | Where |
|---|---|
| Shopify Admin | `1bb2a2-2.myshopify.com/admin` |
| Shopify credentials | `.env` file |
| Steel City login | Account: REDACTED_ACCT / REDACTED_USER / REDACTED_PASS |
| Product data | `product_names.json` (8,081 products) |
| Shopify SKU map | `shopify_product_map.json` (8,353 SKUs) |
| Price locks (MAP) | `price_locks.json` (15 Titan vacuums) |
| Sync change log | `sync_log.json` |
| Import progress | `bulk_import_progress.json` (6,965 products imported) |

---

## 1. Automated Stock & Price Sync

### What It Does
Every 12 hours (via GitHub Actions), the sync script:
- Logs into Steel City, checks stock status + dealer cost for all 7,549 products
- **Out of stock?** Product gets set to "draft" (hidden from store)
- **Back in stock?** Product gets set to "active" (visible again)
- **Cost went up?** Recalculates retail price using markup tiers, updates Shopify
- **Cost went down?** Does nothing (protects your margins)
- **Price-locked SKU?** Never touches the price, even if cost changes

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

### Currently Locked: 15 Titan Vacuum Machines

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
Delete the SKU line from `price_locks.json`. The sync will start managing that SKU's price again.

---

## 3. Pricing Tiers

All non-MAP products are priced automatically from the dealer cost using these tiers:

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
- Minimum price floor: **$6.99**
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
