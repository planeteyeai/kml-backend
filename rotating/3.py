import cv2
import numpy as np
import os
import shutil
import sys
from pathlib import Path

# ===========================================================
# Set your paths here
# ===========================================================
INPUT_FOLDER = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Tukaram.Tanpure\Desktop\Satellite_images_rename_rotate\RHS__L3\IMAGES_ALL_3"

OUTPUT_BASE = sys.argv[2] if len(sys.argv) > 2 else r"C:\Users\Tukaram.Tanpure\Desktop\Satellite_images_rename_rotate\RHS__L3\IMAGES_ALL_4_SORTED"

SLANTED_FOLDER = os.path.join(OUTPUT_BASE, "SLANTED")
STRAIGHT_FOLDER = os.path.join(OUTPUT_BASE, "STRAIGHT")
NONE_FOLDER = os.path.join(OUTPUT_BASE, "NONE_IMG")

# Create output folders
Path(SLANTED_FOLDER).mkdir(parents=True, exist_ok=True)
Path(STRAIGHT_FOLDER).mkdir(parents=True, exist_ok=True)
Path(NONE_FOLDER).mkdir(parents=True, exist_ok=True)

# ===========================================================
# Function: detect angle
# ===========================================================

def get_line_angle(image_path):
    img = cv2.imread(image_path)
    if img is None:
        return None

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Improve edge detection (more robust)
    edges = cv2.Canny(gray, 30, 200)

    # Detect lines
    lines = cv2.HoughLines(edges, 1, np.pi/180, 80)

    if lines is None:
        return None

    # Take first strong line
    rho, theta = lines[0][0]
    angle = np.degrees(theta)

    # Normalize angle (-90 to +90)
    if angle > 90:
        angle -= 180

    return angle

# ===========================================================
# Main process
# ===========================================================

total = 0
slanted_count = 0
straight_count = 0
none_count = 0

print("Starting image classification...\n")

for file in os.listdir(INPUT_FOLDER):

    if not file.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp')):
        continue

    total += 1
    file_path = os.path.join(INPUT_FOLDER, file)

    angle = get_line_angle(file_path)

    # =======================================================
    # No line detected -> NONE folder
    # =======================================================
    if angle is None:
        print(f"{file} -> No line detected -> NONE")
        destination = os.path.join(NONE_FOLDER, file)
        shutil.copy(file_path, destination)
        none_count += 1
        continue

    print(f"{file} -> Angle: {angle:.2f}")

    # =======================================================
    # Classification logic
    # =======================================================

    if -10 <= angle <= 10 or 80 <= abs(angle) <= 100:
        destination = os.path.join(STRAIGHT_FOLDER, file)
        straight_count += 1
        print("-> STRAIGHT")
    else:
        destination = os.path.join(SLANTED_FOLDER, file)
        slanted_count += 1
        print("-> SLANTED")

    shutil.copy(file_path, destination)

# ===========================================================
# Final summary
# ===========================================================

print("\n====================================")
print(f"TOTAL IMAGES: {total}")
print(f"STRAIGHT: {straight_count}")
print(f"SLANTED: {slanted_count}")
print(f"NONE (No Line Detected): {none_count}")
print("DONE! Images separated successfully.")