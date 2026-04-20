import cv2
import numpy as np
import os
import sys

# ==============================
# Input / output folders
# ==============================
input_folder = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Tukaram.Tanpure\Desktop\Satellite_images_rename_rotate\RHS__L3\down_to_up_images"

output_folder = sys.argv[2] if len(sys.argv) > 2 else r"C:\Users\Tukaram.Tanpure\Desktop\Satellite_images_rename_rotate\RHS__L3\up_to_down_images"

os.makedirs(output_folder, exist_ok=True)

# ==============================
# Process all images
# ==============================
for file in os.listdir(input_folder):

    if file.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
        input_path = os.path.join(input_folder, file)
        output_path = os.path.join(output_folder, file)

        print("Processing:", file)

        # ==============================
        # 1. Load image
        # ==============================
        img = cv2.imread(input_path)
        if img is None:
            print("Skipped:", file)
            continue

        # ==============================
        # 2. Flip vertically (reverse)
        # ==============================
        flipped = cv2.flip(img, 0)   # 0 = vertical flip

        # ==============================
        # 3. Ensure left-to-right direction
        # (0 should be TOP, 100 bottom)
        # ==============================
        gray = cv2.cvtColor(flipped, cv2.COLOR_BGR2GRAY)

        top_mean = np.mean(gray[:10, :])
        bottom_mean = np.mean(gray[-10:, :])

        # If upside down, fix it
        if top_mean > bottom_mean:
            flipped = cv2.flip(flipped, 0)

        # ==============================
        # 4. Save
        # ==============================
        cv2.imwrite(output_path, flipped)

        print("Saved:", output_path)

print("\nReverse rotation completed!")