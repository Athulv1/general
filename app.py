#!/usr/bin/env python3
"""
Flooring Layout — web app backend (Flask).

Endpoints
---------
GET  /              -> serves the single-page app (flooring_app.html)
POST /layers        -> upload a DXF, returns its layer names (classified)
POST /generate      -> upload DXF + config JSON, runs the pipeline,
                       returns the run log + a job id for download
GET  /download/<job>-> downloads that job's generated flooring DXF

Run:
    py -3 app.py
    then open http://127.0.0.1:5000
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import ezdxf
from flask import Flask, request, jsonify, send_file, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(HERE, "jobs")
os.makedirs(JOBS, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB DXF cap


@app.route("/")
def index():
    return send_from_directory(HERE, "flooring_app.html")


@app.route("/layers", methods=["POST"])
def layers():
    """Read an uploaded DXF and classify its layers so the form can suggest
    sensible candidates: closed-polygon layers (store / zones), and
    point/insert layers (start point)."""
    f = request.files.get("dxf")
    if not f:
        return jsonify(error="no file uploaded"), 400
    tmp = os.path.join(JOBS, "_layers_" + uuid.uuid4().hex + ".dxf")
    f.save(tmp)
    try:
        doc = ezdxf.readfile(tmp)
        all_names = {l.dxf.name for l in doc.layers}
        closed, points = set(), set()
        blocks = set()
        texts = {}  # readable label -> layer it sits on (first seen)
        open_linework = {}  # layer -> [point lists] (open plines + lines)

        def scan(container):
            for e in container:
                t = e.dxftype()
                if t == "INSERT":
                    blocks.add(e.dxf.name)
                    try:
                        scan(doc.blocks[e.dxf.name])
                    except Exception:
                        pass
                    points.add(_layer(e))
                    continue
                if t == "LWPOLYLINE":
                    if getattr(e, "closed", False):
                        closed.add(_layer(e))
                    else:
                        try:
                            pts = [(p[0], p[1]) for p in e.get_points("xy")]
                            if len(pts) >= 2:
                                open_linework.setdefault(_layer(e), []).append(pts)
                        except Exception:
                            pass
                elif t == "POLYLINE":
                    if getattr(e, "is_closed", False):
                        closed.add(_layer(e))
                    else:
                        try:
                            pts = [(v.dxf.location.x, v.dxf.location.y)
                                   for v in e.vertices]
                            if len(pts) >= 2:
                                open_linework.setdefault(_layer(e), []).append(pts)
                        except Exception:
                            pass
                elif t == "LINE":
                    try:
                        open_linework.setdefault(_layer(e), []).append(
                            [(e.dxf.start.x, e.dxf.start.y),
                             (e.dxf.end.x, e.dxf.end.y)])
                    except Exception:
                        pass
                elif t == "POINT":
                    points.add(_layer(e))
                elif t in ("MTEXT", "TEXT"):
                    try:
                        raw = e.plain_text() if t == "MTEXT" else (e.dxf.text or "")
                        label = " ".join(raw.split())  # collapse newlines/codes
                        if label and any(c.isalpha() for c in label) and len(label) <= 60:
                            texts.setdefault(label, _layer(e))
                    except Exception:
                        pass

        scan(doc.modelspace())
        closed.discard(None); points.discard(None)

        # a layer whose OPEN polylines/lines chain end-to-end into closed
        # rings also qualifies for the closed-shape dropdown (the extractor
        # applies the same chaining at generate time)
        from zone_service import chain_rings
        for lname, plists in open_linework.items():
            if lname in closed or lname is None or len(plists) > 400:
                continue
            try:
                if chain_rings(plists):
                    closed.add(lname)
            except Exception:
                pass
        return jsonify(
            all=sorted(all_names),
            closed=sorted(c for c in closed if c),
            points=sorted(p for p in points if p),
            blocks=sorted(b for b in blocks if b),
            texts=[{"label": k, "layer": v} for k, v in sorted(texts.items())],
        )
    except Exception as exc:
        return jsonify(error=str(exc)), 400
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _layer(e):
    try:
        return e.dxf.layer
    except Exception:
        return None


@app.route("/patterns")
def patterns():
    """Return the hatch pattern names ezdxf can produce (for the form)."""
    try:
        from ezdxf.tools import pattern
        names = ["SOLID"] + sorted(pattern.load().keys())
        return jsonify(patterns=names)
    except Exception as exc:
        return jsonify(patterns=["SOLID", "ANSI31", "NET", "AR-HBONE", "AR-PARQ1", "AR-CONC"],
                       error=str(exc))


@app.route("/generate", methods=["POST"])
def generate():
    """Save the uploaded DXF + config, run the schema-3.1 pipeline as a
    subprocess, and return per-docket results (boq + warnings) + a job id."""
    f = request.files.get("dxf")
    cfg_raw = request.form.get("config")
    if not f or not cfg_raw:
        return jsonify(error="need both a DXF file and a config"), 400
    try:
        cfg = json.loads(cfg_raw)
    except Exception as exc:
        return jsonify(error="invalid config JSON: " + str(exc)), 400

    job = uuid.uuid4().hex[:12]
    wd = os.path.join(JOBS, job)
    os.makedirs(wd, exist_ok=True)

    in_dxf = os.path.join(wd, "input.dxf")
    f.save(in_dxf)

    cfg.setdefault("io", {})
    cfg["io"]["input_dxf"] = in_dxf
    cfg["io"]["output_dir"] = wd
    cfg_path = os.path.join(wd, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)

    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "run_pipeline.py"),
         cfg_path, in_dxf, wd],
        capture_output=True, text=True, cwd=HERE,
    )
    log = (proc.stdout or "")
    if proc.stderr:
        log += "\n--- stderr ---\n" + proc.stderr

    run_log = _read_json(os.path.join(wd, "run_log.json")) or {}
    boq = _read_json(os.path.join(wd, "boq_summary.json")) or {}
    dockets = {}
    for name, entry in (run_log.get("dockets") or {}).items():
        dockets[name] = {
            "ok": entry.get("ok"),
            "warnings": entry.get("warnings") or [],
            "error": entry.get("error"),
            "boq": boq.get(name) or {},
            "download": (f"/download/{job}/{name}"
                         if entry.get("dxf") else None),
        }
    ok = proc.returncode == 0 and bool(run_log.get("ok"))
    return jsonify(ok=ok, job=(job if ok else None), dockets=dockets,
                   errors=run_log.get("errors") or [],
                   warnings=run_log.get("resolver_warnings") or [],
                   boq_summary=boq, log=log)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


@app.route("/download/<job>")
def download(job):
    """Back-compat: the bare job URL serves the flooring docket."""
    return download_docket(job, "flooring")


@app.route("/download/<job>/<docket>")
def download_docket(job, docket):
    safe_job = "".join(c for c in job if c.isalnum())
    safe_docket = "".join(c for c in docket if c.isalnum() or c == "_")
    p = os.path.join(JOBS, safe_job, f"{safe_docket}.dxf")
    if not os.path.exists(p):
        return "not found", 404
    return send_file(p, as_attachment=True,
                     download_name=f"{safe_docket}.dxf")


if __name__ == "__main__":
    print("Flooring app running -> http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
