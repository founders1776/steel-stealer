#!/usr/bin/env python3
"""Merge agent-generated descriptions back into product_names.json."""

import json
import glob
from pathlib import Path

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "product_names.json"

def main():
    with open(PRODUCTS_FILE) as f:
        products = json.load(f)

    total_updated = 0
    names_fixed = 0
    brands_fixed = 0

    # Read all temp_output_*.json files
    output_files = sorted(glob.glob(str(BASE_DIR / "temp_output_*.json")))
    print(f"Found {len(output_files)} output chunks")

    for fpath in output_files:
        with open(fpath) as f:
            updates = json.load(f)

        for key, update in updates.items():
            if key not in products:
                print(f"  WARNING: key {key} not found in products")
                continue

            if "description" in update and update["description"]:
                products[key]["description"] = update["description"]
                total_updated += 1

            if "clean_name" in update and update["clean_name"]:
                old = products[key].get("clean_name", "")
                if update["clean_name"] != old:
                    products[key]["clean_name"] = update["clean_name"]
                    names_fixed += 1

            if "brand" in update and update["brand"]:
                old = products[key].get("brand", "")
                if not old.strip() and update["brand"].strip():
                    products[key]["brand"] = update["brand"]
                    brands_fixed += 1

    print(f"\nResults:")
    print(f"  Descriptions updated: {total_updated}")
    print(f"  Names fixed: {names_fixed}")
    print(f"  Brands filled: {brands_fixed}")

    # Save
    with open(PRODUCTS_FILE, "w") as f:
        json.dump(products, f, indent=2)
    print(f"\nSaved {PRODUCTS_FILE}")

    # Also re-export spreadsheet
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Products"
        headers = ["SKU", "Brand", "Model", "Clean Name", "Description", "Price", "Retail Price", "In Stock", "Raw Name"]
        ws.append(headers)
        for key, p in products.items():
            ws.append([
                p.get("sku", key),
                p.get("brand", ""),
                p.get("model", ""),
                p.get("clean_name", ""),
                p.get("description", ""),
                p.get("price", ""),
                p.get("retail_price", ""),
                p.get("in_stock", ""),
                p.get("raw_name", ""),
            ])
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)
        out_path = BASE_DIR / "output" / "product_descriptions.xlsx"
        wb.save(str(out_path))
        print(f"Spreadsheet exported to {out_path}")
    except ImportError:
        print("openpyxl not available, skipping spreadsheet export")

    # Show samples
    import random
    random.seed(42)
    sample_keys = random.sample(list(products.keys()), min(20, len(products)))
    print("\n--- Sample Descriptions ---")
    for k in sample_keys:
        p = products[k]
        print(f"\n  Name:  {p['clean_name']}")
        print(f"  Brand: {p.get('brand', '')}")
        print(f"  Desc:  {p['description']}")


if __name__ == "__main__":
    main()
