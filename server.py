"""Prompt Canvas - local server for Codex image generation workflow.

Persistence: SQLite at .cowart.db (schema in prompt_canvas_schema.sql).
The .cowart.db filename is kept for backward compatibility with existing data.
A "canvas" is the unit of work (one per Codex thread, ideally). Every shape
and annotation is scoped to a canvas. The "imported" canvas is the boot
default so the very first request always lands somewhere.

Endpoints
- GET  /                            canvas HTML (default canvas)
- GET  /?canvas=<id>                canvas HTML for canvas <id>
- GET  /canvas/<file>               static UI assets
- GET  /assets/<file>               static assets
- GET  /api/health                  health check
- GET  /api/state?canvas=<id>       full snapshot (shapes + annotations) for canvas
- POST /api/canvas                  create a new canvas; returns {id, name}
- GET  /api/canvas                  list all canvases
- POST /api/sync                    browser pushes tldraw snapshot (scoped to canvas)
- POST /api/commands                Codex issues a command (scoped to canvas)
- GET  /generated/<path:rel>        legacy redirect to /page-assets/<path:rel>
- GET  /page-assets/<page>/<file>   serve project-local page assets
- GET  /api/events                  SSE stream
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory

from imagegen import generate as imagegen_generate


def _load_env_file():
    """Load .env if present, otherwise .env.example. Existing env vars win."""
    root = Path(__file__).parent
    for name in (".env", ".env.example"):
        path = root / name
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    for raw in f:
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key and key not in os.environ:
                            os.environ[key] = value
            except OSError:
                pass
            break


_load_env_file()

ROOT = Path(__file__).parent
DB_PATH = ROOT / ".cowart.db"
SCHEMA_PATH = ROOT / "prompt_canvas_schema.sql"

# Project-local canvas directory (Codex plugin mode).
PROJECT_DIR = Path(os.environ.get("PROMPT_CANVAS_PROJECT_DIR") or ROOT)
CANVAS_DIR = Path(os.environ.get("PROMPT_CANVAS_CANVAS_DIR") or (PROJECT_DIR / "canvas"))
PAGE_ASSETS_BASE = CANVAS_DIR / "pages"

app = Flask(__name__, static_folder=None)


def _page_dir(page_id: str) -> Path:
    return PAGE_ASSETS_BASE / page_id


def _page_assets_dir(page_id: str) -> Path:
    d = _page_dir(page_id) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_page_snapshot(page_id: str, data: dict):
    """Persist the latest tldraw snapshot to the project-local page directory."""
    page_dir = _page_dir(page_id)
    page_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = page_dir / "prompt-canvas.json"
    tmp = snapshot_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(snapshot_path)


def _copy_to_page_assets(page_id: str, src: Path, filename: str | None = None) -> Path | None:
    """Copy a generated image into the page-local assets folder. Returns the copied path."""
    if not src or not src.exists():
        return None
    assets_dir = _page_assets_dir(page_id)
    name = filename or src.name
    dst = assets_dir / name
    try:
        import shutil
        shutil.copy2(src, dst)
        return dst
    except Exception as e:
        print(f"[copy_to_page_assets] failed: {e}")
        return None

# Thread-local SQLite connection (flask threaded=True needs per-thread)
_local = threading.local()
_lock = threading.RLock()

# In-memory caches that don't need persistence
_tldraw_cache: dict = {}        # canvas_id -> last tldraw snapshot
_subscribers: list = []         # SSE subscribers


# -------------------------------------------------------------
# DB layer
# -------------------------------------------------------------
def _conn():
    """Per-thread SQLite connection. WAL mode lets readers not block writers."""
    if not hasattr(_local, "con") or _local.con is None:
        con = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=NORMAL")
        _local.con = con
    return _local.con


def _ensure_schema():
    if not SCHEMA_PATH.exists():
        raise RuntimeError(f"schema missing: {SCHEMA_PATH}")
    with open(SCHEMA_PATH) as f:
        _conn().executescript(f.read())


def _now():
    return datetime.utcnow().isoformat() + "Z"


def _row_to_image(row):
    d = dict(row)
    # image_meta is stored as JSON blob
    try:
        d["image_meta"] = json.loads(d.pop("image_meta") or "{}")
    except Exception:
        d["image_meta"] = {}
    return d


def _row_to_annotation(row):
    return dict(row)


def _canvas_get_or_create(canvas_id: str, name: str = None, source: str = None) -> str:
    """Return canvas_id (creates if missing). Default: 'imported'."""
    canvas_id = canvas_id or "imported"
    con = _conn()
    row = con.execute("SELECT id FROM canvases WHERE id=?", (canvas_id,)).fetchone()
    if not row:
        nm = name or canvas_id
        con.execute(
            "INSERT INTO canvases (id, name, source, created_at, updated_at) VALUES (?,?,?,?,?)",
            (canvas_id, nm, source, _now(), _now()),
        )
    return canvas_id


def _event_log(canvas_id: str, action: str, args: dict):
    con = _conn()
    con.execute(
        "INSERT INTO events (canvas_id, action, args_json, ts) VALUES (?,?,?,?)",
        (canvas_id, action, json.dumps(args, ensure_ascii=False), time.time()),
    )
    if canvas_id:
        con.execute("UPDATE canvases SET updated_at=? WHERE id=?", (_now(), canvas_id))


def _next_version(canvas_id: str) -> str:
    con = _conn()
    row = con.execute(
        "SELECT MAX(CAST(SUBSTR(version, 2) AS INTEGER)) FROM images WHERE canvas_id=? AND version LIKE 'v%'",
        (canvas_id,),
    ).fetchone()
    n = (row[0] or 0) + 1
    return f"v{n}"


def _natural_size(image_url: str):
    """Read source PNG size + aspect ratio. Returns (w, h, ar) or (None, None, None)."""
    if not image_url:
        return None, None, None
    try:
        from PIL import Image as _PIL
        if image_url.startswith("/page-assets/"):
            rel = image_url[len("/page-assets/"):]
            p = PAGE_ASSETS_BASE / rel
        else:
            p = ROOT / image_url.lstrip("/")
        if not p.exists():
            return None, None, None
        with _PIL.open(p) as im:
            w, h = im.size
        return w, h, (round(w / h, 4) if h else None)
    except Exception as e:
        print(f"[natural_size] {image_url}: {e}")
        return None, None, None


def _auto_size(prev_url, image_url, current_w, current_h, force=False):
    """Compute a fitted w/h that preserves the image's aspect ratio.
    Returns (w, h) — or current_w, current_h if we should not change."""
    if not image_url:
        return current_w, current_h
    if prev_url and not force:
        return current_w, current_h
    iw, ih, _ = _natural_size(image_url)
    if not iw or not ih:
        return current_w, current_h
    ratio = iw / ih
    MAX = 720
    if ratio >= 1:
        return float(MAX), float(round(MAX / ratio, 1))
    return float(round(MAX * ratio, 1)), float(MAX)


# -------------------------------------------------------------
# Annotation normalization
# -------------------------------------------------------------
def _quadrant_description(rel_x: float, rel_y: float) -> str:
    """Return a human-readable region from normalized coordinates (0-1)."""
    rel_x = max(0.0, min(1.0, rel_x))
    rel_y = max(0.0, min(1.0, rel_y))
    if rel_x < 0.33:
        h = "左"
    elif rel_x < 0.67:
        h = "中"
    else:
        h = "右"
    if rel_y < 0.33:
        v = "上"
    elif rel_y < 0.67:
        v = "中"
    else:
        v = "下"
    if h == "中" and v == "中":
        return "中央"
    if h == "中":
        return f"{v}部中央"
    if v == "中":
        return f"{'左侧' if h == '左' else '右侧'}中部"
    return f"{v}{h}角"


def _normalize_annotation(row, target, tldraw=None):
    """Convert a raw annotation row into structured spatial+semantic form.

    `row` and `target` are sqlite3.Row or dict from the annotations/images tables.
    `tldraw` is the cached tldraw snapshot (optional) used to enrich arrows/draw.
    """
    row = dict(row)
    target = dict(target)
    tx, ty, tw, th = target.get("x", 0), target.get("y", 0), target.get("w", 1), target.get("h", 1)
    if tw == 0:
        tw = 1
    if th == 0:
        th = 1

    kind = row.get("kind") or "annotation"
    text = (row.get("text") or "").strip()
    color = row.get("color") or "#ef4444"

    x = row.get("x") or 0
    y = row.get("y") or 0
    w = row.get("w") or 0
    h = row.get("h") or 0

    # Enrich from the tldraw cache when available (arrow start/end, draw segments).
    start = None
    end = None
    area_pct = None
    shape_cache = None
    if tldraw:
        for shape in tldraw.get("shapes", []):
            if shape.get("id") == row.get("id"):
                shape_cache = shape
                break
    if shape_cache:
        props = shape_cache.get("props", {})
        if kind == "arrow":
            start = props.get("start")
            end = props.get("end")
        elif kind == "draw":
            segments = props.get("segments", [])
            pts = [p for seg in segments for p in seg.get("points", [])]
            if pts:
                xs = [p.get("x", 0) + x for p in pts]
                ys = [p.get("y", 0) + y for p in pts]
                min_x, max_x = min(xs), max(xs)
                min_y, max_y = min(ys), max(ys)
                x, y, w, h = min_x, min_y, max_x - min_x, max_y - min_y
        if not text:
            text = (props.get("text") or "").strip()

    rel_box = {
        "x": round((x - tx) / tw, 3),
        "y": round((y - ty) / th, 3),
        "w": round(w / tw, 3),
        "h": round(h / th, 3),
    }

    # Primary point for region description.
    if kind == "arrow" and start and end:
        sx, sy = start.get("x", x), start.get("y", y)
        ex, ey = end.get("x", x + w), end.get("y", y + h)
        arrow_start_rel = {"x": round((sx - tx) / tw, 3), "y": round((sy - ty) / th, 3)}
        arrow_end_rel = {"x": round((ex - tx) / tw, 3), "y": round((ey - ty) / th, 3)}
        rel_px, rel_py = arrow_end_rel["x"], arrow_end_rel["y"]
    else:
        arrow_start_rel = None
        arrow_end_rel = None
        rel_px = rel_box["x"] + rel_box["w"] / 2
        rel_py = rel_box["y"] + rel_box["h"] / 2

    region = _quadrant_description(rel_px, rel_py)

    if kind in ("draw", "geo") and w and h:
        area_pct = round((w * h) / (tw * th) * 100, 1)

    intent_map = {
        "arrow": "箭头指向，要求修改此处",
        "draw": "手绘圈出，要求修改此区域",
        "text": "文字备注，说明修改需求",
        "geo": "框选区域，要求修改此范围",
    }

    structured = {
        "id": row.get("id"),
        "kind": kind,
        "text": text,
        "color": color,
        "abs_box": {"x": x, "y": y, "w": w, "h": h},
        "rel_box": rel_box,
        "region": region,
        "intent": intent_map.get(kind, "修改批注"),
    }
    if arrow_start_rel:
        structured["arrow_start_rel"] = arrow_start_rel
        structured["arrow_end_rel"] = arrow_end_rel
    if area_pct is not None:
        structured["area_percent"] = area_pct
    return structured


def _annotations_to_markdown(structured, target):
    """Render a list of structured annotations as LLM-friendly markdown."""
    target = dict(target) if target else {}
    lines = []
    lines.append(f"目标图片: {target.get('version', 'v?')} ({target.get('label', 'AI Image')})")
    lines.append(f"目标尺寸: {round(target.get('w', 0))}×{round(target.get('h', 0))}")
    lines.append(f"批注共 {len(structured)} 条:")
    lines.append("")
    kind_label = {"arrow": "箭头", "draw": "画笔圈", "text": "文字", "geo": "几何框"}
    for i, a in enumerate(structured, 1):
        label = kind_label.get(a["kind"], a["kind"])
        region = a["region"]
        text = a["text"] or "（无文字）"
        if a["kind"] == "arrow" and "arrow_start_rel" in a:
            s = a["arrow_start_rel"]
            e = a["arrow_end_rel"]
            lines.append(
                f"{i}. [{label}] 从 ({s['x']*100:.0f}%, {s['y']*100:.0f}%) "
                f"指向 ({e['x']*100:.0f}%, {e['y']*100:.0f}%)，落点 {region}: \"{text}\""
            )
        elif a["kind"] in ("draw", "geo") and "area_percent" in a:
            r = a["rel_box"]
            lines.append(
                f"{i}. [{label}] 覆盖约 {a['area_percent']}% 区域，"
                f"位于 {region} (x≈{r['x']*100:.0f}%, y≈{r['y']*100:.0f}%): \"{text}\""
            )
        else:
            r = a["rel_box"]
            lines.append(
                f"{i}. [{label}] 位于 {region} "
                f"(x≈{r['x']*100:.0f}%, y≈{r['y']*100:.0f}%): \"{text}\""
            )
    lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------
# Boot
# -------------------------------------------------------------
_ensure_schema()
# Ensure default canvas exists
with _lock:
    _canvas_get_or_create("imported", name="Imported", source="imported-from-json")


# -------------------------------------------------------------
# Broadcast / SSE
# -------------------------------------------------------------
def _broadcast(event, data):
    msg = json.dumps({"event": event, "data": data, "ts": time.time()})
    with _lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _subscribers.remove(q)
            except ValueError:
                pass


# -------------------------------------------------------------
# Resolve canvas from request (query param or args)
# -------------------------------------------------------------
def _resolve_canvas():
    """Read canvas_id from query (?canvas=) or JSON body. Required - no silent fallback.

    A "session" (one Codex thread, one fresh browser tab on the root URL) maps
    1:1 to a canvas. Callers that don't have a canvas_id yet should hit the
    root `/` route, which mints a new canvas and redirects.
    """
    cid = request.args.get("canvas")
    if not cid and request.is_json:
        try:
            body = request.get_json(silent=True) or {}
            cid = body.get("canvas_id") or (body.get("args") or {}).get("canvas_id")
        except Exception:
            pass
    if not cid:
        abort(400, description="canvas_id required (open / to mint one)")
    return _canvas_get_or_create(cid)


# -------------------------------------------------------------
# Routes
# -------------------------------------------------------------
# Static assets: serve the Vite build output (canvas/dist) when available.
# In development, run Vite dev server separately and proxy /api through it.
# Fallback to the raw canvas/ directory for unbuilt files like tldraw.css.
# -------------------------------------------------------------
DIST_DIR = ROOT / "canvas" / "dist"
CANVAS_SRC_DIR = ROOT / "canvas"


def _send_dist_or_canvas(fname):
    """Prefer built dist; fall back to source canvas dir."""
    if (DIST_DIR / fname).exists():
        return send_from_directory(DIST_DIR, fname)
    return send_from_directory(CANVAS_SRC_DIR, fname)


@app.get("/")
def index():
    # If no canvas in URL, mint a fresh one and redirect so every "session"
    # (fresh tab / new Codex thread) gets its own isolated canvas.
    cid = request.args.get("canvas")
    if not cid:
        new_id = uuid.uuid4().hex[:12]
        _canvas_get_or_create(new_id, name="Untitled")
        return redirect(f"/?canvas={new_id}", code=302)
    if (DIST_DIR / "index.html").exists():
        return send_from_directory(DIST_DIR, "index.html")
    return send_from_directory(CANVAS_DIR, "index.html")


@app.get("/canvas/<path:fname>")
def canvas_static(fname):
    return _send_dist_or_canvas(fname)


@app.get("/src/<path:fname>")
def src_static(fname):
    """Vite dev server uses /src/...; built output inlines these."""
    return _send_dist_or_canvas(fname)


@app.get("/assets/<path:fname>")
def assets_static(fname):
    return send_from_directory(ROOT / "assets", fname)


@app.get("/api/health")
def health():
    con = _conn()
    canvases = con.execute("SELECT COUNT(*) FROM canvases").fetchone()[0]
    images = con.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    anns = con.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
    return jsonify({"ok": True, "canvases": canvases, "images": images, "annotations": anns})


@app.get("/api/canvas")
def list_canvases():
    con = _conn()
    rows = con.execute(
        "SELECT id, name, summary, source, created_at, updated_at, "
        "  (SELECT COUNT(*) FROM images WHERE canvas_id=canvases.id) AS image_count, "
        "  (SELECT COUNT(*) FROM annotations WHERE canvas_id=canvases.id) AS annotation_count "
        "FROM canvases ORDER BY updated_at DESC"
    ).fetchall()
    return jsonify({"ok": True, "canvases": [dict(r) for r in rows]})


@app.post("/api/canvas")
def create_canvas():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip() or f"Canvas {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    source = body.get("source") or "manual"
    cid = body.get("id") or f"cv_{uuid.uuid4().hex[:10]}"
    with _lock:
        cid = _canvas_get_or_create(cid, name=name, source=source)
        _event_log(cid, "create_canvas", {"name": name, "source": source})
    row = _conn().execute("SELECT * FROM canvases WHERE id=?", (cid,)).fetchone()
    return jsonify({"ok": True, "canvas": dict(row)})


@app.get("/api/state")
def get_state():
    cid = _resolve_canvas()
    con = _conn()
    img_rows = con.execute("SELECT * FROM images WHERE canvas_id=? ORDER BY created_at", (cid,)).fetchall()
    ann_rows = con.execute("SELECT * FROM annotations WHERE canvas_id=? ORDER BY created_at", (cid,)).fetchall()
    images = {r["id"]: _row_to_image(r) for r in img_rows}
    annotations = [_row_to_annotation(r) for r in ann_rows]
    tldraw = _tldraw_cache.get(cid, {"shapes": [], "pageId": None})
    return jsonify({
        "canvas_id": cid,
        "shapes": images,
        "annotations": annotations,
        "tldraw": tldraw,
    })


@app.get("/api/annotations")
def get_annotations():
    """Return structured annotations for a target AI image shape."""
    cid = _resolve_canvas()
    sid = request.args.get("shape_id")
    if not sid:
        abort(400, description="shape_id required")
    con = _conn()
    target = con.execute("SELECT * FROM images WHERE id=? AND canvas_id=?", (sid, cid)).fetchone()
    if not target:
        return jsonify({"ok": False, "error": "shape not found"}), 404
    rows = con.execute(
        "SELECT * FROM annotations WHERE canvas_id=? AND target_image_id=? ORDER BY created_at",
        (cid, sid),
    ).fetchall()
    tldraw = _tldraw_cache.get(cid, {"shapes": []})
    structured = [_normalize_annotation(r, target, tldraw) for r in rows]
    markdown = _annotations_to_markdown(structured, target)
    return jsonify({
        "ok": True,
        "canvas_id": cid,
        "shape_id": sid,
        "annotations": [dict(r) for r in rows],
        "structured": structured,
        "markdown": markdown,
        "count": len(rows),
    })


@app.post("/api/reset")
def reset():
    cid = _resolve_canvas()
    with _lock:
        con = _conn()
        con.execute("DELETE FROM annotations WHERE canvas_id=?", (cid,))
        con.execute("DELETE FROM images WHERE canvas_id=?", (cid,))
        _tldraw_cache.pop(cid, None)
        _event_log(cid, "reset", {})
    _broadcast("reset", {"canvas_id": cid})
    return jsonify({"ok": True, "canvas_id": cid})


@app.post("/api/sync")
def sync():
    cid = _resolve_canvas()
    payload = request.get_json(force=True, silent=True) or {}
    tldraw = payload.get("tldraw", {})
    _tldraw_cache[cid] = tldraw
    with _lock:
        con = _conn()
        # ai-image shapes: upsert
        for shape in tldraw.get("shapes", []):
            if shape.get("type") != "ai-image":
                continue
            raw_id = shape.get("id", "")
            sid = raw_id.split(":", 1)[-1] if raw_id.startswith("shape:") else raw_id
            if not sid:
                continue
            meta = shape.get("meta", {}) or {}
            props = shape.get("props", {}) or {}
            w = float(shape.get("w", props.get("w", 300)) or 300)
            h = float(shape.get("h", props.get("h", 400)) or 400)
            x = float(shape.get("x", 0) or 0)
            y = float(shape.get("y", 0) or 0)
            version = meta.get("version") or props.get("version") or "v?"
            label = meta.get("label") or props.get("label")
            image_url = meta.get("image_url") or props.get("imageUrl")
            prompt = meta.get("prompt") or props.get("prompt")
            source_id = meta.get("source_id") or props.get("sourceId")
            image_meta = meta.get("image_meta") or props.get("imageMeta") or {}
            natural_w, natural_h, aspect_ratio = _natural_size(image_url) if image_url else (None, None, None)
            con.execute(
                """INSERT INTO images
                (id, canvas_id, version, label, x, y, w, h, image_url,
                 natural_w, natural_h, aspect_ratio, prompt, edit_of, model, provider,
                 source_id, image_meta, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    version=excluded.version, label=excluded.label,
                    x=excluded.x, y=excluded.y, w=excluded.w, h=excluded.h,
                    image_url=COALESCE(NULLIF(excluded.image_url, ''), images.image_url),
                    natural_w=COALESCE(NULLIF(excluded.natural_w, ''), NULLIF(excluded.natural_w, 0), images.natural_w),
                    natural_h=COALESCE(NULLIF(excluded.natural_h, ''), NULLIF(excluded.natural_h, 0), images.natural_h),
                    aspect_ratio=COALESCE(NULLIF(excluded.aspect_ratio, ''), NULLIF(excluded.aspect_ratio, 0), images.aspect_ratio),
                    prompt=COALESCE(NULLIF(excluded.prompt, ''), images.prompt),
                    source_id=COALESCE(NULLIF(excluded.source_id, ''), images.source_id),
                    image_meta=excluded.image_meta""",
                (sid, cid, version, label, x, y, w, h, image_url,
                 natural_w, natural_h, aspect_ratio, prompt, None,
                 image_meta.get("model"), image_meta.get("provider"),
                 source_id, json.dumps(image_meta, ensure_ascii=False), _now()),
            )
        # annotations: full-replace (any annotation not in this payload is removed)
        ann_ids_in_payload = set()
        for shape in tldraw.get("shapes", []):
            meta = shape.get("meta", {}) or {}
            if meta.get("role") != "annotation" or not meta.get("target"):
                continue
            raw_id = shape.get("id", "")
            if raw_id in ann_ids_in_payload:
                continue
            ann_ids_in_payload.add(raw_id)
            target = meta["target"]
            target_id = target.split(":", 1)[-1] if target.startswith("shape:") else target
            props = shape.get("props", {}) or {}
            con.execute(
                """INSERT INTO annotations
                (id, canvas_id, target_image_id, kind, x, y, w, h, text, color, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind, x=excluded.x, y=excluded.y, w=excluded.w, h=excluded.h,
                    text=excluded.text, color=excluded.color""",
                (raw_id, cid, target_id, shape.get("type"),
                 shape.get("x"), shape.get("y"),
                 props.get("w"), props.get("h"),
                 meta.get("text", ""), meta.get("color", "#ef4444"), _now()),
            )
        # remove annotations not in payload
        if ann_ids_in_payload:
            placeholders = ",".join("?" * len(ann_ids_in_payload))
            con.execute(
                f"DELETE FROM annotations WHERE canvas_id=? AND id NOT IN ({placeholders})",
                [cid, *ann_ids_in_payload],
            )
        else:
            con.execute("DELETE FROM annotations WHERE canvas_id=?", (cid,))

    # Project-local persistence: write a human-readable page snapshot.
    _write_page_snapshot(cid, {"canvas_id": cid, "tldraw": tldraw})
    return jsonify({"ok": True, "canvas_id": cid})


@app.post("/api/commands")
def commands():
    cid = _resolve_canvas()
    payload = request.get_json(force=True, silent=True) or {}
    action = payload.get("action")
    args = payload.get("args", {}) or {}
    con = _conn()

    if action == "create_ai_image_holder":
        version = args.get("version") or _next_version(cid)
        sid = args.get("shape_id") or f"ai_{uuid.uuid4().hex[:8]}"
        x = float(args.get("x") if args.get("x") is not None else 200)
        y = float(args.get("y") if args.get("y") is not None else 200)
        w = float(args.get("w") if args.get("w") is not None else 360)
        h = float(args.get("h") if args.get("h") is not None else 480)
        label = args.get("label", "AI Image")
        meta = args.get("meta", {})
        with _lock:
            con.execute(
                """INSERT OR REPLACE INTO images
                (id, canvas_id, version, label, x, y, w, h, image_url, natural_w, natural_h,
                 aspect_ratio, prompt, source_id, image_meta, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, cid, version, label, x, y, w, h, None, None, None, None,
                 meta.get("prompt"), meta.get("source_id"),
                 json.dumps({}, ensure_ascii=False), _now()),
            )
            _event_log(cid, action, {"shape_id": sid, "version": version, "label": label, **args})
        row = con.execute("SELECT * FROM images WHERE id=?", (sid,)).fetchone()
        result = {"ok": True, "shape_id": sid, "version": version, "shape": _row_to_image(row), "canvas_id": cid}
        _broadcast("create_ai_image_holder", result)

    elif action == "fill_ai_image_holder":
        sid = args.get("shape_id")
        image_url = args.get("image_url")
        image_meta = args.get("image_meta", {}) or {}
        row = con.execute("SELECT * FROM images WHERE id=? AND canvas_id=?", (sid, cid)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": f"unknown shape: {sid}"}), 400
        prev_url = row["image_url"]
        natural_w, natural_h, aspect_ratio = _natural_size(image_url) if image_url else (None, None, None)
        # auto-size
        new_w, new_h = row["w"], row["h"]
        if image_url and (not prev_url or args.get("auto_size")):
            new_w, new_h = _auto_size(prev_url, image_url, row["w"], row["h"], force=bool(args.get("auto_size")))
        version = args.get("version") or row["version"]
        prompt = args.get("prompt") or row["prompt"]
        # Normalize legacy /generated/ URLs to /page-assets/.
        page_url = image_url.replace("/generated/", "/page-assets/", 1) if image_url and image_url.startswith("/generated/") else image_url
        with _lock:
            con.execute(
                """UPDATE images SET
                    image_url=?, image_meta=?, natural_w=?, natural_h=?, aspect_ratio=?,
                    w=?, h=?, version=COALESCE(?, version), prompt=COALESCE(?, prompt),
                    model=COALESCE(?, model), provider=COALESCE(?, provider)
                WHERE id=? AND canvas_id=?""",
                (page_url, json.dumps(image_meta, ensure_ascii=False),
                 natural_w, natural_h, aspect_ratio,
                 new_w, new_h, version, prompt,
                 image_meta.get("model"), image_meta.get("provider"),
                 sid, cid),
            )
            _event_log(cid, action, {"shape_id": sid, "image_url": page_url, "version": version})
        result = {"ok": True, "shape_id": sid, "image_url": page_url,
                  "version": version, "w": new_w, "h": new_h,
                  "natural_w": natural_w, "natural_h": natural_h, "aspect_ratio": aspect_ratio,
                  "canvas_id": cid}
        _broadcast("fill_ai_image_holder", result)

    elif action == "insert_image":
        """Create a new ai-image shape and fill it with a bitmap (standalone placement)."""
        sid = args.get("shape_id") or f"ai_{uuid.uuid4().hex[:8]}"
        x = float(args.get("x") if args.get("x") is not None else 200)
        y = float(args.get("y") if args.get("y") is not None else 200)
        w = float(args.get("w") if args.get("w") is not None else 360)
        h = float(args.get("h") if args.get("h") is not None else 480)
        label = args.get("label", "AI Image")
        image_url = args.get("image_url") or args.get("imagePath", "")
        file_name = args.get("fileName")
        image_meta = args.get("image_meta", {}) or {}
        version = args.get("version") or _next_version(cid)
        prompt = args.get("prompt") or image_meta.get("prompt", "")

        # If a local file path is provided, copy it into page assets and derive a URL.
        page_url = image_url
        src_path = None
        if image_url and not image_url.startswith(("http://", "https://", "/")):
            src_path = Path(image_url)
            if src_path.exists():
                copied = _copy_to_page_assets(cid, src_path, file_name or src_path.name)
                if copied:
                    page_url = f"/page-assets/{cid}/{copied.name}"
        elif image_url and image_url.startswith("/generated/"):
            page_url = image_url.replace("/generated/", "/page-assets/", 1)

        natural_w, natural_h, aspect_ratio = _natural_size(page_url) if page_url else (None, None, None)
        if page_url and args.get("auto_size"):
            w, h = _auto_size(None, page_url, w, h, force=True)

        with _lock:
            con.execute(
                """INSERT OR REPLACE INTO images
                (id, canvas_id, version, label, x, y, w, h, image_url, natural_w, natural_h,
                 aspect_ratio, prompt, source_id, image_meta, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, cid, version, label, x, y, w, h, page_url, natural_w, natural_h,
                 aspect_ratio, prompt, args.get("source_id"),
                 json.dumps(image_meta, ensure_ascii=False), _now()),
            )
            _event_log(cid, action, {"shape_id": sid, "image_url": page_url, "version": version})
        row = con.execute("SELECT * FROM images WHERE id=?", (sid,)).fetchone()
        result = {
            "ok": True, "shape_id": sid, "version": version, "label": label,
            "image_url": page_url, "x": x, "y": y, "w": w, "h": h,
            "natural_w": natural_w, "natural_h": natural_h, "aspect_ratio": aspect_ratio,
            "shape": _row_to_image(row), "canvas_id": cid,
        }
        _broadcast("insert_image", result)

    elif action == "read_annotations":
        sid = args.get("shape_id")
        target = con.execute("SELECT * FROM images WHERE id=? AND canvas_id=?", (sid, cid)).fetchone()
        rows = con.execute(
            "SELECT * FROM annotations WHERE canvas_id=? AND target_image_id=? ORDER BY created_at",
            (cid, sid),
        ).fetchall()
        tldraw = _tldraw_cache.get(cid, {"shapes": []})
        structured = [_normalize_annotation(r, target, tldraw) for r in rows] if target else []
        markdown = _annotations_to_markdown(structured, target) if target else ""
        result = {
            "ok": True, "canvas_id": cid, "shape_id": sid,
            "annotations": [dict(r) for r in rows],
            "structured": structured,
            "markdown": markdown,
            "count": len(rows),
        }
        _broadcast("read_annotations", result)

    elif action == "generate_image":
        prompt = args.get("prompt", "AI image")
        style = args.get("style", "v1")
        width = int(args.get("width", 720))
        height = int(args.get("height", 960))
        refs = args.get("refs", [])
        try:
            buf = imagegen_generate(prompt=prompt, style=style, width=width, height=height, refs=refs)
        except Exception as e:
            return jsonify({"ok": False, "error": f"imagegen: {e}"}), 500
        # Save directly into project-local page assets
        assets_dir = _page_assets_dir(cid)
        ts = int(time.time() * 1000)
        stem = f"gen-{ts}-{uuid.uuid4().hex[:6]}"
        fname = f"{stem}.jpg"
        fpath = assets_dir / fname
        fpath.write_bytes(buf)
        url = f"/page-assets/{cid}/{fname}"
        meta = {"prompt": prompt, "style": style, "width": width, "height": height, "size_bytes": len(buf)}
        result = {"ok": True, "image_url": url, "image_meta": meta, "canvas_id": cid}
        _broadcast("image_generated", result)

    elif action == "delete_shape":
        sid = args.get("shape_id")
        with _lock:
            con.execute("DELETE FROM images WHERE id=? AND canvas_id=?", (sid, cid))
            _event_log(cid, action, {"shape_id": sid})
        result = {"ok": True, "shape_id": sid, "canvas_id": cid}
        _broadcast("delete_shape", result)

    elif action == "edit_image_from_annotations":
        # Codex-driven edit: read annotations, prompt model, return the desired new image spec.
        # The actual generation is up to Codex (it may use linyuebanzi-image-gen directly).
        # We just record the intent and return the current annotations.
        sid = args.get("shape_id")
        from_version = args.get("from_version")
        to_version = args.get("to_version")
        annotation_text = args.get("annotation", "")
        target = con.execute("SELECT * FROM images WHERE id=? AND canvas_id=?", (sid, cid)).fetchone()
        rows = con.execute(
            "SELECT * FROM annotations WHERE canvas_id=? AND target_image_id=? ORDER BY created_at",
            (cid, sid),
        ).fetchall()
        tldraw = _tldraw_cache.get(cid, {"shapes": []})
        structured = [_normalize_annotation(r, target, tldraw) for r in rows] if target else []
        markdown = _annotations_to_markdown(structured, target) if target else ""
        with _lock:
            _event_log(cid, action, {
                "shape_id": sid, "from_version": from_version,
                "to_version": to_version, "annotation": annotation_text,
                "annotation_count": len(rows),
                "markdown": markdown,
            })
        result = {
            "ok": True, "canvas_id": cid, "shape_id": sid,
            "annotations": [dict(r) for r in rows],
            "structured": structured,
            "markdown": markdown,
            "count": len(rows),
        }

    elif action == "reset":
        with _lock:
            con.execute("DELETE FROM annotations WHERE canvas_id=?", (cid,))
            con.execute("DELETE FROM images WHERE canvas_id=?", (cid,))
            _tldraw_cache.pop(cid, None)
            _event_log(cid, "reset", {})
        result = {"ok": True, "canvas_id": cid}
        _broadcast("reset", {"canvas_id": cid})

    else:
        return jsonify({"ok": False, "error": f"unknown action: {action}"}), 400

    return jsonify(result)


@app.post("/api/submit")
def submit_pending():
    """Save a structured canvas-submit request to <canvas>/_pending/.

    Payload: { canvas_id, target, references, notes, prompt, format }
    Writes: _pending/submit-{ts}.md (markdown for Codex chat) and
            _pending/submit-{ts}.json (full structured payload for automation).
    Returns: { ok, md_path, json_path, ts }
    """
    cid = _resolve_canvas()
    payload = request.get_json(force=True, silent=True) or {}
    pending_dir = _page_dir(cid) / "_pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    md_path = pending_dir / f"submit-{ts}.md"
    json_path = pending_dir / f"submit-{ts}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(payload.get("md", ""), encoding="utf-8")
    with _lock:
        _event_log(cid, "submit", {
            "md_path": str(md_path.relative_to(ROOT)),
            "json_path": str(json_path.relative_to(ROOT)),
            "target": payload.get("target"),
            "ref_count": len(payload.get("references") or []),
        })
    _broadcast("submit", {"canvas_id": cid, "md_path": str(md_path.relative_to(ROOT))})
    return jsonify({
        "ok": True,
        "canvas_id": cid,
        "md_path": f"/page-assets/{cid}/_pending/submit-{ts}.md",
        "json_path": f"/page-assets/{cid}/_pending/submit-{ts}.json",
        "ts": ts,
    })


@app.get("/api/pending")
def list_pending():
    """List pending submit requests for a canvas. For Codex to poll."""
    cid = _resolve_canvas()
    print(f"[poll] Codex polling /api/pending for canvas={cid}")
    pending_dir = _page_dir(cid) / "_pending"
    if not pending_dir.exists():
        return jsonify({"canvas_id": cid, "items": []})
    items = []
    for p in sorted(pending_dir.glob("submit-*.md"), reverse=True):
        meta = p.with_suffix(".json")
        items.append({
            "ts": p.stem.replace("submit-", ""),
            "md_path": f"/page-assets/{cid}/_pending/{p.name}",
            "json_path": f"/page-assets/{cid}/_pending/{meta.name}" if meta.exists() else None,
            "size": p.stat().st_size,
        })
    return jsonify({"canvas_id": cid, "items": items})


@app.post("/api/pending/<ts>/done")
def mark_submit_done(ts):
    """Mark a pending submit request as processed by moving it to _pending/_done/."""
    cid = _resolve_canvas()
    pending_dir = _page_dir(cid) / "_pending"
    done_dir = pending_dir / "_done"
    done_dir.mkdir(parents=True, exist_ok=True)
    md_src = pending_dir / f"submit-{ts}.md"
    json_src = pending_dir / f"submit-{ts}.json"
    moved = []
    for src in (md_src, json_src):
        if src.exists():
            dst = done_dir / src.name
            try:
                src.rename(dst)
                moved.append(str(dst.relative_to(ROOT)))
            except Exception as e:
                return jsonify({"ok": False, "error": f"move failed: {e}"}), 500
    with _lock:
        _event_log(cid, "submit_done", {"ts": ts, "moved": moved})
    return jsonify({"ok": True, "canvas_id": cid, "ts": ts, "moved": moved})


@app.get("/generated/<path:rel>")
def generated(rel):
    """Legacy redirect to /page-assets/<rel>."""
    # Prevent path traversal
    safe = (PAGE_ASSETS_BASE / rel).resolve()
    if not str(safe).startswith(str(PAGE_ASSETS_BASE.resolve())):
        return jsonify({"ok": False, "error": "bad path"}), 400
    return redirect(f"/page-assets/{rel}", code=308)


@app.get("/page-assets/<page_id>/_pending/<path:fname>")
def page_pending_assets(page_id, fname):
    """Serve project-local pending submit files."""
    base = (_page_dir(page_id) / "_pending").resolve()
    safe = (base / fname).resolve()
    if not str(safe).startswith(str(base)):
        return jsonify({"ok": False, "error": "bad path"}), 400
    if not safe.exists():
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_from_directory(base, fname)


@app.get("/page-assets/<page_id>/<path:fname>")
def page_assets(page_id, fname):
    """Serve project-local page assets."""
    base = _page_assets_dir(page_id).resolve()
    safe = (base / fname).resolve()
    if not str(safe).startswith(str(base)):
        return jsonify({"ok": False, "error": "bad path"}), 400
    if not safe.exists():
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_from_directory(base, fname)


@app.get("/api/events")
def events():
    cid = request.args.get("canvas") or "imported"
    def stream():
        q = queue.Queue()
        with _lock:
            _subscribers.append(q)
        try:
            hello = {"event": "hello", "data": {"canvas_id": cid}, "ts": time.time()}
            yield f"data: {json.dumps(hello)}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {msg}\n\n"
                except Exception:
                    yield ": ping\n\n"
        finally:
            with _lock:
                try:
                    _subscribers.remove(q)
                except ValueError:
                    pass

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    host = os.environ.get("PROMPT_CANVAS_HOST", "127.0.0.1")
    port = int(os.environ.get("PROMPT_CANVAS_PORT", "52846"))
    print(f"Prompt Canvas running on http://{host}:{port}")
    app.run(host=host, port=port, threaded=True, debug=False)
