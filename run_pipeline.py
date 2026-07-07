"""Pipeline orchestrator (schema 3.1) — one config, one DXF, N dockets.

``run(config_path, dxf_path, out_dir)`` loads + validates the config, builds
the shared ZoneResolver/context once, then runs every docket whose section is
present and enabled.  Each docket writes its own DXF
(``<out_dir>/<docket>.dxf``); the run produces one merged
``boq_summary.json`` and a structured ``run_log.json`` (warnings/errors, not
scraped stdout).

Engines are pure library code (no prints, no sys.exit); this module's CLI is
the only place that prints or exits.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from typing import Any, Dict, Optional

import ezdxf

import config_loader
from config_loader import ConfigError
from zone_service import ZoneResolver
from dockets import ENGINES
from dockets.base import Ctx, DocketResult, Out

log = logging.getLogger(__name__)


def run(config_path: Optional[str] = None,
        dxf_path: Optional[str] = None,
        out_dir: Optional[str] = None,
        cfg_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Execute the pipeline; returns the structured RunResult dict."""
    result: Dict[str, Any] = {"ok": False, "dockets": {}, "errors": [],
                              "warnings": [], "boq_summary": {}}

    # ── config ─────────────────────────────────────────────────────────────
    try:
        if cfg_dict is not None:
            cfg = config_loader.load_config_dict(cfg_dict)
        else:
            cfg = config_loader.load_config(config_path)
    except ConfigError as exc:
        result["errors"] = list(exc.errors)
        return result
    except Exception as exc:
        result["errors"] = [f"config load failed: {exc}"]
        return result

    io = cfg.get("io") or {}
    dxf_path = dxf_path or io.get("input_dxf")
    out_dir = out_dir or io.get("output_dir") or "."
    if not dxf_path or not os.path.exists(dxf_path):
        result["errors"] = [f"input DXF not found: {dxf_path!r}"]
        return result
    os.makedirs(out_dir, exist_ok=True)

    # ── source doc + shared context ────────────────────────────────────────
    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as exc:
        result["errors"] = [f"cannot read DXF: {exc}"]
        return result
    resolver = ZoneResolver(doc, dxf_path, cfg)
    ctx = Ctx(doc, dxf_path, cfg, resolver)

    # ── dockets ────────────────────────────────────────────────────────────
    dockets = config_loader.enabled_dockets(cfg)
    if not dockets:
        result["errors"] = ["no docket sections present in the config"]
        return result

    any_ok = False
    for name in dockets:
        engine = ENGINES.get(name)
        if engine is None:
            result["errors"].append(f"no engine registered for '{name}'")
            continue
        out_doc = ezdxf.new("R2010", setup=True)
        _copy_display_header(doc, out_doc)
        out = Out(out_doc, cfg)
        try:
            dres = engine(doc, ctx, cfg[name], out)
        except Exception as exc:
            dres = DocketResult(name).fail(
                f"engine crashed: {exc.__class__.__name__}: {exc}")
            log.error("engine %s crashed:\n%s", name, traceback.format_exc())
        dres.dxf_entities_written = len(out.msp)
        dxf_file = None
        if dres.ok:
            dxf_file = os.path.join(out_dir, f"{name}.dxf")
            try:
                out_doc.saveas(dxf_file)
            except Exception as exc:
                dres.fail(f"failed to save DXF: {exc}")
                dxf_file = None
        entry = dres.to_dict()
        entry["dxf"] = os.path.basename(dxf_file) if dxf_file else None
        result["dockets"][name] = entry
        result["boq_summary"][name] = dres.boq
        any_ok = any_ok or dres.ok

    result["warnings"] = list(resolver.warnings)
    result["ok"] = any_ok
    _write_json(os.path.join(out_dir, "boq_summary.json"),
                result["boq_summary"])
    _write_json(os.path.join(out_dir, "run_log.json"), {
        "ok": result["ok"],
        "errors": result["errors"],
        "resolver_warnings": result["warnings"],
        "dockets": {k: {kk: v[kk] for kk in
                        ("ok", "warnings", "error", "dxf",
                         "dxf_entities_written")}
                    for k, v in result["dockets"].items()},
    })
    return result


def _copy_display_header(src_doc, out_doc) -> None:
    """Copy the source drawing's units/display header vars into an output doc
    so hatch patterns, linetypes and units render exactly as in the source
    (a fresh ezdxf doc defaults to meters, which makes viewers rescale)."""
    for var in ("$INSUNITS", "$MEASUREMENT", "$LUNITS", "$AUNITS",
                "$LTSCALE", "$CELTSCALE", "$PSLTSCALE", "$DIMSCALE"):
        try:
            if var in src_doc.header:
                out_doc.header[var] = src_doc.header[var]
        except Exception:
            pass


def _write_json(path: str, data: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.error("cannot write %s: %s", path, exc)


# ---------------------------------------------------------------------------
# CLI (the only printing/exiting layer)
# ---------------------------------------------------------------------------

def main(argv) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if len(argv) < 2:
        print("Usage: py -3 run_pipeline.py <config.json> [input.dxf] "
              "[output_dir]")
        return 2
    config_path = argv[1]
    dxf_path = argv[2] if len(argv) > 2 else None
    out_dir = argv[3] if len(argv) > 3 else None
    result = run(config_path, dxf_path, out_dir)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
