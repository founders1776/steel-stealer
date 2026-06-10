#!/usr/bin/env python3
"""
One-off cleanup: dual-source-brand products that a prior sync flipped to
inventory_management=shopify + inventory_policy=deny (or drafted), leaving them
unbuyable on the storefront. Restore them to "always buyable, not stock-tracked"
so they match the dual-source intent (these brands are always available direct).

Targets are sourced from the LIVE Shopify store by vendor — NOT from
product_names.json. The damaged products (e.g. the Miele/Riccar/Sebo machines)
are pre-existing store products that never lived in product_names, which is why
the old product_names-gated version of this script never reached them.

Usage:
  python3 restore_dual_source.py --dry-run   # Report what would change
  python3 restore_dual_source.py             # Apply
"""

import argparse
import json
import time

import requests

from sync_stock_prices import (
    SHOPIFY_MAP_FILE,
    DUAL_SOURCE_FILE, DUAL_SOURCE_BRANDS_FILE,
    shopify_get, shopify_put, shopify_headers, shopify_api_url,
)


def load_dual_source():
    skus = set()
    brands = set()
    if DUAL_SOURCE_FILE.exists():
        skus = set(json.load(open(DUAL_SOURCE_FILE)))
    if DUAL_SOURCE_BRANDS_FILE.exists():
        brands = {b.strip() for b in json.load(open(DUAL_SOURCE_BRANDS_FILE)) if b and b.strip()}
    return skus, brands


def fetch_products_by_vendor(vendor):
    """Paginate all products for a vendor (Shopify vendor match is case-insensitive)."""
    out = []
    url = shopify_api_url(
        f"products.json?vendor={requests.utils.quote(vendor)}&limit=250"
        "&fields=id,title,vendor,status,variants"
    )
    while url:
        for attempt in range(3):
            resp = requests.get(url, headers=shopify_headers(), timeout=30)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)))
                continue
            resp.raise_for_status()
            break
        out.extend(resp.json().get("products", []))
        nxt = resp.links.get("next")
        url = nxt["url"] if nxt else None
        time.sleep(0.3)
    return out


def collect_targets(brands, dual_skus):
    """Return {product_id: product} for every product matching a dual-source brand
    (by live vendor) or a dual-source SKU (via the Shopify map)."""
    by_id = {}

    for brand in sorted(brands):
        prods = fetch_products_by_vendor(brand)
        kept = 0
        for p in prods:
            # Case-insensitive vendor guard (Shopify search can be fuzzy)
            if (p.get("vendor") or "").strip().upper() == brand.strip().upper():
                by_id[p["id"]] = p
                kept += 1
        print(f"  vendor={brand:12} fetched={len(prods):4} matched={kept}")

    # SKU-level dual-source entries (mapped via the Shopify product map)
    if dual_skus:
        shopify_map = json.load(open(SHOPIFY_MAP_FILE))
        sku_pids = {shopify_map[s]["product_id"] for s in dual_skus if s in shopify_map}
        added = 0
        for pid in sku_pids:
            if pid in by_id:
                continue
            data, _ = shopify_get(f"products/{pid}.json")
            p = (data or {}).get("product")
            if p:
                by_id[p["id"]] = p
                added += 1
            time.sleep(0.1)
        print(f"  dual_source_skus: +{added} products (not already covered by vendor)")

    return by_id


def needs_restore(product):
    """A product needs restoration if it is drafted, or any variant is
    stock-tracked with a deny policy (the prior-sync OOS signature)."""
    if product.get("status") == "draft":
        return "draft"
    for v in product.get("variants", []):
        if v.get("inventory_management") == "shopify" and v.get("inventory_policy") == "deny":
            return "deny"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dual_skus, brands = load_dual_source()
    print(f"Dual-source brands: {sorted(brands)}")
    print(f"Dual-source SKUs:   {len(dual_skus)}")
    print("Collecting targets from live Shopify store...")

    targets = collect_targets(brands, dual_skus)
    print(f"\nTotal dual-source products in store: {len(targets)}")

    to_fix = []
    for pid, p in targets.items():
        why = needs_restore(p)
        if why:
            to_fix.append((pid, p, why))

    from collections import Counter
    by_reason = Counter(why for _, _, why in to_fix)
    by_vendor = Counter((p.get("vendor") or "?") for _, p, _ in to_fix)
    print(f"Need restoration: {len(to_fix)}  (by reason: {dict(by_reason)})")
    print(f"By vendor: {dict(by_vendor)}")
    if to_fix[:12]:
        print("\nSample:")
        for pid, p, why in to_fix[:12]:
            print(f"  [{why:5}] {p.get('status'):7} {pid}  {p.get('title','')[:50]}")

    if args.dry_run:
        print("\nDRY RUN — no changes applied. Re-run without --dry-run to apply.")
        return

    if not to_fix:
        print("\nNothing to do.")
        return

    print(f"\nApplying restoration to {len(to_fix)} products...")
    fixed = failed = 0
    for pid, p, why in to_fix:
        try:
            for v in p.get("variants", []):
                if v.get("inventory_management") == "shopify" and v.get("inventory_policy") == "deny":
                    # inventory_management = null → not stock-tracked, always buyable
                    shopify_put(f"variants/{v['id']}.json", {"variant": {
                        "id": int(v["id"]),
                        "inventory_management": None,
                        "inventory_policy": "continue",
                    }})
                    time.sleep(0.55)
            shopify_put(f"products/{pid}.json", {"product": {
                "id": int(pid),
                "status": "active",
            }})
            time.sleep(0.55)
            fixed += 1
            if fixed % 25 == 0:
                print(f"  restored {fixed}/{len(to_fix)}")
        except Exception as e:
            failed += 1
            print(f"  FAILED {pid}: {e}")

    print(f"\nDone. Restored: {fixed}, Failed: {failed}")


if __name__ == "__main__":
    main()
