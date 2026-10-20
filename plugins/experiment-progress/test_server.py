"""Run with python3 test_server.py. Talks to server.py over stdio with an isolated EXPERIMENT_PROGRESS_HOME."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent


def check():
    with tempfile.TemporaryDirectory(prefix="experiment-track-check-") as folder:
        env = dict(os.environ, EXPERIMENT_PROGRESS_HOME=folder)
        skill = Path(os.environ.get("EXPERIMENT_PROGRESS_SKILL") or Path.home() / ".codex/skills/experiment-progress").expanduser()
        sys.path.insert(0, str(skill / "scripts"))
        os.environ["EXPERIMENT_PROGRESS_HOME"] = folder
        import experiment_progress as ep
        now = time.time()
        key = ep.create_run("轨道测试", folder, plan=[{"id": f"t{i}", "group": "G"} for i in range(5)], started=now - 60)
        for i in range(3):
            ep.report(f"t{i}", "succeeded", run_id=key, started_at=now - 50 + i * 10, ended_at=now - 42 + i * 10)
        ep.report("t3", "running", run_id=key)
        server = subprocess.Popen([sys.executable, "-B", str(HERE / "server.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        seq = iter(range(1, 1000))

        def ask(method, params=None):
            i = next(seq)
            server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
            server.stdin.flush()
            reply = json.loads(server.stdout.readline())
            assert reply["id"] == i, reply
            return reply

        try:
            init = ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})["result"]
            assert init["serverInfo"]["name"] == "experiment-progress" and "tools" in init["capabilities"], init
            server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            tools = {t["name"]: t for t in ask("tools/list")["result"]["tools"]}
            track = tools["experiment.track"]["_meta"]
            assert track["ui"]["visibility"] == ["model", "app"] and track["ui"]["resourceUri"].startswith("ui://"), track
            assert tools["experiment.observatory"]["_meta"]["openai/ui"]["entrypoints"] == [{"type": "global"}, {"type": "thread"}]
            assert all(tools[n]["_meta"]["ui"]["visibility"] == ["app"] for n in ("experiment.snapshot", "experiment.tasks", "experiment.runs"))
            uris = [r["uri"] for r in ask("resources/list")["result"]["resources"]]
            for uri in uris:
                content = ask("resources/read", {"uri": uri})["result"]["contents"][0]
                assert content["mimeType"] == "text/html;profile=mcp-app" and "EP_BRIDGE" in content["text"], uri
                assert "__RUNNER_PNG__" not in content["text"] and "__BRIDGE_JS__" not in content["text"], uri
            card = ask("resources/read", {"uri": track["ui"]["resourceUri"]})["result"]["contents"][0]
            assert "data:image/png;base64," in card["text"] and card["_meta"]["openai/widgetShowCodexWidgetInline"] is True
            result = ask("tools/call", {"name": "experiment.track", "arguments": {}})["result"]
            run = result["structuredContent"]["run"]
            assert result["structuredContent"]["run_id"] == key and run["counts"]["succeeded"] == 3 and run["eta"]["point"], run
            assert "轨道测试" in result["content"][0]["text"] and "进度：3 / 5" in result["content"][0]["text"], result
            full = ask("tools/call", {"name": "experiment.snapshot", "arguments": {"run_id": key, "full": True}})["result"]
            assert "log_tail" in full["structuredContent"] and full["structuredContent"]["active"], full
            table = ask("tools/call", {"name": "experiment.tasks", "arguments": {"run_id": key}})["result"]["structuredContent"]
            assert table["total"] == 5 and len(table["tasks"]) == 5, table
            runs = ask("tools/call", {"name": "experiment.runs"})["result"]["structuredContent"]["runs"]
            assert [r["id"] for r in runs] == [key], runs
            bad = ask("tools/call", {"name": "experiment.track", "arguments": {"run_id": "nope"}})["result"]
            assert bad["isError"] and "无效" in bad["content"][0]["text"], bad
            assert ask("tools/call", {"name": "experiment.nope"})["result"]["isError"]
            assert ask("nope/method")["error"]["code"] == -32601
            assert ask("resources/read", {"uri": "ui://nope"})["error"]["code"] == -32601
            assert ask("ping")["result"] == {}
        finally:
            server.stdin.close()
            server.wait(timeout=5)
        err = server.stderr.read()
        assert not err.strip(), err
        print("PASS: handshake, tools/_meta entrypoints, resources (bridge + sprite inlined), track/snapshot/tasks/runs, errors")


if __name__ == "__main__":
    check()
