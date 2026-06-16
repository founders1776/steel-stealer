#!/usr/bin/env python3
"""
publish_channels.py — publish a run's created products to all sales channels
EXCEPT Point of Sale.

The main .env Shopify token lacks the read/write_publications scope, so this
uses the publications-scoped token stored in the EVAC SEO project. The token is
read into memory only and never printed or persisted here.

Reads created product ids from a sheet-import-style run dir
(<run-dir>/progress.json "created" = {sku: product_id}); resumable via a
"published" list written back into progress.json.

Usage:
  python3 publish_channels.py --run-dir desco_imports/2026-06-15 --dry-run
  python3 publish_channels.py --run-dir desco_imports/2026-06-15
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

TOKEN_FILE = Path("/Users/jamesfeeney98/Desktop/Projects/EVAC SEO Optimization Run/shopify_token.txt")
SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "")
API_VERSION = "2024-10"
EXCLUDE_CHANNELS = {"point of sale"}   # case-insensitive match on publication name


def load_token():
    if not TOKEN_FILE.exists():
        print(f"ERROR: publications token not found at {TOKEN_FILE}")
        sys.exit(1)
    tok = TOKEN_FILE.read_text().strip()
    if not tok.startswith("shpat_"):
        print("ERROR: token file does not contain a shpat_ token")
        sys.exit(1)
    return tok


def make_gql(token):
    url = f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    def gql(query, variables=None, retries=5):
        for attempt in range(retries):
            resp = requests.post(url, headers=headers,
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
            if data.get("errors"):
                if any("throttl" in str(e).lower() for e in data["errors"]) and attempt < retries - 1:
                    time.sleep((attempt + 1) * 2)
                    continue
            return data
        return None
    return gql


def get_target_publications(gql):
    q = "{ publications(first: 50) { edges { node { id name } } } }"
    data = gql(q)
    pubs = [(e["node"]["id"], e["node"]["name"])
            for e in data["data"]["publications"]["edges"]]
    targets = [(pid, name) for pid, name in pubs
               if name.strip().lower() not in EXCLUDE_CHANNELS]
    excluded = [name for _, name in pubs if name.strip().lower() in EXCLUDE_CHANNELS]
    return targets, excluded, pubs


def publish_product(gql, product_id, pub_ids):
    gid = (product_id if str(product_id).startswith("gid://")
           else f"gid://shopify/Product/{product_id}")
    q = """
    mutation($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        userErrors { field message }
      }
    }"""
    inp = [{"publicationId": p} for p in pub_ids]
    data = gql(q, {"id": gid, "input": inp})
    if not data:
        return ["no response"]
    return data["data"]["publishablePublish"]["userErrors"]


def main():
    ap = argparse.ArgumentParser(description="Publish a run's products to all channels except POS")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not SHOPIFY_STORE:
        print("ERROR: set SHOPIFY_STORE")
        sys.exit(1)

    run_dir = Path(args.run_dir)
    progress_file = run_dir / "progress.json"
    progress = json.loads(progress_file.read_text())
    created = progress.get("created", {})          # sku -> product_id
    if not created:
        print("No created products in this run — nothing to publish.")
        return

    token = load_token()
    gql = make_gql(token)
    targets, excluded, allpubs = get_target_publications(gql)
    print(f"Publications: {[n for _, n in allpubs]}")
    print(f"Publishing to: {[n for _, n in targets]}")
    print(f"Excluded: {excluded}")
    pub_ids = [p for p, _ in targets]

    published = set(progress.get("published", []))
    todo = [(sku, pid) for sku, pid in created.items() if str(pid) not in published]
    print(f"{len(created)} created, {len(published)} already published, {len(todo)} to publish")

    if args.dry_run:
        print("[DRY RUN] no publishing.")
        return

    ok = err = 0
    for i, (sku, pid) in enumerate(todo):
        errs = publish_product(gql, pid, pub_ids)
        if errs:
            err += 1
            print(f"  ERROR {sku} ({pid}): {errs}")
        else:
            ok += 1
            published.add(str(pid))
        if (i + 1) % 100 == 0:
            progress["published"] = sorted(published)
            progress_file.write_text(json.dumps(progress, indent=2))
            print(f"  {i+1}/{len(todo)} — {ok} published, {err} errors")
        time.sleep(0.3)

    progress["published"] = sorted(published)
    progress_file.write_text(json.dumps(progress, indent=2))
    print(f"Done: {ok} published, {err} errors, {len(published)} total on channels.")


if __name__ == "__main__":
    main()
