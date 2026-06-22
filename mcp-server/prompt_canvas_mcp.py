"""MCP server for Prompt Canvas.

Speaks MCP (Model Context Protocol) over stdio using JSON-RPC 2.0, and proxies
each tool call to the local Prompt Canvas HTTP server (server.py).

Start the canvas server first, then register this script with Codex as an MCP
server:

    codex mcp add prompt-canvas -- python3 /path/to/mcp-server/prompt_canvas_mcp.py

Environment
-----------
PROMPT_CANVAS_BASE_URL  default http://127.0.0.1:52846
PROMPT_CANVAS_CANVAS_ID default canvas id when the tool call omits one
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any


def _load_env_file():
    """Load .env if present, otherwise .env.example. Existing env vars win."""
    root = Path(__file__).parent.parent
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

_HOST = os.environ.get("PROMPT_CANVAS_HOST", "127.0.0.1")
_PORT = os.environ.get("PROMPT_CANVAS_PORT", "52846")
BASE_URL = os.environ.get("PROMPT_CANVAS_BASE_URL", f"http://{_HOST}:{_PORT}").rstrip("/")
DEFAULT_CANVAS = os.environ.get("PROMPT_CANVAS_CANVAS_ID", "")
SERVER_NAME = "prompt-canvas"
SERVER_VERSION = "0.3.0"


# ---------- HTTP helper ----------
# urllib.request intermittently raises RemoteDisconnected against the Flask dev
# server on this machine, so we use http.client directly for robustness.
def _parse_base():
    parsed = urllib.parse.urlparse(BASE_URL)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _http(method: str, path: str, body: dict | None = None, *,
          query: dict | None = None) -> dict:
    host, port = _parse_base()
    qs = ""
    if query:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in query.items() if v is not None)
    full_path = path
    if qs:
        full_path += "?" + qs

    data = None
    headers = {"Accept": "application/json", "Host": f"{host}:{port}"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(data))

    try:
        print(f"[mcp] {method} {full_path}")
        conn = http.client.HTTPConnection(host, port, timeout=120)
        conn.request(method, full_path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8")
        conn.close()
        if resp.status >= 400:
            print(f"[mcp] {method} {full_path} -> HTTP {resp.status}")
            return {"error": f"http_{resp.status}", "detail": text[:500]}
        print(f"[mcp] {method} {full_path} -> OK")
        return json.loads(text)
    except (ConnectionRefusedError, http.client.HTTPException, OSError) as e:
        print(f"[mcp] {method} {full_path} -> unreachable: {e}")
        return {"error": "prompt_canvas_unreachable", "detail": str(e), "base_url": BASE_URL}
    except json.JSONDecodeError as e:
        print(f"[mcp] {method} {full_path} -> bad_json: {e}")
        return {"error": "bad_json", "detail": str(e)}


def _canvas_id(args: dict) -> str:
    return args.get("canvas_id") or DEFAULT_CANVAS or "imported"


# ---------- tool definitions ----------
TOOLS: list[dict] = [
    {
        "name": "prompt_canvas_open",
        "description": (
            "Return the URL of the local Prompt Canvas and a health check. "
            "Use this first to confirm the canvas server is running before any other tool. "
            "If the server is not running, tell the user to start it with `python3 server.py`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_health",
        "description": "Ping the Prompt Canvas server. Returns ok/version or an error if unreachable.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "prompt_canvas_create_canvas",
        "description": "Create a new canvas. Returns the new canvas id. Each Codex thread should ideally use its own canvas.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "human-readable name for the canvas"},
                "source": {"type": "string", "description": "origin label, e.g. 'codex-thread-abc'"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_list_canvases",
        "description": "List all existing canvases with metadata and counts.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "prompt_canvas_get_state",
        "description": "Get the full snapshot of a canvas: all shapes, annotations, and the latest tldraw snapshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string", "description": "canvas id; uses PROMPT_CANVAS_CANVAS_ID env var or 'imported' if omitted"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_create_ai_image_holder",
        "description": (
            "Create a new AI Image placeholder shape on the canvas. "
            "The user will see a framed box labeled with the version. "
            "After creation, fill it with prompt_canvas_fill_ai_image_holder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
                "x": {"type": "number", "description": "left edge in canvas coords"},
                "y": {"type": "number", "description": "top edge in canvas coords"},
                "w": {"type": "number", "description": "width"},
                "h": {"type": "number", "description": "height"},
                "label": {"type": "string", "description": "label shown on the placeholder"},
                "prompt": {"type": "string", "description": "initial prompt for traceability"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_fill_ai_image_holder",
        "description": (
            "Fill an AI Image placeholder with a generated image URL. "
            "Pass the local URL returned by the image generator (e.g. /page-assets/<canvas>/<file>.jpg)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
                "shape_id": {"type": "string", "description": "id of the ai-image shape to fill"},
                "image_url": {"type": "string", "description": "URL the canvas can render"},
                "prompt": {"type": "string", "description": "prompt that produced the image"},
                "auto_size": {"type": "boolean", "description": "resize the shape to match the image's natural aspect ratio"},
            },
            "required": ["shape_id", "image_url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_read_annotations",
        "description": (
            "Return every annotation drawn on top of a target AI image, "
            "including structured spatial data and a markdown summary suitable for an LLM prompt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
                "shape_id": {"type": "string", "description": "id of the target ai-image shape"},
            },
            "required": ["shape_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_edit_image_from_annotations",
        "description": (
            "Record the intent to edit a target image based on its annotations. "
            "Returns structured annotations and markdown. The agent should then generate the new image "
            "(e.g. with linyuebanzi-image-gen) and fill a new ai-image holder with prompt_canvas_fill_ai_image_holder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
                "shape_id": {"type": "string", "description": "id of the source ai-image shape"},
                "from_version": {"type": "string"},
                "to_version": {"type": "string"},
            },
            "required": ["shape_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_submit",
        "description": (
            "Submit the current canvas state (target image + references + notes + annotations) "
            "as a structured request for Codex. Writes a pending markdown file the agent can read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
                "target": {"type": "object"},
                "references": {"type": "array"},
                "annotations": {"type": "array"},
                "notes": {"type": "array"},
                "next_version": {"type": "string"},
                "md": {"type": "string"},
            },
            "required": ["target", "next_version", "md"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_get_pending_submits",
        "description": (
            "List pending canvas submit requests. Call this periodically to detect when the user "
            "has drawn new annotations and submitted them from the browser."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_mark_submit_done",
        "description": (
            "Mark a pending canvas submit request as processed. Call this after you have generated "
            "and filled the new image so the same request is not processed twice."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
                "ts": {"type": "string", "description": "timestamp from prompt_canvas_get_pending_submits"},
            },
            "required": ["ts"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_delete_shape",
        "description": "Delete a shape from the canvas by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
                "shape_id": {"type": "string"},
            },
            "required": ["shape_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_reset",
        "description": "Erase everything from a canvas. Useful for starting a new project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_get_selection",
        "description": "Return the current page's shapes so the user can pick an anchor. The Prompt Canvas server does not track live selection; this returns the full shape list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_insert_image",
        "description": "Insert a bitmap as a new ai-image shape on the current page. Used for standalone image placement or annotation-edit results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "canvas_id": {"type": "string"},
                "image_url": {"type": "string", "description": "URL or absolute local path to the image"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "w": {"type": "number"},
                "h": {"type": "number"},
                "label": {"type": "string"},
                "prompt": {"type": "string"},
                "fileName": {"type": "string"},
                "auto_size": {"type": "boolean"},
            },
            "required": ["image_url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_create_page",
        "description": "Create a new page (canvas). Same as prompt_canvas_create_canvas.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "source": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "prompt_canvas_list_pages",
        "description": "List all pages (canvases). Same as prompt_canvas_list_canvases.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


# ---------- tool implementations ----------
def tool_prompt_canvas_open(_args: dict) -> dict:
    h = _http("GET", "/api/health")
    return {"url": BASE_URL + "/", "health": h}


def tool_prompt_canvas_health(_args: dict) -> dict:
    return _http("GET", "/api/health")


def tool_prompt_canvas_create_canvas(args: dict) -> dict:
    body = {"name": args.get("name"), "source": args.get("source", "codex")}
    return _http("POST", "/api/canvas", body)


def tool_prompt_canvas_list_canvases(_args: dict) -> dict:
    return _http("GET", "/api/canvas")


def tool_prompt_canvas_get_state(args: dict) -> dict:
    return _http("GET", "/api/state", query={"canvas": _canvas_id(args)})


def tool_prompt_canvas_create_ai_image_holder(args: dict) -> dict:
    cid = _canvas_id(args)
    body = {
        "x": args.get("x", 200),
        "y": args.get("y", 200),
        "w": args.get("w", 360),
        "h": args.get("h", 480),
        "label": args.get("label", "AI Image"),
        "meta": {"prompt": args.get("prompt", "")},
    }
    return _http("POST", "/api/commands", body={"action": "create_ai_image_holder", "args": body},
                 query={"canvas": cid})


def tool_prompt_canvas_fill_ai_image_holder(args: dict) -> dict:
    cid = _canvas_id(args)
    body = {
        "shape_id": args["shape_id"],
        "image_url": args["image_url"],
        "prompt": args.get("prompt"),
        "auto_size": args.get("auto_size", False),
    }
    return _http("POST", "/api/commands", body={"action": "fill_ai_image_holder", "args": body},
                 query={"canvas": cid})


def tool_prompt_canvas_read_annotations(args: dict) -> dict:
    cid = _canvas_id(args)
    sid = args.get("shape_id")
    if not sid:
        return {"error": "shape_id required"}
    return _http("GET", "/api/annotations", query={"canvas": cid, "shape_id": sid})


def tool_prompt_canvas_edit_image_from_annotations(args: dict) -> dict:
    cid = _canvas_id(args)
    sid = args.get("shape_id")
    if not sid:
        return {"error": "shape_id required"}
    body = {
        "shape_id": sid,
        "from_version": args.get("from_version"),
        "to_version": args.get("to_version"),
    }
    return _http("POST", "/api/commands",
                 body={"action": "edit_image_from_annotations", "args": body},
                 query={"canvas": cid})


def tool_prompt_canvas_submit(args: dict) -> dict:
    cid = _canvas_id(args)
    body = {
        "target": args["target"],
        "references": args.get("references", []),
        "annotations": args.get("annotations", []),
        "notes": args.get("notes", []),
        "next_version": args["next_version"],
        "md": args["md"],
    }
    return _http("POST", "/api/submit", body=body, query={"canvas": cid})


def tool_prompt_canvas_get_pending_submits(args: dict) -> dict:
    cid = _canvas_id(args)
    return _http("GET", "/api/pending", query={"canvas": cid})


def tool_prompt_canvas_mark_submit_done(args: dict) -> dict:
    cid = _canvas_id(args)
    ts = args.get("ts")
    if not ts:
        return {"error": "ts required"}
    return _http("POST", f"/api/pending/{ts}/done", query={"canvas": cid})


def tool_prompt_canvas_delete_shape(args: dict) -> dict:
    cid = _canvas_id(args)
    sid = args.get("shape_id")
    if not sid:
        return {"error": "shape_id required"}
    return _http("POST", "/api/commands",
                 body={"action": "delete_shape", "args": {"shape_id": sid}},
                 query={"canvas": cid})


def tool_prompt_canvas_reset(args: dict) -> dict:
    cid = _canvas_id(args)
    return _http("POST", "/api/commands", body={"action": "reset", "args": {}}, query={"canvas": cid})


def tool_prompt_canvas_get_selection(args: dict) -> dict:
    cid = _canvas_id(args)
    state = _http("GET", "/api/state", query={"canvas": cid})
    shapes = (state.get("shapes") or {}) if isinstance(state, dict) else {}
    return {
        "canvas_id": cid,
        "note": "Prompt Canvas does not track live selection server-side. Pick a shape_id from the list below.",
        "shapes": list(shapes.values()) if isinstance(shapes, dict) else shapes,
    }


def tool_prompt_canvas_insert_image(args: dict) -> dict:
    cid = _canvas_id(args)
    body = {
        "shape_id": args.get("shape_id"),
        "image_url": args["image_url"],
        "x": args.get("x", 200),
        "y": args.get("y", 200),
        "w": args.get("w", 360),
        "h": args.get("h", 480),
        "label": args.get("label", "AI Image"),
        "prompt": args.get("prompt"),
        "fileName": args.get("fileName"),
        "auto_size": args.get("auto_size", False),
    }
    return _http("POST", "/api/commands",
                 body={"action": "insert_image", "args": body},
                 query={"canvas": cid})


def tool_prompt_canvas_create_page(args: dict) -> dict:
    return tool_prompt_canvas_create_canvas(args)


def tool_prompt_canvas_list_pages(_args: dict) -> dict:
    return tool_prompt_canvas_list_canvases(_args)


TOOL_RUNNERS = {
    "prompt_canvas_open": tool_prompt_canvas_open,
    "prompt_canvas_health": tool_prompt_canvas_health,
    "prompt_canvas_create_canvas": tool_prompt_canvas_create_canvas,
    "prompt_canvas_create_page": tool_prompt_canvas_create_page,
    "prompt_canvas_list_canvases": tool_prompt_canvas_list_canvases,
    "prompt_canvas_list_pages": tool_prompt_canvas_list_pages,
    "prompt_canvas_get_state": tool_prompt_canvas_get_state,
    "prompt_canvas_get_selection": tool_prompt_canvas_get_selection,
    "prompt_canvas_create_ai_image_holder": tool_prompt_canvas_create_ai_image_holder,
    "prompt_canvas_fill_ai_image_holder": tool_prompt_canvas_fill_ai_image_holder,
    "prompt_canvas_insert_image": tool_prompt_canvas_insert_image,
    "prompt_canvas_read_annotations": tool_prompt_canvas_read_annotations,
    "prompt_canvas_edit_image_from_annotations": tool_prompt_canvas_edit_image_from_annotations,
    "prompt_canvas_submit": tool_prompt_canvas_submit,
    "prompt_canvas_get_pending_submits": tool_prompt_canvas_get_pending_submits,
    "prompt_canvas_mark_submit_done": tool_prompt_canvas_mark_submit_done,
    "prompt_canvas_delete_shape": tool_prompt_canvas_delete_shape,
    "prompt_canvas_reset": tool_prompt_canvas_reset,
}


# ---------- MCP JSON-RPC plumbing ----------
def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _tool_result(text: str, *, is_error: bool = False):
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def handle_message(msg: dict) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"tools": {"listChanged": False}},
        })

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        runner = TOOL_RUNNERS.get(name)
        if not runner:
            return _ok(req_id, _tool_result(f"unknown tool: {name}", is_error=True))
        try:
            result = runner(args)
        except Exception as e:
            return _ok(req_id, _tool_result(f"tool '{name}' crashed: {e}", is_error=True))
        return _ok(req_id, _tool_result(json.dumps(result, ensure_ascii=False, indent=2)))

    if method in ("resources/list", "resources/read", "prompts/list", "prompts/get"):
        if method.endswith("/list"):
            return _ok(req_id, {method.split("/")[0]: []})
        return _err(req_id, -32601, f"method not implemented: {method}")

    return _err(req_id, -32601, f"method not found: {method}")


def main():
    stdin = sys.stdin
    stdout = sys.stdout
    stdout_reopen = getattr(stdout, "reconfigure", None)
    if stdout_reopen:
        try:
            stdout.reconfigure(encoding="utf-8", line_buffering=True)
        except Exception:
            pass
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[prompt-canvas-mcp] bad json: {e}\n")
            continue
        try:
            resp = handle_message(msg)
        except Exception as e:
            resp = _err(msg.get("id"), -32603, f"internal error: {e}")
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
