#!/usr/bin/env python3
"""
Full Road Pipeline
- Read input LineString KML
- Interpolate every INTERVAL_METERS (default 5 m)
- Create chainage Excel (point-based) with Chainage Start / Chainage End
- Compute Median_LHS / Median_RHS (offset from center)
- Create lane layers (L1/L2/L3) for left/right based on LANE_COUNT with LANE_STEP_M
- Produce per-layer KMLs grouped into bins anchored at CHAINAGE_START_KM with bin size KML_MERGE_OFFSET_KM (km)
- Produce merged KML per layer
- Produce per-side merge folders (LHS_kml_merge / RHS_kml_merge): one KML per chainage bin, merging L1+L2+L3 for that bin (e.g. Chainage_0.000_to_0.100_LHS_merged.kml)
- Produce high-quality per-KML road strip PNGs under LHS_KMLs/LHS_images and RHS_KMLs/RHS_images
"""
import sys
import os
import re
import math
import json
import time
import random
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from datetime import datetime, timedelta, timezone
from xml.dom import minidom
from pyproj import Geod
import numpy as np
import pandas as pd
from geopy.distance import geodesic
from geopy import Point
import simplekml
import requests
from PIL import Image, ImageDraw, ImageOps, ImageFilter

# -----------------------------
# USER CONFIG (edit paths & params)
# -----------------------------
if len(sys.argv) >= 9:
    INPUT_KML = sys.argv[1]
    OUTPUT_FOLDER = sys.argv[2]
    CHAINAGE_START_KM = float(sys.argv[3])
    INTERVAL_METERS = float(sys.argv[4])
    LANE_COUNT = int(sys.argv[5])
    KML_MERGE_OFFSET_KM = float(sys.argv[6])
    LANE_STEP_M = float(sys.argv[7])
    OFFSET_LINE_POLYGONS_EXCEL = float(sys.argv[8])
else:
    INPUT_KML = "C:\\Users\\Rudra.Joshi\\Desktop\\kml_web\\kml_creation\\input.kml"
    OUTPUT_FOLDER = "C:\\Users\\Rudra.Joshi\\Desktop\\kml_web\\pipeline"
    CHAINAGE_START_KM = 0  #change
    INTERVAL_METERS = 1
    LANE_COUNT = 4                      #change # allowed values: 0,2,4,6. 2 -> L1 only, 4 -> L1+L2, 6 -> L1+L2+L3
    LANE_STEP_M =  3.4              # meters per lane offset step
    KML_MERGE_OFFSET_KM = 0.100      #change # 0.100 km -> 100 m bins
    OFFSET_LINE_POLYGONS_EXCEL = 2.75  #change # meters (median left/right offset)

CHAINAGE_DECIMALS = 3

# Per-bin KML names from generate_layer_bin_kmls: Chainage_{start}_to_{end}_{LHS_L1}.kml
CHAINAGE_BIN_KML_RE = re.compile(
    r"^Chainage_([\d.]+)_to_([\d.]+)_(LHS_L\d+|RHS_L\d+)\.kml$",
    re.IGNORECASE,
)
SIDE_LANE_FOLDER_RE = re.compile(r"^(LHS|RHS)_L(\d+)$", re.IGNORECASE)

STAC_API = os.environ.get("STAC_API", "https://earth-search.aws.element84.com/v1")
STAC_COLLECTION = os.environ.get("STAC_COLLECTION", "sentinel-2-l2a")
TITILER_COG_API = os.environ.get("TITILER_COG_API", "https://titiler.xyz/cog")
MAX_CLOUD_COVER = float(os.environ.get("MAX_CLOUD_COVER", "20"))
MAX_NODATA_PERCENT = float(os.environ.get("MAX_NODATA_PERCENT", "35"))
SATELLITE_LOOKBACK_DAYS = int(os.environ.get("SATELLITE_LOOKBACK_DAYS", "30"))
SATELLITE_DATE_START = os.environ.get("SATELLITE_DATE_START", "2026-02-10").strip()
SATELLITE_DATE_END = os.environ.get("SATELLITE_DATE_END", "2026-02-20").strip()
SATELLITE_IMAGE_WIDTH = int(os.environ.get("SATELLITE_IMAGE_WIDTH", "4096"))
SATELLITE_IMAGE_HEIGHT = int(os.environ.get("SATELLITE_IMAGE_HEIGHT", "1536"))
SATELLITE_SMOOTHING = os.environ.get("SATELLITE_SMOOTHING", "true").lower() == "true"
SATELLITE_MASK_SUPERSAMPLE = int(os.environ.get("SATELLITE_MASK_SUPERSAMPLE", "4"))
SATELLITE_MASK_FEATHER_PX = float(os.environ.get("SATELLITE_MASK_FEATHER_PX", "0.75"))
SATELLITE_COLOR_MICROBLUR_PX = float(os.environ.get("SATELLITE_COLOR_MICROBLUR_PX", "0.5"))
SYNTHETIC_GRADIENT_GAUSSIAN_PX = float(os.environ.get("SYNTHETIC_GRADIENT_GAUSSIAN_PX", "0.85"))
IMAGE_GEN_MAX_WORKERS = int(os.environ.get("IMAGE_GEN_MAX_WORKERS", "6"))
SATELLITE_FETCH_SCALE = int(os.environ.get("SATELLITE_FETCH_SCALE", "3"))
SATELLITE_ENABLE_SHARPEN = os.environ.get("SATELLITE_ENABLE_SHARPEN", "true").lower() == "true"
SATELLITE_SHARPEN_RADIUS = float(os.environ.get("SATELLITE_SHARPEN_RADIUS", "1.0"))
SATELLITE_SHARPEN_PERCENT = int(os.environ.get("SATELLITE_SHARPEN_PERCENT", "175"))
SATELLITE_SHARPEN_THRESHOLD = int(os.environ.get("SATELLITE_SHARPEN_THRESHOLD", "2"))
FALSE_COLOR_GAIN = float(os.environ.get("FALSE_COLOR_GAIN", "2.5"))
HIGHLIGHT_MAX_INPUT = float(os.environ.get("HIGHLIGHT_MAX_INPUT", "0.4"))
HIGHLIGHT_CLIP_INPUT = float(os.environ.get("HIGHLIGHT_CLIP_INPUT", "0.8"))
HIGHLIGHT_MAX_OUTPUT = float(os.environ.get("HIGHLIGHT_MAX_OUTPUT", "1.0"))
RED_GRADIENT_GAMMA = float(os.environ.get("RED_GRADIENT_GAMMA", "1.25"))
FOCAL_MEAN_RADIUS_METERS = float(os.environ.get("FOCAL_MEAN_RADIUS_METERS", "5.0"))
ALLOW_SYNTHETIC_FALLBACK = os.environ.get("ALLOW_SYNTHETIC_FALLBACK", "false").lower() == "true"
APPLY_HIGHLIGHT_COMPRESS = os.environ.get("APPLY_HIGHLIGHT_COMPRESS", "false").lower() == "true"
APPLY_CUSTOM_RED_GRADIENT = os.environ.get("APPLY_CUSTOM_RED_GRADIENT", "false").lower() == "true"
SATELLITE_BAND_RESCALE = os.environ.get("SATELLITE_BAND_RESCALE", "0,3000").strip()
# Google Earth Engine: optional render path (focal_mean in meters, then visualize + clip).
EARTH_ENGINE_RENDER = os.environ.get("EARTH_ENGINE_RENDER", "false").lower() == "true"
EE_PROJECT = os.environ.get("EE_PROJECT", "").strip()
EE_FOCAL_MEAN_RADIUS_M = float(os.environ.get("EE_FOCAL_MEAN_RADIUS_M", "5"))
EE_VIS_MAX_REFLECTANCE = float(os.environ.get("EE_VIS_MAX_REFLECTANCE", "0.35"))
IMAGE_DIRECTION = os.environ.get("IMAGE_DIRECTION", "down_to_up").strip().lower() or "down_to_up"
IMAGE_GEN_CHUNK_SIZE = int(os.environ.get("IMAGE_GEN_CHUNK_SIZE", "18"))
IMAGE_GEN_CHUNK_BASE_DELAY_SEC = float(os.environ.get("IMAGE_GEN_CHUNK_BASE_DELAY_SEC", "0.35"))
IMAGE_GEN_CHUNK_MAX_DELAY_SEC = float(os.environ.get("IMAGE_GEN_CHUNK_MAX_DELAY_SEC", "8.0"))
IMAGE_GEN_CHUNK_BACKOFF_MULTIPLIER = float(os.environ.get("IMAGE_GEN_CHUNK_BACKOFF_MULTIPLIER", "1.8"))
IMAGE_GEN_CHUNK_SUCCESS_DECAY = float(os.environ.get("IMAGE_GEN_CHUNK_SUCCESS_DECAY", "0.7"))
SATELLITE_HTTP_MAX_RETRIES = int(os.environ.get("SATELLITE_HTTP_MAX_RETRIES", "5"))
SATELLITE_HTTP_BACKOFF_BASE_SEC = float(os.environ.get("SATELLITE_HTTP_BACKOFF_BASE_SEC", "0.8"))
SATELLITE_HTTP_BACKOFF_MAX_SEC = float(os.environ.get("SATELLITE_HTTP_BACKOFF_MAX_SEC", "12.0"))
SATELLITE_INTER_BAND_DELAY_SEC = float(os.environ.get("SATELLITE_INTER_BAND_DELAY_SEC", "0.15"))
SATELLITE_FETCH_MAX_EDGE_PX = int(os.environ.get("SATELLITE_FETCH_MAX_EDGE_PX", "8192"))
SATELLITE_MAX_PARALLEL_BAND_FETCH = int(os.environ.get("SATELLITE_MAX_PARALLEL_BAND_FETCH", "2"))

# geodetic util
geod = Geod(ellps="WGS84")
_http_session = requests.Session()
_band_fetch_slots = None
if SATELLITE_MAX_PARALLEL_BAND_FETCH > 0:
    import threading
    _band_fetch_slots = threading.BoundedSemaphore(max(1, SATELLITE_MAX_PARALLEL_BAND_FETCH))

# output folders
KML_LHS_FOLDER = os.path.join(OUTPUT_FOLDER, "LHS_KMLs")
KML_RHS_FOLDER = os.path.join(OUTPUT_FOLDER, "RHS_KMLs")
EXCEL_FOLDER = os.path.join(OUTPUT_FOLDER, "Excels")
KML_MERGED_FOLDER = os.path.join(OUTPUT_FOLDER, "Merge_KMLs")

