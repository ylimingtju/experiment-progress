#!/usr/bin/env python3
"""Local experiment progress, Python standard library only."""
import argparse
import contextlib
import fcntl
import heapq
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = Path(os.environ.get("EXPERIMENT_PROGRESS_HOME", Path.home() / ".codex/experiment-progress")).expanduser().resolve()
SCRIPT = Path(__file__).resolve()
TERMINAL = {"succeeded", "failed"}
ACTIVE = {"starting", "running", "observing"}


@contextlib.contextmanager
def db():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(DATA / "runs.sqlite3", timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, cwd TEXT NOT NULL,
            total INTEGER, planned INTEGER NOT NULL, demo INTEGER NOT NULL,
            status TEXT NOT NULL, started REAL NOT NULL, ended REAL,
            heartbeat REAL NOT NULL, pid INTEGER, exit_code INTEGER, log TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS tasks (
            run_id TEXT NOT NULL, id TEXT NOT NULL, group_name TEXT NOT NULL,
            state TEXT NOT NULL, started REAL, ended REAL, attempt INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (run_id, id)
          );
          CREATE TABLE IF NOT EXISTS eta_priors (
            run_id TEXT NOT NULL, group_name TEXT NOT NULL,
            seconds REAL NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY (run_id, group_name)
          );
        """)
        with conn:
            yield conn
    finally:
        conn.close()


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError("名称或任务 ID 必须为 1–500 字符的非空字符串")
    return value


def run_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{12}", value):
        raise ValueError("无效的实验轮次 ID")
    return value


def validate_prior(seconds, source):
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("单次实验的启动预估必须为正数秒")
    label(source)


def set_prior(run_id, seconds, source, group="*"):
    validate_prior(seconds, source)
    run_key(run_id)
    label(group)
    with db() as c:
        row = c.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or row["status"] not in ACTIVE:
            raise ValueError("实验轮次不存在或已经结束")
        c.execute("INSERT OR REPLACE INTO eta_priors VALUES (?,?,?,?)", (run_id, group, seconds, source))


def create_run(name, cwd, total=None, plan=None, demo=False, log=None, started=None,
               estimate_seconds=None, estimate_source=None):
    label(name)
    if estimate_seconds is not None or estimate_source is not None:
        validate_prior(estimate_seconds, estimate_source)
    if total is not None and (type(total) is not int or total < 1):
        raise ValueError("总实验数必须为正整数，未知时省略")
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("工作目录不存在")
    rows = []
    if plan is not None:
        if not isinstance(plan, list) or not plan:
            raise ValueError("计划必须为非空 JSON 数组")
        for item in plan:
            rows.append((label(item["id"]), label(item.get("group", "default"))))
        if len({x[0] for x in rows}) != len(rows):
            raise ValueError("计划中的实验 ID 重复")
        if total is not None and total != len(rows):
            raise ValueError("总实验数与计划长度不一致")
        total = len(rows)
    key, now = uuid.uuid4().hex[:12], time.time()
    log = str(Path(log).resolve()) if log else str(DATA / "logs" / (key + ".log"))
    with db() as c:
        c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (key, name, str(cwd), total, int(plan is not None), int(demo),
                   "starting", started or now, None, now, None, None, log))
        c.executemany("INSERT INTO tasks (run_id,id,group_name,state) VALUES (?,?,?,'pending')",
                      [(key, task_id, group) for task_id, group in rows])
        if estimate_seconds is not None:
            c.execute("INSERT INTO eta_priors VALUES (?,?,?,?)", (key, "*", estimate_seconds, estimate_source))
    return key


def report(task_id, state, group=None, run_id=None, started_at=None, ended_at=None):
    """Record one task. Repeated terminal events are idempotent; starting again is a retry."""
    key = run_key(run_id or os.environ.get("EXPERIMENT_PROGRESS_RUN"))
    label(task_id)
    if state not in {"running", "succeeded", "failed"}:
        raise ValueError("状态必须为 running、succeeded 或 failed")
    for value in (started_at, ended_at):
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or value > time.time() + 5):
            raise ValueError("时间必须为有效的 Unix 秒时间戳，不能在未来")
    now = time.time()
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        run = c.execute("SELECT * FROM runs WHERE id=?", (key,)).fetchone()
        if not run or run["status"] not in ACTIVE:
            raise ValueError("实验轮次不存在或已经结束")
        old = c.execute("SELECT * FROM tasks WHERE run_id=? AND id=?", (key, task_id)).fetchone()
        if old and old["state"] == state:
            return
        if old and old["state"] in TERMINAL and state in TERMINAL:
            raise ValueError("已结束任务的结果不能覆盖；重试须先报告 running")
        if not old:
            count = c.execute("SELECT count(*) FROM tasks WHERE run_id=?", (key,)).fetchone()[0]
            if run["planned"] or (run["total"] is not None and count >= run["total"]):
                raise ValueError("任务不在计划内或任务数超过总数")
        group = label(group or (old["group_name"] if old else "default"))
        if old and group != old["group_name"]:
            raise ValueError("同一任务的实验组不能改变")
        start = started_at if started_at is not None else (old["started"] if old else None)
        if state == "running":
            start, end = started_at or now, None
        else:
            end = ended_at or now
            if start is not None and end < start:
                raise ValueError("结束时间早于开始时间")
        attempt = old["attempt"] if old else 1
        if old and old["state"] in TERMINAL and state == "running":
            attempt += 1
        c.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?)",
                  (key, task_id, group, state, start, end, attempt))


@contextlib.contextmanager
def task(task_id, group=None, run_id=None):
    """Optional instrumentation; a monitor error must not change an experiment's result."""
    def emit(state):
        try:
            report(task_id, state, group, run_id)
        except Exception as exc:
            print("[experiment-progress] 状态记录失败: " + str(exc), file=sys.stderr)
    emit("running")
    try:
        yield
    except BaseException:
        emit("failed")
        raise
    else:
        emit("succeeded")


def process_info(pid):
    if pid < 1:
        raise ValueError("PID 必须为正整数")
    p = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "etime="],
                       capture_output=True, text=True, timeout=5)
    if p.returncode or not p.stdout.strip():
        return None
    identity, elapsed = p.stdout.strip().rsplit(None, 1)
    days, _, clock = elapsed.rpartition("-")
    parts = [int(x) for x in clock.split(":")]
    seconds = sum(x * 60**i for i, x in enumerate(reversed(parts))) + (int(days) * 86400 if days else 0)
    return identity, time.time() - seconds


