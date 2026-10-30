#!/usr/bin/env python3
"""
TeamBoard v2 — 项目看板 / 团队状态 / 交付物清单 / 进度报告
零依赖（Python 标准库 + SQLite）。启动：python3 server.py --port 8787
"""
import argparse, glob, json, os, re, sqlite3, subprocess, sys, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
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
# 总监多久没碰计划 = 脱轨。停摆看的是「员工在不在动」，脱轨看的是
# 「总监有没有在管这个计划」——两者可以同时为真，也可以单独发生：
# 员工在干活、总监 13 小时不看计划，看板上的进度和排期就全是过期的。
BOARD_SILENT_SEC = 2700
# 算「碰计划」的事件。故意不含 member.rebind（自动绑会话）、autopilot.*
# （配置）、project.updated（改名字/截止）——这些不是你回来管计划的证据。
PLAN_KINDS = ("task.created", "task.updated", "task.done", "task.abandoned",
              "member.update", "criteria.upsert", "retro.submitted",
              "resource.claim", "resource.release", "hypothesis.added")
DOING_STALE_SEC = 900   # doing 任务也有保质期：15 分钟既没会话写入也没心跳 = 僵尸任务
# 总监在 Codex 里开了子代理却没给工位：这些会话和本项目工位同属一个父会话，
# 10 分钟内还在写 = 现在真的有人在跑，只是看板上没有它的位置
UNSEATED_LIVE_SEC = 600
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
               "due_at", "evidence_path", "abandoned_reason",
               "verify_cmd", "verify_out", "verified_at")
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
        _ack_before = {t: ("ack_at" in {r["name"] for r in c.execute(f"PRAGMA table_info({t})")})
                       for t in ("approvals", "retros")}
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
            # 完成证据三件套：产物路径 + 一条能重跑的命令 + 当时的输出。
            # 光有 evidence_path 只能证明「文件存在」，证明不了「跑过、通过」。
            ("tasks", "verify_cmd", "TEXT DEFAULT ''"),      # 任何人能复制粘贴重跑的那一条命令
            ("tasks", "verify_out", "TEXT DEFAULT ''"),      # 实测输出（≤5 行），没有就是空
            ("tasks", "verified_at", "TEXT DEFAULT ''"),     # 这条证据是什么时候跑的
            ("tasks", "abandoned_reason", "TEXT DEFAULT ''"),# 放弃原因，换人接手时最值钱
            ("members", "is_lead", "INTEGER DEFAULT 0"),  # 总监不计入员工编制
            ("projects", "goal_criteria", "TEXT DEFAULT ''"),
            ("projects", "workdir", "TEXT DEFAULT ''"),
            ("projects", "committed_at", "REAL DEFAULT 0"),   # 当前承诺的登记时刻（改期即重算）
            ("projects", "priority_ratio", "TEXT DEFAULT ''"),  # P1/P2/P3 配额，总监在计划里填，看板按它切
            ("members", "model_actual", "TEXT DEFAULT ''"),
            ("members", "duty_until", "REAL DEFAULT 0"),
            ("members", "thread_id", "TEXT DEFAULT ''"),   # Codex 会话 id：点击员工能跳过去看他做什么
            # 会话自报的身份（session_meta → payload.source.subagent.thread_spawn）。
            # 工位号只是座位，agent_path 才是「这一轮是谁在干」——两者分开，
            # 「看板上写张三、实际跑的是李四」在结构上就不可能发生。
            ("members", "agent_path", "TEXT DEFAULT ''"),
            ("members", "agent_nickname", "TEXT DEFAULT ''"),
            # 员工编制：工位号是「人」的稳定身份，会话只是临时坐进来执行的那一段
            ("members", "seat_no", "INTEGER DEFAULT 0"),       # 工位号，发出去就不再变
            ("members", "roster_state", "TEXT DEFAULT 'active'"),  # active / vacant / swapping
            ("members", "bound_at", "REAL DEFAULT 0"),          # 当前会话何时接管这个工位
            ("members", "bind_count", "INTEGER DEFAULT 1"),     # 接管过几次会话（换人不换工位）
            # 宿主存活：Codex 退出后子代理不会回调任何东西，光等心跳过期会让看板
            # 在 5~15 分钟里显示「工作中」。登记宿主 pid + 启动时刻，进程没了当场判死。
            ("members", "host_pid", "INTEGER DEFAULT 0"),
            ("members", "host_boot", "TEXT DEFAULT ''"),        # 防 pid 复用：启动时刻不一致＝原进程已死
            ("members", "last_alive", "REAL DEFAULT 0"),        # 最后一次确认活着的时刻（算断线时长）
            ("members", "dead_reason", "TEXT DEFAULT ''"),      # process / session / heartbeat
            # last_seen 会被派活/改状态一起刷新，不能当「在干活」的证据。
            # beat_at 只由真正的 /heartbeat 写，是工位自己在说「我还活着」。
            ("members", "beat_at", "REAL DEFAULT 0"),
            # 审批回执：用户点了批准/驳回之后，总监必须在会话里回一句「收到」。
            # ack_at=0 且已决定 = 欠一条回执，写操作会被 ack_gate 拦住。
            # 为什么用独立列而不是「复用 decided_at」：决定是用户做的，回执是总监做的，
            # 两件事必须能分别追责——否则「谁没回话」永远查不出来。
            ("approvals", "ack_at", "REAL DEFAULT 0"),
            ("retros", "ack_at", "REAL DEFAULT 0"),
        ]:
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")}
            if col not in have:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")
        # 老库刚补上 ack_at：已决定的历史单子一律视为已回执，不追着要「收到」
        for _t, _had in _ack_before.items():
            if not _had:
                c.execute(f"UPDATE {_t} SET ack_at=1 WHERE status NOT IN ('pending','submitted')")
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
            consumed_at REAL DEFAULT 0,      -- 批准只能用一次，防止同一张单反复塞人
            created_at TEXT NOT NULL,
            decided_at TEXT DEFAULT '',
            decided_by TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_staff_requests ON staff_requests(project_id, status);
        -- consumed_at：审批单只能用一次。没有它，同一个批准可以被重复用来塞人。
        """)
        # 一次性迁移：老数据里没标总监的，按名字/岗位认出来。总监不占员工编制。
        c.execute("UPDATE members SET is_lead=1 WHERE is_lead=0 AND"
                  " (name LIKE '%主线程%' OR role LIKE '%总调度%')")
        # 修历史遗留：删项目/删任务时没清干净的计时记录，否则这些 session 永远「正在计时」，
        # 把团队利用率算成有人在上班。
        c.execute("DELETE FROM task_sessions WHERE task_id NOT IN (SELECT id FROM tasks)")
        c.execute("DELETE FROM member_threads WHERE project_id NOT IN (SELECT id FROM projects)")
        c.execute("DELETE FROM staff_requests WHERE project_id NOT IN (SELECT id FROM projects)")
        # 审批单只能用一次：老库补列
        have = {r["name"] for r in c.execute("PRAGMA table_info(staff_requests)")}
        if "consumed_at" not in have:
            c.execute("ALTER TABLE staff_requests ADD COLUMN consumed_at REAL DEFAULT 0")
        if "decided_by" not in have:
            c.execute("ALTER TABLE staff_requests ADD COLUMN decided_by TEXT DEFAULT ''")
        if "ack_at" not in have:
            c.execute("ALTER TABLE staff_requests ADD COLUMN ack_at REAL DEFAULT 0")
            # 老单子是历史，不追着要回执；只有本次之后做的决定才要「收到」
            c.execute("UPDATE staff_requests SET ack_at=1 WHERE status<>'pending'")
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


_PROC_CACHE = {"at": 0.0, "map": {}}


def proc_boot(pid):
    """进程启动时刻。pid 会被系统复用，只有启动时刻一致才算「同一个进程」。

    macOS 用 ps，Linux 用 /proc/<pid>/stat 第 22 字段。取不到就返回空串，
    此时只按 pid 是否存在判断（弱一点，但不会误杀）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            # comm 字段带括号且可能含空格，从最后一个 ')' 之后切
            tail = raw[raw.rfind(")") + 2:].split()
            return tail[19] if len(tail) > 19 else ""
        r = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                           capture_output=True, text=True, timeout=2)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def proc_alive(pid, boot=""):
    """进程是否还活着。None = 没登记 pid，无法判断（不要当成死）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    key = (pid, boot or "")
    now = time.time()
    if now - _PROC_CACHE["at"] >= 5:
        _PROC_CACHE.update({"at": now, "map": {}})
    if key in _PROC_CACHE["map"]:
        return _PROC_CACHE["map"][key]
    try:
        os.kill(pid, 0)
        ok = True
    except PermissionError:
        ok = True          # 进程在，只是不归当前用户
    except OSError:
        ok = False
    if ok and boot:
        ok = (proc_boot(pid) == boot)   # 同一个 pid 但换了一个进程 = 原来那个已经死了
    _PROC_CACHE["map"][key] = ok
    return ok


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
    # 用户刚拍过板的那一条还没回执，就先别提新计划：这一轮的起点是「收到」。
    gate = ack_gate(pid)
    if gate:
        return gate
    # 全员停摆时不许提新计划：新计划 = 把人重新铺一遍，可一个人都没在跑，
    # 铺出来的只会是又一份躺在数据库里的清单。
    gate = idle_gate(pid)
    if gate:
        return gate
    # 有人在跑却不在看板上？先挂号再谈新计划——不然这份计划从第一秒就是空气。
    gate = align_gate(pid)
    if gate:
        return gate
    tasks = b.get("tasks") or []
    members = b.get("members") or []
    problems = validate_plan(pid, b)
    if problems:
        return {"error": "计划字段不全，先补全再提交审批", "missing": problems}
    warns = plan_budget_warnings(pid, b)
    # 自驱检查：审批通过只是「纸面开工」。没有心跳，这个项目在用户闭嘴那一刻
    # 就停了。必须在批计划时就把话说明白，而不是等他明天问「怎么一天没动」。
    ap = (project_state(pid) or {}).get("autopilot") or {}
    if not ap.get("found"):
        warns.append("没有自驱心跳：用户一闭嘴，这个项目就停了。批计划前先 "
                     "POST /api/projects/%s/autopilot {action:enable}" % pid)
    elif not ap.get("has_board_check"):
        warns.append("心跳没有「先看团队板」这一步，醒来只会接着干自己那条线。"
                     "补一段：POST /api/projects/%s/autopilot {action:patch}" % pid)
    elif ap.get("too_slow"):
        warns.append(f"心跳间隔 {ap.get('interval_min')} 分钟太长：停摆 15 分钟就该被发现。"
                     f"建议 ≤15 分钟。")
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


ACK_TABLES = {"plan": "approvals", "retro": "retros", "staff": "staff_requests"}
ACK_LABEL = {"plan": "计划审批", "retro": "强制复盘", "staff": "编制变更"}


def list_need_ack(pid):
    """用户已经拍过板、总监还没回「收到」的单子。

    为什么要成一等公民：用户点了批准，总监那一轮可能已经结束了。下一条消息
    如果不强制回执，它就当没发生——继续按旧编制、旧计划往下干，而用户以为
    已经生效。所以「决定」和「回执」必须分开记，欠回执要拦写操作。
    """
    out = []
    with db() as c:
        for r in c.execute("SELECT id,status,decided_at,summary FROM approvals"
                           " WHERE project_id=? AND status<>'pending'"
                           " AND COALESCE(ack_at,0)=0 ORDER BY decided_at DESC LIMIT 20", (pid,)):
            out.append({"kind": "plan", "id": r["id"], "label": ACK_LABEL["plan"],
                        "status": r["status"],
                        "decision_cn": "已批准" if r["status"] == "approved" else "已驳回",
                        "title": (r["summary"] or "").strip() or "计划审批",
                        "decided_at": r["decided_at"] or ""})
        for r in c.execute("SELECT id,status,decided_at,round FROM retros"
                           " WHERE project_id=? AND status IN ('approved','rejected')"
                           " AND COALESCE(ack_at,0)=0 ORDER BY decided_at DESC LIMIT 20", (pid,)):
            out.append({"kind": "retro", "id": r["id"], "label": ACK_LABEL["retro"],
                        "status": r["status"],
                        "decision_cn": "已批准" if r["status"] == "approved" else "已驳回",
                        "title": f"第 {int(r['round'] or 1)} 轮复盘",
                        "decided_at": now_iso()})
        for r in c.execute("SELECT id,kind,status,decided_at,payload,reason FROM staff_requests"
                           " WHERE project_id=? AND status<>'pending'"
                           " AND COALESCE(ack_at,0)=0 ORDER BY decided_at DESC LIMIT 20", (pid,)):
            try:
                pl = json.loads(r["payload"] or "{}")
            except Exception:                       # noqa: BLE001
                pl = {}
            who = pl.get("name") or ""
            out.append({"kind": "staff", "id": r["id"], "label": ACK_LABEL["staff"],
                        "status": r["status"],
                        "decision_cn": "已批准" if r["status"] == "approved" else "已驳回",
                        "title": f"{ {'add': '加人', 'replace': '换人', 'remove': '撤编'}.get(r['kind'], r['kind']) }"
                                 f"{('：' + who) if who else ''}"
                                 f"{('（' + (r['reason'] or '')[:24] + '）') if r['reason'] else ''}",
                        "decided_at": now_iso()})
    return out


def ack_decision(pid, kind, rid, note="", by=""):
    """总监对用户的决定回一句「收到」。没有这一步，决定只落在库里，没进他的脑子。"""
    tbl = ACK_TABLES.get(kind)
    if not tbl:
        return {"error": "kind 只能是 plan / retro / staff"}
    with db() as c:
        r = c.execute(f"SELECT * FROM {tbl} WHERE id=? AND project_id=?", (rid, pid)).fetchone()
        if not r:
            return {"error": f"没有这张{ACK_LABEL[kind]}单：{rid}"}
        if r["status"] in ("pending", "submitted"):
            return {"error": f"这张{ACK_LABEL[kind]}还没被用户决定（{r['status']}），不用回执"}
        c.execute(f"UPDATE {tbl} SET ack_at=? WHERE id=?", (time.time(), rid))
        log_event(c, pid, "approval.acked", actor=by or "",
                  payload=f"{kind} {rid} {r['status']} {note}"[:500])
    # 出事务再数：在同一个连接里数，新开的连接还看不到这次 UPDATE
    left = list_need_ack(pid)
    return {"ok": True, "kind": kind, "id": rid, "status": r["status"],
            "note": note, "need_ack_left": len(left),
            "next": ("还有 " + str(len(left)) + " 条欠回执，逐条 ack"
                     if left else "回执齐了，可以继续推进")}


def ack_gate(pid):
    """欠回执就拦写操作。用户拍过板的那一条，是这一轮的起点，不是背景音。"""
    need = list_need_ack(pid)
    if not need:
        return None
    first = need[0]
    return {"error": f"用户已经对「{first['title']}」做了决定（{first['decision_cn']}），"
                     f"你还没回执。先在会话里回一句「收到」并把下一步说清，"
                     f"再调 ack(kind='{first['kind']}', id='{first['id']}')。"
                     f"欠回执共 {len(need)} 条。",
            "need_ack": True, "need_ack_list": need,
            "next_actions": [f"POST /api/projects/{pid}/ack "
                             "{kind:'plan|retro|staff', id:'<单号>', note:'收到，下一步…'}"]}


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
        # 同一个 task 再 claim 一次 = 续期（长任务必须能续，否则 60 分钟后
        # 别人合法抢走设备，两边都以为自己是唯一持有者）
        renewed = bool(cur and cur["holder_task"] == task_id)
        c.execute("INSERT INTO resource_leases(project_id,resource,holder_task,holder_member,until,note,created_at)"
                  " VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,resource) DO UPDATE SET"
                  " holder_task=excluded.holder_task, holder_member=excluded.holder_member,"
                  " until=excluded.until, note=excluded.note, created_at=excluded.created_at",
                  (pid, name, task_id, member, until, b.get("note", ""), now_iso()))
        log_event(c, pid, "resource.claim", actor=member or task_id,
                  task_id=task_id, payload=f"{name} {minutes}m" + (" (renew)" if renewed else ""))
    return {"ok": True, "resource": name, "holder_task": task_id,
            "until": until, "left_sec": minutes * 60, "renewed": renewed}


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


# 会话目录可覆盖：测试要造一条「正在写」的会话，不该往用户真实目录里扔文件
SESS_ROOT = os.environ.get("TEAMBOARD_SESSIONS") or os.path.expanduser("~/.codex/sessions")
_THREAD_CACHE = {"at": 0.0, "rows": []}
_LIVE_CACHE = {"at": 0.0, "map": {}, "sig": None}
SESSION_LIVE_WINDOW = HEARTBEAT_WINDOW   # 与会话/心跳同一个窗口，别让两个时钟互相打架
# 「在写」和「还活着」是两件事。Codex 的 rollout 每几秒就落一笔 token_count /
# reasoning / function_call_output，真在跑的子代理**不可能** 90 秒一笔都不落。
# 用 300s 的宽窗口判「在写」，会让已经收工/被 429 打死的会话多亮 5 分钟「工作中」
# （实测 2026-10-29：18:23 起的三个子代理 21:00 全部结束后仍显示工作中）。
# 所以存活判定用短窗：90 秒无新事件 = 这条会话此刻不在跑。
SESSION_SILENT_SEC = 90
# 索引覆盖的天数。只扫 2 天会漏掉「三天前开的、一直在写」的长会话——主线程
# 恰好就是这种：文件在 10-27 的目录里，写到今天，于是永远查不到、永远显示离线。
SESSION_INDEX_DAYS = 10
# 全员停摆阈值。总监的默认行为是「自己埋头干」，不回头看工位，
# 于是 4 个员工全停了他也不知道。15 分钟是这个尺度的上限：
# 短于 15 分钟可能只是某条命令跑得久（90 秒静默窗会让它短暂掉线），
# 长于 15 分钟则意味着一整段排期被浪费掉了。
# 触发后写操作全部 403，只有「派人干活」类动作放行——逼他先解决人。
IDLE_ALERT_SEC = 900
# 对齐闸的两条时间线。
#   ALIGN_GRACE_SEC：会话在跑、但看板上没有它的在办任务，持续多久才算「干活不挂号」。
#     10 分钟是刻意放长的：spawn 之后到第一次 update_task 之间必然有空档，
#     收工→领下一个任务之间也是。把这段算成违规只会把闸门变成噪音，
#     而噪音闸的结局是被 force 绕过，等于没有。
#   ALIGN_GHOST_GRACE_SEC：任务还挂着 doing、主人已经不在跑，多久才算僵尸任务。
#     要短：这正是用户反复报的那个 bug（子代理停了，看板还在替它干活）。
ALIGN_GRACE_SEC = 600
ALIGN_GHOST_GRACE_SEC = 120
_AGENT_CACHE = {}
# 事件流里的「这一轮结束了」。Codex 退出时补的是 turn_aborted，
# 正常收工补的是 task_complete——两种都不是「还在干活」。
TERMINAL_EVENTS = {"task_complete", "turn_aborted"}
# 只有这些尾部事件能证明「这一刻真的在跑」。白名单是必须的：
# 重启 Codex 会在会话里写 session_meta / turn_context / compacted，用户发消息写
# user_message——这些都会被 mtime 骗成「刚写过」。
# 黑名单做法（只排除终态）实测漏掉这一整类：重启后什么都没干，主线程亮「工作中」。
BUSY_TAIL = {"function_call", "function_call_output", "reasoning", "token_count",
             "item_started", "item_completed", "agent_message", "token_usage_record"}
IDLE_TAIL = {"user_message"}             # 用户刚发、agent 还没动 = 等指令，不是在工作
_TAIL_CACHE = {}


def _ts_epoch(s):
    """rollout 的 timestamp 是 UTC ISO（2026-10-29T07:15:50.453Z）。"""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def session_tail(path):
    """读 rollout 尾部最后一条事件（类型 / 时间）。

    只看 mtime 会被「退出时补一笔」骗到：14:56 关掉 Codex，文件 mtime 变成 14:56，
    接下来 5 分钟整个团队都显示「工作中」，其实一个都没在跑。
    """
    if not path:
        return {}
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_size, int(st.st_mtime))
    hit = _TAIL_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    info = {}
    try:
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - 20000))
            lines = [x for x in f.read().decode("utf-8", "ignore").strip().splitlines()
                     if x.lstrip().startswith("{")]      # 尾部常留半行（文件正在写）
        for ln in reversed(lines):
            try:
                d = json.loads(ln)
            except Exception:                       # noqa: BLE001
                continue
            pl = d.get("payload") or {}
            info = {"type": (pl.get("type") if isinstance(pl, dict) else "") or d.get("type", ""),
                    "ts": d.get("timestamp", ""),
                    "epoch": _ts_epoch(d.get("timestamp", "")),
                    "reason": (pl.get("reason") if isinstance(pl, dict) else "") or ""}
            break
    except OSError:
        info = {}
    _TAIL_CACHE[path] = (key, info)
    return info


def agent_info(path):
    """会话头的身份字段：agent_path = /root/quest10_device，nickname = Copernicus。

    Codex 在 session_meta 里就写好了这两项（payload.source.subagent.thread_spawn）。
    它是「谁在这个会话里干活」的权威答案——比让总监在 prompt 里手写工位名可靠：
    名字对不上这件事在结构上就不会发生。只读头部 8KB。
    """
    if not path:
        return {}
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_size, int(st.st_mtime))
    hit = _AGENT_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    info = {"agent_path": "", "agent_nickname": "", "depth": 0, "subagent": False}
    try:
        with open(path, "rb") as f:
            head = f.read(8192).decode("utf-8", "ignore")
        m = re.search(r'"agent_path"\s*:\s*"([^"]*)"', head)
        if m:
            info["agent_path"] = m.group(1)[:120]
        m = re.search(r'"agent_nickname"\s*:\s*"([^"]*)"', head)
        if m:
            info["agent_nickname"] = m.group(1)[:60]
        m = re.search(r'"depth"\s*:\s*(\d+)', head)
        if m:
            info["depth"] = int(m.group(1))
        info["subagent"] = '"subagent"' in head[:4000]
    except OSError:
        pass
    _AGENT_CACHE[path] = (key, info)
    return info


def session_path_of(tid):
    """thread_id → rollout 文件绝对路径。索引还没扫到新会话时直接 glob 兜底。"""
    tid = (tid or "").strip()
    if not tid:
        return ""
    p = (_LIVE_CACHE.get("paths") or {}).get(tid)
    if p and os.path.exists(p):
        return p
    hits = glob.glob(os.path.join(SESS_ROOT, "*", "*", "*", f"*-{tid}.jsonl"))
    if not hits:
        return ""
    try:
        return max(hits, key=os.path.getmtime)
    except OSError:
        return hits[0]


def session_mtimes(ttl=2, days=10):
    """会话文件 mtime 索引（thread_id → 最后一次写入时间）。

    子代理跑在 Codex 里，不会主动回调心跳，所以「有没有人干活」不能只看 last_seen。
    这里 glob 最近 10 天的日期目录、只取 mtime、不读内容，2 秒缓存，成本可忽略。

    WHY 从 2 天改到 10 天（2026-10-29 实测）：主线程的会话是 10-27 开的，一直写到今天。
    只扫最近两天 → 这个会话根本不在索引里 → 工位静默时长恒为 -1 → 正在干活的总监被画成
    「离线」。绑定的是全 UUID、文件也确实在写，坏的是索引范围，不是人。
    """
    now = time.time()
    # 目录 mtime 变了（新开了一个子代理 → 新出现一个 rollout 文件）就立刻重扫，
    # 否则新工位最多要等 12 秒才被看见，用户会以为「刚开的员工没上板」。
    today0 = datetime.now()
    dirs = [os.path.join(SESS_ROOT, f"{(today0 - timedelta(days=i)):%Y}",
                         f"{(today0 - timedelta(days=i)):%m}",
                         f"{(today0 - timedelta(days=i)):%d}") for i in range(days)]
    try:
        sig = tuple(os.path.getmtime(d) if os.path.isdir(d) else 0.0 for d in dirs)
    except OSError:
        sig = None
    if now - _LIVE_CACHE["at"] < ttl and _LIVE_CACHE.get("sig") == sig:
        return _LIVE_CACHE["map"]
    out = {}
    paths = {}
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*.jsonl")):
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                          r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", p)
            if m:
                full = m.group(1)
                out[full] = max(out.get(full, 0.0), mt)
                paths[full] = p
    # 成员表里 thread_id 有的存全量 UUID、有的只存前 8 位（工位绑定用短 id），
    # 两种都要能查到；前缀键取同名会话里最新的一次写入。
    # 注意：前缀只是「查询便利」，不能当成「这个工位还活着」的证据——
    # 同一个前缀可能命中另一个会话，会把死掉的子代理判活。存活只认全量 UUID。
    full_keys = {k for k in out if "-" in k}
    for k in list(full_keys):
        short = k[:8]
        if out.get(short, 0.0) < out[k]:
            out[short] = out[k]
    _LIVE_CACHE.update({"at": now, "map": out, "full": full_keys, "sig": sig,
                        "paths": paths})
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


# ---------------------------------------------------------------- autopilot
# 项目的真正病根不是「总监懒」，是「没有自驱」：所有动作都由用户那句话触发，
# 用户不开口，整个项目就停在原地。总监自己也不例外——它没有「醒着」这个状态。
# 所以插件必须回答一个问题：**在用户不说话的情况下，谁能把总监叫醒？**
# 答案只有一个：Codex 的 heartbeat automation（定时把这一轮重新拉起来）。
# 没有它，看板再准也只是个仪表盘；有它，看板才变成闭环。
#
# 插件不自己 spawn codex（会跟 App 抢同一个 thread）。它只做三件事：
# 发现 / 校验 / 把缺的东西原样给出来让总监一次调用就能注册。
AUTO_DIR = os.environ.get("TEAMBOARD_AUTOMATIONS") or os.path.expanduser("~/.codex/automations")
# 心跳 prompt 里必须出现「看图」的字眼，否则到点醒来也不会看团队，
# 只会接着干自己那条线——实测就是这么停摆 45 分钟的。
BOARD_WORDS = ("teamboard", "团队看板", "看板", "team board")
TEAM_WORDS = ("成员", "员工", "idle", "在岗", "工位", "子代理", "team")
_AUTO_CACHE = {"at": 0.0, "sig": None, "rows": []}


def _toml_flat(text):
    """顶层 key = value 的极简解析。文件是 Codex 自己写的固定形状，够用。"""
    out = {}
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if v.startswith('"'):
            try:
                out[k] = json.loads(v)          # TOML 基本字符串 == JSON 字符串
            except Exception:                   # noqa: BLE001
                out[k] = v.strip('"')
        else:
            # 数字要还原成数字：写回时 str → 会被加引号，App 读 1793283393174
            # 当字符串可能解析失败，心跳就静默失效了。
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def _rrule_minutes(rrule):
    """RRULE → 分钟。只认 FREQ=MINUTELY/HOURLY/DAILY 这三种，够表达心跳。"""
    up = (rrule or "").upper()
    m = re.search(r"INTERVAL=(\d+)", up)
    n = int(m.group(1)) if m else 1
    if "FREQ=MINUTELY" in up:
        return n
    if "FREQ=HOURLY" in up:
        return n * 60
    if "FREQ=DAILY" in up:
        return n * 1440
    return 0


def _automations():
    """扫 automations 目录，20 秒缓存。目录变化（新建/删除）会立刻失效。"""
    now = time.time()
    try:
        sig = sorted(os.listdir(AUTO_DIR))
        sig = tuple((d, os.path.getmtime(os.path.join(AUTO_DIR, d, "automation.toml")))
                    for d in sig
                    if os.path.exists(os.path.join(AUTO_DIR, d, "automation.toml")))
    except OSError:
        return []
    if _AUTO_CACHE["sig"] == sig and now - _AUTO_CACHE["at"] < 20:
        return _AUTO_CACHE["rows"]
    rows = []
    for d, _mt in sig:
        fp = os.path.join(AUTO_DIR, d, "automation.toml")
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                kv = _toml_flat(f.read())
        except OSError:
            continue
        kv["_dir"] = d
        kv["_mtime"] = _mt
        rows.append(kv)
    _AUTO_CACHE.update({"at": now, "sig": sig, "rows": rows})
    return rows


def automation_status(lead_thread):
    """这个项目有没有自驱心跳？有 → 校验它会不会真去看团队；没有 → 给注册参数。"""
    tid = (lead_thread or "").strip()
    if not tid:
        return {"found": False, "reason": "no_lead_thread",
                "note": "总监还没绑定会话，无法确认它会不会被叫醒"}
    hit = None
    for r in _automations():
        t = (r.get("target_thread_id") or "").strip()
        if not t:
            continue
        # 前缀也算（thread id 有时被截断存），但优先全等
        if t == tid or tid.startswith(t) or t.startswith(tid[:8]):
            if hit is None or len(t) > len(hit.get("target_thread_id") or ""):
                hit = r
    if not hit:
        return {"found": False, "reason": "no_heartbeat", "lead_thread": tid,
                "register": {
                    "tool": "automation_update",
                    "kind": "heartbeat",
                    "destination": "thread",
                    "targetThreadId": tid,
                    "name": "<项目名> · 自驱守夜",
                    "rrule": "RRULE:FREQ=MINUTELY;INTERVAL=15",
                    "prompt_keywords": ["teamboard", "idle", "成员状态"],
                },
                "note": "没有自驱心跳：用户不开口，这一轮永远不会再来一次"}
    prompt = (hit.get("prompt") or "")
    low = prompt.lower()
    has_board = any(w in low for w in BOARD_WORDS)
    has_team = any(w in low for w in TEAM_WORDS)
    interval = _rrule_minutes(hit.get("rrule"))
    return {"found": True, "id": hit.get("_dir"), "name": hit.get("name") or "",
            "status": (hit.get("status") or "").upper(),
            "rrule": hit.get("rrule") or "", "interval_min": interval,
            "target_thread": hit.get("target_thread_id") or "",
            "has_board_check": bool(has_board and has_team),
            "prompt_chars": len(prompt),
            # 醒得太稀 = 停摆检测窗口比心跳间隔还短，等于没装
            "too_slow": bool(interval and interval > 30),
            "patch_prompt": (
                "\n\n【团队看板 · 每次醒来第一件事（不可跳过）】\n"
                "先调 teamboard 的 board_overview / project_status，读 idle 字段。\n"
                "不要用 list_agents 代替这一步：它只列「正在跑」的子代理，"
                "已经停掉的会直接从列表里消失——看上去一片正常，其实一个人都没在干活。\n"
                "- idle.alert=true → 全员停摆：先给每个空工位派活（update_task 认领 "
                "+ member_duty），一个都不许留空；\n"
                "- align.ok=false → 有人没对齐看板（在跑没挂号 / 任务主人已停 / 有活没绑会话）："
                "先按 align 的三张清单逐条修，修完再谈进度；\n"
                "- 有成员 state≠working 却挂着 doing 任务 → 先回收或重开（spawn → bind_member），"
                "别当它还在跑；\n"
                "- 再干自己那条线。不看团队就直接干活的这一轮，视为无效轮。\n")}


def idle_gate(pid):
    """全员停摆闸：一个干活的都没有 → 除了「派人干活」的写操作一律 403。

    为什么必须做成硬闸：总监的注意力永远在自己手上那条线上，成员状态是
    「看一眼就走」的存在。只在前端标红，实测结果是他根本不会翻到那一屏。
    只有让「新增计划 / 加人 / 导报告」这些他真正想做的动作直接失败，
    他才会回头去派活。

    放行的动作（解闸路径）：update_task / heartbeat / bind_member / member_duty
    / spawn 登记。拦这些等于把唯一出口也锁上，会死锁。
    """
    st = project_state(pid)
    if not st:
        return None
    i = st.get("idle") or {}
    if not i.get("alert"):
        return None
    who = "、".join(
        f"{x['name']}(停 {_hm(x['silent']) if x['silent'] >= 0 else '无会话'})"
        for x in (i.get("silent") or [])[:4])
    return {"error": f"全员停摆 {i['human']}：{i['workers']} 个员工里 0 个在干活，"
                     f"手上还压着 {i['open_tasks']} 条未收工。{who}。"
                     f"先把活派下去（update_task 认领 + member_duty 分工 + 心跳），"
                     f"再谈新计划。",
            "need_idle_fix": True, "idle": i,
            # 给出可直接照做的下一步，别让他再猜该调哪个接口
            "next_actions": [
                "GET  /api/projects/<pid>  → 看 idle.silent 里谁停最久",
                "POST /api/members/<mid>/heartbeat  → 让活着的员工报活",
                "POST /api/tasks/<tid>  {status:'doing', assignee_id:<mid>}  → 把 backlog 派下去",
                "关掉的子代理要重开：spawn → bind_member（换会话不算换人，不走审批）",
            ]}


def align_gate(pid):
    """全员对齐闸：看板与实际执行体对不上 → 拦住「开新计划 / 加人 / 交报告」。

    idle_gate 回答的是「有没有人在干活」，这个闸回答的是「干活的人挂的是哪一条」。
    两者都会漏：4 个人都在跑、但跑的东西看板上一片空白时，idle 是绿的，
    而用户看到的进度、工时、卡点全是空气——这正是「总监不关注队员在干什么」的
    制度化表现，光靠提醒改不过来，只能让他的下一步动作失败。

    放行的是解闸动作本身（update_task / member_duty / bind_member / heartbeat），
    与 idle_gate 同一套放行表，避免死锁。
    """
    st = project_state(pid)
    if not st:
        return None
    a = st.get("align") or {}
    if a.get("ok", True):
        return None
    parts = []
    if a.get("unlogged"):
        parts.append("在跑但没挂号：" + "、".join(
            f"{x['name']}({x['human']})" for x in a["unlogged"][:4]))
    if a.get("ghost"):
        parts.append("任务挂着人没了：" + "、".join(
            f"{x['name']}·{x['task'][:14]}({x['human']})" for x in a["ghost"][:4]))
    if a.get("nosession"):
        parts.append("有活没绑会话：" + "、".join(
            f"{x['name']}·{x['task'][:14]}" for x in a["nosession"][:4]))
    return {"error": "团队没对齐看板：" + "；".join(parts) +
                     "。先把人挂到看板上，再谈新计划。",
            "need_align": True, "align": a,
            "next_actions": [
                "POST /api/members/<mid>/duty  {on:true, minutes:45}  → 登记在场",
                "POST /api/tasks/<tid>  {status:'doing', assignee_id:<mid>}  → 把活挂到他名下",
                "POST /api/projects/<pid>/members/<mid>/bind  {thread_id:'<全 UUID>'}  → 补绑会话",
                "任务主人已经不在跑 → 先 update_task 收回（backlog），再 spawn 重开 + bind",
            ]}


def autopilot_prompt(pid, name):
    """自驱心跳的 prompt。写成模板，不写死某个项目——开源后别人拉起团队也能直接用。"""
    return (
        f"【{name or pid} · 自驱守夜】没有用户的新消息，这一轮是你自己被心跳叫醒的。"
        "按顺序执行，不许跳步：\n"
        "\n"
        "1) 看团队（第一件事，不可跳过）：调用 teamboard 的 project_status，读 idle 字段。\n"
        "   - idle.alert = true（全员停摆）→ 立刻派活：把 backlog 里优先级最高的任务 "
        "update_task 认领 + member_duty 分工给每个人；一个工位都不许空着。\n"
        "   - align.ok = false（团队没对齐看板）→ 按 align.unlogged / align.ghost / "
        "align.nosession 三张清单逐条修：在跑的人必须有在办任务，在办任务的主人必须真在跑，"
        "有活的人必须绑了会话。修完再谈别的。\n"
        "   - 成员 state ≠ working 却挂着 doing 任务 → 先回收（改回 backlog 或换人）"
        "或重开子代理（spawn → bind_member）。不要假设它还活着。\n"
        "   - 静默超过 15 分钟 = 掉线，按掉线处理。\n"
        "2) 看关键路径：critical_gap > 0 说明按当前估算会晚点。要么砍范围，"
        "要么加人——加人/换人必须走审批，不许自作主张。\n"
        "3) 这一轮只推进一件事：critical_chain 上第一个未完成任务。不开新战线。\n"
        "4) done 必须带 verify_cmd + verify_out（可重跑的命令和实测输出），缺一个不算完成。\n"
        "5) 每步都更新 progress，不要攒到最后一次性写。\n"
        "\n"
        "通知规则：默认静默，不发进度噪音。只有三种情况给用户发消息——"
        "(a) 交付完成；(b) 需要用户决策或动手；(c) 全员停摆超过 30 分钟且自救不了。"
    )


def set_autopilot(pid, b):
    """注册 / 暂停 / 校验自驱心跳。插件自己写 automation.toml——
    靠「提醒总监记得去建一个」是没用的，实测它会忘。"""
    with db() as c:
        p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            return {"error": "no such project"}
        lead = c.execute("SELECT * FROM members WHERE project_id=? AND is_lead=1"
                         " ORDER BY created_at LIMIT 1", (pid,)).fetchone()
    tid = ((dict(lead).get("thread_id") if lead else "") or "").strip()
    act = (b.get("action") or "enable").strip().lower()
    if act == "verify":
        _AUTO_CACHE["at"] = 0.0
        return {"ok": True, "autopilot": automation_status(tid)}
    if not tid:
        return {"error": "总监还没绑定会话线程（thread_id 为空），没法把心跳挂到它身上。"
                         "先 bind_member 把主线程会话绑到总监工位。"}
    # 先在目录里找已有的那条心跳（可能是总监自己建的，id 任意），
    # 找不到才用本项目固定前缀新建。按 pid 硬推路径会漏掉已存在的文件。
    cur = automation_status(tid)
    if cur.get("found") and cur.get("id"):
        fp = os.path.join(AUTO_DIR, cur["id"], "automation.toml")
    else:
        fp = os.path.join(AUTO_DIR, f"tb-{pid.replace('proj_', '')[:12]}", "automation.toml")
    d = os.path.dirname(fp)
    if act == "pause":
        if not os.path.exists(fp):
            return {"error": "这个项目还没有自驱心跳，无从暂停", "autopilot": automation_status(tid)}
        kv = _toml_flat(open(fp, encoding="utf-8", errors="replace").read())
        kv["status"] = "PAUSED"
        kv["updated_at"] = int(time.time() * 1000)
        _write_toml(fp, kv)
        _AUTO_CACHE["at"] = 0.0
        return {"ok": True, "autopilot": automation_status(tid)}
    if act == "patch":
        # 只追加「每次醒来先看团队板」这一段，原 prompt 一个字不动。
        # 直接覆盖会把总监辛苦写的工作纪律冲掉，那种改法没人敢用第二次。
        if not os.path.exists(fp):
            return {"error": "这个项目还没有自驱心跳，先 enable", "autopilot": automation_status(tid)}
        kv = _toml_flat(open(fp, encoding="utf-8", errors="replace").read())
        old = kv.get("prompt") or ""
        if "团队看板 · 每次醒来" in old:
            _AUTO_CACHE["at"] = 0.0
            return {"ok": True, "noop": True, "autopilot": automation_status(tid)}
        kv["prompt"] = old + automation_status(tid).get("patch_prompt", "")
        kv["updated_at"] = int(time.time() * 1000)
        _write_toml(fp, kv)
        _AUTO_CACHE["at"] = 0.0
        with db() as c:
            log_event(c, pid, "autopilot.patched", actor="director",
                      payload="prompt += 看板 idle 检查")
        return {"ok": True, "patched": True, "path": fp,
                "autopilot": automation_status(tid)}
    if os.path.exists(fp) and not b.get("force"):
        # enable 的语义是「让它跑起来」。文件已存在但被暂停过，就改回 ACTIVE；
        # 直接把 PAUSED 原样返回，等于告诉调用方成功、实际永不触发。
        kv = _toml_flat(open(fp, encoding="utf-8", errors="replace").read())
        if (kv.get("status") or "").upper() != "ACTIVE":
            kv["status"] = "ACTIVE"
            kv["updated_at"] = int(time.time() * 1000)
            _write_toml(fp, kv)
            _AUTO_CACHE["at"] = 0.0
            return {"ok": True, "resumed": True, "path": fp,
                    "autopilot": automation_status(tid)}
        _AUTO_CACHE["at"] = 0.0
        st = automation_status(tid)
        st["exists"] = True
        return st
    interval = safe_int(b.get("interval_min"), 15, 5, 120)
    name = (b.get("name") or (dict(p).get("name") or pid)).strip()
    kv = {"version": 1, "id": os.path.basename(d), "kind": "heartbeat",
          "name": f"{name} · 自驱守夜",
          "prompt": autopilot_prompt(pid, name),
          "status": "ACTIVE",
          "rrule": f"RRULE:FREQ=MINUTELY;INTERVAL={interval}",
          "target_thread_id": tid,
          "created_at": int(time.time() * 1000),
          "updated_at": int(time.time() * 1000)}
    try:
        os.makedirs(d, exist_ok=True)
        _write_toml(fp, kv)
    except OSError as e:
        return {"error": f"写心跳文件失败：{e}", "path": fp}
    _AUTO_CACHE["at"] = 0.0
    with db() as c:
        log_event(c, pid, "autopilot.enabled", actor="director",
                  payload=f"interval={interval}m thread={tid[:8]}")
    return {"ok": True, "path": fp, "autopilot": automation_status(tid)}


def _write_toml(fp, kv):
    """先写 .tmp 再 rename：心跳文件写在半截会被 App 读成坏配置。"""
    lines = []
    for k, v in kv.items():
        if k.startswith("_"):
            continue
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        else:
            lines.append(f"{k} = {json.dumps(str(v), ensure_ascii=False)}")
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, fp)


def project_state(pid):
    with db() as c:
        p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            return None
        members = [dict(r) for r in c.execute(
            "SELECT * FROM members WHERE project_id=? ORDER BY created_at", (pid,))]
        tasks = [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE project_id=? ORDER BY sort_order, created_at", (pid,))]
        # 总监最后一次「碰计划」是什么时候。只认改计划的事件：
        # member.rebind / autopilot.* / project.updated 是自动或配置写入，
        # 把它们算进来会让「13 小时没碰计划」显示成「刚刚」，等于又骗一次。
        _q = "SELECT kind, created_at FROM events WHERE project_id=? AND kind IN (" \
             + ",".join("?" * len(PLAN_KINDS)) + ") ORDER BY id DESC LIMIT 1"
        last_plan = c.execute(_q, (pid, *PLAN_KINDS)).fetchone()

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
    alive_upd = []      # 本轮确认还在工作的成员，循环结束后一次性落库
    for m in members:
        mine = [t for t in tasks if t["assignee_id"] == m["id"]]
        doing = [t for t in mine if t["status"] == "doing"]
        blocked = [t for t in mine if t["status"] == "blocked"]
        hs = float(m["last_seen"] or 0)
        # 真心跳：只有 /heartbeat 会写它。派活、改状态刷新的是 last_seen，
        # 拿 last_seen 判「在干活」会让刚 spawn 就死掉的子代理装活 15 分钟。
        beat = float(m.get("beat_at") or 0)
        duty = float(m.get("duty_until") or 0)
        m["heartbeat_age"] = max(0, int(now - hs)) if hs > 0 else -1
        m["on_duty"] = duty > now              # 总监派活时登记的在场租约
        m["duty_left"] = max(0, int(duty - now)) if m["on_duty"] else 0
        # 会话文件在写 = 这个工位确实有人在跑，比心跳可靠（子代理不回调心跳）
        tid0 = (m.get("thread_id") or "").strip()
        sess_mt = session_mtimes().get(tid0) if tid0 else 0.0
        # 存活只认全量 UUID 的写入时间（前缀命中不算数，见 session_mtimes 的说明）
        sess_mt_full = sess_mt if (tid0 and tid0 in _LIVE_CACHE.get("full", ())) else 0.0
        m["session_age"] = max(0, int(now - sess_mt)) if sess_mt else -1
        m["session_live"] = bool(sess_mt_full) and (now - sess_mt_full) < SESSION_SILENT_SEC
        # 静默时长单独记一份：前端可以画「静默 3 分钟」，而不是把断线的座位画成在干活
        m["session_silent"] = (max(0, int(now - sess_mt_full)) if sess_mt_full else -1)
        # 会话自报的身份：agent_path 才是「这一轮谁在干」，姓名只是座位招牌。
        # 放在这里实时解析（有 mtime 缓存，读到就复用），不写库——写库会变成
        # 每个轮询周期 5 次 UPDATE，而这两项本来就是会话的属性，不是人的属性。
        ap = agent_info((_LIVE_CACHE.get("paths") or {}).get(tid0, "")) if tid0 else {}
        m["agent_path"] = ap.get("agent_path") or ""
        m["agent_nickname"] = ap.get("agent_nickname") or ""
        m["agent_leaf"] = (m["agent_path"].rstrip("/").split("/")[-1]
                           if m["agent_path"] else "")
        # mtime 新 ≠ 有人在干：退出 Codex 时它会补一笔 turn_aborted / task_complete。
        # 最后一笔是「收工」，这条会话此刻就没有在跑——不管 mtime 有多新。
        # 而且「写了文件」也只证明写过：重启补的 session_meta、用户发消息写的
        # user_message 都会刷新 mtime。只认 BUSY_TAIL（工具/思考/流式增量）才算在跑。
        if sess_mt_full:
            tail = session_tail((_LIVE_CACHE.get("paths") or {}).get(tid0, ""))
            ttype = tail.get("type") or ""
            m["session_tail"] = ttype
            m["session_tail_age"] = (max(0, int(now - tail["epoch"]))
                                     if tail.get("epoch") else -1)
            if ttype in TERMINAL_EVENTS:
                m["session_ended"] = ttype
                m["session_live"] = False
            elif ttype in IDLE_TAIL:
                m["session_awaiting"] = True        # 用户刚发、agent 还没动
                m["session_live"] = False
            elif ttype and ttype not in BUSY_TAIL:
                # 重启/压缩/建会话这类「非工作事件」落在尾部：文件是新的，人没在跑
                m["session_idle_tail"] = ttype
                m["session_live"] = False
        # 宿主进程探活：Codex 一退出，子代理就再也不会有任何回调，
        # 只靠心跳/会话窗口会让看板继续显示「工作中」5~15 分钟。
        # 登记过 pid 的成员，进程没了就当场判死，不等窗口过期。
        hp = int(m.get("host_pid") or 0)
        alive = proc_alive(hp, m.get("host_boot") or "")
        m["host_alive"] = alive                  # None = 没登记 pid，无法判断
        # Codex 重启后同一个会话会被新进程接着写，但库里还记着上一条 pid：
        # 那是「记录过期」，不是「人没了」。会话这一秒还在写 = 活着，pid 只是参考。
        host_dead = (alive is False) and not m["session_live"]
        m["live"] = (m["on_duty"] or m["session_live"]
                     or (beat > 0 and (now - beat) < HEARTBEAT_WINDOW))
        if host_dead:
            m["live"] = False
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
        # 有阻塞任务时，不管人是「工作中」还是「卡住」，成员行上都要看得到卡点，
        # 不能只藏在抽屉里
        m["blocked_count"] = len(blocked)
        m["blocked_title"] = blocked[0]["title"] if blocked else ""
        m["blocked_reason"] = (blocked[0].get("blocked_reason") or "") if blocked else ""
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
        # 阻塞也要有人守着：任务标了 blocked，但执行体早断了，那就不叫「卡住」，
        # 叫「僵住」——卡点是任务写的，僵住是人没了。这两件事必须分开显示，
        # 否则总监会以为有人在盯这个卡点，其实是空的。
        # 「僵住」的前提是「真的没人在跑」。会话这一秒还在写（reasoning / token_count）
        # 就说明人在干活——哪怕心跳停在两小时前、任务还挂着 blocked。
        # 不加 not session_live 的话，会用心跳把正在写会话的活人判死（实测 quest3_detector：
        # tail=reasoning、静默 0 秒，state 却是「僵住」，state_source=task+stale）。
        blocked_dead = bool(blocked) and not m["session_live"] and (
            host_dead
            or bool(m.get("session_ended"))        # 会话最后一笔是收工/中断：没人守着了
            or (bool(sess_mt_full) and (now - sess_mt_full) > DOING_STALE_SEC)
            or (beat > 0 and (now - beat) > DOING_STALE_SEC))
        # 状态以「任务事实」为准：有 doing 任务就是工作中，心跳只用于判定空闲/离线。
        # 子代理跑在 Codex 里，不会稳定回调心跳，所以不能用它否决进行中的任务。
        # 卡的是任务，不是人：会话文件这一秒还在写，就说明这个人正在干活
        # （实测 trigger_state / quest3_detector 每几秒写一次会话，却被画成「卡住·摸鱼中」）
        if blocked and not blocked_dead and m["session_live"] and not host_dead:
            m["state"] = "working"
            m["state_source"] = "session+blocked"
        elif blocked and not blocked_dead:
            m["state"] = "blocked"
            m["state_source"] = "task"
        elif blocked_dead:
            m["state"] = "stale"
            m["state_source"] = "task+stale"
        elif doing and not host_dead and (m["session_live"] or (beat > 0 and (now - beat) < DOING_STALE_SEC)):
            m["state"] = "working"
            m["state_source"] = "session" if m["session_live"] else "task"
        elif doing:
            # 任务还挂在 doing，但会话没写、心跳也没有 = 执行体已经断了，任务没回收
            m["state"] = "stale"
            m["state_source"] = "task+stale"
        elif m["session_live"] and not host_dead:
            # 会话文件正在写 = 这个座位真的在执行。总监通常没有 doing 任务
            # （它的活是调度），只按心跳判会把正在干活的总监显示成摸鱼。
            m["state"] = "working"
            m["state_source"] = "session"
        elif m["live"] and not host_dead:
            m["state"] = "idle"
            m["state_source"] = "heartbeat"
        else:
            m["state"] = "offline"
            m["state_source"] = "heartbeat"
        # 死因：总监接手时最需要知道的是「人为什么没了」，不是「多久没心跳」
        if host_dead:
            m["dead_reason"] = "process"      # 宿主进程退出（Codex 关了）
        elif m["state"] == "stale":
            m["dead_reason"] = "session"      # 进程在，但这条会话已经不写了
        elif m["state"] == "offline":
            m["dead_reason"] = "heartbeat"
        else:
            m["dead_reason"] = ""
        m["working"] = m["state"] == "working"
        m["stalled"] = m["state"] == "stale"           # 任务挂着、人不在：要单独喊出来
        m["online"] = m["state"] != "offline"          # 在岗 = 不是离线
        m["stale"] = bool(doing) and not m["live"]      # 在干活但没心跳上报
        last_act = max(hs, sess_mt_full or 0.0)
        m["offline_for"] = (max(0, int(now - last_act)) if last_act > 0 else -1) \
            if m["state"] == "offline" else 0
        # 看板只分「工作中 / 摸鱼中」：没在干活的时间统一从这里取，别让前端自己猜
        m["away_for"] = 0 if m["state"] == "working" \
            else (max(0, int(now - last_act)) if last_act > 0 else -1)
        # 记下「最后一次确实在工作」的时刻。断线时长、交班简报都靠它，
        # 没有它就只能显示「离线 62m」却答不出「他是什么时候断的」。
        prev_alive = float(m.get("last_alive") or 0)
        if prev_alive <= 0:
            # 老数据没有 last_alive：用手上最硬的一条证据（心跳 / 会话写入）兜底，
            # 否则断线时长会显示成「无记录」，等于没回答用户的问题。
            prev_alive = last_act or 0.0
        m["last_alive"] = prev_alive
        m["gone_for"] = (max(0, int(now - prev_alive))
                         if (prev_alive > 0 and m["state"] != "working") else 0)
        if m["state"] == "working" and (now - prev_alive) > 60:
            alive_upd.append((now, m["id"]))

    if alive_upd:
        with db() as c:
            c.executemany("UPDATE members SET last_alive=? WHERE id=?", alive_upd)

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
        "stalled": len([m for m in members if m["state"] == "stale"]),
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
                             and m["util_pct"] < 20 and m["state"] not in ("working", "stale"))
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
        # 过期 ≠ 已释放：TTL 到了但没人 release，adb 可能还连着那台设备。
        # 「过期自动可抢」只解决了抢不到的问题，没解决「两个人以为自己是唯一持有者」。
        l["overdue_min"] = max(0, int(-l["left_sec"] / 60)) if l["expired"] else 0
        l["queue"] = [t["title"] for t in tasks
                      if (t.get("resource") or "").strip() == l["resource"]
                      and t["status"] in ("backlog", "blocked") and t["id"] != l["holder_task"]]
    # 超时仍占着设备的租约：顶部要单独喊，不能让总监自己翻租约表
    lease_overdue = [l for l in leases if l["expired"] and l["holder_task"]]
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
    # 「已开启 3 个子代理，看板只有 2 个工位」= 用户眼里的对不上。
    # 判定只认三条事实：跟本项目工位同一个父会话、10 分钟内还在写、没被任何项目登记。
    unseated = []
    rebinds = []
    try:
        rows = scan_threads(days=2, limit=400)
        seat_ids = {m["thread_id"] for m in members if m.get("thread_id")}
        mine_parents = {r.get("parent") for r in rows
                        if r.get("id") in seat_ids and r.get("parent")}
        with db() as c:
            bound = {r["thread_id"] for r in
                     c.execute("SELECT thread_id FROM members WHERE thread_id<>''")
                     if r["thread_id"]}
        for r in rows:
            if r.get("parent") not in mine_parents:      continue
            if r.get("id") in bound:                     continue
            if now - r.get("mtime", 0) > UNSEATED_LIVE_SEC: continue
            # 收工了的不算「在跑」：退出时补的那笔也会刷新 mtime。
            # 只有 BUSY_TAIL（工具/思考/流式增量）才证明这一秒真的在干活，
            # 否则「刚建还没开口的会话」也会被当成没落座的员工报警。
            tail = session_tail((_LIVE_CACHE.get("paths") or {}).get(r["id"], ""))
            if (tail.get("type") or "") not in BUSY_TAIL: continue
            # 子代理被关掉又重开 = 一条新会话。工位还绑着旧会话，看板就会显示「摸鱼」，
            # 而人明明在跑。会话开头（spawn 的 prompt）写了工位名，且该工位当前没有活会话
            # → 让原员工接管这条新会话。只认「恰好一个」候选，认不准就晾着让人来定。
            head = r.get("head") or ""
            cands = [m for m in members
                     if not m.get("is_lead") and m["name"] and m["name"] in head
                     and not m["session_live"]]
            if len(cands) == 1:
                res = bind_member(pid, cands[0]["id"], r["id"],
                                  reason="auto:新会话开头写了这个工位名")
                if isinstance(res, dict) and not res.get("error"):
                    rebinds.append({"member": cands[0]["name"], "thread": r["id"][:8]})
                    continue
            unseated.append({"id": r["id"], "short": r["id"][:8],
                             "age_min": r.get("age_min", 0)})
    except Exception:                                     # noqa: BLE001
        unseated = []                                     # 扫不到就不报，宁缺勿假
    prio_counts = {k: len([t for t in open_tasks if t["prio"] == k]) for k in (1, 2, 3)}
    # 全员停摆：员工一个都不在干活。总监不算劳动力——它的活是调度，
    # 拿总监算「还有人在工作」会让停摆永远检测不出来。
    # 停摆时长取「最后一个停止写会话的人」到现在，这样才能说清「停了多久」；
    # 「3 个人离线」这种说法没法行动。
    workers = [m for m in members if not m.get("is_lead")]
    open_n = len(open_tasks)
    if not workers:
        idle_sec, idle_reason = None, "no_workers"
    elif any(m["state"] == "working" for m in workers):
        idle_sec, idle_reason = 0, ""
    else:
        sils = [int(m["session_silent"]) for m in workers
                if int(m.get("session_silent") if m.get("session_silent") is not None else -1) >= 0]
        if sils:
            idle_sec, idle_reason = min(sils), "all_silent"
        else:
            idle_sec, idle_reason = None, "no_session_data"
    # 两级信号，别混成一个：
    #   warn  = 一个人都没在干活（立刻亮，不管停了多久）
    #   alert = 已经停了 15 分钟（硬闸，拦写操作）
    # 只用 alert 会漏掉最关键的场景：Codex 刚重启，退出时补的 turn_aborted
    # 把每条会话的 mtime 刷成「刚刚」，alert 要再等 15 分钟才亮；而此刻
    # 4 个工位确实一个都没在跑，看板却报「全部对得上」。实测踩过。
    idle_warn = bool(workers and open_n
                     and not any(m["state"] == "working" for m in workers))
    idle_alert = bool(idle_warn and (idle_sec or 0) >= IDLE_ALERT_SEC)
    # 脱轨：总监多久没碰过计划。它的存在意义是回答你那个问题——
    # 「他会不会主动去看」→ 不会。所以看板只能把这个事实钉在墙上。
    if last_plan:
        board_silent = max(0, int(now - _epoch(last_plan["created_at"])))
        last_plan_kind = last_plan["kind"]
        last_plan_at = last_plan["created_at"]
    else:
        board_silent, last_plan_kind, last_plan_at = -1, "", ""
    derail = bool(open_n and board_silent >= BOARD_SILENT_SEC)
    # 自驱：用户不说话时，谁能把这一轮叫醒。没有心跳 = 项目只在你开口时才存在。
    lead_m = next((m for m in members if m.get("is_lead")), None)
    auto = automation_status((lead_m or {}).get("thread_id") or "")
    auto["ready"] = bool(auto.get("found") and auto.get("has_board_check")
                         and auto.get("status") == "ACTIVE" and not auto.get("too_slow"))
    # 关键路径剩余 = 链尾那条任务预计几点交 − 现在。
    # 不能把链上各任务的 eta_left 相加：它们已经各自从 now 起算，
    # A 0–2h、B 2–5h 相加得 7h，实际收工只要 5h——那样会把「差多少」直接虚报一半。
    # 「谁在忙」和「离通还差多少」是两个问题：前者看成员行，后者只看这一个数。
    crit_open = [t for t in tasks if t.get("on_critical") and t["status"] != "done"
                 and t["status"] != "abandoned"]
    crit_finish = max((float(t.get("eta_at") or 0) for t in crit_open), default=0.0)
    critical_hours = round(max(0.0, crit_finish - now) / 3600.0, 1) if crit_open else 0.0
    critical_gap = (round(critical_hours - hours_left, 1) if hours_left is not None else None)
    # 无证据的 done：只统计，不回溯改状态。历史任务多半没留证据，
    # 直接把 13 条翻回未完成会让进度条撒谎；暴露数字比伪造闸门有用。
    done_without_evidence = [t for t in tasks if t["status"] == "done"
                             and not (t.get("verify_cmd") or "").strip()]
    # 断线摘要：面板顶部横幅用。全量明细走 /resume-brief，
    # 这里只留三个数，避免每次轮询都重算一份大对象。
    orphans = [t for t in tasks if t["status"] == "doing"
               and next((x for x in members if x["id"] == t["assignee_id"]), {}).get("state") != "working"]
    # ---------------------------------------------------------------- 全员对齐
    # 用户的原话：「总监以及他下面的所有队员必须强制对齐 Teamboard」。
    # 「对齐」在这里只有一种可核对的含义：**谁在跑，跑的是看板上的哪一条**。
    # 三种对不上，每种只有一个正确解法：
    #   unlogged  = 会话在跑，但看板上没有它的在办任务 → update_task(doing) + member_duty
    #   nosession = 有在办任务，但工位没绑会话线程     → bind_member
    #   ghost     = 在办任务的主人已经不在跑          → 回收任务，或 spawn 重开再 bind
    # 总监不参与 unlogged：它的「活」就是这条会话本身，把它算成没挂号会永久红灯。
    # 总监的脱轨由 board.derail 单独报，但那一条**不进闸**——脱轨时它唯一该做的事
    # 就是重排计划（create_approval），把出口堵上等于把车锁死在沟里。
    _last_touch = {}
    for t in tasks:
        aid = t.get("assignee_id") or ""
        if not aid:
            continue
        ts = _epoch(t.get("updated_at") or t.get("created_at"))
        if ts > _last_touch.get(aid, 0):
            _last_touch[aid] = ts
    align_unlogged, align_nosession, align_ghost = [], [], []
    # 「在看板上」= 手上有一条正在推进的活。doing / blocked / review 都算：
    # 卡住的人也在为这条任务干活（正在查卡点），提交待验收的人也可能在补证据。
    # 只有一条都不占、会话却还在写的人，才是真的「干活不挂号」。
    REGISTERED = ("doing", "blocked", "review")
    for m in members:
        if m.get("is_lead"):
            continue
        active_mine = [t for t in tasks
                       if t.get("assignee_id") == m["id"] and t["status"] in REGISTERED]
        doing_mine = [t for t in tasks
                      if t.get("assignee_id") == m["id"] and t["status"] == "doing"]
        if active_mine and not m["thread_id"]:
            align_nosession.append({"id": m["id"], "name": m["name"],
                                    "seat": m.get("seat_label") or "",
                                    "task": active_mine[0]["title"],
                                    "task_id": active_mine[0]["id"]})
        if doing_mine and m["state"] not in ("working", "blocked"):
            t0 = doing_mine[0]
            hung = max(0, int(now - _epoch(t0.get("updated_at") or t0.get("created_at"))))
            if hung >= ALIGN_GHOST_GRACE_SEC:
                align_ghost.append({"id": m["id"], "name": m["name"],
                                    "seat": m.get("seat_label") or "",
                                    "state": m["state"], "hung_sec": hung,
                                    "human": _hm(hung),
                                    "task": t0["title"], "task_id": t0["id"]})
        if m["session_live"] and not active_mine and open_n:
            last = max(_last_touch.get(m["id"], 0), float(m.get("bound_at") or 0))
            gap = int(now - last) if last > 0 else -1
            if gap < 0 or gap >= ALIGN_GRACE_SEC:
                align_unlogged.append({"id": m["id"], "name": m["name"],
                                       "seat": m.get("seat_label") or "",
                                       "agent": m.get("agent_leaf") or "",
                                       "silent": int(m.get("session_silent") if
                                                     m.get("session_silent") is not None else -1),
                                       "unlogged_sec": gap,
                                       "human": _hm(gap) if gap > 0 else "刚上线"})
    align = {"ok": not (align_unlogged or align_nosession or align_ghost),
             "checked": len(workers),
             "unlogged": align_unlogged, "nosession": align_nosession,
             "ghost": align_ghost,
             "grace_sec": ALIGN_GRACE_SEC, "ghost_grace_sec": ALIGN_GHOST_GRACE_SEC,
             # 脱轨只报不拦：它不能进 ok，否则总监想重排计划时会被自己的闸挡住
             "derail": derail}
    last_any = max([float(m.get("last_alive") or 0) for m in members] or [0])
    return {"project": dict(p), "members": members, "tasks": tasks, "stats": stats,
            "resume": {"orphans": len(orphans),
                       "not_working": len([m for m in members if m["state"] != "working"]),
                       "total": len(members),
                       "gap_sec": int(now - last_any) if last_any > 0 else -1,
                       "needs_reconcile": bool(orphans)},
            # 配比：总监填的配额 + 实际切出来的条数。看板顶部照这个显示，别让「全 P1」复发
            "priority_ratio": prio_ratio,
            "priority_quota": {"p1": q1, "p2": q12 - q1, "p3": len(open_tasks) - q12},
            "priority_counts": prio_counts,
            "priority_declared_p1": len([t for t in open_tasks
                                         if t.get("priority_declared") == 1]),
            # 全员停摆：不是「谁离线了」，而是「一个干活的都没有」。
            # 总监看成员行会挑着看（谁在忙就夸谁），只有这个聚合值躲不掉。
            "idle": {"sec": int(idle_sec) if idle_sec is not None else None,
                     "human": _hm(idle_sec) if idle_sec else "",
                     "reason": idle_reason,
                     # warn = 0 人在干活（立刻）；alert = 已持续 15 分钟（硬闸）
                     "warn": idle_warn,
                     "alert": idle_alert,
                     "threshold_sec": IDLE_ALERT_SEC,
                     "workers": len(workers),
                     "working": len([m for m in workers if m["state"] == "working"]),
                     "open_tasks": open_n,
                     # 每个人的静默秒数，长→短。总监要能一眼点名谁停得最久，
                     # 而不是自己再翻五张成员卡。-1 = 没有会话数据（等于从没开工）
                     "silent": sorted(
                         [{"name": m.get("name") or "?", "seat_no": m.get("seat_no") or 0,
                           "state": m["state"],
                           "silent": int(m.get("session_silent") or -1),
                           "task": m.get("current_task") or "",
                           "load": int(m.get("load") or 0)}
                          for m in workers],
                         key=lambda x: (x["silent"] < 0, -(x["silent"] or 0)))},
            # 全员对齐：谁在跑、跑的是哪一条。对不上就拦「开新计划/加人/交报告」
            "align": align,
            # 自驱器：决定「用户不说话时项目会不会继续跑」
            "autopilot": auto,
            # 脱轨：总监多久没碰计划。员工在干活也可能脱轨——那时候看板上的
            # 进度、排期、卡点全是过期数据，越看越像在推进。
            "board": {"silent_sec": board_silent,
                      "human": _hm(board_silent) if board_silent and board_silent > 0 else "",
                      "threshold_sec": BOARD_SILENT_SEC,
                      "derail": derail,
                      "last_kind": last_plan_kind,
                      "last_at": last_plan_at,
                      "open_tasks": open_n},
            # 编制：工位固定，会话可换。加人/换人/撤编都挂在这张申请单上等用户批
            "staff_requests": sreq,
            "staff_pending": [s for s in sreq if s["status"] == "pending"],
            # 欠回执：用户已经批了/驳了，总监还没回「收到」。写操作会被 ack_gate 拦。
            "need_ack": list_need_ack(pid),
            "roster": {
                "seats": len(members),
                "vacant": len([m for m in members if m["roster_state"] == "vacant"]),
                "workers": len([m for m in members if not m.get("is_lead")]),
                "cap": TEAM_CAP,
                "swapped": len([m for m in members if m["bind_count"] > 1]),
            },
            "leases": leases, "hypotheses": hyps[:80], "criteria": crits,
            "lease_overdue": [{"resource": l["resource"], "task": l["holder_title"],
                               "member": l.get("holder_member", ""),
                               "overdue_min": l["overdue_min"]} for l in lease_overdue],
            "criteria_green": len([x for x in crits if x["status"] == "green"]),
            # unknown（还没跑过判定）不进分母：否则「一条都没验」会被读成「全不达标」
            "criteria_total": len([x for x in crits if x["status"] in ("green", "red")]),
            "criteria_all": len(crits), "blocker": blocker,
            "pending_approvals": pending, "team_cap": TEAM_CAP,
            # 在跑却没落座的子代理：看板不能假装它们不存在，也不能假装它们在编
            "unseated": unseated,
            "auto_bound": rebinds,
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
            # 「离通还差多少」：关键路径上还剩几小时 vs 还剩多少时间
            "critical_hours": critical_hours,
            "critical_gap": critical_gap,
            "critical_open": len(crit_open),
            "done_without_evidence": len(done_without_evidence),
            "done_total": len([t for t in tasks if t["status"] == "done"]),
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


def bind_member(pid, mid, thread_id, reason="", host_pid=0, host_boot=""):
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
        # 绑会话的这一刻 Codex 一定还开着：顺手把宿主进程记下来。
        # 之后 Codex 一退，这个 pid 就没了，看板当场判死，不用等心跳过期。
        hp = safe_int(host_pid, 0, 0)
        boot = (host_boot or proc_boot(hp)) if hp > 0 else ""
        if hp > 0:
            c.execute("UPDATE members SET thread_id=?, bound_at=?, bind_count=?, last_seen=?,"
                      " host_pid=?, host_boot=?, beat_at=?, last_alive=?, dead_reason='',"
                      " roster_state=CASE WHEN roster_state IN ('','vacant') THEN 'active'"
                      "                   ELSE roster_state END WHERE id=?",
                      (thread_id, now, binds + 1, now, hp, boot, now, now, mid))
        else:
            c.execute("UPDATE members SET thread_id=?, bound_at=?, bind_count=?, last_seen=?,"
                      " roster_state=CASE WHEN roster_state IN ('','vacant') THEN 'active'"
                      "                   ELSE roster_state END WHERE id=?",
                      (thread_id, now, binds + 1, now, mid))
        log_event(c, pid, "member.rebind", actor=mid,
                  payload=f"工位#{int(m['seat_no'] or 0)} {old or '(空)'} → {thread_id}")
    # 换会话即换名：身份跟着会话走，不消耗一次人事审批——改的是「这一轮谁坐这把椅子」，
    # 不是「编制里多了一个人」。名字对不上从此不可能发生，因为名字是会话自己报的。
    ap = agent_info(session_path_of(thread_id))
    if ap.get("agent_path"):
        with db() as c:
            c.execute("UPDATE members SET agent_path=?, agent_nickname=? WHERE id=?",
                      (ap["agent_path"], ap.get("agent_nickname", ""), mid))
    return {"ok": True, "id": mid, "name": m["name"], "thread_id": thread_id,
            "seat_no": int(m["seat_no"] or 0), "bind_count": binds + 1,
            "prev_thread": old,
            "agent_path": ap.get("agent_path", ""),
            "agent_nickname": ap.get("agent_nickname", "")}


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
            "next": "等用户在会话里点批准（approval_inbox 会把卡片推到对话里）；"
                    "批准后用这个 id 作为 approval_id 继续"}


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
        mb.update({"approval_id": rid,
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
    ok, why = _approved_staff(pid, b, "add")
    if ok:
        return None
    with db() as c:
        active = c.execute("SELECT COUNT(*) FROM members WHERE project_id=?"
                           " AND COALESCE(roster_state,'active')<>'vacant'", (pid,)).fetchone()[0]
    if active <= 0:
        return None      # 空团队：第一波建班子走计划审批，这里放行
    head = "编制已经成型，不能随手加人：员工会一直在变，看板就失去意义。"
    if why != "缺 approval_id":
        head = f"审批单不能用：{why}。"
    return {"error": head +
                     "要加人调 request_staff_change(kind='add', ...)，用户在会话里批准后，"
                     "把返回的审批单 id 作为 approval_id 传进来才落库。",
            "need_approval": True, "staff_request_required": True}


def _approved_staff(pid, b, kind):
    """校验这张编制审批单：必须已批准、没被用过、类型和项目都对得上。

    以前这里只认一个 size_approved=true 的布尔位——模型随手传一个就能绕过
    用户审批。现在改成认审批单 id，批准一次只能落一个人。
    """
    rid = (b.get("approval_id") or "").strip()
    if not rid:
        return False, "缺 approval_id"
    with db() as c:
        r = c.execute("SELECT * FROM staff_requests WHERE id=?", (rid,)).fetchone()
    if not r or r["project_id"] != pid:
        return False, f"没有这张审批单：{rid}"
    if r["kind"] != kind:
        return False, f"审批单类型是 {r['kind']}，不是 {kind}"
    if r["status"] != "approved":
        return False, f"这张审批单还没批准（{r['status']}）"
    if float(r["consumed_at"] or 0) > 0:
        return False, "这张审批单已经用过了，一件事一张单"
    return True, ""


def add_member(pid, b):
    gate = retro_gate(pid)
    if gate:
        return gate
    # 全员停摆时不许扩编：4 个人 0 个在跑，第 5 个人不会让项目变快，
    # 只会多一具僵尸。先让现有的人动起来。
    gate = idle_gate(pid)
    if gate:
        return gate
    # 扩编同理：人已经在跑但没挂号，再加一个只会多一份对不上的账。
    gate = align_gate(pid)
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
            if _approved_staff(pid, b, "add")[0]:
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
    if n >= TEAM_CAP and not _approved_staff(pid, b, "add")[0]:
        return {"error": f"员工上限 {TEAM_CAP} 人（不含项目总监），要开到第 {n+1} 个员工"
                         "必须先让用户审批：走 request_staff_change(kind='add')，"
                         "用户批准后把审批单 id 当 approval_id 传进来",
                "field": "capacity", "need_approval": True, "staff_request_required": True}
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
        rid = (b.get("approval_id") or "").strip()
        if rid:
            c.execute("UPDATE staff_requests SET consumed_at=? WHERE id=? AND consumed_at=0",
                      (now, rid))
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
    # 不能「建成即完成」：绕开 doing 就没有执行记录、没有证据校验，
    # 交付面等于敞开。要完成请先建成 backlog/doing，再用 PATCH 带证据置 done。
    if status == "done":
        return {"error": "任务不能直接建成已完成：先建 backlog（或 doing 派给人），"
                         "做完再用 PATCH status=done 带 evidence/verify_cmd 收工。",
                "field": "status"}
    # 建成即开工同样要走闸：欠回执 / 上游没完成，都不能靠「新建一条 doing」绕过。
    if status == "doing":
        gate = ack_gate(pid)
        if gate:
            return gate
        dep = (b.get("depends_on") or "").strip()
        if dep:
            with db() as c1:
                up = c1.execute("SELECT title,status FROM tasks WHERE id=? AND project_id=?",
                                (dep, pid)).fetchone()
            if up and up["status"] != "done":
                return {"error": f"上游「{up['title']}」还没完成（{up['status']}），"
                                 f"这条不能直接建成 doing。", "field": "depends_on",
                        "need_force": True, "upstream": {"title": up["title"]}}
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
        # 欠回执时不许改任务状态：用户批过的那一条必须先被「收到」，
        # 否则下一轮会按旧编制/旧计划继续跑，而用户以为已经生效。
        # 只挡状态迁移，不挡 progress —— 报进度不是推进，别把唯一的信息通道也堵上。
        if "status" in b and b["status"] != t["status"]:
            gate = ack_gate(t["project_id"])
            if gate:
                return gate
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
        # 依赖顺序：上游没完成就开干/收工，等于把计划当墙纸。
        # 之前只在看板上画个红点（blocked_upstream），那是建议不是约束——
        # 总监完全可以先做链尾，然后把「完成 13 条」摆给你看。
        if st in ("doing", "done") and st != t["status"] and not b.get("force"):
            dep = (b.get("depends_on") or t["depends_on"] or "").strip()
            if dep:
                with db() as c2:
                    up = c2.execute("SELECT title,status FROM tasks WHERE id=?"
                                    " AND project_id=?",
                                    (dep, t["project_id"])).fetchone()
                if up and up["status"] != "done":
                    return {"error": f"上游「{up['title']}」还没完成（{up['status']}），"
                                     f"这条不能进 {st}。先把上游推完；确认串行是多余的，"
                                     f"带 force=true 并说明理由。",
                            "field": "depends_on", "need_force": True,
                            "upstream": {"id": dep, "title": up["title"],
                                         "status": up["status"]}}
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
            # 交付必须留下执行痕迹：从没进过 doing、也没计过时的任务，
            # 说明没有任何人在它上面跑过——那不是「做完了」，是「写完了」。
            # 这一条是「必须经过插件」最硬的落地：不挂号就交不了。
            has_run = bool(c.execute("SELECT 1 FROM task_sessions WHERE task_id=? LIMIT 1",
                                     (tid,)).fetchone())
            if not has_run and not (t["time_spent"] or 0) and not (t["doing_since"] or 0):
                return {"error": "这条任务没有任何执行记录（没进过 doing、也没计过时），不能标完成。"
                                 "先 status=doing + assignee_id 派给具体的人，让他真跑一遍；"
                                 "确有特殊情况（历史任务补录）带 force=true",
                        "field": "execution", "need_execution": True}
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
            # 「修好了」不值钱，「任何人能重跑的那条命令」才值钱。
            # 只给文件路径 = 证明文件存在，证明不了跑过、通过。
            vc = (b.get("verify_cmd") or t["verify_cmd"] or "").strip()
            if not vc:
                return {"error": "标完成必须带 verify_cmd：一条能复制粘贴重跑、"
                                 "一眼看出通过/失败的命令（例：python3 tests/x.py --case 3）",
                        "field": "verify_cmd", "need_evidence": True,
                        "hint": "纯人工判断确实没有命令的，带 force=true；看板会把它算进「无证据完成」"}
            b.setdefault("verify_cmd", vc)
            b.setdefault("verified_at", now_iso())
            vo = (b.get("verify_out") or "").strip()
            if vo:
                # 输出只留 5 行：日志全文去 evidence_path 里找，卡片上要的是结论
                b["verify_out"] = "\n".join(vo.splitlines()[:5])[:600]
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


def _hm(sec):
    """秒 → 人话（1小时2分）。"""
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分钟"
    return f"{sec // 3600}小时{(sec % 3600) // 60}分"


DEAD_WHY = {"process": "宿主进程已退出（Codex 关了）",
            "session": "会话已停止写入",
            "heartbeat": "长时间没有心跳"}


def project_health(pid, st=None):
    """一个接口回答「哪里对不上」：静默、掉线、空工位、无主任务。

    三处判断（面板、board_overview、总监自查）读同一份数字，
    否则总监每轮开局都要拿 list_agents 和看板手工比对，永远比不完。
    """
    st = st or project_state(pid)
    if not st:
        return None
    now = time.time()
    stale, vacant, orphan_tasks, unseated = [], [], [], st.get("unseated") or []
    silent_min = -1
    for m in st["members"]:
        sil = int(m.get("session_silent") or -1)
        if sil >= 0:
            silent_min = sil if silent_min < 0 else min(silent_min, sil)
        doing = [t for t in st["tasks"]
                 if t["assignee_id"] == m["id"] and t["status"] == "doing"]
        # 掉线：有活没干完，但会话已经静默。90 秒前它还算「在干活」，
        # 现在必须由这个字段把窗口压下来——不然要等 15 分钟才变「僵住」。
        if m["state"] == "stale" and doing:
            stale.append({"seat": m.get("seat_label"), "name": m["name"],
                          "agent": m.get("agent_leaf") or "",
                          "silent_sec": sil, "state": m["state"],
                          "doing": doing[0]["title"], "doing_id": doing[0]["id"]})
        # 空工位：会话死够久、手上又没活。这是陈述事实，不是撤编，不消耗审批。
        dead_for = (now - float(m.get("last_alive") or 0)) if m.get("last_alive") else -1
        if (not doing and not m.get("is_lead") and m["state"] in ("offline", "stale")
                and dead_for > 2 * 3600):
            vacant.append({"seat": m.get("seat_label"), "name": m["name"],
                           "agent": m.get("agent_leaf") or "",
                           "dead_human": _hm(int(dead_for))})
    for t in st["tasks"]:
        if t["status"] != "doing":
            continue
        who = next((x for x in st["members"] if x["id"] == t["assignee_id"]), None)
        if who is None or who["state"] not in ("working", "blocked"):
            orphan_tasks.append({"id": t["id"], "title": t["title"],
                                 "assignee": (who or {}).get("name", "—"),
                                 "state": (who or {}).get("state", "no-owner")})
    return {"project_id": pid, "name": st["project"].get("name", ""),
            "generated_at": now_iso(),
            "silent_sec": silent_min,
            "stale": stale, "vacant": vacant, "unseated": unseated,
            "orphan_tasks": orphan_tasks,
            # 对齐：三个数分开给，前端不用自己再拆
            "align": (st.get("align") or {}),
            "counts": {"members": len(st["members"]), "stale": len(stale),
                       "vacant": len(vacant), "unseated": len(unseated),
                       "orphan_tasks": len(orphan_tasks),
                       "unlogged": len((st.get("align") or {}).get("unlogged") or []),
                       "nosession": len((st.get("align") or {}).get("nosession") or []),
                       "ghost": len((st.get("align") or {}).get("ghost") or [])},
            "summary": " · ".join(filter(None, [
                # 停摆必须排在最前面：它是唯一一条「整队都没在动」的结论，
                # 其余几项都只是局部异常。
                (f"全员停摆 {(st.get('idle') or {}).get('human', '')}"
                 if (st.get("idle") or {}).get("alert") else
                 ("0 人在干活" if (st.get("idle") or {}).get("warn") else "")),
                # 对齐排在停摆之后、其余之前：它解释「为什么看板是空的」。
                (("团队没对齐：" + "·".join(filter(None, [
                    (f"{len((st.get('align') or {}).get('unlogged') or [])} 人在跑没挂号"
                     if (st.get("align") or {}).get("unlogged") else ""),
                    (f"{len((st.get('align') or {}).get('ghost') or [])} 条任务人已不在跑"
                     if (st.get("align") or {}).get("ghost") else ""),
                    (f"{len((st.get('align') or {}).get('nosession') or [])} 条任务没绑会话"
                     if (st.get("align") or {}).get("nosession") else "")])))
                 if not (st.get("align") or {}).get("ok", True) else ""),
                ("没有自驱心跳，用户不出声项目就停"
                 if not (st.get("autopilot") or {}).get("ready") else ""),
                (f"总监 {st['board']['human']}没碰计划"
                 if (st.get("board") or {}).get("derail") else ""),
                # 欠回执：用户点过批准，总监没回「收到」——决定没生效，后续全是旧账
                (f"{len(st.get('need_ack') or [])} 条审批已决定但没回执"
                 if st.get("need_ack") else ""),
                f"{len(stale)} 个工位已静默" if stale else "",
                f"{len(orphan_tasks)} 条任务无主" if orphan_tasks else "",
                f"{len(unseated)} 个会话没登记工位" if unseated else "",
                f"{len(vacant)} 个空工位" if vacant else ""])) or "全部对得上"}


def resume_brief(pid):
    """交班简报：新会话开工前，总监必须先看这一份。

    Codex 一退出子代理就停了，但数据库里还留着「doing」。新会话里如果总监
    不知道这件事，会以为人还在干、继续等，整条链空转几十分钟。
    这里把「谁断了、断多久、哪条任务烂尾、该怎么处置」一次摊开。
    """
    st = project_state(pid)
    if not st:
        return None
    now = time.time()
    tasks = st["tasks"]

    doing_by = {}
    for t in tasks:
        if t["status"] == "doing" and t.get("assignee_id"):
            doing_by.setdefault(t["assignee_id"], []).append(t)

    team, orphans = [], []
    for m in st["members"]:
        alive = float(m.get("last_alive") or 0)
        gone = int(now - alive) if alive > 0 else -1
        doing = doing_by.get(m["id"], [])
        idle_of = {t["id"]: max(0, int(now - _epoch(t.get("updated_at") or t.get("created_at"))))
                   for t in doing}
        team.append({
            "id": m["id"], "seat": m.get("seat_label"), "name": m["name"],
            # 会话自报的身份：换人接手时，这两个字段比工位名更能说明「谁在干」
            "agent": m.get("agent_leaf") or "", "nickname": m.get("agent_nickname") or "",
            "silent_sec": m.get("session_silent", -1),
            "role": m.get("role", ""), "state": m["state"],
            "state_source": m.get("state_source", ""),
            "dead_reason": m.get("dead_reason", ""),
            "dead_why": DEAD_WHY.get(m.get("dead_reason"), ""),
            "host_alive": m.get("host_alive"),
            "gone_for": gone, "gone_human": _hm(gone) if gone >= 0 else "无记录",
            "is_lead": bool(m.get("is_lead")),
            "doing": [{"id": t["id"], "title": t["title"],
                       "progress": safe_int(t.get("progress"), 0, 0, 100),
                       "idle_sec": idle_of.get(t["id"], 0),
                       "idle_human": _hm(idle_of.get(t["id"], 0))} for t in doing],
        })
        if m["state"] != "working":
            for t in doing:
                orphans.append({
                    "task_id": t["id"], "title": t["title"],
                    "assignee": m["name"], "seat": m.get("seat_label") or "",
                    "progress": safe_int(t.get("progress"), 0, 0, 100),
                    "idle_sec": idle_of.get(t["id"], 0),
                    "idle_human": _hm(idle_of.get(t["id"], 0)),
                    "why": DEAD_WHY.get(m.get("dead_reason"), "状态不明"),
                })

    # 最后一次「确实有人在干活」= 全队 last_alive 的最大值
    last_any = max([float(m.get("last_alive") or 0) for m in st["members"]] or [0])
    gap = int(now - last_any) if last_any > 0 else -1
    dead = [x for x in team if x["state"] != "working"]
    up = [x for x in team if x["state"] == "working"]

    longest = max([x["gone_for"] for x in dead if x["gone_for"] >= 0] or [0])
    gap_text = "刚刚" if 0 <= gap < 60 else (_hm(gap) if gap >= 0 else "无记录")
    if not st["members"]:
        verdict = "这个项目还没有员工。先建团队再派活。"
    elif not dead:
        verdict = f"全队 {len(up)} 人都在线上，没有断线员工。"
    elif not up:
        verdict = (f"全队 {len(team)} 人都不在岗，最后一次有人干活是 {gap_text}前。"
                   + (f" {len(orphans)} 条 doing 任务烂尾。" if orphans else ""))
    elif not orphans:
        verdict = (f"{len(up)}/{len(team)} 人在岗；{len(dead)} 人不在岗，"
                   f"最长已断 {_hm(longest)}。没有 doing 任务被挂住。")
    else:
        verdict = (f"{len(dead)}/{len(team)} 人不在岗（最长已断 {_hm(longest)}），"
                   f"{len(orphans)} 条 doing 任务烂尾，需要先处置再派新活。")

    actions = []
    for x in team:
        if x["state"] == "stale" and x["doing"]:
            actions.append(f"{x['seat']} {x['name']}：{x['doing'][0]['title']}"
                           f"（{x['doing'][0]['idle_human']}未更新）——先决定续做还是转 blocked")
    if orphans:
        actions.append("烂尾任务不要直接改 done：能续就重新拉起子代理并 bind_member 接管同一工位；"
                       "不能续就改 blocked 并写清卡点")
    if not up:
        actions.append("当前没有人在岗：先按原编制把子代理拉起来并 bind_member，再谈新任务")
    if not actions:
        actions.append("无需处置，直接继续原计划")
    # 在跑但没登记工位的会话：不算员工，却在烧 token。这是「看板对不上实际情况」
    # 的第一大来源，必须让总监在开工第一步就看到。
    unseated = st.get("unseated") or []
    if unseated:
        verdict = (f"⚠ {len(unseated)} 个会话在跑但没登记工位"
                   f"（{', '.join(x['short'] for x in unseated[:3])}）——看板看不见它，"
                   f"派不了活、记不了时，等于白跑。先 bind_member。 | " + verdict)
        actions.insert(0, "先给在跑的会话落座：bind_member(member=\"<工位名>\", "
                          "thread_id=\"<全 UUID>\")，再 member_duty(on=true, minutes=45)")
    # 对齐：开工第一件事，把「谁在跑 / 跑的是哪条」对一遍。对不上就不要往下走，
    # 否则后面所有数字都建立在一张空账上。
    align = st.get("align") or {}
    if not align.get("ok", True):
        bits = []
        if align.get("unlogged"):
            bits.append("在跑没挂号 " + "、".join(
                f"{x['name']}({x['human']})" for x in align["unlogged"][:4]))
        if align.get("ghost"):
            bits.append("任务主人已停 " + "、".join(
                f"{x['name']}·{x['task'][:12]}" for x in align["ghost"][:3]))
        if align.get("nosession"):
            bits.append("有活没绑会话 " + "、".join(
                x["name"] for x in align["nosession"][:3]))
        verdict = (f"⛔ 团队没对齐看板：{'；'.join(bits)}。 | " + verdict)
        actions.insert(0, "先对齐：每个在跑的人 update_task(status=doing, assignee_id=<mid>) "
                          "挂到看板上，再 member_duty(on=true) 登记在场；"
                          "任务主人已经不在跑的，先收回 backlog 再重新派人")
    # 欠回执：用户点过批准/驳回，总监没回「收到」。这是「决定没生效」，
    # 比任何进度数字都靠前——它会拦住改状态/新计划/导报告。
    need_ack = st.get("need_ack") or []
    if need_ack:
        verdict = ("⛔ 欠回执 " + str(len(need_ack)) + " 条（用户已决定、你没回话）："
                   + "、".join(f"{x['label']}·{x['title'][:16]}（{x['decision_cn']}）"
                               for x in need_ack[:3])
                   + "。先逐条 ack 回「收到」并说下一步。 | " + verdict)
        actions.insert(0, "先回执：" + "；".join(
            f"ack(kind='{x['kind']}', id='{x['id']}')" for x in need_ack[:3]))
    # 作废清单：接手的人最容易犯的错是把已经排除的路再走一遍。
    # 这条比任何进度数字都省时间，所以放在简报里，不藏在任务详情里。
    invalidated = [{"id": t["id"], "title": t["title"],
                    "reason": (t.get("abandoned_reason") or "").strip(),
                    "when": t.get("updated_at", "")}
                   for t in tasks if t["status"] == "abandoned"]
    if invalidated:
        actions.append(f"先读「已作废 {len(invalidated)} 条」：这些路已经排除了，别再走一遍")

    return {
        "project_id": pid, "name": st["project"].get("name", ""), "generated_at": now_iso(),
        "gap_sec": gap, "gap_human": _hm(gap) if gap >= 0 else "无记录",
        "last_active_at": (datetime.fromtimestamp(last_any).astimezone().isoformat(timespec="seconds")
                           if last_any > 0 else ""),
        "working": len(up), "not_working": len(dead), "total": len(team),
        "needs_reconcile": bool(orphans),
        "align": align,
        "verdict": verdict,
        "orphans": orphans,
        "unseated": unseated,
        "invalidated": invalidated,
        "team": team,
        "actions": actions,
        "note": "这是事实快照，不是建议：先处置 orphans，再派新活。",
    }


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
        # 编制是用户批准过的：改人名/岗位等于换人，必须走审批。
        # 允许改的只有「状态类」字段——模型、容量、备注这些不动编制的。
        locked = [k for k in ("name", "role")
                  if k in b and (b[k] or "").strip() != (m[k] or "").strip()]
        if locked and not b.get("staff_approved"):
            return {"error": f"「{m['name']}」的{'、'.join(locked)}不能直接改——换人/改岗位要走审批。"
                             "调 request_staff_change(kind='replace', member=..., name=..., role=..., reason=...) "
                             "提交给用户，批准后才会落到工位上。",
                    "need_approval": True, "staff_request_required": True, "field": locked[0]}
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


def heartbeat(mid, b=None):
    """刷心跳；带 pid 的会顺手登记宿主进程，Codex 一退出就能当场判死。

    pid 必须由调用方传（MCP server 自己的 pid 不是 Codex 的），
    子代理上报时传它所在那个 Codex 进程；不传就维持原来的纯时间窗口判定。
    """
    b = b or {}
    with db() as c:
        row = c.execute("SELECT * FROM members WHERE id=?", (mid,)).fetchone()
        if not row:
            return None
        ts = time.time()
        sets, vals = ["last_seen=?"], [ts]
        try:
            hp = int(b.get("pid") or 0)
        except (TypeError, ValueError):
            hp = 0
        boot = (b.get("boot") or proc_boot(hp)) if hp > 0 else ""
        # 进程已经死了还刷心跳，等于用一条假消息把断线时间往后推。
        # 这种心跳只登记 pid（好让状态机判死），不算「活着」。
        alive = proc_alive(hp, boot) if hp > 0 else None
        if alive is not False:
            sets += ["beat_at=?", "last_alive=?"]
            vals += [ts, ts]
        if hp > 0:
            sets += ["host_pid=?", "host_boot=?", "dead_reason=''"]
            vals += [hp, boot]
        c.execute(f"UPDATE members SET {','.join(sets)} WHERE id=?", vals + [mid])
    return {"ok": True, "id": mid, "ts": now_iso(),
            "host_alive": alive}


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
        hp = safe_int(b.get("host_pid"), 0, 0)
        for mid in hit:
            c.execute("UPDATE members SET last_seen=? WHERE id=?", (ts, mid))
        if hp > 0 and lead:
            # 总监的宿主进程：Codex 一退，这条 pid 就没了，总监当场显示断线
            c.execute("UPDATE members SET host_pid=?, host_boot=? WHERE id=?",
                      (hp, b.get("host_boot") or proc_boot(hp), lead["id"]))
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
        # 空壳项目不上首页：只建了壳、计划还没批（或刚被驳回）时任务和成员都是 0，
        # 留着只会让首页多一张「0 任务 0 人」的卡片。行不删——总监改完计划重新提交，
        # 批准后任务落库，卡片自然回来。
        if not st["tasks"] and not st["members"] and not (st.get("pending_approvals") or []):
            continue
        hl = project_health(p["id"], st)
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
            # 首页卡片也要能看出「有人断了、任务还挂着」——点进去才发现就太晚了
            "resume": st.get("resume"),
            # 在跑但没落座的会话：首页就该喊，不然总监根本不知道有人在白跑
            "unseated": st.get("unseated") or [],
            # 首页也要能一眼看出「这个项目还会不会自己往前走」
            "idle": st.get("idle"),
            "autopilot": {k: (st.get("autopilot") or {}).get(k)
                          for k in ("found", "ready", "has_board_check",
                                    "interval_min", "too_slow", "status")},
            # 一句话说清「哪里对不上」：面板和 board_overview 读同一份数字
            "health": {"summary": hl["summary"], "counts": hl["counts"]},
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


class Board(ThreadingHTTPServer):
    """HTTPServer.server_bind() 会做一次 getfqdn(host) 反向解析；DNS 不通时卡死。

    看板只监听本机，server_name 用 IP 就够，不需要那段可能阻塞几十秒的解析。
    """
    daemon_threads = True

    def server_bind(self):
        super(HTTPServer, self).server_bind()   # 只做 TCPServer 的 bind，不碰 getfqdn
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


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
        m = re.fullmatch(r"/api/projects/([\w]+)/resume-brief", p)
        if m:
            r = resume_brief(m.group(1))
            return self._send(200 if r else 404, r or {"error": "no such project"})
        m = re.fullmatch(r"/api/projects/([\w]+)/health", p)
        if m:
            r = project_health(m.group(1))
            return self._send(200 if r else 404, r or {"error": "no such project"})
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
            # 交不了的报告不该交：全员停摆时导出的「进度」是假的，
            # 用户看到的是百分比，看不到下面一个人都没在跑。
            gate = idle_gate(m.group(1))
            if gate:
                gate["error"] = "导出被拦：" + gate["error"]
                return self._send(403, gate)
            gate = ack_gate(m.group(1))
            if gate:
                gate["error"] = "导出被拦：" + gate["error"]
                return self._send(403, gate)
            # 看板和实际执行体对不上时，导出的进度表就是在给用户看假账。
            gate = align_gate(m.group(1))
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
            return self._send(400 if isinstance(r, dict) and "error" in r else 201, r)
        # 回执：用户点了批准/驳回之后，总监必须把「收到」落成一条记录
        m = re.fullmatch(r"/api/projects/([\w]+)/ack", p)
        if m:
            r = ack_decision(m.group(1), (b.get("kind") or "").strip(),
                             (b.get("id") or "").strip(),
                             b.get("note", ""), b.get("by", ""))
            return self._send(400 if "error" in r else 200, r)
        m = re.fullmatch(r"/api/retros/([\w]+)/decide", p)
        if m:
            r = decide_retro(m.group(1), b)
            return self._send(400 if "error" in r else 200, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/approvals", p)
        if m:
            r = create_approval(m.group(1), b)
            return self._send(400 if "error" in r else 201, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/autopilot", p)
        if m:
            r = set_autopilot(m.group(1), b)
            return self._send(400 if "error" in r else 200, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/members", p)
        if m:
            r = add_member(m.group(1), b)
            return self._send(400 if isinstance(r, dict) and "error" in r else 201, r)
        m = re.fullmatch(r"/api/projects/([\w]+)/members/([\w]+)/bind", p)
        if m:
            r = bind_member(m.group(1), m.group(2), b.get("thread_id", ""), b.get("reason", ""),
                            b.get("host_pid") or 0, b.get("host_boot") or "")
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
            r = heartbeat(m.group(1), b)
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


def _safe_stdio():
    """Windows 控制台默认 cp1252：横幅里的「→」会让进程在 serve 之前直接死掉。"""
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _safe_stdio()
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default="",
                    help="SQLite 文件路径（优先级高于 TEAMBOARD_DB；默认 <脚本目录>/board.db）")
    a = ap.parse_args()
    if a.db:
        DB_PATH = os.path.abspath(os.path.expanduser(a.db))
    print(f"TeamBoard v2 -> http://{a.host}:{a.port}  (db: {DB_PATH})", flush=True)
    init_db()
    ensure_seats()          # 老库补工位号：编制要能一眼看出「谁是谁」
    backfill_sessions()
    srv = Board((a.host, a.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
