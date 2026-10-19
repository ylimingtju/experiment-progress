"""Run with python3 test_monitor.py. Uses an isolated temporary data directory."""
import concurrent.futures
import heapq
import json
import math
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def check():
    with tempfile.TemporaryDirectory(prefix="experiment-progress-check-") as folder:
        os.environ["EXPERIMENT_PROGRESS_HOME"] = folder
        import experiment_progress as m
        root = Path(folder)
        plan = [{"id": str(i), "group": "A"} for i in range(6)]
        now = time.time()
        key = m.create_run("test", root, plan=plan, started=now-100)
        for i, duration in enumerate((10, 12, 14)):
            m.report(str(i), "succeeded", run_id=key, started_at=now-60, ended_at=now-60+duration)
        m.report("3", "running", run_id=key, started_at=now-2)
        snapshot = m.snapshot(key)
        assert snapshot["counts"] == {"succeeded": 3, "failed": 0, "running": 1, "pending": 2}
        with m.db() as c:
            tasks = [dict(t) for t in c.execute("SELECT * FROM tasks WHERE run_id=?", (key,))]
            run = dict(c.execute("SELECT * FROM runs WHERE id=?", (key,)).fetchone())
        near = lambda got, want: all(abs(x - y) <= 0.3 for x, y in zip(got, want))
        eta = m.estimate(run, tasks, now)
        assert near(eta["seconds"], [18.8, 61.6]) and near([eta["point"]], [34.1]) and eta["label"] == "按组估计", eta
        assert m.estimate(run, tasks, now+20)["seconds"] is None
        # A single timed success must unlock estimates, including groups not yet started.
        early = [tasks[0], tasks[3], tasks[4], dict(tasks[5], group_name="B")]
        eta = m.estimate(dict(run, total=4), early, now)
        assert near(eta["seconds"], [8.0, 98.4]) and eta["label"] == "跨组粗估", eta
        same_group = [t for t in early if t["group_name"] == "A"]
        eta = m.estimate(dict(run, total=3), same_group, now)
        assert near(eta["seconds"], [4.2, 78.0]) and eta["label"] == "初步估计", eta
        two_samples = [tasks[0], tasks[1], tasks[3]]
        assert m.estimate(dict(run, total=3), two_samples, now)["label"] == "初步估计"
        cold = [tasks[3], tasks[4]]
        assert m.estimate(dict(run, total=2), cold, now)["seconds"] is None
        priors = {"*": {"seconds": 20, "source": "fixture historical duration"}}
        eta = m.estimate(dict(run, total=2), cold, now, priors)
        assert near(eta["seconds"], [19, 76]) and eta["label"] == "初始预估", eta
        assert "fixture historical duration" in eta["reason"]
        # Failed, untimed and zero-duration events cannot become timing samples.
        for bad_sample in (dict(tasks[0], state="failed"), dict(tasks[0], started=None),
                           dict(tasks[0], ended=tasks[0]["started"])):
            assert m.estimate(dict(run, total=3), [bad_sample] + cold, now)["seconds"] is None
        eta = m.estimate(dict(run, total=6, planned=False),
                         [tasks[0], dict(tasks[3], group_name="B")], now)
        assert eta["seconds"] and eta["label"] == "跨组粗估", eta
        # A task past every sample of its group widens the estimate without hiding it or depending on row order.
        overrun_run = dict(run, heartbeat=now+20)
        slow = tasks[:3] + [dict(tasks[3], started=now-2), dict(tasks[4], state="running", started=now-12), tasks[5]]
        eta = m.estimate(overrun_run, slow, now+20)
        assert near(eta["seconds"], [8.4, 63.1]) and eta["label"] == "动态粗估" and "overrun" in eta["methods"], eta
        assert m.estimate(overrun_run, list(reversed(slow)), now+20) == eta
        def fixture(rows, total=None):
            """rows: (id, group, state, seconds or running age); finished tasks end before `now`."""
            out = []
            for i, g, state, x in rows:
                start = now - x if state == "running" else (None if state == "pending" else now - 500)
                out.append({"run_id": key, "id": i, "group_name": g, "state": state, "started": start,
                            "ended": start + x if state in ("succeeded", "failed") else None, "attempt": 1})
            return dict(run, total=total or len(out)), out
        # Fast failures lower the expected time of the tasks still waiting in that group.
        done = [(f"s{i}", "F", "succeeded", 10) for i in range(6)]
        waiting = [("r", "F", "running", 0.1)] + [(f"p{i}", "F", "pending", 0) for i in range(10)]
        with_fail = m.estimate(*fixture(done + [(f"f{i}", "F", "failed", 0.1) for i in range(6)] + waiting), now)
        without = m.estimate(*fixture(done + waiting), now)
        assert with_fail["point"] < 0.7 * without["point"], (with_fail, without)
        # Groups with no samples: extrapolate along a size number in the name, else borrow the closest name.
        sized = [(f"a{i}", "BO/n=10", "succeeded", 1) for i in range(3)] + [(f"b{i}", "BO/n=20", "succeeded", 8) for i in range(3)]
        eta = m.estimate(*fixture(sized + [("r", "BO/n=10", "running", 0.1)] + [(f"c{i}", "BO/n=30", "pending", 0) for i in range(3)]), now)
        assert "scaled" in eta["methods"] and eta["label"] == "规模外推" and 70 < eta["point"] < 95, eta
        eta = m.estimate(*fixture([(f"a{i}", "BO/n=10", "succeeded", 1) for i in range(3)] + [(f"b{i}", "BO/n=30", "succeeded", 100) for i in range(3)]
                                  + [("r", "BO/n=10", "running", 0.1), ("c0", "control/n=30", "pending", 0), ("c1", "control/n=30", "pending", 0)]), now)
        assert eta["label"] == "相近组借用" and "control/n=30 ← BO/n=30" in eta["reason"] and 180 < eta["point"] < 220, eta
        # A running task's remaining time is conditioned on how long it has already run.
        eta = m.estimate(*fixture([(f"s{i}", "G", "succeeded", d) for i, d in enumerate((10, 10, 10, 30, 30, 30))] + [("r", "G", "running", 15)]), now)
        assert near([eta["point"]], [15]), eta
        # Tasks sharing a parameter value (ID segment or plan params) run alike.
        def by_instance(slow_inst, params=None):
            rows = [(f"cfg{c}/inst{x}/s0", "G", "succeeded", 40 if x == "A" else 10) for c in range(3) for x in "AB"]
            rows += [("cfg9/instB/s1", "G", "running", 0.1), (f"cfg9/inst{slow_inst}/s0", "G", "pending", 0)]
            return m.estimate(*fixture(rows), now, params=params)
        slow_next, fast_next = by_instance("A"), by_instance("B")
        # The pending task differs only in instance (A took 40 s, B 10 s); at least half of that gap should show.
        assert "params" in slow_next["methods"] and slow_next["point"] - fast_next["point"] > 15, (slow_next, fast_next)
        opaque = {f"cfg{c}/inst{x}/s{s}": {"inst": x} for c in range(10) for x in "AB" for s in range(2)}
        alias = {i: f"t{n}" for n, i in enumerate(sorted(opaque))}
        renamed = lambda e: m.estimate(*fixture([(alias[i], g, st, d) for i, g, st, d in e]), now,
                                       params={alias[i]: opaque[i] for i, *_ in e})
        rows = [(f"cfg{c}/inst{x}/s0", "G", "succeeded", 40 if x == "A" else 10) for c in range(3) for x in "AB"]
        assert renamed(rows + [("cfg9/instB/s1", "G", "running", 0.1), ("cfg9/instA/s0", "G", "pending", 0)])["point"] - \
            renamed(rows + [("cfg9/instB/s1", "G", "running", 0.1), ("cfg9/instB/s0", "G", "pending", 0)])["point"] > 15
        # Seeded replay of a whole synthetic run: the range should usually contain the time actually left.
        rng, slots, synthetic = random.Random(7), [now - 5000] * 2, []
        for j in range(160):
            g = ("n=10", "n=20", "n=30", "n=40")[j // 10 % 4]
            start = heapq.heappop(slots)
            seconds = {"n=10": 2, "n=20": 9, "n=30": 25, "n=40": 60}[g] * math.exp(rng.gauss(0, 0.35))
            synthetic.append({"run_id": key, "id": f"cfg{j // 40}/inst{j % 10}", "group_name": g, "state": "succeeded",
                              "started": start, "ended": start + seconds, "attempt": 1})
            heapq.heappush(slots, start + seconds)
        finish, hits, errors = max(t["ended"] for t in synthetic), 0, []
        for k in range(1, 40):
            at = now - 5000 + (finish - now + 5000) * k / 40
            eta = m.estimate(dict(run, total=160, heartbeat=at), m.replay(synthetic, at), at)
            hits += eta["seconds"][0] <= finish - at <= eta["seconds"][1]
            errors.append(abs(math.log(eta["point"] / (finish - at))))
        assert hits / 39 >= 0.7 and sorted(errors)[19] < math.log(1.25), (hits, sorted(errors)[19])
        m.set_prior(key, 20, "fixture historical duration")
        with m.db() as c:
            stored = dict(c.execute("SELECT * FROM eta_priors WHERE run_id=?", (key,)).fetchone())
        assert stored["seconds"] == 20 and stored["source"] == "fixture historical duration"
        for seconds, source in ((0, "invalid"), (float("nan"), "invalid"), (20, "")):
            try:
                m.set_prior(key, seconds, source)
                raise AssertionError("accepted invalid prior")
            except ValueError:
                pass
        m.report("0", "succeeded", run_id=key)
        assert m.snapshot(key)["counts"]["succeeded"] == 3
        try:
            m.report("outside-plan", "running", run_id=key)
            raise AssertionError("accepted an unknown task")
        except ValueError:
            pass
        m.report("4", "running", run_id=key)
        m.report("4", "failed", run_id=key)
        m.report("4", "running", run_id=key)
        retried = next(t for t in m.snapshot(key)["active"] if t["id"] == "4")
        assert retried["attempt"] == 2 and m.snapshot(key)["counts"]["failed"] == 0
        snap = m.snapshot(key)
        assert snap["groups"][0]["samples"] == 3 and snap["groups"][0]["median"] == 12, snap["groups"]
        assert (snap["groups"][0]["p25"], snap["groups"][0]["p75"]) == (11, 13)
        assert snap["throughput"]["samples"] == 3 and snap["concurrency"] == snap["counts"]["running"]
        # Matrix order never uses SQLite rowid (INSERT OR REPLACE changes it): natural group order,
        # started tasks by start time, then unstarted tasks by natural ID.
        mixed = m.create_run("order", root, plan=[{"id": i, "group": g} for i, g in (("c-10", "n=10"), ("c-3", "n=10"), ("c-2", "n=10"), ("c-1", "n=2"))])
        m.report("c-10", "running", run_id=mixed, started_at=now - 5)
        m.report("c-10", "succeeded", run_id=mixed)
        m.report("c-2", "running", run_id=mixed)
        table = m.task_table(mixed)
        assert table["groups"] == ["n=2", "n=10"] and [t[0] for t in table["tasks"]] == ["c-1", "c-10", "c-2", "c-3"], table
        assert table["tasks"][2][2] == m.STATE_CODES["running"]
        version = m.snapshot(mixed)["tasks_version"]
        m.report("c-2", "succeeded", run_id=mixed)
        assert m.snapshot(mixed)["tasks_version"] != version
        # Processes started with an older copy keep writing positional SQL; the schema must stay compatible.
        with m.db() as c:
            c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("0123456789ab", "legacy", folder, 1, 0, 0, "running", now, None, time.time(), None, None, "x.log"))
            c.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?)",
                      ("0123456789ab", "legacy-task", "default", "succeeded", now - 2, now, 1))
        legacy = m.snapshot("0123456789ab")
        assert legacy["counts"]["succeeded"] == 1 and "legacy" in m.brief(legacy)
        with_params = m.create_run("params", root, plan=[{"id": "x1", "group": "G", "params": {"inst": "A", "seed": 1}}, {"id": "x2"}])
        with m.db() as c:
            assert [tuple(r) for r in c.execute("SELECT id, params FROM task_params WHERE run_id=?", (with_params,))] == [("x1", '{"inst": "A", "seed": 1}')]
        for bad in ({"inst": ["A"]}, "inst=A", {"": 1}):
            try:
                m.create_run("bad params", root, plan=[{"id": "x", "params": bad}])
                raise AssertionError("accepted invalid params")
            except ValueError:
                pass
        replayed = m.create_run("replayed", root, plan=[{"id": f"cfg{i % 3}/inst{i % 4}", "group": "G"} for i in range(12)], started=now - 200)
        for i in range(12):
            m.report(f"cfg{i % 3}/inst{i % 4}", "succeeded", run_id=replayed, started_at=now - 190 + 15 * i, ended_at=now - 180 + 15 * i + i % 4)
        m.finish(replayed, 0)
        result = m.backtest(replayed)
        assert result["points"] > 20 and 0 <= result["coverage"] <= 1 and result["error"] >= 1, result
        printed = subprocess.check_output([sys.executable, str(m.SCRIPT), "backtest", "--run", replayed], text=True)
        assert replayed in printed and "落在范围内" in printed, printed
        second = m.create_run("parallel", root, total=20)
        def worker(i):
            with m.task(str(i), run_id=second):
                time.sleep(0.01)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(20)))
        assert m.snapshot(second)["counts"]["succeeded"] == 20
        m.finish(second, 0)
        assert m.snapshot(second)["status"] == "completed"
        empty = m.create_run("no events", root, total=4, estimate_seconds=20, estimate_source="fixture pilot")
        m.report("first", "running", run_id=empty)
        assert m.snapshot(empty)["eta"]["label"] == "初始预估"
        subprocess.check_call([sys.executable, str(m.SCRIPT), "set-prior", "--run", empty,
                               "--seconds", "30", "--source", "fixture group pilot", "--group", "default"])
        assert "fixture group pilot" in m.snapshot(empty)["eta"]["reason"]
        m.finish(empty, 0)
        assert m.snapshot(empty)["status"] == "unverified"
        unknown = m.create_run("unknown", root)
        assert m.snapshot(unknown)["total"] is None
        assert m.snapshot(unknown)["eta"]["seconds"] is None
        partial = m.create_run("partial failure", root, total=2)
        m.report("one", "succeeded", run_id=partial)
        m.report("two", "failed", run_id=partial)
        m.finish(partial, 0)
        assert m.snapshot(partial)["status"] == "completed_with_errors"
        with m.db() as c:
            c.execute("UPDATE runs SET heartbeat=? WHERE id=?", (now-30, key))
        assert m.snapshot(key)["stale"]
        assert m.snapshot(key)["eta"]["seconds"] is None
        url = m.ensure_server()
        assert m.ensure_server() == url
        meta = json.loads((root/"server.json").read_text())
        try:
            req = urllib.request.Request(meta["base"]+"/api/runs", headers={"Authorization":"Bearer "+meta["token"]})
            with urllib.request.urlopen(req) as response:
                assert len(json.load(response)) == 9
            # Updating only the web service can preserve existing dashboard links.
            os.kill(meta["pid"], signal.SIGTERM)
            for _ in range(50):
                try:
                    urllib.request.urlopen(req, timeout=0.2).close()
                except OSError:
                    break
                time.sleep(0.1)
            replacement = subprocess.Popen([sys.executable, str(m.SCRIPT), "_serve", "--reuse-address"],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                updated = json.loads((root/"server.json").read_text())
                if updated["pid"] == replacement.pid:
                    break
                time.sleep(0.1)
            meta = updated
            assert meta["pid"] == replacement.pid and m.ensure_server() == url
            # dashboard --restart swaps only the web service and keeps the link.
            assert m.ensure_server(restart=True) == url
            replacement.wait(timeout=5)
            meta = json.loads((root/"server.json").read_text())
            assert meta["pid"] != replacement.pid
            with urllib.request.urlopen(urllib.request.Request(meta["base"]+"/api/run/"+mixed+"/tasks", headers={"Authorization":"Bearer "+meta["token"]})) as response:
                assert [t[0] for t in json.load(response)["tasks"]] == ["c-1", "c-10", "c-2", "c-3"]
                assert "img-src 'self' data:" in response.headers["Content-Security-Policy"]
            with urllib.request.urlopen(req) as response:
                assert len(json.load(response)) == 9
            for path, headers in (("/api/runs", {}), ("/api/run/"+mixed+"/tasks", {}),
                                  ("/api/runs", {"Authorization":"Bearer "+meta["token"],"Origin":"https://example.com"})):
                try:
                    urllib.request.urlopen(urllib.request.Request(meta["base"]+path, headers=headers))
                    raise AssertionError("unauthorized request accepted")
                except urllib.error.HTTPError as exc:
                    assert exc.code == 403
            command = [sys.executable, str(m.SCRIPT), "launch", "--name", "subprocess", "--cwd", folder, "--total", "1", "--estimate-seconds", "1", "--estimate-source", "fixture pilot", "--", sys.executable, "-c",
                       "import sys,time;sys.path.insert(0,"+repr(str(m.SCRIPT.parent))+");from experiment_progress import task\nwith task('one'): time.sleep(0.25)\nprint('finished')"]
            launched = json.loads(subprocess.check_output(command, text=True))
            for _ in range(70):
                state = m.snapshot(launched["run_id"])
                if state["ended"]:
                    break
                time.sleep(0.1)
            assert state["status"] == "completed", state
            assert "finished" in state["log_tail"]
            with m.db() as c:
                assert c.execute("SELECT seconds FROM eta_priors WHERE run_id=?", (launched["run_id"],)).fetchone()[0] == 1
            failed_command = command[:]
            failed_command[-1] = "raise SystemExit(7)"
            failed = json.loads(subprocess.check_output(failed_command, text=True))
            for _ in range(70):
                state = m.snapshot(failed["run_id"])
                if state["ended"]:
                    break
                time.sleep(0.1)
            assert state["status"] == "failed" and state["exit_code"] == 7
            summary = subprocess.check_output([sys.executable, str(m.SCRIPT), "status", "--run", partial, "--brief"], text=True)
            assert "已结束 · 含失败任务" in summary and "失败 1" in summary, summary
            assert len(subprocess.check_output([sys.executable, str(m.SCRIPT), "list", "--limit", "3"], text=True).strip().splitlines()) == 3
            child = subprocess.Popen([sys.executable,"-c","import time; time.sleep(3)"])
            try:
                attached = json.loads(subprocess.check_output([sys.executable,str(m.SCRIPT),"attach","--name","existing process","--pid",str(child.pid),"--cwd",folder],text=True))
                child.wait(timeout=8)
                for _ in range(70):
                    state = m.snapshot(attached["run_id"])
                    if state["ended"]:
                        break
                    time.sleep(0.1)
                assert state["status"] == "exit_unknown" and state["exit_code"] is None
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait()
        finally:
            os.kill(meta["pid"], signal.SIGTERM)
        print("PASS: counts, retries, concurrent writes, legacy SQL, task order, group stats, ETA (grouped, prior, overrun, failures, scaling, similar names, parameters, replay), stale state, HTTP access, restart, brief/list, detached launch, failure, attach")


if __name__ == "__main__":
    check()
