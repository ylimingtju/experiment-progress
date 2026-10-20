#!/usr/bin/env python3
"""Codex plugin 「实验跑道」: a stdio MCP server (standard library only) serving experiment-progress records as
MCP App views. Records, ETA and model forecasts all come from the experiment-progress skill; nothing is duplicated."""
import base64
import json
import os
from pathlib import Path
import sys
import traceback

HERE = Path(__file__).resolve().parent
SKILL = Path(os.environ.get("EXPERIMENT_PROGRESS_SKILL") or Path.home() / ".codex/skills/experiment-progress").expanduser()
sys.path.insert(0, str(SKILL / "scripts"))
import experiment_progress as ep  # noqa: E402

VERSION = "0.1.3"
MIME = "text/html;profile=mcp-app"
TRACK = "ui://experiment-progress/track.html"
OBSERVATORY = "ui://experiment-progress/observatory.html"
RUN = {"type": "object", "additionalProperties": False,
       "properties": {"run_id": {"type": "string", "description": "12 位轮次 ID；省略时取最近一个运行中的轮次"}}}
APP_ONLY = {"ui": {"visibility": ["app"]}}
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

TOOLS = [
    {"name": "experiment.track", "title": "实验跑道",
     "description": "在当前线程放一条实时刷新的实验进度跑道（进度、预计剩余、最迟时间）。实验启动并确认接入后调用一次；"
                    "之后跑道自行刷新，不要为更新数字重复调用。",
     "inputSchema": RUN, "annotations": READ_ONLY,
     "_meta": {"ui": {"resourceUri": TRACK, "visibility": ["model", "app"]}, "openai/outputTemplate": TRACK,
               "openai/ui": {"entrypoints": [{"type": "thread"}]}}},
    {"name": "experiment.observatory", "title": "实验观测台",
     "description": "完整的实验观测台：所有轮次、任务矩阵、完成曲线、分组耗时与日志。",
     "inputSchema": RUN, "annotations": READ_ONLY,
     "_meta": {"ui": {"resourceUri": OBSERVATORY, "visibility": ["app"]}, "openai/iconStyle": "monochrome",
               "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]}}},
    {"name": "experiment.snapshot", "title": "轮次快照", "description": "某一轮次的当前状态（供界面轮询）。",
     "inputSchema": {**RUN, "properties": {**RUN["properties"], "full": {"type": "boolean"}}},
     "annotations": READ_ONLY, "_meta": APP_ONLY},
    {"name": "experiment.tasks", "title": "任务矩阵", "description": "某一轮次全部任务的紧凑表。",
     "inputSchema": RUN, "annotations": READ_ONLY, "_meta": APP_ONLY},
    {"name": "experiment.runs", "title": "轮次列表", "description": "最近 50 个轮次。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": READ_ONLY, "_meta": APP_ONLY},
]


def pick(run_id=None):
    """The run a view shows: the given one, else the newest live run, else the newest run."""
    if run_id:
        return ep.run_key(run_id)
    runs = ep.list_runs(50)
    live = [r for r in runs if r["status"] in ep.ACTIVE and not r["stale"]]
    return (live or runs)[0]["id"] if runs else None


def compact(run):
    """The fields the track needs; the full snapshot also carries task lists and an 8 KB log tail."""
    eta = run["eta"]
    lines = [x for x in (run.get("log_tail") or "").splitlines() if x.strip()]
    out = {k: run.get(k) for k in ("id", "name", "status", "stale", "demo", "total", "started", "ended", "heartbeat",
                                   "elapsed", "counts", "throughput", "concurrency", "tasks_version", "server_time",
                                   "exit_code")}
    out["eta"] = {k: eta.get(k) for k in ("seconds", "point", "latest", "label", "reason", "limit", "methods", "forecast")}
    out["groups"] = [{k: g.get(k) for k in ("name", "succeeded", "failed", "running", "pending", "median")}
                     for g in run["groups"]]
    out["last_log"] = lines[-1][-240:] if lines else ""
    return out


def call(name, args):
    if name == "experiment.runs":
        return {"runs": ep.list_runs(50)}, None
    key = pick(args.get("run_id"))
    if key is None:
        return {"run": None}, "还没有实验记录。"
    if name == "experiment.tasks":
        return ep.task_table(key), None
    run = ep.snapshot(key)
    if name == "experiment.snapshot" and args.get("full"):
        return run, None
    data = {"run_id": key, "run": compact(run)}
    # The model sees the same few lines as `status --brief`; the view polls on its own afterwards.
    return data, (ep.brief(run) if name == "experiment.track" else None)


def page(name):
    html = (HERE / "assets" / name).read_text(encoding="utf-8")
    runner = base64.b64encode((HERE / "assets/runner.png").read_bytes()).decode()
    bridge = (HERE / "assets/bridge.js").read_text(encoding="utf-8")
    return html.replace("__RUNNER_PNG__", "data:image/png;base64," + runner).replace("__BRIDGE_JS__", bridge)


def observatory():
    """The skill's dashboard, served inside Codex: same file, with its fetch layer swapped for MCP tool calls."""
    html = (SKILL / "assets/dashboard.html").read_text(encoding="utf-8")
    bridge = (HERE / "assets/bridge.js").read_text(encoding="utf-8")
    return html.replace("<script>", "<script>" + bridge + "\n</script>\n<script>", 1)


RESOURCES = {
    TRACK: ("实验跑道", lambda: page("track.html"), {"openai/widgetShowCodexWidgetInline": True,
                                                    "openai/widgetHeightHint": 52, "openai/widgetHideAttribution": True}),
    OBSERVATORY: ("实验观测台", observatory, {}),
}


class Unknown(Exception):
    pass


def handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                "serverInfo": {"name": "experiment-progress", "title": "实验观测台", "version": VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "resources/list":
        return {"resources": [{"uri": uri, "name": title, "mimeType": MIME} for uri, (title, _, _) in RESOURCES.items()]}
    if method in ("resources/templates/list", "prompts/list"):
        return {"resourceTemplates": []} if method.startswith("resources") else {"prompts": []}
    if method == "resources/read":
        if params.get("uri") not in RESOURCES:
            raise Unknown("unknown resource")
        title, build, meta = RESOURCES[params["uri"]]
        return {"contents": [{"uri": params["uri"], "mimeType": MIME, "text": build(),
                              "_meta": {"ui": {"prefersBorder": False, "csp": {"resourceDomains": ["data:"]}}, **meta}}]}
    if method == "tools/call":
        name = params.get("name")
        if name not in {t["name"] for t in TOOLS}:
            return {"isError": True, "content": [{"type": "text", "text": "unknown tool " + str(name)}]}
        try:
            data, text = call(name, params.get("arguments") or {})
        except (ValueError, KeyError, OSError) as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        return {"structuredContent": data,
                "content": [{"type": "text", "text": text or json.dumps({"ok": True}, ensure_ascii=False)}]}
    raise Unknown("method not found")


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in msg or "method" not in msg:
            continue                                     # notifications and stray responses need no reply
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except Unknown as exc:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": str(exc)}}
        except Exception as exc:                         # keep serving; report the failure to the host
            traceback.print_exc(file=sys.stderr)
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(exc)}}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
