#!/usr/bin/env python3
"""
sheet_import.py — import products from dealer pricing sheets into Shopify.

Pipeline (resumable, per-run directory under sheet_imports/):
  1. parse     — Claude writes manifest.json; this step VALIDATES it
  2. match     — bucket SKUs: new / existing / sku_fixes / ambiguous
  3. research  — Claude agents write content/<sku>.json; this step VALIDATES
  4. images    — download + validate image URLs from content files
  5. create    — create draft products for the `new` bucket   (MUTATES STORE)
  6. update    — smart-update the `existing` bucket           (MUTATES STORE)
  7. register  — price_locks / dual_source / map rebuild / report.md

Imported SKUs are NEVER added to bulk_import_progress.json — that file is
the sync's gate, and staying out of it keeps these products invisible to
the 12h Steel City loop. price_locks.json is the second layer.

Usage:
  python3 sheet_import.py --run-dir sheet_imports/lindhaus_2026-06-12 --step match --dry-run
  python3 sheet_import.py --run-dir sheet_imports/lindhaus_2026-06-12 --step create
  python3 sheet_import.py --run-dir sheet_imports/lindhaus_2026-06-12 --activate
"""

import argparse
import hashlib
import html
import json
import logging
import re
import sys
import time
from pathlib import Path

import requests

from import_missing_products import (
    shopify_api_url,
    shopify_get,
    shopify_headers,
    shopify_post,
    slugify,
    parse_price,
    calculate_markup_price,
    generate_tags,
    SHOPIFY_STORE,
    SHOPIFY_ACCESS_TOKEN,
    SHOPIFY_API_VERSION,
)

BASE_DIR = Path(__file__).parent
SHOPIFY_MAP_FILE = BASE_DIR / "shopify_product_map.json"
PRICE_LOCKS_FILE = BASE_DIR / "price_locks.json"
DUAL_SOURCE_BRANDS_FILE = BASE_DIR / "dual_source_brands.json"
DUAL_SOURCE_SKUS_FILE = BASE_DIR / "dual_source_skus.json"
BULK_IMPORT_FILE = BASE_DIR / "bulk_import_progress.json"
MISSING_IMPORT_FILE = BASE_DIR / "missing_import_progress.json"

MIN_IMAGE_PX = 200          # reject thumbnails smaller than this on either axis
MAX_IMAGES_PER_PRODUCT = 8
THIN_DESCRIPTION_CHARS = 200  # below this, an existing card's description is "thin"
ENRICH_IMAGE_THRESHOLD = 2    # existing cards with fewer images than this get ours

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def shopify_put(path, payload, retries=3):
    """PUT helper mirroring shopify_post (import_missing_products has no PUT)."""
    url = shopify_api_url(path)
    headers = shopify_headers()
    for attempt in range(retries):
        try:
            resp = requests.put(url, json=payload, headers=headers, timeout=60)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)))
                continue
            if resp.status_code in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep((attempt + 1) * 5)
                continue
            if resp.status_code == 422:
                return resp.json(), resp.status_code
            resp.raise_for_status()
            return resp.json(), resp.status_code
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            if attempt < retries - 1:
                time.sleep((attempt + 1) * 5)
            else:
                raise
    return None, None


# ── Run-dir state ────────────────────────────────────────────────────────────

def load_run(run_dir: Path):
    """Load manifest + progress for a run. Manifest must exist (Claude writes it)."""
    manifest_file = run_dir / "manifest.json"
    if not manifest_file.exists():
        log.error(f"{manifest_file} not found — run the parse step first "
                  f"(Claude reads the sheet and writes manifest.json).")
        sys.exit(1)
    manifest = json.loads(manifest_file.read_text())
    progress_file = run_dir / "progress.json"
    if progress_file.exists():
        progress = json.loads(progress_file.read_text())
    else:
        progress = {
            "steps_done": [],
            "buckets": {"new": [], "existing": {}, "sku_fixes": {}, "ambiguous": {}},
            "images_done": [],
            "created": {},      # sku -> product_id (drafts made by step 5)
            "updated": {},      # sku -> list of changes applied by step 6
            "failed": {},       # sku -> error string
            "flagged_pricing": [],
            "activated": False,
        }
    return manifest, progress


def save_progress(run_dir: Path, progress):
    (run_dir / "progress.json").write_text(json.dumps(progress, indent=2))


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def normalize_o0(sku):
    """O and 0 are interchangeable for matching; sheets are authoritative."""
    return sku.strip().upper().replace("O", "0")


# ── Step stubs (implemented in later tasks) ──────────────────────────────────

