import cv2
import numpy as np
import os
import sys

# ============================================================
# Base folders
# ============================================================
input_base = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Tukaram.Tanpure\Desktop\Satellite_images_rename_rotate\RHS__L3\IMAGES_ALL_4_SORTED"
output_base = sys.argv[2] if len(sys.argv) > 2 else r"C:\Users\Tukaram.Tanpure\Desktop\Satellite_images_rename_rotate\RHS__L3\down_to_up_images"

os.makedirs(output_base, exist_ok=True)

# ============================================================
# Loop all subfolders
# ============================================================
for root, dirs, files in os.walk(input_base):

    for file in files:

        if not file.lower().endswith((".jpg", ".jpeg", ".png")):
            continue

        input_path = os.path.join(root, file)

        # Flat output path (no subfolders)
        output_path = os.path.join(output_base, file)

        print("Processing:", input_path)

        # ==============================
        # 1. Load image
        # ==============================
        img = cv2.imread(input_path)
        if img is None:
            print("ERROR: Cannot read:", file)
            continue

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # ==============================
        # 2. Threshold
        # ==============================
        _, thresh = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)

        # ==============================
        # 3. Contour
        # ==============================
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if len(contours) == 0:
            print("ERROR: No contour:", file)
            continue

        cnt = max(contours, key=cv2.contourArea)

        # ==============================
        # 4. PCA rotation
        # ==============================
        data_pts = np.squeeze(cnt).astype(np.float32)
        mean, eigenvectors = cv2.PCACompute(data_pts, mean=None)

        angle = np.arctan2(eigenvectors[0, 1], eigenvectors[0, 0])
        angle_deg = np.degrees(angle)

        (h, w) = img.shape[:2]
        center = (w // 2, h // 2)

        M = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
        rotated = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC)

        # ==============================
        # 5. Crop
        # ==============================
        gray_r = cv2.cvtColor(rotated, cv2.COLOR_BGR2GRAY)
        _, thresh_r = cv2.threshold(gray_r, 10, 255, cv2.THRESH_BINARY)

        contours_r, _ = cv2.findContours(thresh_r, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if len(contours_r) == 0:
            print("ERROR: No contour after rotation:", file)
            continue

        cnt_r = max(contours_r, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(cnt_r)
        cropped = rotated[y:y+h, x:x+w]

        # ==============================
        # 6. Fix direction (0 -> 100)
        # ==============================
        left_mean = np.mean(cropped[:, :10])
        right_mean = np.mean(cropped[:, -10:])

        if left_mean > right_mean:
            cropped = cv2.flip(cropped, 1)

        # ==============================
        # 7. Save (flat folder)
        # ==============================
        final_output_path = os.path.join(output_base, file)

        # avoid overwrite if duplicate filenames exist
        base, ext = os.path.splitext(file)
        counter = 1
        while os.path.exists(final_output_path):
            final_output_path = os.path.join(output_base, f"{base}_{counter}{ext}")
            counter += 1

        cv2.imwrite(final_output_path, cropped)

        print("Saved:", final_output_path)

print("\nAll images processed into single folder successfully")