def finish(key, code=None, observed=False):
    with db() as c:
        row = c.execute("SELECT total FROM runs WHERE id=?", (key,)).fetchone()
        counts = dict(c.execute("SELECT state,count(*) FROM tasks WHERE run_id=? GROUP BY state", (key,)).fetchall())
        ended = counts.get("succeeded", 0) + counts.get("failed", 0)
        status = "exit_unknown" if observed else "failed" if code else "unverified"
        if not observed and code == 0 and row["total"] is not None and ended == row["total"]:
            status = "completed_with_errors" if counts.get("failed") else "completed"
        c.execute("UPDATE runs SET status=?,ended=?,heartbeat=?,exit_code=? WHERE id=?",
                  (status, time.time(), time.time(), code, key))


def supervise(key, command=None, pid=None, identity=None):
    key = run_key(key)
    with db() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (key,)).fetchone()
    env = dict(os.environ, EXPERIMENT_PROGRESS_RUN=key, EXPERIMENT_PROGRESS_HOME=str(DATA), PYTHONUNBUFFERED="1")
    try:
        if command:
            Path(row["log"]).parent.mkdir(parents=True, exist_ok=True)
            with open(row["log"], "ab", buffering=0) as log:
                child = subprocess.Popen(command, cwd=row["cwd"], env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT)
            pid = child.pid
        with db() as c:
            c.execute("UPDATE runs SET pid=?,status=?,heartbeat=? WHERE id=?",
                      (pid, "running" if command else "observing", time.time(), key))
        while True:
            if command:
                try:
                    code = child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    finish(key, code)
                    break
            else:
                info = process_info(pid)
                if not info or info[0] != identity:
                    finish(key, observed=True)
                    break
                time.sleep(2)
            with db() as c:
                c.execute("UPDATE runs SET heartbeat=? WHERE id=?", (time.time(), key))
    except Exception as exc:
        with db() as c:
            c.execute("UPDATE runs SET status='monitor_error',ended=?,heartbeat=? WHERE id=?",
                      (time.time(), time.time(), key))
        print("监测程序异常；实验进程状态须另行核验: " + str(exc), file=sys.stderr)


