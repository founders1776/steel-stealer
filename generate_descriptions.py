#!/usr/bin/env python3
"""
generate_descriptions.py
Template-based product description generator for Shopify listings.
Reads product_names.json, detects category, generates 1-3 sentence descriptions,
and writes the 'description' field back.
"""

import json
import re
import random

random.seed(42)

INPUT_FILE = "product_names.json"

# ---------------------------------------------------------------------------
# Category detection — order matters: more specific patterns first
# ---------------------------------------------------------------------------

CATEGORY_PATTERNS = [
    # Bags
    ("bag_paper", re.compile(r"\bpaper\s*bag", re.I)),
    ("bag_cloth", re.compile(r"\bcloth\s*bag", re.I)),
    ("bag", re.compile(r"\bbags?\b", re.I)),
    # Filters
    ("filter_hepa", re.compile(r"\bhepa\b.*\bfilter\b|\bfilter\b.*\bhepa\b", re.I)),
    ("filter_foam", re.compile(r"\bfoam\b.*\bfilter\b|\bfilter\b.*\bfoam\b", re.I)),
    ("filter_exhaust", re.compile(r"\bexhaust\b.*\bfilter\b|\bfilter\b.*\bexhaust\b", re.I)),
    ("filter_pre_motor", re.compile(r"\bpre[\s-]?motor\b.*\bfilter\b|\bfilter\b.*\bpre[\s-]?motor\b", re.I)),
    ("filter", re.compile(r"\bfilters?\b", re.I)),
    # Belts
    ("belt_geared", re.compile(r"\bgeared\b.*\bbelt\b|\bbelt\b.*\bgeared\b", re.I)),
    ("belt_cogged", re.compile(r"\bcogged\b.*\bbelt\b|\bbelt\b.*\bcogged\b", re.I)),
    ("belt", re.compile(r"\bbelts?\b", re.I)),
    # Brush / Brush Roll
    ("carbon_brush", re.compile(r"\bcarbon\s*brush", re.I)),
    ("brush_roll", re.compile(r"\bbrush\s*roll\b|\broller\s*brush\b|\bagitator\b", re.I)),
    ("brush_strip", re.compile(r"\bbrush\s*strip", re.I)),
    ("brush", re.compile(r"\bbrush\b", re.I)),
    # Motors
    ("motor", re.compile(r"\bmotor\b", re.I)),
    # Hoses
    ("hose", re.compile(r"\bhose\b", re.I)),
    # Cords
    ("cord", re.compile(r"\bcords?\b|\bpower\s*cord\b", re.I)),
    # Wheels / Axles
    ("wheel", re.compile(r"\bwheels?\b|\bcastors?\b|\bcasters?\b", re.I)),
    ("axle", re.compile(r"\baxle\b", re.I)),
    # Switches
    ("switch", re.compile(r"\bswitch\b", re.I)),
    # Handles
    ("handle", re.compile(r"\bhandle\b", re.I)),
    # Nozzles
    ("nozzle", re.compile(r"\bnozzle\b", re.I)),
    # Fans
    ("fan", re.compile(r"\bfan\b", re.I)),
    # Springs
    ("spring", re.compile(r"\bspring\b", re.I)),
    # Screws / Hardware
    ("hardware", re.compile(r"\bscrews?\b|\bnuts?\b|\bbolts?\b|\brivets?\b|\bwashers?\b|\bhardware\b", re.I)),
    # Dust cup / bin
    ("dust_cup", re.compile(r"\bdust\s*(cup|bin|container)\b", re.I)),
    # Wands
    ("wand", re.compile(r"\bwands?\b", re.I)),
    # Bearings
    ("bearing", re.compile(r"\bbearings?\b", re.I)),
    # Gaskets / Seals
    ("gasket", re.compile(r"\bgaskets?\b|\bseals?\b|\bo[\s-]?ring\b", re.I)),
    # Bumpers
    ("bumper", re.compile(r"\bbumpers?\b", re.I)),
    # Attachments / Tools
    ("attachment", re.compile(r"\battachment\b|\bcrevice\s*tool\b|\bupholstery\s*tool\b|\bdusting\s*brush\b|\bfloor\s*tool\b", re.I)),
    # Plates / covers / housings
    ("cover", re.compile(r"\bcover\b|\bplate\b|\bhousing\b|\bshroud\b", re.I)),
    # Pedals
    ("pedal", re.compile(r"\bpedal\b", re.I)),
    # Latch / Catch
    ("latch", re.compile(r"\blatch\b|\bcatch\b|\bclasp\b|\block\b", re.I)),
    # Valves
    ("valve", re.compile(r"\bvalve\b", re.I)),
    # Casters
    ("caster", re.compile(r"\bcaster\b", re.I)),
    # Power head / power nozzle (catch-all after nozzle)
    ("power_head", re.compile(r"\bpower\s*(head|nozzle)\b", re.I)),
    # Complete machine (catch if it looks like a full unit, not a part)
    ("machine", re.compile(r"\b(vacuum|cleaner|steamer|extractor|spot\s*cleaner)\b(?!.*\b(filter|belt|bag|hose|cord|motor|brush|wheel|switch|handle|nozzle|fan|spring|screw|wand|bearing|gasket|bumper|attachment|cover|plate|pedal|latch|valve)\b)", re.I)),
]


