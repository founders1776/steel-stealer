#!/usr/bin/env python3
"""
Phase 4b: Scrape competitor prices for all products.

All competitors are Shopify stores — uses /search/suggest.json API with
async aiohttp for parallelization (30-50 concurrent requests per domain).

Previous version was sequential (28+ hours, always timed out on GitHub Actions).
This version completes in ~30-60 minutes.

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
COMPETITORS_FILE = BASE_DIR / "competitors.json"
PROGRESS_FILE = BASE_DIR / "competitor_price_progress.json"
OUTPUT_FILE = BASE_DIR / "competitor_prices.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

CONCURRENCY = 30  # max concurrent requests per domain
BATCH_SIZE = 100  # checkpoint progress every N SKUs

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


def sku_matches(sku, text):
    sku_lower = sku.lower()
    text_lower = text.lower()
    if sku_lower in text_lower:
        return True
    return normalize_sku(sku) in normalize_sku(text)


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


# ── Async Shopify Scraper ─────────────────────────────────────────────────────

async def fetch_suggest(session, sem, domain, sku, dealer_cost):
    """Fetch suggest API + product.json for a single SKU. Returns (sku, match_or_None)."""
    base_url = f"https://{domain}"
    suggest_url = f"{base_url}/search/suggest.json"

    async with sem:
        try:
            # Step 1: suggest API
            params = {
                "q": sku,
                "resources[type]": "product",
                "resources[limit]": 5,
            }
            async with session.get(suggest_url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    # Back off and retry once
                    await asyncio.sleep(random.uniform(5, 10))
                    async with session.get(suggest_url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                        if resp2.status != 200:
                            return (sku, None)
                        data = await resp2.json(content_type=None)
                elif resp.status != 200:
                    return (sku, None)
                else:
                    data = await resp.json(content_type=None)

            products_found = data.get("resources", {}).get("results", {}).get("products", [])

            for p in products_found:
                title = p.get("title", "")
                url = p.get("url", "")
                if not (sku_matches(sku, title) or sku_matches(sku, url)):
                    continue
                if is_accessory_match(sku, title):
                    continue

                # Step 2: fetch full product JSON for accurate price
                product_url = f"{base_url}{url}.json"
                try:
                    async with session.get(product_url, timeout=aiohttp.ClientTimeout(total=15)) as prod_resp:
                        if prod_resp.status != 200:
                            continue
                        prod_data = await prod_resp.json(content_type=None)
                        variants = prod_data.get("product", {}).get("variants", [])
                        if variants:
                            price = parse_price_str(variants[0].get("price"))
                            if price and price > 0 and price_is_sane(price, dealer_cost):
                                return (sku, {
                                    "price": price,
                                    "url": f"{base_url}{url}",
                                    "title": title,
                                })
                except Exception:
                    continue

            return (sku, None)

        except asyncio.TimeoutError:
            return (sku, None)
        except Exception as e:
            log.debug(f"[{domain}] Error for {sku}: {e}")
            return (sku, None)


async def scrape_shopify_async(domain, skus_with_products, progress_key, progress, limit=0):
    """Scrape a Shopify competitor with async concurrent requests."""
    work = [(sku, prod) for sku, prod in skus_with_products if sku not in progress.get(progress_key, {})]
    if limit:
        work = work[:limit]

    if not work:
        log.info(f"[{domain}] No work remaining (all SKUs already checked)")
        return {}

    log.info(f"[{domain}] Async scraper: {len(work)} SKUs, concurrency={CONCURRENCY}")
    results = {}
    sem = asyncio.Semaphore(CONCURRENCY)

    connector = aiohttp.TCPConnector(limit=CONCURRENCY, limit_per_host=CONCURRENCY)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        # Process in batches for checkpointing
        for batch_start in range(0, len(work), BATCH_SIZE):
            batch = work[batch_start:batch_start + BATCH_SIZE]
            tasks = []
            for sku, product in batch:
                dealer_cost = parse_price_str(product.get("price"))
                tasks.append(fetch_suggest(session, sem, domain, sku, dealer_cost))

            batch_results = await asyncio.gather(*tasks)

            for sku, match in batch_results:
                if match:
                    results[sku] = match
                    progress.setdefault(progress_key, {})[sku] = match
                else:
                    progress.setdefault(progress_key, {})[sku] = None

            # Checkpoint
            save_progress(progress)
            done = batch_start + len(batch)
            matched = sum(1 for v in progress.get(progress_key, {}).values() if v)
            total_checked = len(progress.get(progress_key, {}))
            log.info(f"[{domain}] {done}/{len(work)} done | "
                     f"total checked: {total_checked} | matched: {matched} "
                     f"({100 * matched // max(total_checked, 1)}%)")

            # Small pause between batches to be polite
            await asyncio.sleep(random.uniform(1.0, 2.0))

    return results


# ── Progress ──────────────────────────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {}


def save_progress(progress):
    PROGRESS_FILE.write_text(json.dumps(progress))


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_scrape(competitors, skus_with_products, progress, limit=0):
    """Run the async scraper for all competitors."""
    for comp in competitors:
        domain = comp["domain"]
        progress_key = domain

        already_done = len(progress.get(progress_key, {}))
        if already_done >= len(skus_with_products) and not limit:
            log.info(f"[{domain}] Already complete ({already_done} SKUs). Skipping.")
            continue

        log.info(f"\n{'=' * 60}")
        log.info(f"Scraping: {domain}")
        log.info(f"{'=' * 60}")

        await scrape_shopify_async(domain, skus_with_products, progress_key, progress, limit)

        save_progress(progress)
        matched = sum(1 for v in progress.get(progress_key, {}).values() if v)
        total = len(progress.get(progress_key, {}))
        log.info(f"[{domain}] Complete: {total} checked, {matched} matched "
                 f"({100 * matched // max(total, 1)}%)")


def main():
    parser = argparse.ArgumentParser(description="Scrape competitor prices (async)")
    parser.add_argument("--competitor", type=str, help="Scrape only this competitor domain")
    parser.add_argument("--limit", type=int, default=0, help="Limit SKUs per competitor (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving final output")
    parser.add_argument("--rebuild", action="store_true",
                        help="Skip scraping, just rebuild competitor_prices.json from progress data")
    parser.add_argument("--fresh", action="store_true",
                        help="Clear progress and start fresh (re-scrape everything)")
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

    log.info(f"Total unique SKUs: {len(skus_with_products)}")

    # Filter to requested competitor(s)
    competitors = competitors_config.get("competitors", [])
    if args.competitor:
        competitors = [c for c in competitors if c["domain"] == args.competitor]
        if not competitors:
            log.error(f"Competitor '{args.competitor}' not found in competitors.json")
            return

    log.info(f"Competitors to scrape: {len(competitors)}")

    if not args.rebuild:
        start = time.time()
        asyncio.run(run_scrape(competitors, skus_with_products, progress, args.limit))
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