# -----------------------------
# Utility functions
# -----------------------------
def _road_mask_for_composite(rings, min_lon, min_lat, max_lon, max_lat, render_w, render_h, ms, feather_px):
    """
    Anti-aliased soft mask for pasting road imagery: supersample polygon, downscale,
    then optional Gaussian feather so edges and in-strip transitions blend smoothly.
    """
    mask_w = render_w * ms
    mask_h = render_h * ms
    road_mask = Image.new("L", (mask_w, mask_h), 0)
    md = ImageDraw.Draw(road_mask)
    for ring in rings:
        pts = []
        for lon, lat in ring:
            x = (lon - min_lon) * mask_w / (max_lon - min_lon)
            y = (max_lat - lat) * mask_h / (max_lat - min_lat)
            pts.append((x, y))
        if len(pts) >= 3:
            md.polygon(pts, fill=255)
    if ms > 1:
        road_mask = road_mask.resize((render_w, render_h), Image.Resampling.LANCZOS)
    if feather_px > 1e-6:
        road_mask = road_mask.filter(ImageFilter.GaussianBlur(radius=float(feather_px)))
    return road_mask


def _apply_output_sharpen(img):
    if not SATELLITE_ENABLE_SHARPEN:
        return img
    radius = max(0.1, float(SATELLITE_SHARPEN_RADIUS))
    percent = max(0, int(SATELLITE_SHARPEN_PERCENT))
    threshold = max(0, int(SATELLITE_SHARPEN_THRESHOLD))
    return img.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=threshold))


def clear_folder(folder_path):
    """Delete all files and subfolders inside the given folder."""
    if os.path.exists(folder_path):
        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f"Failed to delete {file_path}. Reason: {e}")


def _extract_polygon_rings_from_kml(kml_path):
    """Return polygon outer rings as list of [(lon, lat), ...] from a KML file."""
    rings = []
    try:
        doc = minidom.parse(kml_path)
    except Exception:
        return rings
    for polygon in doc.getElementsByTagName("Polygon"):
        coord_nodes = polygon.getElementsByTagName("coordinates")
        if not coord_nodes or not coord_nodes[0].firstChild:
            continue
        coord_text = coord_nodes[0].firstChild.nodeValue.strip()
        if not coord_text:
            continue
        ring = []
        for pair in coord_text.split():
            parts = pair.split(",")
            if len(parts) < 2:
                continue
            try:
                lon = float(parts[0])
                lat = float(parts[1])
                ring.append((lon, lat))
            except Exception:
                continue
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _lerp_color(c1, c2, t):
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _longest_ring_edge_unit_vector(pixel_rings):
    """Unit vector along the longest edge of the pixel-space rings (approx. road axis)."""
    best_len = 0.0
    ux, uy = 0.0, 1.0
    for pts in pixel_rings:
        n = len(pts)
        if n < 2:
            continue
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            dx, dy = x1 - x0, y1 - y0
            L = math.hypot(dx, dy)
            if L > best_len:
                best_len = L
                ux, uy = dx / L, dy / L
    return ux, uy


def _project_scalar_range_on_axis(pixel_rings, ux, uy):
    """Min/max of dot(p, u) over all vertices, for normalizing gradient parameter t."""
    projs = []
    for pts in pixel_rings:
        for px, py in pts:
            projs.append(px * ux + py * uy)
    if not projs:
        return 0.0, 1.0
    return float(min(projs)), float(max(projs))


def _build_datetime_range():
    if SATELLITE_DATE_START and SATELLITE_DATE_END:
        start = _normalize_iso_date(SATELLITE_DATE_START)
        end = _normalize_iso_date(SATELLITE_DATE_END)
        return f"{start}T00:00:00Z/{end}T23:59:59Z"
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=SATELLITE_LOOKBACK_DAYS)
    start_iso = start_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    end_iso = end_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f"{start_iso}/{end_iso}"


def _build_day_datetime_range(day_str):
    start_iso = f"{day_str}T00:00:00Z"
    end_iso = f"{day_str}T23:59:59Z"
    return f"{start_iso}/{end_iso}"


def _normalize_iso_date(date_str):
    """Accept YYYY-MM-DD or DD/MM/YYYY and return YYYY-MM-DD."""
    if "/" in date_str:
        d, m, y = date_str.split("/")
        return f"{y}-{m}-{d}"
    return date_str


def _run_rotation_chain_for_folder(image_folder, direction):
    """
    Run backend/rotating/*.py on merge and lane images (see IMAGE_DIRECTION env).

    Maps to UI direction (App.js):
      - down_to_up  ("South To North"): 1.py -> 2.py -> 3.py -> 4.py
      - up_to_down  ("North To South"): 1.py -> 2.py -> 3.py -> 4.py -> 5.py

    Final images replace originals in image_folder. Requires opencv-python-headless.
    """
    if not os.path.isdir(image_folder):
        return 0
    files = [
        f for f in os.listdir(image_folder)
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp"))
    ]
    if not files:
        return 0

    direction = (direction or "down_to_up").strip().lower()
    if direction not in {"down_to_up", "up_to_down"}:
        direction = "down_to_up"

    rotating_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rotating")
    required_scripts = ["1.py", "2.py", "3.py", "4.py"]
    if direction == "up_to_down":
        required_scripts.append("5.py")
    missing_scripts = [
        script_name
        for script_name in required_scripts
        if not os.path.isfile(os.path.join(rotating_dir, script_name))
    ]
    if missing_scripts:
        print(
            f"WARN: Rotation scripts missing ({', '.join(missing_scripts)}) in {rotating_dir}. "
            "Skipping rotation and keeping original images."
        )
        return 0

    python_exe = sys.executable or "python"
    work_root = os.path.join(image_folder, "_rotation_work")
    if os.path.exists(work_root):
        shutil.rmtree(work_root, ignore_errors=True)
    os.makedirs(work_root, exist_ok=True)

    stage2 = os.path.join(work_root, "stage2")
    stage3 = os.path.join(work_root, "stage3")
    stage4_sorted = os.path.join(work_root, "stage4_sorted")
    final_dir = os.path.join(work_root, "final")
    stage_map = {
        "1.py": [image_folder, stage2],
        "2.py": [stage2, stage3],
        "3.py": [stage3, stage4_sorted],
        "4.py": [stage4_sorted, final_dir],
    }
    if direction == "up_to_down":
        up_to_down_dir = os.path.join(work_root, "up_to_down")
        stage_map["5.py"] = [final_dir, up_to_down_dir]
        final_dir = up_to_down_dir

    for script_name in ("1.py", "2.py", "3.py", "4.py", "5.py"):
        if script_name not in stage_map:
            continue
        script_path = os.path.join(rotating_dir, script_name)
        args = [python_exe, script_path, *stage_map[script_name]]
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            raise RuntimeError(
                f"Rotation script {script_name} failed for {image_folder}: {err}"
            )

    if not os.path.isdir(final_dir):
        shutil.rmtree(work_root, ignore_errors=True)
        print(f"WARN: Rotation final dir missing for {image_folder}, keeping originals.")
        return 0

    final_files = [
        f for f in os.listdir(final_dir)
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp"))
    ]
    if not final_files and files:
        shutil.rmtree(work_root, ignore_errors=True)
        print(
            f"WARN: Rotation chain produced no images for {image_folder} "
            f"(direction={direction}); keeping originals. "
            "Check OpenCV (opencv-python-headless) and rotating/*.py logs."
        )
        return 0

    for name in files:
        src = os.path.join(image_folder, name)
        if os.path.isfile(src):
            os.unlink(src)
    for name in final_files:
        shutil.copy2(os.path.join(final_dir, name), os.path.join(image_folder, name))
    shutil.rmtree(work_root, ignore_errors=True)
    return len(final_files)


def _run_rotation_chain_for_side_images(side_images_root, direction):
    total = 0
    if not os.path.isdir(side_images_root):
        return total
    for lane_name in os.listdir(side_images_root):
        lane_dir = os.path.join(side_images_root, lane_name)
        if not os.path.isdir(lane_dir):
            continue
        total += _run_rotation_chain_for_folder(lane_dir, direction)
    return total


def _rings_to_geojson_polygon(rings):
    outer = rings[0]
    if outer[0] != outer[-1]:
        outer = outer + [outer[0]]
    return {"type": "Polygon", "coordinates": [outer]}


def _bbox_from_rings(rings):
    min_lon = min(p[0] for r in rings for p in r)
    max_lon = max(p[0] for r in rings for p in r)
    min_lat = min(p[1] for r in rings for p in r)
    max_lat = max(p[1] for r in rings for p in r)
    return min_lon, min_lat, max_lon, max_lat


def _item_date_str(item):
    dt = ((item.get("properties") or {}).get("datetime") or "")[:10]
    return dt if len(dt) == 10 else None


def _item_nodata_percent(item):
    props = item.get("properties") or {}
    val = props.get("s2:nodata_pixel_percentage")
    try:
        return float(val)
    except Exception:
        return None


def _get_false_color_asset_hrefs(item):
    """
    Sentinel-2 L2A false color requested by user:
      R <- B08 (nir)
      G <- B04 (red)
      B <- B03 (green)
    """
    assets = item.get("assets") or {}
    b08_href = (assets.get("nir") or assets.get("nir08") or {}).get("href")
    b04_href = (assets.get("red") or {}).get("href")
    b03_href = (assets.get("green") or {}).get("href")
    if b08_href and b04_href and b03_href:
        return {"b08": b08_href, "b04": b04_href, "b03": b03_href}
    return None


def _highlight_compress_lut(min_val=0.0, max_input=0.4, clip_input=0.8, max_output=1.0, gain=2.5):
    """
    Build 8-bit LUT approximating Sentinel-Hub HighlightCompressVisualizer
    behavior over normalized range [min_val, max_val].
    """
    lut = []
    max_input = max(min_val + 1e-9, max_input)
    clip_input = max(max_input + 1e-9, clip_input)
    max_output = max(0.0, min(1.0, max_output))
    log_den = math.log1p(9.0)
    for i in range(256):
        x = i / 255.0
        v = gain * max(0.0, x - min_val)
        if v <= max_input:
            # Linear response in low reflectance region.
            y = 0.75 * (v / max_input)
        elif v >= clip_input:
            y = max_output
        else:
            # Smoothly compress highlights between max_input and clip_input.
            t = (v - max_input) / (clip_input - max_input)
            y = 0.75 + 0.25 * (math.log1p(9.0 * t) / log_den)
            y = y * max_output
        lut.append(int(round(y * 255.0)))
    return lut


