# Environment Setup: 
# pip install ezdxf
# pip install shapely

import os
import glob
import ezdxf

try:
    from shapely.geometry import Polygon, box, Point
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False
    print("WARNING: 'shapely' library is not installed. Advanced speaker placement requires it.")
    print("Please run: pip install shapely")


def _extract_raw_mtext_blocks(dxf_path):
    try:
        with open(dxf_path, 'r', errors='replace') as f:
            content = f.read()
    except Exception:
        return {}
    blocks = {}
    lines = content.split('\n')
    i = 0
    while i < len(lines):
        if lines[i].strip() == '0' and i + 1 < len(lines) and lines[i + 1].strip() == 'MTEXT':
            start = i
            i += 2
            handle = None
            while i < len(lines):
                if lines[i].strip() == '0':
                    break
                if lines[i].strip() == '5' and i + 1 < len(lines):
                    handle = lines[i + 1].strip()
                i += 2
            if handle:
                blocks[handle] = '\n'.join(lines[start:i])
        else:
            i += 1
    return blocks


def _transplant_mtext_blocks(dxf_path, raw_blocks_by_handle):
    if not raw_blocks_by_handle:
        return
    try:
        with open(dxf_path, 'r', errors='replace') as f:
            content = f.read()
    except Exception:
        return
    lines = content.split('\n')
    out = []
    replaced = 0
    i = 0
    while i < len(lines):
        if lines[i].strip() == '0' and i + 1 < len(lines) and lines[i + 1].strip() == 'MTEXT':
            entity_start = i
            i += 2
            handle = None
            while i < len(lines):
                if lines[i].strip() == '0':
                    break
                if lines[i].strip() == '5' and i + 1 < len(lines):
                    handle = lines[i + 1].strip()
                i += 2
            if handle and handle in raw_blocks_by_handle:
                out.append(raw_blocks_by_handle[handle])
                replaced += 1
            else:
                out.append('\n'.join(lines[entity_start:i]))
        else:
            out.append(lines[i])
            i += 1
    try:
        with open(dxf_path, 'w', errors='replace') as f:
            f.write('\n'.join(out))
        print(f"  [MTEXT transplant] OK - {replaced}/{len(raw_blocks_by_handle)} entities restored verbatim.")
    except Exception as e:
        print(f"  [MTEXT transplant] WRITE ERROR: {e}")


_raw_mtext = {}


def load_dxf(filepath):
    """Loads a DXF file and returns the document object."""
    global _raw_mtext
    try:
        _raw_mtext = _extract_raw_mtext_blocks(filepath)
        print(f"  [MTEXT transplant] Extracted {len(_raw_mtext)} raw MTEXT blocks from input.")
        doc = ezdxf.readfile(filepath)
        return doc
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return None

def detect_foh_points(doc):
    """Detects FOH_ZONE_OUTLINE and returns its raw points to form a polygon."""
    msp = doc.modelspace()
    foh_outline = []
    
    # Look for LWPOLYLINE on the specific layer
    for entity in msp.query('LWPOLYLINE[layer=="FOH_ZONE_OUTLINE"]'):
        foh_outline = [(p[0], p[1]) for p in entity.get_points(format='xy')]
        if foh_outline:
            break
            
    if not foh_outline:
        # Fallback: check all polylines for layer containing FOH_ZONE
        for entity in msp.query('LWPOLYLINE'):
            if 'FOH_ZONE_OUTLINE' in entity.dxf.layer.upper():
                foh_outline = [(p[0], p[1]) for p in entity.get_points(format='xy')]
                if foh_outline:
                    break

    return foh_outline

def get_backwall_from_foh(foh):
    import math

    valid_segs = []
    for i in range(len(foh)-1):
        curr = (round(foh[i][0]), round(foh[i][1]))
        next = (round(foh[i+1][0]), round(foh[i+1][1]))
        # print(curr)
        # print(next, "\n")
        if abs(curr[1]-next[1]) < 100:
            valid_segs.append((curr, next))
    
    max_y = None
    min_y = None
    for s in valid_segs:
        # print(s[0][1], s[1][1])
        # print(max_y, min_y)
        if max_y is None:
            # print("if")
            max_y = max(s[0][1], s[1][1])
            min_y = min((s[0][1], s[1][1]))
        else:
            # print("else")
            max_y = max(max_y, max(s[0][1], s[1][1]))
            min_y = min(min_y, min(s[0][1], s[1][1]))
    # print(max_y, min_y)
    mid = min_y + ((max_y - min_y)/2)
    print(mid)
    valid_segs = [s for s in valid_segs if s[0][1] > mid]

    def distance(p1, p2):
        return math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)
    
    longest = max(valid_segs, key=lambda s: distance(s[0], s[1]))
    print(longest)
    return longest

