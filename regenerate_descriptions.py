#!/usr/bin/env python3
"""
regenerate_descriptions.py — Fix and regenerate ALL product descriptions.

Fixes:
1. Products with SKU-only clean_names — re-cleans using API description field
2. Never says "select vacuum models" — uses specific product/model info instead
3. Includes model numbers from the title in descriptions
4. Better descriptions for products without brand/model info

Usage:
  python3 regenerate_descriptions.py          # Fix names + regenerate all descriptions
  python3 regenerate_descriptions.py --dry    # Preview changes without saving
"""

import argparse
import json
import re
import random
from pathlib import Path

import openpyxl

random.seed(42)

BASE_DIR = Path(__file__).parent

# ── Known brands for extraction from product names ──

KNOWN_BRANDS = [
    # Multi-word first (longest match)
    "Carpet Pro", "Clean Max", "Cen-Tec", "Dirt Devil", "Filter Queen",
    "Fuller Brush", "Hide A Hose", "IPC Eagle", "MD Manufacturing",
    "Modern Day", "Powr-Flite", "Shop-Vac", "Wessel Werk",
    # Single-word
    "Advance", "Aerus", "Ametek", "Beam", "Bissell", "Bosch", "Broan",
    "CycloVac", "Dyson", "Electrolux", "Eureka", "Evolution", "Hayden",
    "HiZero", "Hoover", "Intervac", "Karcher", "Kenmore", "Kirby",
    "Koblenz", "Lamb", "Lindhaus", "Maytag", "Miele", "Nilfisk",
    "NSS", "Nutone", "Oreck", "Pacific", "Panasonic", "Perfect",
    "Plastiflex", "ProTeam", "Proteam", "Pullman", "Rainbow", "Regina",
    "Rexair", "Riccar", "Royal", "Samsung", "Sanitaire", "Sanyo",
    "SEBO", "Shark", "Sharp", "ShopVac", "Simplicity", "Singer",
    "Sirena", "Tennant", "Titan", "Tornado", "TriStar", "Vacuflo",
    "Vacumaid", "Windsor",
]


def extract_brand_from_name(clean_name, raw_name=""):
    """Extract a brand from the product name if one matches KNOWN_BRANDS."""
    text = f"{clean_name} {raw_name}" if raw_name else clean_name
    for brand in KNOWN_BRANDS:
        if re.search(rf'\b{re.escape(brand)}\b', text, re.I):
            return brand
    return ""
PRODUCTS_FILE = BASE_DIR / "product_names.json"
PROGRESS_FILE = BASE_DIR / "full_discovery_progress.json"
OUTPUT_DIR = BASE_DIR / "output"

# ── Name cleaning (from full_discovery.py) ──

ABBREVIATIONS = {
    'ASSY': 'Assembly', 'ASSEM': 'Assembly', 'ASSEMBLE': 'Assembly',
    'ASY': 'Assembly', 'MTR': 'Motor', 'BRG': 'Bearing',
    'BLK': 'Black', 'WHT': 'White', 'GRN': 'Green', 'BLU': 'Blue',
    'GRY': 'Gray', 'SLVR': 'Silver', 'CLR': 'Clear', 'ORG': 'Orange',
    'PNK': 'Pink', 'PUR': 'Purple', 'YEL': 'Yellow', 'BRN': 'Brown',
    'REFL': 'Reflector', 'HNDL': 'Handle', 'VAC': 'Vacuum',
    'UPRT': 'Upright', 'UPRI': 'Upright', 'CLNR': 'Cleaner',
    'DIAM': 'Diameter', 'SQ': 'Square', 'PR': 'Pair',
    'COMMERICIAL': 'Commercial', 'CLOTHBAG': 'Cloth Bag',
}

KEEP_UPPER = {
    'HEPA', 'LED', 'UV', 'AC', 'DC', 'USA', 'XL', 'II', 'III', 'IV',
    'V', 'VI', 'VII', 'VIII', 'IX', 'RH', 'LH', 'PWR',
}


def is_model_number(word):
    clean = word.strip(',-/()')
    if not clean:
        return False
    has_digit = bool(re.search(r'\d', clean))
    has_alpha = bool(re.search(r'[A-Za-z]', clean))
    if has_digit and has_alpha and len(clean) >= 3:
        return True
    if clean.isdigit() and len(clean) >= 4:
        return True
    return False


def restructure_name(text):
    text = text.strip(' ,-/')
    if not text:
        return ""
    if ',' not in text and '-' not in text:
        return text
    parts = [p.strip() for p in text.split(',') if p.strip()]
    if len(parts) <= 1:
        return text
    product_type = parts[0].strip()
    modifiers = parts[1:]
    short_mods = []
    long_mods = []
    for mod in modifiers:
        mod = mod.strip()
        if mod.lower().startswith('fits'):
            long_mods.append(mod)
        elif is_model_number(mod.split()[0] if mod.split() else mod):
            long_mods.append(mod)
        elif len(mod.split()) <= 2 and not any(c.isdigit() for c in mod):
            short_mods.append(mod)
        else:
            long_mods.append(mod)
    result_parts = []
    if short_mods:
        result_parts.extend(short_mods)
    result_parts.append(product_type)
    result = ' '.join(result_parts)
    if long_mods:
        result += ' - ' + ', '.join(long_mods)
    return result