def _apply_bright_red_gradient(img, gamma=1.25):
    """
    Apply the requested bright red 3-stop corridor gradient:
      start #B84A4A, mid #E53935, end #FF2D2D
    with slight gamma lift to brighten mid-tones.
    """
    start = (0xB8, 0x4A, 0x4A)
    mid = (0xE5, 0x39, 0x35)
    end = (0xFF, 0x2D, 0x2D)

    gray = ImageOps.grayscale(img)
    # Stretch to full dynamic range for clean gradient utilization.
    gray = ImageOps.autocontrast(gray, cutoff=1)

    gamma = max(1.0, min(2.0, float(gamma)))
    inv_gamma = 1.0 / gamma
    lut_r = []
    lut_g = []
    lut_b = []
    for i in range(256):
        t = i / 255.0
        t = pow(t, inv_gamma)  # lift mids slightly
        if t <= 0.5:
            u = t / 0.5
            c = _lerp_color(start, mid, u)
        else:
            u = (t - 0.5) / 0.5
            c = _lerp_color(mid, end, u)
        lut_r.append(c[0])
        lut_g.append(c[1])
        lut_b.append(c[2])
    r = gray.point(lut_r)
    g = gray.point(lut_g)
    b = gray.point(lut_b)
    return Image.merge("RGB", (r, g, b))


def _http_request_with_retry(method, url, timeout, retry_statuses=None, **kwargs):
    """
    Retry transient network/rate-limit failures with exponential backoff + jitter.
    Keeps logic same while improving reliability for large KML batches.
    """
    retry_statuses = retry_statuses or {429, 500, 502, 503, 504}
    retries = max(1, SATELLITE_HTTP_MAX_RETRIES)
    backoff_base = max(0.05, SATELLITE_HTTP_BACKOFF_BASE_SEC)
    backoff_max = max(backoff_base, SATELLITE_HTTP_BACKOFF_MAX_SEC)

    last_exc = None
    for attempt in range(retries):
        try:
            rsp = _http_session.request(method, url, timeout=timeout, **kwargs)
            # Retry selected status codes with respect for Retry-After when present.
            if rsp.status_code in retry_statuses and attempt < retries - 1:
                retry_after = rsp.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        wait_s = float(retry_after)
                    except Exception:
                        wait_s = backoff_base * (2 ** attempt)
                else:
                    wait_s = backoff_base * (2 ** attempt)
                wait_s = min(backoff_max, wait_s) + random.uniform(0.0, 0.35)
                time.sleep(wait_s)
                continue
            rsp.raise_for_status()
            return rsp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= retries - 1:
                break
            wait_s = min(backoff_max, backoff_base * (2 ** attempt)) + random.uniform(0.0, 0.35)
            time.sleep(wait_s)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("HTTP request failed without response.")


def _stac_search(geom, datetime_range, limit=60, sort_desc=True):
    search_url = f"{STAC_API.rstrip('/')}/search"
    body = {
        "collections": [STAC_COLLECTION],
        "limit": limit,
        "datetime": datetime_range,
        "intersects": geom,
        "query": {"eo:cloud_cover": {"lt": MAX_CLOUD_COVER}},
        "sortby": [{"field": "properties.datetime", "direction": "desc" if sort_desc else "asc"}],
    }
    try:
        r = _http_request_with_retry(
            "POST",
            search_url,
            json=body,
            timeout=40,
            retry_statuses={429, 500, 502, 503, 504},
        )
        r.raise_for_status()
        return r.json().get("features", [])
    except Exception as e:
        print(f"[IMG] STAC search failed: {e}")
        return []


def _search_latest_stac_visual_item(rings, forced_date=None):
    geom = _rings_to_geojson_polygon(rings)
    dt_range = _build_day_datetime_range(forced_date) if forced_date else _build_datetime_range()
    limit = 12 if forced_date else 40
    features = _stac_search(geom, dt_range, limit=limit, sort_desc=True)
    for f in features:
        false_color_assets = _get_false_color_asset_hrefs(f)
        ndp = _item_nodata_percent(f)
        if false_color_assets and (ndp is None or ndp <= MAX_NODATA_PERCENT):
            return f
    return None


def _choose_consistent_date_for_side(kml_entries):
    """
    Choose one acquisition date for all segment images on a side to avoid patchy
    outputs. Candidate dates are discovered from corridor bbox and scored by
    coverage on sampled segments.
    """
    if not kml_entries:
        return None

    # Corridor bbox from all entries
    min_lon = min(_bbox_from_rings(r)[0] for _, r in kml_entries)
    min_lat = min(_bbox_from_rings(r)[1] for _, r in kml_entries)
    max_lon = max(_bbox_from_rings(r)[2] for _, r in kml_entries)
    max_lat = max(_bbox_from_rings(r)[3] for _, r in kml_entries)
    bbox_poly = {
        "type": "Polygon",
        "coordinates": [[
            [min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat],
            [min_lon, max_lat], [min_lon, min_lat]
        ]]
    }

    discovered = _stac_search(bbox_poly, _build_datetime_range(), limit=120, sort_desc=True)
    candidate_dates = []
    seen = set()
    for it in discovered:
        if not _get_false_color_asset_hrefs(it):
            continue
        ndp = _item_nodata_percent(it)
        if ndp is not None and ndp > MAX_NODATA_PERCENT:
            continue
        d = _item_date_str(it)
        if d and d not in seen:
            seen.add(d)
            candidate_dates.append(d)
        if len(candidate_dates) >= 8:
            break
    if not candidate_dates:
        return None

    # Evenly sample entries to avoid excessive requests but still represent corridor
    sample_size = min(24, len(kml_entries))
    step = max(1, len(kml_entries) // sample_size)
    sampled = [kml_entries[i] for i in range(0, len(kml_entries), step)][:sample_size]

    best = None  # tuple(score_hits, -avg_cloud, date_rank, date)
    for rank, d in enumerate(candidate_dates):
        hits = 0
        cloud_vals = []
        for _, rings in sampled:
            it = _search_latest_stac_visual_item(rings, forced_date=d)
            if it:
                hits += 1
                cc = (it.get("properties") or {}).get("eo:cloud_cover")
                try:
                    cloud_vals.append(float(cc))
                except Exception:
                    pass
        avg_cloud = (sum(cloud_vals) / len(cloud_vals)) if cloud_vals else 9999.0
        score = (hits, -avg_cloud, -rank)
        if best is None or score > best[:3]:
            best = (hits, -avg_cloud, -rank, d)
    return best[3] if best else candidate_dates[0]


def _render_synthetic_from_rings(rings, out_png_path, width=2048, height=768, supersample=3):
    min_lon, min_lat, max_lon, max_lat = _bbox_from_rings(rings)
    if max_lon - min_lon < 1e-12:
        max_lon = min_lon + 1e-12
    if max_lat - min_lat < 1e-12:
        max_lat = min_lat + 1e-12
    w = int(width * supersample)
    h = int(height * supersample)
    pad_x = 0.08
    pad_y = 0.08
    sx = w / ((max_lon - min_lon) * (1.0 + 2.0 * pad_x))
    sy = h / ((max_lat - min_lat) * (1.0 + 2.0 * pad_y))
    scale = min(sx, sy)
    lon_center = (min_lon + max_lon) / 2.0
    lat_center = (min_lat + max_lat) / 2.0
    ox = w / 2.0 - (lon_center * scale)
    oy = h / 2.0 + (lat_center * scale)
    base = Image.new("RGB", (w, h), (0, 0, 0))
    c1 = (224, 50, 58)
    c2 = (146, 101, 105)

    pixel_rings = []
    for ring in rings:
        pts = []
        for lon, lat in ring:
            x = lon * scale + ox
            y = oy - lat * scale
            pts.append((x, y))
        if len(pts) >= 3:
            pixel_rings.append(pts)

    # Gradient along road length (not horizontal scanlines), so a diagonal strip
    # does not show stepped "horizontal blocks" of one color per row.
    ux, uy = _longest_ring_edge_unit_vector(pixel_rings)
    t_lo, t_hi = _project_scalar_range_on_axis(pixel_rings, ux, uy)
    span = max(t_hi - t_lo, 1e-9)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    t = ((xx * ux + yy * uy) - t_lo) / span
    t = np.clip(t, 0.0, 1.0)
    rch = c1[0] * (1.0 - t) + c2[0] * t
    gch = c1[1] * (1.0 - t) + c2[1] * t
    bch = c1[2] * (1.0 - t) + c2[2] * t
    grad = Image.fromarray(
        np.stack([rch, gch, bch], axis=-1).astype(np.uint8),
        mode="RGB",
    )
    if SYNTHETIC_GRADIENT_GAUSSIAN_PX > 1e-6:
        grad = grad.filter(ImageFilter.GaussianBlur(radius=SYNTHETIC_GRADIENT_GAUSSIAN_PX))

    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    for pts in pixel_rings:
        md.polygon(pts, fill=255)
    base.paste(grad, (0, 0), mask)
    final_img = base.resize((width, height), Image.Resampling.LANCZOS)
    final_img.save(out_png_path, format="PNG", optimize=True)
    return True, {"source": "synthetic"}


_ee_initialized = False


def _ensure_earth_engine():
    global _ee_initialized
    if _ee_initialized:
        return True
    try:
        import ee
    except ImportError:
        print("[IMG] earthengine-api not installed; skip Earth Engine render.")
        return False
    try:
        if EE_PROJECT:
            ee.Initialize(project=EE_PROJECT)
        else:
            ee.Initialize()
    except Exception as e:
        print(f"[IMG] Earth Engine Initialize failed: {e}")
        return False
    _ee_initialized = True
    return True


def _rings_to_ee_geometry(rings, ee):
    """Build ee.Geometry (Polygon or MultiPolygon) from lon/lat rings."""
    polys = []
    for ring in rings:
        if len(ring) < 3:
            continue
        seq = [[float(lon), float(lat)] for lon, lat in ring]
        if seq[0] != seq[-1]:
            seq.append(seq[0])
        polys.append(seq)
    if not polys:
        return None
    if len(polys) == 1:
        return ee.Geometry.Polygon(polys[0])
    return ee.Geometry.MultiPolygon([[p] for p in polys])


def _render_road_png_via_earth_engine(
    rings,
    min_lon,
    min_lat,
    max_lon,
    max_lat,
    out_png_path,
    width,
    height,
    render_w,
    render_h,
    x_off,
    y_off,
    forced_date,
):
    """
    Sentinel-2 SR (Harmonized) false color B8,B4,B3 with Earth Engine focal_mean
    smoothing (same idea as vigour_class_img.focal_mean(..., units='meters')),
    then visualize + clip to corridor geometry. Exports a PNG via getThumbURL
    (tile_fetcher from getMapId is included in metadata for map overlays).
    """
    if not _ensure_earth_engine():
        return False, None
    import ee

    geom_ee = _rings_to_ee_geometry(rings, ee)
    if geom_ee is None:
        return False, None

    if forced_date:
        d0 = ee.Date(str(forced_date)[:10])
        d1 = d0.advance(1, "day")
    else:
        if SATELLITE_DATE_START and SATELLITE_DATE_END:
            d0 = ee.Date(_normalize_iso_date(SATELLITE_DATE_START))
            d1 = ee.Date(_normalize_iso_date(SATELLITE_DATE_END)).advance(1, "day")
        else:
            d1 = ee.Date(datetime.now(timezone.utc))
            d0 = d1.advance(-SATELLITE_LOOKBACK_DAYS, "day")

    ic = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(geom_ee)
        .filterDate(d0, d1)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", MAX_CLOUD_COVER))
    )
    try:
        n = ic.size().getInfo()
    except Exception as e:
        print(f"[IMG] Earth Engine collection query failed: {e}")
        return False, None
    if n == 0:
        return False, {"error": "ee_no_images"}

    img = ic.sort("CLOUDY_PIXEL_PERCENTAGE").first()
    # Surface reflectance (0..1) after /10000 — same bands as Titiler false color.
    false_rgb = img.select(["B8", "B4", "B3"]).toFloat().divide(10000.0)
    # Match user workflow: focal_mean in meters, then visualize, then clip.
    smoothed = false_rgb.focal_mean(radius=EE_FOCAL_MEAN_RADIUS_M, units="meters")
    vmax = max(0.05, min(1.0, EE_VIS_MAX_REFLECTANCE))
    vigour_vis = {
        "bands": ["B8", "B4", "B3"],
        "min": [0.0, 0.0, 0.0],
        "max": [vmax, vmax, vmax],
        "gamma": [1.0, 1.0, 1.0],
    }
    smoothed_vis = smoothed.visualize(**vigour_vis).clip(geom_ee)

    tile_url = None
    try:
        map_id = smoothed_vis.getMapId()
        tf = map_id.get("tile_fetcher") or {}
        tile_url = tf.get("url_format")
    except Exception:
        pass

    fetch_scale = max(1, SATELLITE_FETCH_SCALE)
    fetch_w = max(1, render_w * fetch_scale)
    fetch_h = max(1, render_h * fetch_scale)
    # Earth Engine getThumbURL allows up to ~8192; cap to avoid oversized requests.
    max_px = 8192
    if fetch_w > max_px or fetch_h > max_px:
        s = min(max_px / fetch_w, max_px / fetch_h)
        fetch_w = max(1, int(round(fetch_w * s)))
        fetch_h = max(1, int(round(fetch_h * s)))

    thumb_params = {
        "region": [min_lon, min_lat, max_lon, max_lat],
        "dimensions": f"{fetch_w}x{fetch_h}",
        "format": "png",
        "crs": "EPSG:4326",
    }
    try:
        thumb_url = smoothed_vis.getThumbURL(thumb_params)
    except Exception as e:
        print(f"[IMG] Earth Engine getThumbURL failed: {e}")
        return False, None

    try:
        rsp = _http_request_with_retry(
            "GET",
            thumb_url,
            timeout=120,
            retry_statuses={429, 500, 502, 503, 504},
        )
        rsp.raise_for_status()
        src_img = Image.open(BytesIO(rsp.content)).convert("RGB")
    except Exception as e:
        print(f"[IMG] Earth Engine thumb download failed: {e}")
        return False, None

    if src_img.size != (render_w, render_h):
        src_img = src_img.resize((render_w, render_h), Image.Resampling.LANCZOS)

    if APPLY_CUSTOM_RED_GRADIENT:
        src_img = _apply_bright_red_gradient(src_img, gamma=RED_GRADIENT_GAMMA)
    if SATELLITE_COLOR_MICROBLUR_PX > 1e-6:
        src_img = src_img.filter(ImageFilter.GaussianBlur(radius=SATELLITE_COLOR_MICROBLUR_PX))
    src_img = _apply_output_sharpen(src_img)

    ms = max(1, SATELLITE_MASK_SUPERSAMPLE)
    road_mask = _road_mask_for_composite(
        rings,
        min_lon,
        min_lat,
        max_lon,
        max_lat,
        render_w,
        render_h,
        ms,
        SATELLITE_MASK_FEATHER_PX,
    )
    out_img = Image.new("RGB", (width, height), (0, 0, 0))
    out_img.paste(src_img, (x_off, y_off), road_mask)
    out_img.save(out_png_path, format="PNG", optimize=True)

    meta = {
        "source": "earth-engine-s2-sr-harmonized",
        "ee_collection": "COPERNICUS/S2_SR_HARMONIZED",
        "focal_mean_radius_m": EE_FOCAL_MEAN_RADIUS_M,
        "visualize_max_reflectance": vmax,
        "tile_url_format": tile_url,
        "processing": {
            "earth_engine": True,
            "custom_red_gradient": APPLY_CUSTOM_RED_GRADIENT,
            "mask_feather_px": SATELLITE_MASK_FEATHER_PX,
            "mask_supersample": ms,
            "color_microblur_px": SATELLITE_COLOR_MICROBLUR_PX,
            "sharpen_enabled": SATELLITE_ENABLE_SHARPEN,
            "sharpen_radius": SATELLITE_SHARPEN_RADIUS,
            "sharpen_percent": SATELLITE_SHARPEN_PERCENT,
            "sharpen_threshold": SATELLITE_SHARPEN_THRESHOLD,
        },
        "render_size": f"{render_w}x{render_h}",
    }
    if forced_date:
        meta["forced_date"] = forced_date
    return True, meta


