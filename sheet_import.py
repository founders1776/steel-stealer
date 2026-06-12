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
import base64
import hashlib
import html
import json
import logging
import re
import subprocess
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


IMG_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

def step_images(run_dir, manifest, progress, dry_run=False):
    """Download image_urls from content files into images/<sku>/NN.<ext>."""
    from PIL import Image  # lazy: PIL only needed here

    content_dir = run_dir / "content"
    images_root = run_dir / "images"
    need = list(progress["buckets"]["new"]) + progress.get("needs_enrichment", [])
    done = set(progress.get("images_done", []))

    for sku in need:
        if sku in done:
            continue
        f = content_dir / f"{sku}.json"
        if not f.exists():
            continue
        urls = json.loads(f.read_text()).get("image_urls", [])[:MAX_IMAGES_PER_PRODUCT * 2]
        sku_dir = images_root / sku
        sku_dir.mkdir(parents=True, exist_ok=True)
        seen_hashes, saved = set(), 0
        for url in urls:
            if saved >= MAX_IMAGES_PER_PRODUCT:
                break
            try:
                resp = requests.get(url, headers=IMG_HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.content
            except Exception as e:
                log.warning(f"  {sku}: download failed {url} ({e})")
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_hashes:
                continue
            tmp = sku_dir / "_tmp"
            tmp.write_bytes(data)
            try:
                with Image.open(tmp) as im:
                    w, h = im.size
                    fmt = (im.format or "").lower()
                if min(w, h) < MIN_IMAGE_PX or fmt not in ("jpeg", "png", "webp", "gif"):
                    tmp.unlink()
                    continue
            except Exception:
                tmp.unlink()
                continue
            ext = {"jpeg": "jpg"}.get(fmt, fmt)
            tmp.rename(sku_dir / f"{saved:02d}.{ext}")
            seen_hashes.add(digest)
            saved += 1
            time.sleep(0.2)
        log.info(f"  {sku}: {saved} image(s)")
        done.add(sku)
        progress["images_done"] = sorted(done)
        save_progress(run_dir, progress)

    no_image = [s for s in need
                if not (images_root / s).is_dir() or not any((images_root / s).iterdir())]
    log.info(f"images: {len(done)} SKUs processed, {len(no_image)} without any image")
    if "images" not in progress["steps_done"]:
        progress["steps_done"].append("images")
    save_progress(run_dir, progress)


def resolve_price(row, flagged):
    """MAP -> MSRP -> tiered markup. Returns (price, source) or (None, reason)."""
    sku = str(row["sku"]).strip()
    if sku in flagged:
        return None, "flagged: MAP <= dealer cost — manual pricing required"
    if row.get("map_price"):
        return float(row["map_price"]), "MAP"
    if row.get("msrp"):
        return float(row["msrp"]), "MSRP"
    return calculate_markup_price(float(row["dealer_cost"])), "tiered markup"


def build_image_payloads(run_dir, sku):
    sku_dir = run_dir / "images" / sku
    payloads = []
    if sku_dir.is_dir():
        for p in sorted(sku_dir.iterdir()):
            if p.suffix.lower() in (".jpg", ".png", ".webp", ".gif"):
                payloads.append({
                    "attachment": base64.b64encode(p.read_bytes()).decode(),
                    "filename": f"{slugify(sku)}-{p.name}",
                })
    return payloads


def step_create(run_dir, manifest, progress, dry_run=False):
    """Create DRAFT products for the `new` bucket. MUTATES THE STORE unless --dry-run."""
    if "parse" not in progress["steps_done"]:
        log.error("run --step parse first — flagged-pricing data missing")
        sys.exit(1)
    rows = {str(r["sku"]).strip(): r for r in manifest["rows"]}
    flagged = set(progress.get("flagged_pricing", []))
    content_dir = run_dir / "content"
    created = progress.get("created", {})

    for sku in progress["buckets"]["new"]:
        if sku in created:
            continue
        row = rows[sku]
        content_file = content_dir / f"{sku}.json"
        if not content_file.exists():
            progress["failed"][sku] = "create skipped: no content file (re-run research)"
            save_progress(run_dir, progress)
            log.error(f"  FAILED {sku}: no content file — re-run research")
            continue
        c = json.loads(content_file.read_text())
        price, source = resolve_price(row, flagged)
        if price is None:
            log.warning(f"  SKIP {sku}: {source}")
            continue

        product = {
            "title": c["title"],
            "body_html": c["body_html"],
            "vendor": manifest["vendor"],
            "status": "draft",
            "tags": ", ".join(generate_tags({
                "clean_name": c["title"], "brand": manifest["vendor"], "model": ""})),
            "metafields_global_title_tag": c.get("meta_title", c["title"]),
            "metafields_global_description_tag": c.get("meta_description", ""),
            "variants": [{
                "sku": sku,
                "price": f"{price:.2f}",
                "cost": f"{float(row['dealer_cost']):.2f}",
                "inventory_management": None,
                "inventory_policy": "continue",
                "taxable": True,
            }],
            "images": build_image_payloads(run_dir, sku),
        }

        if dry_run:
            log.info(f"  DRY {sku}: would create draft '{c['title']}' "
                     f"@ ${price:.2f} ({source}), cost ${row['dealer_cost']:.2f}, "
                     f"{len(product['images'])} image(s)")
            continue

        data, code = shopify_post("products.json", {"product": product})
        if data and "product" in data:
            pid = data["product"]["id"]
            created[sku] = str(pid)
            progress["failed"].pop(sku, None)
            progress["created"] = created
            save_progress(run_dir, progress)
            log.info(f"  CREATED {sku} -> product {pid} @ ${price:.2f} ({source})")
        else:
            progress["failed"][sku] = f"create failed: HTTP {code}: {json.dumps(data)[:300]}"
            save_progress(run_dir, progress)
            log.error(f"  FAILED {sku}: HTTP {code}")
        time.sleep(0.55)

    if not dry_run and "create" not in progress["steps_done"]:
        progress["steps_done"].append("create")
        save_progress(run_dir, progress)
    log.info(f"create: {len(created)} created total, {len(progress['failed'])} failed")


def step_update(run_dir, manifest, progress, dry_run=False):
    """Smart-update existing cards. MUTATES THE STORE unless --dry-run."""
    if "parse" not in progress["steps_done"]:
        log.error("run --step parse first — flagged-pricing data missing")
        sys.exit(1)
    rows = {str(r["sku"]).strip(): r for r in manifest["rows"]}
    flagged = set(progress.get("flagged_pricing", []))
    content_dir = run_dir / "content"
    updated = progress.get("updated", {})
    needs_enrichment = set(progress.get("needs_enrichment", []))

    for sku, ids in progress["buckets"]["existing"].items():
        if sku in updated and sku not in needs_enrichment:
            continue
        try:
            row = rows[sku]
            changes = []

            # Live product state — never decide off local data
            resp = shopify_get(f"products/{ids['product_id']}.json")
            if resp is None:
                progress["failed"][sku] = "update skipped: product GET failed after retries"
                save_progress(run_dir, progress)
                log.error(f"  FAILED {sku}: product GET failed after retries — continuing")
                continue
            live = resp.json()["product"]
            time.sleep(0.3)

            # 1) SKU spelling fix (sheet is authoritative)
            fix = progress["buckets"]["sku_fixes"].get(sku)
            if fix:
                if dry_run:
                    log.info(f"  DRY {sku}: would fix store SKU {fix['store_sku']!r} -> {sku!r}")
                    changes.append(f"sku fixed from {fix['store_sku']}")
                else:
                    data, code = shopify_put(f"variants/{ids['variant_id']}.json",
                                {"variant": {"id": int(ids["variant_id"]), "sku": sku}})
                    if code != 200:
                        log.warning(f"  {sku}: SKU-fix PUT returned HTTP {code} — skipping sku fix record")
                    else:
                        changes.append(f"sku fixed from {fix['store_sku']}")
                    time.sleep(0.55)

            # 2) Cost + price (always). Flagged-pricing SKUs get cost only.
            price, source = resolve_price(row, flagged)
            variant_payload = {"id": int(ids["variant_id"]),
                               "cost": f"{float(row['dealer_cost']):.2f}"}
            if price is not None:
                variant_payload["price"] = f"{price:.2f}"
                changes.append(f"price ${price:.2f} ({source}), cost ${row['dealer_cost']:.2f}")
            else:
                changes.append(f"cost ${row['dealer_cost']:.2f} only ({source})")
            if dry_run:
                log.info(f"  DRY {sku}: {changes[-1]}")
            else:
                data, code = shopify_put(f"variants/{ids['variant_id']}.json",
                                         {"variant": variant_payload})
                if code != 200:
                    raise RuntimeError(f"variant PUT HTTP {code}")
                time.sleep(0.55)

            # 3) Enrichment: images if sparse, description if thin
            content_file = content_dir / f"{sku}.json"
            wants_images = len(live.get("images", [])) < ENRICH_IMAGE_THRESHOLD
            wants_desc = len(strip_html(live.get("body_html", ""))) < THIN_DESCRIPTION_CHARS
            if (wants_images or wants_desc) and not content_file.exists():
                needs_enrichment.add(sku)
                log.info(f"  {sku}: needs enrichment ({'images' if wants_images else ''}"
                         f"{'+' if wants_images and wants_desc else ''}"
                         f"{'description' if wants_desc else ''}) — research it, then re-run update")
            elif content_file.exists():
                c = json.loads(content_file.read_text())
                if wants_desc:
                    if dry_run:
                        log.info(f"  DRY {sku}: would replace thin description")
                    else:
                        data, code = shopify_put(f"products/{ids['product_id']}.json",
                                                 {"product": {
                            "id": int(ids["product_id"]),
                            "body_html": c["body_html"],
                            "metafields_global_title_tag": c.get("meta_title", ""),
                            "metafields_global_description_tag": c.get("meta_description", ""),
                        }})
                        if code != 200:
                            raise RuntimeError(f"description PUT HTTP {code}")
                        time.sleep(0.55)
                    changes.append("description rewritten")
                if wants_images:
                    imgs = build_image_payloads(run_dir, sku)
                    uploaded = 0
                    for img in imgs:
                        if dry_run:
                            continue
                        idata, icode = shopify_post(
                            f"products/{ids['product_id']}/images.json", {"image": img})
                        if icode not in (200, 201):
                            log.warning(f"  {sku}: image POST returned HTTP {icode} — skipping")
                        else:
                            uploaded += 1
                        time.sleep(0.55)
                    if imgs:
                        count = len(imgs) if dry_run else uploaded
                        changes.append(f"{count} image(s) added"
                                       + (" [dry]" if dry_run else ""))
                if not dry_run:
                    needs_enrichment.discard(sku)

            if not dry_run:
                updated[sku] = changes
                progress["updated"] = updated
                progress["failed"].pop(sku, None)
            progress["needs_enrichment"] = sorted(needs_enrichment)
            save_progress(run_dir, progress)
        except Exception as e:
            progress["failed"][sku] = f"update failed: {e}"
            save_progress(run_dir, progress)
            log.error(f"  FAILED {sku}: {e} — continuing")
            continue

    if not dry_run and "update" not in progress["steps_done"]:
        progress["steps_done"].append("update")
        save_progress(run_dir, progress)
    log.info(f"update: {len(updated)} cards updated, "
             f"{len(needs_enrichment)} queued for enrichment research")


def step_register(run_dir, manifest, progress, dry_run=False):
    """Registry updates + report.md. Mutates LOCAL json files, not the store."""
    rows = {str(r["sku"]).strip(): r for r in manifest["rows"]}
    flagged = set(progress.get("flagged_pricing", []))
    touched = list(progress.get("created", {})) + list(progress.get("updated", {}))

    # 1) price_locks.json — every imported SKU @ MAP (or note when markup-priced)
    locks = json.loads(PRICE_LOCKS_FILE.read_text()) if PRICE_LOCKS_FILE.exists() else {}
    added_locks = 0
    for sku in touched:
        if sku in locks:
            continue
        if sku not in rows:
            log.warning(f"  {sku}: in progress but not in manifest — no lock written")
            continue
        price, source = resolve_price(rows[sku], flagged)
        locks[sku] = (f"${price:.2f} MAP ({manifest['brand']} sheet {manifest['parsed_at']})"
                      if source == "MAP" else
                      f"{source} (sheet import {manifest['parsed_at']}) — treat as MAP-protected")
        added_locks += 1

    # 2) dual_source_brands.json — new brand?
    brands = json.loads(DUAL_SOURCE_BRANDS_FILE.read_text()) if DUAL_SOURCE_BRANDS_FILE.exists() else []
    brand_added = False
    if manifest["brand"].upper() not in {b.upper() for b in brands}:
        brands.append(manifest["brand"].upper())
        brand_added = True

    # 3) dual_source_skus.json — matched cards the Steel City sync currently
    #    tracks. The sync's gate is a PRODUCT-ID set built from
    #    bulk_import_progress.json + missing_import_progress.json (see
    #    sync_stock_prices.py:763-781), keyed by handle with {id, status}.
    #    Any existing-bucket card whose product_id is in that set gets its
    #    (sheet) SKU added to dual_source_skus.json so the sync stops
    #    stock-drafting it — it's now available direct from the distributor.
    ds_skus = json.loads(DUAL_SOURCE_SKUS_FILE.read_text()) if DUAL_SOURCE_SKUS_FILE.exists() else []
    sync_pids = set()
    if BULK_IMPORT_FILE.exists():
        bulk = json.loads(BULK_IMPORT_FILE.read_text())
        sync_pids = {v["id"] for v in bulk.values()
                     if isinstance(v, dict) and v.get("status") == "created"}
    if MISSING_IMPORT_FILE.exists():
        mp = json.loads(MISSING_IMPORT_FILE.read_text())
        for v in mp.get("uploaded", {}).values():
            if isinstance(v, dict) and v.get("status") == "created" and v.get("id"):
                sync_pids.add(v["id"])
    sync_pid_strs = {str(p) for p in sync_pids}
    # For sku_fixes rows this records the SHEET spelling; the sync keys off the
    # old spelling until the map rebuild rekeys it — protection actually comes
    # from the rebuilt map, the list entry is belt-and-suspenders.
    ds_added = []
    for sku, ids in progress["buckets"]["existing"].items():
        if str(ids["product_id"]) in sync_pid_strs and sku not in ds_skus:
            ds_skus.append(sku)
            ds_added.append(sku)

    # 4) report.md
    images_root = run_dir / "images"
    need = list(progress["buckets"]["new"]) + progress.get("needs_enrichment", [])
    no_image = [s for s in need
                if not (images_root / s).is_dir() or not any((images_root / s).iterdir())]
    lines = [
        f"# Sheet import report — {manifest['brand']} ({manifest['parsed_at']})",
        f"Source: `{manifest['source_sheet']}`", "",
        f"## Created ({len(progress.get('created', {}))} drafts)",
        *(f"- {s} -> product {pid}" for s, pid in sorted(progress.get("created", {}).items())),
        "", f"## Updated in place ({len(progress.get('updated', {}))})",
        *(f"- {s}: {'; '.join(ch)}" for s, ch in sorted(progress.get("updated", {}).items())),
        "", f"## SKU spelling fixes ({len(progress['buckets']['sku_fixes'])})",
        *(f"- store {v['store_sku']!r} -> sheet {k!r}"
          for k, v in sorted(progress["buckets"]["sku_fixes"].items())),
        "", f"## Ambiguous matches — MANUAL REVIEW ({len(progress['buckets']['ambiguous'])})",
        *(f"- {k}: candidates {v}" for k, v in sorted(progress["buckets"]["ambiguous"].items())),
        "", f"## Flagged pricing (MAP <= cost) — MANUAL PRICING ({len(flagged)})",
        *(f"- {s}" for s in sorted(flagged)),
        "", f"## No image found ({len(no_image)})",
        *(f"- {s}" for s in sorted(no_image)),
        "", f"## Failed ({len(progress.get('failed', {}))})",
        *(f"- {s}: {err}" for s, err in sorted(progress.get("failed", {}).items())),
        "", f"## Skipped at parse ({len(manifest.get('skipped_rows', []))})",
        *(f"- {r['raw']} — {r['reason']}" for r in manifest.get("skipped_rows", [])),
        "",
        "## Follow-ups",
        f"- Registry files changed: re-encrypt the CI data bundle and commit "
        f"(see OPERATIONS.md / workflow tar list) or the next CI run resurrects stale copies.",
        f"- If a NEW brand was added: update the `DUAL_SOURCE_BRANDS` GitHub secret "
        f"(`gh secret set DUAL_SOURCE_BRANDS < dual_source_brands.json`) — CI overwrites "
        f"the local file from that secret every run.",
    ]
    (run_dir / "report.md").write_text("\n".join(lines))

    if dry_run:
        log.info(f"DRY register: +{added_locks} price locks, brand_added={brand_added}, "
                 f"+{len(ds_added)} dual-source SKUs")
    else:
        PRICE_LOCKS_FILE.write_text(json.dumps(locks, indent=2))
        DUAL_SOURCE_BRANDS_FILE.write_text(json.dumps(brands, indent=2))
        DUAL_SOURCE_SKUS_FILE.write_text(json.dumps(ds_skus, indent=2))
        log.info("rebuilding shopify_product_map.json ...")
        subprocess.run([sys.executable, "build_shopify_map.py"], check=True, cwd=BASE_DIR)

    if not dry_run and "register" not in progress["steps_done"]:
        progress["steps_done"].append("register")
    save_progress(run_dir, progress)
    log.info(f"register: +{added_locks} locks, brand_added={brand_added}, "
             f"+{len(ds_added)} dual-source SKUs, report -> {run_dir / 'report.md'}")


def step_activate(run_dir, manifest, progress, dry_run=False):
    """Flip this run's created drafts to active (after James reviews)."""
    had_failures = False
    for sku, pid in progress.get("created", {}).items():
        if dry_run:
            log.info(f"  DRY {sku}: would activate product {pid}")
            continue
        data, code = shopify_put(f"products/{pid}.json",
                    {"product": {"id": int(pid), "status": "active"}})
        if code != 200:
            log.error(f"  FAILED activate {sku} (product {pid}): HTTP {code} — continuing")
            had_failures = True
            continue
        log.info(f"  ACTIVATED {sku} (product {pid})")
        time.sleep(0.55)
    if not dry_run:
        if had_failures:
            log.warning("some activations failed — re-run --activate to retry")
        else:
            progress["activated"] = True
        save_progress(run_dir, progress)


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