def step_parse_validate(run_dir, manifest, progress, dry_run=False):
    """Validate a Claude-written manifest.json. Read-only."""
    errors, flagged = [], []
    seen = set()
    for i, row in enumerate(manifest.get("rows", [])):
        sku = str(row.get("sku", "")).strip()
        if not sku:
            errors.append(f"row {i}: empty sku")
            continue
        if sku in seen:
            errors.append(f"row {i}: duplicate sku {sku} in manifest")
        seen.add(sku)
        cost = row.get("dealer_cost")
        if not isinstance(cost, (int, float)) or cost <= 0:
            errors.append(f"row {i} ({sku}): bad dealer_cost {cost!r}")
            continue
        map_price = row.get("map_price")
        if map_price is not None:
            if not isinstance(map_price, (int, float)) or map_price <= 0:
                errors.append(f"row {i} ({sku}): bad map_price {map_price!r}")
            elif map_price <= cost:
                # MAP at or below dealer cost is suspicious (e.g. Lindhaus M28R
                # nozzle listed $22.80/$22.80) — review, don't price blindly.
                flagged.append(sku)
        if not str(row.get("name", "")).strip():
            errors.append(f"row {i} ({sku}): empty name")

    if errors:
        for e in errors:
            log.error(f"  {e}")
        log.error(f"manifest INVALID: {len(errors)} error(s)")
        sys.exit(1)

    progress["flagged_pricing"] = sorted(set(progress.get("flagged_pricing", []) + flagged))
    if "parse" not in progress["steps_done"]:
        progress["steps_done"].append("parse")
    save_progress(run_dir, progress)
    log.info(f"manifest OK: {len(manifest['rows'])} rows, "
             f"{len(manifest.get('skipped_rows', []))} skipped at parse, "
             f"{len(flagged)} flagged (MAP <= cost)")


def graphql_find_sku(sku):
    """Return list of {variant_id, product_id, sku} for an exact live-store SKU match."""
    escaped = sku.replace('"', '\\"')
    query = '''{ productVariants(first: 5, query: "sku:%s") {
        edges { node { id sku product { id } } } } }''' % escaped
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
               "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            resp = requests.post(url, json={"query": query}, headers=headers, timeout=30)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)))
                continue
            resp.raise_for_status()
            out = []
            for edge in resp.json().get("data", {}).get("productVariants", {}).get("edges", []):
                node = edge.get("node", {})
                if node.get("sku", "").strip() == sku.strip():
                    out.append({
                        "variant_id": node["id"].split("/")[-1],
                        "product_id": node["product"]["id"].split("/")[-1],
                        "sku": node["sku"],
                    })
            return out
        except Exception:
            if attempt == 2:
                raise
            time.sleep((attempt + 1) * 5)
    raise RuntimeError(f"graphql_find_sku({sku!r}): retries exhausted — refusing to "
                       "classify as new (duplicate-product risk)")


def step_match(run_dir, manifest, progress, dry_run=False):
    """Bucket manifest rows: new / existing / sku_fixes / ambiguous. Read-only."""
    if not SHOPIFY_MAP_FILE.exists():
        log.error("shopify_product_map.json missing — run build_shopify_map.py first")
        sys.exit(1)
    shopify_map = json.loads(SHOPIFY_MAP_FILE.read_text())
    vendor = manifest["vendor"].strip().lower()

    # Index live-store SKUs (exact and O0-normalized) for this vendor + global exact
    exact = {}           # sku -> map entry
    fuzzy = {}           # normalized sku -> [(store_sku, entry)] — same vendor only
    for store_sku, entry in shopify_map.items():
        exact[store_sku] = entry
        if entry.get("vendor", "").strip().lower() == vendor:
            fuzzy.setdefault(normalize_o0(store_sku), []).append((store_sku, entry))

    buckets = {"new": [], "existing": {}, "sku_fixes": {}, "ambiguous": {}}
    for row in manifest["rows"]:
        sku = str(row["sku"]).strip()
        if sku in exact:
            e = exact[sku]
            buckets["existing"][sku] = {"product_id": e["product_id"],
                                        "variant_id": e["variant_id"]}
            continue
        candidates = [(s, e) for s, e in fuzzy.get(normalize_o0(sku), []) if s != sku]
        if len(candidates) == 1:
            store_sku, e = candidates[0]
            # Sheet SKU is authoritative: step 6 fixes the store SKU spelling.
            buckets["sku_fixes"][sku] = {"store_sku": store_sku,
                                         "product_id": e["product_id"],
                                         "variant_id": e["variant_id"]}
            buckets["existing"][sku] = {"product_id": e["product_id"],
                                        "variant_id": e["variant_id"]}
        elif len(candidates) > 1:
            buckets["ambiguous"][sku] = [s for s, _ in candidates]
        else:
            buckets["new"].append(sku)

    # Live double-check: nothing goes to `new` on local data alone.
    confirmed_new = []
    for sku in buckets["new"]:
        live = graphql_find_sku(sku)
        if live:
            log.warning(f"  {sku}: in store but missing from local map — treating as existing")
            buckets["existing"][sku] = {"product_id": live[0]["product_id"],
                                        "variant_id": live[0]["variant_id"]}
        else:
            confirmed_new.append(sku)
        time.sleep(0.3)
    buckets["new"] = confirmed_new

    progress["buckets"] = buckets
    if "match" not in progress["steps_done"]:
        progress["steps_done"].append("match")
    save_progress(run_dir, progress)
    log.info(f"match: {len(buckets['new'])} new, {len(buckets['existing'])} existing "
             f"({len(buckets['sku_fixes'])} need SKU fix), "
             f"{len(buckets['ambiguous'])} ambiguous (manual review)")