def quantile(values, fraction):
    values = sorted(values)
    pos = (len(values) - 1) * fraction
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def estimate(run, tasks, now, priors=None):
    if run["status"] not in ACTIVE or now - run["heartbeat"] > 12:
        return {"seconds": None, "reason": "轮次未运行或监测信号中断"}
    active = [t for t in tasks if t["state"] == "running"]
    pending = [t for t in tasks if t["state"] == "pending"]
    if run["total"] is None:
        return {"seconds": None, "reason": "总实验数未知"}
    if not active:
        return {"seconds": None, "reason": "等待运行中的实验与并发信息"}
    if not run["planned"]:
        groups = {t["group_name"] for t in tasks}
        group = next(iter(groups)) if len(groups) == 1 else None
        pending += [{"group_name": group} for _ in range(max(0, run["total"] - len(tasks)))]
    samples, pooled = {}, []
    for t in sorted(tasks, key=lambda t: t["ended"] or 0):
        if t["state"] == "succeeded" and t["started"] is not None and t["ended"] is not None:
            elapsed = t["ended"] - t["started"]
            if elapsed > 0:
                samples.setdefault(t["group_name"], []).append(elapsed)
                pooled.append(elapsed)
    needed = {t["group_name"] for t in active + pending}
    samples = {g: v[-30:] for g, v in samples.items()}
    pooled, priors = pooled[-30:], priors or {}
    durations, methods, sources = {}, set(), set()
    for group in needed:
        values = samples.get(group, [])
        prior = priors.get(group) or priors.get("*")
        if values:
            if len(values) >= 3:
                durations[group] = [quantile(values, 0.25), quantile(values, 0.75)]
                methods.add("grouped")
            else:
                durations[group] = [min(values) * 0.5, max(values) * 2]
                methods.add("few")
        elif prior:
            durations[group] = [prior["seconds"] * 0.5, prior["seconds"] * 2]
            methods.add("prior")
            sources.add(prior["source"])
        elif pooled:
            durations[group] = [quantile(pooled, 0.25) * 0.5, quantile(pooled, 0.75) * 2]
            methods.add("pooled")
        else:
            return {"seconds": None, "reason": "尚无有计时的成功样本；可录入有依据的单次耗时预估，不必等待每组 3 次"}
    # ponytail: early/overrun ranges are heuristics, not confidence intervals; calibrate with comparable historical runs when available.
    for group in {t["group_name"] for t in active}:
        age = max(max(0, now - (t["started"] or now)) for t in active if t["group_name"] == group)
        limits = durations[group]
        if age >= limits[1]:
            limits[0] = max(limits[0], age * 1.25)
            limits[1] = max(limits[1], age * 2)
            methods.add("overrun")
    bounds = []
    # ponytail: greedy queue estimate assumes current concurrency and independent jobs; use the real scheduler when dependencies matter.
    for index in (0, 1):
        duration = {g: v[index] for g, v in durations.items()}
        slots = [max(0, duration[t["group_name"]] - max(0, now - (t["started"] or now))) for t in active]
        heapq.heapify(slots)
        for t in pending:
            heapq.heappush(slots, heapq.heappop(slots) + duration[t["group_name"]])
        bounds.append(max(slots))
    title = "按组估计"
    notes = []
    if "grouped" in methods:
        notes.append("有 3 个及以上同组样本的组使用最近至多 30 个样本的 25%–75% 分位耗时")
    if "few" in methods:
        title = "初步估计"
        notes.append("部分组仅有 1–2 个同组样本，使用最短样本的 0.5 倍至最长样本的 2 倍作粗估")
    if "prior" in methods:
        title = "初始预估"
        notes.append("无同组样本的组使用启动预估的 0.5–2 倍；依据：" + "；".join(sorted(sources)))
    if "pooled" in methods:
        title = "跨组粗估"
        notes.append("无同组样本的任务暂借最近已完成任务的耗时，并扩大范围；组间差异可能使预测偏差很大")
    if "overrun" in methods:
        title = "动态粗估"
        notes.append("有任务超出原范围，按该任务已运行时间的 1.25–2 倍上调同组单次总耗时")
    if not run["planned"]:
        notes.append("未提供逐项计划，未报告任务沿用当前组或总体耗时")
    notes.append("按当前并发数估算，范围不是置信区间或完成时限")
    return {"seconds": bounds, "label": title, "reason": "。".join(notes)}


