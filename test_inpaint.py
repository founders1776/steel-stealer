#!/usr/bin/env python3
"""Test targeted inpainting to remove Steel City watermark from a single product image."""

import cv2
import numpy as np
from PIL import Image

PART = "01-7887-01"
INPUT_PATH = f"images/{PART}/1.jpg"
RAW_PATH = f"images_raw/{PART}.jpg"
OUTPUT_PATH = f"images/{PART}/1_inpainted.jpg"
MASK_PATH = f"images/{PART}/mask_debug.png"

def detect_watermark_on_product(img_rgb):
    """
    Detect the Steel City watermark text that overlaps the product.

    Strategy:
    1. Find product pixels (non-white, non-logo areas)
    2. Within product pixels, look for the watermark text which appears as
       slightly different colored text (usually bluish/darker) on the product surface
    3. The watermark text "steelcityvac.com" with the swoosh appears in the
       center-left area of the original image, which maps to a specific region
       on the processed 2048x2048 canvas
    """
    h, w = img_rgb.shape[:2]

    # Convert to different color spaces for analysis
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    lab = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2LAB)

    # Step 1: Create product mask (anything that's not the white background or faint VaM logo)
    # VaM logo is very faint (>230 per channel), background is white (>245)
    # Product pixels are substantially darker
    product_mask = np.any(img_rgb < 200, axis=2).astype(np.uint8) * 255

    # Step 2: Within the product area, detect the watermark text
    # The Steel City watermark appears as slightly blue-tinted text on the product
    # It's typically in the lower-left to center area of the product

    # Look for blue-ish pixels within the product area
    # The watermark text has a blue/navy tint that differs from the product's green/dark color
    b_channel = img_rgb[:, :, 2].astype(float)  # Blue channel
    r_channel = img_rgb[:, :, 0].astype(float)  # Red channel
    g_channel = img_rgb[:, :, 1].astype(float)  # Green channel

    # The watermark text tends to have higher blue relative to red/green compared
    # to the surrounding product pixels. Also look at the LAB color space -
    # the 'b' channel in LAB detects blue-yellow axis
    l_ch, a_ch, b_ch = lab[:, :, 0], lab[:, :, 1], lab[:, :, 2]

    # Method: Compare each product pixel to its local neighborhood
    # Watermark text creates local color anomalies - slightly different hue/saturation
    # compared to surrounding product pixels

    # Use a local standard deviation approach on the blue channel within product area
    # Watermark text creates subtle but detectable local variations

    # Blur to get local average
    kernel_size = 15
    local_mean_b = cv2.blur(b_channel, (kernel_size, kernel_size))
    local_mean_r = cv2.blur(r_channel, (kernel_size, kernel_size))
    local_mean_g = cv2.blur(g_channel, (kernel_size, kernel_size))

    # The watermark text has blue that deviates from the local mean
    # (it's more blue than surrounding product pixels)
    blue_deviation = b_channel - local_mean_b

    # Also check: watermark pixels tend to have blue > green and blue > red locally
    blue_dominant = (b_channel > r_channel + 3) & (b_channel > g_channel + 3)

    # Combine: pixels that are on the product AND have blue deviation AND blue dominant
    watermark_candidate = (
        (product_mask > 0) &  # On the product
        (blue_deviation > 5) &  # Blue is higher than local average
        blue_dominant  # Blue is dominant channel
    ).astype(np.uint8) * 255

    # Clean up with morphological operations
    # Close small gaps in text
    kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    watermark_candidate = cv2.morphologyEx(watermark_candidate, cv2.MORPH_CLOSE, kernel_close)

    # Remove tiny noise
    kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    watermark_candidate = cv2.morphologyEx(watermark_candidate, cv2.MORPH_OPEN, kernel_open)

    # Dilate slightly to ensure full coverage of watermark edges
    kernel_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    watermark_mask = cv2.dilate(watermark_candidate, kernel_dilate, iterations=1)

    # Only keep the mask where it overlaps product pixels
    watermark_mask = cv2.bitwise_and(watermark_mask, product_mask)

    return watermark_mask


def inpaint_watermark(img_path, output_path, mask_debug_path=None):
    """Load image, detect watermark, inpaint it, save result."""

    # Load the processed image
    img_pil = Image.open(img_path).convert("RGB")
    img_rgb = np.array(img_pil)
    img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)

    print(f"Image size: {img_rgb.shape}")

    # Detect watermark
    mask = detect_watermark_on_product(img_rgb)

    # Count mask pixels
    mask_pixels = np.count_nonzero(mask)
    total_pixels = mask.shape[0] * mask.shape[1]
    print(f"Watermark mask: {mask_pixels} pixels ({mask_pixels/total_pixels*100:.2f}% of image)")

    if mask_pixels == 0:
        print("No watermark detected!")
        return False

    # Save debug mask
    if mask_debug_path:
        # Create a visualization: original with red overlay where mask is
        debug_img = img_rgb.copy()
        debug_img[mask > 0] = [255, 0, 0]  # Red where watermark detected
        debug_pil = Image.fromarray(debug_img)
        debug_pil.save(mask_debug_path)
        print(f"Debug mask saved to {mask_debug_path}")

    # Inpaint using OpenCV
    # TELEA method works well for small text removal
    result_bgr = cv2.inpaint(img_bgr, mask, inpaintRadius=5, flags=cv2.INPAINT_TELEA)

    # Convert back to RGB and save
    result_rgb = cv2.cvtColor(result_bgr, cv2.COLOR_BGR2RGB)
    result_pil = Image.fromarray(result_rgb)
    result_pil.save(output_path, "JPEG", quality=95)
    print(f"Inpainted image saved to {output_path}")

    return True


if __name__ == "__main__":
    print(f"Testing inpainting on {PART}...")
    success = inpaint_watermark(INPUT_PATH, OUTPUT_PATH, MASK_PATH)
    if success:
        print("\nDone! Compare:")
        print(f"  Original:  {INPUT_PATH}")
        print(f"  Inpainted: {OUTPUT_PATH}")
        print(f"  Mask:      {MASK_PATH}")
    else:
        print("\nFailed - no watermark detected")