def render_kml_to_road_image(kml_path, out_png_path, width=SATELLITE_IMAGE_WIDTH, height=SATELLITE_IMAGE_HEIGHT, forced_date=None):
    """
    Render from latest Sentinel-2 imagery (Earth Search STAC false color B08-B04-B03),
    clipped to road polygon area. If disabled/failed and fallback is enabled,
    render synthetic road strip image.
    """
    rings = _extract_polygon_rings_from_kml(kml_path)
    if not rings:
        return False, {"error": "no_polygons"}
    min_lon, min_lat, max_lon, max_lat = _bbox_from_rings(rings)
    if max_lon - min_lon < 1e-12 or max_lat - min_lat < 1e-12:
        if ALLOW_SYNTHETIC_FALLBACK:
            return _render_synthetic_from_rings(rings, out_png_path, width=width, height=height)
        return False, {"error": "invalid_bbox"}

    # Preserve KML orientation by keeping true bbox aspect ratio (in meters).
    # We render into a fitted viewport and center it on a fixed-size canvas.
    mid_lat = (min_lat + max_lat) / 2.0
    bbox_w_m = geod.line_length([min_lon, max_lon], [mid_lat, mid_lat])
    bbox_h_m = geod.line_length([min_lon, min_lon], [min_lat, max_lat])
    if bbox_w_m <= 0 or bbox_h_m <= 0:
        if ALLOW_SYNTHETIC_FALLBACK:
            return _render_synthetic_from_rings(rings, out_png_path, width=width, height=height)
        return False, {"error": "invalid_bbox_dimensions"}

    bbox_aspect = bbox_w_m / bbox_h_m
    canvas_aspect = width / max(1, height)
    if bbox_aspect >= canvas_aspect:
        render_w = width
        render_h = max(1, int(round(width / bbox_aspect)))
    else:
        render_h = height
        render_w = max(1, int(round(height * bbox_aspect)))
    x_off = (width - render_w) // 2
    y_off = (height - render_h) // 2

    if EARTH_ENGINE_RENDER:
        ok_ee, meta_ee = _render_road_png_via_earth_engine(
            rings,
            min_lon,
            min_lat,
            max_lon,
            max_lat,
            out_png_path,
            width,
            height,
            render_w,
            render_h,
            x_off,
            y_off,
            forced_date,
        )
        if ok_ee:
            return True, meta_ee
        print("[IMG] Earth Engine render unavailable; falling back to STAC/Titiler.")

    item = _search_latest_stac_visual_item(rings, forced_date=forced_date)
    if item:
        false_color_assets = _get_false_color_asset_hrefs(item)
        if false_color_assets:
            fetch_scale = max(1, SATELLITE_FETCH_SCALE)
            fetch_w = render_w * fetch_scale
            fetch_h = render_h * fetch_scale
            # Cap oversized requests to reduce 429s/timeouts on public Titiler.
            max_edge = max(512, SATELLITE_FETCH_MAX_EDGE_PX)
            if fetch_w > max_edge or fetch_h > max_edge:
                scale = min(max_edge / fetch_w, max_edge / fetch_h)
                fetch_w = max(1, int(round(fetch_w * scale)))
                fetch_h = max(1, int(round(fetch_h * scale)))
            bbox_url = (
                f"{TITILER_COG_API.rstrip('/')}/bbox/"
                f"{min_lon},{min_lat},{max_lon},{max_lat}/{fetch_w}x{fetch_h}.png"
            )
            try:
                hc_lut = None
                if APPLY_HIGHLIGHT_COMPRESS:
                    hc_lut = _highlight_compress_lut(
                        min_val=0.0,
                        max_input=HIGHLIGHT_MAX_INPUT,
                        clip_input=HIGHLIGHT_CLIP_INPUT,
                        max_output=HIGHLIGHT_MAX_OUTPUT,
                        gain=FALSE_COLOR_GAIN,
                    )

                def fetch_band(href):
                    params = {"url": href, "resampling": "cubic"}
                    # Keep values from STAC assets but apply display stretch so output is not black.
                    # Typical Sentinel-2 reflectance display window: 0..3000.
                    if SATELLITE_BAND_RESCALE:
                        params["rescale"] = SATELLITE_BAND_RESCALE
                    if _band_fetch_slots is not None:
                        _band_fetch_slots.acquire()
                    try:
                        rsp = _http_request_with_retry(
                            "GET",
                            bbox_url,
                            params=params,
                            timeout=60,
                            retry_statuses={429, 500, 502, 503, 504},
                        )
                    finally:
                        if _band_fetch_slots is not None:
                            _band_fetch_slots.release()
                    rsp.raise_for_status()
                    band = Image.open(BytesIO(rsp.content)).convert("L")
                    if SATELLITE_INTER_BAND_DELAY_SEC > 1e-9:
                        time.sleep(SATELLITE_INTER_BAND_DELAY_SEC)
                    if hc_lut is not None:
                        return band.point(hc_lut)
                    return band

                b08_band = fetch_band(false_color_assets["b08"])
                b04_band = fetch_band(false_color_assets["b04"])
                b03_band = fetch_band(false_color_assets["b03"])
                src_img = Image.merge("RGB", (b08_band, b04_band, b03_band))
                if fetch_scale > 1:
                    src_img = src_img.resize((render_w, render_h), Image.Resampling.LANCZOS)
                if SATELLITE_SMOOTHING:
                    # Approximate focal_mean(radius=5m): convert meters -> pixels and box blur.
                    mpp_x = bbox_w_m / max(1, render_w)
                    mpp_y = bbox_h_m / max(1, render_h)
                    mpp = max(1e-6, (mpp_x + mpp_y) / 2.0)
                    focal_radius_px = max(0.6, FOCAL_MEAN_RADIUS_METERS / mpp)
                    src_img = src_img.filter(ImageFilter.BoxBlur(radius=focal_radius_px))
                    # Gentle Gaussian pass to avoid hard transitions after mean filter.
                    src_img = src_img.filter(ImageFilter.GaussianBlur(radius=0.85))
                elif SATELLITE_COLOR_MICROBLUR_PX > 1e-6:
                    src_img = src_img.filter(
                        ImageFilter.GaussianBlur(radius=SATELLITE_COLOR_MICROBLUR_PX)
                    )
                if APPLY_CUSTOM_RED_GRADIENT:
                    src_img = _apply_bright_red_gradient(src_img, gamma=RED_GRADIENT_GAMMA)
                src_img = _apply_output_sharpen(src_img)

                ms = max(1, SATELLITE_MASK_SUPERSAMPLE)
                road_mask = _road_mask_for_composite(
                    rings,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    render_w,
                    render_h,
                    ms,
                    SATELLITE_MASK_FEATHER_PX,
                )
                out_img = Image.new("RGB", (width, height), (0, 0, 0))
                out_img.paste(src_img, (x_off, y_off), road_mask)
                out_img.save(out_png_path, format="PNG", optimize=True)
                props = item.get("properties") or {}
                meta = {
                    "source": "sentinel-2-l2a",
                    "stac_endpoint": STAC_API,
                    "collection": STAC_COLLECTION,
                    "item_id": item.get("id"),
                    "datetime": props.get("datetime"),
                    "cloud_cover": props.get("eo:cloud_cover"),
                    "b08_asset": false_color_assets["b08"],
                    "b04_asset": false_color_assets["b04"],
                    "b03_asset": false_color_assets["b03"],
                    "processing": {
                        "band_rescale": SATELLITE_BAND_RESCALE,
                        "highlight_compress": APPLY_HIGHLIGHT_COMPRESS,
                        "custom_red_gradient": APPLY_CUSTOM_RED_GRADIENT,
                        "smoothing": SATELLITE_SMOOTHING,
                        "mask_feather_px": SATELLITE_MASK_FEATHER_PX,
                        "mask_supersample": ms,
                        "sharpen_enabled": SATELLITE_ENABLE_SHARPEN,
                        "sharpen_radius": SATELLITE_SHARPEN_RADIUS,
                        "sharpen_percent": SATELLITE_SHARPEN_PERCENT,
                        "sharpen_threshold": SATELLITE_SHARPEN_THRESHOLD,
                    },
                    "render_size": f"{render_w}x{render_h}",
                }
                return True, meta
            except Exception as e:
                print(f"[IMG] Satellite render failed for {kml_path}: {e}")
    if ALLOW_SYNTHETIC_FALLBACK:
        return _render_synthetic_from_rings(rings, out_png_path, width=width, height=height)
    return False, {"error": "no_satellite_scene"}