def snapshot(key):
    with db() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_key(key),)).fetchone()
        if not row:
            raise ValueError("轮次不存在")
        run = dict(row)
        tasks = [dict(x) for x in c.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY rowid", (key,))]
        priors = {x["group_name"]: dict(x) for x in c.execute("SELECT * FROM eta_priors WHERE run_id=?", (key,))}
    now = time.time()
    counts = {s: sum(t["state"] == s for t in tasks) for s in ("succeeded", "failed", "running", "pending")}
    if run["total"] is not None:
        counts["pending"] = run["total"] - counts["succeeded"] - counts["failed"] - counts["running"]
    groups = {}
    for t in tasks:
        g = groups.setdefault(t["group_name"], {"name": t["group_name"], "registered": 0, "succeeded": 0, "failed": 0, "running": 0, "pending": 0})
        g["registered"] += 1
        g[t["state"]] += 1
    run.update(counts=counts, groups=list(groups.values()),
               active=[t for t in tasks if t["state"] == "running"][:30],
               failures=[t for t in tasks if t["state"] == "failed"][:30],
               elapsed=max(0, (run["ended"] or now) - run["started"]),
               stale=run["status"] in ACTIVE and now - run["heartbeat"] > 12,
               server_time=now, eta=estimate(run, tasks, now, priors))
    try:
        with open(run["log"], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8000))
            run["log_tail"] = f.read(8000).decode("utf-8", errors="replace")
    except OSError:
        run["log_tail"] = "尚无日志或日志不可读"
    return run


def serve(reuse_address=False):
    previous = json.loads((DATA / "server.json").read_text()) if reuse_address else {}
    token = previous.get("token") or secrets.token_urlsafe(32)
    html = (SCRIPT.parent.parent / "assets/dashboard.html").read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            body = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            host = self.headers.get("Host", "")
            if host not in {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}:
                return self.send(403, {"error": "Host denied"})
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                return self.send(403, {"error": "Origin denied"})
            path = urlsplit(self.path).path
            if path == "/":
                return self.send(200, html, "text/html; charset=utf-8")
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return self.send(403, {"error": "请通过 Skill 或本地启动器打开面板"})
            try:
                if path == "/api/health":
                    return self.send(200, {"service": "experiment-progress", "version": 1})
                if path == "/api/runs":
                    with db() as c:
                        rows = [dict(x) for x in c.execute("SELECT id,name,status,started,demo FROM runs ORDER BY started DESC LIMIT 50")]
                    return self.send(200, rows)
                if path.startswith("/api/run/"):
                    return self.send(200, snapshot(path.removeprefix("/api/run/")))
                return self.send(404, {"error": "Not found"})
            except (ValueError, sqlite3.Error) as exc:
                return self.send(400, {"error": str(exc)})
    server = ThreadingHTTPServer(("127.0.0.1", urlsplit(previous.get("base", "")).port or 0), Handler)
    metadata = {"pid": os.getpid(), "base": "http://127.0.0.1:" + str(server.server_port), "token": token}
    tmp = DATA / ("server-" + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(metadata))
    tmp.chmod(0o600)
    tmp.replace(DATA / "server.json")
    server.serve_forever()


