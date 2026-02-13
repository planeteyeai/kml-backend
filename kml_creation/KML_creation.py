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
"""
import sys
import os
import math
import shutil
from xml.dom import minidom
from pyproj import Geod
import pandas as pd
from geopy.distance import geodesic
from geopy import Point
import simplekml

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
    INTERVAL_METERS = 5
    LANE_COUNT = 4                      #change # allowed values: 0,2,4,6. 2 -> L1 only, 4 -> L1+L2, 6 -> L1+L2+L3
    LANE_STEP_M =  3.4              # meters per lane offset step
    KML_MERGE_OFFSET_KM = 0.100      #change # 0.100 km -> 100 m bins
    OFFSET_LINE_POLYGONS_EXCEL = 2.75  #change # meters (median left/right offset)

CHAINAGE_DECIMALS = 3

# geodetic util
geod = Geod(ellps="WGS84")

# output folders
KML_LHS_FOLDER = os.path.join(OUTPUT_FOLDER, "LHS_KMLs")
KML_RHS_FOLDER = os.path.join(OUTPUT_FOLDER, "RHS_KMLs")
EXCEL_FOLDER = os.path.join(OUTPUT_FOLDER, "Excels")
KML_MERGED_FOLDER = os.path.join(OUTPUT_FOLDER, "Merge_KMLs")

# -----------------------------
# Utility functions
# -----------------------------
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

def read_linestring_from_kml(kml_path):
    """Read all <coordinates> from KML and return a combined list of (lon, lat)."""
    try:
        doc = minidom.parse(kml_path)
        coords_elements = doc.getElementsByTagName("coordinates")
        if coords_elements.length == 0:
            raise ValueError("No <coordinates> found in KML.")
        
        all_coords = []
        for c_el in coords_elements:
            if c_el.firstChild and c_el.firstChild.nodeValue.strip():
                coord_text = c_el.firstChild.nodeValue.strip()
                coord_pairs = coord_text.split()
                for pair in coord_pairs:
                    parts = pair.split(",")
                    if len(parts) < 2:
                        continue
                    lon = float(parts[0])
                    lat = float(parts[1])
                    all_coords.append((lon, lat))
        
        if not all_coords:
            raise ValueError("All <coordinates> tags are empty.")

        return all_coords
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
        # find segment index
        i = 0
        while i < len(cum) - 1 and cum[i + 1] < cur - 1e-9:
            i += 1
        seg_start = cum[i]
        seg_end = cum[i + 1] if i + 1 < len(cum) else seg_start
        if seg_end == seg_start:
            frac = 0.0
        else:
            frac = (cur - seg_start) / (seg_end - seg_start)
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


def merge_layer_folder_to_single_kml(layer_folder, out_merge_path):
    """Merge all KML polygons in layer_folder into a single KML file, preserving each polygon's name."""
    files = [os.path.join(layer_folder, f) for f in os.listdir(layer_folder) if f.lower().endswith('.kml')]
    files = sorted(files)
    mk = simplekml.Kml()
    for fp in files:
        try:
            doc = minidom.parse(fp)
        except Exception:
            continue
        placemarks = doc.getElementsByTagName("Placemark")
        for pm in placemarks:
            # Get Placemark name (use first <name> child; fallback to "Untitled Polygon" if missing)
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
                        lon = float(parts[0]); lat = float(parts[1])
                        coords.append((lon, lat))
                    mk.newpolygon(name=poly_name, outerboundaryis=coords)
    mk.save(out_merge_path)
    return out_merge_path

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

    # 5) Generate per-layer binned KMLs
    print("5) Generating per-layer binned KMLs ...")
    all_generated_kmls = []
    for a_path, b_path, out_folder, layer_tag in layer_pairs:
        outs = generate_layer_bin_kmls(a_path, b_path, out_folder, layer_tag, bin_km=KML_MERGE_OFFSET_KM)
        all_generated_kmls.extend(outs)
        print(f"  -> Generated {len(outs)} bin-KMLs for {layer_tag} in {out_folder}")

    # 6) Merge each layer folder into a single KML under Merge_KMLs
    print("6) Merging layer folders into single KMLs in Merge_KMLs ...")
    for sub in os.listdir(KML_LHS_FOLDER):
        layer_dir = os.path.join(KML_LHS_FOLDER, sub)
        if os.path.isdir(layer_dir):
            out_merge = os.path.join(KML_MERGED_FOLDER, f"{sub}_merge.kml")
            merge_layer_folder_to_single_kml(layer_dir, out_merge)
            print(f"  -> Merged {sub} -> {out_merge}")

    for sub in os.listdir(KML_RHS_FOLDER):
        layer_dir = os.path.join(KML_RHS_FOLDER, sub)
        if os.path.isdir(layer_dir):
            out_merge = os.path.join(KML_MERGED_FOLDER, f"{sub}_merge.kml")
            merge_layer_folder_to_single_kml(layer_dir, out_merge)
            print(f"  -> Merged {sub} -> {out_merge}")

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