def generate_lane_kml_images(side_root, side_tag):
    """
    Generate per-KML PNGs for lane folders into:
      {side_root}/{side_tag}_images/{side_tag}_L1|L2|L3/*.png
    and merged KML folder into:
      {side_root}/{side_tag}_kml_merge_images/*.png
    """
    side_upper = side_tag.upper()
    lane_out_root = os.path.join(side_root, f"{side_upper}_images")
    merge_out_root = os.path.join(side_root, f"{side_upper}_kml_merge_images")
    os.makedirs(lane_out_root, exist_ok=True)
    os.makedirs(merge_out_root, exist_ok=True)
    clear_folder(lane_out_root)
    clear_folder(merge_out_root)
    generated_lane = 0
    generated_merge = 0

    lane_dirs = []
    merge_dir = None
    merge_folder_name = f"{side_upper}_kml_merge".upper()
    for sub in os.listdir(side_root):
        m = SIDE_LANE_FOLDER_RE.match(sub)
        p = os.path.join(side_root, sub)
        if not os.path.isdir(p):
            continue
        if m and m.group(1).upper() == side_upper:
            layer_num = int(m.group(2))
            lane_dirs.append((layer_num, sub, p))
            continue
        if sub.upper() == merge_folder_name:
            merge_dir = (sub, p)
    lane_dirs.sort(key=lambda x: x[0])

    # Build full KML entry list once and lock one satellite date for this side.
    kml_entries = []  # [(src_path, rings), ...]
    for _, _, lane_dir in lane_dirs:
        for fname in sorted(os.listdir(lane_dir)):
            if not fname.lower().endswith(".kml"):
                continue
            src = os.path.join(lane_dir, fname)
            rings = _extract_polygon_rings_from_kml(src)
            if rings:
                kml_entries.append((src, rings))
    if merge_dir:
        _, merge_path = merge_dir
        for fname in sorted(os.listdir(merge_path)):
            if not fname.lower().endswith(".kml"):
                continue
            src = os.path.join(merge_path, fname)
            rings = _extract_polygon_rings_from_kml(src)
            if rings:
                kml_entries.append((src, rings))
    locked_date = _choose_consistent_date_for_side(kml_entries)
    if locked_date:
        print(f"[IMG] Using consistent acquisition date for {side_upper}: {locked_date}")
    else:
        print(f"[IMG] No side-wide date lock found for {side_upper}, fallback to per-segment latest.")

    lane_jobs = []   # (src, dst_png, kind)
    merge_jobs = []  # (src, dst_png, kind)
    for _, lane_name, lane_dir in lane_dirs:
        out_lane = os.path.join(lane_out_root, lane_name)
        os.makedirs(out_lane, exist_ok=True)
        for fname in sorted(os.listdir(lane_dir)):
            if not fname.lower().endswith(".kml"):
                continue
            src = os.path.join(lane_dir, fname)
            base = os.path.splitext(fname)[0]
            lane_jobs.append((
                src,
                os.path.join(out_lane, f"{base}.png"),
                "lane",
            ))
    if merge_dir:
        _, merge_path = merge_dir
        for fname in sorted(os.listdir(merge_path)):
            if not fname.lower().endswith(".kml"):
                continue
            src = os.path.join(merge_path, fname)
            base = os.path.splitext(fname)[0]
            merge_jobs.append((
                src,
                os.path.join(merge_out_root, f"{base}.png"),
                "merge",
            ))
    # Priority: generate merged-strip images first so distress can run as soon as
    # LHS/RHS merge images are ready, while lane images continue in parallel flow.
    jobs = merge_jobs + lane_jobs

    def _run_one(job):
        src, dst_png, kind = job
        try:
            ok, meta = render_kml_to_road_image(src, dst_png, forced_date=locked_date)
        except Exception as exc:
            return src, False, {"error": f"render_exception: {exc}"}, kind

        # For big KMLs, satellite availability can fail for some merge segments.
        # Ensure merge-strip images are still produced via synthetic fallback.
        if (not ok) and kind == "merge":
            try:
                rings = _extract_polygon_rings_from_kml(src)
                if rings:
                    fb_ok, fb_meta = _render_synthetic_from_rings(
                        rings,
                        dst_png,
                        width=SATELLITE_IMAGE_WIDTH,
                        height=SATELLITE_IMAGE_HEIGHT,
                    )
                    if fb_ok:
                        return src, True, {"fallback": "synthetic", "base_error": meta, "meta": fb_meta}, kind
            except Exception as exc:
                return src, False, {"error": f"merge_fallback_exception: {exc}", "base_error": meta}, kind
        return src, ok, meta, kind

    max_workers = max(1, min(IMAGE_GEN_MAX_WORKERS, len(jobs) if jobs else 1))
    # Satellite path makes multiple HTTP calls per KML; keep concurrency conservative
    # to avoid API throttling while preserving throughput.
    if max_workers > 3:
        max_workers = 3
    if len(jobs) >= 80:
        # Large KML sets can exhaust memory/network when too many renders run in parallel.
        max_workers = min(max_workers, 2)
    def _is_rate_limited(meta):
        if meta is None:
            return False
        if isinstance(meta, (dict, list)):
            txt = json.dumps(meta, default=str).lower()
        else:
            txt = str(meta).lower()
        return ("429" in txt) or ("too many requests" in txt) or ("rate limit" in txt)

    chunk_size = max(1, min(IMAGE_GEN_CHUNK_SIZE, len(jobs) if jobs else 1))
    adaptive_delay_s = 0.0
    base_delay = max(0.0, IMAGE_GEN_CHUNK_BASE_DELAY_SEC)
    max_delay = max(base_delay, IMAGE_GEN_CHUNK_MAX_DELAY_SEC)
    backoff_mult = max(1.05, IMAGE_GEN_CHUNK_BACKOFF_MULTIPLIER)
    success_decay = min(0.99, max(0.1, IMAGE_GEN_CHUNK_SUCCESS_DECAY))

    for start_idx in range(0, len(jobs), chunk_size):
        chunk = jobs[start_idx : start_idx + chunk_size]
        if adaptive_delay_s > 1e-6:
            print(
                f"[IMG] Throttle pause for {side_upper}: {adaptive_delay_s:.2f}s "
                f"before chunk {start_idx // chunk_size + 1}"
            )
            time.sleep(adaptive_delay_s)

        chunk_failures = 0
        chunk_rate_limits = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(_run_one, j) for j in chunk]
            for fut in as_completed(futures):
                try:
                    src, ok, meta, kind = fut.result()
                except Exception as exc:
                    print(f"[IMG] Worker failure: {exc}")
                    chunk_failures += 1
                    continue
                if ok:
                    if kind == "merge":
                        generated_merge += 1
                    else:
                        generated_lane += 1
                else:
                    chunk_failures += 1
                    if _is_rate_limited(meta):
                        chunk_rate_limits += 1
                    print(f"[IMG] Skipped {src}: {meta}")

        if chunk_rate_limits > 0:
            next_delay = base_delay if adaptive_delay_s <= 1e-6 else adaptive_delay_s
            adaptive_delay_s = min(max_delay, next_delay * backoff_mult)
            print(
                f"[IMG] {side_upper} chunk hit rate limits ({chunk_rate_limits}/{len(chunk)}). "
                f"Increasing delay to {adaptive_delay_s:.2f}s."
            )
        elif chunk_failures == 0 and adaptive_delay_s > 1e-6:
            adaptive_delay_s *= success_decay
            if adaptive_delay_s < base_delay * 0.5:
                adaptive_delay_s = 0.0
    return lane_out_root, generated_lane, merge_out_root, generated_merge


