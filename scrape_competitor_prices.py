#!/usr/bin/env python3
"""
Phase 4b: Scrape competitor prices for all products.

All competitors are Shopify stores, so we pull each store's FULL catalog via the
public /products.json endpoint (250/page, ?page=N) and match our SKUs LOCALLY
against variants[].sku + titles. Domains are pulled concurrently.

This replaced per-SKU /search/suggest.json queries (~13k SKUs × ~12 stores ≈
160k requests), which got anti-bot rate-limited to a crawl (17h for ~1.5 stores).
The catalog approach is a few hundred requests total → no throttling, ~2 minutes
for all competitors.

Usage:
  python3 scrape_competitor_prices.py                           # Full run (all competitors)
  python3 scrape_competitor_prices.py --competitor ezvacuum.com  # Single competitor
  python3 scrape_competitor_prices.py --limit 50                # Test with 50 SKUs
  python3 scrape_competitor_prices.py --dry-run                 # Preview without saving
  python3 scrape_competitor_prices.py --rebuild                 # Re-aggregate from progress (no scraping)
"""

import argparse
import asyncio
import json
import logging
import random
import re
import statistics
import time
from pathlib import Path
from urllib.parse import quote

try:
    import aiohttp
except ImportError:
    aiohttp = None

import requests  # fallback for single-request operations

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "product_names.json"
DESCO_PRODUCTS_FILE = BASE_DIR / "desco_products.json"
REPRICE_TARGETS_FILE = BASE_DIR / "reprice_targets.json"
COMPETITORS_FILE = BASE_DIR / "competitors.json"
PROGRESS_FILE = BASE_DIR / "competitor_price_progress.json"
OUTPUT_FILE = BASE_DIR / "competitor_prices.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


# ── SKU Matching (unchanged from original) ────────────────────────────────────

ACCESSORY_INDICATORS = re.compile(
    r'\b(fits?|for|compatible\s+with|replacement|bag[s]?\s+for|filter[s]?\s+for|'
    r'belt[s]?\s+for|hose\s+for|cord\s+for|brush\s+for|part\s+for|'
    r'pack\s+of|set\s+of|\d+[\-\s]?pack)\b',
    re.IGNORECASE,
)


def normalize_sku(sku):
    return re.sub(r'[\-\s\.]', '', sku).lower()


def is_accessory_match(sku, title):
    if not title:
        return False
    title_lower = title.lower()
    sku_lower = sku.lower()
    sku_pos = title_lower.find(sku_lower)
    if sku_pos == -1:
        sku_pos = title_lower.find(normalize_sku(sku))
    if sku_pos == -1:
        return False
    text_before_sku = title_lower[:sku_pos]
    if ACCESSORY_INDICATORS.search(text_before_sku):
        return True
    for pattern in [r'\bfits?\s+', r'\bfor\s+', r'\bcompatible\s+with\s+']:
        if re.search(pattern + re.escape(sku_lower), title_lower):
            return True
    return False


def price_is_sane(competitor_price, dealer_cost):
    if not dealer_cost or dealer_cost <= 0:
        return True
    ratio = competitor_price / dealer_cost
    return 0.50 <= ratio <= 5.0


def parse_price_str(price_str):
    if not price_str:
        return None
    match = re.search(r'[\d]+\.?\d*', str(price_str))
    return float(match.group()) if match else None


# ── Shopify catalog scraper (pull /products.json, match locally) ──────────────
#
# Every competitor is a Shopify store. Querying /search/suggest.json once per
# SKU was ~13k SKUs × ~12 stores ≈ 160k requests → anti-bot rate-limited to a
# crawl. Instead we pull each store's FULL catalog via the public
# /products.json endpoint (250 products/page, tens-to-hundreds of requests per
# store) and match our SKUs LOCALLY against variants[].sku + product titles.
# ~300x fewer requests → no throttling, and far faster.

async def fetch_catalog(session, domain, deadline=None):
    """Page through a store's public /products.json with ?page=N (since_id is
    silently ignored by many storefronts). Returns the full product list, or []
    if the endpoint is closed/blocked. Stops at the first short/empty page."""
    base = f"https://{domain}"
    products = []
    page = 1
    for _ in range(4000):  # safety cap (~1M products)
        if deadline and time.time() > deadline:
            log.info(f"[{domain}] deadline hit at {len(products)} products")
            break
        url = f"{base}/products.json?limit=250&page={page}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    if not products:
                        log.warning(f"[{domain}] /products.json HTTP {resp.status}")
                    break
                data = await resp.json(content_type=None)
        except Exception as e:
            log.warning(f"[{domain}] catalog page {page} failed: {e}")
            break
        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 250:   # last page
            break
        page += 1
        await asyncio.sleep(random.uniform(0.3, 0.8))  # polite between pages
    return products


