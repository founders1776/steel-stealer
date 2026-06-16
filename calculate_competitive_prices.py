#!/usr/bin/env python3
"""
Phase 4c: Calculate competitive prices based on competitor data.

Reads competitor prices, applies undercut logic, enforces break-even floor,
and outputs pricing decisions. Deliberately separate from scraping so it can
be re-run instantly with different parameters.

Usage:
  python3 calculate_competitive_prices.py              # Full run
  python3 calculate_competitive_prices.py --dry-run    # Preview without updating product_names.json
"""

import argparse
import json
import logging
import re
import statistics
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "product_names.json"
COMPETITOR_PRICES_FILE = BASE_DIR / "competitor_prices.json"
PRICE_LOCKS_FILE = BASE_DIR / "price_locks.json"
OUTPUT_FILE = BASE_DIR / "pricing_decisions.json"

# ── Markup tiers (fallback when no competitor data) ─────────────────────────

MARKUP_TIERS = [
    (1.00,    8.0),
    (3.00,    4.5),
    (7.00,    3.2),
    (15.00,   2.5),
    (30.00,   2.2),
    (60.00,   1.9),
    (120.00,  1.7),
    (300.00,  1.5),
    (float("inf"), 1.4),
]

MIN_PRICE = 6.99      # store display floor — nothing listed below this
MIN_MARGIN = 0.20     # minimum gross margin: (price - cost) / price >= 20%

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def parse_price(price_str):
    if not price_str:
        return None
    match = re.search(r'[\d]+\.?\d*', str(price_str))
    return float(match.group()) if match else None


def get_markup(cost):
    for max_cost, multiplier in MARKUP_TIERS:
        if cost <= max_cost:
            return multiplier
    return MARKUP_TIERS[-1][1]


def charm_price(raw_price):
    dollar = int(raw_price)
    cents = raw_price - dollar
    if cents < 0.50:
        return float(dollar - 1) + 0.99 if dollar > 0 else 0.99
    else:
        return float(dollar) + 0.99


def calculate_markup_price(cost):
    """Fallback tiered markup pricing."""
    markup = get_markup(cost)
    raw = cost * markup
    retail = charm_price(raw)
    return max(retail, MIN_PRICE)


def margin_floor(cost):
    """Lowest price holding MIN_MARGIN gross margin: price >= cost / (1 - MIN_MARGIN).
    The undercut gate. The $6.99 store floor is a separate final clamp."""
    return cost / (1 - MIN_MARGIN)


def filter_competitor_prices(sku, ref_price, comp_data):
    """Validated competitor prices: within 0.5x-5x of ref_price and where the SKU
    appears in the matched title/URL. Mirrors sync_stock_prices.filter_competitor_prices
    so both pipelines reject accessory/clone mismatches identically."""
    sku_lower = sku.lower()
    sku_norm = re.sub(r'[\-\s\.]', '', sku).lower()
    valid_prices = []
    for _domain, cdata in comp_data.get("competitors", {}).items():
        if not cdata or not cdata.get("price"):
            continue
        ratio = cdata["price"] / ref_price if ref_price and ref_price > 0 else 1
        if not (0.50 <= ratio <= 5.0):
            continue
        comp_title = (cdata.get("title") or "").lower()
        comp_url = (cdata.get("url") or "").split("?")[0].lower()
        comp_text = comp_title + " " + comp_url
        comp_text_norm = re.sub(r'[\-\s\.]', '', comp_text)
        if sku_lower not in comp_text and sku_norm not in comp_text_norm:
            continue
        valid_prices.append(cdata["price"])
    return valid_prices


def competitive_target(valid_prices, floor):
    """Walk competitors low→high; undercut the cheapest beatable one by $1 while
    holding `floor`. Returns the pre-clamp undercut price, or None if none clear
    the floor."""
    for comp in sorted(valid_prices):
        raw = charm_price(comp - 1.00)
        if raw >= floor:
            return raw
    return None


