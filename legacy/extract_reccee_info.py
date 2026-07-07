try:
    import ezdxf
except ImportError:
    ezdxf = None  # type: ignore
from ezdxf.math import Vec2
import json, os, sys, shutil, subprocess, tempfile, pathlib
import math
from typing import Optional, List, Dict, Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BOQ_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))

INPUT_DIR = os.path.join(_BOQ_ROOT, "input")
OUTPUT_DIR = os.path.join(_BOQ_ROOT, "output")

def convert_dwg_to_dxf(file_path):
    """Converts a DWG file to DXF using ODA File Converter."""
    oda_converter_path = _find_oda_converter()
    if not oda_converter_path or not os.path.exists(oda_converter_path):
        raise FileNotFoundError(
            "ODA File Converter not found. Please install it or set ODAFC_PATH.")

    print(f"Converting {file_path} to DXF using {oda_converter_path}...")
    temp_in = tempfile.mkdtemp()
    temp_out = tempfile.mkdtemp()

    filename = os.path.basename(file_path)
    shutil.copy2(file_path, os.path.join(temp_in, filename))

    run_env = os.environ.copy()
    for var in ["QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "PYTHONPATH"]:
        run_env.pop(var, None)
    run_env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:" + run_env.get("PATH", "")

    cmd = [oda_converter_path, temp_in, temp_out, "ACAD2018", "DXF", "0", "1", filename]

    try:
        if sys.platform == 'linux' and not oda_converter_path.lower().endswith('.exe'):
            xvfb_path = shutil.which("xvfb-run") or (
                "/usr/bin/xvfb-run" if os.path.exists("/usr/bin/xvfb-run") else None
            )
            if xvfb_path:
                cmd_str = f'{xvfb_path} -a "{oda_converter_path}" "{temp_in}" "{temp_out}" "ACAD2018" "DXF" "0" "1" "{filename}"'
                subprocess.run(cmd_str, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=run_env)
            elif not os.environ.get("DISPLAY"):
                run_env["QT_QPA_PLATFORM"] = "offscreen"
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=run_env)
            else:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=run_env)
        else:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=run_env)
    except subprocess.CalledProcessError as e:
        shutil.rmtree(temp_in)
        shutil.rmtree(temp_out)
        raise RuntimeError(f"Conversion failed: {e.stderr.decode()}")

    base_name = os.path.splitext(filename)[0]
    dxf_filename = base_name + ".dxf"
    temp_dxf_path = os.path.join(temp_out, dxf_filename)
    final_dxf_path = os.path.join(INPUT_DIR, dxf_filename)

    # Ensure INPUT_DIR exists
    os.makedirs(INPUT_DIR, exist_ok=True)

    if os.path.exists(temp_dxf_path):
        shutil.copy2(temp_dxf_path, final_dxf_path)
        shutil.rmtree(temp_in)
        shutil.rmtree(temp_out)
        return final_dxf_path

    shutil.rmtree(temp_in)
    shutil.rmtree(temp_out)
    raise FileNotFoundError(f"Failed to find converted DXF for {filename}")

def _find_oda_converter():
    """Find ODA File Converter executable."""
    env = os.environ.get("ODAFC_PATH")
    if env and pathlib.Path(env).is_file():
        return env

    on_path = shutil.which("ODAFileConverter")
    if on_path:
        return on_path

    # Check Windows native paths
    for pf in [r"C:\Program Files", r"C:\Program Files (x86)"]:
        pf_dir = pathlib.Path(pf)
        if not pf_dir.exists():
            continue
        for match in pf_dir.glob("ODA/ODAFileConverter*/ODAFileConverter.exe"):
            return str(match)

    if sys.platform == 'linux':
        # Check native Linux path first
        native_linux_path = pathlib.Path("/usr/bin/ODAFileConverter")
        if native_linux_path.exists():
            return str(native_linux_path)

        # Fallback to WSL Windows paths
        for pf in ["/mnt/c/Program Files", "/mnt/c/Program Files (x86)"]:
            pf_dir = pathlib.Path(pf)
            if not pf_dir.exists():
                continue
            for match in pf_dir.glob("ODA/ODAFileConverter*/ODAFileConverter.exe"):
                return str(match)

    return None