def _generate_and_rotate_side_images(side_root, side_tag, image_direction):
    """
    Run full image generation + rotation for one side.
    Returned dict is used for parallel side execution logging in run_pipeline().
    """
    out_dir, count, merge_out_dir, merge_count = generate_lane_kml_images(side_root, side_tag)
    rotated = _run_rotation_chain_for_side_images(out_dir, image_direction)
    rotated_merge = _run_rotation_chain_for_folder(merge_out_dir, image_direction)
    return {
        "side": side_tag.upper(),
        "out_dir": out_dir,
        "count": count,
        "merge_out_dir": merge_out_dir,
        "merge_count": merge_count,
        "rotated": rotated,
        "rotated_merge": rotated_merge,
    }


def _run_side_pipeline(side_tag, side_root, side_layer_pairs, image_direction):
    """
    Execute KML->merge->image flow for one side end-to-end.
    Running LHS/RHS side pipelines in parallel reduces total wall-clock time.
    """
    side_upper = side_tag.upper()
    generated_kmls = []

    print(f"5) [{side_upper}] Generating per-layer binned KMLs ...")
    for a_path, b_path, out_folder, layer_tag in side_layer_pairs:
        outs = generate_layer_bin_kmls(
            a_path, b_path, out_folder, layer_tag, bin_km=KML_MERGE_OFFSET_KM
        )
        generated_kmls.extend(outs)
        print(f"  -> [{side_upper}] Generated {len(outs)} bin-KMLs for {layer_tag} in {out_folder}")

    print(f"6) [{side_upper}] Merging layer folders into Merge_KMLs ...")
    for sub in sorted(os.listdir(side_root)):
        layer_dir = os.path.join(side_root, sub)
        if not os.path.isdir(layer_dir):
            continue
        out_merge = os.path.join(KML_MERGED_FOLDER, f"{sub}_merge.kml")
        try:
            merge_layer_folder_to_single_kml(layer_dir, out_merge)
            print(f"  -> [{side_upper}] Merged {sub} -> {out_merge}")
        except Exception as exc:
            print(f"  -> [{side_upper}] Merge failed for {sub}: {exc}")

    print(f"6.1) [{side_upper}] Merging side files into {side_upper}_merge ...")
    side_merged = merge_merge_kml_side_files(KML_MERGED_FOLDER, side_upper)
    if side_merged:
        print(f"  -> [{side_upper}] Side merged file: {side_merged}")

    print(f"7) [{side_upper}] Populating {side_upper}_kml_merge ...")
    side_merge_folder = merge_side_lane_folders_into_merge_kml(side_root, side_upper)
    if side_merge_folder:
        print(f"  -> [{side_upper}] Side merge folder: {side_merge_folder}")

    print(f"8) [{side_upper}] Generating lane-wise road images ...")
    image_result = _generate_and_rotate_side_images(side_root, side_upper, image_direction)

    return {
        "side": side_upper,
        "generated_kmls": generated_kmls,
        "side_merged_file": side_merged,
        "side_merge_folder": side_merge_folder,
        "image_result": image_result,
    }

def read_linestring_from_kml(kml_path):
    """Read all <coordinates> from KML and return a combined list of (lon, lat)."""
    try:
        doc = minidom.parse(kml_path)
        coords_elements = doc.getElementsByTagName("coordinates")
        if coords_elements.length == 0:
            raise ValueError("No <coordinates> found in KML.")
        
        all_coords = []
        import re
        for c_el in coords_elements:
            if not c_el.firstChild:
                continue
            coord_text = c_el.firstChild.nodeValue.strip()
            if not coord_text:
                continue
            
            # Use regex to find all coordinate pairs more robustly
            # Matches "lon,lat" or "lon,lat,alt" separated by whitespace
            coord_pairs = re.findall(r"([-+]?\d*\.\d+|[-+]?\d+),\s*([-+]?\d*\.\d+|[-+]?\d+)(?:,\s*[-+]?\d*\.\d+|[-+]?\d+)?", coord_text)
            
            for lon_str, lat_str in coord_pairs:
                try:
                    lon = float(lon_str)
                    lat = float(lat_str)
                    all_coords.append((lon, lat))
                except (ValueError, TypeError):
                    continue
        
        if not all_coords:
            raise ValueError("All <coordinates> tags are empty.")

        # Drop invalid / duplicate consecutive points so downstream interpolation
        # never receives degenerate coordinate sequences from large KMLs.
        cleaned = []
        for lon, lat in all_coords:
            if not (math.isfinite(lon) and math.isfinite(lat)):
                continue
            if cleaned and abs(cleaned[-1][0] - lon) < 1e-12 and abs(cleaned[-1][1] - lat) < 1e-12:
                continue
            cleaned.append((lon, lat))
        if len(cleaned) < 2:
            raise ValueError("Input KML must contain at least two valid coordinates.")

        return cleaned
    except Exception as e:
        print(f"Error parsing KML {kml_path}: {e}")
        raise


def interpolate_geodesic_points(line_coords, interval_meters):
    """
    Interpolate points every interval_meters along the LineString.
    Returns list of (lon, lat).
    """
    if len(line_coords) < 2:
        return []
    if interval_meters <= 0:
        raise ValueError("INTERVAL_METERS must be > 0")
    # cumulative distances (meters) along the polyline
    cum = [0.0]
    for i in range(1, len(line_coords)):
        lon1, lat1 = line_coords[i - 1]
        lon2, lat2 = line_coords[i]
        seg_len = geod.line_length([lon1, lon2], [lat1, lat2])
        cum.append(cum[-1] + seg_len)
    total_len = cum[-1]
    if total_len <= 0:
        return []
    pts = []
    cur = 0.0
    # include last point with small epsilon
    while cur <= total_len + 1e-6:
        # Clamp slight floating drift so we never step beyond last segment.
        cur_eval = min(cur, total_len)
        # find segment index
        i = 0
        while i < len(cum) - 1 and cum[i + 1] < cur_eval - 1e-9:
            i += 1
        if i >= len(line_coords) - 1:
            i = len(line_coords) - 2
        seg_start = cum[i]
        seg_end = cum[i + 1] if i + 1 < len(cum) else seg_start
        if seg_end == seg_start:
            frac = 0.0
        else:
            frac = (cur_eval - seg_start) / (seg_end - seg_start)
        lon1, lat1 = line_coords[i]
        lon2, lat2 = line_coords[i + 1]
        ilon = lon1 + frac * (lon2 - lon1)
        ilat = lat1 + frac * (lat2 - lat1)
        pts.append((ilon, ilat))
        cur += interval_meters
    return pts


def make_chainages(start_km, n_points, step_m):
    """Return labels and numeric km values for n_points."""
    step_km = step_m / 1000.0
    chainages = [round(start_km + i * step_km, CHAINAGE_DECIMALS) for i in range(n_points)]
    labels = [f"{c:.{CHAINAGE_DECIMALS}f}" for c in chainages]
    return labels, chainages


def calculate_bearing(A: Point, B: Point):
    """Calculate forward azimuth from A to B (degrees)."""
    lat1 = math.radians(A.latitude)
    lon1 = math.radians(A.longitude)
    lat2 = math.radians(B.latitude)
    lon2 = math.radians(B.longitude)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def offset_point(lat, lon, offset_meters, side, prev_pt=None, next_pt=None):
    """
    Offset a geodetic point to its left or right by offset_meters.
    side: 'left' or 'right'
    prev_pt/next_pt: geopy.Point objects (optional) to compute local average bearing
    Returns (lat_out, lon_out)
    """
    current = Point(latitude=lat, longitude=lon)
    if prev_pt and next_pt:
        b1 = calculate_bearing(prev_pt, current)
        b2 = calculate_bearing(current, next_pt)
        # average bearing (circular)
        x = math.cos(math.radians(b1)) + math.cos(math.radians(b2))
        y = math.sin(math.radians(b1)) + math.sin(math.radians(b2))
        bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
    elif next_pt:
        bearing = calculate_bearing(current, next_pt)
    elif prev_pt:
        bearing = calculate_bearing(prev_pt, current)
    else:
        bearing = 0.0
    if side == "left":
        b = (bearing - 90) % 360
    else:
        b = (bearing + 90) % 360
    dest = geodesic(meters=offset_meters).destination((lat, lon), b)
    return dest.latitude, dest.longitude


def df_chain_to_segment_excel(df_chain, excel_path):
    """
    Given df_chain with columns: chainage_km_str, chainage_km, latitude, longitude
    Write an excel with columns: Chainage Start, Chainage End, Latitude, Longitude
    Each row is a chainage POINT start (end = start + step_km).
    """
    step_km = INTERVAL_METERS / 1000.0
    df = df_chain.copy()
    df["Chainage Start"] = df["chainage_km"].round(CHAINAGE_DECIMALS)
    df["Chainage End"] = (df["chainage_km"] + step_km).round(CHAINAGE_DECIMALS)
    df_out = df[["Chainage Start", "Chainage End", "latitude", "longitude"]].rename(
        columns={"latitude": "Latitude", "longitude": "Longitude"}
    )
    df_out.to_excel(excel_path, index=False)
    return df_out


