"""Run with python3 test_monitor.py. Uses an isolated temporary data directory."""
import concurrent.futures
import json
import os
from pathlib import Path
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
        assert m.estimate(run, tasks, now)["seconds"] == [31, 37]
        assert m.estimate(run, tasks, now+20)["seconds"] is None
        # A single timed success must unlock estimates, including groups not yet started.
        early = [tasks[0], tasks[3], tasks[4], dict(tasks[5], group_name="B")]
        eta = m.estimate(dict(run, total=4), early, now)
        assert eta["seconds"] == [13, 58] and eta["label"] == "跨组粗估", eta
        same_group = [t for t in early if t["group_name"] == "A"]
        eta = m.estimate(dict(run, total=3), same_group, now)
        assert eta["seconds"] == [8, 38] and eta["label"] == "初步估计", eta
        two_samples = [tasks[0], tasks[1], tasks[3]]
        assert m.estimate(dict(run, total=3), two_samples, now)["label"] == "初步估计"
        cold = [tasks[3], tasks[4]]
        assert m.estimate(dict(run, total=2), cold, now)["seconds"] is None
        priors = {"*": {"seconds": 20, "source": "fixture historical duration"}}
        eta = m.estimate(dict(run, total=2), cold, now, priors)
        assert eta["seconds"] == [18, 78] and eta["label"] == "初始预估", eta
        assert "fixture historical duration" in eta["reason"]
        # Failed, untimed and zero-duration events cannot become timing samples.
        for bad_sample in (dict(tasks[0], state="failed"), dict(tasks[0], started=None),
                           dict(tasks[0], ended=tasks[0]["started"])):
            assert m.estimate(dict(run, total=3), [bad_sample] + cold, now)["seconds"] is None
        eta = m.estimate(dict(run, total=6, planned=False),
                         [tasks[0], dict(tasks[3], group_name="B")], now)
        assert eta["seconds"] and eta["label"] == "跨组粗估", eta
        # A slow active task widens the estimate without hiding it or depending on row order.
        overrun_run = dict(run, heartbeat=now+20)
        slow = tasks[:3] + [dict(tasks[3], started=now-2), dict(tasks[4], state="running", started=now-12), tasks[5]]
        eta = m.estimate(overrun_run, slow, now+20)
        assert eta["seconds"] == [48, 96] and eta["label"] == "动态粗估", eta
        assert m.estimate(overrun_run, list(reversed(slow)), now+20)["seconds"] == eta["seconds"]
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
                assert len(json.load(response)) == 5
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
            with urllib.request.urlopen(req) as response:
                assert len(json.load(response)) == 5
            for headers in ({}, {"Authorization":"Bearer "+meta["token"],"Origin":"https://example.com"}):
                try:
                    urllib.request.urlopen(urllib.request.Request(meta["base"]+"/api/runs", headers=headers))
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
        print("PASS: counts, retries, concurrent writes, early/grouped/prior/overrun ETA, stale state, HTTP access, detached launch, failure, attach")


if __name__ == "__main__":
    check()
