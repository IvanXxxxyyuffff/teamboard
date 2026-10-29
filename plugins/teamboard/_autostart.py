"""Self-start for the board service (open-source build only).

The OSS plugin ships without a launch agent, so the MCP server boots the board
on demand: probe `/api/health` on 8787, then the next free port.

The local development build does not import this module — it runs the board
under a LaunchAgent with a fixed DB, and `mcp_server.py` keeps the plain
`TEAMBOARD_URL` default. `scripts/build_oss.sh` swaps that one line in.

Environment:
  TEAMBOARD_URL     full base URL of an already-running board (skips autostart)
  TEAMBOARD_HOME    data directory for db/log/lock (default <plugin>/.teamboard)
  TEAMBOARD_PORT    preferred port (default 8787; falls back to the next free one)
  TEAMBOARD_PYTHON  interpreter used to spawn the board (default sys.executable)
"""
import json, os, socket, subprocess, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(HERE, "server.py")
HOME = os.environ.get("TEAMBOARD_HOME") or os.path.join(HERE, ".teamboard")


def _log(msg):
    print(f"[teamboard] {msg}", file=sys.stderr, flush=True)


def _health(base, timeout=0.6):
    """True only if a TeamBoard board answers there (not just any server)."""
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=timeout) as r:
            return json.loads(r.read() or b"{}").get("app") == "teamboard"
    except Exception:
        return False


def _port_free(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _spawn(port):
    os.makedirs(HOME, exist_ok=True)
    env = dict(os.environ)
    env["TEAMBOARD_DB"] = env.get("TEAMBOARD_DB") or os.path.join(HOME, "board.db")
    env["TEAMBOARD_HOME"] = HOME
    py = os.environ.get("TEAMBOARD_PYTHON") or sys.executable
    logf = open(os.path.join(HOME, "server.log"), "ab")
    subprocess.Popen([py, SERVER_PY, "--port", str(port)], cwd=HERE, env=env,
                     stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                     start_new_session=True, close_fds=True)


def boot(budget=10.0):
    """Return the base URL of a live board, starting one if needed.

    Safe under concurrency: two MCP processes racing on the same port see each
    other's health check, and only the loser of the bind retries.
    """
    given = (os.environ.get("TEAMBOARD_URL") or "").rstrip("/")
    if given:
        return given
    want = int(os.environ.get("TEAMBOARD_PORT") or 8787)
    started = time.time()
    for port in range(want, want + 15):
        base = f"http://127.0.0.1:{port}"
        if _health(base):
            return base
        if not _port_free(port):
            continue                      # someone else owns it, try the next
        _spawn(port)
        for _ in range(50):               # up to ~10s for first boot
            time.sleep(0.2)
            if _health(base):
                _log(f"board started on {base} (data: {HOME})")
                return base
            if time.time() - started > budget:
                break
    _log(f"board did not come up; see {os.path.join(HOME, 'server.log')}")
    return f"http://127.0.0.1:{want}"     # let the tools report the real error