META_TITLE_MAX = 70       # soft caps — warn, don't fail
META_DESC_MAX = 170

def step_research_validate(run_dir, manifest, progress, dry_run=False):
    """Validate Claude-agent content files for every SKU that needs one. Read-only."""
    content_dir = run_dir / "content"
    need = list(progress["buckets"]["new"]) + progress.get("needs_enrichment", [])
    missing, invalid, ok = [], [], []
    for sku in need:
        f = content_dir / f"{sku}.json"
        if not f.exists():
            missing.append(sku)
            continue
        try:
            c = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            invalid.append(f"{sku}: bad JSON ({e})")
            continue
        problems = []
        if c.get("sku") != sku:
            problems.append("sku mismatch")
        if not str(c.get("title", "")).strip():
            problems.append("empty title")
        body_text = strip_html(c.get("body_html", ""))
        if len(body_text) < THIN_DESCRIPTION_CHARS:
            problems.append(f"body too thin ({len(body_text)} chars)")
        if sku not in c.get("body_html", ""):
            problems.append("SKU not mentioned in body")
        if not isinstance(c.get("image_urls"), list):
            problems.append("image_urls not a list")
        if len(c.get("meta_title", "")) > META_TITLE_MAX:
            log.warning(f"  {sku}: meta_title {len(c['meta_title'])} chars (>{META_TITLE_MAX})")
        if len(c.get("meta_description", "")) > META_DESC_MAX:
            log.warning(f"  {sku}: meta_description {len(c['meta_description'])} chars (>{META_DESC_MAX})")
        if problems:
            invalid.append(f"{sku}: " + "; ".join(problems))
        else:
            ok.append(sku)

    for line in invalid:
        log.error(f"  INVALID {line}")
    log.info(f"research: {len(ok)} ok, {len(missing)} missing, {len(invalid)} invalid "
             f"of {len(need)} needed")
    if missing:
        log.info("missing: " + ", ".join(missing[:20]) + ("..." if len(missing) > 20 else ""))
    if not missing and not invalid:
        if "research" not in progress["steps_done"]:
            progress["steps_done"].append("research")
        save_progress(run_dir, progress)


def step_images(run_dir, manifest, progress, dry_run=False):
    raise NotImplementedError


def step_create(run_dir, manifest, progress, dry_run=False):
    raise NotImplementedError


def step_update(run_dir, manifest, progress, dry_run=False):
    raise NotImplementedError


def step_register(run_dir, manifest, progress, dry_run=False):
    raise NotImplementedError


def step_activate(run_dir, manifest, progress, dry_run=False):
    raise NotImplementedError


# ── Main ─────────────────────────────────────────────────────────────────────

STEPS = ["parse", "match", "research", "images", "create", "update", "register"]

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="e.g. sheet_imports/lindhaus_2026-06-12")
    ap.add_argument("--step", choices=STEPS, help="run a single step")
    ap.add_argument("--dry-run", action="store_true", help="no Shopify mutations")
    ap.add_argument("--activate", action="store_true",
                    help="flip this run's created drafts to active")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        log.error(f"run dir {run_dir} does not exist")
        sys.exit(1)

    manifest, progress = load_run(run_dir)

    if args.activate:
        step_activate(run_dir, manifest, progress, dry_run=args.dry_run)
        return

    if not args.step:
        ap.error("--step or --activate required")

    fn = {
        "parse": step_parse_validate,
        "match": step_match,
        "research": step_research_validate,
        "images": step_images,
        "create": step_create,
        "update": step_update,
        "register": step_register,
    }[args.step]
    fn(run_dir, manifest, progress, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