def place_speakers_advanced(doc, foh_points, offset=150, crop_amount=300):
    """
    Advanced speaker placement using Shapely for Computational Geometry.
    Handles irregular room shapes, chamfered corners, and guarantees no wall collisions.
    """
    if not foh_points or len(foh_points) < 3:
        return
        
    msp = doc.modelspace()
    
    print(f"\n  [STEP 1: FOH Zone Detection]")
    # 1. Create Shapely Polygon
    foh_poly = Polygon(foh_points)
    if not foh_poly.is_valid:
        foh_poly = foh_poly.buffer(0) # Attempt to fix self-intersections

    max_y_bw = get_backwall_from_foh(foh_points)[0][1]
    print("max_y_bw", max_y_bw)
        
    # 2. Apply Vertical Crop
    min_x, min_y, max_x, max_y = foh_poly.bounds
    print(f"    -> Original Bounds: Min({min_x:.2f}, {min_y:.2f}) | Max({max_x:.2f}, {max_y:.2f})")
    
    new_min_y = min_y + crop_amount
    new_max_y = max_y - crop_amount

    new_max_y = min(new_max_y, max_y_bw)
    
    print(f"\n  [STEP 2: Vertical Cropping]")
    if new_min_y < new_max_y:
        print(f"    -> Applied {crop_amount}mm Crop. New Y Bounds: {new_min_y:.2f} to {new_max_y:.2f}")
        # Create a cropping rectangle and intersect it with the room polygon
        crop_box = box(min_x - 1000, new_min_y, max_x + 1000, new_max_y) # wide enough to cover all X
        cropped_poly = foh_poly.intersection(crop_box)
    else:
        print("    -> Warning: Cropping amount exceeds FOH zone height. Skipping crop.")
        cropped_poly = foh_poly
        
    if cropped_poly.is_empty:
        print("    -> Warning: FOH zone became empty after cropping. Skipping speakers.")
        return
        
    print(f"\n  [STEP 3: Speaker Placement]")
    # 3. Apply Inward Offset (Negative Buffer)
    print(f"    -> Applying {offset}mm inward offset using polygon buffer...")
    safe_zone = cropped_poly.buffer(-offset)
    
    if safe_zone.is_empty:
        print(f"    -> Warning: FOH zone is too narrow to accommodate an offset of {offset}mm.")
        return
        
    # If the offset splits the room into multiple pieces, take the largest one
    if safe_zone.geom_type == 'MultiPolygon':
        safe_zone = max(safe_zone.geoms, key=lambda p: p.area)
        
    # 4. Find the 4 Extreme Corners
    coords = list(safe_zone.exterior.coords)
    
    # Mathematical Heuristics to find the absolute extreme physical corners
    top_right = max(coords, key=lambda p: p[0] + p[1])
    bottom_left = min(coords, key=lambda p: p[0] + p[1])
    top_left = min(coords, key=lambda p: p[0] - p[1])
    bottom_right = max(coords, key=lambda p: p[0] - p[1])
    
    positions = [top_left, top_right, bottom_right, bottom_left]
    positions = list(set(positions)) # Remove duplicates
    
    if 'AUDIO_SPEAKER' not in doc.layers:
        doc.layers.add('AUDIO_SPEAKER', color=1) # 1 = Red
        
    speaker_size = 300
    half = speaker_size / 2
    for i, pos in enumerate(positions):
        print(f"    -> Placed Speaker {i+1} at ({pos[0]:.2f}, {pos[1]:.2f})")
        
        # Draw 300x300 Square
        p1 = (pos[0]-half, pos[1]-half)
        p2 = (pos[0]+half, pos[1]-half)
        p3 = (pos[0]+half, pos[1]+half)
        p4 = (pos[0]-half, pos[1]+half)
        msp.add_lwpolyline([p1, p2, p3, p4], close=True, dxfattribs={'layer': 'AUDIO_SPEAKER'})
        
        # Add labels
        label_pos = (pos[0], pos[1] - half - 100)
        msp.add_text("SPEAKER", dxfattribs={'layer': 'AUDIO_SPEAKER', 'height': 50}).set_placement(label_pos)