def main():
    parser = argparse.ArgumentParser(description="Calculate competitive prices")
    parser.add_argument("--dry-run", action="store_true", help="Don't update product_names.json")
    args = parser.parse_args()

    products = json.loads(PRODUCTS_FILE.read_text())

    if not COMPETITOR_PRICES_FILE.exists():
        log.error(f"{COMPETITOR_PRICES_FILE} not found. Run scrape_competitor_prices.py first.")
        return

    competitor_data = json.loads(COMPETITOR_PRICES_FILE.read_text())
    competitor_prices = competitor_data.get("prices", {})

    price_locks = set()
    if PRICE_LOCKS_FILE.exists():
        locks_data = json.loads(PRICE_LOCKS_FILE.read_text())
        price_locks = {k for k in locks_data.keys() if not k.startswith("_")}

    log.info(f"Products: {len(products)}")
    log.info(f"SKUs with competitor data: {len(competitor_prices)}")
    log.info(f"MAP-locked SKUs: {len(price_locks)}")

    # Process each product
    decisions = {}
    stats = {
        "total": 0,
        "competitor_priced": 0,
        "markup_fallback_no_data": 0,
        "markup_fallback_below_breakeven": 0,
        "map_locked": 0,
    }

    for key, product in products.items():
        sku = product.get("sku", key)
        dealer_cost = parse_price(product.get("price"))
        old_retail = parse_price(product.get("retail_price"))
        stats["total"] += 1

        if not dealer_cost:
            continue

        # Skip MAP-locked products
        if sku in price_locks:
            stats["map_locked"] += 1
            decisions[sku] = {
                "dealer_cost": dealer_cost,
                "final_price": old_retail,
                "method": "map_locked",
            }
            continue

        floor = round(margin_floor(dealer_cost), 2)
        markup_price = calculate_markup_price(dealer_cost)

        # Check for competitor data
        comp_data = competitor_prices.get(sku)
        if not comp_data or comp_data.get("num_competitors", 0) == 0:
            # No competitor data — use tiered markup
            stats["markup_fallback_no_data"] += 1
            final_price = markup_price
            decisions[sku] = {
                "dealer_cost": dealer_cost,
                "margin_floor": floor,
                "final_price": final_price,
                "method": "markup_no_data",
                "old_retail": old_retail,
            }
            if not args.dry_run:
                products[key]["retail_price"] = f"${final_price:.2f}"
            continue

        # Validated competitor prices (rejects accessory/clone mismatches).
        valid_prices = filter_competitor_prices(sku, dealer_cost, comp_data)
        if not valid_prices:
            stats["markup_fallback_no_data"] += 1
            final_price = markup_price
            decisions[sku] = {
                "dealer_cost": dealer_cost,
                "margin_floor": floor,
                "final_price": final_price,
                "method": "markup_no_valid_data",
                "old_retail": old_retail,
            }
            if not args.dry_run:
                products[key]["retail_price"] = f"${final_price:.2f}"
            continue

        # Walk competitors low→high; undercut the cheapest beatable by $1 at the
        # 20% margin floor, else tiered markup. $6.99 store floor clamps after.
        target = competitive_target(valid_prices, margin_floor(dealer_cost))
        if target is not None:
            stats["competitor_priced"] += 1
            final_price = max(target, MIN_PRICE)
            method = "competitor_undercut"
        else:
            stats["markup_fallback_below_breakeven"] += 1
            final_price = markup_price
            method = "markup_below_floor"

        decisions[sku] = {
            "dealer_cost": dealer_cost,
            "margin_floor": floor,
            "competitor_min": round(min(valid_prices), 2),
            "num_competitors": len(valid_prices),
            "final_price": final_price,
            "method": method,
            "old_retail": old_retail,
        }

        if not args.dry_run:
            products[key]["retail_price"] = f"${final_price:.2f}"
            products[key]["competitor_price"] = f"${round(sum(valid_prices) / len(valid_prices), 2):.2f}"

    # Output
    output = {
        "calculated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": stats,
        "decisions": decisions,
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    log.info(f"\nSaved pricing decisions to {OUTPUT_FILE}")

    if not args.dry_run:
        PRODUCTS_FILE.write_text(json.dumps(products, indent=2))
        log.info(f"Updated {PRODUCTS_FILE} with new retail prices")

    # Summary
    log.info(f"\n{'='*50}")
    log.info(f"PRICING SUMMARY")
    log.info(f"{'='*50}")
    log.info(f"Total products:              {stats['total']}")
    log.info(f"Competitor-priced:           {stats['competitor_priced']}")
    log.info(f"Markup (no competitor data): {stats['markup_fallback_no_data']}")
    log.info(f"Markup (below break-even):   {stats['markup_fallback_below_breakeven']}")
    log.info(f"MAP-locked:                  {stats['map_locked']}")

    # Price distribution stats
    competitor_finals = [d["final_price"] for d in decisions.values()
                         if d.get("method") == "competitor" and d.get("final_price")]
    if competitor_finals:
        log.info(f"\nCompetitor-priced products:")
        log.info(f"  Avg price:    ${statistics.mean(competitor_finals):.2f}")
        log.info(f"  Median price: ${statistics.median(competitor_finals):.2f}")

    all_finals = [d["final_price"] for d in decisions.values() if d.get("final_price")]
    if all_finals:
        log.info(f"\nAll products:")
        log.info(f"  Avg price:    ${statistics.mean(all_finals):.2f}")
        log.info(f"  Median price: ${statistics.median(all_finals):.2f}")

    # Margin analysis
    margins = []
    for d in decisions.values():
        if d.get("final_price") and d.get("dealer_cost") and d["dealer_cost"] > 0:
            margin = (d["final_price"] - d["dealer_cost"]) / d["final_price"] * 100
            margins.append(margin)
    if margins:
        log.info(f"  Avg margin:   {statistics.mean(margins):.1f}%")
        log.info(f"  Min margin:   {min(margins):.1f}%")

    # Show price changes
    changes_up = sum(1 for d in decisions.values()
                     if d.get("old_retail") and d.get("final_price") and d["final_price"] > d["old_retail"])
    changes_down = sum(1 for d in decisions.values()
                       if d.get("old_retail") and d.get("final_price") and d["final_price"] < d["old_retail"])
    unchanged = sum(1 for d in decisions.values()
                    if d.get("old_retail") and d.get("final_price") and d["final_price"] == d["old_retail"])
    log.info(f"\nPrice changes vs current:")
    log.info(f"  Increased: {changes_up}")
    log.info(f"  Decreased: {changes_down}")
    log.info(f"  Unchanged: {unchanged}")


if __name__ == "__main__":
    main()