def detect_category(clean_name: str) -> str:
    for cat, pattern in CATEGORY_PATTERNS:
        if pattern.search(clean_name):
            return cat
    return "generic"


# ---------------------------------------------------------------------------
# Attribute extraction helpers
# ---------------------------------------------------------------------------

def extract_models(product: dict) -> str:
    """Return a friendly model string from the clean_name or model field."""
    model = product.get("model", "").strip()
    if model:
        return model
    return ""


def extract_quantity(clean_name: str) -> str:
    """Look for pack quantities like '3 Pack', '10 Pack', '2-Pack'."""
    m = re.search(r"(\d+)\s*[-]?\s*pack", clean_name, re.I)
    if m:
        return m.group(1)
    return ""


def extract_length(clean_name: str) -> str:
    """Look for length specs like 20'6'', 30 feet, 13 1/8 inches."""
    m = re.search(r"(\d+['\u2019]\d*[\"'\u2019]*\s*(?:long)?)", clean_name, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"(\d+\s*(?:1/\d+\s*)?(?:inch(?:es)?|feet|foot|ft))", clean_name, re.I)
    if m:
        return m.group(1).strip()
    return ""


def extract_color(clean_name: str) -> str:
    colors = ["black", "white", "gray", "grey", "red", "blue", "green", "yellow",
              "orange", "purple", "pink", "silver", "clear", "brown", "tan", "beige"]
    name_lower = clean_name.lower()
    found = [c for c in colors if re.search(rf"\b{c}\b", name_lower)]
    return found[0].title() if found else ""


def brand_display(brand_str: str) -> str:
    """Pick the first brand for display (some have 'Advance,Nilfisk,Clarke')."""
    if not brand_str or not brand_str.strip():
        return ""
    return brand_str.split(",")[0].strip()


def compat_phrase(brand: str, model: str, clean_name: str = "") -> str:
    """Build 'compatible with {brand} {model}' phrase. Never say 'select vacuum models'."""
    parts = []
    if brand:
        parts.append(brand)
    if model:
        parts.append(model)
    if parts:
        return f"compatible with {' '.join(parts)} vacuum models"
    # No brand/model — try to extract something useful from clean_name
    if clean_name:
        return f"for {clean_name}"
    return "for your vacuum"


# ---------------------------------------------------------------------------
# Description templates by category
# ---------------------------------------------------------------------------