def match_catalog(domain, products, our_index):
    """Match a store's catalog against our SKUs. our_index: normalized SKU →
    (our_sku, dealer_cost). Returns {our_sku: {price, url, title}}, keeping the
    LOWEST sane price per SKU."""
    base = f"https://{domain}"
    found = {}
    for p in products:
        title = p.get("title", "") or ""
        handle = p.get("handle", "")
        url = f"{base}/products/{handle}"
        for v in p.get("variants", []):
            price = parse_price_str(v.get("price"))
            if not price or price <= 0:
                continue
            # Candidate normalized SKU keys: the variant.sku plus tokens from
            # sku+title (stores with messy sku fields still match by title).
            vsku = v.get("sku") or ""
            candidates = set()
            if vsku:
                candidates.add(normalize_sku(vsku))
            for tok in re.split(r'[\s,/|;]+', f"{vsku} {title}"):
                ntok = normalize_sku(tok)
                if len(ntok) >= 4:   # skip tiny ambiguous tokens
                    candidates.add(ntok)
            for ncs in candidates:
                hit = our_index.get(ncs)
                if not hit:
                    continue
                our_sku, dealer_cost = hit
                if is_accessory_match(our_sku, title):
                    continue
                if not price_is_sane(price, dealer_cost):
                    continue
                prev = found.get(our_sku)
                if prev is None or price < prev["price"]:
                    found[our_sku] = {"price": price, "url": url, "title": title}
                break
    return found


async def scrape_shopify_catalog(domain, our_index, progress, save_lock=None, deadline=None):
    """Pull a store's catalog and record SKU matches in progress[domain]."""
    connector = aiohttp.TCPConnector(limit=4, limit_per_host=4)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        products = await fetch_catalog(session, domain, deadline=deadline)
    if not products:
        log.warning(f"[{domain}] no catalog retrieved — skipping")
        return
    matches = match_catalog(domain, products, our_index)
    dom = progress.setdefault(domain, {})
    dom.clear()
    dom.update(matches)
    if save_lock is not None:
        async with save_lock:
            save_progress(progress)
    else:
        save_progress(progress)
    log.info(f"[{domain}] catalog: {len(products)} products → {len(matches)} SKU matches")


# ── Progress ──────────────────────────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {}


def save_progress(progress):
    PROGRESS_FILE.write_text(json.dumps(progress))


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_scrape(competitors, skus_with_products, progress, limit=0, deadline=None):
    """Pull every competitor's full Shopify catalog CONCURRENTLY and match our
    SKUs locally. Few hundred requests total instead of ~160k per-SKU queries,
    so no rate-limiting and wall-clock ≈ the slowest single catalog. A shared
    lock serializes progress saves; a closed/blocked /products.json is skipped
    without stalling the rest."""
    our_index = {}
    for sku, prod in skus_with_products:
        our_index[normalize_sku(sku)] = (sku, parse_price_str(prod.get("price")))

    save_lock = asyncio.Lock()
    domains = [c["domain"] for c in competitors]
    log.info(f"Pulling {len(domains)} competitor catalogs concurrently; "
             f"matching {len(our_index)} of our SKUs locally")

    async def _one(domain):
        try:
            await scrape_shopify_catalog(domain, our_index, progress, save_lock, deadline)
        except Exception as e:
            log.warning(f"[{domain}] failed: {e}")

    await asyncio.gather(*(_one(d) for d in domains))


