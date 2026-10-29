#!/usr/bin/env python3
"""
TeamBoard v2 — 项目看板 / 团队状态 / 交付物清单 / 进度报告
零依赖（Python 标准库 + SQLite）。启动：python3 server.py --port 8787
"""
import argparse, glob, json, os, re, sqlite3, subprocess, sys, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
UI_DIR = os.path.join(BASE, "ui")          # 插件包里的 MCP 面板；单机开发时退回 static/
HOME = os.environ.get("TEAMBOARD_HOME") or os.path.expanduser("~/.teamboard")
_HOME = os.environ.get("TEAMBOARD_HOME", "").rstrip("/")
DB_PATH = (os.environ.get("TEAMBOARD_DB")
           or (_HOME + "/board.db" if _HOME else os.path.join(BASE, "board.db")))
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
CATALOG = os.environ.get("TEAMBOARD_CATALOG",
                         os.path.expanduser("~/.codex/magpie-desktop-models.json"))
try:
    with open(os.path.join(BASE, "VERSION"), encoding="utf-8") as _f:
        VERSION = _f.read().strip() or "0.0.0"
except OSError:
    VERSION = "2.1.0"

PROVIDER_GROUP = {
    "gpt": "OpenAI",
    "auto": "routing group",
    "sensenova": "SenseNova",
    "workbuddy": "workbuddy 网关",
    "agnes": "agnes",
    "cline": "Cline",
    "trae": "Trae CN",
}
GROUP_ORDER = {"OpenAI": 0, "workbuddy 网关": 1, "SenseNova": 2,
               "routing group": 3, "Trae CN": 4, "agnes": 5, "Cline": 6}
SKIP_SLUGS = {"codex-auto-review", "gpt-reserve"}
STARTED_AT = time.time()


def provider_of(slug):
    head = slug.split("/")[0]
    if head.startswith("gpt"):
        return "OpenAI"
    for k, v in PROVIDER_GROUP.items():
        if head.startswith(k):
            return v
    return head


