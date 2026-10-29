"""Shared helpers: free port, temp home, HTTP with error capture, cleanup."""
import json, os, pathlib, shutil, socket, sys, tempfile, time, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "teamboard"
PYTHON = sys.executable

# macOS 托管 runner 会设 HTTP_PROXY/HTTPS_PROXY：uranium 会把 127.0.0.1 也交给代理，
# 于是「服务起来了」变成「请求超时」。所有 loopback 请求显式禁用代理。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def temp_home():
    d = tempfile.mkdtemp(prefix="tb-test-")
    return pathlib.Path(d)


def env_for(home, port, catalog=True):
    e = dict(os.environ)
    e["TEAMBOARD_HOME"] = str(home)
    e["TEAMBOARD_DB"] = str(home / "board.db")
    e["TEAMBOARD_PORT"] = str(port)
    if catalog:
        e["TEAMBOARD_CATALOG"] = str(ROOT / "tests" / "fixtures" / "models.json")
    return e


def call(base, method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"raw": raw.decode("utf-8", "replace")}


def wait_health(base, timeout=15):
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        try:
            code, body = call(base, "GET", "/api/health", timeout=1)
            if code == 200 and body.get("app") == "teamboard":
                return body
            last = f"http {code}: {str(body)[:200]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.15)
    raise RuntimeError(f"board never became healthy on {base} (last: {last})")


def kill_board(base):
    """Stop the board we started, using the pid it reports."""
    try:
        _, body = call(base, "GET", "/api/health", timeout=1)
        pid = body.get("pid")
        if pid:
            os.kill(pid, 15)
    except Exception:
        pass