def detect_pickup_table(doc):
    """Detects pickup window table based on block name or text, and finds its geometric center."""
    msp = doc.modelspace()
    
    # 1. Search Block References (INSERT entities)
    for insert in msp.query('INSERT'):
        if 'pickup' in insert.dxf.name.lower():
            # Calculate true global centroid based on inner geometries of the block
            if insert.dxf.name in doc.blocks:
                block = doc.blocks[insert.dxf.name]
                min_x, min_y = float('inf'), float('inf')
                max_x, max_y = -float('inf'), -float('inf')
                
                for e in block.query('LWPOLYLINE LINE'):
                    if e.dxftype() == 'LWPOLYLINE':
                        points = e.get_points(format='xy')
                    else:
                        points = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
                        
                    for p in points:
                        min_x = min(min_x, p[0])
                        max_x = max(max_x, p[0])
                        min_y = min(min_y, p[1])
                        max_y = max(max_y, p[1])
                        
                if min_x != float('inf'):
                    cx = (min_x + max_x) / 2
                    cy = (min_y + max_y) / 2
                    return (insert.dxf.insert.x + cx, insert.dxf.insert.y + cy)
                    
            # Fallback to pure insertion point if block has no lines
            return (insert.dxf.insert.x, insert.dxf.insert.y)
            
    # 2. Search TEXT and MTEXT entities
    text_pos = None
    for text in msp.query('TEXT MTEXT'):
        text_content = text.dxf.text if text.dxftype() == 'TEXT' else text.text
        if 'pickup' in text_content.lower():
            text_pos = (text.dxf.insert.x, text.dxf.insert.y)
            break
                
    if not text_pos:
        return None

    # We found the text. Now let's find the actual drawn TABLE boundary that encloses it
    if HAS_SHAPELY:
        point = Point(text_pos[0], text_pos[1])
        containing_polys = []
        
        for entity in msp.query('LWPOLYLINE'):
            try:
                points = [(p[0], p[1]) for p in entity.get_points(format='xy')]
                if len(points) >= 3:
                    poly = Polygon(points)
                    # Expand by 10mm to catch text that is perfectly flush with the edge
                    if poly.is_valid and poly.buffer(10).contains(point):
                        containing_polys.append(poly)
            except:
                continue
                
        if containing_polys:
            # Sort by area ascending to find the tightest enclosing box (the table itself)
            containing_polys.sort(key=lambda p: p.area)
            tightest_poly = containing_polys[0]
            # Return the true geometric center of the physical table!
            return (tightest_poly.centroid.x, tightest_poly.centroid.y)
            
    # Fallback to text position if no table outline was found
    return text_pos

def place_speaker_box(doc, table_pos):
    """Places a speaker box centered under the pickup table."""
    print(f"\n  [STEP 4: Pickup Table & Speaker Box]")
    if not table_pos:
        print("    -> Pickup table not found. Skipping speaker box.")
        return
        
    msp = doc.modelspace()
    
    if 'AUDIO_BOX' not in doc.layers:
        doc.layers.add('AUDIO_BOX', color=3) # 3 = Green
        
    # Place it directly at the table's detected position (inside the table)
    box_center_x = table_pos[0]
    box_center_y = table_pos[1] 
    
    box_width = 400
    box_height = 200
    
    print(f"    -> Found Pickup Table. Placed Speaker Box at ({box_center_x:.2f}, {box_center_y:.2f})")
    
    # Define rectangle points
    p1 = (box_center_x - box_width/2, box_center_y - box_height/2)
    p2 = (box_center_x + box_width/2, box_center_y - box_height/2)
    p3 = (box_center_x + box_width/2, box_center_y + box_height/2)
    p4 = (box_center_x - box_width/2, box_center_y + box_height/2)
    
    msp.add_lwpolyline([p1, p2, p3, p4], close=True, dxfattribs={'layer': 'AUDIO_BOX'})
    
    # Add label
    label_pos = (box_center_x - 180, box_center_y - box_height/2 - 100)
    msp.add_text("SPEAKER BOX", dxfattribs={'layer': 'AUDIO_BOX', 'height': 50}).set_placement(label_pos)

