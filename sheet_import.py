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
    shopify_get,
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
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/{path}"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
               "Content-Type": "application/json"}
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
    raise NotImplementedError


def step_match(run_dir, manifest, progress, dry_run=False):
    raise NotImplementedError


def step_research_validate(run_dir, manifest, progress, dry_run=False):
    raise NotImplementedError


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
