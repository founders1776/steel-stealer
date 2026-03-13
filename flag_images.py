#!/usr/bin/env python3
"""
flag_images.py — Flag low-quality product images for manual review.

Analyzes PROCESSED images (2048x2048 with VaM logo background) for:
1. Faint/low-contrast product — product nearly invisible against white background
2. Small product — product is abnormally small (bad extraction or tiny source)
3. Cutoff — rembg clipped the product at the canvas edges

NOTES on what was tested and discarded:
- Watermark overlap on raw images: Tested extensively. rembg removes the
  "www.steelcityvac.com" watermark along with the background in virtually all
  cases, even when the product physically overlaps the watermark zone. Not useful.
- Color saturation check: Flagged products that are genuinely colorful (blue
  covers, yellow filters, orange cords) as "background residue." 100% false
  positive rate — these products really are colored. Not useful.

Outputs:
- flag_results.json — all flagged parts with scores and categories
- review_sheets/review_{category}_{nn}.jpg — contact sheets per category
"""

import json
import os
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ─── Configuration ──────────────────────────────────────────────────────────

PROC_DIR = "images"
OUTPUT_JSON = "flag_results.json"
SHEET_DIR = "review_sheets"

# The VaM logo background is very faint (~240+ per channel). Product pixels
# are anything substantially darker.
PRODUCT_THRESHOLD = 230  # any channel < 230 = product pixel

# Faint product: product pixels exist but are very close to white
FAINT_BRIGHTNESS_THRESHOLD = 200  # mean brightness > 200 = faint product

# Small product: product occupies very little of the canvas
SMALL_PRODUCT_PCT = 0.025  # < 2.5% of canvas area = flag

# Cutoff: product pixels touch the edge of the canvas
EDGE_PROXIMITY_PX = 50

# Contact sheet layout
SHEET_COLS = 6
SHEET_ROWS = 5
THUMB_SIZE = 310
LABEL_HEIGHT = 25
SHEET_PADDING = 10
IMAGES_PER_SHEET = SHEET_COLS * SHEET_ROWS


# ─── Image Analysis ─────────────────────────────────────────────────────────

def analyze_processed_image(proc_path):
    """Analyze a processed image for quality issues."""
    try:
        img = Image.open(proc_path).convert("RGB")
    except Exception:
        return None

    arr = np.array(img)
    h, w = arr.shape[:2]

    # Identify product pixels
    is_product = np.any(arr < PRODUCT_THRESHOLD, axis=2)
    product_count = int(np.count_nonzero(is_product))
    total = h * w
    product_pct = product_count / total

    result = {
        "product_pct": round(product_pct, 4),
        "small_flagged": False,
        "faint_flagged": False,
        "cutoff_flagged": False,
        "edges_touched": [],
        "mean_brightness": 0.0,
    }

    if product_count < 100:
        result["small_flagged"] = True
        return result

    product_pixels = arr[is_product]

    # ── Faint/low-contrast ──
    mean_brightness = float(product_pixels.mean())
    result["mean_brightness"] = round(mean_brightness, 1)
    result["faint_flagged"] = mean_brightness > FAINT_BRIGHTNESS_THRESHOLD

    # ── Small product ──
    result["small_flagged"] = product_pct < SMALL_PRODUCT_PCT

    # ── Cutoff at edges ──
    rows = np.any(is_product, axis=1)
    cols = np.any(is_product, axis=0)
    row_indices = np.where(rows)[0]
    col_indices = np.where(cols)[0]

    if len(row_indices) > 0 and len(col_indices) > 0:
        edges = []
        if row_indices[0] < EDGE_PROXIMITY_PX:
            edges.append("top")
        if row_indices[-1] > h - EDGE_PROXIMITY_PX:
            edges.append("bottom")
        if col_indices[0] < EDGE_PROXIMITY_PX:
            edges.append("left")
        if col_indices[-1] > w - EDGE_PROXIMITY_PX:
            edges.append("right")
        result["edges_touched"] = edges
        result["cutoff_flagged"] = len(edges) > 0

    return result


# ─── Scan All Images ────────────────────────────────────────────────────────