def model_catalog():
    """读 Codex 桌面版真实的模型目录，员工下拉框与它保持一致。"""
    try:
        with open(CATALOG, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for m in data.get("models", []):
        slug = m.get("slug")
        if not slug or slug in SKIP_SLUGS:
            continue
        if m.get("visibility") not in (None, "list"):
            continue
        levels = [{"effort": l.get("effort"), "desc": l.get("description", "")}
                  for l in (m.get("supported_reasoning_levels") or [])]
        out.append({"id": slug,
                    "name": m.get("display_name") or slug,
                    "group": provider_of(slug),
                    "default_effort": m.get("default_reasoning_level") or "",
                    "efforts": levels,
                    "multi_agent_effort": m.get("multi_agent_reasoning_effort") or ""})
    out.sort(key=lambda x: (GROUP_ORDER.get(x["group"], 90), x["name"].lower()))
    return out


def check_model(model):
    """模型必须是 Codex 真实目录里的 id；空字符串表示跟随当前会话模型。"""
    if not model:
        return None
    cat = model_catalog()
    if not cat:
        # 目录读不到时不能放行，否则会写进根本不存在的模型
        return ("读不到模型目录（TEAMBOARD_CATALOG 或 ~/.codex/magpie-desktop-models.json），暂时不能指定模型；留空＝跟随当前会话模型")
    if not any(m["id"] == model for m in cat):
        return f"未知模型：{model}（必须在 /api/models 的真实目录里）"
    return None


def check_effort(model, effort):
    """档位只能取该模型 supported_reasoning_levels 里真实存在的值。"""
    if not effort:
        return None
    if not model:
        return "档位必须绑定一个具体模型；跟随会话模型时请留空 effort"
    for m in model_catalog():
        if m["id"] == model:
            allowed = [e["effort"] for e in m["efforts"]]
            if not allowed:
                return f"模型 {model} 没有可调档位"
            if effort not in allowed:
                return f"模型 {model} 只支持档位：{', '.join(allowed)}"
            return None
    return f"未知模型：{model}（档位无法校验）"

COLUMNS = ["backlog", "doing", "review", "done", "blocked", "abandoned"]
COL_LABEL = {"backlog": "待办", "doing": "进行中", "review": "待验收",
             "done": "已完成", "blocked": "阻塞", "abandoned": "已放弃"}
HEARTBEAT_WINDOW = 300  # 心跳有效期：只有「没有进行中任务」时才用离线判定
TEAM_CAP = 4  # 默认员工上限（不含项目总监）；与执行器并发槽对齐；超过要走扩编审批

# 时间压力闸：剩余时间跌破这些比例就升级，critical 起强制总监复盘
PRESSURE_WATCH_LEFT = 0.50   # 剩余 ≤50% → 观察
PRESSURE_CRIT_LEFT = 0.30    # 剩余 ≤30% → 强制复盘（用户定的 30% 红线）
RETRO_REARM_SEC = {"critical": 3600, "overdue": 1800}   # 复盘批准后的有效期，过时重新要一轮
RETRO_BLOCKED_ACTIONS = ["加人（扩编）", "新计划审批", "导出交付报告"]

RETRO_QUESTIONS = [
    {"key": "speed", "q": "怎么提速：具体动作 + 每条预计省多少分钟",
     "hint": "≥3 条可执行动作；「加把劲 / 优化一下」不算"},
    {"key": "deadline", "q": "能否按期：能 / 不能，差多少小时，卡在哪条链",
     "hint": "要引用看板数字，别自我感觉良好"},
    {"key": "staffing", "q": "是否加人：几个、什么岗位、接谁手上的哪条任务、省多少",
     "hint": "不加也要写理由；加人必须先过复盘，否则 403"},
    {"key": "utilization", "q": "利用率：谁在摸鱼、摸多久、下一步怎么处置",
     "hint": "看板给了实测工时，编造会被对账"},
    {"key": "scope", "q": "砍范围：砍哪条任务/哪个验收项，目标是否要改",
     "hint": "不砍就写「不砍」+ 理由"},
    {"key": "risk", "q": "最大风险：最可能让目标失败的一件事 + 兜底方案",
     "hint": "一句话说清，别写套话"},
]

# 岗位一律中文；常见英文写法自动归一，避免看板上出现 PM / Dev / QA
ROLE_MAP = {
    "pm": "项目总监", "project manager": "项目总监", "product manager": "项目总监",
    "lead": "项目总监", "owner": "项目总监", "director": "项目总监",
    "research": "调研分析", "researcher": "调研分析", "analyst": "调研分析",
    "design": "方案设计", "designer": "方案设计", "ui": "方案设计", "ux": "方案设计",
    "dev": "执行实现", "developer": "执行实现", "engineer": "执行实现",
    "frontend": "前端开发", "backend": "后端开发", "fullstack": "全栈开发",
    "qa": "测试验收", "test": "测试验收", "tester": "测试验收",
    "ops": "运维部署", "writer": "内容运营", "copywriter": "内容运营",
    "coordinator": "项目统筹", "planner": "项目统筹",
}


def cn_role(role):
    r = (role or "").strip()
    return ROLE_MAP.get(r.lower(), r)

TASK_FIELDS = ("title", "detail", "role", "assignee_id", "status", "priority",
               "sort_order", "deliverable", "dod", "depends_on", "estimate",
               "progress", "blocked_reason", "milestone", "resource", "evidence",
               "due_at", "evidence_path", "abandoned_reason")
EVIDENCE = ("observed", "inferred", "assumed")


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@contextmanager
def db():
    """每次调用一个连接，用完必关。

    sqlite3.Connection 自身的上下文管理器只做 commit/rollback，不会 close；
    早先直接 return conn 导致每个请求泄漏一组 fd（db/wal/shm），
    跑几百次后内核回 EMFILE，全站 API 变成 "unable to open database file"。
    这里必须显式 close，否则看板会在运行一两天后整体挂掉。
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    # WAL 是写在库文件头里的持久设置，建库时设一次就够；
    # 每条连接都执行它等于每次都拿写锁，多请求并发时会撞 "database is locked"。
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:          # 事务语义：正常提交，异常回滚
            yield conn
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT DEFAULT '',
            lead TEXT DEFAULT '', deadline TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS members(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL, role TEXT DEFAULT '', kind TEXT DEFAULT 'human',
            model TEXT DEFAULT '', effort TEXT DEFAULT '',
            state TEXT DEFAULT 'idle', note TEXT DEFAULT '',
            capacity INTEGER DEFAULT 3, last_seen REAL DEFAULT 0,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL, detail TEXT DEFAULT '', role TEXT DEFAULT '',
            assignee_id TEXT REFERENCES members(id) ON DELETE SET NULL,
            status TEXT DEFAULT 'backlog', priority INTEGER DEFAULT 2,
            sort_order REAL DEFAULT 0, deliverable TEXT DEFAULT '', dod TEXT DEFAULT '',
            depends_on TEXT DEFAULT '', estimate REAL DEFAULT 1,
            progress INTEGER DEFAULT 0, blocked_reason TEXT DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, delivered_at TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS resource_leases(
            project_id TEXT NOT NULL, resource TEXT NOT NULL,
            holder_task TEXT DEFAULT '', holder_member TEXT DEFAULT '',
            until REAL DEFAULT 0, note TEXT DEFAULT '', created_at TEXT NOT NULL,
            PRIMARY KEY(project_id, resource));
        CREATE TABLE IF NOT EXISTS hypotheses(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT DEFAULT '',
            claim TEXT NOT NULL, claim_key TEXT NOT NULL, command TEXT DEFAULT '',
            verdict TEXT DEFAULT 'open', note TEXT DEFAULT '', by TEXT DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_hyp_key ON hypotheses(project_id, claim_key);
        CREATE TABLE IF NOT EXISTS criteria(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, text TEXT NOT NULL,
            cmd TEXT DEFAULT '', status TEXT DEFAULT 'unknown', last_run TEXT DEFAULT '',
            output TEXT DEFAULT '', sort_order REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
            actor TEXT DEFAULT '', task_id TEXT DEFAULT '', kind TEXT NOT NULL,
            payload TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS retros(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, round INTEGER DEFAULT 1,
            trigger TEXT DEFAULT '', level TEXT DEFAULT '', left_pct INTEGER DEFAULT 0,
            slip_hours REAL DEFAULT 0, status TEXT DEFAULT 'pending',
            answers TEXT DEFAULT '{}', note TEXT DEFAULT '', by TEXT DEFAULT '',
            required_at REAL DEFAULT 0, submitted_at REAL DEFAULT 0,
            decided_at REAL DEFAULT 0, decided_by TEXT DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_retro_proj ON retros(project_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_members_project ON members(project_id);
        CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id DESC);
        CREATE TABLE IF NOT EXISTS task_sessions(
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            member_id TEXT DEFAULT '',
            started_at REAL NOT NULL,
            ended_at REAL,
            seconds INTEGER DEFAULT 0,
            source TEXT DEFAULT 'status');
        CREATE INDEX IF NOT EXISTS idx_sessions_task ON task_sessions(task_id, started_at DESC);
        CREATE TABLE IF NOT EXISTS approvals(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            kind TEXT DEFAULT 'plan', summary TEXT DEFAULT '', payload TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending', note TEXT DEFAULT '',
            created_at TEXT NOT NULL, decided_at TEXT DEFAULT '');
        """)
        # 轻量迁移：老库补列
        for tbl, col, ddl in [
            ("projects", "deadline", "TEXT DEFAULT ''"),
            ("members", "capacity", "INTEGER DEFAULT 3"),
            ("members", "model", "TEXT DEFAULT ''"),
            ("members", "effort", "TEXT DEFAULT ''"),
            ("tasks", "role", "TEXT DEFAULT ''"),
            ("tasks", "deliverable", "TEXT DEFAULT ''"),
            ("tasks", "dod", "TEXT DEFAULT ''"),
            ("tasks", "depends_on", "TEXT DEFAULT ''"),
            ("tasks", "estimate", "REAL DEFAULT 1"),
            ("tasks", "progress", "INTEGER DEFAULT 0"),
            ("tasks", "delivered_at", "TEXT DEFAULT ''"),
            ("tasks", "time_spent", "INTEGER DEFAULT 0"),
            ("tasks", "doing_since", "REAL DEFAULT 0"),
            ("tasks", "milestone", "TEXT DEFAULT ''"),
            ("tasks", "resource", "TEXT DEFAULT ''"),
            ("tasks", "evidence", "TEXT DEFAULT ''"),
            ("tasks", "due_at", "REAL DEFAULT 0"),        # 排期：这条任务最晚什么时候要交
            ("tasks", "paused_at", "REAL DEFAULT 0"),     # 等设备/等审批：暂停时段不计入工时
            ("tasks", "paused_sec", "REAL DEFAULT 0"),
            ("tasks", "evidence_path", "TEXT DEFAULT ''"),   # 证据落在哪个文件/目录
            ("tasks", "abandoned_reason", "TEXT DEFAULT ''"),# 放弃原因，换人接手时最值钱
            ("members", "is_lead", "INTEGER DEFAULT 0"),  # 总监不计入员工编制
            ("projects", "goal_criteria", "TEXT DEFAULT ''"),
            ("projects", "workdir", "TEXT DEFAULT ''"),
            ("projects", "committed_at", "REAL DEFAULT 0"),   # 当前承诺的登记时刻（改期即重算）
            ("projects", "priority_ratio", "TEXT DEFAULT ''"),  # P1/P2/P3 配额，总监在计划里填，看板按它切
            ("members", "model_actual", "TEXT DEFAULT ''"),
            ("members", "duty_until", "REAL DEFAULT 0"),
            ("members", "thread_id", "TEXT DEFAULT ''"),   # Codex 会话 id：点击员工能跳过去看他做什么
            # 员工编制：工位号是「人」的稳定身份，会话只是临时坐进来执行的那一段
            ("members", "seat_no", "INTEGER DEFAULT 0"),       # 工位号，发出去就不再变
            ("members", "roster_state", "TEXT DEFAULT 'active'"),  # active / vacant / swapping
            ("members", "bound_at", "REAL DEFAULT 0"),          # 当前会话何时接管这个工位
            ("members", "bind_count", "INTEGER DEFAULT 1"),     # 接管过几次会话（换人不换工位）
        ]:
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")}
            if col not in have:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")
        # 编制台账：工位 ↔ 会话的绑定历史。子代理被关掉重开，台账能证明是同一把椅子。
        c.executescript("""
        CREATE TABLE IF NOT EXISTS member_threads(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            bound_at REAL NOT NULL,
            released_at REAL,
            reason TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_member_threads ON member_threads(member_id, bound_at);
        CREATE TABLE IF NOT EXISTS staff_requests(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,              -- add 加人 / replace 换人 / remove 撤编
            member_id TEXT DEFAULT '',
            payload TEXT DEFAULT '{}',
            reason TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',   -- pending / approved / rejected
            created_at TEXT NOT NULL,
            decided_at TEXT DEFAULT '',
            decided_by TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_staff_requests ON staff_requests(project_id, status);
        """)
        # 一次性迁移：老数据里没标总监的，按名字/岗位认出来。总监不占员工编制。
        c.execute("UPDATE members SET is_lead=1 WHERE is_lead=0 AND"
                  " (name LIKE '%主线程%' OR role LIKE '%总调度%')")
        # 修历史遗留：删项目/删任务时没清干净的计时记录，否则这些 session 永远「正在计时」，
        # 把团队利用率算成有人在上班。
        c.execute("DELETE FROM task_sessions WHERE task_id NOT IN (SELECT id FROM tasks)")
        c.execute("DELETE FROM member_threads WHERE project_id NOT IN (SELECT id FROM projects)")
        c.execute("DELETE FROM staff_requests WHERE project_id NOT IN (SELECT id FROM projects)")
        # 迁移：老项目没有「承诺登记时刻」，用建项时刻兜底，行为与改动前一致
        for r in c.execute("SELECT id, created_at FROM projects"
                           " WHERE committed_at IS NULL OR committed_at=0").fetchall():
            try:
                ts = datetime.fromisoformat(
                    str(r["created_at"]).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = time.time()
            c.execute("UPDATE projects SET committed_at=? WHERE id=?", (ts, r["id"]))


def backfill_sessions():
    with db() as c:
        doing = c.execute("SELECT * FROM tasks WHERE status='doing'").fetchall()
        for t in doing:
            has = c.execute("SELECT 1 FROM task_sessions WHERE task_id=? AND ended_at IS NULL",
                            (t["id"],)).fetchone()
            if not has:
                since = float(t["doing_since"] or 0) or _epoch(t["updated_at"] or t["created_at"])
                open_session(c, t, start=since)


def log_event(c, pid, kind, actor="", task_id="", payload=""):
    c.execute("INSERT INTO events(project_id,actor,task_id,kind,payload,created_at)"
              " VALUES(?,?,?,?,?,?)", (pid, actor, task_id, kind, payload, now_iso()))


def new_id(p):
    return f"{p}_{uuid.uuid4().hex[:8]}"


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return time.time()


def project_workdir(pid):
    with db() as c:
        r = c.execute("SELECT workdir FROM projects WHERE id=?", (pid,)).fetchone()
    return (r["workdir"] if r and r["workdir"] else os.getcwd())


def parse_deliverables(spec):
    """把交付物描述拆成路径列表。

    支持：`a|b`、`a + b`、`a、b`、`{A,B,C}.md` 花括号展开；
    第二个 token 没目录时继承前一个 token 的目录（`work/x/A.md + B.md`）。
    """
    out, seen = [], set()
    for chunk in re.split(r"\s*(?:\||\+|、|;)\s*", spec or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        paths = [chunk]
        m = re.search(r"\{([^{}]+)\}", chunk)
        if m:
            paths = []
            for opt in m.group(1).split(","):
                paths.append(chunk[:m.start()] + opt.strip() + chunk[m.end():])
        for q in paths:
            q = q.strip().strip("`")
            if not q:
                continue
            if "/" not in q and out:
                q = os.path.join(os.path.dirname(out[-1]), q) if os.path.dirname(out[-1]) else q
            if q not in seen:
                seen.add(q)
                out.append(q)
    return out


def deliverable_exists(base, q):
    full = q if os.path.isabs(q) else os.path.join(base, q)
    if any(ch in q for ch in "*?["):
        return bool(glob.glob(full, recursive=True))
    return os.path.exists(full)


def check_deliverables(base, spec):
    """返回 (是否全部存在, 是否至少有一个存在, 缺失列表)。"""
    paths = parse_deliverables(spec)
    if not paths:
        return True, True, []
    flags = [deliverable_exists(base, q) for q in paths]
    return all(flags), any(flags), [q for q, ok in zip(paths, flags) if not ok]


def safe_int(v, default=0, lo=None, hi=None):
    """外部传入的数字一律先过这里：脏数据不能把整个 /api 打成 500。"""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def open_session(c, task, member_id="", start=None):
    """开始一段计时；先累计关闭残留会话，避免重复进入 doing 时丢工时。"""
    close_sessions(c, task["id"])
    c.execute("INSERT INTO task_sessions(id,task_id,project_id,member_id,started_at,source)"
              " VALUES(?,?,?,?,?,'status')",
              (new_id("ses"), task["id"], task["project_id"],
               member_id or task["assignee_id"] or "", start or time.time()))


def close_sessions(c, task_id, add_to_task=True):
    rows = c.execute("SELECT * FROM task_sessions WHERE task_id=? AND ended_at IS NULL",
                     (task_id,)).fetchall()
    total = 0
    for r in rows:
        secs = max(0, int(time.time() - float(r["started_at"])))
        c.execute("UPDATE task_sessions SET ended_at=?, seconds=? WHERE id=?",
                  (time.time(), secs, r["id"]))
        total += secs
    if add_to_task and total:
        c.execute("UPDATE tasks SET time_spent=COALESCE(time_spent,0)+? WHERE id=?",
                  (total, task_id))
    return total


# ------------------------------------------------------------------ approvals
def _approval_row(a):
    d = dict(a)
    try:
        d["payload"] = json.loads(d.get("payload") or "{}")
    except json.JSONDecodeError:
        d["payload"] = {}
    plan = d["payload"] or {}
    d["members"] = plan.get("members") or []
    d["tasks"] = plan.get("tasks") or []
    d["goal"] = plan.get("goal", "")
    d["goal_criteria"] = plan.get("goal_criteria", "")
    # 上限按「员工人数」算（不含项目总监），也不是只看计划里几个人
    try:
        with db() as c:
            cur = c.execute("SELECT COUNT(*) FROM members WHERE project_id=?"
                            " AND COALESCE(is_lead,0)=0",
                            (d["project_id"],)).fetchone()[0]
    except Exception:
        cur = 0
    d["current_members"] = cur
    d["total_after"] = cur + len(d["members"])
    d["over_cap"] = d["total_after"] > TEAM_CAP
    d["team_cap"] = TEAM_CAP
    return d


def get_approval(aid):
    with db() as c:
        a = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    return _approval_row(a) if a else None


def list_approvals(pid, status=""):
    q = "SELECT * FROM approvals WHERE project_id=?"
    args = [pid]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY created_at DESC"
    with db() as c:
        return [_approval_row(a) for a in c.execute(q, args)]


REQUIRED_TASK_FIELDS = ("title", "role", "assignee", "milestone", "deliverable", "dod")

# 优先级不是「重要程度」的形容词，是「同一时刻有人推」的配额。
# 全填 P1 等于没填（实测 22 条任务 18 条 P1），所以配比由总监在计划里写明，
# 看板按这个比例切实排优先级；比例不合规就是计划问题，不靠模型自觉。
DEFAULT_PRIORITY_RATIO = {"p1": 30, "p2": 40, "p3": 30}


def parse_priority_ratio(raw):
    """接受 {"p1":30,"p2":40} / "30/40/30" / "p1=30,p2=40"。返回 (ratio, error)。"""
    if raw in (None, "", {}, []):
        return None, "缺 priority_ratio"
    got = {}
    if isinstance(raw, dict):
        for k in ("p1", "p2", "p3"):
            v = raw.get(k)
            if v in (None, ""):
                continue
            try:
                got[k] = float(v)
            except (TypeError, ValueError):
                return None, f"priority_ratio.{k} 不是数字：{v!r}"
    else:
        nums = re.findall(r"\d+(?:\.\d+)?", str(raw))
        if len(nums) not in (2, 3):
            return None, "priority_ratio 要写成 3 个百分比，例如 30/40/30"
        for k, n in zip(("p1", "p2", "p3"), nums):
            got[k] = float(n)
    for k in ("p1", "p2", "p3"):
        got.setdefault(k, 100.0 - sum(v for kk, v in got.items() if kk != k))
    if any(v < 0 for v in got.values()):
        return None, "priority_ratio 不能有负数"
    total = sum(got.values())
    if abs(total - 100) > 0.5:
        return None, f"priority_ratio 三项之和必须是 100，现在是 {total:g}"
    return {k: int(round(v)) for k, v in got.items()}, ""


def ratio_text(r):
    return f"P1 {r['p1']}% · P2 {r['p2']}% · P3 {r['p3']}%"


def ratio_quota(ratio, n_open):
    """配比 → 实际条数（至少 1 条 P1，P1+P2 不超过在办总数）。"""
    if n_open <= 0:
        return 0, 0
    q1 = max(1, int(round(n_open * ratio["p1"] / 100.0)))
    q12 = max(q1, int(round(n_open * (ratio["p1"] + ratio["p2"]) / 100.0)))
    return min(q1, n_open), min(q12, n_open)


def validate_plan(pid, b, require_ratio=True):
    """计划必须写全：目标 + 每个人的岗位 + 每个任务的阶段/负责人/产出/验收/依赖。

    返回缺失清单；空列表代表可以提交审批。
    """
    problems = []
    goal = (b.get("goal") or "").strip()
    with db() as c:
        row = c.execute("SELECT goal FROM projects WHERE id=?", (pid,)).fetchone()
    if not goal and not (row and (row["goal"] or "").strip()):
        problems.append("缺少目标：plan.goal 要写清这个项目最终要达成什么")
    if not (b.get("goal_criteria") or "").strip():
        problems.append("缺少目标验收标准：plan.goal_criteria（怎样算达成）")
    if require_ratio:
        _r, _err = parse_priority_ratio(b.get("priority_ratio"))
        if _err:
            problems.append(
                "缺少优先级配比 priority_ratio：P1/P2/P3 各留多少配额，"
                '例如 {"p1": 30, "p2": 40, "p3": 30}。'
                "P1 是「现在必须有人在推」的那几条，不是「重要」的代名词。")

    members = b.get("members") or []
    if not members:
        problems.append("缺少成员：plan.members 至少 1 人")
    names = set()
    for i, m in enumerate(members, 1):
        n = (m.get("name") or "").strip()
        r = (m.get("role") or "").strip()
        if not n:
            problems.append(f"成员 #{i} 缺姓名")
        if not r:
            problems.append(f"成员 {n or '#'+str(i)} 缺岗位")
        elif not re.search(r"[\u4e00-\u9fff]", r):
            problems.append(f"成员 {n} 的岗位必须是中文：{r}")
        names.add(n)

    tasks = b.get("tasks") or []
    if not tasks:
        problems.append("缺少任务：plan.tasks 至少 1 条")
    titles = {(t.get("title") or "").strip() for t in tasks}
    for i, t in enumerate(tasks, 1):
        label = (t.get("title") or "").strip() or f"#{i}"
        for f in REQUIRED_TASK_FIELDS:
            if not (t.get(f) or "").strip():
                problems.append(f"任务「{label}」缺 {f}")
        if "depends_on_title" not in t:
            problems.append(f"任务「{label}」缺 depends_on_title（无依赖请显式写空字符串）")
        dep = (t.get("depends_on_title") or "").strip()
        if dep and dep not in titles:
            problems.append(f"任务「{label}」的依赖「{dep}」不在本计划里")
        an = (t.get("assignee") or "").strip()
        if an and an not in names:
            problems.append(f"任务「{label}」的负责人「{an}」不在本计划成员里")
        ev = (t.get("evidence") or "").strip()
        if ev and ev not in EVIDENCE:
            problems.append(f"任务「{label}」的证据等级只能是 {EVIDENCE}")
        # 提交时就按 add_task 的口径校验：不能让计划「审批能过、落库必失败」
        st = (t.get("status") or "backlog")
        if st not in COLUMNS:
            problems.append(f"任务「{label}」的 status 只能是 {COLUMNS}，收到 {st!r}")
        pr = t.get("priority")
        if pr not in (None, "", 1, 2, 3, "1", "2", "3"):
            problems.append(f"任务「{label}」的 priority 只能是 1/2/3，收到 {pr!r}")
        try:
            est = float(t.get("estimate") or 0)
        except (TypeError, ValueError):
            est = 0
        if est <= 0:
            problems.append(f"任务「{label}」缺用时规划 estimate（人时，>0）")
    # 资源独占：计划里两条 doing 任务抢同一台设备，落库时第二条必然被拒
    busy = {}
    for t in tasks:
        if (t.get("status") or "backlog") != "doing":
            continue
        r = (t.get("resource") or "").strip()
        if not r:
            continue
        if r in busy:
            problems.append(f"资源冲突：任务「{t.get('title')}」和「{busy[r]}」都标 doing 且都占 {r}")
        busy[r] = t.get("title")
    return problems


def plan_budget_warnings(pid, b):
    """计划工时 vs 可用工时：超了不硬拒（估时天然不准），但必须让用户看见。"""
    warns = []
    tasks = b.get("tasks") or []
    members = b.get("members") or []
    total = 0.0
    for t in tasks:
        try:
            total += float(t.get("estimate") or 0)
        except (TypeError, ValueError):
            pass
    with db() as c:
        row = c.execute("SELECT deadline FROM projects WHERE id=?", (pid,)).fetchone()
    hours_left = 0.0
    dl_raw = ((row["deadline"] if row else "") or "").strip()
    for fmt, eod in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%dT%H:%M", False),
                     ("%Y-%m-%d", True), ("%Y/%m/%d", True)):
        try:
            _dt = datetime.strptime(dl_raw, fmt)
            if eod:
                _dt = _dt.replace(hour=23, minute=59, second=59)
            hours_left = max(0.0, (_dt.astimezone().timestamp() - time.time()) / 3600.0)
            break
        except Exception:
            continue
    cap = hours_left * max(1, len(members)) * 0.85 if hours_left else 0.0
    if not dl_raw:
        warns.append("项目没有截止时间：无法做时间压力闸，也不会触发强制复盘。")
    elif hours_left <= 0:
        warns.append(f"项目已过截止时间（{dl_raw}）：这份计划不可能按期完成，先改 deadline 再谈排期。")
    elif total > cap:
        warns.append(f"工时超预算：计划合计 {total:.1f} 人时，剩余 {hours_left:.1f}h × {len(members)} 人 × 85% = {cap:.1f} 人时；"
                     f"要么砍任务、要么加人（>5 人需审批）、要么推迟截止。")
    n_by_role = {}
    for t in tasks:
        r = (t.get("role") or "").strip()
        if r:
            n_by_role[r] = n_by_role.get(r, 0) + 1
    if members and len(tasks) < len(members):
        warns.append(f"任务数 {len(tasks)} 少于人数 {len(members)}：有人会立刻空转，建议任务数 ≥ 人数。")
    # 自报优先级只有在「有分布」时才是信息。P1 是配额，配比由总监填、看板照着切。
    ratio, _rerr = parse_priority_ratio(b.get("priority_ratio"))
    ratio = ratio or DEFAULT_PRIORITY_RATIO
    n = len(tasks)
    if n:
        cnt = {1: 0, 2: 0, 3: 0}
        for t in tasks:
            try:
                k = int(t.get("priority") or 2)
            except (TypeError, ValueError):
                k = 2
            cnt[k if k in (1, 2, 3) else 2] += 1
        q1, q12 = ratio_quota(ratio, n)
        p1_share = 100.0 * cnt[1] / n
        if p1_share > ratio["p1"] + 10:
            warns.append(
                f"优先级配比不符：填的配比是 {ratio_text(ratio)}，"
                f"实际 {cnt[1]}/{n} 条标成 P1（{p1_share:.0f}%）。"
                f"按配比只能有 {q1} 条 P1 / {q12 - q1} 条 P2 / {n - q12} 条 P3，"
                f"多的会被看板按「关键路径 / 阻塞下游 / 超预算」压到 P2、P3。")
        elif cnt[1] == 0:
            warns.append(f"一条 P1 都没有：配比 {ratio_text(ratio)} 下应该有 {q1} 条 "
                         f"「现在就必须有人在推」的任务，全排成 P2/P3 等于没人推。")
    # 依赖为空 = 没有依赖图，卡点只能靠人肉判断
    no_dep = len([t for t in tasks
                  if not (t.get("depends_on_title") or "").strip()])
    if len(tasks) >= 4 and no_dep == len(tasks):
        warns.append(f"依赖全空（{len(tasks)}/{len(tasks)} 条没写 depends_on_title）："
                     f"看板画不出「卡在哪」，只能按人排队。至少给串行链上的任务写上游。")
    return warns


def create_approval(pid, b):
    gate = retro_gate(pid)
    if gate:
        return gate
    tasks = b.get("tasks") or []
    members = b.get("members") or []
    problems = validate_plan(pid, b)
    if problems:
        return {"error": "计划字段不全，先补全再提交审批", "missing": problems}
    warns = plan_budget_warnings(pid, b)
    ratio, _rerr = parse_priority_ratio(b.get("priority_ratio"))
    ratio = ratio or DEFAULT_PRIORITY_RATIO
    aid = new_id("apr")
    payload = {"members": members, "tasks": tasks, "note": b.get("note", ""),
               "warnings": warns,
               "goal": (b.get("goal") or "").strip(),
               "goal_criteria": (b.get("goal_criteria") or "").strip(),
               "priority_ratio": ratio}
    with db() as c:
        c.execute("INSERT INTO approvals(id,project_id,kind,summary,payload,status,created_at)"
                  " VALUES(?,?,?,?,?,?,?)",
                  (aid, pid, b.get("kind", "plan"), b.get("summary", ""),
                   json.dumps(payload, ensure_ascii=False), "pending", now_iso()))
        log_event(c, pid, "approval.requested", payload=b.get("summary", ""))
    out = get_approval(aid)
    if isinstance(out, dict):
        out["warnings"] = warns
    return out


def apply_plan(pid, plan):
    """审批通过后落库：成员 → 任务 → 依赖，**全部在一个事务里**。

    半套落库比整体失败更糟：用户看到「落库失败，已退回待审批」，改完再点一次批准，
    上一轮已经写进去的成员/任务还在，任务按标题无去重直接再来一份 —— 进度、工时、
    排期全被污染的重复任务带歪。所以这里不逐条提交，也不复用会自己开连接的
    add_member / add_task：一次 `with db()`，任何一条失败整笔回滚。
    """
    # 老审批单没有 priority_ratio：批准时不拦，按默认配比落库（新计划在提交时已强制填）
    problems = validate_plan(pid, plan, require_ratio=False)
    if problems:
        return problems
    ratio, _rerr = parse_priority_ratio(plan.get("priority_ratio"))
    ratio = ratio or DEFAULT_PRIORITY_RATIO
    name2id, idx2id, title2id = {}, {}, {}
    now = time.time()
    try:
        with db() as c:
            c.execute("UPDATE projects SET priority_ratio=? WHERE id=?",
                      (json.dumps(ratio), pid))
            for r in c.execute("SELECT id, name FROM members WHERE project_id=?", (pid,)):
                name2id[r["name"]] = r["id"]
            seat = (c.execute("SELECT COALESCE(MAX(seat_no),0) FROM members WHERE project_id=?",
                              (pid,)).fetchone()[0] or 0)
            for m in plan.get("members") or []:
                nm = (m.get("name") or "").strip()
                if nm and nm in name2id:      # 同名 = 同一个人，接管原工位，不新建
                    continue
                mid = new_id("mem")
                seat += 1
                tid0 = (m.get("thread_id") or "").strip()
                c.execute("INSERT INTO members(id,project_id,name,role,kind,model,effort,state,"
                          "capacity,last_seen,created_at,is_lead,thread_id,seat_no,roster_state,"
                          "bound_at,bind_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (mid, pid, nm or "未命名", cn_role(m.get("role", "")),
                           m.get("kind", "agent"), m.get("model", ""), m.get("effort", ""),
                           "idle", int(m.get("capacity", 3) or 3), now, now_iso(),
                           1 if m.get("is_lead") else 0, tid0, seat, "active",
                           now if tid0 else 0, 1 if tid0 else 0))
                if tid0:
                    c.execute("INSERT INTO member_threads(id,project_id,member_id,thread_id,"
                              "bound_at,reason) VALUES(?,?,?,?,?,?)",
                              (new_id("thr"), pid, mid, tid0, now, "计划批准时入职"))
                name2id[nm] = mid
                log_event(c, pid, "member.joined", actor=mid, payload=nm)

            mx = c.execute("SELECT COALESCE(MAX(sort_order),0) FROM tasks WHERE project_id=?",
                           (pid,)).fetchone()[0]
            for i, t in enumerate(plan.get("tasks") or []):
                tid = new_id("task")
                status = t.get("status") or "backlog"
                an = (t.get("assignee") or "").strip()
                mx += 100
                c.execute("""INSERT INTO tasks(id,project_id,title,detail,role,assignee_id,status,
                             priority,sort_order,deliverable,dod,depends_on,estimate,progress,
                             blocked_reason,created_at,updated_at,delivered_at,milestone,resource,
                             evidence,doing_since)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (tid, pid, (t.get("title") or "未命名任务"), t.get("detail", ""),
                           cn_role(t.get("role", "")), name2id.get(an) or None, status,
                           safe_int(t.get("priority", 2), 2, 1, 3), mx,
                           t.get("deliverable", ""), t.get("dod", ""), "",
                           max(0.1, safe_float(t.get("estimate", 1), 1)),
                           100 if status == "done" else safe_int(t.get("progress"), 0, 0, 100),
                           t.get("blocked_reason", ""), now_iso(), now_iso(),
                           now_iso() if status == "done" else "", t.get("milestone", ""),
                           t.get("resource", ""),
                           t.get("evidence", "") if t.get("evidence") in EVIDENCE else "",
                           now if status == "doing" else 0))
                idx2id[i] = tid
                title2id[(t.get("title") or "").strip()] = tid
                log_event(c, pid, "task.created", actor=name2id.get(an, ""), task_id=tid,
                          payload=t.get("title", ""))
                if status == "doing":
                    row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                    open_session(c, row)

            errors = []
            for i, t in enumerate(plan.get("tasks") or []):
                dep = (t.get("depends_on_title") or "").strip()
                tid = idx2id.get(i)
                if not dep or not tid:
                    continue
                did = title2id.get(dep)
                if did:
                    c.execute("UPDATE tasks SET depends_on=? WHERE id=?", (did, tid))
                else:
                    errors.append(f"任务 {t.get('title')} 的依赖「{dep}」不在本计划里")
            if errors:                    # 依赖缺失也算失败：整笔回滚，不留半套
                raise ValueError("；".join(errors))
    except Exception as e:                # noqa: BLE001
        return [f"计划落库失败（已整笔回滚，库里没有残留）：{e}"]
    return []


def decide_approval(aid, b):
    a = get_approval(aid)
    if not a:
        return None
    decision = b.get("decision")
    if decision not in ("approve", "reject"):
        return {"error": "decision 只能是 approve / reject"}
    if decision == "approve" and a["over_cap"] and not b.get("confirm_over_cap"):
        return {"error": f"批准后团队共 {a['total_after']} 人，超过默认上限 {TEAM_CAP} 人："
                         "勾选扩编确认（confirm_over_cap=true）后才能批准",
                "need_confirm": True, "total_after": a["total_after"]}

    # 抢占式改状态：并发双击只有一个能拿到 pending
    new_status = "approved" if decision == "approve" else "rejected"
    with db() as c:
        cur = c.execute("UPDATE approvals SET status=?, note=?, decided_at=?"
                        " WHERE id=? AND status='pending'",
                        (new_status, b.get("note", ""), now_iso(), aid))
        if cur.rowcount != 1:
            return {"error": "该审批已被处理，不能重复提交"}
        log_event(c, a["project_id"],
                  "approval.approved" if decision == "approve" else "approval.rejected",
                  payload=a.get("summary", ""))

    errors = []
    if decision == "approve":
        try:
            errors = apply_plan(a["project_id"], a["payload"])
        except Exception as e:            # noqa: BLE001
            # 任何异常都不能把审批留在 approved + 半套计划：退回 pending 让用户重试
            errors = [f"落库异常：{e}"]
        if errors:
            # 落库失败：退回 pending，别留半套班子还标已批准
            with db() as c:
                c.execute("UPDATE approvals SET status='pending', decided_at='' WHERE id=?", (aid,))
            return {"error": "计划落库失败，已退回待审批", "apply_errors": errors}
        payload = a["payload"]
        if payload.get("goal"):
            with db() as c:
                c.execute("UPDATE projects SET goal=?, goal_criteria=? WHERE id=?",
                          (payload["goal"], payload.get("goal_criteria", ""), a["project_id"]))
    out = get_approval(aid)
    out["apply_errors"] = errors
    return out


# ------------------------------------------------------------------ read
def compute_schedule(tasks, now, pool_lanes=1):
    """串行化排期：同一个人一次只干一件，跨人并行；并行分支取最晚的那条。

    - 剩余工作量 = estimate − 已烧净工时（time_spent + 当前段），不是「从 now 重新估一遍」；
    - 有依赖的任务必须等上游的预计交付时刻；
    - 同一个人手里的任务排队，排队时间体现在 start 上；
    - done 不参与排队，但它的完成时刻仍然是下游的起点。
    只算不写库：数据一变，排期就跟着变。
    """
    tmap = {t["id"]: t for t in tasks}
    lane_until, lane_prev = {}, {}
    # 没人认领的任务不是一个「隐形的超长工位」：按当前可用人数开并行槽，
    # 否则 3 条各 2h 的 backlog 会被排成 6h，把 ETA 和「注定迟到」判据一起抬歪。
    pool_lanes = max(1, int(pool_lanes or 1))
    pool_seq = [0]
    eta, cause, stack = {}, {}, set()

    def finish_of(t):
        tid = t["id"]
        if tid in eta:
            return eta[tid][1]
        if tid in stack:                     # 依赖成环：退化成按自身时长，别死循环
            return now + max(0.0, float(t["estimate"] or 1)) * 3600
        stack.add(tid)
        dep = t.get("depends_on") or ""
        dep_t = tmap.get(dep)
        dep_fin = finish_of(dep_t) if dep_t else 0.0
        stack.discard(tid)

        # done 和 abandoned 都不再消耗未来工时：前者交付了，后者已经不做了。
        # 把 abandoned 留在排期里会让 ETA 和关键路径虚高，指错「唯一的卡点」。
        if t["status"] in ("done", "abandoned"):
            eta[tid] = (0.0, 0.0)
            cause[tid] = None
            return 0.0

        left_h = max(0.0, float(t["estimate"] or 0)
                     - float(t.get("total_time") or 0) / 3600.0)
        dur = left_h * 3600.0
        key = t.get("assignee_id")
        if not key:
            key = f"__pool_{pool_seq[0] % pool_lanes}"
            pool_seq[0] += 1
        lane_at = lane_until.get(key, 0.0)
        start, why = max(((now, None), (lane_at, lane_prev.get(key)), (dep_fin, dep)),
                         key=lambda x: x[0])
        fin = start + dur
        eta[tid] = (start, fin)
        cause[tid] = why
        lane_until[key] = fin
        lane_prev[key] = tid
        return fin

    for t in tasks:
        finish_of(t)

    live = [t for t in tasks
            if t["status"] not in ("done", "abandoned") and t["id"] in eta]
    end = max(live, key=lambda t: eta[t["id"]][1], default=None)
    chain, cur, seen = [], end, set()
    while cur is not None and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur["id"])
        nxt = cause.get(cur["id"])
        cur = tmap.get(nxt) if nxt else None
    chain.reverse()
    return {"eta": eta, "cause": cause,
            "finish": eta[end["id"]][1] if end else 0.0,
            "end_id": end["id"] if end else "",
            "critical": set(chain), "chain": chain}



def claim_resource(pid, name, b):
    """设备租约：同一时刻只发给一个人，带截止时间；过期自动可抢。"""
    minutes = safe_int(b.get("minutes", 60), 60, 1, 1440)
    task_id = (b.get("task_id") or "").strip()
    member = (b.get("member") or "").strip()
    with db() as c:
        cur = c.execute("SELECT * FROM resource_leases WHERE project_id=? AND resource=?",
                        (pid, name)).fetchone()
        now = time.time()
        if cur and float(cur["until"] or 0) > now and cur["holder_task"] != task_id:
            if not b.get("force"):
                holder = c.execute("SELECT title FROM tasks WHERE id=?",
                                   (cur["holder_task"],)).fetchone()
                return {"error": f"设备 {name} 已被「{holder['title'] if holder else cur['holder_task']}」"
                                 f"占用，还剩 {int((float(cur['until'])-now)/60)} 分钟",
                        "field": "resource", "need_approval": True,
                        "holder": {"task_id": cur["holder_task"],
                                   "title": holder["title"] if holder else "",
                                   "until": cur["until"], "left_sec": int(float(cur["until"])-now)}}
        until = now + minutes * 60
        c.execute("INSERT INTO resource_leases(project_id,resource,holder_task,holder_member,until,note,created_at)"
                  " VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,resource) DO UPDATE SET"
                  " holder_task=excluded.holder_task, holder_member=excluded.holder_member,"
                  " until=excluded.until, note=excluded.note, created_at=excluded.created_at",
                  (pid, name, task_id, member, until, b.get("note", ""), now_iso()))
        log_event(c, pid, "resource.claim", actor=member or task_id,
                  task_id=task_id, payload=f"{name} {minutes}m")
    return {"ok": True, "resource": name, "holder_task": task_id,
            "until": until, "left_sec": minutes * 60}


def release_resource(pid, name, b):
    with db() as c:
        cur = c.execute("SELECT * FROM resource_leases WHERE project_id=? AND resource=?",
                        (pid, name)).fetchone()
        if not cur:
            return {"ok": True, "released": False}
        want = (b.get("task_id") or "").strip()
        if want and cur["holder_task"] != want and not b.get("force"):
            return {"error": "租约不在你手上，不能释放", "holder": cur["holder_task"]}
        c.execute("DELETE FROM resource_leases WHERE project_id=? AND resource=?", (pid, name))
        log_event(c, pid, "resource.release", actor=want, payload=name)
    return {"ok": True, "released": True}


def add_hypothesis(tid, b):
    claim = (b.get("claim") or "").strip()
    if not claim:
        return {"error": "假设不能为空", "field": "claim"}
    key = re.sub(r"[\s，。,.、:：]+", "", claim.lower())[:120]
    with db() as c:
        t = c.execute("SELECT project_id FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t:
            return None
        pid = t["project_id"]
        prev = c.execute("SELECT * FROM hypotheses WHERE project_id=? AND claim_key=?"
                         " AND verdict='refuted' ORDER BY created_at LIMIT 1", (pid, key)).fetchone()
        hid = new_id("hyp")
        c.execute("INSERT INTO hypotheses(id,project_id,task_id,claim,claim_key,command,verdict,note,by,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (hid, pid, tid, claim, key, b.get("command", ""),
                   b.get("verdict", "open") if b.get("verdict") in ("open", "confirmed", "refuted") else "open",
                   b.get("note", ""), b.get("by", ""), now_iso(), now_iso()))
        log_event(c, pid, "hypothesis.added", actor=b.get("by", ""), task_id=tid, payload=claim[:60])
    out = {"ok": True, "id": hid, "claim": claim, "verdict": b.get("verdict", "open")}
    if prev:
        out["dupe_of"] = prev["id"]
        out["warning"] = f"这条假设之前已被证伪（{prev['id']}），别再跑一遍"
    return out


def update_hypothesis(hid, b):
    with db() as c:
        h = c.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
        if not h:
            return None
        sets, vals = [], []
        for k in ("claim", "command", "verdict", "note"):
            if k in b:
                if k == "verdict" and b[k] not in ("open", "confirmed", "refuted"):
                    return {"error": "verdict 只能是 open/confirmed/refuted"}
                sets.append(f"{k}=?")
                vals.append(b[k])
        if not sets:
            return {"error": "nothing to update"}
        sets.append("updated_at=?")
        vals.append(now_iso())
        vals.append(hid)
        c.execute(f"UPDATE hypotheses SET {','.join(sets)} WHERE id=?", vals)
        log_event(c, h["project_id"], "hypothesis.updated", actor=b.get("by", ""),
                  task_id=h["task_id"], payload=b.get("verdict", ""))
    return {"ok": True, "id": hid}


def upsert_criteria(pid, b):
    """验收标准拆成可判定条目：每条一句话 + 一条命令 + 当前红绿。"""
    items = b.get("items") if "items" in b else [b]
    out = []
    with db() as c:
        for i, it in enumerate(items):
            text = (it.get("text") or "").strip()
            if not text:
                continue
            cid = (it.get("id") or new_id("crit"))
            row = c.execute("SELECT * FROM criteria WHERE id=?", (cid,)).fetchone()
            if row:
                c.execute("UPDATE criteria SET text=?, cmd=?, sort_order=? WHERE id=?",
                          (text, it.get("cmd", row["cmd"]), float(it.get("sort_order", row["sort_order"])), cid))
            else:
                c.execute("INSERT INTO criteria(id,project_id,text,cmd,status,sort_order)"
                          " VALUES(?,?,?,?,?,?)",
                          (cid, pid, text, it.get("cmd", ""), it.get("status", "unknown"),
                           float(it.get("sort_order", (i + 1) * 10))))
            out.append(cid)
        log_event(c, pid, "criteria.upsert", payload=f"{len(out)} 条")
    return {"ok": True, "ids": out}


def update_criterion(cid, b):
    with db() as c:
        r = c.execute("SELECT * FROM criteria WHERE id=?", (cid,)).fetchone()
        if not r:
            return None
        st = b.get("status", r["status"])
        if st not in ("unknown", "green", "red"):
            return {"error": "status 只能是 unknown/green/red"}
        # 绿色 = 已实测通过。没有实测输出就不许标绿，否则「验收全绿」是自评出来的
        if st == "green" and not (b.get("output") or "").strip():
            return {"error": "标绿必须带实测输出（output 传命令的原始回显摘要）；"
                             "没跑过就只能是 unknown", "field": "output"}
        c.execute("UPDATE criteria SET status=?, last_run=?, output=? WHERE id=?",
                  (st, b.get("run_at") or now_iso(), (b.get("output") or "")[:2000], cid))
        log_event(c, r["project_id"], "criteria.run", payload=f"{st}:{r['text'][:30]}")
    return {"ok": True, "id": cid, "status": st}


def pressure_of(now, created_ts, deadline_ts, eta_finish):
    """时间压力分级。

    两条判据：① 剩余时间占计划工期的比例（30% 是用户定的强制复盘红线）；
    ② 排期预测会不会晚于截止——干活慢但时间表还够，不该报警；反过来，
    时间还剩一半但按当前速度注定晚 3 小时，才是真危险。
    """
    if not deadline_ts:
        return {"level": "none", "left_pct": None, "left_sec": None,
                "slip_hours": None, "reason": "未设截止时间"}
    span = max(1.0, (deadline_ts - created_ts) if created_ts else (deadline_ts - now))
    left_sec = deadline_ts - now
    left_pct = max(0, min(100, round(100 * left_sec / span)))
    slip_h = round((eta_finish - deadline_ts) / 3600.0, 1) if eta_finish else None
    if left_sec <= 0:
        level, reason = "overdue", f"已超截止 {abs(round(left_sec / 3600.0, 1))}h"
    elif left_pct <= PRESSURE_CRIT_LEFT * 100:
        level, reason = "critical", f"只剩 {left_pct}% 时间"
    elif left_pct <= PRESSURE_WATCH_LEFT * 100:
        level, reason = "watch", f"只剩 {left_pct}% 时间"
    else:
        level, reason = "ok", f"还剩 {left_pct}% 时间"
    # 预测性升级：排期注定迟到，且要迟掉剩余时间的 1/4 以上 → 直接升级
    if level in ("ok", "watch") and slip_h and slip_h > 0:
        if left_sec > 0 and slip_h * 3600 >= 0.25 * left_sec:
            level = "critical"
            reason = f"按当前速度要晚 {slip_h}h（剩余只有 {round(left_sec / 3600.0, 1)}h）"
        else:
            level = "watch"
            reason = f"按当前速度要晚 {slip_h}h"
    return {"level": level, "left_pct": left_pct, "left_sec": int(left_sec),
            "slip_hours": slip_h, "reason": reason,
            "watch_at": int(PRESSURE_WATCH_LEFT * 100),
            "crit_at": int(PRESSURE_CRIT_LEFT * 100)}


def retro_state(pid, pressure, now):
    """复盘闸门：critical/overdue 且没有「新鲜且已批准」的复盘 → 一律拦。"""
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM retros WHERE project_id=? ORDER BY created_at DESC LIMIT 20", (pid,))]
    for r in rows:
        try:
            r["answers"] = json.loads(r.get("answers") or "{}")
        except json.JSONDecodeError:
            r["answers"] = {}
    latest = rows[0] if rows else None
    level = pressure.get("level")
    required = level in ("critical", "overdue")
    interval = RETRO_REARM_SEC.get(level, 0)
    satisfied, need_reason = False, ""
    if required:
        if not latest:
            need_reason = "还没有任何复盘"
        elif latest["status"] == "pending":
            need_reason = f"第 {latest['round']} 轮复盘还没提交"
        elif latest["status"] == "submitted":
            need_reason = f"第 {latest['round']} 轮复盘等你批准"
        elif latest["status"] == "approved":
            age = now - float(latest.get("decided_at") or 0)
            if age < interval:
                satisfied = True
            else:
                need_reason = (f"上一轮复盘已过 {int(age // 60)} 分钟，"
                               f"{'超期' if level == 'overdue' else '高压'}下要求再来一轮")
        else:  # rejected
            need_reason = f"第 {latest['round']} 轮复盘被驳回：{latest.get('note') or '未写原因'}"
    nxt_round = (max([r["round"] for r in rows]) + 1) if rows else 1
    return {
        "required": required and not satisfied,
        "level": level,
        "round": (latest["round"] if latest and not satisfied and required else nxt_round),
        "latest": latest,
        "history": rows[:5],
        "questions": RETRO_QUESTIONS,
        "blocked_actions": RETRO_BLOCKED_ACTIONS if (required and not satisfied) else [],
        "rearm_min": int(interval // 60) if interval else 0,
        "reason": need_reason,
        "asked_at": (float(latest["required_at"]) if latest else now),
    }


def submit_retro(pid, b):
    """总监提交复盘：六个问题必须逐条回答，空话会被长度校验挡掉。"""
    with db() as c:
        p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not p:
        return {"error": "no such project"}
    ans = b.get("answers") or {}
    missing, short = [], []
    for q in RETRO_QUESTIONS:
        v = str(ans.get(q["key"]) or "").strip()
        if not v:
            missing.append(q["key"])
        elif len(v) < 8:
            short.append(q["key"])
    if missing:
        return {"error": f"复盘回答不全，缺：{', '.join(missing)}",
                "field": "answers", "missing": missing, "questions": RETRO_QUESTIONS}
    if short:
        return {"error": f"回答太短（<8 字），等于没说：{', '.join(short)}",
                "field": "answers", "short": short}
    now = time.time()
    with db() as c:
        prev = c.execute("SELECT MAX(round) FROM retros WHERE project_id=?", (pid,)).fetchone()[0] or 0
        rid = new_id("retro")
        st = project_state(pid)
        pr = st["pressure"] if st else {}
        c.execute("INSERT INTO retros(id,project_id,round,trigger,level,left_pct,slip_hours,"
                  "status,answers,by,required_at,submitted_at,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, pid, prev + 1, b.get("trigger") or ("deadline" if pr.get("level") in
                   ("critical", "overdue") else "manual"), pr.get("level", ""),
                   int(pr.get("left_pct") or 0), float(pr.get("slip_hours") or 0),
                   "submitted", json.dumps(ans, ensure_ascii=False), b.get("by", ""),
                   now, now, now_iso(), now_iso()))
        log_event(c, pid, "retro.submitted", actor=b.get("by", ""),
                  payload=json.dumps({"round": prev + 1}, ensure_ascii=False))
    return next(r for r in retro_state(pid, st["pressure"], now)["history"] if r["id"] == rid)


def decide_retro(rid, b):
    """用户批准/驳回复盘。只有批准才算闸门放行。"""
    decision = (b.get("decision") or "").strip()
    if decision not in ("approve", "reject"):
        return {"error": "decision 只能是 approve / reject"}
    with db() as c:
        r = c.execute("SELECT * FROM retros WHERE id=?", (rid,)).fetchone()
        if not r:
            return {"error": "no such retro"}
        if r["status"] != "submitted":
            return {"error": f"这条复盘当前是 {r['status']}，只能审批 submitted 状态"}
        status = "approved" if decision == "approve" else "rejected"
        c.execute("UPDATE retros SET status=?, note=?, decided_at=?, decided_by=?, updated_at=?"
                  " WHERE id=?",
                  (status, b.get("note", ""), time.time(), b.get("by", "user"),
                   now_iso(), rid))
        log_event(c, r["project_id"], f"retro.{status}", actor=b.get("by", "user"),
                  payload=b.get("note", ""))
        out = dict(c.execute("SELECT * FROM retros WHERE id=?", (rid,)).fetchone())
    out["answers"] = json.loads(out.get("answers") or "{}")
    return out


SESS_ROOT = os.path.expanduser("~/.codex/sessions")
_THREAD_CACHE = {"at": 0.0, "rows": []}
_LIVE_CACHE = {"at": 0.0, "map": {}}
SESSION_LIVE_WINDOW = 240   # 会话文件在 4 分钟内有写入 → 这个工位一定有人在跑


def session_mtimes(ttl=12, days=2):
    """会话文件 mtime 索引（thread_id → 最后一次写入时间）。

    子代理跑在 Codex 里，不会主动回调心跳，所以「有没有人干活」不能只看 last_seen。
    这里只 glob 最近两天的日期目录、只取 mtime、不读内容，12 秒缓存，成本可忽略。
    """
    now = time.time()
    if now - _LIVE_CACHE["at"] < ttl:
        return _LIVE_CACHE["map"]
    out = {}
    today = datetime.now()
    for i in range(days):
        d = today - timedelta(days=i)
        pat = os.path.join(SESS_ROOT, f"{d:%Y}", f"{d:%m}", f"{d:%d}", "*.jsonl")
        for p in glob.glob(pat):
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                          r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", p)
            if m:
                full = m.group(1)
                out[full] = max(out.get(full, 0.0), mt)
    # 成员表里 thread_id 有的存全量 UUID、有的只存前 8 位（工位绑定用短 id），
    # 两种都要能查到；前缀键取同名会话里最新的一次写入。
    for k in [k for k in out if "-" in k]:
        short = k[:8]
        if out.get(short, 0.0) < out[k]:
            out[short] = out[k]
    _LIVE_CACHE.update({"at": now, "map": out})
    return out


def _snippet(head):
    for raw in re.findall(r'"text"\s*:\s*"((?:[^"\\]|\\.){8,300})"', head):
        try:
            s = json.loads('"' + raw + '"')      # 走 JSON 解码，别用 unicode_escape（会毁中文）
        except Exception:
            s = raw
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) < 12 or s.startswith("<") or "page_id" in s or "http" in s[:8]:
            continue
        return s[:120]
    return ""


def scan_threads(days=14, limit=400):
    """扫本机 Codex 会话文件，给「点员工 → 打开他的会话」找候选。

    只读 ~/.codex/sessions 里的 session_meta（id / parent / cwd）+ 开头几十行文本，
    20 秒缓存。不猜、不伪造：拿不到就是空列表，前端会提示手工登记。
    """
    now = time.time()
    if now - _THREAD_CACHE["at"] < 20:
        return _THREAD_CACHE["rows"]
    rows = []
    for p in glob.glob(os.path.join(SESS_ROOT, "**", "*.jsonl"), recursive=True):
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if now - mt > days * 86400:
            continue
        try:
            with open(p, encoding="utf8", errors="ignore") as f:
                meta = (json.loads(f.readline() or "{}").get("payload") or {})
                # 成员名常常出现在几十行之后（第一条大 prompt 很长），
                # 只读前 30 行会匹配不到；这里取前 60 行、上限 300KB
                buf, total = [], 0
                for _ in range(60):
                    ln = f.readline()
                    if not ln:
                        break
                    buf.append(ln)
                    total += len(ln)
                    if total > 300000:
                        break
                head = "".join(buf)
        except Exception:
            continue
        tid = meta.get("id") or ""
        if not tid:
            continue
        rows.append({"id": tid, "parent": meta.get("parent_thread_id") or "",
                     "cwd": meta.get("cwd") or "", "mtime": mt,
                     "age_min": int((now - mt) / 60), "snip": _snippet(head),
                     "head": head})
    rows.sort(key=lambda r: -r["mtime"])
    rows = rows[:limit]
    _THREAD_CACHE.update({"at": now, "rows": rows})
    return rows


def threads_for_project(pid):
    """给每个工位找候选会话。

    排序规则（踩过的坑）：主线程那条 rollout 正文里会引用所有子代理 id/名字，
    以前靠正文 grep 排在最前，点「打开会话」就跳进 93MB 的主线程记录里。
    现在：① 已登记的 thread_id 精确命中排第一；② 有 parent_thread_id 的（子代理）
    优先于无 parent 的（主线程）；③ 正文 grep 只作兜底。
    """
    st = project_state(pid)
    if not st:
        return None
    rows = scan_threads()
    byid = {r["id"]: r for r in rows}
    out = []
    for m in st["members"]:
        name = (m.get("name") or "").strip()
        keys = [k for k in (name, name.split("/")[-1].strip(), (m.get("role") or "").split("·")[-1].strip())
                if len(k) >= 2]
        cands, seen = [], set()

        def add(r, exact=False):
            if r["id"] in seen or len(cands) >= 3:
                return
            seen.add(r["id"])
            cands.append({"id": r["id"], "parent": r["parent"], "age_min": r["age_min"],
                          "snip": r["snip"] or r["cwd"][-60:], "exact": exact,
                          "is_sub": bool(r["parent"])})

        tid = (m.get("thread_id") or "").strip()
        hit = byid.get(tid) if tid else None
        if hit:
            add(hit, exact=True)
        matched = [r for r in rows if any(k and k in r["head"] for k in keys)]
        for r in [x for x in matched if x["parent"]]:      # 子代理优先
            add(r)
        for r in [x for x in matched if not x["parent"]]:  # 主线程兜底
            add(r)
        out.append({"member_id": m["id"], "name": name, "thread_id": m.get("thread_id") or "",
                    "candidates": cands})
    return {"members": out, "scanned": len(rows)}


UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def resolve_thread_id(tid):
    """把工位上的短 id（8 位）还原成会话全 UUID。

    编制表里 thread_id 可能存的是 `01a182d1` 这种前缀（早期绑定），
    但深链、会话文件都要全 UUID —— 这里按会话文件 mtime 取最新的一个。
    """
    tid = (tid or "").strip()
    if UUID_RE.fullmatch(tid):
        return tid
    if re.fullmatch(r"[0-9a-fA-F]{8}", tid):
        mt = session_mtimes()
        hits = [k for k in mt if "-" in k and k[:8].lower() == tid.lower()]
        if hits:
            return max(hits, key=lambda k: mt[k])
    return ""


def open_thread(tid):
    """在 Codex 里打开这个会话：走 codex:// 深链（本机已注册 com.openai.codex）。"""
    full = resolve_thread_id(tid)
    if not full:
        return {"error": f"找不到这个会话：{tid or '(空)'}。"
                         "短 id 需要对应会话文件还在（最近两天），或让总监重新绑定会话。"}
    if sys.platform != "darwin":
        return {"error": "codex:// 深链目前只有 macOS 支持；本机请用 Codex 会话列表打开",
                "thread": full}
    r = subprocess.run(["open", "-g", f"codex://threads/{full}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"error": (r.stderr or "open failed").strip()[:200], "thread": full}
    return {"ok": True, "thread": full, "url": f"codex://threads/{full}"}


def retro_gate(pid):
    """写操作前的统一闸门：需要复盘却没过 → 返回 403 体。"""
    st = project_state(pid)
    if not st:
        return None
    r = st.get("retro") or {}
    if r.get("required"):
        return {"error": f"时间压力 {r['level']}：{r['reason']}。"
                         f"总监必须先提交第 {r['round']} 轮复盘并由用户批准，"
                         f"在此之前 {'、'.join(r['blocked_actions'])} 一律拒绝",
                "need_retro": True, "retro": r}
    return None


def project_state(pid):
    with db() as c:
        p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            return None
        members = [dict(r) for r in c.execute(
            "SELECT * FROM members WHERE project_id=? ORDER BY created_at", (pid,))]
        tasks = [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE project_id=? ORDER BY sort_order, created_at", (pid,))]

    now = time.time()
    workdir = (p["workdir"] if "workdir" in p.keys() and p["workdir"] else os.getcwd())
    # 配比：项目落库时存的是计划里总监填的那份；老项目没填过就按默认 30/40/30 切
    prio_ratio, _prio_rerr = parse_priority_ratio(
        p["priority_ratio"] if "priority_ratio" in p.keys() else "")
    prio_ratio = prio_ratio or DEFAULT_PRIORITY_RATIO
    # 一条查询取全部在跑的计时段：原来每条 doing 任务单开一次连接（N+1），
    # doing 一多，刷新时就是几十次连接 + 几十次 PRAGMA。
    open_start = {}
    mine_ids = {t["id"] for t in tasks}
    with db() as c2:
        for r in c2.execute("SELECT task_id, started_at FROM task_sessions"
                            " WHERE ended_at IS NULL"):
            if r["task_id"] not in mine_ids:
                continue
            cur = open_start.get(r["task_id"])
            if cur is None or float(r["started_at"]) > cur:
                open_start[r["task_id"]] = float(r["started_at"])
    for t in tasks:
        t["time_spent"] = safe_int(t.get("time_spent"), 0, 0)
        t["elapsed"] = 0
        if t["status"] == "doing" and not (t.get("paused_at") or 0):
            started = open_start.get(t["id"])
            if started is not None:
                t["elapsed"] = max(0, int(now - started))
        t["total_time"] = t["time_spent"] + t["elapsed"]
        t["paused"] = bool(t.get("paused_at") or 0) and t["status"] == "doing"
        t["paused_for"] = int(now - float(t["paused_at"])) if t["paused"] else 0
        t["budget_sec"] = int(float(t["estimate"] or 0) * 3600)
        t["budget_left"] = t["budget_sec"] - t["total_time"]
        t["over_budget"] = t["status"] not in ("done", "abandoned") and t["budget_left"] < 0
        # 超支额度对所有任务都算：真正估崩的往往是已经做完的那几条，
        # 只在未完成任务里排榜 = 永远看不到最该复盘的对象。
        t["over_budget_sec"] = max(0, -t["budget_left"])

    # 串行化排期：每人的任务排队，跨人并行，目标完成时刻取最晚的那条
    # 未指派任务按「能干活的人数」开并行槽（总监不接执行）
    _lanes = max(1, len([m for m in members if not m.get("is_lead")]) or 1)
    sch = compute_schedule(tasks, now, pool_lanes=_lanes)
    # 下游计数：谁被最多任务守着，谁才是真关键
    down = {}
    for t in tasks:
        dep = (t.get("depends_on") or "").strip()
        if dep:
            down[dep] = down.get(dep, 0) + 1
    for t in tasks:
        st, fin = sch["eta"].get(t["id"], (0.0, 0.0))
        if t["status"] == "done":
            t["eta_at"], t["eta_left"], t["wait_sec"] = 0.0, 0.0, 0
            t["on_critical"] = False
            continue
        t["eta_at"] = fin
        t["eta_left"] = fin - now
        t["wait_sec"] = max(0, int(st - now))
        t["on_critical"] = t["id"] in sch["critical"]
    # 实排优先级：不采信自报。自报只留一份审计值，排序一律按证据算。
    for t in tasks:
        t["priority_declared"] = t["priority"]
        t["downstream"] = down.get(t["id"], 0)
        if t["status"] in ("done", "abandoned"):
            t["prio"], t["prio_why"] = 0, ""      # 已收工的任务不排位
            continue
        score, why = 0, []
        if t.get("on_critical"):
            score += 3; why.append("在关键路径上")
        if t["downstream"] >= 2:
            score += 3; why.append(f"阻塞 {t['downstream']} 条下游")
        elif t["downstream"] == 1:
            score += 1; why.append("有 1 条下游在等它")
        if t.get("over_budget"):
            score += 2; why.append("已超预算")
        if t["status"] == "blocked":
            score += 2; why.append("当前阻塞")
        # 已经在做的活不能「延后」：投入沉没 + 占着人，至少 P2
        if t["status"] == "doing":
            score += 1; why.append("已在做，占着人")
        if t.get("eta_left", 0) < 0:
            score += 1; why.append("预计会拖过截止")
        t["prio_score"], t["prio_why"] = score, " · ".join(why)
    # 配比切档：P1/P2 各留几条由项目配比决定（总监在计划里填，默认 30/40/30）。
    # 「全 P1」的根因是把优先级当形容词；这里改成硬配额，超额的一句话就掉档。
    open_tasks = [t for t in tasks if t["status"] not in ("done", "abandoned")]
    q1, q12 = ratio_quota(prio_ratio, len(open_tasks))
    ranked = sorted(open_tasks, key=lambda t: (-t["prio_score"], t["eta_left"]))
    p1_ids = {t["id"] for t in ranked[:q1] if t["prio_score"] > 0}
    p2_ids = {t["id"] for t in ranked[q1:q12] if t["prio_score"] > 0}
    for t in open_tasks:
        if t["id"] in p1_ids:
            t["prio"] = 1
        elif t["id"] in p2_ids:
            t["prio"] = 2
        else:
            t["prio"] = 3
            t["prio_why"] = ((t["prio_why"] + f" · 配额已满（{ratio_text(prio_ratio)}），排期往后放")
                             if t["prio_score"] > 0
                             else "不在关键路径、没有下游在等、没超预算")
    # 已经在做的不降到 P3：人已经压上去了，标成「最低优先级」只会让看板自相矛盾
    for t in open_tasks:
        if t["status"] == "doing" and t["prio"] == 3:
            t["prio"] = 2
            t["prio_why"] = "已在做，占着人（在做的不降到 P3）"

    tmap = {t["id"]: t for t in tasks}
    for m in members:
        mine = [t for t in tasks if t["assignee_id"] == m["id"]]
        doing = [t for t in mine if t["status"] == "doing"]
        blocked = [t for t in mine if t["status"] == "blocked"]
        hs = float(m["last_seen"] or 0)
        duty = float(m.get("duty_until") or 0)
        m["heartbeat_age"] = max(0, int(now - hs)) if hs > 0 else -1
        m["on_duty"] = duty > now              # 总监派活时登记的在场租约
        m["duty_left"] = max(0, int(duty - now)) if m["on_duty"] else 0
        # 会话文件在写 = 这个工位确实有人在跑，比心跳可靠（子代理不回调心跳）
        tid0 = (m.get("thread_id") or "").strip()
        sess_mt = session_mtimes().get(tid0) if tid0 else 0.0
        m["session_age"] = max(0, int(now - sess_mt)) if sess_mt else -1
        m["session_live"] = bool(sess_mt) and (now - sess_mt) < SESSION_LIVE_WINDOW
        m["live"] = (m["on_duty"] or m["session_live"]
                     or (hs > 0 and (now - hs) < HEARTBEAT_WINDOW))
        m["load"] = len([t for t in mine if t["status"] in ("backlog", "doing", "review", "blocked")])
        # 超载：一个人手里压的活超过他的并发槽（capacity），看板要显式喊出来，
        # 否则「5 个人 20 条任务」看着很忙，实际全堵在一个人身上。
        m["overload"] = bool(m.get("capacity") and m["load"] > int(m["capacity"]))
        m["done_count"] = len([t for t in mine if t["status"] == "done"])
        m["total"] = len(mine)
        m["current_task"] = doing[0]["title"] if doing else ""
        # 点开成员要能回答「他在做什么、卡在哪」：把在办任务和卡点直接带上，
        # 别让前端再去翻 events 猜
        def _waits_on(d):
            up = tmap.get(d.get("depends_on") or "")
            return up["title"] if (up and up["status"] != "done") else ""
        m["doing_tasks"] = [{"id": d["id"], "title": d["title"],
                             "progress": safe_int(d.get("progress"), 0, 0, 100),
                             "elapsed": safe_int(d.get("elapsed"), 0, 0),
                             "total_time": safe_int(d.get("total_time"), 0, 0),
                             "stale_sec": max(0, int(now - _epoch(d.get("updated_at")
                                                                  or d.get("created_at")))),
                             "over_budget": bool(d.get("over_budget")),
                             "waits_on": _waits_on(d)}
                            for d in doing]
        m["stuck"] = ((blocked[0].get("blocked_reason") or "已标阻塞（未写原因）")
                      if blocked else (_waits_on(doing[0]) if doing else ""))
        m["thread_id"] = (m.get("thread_id") or "").strip()
        # 工位：员工身份是「椅子」，会话是坐在椅子上的人。换人不换椅子。
        m["seat_no"] = int(m.get("seat_no") or 0)
        m["seat_label"] = f"#{m['seat_no']:02d}" if m["seat_no"] else "#--"
        m["roster_state"] = (m.get("roster_state") or "active").strip() or "active"
        m["bind_count"] = int(m.get("bind_count") or 1)
        m["bound_at"] = float(m.get("bound_at") or 0)
        m["bound_age"] = max(0, int(now - m["bound_at"])) if m["bound_at"] else -1
        m["progress"] = round(sum(safe_int(t.get("progress"), 0, 0, 100) for t in mine) / len(mine)) if mine else 0
        m["time_total"] = sum(safe_int(t.get("total_time"), 0, 0) for t in mine)
        m["time_running"] = sum(safe_int(t.get("elapsed"), 0, 0) for t in mine)
        # 状态以「任务事实」为准：有 doing 任务就是工作中，心跳只用于判定空闲/离线。
        # 子代理跑在 Codex 里，不会稳定回调心跳，所以不能用它否决进行中的任务。
        if blocked:
            m["state"] = "blocked"
            m["state_source"] = "task"
        elif doing:
            m["state"] = "working"
            m["state_source"] = "task"
        elif m["session_live"]:
            # 会话文件正在写 = 这个座位真的在执行。总监通常没有 doing 任务
            # （它的活是调度），只按心跳判会把正在干活的总监显示成摸鱼。
            m["state"] = "working"
            m["state_source"] = "session"
        elif m["live"]:
            m["state"] = "idle"
            m["state_source"] = "heartbeat"
        else:
            m["state"] = "offline"
            m["state_source"] = "heartbeat"
        m["working"] = m["state"] == "working"
        m["online"] = m["state"] != "offline"          # 在岗 = 不是离线
        m["stale"] = bool(doing) and not m["live"]      # 在干活但没心跳上报
        last_act = max(hs, sess_mt or 0.0)
        m["offline_for"] = (max(0, int(now - last_act)) if last_act > 0 else -1) \
            if m["state"] == "offline" else 0
        # 看板只分「工作中 / 摸鱼中」：没在干活的时间统一从这里取，别让前端自己猜
        m["away_for"] = 0 if m["state"] == "working" \
            else (max(0, int(now - last_act)) if last_act > 0 else -1)

    # 会话接管台账：证明「关掉子代理又开一个」是同一把椅子换人，不是新员工
    with db() as c:
        hist = c.execute("SELECT member_id, thread_id, bound_at, released_at, reason"
                         " FROM member_threads WHERE project_id=? ORDER BY bound_at", (pid,)).fetchall()
    bym = {}
    for h in hist:
        bym.setdefault(h["member_id"], []).append(
            {"thread_id": h["thread_id"], "bound_at": h["bound_at"],
             "released_at": h["released_at"], "reason": h["reason"] or ""})
    for m in members:
        m["thread_history"] = bym.get(m["id"], [])[-5:]

    for t in tasks:
        t["dep_title"] = tmap.get(t["depends_on"], {}).get("title", "") if t["depends_on"] else ""
        t["dep_done"] = (tmap.get(t["depends_on"], {}).get("status") == "done") if t["depends_on"] else True
        # 阻塞传播：把整条上游链上没完成的都算进来
        seen, stack, ups = set(), [t], []
        while stack:
            cur = stack.pop()
            did = cur["depends_on"]
            if not did or did in seen or did not in tmap:
                continue
            seen.add(did)
            up = tmap[did]
            if up["status"] != "done":
                ups.append(up["title"])
            stack.append(up)
        t["blocked_upstream"] = ups
        t["dep_blocked"] = bool(ups)
        if t["status"] == "done":
            t["progress"] = 100
        # 交付物存在性：✔ 真实存在 / ✘ 不存在 / 空 = 未要求
        dl = (t.get("deliverable") or "").strip()
        paths = parse_deliverables(dl)
        t["deliverable_paths"] = paths
        all_ok, any_ok, missing = check_deliverables(workdir, dl)
        t["deliverable_missing"] = missing
        t["deliverable_ok"] = None if not paths else (True if all_ok else ("partial" if any_ok else False))
        # 停滞：doing 任务多久没更新
        t["stale_sec"] = 0
        if t["status"] == "doing":
            t["stale_sec"] = max(0, int(now - _epoch(t["updated_at"] or t["created_at"])))
        t["stale_level"] = ("crit" if t["stale_sec"] >= 90 * 60
                            else "warn" if t["stale_sec"] >= 30 * 60 else "ok")
        t["estimate"] = safe_float(t.get("estimate"), 1) or 1

    # 资源冲突：同一资源被两个 doing 任务同时占用
    res_use = {}
    for t in tasks:
        if t["status"] == "doing" and (t.get("resource") or "").strip():
            res_use.setdefault(t["resource"].strip(), []).append(t["title"])
    conflicts = {k: v for k, v in res_use.items() if len(v) > 1}
    for t in tasks:
        r = (t.get("resource") or "").strip()
        t["resource_conflict"] = bool(r and r in conflicts)

    total = len(tasks)
    # 加权进度：按 estimate 加权，新增 backlog 不会把已完成的工作「稀释」掉。
    # abandoned 是「决定不做」，既不该拉低进度也不该占分母——否则一标放弃进度就掉。
    scorable = [t for t in tasks if t["status"] != "abandoned"]
    wsum = sum(t["estimate"] for t in scorable)
    overall = (round(sum(safe_int(t.get("progress"), 0, 0, 100) * t["estimate"]
                         for t in scorable) / wsum) if wsum else 0)
    stats = {
        "total": total, "backlog": len([t for t in tasks if t["status"] == "backlog"]),
        "doing": len([t for t in tasks if t["status"] == "doing"]),
        "review": len([t for t in tasks if t["status"] == "review"]),
        "done": len([t for t in tasks if t["status"] == "done"]),
        "blocked": len([t for t in tasks if t["status"] == "blocked"]),
        "abandoned": len([t for t in tasks if t["status"] == "abandoned"]),
        "members": len(members), "online": len([m for m in members if m["online"]]),
        "workers": len([m for m in members if not m.get("is_lead")]),
        "lead": len([m for m in members if m.get("is_lead")]),
        "working": len([m for m in members if m["state"] == "working"]),
        "offline": len([m for m in members if m["state"] == "offline"]),
        "blocked_members": len([m for m in members if m["state"] == "blocked"]),
        "overall": overall,
        "deliverables": len([t for t in tasks if t["deliverable"]]),
        "delivered": len([t for t in tasks if t["deliverable"] and t["status"] in ("review", "done")]),
    }
    # 按岗位聚合
    roles = {}
    for t in tasks:
        r = t["role"] or "未分类"
        d = roles.setdefault(r, {"role": r, "total": 0, "done": 0, "doing": 0,
                                 "blocked": 0, "progress_sum": 0, "members": set()})
        d["total"] += 1
        d["progress_sum"] += safe_int(t.get("progress"), 0, 0, 100)
        if t["status"] == "done": d["done"] += 1
        if t["status"] == "doing": d["doing"] += 1
        if t["status"] == "blocked": d["blocked"] += 1
        for m in members:
            if m["id"] == t["assignee_id"]: d["members"].add(m["name"])
    role_rows = []
    for d in roles.values():
        role_rows.append({"role": d["role"], "total": d["total"], "done": d["done"],
                          "doing": d["doing"], "blocked": d["blocked"],
                          "progress": round(d["progress_sum"] / d["total"]) if d["total"] else 0,
                          "owners": "、".join(sorted(d["members"]))})
    role_rows.sort(key=lambda x: -x["total"])

    # 目标 → 阶段（milestone）→ 任务
    mss = {}
    for t in tasks:
        key = (t.get("milestone") or "").strip() or "未分阶段"
        d = mss.setdefault(key, {"name": key, "total": 0, "done": 0, "doing": 0,
                                 "blocked": 0, "weight": 0.0, "wsum": 0.0, "tasks": []})
        d["total"] += 1
        d["weight"] += t["estimate"]
        d["wsum"] += safe_int(t.get("progress"), 0, 0, 100) * t["estimate"]
        d["tasks"].append(t["id"])
        if t["status"] == "done": d["done"] += 1
        if t["status"] == "doing": d["doing"] += 1
        if t["status"] == "blocked": d["blocked"] += 1
    milestones = []
    for d in mss.values():
        milestones.append({"name": d["name"], "total": d["total"], "done": d["done"],
                           "doing": d["doing"], "blocked": d["blocked"],
                           "progress": round(d["wsum"] / d["weight"]) if d["weight"] else 0})
    milestones.sort(key=lambda x: (x["name"] == "未分阶段", -x["total"]))

    # 目标合规：目标 → 阶段的这条链有没有断
    goal_txt = (p["goal"] or "").strip()
    crit_txt = (p["goal_criteria"] or "").strip()
    viol = []
    if not goal_txt:
        viol.append({"code": "no_goal", "sev": "P0", "task": "",
                     "text": "项目还没写目标"})
    elif not crit_txt:
        viol.append({"code": "no_criteria", "sev": "P1", "task": "",
                     "text": "缺目标验收标准（怎样算达成）"})
    if goal_txt:
        for t in tasks:
            if not (t.get("milestone") or "").strip():
                viol.append({"code": "no_milestone", "sev": "P1", "task": t["title"],
                             "text": "还没归入阶段（历史任务，需总监补挂）"})
            if not (t.get("deliverable") or "").strip():
                viol.append({"code": "no_deliverable", "sev": "P2", "task": t["title"],
                             "text": "没写产出物"})
            if not (t.get("dod") or "").strip():
                viol.append({"code": "no_dod", "sev": "P2", "task": t["title"],
                             "text": "没写验收标准"})
            if t["status"] in ("review", "done") and t["deliverable_ok"] is False:
                viol.append({"code": "missing_output", "sev": "P0", "task": t["title"],
                             "text": "交付物不存在"})
            if (t.get("evidence") == "inferred"
                    and safe_int(t.get("progress"), 0) >= 90):
                viol.append({"code": "weak_evidence", "sev": "P1", "task": t["title"],
                             "text": "推断证据却标了 ≥90%"})
    # 严重项在前，其次按出现顺序：看板只展开前几条也不需要用户自己排
    viol.sort(key=lambda x: x["sev"])

    # 目标只有一个、挂在项目上，所以「任务在不在目标里」由项目决定，不由任务决定。
    # 任务级能缺的只有「阶段归属」，这两件事之前混成了一个字段，才让每张卡都报未挂目标。
    scoped = [t for t in tasks if (t.get("milestone") or "").strip()]
    stage_coverage = (round(100 * len(scoped) / len(tasks)) if tasks else 100)
    if not goal_txt:
        stage_coverage = 0
    for t in tasks:
        t["goal_attached"] = bool(goal_txt)

    for t in tasks:
        t["evidence_warning"] = bool(
            t["status"] == "done" and (t.get("evidence") or "") in ("inferred", "assumed"))

    # 截止与剩余估算（estimate 按「人时」计）
    remaining = sum(t["estimate"] * (100 - safe_int(t.get("progress"), 0, 0, 100)) / 100.0
                    for t in tasks if t["status"] not in ("done", "abandoned"))
    # 截止时间：只写日期时按「当天 23:59:59」算，否则写 10-30 会让 10-29 晚上就显示逾期。
    dl_raw = (p["deadline"] or "").strip()
    deadline_ts = None
    for fmt, eod in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%dT%H:%M", False),
                     ("%Y-%m-%d", True), ("%Y/%m/%d", True)):
        try:
            _dt = datetime.strptime(dl_raw, fmt)
            if eod:
                _dt = _dt.replace(hour=23, minute=59, second=59)
            deadline_ts = _dt.astimezone().timestamp()
            break
        except Exception:
            continue
    hours_left = ((deadline_ts - now) / 3600.0) if deadline_ts else None
    # 时间燃烧比：给总监和成员一个「还剩几成时间」的硬指标
    created_ts = None
    try:
        created_ts = datetime.fromisoformat(
            str(p["created_at"]).replace("Z", "+00:00")).timestamp()
    except Exception:
        created_ts = None
    # 起算点 = 当前承诺的登记时刻，不是建项目的时刻。
    # 改期后用 created_at 会让「已用时间」从 100% 倒退成 38%，看板像在往回走。
    committed_ts = float(p["committed_at"] or 0) if "committed_at" in p.keys() else 0.0
    burn_start = committed_ts or created_ts
    time_used_pct = None
    if deadline_ts and burn_start and deadline_ts > burn_start:
        time_used_pct = round(100 * (now - burn_start) / (deadline_ts - burn_start))
        time_used_pct = max(0, min(100, time_used_pct))
    estimates_all_one = bool(tasks) and all(abs(t["estimate"] - 1) < 1e-9 for t in tasks)

    # 排期结论：预计什么时候收工、比截止早还是晚、卡在哪条链上
    eta_finish = sch["finish"] or None
    eta_slip_hours = (round((eta_finish - deadline_ts) / 3600.0, 1)
                      if (eta_finish and deadline_ts) else None)
    crit_titles = [tmap[i]["title"] for i in sch["chain"] if i in tmap]

    # 时间压力 + 利用率：这两块是「强制复盘」的判据，必须先于 return 算出来
    pressure = pressure_of(now, burn_start, deadline_ts, eta_finish)
    elapsed = max(1.0, now - burn_start) if burn_start else None
    workers = [m for m in members if not m.get("is_lead")]
    for m in members:
        # 利用率 = 真实计过时的工时 / 项目已流逝时间；没计时器就是 0，不靠猜
        m["util_pct"] = (min(100, round(100.0 * m["time_total"] / elapsed))
                         if elapsed else 0)
        m["idle_pct"] = max(0, 100 - m["util_pct"])
        m["slacking"] = bool(not m.get("is_lead") and elapsed and elapsed > 900
                             and m["util_pct"] < 20 and m["state"] != "working")
    team_util = (min(100, round(100.0 * sum(m["time_total"] for m in workers)
                               / (len(workers) * elapsed)))
                 if (workers and elapsed) else 0)
    slackers = [{"id": m["id"], "name": m["name"], "role": m["role"],
                 "util_pct": m["util_pct"], "away_for": m["away_for"],
                 "state": m["state"]}
                for m in sorted(workers, key=lambda x: x["util_pct"])
                if m["slacking"]][:5]
    utilization = {
        "team_pct": team_util,
        "elapsed_sec": int(elapsed or 0),
        "working_sec": int(sum(m["time_total"] for m in workers)),
        "workers": len(workers),
        "slackers": slackers,
        "note": ("利用率 = 员工真实计时工时 ÷ 项目已流逝时间；同一人被派了并行任务时"
                 "仍按一条时间线算，所以上限 100%"),
    }
    retro = retro_state(pid, pressure, now)
    # 复盘里「利用率」那道题必须带实测数字，避免总监编
    retro["utilization"] = {"team_pct": team_util, "slackers": slackers}
    over_top = [{"id": t["id"], "title": t["title"], "over_sec": int(t["over_budget_sec"]),
                 "estimate": t["estimate"], "spent": int(t["total_time"]),
                 "status": t["status"]}
                for t in sorted((x for x in tasks if x["over_budget_sec"] > 0),
                                key=lambda x: -x["over_budget_sec"])[:3]]
    # 估时校准：已完成任务的「估算 vs 实花」
    # 只用真正计过时的完成项：历史任务没开计时器，混进来会把偏差算成「估得离谱」
    timed = [t for t in tasks if t["status"] == "done" and float(t["time_spent"] or 0) > 0]
    d_est = sum(float(t["estimate"] or 0) for t in timed)
    d_act = sum(float(t["total_time"] or 0) / 3600.0 for t in timed)
    estimate_bias = (round(100 * (d_act / d_est - 1)) if d_est > 0 else None)

    # 影响面：有多少条链压在这条任务上（换人接手时先看这个数字）
    down = {t["id"]: 0 for t in tasks}
    for t in tasks:
        cur, seen = t, set()
        while cur and cur["depends_on"] in tmap and cur["depends_on"] not in seen:
            seen.add(cur["depends_on"])
            down[cur["depends_on"]] += 1
            cur = tmap[cur["depends_on"]]
    for t in tasks:
        t["downstream"] = down[t["id"]]

    # 资源租约（设备独占）
    with db() as c:
        leases = [dict(r) for r in c.execute(
            "SELECT * FROM resource_leases WHERE project_id=?", (pid,))]
        hyps = [dict(r) for r in c.execute(
            "SELECT * FROM hypotheses WHERE project_id=? ORDER BY created_at DESC LIMIT 300", (pid,))]
        crits = [dict(r) for r in c.execute(
            "SELECT * FROM criteria WHERE project_id=? ORDER BY sort_order, text", (pid,))]
    for l in leases:
        l["holder_title"] = tmap.get(l["holder_task"], {}).get("title", "")
        l["left_sec"] = int(float(l["until"] or 0) - now)
        l["expired"] = l["left_sec"] <= 0
        l["queue"] = [t["title"] for t in tasks
                      if (t.get("resource") or "").strip() == l["resource"]
                      and t["status"] in ("backlog", "blocked") and t["id"] != l["holder_task"]]
    refuted = {}
    for h in hyps:
        if h["verdict"] == "refuted":
            refuted.setdefault(h["claim_key"], h["id"])
    by_task = {}
    for h in hyps:
        by_task.setdefault(h["task_id"], []).append(h)
    for t in tasks:
        t["hypotheses"] = by_task.get(t["id"], [])

    # 唯一卡点：影响面最大的那条
    cands = [t for t in tasks if t["status"] == "blocked"] \
        or [t for t in tasks if t.get("dep_blocked")] \
        or [t for t in tasks if t["status"] == "doing" and t.get("stale_level") in ("warn", "crit")]
    blocker = None
    if cands:
        def _rank(t):
            kind = 2 if t["status"] == "blocked" else (1 if t.get("dep_blocked") else 0)
            return (kind, t["downstream"], now - _epoch(t.get("updated_at") or t["created_at"]))
        b0 = max(cands, key=_rank)
        kind = ("blocked" if b0["status"] == "blocked"
                else "waiting" if b0.get("dep_blocked") else "stalled")
        blocker = {"id": b0["id"], "title": b0["title"], "downstream": b0["downstream"],
                   "kind": kind,
                   # 停滞/卡住的时长按「最后一次更新」算，doing_since 会被重开刷新，不可靠
                   "since": int(max(0, now - _epoch(b0.get("updated_at") or b0["created_at"]))),
                   "reason": b0.get("blocked_reason")
                             or ("等 " + b0["dep_title"] if b0.get("dep_title") else
                                 ("已 0 进展" if kind == "stalled" else ""))}

    pending = list_approvals(pid, "pending")
    sreq = list_staff_requests(pid)
    prio_counts = {k: len([t for t in open_tasks if t["prio"] == k]) for k in (1, 2, 3)}
    return {"project": dict(p), "members": members, "tasks": tasks, "stats": stats,
            # 配比：总监填的配额 + 实际切出来的条数。看板顶部照这个显示，别让「全 P1」复发
            "priority_ratio": prio_ratio,
            "priority_quota": {"p1": q1, "p2": q12 - q1, "p3": len(open_tasks) - q12},
            "priority_counts": prio_counts,
            "priority_declared_p1": len([t for t in open_tasks
                                         if t.get("priority_declared") == 1]),
            # 编制：工位固定，会话可换。加人/换人/撤编都挂在这张申请单上等用户批
            "staff_requests": sreq,
            "staff_pending": [s for s in sreq if s["status"] == "pending"],
            "roster": {
                "seats": len(members),
                "vacant": len([m for m in members if m["roster_state"] == "vacant"]),
                "workers": len([m for m in members if not m.get("is_lead")]),
                "cap": TEAM_CAP,
                "swapped": len([m for m in members if m["bind_count"] > 1]),
            },
            "leases": leases, "hypotheses": hyps[:80], "criteria": crits,
            "criteria_green": len([x for x in crits if x["status"] == "green"]),
            # unknown（还没跑过判定）不进分母：否则「一条都没验」会被读成「全不达标」
            "criteria_total": len([x for x in crits if x["status"] in ("green", "red")]),
            "criteria_all": len(crits), "blocker": blocker,
            "pending_approvals": pending, "team_cap": TEAM_CAP,
            "roles": role_rows, "milestones": milestones,
            "resource_conflicts": conflicts,
            "goal_violations": viol[:200],
            "goal_score": stage_coverage,          # 兼容旧字段：阶段覆盖率
            "goal_coverage": stage_coverage,
            "stage_coverage": stage_coverage,
            "stage_scoped": len(scoped),
            "stage_total": len(tasks),
            # 达成度按全部任务算：项目只有一个目标，所有任务都算在目标里
            "goal_progress": (round(sum(safe_int(t.get("progress"), 0, 0, 100) * t["estimate"]
                                        for t in tasks if t["status"] != "abandoned")
                                    / sum(t["estimate"] for t in tasks
                                          if t["status"] != "abandoned"))
                              if any(t["status"] != "abandoned" and t["estimate"]
                                     for t in tasks) else 0),
            "remaining_hours": round(remaining, 1),
            "hours_left": (round(hours_left, 1) if hours_left is not None else None),
            "deadline_ts": deadline_ts,
            "eta_finish": eta_finish,
            "eta_slip_hours": eta_slip_hours,
            "critical_chain": crit_titles,
            "over_top": over_top,
            "estimate_bias": estimate_bias,
            "estimate_done_est": round(d_est, 1), "estimate_done_act": round(d_act, 1),
            "time_used_pct": time_used_pct,
            "pressure": pressure,
            "utilization": utilization,
            "retro": retro,
            "retro_required": bool(retro.get("required")),
            "now_ts": now,
            "over_budget": (remaining > hours_left) if hours_left is not None else False,
            "estimates_unfilled": estimates_all_one,
            "deliverables_missing": len([t for t in tasks
                                      if t["deliverable_ok"] is False
                                      and t["status"] in ("review", "done")]),
            "columns": COLUMNS, "col_label": COL_LABEL,
            "server_time": now_iso()}


# ------------------------------------------------------------------ write
def create_project(b):
    if not (b.get("deadline") or "").strip():
        return {"error": "缺少项目总时长：deadline 必填（例如 2026-10-30 18:00）。"
                         "没有截止时间就无法做时间压力闸和复盘。",
                "missing": ["deadline"]}
    pid = new_id("proj")
    with db() as c:
        c.execute("INSERT INTO projects(id,name,goal,lead,deadline,created_at,workdir,"
                  "goal_criteria,committed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (pid, b.get("name") or "未命名项目", b.get("goal", ""),
                   b.get("lead", ""), b.get("deadline", ""), now_iso(),
                   b.get("workdir", ""), b.get("goal_criteria", ""), time.time()))
        log_event(c, pid, "project.created", actor=b.get("lead", ""), payload=b.get("name", ""))
    return project_state(pid)


STAFF_KINDS = ("add", "replace", "remove")   # 加人 / 换人 / 撤编


def ensure_seats(pid=None):
    """给没有工位号的老成员补号：按入职先后排，号码只增不减。

    工位号是「原班人马」的唯一凭证——会话会换，工位不会。
    """
    with db() as c:
        projs = ([pid] if pid else
                 [r["project_id"] for r in c.execute(
                     "SELECT DISTINCT project_id FROM members")])
    for pj in projs:
        with db() as c:
            rows = c.execute("SELECT id, seat_no FROM members WHERE project_id=?"
                             " ORDER BY created_at, id", (pj,)).fetchall()
            used = {int(r["seat_no"] or 0) for r in rows if int(r["seat_no"] or 0) > 0}
            nxt = 1
            for r in rows:
                if int(r["seat_no"] or 0) > 0:
                    continue
                while nxt in used:
                    nxt += 1
                c.execute("UPDATE members SET seat_no=? WHERE id=?", (nxt, r["id"]))
                used.add(nxt)
            # 老数据里的会话也补进台账，否则「接管过几次」永远是 0
            for r in c.execute("SELECT id, thread_id, bound_at, created_at FROM members"
                               " WHERE project_id=? AND COALESCE(thread_id,'')<>''", (pj,)).fetchall():
                has = c.execute("SELECT 1 FROM member_threads WHERE member_id=?", (r["id"],)).fetchone()
                if has:
                    continue
                ts = float(r["bound_at"] or 0) or _epoch(r["created_at"]) or time.time()
                c.execute("INSERT INTO member_threads(id,project_id,member_id,thread_id,bound_at,reason)"
                          " VALUES(?,?,?,?,?,?)",
                          (new_id("thr"), pj, r["id"], r["thread_id"], ts, "历史登记"))
                c.execute("UPDATE members SET bound_at=?,"
                          " bind_count=(SELECT COUNT(*) FROM member_threads WHERE member_id=?)"
                          " WHERE id=?", (ts, r["id"], r["id"]))


def bind_member(pid, mid, thread_id, reason=""):
    """接管工位：换会话，不换人。

    子代理被关闭、重开一个新的，只是换了一段执行会话：
    员工身份、工位号、历史任务、工时、证据全部留在原工位上。
    """
    thread_id = (thread_id or "").strip()
    if not thread_id:
        return {"error": "缺 thread_id：spawn 完子代理，把它的 threadId 传进来接管工位"}
    with db() as c:
        m = c.execute("SELECT * FROM members WHERE id=? AND project_id=?", (mid, pid)).fetchone()
        if not m:
            return {"error": f"编制里没有这个员工：{mid}", "not_found": True}
        # 一个会话只能坐一个工位：把占着同一会话的其他人先撤下来
        for s in c.execute("SELECT id, name, seat_no FROM members"
                           " WHERE project_id=? AND thread_id=? AND id<>?",
                           (pid, thread_id, mid)).fetchall():
            c.execute("UPDATE members SET thread_id='' WHERE id=?", (s["id"],))
            c.execute("UPDATE member_threads SET released_at=?"
                      " WHERE member_id=? AND released_at IS NULL", (time.time(), s["id"]))
            log_event(c, pid, "member.thread_moved", actor=s["id"], payload=s["name"])
        old = (m["thread_id"] or "").strip()
        binds = int(m["bind_count"] or 1)
        if old == thread_id:
            return {"ok": True, "id": mid, "name": m["name"], "thread_id": thread_id,
                    "seat_no": int(m["seat_no"] or 0), "bind_count": binds, "unchanged": True}
        now = time.time()
        c.execute("UPDATE member_threads SET released_at=?"
                  " WHERE member_id=? AND released_at IS NULL", (now, mid))
        c.execute("INSERT INTO member_threads(id,project_id,member_id,thread_id,bound_at,reason)"
                  " VALUES(?,?,?,?,?,?)",
                  (new_id("thr"), pid, mid, thread_id, now, reason or "接管工位"))
        c.execute("UPDATE members SET thread_id=?, bound_at=?, bind_count=?, last_seen=?,"
                  " roster_state=CASE WHEN roster_state IN ('','vacant') THEN 'active'"
                  "                   ELSE roster_state END WHERE id=?",
                  (thread_id, now, binds + 1, now, mid))
        log_event(c, pid, "member.rebind", actor=mid,
                  payload=f"工位#{int(m['seat_no'] or 0)} {old or '(空)'} → {thread_id}")
    return {"ok": True, "id": mid, "name": m["name"], "thread_id": thread_id,
            "seat_no": int(m["seat_no"] or 0), "bind_count": binds + 1,
            "prev_thread": old}


def find_member(pid, ref):
    """按 id / 姓名 / 工位号找员工。总监嘴上说的是名字，手里未必有 id。"""
    ref = (ref or "").strip()
    if not ref:
        return None
    with db() as c:
        r = c.execute("SELECT * FROM members WHERE project_id=? AND id=?", (pid, ref)).fetchone()
        if r:
            return dict(r)
        r = c.execute("SELECT * FROM members WHERE project_id=? AND name=?", (pid, ref)).fetchone()
        if r:
            return dict(r)
        if ref.isdigit():
            r = c.execute("SELECT * FROM members WHERE project_id=? AND seat_no=?",
                          (pid, int(ref))).fetchone()
            if r:
                return dict(r)
        r = c.execute("SELECT * FROM members WHERE project_id=? AND name LIKE ?",
                      (pid, f"%{ref}%")).fetchone()
    return dict(r) if r else None


def create_staff_request(pid, b):
    """编制变更申请：加人 / 换人 / 撤编都必须先过用户这一关。"""
    kind = (b.get("kind") or "").strip()
    if kind not in STAFF_KINDS:
        return {"error": "kind 只能是 add（加人）/ replace（换人）/ remove（撤编）",
                "field": "kind"}
    if not (b.get("reason") or "").strip():
        return {"error": "缺理由：为什么现在必须动编制（人力不够 / 有人卡住 / 岗位不匹配）",
                "field": "reason"}
    m = None
    if kind in ("replace", "remove"):
        m = find_member(pid, b.get("member") or b.get("member_id") or "")
        if not m:
            return {"error": "replace / remove 必须指定 staff.member（姓名、工位号或 id）"}
    payload = {k: b.get(k) for k in ("name", "role", "model", "effort", "thread_id")
               if b.get(k)}
    rid = new_id("stf")
    with db() as c:
        c.execute("INSERT INTO staff_requests(id,project_id,kind,member_id,payload,reason,status,created_at)"
                  " VALUES(?,?,?,?,?,?, 'pending', ?)",
                  (rid, pid, kind, (m or {}).get("id", ""),
                   json.dumps(payload, ensure_ascii=False), b.get("reason", ""), now_iso()))
        log_event(c, pid, "staff.requested", payload=f"{kind} {b.get('reason','')}")
    verb = {"add": "加人", "replace": "换人", "remove": "撤编"}[kind]
    return {"ok": True, "id": rid, "kind": kind, "status": "pending",
            "member": (m or {}).get("name", ""),
            "seat_no": int((m or {}).get("seat_no") or 0),
            "summary": f"申请{verb}：{m['name'] if m else b.get('name','')} · {b.get('reason','')}",
            "need_approval": True,
            "next": "等用户在看板上点批准（或 POST /api/projects/<pid>/staff-requests/<id>/decide）"}


def decide_staff_request(rid, b):
    """用户批/驳一条编制变更。批准才真正改编制。"""
    decision = b.get("decision")
    if decision not in ("approve", "reject"):
        return {"error": "decision 只能是 approve / reject"}
    with db() as c:
        r = c.execute("SELECT * FROM staff_requests WHERE id=?", (rid,)).fetchone()
    if not r:
        return None
    if r["status"] != "pending":
        return {"error": f"这张申请已经处理过（{r['status']}）"}
    status = "approved" if decision == "approve" else "rejected"
    with db() as c:
        cur = c.execute("UPDATE staff_requests SET status=?, decided_at=?, decided_by=?"
                        " WHERE id=? AND status='pending'",
                        (status, now_iso(), b.get("by", "user"), rid))
        if cur.rowcount != 1:
            return {"error": "该申请已被处理，不能重复提交"}
    if decision == "reject":
        return {"ok": True, "id": rid, "status": status, "note": "已驳回，编制不变"}

    pid, kind = r["project_id"], r["kind"]
    payload = json.loads(r["payload"] or "{}")
    if kind == "add":
        mb = dict(payload)
        # 用户已经批过这张扩编单，落库时不该再被 TEAM_CAP 挡一次
        mb.update({"seat_approved": True, "size_approved": True,
                   "model_confirmed": True, "kind": "agent"})
        mb.setdefault("name", "未命名员工")
        out = add_member(pid, mb)
        if isinstance(out, dict) and "error" in out:
            with db() as c:
                c.execute("UPDATE staff_requests SET status='pending', decided_at='' WHERE id=?", (rid,))
            return {"error": "加人落库失败，已退回待审批", "detail": out}
        return {"ok": True, "id": rid, "status": status, "member": out}
    mid = r["member_id"]
    if kind == "replace":
        sets, vals = [], []
        for k in ("name", "role", "model", "effort"):
            if payload.get(k):
                sets.append(f"{k}=?")
                vals.append(payload[k])
        sets += ["thread_id=''", "roster_state='vacant'", "duty_until=0"]
        vals.append(mid)
        with db() as c:
            c.execute(f"UPDATE members SET {', '.join(sets)} WHERE id=?", vals)
            c.execute("UPDATE member_threads SET released_at=?"
                      " WHERE member_id=? AND released_at IS NULL", (time.time(), mid))
            log_event(c, pid, "member.replaced", actor=mid, payload=json.dumps(payload, ensure_ascii=False))
        return {"ok": True, "id": rid, "status": status, "note": "工位保留，等新子代理接管",
                "next": f"spawn 之后调 bind_member(member='{mid}', thread_id='<新 id>') 接管该工位"}
    if kind == "remove":
        with db() as c:
            c.execute("UPDATE members SET roster_state='vacant', thread_id='', duty_until=0"
                      " WHERE id=?", (mid,))
            c.execute("UPDATE member_threads SET released_at=?"
                      " WHERE member_id=? AND released_at IS NULL", (time.time(), mid))
            c.execute("UPDATE tasks SET status='backlog', assignee_id=''"
                      " WHERE assignee_id=? AND status IN ('doing','blocked')", (mid,))
            log_event(c, pid, "member.removed", actor=mid, payload=r["reason"])
        return {"ok": True, "id": rid, "status": status,
                "note": "工位作废，在办任务退回待办池"}
    return {"ok": True, "id": rid, "status": status}


def list_staff_requests(pid):
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM staff_requests WHERE project_id=? ORDER BY created_at DESC", (pid,))]
    for r in rows:
        try:
            r["payload"] = json.loads(r["payload"] or "{}")
        except Exception:
            r["payload"] = {}
        if r["member_id"]:
            with db() as c:
                m = c.execute("SELECT name, seat_no, role FROM members WHERE id=?",
                              (r["member_id"],)).fetchone()
            if m:
                r["member_name"] = m["name"]
                r["seat_no"] = int(m["seat_no"] or 0)
                r["member_role"] = m["role"]
    return rows


def _roster_gate(pid, b):
    """新建员工的闸：编制已成型时，只有审批过的申请才许进人。"""
    if b.get("seat_approved") or b.get("size_approved"):
        return None
    with db() as c:
        active = c.execute("SELECT COUNT(*) FROM members WHERE project_id=?"
                           " AND COALESCE(roster_state,'active')<>'vacant'", (pid,)).fetchone()[0]
    if active <= 0:
        return None      # 空团队：第一波建班子走计划审批，这里放行
    return {"error": "编制已经成型，不能随手加人：员工会一直在变，看板就失去意义。"
                     "要加人调 request_staff_change(kind='add', ...)，用户在面板批准后才落库。",
            "need_approval": True, "staff_request_required": True}


def add_member(pid, b):
    gate = retro_gate(pid)
    if gate:
        return gate
    err = check_model(b.get("model", "")) or check_effort(b.get("model", ""), b.get("effort", ""))
    if err:
        return {"error": err, "field": "effort"}
    if b.get("model") and not b.get("model_confirmed"):
        return {"error": "指定模型必须先由用户确认：先问用户用当前会话模型还是指定模型，"
                         "确认后再带 model_confirmed=true 提交", "field": "model"}
    # 同名的已经在编：这是「换了会话」，不是「新来一个人」，让他接管自己的工位。
    nm = (b.get("name") or "").strip()
    if nm:
        with db() as c:
            same = c.execute("SELECT id, name, seat_no FROM members WHERE project_id=? AND name=?",
                             (pid, nm)).fetchone()
        if same:
            if b.get("seat_approved") or b.get("size_approved"):
                return next(m for m in project_state(pid)["members"] if m["id"] == same["id"])
            return {"error": f"编制里已经有「{same['name']}」（工位 #{int(same['seat_no'] or 0)}）。"
                             f"关掉子代理再开一个新的，不是换员工，是换会话："
                             f"调 bind_member(member='{same['name']}', thread_id='<新会话id>') 让他接管这个工位。",
                    "use_bind": True, "member_id": same["id"],
                    "seat_no": int(same["seat_no"] or 0)}
    # 编制已成型时不许随手加人：加人必须走审批，否则人数会一直漂
    rg = _roster_gate(pid, b)
    if rg:
        return rg
    with db() as c:
        # 空工位（撤编/换人后 roster_state='vacant'）不占编制：否则撤一个人就再也补不回来
        n = c.execute("SELECT COUNT(*) FROM members WHERE project_id=?"
                      " AND COALESCE(is_lead,0)=0"
                      " AND COALESCE(roster_state,'active')='active'", (pid,)).fetchone()[0]
    if n >= TEAM_CAP and not b.get("size_approved"):
        return {"error": f"员工上限 {TEAM_CAP} 人（不含项目总监），要开到第 {n+1} 个员工"
                         f"必须先让用户审批：问过用户再带 size_approved=true 提交（或走计划审批）",
                "field": "capacity", "need_approval": True}
    mid = new_id("mem")
    now = time.time()
    with db() as c:
        seat = (c.execute("SELECT COALESCE(MAX(seat_no),0)+1 FROM members WHERE project_id=?",
                          (pid,)).fetchone()[0] or 1)
        tid = (b.get("thread_id") or "").strip()
        c.execute("INSERT INTO members(id,project_id,name,role,kind,model,effort,state,capacity,"
                  "last_seen,created_at,is_lead,thread_id,seat_no,roster_state,bound_at,bind_count)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (mid, pid, b.get("name") or "未命名", cn_role(b.get("role", "")),
                   b.get("kind", "human"), b.get("model", ""), b.get("effort", ""), "idle",
                   int(b.get("capacity", 3)), now, now_iso(),
                   1 if b.get("is_lead") else 0, tid, int(seat),
                   "active", now if tid else 0, 1 if tid else 0))
        if tid:
            c.execute("INSERT INTO member_threads(id,project_id,member_id,thread_id,bound_at,reason)"
                      " VALUES(?,?,?,?,?,?)",
                      (new_id("thr"), pid, mid, tid, now, "入职即绑定"))
        log_event(c, pid, "member.joined", actor=mid, payload=b.get("name", ""))
    return next(m for m in project_state(pid)["members"] if m["id"] == mid)


def check_links(c, pid, assignee_id, depends_on):
    """负责人和依赖都必须落在本项目里：跨项目指派会让两边的负载/依赖图都对不上。"""
    assignee_id = (assignee_id or "").strip()
    depends_on = (depends_on or "").strip()
    own = c is not None
    try:
        if not own:
            c = sqlite3.connect(DB_PATH, timeout=15)
            c.row_factory = sqlite3.Row
        if assignee_id:
            row = c.execute("SELECT 1 FROM members WHERE id=? AND project_id=?",
                            (assignee_id, pid)).fetchone()
            if not row:
                return {"error": f"负责人 {assignee_id} 不在本项目成员里（跨项目指派会被拒绝）",
                        "field": "assignee_id"}
        if depends_on:
            row = c.execute("SELECT 1 FROM tasks WHERE id=? AND project_id=?",
                            (depends_on, pid)).fetchone()
            if not row:
                return {"error": f"依赖 {depends_on} 不是本项目的任务（悬空依赖会被拒绝）",
                        "field": "depends_on"}
        return None
    finally:
        if not own and c is not None:
            c.close()


def add_task(pid, b):
    tid = new_id("task")
    status = b.get("status", "backlog")
    if status not in COLUMNS:
        return {"error": f"status must be one of {COLUMNS}"}
    bad = check_links(None, pid, b.get("assignee_id"), b.get("depends_on"))
    if bad:
        return bad
    # 目标闸：项目写了目标，新任务就必须挂在阶段上，禁止无主任务
    with db() as c0:
        g = c0.execute("SELECT goal FROM projects WHERE id=?", (pid,)).fetchone()
    if g and (g["goal"] or "").strip() and not b.get("allow_unscoped"):
        miss = [k for k in ("milestone", "deliverable", "dod")
                if not str(b.get(k) or "").strip()]
        if miss:
            return {"error": "项目已有目标，新任务必须写明这些字段才能建：" + "、".join(miss),
                    "field": "scope", "missing": miss}
    prog = safe_int(b.get("progress", 100 if status == "done" else 0), 0, 0, 100)
    with db() as c:
        if status == "doing" and (b.get("resource") or "").strip() and not b.get("force"):
            h = resource_holder(c, pid, b.get("resource"))
            if h:
                return {"error": f"资源 {b['resource']} 已被「{h['title']}」占用（doing），"
                                 "换资源或先让占用方收工；确认强行共享再带 force=true",
                        "field": "resource", "need_approval": True,
                        "holder": {"id": h["id"], "title": h["title"]}}
        mx = c.execute("SELECT COALESCE(MAX(sort_order),0) FROM tasks WHERE project_id=?",
                       (pid,)).fetchone()[0]
        c.execute("""INSERT INTO tasks(id,project_id,title,detail,role,assignee_id,status,priority,
                     sort_order,deliverable,dod,depends_on,estimate,progress,blocked_reason,
                     created_at,updated_at,delivered_at,milestone,resource,evidence,doing_since)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (tid, pid, b.get("title") or "未命名任务", b.get("detail", ""),
                   cn_role(b.get("role", "")), b.get("assignee_id") or None, status,
                   safe_int(b.get("priority", 2), 2, 1, 3), mx + 100, b.get("deliverable", ""),
                   b.get("dod", ""), b.get("depends_on", ""), max(0.1, safe_float(b.get("estimate", 1), 1)),
                   prog, b.get("blocked_reason", ""), now_iso(), now_iso(),
                   now_iso() if status == "done" else "", b.get("milestone", ""),
                   b.get("resource", ""), b.get("evidence", "") if b.get("evidence") in EVIDENCE else "",
                   time.time() if status == "doing" else 0))
        log_event(c, pid, "task.created", actor=b.get("assignee_id", ""), task_id=tid,
                  payload=b.get("title", ""))
        if status == "doing":
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            open_session(c, row)
    return next(t for t in project_state(pid)["tasks"] if t["id"] == tid)


def update_task(tid, b):
    with db() as c:
        t = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t:
            return None
        if "status" in b and b["status"] not in COLUMNS:
            return {"error": f"status must be one of {COLUMNS}"}
        # 字段归一：脏数据不入库
        for k in ("progress", "priority"):
            if k in b:
                b[k] = safe_int(b[k], 0, 0, 100) if k == "progress" else safe_int(b[k], 2, 1, 3)
        if "estimate" in b:
            b["estimate"] = max(0.1, safe_float(b["estimate"], 1))
        if "evidence" in b and b["evidence"] not in EVIDENCE and b["evidence"] != "":
            return {"error": f"evidence must be one of {EVIDENCE}"}
        if "milestone" in b and not str(b.get("milestone") or "").strip():
            with db() as c0:
                g = c0.execute("SELECT goal FROM projects WHERE id=?",
                               (t["project_id"],)).fetchone()
            if g and (g["goal"] or "").strip():
                return {"error": "项目已有目标，任务的阶段不能清空", "field": "milestone"}

        # 换负责人 / 换依赖同样要守项目边界：跨项目指派会让两边的负载和依赖图都对不上
        if "assignee_id" in b or "depends_on" in b:
            bad = check_links(c, t["project_id"],
                              b.get("assignee_id") if "assignee_id" in b else t["assignee_id"],
                              b.get("depends_on") if "depends_on" in b else t["depends_on"])
            if bad:
                return bad
        # 自己不能依赖自己：环形依赖会让排期永远算不出完成时间
        if (b.get("depends_on") or "").strip() == tid:
            return {"error": "任务不能依赖自己", "field": "depends_on"}

        st = b.get("status")
        paused_flag = b.pop("paused", None)
        # 资源硬约束：同一资源不能同时被两个 doing 任务占用
        eff_res = (b.get("resource") if "resource" in b else t["resource"]) or ""
        if (st == "doing" or (st == t["status"] == "doing")) and eff_res.strip() \
                and not b.get("force"):
            with db() as c2:
                h = resource_holder(c2, t["project_id"], eff_res, t["id"])
            if h:
                return {"error": f"资源 {eff_res} 已被「{h['title']}」占用（doing）；"
                                 "先让占用方收工，或确认共享后再带 force=true",
                        "field": "resource", "need_approval": True,
                        "holder": {"id": h["id"], "title": h["title"]}}
        # 放弃必须写原因：换人/换模型接手时，这条比任何进度都有用
        if st == "abandoned":
            reason = (b.get("abandoned_reason") or t["abandoned_reason"] or "").strip()
            if not reason:
                return {"error": "标放弃必须写 abandoned_reason（为什么放弃、卡在哪）",
                        "field": "abandoned_reason"}
        # 无证据不许置 done：evidence 等级 + evidence_path 都必填，且路径要真的存在
        if st == "done" and not b.get("force"):
            ev = (b.get("evidence") or t["evidence"] or "").strip()
            ep = (b.get("evidence_path") or t["evidence_path"] or "").strip()
            if not ev:
                return {"error": "标完成必须带 evidence（observed / inferred / assumed）",
                        "field": "evidence", "need_evidence": True}
            if not ep:
                return {"error": "标完成必须带 evidence_path（证据落在哪个文件或目录）",
                        "field": "evidence_path", "need_evidence": True}
            _ok, any_ok, miss = check_deliverables(project_workdir(t["project_id"]), ep)
            if not any_ok:
                return {"error": "evidence_path 不存在：" + "、".join(miss),
                        "field": "evidence_path", "missing": miss, "need_evidence": True}
        # 交付物必须真实存在，才允许标记完成
        if st == "done":
            dl = (b.get("deliverable") or t["deliverable"] or "").strip()
            base = project_workdir(t["project_id"])
            all_ok, any_ok, missing = check_deliverables(base, dl)
            if dl and not any_ok:
                return {"error": "交付物一个都不存在，不能标完成：" + "、".join(missing),
                        "field": "deliverable", "missing": missing}
        # 证据等级：推断不能当已完成
        eff_ev = b.get("evidence", t["evidence"] or "")
        eff_prog = b.get("progress", 100 if st == "done" else t["progress"] or 0)
        if eff_ev == "inferred" and safe_int(eff_prog, 0) >= 90:
            return {"error": "证据等级是 inferred（推断），进度不能 ≥90%；补观测证据或改成 observed",
                    "field": "evidence"}
        sets, vals = [], []
        for k in TASK_FIELDS:
            if k in b:
                sets.append(f"{k}=?")
                vals.append(b[k])
        if st == "done":
            sets += ["progress=?", "delivered_at=?"]
            vals += [100, now_iso()]
        elif st in ("doing", "review", "backlog") and "progress" not in b and t["progress"] >= 100:
            sets.append("progress=?")
            vals.append(95 if st == "review" else 60)
        if st and st != t["status"]:
            sets.append("doing_since=?")
            vals.append(time.time() if st == "doing" else 0)
            # 状态迁移就结束暂停：否则「暂停期间不计时」只在地图上成立，
            # 后台还挂着一条 open session，下次收工会把这段墙钟全算进工时。
            if t["paused_at"]:
                waited = max(0.0, time.time() - float(t["paused_at"]))
                sets += ["paused_at=?", "paused_sec=COALESCE(paused_sec,0)+?"]
                vals += [0, waited]
        if st and st != "blocked" and "blocked_reason" not in b:
            sets.append("blocked_reason=?")
            vals.append("")
        sets.append("updated_at=?")
        vals.append(now_iso())
        vals.append(tid)
        c.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", vals)

        # 暂停 = 关掉计时但不改状态：等设备/等审批的墙钟时间不算工时，倒计时也不会被它拖红
        if paused_flag is True and not t["paused_at"]:
            close_sessions(c, tid)
            c.execute("UPDATE tasks SET paused_at=? WHERE id=?", (time.time(), tid))
            log_event(c, t["project_id"], "task.paused", actor=b.get("actor", ""), task_id=tid)
        elif paused_flag is False and t["paused_at"]:
            waited = max(0.0, time.time() - float(t["paused_at"]))
            c.execute("UPDATE tasks SET paused_at=0, paused_sec=COALESCE(paused_sec,0)+? WHERE id=?",
                      (waited, tid))
            log_event(c, t["project_id"], "task.resumed", actor=b.get("actor", ""), task_id=tid,
                      payload=f"等待 {int(waited)}s")
            if (st or t["status"]) == "doing":
                row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                if row and not row["paused_at"]:
                    open_session(c, row)
        if paused_flag is None and st == "doing" and t["status"] != "doing":
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            open_session(c, row, b.get("assignee_id") or t["assignee_id"] or "")
        elif st and st != "doing" and t["status"] == "doing":
            close_sessions(c, tid)

        if st and t["assignee_id"]:
            if st == "doing":
                c.execute("UPDATE members SET state='working', last_seen=? WHERE id=?",
                          (time.time(), t["assignee_id"]))
            elif st == "blocked":
                c.execute("UPDATE members SET state='blocked', last_seen=? WHERE id=?",
                          (time.time(), t["assignee_id"]))
            elif st in ("done", "review", "backlog"):
                left = c.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND status='doing'",
                                 (t["assignee_id"],)).fetchone()[0]
                if left == 0:
                    c.execute("UPDATE members SET state='idle', last_seen=? WHERE id=?",
                              (time.time(), t["assignee_id"]))
        # 记下到底改了什么：只写 blocked_reason 的话，事后追不到「6820 是谁写成 Disable 的」
        _skip = {"actor", "force", "paused", "confirm_over_cap"}
        diff = {k: [t[k] if k in t.keys() else None, v]
                for k, v in b.items() if k not in _skip and (t[k] if k in t.keys() else None) != v}
        if st and st == "blocked" and b.get("blocked_reason"):
            diff.setdefault("blocked_reason", ["", b.get("blocked_reason")])
        log_event(c, t["project_id"], f"task.{st or 'updated'}",
                  actor=b.get("actor", ""), task_id=tid,
                  payload=json.dumps(diff, ensure_ascii=False)[:2000])
        pid = t["project_id"]
    return next((x for x in project_state(pid)["tasks"] if x["id"] == tid), None)


def update_member(mid, b):
    # 会话登记走 bind_member：要留接管台账，不能当成普通字段覆盖
    if (b.get("thread_id") or "").strip():
        with db() as c:
            row = c.execute("SELECT project_id FROM members WHERE id=?", (mid,)).fetchone()
        if row:
            r = bind_member(row["project_id"], mid, b["thread_id"], b.get("reason", ""))
            if "error" in r:
                return r
            b = {k: v for k, v in b.items() if k != "thread_id"}
            if not b:
                return next((x for x in project_state(row["project_id"])["members"]
                             if x["id"] == mid), None)
    with db() as c:
        m = c.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
        if not m:
            return None
        err = check_model(b.get("model", m["model"])) or check_effort(b.get("model", m["model"]), b.get("effort", m["effort"]))
        if not err and b.get("model") and b["model"] != m["model"] and not b.get("model_confirmed"):
            err = "改模型必须先由用户确认，确认后带 model_confirmed=true"
        if err:
            return {"error": err, "field": "effort"}
        sets, vals = [], []
        for k in ("name", "role", "state", "note", "capacity", "model", "effort", "kind"):
            if k in b:
                sets.append(f"{k}=?")
                vals.append(b[k])
        if "state" in b and b["state"] not in ("idle", "working", "blocked", "offline"):
            return {"error": "bad state"}
        sets.append("last_seen=?")
        vals.append(time.time())
        vals.append(mid)
        c.execute(f"UPDATE members SET {','.join(sets)} WHERE id=?", vals)
        log_event(c, m["project_id"], "member.update", actor=mid, payload=b.get("note", ""))
        pid = m["project_id"]
    return next((x for x in project_state(pid)["members"] if x["id"] == mid), None)


def heartbeat(mid):
    with db() as c:
        if not c.execute("SELECT 1 FROM members WHERE id=?", (mid,)).fetchone():
            return None
        c.execute("UPDATE members SET last_seen=? WHERE id=?", (time.time(), mid))
    return {"ok": True, "id": mid, "ts": now_iso()}


def heartbeat_project(pid, b):
    """总监批量刷心跳：只刷明确点名的人，避免把已停的子代理伪造成在线。

    b.members 可传成员 id 或姓名；不传则只刷「有 doing 任务」的成员。
    b.thread_id：调用方自己的会话 id。传了就顺手把总监绑到这个会话上
    （总监换了会话 = 同一把椅子换人，台账记录，工位号不变）。
    """
    who = b.get("members") or []
    if isinstance(who, str):
        who = [who]
    tid = (b.get("thread_id") or "").strip()
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, name, is_lead FROM members WHERE project_id=?", (pid,))]
        if not rows:
            return None
        lead = next((r for r in rows if int(r.get("is_lead") or 0) == 1), None)
        if tid and lead:
            # 只有「总监原来坐的那个会话已经死了」才自动搬到当前会话：
            # 活着的时候不乱搬，死掉的时候也不能让看板把在干活的总监显示成摸鱼。
            old_tid = ""
            with db() as c2:
                rr = c2.execute("SELECT thread_id FROM members WHERE id=?",
                                (lead["id"],)).fetchone()
                old_tid = ((rr["thread_id"] if rr else "") or "").strip()
            old_mt = session_mtimes().get(old_tid) if old_tid else 0.0
            # 绑定存**全 UUID**：短 id 打不开会话（深链要求完整 id），只是早期数据的样子
            same = bool(old_tid) and (old_tid == tid or old_tid == tid[:8]
                                      or tid.startswith(old_tid))
            if not same and (not old_mt or time.time() - old_mt > 1800):
                bind_member(pid, lead["id"], tid, reason="auto: 总监会话已停，跟到当前会话")
            who = list(who) + [lead["id"]]
        if who:
            want = {str(x).strip() for x in who if str(x).strip()}
            hit = [r["id"] for r in rows if r["id"] in want or r["name"] in want]
            unknown = sorted(want - {r["name"] for r in rows} - set(hit))
        else:
            hit = [r["id"] for r in c.execute(
                "SELECT DISTINCT assignee_id AS id FROM tasks"
                " WHERE project_id=? AND status='doing' AND assignee_id<>''", (pid,))]
            hit = [h["id"] for h in hit if h["id"]]
            unknown = []
        ts = time.time()
        for mid in hit:
            c.execute("UPDATE members SET last_seen=? WHERE id=?", (ts, mid))
    return {"ok": True, "project_id": pid, "touched": hit,
            "unknown": unknown, "ts": now_iso()}


def set_duty(mid, b):
    """在场租约：总监派活时登记，到期自动失效；done 后可显式关闭。"""
    on = bool(b.get("on", True))
    minutes = safe_int(b.get("minutes", 45), 45, 1, 720)
    with db() as c:
        m = c.execute("SELECT project_id FROM members WHERE id=?", (mid,)).fetchone()
        if not m:
            return None
        until = (time.time() + minutes * 60) if on else 0
        # 只有「登记在场」才算一次心跳；注销租约不能顺手刷新 last_seen，
        # 否则刚收工的人会凭这条写入再白拿 5 分钟在线。
        if on:
            c.execute("UPDATE members SET duty_until=?, last_seen=? WHERE id=?",
                      (until, time.time(), mid))
        else:
            c.execute("UPDATE members SET duty_until=0 WHERE id=?", (mid,))
        log_event(c, m["project_id"], "member.duty",
                  actor=mid, payload=("on" if on else "off") + f" {minutes}m")
        pid = m["project_id"]
    return {"ok": True, "id": mid, "on_duty": on,
            "duty_left": minutes * 60 if on else 0,
            "project_id": pid}


def resource_holder(c, project_id, resource, exclude_id=""):
    if not (resource or "").strip():
        return None
    return c.execute(
        "SELECT id,title FROM tasks WHERE project_id=? AND status='doing'"
        " AND TRIM(resource)=? AND id<>?", (project_id, resource.strip(), exclude_id)
    ).fetchone()


def list_events(pid, limit=80):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT ?", (pid, limit))]


def overview():
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY created_at DESC")]
    out = []
    for p in rows:
        st = project_state(p["id"])
        out.append({
            "id": p["id"], "name": p["name"], "goal": p.get("goal", ""),
            "lead": p.get("lead", ""), "deadline": p.get("deadline", ""),
            "deadline_ts": st.get("deadline_ts"), "hours_left": st.get("hours_left"),
            "time_used_pct": st.get("time_used_pct"),
            "pressure": st.get("pressure"),
            "retro_required": st.get("retro_required"),
            "retro_reason": (st.get("retro") or {}).get("reason", ""),
            "team_util": (st.get("utilization") or {}).get("team_pct"),
            # 首页卡片上的「待你批」角标：计划 + 复盘 + 编制变更全算上
            "pending_approvals": (len(st.get("pending_approvals") or [])
                                  + len(st.get("staff_pending") or [])
                                  + (1 if ((st.get("retro") or {}).get("latest") or {})
                                     .get("status") == "submitted" else 0)),
            "created_at": p["created_at"],
            "stats": {k: st["stats"][k] for k in
                      ("total", "done", "doing", "review", "blocked", "backlog",
                       "members", "online", "overall", "delivered", "deliverables")},
            "roles": [{"role": r["role"], "owners": r["owners"], "total": r["total"],
                       "progress": r["progress"]} for r in st["roles"]],
            "members": [{"id": m["id"], "name": m["name"], "role": m["role"],
                         "state": m["state"], "kind": m["kind"], "progress": m["progress"],
                         "current_task": m["current_task"],
                         "current_task_progress": next((t["progress"] for t in st["tasks"]
                            if t["assignee_id"] == m["id"] and t["status"] == "doing"), 0),
                         "blocked_task": next((t["title"] for t in st["tasks"]
                            if t["assignee_id"] == m["id"] and t["status"] == "blocked"), ""),
                         "blocked_reason": next((t["blocked_reason"] for t in st["tasks"]
                            if t["assignee_id"] == m["id"] and t["status"] == "blocked"), ""),
                         "load": m["load"], "online": m["online"],
                         "offline_for": m["offline_for"], "time_running": m["time_running"],
                         "time_total": m["time_total"],
                         "capacity": m["capacity"], "done_count": m["done_count"],
                         "total": m["total"]} for m in st["members"]],
        })
    agg = {"projects": len(out),
           "tasks": sum(o["stats"]["total"] for o in out),
           "members": sum(o["stats"]["members"] for o in out),
           "overall": round(sum(o["stats"]["overall"] for o in out) / len(out)) if out else 0,
           "blocked": sum(o["stats"]["blocked"] for o in out)}
    return {"projects": out, "agg": agg}


# ------------------------------------------------------------------ report
def markdown_report(pid):
    s = project_state(pid)
    if not s:
        return None
    p, st, ms, ts = s["project"], s["stats"], s["members"], s["tasks"]
    L = [f"# {p['name']} · 进度报告", "",
         f"- 生成时间：{now_iso()}",
         f"- 目标：{p.get('goal') or '（未填写）'}",
         f"- 负责人：{p.get('lead') or '（未填写）'}",
         f"- 截止：{p.get('deadline') or '（未设定）'}",
         f"- **总进度：{st['overall']}%**（完成 {st['done']}/{st['total']}）", "",
         "## 一、总览", "",
         f"| 状态 | 数量 |", "|---|---|"]
    for k in COLUMNS:
        L.append(f"| {COL_LABEL[k]} | {st.get(k, 0)} |")
    L += [f"| 成员 | {st['members']}（在线 {st['online']}） |", ""]

    L += ["## 二、按岗位", "", "| 岗位 | 负责人 | 任务 | 完成 | 进行中 | 阻塞 | 进度 |",
          "|---|---|---|---|---|---|---|"]
    for r in s["roles"]:
        L.append(f"| {r['role']} | {r['owners'] or '-'} | {r['total']} | {r['done']} | "
                 f"{r['doing']} | {r['blocked']} | {r['progress']}% |")
    L.append("")

    L += ["## 三、成员状态", "",
          "| 成员 | 角色 | 状态 | 当前任务 | 负载 | 完成 | 进度 | 利用率 |",
          "|---|---|---|---|---|---|---|---|"]
    state_cn = {"working": "工作中", "idle": "空闲", "blocked": "阻塞", "offline": "离线"}
    for m in ms:
        L.append(f"| {m['name']} | {m['role']} | {state_cn.get(m['state'], m['state'])} | "
                 f"{m['current_task'] or '-'} | {m['load']}/{m['capacity']} | "
                 f"{m['done_count']}/{m['total']} | {m['progress']}% | {m['util_pct']}% |")
    L.append("")

    L += ["## 四、任务明细", ""]
    for k in COLUMNS:
        items = [t for t in ts if t["status"] == k]
        if not items:
            continue
        L.append(f"### {COL_LABEL[k]}（{len(items)}）")
        L.append("")
        for t in items:
            who = next((m["name"] for m in ms if m["id"] == t["assignee_id"]), "未分配")
            L.append(f"- **{t['title']}** · {t['role'] or '未分类'} / {who} · {t['progress']}%")
            if t["deliverable"]:
                L.append(f"  - 产出：`{t['deliverable']}`")
            if t["dod"]:
                L.append(f"  - 验收：{t['dod']}")
            if t["dep_title"]:
                L.append(f"  - 依赖：{t['dep_title']}（{'已满足' if t['dep_done'] else '未完成'}）")
            if t["blocked_reason"]:
                L.append(f"  - ⛔ 阻塞：{t['blocked_reason']}")
        L.append("")

    pr = s.get("pressure") or {}
    ut = s.get("utilization") or {}
    rt = s.get("retro") or {}
    L += ["## 五、时间压力与强制复盘", "",
          f"- 压力等级：**{pr.get('level', '-')}**（{pr.get('reason', '-')}）",
          f"- 团队利用率：{ut.get('team_pct', 0)}%（{ut.get('workers', 0)} 名员工，"
          f"实测工时 {round((ut.get('working_sec') or 0) / 3600.0, 1)}h / "
          f"已流逝 {round((ut.get('elapsed_sec') or 0) / 3600.0, 1)}h）"]
    if ut.get("slackers"):
        L.append("- 摸鱼名单：" + "、".join(
            f"{x['name']}（{x['util_pct']}%）" for x in ut["slackers"]))
    if rt.get("required"):
        L.append(f"- ⛔ **强制复盘未完成**：{rt.get('reason')}；"
                 f"阻断 {'、'.join(rt.get('blocked_actions') or [])}")
    latest = rt.get("latest")
    if latest:
        L += ["", f"### 第 {latest['round']} 轮复盘（{latest['status']}）", ""]
        for q in RETRO_QUESTIONS:
            a = (latest.get("answers") or {}).get(q["key"]) or "-"
            L.append(f"- **{q['q']}** → {a}")
    L.append("")

    blocked = [t for t in ts if t["status"] == "blocked"]
    L += ["## 六、风险与待决策", ""]
    if blocked:
        for t in blocked:
            who = next((m["name"] for m in ms if m["id"] == t["assignee_id"]), "未分配")
            L.append(f"- ⛔ **{t['title']}**（{who}）：{t['blocked_reason'] or '未说明原因'}")
    else:
        L.append("- 无阻塞项")
    waiting = [t for t in ts if t["status"] == "review"]
    if waiting:
        L += ["", "待验收："]
        for t in waiting:
            L.append(f"- {t['title']} → `{t['deliverable'] or '未填产出路径'}`")
    L.append("")
    return "\n".join(L)


# ------------------------------------------------------------------ demo
def seed_demo():
    p = create_project({"name": "咖啡品牌官网 v1", "goal": "4 周内上线品牌官网，含内容体系、设计、开发与验收",
                        "lead": "你", "deadline": "2026-11-25"})
    pid = p["project"]["id"]
    people = [("统筹 · 阿禾", "项目统筹", "human", 4),
              ("调研 · 苏黎", "市场调研", "agent", 3),
              ("设计 · 陈默", "品牌与视觉", "agent", 3),
              ("开发 · 赵一鸣", "前端开发", "agent", 3),
              ("质检 · 何静", "测试验收", "human", 4)]
    ids = {}
    for name, role, kind, cap in people:
        mid = add_member(pid, {"name": name, "role": role, "kind": kind,
                               "capacity": cap})["id"]
        ids[role] = mid
        ids[name] = mid
    T = [
        ("竞品官网拆解（10 家）", "市场调研", "调研 · 苏黎", "done", 1, 3,
         "deliverables/竞品拆解.md", "覆盖 10 家，每家给 3 条可借鉴点", "", 100),
        ("目标用户访谈与画像", "市场调研", "调研 · 苏黎", "review", 1, 3,
         "deliverables/用户画像.md", "至少 5 位访谈，输出 2 个画像", "", 100),
        ("品牌调性与文案基调", "品牌与视觉", "设计 · 陈默", "done", 1, 2,
         "deliverables/品牌调性.md", "给出 3 组关键词与参考图", "", 100),
        ("首页视觉稿（桌面 + 移动）", "品牌与视觉", "设计 · 陈默", "doing", 1, 5,
         "deliverables/首页视觉稿.fig", "两种分辨率，含交互标注", "", 60),
        ("全站设计规范 v1", "品牌与视觉", "设计 · 陈默", "backlog", 2, 3,
         "deliverables/设计规范.md", "色板/字阶/间距/组件", "", 0),
        ("站点信息架构与页面清单", "项目统筹", "统筹 · 阿禾", "done", 1, 2,
         "deliverables/信息架构.md", "站点地图 + 每页目标", "", 100),
        ("内容排期与上线清单", "项目统筹", "统筹 · 阿禾", "doing", 2, 2,
         "deliverables/内容排期.csv", "到人、到天、可勾选", "", 40),
        ("前端脚手架与部署流水线", "前端开发", "开发 · 赵一鸣", "done", 1, 3,
         "site/", "本地跑通 + 一键部署", "", 100),
        ("首页与产品页实现", "前端开发", "开发 · 赵一鸣", "doing", 1, 6,
         "site/src/pages/", "与视觉稿像素级对齐，移动端适配", "", 45),
        ("CMS 接口联调", "前端开发", "开发 · 赵一鸣", "blocked", 2, 4,
         "site/src/api/", "接口文档齐全，含错误码", "", 20),
        ("验收清单与回归用例", "测试验收", "质检 · 何静", "backlog", 2, 3,
         "deliverables/验收清单.md", "覆盖全部页面与断点", "", 0),
        ("性能与可访问性基线", "测试验收", "质检 · 何静", "backlog", 3, 2,
         "deliverables/性能报告.md", "Lighthouse ≥ 90", "", 0),
    ]
    created = {}
    for title, role, who, status, prio, est, deliv, dod, dep, prog in T:
        t = add_task(pid, {"title": title, "role": role, "assignee_id": ids[who],
                           "status": status, "priority": prio, "estimate": est,
                           "deliverable": deliv, "dod": dod, "progress": prog})
        created[title] = t["id"]
    update_task(created["CMS 接口联调"],
                {"status": "blocked", "blocked_reason": "后端接口文档只给了 3/12 个端点，缺分页与错误码"})
    update_task(created["首页与产品页实现"], {"depends_on": created["首页视觉稿（桌面 + 移动）"]})
    update_task(created["验收清单与回归用例"], {"depends_on": created["全站设计规范 v1"]})
    return project_state(pid)


# ------------------------------------------------------------------ http
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "TeamBoard/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8", extra=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        # 只对本机来源回显 Origin：`*` 会让任意网站 fetch 读光看板（项目名、任务、
        # 成员会话 id）。非浏览器调用没有 Origin，不需要 CORS 头。
        o = self.headers.get("Origin")
        host = (urlparse(o).hostname or "").lower() if o else ""
        if o and host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            v = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {"__bad_body__": "JSON 解析失败，检查请求体"}
        if not isinstance(v, dict):
            return {"__bad_body__": "请求体必须是 JSON 对象（{...}），不能是数组或标量"}
        return v

    def _static(self, path):
        if path in ("/", ""):
            rel = "home.html"
        elif path == "/board":
            rel = "index.html"
        elif path == "/panel":
            # 插件包里面板在 ui/panel.html；单机开发时退回 static/panel.html
            for cand in (os.path.join(UI_DIR, "panel.html"),
                         os.path.join(STATIC, "panel.html")):
                if os.path.isfile(cand):
                    with open(cand, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
            return self._send(404, {"error": "panel.html not found"})
        else:
            rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        with open(full, "rb") as f:
            self._send(200, f.read(), MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

    def _origin_ok(self):
        """写接口只接受本机来源：挡住任意网页发起的 CSRF。

        注意：`Origin: null` **不能**放行——任何 `<iframe sandbox>` 里的脚本，
        以及任何 `file://` 页面，发出来的 Origin 都是 null。放行 null 等于
        「任意网页都能建/删项目」。MCP App 面板走 tb_api 代理（无 Origin 头），
        不依赖这条放行。
        """
        o = self.headers.get("Origin")
        if not o:
            return True  # curl / MCP 等非浏览器调用
        h = (urlparse(o).hostname or "").lower()
        return h in ("127.0.0.1", "localhost", "::1", "0.0.0.0")

    def do_OPTIONS(self):
        return self._send(204, b"")

    def do_GET(self):
        try:
            return self._do_GET()
        except Exception as e:
            try:
                return self._send(500, {"error": f"internal: {e}"})
            except Exception:
                return None

    def _do_GET(self):
        u = urlparse(self.path); p, q = u.path, parse_qs(u.query)
        if not p.startswith("/api/"):
            return self._static(p)
        if p == "/api/health":
            return self._send(200, {"app": "teamboard", "version": VERSION,
                                    "db": DB_PATH, "pid": os.getpid(),
                                    "uptime_sec": int(time.time() - STARTED_AT)})
        if p == "/api/projects":
            with db() as c:
                return self._send(200, [dict(r) for r in c.execute(
                    "SELECT * FROM projects ORDER BY created_at DESC")])
        if p == "/api/models":
            return self._send(200, model_catalog())
        if p == "/api/overview":
            return self._send(200, overview())
        m = re.fullmatch(r"/api/projects/([\w]+)", p)
        if m:
            st = project_state(m.group(1))
            return self._send(200 if st else 404, st or {"error": "no such project"})
        m = re.fullmatch(r"/api/tasks/([\w]+)/time", p)
        if m:
            with db() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM task_sessions WHERE task_id=? ORDER BY started_at DESC LIMIT 100",
                    (m.group(1),))]
            for r in rows:
                r["seconds"] = int(r["seconds"] or (time.time() - r["started_at"] if not r["ended_at"] else 0))
            return self._send(200, {"task_id": m.group(1), "sessions": rows,
                                    "total": sum(int(r["seconds"] or 0) for r in rows)})
        m = re.fullmatch(r"/api/projects/([\w]+)/approvals", p)
        if m:
            return self._send(200, list_approvals(m.group(1), q.get("status", [""])[0]))
        m = re.fullmatch(r"/api/projects/([\w]+)/staff-requests", p)
        if m:
            return self._send(200, {"staff_requests": list_staff_requests(m.group(1))})
        m = re.fullmatch(r"/api/approvals/([\w]+)", p)
        if m:
            a = get_approval(m.group(1))
            return self._send(200 if a else 404, a or {"error": "no approval"})
        m = re.fullmatch(r"/api/projects/([\w]+)/events", p)
        if m:
            return self._send(200, list_events(m.group(1), safe_int(q.get("limit", ["80"])[0], 80, 1, 500)))
        m = re.fullmatch(r"/api/projects/([\w]+)/retros", p)
        if m:
            st = project_state(m.group(1))
            if not st:
                return self._send(404, {"error": "no such project"})
            return self._send(200, {"retro": st["retro"], "pressure": st["pressure"],
                                    "utilization": st["utilization"]})
        if p == "/api/open-thread":
            return self._send(200, open_thread((q.get("thread") or [""])[0]))
        m = re.fullmatch(r"/api/projects/([\w]+)/threads", p)
        if m:
            r = threads_for_project(m.group(1))
            return self._send(200 if r else 404, r or {"error": "no such project"})
        m = re.fullmatch(r"/api/projects/([\w]+)/report", p)
        if m:
            gate = retro_gate(m.group(1))
            if gate:
                gate["error"] = "导出被拦：" + gate["error"]
                return self._send(403, gate)
            md = markdown_report(m.group(1))
            if md is None:
                return self._send(404, {"error": "no such project"})
            extra = {}
            if "download" in q:
                extra["Content-Disposition"] = 'attachment; filename="progress.md"'
            return self._send(200, md.encode(), "text/markdown; charset=utf-8", extra)
        return self._send(404, {"error": "no route"})

    def do_POST(self):
        try:
            return self._do_POST()
        except Exception as e:
            try:
                return self._send(500, {"error": f"internal: {e}"})
            except Exception:
                return None

    def _do_POST(self):
        if not self._origin_ok():
            return self._send(403, {"error": "cross-origin write blocked"})
        p = urlparse(self.path).path; b = self._body()
        if b.get("__bad_body__"):
            return self._send(400, {"error": b["__bad_body__"]})
        if p == "/api/projects":
            r = create_project(b)
            # 校验失败必须是 400：回 201 + {"error":…} 会让调用方以为建成功了
            return self._send(400 if isinstance(r, dict) and "error" in r else 201, r)
        if p == "/api/demo":
            return self._send(201, seed_demo())
        m = re.fullmatch(r"/api/tasks/([\w]+)/time", p)
        if m:
            with db() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM task_sessions WHERE task_id=? ORDER BY started_at DESC LIMIT 100",
                    (m.group(1),))]
            for r in rows:
                r["seconds"] = int(r["seconds"] or (time.time() - r["started_at"] if not r["ended_at"] else 0))
            return self._send(200, {"task_id": m.group(1), "sessions": rows,
                                    "total": sum(int(r["seconds"] or 0) for r in rows)})
        m = re.fullmatch(r"/api/projects/([\w]+)/resources/([^/]+)/claim", p)
        if m:
            r = claim_resource(m.group(1), m.group(2), b)
            return self._send(400 if "error" in r else 200, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/resources/([^/]+)/release", p)
        if m:
            r = release_resource(m.group(1), m.group(2), b)
            return self._send(400 if "error" in r else 200, r)
        m = re.fullmatch(r"/api/tasks/([\w]+)/hypotheses", p)
        if m:
            r = add_hypothesis(m.group(1), b)
            return self._send(404 if r is None else (400 if "error" in r else 201), r or {"error": "no task"})
        m = re.fullmatch(r"/api/projects/([\w]+)/criteria", p)
        if m:
            r = upsert_criteria(m.group(1), b)
            return self._send(201, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/retros", p)
        if m:
            r = submit_retro(m.group(1), b)
            return self._send(400 if "error" in r else 201, r)
        m = re.fullmatch(r"/api/retros/([\w]+)/decide", p)
        if m:
            r = decide_retro(m.group(1), b)
            return self._send(400 if "error" in r else 200, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/approvals", p)
        if m:
            r = create_approval(m.group(1), b)
            return self._send(400 if "error" in r else 201, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/members", p)
        if m:
            r = add_member(m.group(1), b)
            return self._send(400 if isinstance(r, dict) and "error" in r else 201, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/members/([\w]+)/bind", p)
        if m:
            r = bind_member(m.group(1), m.group(2), b.get("thread_id", ""), b.get("reason", ""))
            return self._send(400 if r.get("error") else 200, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/staff-requests", p)
        if m:
            r = create_staff_request(m.group(1), b)
            return self._send(400 if r.get("error") else 201, r)
        m = re.fullmatch(r"/api/staff-requests/([\w]+)/decide", p)
        if m:
            r = decide_staff_request(m.group(1), b)
            if r is None:
                return self._send(404, {"error": "no staff request"})
            return self._send(400 if r.get("error") else 200, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/tasks", p)
        if m:
            r = add_task(m.group(1), b)
            return self._send(400 if isinstance(r, dict) and "error" in r else 201, r)
        m = re.fullmatch(r"/api/approvals/([\w]+)/decide", p)
        if m:
            r = decide_approval(m.group(1), b)
            if r is None:
                return self._send(404, {"error": "no approval"})
            return self._send(400 if "error" in r else 200, r)
        m = re.fullmatch(r"/api/members/([\w]+)/duty", p)
        if m:
            r = set_duty(m.group(1), b)
            return self._send(200 if r else 404, r or {"error": "no member"})
        m = re.fullmatch(r"/api/members/([\w]+)/heartbeat", p)
        if m:
            r = heartbeat(m.group(1))
            return self._send(200 if r else 404, r or {"error": "no member"})
        m = re.fullmatch(r"/api/projects/([\w]+)/heartbeat", p)
        if m:
            r = heartbeat_project(m.group(1), b)
            return self._send(200 if r else 404, r or {"error": "no project"})
        return self._send(404, {"error": "no route"})

    def do_PATCH(self):
        try:
            return self._do_PATCH()
        except Exception as e:
            try:
                return self._send(500, {"error": f"internal: {e}"})
            except Exception:
                return None

    def _do_PATCH(self):
        if not self._origin_ok():
            return self._send(403, {"error": "cross-origin write blocked"})
        p = urlparse(self.path).path; b = self._body()
        if b.get("__bad_body__"):
            return self._send(400, {"error": b["__bad_body__"]})
        m = re.fullmatch(r"/api/projects/([\w]+)", p)
        if m:
            pid = m.group(1)
            sets, vals = [], []
            for k in ("name", "goal", "goal_criteria", "workdir", "lead", "deadline"):
                if k in b:
                    sets.append(f"{k}=?")
                    vals.append(b[k])
            if not sets:
                return self._send(400, {"error": "no editable field"})
            with db() as c:
                old = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
                if not old:
                    return self._send(404, {"error": "no project"})
                # 改期 = 重新承诺：起算点挪到此刻，否则「已用时间」会突然倒退
                if "deadline" in b and (old["deadline"] or "") != (b["deadline"] or ""):
                    sets.append("committed_at=?")
                    vals.append(time.time())
                vals.append(pid)
                c.execute(f"UPDATE projects SET {','.join(sets)} WHERE id=?", vals)
                # 记 old → new：只留字段名的话，事后没人能回答「截止时间从几点改到几点」
                diff = {k: [old[k], b[k]] for k in ("name", "goal", "goal_criteria",
                                                    "workdir", "lead", "deadline")
                        if k in b and (old[k] or "") != (b[k] or "")}
                log_event(c, pid, "project.updated",
                          payload=json.dumps(diff, ensure_ascii=False)[:2000])
            return self._send(200, project_state(pid))
        m = re.fullmatch(r"/api/hypotheses/([\w]+)", p)
        if m:
            r = update_hypothesis(m.group(1), b)
            return self._send(404 if r is None else (400 if "error" in r else 200), r or {"error": "no hypothesis"})
        m = re.fullmatch(r"/api/criteria/([\w]+)", p)
        if m:
            r = update_criterion(m.group(1), b)
            return self._send(404 if r is None else (400 if "error" in r else 200), r or {"error": "no criterion"})
        m = re.fullmatch(r"/api/tasks/([\w]+)", p)
        if m:
            r = update_task(m.group(1), b)
            if r and "error" in r:
                return self._send(400, r)
            return self._send(200 if r else 404, r or {"error": "no task"})
        m = re.fullmatch(r"/api/members/([\w]+)", p)
        if m:
            r = update_member(m.group(1), b)
            if r and "error" in r:
                return self._send(400, r)
            return self._send(200 if r else 404, r or {"error": "no member"})
        return self._send(404, {"error": "no route"})

    def do_DELETE(self):
        try:
            return self._do_DELETE()
        except Exception as e:
            try:
                return self._send(500, {"error": f"internal: {e}"})
            except Exception:
                return None

    def _do_DELETE(self):
        if not self._origin_ok():
            return self._send(403, {"error": "cross-origin write blocked"})
        p = urlparse(self.path).path
        m = re.fullmatch(r"/api/projects/([\w]+)", p)
        if m:
            pid = m.group(1)
            with db() as c:
                if not c.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                    return self._send(404, {"error": "no project"})
                c.execute("DELETE FROM events WHERE project_id=?", (pid,))
                c.execute("DELETE FROM approvals WHERE project_id=?", (pid,))
                c.execute("DELETE FROM task_sessions WHERE project_id=?", (pid,))
                c.execute("DELETE FROM resource_leases WHERE project_id=?", (pid,))
                c.execute("DELETE FROM hypotheses WHERE project_id=?", (pid,))
                c.execute("DELETE FROM criteria WHERE project_id=?", (pid,))
                c.execute("DELETE FROM retros WHERE project_id=?", (pid,))
                c.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
                c.execute("DELETE FROM members WHERE project_id=?", (pid,))
                c.execute("DELETE FROM projects WHERE id=?", (pid,))
            return self._send(200, {"ok": True, "deleted": pid})
        m = re.fullmatch(r"/api/tasks/([\w]+)", p)
        if not m:
            return self._send(404, {"error": "no route"})
        with db() as c:
            t = c.execute("SELECT project_id FROM tasks WHERE id=?", (m.group(1),)).fetchone()
            if not t:
                return self._send(404, {"error": "no task"})
            c.execute("DELETE FROM task_sessions WHERE task_id=?", (m.group(1),))
            c.execute("DELETE FROM hypotheses WHERE task_id=?", (m.group(1),))
            c.execute("UPDATE resource_leases SET holder_task='' WHERE holder_task=?", (m.group(1),))
            c.execute("DELETE FROM tasks WHERE id=?", (m.group(1),))
            log_event(c, t["project_id"], "task.deleted", task_id=m.group(1))
        return self._send(200, {"ok": True})


def main():
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default="",
                    help="SQLite 文件路径（优先级高于 TEAMBOARD_DB；默认 <脚本目录>/board.db）")
    a = ap.parse_args()
    if a.db:
        DB_PATH = os.path.abspath(os.path.expanduser(a.db))
    init_db()
    ensure_seats()          # 老库补工位号：编制要能一眼看出「谁是谁」
    backfill_sessions()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"TeamBoard v2 → http://{a.host}:{a.port}  (db: {DB_PATH})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
