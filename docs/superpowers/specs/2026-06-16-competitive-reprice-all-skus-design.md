# Competitive Reprice for All Catalog SKUs — Design

**Date:** 2026-06-16
**Status:** Approved (design)
**Author:** James + Claude

## Problem

A customer ordered 5× Hoover harness `440013719` priced at **$54.99** against a
dealer cost of **$52.65** — ~$0.46 net per unit. Investigation found three
compounding faults:

1. **Stale competitor data.** The weekly scrape froze at **2026-03-18**. The
   `2026-04-29` security commit (`4ba4145`) untracked `competitors.json` from the
   public repo and moved it to a secret, but the `COMPETITORS_JSON` GitHub secret
   was not created until **2026-06-11**. For ~6 weeks the job ran, found no
   competitor list, skipped the scrape, and still reported success because the
   calculate step used `python … | tee`, whose pipeline exit code is always
   `tee`'s `0`. False-green every Sunday. (CI `| tee` masking fixed `set -o
   pipefail`; secret now set.)

2. **Zero-profit floor.** `get_best_price()` / `competitive_target()` floored
   undercut prices at `break_even = (cost + $0.30) / 0.971` — essentially cost.
   Beating the lowest competitor by $1 could legally land $2 over cost.

3. **No push path for ungated SKUs.** Competitive prices computed by
   `calculate_competitive_prices.py` only land in `product_names.json`. The only
   code that writes prices to Shopify is `sync_stock_prices.py`, gated to
   `bulk_import_progress` + `reprice_targets`. `440013719` is in neither gate, so
   its store price never moved off the original import value.

   Additionally, **4,408 Desco net-new products** (in `desco_products.json`, not
   `product_names.json`) are invisible to the entire pricing pipeline — never
   scraped, never repriced.

## Goal

Every sellable item — parts, attachments, filters, wands, hoses, bags, general
accessories — across **both distributors** is priced to **undercut the lowest
competitor by $1**, provided the result holds a **minimum 20% gross margin** and
never lists below **$6.99**. Otherwise fall back (average undercut → tiered
markup). The first correction run is **up-only** to avoid starting a price war
while fixing stale data; ongoing 12h automation reverts to the normal
both-directions competitive strategy.

## Pricing Rule (cost-based competitive pricing)

Two **independent** constraints:
- **Margin gate** `MARGIN_FLOOR = cost / 0.80` — the 20% minimum gross margin that
  decides whether an undercut is allowed. This is the *only* gate on the undercut
  decision.
- **Store display floor** `STORE_FLOOR = 6.99` — no item is ever *listed* below
  this. Applied as a final clamp to the chosen price; **not** part of the margin
  gate.

For a SKU with dealer `cost` and validated competitor prices:

```
margin_floor = cost / 0.80                # 20% gross margin — the undercut GATE

# Walk competitors low → high. Drop any we cannot beat while holding 20% margin.
# Undercut the FIRST (cheapest) competitor we CAN beat by $1.
for comp in sorted(valid_prices):         # ascending
    raw = charm(comp - 1.00)              # pre-clamp undercut price
    if raw >= margin_floor:               # gate on MARGIN only (not $6.99)
        price = raw
        break
else:
    price = tiered_markup(cost)           # no competitor beatable at 20% → markup