def main():
    parser = argparse.ArgumentParser(description="Scrape competitor prices (async)")
    parser.add_argument("--competitor", type=str, help="Scrape only this competitor domain")
    parser.add_argument("--limit", type=int, default=0, help="Limit SKUs per competitor (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving final output")
    parser.add_argument("--rebuild", action="store_true",
                        help="Skip scraping, just rebuild competitor_prices.json from progress data")
    parser.add_argument("--fresh", action="store_true",
                        help="Clear progress and start fresh (re-scrape everything)")
    parser.add_argument("--budget-minutes", type=int, default=0,
                        help="Stop scraping after N minutes (progress saved; next run "
                             "resumes). For CI, where jobs are hard-killed at 6h.")
    args = parser.parse_args()

    if aiohttp is None:
        log.error("aiohttp not installed. Run: pip install aiohttp")
        return

    if not COMPETITORS_FILE.exists():
        log.error(f"{COMPETITORS_FILE} not found.")
        return

    products = json.loads(PRODUCTS_FILE.read_text())
    competitors_config = json.loads(COMPETITORS_FILE.read_text())

    if args.fresh:
        log.info("Fresh mode — clearing all progress")
        progress = {}
    else:
        progress = load_progress()

    # Build SKU list
    skus_with_products = []
    seen = set()
    for key, prod in products.items():
        sku = prod.get("sku", key)
        if sku not in seen:
            skus_with_products.append((sku, prod))
            seen.add(sku)

    # Reprice targets (dual-source brand parts, e.g. SEBO) aren't all Steel
    # City products, so merge any extra SKUs in. price_is_sane gets ref_price
    # (our retail) when there's no dealer cost — the 0.5x-5x band still
    # filters out wrong-product matches.
    if REPRICE_TARGETS_FILE.exists():
        targets = {k: v for k, v in json.loads(REPRICE_TARGETS_FILE.read_text()).items()
                   if not k.startswith("_")}
        added = 0
        for sku, target in targets.items():
            if sku not in seen:
                ref = target.get("dealer_cost") or target.get("ref_price")
                skus_with_products.append((sku, {"price": str(ref or "")}))
                seen.add(sku)
                added += 1
        log.info(f"Reprice targets merged: +{added} SKUs")

    # Desco (second distributor) — net-new SKUs live in desco_products.json, not
    # product_names.json. Merge them so every sellable item gets competitor data.
    if DESCO_PRODUCTS_FILE.exists():
        desco = json.loads(DESCO_PRODUCTS_FILE.read_text())
        added = 0
        for sku, d in desco.items():
            if sku not in seen:
                skus_with_products.append((sku, {"price": str(d.get("dealer_cost") or "")}))
                seen.add(sku)
                added += 1
        log.info(f"Desco products merged: +{added} SKUs")

    log.info(f"Total unique SKUs: {len(skus_with_products)}")

    # Filter to requested competitor(s)
    competitors = competitors_config.get("competitors", [])
    if args.competitor:
        competitors = [c for c in competitors if c["domain"] == args.competitor]
        if not competitors:
            log.error(f"Competitor '{args.competitor}' not found in competitors.json")
            return

    log.info(f"Competitors to scrape: {len(competitors)}")

    # A full sweep takes longer than one budgeted CI run, so progress
    # accumulates across runs (the progress file ships in the data bundle).
    # Once every domain has covered every SKU — or the cycle is older than
    # MAX_CYCLE_DAYS (a perpetually-blocked domain would otherwise pin the
    # cycle open forever) — clear it so the next cycle re-scrapes fresh
    # prices instead of standing still.
    MAX_CYCLE_DAYS = 21
    if not args.rebuild and not args.fresh and not args.limit and progress:
        sweep_complete = all(
            len(progress.get(c["domain"], {})) >= len(skus_with_products)
            for c in competitors)
        started_at = progress.get("_started_at")
        cycle_expired = bool(started_at) and (time.time() - started_at) > MAX_CYCLE_DAYS * 86400
        if sweep_complete or cycle_expired:
            log.info("Previous sweep %s — starting a fresh cycle"
                     % ("complete" if sweep_complete else "expired"))
            progress = {}
    if not args.rebuild:
        progress.setdefault("_started_at", time.time())

    if not args.rebuild:
        start = time.time()
        deadline = start + args.budget_minutes * 60 if args.budget_minutes else None
        asyncio.run(run_scrape(competitors, skus_with_products, progress, args.limit,
                               deadline=deadline))
        elapsed = time.time() - start
        log.info(f"\nScraping completed in {elapsed / 60:.1f} minutes")

    # Build final output: aggregate prices per SKU across all competitors
    log.info("\nAggregating prices across all competitors...")
    all_prices = {}
    for comp in competitors_config.get("competitors", []):
        domain = comp["domain"]
        domain_data = progress.get(domain, {})
        for sku, match_data in domain_data.items():
            if match_data and match_data.get("price"):
                if sku not in all_prices:
                    all_prices[sku] = {}
                all_prices[sku][domain] = {
                    "price": match_data["price"],
                    "url": match_data.get("url", ""),
                    "title": match_data.get("title", ""),
                }

    output_prices = {}
    for sku, competitors_data in all_prices.items():
        valid_prices = [c["price"] for c in competitors_data.values() if c and c.get("price")]
        if not valid_prices:
            continue
        output_prices[sku] = {
            "competitors": competitors_data,
            "avg_price": round(statistics.mean(valid_prices), 2),
            "min_price": round(min(valid_prices), 2),
            "num_competitors": len(valid_prices),
        }

    output = {
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_skus_with_data": len(output_prices),
        "prices": output_prices,
    }

    # Never replace a healthy output file with a drastically smaller one — a
    # blocked/failed run must not destroy months of competitor data (this
    # happened: run 27394350688 matched 0 SKUs and shipped an empty file).
    # Mid-cycle partial sweeps legitimately shrink somewhat; 50% is the line.
    if not args.dry_run and OUTPUT_FILE.exists():
        try:
            existing = json.loads(OUTPUT_FILE.read_text()).get("prices", {})
        except Exception:
            existing = {}
        if len(existing) > 100 and len(output_prices) < 0.5 * len(existing):
            log.error(f"REFUSING to overwrite {OUTPUT_FILE.name}: new data has "
                      f"{len(output_prices)} SKUs vs existing {len(existing)} — "
                      f"keeping the existing file")
            return

    if not args.dry_run:
        OUTPUT_FILE.write_text(json.dumps(output, indent=2))
        log.info(f"\nSaved to {OUTPUT_FILE}")
    else:
        log.info("\nDry run — not saving output file")

    log.info(f"SKUs with competitor data: {len(output_prices)}/{len(skus_with_products)}")
    if output_prices:
        all_avgs = [v["avg_price"] for v in output_prices.values()]
        log.info(f"Avg competitor price: ${statistics.mean(all_avgs):.2f}")
        log.info(f"Median competitor price: ${statistics.median(all_avgs):.2f}")


if __name__ == "__main__":
    main()
