#!/usr/bin/env python3
"""Generate contact sheets of ALL processed images for visual watermark review."""

import os
from PIL import Image, ImageDraw, ImageFont

PROC_DIR = "images"
SHEET_DIR = "review_sheets"

SHEET_COLS = 6
SHEET_ROWS = 5
THUMB_SIZE = 310
LABEL_HEIGHT = 25
SHEET_PADDING = 10
IMAGES_PER_SHEET = SHEET_COLS * SHEET_ROWS


def get_font():
    for path in ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFCompact.ttf"]:
        try:
            return ImageFont.truetype(path, 14)
        except Exception:
            continue
    return ImageFont.load_default()


def main():
    # Get all parts sorted alphabetically
    parts = sorted(d for d in os.listdir(PROC_DIR)
                   if os.path.isfile(os.path.join(PROC_DIR, d, "1.jpg")))
    total = len(parts)
    print(f"Generating contact sheets for {total} images...")

    os.makedirs(SHEET_DIR, exist_ok=True)
    # Clean old sheets
    for f in os.listdir(SHEET_DIR):
        if f.startswith("all_"):
            os.remove(os.path.join(SHEET_DIR, f))

    font = get_font()
    cell_w = THUMB_SIZE + SHEET_PADDING
    cell_h = THUMB_SIZE + LABEL_HEIGHT + SHEET_PADDING
    sheet_w = SHEET_COLS * cell_w + SHEET_PADDING
    sheet_h = SHEET_ROWS * cell_h + SHEET_PADDING

    sheet_num = 0
    for batch_start in range(0, total, IMAGES_PER_SHEET):
        batch = parts[batch_start:batch_start + IMAGES_PER_SHEET]
        sheet_num += 1

        sheet = Image.new("RGB", (sheet_w, sheet_h), (255, 255, 255))
        draw = ImageDraw.Draw(sheet)

        for idx, part in enumerate(batch):
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

            label = part[:28]
            draw.text((x + 5, y + THUMB_SIZE + 2), label, fill=(0, 0, 0), font=font)

        filename = f"all_{sheet_num:03d}.jpg"
        sheet.save(os.path.join(SHEET_DIR, filename), "JPEG", quality=90)

        if sheet_num % 20 == 0:
            print(f"  Sheet {sheet_num}...")

    print(f"Done! {sheet_num} contact sheets in {SHEET_DIR}/")


if __name__ == "__main__":
    main()
