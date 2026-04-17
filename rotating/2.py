# ===========================================================

# Rotate images to horizontal by always rotating clockwise

# ===========================================================

import cv2
import numpy as np
import os
import sys
from pathlib import Path



# --- Rotation function (always clockwise if needed) ---

def rotate_to_horizontal(input_image_path, output_image_path):
    img = cv2.imread(input_image_path)
    if img is None:
        print(f"ERROR: Cannot read {input_image_path}")
        return False

    h, w = img.shape[:2]

    # If image is vertical -> rotate clockwise 90 degrees
    if h > w:
        rotation_angle = -90  # clockwise
        M = cv2.getRotationMatrix2D((w / 2, h / 2), rotation_angle, 1.0)

        cos = abs(M[0, 0])
        sin = abs(M[0, 1])
        new_w = int(h * sin + w * cos)
        new_h = int(h * cos + w * sin)

        M[0, 2] += (new_w - w) / 2
        M[1, 2] += (new_h - h) / 2

        rotated = cv2.warpAffine(img, M, (new_w, new_h), borderValue=(255, 255, 255))
        cv2.imwrite(output_image_path, rotated)
        print(f"Rotated right -> {output_image_path}")

    else:
        cv2.imwrite(output_image_path, img)
        print(f"Already horizontal -> {output_image_path}")

    return True


# --- Process all images in folder ---

def process_folder(input_folder, output_folder):
    Path(output_folder).mkdir(parents=True, exist_ok=True)

    files = [
        f for f in os.listdir(input_folder)
        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp'))
    ]

    if not files:
        print("WARN: No image files found.")
        return True

    for filename in files:
        input_path = os.path.join(input_folder, filename)
        output_path = os.path.join(output_folder, f"{filename}")
        print(f"\nProcessing: {filename}")
        rotate_to_horizontal(input_path, output_path)

    print("\nCompleted rotation for all images!")


# --- Set folder paths (change for your Drive) ---

# input_folder  = "/content/drive/MyDrive/LHS__L1/IMAGES_All_2"
# output_folder = "/content/drive/MyDrive/LHS__L1/IMAGES_All_3"
if __name__ == "__main__":
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    input_folder = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "RHS__L3", "IMAGES_ALL_2")
    output_folder = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE_DIR, "RHS__L3", "IMAGES_ALL_3")
    process_folder(input_folder, output_folder)