def extract_reccee(dxf_path: str, id_layer: Dict[str, Any]) -> Dict[str, Any]:
    res = {}

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    
    base      = os.path.splitext(os.path.basename(dxf_path))[0]
    ext       = os.path.splitext(dxf_path)[1].lower()

    if ext == ".dwg":
        print("\n--- Step 1a: DWG -> DXF (ODA File Converter) ---")
        try:
            converted_dxf = convert_dwg_to_dxf(dxf_path)
            print(f"  Converted DXF: {converted_dxf}")
        except Exception as e:
            print(f"  [ERROR] DWG conversion failed: {e}")
            return {"output_dxf": None, "report": None}

    for key, val in id_layer.items():
        print(f"START KEY {key}")
        xmin, ymin, xmax, ymax = get_clip_box(doc=doc, layer=key)
        dims = "DIM" in val["types"]
        lines = "LINE" in val["types"]
        lines = collect_lines_in_box(msp, [xmin, ymin, xmax, ymax], read_lines=lines, read_dims=dims)
        if "CANAPE" in key:
            facade_dims = []
            for val in lines["DIMS"]:
                facade_dims.append(val[-1])
        else:
            lines = lines["LINES"]
            facade = extract_facade_boundary(lines)
            res["signage_box"] = get_signage_bbox(msp, lines, facade=facade, text=val.get("signage_key", ""))
        print(f"END KEY {key}")
    
    facade_dims.sort()
    res["bottom_shutter"] = facade_dims[0]
    res["slab_height"] = facade_dims[-1]
    if len(facade_dims) == 4:
        res["bottom_beam"] = facade_dims[1]
        res["bottom facade"] = facade_dims[2]
    else:
        res["bottom_beam"] = facade_dims[1]
        res["bottom facade"] = facade_dims[1]

    return res

def get_clip_box(doc, layer: str):
    msp = doc.modelspace()
    box_entities = [e for e in msp if e.dxf.layer == layer]

    if not box_entities:
        raise ValueError(f"No entities found on layer {layer}")

    all_x, all_y = [], []

    for e in box_entities:
        etype = e.dxftype()
        if etype == "LINE":
            all_x += [e.dxf.start.x, e.dxf.end.x]
            all_y += [e.dxf.start.y, e.dxf.end.y]
        elif etype == "LWPOLYLINE":
            for pt in e.get_points():
                all_x.append(pt[0])
                all_y.append(pt[1])
        elif etype == "POLYLINE":
            for v in e.vertices:
                all_x.append(v.dxf.location.x)
                all_y.append(v.dxf.location.y)

    if not all_x:
        raise ValueError(f"Could not extract coordinates from {layer} layer")

    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)

    # print(f"Clip box: ({xmin:.2f}, {ymin:.2f}) -> ({xmax:.2f}, {ymax:.2f})")
    # print(f"Box width:  {xmax - xmin:.2f}")
    # print(f"Box height: {ymax - ymin:.2f}")
    return xmin, ymin, xmax, ymax

def line_in_bbox(line, bbox):
    xmin, ymin, xmax, ymax = bbox
    return all(xmin <= p[0] <= xmax and ymin <= p[1] <= ymax for p in line)

def collect_lines_in_box(msp, bbox, read_lines=False, read_dims=False):
    """
    Return all line segments as list of (start_Vec2, end_Vec2, layer).
    Handles LINE and LWPOLYLINE — DWG-converted DXFs use LWPOLYLINE for walls.
    """
    lines = {}
    if read_lines:
        for e in msp.query('LINE'):
            start = (e.dxf.start.x, e.dxf.start.y)
            end = (e.dxf.end.x, e.dxf.end.y)
            if line_in_bbox([start, end], bbox=bbox):
                s  = Vec2(start[0], start[1])
                en = Vec2(end[0],   end[1])
                lines.setdefault("LINES", []).append((s, en, e.dxf.layer))
        for e in msp.query('LWPOLYLINE'):
            try:
                pts   = list(e.get_points())
                layer = e.dxf.layer
                for i in range(len(pts) - 1):
                    start = (pts[i][0],   pts[i][1])
                    end = (pts[i+1][0], pts[i+1][1])
                    if line_in_bbox([start, end], bbox=bbox):
                        s  = Vec2(start[0], start[1])
                        en = Vec2(end[0], end[1])
                        lines.setdefault("LINES", []).append((s, en, e.dxf.layer))
                if e.closed and len(pts) >= 2 and line_in_bbox([pts[-1][0], pts[-1][1], pts[0][0], pts[0][1]], bbox=bbox):
                    lines.append((Vec2(pts[-1][0], pts[-1][1]), Vec2(pts[0][0],  pts[0][1]), layer))
            except Exception:
                pass
    if read_dims:
        for e in msp.query('DIMENSION'):
            ent_s, ent_e, len = get_dimension(e)
            if line_in_bbox([ent_s, ent_e], bbox=bbox):
                lines.setdefault("DIMS", []).append((ent_s, ent_e, e.dxf.layer, len))

    return lines

def distance(p1, p2):
    return math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)