TEMPLATES = {
    "filter_hepa": [
        lambda b, m, **kw: f"Replacement HEPA filter {compat_phrase(b, m)}. Captures fine dust, allergens, and microscopic particles for cleaner air output.",
        lambda b, m, **kw: f"Genuine replacement HEPA filter designed for {b + ' ' if b else ''}{m + ' ' if m else ''}vacuums. Traps 99.97% of particles for improved indoor air quality.",
    ],
    "filter_foam": [
        lambda b, m, **kw: f"Replacement foam filter {compat_phrase(b, m)}. Washable foam design traps fine particles and helps maintain strong suction performance.",
        lambda b, m, **kw: f"Foam pre-filter designed for {b + ' ' if b else ''}{m + ' ' if m else ''}vacuums. Captures dust and debris to protect the motor and extend vacuum life.",
    ],
    "filter_exhaust": [
        lambda b, m, **kw: f"Replacement exhaust filter {compat_phrase(b, m)}. Filters outgoing air to reduce dust recirculation and keep your home cleaner.{' Sold in ' + kw.get('qty','') + '-pack.' if kw.get('qty') else ''}",
    ],
    "filter_pre_motor": [
        lambda b, m, **kw: f"Replacement pre-motor filter {compat_phrase(b, m)}. Protects the motor from dust and debris, helping extend the life of your vacuum.",
    ],
    "filter": [
        lambda b, m, **kw: f"Replacement filter {compat_phrase(b, m)}. Helps maintain optimal suction and air filtration for effective cleaning performance.",
        lambda b, m, **kw: f"Genuine replacement filter for {b + ' ' if b else ''}{m + ' ' if m else ''}vacuums. Keeps airflow strong and traps dust for cleaner exhaust.",
    ],
    "bag_paper": [
        lambda b, m, **kw: f"Replacement paper vacuum bags {compat_phrase(b, m)}. Disposable design makes for quick, hygienic dust disposal.{' Pack of ' + kw.get('qty','') + '.' if kw.get('qty') else ''}",
    ],
    "bag_cloth": [
        lambda b, m, **kw: f"Reusable cloth vacuum bag {compat_phrase(b, m)}. Durable cloth construction can be emptied and reused, reducing ongoing replacement costs.",
    ],
    "bag": [
        lambda b, m, **kw: f"Replacement vacuum bags {compat_phrase(b, m)}. Designed for a secure fit to maximize dust capture and maintain suction.{' Pack of ' + kw.get('qty','') + '.' if kw.get('qty') else ''}",
        lambda b, m, **kw: f"Genuine replacement bags for {b + ' ' if b else ''}{m + ' ' if m else ''}vacuums. Easy to install for quick, mess-free disposal.{' Pack of ' + kw.get('qty','') + '.' if kw.get('qty') else ''}",
    ],
    "belt_geared": [
        lambda b, m, **kw: f"Replacement geared belt {compat_phrase(b, m)}. Geared design provides consistent brush roll speed for reliable carpet agitation.",
    ],
    "belt_cogged": [
        lambda b, m, **kw: f"Replacement cogged belt {compat_phrase(b, m)}. Cogged teeth prevent slipping for consistent brush roll performance and better cleaning results.",
    ],
    "belt": [
        lambda b, m, **kw: f"Replacement drive belt {compat_phrase(b, m)}. Restores proper brush roll spin for effective carpet cleaning and debris pickup.",
        lambda b, m, **kw: f"Genuine replacement belt for {b + ' ' if b else ''}{m + ' ' if m else ''}vacuums. Keeps the brush roll turning at the correct speed for optimal performance.",
    ],
    "carbon_brush": [
        lambda b, m, **kw: f"Replacement carbon motor brushes {compat_phrase(b, m)}. Essential for maintaining electrical contact within the motor for reliable operation.{' Pack of ' + kw.get('qty','') + '.' if kw.get('qty') else ''}",
    ],
    "brush_roll": [
        lambda b, m, **kw: f"Replacement brush roll {compat_phrase(b, m)}. Agitates carpet fibers to loosen dirt and debris for deeper cleaning.{' ' + kw.get('length','') + ' length.' if kw.get('length') else ''}",
        lambda b, m, **kw: f"Genuine replacement brush roll for {b + ' ' if b else ''}{m + ' ' if m else ''}vacuums. Restores agitation performance for effective carpet and floor cleaning.",
    ],
    "brush_strip": [
        lambda b, m, **kw: f"Replacement brush strip {compat_phrase(b, m)}. Attaches to the brush roll to sweep and agitate carpet fibers during cleaning.",
    ],
    "brush": [
        lambda b, m, **kw: f"Replacement brush {compat_phrase(b, m)}. Maintains effective sweeping and agitation for thorough cleaning results.",
    ],
    "motor": [
        lambda b, m, **kw: f"Replacement motor assembly {compat_phrase(b, m)}. Restores full suction power and performance to your vacuum.",
        lambda b, m, **kw: f"Genuine replacement motor for {b + ' ' if b else ''}{m + ' ' if m else ''}vacuums. Provides the suction power needed for effective cleaning on all surfaces.",
    ],
    "hose": [
        lambda b, m, **kw: f"Replacement hose {compat_phrase(b, m)}. Restores strong suction and flexible reach for above-floor cleaning tasks.",
        lambda b, m, **kw: f"Genuine replacement hose for {b + ' ' if b else ''}{m + ' ' if m else ''}vacuums. Provides a secure, airtight connection for maximum suction performance.",
    ],
    "cord": [
        lambda b, m, **kw: f"Replacement power cord {compat_phrase(b, m)}.{' ' + kw.get('length','') + ' length provides' if kw.get('length') else ' Provides'} extended reach for larger cleaning areas without switching outlets.",
    ],
    "wheel": [
        lambda b, m, **kw: f"Replacement wheel {compat_phrase(b, m)}. Restores smooth rolling and easy maneuverability across floors and carpets.",
    ],
    "axle": [
        lambda b, m, **kw: f"Replacement axle {compat_phrase(b, m)}. Ensures smooth, stable wheel rotation for easy vacuum movement.",
    ],
    "switch": [
        lambda b, m, **kw: f"Replacement switch {compat_phrase(b, m)}. Restores reliable on/off or speed control functionality to your vacuum.",
    ],
    "handle": [
        lambda b, m, **kw: f"Replacement handle assembly {compat_phrase(b, m)}. Restores comfortable grip and full control while vacuuming.",
    ],
    "nozzle": [
        lambda b, m, **kw: f"Replacement nozzle assembly {compat_phrase(b, m)}. Provides effective suction contact with floors for optimal dirt pickup.",
    ],
    "fan": [
        lambda b, m, **kw: f"Replacement fan {compat_phrase(b, m)}. Restores proper airflow and suction power for effective cleaning performance.",
    ],
    "spring": [
        lambda b, m, **kw: f"Replacement spring {compat_phrase(b, m)}. Restores proper tension and mechanical function to your vacuum.",
    ],
    "hardware": [
        lambda b, m, **kw: f"Replacement hardware {compat_phrase(b, m)}. Ensures a secure, factory-spec fit for reliable vacuum assembly.{' Pack of ' + kw.get('qty','') + '.' if kw.get('qty') else ''}",
    ],
    "dust_cup": [
        lambda b, m, **kw: f"Replacement dust cup {compat_phrase(b, m)}. Easy-empty design for quick, hygienic disposal of collected dirt and debris.",
    ],
    "wand": [
        lambda b, m, **kw: f"Replacement wand {compat_phrase(b, m)}. Extends your reach for cleaning above-floor surfaces, ceilings, and tight spaces.",
    ],
    "bearing": [
        lambda b, m, **kw: f"Replacement bearing {compat_phrase(b, m)}. Ensures smooth, quiet rotation of moving parts for reliable vacuum operation.",
    ],
    "gasket": [
        lambda b, m, **kw: f"Replacement gasket/seal {compat_phrase(b, m)}. Provides an airtight seal to maintain strong suction and prevent air leaks.",
    ],
    "bumper": [
        lambda b, m, **kw: f"Replacement bumper {compat_phrase(b, m)}. Protects furniture and baseboards from scuffs and scratches during vacuuming.",
    ],
    "attachment": [
        lambda b, m, **kw: f"Replacement attachment tool {compat_phrase(b, m)}. Extends your vacuum's versatility for cleaning upholstery, crevices, and hard-to-reach areas.",
    ],
    "cover": [
        lambda b, m, **kw: f"Replacement cover/plate {compat_phrase(b, m)}. Restores a secure, factory-fit closure for proper vacuum operation.",
    ],
    "pedal": [
        lambda b, m, **kw: f"Replacement pedal {compat_phrase(b, m)}. Restores proper foot-operated control for easy height adjustment or drive engagement.",
    ],
    "latch": [
        lambda b, m, **kw: f"Replacement latch/catch {compat_phrase(b, m)}. Ensures a secure closure for reliable vacuum operation during use.",
    ],
    "valve": [
        lambda b, m, **kw: f"Replacement valve {compat_phrase(b, m)}. Restores proper airflow control for consistent suction performance.",
    ],
    "power_head": [
        lambda b, m, **kw: f"Replacement power head {compat_phrase(b, m)}. Features a motorized brush roll for deep carpet cleaning and effective debris pickup.",
    ],
    "machine": [
        lambda b, m, **kw: f"{b + ' ' if b else ''}{m + ' ' if m else ''}vacuum cleaner. Delivers powerful suction and reliable performance for thorough home or commercial cleaning.",
    ],
    "generic": [
        lambda b, m, **kw: f"Replacement part {compat_phrase(b, m)}. Restores your vacuum to optimal working condition with a factory-spec fit.",
        lambda b, m, **kw: f"Genuine replacement component for {b + ' ' if b else ''}{m + ' ' if m else ''}vacuums. Designed for a precise fit and reliable performance.",
    ],
}