def scan_all():
    """Scan all processed images and return flagged results."""
    proc_files = {}
    for d in os.listdir(PROC_DIR):
        p = os.path.join(PROC_DIR, d, "1.jpg")
        if os.path.isfile(p):
            proc_files[d] = p

    all_parts = sorted(proc_files.keys())
    total = len(all_parts)

    print(f"Scanning {total} processed images...")

    results = {
        "faint": [],
        "small": [],
        "cutoff": [],
        "all_flagged": {},
    }

    start = time.time()
    for i, part in enumerate(all_parts):
        if (i + 1) % 500 == 0:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed
            eta = (total - i - 1) / rate
            print(f"  [{i+1}/{total}] {rate:.0f} parts/sec, ETA {eta:.0f}s")

        analysis = analyze_processed_image(proc_files[part])
        if analysis is None:
            continue

        flags = {}

        if analysis["faint_flagged"]:
            flags["faint"] = {
                "mean_brightness": analysis["mean_brightness"],
                "product_pct": analysis["product_pct"],
            }
            results["faint"].append({
                "part": part,
                "mean_brightness": analysis["mean_brightness"],
                "product_pct": analysis["product_pct"],
            })

        if analysis["small_flagged"]:
            flags["small"] = {
                "product_pct": analysis["product_pct"],
            }
            results["small"].append({
                "part": part,
                "product_pct": analysis["product_pct"],
            })

        if analysis["cutoff_flagged"]:
            flags["cutoff"] = {
                "edges_touched": analysis["edges_touched"],
                "product_pct": analysis["product_pct"],
            }
            results["cutoff"].append({
                "part": part,
                "edges_touched": analysis["edges_touched"],
                "product_pct": analysis["product_pct"],
            })

        if flags:
            results["all_flagged"][part] = flags

    elapsed = time.time() - start
    print(f"\nScan complete in {elapsed:.1f}s")

    # Sort by severity
    results["faint"].sort(key=lambda x: x["mean_brightness"], reverse=True)
    results["small"].sort(key=lambda x: x["product_pct"])
    results["cutoff"].sort(key=lambda x: len(x["edges_touched"]), reverse=True)

    summary = {
        "total_scanned": total,
        "faint_flagged": len(results["faint"]),
        "small_flagged": len(results["small"]),
        "cutoff_flagged": len(results["cutoff"]),
        "total_unique_flagged": len(results["all_flagged"]),
    }
    results["summary"] = summary

    print(f"\n--- Summary ---")
    print(f"Total scanned:       {summary['total_scanned']}")
    print(f"Faint product:       {summary['faint_flagged']}")
    print(f"Small product:       {summary['small_flagged']}")
    print(f"Cutoff at edges:     {summary['cutoff_flagged']}")
    print(f"Total unique flags:  {summary['total_unique_flagged']}")

    return results


# ─── Contact Sheet Generation ──────────────────────────────────────────────

def get_font():
    """Get a font for labels, falling back to default."""
    for path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFCompact.ttf",
    ]:
        try:
            return ImageFont.truetype(path, 14)
        except Exception:
            continue
    return ImageFont.load_default()


def make_contact_sheets(flagged_list, category):
    """Generate contact sheet JPEGs for a list of flagged parts."""
    if not flagged_list:
        print(f"  No {category} flags — skipping contact sheets")
        return

    os.makedirs(SHEET_DIR, exist_ok=True)
    font = get_font()

    cell_w = THUMB_SIZE + SHEET_PADDING
    cell_h = THUMB_SIZE + LABEL_HEIGHT + SHEET_PADDING
    sheet_w = SHEET_COLS * cell_w + SHEET_PADDING
    sheet_h = SHEET_ROWS * cell_h + SHEET_PADDING

    sheet_num = 0
    for batch_start in range(0, len(flagged_list), IMAGES_PER_SHEET):
        batch = flagged_list[batch_start:batch_start + IMAGES_PER_SHEET]
        sheet_num += 1

        sheet = Image.new("RGB", (sheet_w, sheet_h), (255, 255, 255))
        draw = ImageDraw.Draw(sheet)

        for idx, item in enumerate(batch):
            part = item["part"]
            row = idx // SHEET_COLS
            col = idx % SHEET_COLS

            x = SHEET_PADDING + col * cell_w
            y = SHEET_PADDING + row * cell_h

            img_path = os.path.join(PROC_DIR, part, "1.jpg")

            try:
                thumb = Image.open(img_path).convert("RGB")
                thumb.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
                tx = x + (THUMB_SIZE - thumb.width) // 2
                ty = y + (THUMB_SIZE - thumb.height) // 2
                sheet.paste(thumb, (tx, ty))
            except Exception:
                draw.rectangle([x, y, x + THUMB_SIZE, y + THUMB_SIZE],
                               fill=(240, 240, 240), outline=(200, 200, 200))
                draw.text((x + 10, y + THUMB_SIZE // 2), "MISSING",
                          fill=(180, 0, 0), font=font)

            # Build label
            label = part
            if "mean_brightness" in item:
                label += f" (b={item['mean_brightness']:.0f})"
            elif "edges_touched" in item:
                label += f" ({','.join(item['edges_touched'])})"
            elif "product_pct" in item:
                label += f" ({item['product_pct']:.2%})"

            if len(label) > 30:
                label = label[:27] + "..."

            draw.text((x + 5, y + THUMB_SIZE + 2), label,
                      fill=(0, 0, 0), font=font)

        filename = f"review_{category}_{sheet_num:02d}.jpg"
        sheet.save(os.path.join(SHEET_DIR, filename), "JPEG", quality=90)

    print(f"  {category}: {sheet_num} contact sheet(s) → {SHEET_DIR}/")


def generate_all_sheets(results):
    """Generate contact sheets for all flag categories."""
    print("\nGenerating contact sheets...")
    make_contact_sheets(results["faint"], "faint")
    make_contact_sheets(results["small"], "small")
    make_contact_sheets(results["cutoff"], "cutoff")


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    results = scan_all()

    with open(OUTPUT_JSON, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {OUTPUT_JSON}")

    generate_all_sheets(results)

    print(f"\nDone! Review contact sheets in {SHEET_DIR}/ directory.")
    print(f"Total flagged: {results['summary']['total_unique_flagged']} parts")


if __name__ == "__main__":
    main()
