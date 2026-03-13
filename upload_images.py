#!/usr/bin/env python3
"""
Upload product images to Shopify Files API.

Reads from images/{sku}/1.jpg, uploads via staged upload flow,
saves CDN URLs to image_urls.json (resumable with checkpointing).
"""

import json
import os
import sys
import time

import requests

# ── Config ────────────────────────────────────────────────────────────────────

IMAGES_DIR = "images"
URLS_FILE = "image_urls.json"
CHECKPOINT_EVERY = 25

# Load from .env file
def load_env():
    if os.path.exists(".env"):
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())

load_env()

SHOP = os.environ.get("SHOPIFY_STORE", "")
TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
API_VERSION = "2024-10"


# ── Shopify API ───────────────────────────────────────────────────────────────

def graphql(query, variables=None):
    """Execute Shopify Admin GraphQL query."""
    url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()


def upload_single_image(filepath, filename):
    """Upload one image to Shopify Files. Returns CDN URL or None."""

    # Step 1: Create staged upload target
    result = graphql("""
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets {
          url
          resourceUrl
          parameters { name value }
        }
        userErrors { field message }
      }
    }
    """, {
        "input": [{
            "resource": "FILE",
            "filename": filename,
            "mimeType": "image/jpeg",
            "httpMethod": "POST",
        }]
    })

    targets = result.get("data", {}).get("stagedUploadsCreate", {}).get("stagedTargets", [])
    if not targets:
        errors = result.get("data", {}).get("stagedUploadsCreate", {}).get("userErrors", [])
        print(f" staged error: {errors}")
        return None

    target = targets[0]
    params = {p["name"]: p["value"] for p in target["parameters"]}

    # Step 2: Upload file to staged URL
    with open(filepath, "rb") as f:
        resp = requests.post(
            target["url"],
            data=params,
            files={"file": (filename, f, "image/jpeg")},
            timeout=120,
        )

    if resp.status_code not in (200, 201, 204):
        print(f" upload failed: HTTP {resp.status_code}")
        return None

    # Step 3: Register file in Shopify
    result = graphql("""
    mutation fileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          ... on MediaImage {
            id
            image { url }
          }
        }
        userErrors { field message }
      }
    }
    """, {
        "files": [{
            "originalSource": target["resourceUrl"],
            "contentType": "IMAGE",
        }]
    })

    files_created = result.get("data", {}).get("fileCreate", {}).get("files", [])
    if files_created and files_created[0].get("image", {}).get("url"):
        return files_created[0]["image"]["url"]

    # Image processing may be async — return resource URL as fallback
    return target["resourceUrl"]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not SHOP or not TOKEN:
        print("ERROR: Missing SHOPIFY_STORE or SHOPIFY_ACCESS_TOKEN in .env")
        sys.exit(1)

    # Test connection
    print(f"Store: {SHOP}")
    try:
        test = graphql("{ shop { name } }")
        shop_name = test.get("data", {}).get("shop", {}).get("name", "?")
        print(f"Connected: {shop_name}")
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    # Load existing progress
    url_map = {}
    if os.path.exists(URLS_FILE):
        with open(URLS_FILE) as f:
            url_map = json.load(f)
    print(f"Already uploaded: {len(url_map)} SKUs")

    # Build upload queue — only SKUs not yet uploaded
    queue = []
    for folder in sorted(os.listdir(IMAGES_DIR)):
        folder_path = os.path.join(IMAGES_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        if folder in url_map:
            continue
        imgs = sorted(f for f in os.listdir(folder_path) if f.endswith(".jpg"))
        if imgs:
            queue.append((folder, folder_path, imgs))

    total = len(queue)
    print(f"To upload: {total} SKUs\n")

    if total == 0:
        print("Nothing to upload!")
        return

    errors = 0
    for i, (sku, folder_path, imgs) in enumerate(queue):
        print(f"[{i+1}/{total}] {sku} ({len(imgs)} img)...", end=" ", flush=True)

        urls = []
        for img_file in imgs:
            filepath = os.path.join(folder_path, img_file)
            filename = f"{sku}_{img_file}"

            try:
                url = upload_single_image(filepath, filename)
                if url:
                    urls.append(url)
                else:
                    errors += 1
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    # Rate limited — wait and retry
                    retry_after = int(e.response.headers.get("Retry-After", 2))
                    print(f"rate limited, waiting {retry_after}s...", end=" ", flush=True)
                    time.sleep(retry_after)
                    try:
                        url = upload_single_image(filepath, filename)
                        if url:
                            urls.append(url)
                        else:
                            errors += 1
                    except Exception:
                        errors += 1
                        print(f"retry failed", end=" ", flush=True)
                else:
                    errors += 1
                    print(f"HTTP {e.response.status_code if e.response else '?'}", end=" ", flush=True)
            except Exception as e:
                errors += 1
                print(f"error: {e}", end=" ", flush=True)

            time.sleep(0.3)  # Rate limit buffer

        url_map[sku] = urls
        status = f"✓ {len(urls)}" if urls else "✗ no urls"
        print(status)

        # Checkpoint
        if (i + 1) % CHECKPOINT_EVERY == 0:
            with open(URLS_FILE, "w") as f:
                json.dump(url_map, f, indent=2)
            print(f"  [checkpoint: {len(url_map)} saved]")

    # Final save
    with open(URLS_FILE, "w") as f:
        json.dump(url_map, f, indent=2)

    uploaded = sum(1 for v in url_map.values() if v)
    total_urls = sum(len(v) for v in url_map.values())
    print(f"\nDone! {uploaded} SKUs with images ({total_urls} total URLs)")
    print(f"Errors: {errors}")
    print(f"Saved to {URLS_FILE}")


if __name__ == "__main__":
    main()
