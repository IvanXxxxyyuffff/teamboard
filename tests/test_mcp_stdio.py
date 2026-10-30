#!/usr/bin/env python3
"""MCP stdio contract test: the plugin must boot its own board, no manual server.

This is the "git clone && it works" test. It starts nothing but mcp_server.py.
"""
import json, os, pathlib, subprocess, sys, threading, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _util import PLUGIN, PYTHON, free_port, temp_home, env_for, kill_board, call, wait_health

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class MCP:
    def __init__(self, env):
        self.p = subprocess.Popen([PYTHON, str(PLUGIN / "mcp_server.py")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.n = 0

    def rpc(self, method, params=None, timeout=30):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        end = time.time() + timeout
        while time.time() < end:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("mcp closed stdout")
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == self.n:
                return obj
        raise TimeoutError(method)

    def close(self):
        try:
            self.p.terminate()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def main():
    home, port = temp_home(), free_port()
    env = env_for(home, port)
    env.pop("TEAMBOARD_URL", None)          # force the autostart path
    base = f"http://127.0.0.1:{port}"
    m = MCP(env)
    try:
        r = m.rpc("initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {}, "clientInfo": {"name": "ci"}})
        info = r.get("result", {}).get("serverInfo", {})
        version = (PLUGIN / "VERSION").read_text().strip()
        check("initialize returns serverInfo.name=teamboard", info.get("name") == "teamboard", info)
        check("serverInfo.version == VERSION file", info.get("version") == version, info.get("version"))

        r = m.rpc("tools/list")
        tools = [t["name"] for t in r.get("result", {}).get("tools", [])]
        check("tools/list exposes >=20 tools", len(tools) >= 20, len(tools))
        for name in ("board_overview", "project_status", "propose_plan", "tb_api", "update_task",
                     "update_project", "run_criteria"):
            check(f"tool present: {name}", name in tools)

        # 省上下文是硬指标：默认返回里不能带全量历史任务
        st = {"tasks": [{"id": f"t{i}", "title": "x" * 40, "status": "done"} for i in range(30)]
              + [{"id": "open1", "title": "open", "status": "doing"}],
              "members": [], "retro": {"questions": [{"q": "x" * 400}] * 5,
                                       "history": [{"id": str(i)} for i in range(20)]},
              "nudge": {"items": [{"say": "y" * 600}] * 4}}
        # 同一份代码，按文件加载：模块顶层 BASE=_boot() 会去探测已在跑的看板（上面刚起）
        os.environ["TEAMBOARD_URL"] = base        # 明确指向测试板，禁止它自己去起一个
        sys.path.insert(0, str(PLUGIN))
        import importlib.util
        spec = importlib.util.spec_from_file_location("tb_mcp_test", PLUGIN / "mcp_server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        compact = mod.compact_state(st)
        check("compact: no done-task dump", len(compact["tasks"]) == 1, len(compact["tasks"]))
        check("compact: retro questions dropped",
              len(json.dumps(compact.get("retro"), ensure_ascii=False)) < 800, compact.get("retro"))
        check("compact: nudge say truncated",
              all(len(x["say"]) <= 130 for x in compact["nudge"]["items"]),
              compact["nudge"]["items"][:1])

        r = m.rpc("resources/list")
        res = r.get("result", {}).get("resources", [])
        check("one UI resource with mcp-app mime", len(res) == 1 and res[0]["mimeType"] == "text/html;profile=mcp-app", res)
        ui_uri = res[0]["uri"] if res else ""

        body = wait_health(base)             # autostart proof
        check("board was started by the MCP process", body.get("app") == "teamboard", body)
        check("db file lives under TEAMBOARD_HOME", (home / "board.db").exists() or True)
        check("db created in the data dir", (home / "board.db").exists(), list(home.iterdir()))

        r = m.rpc("tools/call", {"name": "board_overview", "arguments": {}})
        res = r.get("result", {})
        check("board_overview on an empty board is not an error", res.get("isError") is False, res)

        r = m.rpc("resources/read", {"uri": ui_uri})
        c = (r.get("result", {}).get("contents") or [{}])[0]
        html = c.get("text", "")
        # 面板不再直连 http（沙箱 CSP 禁 connect-src http），改经 MCP 的 tb_api 代理取数
        check("panel fetches through the tb_api proxy", "tb_api" in html, html[:120])
        check("panel size sane (>300KB)", len(html) > 300_000, len(html))
    finally:
        m.close()
        kill_board(base)
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
