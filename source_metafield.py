#!/usr/bin/env python3
"""
source_metafield.py — manage the `custom.source` product metafield (which
distributor a product came from) on the Shopify store, and mirror it locally
in source_map.json.

Values are comma-joined, sorted, unique source tokens: "steel_city", "desco",
or "desco,steel_city" for products both distributors carry.

Steel City source is inferred from our import progress files; Desco source is
inferred from the overlap between desco_products.json and the live store map.
The backfill MERGES with any existing custom.source value so it is idempotent
and never clobbers a source already recorded.

Usage:
  python3 source_metafield.py                    # ensure the metafield definition exists
  python3 source_metafield.py --backfill --dry-run   # report counts, write nothing
  python3 source_metafield.py --backfill             # apply to the live store + write source_map.json
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
API_VERSION = "2024-10"

DESCO_PRODUCTS_FILE = BASE_DIR / "desco_products.json"
SHOPIFY_MAP_FILE = BASE_DIR / "shopify_product_map.json"
BULK_PROGRESS_FILE = BASE_DIR / "bulk_import_progress.json"
MISSING_PROGRESS_FILE = BASE_DIR / "missing_import_progress.json"
SOURCE_MAP_FILE = BASE_DIR / "source_map.json"

GRAPHQL_URL = f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}/graphql.json"
HEADERS = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
           "Content-Type": "application/json"}


def gql(query, variables=None, retries=5):
    for attempt in range(retries):
        resp = requests.post(GRAPHQL_URL, headers=HEADERS,
                             json={"query": query, "variables": variables or {}},
                             timeout=60)
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 2)))
            continue
        if resp.status_code in (500, 502, 503, 504) and attempt < retries - 1:
            time.sleep((attempt + 1) * 3)
            continue
        resp.raise_for_status()
        data = resp.json()
        # GraphQL throttle inside a 200
        if data.get("errors"):
            throttled = any("throttl" in str(e).lower() for e in data["errors"])
            if throttled and attempt < retries - 1:
                time.sleep((attempt + 1) * 2)
                continue
        return data
    return None


def gid(product_id):
    pid = str(product_id)
    if pid.startswith("gid://"):
        return pid
    return f"gid://shopify/Product/{pid}"


# ── Source helpers ────────────────────────────────────────────────────────────

def join_sources(sources):
    return ",".join(sorted({s.strip() for s in sources if s and s.strip()}))


def split_sources(value):
    if not value:
        return set()
    return {s.strip() for s in str(value).split(",") if s.strip()}


# ── Metafield definition ──────────────────────────────────────────────────────

def ensure_definition():
    q = """
    mutation {
      metafieldDefinitionCreate(definition: {
        name: "Source", namespace: "custom", key: "source",
        type: "single_line_text_field", ownerType: PRODUCT
      }) { createdDefinition { id } userErrors { field message code } }
    }"""
    data = gql(q)
    errs = data["data"]["metafieldDefinitionCreate"]["userErrors"]
    if any(e.get("code") == "TAKEN" for e in errs):
        print("custom.source definition: already exists")
    elif errs:
        print(f"custom.source definition: userErrors {errs}")
    else:
        print("custom.source definition: created")


def get_source(product_gid):
    q = """
    query($id: ID!) {
      product(id: $id) { metafield(namespace: "custom", key: "source") { value } }
    }"""
    data = gql(q, {"id": product_gid})
    try:
        mf = data["data"]["product"]["metafield"]
        return mf["value"] if mf else None
    except (KeyError, TypeError):
        return None


def set_source(product_gid, source_str):
    q = """
    mutation($id: ID!, $v: String!) {
      metafieldsSet(metafields: [{
        ownerId: $id, namespace: "custom", key: "source",
        type: "single_line_text_field", value: $v }]) {
        userErrors { field message }
      }
    }"""
    data = gql(q, {"id": product_gid, "v": source_str})
    errs = data["data"]["metafieldsSet"]["userErrors"] if data else [{"message": "no response"}]
    return errs


# ── Source inference from local files ─────────────────────────────────────────

def norm_sku(s):
    return re.sub(r"[^A-Za-z0-9]", "", str(s)).upper()


def steel_city_product_ids():
    """Product ids we imported (Steel City schematic + dealer-sheet imports)."""
    ids = set()
    if BULK_PROGRESS_FILE.exists():
        for v in json.loads(BULK_PROGRESS_FILE.read_text()).values():
            if isinstance(v, dict) and v.get("status") == "created" and v.get("id"):
                ids.add(str(v["id"]))
    if MISSING_PROGRESS_FILE.exists():
        for v in json.loads(MISSING_PROGRESS_FILE.read_text()).get("uploaded", {}).values():
            if isinstance(v, dict) and v.get("id"):
                ids.add(str(v["id"]))
    for prog in BASE_DIR.glob("sheet_imports/*/progress.json"):
        for pid in json.loads(prog.read_text()).get("created", {}).values():
            pid = pid if isinstance(pid, str) else (pid or {}).get("id") if isinstance(pid, dict) else pid
            if pid:
                ids.add(str(pid))
    return ids


def desco_overlap(shopify_map):
    """Map product_id -> store_sku for Desco SKUs that already exist in the store.

    Matches on normalized SKU and O<->0 fuzzing against the store map."""
    desco = json.loads(DESCO_PRODUCTS_FILE.read_text())
    # normalized index of store SKUs
    store_norm = {}        # norm -> (store_sku, product_id)
    store_o0 = {}          # norm-with-O->0 -> (store_sku, product_id)
    for sku, ids in shopify_map.items():
        pid = ids.get("product_id") if isinstance(ids, dict) else None
        if not pid:
            continue
        n = norm_sku(sku)
        store_norm.setdefault(n, (sku, str(pid)))
        store_o0.setdefault(n.replace("O", "0"), (sku, str(pid)))
    overlap = {}           # product_id -> store_sku
    for dsku in desco:
        n = norm_sku(dsku)
        hit = store_norm.get(n) or store_o0.get(n.replace("O", "0"))
        if hit:
            store_sku, pid = hit
            overlap[pid] = store_sku
    return overlap


# ── Backfill ──────────────────────────────────────────────────────────────────

def backfill(dry_run=False):
    if not SHOPIFY_MAP_FILE.exists():
        print("shopify_product_map.json missing — run build_shopify_map.py first.")
        return
    shopify_map = json.loads(SHOPIFY_MAP_FILE.read_text())
    # product_id -> store_sku (reverse map, first sku wins)
    pid_to_sku = {}
    for sku, ids in shopify_map.items():
        pid = ids.get("product_id") if isinstance(ids, dict) else None
        if pid:
            pid_to_sku.setdefault(str(pid), sku)

    sc_ids = steel_city_product_ids()
    desco_ids = desco_overlap(shopify_map)   # product_id -> store_sku

    # union of products to consider
    targets = set(sc_ids) | set(desco_ids)
    print(f"Steel City product ids: {len(sc_ids)}")
    print(f"Desco-overlap product ids: {len(desco_ids)}")
    print(f"Products to tag (union): {len(targets)}")

    # resume state
    state = {}
    if SOURCE_MAP_FILE.exists():
        state = json.loads(SOURCE_MAP_FILE.read_text())
    done = set(state.get("_backfilled", []))

    counts = {"steel_city_only": 0, "desco_only": 0, "multi": 0,
              "skip_already_correct": 0, "written": 0, "errors": 0}
    samples = []
    source_map = {k: v for k, v in state.items() if not k.startswith("_")}

    for i, pid in enumerate(sorted(targets)):
        want = set()
        if pid in sc_ids:
            want.add("steel_city")
        if pid in desco_ids:
            want.add("desco")
        store_sku = desco_ids.get(pid) or pid_to_sku.get(pid, pid)

        # tally by intended composition
        if want == {"steel_city"}:
            counts["steel_city_only"] += 1
        elif want == {"desco"}:
            counts["desco_only"] += 1
        else:
            counts["multi"] += 1
        if len(samples) < 5:
            samples.append((store_sku, join_sources(want)))

        if dry_run:
            continue
        if pid in done:
            continue

        # merge with existing live value
        existing = split_sources(get_source(gid(pid)))
        final = want | existing
        final_str = join_sources(final)
        if existing == final:
            counts["skip_already_correct"] += 1
        else:
            errs = set_source(gid(pid), final_str)
            if errs:
                counts["errors"] += 1
                print(f"  ERROR {store_sku} ({pid}): {errs}")
            else:
                counts["written"] += 1
        source_map[store_sku] = final_str
        done.add(pid)
        time.sleep(0.3)
        if (i + 1) % 200 == 0:
            state = {"_comment": "SKU -> distributor source(s); mirror of custom.source metafield.",
                     "_backfilled": sorted(done)}
            state.update(source_map)
            SOURCE_MAP_FILE.write_text(json.dumps(state, indent=2))
            print(f"  checkpoint {i+1}/{len(targets)} — written {counts['written']}")

    print("\nBackfill summary:")
    print(f"  steel_city only:        {counts['steel_city_only']}")
    print(f"  desco only:             {counts['desco_only']}")
    print(f"  multi (steel_city+desco): {counts['multi']}")
    if not dry_run:
        print(f"  written to Shopify:     {counts['written']}")
        print(f"  already correct (skip): {counts['skip_already_correct']}")
        print(f"  errors:                 {counts['errors']}")
    print("  samples (sku -> source):")
    for sku, src in samples:
        print(f"    {sku} -> {src}")

    if dry_run:
        print("\n[DRY RUN] No Shopify writes, source_map.json not written.")
        return
    state = {"_comment": "SKU -> distributor source(s); mirror of custom.source metafield.",
             "_backfilled": sorted(done)}
    state.update(source_map)
    SOURCE_MAP_FILE.write_text(json.dumps(state, indent=2))
    print(f"\nWrote {SOURCE_MAP_FILE.name} ({len(source_map)} skus).")


def main():
    ap = argparse.ArgumentParser(description="Manage custom.source metafield + source_map.json")
    ap.add_argument("--backfill", action="store_true", help="Backfill source on existing products")
    ap.add_argument("--dry-run", action="store_true", help="Report only; no writes")
    args = ap.parse_args()

    if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
        print("ERROR: set SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN.")
        sys.exit(1)

    ensure_definition()
    if args.backfill:
        backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