def save_offset_excel(input_df, offsets_lonlat, excel_path):
    """
    offsets_lonlat: list of (lon, lat)
    input_df: must contain 'chainage_km' or 'Chainage Start'
    Output columns: Chainage Start, Chainage End, Latitude, Longitude
    """
    step_km = INTERVAL_METERS / 1000.0
    if "chainage_km" in input_df.columns:
        chainage_source = input_df["chainage_km"]
    elif "Chainage Start" in input_df.columns:
        chainage_source = input_df["Chainage Start"]
    else:
        raise ValueError("Input DataFrame must contain 'chainage_km' or 'Chainage Start'.")
    df = pd.DataFrame({
        "Chainage Start": [round(float(x), CHAINAGE_DECIMALS) for x in chainage_source],
        "Chainage End": [round(float(x) + step_km, CHAINAGE_DECIMALS) for x in chainage_source],
        "Latitude": [lat for lon, lat in offsets_lonlat],
        "Longitude": [lon for lon, lat in offsets_lonlat]
    })
    df.to_excel(excel_path, index=False)
    return df


# -----------------------------
# Layer creation (Excel outputs)
# -----------------------------
def create_layers_from_base(base_df_path, side, prefix, count_layers):
    """
    base_df_path: path to excel with Chainage Start/End, Latitude, Longitude (median)
    side: 'left' or 'right'
    prefix: 'LHS' or 'RHS'
    count_layers: number of layers to create (1..3)
    Returns dict: { "LHS_L1": "/path/to/LHS_L1.xlsx", ... }
    """
    created_paths = {}
    prev_df = pd.read_excel(base_df_path)
    # ensure columns expected
    if not {"Chainage Start", "Chainage End", "Latitude", "Longitude"}.issubset(prev_df.columns):
        raise ValueError("Base Excel missing required columns.")
    for L in range(1, count_layers + 1):
        layer_name = f"{prefix}_L{L}"
        prev_points = [Point(latitude=row["Latitude"], longitude=row["Longitude"]) for _, row in prev_df.iterrows()]
        offsets = []
        for i, p in enumerate(prev_points):
            prev_pt = prev_points[i - 1] if i > 0 else None
            next_pt = prev_points[i + 1] if i < len(prev_points) - 1 else None
            lat_off, lon_off = offset_point(p.latitude, p.longitude, LANE_STEP_M, side, prev_pt=prev_pt, next_pt=next_pt)
            offsets.append((lon_off, lat_off))
        excel_path = os.path.join(EXCEL_FOLDER, f"{layer_name}.xlsx")
        df_created = save_offset_excel(prev_df, offsets, excel_path)
        created_paths[layer_name] = excel_path
        # prepare prev_df for next iteration (child layer)
        prev_df = df_created.copy()
    return created_paths


# -----------------------------
# KML generation: per-layer bin KMLs (bins anchored to CHAINAGE_START_KM)
# -----------------------------
def generate_layer_bin_kmls(a_path, b_path, out_layer_folder, layer_tag, bin_km=KML_MERGE_OFFSET_KM):
    """
    Merge two excel files (a_path, b_path) and create KMLs grouped by bin_km anchored at CHAINAGE_START_KM.
    Each bin will produce one KML named Chainage_{start:.3f}_to_{end:.3f}_{layer_tag}.kml
    bin_km is in kilometers (e.g., 0.100)
    """
    os.makedirs(out_layer_folder, exist_ok=True)
    a = pd.read_excel(a_path)
    b = pd.read_excel(b_path)

    # normalize rounding
    a["Chainage Start"] = a["Chainage Start"].round(CHAINAGE_DECIMALS)
    a["Chainage End"] = a["Chainage End"].round(CHAINAGE_DECIMALS)
    b["Chainage Start"] = b["Chainage Start"].round(CHAINAGE_DECIMALS)
    b["Chainage End"] = b["Chainage End"].round(CHAINAGE_DECIMALS)

    merged = pd.merge(a, b, on=["Chainage Start", "Chainage End"], suffixes=("_1", "_2"))
    merged = merged.sort_values("Chainage Start").reset_index(drop=True)

    if merged.empty:
        return []

    # group rows into bins anchored to CHAINAGE_START_KM
    bins = {}
    for idx, row in merged.iterrows():
        # Only process if we can form a segment with the next row
        if idx >= merged.shape[0] - 1:
            continue
        if merged.loc[idx + 1, "Chainage Start"] != merged.loc[idx, "Chainage End"]:
            continue

        start_km = float(row["Chainage Start"])  # numeric in km
        rel = (start_km - CHAINAGE_START_KM) / bin_km
        bin_idx = int(math.floor(rel + 1e-9))
        bins.setdefault(bin_idx, []).append(idx)

    out_paths = []
    for bin_idx, indices in sorted(bins.items()):
        start_bin_km = CHAINAGE_START_KM + bin_idx * bin_km
        end_bin_km = start_bin_km + bin_km
        kml = simplekml.Kml()
        for i in indices:
            # create polygon for segment i -> i+1
            coords = [
                (merged.loc[i, "Longitude_1"], merged.loc[i, "Latitude_1"]),
                (merged.loc[i + 1, "Longitude_1"], merged.loc[i + 1, "Latitude_1"]),
                (merged.loc[i + 1, "Longitude_2"], merged.loc[i + 1, "Latitude_2"]),
                (merged.loc[i, "Longitude_2"], merged.loc[i, "Latitude_2"]),
                (merged.loc[i, "Longitude_1"], merged.loc[i, "Latitude_1"])
            ]
            name = f"{merged.loc[i,'Chainage Start']:.{CHAINAGE_DECIMALS}f}_to_{merged.loc[i,'Chainage End']:.{CHAINAGE_DECIMALS}f}"
            full_name = f"Chainage_{name}_{layer_tag}"
            pol = kml.newpolygon(name=full_name, outerboundaryis=coords)
            pol.style.polystyle.fill = 0

        out_name = os.path.join(out_layer_folder, f"Chainage_{start_bin_km:.{CHAINAGE_DECIMALS}f}_to_{end_bin_km:.{CHAINAGE_DECIMALS}f}_{layer_tag}.kml")
        kml.save(out_name)
        out_paths.append(out_name)
    return out_paths


def _append_polygons_from_kml_file(mk, fp):
    """Parse one KML file and append its polygon placemarks to an existing simplekml.Kml."""
    try:
        doc = minidom.parse(fp)
    except Exception:
        return
    placemarks = doc.getElementsByTagName("Placemark")
    for pm in placemarks:
        name_nodes = pm.getElementsByTagName("name")
        poly_name = name_nodes[0].firstChild.nodeValue.strip() if name_nodes and name_nodes[0].firstChild else "Untitled Polygon"
        polys = pm.getElementsByTagName("Polygon")
        for p in polys:
            coords_nodes = p.getElementsByTagName("coordinates")
            if coords_nodes and coords_nodes[0].firstChild:
                coord_text = coords_nodes[0].firstChild.nodeValue.strip()
                coord_pairs = coord_text.split()
                coords = []
                for pair in coord_pairs:
                    parts = pair.split(",")
                    lon = float(parts[0])
                    lat = float(parts[1])
                    coords.append((lon, lat))
                mk.newpolygon(name=poly_name, outerboundaryis=coords)


def merge_layer_folder_to_single_kml(layer_folder, out_merge_path):
    """Merge all KML polygons in layer_folder into a single KML file, preserving each polygon's name."""
    files = [os.path.join(layer_folder, f) for f in os.listdir(layer_folder) if f.lower().endswith('.kml')]
    files = sorted(files)
    mk = simplekml.Kml()
    for fp in files:
        _append_polygons_from_kml_file(mk, fp)
    mk.save(out_merge_path)
    return out_merge_path


def merge_merge_kml_side_files(merge_dir, side_tag):
    """Merge Merge_KMLs/{side_tag}_L*_merge.kml files into Merge_KMLs/{side_tag}_merge.kml."""
    side_upper = side_tag.upper()
    layer_files = []
    for idx in (1, 2, 3):
        fp = os.path.join(merge_dir, f"{side_upper}_L{idx}_merge.kml")
        if os.path.isfile(fp):
            layer_files.append(fp)
    if not layer_files:
        return None
    out_path = os.path.join(merge_dir, f"{side_upper}_merge.kml")
    mk = simplekml.Kml()
    for fp in layer_files:
        _append_polygons_from_kml_file(mk, fp)
    mk.save(out_path)
    return out_path


def merge_side_lane_folders_into_merge_kml(side_root, side_tag):
    """
    Populate side_root/{side_tag}_kml_merge/ with one KML per chainage range: all lane
    files sharing the same start/end (L1, L2, L3) are merged into polygons in lane order,
    saved as Chainage_{start}_to_{end}_{side_tag}_merged.kml (e.g. ..._LHS_merged.kml).
    """
    merge_folder_name = f"{side_tag}_kml_merge"
    side_upper = side_tag.upper()
    lane_pat = re.compile(rf"^{re.escape(side_tag)}_L(\d+)$")
    lane_dirs = []
    for sub in os.listdir(side_root):
        m = lane_pat.match(sub)
        if not m:
            continue
        p = os.path.join(side_root, sub)
        if os.path.isdir(p):
            lane_dirs.append((int(m.group(1)), p))
    lane_dirs.sort(key=lambda x: x[0])
    if not lane_dirs:
        return None
    # (chainage start, chainage end) -> layer_num -> filepath
    bin_to_layer_files = {}
    for layer_num, layer_folder in lane_dirs:
        for fname in os.listdir(layer_folder):
            if not fname.lower().endswith(".kml"):
                continue
            bm = CHAINAGE_BIN_KML_RE.match(fname)
            if not bm:
                continue
            tag = bm.group(3)
            if not tag.upper().startswith(f"{side_upper}_L"):
                continue
            start_km = float(bm.group(1))
            end_km = float(bm.group(2))
            key = (start_km, end_km)
            fp = os.path.join(layer_folder, fname)
            bin_to_layer_files.setdefault(key, {})[layer_num] = fp

    if not bin_to_layer_files:
        return None
    sorted_bins = sorted(bin_to_layer_files.keys(), key=lambda k: (k[0], k[1]))
    out_dir = os.path.join(side_root, merge_folder_name)
    os.makedirs(out_dir, exist_ok=True)
    clear_folder(out_dir)
    for start_km, end_km in sorted_bins:
        layer_map = bin_to_layer_files[(start_km, end_km)]
        mk = simplekml.Kml()
        for layer_num, _ in lane_dirs:
            fp = layer_map.get(layer_num)
            if fp:
                _append_polygons_from_kml_file(mk, fp)
        out_name = (
            f"Chainage_{start_km:.{CHAINAGE_DECIMALS}f}_to_{end_km:.{CHAINAGE_DECIMALS}f}_"
            f"{side_tag}_merged.kml"
        )
        mk.save(os.path.join(out_dir, out_name))
    return out_dir

