# ===========================================================
#
# Rotate rectangles for all images in a folder
#
# (Even if no rectangle detected, original image will be saved)
#
# ============================================================

# --- Install dependencies ---
# !pip install opencv-python-headless numpy -q

# --- Import libraries ---
import cv2
import numpy as np
import os
import sys
from pathlib import Path
# from google.colab import drive # Moved to a separate cell

# --- Rotation function ---
def rotate_rectangle_to_horizontal(input_image_path, output_image_path, no_rectangle_output_path):
    img = cv2.imread(input_image_path)
    if img is None:
        print(f"ERROR: Could not read image -> {input_image_path}")
        return False

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # Green detection (adjust if needed)
    lower_green = np.array([40, 40, 40])
    upper_green = np.array([80, 255, 255])
    mask = cv2.inRange(hsv, lower_green, upper_green)

    # Fallback detection (if no green found)
    if np.sum(mask) == 0:
        _, mask = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY_INV)

    # Morph cleaning
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    # Find contours
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # --- Added Canny Edge Detection Fallback in the flow ---
    if not contours:
        edges = cv2.Canny(gray, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # No rectangle detected -> save image as-is in a separate folder
    if not contours:
        Path(os.path.dirname(no_rectangle_output_path)).mkdir(parents=True, exist_ok=True)
        Path(os.path.dirname(output_image_path)).mkdir(parents=True, exist_ok=True)
        cv2.imwrite(output_image_path, img)
        cv2.imwrite(no_rectangle_output_path, img)
        print(f"WARN: No rectangle detected -> saving original image to {no_rectangle_output_path}")
        return True

    # Use largest detected contour
    largest_contour = max(contours, key=cv2.contourArea)
    rect = cv2.minAreaRect(largest_contour)

    center, (width, height), angle = rect

    rotation_angle = angle + 90 if width < height else angle

    while rotation_angle > 45:
        rotation_angle -= 90
    while rotation_angle < -45:
        rotation_angle += 90

    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), rotation_angle, 1.0)

    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    new_w = int(h * sin + w * cos)
    new_h = int(h * cos + w * sin)

    M[0, 2] += (new_w - w) / 2
    M[1, 2] += (new_h - h) / 2

    rotated = cv2.warpAffine(img, M, (new_w, new_h), borderValue=(255, 255, 255))
    cv2.imwrite(output_image_path, rotated)

    print(f"Saved: {output_image_path}")
    return True

# --- Function to process entire folder ---
def process_folder(input_folder, output_folder):
    Path(output_folder).mkdir(parents=True, exist_ok=True)

    no_rectangle_subfolder = os.path.join(output_folder, "no_rectangle_detected")
    Path(no_rectangle_subfolder).mkdir(parents=True, exist_ok=True)

    file_count = 0
    for filename in os.listdir(input_folder):
        if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp')):
            file_count += 1
            input_path = os.path.join(input_folder, filename)
            output_path = os.path.join(output_folder, f"{filename}")
            no_rectangle_output_path = os.path.join(no_rectangle_subfolder, filename) # Save original filename without 'rotated_' prefix

            print(f"\nProcessing: {filename}")
            rotate_rectangle_to_horizontal(input_path, output_path, no_rectangle_output_path)

    if file_count == 0:
        print("WARN: No image files found in input folder.")
    else:
        print(f"\nCompleted! Processed {file_count} images.")

# --- Run batch rotation ---
# input_folder = "/content/drive/MyDrive/LHS__L2/IMAGES_All"   # Change this
# output_folder = "/content/drive/MyDrive/LHS__L2/IMAGES_All_2" # Change this
if __name__ == "__main__":
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    input_folder = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "RHS__L3", "IMAGES_ALL")
    output_folder = sys.argv[2] if len(sys.argv) > 2 else os.path.join(BASE_DIR, "RHS__L3", "IMAGES_ALL_2")
    process_folder(input_folder, output_folder)