def smart_title_case(text):
    words = text.split()
    result = []
    small_words = {'a', 'an', 'the', 'and', 'or', 'for', 'of', 'with', 'in', 'on', 'to', 'at', 'by'}
    for i, word in enumerate(words):
        stripped = word.strip(',-/()"\'')
        if not stripped:
            result.append(word)
            continue
        if is_model_number(stripped):
            result.append(word.upper())
            continue
        if stripped.upper() in KEEP_UPPER:
            result.append(word.upper())
            continue
        if re.match(r'^[\d\'"/.]+$', stripped):
            result.append(word)
            continue
        if stripped.lower() in small_words and i > 0:
            prev = words[i - 1] if i > 0 else ''
            if not prev.endswith('-'):
                result.append(word.lower())
                continue
        result.append(word.capitalize())
    return ' '.join(result)


def clean_product_name(raw_name, brand, part_number):
    name = raw_name
    if not name:
        return f"{brand} Replacement Part" if brand else "Replacement Part"

    first_brand = brand.split(",")[0].strip() if brand else ""
    all_brands = [b.strip() for b in brand.split(",") if b.strip()] if brand else []

    name = re.sub(r'<br\s*/?>', ' | ', name, flags=re.IGNORECASE)
    name = re.sub(r'<[^>]+>', '', name)
    name = re.sub(r'\bNLA\b[\s\d/ -]*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'PLEASE USE ALT.*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'DO NOT USE.*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'USE ALT\s*#?\s*.*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\bDISCONTINUED\b[^|]*', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\bOEM\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\b(REPL|REPLACES?)\s+[\w-]+', '', name, flags=re.IGNORECASE)
    name = re.sub(r'SAME AS\s+[\w-]+', '', name, flags=re.IGNORECASE)
    name = re.sub(r'SPARE PARTS', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\d+[A-Z]?\s*:\s*', '', name)

    for b in all_brands:
        bp = re.escape(b)
        name = re.sub(rf'[-/,]\s*{bp}\b', ',', name, flags=re.IGNORECASE)
        name = re.sub(rf'^\s*{bp}\b\s*[,]?\s*', '', name, flags=re.IGNORECASE)
        name = re.sub(rf'\b{bp}\b\s*,?\s*', '', name, flags=re.IGNORECASE)

    name = re.sub(r'\bW\s*/\s*', 'with ', name, flags=re.IGNORECASE)
    name = re.sub(r'\bW/(?=\w)', 'with ', name, flags=re.IGNORECASE)
    name = re.sub(r'\((\d+)\s*PK\)', r'(\1 Pack)', name, flags=re.IGNORECASE)
    name = re.sub(r'\((\d+)\s*PAK\)', r'(\1 Pack)', name, flags=re.IGNORECASE)
    name = re.sub(r'\b(\d+)\s*PK\b', r'\1 Pack', name, flags=re.IGNORECASE)
    name = re.sub(r'\b(\d+)\s*PAK\b', r'\1 Pack', name, flags=re.IGNORECASE)
    name = re.sub(r'PACKS? OF (\d+)', r'\1 Pack', name, flags=re.IGNORECASE)

    for abbr, expansion in ABBREVIATIONS.items():
        name = re.sub(rf'\b{abbr}\b', expansion, name, flags=re.IGNORECASE)

    name = re.sub(r'\bALSO FITS?\b', 'Fits', name, flags=re.IGNORECASE)

    segments = [s.strip() for s in name.split('|') if s.strip()]
    main = segments[0] if segments else name
    extra = segments[1:] if len(segments) > 1 else []
    main = restructure_name(main)
    if extra:
        extra_clean = [restructure_name(e) for e in extra]
        extra_clean = [e for e in extra_clean if e and len(e) > 2]
        if extra_clean:
            main = main + " - " + ", ".join(extra_clean)
    name = main

    name = smart_title_case(name)

    if first_brand:
        if not name.lower().startswith(first_brand.lower()):
            name = f"{first_brand} {name}"

    name = re.sub(r'\s{2,}', ' ', name)
    name = re.sub(r',\s*,', ',', name)
    name = re.sub(r'\(\s*\)', '', name)
    name = re.sub(r'^\s*[-,/]\s*', '', name)
    name = re.sub(r'\s*[-,/]\s*$', '', name)
    name = re.sub(r'\s*,\s*$', '', name)
    name = re.sub(r'\s+-\s*$', '', name)
    name = name.strip()

    if len(name) > 120:
        for sep in [' - ', ', ']:
            cut = name[:120].rfind(sep)
            if cut > 50:
                name = name[:cut]
                break
        else:
            cut = name[:120].rfind(' ')
            if cut > 50:
                name = name[:cut]
        name = name.strip(' ,-/')

    return name


# ── Category detection ──

CATEGORY_PATTERNS = [
    # Electrical parts (before other categories so "capacitor" isn't caught by generic)
    ("capacitor", re.compile(r"\bcapacitor\b", re.I)),
    ("circuit_board", re.compile(r"\bcircuit\s*board\b|\bpcb\b|\bcontrol\s*board\b|\bboard\b", re.I)),
    # Bags
    ("bag_paper", re.compile(r"\bpaper\s*bag", re.I)),
    ("bag_cloth", re.compile(r"\bcloth\s*bag", re.I)),
    ("bag", re.compile(r"\bbags?\b", re.I)),
    ("filter_hepa", re.compile(r"\bhepa\b.*\bfilter\b|\bfilter\b.*\bhepa\b", re.I)),
    ("filter_foam", re.compile(r"\bfoam\b.*\bfilter\b|\bfilter\b.*\bfoam\b", re.I)),
    ("filter_exhaust", re.compile(r"\bexhaust\b.*\bfilter\b|\bfilter\b.*\bexhaust\b", re.I)),
    ("filter_pre_motor", re.compile(r"\bpre[\s-]?motor\b.*\bfilter\b|\bfilter\b.*\bpre[\s-]?motor\b", re.I)),
    ("filter", re.compile(r"\bfilters?\b", re.I)),
    ("belt_geared", re.compile(r"\bgeared\b.*\bbelt\b|\bbelt\b.*\bgeared\b", re.I)),
    ("belt_cogged", re.compile(r"\bcogged\b.*\bbelt\b|\bbelt\b.*\bcogged\b", re.I)),
    ("belt", re.compile(r"\bbelts?\b", re.I)),
    ("carbon_brush", re.compile(r"\bcarbon\s*brush", re.I)),
    ("brush_roll", re.compile(r"\bbrush\s*roll\b|\broller\s*brush\b|\bagitator\b", re.I)),
    ("brush_strip", re.compile(r"\bbrush\s*strip", re.I)),
    ("brush", re.compile(r"\bbrush\b", re.I)),
    ("hardware", re.compile(r"\bscrews?\b|\bnuts?\b|\bbolts?\b|\brivets?\b|\bwashers?\b|\bhardware\b", re.I)),
    # Motor sub-components — BEFORE generic "motor"
    ("motor_wire", re.compile(r"\bmotor\b.*\b(wire|wiring|lead)\b|\b(wire|wiring|lead)\b.*\bmotor\b", re.I)),
    ("motor_cover", re.compile(r"\bmotor\b.*\b(cover|housing|shroud|shell|plate)\b|\b(cover|housing|shroud|shell|plate)\b.*\bmotor\b", re.I)),
    ("motor_gasket", re.compile(r"\bmotor\b.*\b(gasket|seal)\b|\b(gasket|seal)\b.*\bmotor\b", re.I)),
    ("motor_bearing", re.compile(r"\bmotor\b.*\bbearing\b|\bbearing\b.*\bmotor\b", re.I)),
    ("motor_bracket", re.compile(r"\bmotor\b.*\b(bracket|mount|cradle|base)\b|\b(bracket|mount|cradle|base)\b.*\bmotor\b", re.I)),
    ("motor_fan", re.compile(r"\bmotor\b.*\bfan\b|\bfan\b.*\bmotor\b", re.I)),
    # Generic motor — only matches when no sub-component keyword present
    ("motor", re.compile(r"\bmotor\b", re.I)),
    ("hose", re.compile(r"\bhose\b", re.I)),
    ("cord", re.compile(r"\bcords?\b|\bpower\s*cord\b", re.I)),
    ("wheel", re.compile(r"(?<!\bno\s)\bwheels?\b|\bcastors?\b|\bcasters?\b", re.I)),
    ("axle", re.compile(r"\baxle\b", re.I)),
    ("switch", re.compile(r"\bswitch\b", re.I)),
    ("handle", re.compile(r"\bhandle\b", re.I)),
    ("nozzle", re.compile(r"\bnozzle\b", re.I)),
    ("fan", re.compile(r"\bfan\b", re.I)),
    ("spring", re.compile(r"\bspring\b", re.I)),
    ("dust_cup", re.compile(r"\bdust\s*(cup|bin|container)\b", re.I)),
    ("wand", re.compile(r"\bwands?\b", re.I)),
    ("bearing", re.compile(r"\bbearings?\b", re.I)),
    ("gasket", re.compile(r"\bgaskets?\b|\bseals?\b|\bo[\s-]?ring\b", re.I)),
    ("bumper", re.compile(r"\bbumpers?\b", re.I)),
    ("attachment", re.compile(r"\battachment\b|\bcrevice\s*tool\b|\bupholstery\s*tool\b|\bdusting\s*brush\b|\bfloor\s*tool\b|\btool\b", re.I)),
    ("cover", re.compile(r"\bcover\b|\bplate\b|\bhousing\b|\bshroud\b|\blid\b", re.I)),
    ("pedal", re.compile(r"\bpedal\b", re.I)),
    ("latch", re.compile(r"\blatch\b|\bcatch\b|\bclasp\b", re.I)),
    ("valve", re.compile(r"\bvalve\b", re.I)),
    ("cuff", re.compile(r"\bcuff\b|\badapter\b|\bconnector\b|\bcoupling\b", re.I)),
    ("power_head", re.compile(r"\bpower\s*(head|nozzle)\b", re.I)),
    # New categories to reduce generics
    ("cleaning_solution", re.compile(r"\b(solution|shampoo|detergent|deodor|fragrance|odor)\b", re.I)),
    ("tank", re.compile(r"\btanks?\b|\breservoir\b", re.I)),
    ("tube_duct", re.compile(r"\b(tube|pipe|duct)\b", re.I)),
    ("wire_cable", re.compile(r"\b(wire|cable|wiring)\b", re.I)),
    ("connector", re.compile(r"\b(connector|coupl)\b", re.I)),
    ("pump", re.compile(r"\bpumps?\b", re.I)),
    ("light_bulb", re.compile(r"\b(light|bulb|lamp|led)\b", re.I)),
    ("knob_dial", re.compile(r"\b(knob|dial)\b", re.I)),
    ("clip", re.compile(r"\bclips?\b", re.I)),
    ("roller", re.compile(r"\brollers?\b", re.I)),
    ("label_decal", re.compile(r"\b(label|decal|sticker)\b", re.I)),
    ("sensor", re.compile(r"\bsensors?\b", re.I)),
    ("door_lid", re.compile(r"\bdoor\b", re.I)),
    # Machine — expanded exclusion list
    ("machine", re.compile(
        r"\b(vacuum|cleaner|steamer|extractor|spot\s*cleaner)\b"
        r"(?!.*\b(filter|belt|bag|hose|cord|motor|brush|wheel|switch|handle|nozzle|fan|spring|"
        r"screw|wand|bearing|gasket|bumper|attachment|cover|plate|pedal|latch|valve|"
        r"tank|separator|assembly|tube|duct|wire|board|circuit|pump|cap|ring|kit|tool|"
        r"base|body|door|harness|tip|clamp|arm|link|pad|solution|water|button|holder|"
        r"sensor|light|bulb|plug|connector|adapter|strap|hook|spacer|bushing|retainer|"
        r"knob|dial|lid|mop|roller|cable|label|clip|suction)\b)", re.I)),
]


def detect_category(text):
    """Detect product category from name or description text."""
    for cat, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return cat
    return "generic"


# ── Extract model numbers from clean_name ──

COLORS_SET = {'black', 'white', 'gray', 'grey', 'red', 'blue', 'green', 'yellow',
               'orange', 'purple', 'pink', 'silver', 'clear', 'brown', 'tan', 'beige'}
NON_MODEL_WORDS = {'pack', 'pk', 'pair', 'motor', 'vacuum', 'upright', 'canister',
                   'assembly', 'handle', 'filter', 'belt', 'hose', 'cord', 'brush',
                   'bag', 'wand', 'nozzle', 'switch', 'wheel', 'cover', 'plate',
                   'series', 'stage', 'power', 'commercial'} | COLORS_SET


def extract_models_from_name(clean_name):
    """Extract model numbers that appear in the product title."""
    models = []

    # Look for "Fits MODEL" or "for MODEL" patterns first (most reliable)
    fits = re.findall(r'(?:Fits?|for|compatible with)\s+([\w/-]+(?:\s*,\s*[\w/-]+)*)', clean_name, re.I)
    for f in fits:
        for part in re.split(r'[,/]', f):
            part = part.strip().strip('()')
            if part and is_model_number(part) and part.lower() not in NON_MODEL_WORDS:
                models.append(part)

    # Look for patterns after " - " separator (usually model info)
    dash_parts = clean_name.split(' - ')
    if len(dash_parts) > 1:
        for part in dash_parts[1:]:
            for word in re.split(r'[,\s]+', part):
                word = re.sub(r'[()]+', '', word).strip()
                if not word:
                    continue
                if is_model_number(word) and word.lower() not in NON_MODEL_WORDS:
                    models.append(word)

    # Look for alphanumeric model patterns like "BH50010", "DC27", "S3681D"
    # Must have BOTH letters and digits
    for m in re.findall(r'\b([A-Z]{1,4}\d{2,}[A-Z]*\d*)\b', clean_name, re.I):
        m_clean = re.sub(r'[()]+', '', m).strip()
        if m_clean and m_clean not in models and m_clean.lower() not in NON_MODEL_WORDS:
            models.append(m_clean)
    for m in re.findall(r'\b(\d{2,}[A-Z]{1,4}\d*)\b', clean_name, re.I):
        m_clean = re.sub(r'[()]+', '', m).strip()
        if m_clean and m_clean not in models and m_clean.lower() not in NON_MODEL_WORDS:
            models.append(m_clean)

    # Look for "Brand NNNN" patterns where NNNN is a pure digit model number (4+ digits)
    brand_model = re.findall(r'(?:Bissell|Hoover|Eureka|Dyson|Kirby|Oreck|Miele|Kenmore|Riccar|Sanitaire|ProTeam|Windsor|Electrolux|Panasonic|Sharp|Compact|Rexair|Rainbow|Tristar|Filter Queen|Royal|Dirt Devil|Simplicity)\s+(\d{3,})', clean_name, re.I)
    for m in brand_model:
        if m not in models:
            models.append(m)

    # Filter out pack sizes, dimensions, colors, and common words
    filtered = []
    for m in models:
        if m.isdigit() and len(m) < 3:
            continue
        if re.match(r'^\d+[/x]\d+$', m):
            continue
        if m.lower() in NON_MODEL_WORDS:
            continue
        # Skip if it's just a color with a dash like "BLACK-1"
        if re.match(r'^(black|white|gray|grey|red|blue|green)-?\d*$', m, re.I):
            continue
        # Skip common words with digits stripped of parens like MOTOR2
        base_word = re.sub(r'\d+$', '', m).lower()
        if base_word in NON_MODEL_WORDS:
            continue
        # Skip pack-size patterns like "10PK", "6PK", "3PK", "MOTOR2PK"
        if re.match(r'^\d+PK', m, re.I):
            continue
        if re.search(r'\d+PK', m, re.I):
            continue
        # Skip anything with HTML tags
        if '<' in m or '>' in m:
            continue
        filtered.append(m)

    return list(dict.fromkeys(filtered))  # dedupe preserving order


def extract_quantity(text):
    m = re.search(r"(\d+)\s*[-]?\s*pack", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)\s*PK\b", text, re.I)
    return m.group(1) if m else ""


def extract_length(text):
    m = re.search(r"(\d+['\u2019]\d*[\"'\u2019]*\s*(?:long)?)", text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"(\d+\s*(?:1/\d+\s*)?(?:inch(?:es)?|feet|foot|ft|['\"]\s*))", text, re.I)
    return m.group(1).strip() if m else ""


def extract_size(text):
    """Extract size info like '1 1/4', '1-1/2 inch' etc."""
    m = re.search(r"(\d+[\s-]?\d*/\d+[\"'']*\s*(?:inch|\")?)", text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"(\d+(?:\.\d+)?\s*(?:mm|cm|inch(?:es)?|\")\b)", text, re.I)
    return m.group(1).strip() if m else ""


def extract_color(text):
    colors = ["black", "white", "gray", "grey", "red", "blue", "green", "yellow",
              "orange", "purple", "pink", "silver", "clear", "brown", "tan", "beige"]
    text_lower = text.lower()
    found = [c for c in colors if re.search(rf"\b{c}\b", text_lower)]
    return found[0].title() if found else ""


def brand_display(brand_str):
    if not brand_str or not brand_str.strip():
        return ""
    return brand_str.split(",")[0].strip()


# ── Description generation ──

def build_compat(brand, model, clean_name):
    """Build a compatibility phrase. Never says 'select vacuum models'."""
    if brand and model:
        return f"compatible with {brand} {model} vacuum models"
    if brand:
        return f"compatible with {brand} vacuum models"
    if model:
        return f"compatible with {model} vacuum models"
    # No brand/model — try to pull model numbers from the clean_name
    models = extract_models_from_name(clean_name)
    if models:
        model_str = ", ".join(models[:3])
        return f"compatible with {model_str} vacuum models"
    # Last resort — use the product name itself (trimmed)
    # Strip the category word from the beginning to avoid "Filter for Filter..."
    return "for your vacuum"


CATEGORY_NAMES = {
    "capacitor": "capacitor",
    "circuit_board": "circuit board",
    "filter_hepa": "HEPA filter",
    "filter_foam": "foam filter",
    "filter_exhaust": "exhaust filter",
    "filter_pre_motor": "pre-motor filter",
    "filter": "filter",
    "bag_paper": "paper vacuum bags",
    "bag_cloth": "cloth vacuum bag",
    "bag": "vacuum bags",
    "belt_geared": "geared belt",
    "belt_cogged": "cogged belt",
    "belt": "drive belt",
    "carbon_brush": "carbon motor brushes",
    "brush_roll": "brush roll",
    "brush_strip": "brush strip",
    "brush": "brush",
    "motor": "motor assembly",
    "motor_wire": "motor wiring",
    "motor_cover": "motor cover",
    "motor_gasket": "motor gasket/seal",
    "motor_bearing": "motor bearing",
    "motor_bracket": "motor mounting bracket",
    "motor_fan": "motor fan",
    "hose": "hose",
    "cord": "power cord",
    "wheel": "wheel",
    "axle": "axle",
    "switch": "switch",
    "handle": "handle assembly",
    "nozzle": "nozzle assembly",
    "fan": "fan",
    "spring": "spring",
    "hardware": "hardware",
    "dust_cup": "dust cup",
    "wand": "wand",
    "bearing": "bearing",
    "gasket": "gasket/seal",
    "bumper": "bumper",
    "attachment": "attachment tool",
    "cover": "cover/plate",
    "pedal": "pedal",
    "latch": "latch/catch",
    "valve": "valve",
    "cuff": "hose cuff/adapter",
    "power_head": "power head",
    "cleaning_solution": "cleaning product",
    "tank": "tank/reservoir",
    "tube_duct": "tube/duct",
    "wire_cable": "wiring/cable",
    "connector": "connector",
    "pump": "pump",
    "light_bulb": "light/bulb",
    "knob_dial": "knob/dial",
    "clip": "clip",
    "roller": "roller",
    "label_decal": "label/decal",
    "sensor": "sensor",
    "door_lid": "access door",
    "machine": "vacuum cleaner",
    "generic": "part",
}

CATEGORY_BENEFITS = {
    "capacitor": "Provides proper electrical regulation for consistent motor performance and reliable operation.",
    "circuit_board": "Restores electronic control functions for reliable vacuum operation.",
    "filter_hepa": "Captures fine dust, allergens, and microscopic particles down to 0.3 microns for cleaner air output.",
    "filter_foam": "Washable foam design traps fine particles and helps maintain strong suction performance.",
    "filter_exhaust": "Filters outgoing air to reduce dust recirculation and keep your environment cleaner.",
    "filter_pre_motor": "Protects the motor from dust and debris, helping extend the life of your vacuum.",
    "filter": "Helps maintain optimal suction and air filtration for effective cleaning performance.",
    "bag_paper": "Disposable design makes for quick, hygienic dust disposal after each use.",
    "bag_cloth": "Durable cloth construction can be emptied and reused, reducing ongoing replacement costs.",
    "bag": "Designed for a secure fit to maximize dust capture and maintain suction.",
    "belt_geared": "Geared design provides consistent brush roll speed for reliable carpet agitation without slipping.",
    "belt_cogged": "Cogged teeth prevent slipping for consistent brush roll performance and better cleaning results.",
    "belt": "Restores proper brush roll spin for effective carpet cleaning and debris pickup.",
    "carbon_brush": "Essential for maintaining electrical contact within the motor for reliable operation.",
    "brush_roll": "Agitates carpet fibers to loosen embedded dirt and debris for deeper cleaning.",
    "brush_strip": "Attaches to the brush roll to sweep and agitate carpet fibers during cleaning.",
    "brush": "Maintains effective sweeping and agitation for thorough cleaning results.",
    "motor": "Restores full suction power and performance to your vacuum.",
    "motor_wire": "Restores proper electrical connection to the motor for reliable power delivery.",
    "motor_cover": "Restores a secure, factory-fit enclosure to protect motor components during operation.",
    "motor_gasket": "Provides an airtight seal around the motor to maintain strong suction and prevent air leaks.",
    "motor_bearing": "Ensures smooth, quiet motor rotation for reliable vacuum operation.",
    "motor_bracket": "Provides secure motor mounting for stable, vibration-free vacuum operation.",
    "motor_fan": "Restores proper airflow and suction power for effective cleaning performance.",
    "hose": "Restores strong suction and flexible reach for above-floor cleaning tasks.",
    "cord": "Provides extended reach for cleaning larger areas without switching outlets.",
    "wheel": "Restores smooth rolling and easy maneuverability across floors and carpets.",
    "axle": "Ensures smooth, stable wheel rotation for easy vacuum movement.",
    "switch": "Restores reliable on/off or speed control functionality to your vacuum.",
    "handle": "Restores comfortable grip and full control while vacuuming.",
    "nozzle": "Provides effective suction contact with floors for optimal dirt pickup.",
    "fan": "Restores proper airflow and suction power for effective cleaning performance.",
    "spring": "Restores proper tension and mechanical function to your vacuum.",
    "hardware": "Ensures a secure, factory-spec fit for reliable vacuum assembly.",
    "dust_cup": "Easy-empty design for quick, hygienic disposal of collected dirt and debris.",
    "wand": "Extends your reach for cleaning above-floor surfaces, ceilings, and tight spaces.",
    "bearing": "Ensures smooth, quiet rotation of moving parts for reliable vacuum operation.",
    "gasket": "Provides an airtight seal to maintain strong suction and prevent air leaks.",
    "bumper": "Protects furniture and baseboards from scuffs and scratches during vacuuming.",
    "attachment": "Extends your vacuum's versatility for cleaning upholstery, crevices, and hard-to-reach areas.",
    "cover": "Restores a secure, factory-fit closure for proper vacuum operation.",
    "pedal": "Restores proper foot-operated control for easy height adjustment or drive engagement.",
    "latch": "Ensures a secure closure for reliable vacuum operation during use.",
    "valve": "Restores proper airflow control for consistent suction performance.",
    "cuff": "Provides a secure connection between your hose and attachments for airtight suction.",
    "power_head": "Features a motorized brush roll for deep carpet cleaning and effective debris pickup.",
    "cleaning_solution": "Removes tough stains and odors for a fresh, deep-cleaned result.",
    "tank": "Holds cleaning solution or collected water for uninterrupted operation.",
    "tube_duct": "Maintains proper airflow and internal routing for consistent vacuum performance.",
    "wire_cable": "Restores proper electrical connections for reliable vacuum operation.",
    "connector": "Provides a secure junction between components for airtight, reliable operation.",
    "pump": "Restores fluid delivery for effective cleaning and solution application.",
    "light_bulb": "Restores illumination for better visibility while cleaning.",
    "knob_dial": "Restores easy-to-use controls for convenient vacuum adjustment.",
    "clip": "Provides secure fastening to keep vacuum components properly aligned.",
    "roller": "Ensures smooth movement and proper component function during operation.",
    "label_decal": "Restores factory-original branding and identification markings.",
    "sensor": "Restores automatic detection and response functions for smart vacuum operation.",
    "door_lid": "Provides secure access to internal components while maintaining proper airflow.",
    "machine": "Delivers powerful suction and reliable performance for thorough home or commercial cleaning.",
    "generic": "Restores your vacuum to optimal working condition with a factory-spec fit.",
}


def build_name_context(clean_name, raw_desc):
    """Extract useful contextual details from the product name for the description."""
    # Clean HTML from raw_desc before processing
    clean_raw = re.sub(r'<[^>]+>', ' ', raw_desc) if raw_desc else ""
    # Combine both sources for maximum info
    text = f"{clean_name} {clean_raw}" if clean_raw else clean_name

    # Extract brand mentions from the name itself
    brand_patterns = [
        'Dirt Devil', 'Hoover', 'Eureka', 'Dyson', 'Kirby', 'Oreck', 'Miele',
        'Kenmore', 'Riccar', 'Sanitaire', 'ProTeam', 'Windsor', 'Electrolux',
        'Panasonic', 'Sharp', 'Compact', 'Rexair', 'Rainbow', 'TriStar',
        'Filter Queen', 'Royal', 'Simplicity', 'Bissell', 'Shark', 'Nutone',
        'Beam', 'Broan', 'Vacuflo', 'Hayden', 'Aerus', 'Proteam', 'Clarke',
        'Advance', 'Nilfisk', 'Fuller Brush', 'Lindhaus', 'Sebo', 'Titan',
        'Centec', 'Plastiflex', 'Intervac', 'Modern Day', 'Lamb', 'Ametek',
        'Vaculine', 'Hide A Hose', 'CycloVac', 'Vacumaid', 'MD Manufacturing',
        'Singer', 'Bissel', 'Lindhaus', 'Pullman', 'Powr-Flite', 'Tennant',
        'Karcher', 'Nilf?isk', 'Bosch', 'Samsung', 'LG',
    ]
    found_brand = ""
    for bp in brand_patterns:
        if re.search(rf'\b{re.escape(bp)}\b', text, re.I):
            found_brand = bp
            break

    # Extract model/compatibility info after brand
    found_models = extract_models_from_name(text)

    return found_brand, found_models


def generate_description(clean_name, brand, model="", raw_desc=""):
    """Generate a 1-3 sentence product description. Uses product name details for specificity."""
    # Use both clean_name and raw_desc for category detection
    text_for_detection = f"{clean_name} {raw_desc}" if raw_desc else clean_name
    category = detect_category(clean_name)
    if category == "generic" and raw_desc:
        category = detect_category(raw_desc)

    cat_name = CATEGORY_NAMES.get(category, "part")
    benefit = CATEGORY_BENEFITS.get(category, CATEGORY_BENEFITS["generic"])

    qty = extract_quantity(clean_name) or extract_quantity(raw_desc or "")
    length = extract_length(clean_name) or extract_length(raw_desc or "")
    size = extract_size(clean_name) or extract_size(raw_desc or "")
    color = extract_color(clean_name) or extract_color(raw_desc or "")

    # If no brand/model from the product record, try to extract from the name itself
    name_brand, name_models = build_name_context(clean_name, raw_desc)
    effective_brand = brand or name_brand
    effective_models = []
    if model:
        effective_models.append(model)
    effective_models.extend(m for m in name_models if m != model)

    # Build compatibility phrase using all available info
    if effective_brand and effective_models:
        model_str = ", ".join(effective_models[:3])
        compat = f"for {effective_brand} {model_str} vacuum models"
    elif effective_brand:
        compat = f"for {effective_brand} vacuums"
    elif effective_models:
        model_str = ", ".join(effective_models[:3])
        compat = f"for {model_str} vacuum models"
    else:
        # No brand or models found — build a descriptive compat phrase
        # Don't just repeat the full product name, extract the useful context
        # Strip the category/product-type words to get the contextual modifiers
        name_stripped = re.sub(
            r'\b(replacement|filter|belt|bag|hose|cord|motor|brush|wheel|switch|'
            r'handle|nozzle|fan|spring|wand|bearing|gasket|bumper|cover|plate|'
            r'assembly|vacuum|part|paper|foam|hepa|exhaust|pre-motor|geared|'
            r'cogged|carbon|strip|roll|dust|cup|power|head|capacitor|circuit|'
            r'board|latch|catch|valve|axle|pedal|cuff|adapter|connector)\b',
            '', clean_name, flags=re.I
        ).strip(' ,-/')
        # Also strip leading/trailing junk
        name_stripped = re.sub(r'^[\s,/-]+|[\s,/-]+$', '', name_stripped)
        if name_stripped and len(name_stripped) > 5 and name_stripped.lower() != clean_name.lower():
            compat = f"for {name_stripped}"
        else:
            compat = "for your vacuum"

    # Build sentence 1: what it is + compatibility
    if category == "machine":
        s1 = f"{effective_brand + ' ' if effective_brand else ''}{effective_models[0] + ' ' if effective_models else ''}vacuum cleaner."
    else:
        s1 = f"Replacement {cat_name} {compat}."

    # Build sentence 2: benefit
    s2 = benefit

    # Build sentence 3: optional details (quantity, length, size, color)
    details = []
    if qty:
        details.append(f"Pack of {qty}")
    if length:
        details.append(f"{length} length")
    if size and size not in clean_name[:20]:
        details.append(f"{size} size")
    if color:
        details.append(f"{color} color")
    s3 = ". ".join(details) + "." if details else ""

    desc = f"{s1} {s2}"
    if s3:
        desc += f" {s3}"

    return re.sub(r"  +", " ", desc).strip()


def is_sku_like(name):
    """Check if a name looks like just a SKU/part number, not a real product name."""
    if not name:
        return True
    stripped = name.strip()
    # Pure numbers or number-dash-number patterns (e.g., "32-1320-01", "1470944500")
    if re.match(r'^[\d-]+$', stripped):
        return True
    # Alphanumeric codes with no spaces (e.g., "AK12206", "PBC25LV", "A-MD814L")
    if re.match(r'^[A-Z\d][\w-]*$', stripped, re.I) and ' ' not in stripped and len(stripped) < 15:
        # Has both letters and digits — looks like a product code
        has_digit = bool(re.search(r'\d', stripped))
        has_alpha = bool(re.search(r'[A-Za-z]', stripped))
        if has_digit and has_alpha:
            return True
        # Pure digits, longish
        if stripped.replace('-', '').isdigit():
            return True
    # Very short with mostly digits
    if len(stripped) < 12 and sum(c.isdigit() for c in stripped) > len(stripped) * 0.5:
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Regenerate all product descriptions")
    parser.add_argument("--dry", action="store_true", help="Preview changes without saving")
    args = parser.parse_args()

    with open(PRODUCTS_FILE) as f:
        products = json.load(f)

    # Load enrichment data for fixing SKU-only names
    enriched = {}
    pc_to_enrich_key = {}  # product_code -> enrichment key (reverse map)
    if PROGRESS_FILE.exists():
        progress = json.load(open(PROGRESS_FILE))
        enriched = progress.get("enriched", {})
        for ekey, edata in enriched.items():
            if not edata:
                continue
            pc = edata.get("product_code", "")
            if pc:
                pc_to_enrich_key[pc] = ekey

    print(f"Loaded {len(products)} products, {len(enriched)} enrichment records, {len(pc_to_enrich_key)} reverse mappings")

    # Stats
    names_fixed = 0
    brands_extracted = 0
    descs_regenerated = 0
    had_generic_before = 0
    still_generic = 0

    for key, product in products.items():
        old_desc = product.get("description", "")
        if "select vacuum models" in old_desc:
            had_generic_before += 1

        clean_name = product.get("clean_name", "")
        brand = brand_display(product.get("brand", ""))
        model = product.get("model", "")
        sku = product.get("sku", key)
        raw_name = product.get("raw_name", "")
        raw_desc = product.get("raw_description", "")

        # Fix 1: If clean_name is just a SKU, try to rebuild from enrichment data
        if is_sku_like(clean_name):
            # Try enrichment data: direct key lookup, then reverse map via product_code
            e = enriched.get(key) or {}
            if not e.get("description"):
                enrich_key = pc_to_enrich_key.get(sku) or pc_to_enrich_key.get(key)
                if enrich_key:
                    e = enriched.get(enrich_key) or {}

            api_desc = (e.get("description") or "") if e else ""
            api_mfr = (e.get("manufacturer") or "") if e else ""

            if api_desc and not is_sku_like(api_desc):
                # Use the API description to build a real name
                new_name = clean_product_name(api_desc, api_mfr or brand, sku)
                if not is_sku_like(new_name) and len(new_name) > len(clean_name):
                    product["clean_name"] = new_name
                    clean_name = new_name
                    if api_mfr and not brand:
                        product["brand"] = api_mfr
                        brand = brand_display(api_mfr)
                    product["raw_description"] = api_desc
                    raw_desc = api_desc
                    names_fixed += 1

        # Fix 2: If raw_desc is empty, try to pull from enrichment
        if not raw_desc or raw_desc == raw_name:
            e = enriched.get(key) or {}
            if not e.get("description"):
                enrich_key = pc_to_enrich_key.get(sku) or pc_to_enrich_key.get(key)
                if enrich_key:
                    e = enriched.get(enrich_key) or {}
            if e and e.get("description"):
                raw_desc = e["description"]
                product["raw_description"] = raw_desc

        # Fix 3: Extract brand from name if empty
        if not product.get("brand", "").strip():
            extracted = extract_brand_from_name(clean_name, raw_name)
            if extracted:
                product["brand"] = extracted
                brand = brand_display(extracted)
                brands_extracted += 1

        # Fix 4: Regenerate description for ALL products
        desc = generate_description(clean_name, brand, model, raw_desc)
        product["description"] = desc
        descs_regenerated += 1

        if "select vacuum models" in desc:
            still_generic += 1

    print(f"\nResults:")
    print(f"  Names fixed (SKU → real name): {names_fixed}")
    print(f"  Brands extracted from names: {brands_extracted}")
    print(f"  Descriptions regenerated: {descs_regenerated}")
    print(f"  Had 'select vacuum models' before: {had_generic_before}")
    print(f"  Still have 'select' after: {still_generic} (should be 0)")

    # Category distribution
    cat_counts = {}
    for p in products.values():
        cat = detect_category(p.get("clean_name", ""))
        if cat == "generic":
            cat = detect_category(p.get("raw_description", ""))
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    print(f"\nCategory distribution:")
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:20s}: {count:5d}")

    # Sample some regenerated descriptions
    print("\n--- Sample Descriptions (previously generic) ---")
    generic_keys = [k for k, p in products.items()
                    if "select vacuum models" in (p.get("raw_description", "") or "")
                    or not p.get("brand")]
    if generic_keys:
        for k in random.sample(generic_keys, min(15, len(generic_keys))):
            p = products[k]
            print(f"\n  Name:  {p['clean_name']}")
            print(f"  Desc:  {p['description']}")
            print(f"  Brand: {p.get('brand', '')} | Model: {p.get('model', '')}")

    if args.dry:
        print("\n[DRY RUN] No files modified.")
        return

    # Save updated products
    with open(PRODUCTS_FILE, "w") as f:
        json.dump(products, f, indent=2)
    print(f"\nSaved {PRODUCTS_FILE}")

    # Re-export spreadsheet
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Products"
    headers = ["SKU", "Brand", "Model", "Clean Name", "Description", "Price", "In Stock", "Raw Name"]
    ws.append(headers)
    for key, p in products.items():
        ws.append([
            p.get("sku", key),
            p.get("brand", ""),
            p.get("model", ""),
            p.get("clean_name", ""),
            p.get("description", ""),
            p.get("price", ""),
            p.get("in_stock", ""),
            p.get("raw_name", ""),
        ])
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)

    out_path = OUTPUT_DIR / "product_descriptions.xlsx"
    wb.save(str(out_path))
    print(f"Spreadsheet exported to {out_path}")


if __name__ == "__main__":
    main()