def get_dimension(entity):
    try:
        p1 = entity.dxf.defpoint2
        p2 = entity.dxf.defpoint3
        start = (float(p1.x), float(p1.y))
        end = (float(p2.x), float(p2.y))
    except Exception:
        start, end = None, None

    try:
        length = entity.dxf.actual_measurement
    except Exception:
        length = distance(start, end) if start and end else None

    return start, end, length

def extract_facade_boundary(lines):
    """
    Determine the outer facade rectangle from the drawing geometry.
    Returns dict with keys: x_min, x_max, y_min, y_max, width, height.
    """
    all_x, all_y = [], []
    for s, e, _ in lines:
        all_x.extend([s.x, e.x])
        all_y.extend([s.y, e.y])
    if not all_x:
        return None

    h_lines, v_lines = classify_lines(lines)
    longest_h = max(h_lines, key=lambda l: abs(l[1].x - l[0].x)) if h_lines else None

    if longest_h:
        floor_y = longest_h[0].y
    else:
        floor_y = min(all_y)

    facade_verts = [l for l in v_lines
                    if abs(l[0].y - floor_y) < 2 or abs(l[1].y - floor_y) < 2]

    if facade_verts:
        top_y = max(max(l[0].y, l[1].y) for l in facade_verts)
        xs    = [l[0].x for l in facade_verts]
        x_min = min(xs)
        x_max = max(xs)
    else:
        top_y = max(all_y)
        x_min = min(all_x)
        x_max = max(all_x)

    y_min = min(floor_y, top_y)
    y_max = max(floor_y, top_y)

    return {
        'x_min': x_min, 'x_max': x_max,
        'y_min': y_min, 'y_max': y_max,
        'width': x_max - x_min,
        'height': y_max - y_min,
    }

def classify_lines(lines):
    """Split lines into horizontal / vertical buckets (tolerance 1 mm)."""
    h_lines, v_lines = [], []
    for s, e, layer in lines:
        dx = abs(e.x - s.x)
        dy = abs(e.y - s.y)
        if dy < 1.0:
            h_lines.append((s, e, layer))
        elif dx < 1.0:
            v_lines.append((s, e, layer))
    return h_lines, v_lines

def get_signage_bbox(msp, lines, facade=None, text="PROPOSED SIGNAGE", canape=False):
    """
    Finds the PROPOSED SIGNAGE text, inflates to nearest H-lines for top/bottom,
    derives x bounds from those same H-lines, draws the result on layer
    'SIGNAGE_BBOX' (frozen, yellow), and returns the bbox dict.

    Returns dict {x_min, x_max, y_min, y_max, width, height} or None.
    """
    text_pt = None
    for e in msp.query('MTEXT TEXT'):
        text = e.dxf.text if e.dxftype() == 'TEXT' else e.text
        if text in text.upper():
            text_pt = Vec2(e.dxf.insert.x, e.dxf.insert.y)
            break

    if not text_pt:
        return None

    h_lines, v_lines = classify_lines(lines)

    # UP: nearest H-line above text that spans the text x position
    up_lines = [l for l in h_lines
                if l[0].y > text_pt.y
                and min(l[0].x, l[1].x) - 1 <= text_pt.x <= max(l[0].x, l[1].x) + 1]
    top_y    = min(up_lines, key=lambda l: l[0].y)[0].y if up_lines else (
               facade['y_max'] if facade else text_pt.y + 1000)

    # DOWN: nearest H-line below text that spans the text x position
    down_lines = [l for l in h_lines
                  if l[0].y < text_pt.y
                  and min(l[0].x, l[1].x) - 1 <= text_pt.x <= max(l[0].x, l[1].x) + 1]
    bottom_y   = max(down_lines, key=lambda l: l[0].y)[0].y if down_lines else (
                 facade['y_min'] if facade else text_pt.y - 1000)

    # X bounds: read directly from the x-extents of the top and bottom H-lines.
    # More reliable than V-line inflation — panel boundary lines are often
    # slightly non-vertical (dx ~5-10 mm) and get filtered out by the classifier.
    if up_lines and down_lines:
        top_line = min(up_lines,  key=lambda l: l[0].y)
        bot_line = max(down_lines, key=lambda l: l[0].y)
        left_x  = max(min(top_line[0].x, top_line[1].x),
                      min(bot_line[0].x, bot_line[1].x))
        right_x = min(max(top_line[0].x, top_line[1].x),
                      max(bot_line[0].x, bot_line[1].x))
    elif facade:
        left_x  = facade['x_min']
        right_x = facade['x_max']
    else:
        left_x  = text_pt.x - 1000
        right_x = text_pt.x + 1000

    height = top_y - bottom_y
    if height < 100:
        return None

    return {
        'x_min':  left_x,
        'x_max':  right_x,
        'y_min':  bottom_y,
        'y_max':  top_y,
        'width':  right_x - left_x,
        'height': height,
    }