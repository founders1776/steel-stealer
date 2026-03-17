#!/usr/bin/env python3
"""
Phase 4b: Scrape competitor prices for all products.

Supports two scraper types:
  - Shopify sites: Uses /search/suggest.json API (proven pattern from ezvacuum_cross_ref.py)
  - Non-Shopify sites: Uses Playwright + playwright-stealth with per-site CSS selectors

Usage:
  python3 scrape_competitor_prices.py                           # Full run (all competitors)
  python3 scrape_competitor_prices.py --competitor ezvacuum.com  # Single competitor
  python3 scrape_competitor_prices.py --limit 50                # Test with 50 SKUs
  python3 scrape_competitor_prices.py --dry-run                 # Preview without saving
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

import requests

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


# ── SKU Matching ────────────────────────────────────────────────────────────

def normalize_sku(sku):
    """Strip dashes, spaces, dots for comparison."""
    return re.sub(r'[\-\s\.]', '', sku).lower()


def sku_matches(sku, text):
    """Check if SKU appears in text (exact or normalized)."""
    sku_lower = sku.lower()
    text_lower = text.lower()
    if sku_lower in text_lower:
        return True
    return normalize_sku(sku) in normalize_sku(text)


def parse_price_str(price_str):
    """Extract numeric price from string like '$12.99' or '12.99'."""
    if not price_str:
        return None
    match = re.search(r'[\d]+\.?\d*', str(price_str))
    return float(match.group()) if match else None


# ── Shopify Scraper ─────────────────────────────────────────────────────────

def scrape_shopify_site(domain, skus_with_products, progress_key, progress, limit=0):
    """Scrape a Shopify-based competitor using their suggest API."""
    base_url = f"https://{domain}"
    suggest_url = f"{base_url}/search/suggest.json"
    results = {}
    work = [(sku, prod) for sku, prod in skus_with_products if sku not in progress.get(progress_key, {})]
    if limit:
        work = work[:limit]

    log.info(f"[{domain}] Shopify scraper: {len(work)} SKUs to check")

    for i, (sku, product) in enumerate(work):
        try:
            params = {
                "q": sku,
                "resources[type]": "product",
                "resources[limit]": 5,
            }
            resp = requests.get(suggest_url, params=params, headers=HEADERS, timeout=15)
            if resp.status_code == 429:
                log.warning(f"[{domain}] Rate limited at SKU {sku}, waiting 30s...")
                time.sleep(30)
                resp = requests.get(suggest_url, params=params, headers=HEADERS, timeout=15)

            if resp.status_code != 200:
                progress.setdefault(progress_key, {})[sku] = None
                continue

            data = resp.json()
            products_found = data.get("resources", {}).get("results", {}).get("products", [])

            match = None
            for p in products_found:
                title = p.get("title", "")
                url = p.get("url", "")
                if sku_matches(sku, title) or sku_matches(sku, url):
                    # Get full product JSON for accurate price
                    product_url = f"{base_url}{url}.json"
                    try:
                        prod_resp = requests.get(product_url, headers=HEADERS, timeout=15)
                        if prod_resp.status_code == 200:
                            prod_data = prod_resp.json().get("product", {})
                            variants = prod_data.get("variants", [])
                            if variants:
                                price = parse_price_str(variants[0].get("price"))
                                if price and price > 0:
                                    match = {
                                        "price": price,
                                        "url": f"{base_url}{url}",
                                        "title": title,
                                    }
                                    break
                    except Exception:
                        pass

            if match:
                results[sku] = match
                progress.setdefault(progress_key, {})[sku] = match
            else:
                progress.setdefault(progress_key, {})[sku] = None

        except Exception as e:
            log.warning(f"[{domain}] Error for {sku}: {e}")
            progress.setdefault(progress_key, {})[sku] = None

        if (i + 1) % 50 == 0:
            matched = sum(1 for v in progress.get(progress_key, {}).values() if v)
            total = len(progress.get(progress_key, {}))
            log.info(f"[{domain}] {total}/{len(work) + (total - len(results))} | "
                     f"Matched: {matched} ({100*matched//max(total,1)}%)")

        # Checkpoint every 100
        if (i + 1) % 100 == 0:
            save_progress(progress)

        time.sleep(random.uniform(0.5, 1.0))

    return results


# ── Playwright Scraper (non-Shopify) ────────────────────────────────────────

async def scrape_custom_site(domain, selectors, skus_with_products, progress_key, progress, limit=0):
    """Scrape a non-Shopify site using Playwright with per-site CSS selectors."""
    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async
    except ImportError:
        log.error("Playwright not available. Install with: pip install playwright playwright-stealth")
        return {}

    results = {}
    work = [(sku, prod) for sku, prod in skus_with_products if sku not in progress.get(progress_key, {})]
    if limit:
        work = work[:limit]

    log.info(f"[{domain}] Playwright scraper: {len(work)} SKUs to check")

    search_url_template = selectors.get("search_url", f"https://{domain}/search?q={{sku}}")
    price_selector = selectors.get("price_selector", ".price")
    title_selector = selectors.get("title_selector", "h1")
    result_selector = selectors.get("result_selector", ".product-item a")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        await stealth_async(page)

        for i, (sku, product) in enumerate(work):
            try:
                search_url = search_url_template.replace("{sku}", quote(sku))
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)

                # Find product link in search results
                result_links = await page.query_selector_all(result_selector)
                product_url = None
                for link in result_links[:5]:
                    text = await link.text_content() or ""
                    href = await link.get_attribute("href") or ""
                    if sku_matches(sku, text) or sku_matches(sku, href):
                        product_url = href if href.startswith("http") else f"https://{domain}{href}"
                        break

                if not product_url:
                    progress.setdefault(progress_key, {})[sku] = None
                    continue

                # Visit product page and extract price
                await page.goto(product_url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1500)

                price_el = await page.query_selector(price_selector)
                if price_el:
                    price_text = await price_el.text_content()
                    price = parse_price_str(price_text)
                    if price and price > 0:
                        title_el = await page.query_selector(title_selector)
                        title = await title_el.text_content() if title_el else ""
                        results[sku] = {
                            "price": price,
                            "url": product_url,
                            "title": title.strip(),
                        }
                        progress.setdefault(progress_key, {})[sku] = results[sku]
                    else:
                        progress.setdefault(progress_key, {})[sku] = None
                else:
                    progress.setdefault(progress_key, {})[sku] = None

            except Exception as e:
                log.warning(f"[{domain}] Error for {sku}: {e}")
                progress.setdefault(progress_key, {})[sku] = None

            if (i + 1) % 50 == 0:
                matched = sum(1 for v in progress.get(progress_key, {}).values() if v)
                total = len(progress.get(progress_key, {}))
                log.info(f"[{domain}] {total}/{len(work)} | Matched: {matched}")

            if (i + 1) % 100 == 0:
                save_progress(progress)

            await page.wait_for_timeout(random.randint(800, 1500))

        await browser.close()

    return results


# ── Progress ────────────────────────────────────────────────────────────────

def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {}


def save_progress(progress):
    PROGRESS_FILE.write_text(json.dumps(progress))


# ── Outlier Filter ──────────────────────────────────────────────────────────

def filter_outlier_prices(all_prices):
    """Remove prices that are >3x the median for the same SKU."""
    cleaned = {}
    for sku, competitors in all_prices.items():
        prices = [c["price"] for c in competitors.values() if c and c.get("price")]
        if len(prices) < 2:
            cleaned[sku] = competitors
            continue
        median = statistics.median(prices)
        threshold = median * 3
        cleaned[sku] = {
            domain: data for domain, data in competitors.items()
            if not data or not data.get("price") or data["price"] <= threshold
        }
    return cleaned


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape competitor prices")
    parser.add_argument("--competitor", type=str, help="Scrape only this competitor domain")
    parser.add_argument("--limit", type=int, default=0, help="Limit SKUs per competitor (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving final output")
    args = parser.parse_args()

    if not COMPETITORS_FILE.exists():
        log.error(f"{COMPETITORS_FILE} not found. Run discover_competitors.py first, "
                  "then create competitors.json with your selected sites.")
        return

    products = json.loads(PRODUCTS_FILE.read_text())
    competitors_config = json.loads(COMPETITORS_FILE.read_text())
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

    # Scrape each competitor
    for comp in competitors:
        domain = comp["domain"]
        comp_type = comp.get("type", "shopify")
        progress_key = domain

        already_done = len(progress.get(progress_key, {}))
        if already_done >= len(skus_with_products) and not args.limit:
            log.info(f"[{domain}] Already complete ({already_done} SKUs). Skipping.")
            continue

        log.info(f"\n{'='*60}")
        log.info(f"Scraping: {domain} (type: {comp_type})")
        log.info(f"{'='*60}")

        if comp_type == "shopify":
            scrape_shopify_site(domain, skus_with_products, progress_key, progress, args.limit)
        elif comp_type == "custom":
            selectors = comp.get("selectors", {})
            asyncio.run(scrape_custom_site(
                domain, selectors, skus_with_products, progress_key, progress, args.limit
            ))
        else:
            log.warning(f"[{domain}] Unknown type '{comp_type}', skipping")
            continue

        save_progress(progress)
        matched = sum(1 for v in progress.get(progress_key, {}).values() if v)
        total = len(progress.get(progress_key, {}))
        log.info(f"[{domain}] Complete: {total} checked, {matched} matched "
                 f"({100*matched//max(total,1)}%)")

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
                }

    # Filter outliers
    all_prices = filter_outlier_prices(all_prices)

    # Build output with stats
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