def ensure_server():
    with db():
        pass
    with open(DATA / "server.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        def existing():
            try:
                meta = json.loads((DATA / "server.json").read_text())
                if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", meta["base"]):
                    return None
                req = urllib.request.Request(meta["base"] + "/api/health", headers={"Authorization": "Bearer " + meta["token"]})
                with urllib.request.urlopen(req, timeout=1) as response:
                    if json.load(response).get("service") == "experiment-progress":
                        return meta
            except (OSError, ValueError, KeyError):
                return None
        meta = existing()
        if not meta:
            with open(DATA / "service.log", "ab") as log:
                subprocess.Popen([sys.executable, str(SCRIPT), "_serve"], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 env=dict(os.environ, EXPERIMENT_PROGRESS_HOME=str(DATA)))
            for _ in range(50):
                time.sleep(0.1)
                meta = existing()
                if meta:
                    break
            if not meta:
                raise RuntimeError("面板服务未启动，请检查 " + str(DATA / "service.log"))
        return meta["base"] + "/?token=" + meta["token"]


def launch(args):
    plan = json.loads(Path(args.plan).read_text()) if args.plan else None
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if args.action == "launch" and not command:
        raise ValueError("请在 -- 后提供前台实验命令")
    info = process_info(args.pid) if args.action == "attach" else None
    if args.action == "attach" and not info:
        raise ValueError("指定进程不存在")
    url = ensure_server()
    key = create_run(args.name, args.cwd, args.total, plan, args.demo,
                     args.log if args.action == "attach" else None, info[1] if info else None,
                     args.estimate_seconds, args.estimate_source)
    child_args = [sys.executable, str(SCRIPT), "_supervise", "--run", key]
    if info:
        child_args += ["--pid", str(args.pid), "--identity", info[0]]
    else:
        child_args += ["--"] + command
    with open(DATA / "service.log", "ab") as log:
        try:
            subprocess.Popen(child_args, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log,
                             env=dict(os.environ, EXPERIMENT_PROGRESS_HOME=str(DATA)))
        except OSError:
            finish(key, 127)
            raise
    result = {"run_id": key, "url": url + "&run=" + key, "data_dir": str(DATA)}
    print(json.dumps(result, ensure_ascii=False))
    if args.open:
        webbrowser.open(result["url"])


def main():
    parser = argparse.ArgumentParser(description="本地实验进度面板")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("dashboard", help="启动或复用面板服务")
    p.add_argument("--open", action="store_true")
    for name in ("launch", "attach"):
        p = sub.add_parser(name)
        p.add_argument("--name", required=True)
        p.add_argument("--cwd", default=os.getcwd())
        p.add_argument("--total", type=int)
        p.add_argument("--plan")
        p.add_argument("--estimate-seconds", type=float, help="有依据的单次实验初始耗时预估，单位秒")
        p.add_argument("--estimate-source", help="初始耗时预估的依据，与 --estimate-seconds 同时使用")
        p.add_argument("--demo", action="store_true")
        p.add_argument("--open", action="store_true")
        if name == "attach":
            p.add_argument("--pid", type=int, required=True)
            p.add_argument("--log")
        p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("event")
    p.add_argument("state", choices=["running", "succeeded", "failed"])
    p.add_argument("--task", required=True)
    p.add_argument("--run", default=os.environ.get("EXPERIMENT_PROGRESS_RUN"))
    p.add_argument("--group")
    p.add_argument("--started-at", type=float)
    p.add_argument("--ended-at", type=float)
    p = sub.add_parser("status")
    p.add_argument("--run", required=True)
    p = sub.add_parser("set-prior")
    p.add_argument("--run", required=True)
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--group", default="*", help="省略时作为所有未采样组的预估")
    p = sub.add_parser("_serve")
    p.add_argument("--reuse-address", action="store_true")
    p = sub.add_parser("_supervise")
    p.add_argument("--run", required=True)
    p.add_argument("--pid", type=int)
    p.add_argument("--identity")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action in {"launch", "attach"}:
            launch(args)
        elif args.action == "dashboard":
            url = ensure_server()
            print(url)
            if args.open:
                webbrowser.open(url)
        elif args.action == "event":
            report(args.task, args.state, args.group, args.run, args.started_at, args.ended_at)
        elif args.action == "status":
            print(json.dumps(snapshot(args.run), ensure_ascii=False, indent=2))
        elif args.action == "set-prior":
            set_prior(args.run, args.seconds, args.source, args.group)
        elif args.action == "_serve":
            serve(args.reuse_address)
        elif args.action == "_supervise":
            command = args.command[1:] if args.command and args.command[0] == "--" else args.command
            supervise(args.run, command, args.pid, args.identity)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