def save_output(doc, filepath):
    """Saves the modified DXF document."""
    try:
        doc.saveas(filepath)
        _transplant_mtext_blocks(filepath, _raw_mtext)
    except Exception as e:
        print(f"Error saving {filepath}: {e}")

def generate(input_dxf: str, output_path: str, ctx=None):
    """Docket 2 — audio layout entry point for main pipeline."""
    global _raw_mtext
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if ctx is not None and ctx["raw_mtext"]:
        _raw_mtext = ctx["raw_mtext"]
        doc = ezdxf.readfile(input_dxf)
    else:
        doc = load_dxf(input_dxf)
    if not doc:
        return None

    # ── Inputs from ctx (skip msp reads when context available) ──────────────
    if ctx is not None and ctx["foh-area"] is not None:
        foh_points = list(ctx["foh-area"][0])
    else:
        foh_points = detect_foh_points(doc)

    print(foh_points)

    def centroid(points):
        x = sum(p[0] for p in points) / len(points)
        y = sum(p[1] for p in points) / len(points)
        return (x, y)

    # print("ctx: ", ctx["text"]["pickup table"])
    if ctx is not None and ctx["text"]["pickup table"] is not None:
        table_pos = centroid(ctx["text"]["pickup table"][0]["bbox"])
        print("******TABLE POS", table_pos)
    else:
        table_pos = detect_pickup_table(doc)

    # TODO: Get miny value of BOH at xmin and xmax. Define the ymax for left and right for speaker
    

    # ── Drawing ───────────────────────────────────────────────────────────────
    if foh_points:
        place_speakers_advanced(doc, foh_points, offset=150, crop_amount=300)
    place_speaker_box(doc, table_pos)

    # legend_utils legend disabled — legend_builder.py handles this from main.py
    # import importlib.util as _ilu, os as _os
    # from shapely.geometry import Polygon as _Poly
    # _legend_spec = _ilu.spec_from_file_location(
    #     'legend_utils',
    #     _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'legend_utils.py'))
    # _legend_mod = _ilu.module_from_spec(_legend_spec)
    # _legend_spec.loader.exec_module(_legend_mod)
    # _msp = doc.modelspace()

    # # Use ctx.store_poly for legend origin; fall back to msp scan
    # if ctx is not None and ctx.store_poly is not None:
    #     _sp_bounds = ctx.store_poly.bounds
    #     _lx = _sp_bounds[2] + 2000.0
    #     _ly = _sp_bounds[1]
    # else:
    #     _sp = None
    #     for _e in _msp.query('LWPOLYLINE'):
    #         if _e.dxf.layer.strip() == 'STORE_OUTLINE':
    #             _pts = list(_e.get_points('xy'))
    #             if len(_pts) >= 3:
    #                 _sp = _Poly(_pts)
    #                 break
    #     _lx = (_sp.bounds[2] + 2000.0) if _sp else 2000.0
    #     _ly = _sp.bounds[1] if _sp else 0.0

    # _legend_mod.draw_legend_for_docket(_msp, doc, 'audio', origin=(_lx, _ly))
    save_output(doc, output_path)
    return output_path

def main():
    """Main workflow to process DXF files."""
    input_dir = "input"
    output_dir = "output"
    
    if not HAS_SHAPELY:
        print("Critical Error: 'shapely' is not installed.")
        print("Please run 'pip install shapely' in your terminal and try again.")
        return

    if not os.path.exists(input_dir):
        print(f"Creating '{input_dir}' directory. Please place DXF files here.")
        os.makedirs(input_dir)
        return
        
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    dxf_files = glob.glob(os.path.join(input_dir, "*.dxf"))
    
    if not dxf_files:
        print(f"No DXF files found in '{input_dir}' directory.")
        return
        
    for filepath in dxf_files:
        print(f"\n{'='*60}")
        print(f"Processing: {filepath}")
        print(f"{'='*60}")
        
        filename = os.path.basename(filepath)
        output_path = os.path.join(output_dir, f"audio_{filename}")
        
        doc = load_dxf(filepath)
        if not doc:
            continue
            
        # 1. Handle FOH Zone and Speakers (Advanced Geometric Approach)
        foh_points = detect_foh_points(doc)
