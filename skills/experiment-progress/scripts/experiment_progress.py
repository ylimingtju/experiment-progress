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
import signal
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
STATE_CODES = {"pending": 0, "running": 1, "succeeded": 2, "failed": 3}
VERSION = 2


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
          CREATE TABLE IF NOT EXISTS task_params (
            run_id TEXT NOT NULL, id TEXT NOT NULL, params TEXT NOT NULL,
            PRIMARY KEY (run_id, id)
          );
          CREATE TABLE IF NOT EXISTS eta_limits (
            run_id TEXT NOT NULL, group_name TEXT NOT NULL,
            seconds REAL NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY (run_id, group_name)
          );
          CREATE TABLE IF NOT EXISTS eta_forecasts (
            run_id TEXT NOT NULL, at REAL NOT NULL, ended INTEGER NOT NULL,
            remaining REAL NOT NULL, low REAL, high REAL, basis TEXT NOT NULL,
            PRIMARY KEY (run_id, at)
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


def natural(value):
    """Sort key: case-001 < case-2 < case-10."""
    return [int(x) if x.isdigit() else x for x in re.split(r"([0-9]+)", value)]


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


def set_limit(run_id, seconds, source, group="*"):
    """Per-task time limit (e.g. a solver TimeLimit): no task of the group is expected to run longer."""
    validate_prior(seconds, source)
    run_key(run_id)
    label(group)
    with db() as c:
        row = c.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or row["status"] not in ACTIVE:
            raise ValueError("实验轮次不存在或已经结束")
        c.execute("INSERT OR REPLACE INTO eta_limits VALUES (?,?,?,?)", (run_id, group, seconds, source))


def record_forecast(run_id, remaining, basis, low=None, high=None):
    """A model's judgment of the time left, written during an existing silent follow-up."""
    for value in (remaining, low, high):
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
            raise ValueError("剩余时间必须为正数秒")
    if not (low or remaining) <= remaining <= (high or remaining):
        raise ValueError("须满足 low ≤ remaining ≤ high")
    label(basis)
    with db() as c:
        row = c.execute("SELECT status FROM runs WHERE id=?", (run_key(run_id),)).fetchone()
        if not row or row["status"] not in ACTIVE:
            raise ValueError("实验轮次不存在或已经结束")
        ended = c.execute("SELECT count(*) FROM tasks WHERE run_id=? AND state IN ('succeeded','failed')", (run_id,)).fetchone()[0]
        c.execute("INSERT OR REPLACE INTO eta_forecasts VALUES (?,?,?,?,?,?,?)",
                  (run_id, time.time(), ended, remaining, low, high, basis))


def create_run(name, cwd, total=None, plan=None, demo=False, log=None, started=None,
               estimate_seconds=None, estimate_source=None, limit_seconds=None, limit_source=None):
    label(name)
    if estimate_seconds is not None or estimate_source is not None:
        validate_prior(estimate_seconds, estimate_source)
    if limit_seconds is not None or limit_source is not None:
        validate_prior(limit_seconds, limit_source)
    if total is not None and (type(total) is not int or total < 1):
        raise ValueError("总实验数必须为正整数，未知时省略")
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("工作目录不存在")
    rows, params = [], []
    if plan is not None:
        if not isinstance(plan, list) or not plan:
            raise ValueError("计划必须为非空 JSON 数组")
        for item in plan:
            rows.append((label(item["id"]), label(item.get("group", "default"))))
            if item.get("params") is not None:
                values = item["params"]
                if (not isinstance(values, dict) or len(values) > 20 or any(
                        not isinstance(k, str) or not k or len(k) > 100 or type(v) not in (str, int, float, bool)
                        or len(str(v)) > 200 for k, v in values.items())):
                    raise ValueError("计划中的 params 必须为至多 20 项的 {参数名: 字符串或数值}")
                params.append((rows[-1][0], json.dumps(values, ensure_ascii=False, sort_keys=True)))
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
        c.executemany("INSERT INTO task_params VALUES (?,?,?)", [(key, task_id, p) for task_id, p in params])
        if estimate_seconds is not None:
            c.execute("INSERT INTO eta_priors VALUES (?,?,?,?)", (key, "*", estimate_seconds, estimate_source))
        if limit_seconds is not None:
            c.execute("INSERT INTO eta_limits VALUES (?,?,?,?)", (key, "*", limit_seconds, limit_source))
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


def tokens(name):
    """Words and numbers of a group name: 'BO/n=30' -> ['BO', 'n', '30']."""
    return re.findall(r"[0-9]+(?:\.[0-9]+)?|[^\W\d_]+", name or "")


def task_keys(task_id, params=None):
    """Parameter values of one task: plan params when given, else ID segments split on / | , ; by position."""
    if params:
        return [f"{k}={v}" for k, v in sorted(params.items())]
    parts = [x for x in re.split(r"[/|,;\s]+", task_id or "") if x]
    return [x if "=" in x else f"#{i + 1}={x}" for i, x in enumerate(parts)]


def param_effects(done, keys, rounds=4, shrink=2.0):
    """Additive log-duration effect of each parameter value within its group, shrunk toward 0 so that
    values seen only once or twice barely move the prediction."""
    logs = {}
    for t in done:
        logs.setdefault(t["group_name"], []).append(math.log(t["ended"] - t["started"]))
    base = {g: sum(v) / len(v) for g, v in logs.items()}
    resid = [(keys.get(t["id"], []), math.log(t["ended"] - t["started"]) - base[t["group_name"]]) for t in done]
    effect = {}
    for _ in range(rounds):
        sums, counts = {}, {}
        for ks, r in resid:
            total = sum(effect.get(k, 0) for k in ks)
            for k in ks:
                sums[k] = sums.get(k, 0) + r - total + effect.get(k, 0)
                counts[k] = counts.get(k, 0) + 1
        effect = {k: sums[k] / (counts[k] + shrink) for k in sums}
    return effect


def weighted_mean(values, half_life=30):
    weights = [0.5 ** ((len(values) - 1 - i) / half_life) for i in range(len(values))]
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


def power_fit(points, x):
    """Least-squares duration ~ a * size^b in log space; returns the prediction at x and the fit residual."""
    pts = [(math.log(k), math.log(v)) for k, v in points.items() if k > 0 and v > 0]
    if len(pts) < 2:
        return None
    mx, my = sum(a for a, _ in pts) / len(pts), sum(b for _, b in pts) / len(pts)
    sxx = sum((a - mx) ** 2 for a, _ in pts)
    slope = sum((a - mx) * (b - my) for a, b in pts) / sxx
    spread = math.sqrt(sum((b - my - slope * (a - mx)) ** 2 for a, b in pts) / len(pts))
    return math.exp(my + slope * (math.log(x) - mx)), spread


Z = 0.674  # about half of outcomes inside the range; the backtest reports the real share
WINDOW = 120
ETA_TITLES = {"grouped": "按组估计", "few": "初步估计", "scaled": "规模外推", "prior": "初始预估",
              "similar": "相近组借用", "pooled": "跨组粗估"}


def estimate(run, tasks, now, priors=None, params=None, limits=None):
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
    timed = sorted((t for t in tasks if t["state"] in TERMINAL and t["started"] is not None
                    and t["ended"] is not None and t["ended"] > t["started"]), key=lambda t: (t["ended"], t["id"]))
    done = [t for t in timed if t["state"] == "succeeded"]
    params = params or {}
    keys = {t["id"]: task_keys(t["id"], params.get(t["id"])) for t in tasks}
    # A parameter whose values never repeat (e.g. a bare 'case-017') says nothing about other tasks.
    seen = {}
    for ks in keys.values():
        for k in ks:
            seen[k] = seen.get(k, 0) + 1
    shared = {k.split("=")[0] for k, n in seen.items() if n > 1}
    keys = {i: [k for k in ks if k.split("=")[0] in shared] for i, ks in keys.items()}
    effect = param_effects(done, keys)
    # A value not seen yet (e.g. a new configuration) is assumed to behave like that parameter's recent values,
    # not like the run-wide average, so the recency weighting below still tracks drift.
    recent = {}
    for t in done[-WINDOW:]:
        for k in keys.get(t["id"], []):
            recent.setdefault(k.split("=")[0], []).append(effect.get(k, 0))
    recent = {name: weighted_mean(v) for name, v in recent.items()}
    factor = lambda t: math.exp(sum(effect[k] if k in effect else recent.get(k.split("=")[0], 0)
                                    for k in keys.get(t.get("id"), [])))
    succ, fail, failures = {}, {}, {}
    for t in done:
        succ.setdefault(t["group_name"], []).append(t)
    for t in timed:
        if t["state"] == "failed":
            fail.setdefault(t["group_name"], []).append(t["ended"] - t["started"])
    for t in tasks:
        if t["state"] == "failed":
            failures[t["group_name"]] = failures.get(t["group_name"], 0) + 1
    # Per-task time limits: given ones, else one inferred when several tasks stop at the same maximum
    # (a solver TimeLimit piles durations up just above it). Solver wrap-up may overshoot a given limit slightly.
    limits, spans = limits or {}, [t["ended"] - t["started"] for t in timed]
    top = max(spans, default=0)
    piled = sum(d >= top * 0.998 for d in spans)
    inferred = top if top >= 60 and piled >= 3 else None
    caps = {}

    def cap(g):
        if g not in caps:
            given = limits.get(g) or limits.get("*")
            caps[g] = (max([given["seconds"]] + [d for d in spans if given["seconds"] <= d <= given["seconds"] * 1.05])
                       if given else inferred)
        return caps[g]
    # Per group: base seconds of a task with average parameters, relative systematic error, per-task variance,
    # failure share, mean failure seconds and the share of runs that hit the time limit. A run stopped by the limit
    # is censored: its parameters say nothing about how much longer it would have taken, so it is not rescaled.
    hits = lambda t, limit: bool(limit) and t["ended"] - t["started"] >= limit * 0.998
    model, methods, sources, borrowed = {}, set(), set(), []
    for g, ts in succ.items():
        ts = ts[-WINDOW:]
        free = [t for t in ts if not hits(t, cap(g))]
        values = [(t["ended"] - t["started"]) / factor(t) for t in (free or ts)]
        base, n = weighted_mean(values), len(values)
        share = failures.get(g, 0) / (failures.get(g, 0) + len(succ[g]))
        lost = sum(fail[g][-WINDOW:]) / len(fail[g][-WINDOW:]) if fail.get(g) else base
        block = max(10, n // 4)
        if n >= 2 * block:
            means = [sum(values[i:i + block]) / block for i in range(n - block * (n // block), n, block)]
            logs = [math.log(x) for x in means]
            spread = max(0.2, math.sqrt(sum((x - sum(logs) / len(logs)) ** 2 for x in logs) / len(logs)))
        else:
            spread = 0.35 if n >= 3 else math.log(2) / Z
        variance = sum((x - base) ** 2 for x in values) / n if n > 1 else base ** 2
        model[g] = [base, spread, variance, share, lost, 1 - len(free) / len(ts)]
        methods.add("grouped" if len(ts) >= 3 else "few")
    own = dict(model)
    families = {}
    for g, m in own.items():
        tk = tokens(g)
        for i, x in enumerate(tk):
            if x[0].isdigit():
                families.setdefault((tuple(tk[:i]), tuple(tk[i + 1:])), {})[float(x)] = m[0]
    priors = priors or {}
    for g in sorted({t["group_name"] for t in active + pending} - set(own), key=str):
        prior, tk = priors.get(g) or priors.get("*"), tokens(g)
        fits = [power_fit(families[k], float(x)) for i, x in enumerate(tk) if x[0].isdigit() and float(x) > 0
                for k in [(tuple(tk[:i]), tuple(tk[i + 1:]))] if len(families.get(k, {})) >= 2]
        fits = [f for f in fits if f]
        if prior:
            model[g] = [prior["seconds"], math.log(2) / Z, 0, 0, 0, 0]
            methods.add("prior")
            sources.add(prior["source"])
        elif fits:
            mean, spread = min(fits, key=lambda f: f[1])
            model[g] = [mean, 0.3 + spread, mean ** 2 * 0.1, 0, 0, 0]
            methods.add("scaled")
        elif own:
            shared = {h: len(set(tk) & set(tokens(h))) for h in own}
            best = max(shared.values())
            donors = sorted(h for h, s in shared.items() if s == best) if best else []
            if donors:
                mean = math.exp(sum(math.log(own[h][0]) for h in donors) / len(donors))
                model[g] = [mean, 0.6, mean ** 2, 0, 0, 0]
                methods.add("similar")
                borrowed.append(f"{g} ← {'、'.join(donors[:2])}")
            else:
                mean = sum(m[0] for m in own.values()) / len(own)
                model[g] = [mean, 1.0, mean ** 2, 0, 0, 0]
                methods.add("pooled")
        else:
            return {"seconds": None, "reason": "尚无有计时的成功样本；可录入有依据的单次耗时预估，不必等待每组 3 次"}
    for g, m in model.items():
        if cap(g):
            m[0], m[2], m[4] = min(m[0], cap(g)), min(m[2], cap(g) ** 2 / 4), min(m[4], cap(g))

    def expected(t):
        base, _, _, share, lost, hit = model[t["group_name"]]
        limit = cap(t["group_name"])
        typical = min(base * factor(t), limit or math.inf)
        return (1 - share) * (hit * limit + (1 - hit) * typical if limit else typical) + share * lost

    overdue = []
    age = lambda t: max(0, now - (t["started"] or now))

    def remaining(t):
        limit = cap(t["group_name"])
        if limit and age(t) >= limit:
            methods.add("overlimit")
            return limit * 0.05
        # Mean remaining time among this group's past successes that ran longer than this task has so far.
        scale = factor(t)
        longer = [(x["ended"] - x["started"] if hits(x, limit) else (x["ended"] - x["started"]) / factor(x) * scale) - age(t)
                  for x in succ.get(t["group_name"], [])[-WINDOW:]]
        longer = [x for x in longer if x > 0]
        if len(longer) >= 3:
            left = sum(longer) / len(longer)
        elif age(t) > expected(t):
            methods.add("overrun")
            overdue.append(age(t) * 0.5)
            left = age(t) * 0.5
        else:
            left = max(expected(t) - age(t), age(t) * 0.5)
        return min(left, limit - age(t)) if limit else left
    # ponytail: greedy queue estimate assumes current concurrency and independent jobs; use the real scheduler when dependencies matter.
    slots, load, count = [], {}, {}
    for t in active + pending:
        if t.get("state") == "running":
            seconds = remaining(t)
            heapq.heappush(slots, seconds)
        else:
            seconds = expected(t)
            heapq.heapreplace(slots, slots[0] + seconds)
        load[t["group_name"]] = load.get(t["group_name"], 0) + seconds
        count[t["group_name"]] = count.get(t["group_name"], 0) + 1
    point, work = max(slots), sum(load.values())
    # ponytail: the range is a heuristic from block-to-block drift and task noise, not a confidence interval;
    # an overdue task's remaining time counts as fully uncertain (relative sd 1).
    spread = math.sqrt(sum((load[g] * model[g][1]) ** 2 + count[g] * model[g][2] for g in load)
                       + sum(x ** 2 for x in overdue)) / work if work > 0 else 0
    bounds = [point * math.exp(-Z * spread), point * math.exp(Z * spread)]
    latest = None
    if all(cap(t["group_name"]) for t in active + pending):
        # Every remaining task runs to its limit: a hard ceiling under the current concurrency.
        slots = []
        for t in active + pending:
            limit = cap(t["group_name"])
            if t.get("state") == "running":
                heapq.heappush(slots, max(limit - age(t), limit * 0.05))
            else:
                heapq.heapreplace(slots, slots[0] + limit)
        latest = max(slots)
        point, bounds = min(point, latest), [min(bounds[0], latest), min(bounds[1], latest)]
    bounds = [round(x, 1) for x in bounds]
    limited = sorted({str(cap(t["group_name"])) for t in active + pending if cap(t["group_name"])})
    used = sorted({k.split("=")[0] for t in active + pending for k in keys.get(t.get("id"), [])
                   if abs(effect.get(k, 0)) > 0.01})
    if used:
        methods.add("params")
    order = ["grouped", "few", "scaled", "prior", "similar", "pooled"]
    title = "动态粗估" if methods & {"overrun", "overlimit"} else ETA_TITLES[max((m for m in methods if m in ETA_TITLES), key=order.index)]
    notes = []
    if methods & {"grouped", "few"}:
        notes.append("单项耗时取同组最近至多 120 个成功样本的近期加权平均；失败任务按其自身耗时计入")
    if "few" in methods:
        notes.append("部分组仅有 1–2 个同组样本，范围按 0.5–2 倍放宽")
    if used:
        names = "、".join(x if not x.startswith("#") else f"ID 第 {x[1:]} 段" for x in used[:4])
        notes.append(f"按参数相似性校正单项耗时（{names}）：相同取值的已完成任务偏快或偏慢，剩余同取值任务随之调整")
    if "scaled" in methods:
        notes.append("无样本的组按组名中的规模数字拟合幂律外推")
    if "prior" in methods:
        notes.append("无同组样本的组使用启动预估，范围 0.5–2 倍；依据：" + "；".join(sorted(sources)))
    if borrowed:
        notes.append("无样本的组借用组名最相近的组：" + "；".join(borrowed[:3]) + ("等" if len(borrowed) > 3 else ""))
    if "pooled" in methods:
        notes.append("无样本且找不到相近组的任务暂借全部组的平均耗时，偏差可能很大")
    limit_text = None
    if limited:
        given = sorted({f"{x['seconds']:g} 秒（{x['source']}）" for x in limits.values()})
        limit_text = ("单项时限 " + "；".join(given) if given else
                      f"推断单项时限约 {inferred:.0f} 秒（{piled} 个任务停在该值附近）")
        notes.append(limit_text + "：每项估计不超过时限")
    if "overrun" in methods:
        notes.append("有任务已超过同组常见耗时，按其已运行时间的一半估计剩余")
    if "overlimit" in methods:
        notes.append("有任务已达到单项时限，按很快结束估计")
    if not run["planned"]:
        notes.append("未提供逐项计划，未报告任务沿用当前组或总体耗时")
    notes.append("按当前并发数排队估算；大概率范围约有一半把握（启发式），不是置信区间" +
                 ("；最迟时间按所有剩余任务跑满时限计" if latest else ""))
    return {"seconds": bounds, "point": round(point, 1), "latest": round(latest, 1) if latest else None, "limit": limit_text,
            "label": title, "reason": "。".join(notes), "methods": sorted(methods)}


def eta_inputs(c, key):
    """Priors, task parameters and time limits stored beside a run."""
    priors = {x["group_name"]: dict(x) for x in c.execute("SELECT * FROM eta_priors WHERE run_id=?", (key,))}
    params = {x["id"]: json.loads(x["params"]) for x in c.execute("SELECT id,params FROM task_params WHERE run_id=?", (key,))}
    limits = {x["group_name"]: dict(x) for x in c.execute("SELECT * FROM eta_limits WHERE run_id=?", (key,))}
    return priors, params, limits


FORECAST_FRESH = 3 * 3600


def forecast_view(row, live, now):
    """The latest model forecast, counted down from when it was made; it leads only while fresh."""
    if not row:
        return None
    age = now - row["at"]
    fresh = live and age <= FORECAST_FRESH and now <= row["at"] + (row["high"] or row["remaining"])
    why = (None if fresh else "轮次未运行或监测信号中断" if not live else
           "研判已超过 3 小时" if age > FORECAST_FRESH else "已超过模型研判的完成时间")
    left = lambda x: None if x is None else round(max(0, row["at"] + x - now), 1)
    return {"remaining": left(row["remaining"]), "low": left(row["low"]), "high": left(row["high"]),
            "finish": round(row["at"] + row["remaining"], 1), "at": row["at"], "age": round(age),
            "ended": row["ended"], "basis": row["basis"], "fresh": fresh, "stale_reason": why}


def replay(tasks, at):
    """Task states as they stood at time `at`, rebuilt from start and end stamps (for backtests)."""
    out = []
    for t in tasks:
        t = dict(t)
        if t["ended"] is not None and t["ended"] <= at:
            pass
        elif t["started"] is not None and t["started"] <= at:
            t.update(state="running", ended=None)
        else:
            t.update(state="pending", started=None, ended=None)
        out.append(t)
    return out


def backtest(key, points=40):
    """Replay a finished run: at evenly spaced moments, compare the ETA then with the time actually left."""
    with db() as c:
        run = dict(c.execute("SELECT * FROM runs WHERE id=?", (run_key(key),)).fetchone() or {})
        if not run:
            raise ValueError("轮次不存在")
        tasks = [dict(x) for x in c.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY rowid", (key,))]
        priors, params, limits = eta_inputs(c, key)
        forecasts = [dict(x) for x in c.execute("SELECT * FROM eta_forecasts WHERE run_id=? ORDER BY at", (key,))]
    ends = [t["ended"] for t in tasks if t["ended"] is not None and t["started"] is not None]
    if run["status"] in ACTIVE or len(ends) < 10:
        raise ValueError("只能回放已结束且至少有 10 个计时任务的轮次")
    finish = max(ends)
    rows = []
    for k in range(1, points):
        at = run["started"] + (finish - run["started"]) * k / points
        eta = estimate(dict(run, status="running", heartbeat=at), replay(tasks, at), at, priors, params, limits)
        if eta["seconds"] and finish > at:
            rows.append((finish - at, eta["seconds"][0], eta["point"], eta["seconds"][1]))
    if not rows:
        raise ValueError("回放中没有可用的 ETA")
    mid = lambda v: sorted(v)[len(v) // 2] if v else None
    miss = lambda guess, truth: abs(math.log(guess / truth))
    result = {"run": run["id"], "name": run["name"], "points": len(rows), "tasks": len(tasks),
              "coverage": sum(lo <= truth <= hi for truth, lo, _, hi in rows) / len(rows),
              "error": math.exp(mid([miss(p, truth) for truth, _, p, _ in rows if p > 0])),
              "width": mid([hi / lo for _, lo, _, hi in rows if lo > 0]), "forecasts": None}
    # Model forecasts made during the run, scored against the actual finish and against the algorithm at the same moment.
    scored = []
    for f in forecasts:
        if f["at"] < finish:
            algo = estimate(dict(run, status="running", heartbeat=f["at"]), replay(tasks, f["at"]), f["at"], priors, params, limits)
            scored.append((finish - f["at"], f, algo.get("point")))
    if scored:
        ranged = [(truth, f) for truth, f, _ in scored if f["low"] and f["high"]]
        result["forecasts"] = {
            "count": len(scored), "error": math.exp(mid([miss(f["remaining"], truth) for truth, f, _ in scored])),
            "algorithm_error": math.exp(mid([miss(p, truth) for truth, _, p in scored if p])) if any(p for *_, p in scored) else None,
            "coverage": sum(f["low"] <= truth <= f["high"] for truth, f in ranged) / len(ranged) if ranged else None}
    return result


def snapshot(key):
    with db() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_key(key),)).fetchone()
        if not row:
            raise ValueError("轮次不存在")
        run = dict(row)
        tasks = [dict(x) for x in c.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY rowid", (key,))]
        priors, params, limits = eta_inputs(c, key)
        forecast = c.execute("SELECT * FROM eta_forecasts WHERE run_id=? ORDER BY at DESC LIMIT 1", (key,)).fetchone()
    now = time.time()
    counts = {s: sum(t["state"] == s for t in tasks) for s in ("succeeded", "failed", "running", "pending")}
    if run["total"] is not None:
        counts["pending"] = run["total"] - counts["succeeded"] - counts["failed"] - counts["running"]
    groups, durations = {}, {}
    for t in tasks:
        g = groups.setdefault(t["group_name"], {"name": t["group_name"], "registered": 0, "succeeded": 0, "failed": 0, "running": 0, "pending": 0})
        g["registered"] += 1
        g[t["state"]] += 1
        if t["state"] == "succeeded" and t["started"] is not None and t["ended"] is not None and t["ended"] > t["started"]:
            durations.setdefault(t["group_name"], []).append(t["ended"] - t["started"])
    for name, g in groups.items():
        values = durations.get(name, [])
        g.update(samples=len(values), median=quantile(values, 0.5) if values else None,
                 p25=quantile(values, 0.25) if values else None, p75=quantile(values, 0.75) if values else None)
    # Recent pace only describes this run so far; it is not used by the ETA.
    ends = sorted(t["ended"] for t in tasks if t["state"] in TERMINAL and t["ended"] is not None)[-30:]
    span = ends[-1] - ends[0] if len(ends) > 1 else 0
    version = [len(tasks), counts["succeeded"], counts["failed"], counts["running"],
               max((t["started"] or 0 for t in tasks), default=0), max((t["ended"] or 0 for t in tasks), default=0),
               sum(t["attempt"] for t in tasks)]
    run.update(counts=counts, groups=sorted(groups.values(), key=lambda g: natural(g["name"])),
               throughput={"per_hour": (len(ends) - 1) / span * 3600 if span > 0 else None, "samples": len(ends)},
               concurrency=counts["running"], tasks_version=":".join(str(round(x, 3)) for x in version),
               active=[t for t in tasks if t["state"] == "running"][:30],
               failures=[t for t in tasks if t["state"] == "failed"][:30],
               elapsed=max(0, (run["ended"] or now) - run["started"]),
               stale=run["status"] in ACTIVE and now - run["heartbeat"] > 12,
               server_time=now, eta=estimate(run, tasks, now, priors, params, limits))
    run["eta"]["forecast"] = forecast_view(forecast, run["status"] in ACTIVE and not run["stale"], now)
    try:
        with open(run["log"], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8000))
            run["log_tail"] = f.read(8000).decode("utf-8", errors="replace")
    except OSError:
        run["log_tail"] = "尚无日志或日志不可读"
    return run


def task_table(key):
    """Every task of one run, compact for the task matrix: groups in natural order; within a group,
    started tasks by start time (so cells fill in execution order), then unstarted tasks by natural ID."""
    with db() as c:
        row = c.execute("SELECT total,planned FROM runs WHERE id=?", (run_key(key),)).fetchone()
        if not row:
            raise ValueError("轮次不存在")
        tasks = [dict(x) for x in c.execute("SELECT * FROM tasks WHERE run_id=?", (key,))]
    names = sorted({t["group_name"] for t in tasks}, key=natural)
    index = {name: i for i, name in enumerate(names)}
    def order(t):
        when = t["started"] or t["ended"]
        return (index[t["group_name"]], 0, when, []) if when else (index[t["group_name"]], 1, 0, natural(t["id"]))
    tasks.sort(key=order)
    stamp = lambda x: None if x is None else round(x, 2)
    return {"total": row["total"], "planned": bool(row["planned"]), "groups": names,
            "tasks": [[t["id"], index[t["group_name"]], STATE_CODES[t["state"]], stamp(t["started"]),
                       stamp(t["ended"]), t["attempt"]] for t in tasks]}


def list_runs(limit=50):
    now = time.time()
    with db() as c:
        rows = [dict(x) for x in c.execute("""
          SELECT r.id, r.name, r.status, r.started, r.ended, r.demo, r.total, r.heartbeat,
                 coalesce(sum(t.state='succeeded'),0) AS succeeded, coalesce(sum(t.state='failed'),0) AS failed,
                 coalesce(sum(t.state='running'),0) AS running
          FROM runs r LEFT JOIN tasks t ON t.run_id=r.id
          GROUP BY r.id ORDER BY r.started DESC LIMIT ?""", (limit,))]
    for r in rows:
        r["stale"] = r["status"] in ACTIVE and now - r["heartbeat"] > 12
    return rows


def duration(seconds):
    seconds = max(0, round(seconds))
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{h} 小时 {m:02d} 分" if h else f"{m} 分 {s:02d} 秒" if m else f"{s} 秒"


STATUS_TEXT = {"starting": "正在启动", "running": "运行中", "observing": "正在观察已有进程", "completed": "全部成功",
               "completed_with_errors": "已结束 · 含失败任务", "failed": "实验进程失败",
               "unverified": "进程已结束 · 计数未核实", "exit_unknown": "已观察到进程退出 · 退出码未知",
               "monitor_error": "监测程序异常 · 进程状态待核验"}


def brief(run):
    """A few lines for a quick human or agent check; the dashboard has the details."""
    c, total = run["counts"], run["total"]
    ended = c["succeeded"] + c["failed"]
    state = STATUS_TEXT.get(run["status"], run["status"])
    if run["stale"]:
        state += " · 监测信号中断，待核验"
    progress = f"{ended} / {total}（{100 * ended / total:.1f}%）" if total else f"{ended} / 总量未知"
    lines = [f"{run['name']} [{run['id']}]", "状态：" + state,
             f"进度：{progress} · 成功 {c['succeeded']} · 失败 {c['failed']} · 运行中 {c['running']} · 等待 {c['pending']}",
             ("最后确认的耗时：" + duration(max(0, run["heartbeat"] - run["started"])) if run["stale"]
              else "已运行：" + duration(run["elapsed"]))]
    eta, live = run["eta"], run["status"] in ACTIVE and not run["stale"]
    span = lambda a, b: duration(a) if duration(a) == duration(b) else duration(a) + "–" + duration(b)
    ago = lambda x: "刚刚" if x < 60 else duration(x) + "前"
    f = eta.get("forecast")
    lead = bool(f and f["fresh"])
    if lead:
        extra = f"（研判区间 {span(f['low'], f['high'])}）" if f["low"] is not None and f["high"] is not None else ""
        lines.append(f"预计剩余（模型研判，{ago(f['age'])}）：约 {duration(f['remaining'])}{extra}")
    if eta["seconds"] and live:
        lines.append(f"{'算法估计' if lead else '预计剩余'}（{eta['label']}）：约 {duration(eta['point'])}"
                     f"（大概率 {span(*eta['seconds'])}）")
        if eta.get("latest"):
            lines.append(f"最迟：约 {duration(eta['latest'])}（所有剩余任务跑满单项时限）")
    elif run["status"] in ACTIVE:
        lines.append(("算法估计" if lead else "预计剩余") + "：暂无法估计（" + eta["reason"] + "）")
    if f and not lead:
        lines.append(f"上次模型研判：{ago(f['age'])}，当时估计剩余 {duration(f['finish'] - f['at'])}；{f['stale_reason']}")
    if live:
        # What a model needs for its next forecast during a silent follow-up.
        if eta.get("limit"):
            lines.append(eta["limit"])
        left = [g for g in run["groups"] if g["running"] + g["pending"]]
        parts = [f"{g['name']} {g['running'] + g['pending']} 项（" + (f"中位 {duration(g['median'])}" if g["median"] else "尚无样本") + "）"
                 for g in left[:12]]
        if len(left) > 12:
            parts[-1] += "等"
        unregistered = (total or 0) - sum(g["registered"] for g in run["groups"])
        if unregistered > 0:
            parts.append(f"尚未登记 {unregistered} 项")
        if parts:
            lines.append("剩余任务：" + "；".join(parts))
    if run["throughput"]["per_hour"]:
        lines.append(f"近期节奏：约 {run['throughput']['per_hour']:.1f} 项/小时（最近 {run['throughput']['samples']} 项结束时间）")
    if run["exit_code"] is not None:
        lines.append(f"退出码：{run['exit_code']}")
    return "\n".join(lines)


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
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
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
                    return self.send(200, {"service": "experiment-progress", "version": VERSION, "pid": os.getpid()})
                if path == "/api/runs":
                    return self.send(200, list_runs())
                if path.startswith("/api/run/") and path.endswith("/tasks"):
                    return self.send(200, task_table(path.removeprefix("/api/run/").removesuffix("/tasks")))
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


def live_server():
    try:
        meta = json.loads((DATA / "server.json").read_text())
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", meta["base"]):
            return None
        req = urllib.request.Request(meta["base"] + "/api/health", headers={"Authorization": "Bearer " + meta["token"]})
        with urllib.request.urlopen(req, timeout=1) as response:
            health = json.load(response)
        if health.get("service") == "experiment-progress":
            return dict(meta, version=health.get("version", 1))
    except (OSError, ValueError, KeyError):
        return None


def stop_server(pid):
    """Stop only the dashboard service; experiment and _supervise processes are never touched."""
    p = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
    if p.returncode or SCRIPT.name not in p.stdout or " _serve" not in p.stdout:
        raise RuntimeError("server.json 中的 PID 不是本面板服务，未做任何操作")
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=5).stdout.strip()
        if not state or state.startswith("Z"):
            return
        time.sleep(0.1)
    raise RuntimeError("旧面板服务未退出，PID " + str(pid))


def ensure_server(restart=False):
    """Reuse the running dashboard; restart swaps only the web service and keeps the link."""
    with db():
        pass
    with open(DATA / "server.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        meta = live_server()
        if meta and restart:
            stop_server(meta["pid"])
        if not meta or restart:
            reuse = restart and (DATA / "server.json").exists()
            with open(DATA / "service.log", "ab") as log:
                child = subprocess.Popen([sys.executable, str(SCRIPT), "_serve"] + (["--reuse-address"] if reuse else []),
                                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         env=dict(os.environ, EXPERIMENT_PROGRESS_HOME=str(DATA)))
            for _ in range(50):
                time.sleep(0.1)
                meta = live_server()
                if meta and meta["pid"] == child.pid:
                    break
            else:
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
                     args.estimate_seconds, args.estimate_source, args.task_limit_seconds, args.task_limit_source)
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
    p.add_argument("--restart", action="store_true", help="只替换面板服务以载入新页面，保留原链接；不影响实验进程")
    for name in ("launch", "attach"):
        p = sub.add_parser(name)
        p.add_argument("--name", required=True)
        p.add_argument("--cwd", default=os.getcwd())
        p.add_argument("--total", type=int)
        p.add_argument("--plan")
        p.add_argument("--estimate-seconds", type=float, help="有依据的单次实验初始耗时预估，单位秒")
        p.add_argument("--estimate-source", help="初始耗时预估的依据，与 --estimate-seconds 同时使用")
        p.add_argument("--task-limit-seconds", type=float, help="单项时限（如求解器 TimeLimit），单位秒")
        p.add_argument("--task-limit-source", help="单项时限的出处，与 --task-limit-seconds 同时使用")
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
    p.add_argument("--brief", action="store_true", help="只输出几行中文摘要")
    p = sub.add_parser("list", help="列出最近的实验轮次")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("backtest", help="回放已结束轮次，检验 ETA 当时的准确度（只读）")
    p.add_argument("--run", help="省略时回放最近 20 个已结束轮次")
    p = sub.add_parser("set-limit", help="录入单项时限，ETA 不会超过它")
    p.add_argument("--run", required=True)
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--group", default="*", help="省略时适用于所有组")
    p = sub.add_parser("forecast", help="写入模型在静默跟进中对剩余时间的研判")
    p.add_argument("--run", required=True)
    p.add_argument("--remaining", type=float, required=True, help="剩余秒数")
    p.add_argument("--low", type=float, help="研判区间下端，秒")
    p.add_argument("--high", type=float, help="研判区间上端，秒")
    p.add_argument("--basis", required=True, help="研判依据，一两句话")
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
            url = ensure_server(args.restart)
            print(url)
            if args.open:
                webbrowser.open(url)
        elif args.action == "event":
            report(args.task, args.state, args.group, args.run, args.started_at, args.ended_at)
        elif args.action == "status":
            run = snapshot(args.run)
            print(brief(run) if args.brief else json.dumps(run, ensure_ascii=False, indent=2))
        elif args.action == "list":
            for r in list_runs(max(1, args.limit)):
                ended = r["succeeded"] + r["failed"]
                state = STATUS_TEXT.get(r["status"], r["status"]) + (" · 信号中断" if r["stale"] else "")
                when = time.strftime("%m-%d %H:%M", time.localtime(r["started"]))
                print(f"{r['id']}  {when}  {ended}/{r['total'] if r['total'] is not None else '?'}  {state}  {'[模拟] ' * r['demo']}{r['name']}")
        elif args.action == "backtest":
            keys = [args.run] if args.run else [r["id"] for r in list_runs(20) if r["status"] not in ACTIVE]
            for key in keys:
                try:
                    b = backtest(key)
                except ValueError as exc:
                    if args.run:
                        raise
                    print(f"{key}  跳过：{exc}")
                    continue
                print(f"{b['run']}  {b['tasks']} 项 · {b['points']} 个时点：实际剩余落在大概率范围内 {b['coverage']:.0%}，"
                      f"算法点估计中位偏差 ×{b['error']:.2f}，范围上下界比中位 ×{b['width']:.2f}  {b['name']}")
                f = b["forecasts"]
                if f:
                    print(f"    模型研判 {f['count']} 次：中位偏差 ×{f['error']:.2f}"
                          + (f"；同一时刻算法 ×{f['algorithm_error']:.2f}" if f["algorithm_error"] else "")
                          + (f"；实际剩余落在研判区间内 {f['coverage']:.0%}" if f["coverage"] is not None else ""))
        elif args.action == "set-limit":
            set_limit(args.run, args.seconds, args.source, args.group)
        elif args.action == "forecast":
            record_forecast(args.run, args.remaining, args.basis, args.low, args.high)
        elif args.action == "set-prior":
            set_prior(args.run, args.seconds, args.source, args.group)
        elif args.action == "_serve":
            serve(args.reuse_address)
        elif args.action == "_supervise":
            command = args.command[1:] if args.command and args.command[0] == "--" else args.command
            supervise(args.run, command, args.pid, args.identity)
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