def create_chainage_line_kml(df_chain, out_kml_path):
    """
    Create KML showing:
    - 5m chainage segments as WHITE LineStrings
    - Placemark POINT at every 5m chainage (YELLOW)
    """
    print("-> Creating 5m chainage Line + Point KML...")
    kml = simplekml.Kml()
 
    for i in range(len(df_chain) - 1):
        row1 = df_chain.iloc[i]
        row2 = df_chain.iloc[i + 1]
 
        start_km = round(row1["chainage_km"], CHAINAGE_DECIMALS)
        end_km   = round(row2["chainage_km"], CHAINAGE_DECIMALS)
 
        p1 = (row1["longitude"], row1["latitude"])
        p2 = (row2["longitude"], row2["latitude"])
 
        seg_name = f"{start_km:.{CHAINAGE_DECIMALS}f}_to_{end_km:.{CHAINAGE_DECIMALS}f}"
 
        # 1. WHITE LINE SEGMENT
        line = kml.newlinestring(
            name=f"Chainage_{seg_name}",
            coords=[p1, p2]
        )
        line.style.linestyle.width = 3
        line.style.linestyle.color = simplekml.Color.white
 
        # 2. YELLOW PLACEMARK POINT
        point = kml.newpoint(
            name=f"CH {start_km:.{CHAINAGE_DECIMALS}f}",
            coords=[p1]
        )
        point.style.iconstyle.scale = 0.8
        point.style.iconstyle.color = simplekml.Color.yellow
 
    # add last end point
    last = df_chain.iloc[-1]
    last_km = round(last["chainage_km"], CHAINAGE_DECIMALS)
    last_pt = (last["longitude"], last["latitude"])
 
    point = kml.newpoint(
        name=f"CH {last_km:.{CHAINAGE_DECIMALS}f}",
        coords=[last_pt]
    )
    point.style.iconstyle.scale = 0.8
    point.style.iconstyle.color = simplekml.Color.yellow
 
    kml.save(out_kml_path)
    print("-> Chainage Line + Point KML saved:", out_kml_path)


# -----------------------------
# Pipeline Execution
# -----------------------------
def run_pipeline():
    # Clear and ensure output folders exist
    print(f"0) Initializing output folders in {OUTPUT_FOLDER}...")
    for p in [KML_LHS_FOLDER, KML_RHS_FOLDER, EXCEL_FOLDER, KML_MERGED_FOLDER]:
        clear_folder(p)
        os.makedirs(p, exist_ok=True)

    print("1) Reading input KML & interpolating...")
    line_coords = read_linestring_from_kml(INPUT_KML)
    interp_points = interpolate_geodesic_points(line_coords, INTERVAL_METERS)
    if not interp_points:
        raise RuntimeError("No interpolation points generated - check input KML and INTERVAL_METERS.")

    chainage_strs, chainage_nums = make_chainages(CHAINAGE_START_KM, len(interp_points), INTERVAL_METERS)
    if len(chainage_nums) != len(interp_points):
        raise RuntimeError("Chainage count mismatch vs interpolated points")

    df_chain = pd.DataFrame({
        "chainage_km_str": chainage_strs,
        "chainage_km": chainage_nums,
        "latitude": [p[1] for p in interp_points],
        "longitude": [p[0] for p in interp_points]
    })

    print("2) Saving chainage Excel...")
    chain_excel = os.path.join(EXCEL_FOLDER, "chainage_points.xlsx")
    df_chain_to_segment_excel(df_chain, chain_excel)
    print(f"  [OK] Saved: {chain_excel}")

    # NEW - Create chainage line KML (5m segments)
    chainage_kml_path = os.path.join(KML_MERGED_FOLDER, "line_polygons_chainage.kml")
    create_chainage_line_kml(df_chain, chainage_kml_path)
    
    # 2) Median offsets (Median_LHS / Median_RHS)
    print(f"2) Computing Median_LHS & Median_RHS (offset = {OFFSET_LINE_POLYGONS_EXCEL} m)...")
    offset_L = []
    offset_R = []
    for i, row in df_chain.iterrows():
        lat = row.latitude
        lon = row.longitude
        prev_pt = None
        next_pt = None
        if i > 0:
            prev_pt = Point(latitude=df_chain.loc[i - 1, "latitude"],
                            longitude=df_chain.loc[i - 1, "longitude"])
        if i < len(df_chain) - 1:
            next_pt = Point(latitude=df_chain.loc[i + 1, "latitude"],
                            longitude=df_chain.loc[i + 1, "longitude"])
        lat_l, lon_l = offset_point(lat, lon, OFFSET_LINE_POLYGONS_EXCEL, "left", prev_pt=prev_pt, next_pt=next_pt)
        lat_r, lon_r = offset_point(lat, lon, OFFSET_LINE_POLYGONS_EXCEL, "right", prev_pt=prev_pt, next_pt=next_pt)
        offset_L.append((lon_l, lat_l))
        offset_R.append((lon_r, lat_r))

    median_lhs_path = os.path.join(EXCEL_FOLDER, "median_lhs_offset.xlsx")
    median_rhs_path = os.path.join(EXCEL_FOLDER, "median_rhs_offset.xlsx")

    df_median_lhs = save_offset_excel(df_chain, offset_L, median_lhs_path)
    df_median_rhs = save_offset_excel(df_chain, offset_R, median_rhs_path)

    print(f"  [OK] Saved: {median_lhs_path}")
    print(f"  [OK] Saved: {median_rhs_path}")

    # 3) Generate lane layers based on LANE_COUNT
    print(f"3) Generating lane layers for LANE_COUNT = {LANE_COUNT} ...")
    def compute_layer_count(lane_count):
        if lane_count < 2:
            return 0
        cnt = 1
        if lane_count >= 4:
            cnt += 1
        if lane_count >= 6:
            cnt += 1
        return cnt

    left_layers = compute_layer_count(LANE_COUNT)
    right_layers = left_layers

    left_created = {}
    right_created = {}

    if left_layers > 0:
        left_created = create_layers_from_base(median_lhs_path, "left", "LHS", left_layers)
    if right_layers > 0:
        right_created = create_layers_from_base(median_rhs_path, "right", "RHS", right_layers)

    print("-> Left layers created:", left_created)
    print("-> Right layers created:", right_created)

    # 4) Build layer_pairs (parent -> child) for L1->L2 and L2->L3
    print("4) Preparing layer pairings for KML generation...")
    layer_pairs = []  # tuples (path_parent, path_child, out_folder, layer_tag)

    def add_pair_if_exists(parent_key, child_key, base_folder, created_dict):
        if parent_key in created_dict and child_key in created_dict:
            out_folder = os.path.join(base_folder, child_key)
            os.makedirs(out_folder, exist_ok=True)
            layer_pairs.append((created_dict[parent_key], created_dict[child_key], out_folder, child_key))
            print(f"  [OK] Pair registered: {parent_key} -> {child_key}")

    # L1 pairing: median -> L1
    if "LHS_L1" in left_created:
        lhs_l1_folder = os.path.join(KML_LHS_FOLDER, "LHS_L1")
        os.makedirs(lhs_l1_folder, exist_ok=True)
        layer_pairs.append((median_lhs_path, left_created["LHS_L1"], lhs_l1_folder, "LHS_L1"))
        print("  [OK] LHS_L1 pair (median -> LHS_L1) added")
    if "RHS_L1" in right_created:
        rhs_l1_folder = os.path.join(KML_RHS_FOLDER, "RHS_L1")
        os.makedirs(rhs_l1_folder, exist_ok=True)
        layer_pairs.append((median_rhs_path, right_created["RHS_L1"], rhs_l1_folder, "RHS_L1"))
        print("  [OK] RHS_L1 pair (median -> RHS_L1) added")

    # L2 and L3 pairings
    add_pair_if_exists("LHS_L1", "LHS_L2", KML_LHS_FOLDER, left_created)
    add_pair_if_exists("RHS_L1", "RHS_L2", KML_RHS_FOLDER, right_created)
    add_pair_if_exists("LHS_L2", "LHS_L3", KML_LHS_FOLDER, left_created)
    add_pair_if_exists("RHS_L2", "RHS_L3", KML_RHS_FOLDER, right_created)

    # 5-8) Run side pipelines in parallel from KML generation through image rendering.
    print("5-8) Running LHS/RHS side pipelines in parallel ...")
    side_jobs = []
    lhs_pairs = [p for p in layer_pairs if p[3].upper().startswith("LHS_")]
    rhs_pairs = [p for p in layer_pairs if p[3].upper().startswith("RHS_")]
    if lhs_pairs:
        side_jobs.append(("LHS", KML_LHS_FOLDER, lhs_pairs))
    if rhs_pairs:
        side_jobs.append(("RHS", KML_RHS_FOLDER, rhs_pairs))

    all_generated_kmls = []
    side_results = {}
    if side_jobs:
        with ThreadPoolExecutor(max_workers=min(2, len(side_jobs))) as ex:
            futures = {
                ex.submit(_run_side_pipeline, side, root, pairs, IMAGE_DIRECTION): side
                for side, root, pairs in side_jobs
            }
            for fut in as_completed(futures):
                side = futures[fut]
                try:
                    result = fut.result()
                    side_results[side] = result
                    all_generated_kmls.extend(result.get("generated_kmls", []))
                except Exception as exc:
                    print(f"  -> {side} side pipeline failed: {exc}")

    for side in ["LHS", "RHS"]:
        result = side_results.get(side)
        if not result:
            continue
        res = result["image_result"]
        print(f"  -> {side} rotated images: {res['rotated']} files using {IMAGE_DIRECTION}")
        print(f"  -> {side} images: {res['count']} files in {res['out_dir']}")
        print(f"  -> {side} merge rotated images: {res['rotated_merge']} files using {IMAGE_DIRECTION}")
        print(f"  -> {side} merge images: {res['merge_count']} files in {res['merge_out_dir']}")

    print("ALL DONE")
    print(f"Output folder: {OUTPUT_FOLDER}")
    return {
        "chainage_excel": chain_excel,
        "median_lhs": median_lhs_path,
        "median_rhs": median_rhs_path,
        "layer_excels": {**left_created, **right_created},
        "generated_kmls": all_generated_kmls
    }

if __name__ == "__main__":
    import traceback
    try:
        run_pipeline()
    except Exception as e:
        print("CRITICAL_PYTHON_ERROR_START")
        traceback.print_exc()
        print("CRITICAL_PYTHON_ERROR_END")
        sys.exit(1)