price = max(price, 6.99)                  # store display floor — final clamp only
```

Worked example — `comps = [55, 70, 90]`, `cost = 60`, `margin_floor = 75`:
- $55 → `charm(54)=53.99 < 75` → **excluded** (can't beat at 20% margin)
- $70 → `charm(69)=68.99 < 75` → **excluded**
- $90 → `charm(89)=88.99 ≥ 75` → **use $88.99** (undercut the $90 competitor)

Cheap-item example — `comp = 5.00`, `cost = 3.00`, `margin_floor = 3.75`:
- $5 → `charm(4)=3.99 ≥ 3.75` → undercut allowed (margin 24.8%) → `raw = 3.99`
- final clamp → **listed $6.99** (store floor; never below it even though the
  undercut math cleared margin at $3.99).

- **Gross margin** = `(price - cost) / price`. `price >= cost / (1 - 0.20)` is the
  algebraic equivalent of the 20% gate. Shopify fees are not included in the
  margin definition (separate from the legacy `break_even`). The $6.99 store
  floor is orthogonal — a display rule, never a margin requirement.
- **Beat by a flat $1.** No tiered average undercut — that fallback is removed.
  We undercut the cheapest competitor we can profitably beat; competitors priced
  below our floor are simply skipped, not averaged in.
- `charm()` = existing `.99` charm-pricing rounding. Unchanged.
- **Up-only flag:** when `--up-only`, apply a computed price only if it exceeds
  the SKU's current retail. Never lowers a price this run.

This floor replaces `break_even` in `get_best_price()` (shared by the gated Steel
City loop and the new pass). `run_reprice_targets()` (SEBO / dual-source) keeps
its own floor logic — its Steel City costs are inflated reseller estimates, not
true dealer costs, so a margin floor off them would be wrong.

## Components

### 1. `scrape_competitor_prices.py` — cover Desco
After building `skus_with_products` from `product_names.json` (and merging
`reprice_targets`), also merge SKUs from `desco_products.json` not already seen,
using `dealer_cost` as the ref price for the sanity band. Same pattern as the
existing reprice-targets merge. Result: the 4,408 Desco net-new SKUs get scraped.

### 2. `sync_stock_prices.py` — new `run_competitive_reprice()` pass
- Mirrors `run_reprice_targets()`: price-only, resumable (own progress key),
  runs after `run_reprice_targets`, before the Steel City stock loop.
- **Candidate set:** union of `product_names.json` + `desco_products.json`, keep a
  SKU when **all** hold:
  - has `variant_id` in `shopify_product_map.json`
  - has a dealer cost (`product_names.price` or `desco.dealer_cost`)
  - has validated competitor data (`filter_competitor_prices` passes — includes
    the aftermarket-title filter that rejects "compare to / replacement part for"
    clone listings)
  - not in `price_locks.json` (MAP)
  - not dual-source SKU or brand (owned by `run_reprice_targets`)
- Genuine pre-existing store products are excluded automatically: they are not in
  `product_names`/`desco_products` and have no dealer cost. `440013719` qualifies
  because it is in `product_names`.
- This replaces the `our_product_ids` listing-ownership gate (about who *created*
  the listing — irrelevant to a price update) with a **data-ownership +
  has-cost + has-competitor** gate.
- Applies the Pricing Rule above. Honors `--up-only`. Writes back to
  `product_names.json` retail (and leaves cost-per-item alone for Desco, whose
  Shopify cost is the direct dealer cost, mirroring the reprice-targets rule).

### 3. `competitive_target()` selection + `get_best_price()` floor
- `competitive_target(valid_prices, floor)` becomes the **walk** (sorted low→high,
  drop unbeatable, undercut first beatable by $1, gating each candidate on the
  `floor` arg pre-clamp). Returns `None` if none clear the floor. Shared by
  `get_best_price()` **and** `run_reprice_targets()`; the `floor` argument stays
  per-caller, so SEBO keeps its own floor while adopting the same selection
  logic. The tiered-average-undercut branch is deleted.
- `get_best_price()` passes `floor = cost / 0.80` (20% margin gate), replacing
  `break_even(cost)`. The `$6.99` store floor is applied separately as a final
  `max(price, 6.99)` clamp on whatever price is chosen (undercut or markup) —
  it is **not** folded into the margin gate. Affects the gated Steel City loop
  and the new pass identically.
- Docs to update on landing: `OPERATIONS.md` §2b (reprice describes avg-undercut
  fallback) and the `feedback_pricing_strategy` memory (both predate the walk).

### 4. CLI
- `--up-only` — global this-run guard: never lower a price.
- New pass runs by default in the normal sync (so 12h automation closes the loop
  permanently); the first correction run is invoked manually with
  `--up-only --dry-run` first.

## Data Flow

```
scrape_competitor_prices.py  (product_names ∪ desco_products ∪ reprice_targets)
        └─► competitor_prices.json
                └─► sync_stock_prices.py
                        ├─ run_reprice_targets()      (SEBO, own floor)    ─► Shopify
                        ├─ run_competitive_reprice()  (all catalog, 20% floor, up-only) ─► Shopify
                        └─ Steel City stock loop      (gated, get_best_price w/ new floor) ─► Shopify
```

## Safety / Non-Goals

- **Price-only.** The new pass never touches stock, status, NLA, inventory
  tracking, or cost-per-item.
- **Competitor-driven.** No validated competitor data → no change (markup
  fallback only fires for SKUs that *have* competitor data but can't be
  undercut profitably; SKUs with no data at all are left untouched by the pass).
- **Resumable** via a dedicated progress key; safe to re-run.
- **Dry-run first.** Report count of SKUs moving and magnitude (flag large
  jumps) before any live write.
- Does **not** migrate Desco into `product_names.json` (kept as separate source,
  read alongside). Does **not** change `run_reprice_targets` floor logic.

## Rollout Sequence

1. Land code changes (scraper Desco merge, new pass, floor change, `--up-only`).
2. Fresh full `scrape_competitor_prices.py` (now covers Desco).
3. `sync_stock_prices.py --competitive-reprice --up-only --dry-run` → review
   report: # SKUs moving, distribution of increases, top jumps.
4. James reviews the dry-run diff.
5. Apply live; spot-check a sample against live competitor pages.
6. Ongoing: the pass runs both-directions inside the 12h sync.

## Open Questions

None — design approved 2026-06-16.