def generate_description(product: dict) -> str:
    clean_name = product.get("clean_name", "")
    brand = brand_display(product.get("brand", ""))
    model = extract_models(product)
    qty = extract_quantity(clean_name)
    length = extract_length(clean_name)
    color = extract_color(clean_name)

    category = detect_category(clean_name)
    templates = TEMPLATES.get(category, TEMPLATES["generic"])
    template_fn = random.choice(templates)

    desc = template_fn(brand, model, qty=qty, length=length, color=color)

    # Clean up any double spaces or trailing issues
    desc = re.sub(r"  +", " ", desc).strip()
    return desc


def main():
    with open(INPUT_FILE, "r") as f:
        products = json.load(f)

    print(f"Loaded {len(products)} products")

    # Category distribution for reporting
    cat_counts = {}
    empty_count = 0

    for key, product in products.items():
        cat = detect_category(product.get("clean_name", ""))
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

        desc = generate_description(product)
        product["description"] = desc

        if not desc or desc == product.get("clean_name", ""):
            empty_count += 1

    # Write back
    with open(INPUT_FILE, "w") as f:
        json.dump(products, f, indent=2)

    print(f"\nDescriptions generated for {len(products)} products")
    print(f"Empty/duplicate descriptions: {empty_count}")
    print(f"\nCategory distribution:")
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:20s}: {count:5d}")

    # Sample 20 random descriptions
    print("\n--- Sample Descriptions ---")
    keys = list(products.keys())
    random.seed(99)
    for k in random.sample(keys, 20):
        p = products[k]
        print(f"\n  Name:  {p['clean_name']}")
        print(f"  Desc:  {p['description']}")
        print(f"  Chars: {len(p['description'])}")


if __name__ == "__main__":
    main()
