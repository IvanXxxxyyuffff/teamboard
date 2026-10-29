#!/usr/bin/env python3
"""TeamBoard MCP server (stdio). 薄封装：把看板 HTTP API 暴露成 MCP 工具。"""
import json, os, sys, time, urllib.request, urllib.error

from _autostart import boot as _boot
BASE = _boot()        # 探测已在跑的看板，没有就起一个（开源版无 launch agent）
NAME = "teamboard"
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
              encoding="utf-8") as _f:
        VERSION = _f.read().strip() or "2.1.0"
except OSError:
    VERSION = "2.1.0"
UI_URI = "ui://teamboard/panel13"   # v4: 面板改为经 MCP 代理取数（沙箱 CSP 禁 connect-src http）
HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "ui", "panel.html")
UI_MIME = "text/html;profile=mcp-app"
UI_META = {"ui": {"resourceUri": UI_URI}}
# 有 HTTP_PROXY 的机器上，urllib 默认把 127.0.0.1 也交给代理 → 本地看板永远超时。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 带这个 meta 的工具，结果会直接在会话里渲染成面板


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _host():
    """Codex 宿主进程。MCP server 是 Codex 拉起来的，父进程没了＝Codex 关了。

    把这个 pid 登记到看板，宿主一退出就能当场判定「人不在了」，
    不用等心跳/会话窗口过期（那要 5~15 分钟，期间看板会骗人）。
    """
    try:
        ppid = os.getppid()
        return {"host_pid": ppid} if ppid and ppid > 1 else {}
    except Exception:
        return {}


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=15) as r:
            raw = r.read()
        return json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode()
    except urllib.error.HTTPError as e:
        # 把 400/403/409 的 JSON 原样交给调用方，别让工具直接崩
        raw = e.read()
        try:
            out = json.loads(raw) if raw[:1] in (b"{", b"[") else {"error": raw.decode()}
        except Exception:
            out = {"error": raw.decode(errors="replace")[:300]}
        if isinstance(out, dict):
            out.setdefault("status", e.code)
        return out
    except urllib.error.URLError as e:
        # 服务没起：给出能直接照做的修复命令，而不是抛栈
        return {"error": f"连不上看板服务 {BASE}（{getattr(e, 'reason', e)}）。"
                         "先启动：python3 ~/.codex/teamboard/server.py &",
                "status": 0, "offline": True}
    except TimeoutError:
        return {"error": f"看板服务 {BASE} 15s 没响应（可能在跑长事务）", "status": 0}


def pick_project(name):
    rows = http("GET", "/api/projects")
    if not rows:
        raise RuntimeError("还没有任何项目")
    if not name:
        return rows[0]
    for p in rows:
        if name in (p["id"], p["name"]) or name in p["name"]:
            return p
    raise RuntimeError(f"找不到项目：{name}")


def fmt_dur(s):
    s = max(0, int(s or 0))
    if s < 60: return f"{s}s"
    m = s // 60
    if m < 60: return f"{m}m"
    h, mm = divmod(m, 60)
    if h < 24: return f"{h}h{mm}m" if mm else f"{h}h"
    d, hh = divmod(h, 24)
    return f"{d}d{hh}h" if hh else f"{d}d"


def _session_id():
    """本进程所在 Codex 会话的全 UUID（短 id 打不开会话深链，别截断）。"""
    return (os.environ.get("CODEX_SESSION_ID") or "").strip()


def touch_lead(pid):
    """总监每查一次看板就给自己刷一次心跳：这是真实活动，不是伪造。

    顺手把「总监现在坐在哪个会话」报上去：老会话已经停了就自动搬过来
    （换人不换工位，台账会记一条），否则看板会把正在干活的总监显示成摸鱼。
    """
    try:
        st = http("GET", "/api/projects/" + pid)
        lead = [m for m in st["members"]
                if m.get("is_lead") or "总监" in (m.get("role") or "")]
        if lead:
            body = {"members": [m["id"] for m in lead]}
            sid = _session_id()
            if sid:
                body["thread_id"] = sid
            body.update(_host())
            http("POST", f"/api/projects/{pid}/heartbeat", body)
    except Exception as e:  # 心跳失败不能挡住查状态
        log("touch_lead failed:", e)


def summarize(p):
    st = http("GET", "/api/projects/" + p["id"])
    s = st["stats"]
    lines = [f"# {p['name']}　{s['overall']}%",
             f"目标：{p.get('goal') or '-'}" + (f"　截止：{p['deadline']}" if p.get("deadline") else ""),
             "排期：" + ("·".join(filter(None, [
                 ("预计收工 " + time.strftime("%H:%M", time.localtime(st["eta_finish"]))
                  + ("" if st.get("eta_slip_hours") is None else
                     f"（比截止{'早' if st['eta_slip_hours']<=0 else '晚'} {abs(st['eta_slip_hours'])}h）"))
                 if st.get("eta_finish") else "",
                 ("关键路径 " + " → ".join(st["critical_chain"])) if (st.get("critical_chain") or [])[1:] else ""])) or "无在办任务"),
             "倒计时：" + ("·".join(filter(None, [
                 f"剩 {st['hours_left']}h" if st.get("hours_left") is not None else "",
                 f"剩余工时 {st['remaining_hours']}h" if st.get("remaining_hours") is not None else "",
                 "已超支" if st.get("over_budget") else ""])) or "未设截止"),
             f"任务 {s['total']}｜完成 {s['done']}｜进行中 {s['doing']}｜待验收 {s['review']}｜阻塞 {s['blocked']}", "",
             "## 成员"]
    rs = st.get("resume") or {}
    if rs.get("needs_reconcile"):
        lines.insert(0, f"⚠ {rs['orphans']} 条 doing 任务的人已经不在岗（不是在进行中）。"
                        f"先调 resume_brief 处置，别当它还活着等。")
    for m in st["members"]:
        state = {"working": "工作中", "idle": "空闲", "blocked": "阻塞",
                 "stale": "僵住（任务没回收）", "offline": "离线"}.get(m["state"], m["state"])
        model = f"　模型 {m['model']}" if m.get("model") else ""
        if m.get("effort"):
            model += f"／{m['effort']}"
        clock = ""
        if m["state"] == "offline" and m.get("offline_for", -1) > 0:
            clock += f"　离线 {fmt_dur(m['offline_for'])}"
        if m.get("heartbeat_age", -1) >= 0:
            clock += f"　心跳 {fmt_dur(m['heartbeat_age'])} 前"
        if m.get("time_running"):
            clock += f"　计时 {fmt_dur(m['time_running'])}"
        if m.get("time_total"):
            clock += f"　累计 {fmt_dur(m['time_total'])}"
        lines.append(f"- {m['name']}（{m['role']}）{state}　负载 {m['load']}/{m['capacity']}{model}"
                     f"　完成 {m['done_count']}/{m['total']}{clock}　{m['current_task'] or ''}")
    blocked = [t for t in st["tasks"] if t["status"] == "blocked"]
    if blocked:
        lines += ["", "## 阻塞"]
        for t in blocked:
            lines.append(f"- ⛔ {t['title']}：{t['blocked_reason'] or '未说明'}")
    pr = st.get("pressure") or {}
    rt = st.get("retro") or {}
    ut = st.get("utilization") or {}
    if pr.get("level") and pr["level"] != "none":
        slack = "、".join("%s（%d%%）" % (x["name"], x["util_pct"])
                          for x in (ut.get("slackers") or []))
        lines += ["", f"## 时间压力：{pr['level']}（{pr.get('reason','')}）",
                  "团队利用率 %s%%" % ut.get("team_pct", 0)
                  + ("；摸鱼：%s" % slack if slack else "；没人在摸鱼")]
    if rt.get("required"):
        lines += ["", f"⛔ 强制复盘未完成：{rt.get('reason')}",
                  f"   被拦：{'、'.join(rt.get('blocked_actions') or [])}",
                  "   交法：submit_retro(speed/deadline/staffing/utilization/scope/risk) 六问必答，交完等用户批准"]
    elif (rt.get("latest") or {}).get("status") == "approved":
        lines.append(f"复盘：第 {rt['latest']['round']} 轮已批准（{rt.get('rearm_min')} 分钟内有效）")
    return "\n".join(lines), st


TOOLS = [
    {"name": "board_overview", "description": "列出所有项目、总进度、成员数与阻塞数，并展示团队面板（可交互）",
     "inputSchema": {"type": "object", "properties": {}},
     "_meta": {"ui": {"resourceUri": UI_URI}}},
    {"name": "show_team_panel", "description": "在对话里内嵌打开团队面板：团队 / 员工 / 当前任务 / 进度",
     "inputSchema": {"type": "object", "properties": {}},
     "_meta": {"ui": {"resourceUri": UI_URI}}},
    # 用户手动打开的入口：出现在「输出内容 / 内容标签页」里，不用等模型调用
    {"name": "tb_api", "title": "看板数据代理",
     "description": "内部工具：代理访问看板 HTTP API，供内嵌面板读取数据（沙箱 CSP 禁止直连 127.0.0.1）",
     "inputSchema": {"type": "object", "properties": {
         "method": {"type": "string", "description": "GET/POST/PATCH/DELETE"},
         "path": {"type": "string", "description": "以 /api 开头的路径"},
         "body": {"type": "object", "description": "请求体"}},
         "required": ["path"]}},
    {"name": "teamboard_panel", "title": "团队看板",
     "description": "把团队看板作为内容标签页打开：我的团队 / 每个员工在做什么 / 进度 / 卡点",
     "inputSchema": {"type": "object", "properties": {}},
     "_meta": {"ui": {"resourceUri": UI_URI, "visibility": ["model", "app"]},
               "openai/ui": {"entrypoints": [{"type": "thread"}, {"type": "global"}]}}},
    {"name": "project_status", "description": "查看某个项目的详细状态：成员在做什么、任务分布、阻塞项",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目名或 id，留空取最近一个"}},
     "_meta": {"ui": {"resourceUri": UI_URI}}}},
    {"name": "resume_brief", "description": "交班简报：新会话/重启 Codex 之后必须先调这一个。回答「谁断了、断了多久、哪条 doing 任务烂尾、先处置什么」。不调就派活，会对着已经停掉的子代理空等",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目名或 id，留空取最近一个"}}}},
    {"name": "open_board", "description": "返回看板链接，用于在 Codex 面板里打开；不传 project 打开全部项目总览",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目名或 id，留空=总览"}}}},
    {"name": "create_project", "description": "新建项目",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "goal": {"type": "string"},
         "goal_criteria": {"type": "string", "description": "目标验收标准：怎样算达成"},
         "workdir": {"type": "string", "description": "项目工作目录（绝对路径），交付物相对它校验"},
         "deadline": {"type": "string", "description": "项目总时长/截止，必填，如 2026-10-30 18:00 或 2026-10-30"}},
     "required": ["name", "deadline", "goal", "goal_criteria"]}},
    {"name": "propose_plan", "description": "提交计划给用户审批：成员（中文岗位）+ 任务清单。用户在面板点批准后才落库执行；默认团队上限 5 人，超编要在审批里二次确认",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目名或 id"},
         "summary": {"type": "string", "description": "一句话说清这版计划要干什么"},
         "goal": {"type": "string", "description": "目标：这个项目最终要达成什么（必填，会显示在目标页顶部）"},
         "goal_criteria": {"type": "string", "description": "目标验收标准：怎样算达成（必填）"},
         "members": {"type": "array", "description": "成员列表，字段：name, role(中文岗位), kind(agent/human), model(留空=跟随会话), effort", "items": {"type": "object"}},
         "priority_ratio": {"type": "object", "description": "优先级配比（必填）：P1/P2/P3 各占多少百分比，三项和=100，例如 {\"p1\": 30, \"p2\": 40, \"p3\": 30}。P1 是「现在必须有一个人在推」的配额，不是「重要」的意思；全填 P1 会被看板按配额压回 P2/P3"},
         "tasks": {"type": "array", "description": "任务列表。每条必填：title, role, assignee(成员姓名), milestone(阶段), deliverable(产出路径), dod(验收标准), depends_on_title(前置任务标题，无则空字符串), estimate(人时，>0，缺了会被拒), priority(1/2/3，按 priority_ratio 分配)；可选 resource, evidence", "items": {"type": "object"}}},
       "required": ["project", "tasks", "goal", "goal_criteria", "members", "priority_ratio"]},
     "_meta": UI_META},
    {"name": "plan_status", "description": "查计划审批状态：pending 等用户点批准；approved 表示成员和任务已自动落库；rejected 带驳回原因",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}}},
     "_meta": UI_META},
    {"name": "add_task", "description": "在项目里新建任务并指派（临时补单用；正式计划请走 propose_plan 审批）",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "title": {"type": "string"},
         "role": {"type": "string"}, "assignee": {"type": "string", "description": "成员名或岗位名"},
         "status": {"type": "string", "enum": ["backlog", "doing", "review", "done", "blocked"]},
         "priority": {"type": "integer", "minimum": 1, "maximum": 3},
         "deliverable": {"type": "string"}, "dod": {"type": "string"},
         "estimate": {"type": "number", "description": "用时规划，人时，>0（必填）"},
         "milestone": {"type": "string", "description": "阶段"},
         "depends_on_title": {"type": "string", "description": "前置任务标题，无则空字符串"}},
     "required": ["title", "assignee", "estimate"]}},
    {"name": "add_member", "description": "加一个员工（AI 子代理或真人）。编制已成型时必须先 request_staff_change 拿用户批准，再把审批单 id 当 approval_id 传进来——没有审批单会被拒。模型必须由用户确认：留空＝跟随当前会话模型（默认、无额外消耗）；只有用户明确要求「给某人指定某模型」时才传 model/effort，禁止由负责人自行分配",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "name": {"type": "string", "description": "姓名，建议「岗位 · 名字」"},
         "role": {"type": "string"},
         "kind": {"type": "string", "enum": ["agent", "human"]},
         "model": {"type": "string", "description": "留空＝跟随当前会话模型（推荐）。仅在用户点名指定模型时填写，且必须是 /api/models 里真实存在的 id"},
         "effort": {"type": "string", "description": "留空＝模型默认档位。仅当用户明确要求时填写，且必须在该模型真实支持的档位内"},
         "model_confirmed": {"type": "boolean", "description": "只有用户亲口确认「给这个成员用指定模型」后才传 true；服务端会拒绝未经确认的 model"},
         "capacity": {"type": "integer", "minimum": 1},
         "approval_id": {"type": "string", "description": "已批准的编制审批单 id（staff_requests 里的 id）。编制成型后没有它加不了人"}}, "required": ["name"]}},
    {"name": "set_member_model", "description": "项目总监专用：调整某个员工使用的模型与思考档位。模型必须真实存在，档位必须是该模型支持的；留空 model 表示跟随当前会话模型。用户提出调整后再调用",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "member": {"type": "string", "description": "员工姓名、id 或岗位"},
         "model": {"type": "string", "description": "模型 id；留空=跟随当前会话模型"},
         "effort": {"type": "string", "description": "该模型真实支持的档位，留空=模型默认"}},
      "required": ["project", "member"]}},
    {"name": "list_models", "description": "列出 Codex 真实可用的模型、分组以及每个模型支持的思考档位（给用户选模型时用，不要虚构）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "update_task", "description": "改任务状态/进度/阻塞原因（子代理领活、交付、报阻塞都用它）",
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string", "description": "任务标题或 id"},
         "status": {"type": "string", "enum": ["backlog", "doing", "review", "done", "blocked"]},
         "progress": {"type": "integer", "minimum": 0, "maximum": 100},
         "blocked_reason": {"type": "string"},
         "milestone": {"type": "string", "description": "阶段名（目标拆解的一层）"},
         "deliverable": {"type": "string", "description": "产出路径，多个用 |，支持 a/{X,Y}.md；标 done 前必须真实存在"},
         "dod": {"type": "string", "description": "验收标准"},
         "resource": {"type": "string", "description": "独占资源，如 emulator-5556；同时被两个 doing 任务占用会报冲突"},
         "evidence": {"type": "string", "enum": ["observed", "inferred", "assumed"],
                      "description": "证据等级；inferred 不能 ≥90%"},
         "estimate": {"type": "number", "description": "工作量（人时）。排期和倒计时都靠它：estimate 就是这条任务的时间预算"},
         "paused": {"type": "boolean", "description": "true=暂停计时（等设备/等审批），false=恢复；暂停期间墙钟不算工时"},
         "evidence_path": {"type": "string", "description": "证据文件/目录，置 done 必填且必须真实存在"},
         "abandoned_reason": {"type": "string", "description": "放弃原因，status=abandoned 时必填"},
         "due_at": {"type": "number", "description": "硬截止（epoch 秒）。一般不用填，排期会自动算预计交付"}}, "required": ["task"]}},
    {"name": "claim_resource", "description": "给设备/独占资源上租约（例如 emulator-5556）。被占用返回 400+holder，过期自动可抢",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "resource": {"type": "string", "description": "资源名，如 emulator-5556"},
         "task": {"type": "string", "description": "占用它的任务标题或 id"},
         "member": {"type": "string", "description": "谁在用"},
         "minutes": {"type": "integer", "description": "租约分钟数，默认 60"},
         "force": {"type": "boolean"}}, "required": ["resource", "task"]}},
    {"name": "release_resource", "description": "释放设备租约（收工时一定要关，别让别人白等）",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "resource": {"type": "string"},
         "task": {"type": "string"}}, "required": ["resource"]}},
    {"name": "add_hypothesis", "description": "记录一条假设（含证伪命令与结论）。同一假设重复登记会自动提示「已证伪，别再跑」",
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string", "description": "任务标题或 id"},
         "claim": {"type": "string", "description": "假设原文"},
         "command": {"type": "string", "description": "验证/证伪用的命令"},
         "verdict": {"type": "string", "enum": ["open", "confirmed", "refuted"]},
         "note": {"type": "string"},
         "by": {"type": "string"}}, "required": ["task", "claim"]}},
    {"name": "set_criteria", "description": "把 goal_criteria 拆成可判定的验收条目（每条一句话，命令可选）",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "id": {"type": "string"}, "text": {"type": "string"},
             "cmd": {"type": "string"}, "sort_order": {"type": "number"}}}}}, "required": ["items"]}},
    {"name": "mark_criterion", "description": "记录一条验收条目的红绿（绿=已实测通过）",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "status": {"type": "string", "enum": ["green", "red", "unknown"]},
         "output": {"type": "string", "description": "实测输出摘要，一两行"}}, "required": ["id", "status"]}},
    {"name": "member_duty", "description": "登记/结束某个员工的在场租约。总监 spawn 子代理时开（默认 45 分钟），收工或 wait_agent 返回后关；开着就算在岗，不用手工刷心跳",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "member": {"type": "string", "description": "成员姓名、id 或岗位"},
         "on": {"type": "boolean", "description": "true=开始在场，false=收工"},
         "minutes": {"type": "integer", "description": "租约时长，默认 45 分钟，最长 720"}},
      "required": ["member"]}},
    {"name": "bind_member", "description": "让某个员工接管一个新会话（换会话不换人）。子代理被关掉、重开一个新的之后必须调这个：员工身份、工位号、历史任务与工时都不变，看板上的人数也不会变。禁止用 add_member 新建一个人",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "member": {"type": "string", "description": "员工姓名、工位号（如 3）或 id"},
         "thread_id": {"type": "string", "description": "新子代理的 threadId（UUID）"},
         "reason": {"type": "string", "description": "为什么换会话，如「上个会话结束，重开一个接着干」"}},
      "required": ["member", "thread_id"]}},
    {"name": "request_staff_change", "description": "申请加人 / 换人 / 撤编。编制是固定的：想在编制外动人，必须先提这张申请，用户在会话里批准后才生效。add=加人，replace=换人（工位保留、人换），remove=撤编",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "kind": {"type": "string", "enum": ["add", "replace", "remove"]},
         "member": {"type": "string", "description": "replace / remove 必填：换谁 / 撤谁（姓名、工位号或 id）"},
         "name": {"type": "string", "description": "add 必填：新员工姓名；replace 可选：接任者姓名"},
         "role": {"type": "string", "description": "中文岗位"},
         "model": {"type": "string", "description": "留空＝跟随当前会话模型"},
         "effort": {"type": "string"},
         "reason": {"type": "string", "description": "为什么现在必须动编制：人力不够 / 有人卡住 / 岗位不匹配"}},
      "required": ["kind", "reason"]},
     "_meta": UI_META},
    {"name": "staff_requests", "description": "查编制变更申请：哪些在等用户批、批了没、批了什么",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
     "_meta": UI_META},
    {"name": "approval_inbox", "description": "把项目里所有待审批项一次性摆到会话里：计划审批 / 强制复盘 / 编制变更。用户说「有什么要我批的」、或你不确定有没有漏批时调这个——卡片会直接在对话里弹出来",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目名或 id，留空取最近一个"}}},
     "_meta": UI_META},
    {"name": "decide", "description": "用户在会话里口头说了「批准 / 驳回」之后，由你把决定落库。id 从 approval_inbox 或对应的提交返回值里拿。计划超编时批准要带 confirm_over_cap=true",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["plan", "retro", "staff"]},
         "id": {"type": "string", "description": "审批单 id"},
         "decision": {"type": "string", "enum": ["approve", "reject"]},
         "note": {"type": "string", "description": "驳回原因（reject 时必填）"},
         "confirm_over_cap": {"type": "boolean", "description": "计划超过 5 人上限时，批准必须带 true"}},
      "required": ["kind", "id", "decision"]}},
    {"name": "goal_audit", "description": "目标合规体检：目标/验收标准是否有、每个任务是否挂在阶段上、产出与证据是否达标。分配任务前和交付前各跑一次",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目名或 id，留空取最近一个"}}}},
    {"name": "submit_retro", "description": "提交「强制复盘」。时间压力到 critical（剩余 ≤30%）/overdue 时看板会拦掉加人、新计划、导出报告，必须先交这一轮：六个问题逐条回答（各 ≥8 字），交完等用户批准才解锁",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "speed": {"type": "string", "description": "怎么提速：≥3 条具体动作 + 每条预计省多少分钟"},
         "deadline": {"type": "string", "description": "能否按期：能/不能 + 差多少小时 + 卡在哪条链"},
         "staffing": {"type": "string", "description": "是否加人：几个、什么岗位、接谁的哪条任务、省多少；不加也写理由"},
         "utilization": {"type": "string", "description": "谁在摸鱼、摸多久、怎么处置（看板已给实测数字）"},
         "scope": {"type": "string", "description": "砍哪条任务/哪个验收项；不砍写「不砍」+ 理由"},
         "risk": {"type": "string", "description": "最可能让目标失败的一件事 + 兜底方案"},
         "by": {"type": "string", "description": "谁提交的，默认项目总监"}},
      "required": ["speed", "deadline", "staffing", "utilization", "scope", "risk"]},
     "_meta": UI_META},
    {"name": "retro_status", "description": "查强制复盘状态：是否被闸住、第几轮、答了什么、用户批没批、利用率实测值",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
     "_meta": UI_META},
    {"name": "set_member_thread", "description": "把某个员工的 Codex 会话 id 登记到看板。spawn 子代理后必须立刻调用，否则面板点成员无法跳转到他的会话（这是硬要求，不是可选项）",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "member": {"type": "string", "description": "成员姓名或 id"},
         "thread_id": {"type": "string", "description": "子代理的 threadId（UUID）"}},
      "required": ["member", "thread_id"]}},
    {"name": "heartbeat", "description": "刷新员工心跳（在岗证明）。不传 members 时只刷有进行中任务的人；总监派活/轮询后调用，或让子代理开工时自己调",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"},
         "members": {"type": "array", "items": {"type": "string"},
                     "description": "成员 id 或姓名；不传＝只刷有 doing 任务的人"}}}},
]


def call(name, args):
    if name == "tb_api":
        m = (args.get("method") or "GET").upper()
        path = args.get("path") or "/"
        if not path.startswith("/api"):
            return "rejected", {"data": None, "error": "only /api paths"}
        try:
            out = http(m, path, args.get("body"))
        except Exception as e:  # noqa: BLE001
            return "error", {"data": None, "error": str(e)}
        return "ok", {"data": out}

    if name in ("board_overview", "show_team_panel", "teamboard_panel"):
        d = http("GET", "/api/overview")
        for p in d["projects"]:
            touch_lead(p["id"])
        lines = [f"共 {d['agg']['projects']} 个项目｜{d['agg']['tasks']} 个任务｜"
                 f"{d['agg']['members']} 人｜平均进度 {d['agg']['overall']}%｜阻塞 {d['agg']['blocked']}", ""]
        for p in d["projects"]:
            s = p["stats"]
            lines.append(f"- **{p['name']}** {s['overall']}%｜完成 {s['done']}/{s['total']}｜"
                         f"进行中 {s['doing']}｜阻塞 {s['blocked']}｜{len(p['roles'])} 岗位 / {s['members']} 人")
        # 新会话第一眼就要看到：有人断了、任务还挂着 doing。
        # 不提示的话，总监会默认「进行中＝有人在做」，然后空等。
        stuck = [p["name"] for p in d["projects"] if (p.get("resume") or {}).get("needs_reconcile")]
        if stuck:
            lines.append("")
            lines.append(f"⚠ {'、'.join(stuck)} 有 doing 任务的人已经不在岗。"
                         "先 resume_brief 处置，再派新活。")
        text = "\n".join(lines)
        first = d["projects"][0]["id"] if d["projects"] else ""
        return text, {"projects": d["projects"], "agg": d["agg"],
                      "board_url": (BASE + "/board?pid=" + first) if first else BASE + "/"}

    if name == "project_status":
        p = pick_project(args.get("project", ""))
        touch_lead(p["id"])
        text, st = summarize(p)
        return text, st

    if name == "resume_brief":
        p = pick_project(args.get("project", ""))
        r = http("GET", f"/api/projects/{p['id']}/resume-brief")
        if not isinstance(r, dict) or r.get("error"):
            return f"拿不到交班简报：{(r or {}).get('error', '未知错误')}", r
        lines = [f"# 交班简报 · {r.get('name') or p['name']}",
                 f"{r['verdict']}",
                 f"（最后一次有人在干活：{r.get('last_active_at') or '无记录'}）", ""]
        lead = [x for x in r["team"] if x.get("is_lead")]
        others = [x for x in r["team"] if not x.get("is_lead")]
        for x in lead + others:
            mark = {"working": "●", "blocked": "◐", "stale": "◌", "offline": "○"}.get(x["state"], "?")
            tail = ""
            if x["state"] == "working":
                tail = "在岗"
            elif x["state"] == "blocked":
                # 阻塞的人在不在岗不重要，重要的是他卡在哪——别拿「断了 0 秒」糊弄
                tail = "卡住"
            else:
                tail = (x.get("dead_why") or "不在岗") + (f" · 已断 {x['gone_human']}"
                                                          if x["gone_for"] >= 0 else "")
            lines.append(f"{mark} {x['seat']} {x['name']}（{x['role']}）— {tail}")
            for d in x.get("doing") or []:
                lines.append(f"    └ {d['title']} · {d['progress']}% · {d['idle_human']}没更新")
        if r.get("orphans"):
            lines += ["", f"## 烂尾任务 {len(r['orphans'])} 条（doing 但人不在）"]
            for o in r["orphans"]:
                lines.append(f"- {o['task_id']} {o['title']} — {o['assignee']} · {o['why']} · {o['idle_human']}没动")
        lines += ["", "## 先做这些"] + [f"{i}. {a}" for i, a in enumerate(r["actions"], 1)]
        lines += ["", "派新活之前先把上面处置完；否则你会对着一批已经停掉的子代理空等。"]
        return "\n".join(lines), r

    if name == "goal_audit":
        p = pick_project(args.get("project", ""))
        st = http("GET", "/api/projects/" + p["id"])
        v = st.get("goal_violations") or []
        sc = st.get("stage_coverage", st.get("goal_score", 0))
        att = st.get("goal_progress", 0)
        pos = st.get("project", {})
        hl = st.get("hours_left")
        rem = st.get("remaining_hours")
        cd = ("·".join(filter(None, [
            f"距截止 {hl}h" if hl is not None else "",
            f"剩余工时 {rem}h" if rem is not None else "",
            "已超支" if st.get("over_budget") else ""])) or "未设截止")
        lines = [f"# {pos.get('name','')} · 目标达成 {att}% · 阶段覆盖 {sc}%",
                 f"目标：{pos.get('goal') or '（缺）'}",
                 f"验收标准：{pos.get('goal_criteria') or '（缺）'}",
                 f"倒计时：{cd}"]
        if not v:
            lines.append("\n目标 → 阶段 → 任务 全部对齐，产出与验收齐全。")
        else:
            by = {}
            for x in v:
                by.setdefault(x["code"], []).append(x)
            label = {"no_goal": "缺目标", "no_criteria": "缺目标验收标准",
                     "no_milestone": "没挂阶段", "no_deliverable": "没写产出",
                     "no_dod": "没写验收", "missing_output": "交付物不存在",
                     "weak_evidence": "证据不足却标高进度"}
            lines.append(f"\n共 {len(v)} 项不达标：")
            for k, arr in by.items():
                names = "、".join(sorted({x.get("task") or "（项目级）" for x in arr})[:8])
                lines.append(f"- **{label.get(k, k)}** {len(arr)} 项：{names}")
            lines.append("\n修完再派活/交付：任务必须 milestone + deliverable + dod；"
                         "review/done 的交付物必须真实存在。")
        return "\n".join(lines), {"goal_progress": att, "stage_coverage": sc,
                                  "hours_left": hl, "remaining_hours": rem,
                                  "over_budget": st.get("over_budget", False),
                                  "violations": v}

    if name == "submit_retro":
        p = pick_project(args.get("project", ""))
        answers = {k: args.get(k, "") for k in
                   ("speed", "deadline", "staffing", "utilization", "scope", "risk")}
        r = http("POST", f"/api/projects/{p['id']}/retros",
                 {"answers": answers, "by": args.get("by") or "项目总监"})
        if r.get("error"):
            return "复盘没交上去：" + r["error"], r
        return (f"第 {r['round']} 轮复盘已提交（{p['name']}）。状态 submitted："
                f"审批卡已经推到会话里，用户点批准就解锁；批准前加人/新计划/导出报告仍然被拦。"), \
               {"retro": r, "project_id": p["id"],
                "pending": {"kind": "retro", "project_id": p["id"], "data": r}}

    if name == "retro_status":
        p = pick_project(args.get("project", ""))
        d = http("GET", f"/api/projects/{p['id']}/retros")
        rt, pr, ut = d["retro"], d["pressure"], d["utilization"]
        lines = [f"# {p['name']} · 时间压力 {pr.get('level')}（{pr.get('reason','')}）",
                 f"剩余 {pr.get('left_pct')}%｜排期偏差 {pr.get('slip_hours')}h｜"
                 f"团队利用率 {ut.get('team_pct')}%"]
        if rt.get("required"):
            lines += [f"⛔ 强制复盘第 {rt['round']} 轮未完成：{rt.get('reason')}",
                      "被拦：" + "、".join(rt.get("blocked_actions") or [])]
        latest = rt.get("latest")
        if latest:
            lines += ["", f"最新一轮：第 {latest['round']} 轮 · {latest['status']}"]
            qs = {q["key"]: q["q"] for q in (rt.get("questions") or [])}
            for k, v in (latest.get("answers") or {}).items():
                lines.append(f"- {k}：{v}")
        return "\n".join(lines), d

    if name == "set_member_thread":
        st = http("GET", "/api/overview")
        mid, pid = "", ""
        for pp in st["projects"]:
            ps = http("GET", "/api/projects/" + pp["id"])
            for m in ps["members"]:
                if args["member"] in (m["id"], m["name"]) or args["member"] in m["name"]:
                    mid, pid = m["id"], pp["id"]; break
            if mid: break
        if not mid:
            return "没找到这个成员：" + args["member"], {}
        r = http("PATCH", "/api/members/" + mid, {"thread_id": args["thread_id"]})
        if r.get("error"):
            return "登记失败：" + r["error"], r
        return (f"已登记 {r['name']} 的会话 {args['thread_id']}；"
                f"用户点这个员工就能直接打开他的会话。"), r

    if name == "heartbeat":
        p = pick_project(args.get("project", ""))
        who = args.get("members") or []
        r = http("POST", f"/api/projects/{p['id']}/heartbeat",
                 dict({"members": who} if who else {}, **_host()))
        n = len(r.get("touched") or [])
        tgt = "有进行中任务的人" if not who else "、".join(who)
        text = f"心跳已刷新：{n} 人（{tgt}）"
        if r.get("unknown"):
            text += f"\n未匹配到：{'、'.join(r['unknown'])}"
        return text, r

    if name == "open_board":
        proj = args.get("project", "")
        url = BASE + "/" + (f"board?pid={pick_project(proj)['id']}" if proj else "")
        return f"看板地址：{url}", {"url": url}

    if name == "create_project":
        r = http("POST", "/api/projects",
                 {k: args.get(k, "") for k in ("name", "goal", "goal_criteria", "workdir", "deadline")})
        return f"已创建项目：{r['project']['name']}（{r['project']['id']}）", r

    if name == "propose_plan":
        p = pick_project(args.get("project", ""))
        if not (args.get("goal") or "").strip():
            return ("计划缺目标：propose_plan 的 goal 必填（这个项目最终要达成什么），"
                    "goal_criteria 也要填。服务端会拒绝不合格的计划。"), {"error": "missing goal"}
        ratio = args.get("priority_ratio")
        if ratio in (None, "", {}, []):
            return ("计划缺 priority_ratio（优先级配比）：P1/P2/P3 各占多少百分比，三项和=100。"
                    '例如 {"p1": 30, "p2": 40, "p3": 30}。'
                    "P1 是配额不是形容词——全填 P1 等于没优先级，服务端会按配比把多出来的压到 P2/P3。"), \
                   {"error": "missing priority_ratio"}
        r = http("POST", f"/api/projects/{p['id']}/approvals", {
            "summary": args.get("summary", ""),
            "goal": args.get("goal", ""),
            "goal_criteria": args.get("goal_criteria", ""),
            "priority_ratio": ratio,
            "members": args.get("members", []),
            "tasks": args.get("tasks", [])})
        n_m, n_t = len(r["members"]), len(r["tasks"])
        head = f"已提交计划审批：{n_m} 人 / {n_t} 个任务"
        if r["over_cap"]:
            head += f"（超过默认 {r['team_cap']} 人上限，批准时需二次确认扩编）"
        url = f"{BASE}/board?pid={p['id']}&approval={r['id']}"
        return (f"{head}\n审批面板：{url}\n用户批准后，成员与任务会自动落到看板，"
                f"再用 plan_status 确认状态。"), \
               {"approval": r, "url": url, "project_id": p["id"],
                "pending": {"kind": "plan", "project_id": p["id"], "data": r}}

    if name == "plan_status":
        p = pick_project(args.get("project", ""))
        rows = http("GET", f"/api/projects/{p['id']}/approvals")
        if not rows:
            return "这个项目还没有提交过计划审批。", {"approvals": []}
        a = rows[0]
        label = {"pending": "等待用户审批", "approved": "已批准（成员/任务已落库）",
                 "rejected": "已驳回"}.get(a["status"], a["status"])
        text = f"最新计划：{a.get('summary') or '—'}｜状态：{label}"
        if a.get("note"):
            text += f"｜备注：{a['note']}"
        return text, {"approval": a, "approvals": rows}

    if name == "add_task":
        p = pick_project(args.get("project", ""))
        st = http("GET", "/api/projects/" + p["id"])
        body = {"title": args["title"], "role": args.get("role", ""),
                "status": args.get("status", "backlog"), "priority": int(args.get("priority", 2)),
                "deliverable": args.get("deliverable", ""), "dod": args.get("dod", ""),
                "estimate": args.get("estimate"), "milestone": args.get("milestone", ""),
                "resource": args.get("resource", ""), "evidence": args.get("evidence", "")}
        if body["estimate"] is None:
            body.pop("estimate")
        who = args.get("assignee", "")
        if who:
            for m in st["members"]:
                if who in (m["id"], m["name"]) or who == m["role"]:
                    body["assignee_id"] = m["id"]
                    break
        dep = (args.get("depends_on_title") or "").strip()
        if dep:
            hit = next((t for t in st["tasks"] if t["title"] == dep), None)
            if not hit:
                raise RuntimeError(f"找不到前置任务「{dep}」；先建它，或把 depends_on_title 留空")
            body["depends_on"] = hit["id"]
        t = http("POST", f"/api/projects/{p['id']}/tasks", body)
        if isinstance(t, dict) and t.get("error"):
            raise RuntimeError(t["error"])
        return f"已建任务：{t['title']}（{t['id']}）", t

    if name == "add_member":
        p = pick_project(args.get("project", ""))
        m = http("POST", f"/api/projects/{p['id']}/members", {
            "name": args["name"], "role": args.get("role", ""),
            "kind": args.get("kind", "agent"), "model": args.get("model", ""),
            "effort": args.get("effort", ""),
            "model_confirmed": bool(args.get("model_confirmed", False)),
            "approval_id": args.get("approval_id", ""),
            "capacity": int(args.get("capacity", 3))})
        if m.get("model"):
            model = f"，指定模型 {m['model']}" + (f"／{m['effort']}" if m.get("effort") else "")
        else:
            model = "，模型跟随当前会话（实际继承父代理，无额外模型开销）"
        return f"已加入：{m['name']}（{m['role'] or '未填岗位'}{model}）", m

    if name == "list_models":
        rows = http("GET", "/api/models")
        by = {}
        for m in rows:
            by.setdefault(m["group"], []).append(m)
        lines = [f"共 {len(rows)} 个真实模型"]
        for g, items in by.items():
            lines.append(f"## {g}")
            for m in items:
                eff = " / ".join(e["effort"] for e in m.get("efforts") or []) or "无档位"
                lines.append(f"- {m['id']}（{m['name']}）档位：{eff}；默认 {m.get('default_effort') or '—'}")
        return "\n".join(lines), {"models": rows}

    if name == "claim_resource":
        p = pick_project(args.get("project", ""))
        st = http("GET", "/api/projects/" + p["id"])
        tid = next((t["id"] for t in st["tasks"]
                    if args["task"] in (t["id"], t["title"]) or args["task"] in t["title"]), "")
        body = {"task_id": tid, "minutes": args.get("minutes", 60),
                "force": bool(args.get("force")), "member": args.get("member", "")}
        r = http("POST", f"/api/projects/{p['id']}/resources/{args['resource']}/claim", body)
        if isinstance(r, dict) and r.get("error"):
            return f"没拿到：{r['error']}", r
        return (f"拿到 {args['resource']} · 剩 {r['left_sec']//60} 分钟"), r

    if name == "release_resource":
        p = pick_project(args.get("project", ""))
        st = http("GET", "/api/projects/" + p["id"])
        tid = next((t["id"] for t in st["tasks"]
                    if args.get("task") and (args["task"] in (t["id"], t["title"]) or args["task"] in t["title"])), "")
        r = http("POST", f"/api/projects/{p['id']}/resources/{args['resource']}/release", {"task_id": tid})
        return ("已释放 " if r.get("released") else "本来就没占用 ") + args["resource"], r

    if name == "add_hypothesis":
        d = http("GET", "/api/overview")
        tid = ""
        for pp in d["projects"]:
            st = http("GET", "/api/projects/" + pp["id"])
            for t in st["tasks"]:
                if args["task"] in (t["id"], t["title"]) or args["task"] in t["title"]:
                    tid = t["id"]; break
            if tid: break
        if not tid:
            raise RuntimeError(f"找不到任务：{args['task']}")
        r = http("POST", f"/api/tasks/{tid}/hypotheses",
                 {k: args[k] for k in ("claim", "command", "verdict", "note", "by") if k in args})
        if r.get("warning"):
            return "⚠ " + r["warning"], r
        return f"记下：{args['claim'][:40]}（{r.get('verdict')}）", r

    if name == "set_criteria":
        p = pick_project(args.get("project", ""))
        r = http("POST", f"/api/projects/{p['id']}/criteria", {"items": args["items"]})
        return f"登记 {len(r.get('ids', []))} 条验收", r

    if name == "mark_criterion":
        r = http("PATCH", "/api/criteria/" + args["id"],
                 {"status": args["status"], "output": args.get("output", "")})
        return f"{args['id']} → {args['status']}", r

    if name == "approval_inbox":
        p = pick_project(args.get("project", ""))
        st = http("GET", f"/api/projects/{p['id']}")
        pend = [{"kind": "plan", "data": a} for a in (st.get("pending_approvals") or [])]
        latest = ((st.get("retro") or {}).get("latest") or {})
        if latest.get("status") == "submitted":
            pend.append({"kind": "retro", "data": latest})
        pend += [{"kind": "staff", "data": s} for s in (st.get("staff_pending") or [])]
        if not pend:
            return f"{p['name']}：没有待审批项。", {"pending": [], "project_id": p["id"]}
        label = {"plan": "计划审批", "retro": "强制复盘", "staff": "编制变更"}
        lines = [f"{p['name']} 有 {len(pend)} 项等你批："]
        for i, x in enumerate(pend, 1):
            d = x["data"]
            head = d.get("summary") or d.get("reason") or ""
            lines.append(f"{i}. {label.get(x['kind'], x['kind'])}｜{head}｜id={d.get('id','—')}")
        return "\n".join(lines), {"pending": pend, "project_id": p["id"]}

    if name == "decide":
        kind, rid, d = args.get("kind"), args.get("id", ""), args.get("decision")
        if d not in ("approve", "reject"):
            return "decision 只能是 approve / reject", {"error": "bad decision"}
        path = {"plan": f"/api/approvals/{rid}/decide",
                "retro": f"/api/retros/{rid}/decide",
                "staff": f"/api/staff-requests/{rid}/decide"}.get(kind)
        if not path:
            return "kind 只能是 plan / retro / staff", {"error": "bad kind"}
        body = {"decision": d, "note": args.get("note", "")}
        if kind == "plan" and args.get("confirm_over_cap"):
            body["confirm_over_cap"] = True
        r = http("POST", path, body)
        if isinstance(r, dict) and r.get("error"):
            return "没落库：" + r["error"], r
        return (f"{kind} {rid} 已{'批准' if d == 'approve' else '驳回'}。"), \
               {"decided": r, "kind": kind, "decision": d}

    if name == "bind_member":
        p = pick_project(args.get("project", ""))
        st = http("GET", f"/api/projects/{p['id']}")
        ref = str(args.get("member", "")).strip()
        hit = None
        for m in st["members"]:
            if ref in (m["id"], m["name"], str(m.get("seat_no", ""))):
                hit = m
                break
        if not hit:
            return f"编制里没有「{ref}」。先看 project_status 确认工位号和姓名。", {"error": "member not found"}
        r = http("POST", f"/api/projects/{p['id']}/members/{hit['id']}/bind",
                 dict({"thread_id": args["thread_id"], "reason": args.get("reason", "")}, **_host()))
        if r.get("error"):
            return "接管失败：" + r["error"], r
        return (f"{hit['seat_label']} {hit['name']} 已接管新会话 {args['thread_id'][:8]}…"
                f"（第 {r.get('bind_count')} 次，工位不变）。"
                f"现在可以给他派活了；不要再调 add_member。"), {"member": r, "project_id": p["id"]}

    if name == "request_staff_change":
        p = pick_project(args.get("project", ""))
        body = {k: args.get(k) for k in ("kind", "member", "name", "role", "model", "effort", "reason")
                if args.get(k)}
        r = http("POST", f"/api/projects/{p['id']}/staff-requests", body)
        if r.get("error"):
            return "申请没提上去：" + r["error"], r
        return (f"{r['summary']}\n审批卡已推到会话里，等用户点批准；批准前编制不变。"), \
               {"staff_request": r, "pending": {"kind": "staff", "project_id": p["id"], "data": r},
                "project_id": p["id"]}

    if name == "staff_requests":
        p = pick_project(args.get("project", ""))
        rows = http("GET", f"/api/projects/{p['id']}/staff-requests")["staff_requests"]
        if not rows:
            return "这个项目还没有编制变更申请。", {"staff_requests": [], "project_id": p["id"]}
        label = {"pending": "等用户批", "approved": "已批准", "rejected": "已驳回"}
        kind = {"add": "加人", "replace": "换人", "remove": "撤编"}
        lines = [f"{kind.get(x['kind'], x['kind'])}｜{label.get(x['status'], x['status'])}｜"
                 f"{x.get('member_name') or ''}｜{x.get('reason') or ''}" for x in rows]
        return "\n".join(lines), {"staff_requests": rows, "project_id": p["id"]}

    if name == "member_duty":
        p = pick_project(args.get("project", ""))
        st = http("GET", "/api/projects/" + p["id"])
        who = args["member"]
        target = next((m for m in st["members"]
                       if who in (m["id"], m["name"], m["role"]) or who in m["name"]), None)
        if not target:
            raise RuntimeError(f"找不到成员：{who}")
        on = bool(args.get("on", True))
        r = http("POST", f"/api/members/{target['id']}/duty",
                 {"on": on, "minutes": int(args.get("minutes", 45))})
        if on:
            return (f"{target['name']} 进入在场（{r['duty_left']//60} 分钟内不算离线；"
                    f"收工记得 member_duty(on=false)）"), r
        return f"{target['name']} 已收工（不抹掉已有的计时）", r

    if name == "set_member_model":
        p = pick_project(args.get("project", ""))
        st = http("GET", "/api/projects/" + p["id"])
        who = args["member"]
        target = next((m for m in st["members"]
                       if who in (m["id"], m["name"], m["role"]) or who in m["name"]), None)
        if not target:
            raise RuntimeError(f"找不到成员：{who}")
        body = {"model": args.get("model", ""), "effort": args.get("effort", ""),
                "model_confirmed": True}
        r = http("PATCH", "/api/members/" + target["id"], body)
        if isinstance(r, dict) and r.get("error"):
            raise RuntimeError(r["error"])
        if r.get("model"):
            desc = f"指定模型 {r['model']}" + (f"／{r['effort']}" if r.get("effort") else "")
        else:
            desc = "跟随当前会话模型"
        return f"已调整：{r['name']} → {desc}", r

    if name == "update_task":
        d = http("GET", "/api/overview")
        target = None
        for p in d["projects"]:
            st = http("GET", "/api/projects/" + p["id"])
            for t in st["tasks"]:
                if args["task"] in (t["id"], t["title"]) or args["task"] in t["title"]:
                    target = t
                    break
            if target:
                break
        if not target:
            raise RuntimeError(f"找不到任务：{args['task']}")
        body = {k: args[k] for k in ("status", "progress", "blocked_reason", "milestone",
                                     "deliverable", "dod", "resource", "evidence", "estimate",
                                     "paused", "due_at", "evidence_path", "abandoned_reason")
                if k in args}
        t = http("PATCH", "/api/tasks/" + target["id"], body)
        if isinstance(t, dict) and t.get("error"):
            return f"没改成：{t['error']}", t
        extra = []
        if t.get("evidence"):
            extra.append(f"证据 {t['evidence']}")
        if t.get("deliverable_ok") is False:
            extra.append("交付物缺失 ✘")
        if t.get("resource_conflict"):
            extra.append("资源冲突 ⚠")
        return (f"{t['title']} → {t['status']} {t['progress']}%"
                + ("　" + " · ".join(extra) if extra else "")), t

    raise RuntimeError(f"未知工具：{name}")


def reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def error(rid, code, msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": code, "message": msg}}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _safe_stdio():
    """Windows 控制台默认 cp1252：中文/箭头日志会让 MCP 进程在握手前死掉。"""
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _safe_stdio()
    log(f"[teamboard-mcp] start, board={BASE}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            icon = ""
            try:
                import base64 as _b64
                with open(os.path.join(HERE, "assets", "logo.svg"), "rb") as f:
                    icon = "data:image/svg+xml;base64," + _b64.b64encode(f.read()).decode()
            except OSError:
                pass
            info = {"name": NAME, "version": VERSION}
            if icon:
                info["icons"] = [{"src": icon, "mimeType": "image/svg+xml", "sizes": ["any"]}]
            reply(rid, {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                        "capabilities": {"tools": {"listChanged": False},
                                         "resources": {"listChanged": False, "subscribe": False}},
                        "serverInfo": info})
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            reply(rid, {})
        elif method == "resources/list":
            reply(rid, {"resources": [{"uri": UI_URI, "name": "TeamBoard 面板",
                                       "description": "团队 / 员工 / 任务 / 进度",
                                       "mimeType": UI_MIME,
                                       "_meta": {"openai/widgetCSP": {
                                           "frame_domains": [BASE],
                                           "connect_domains": [BASE],
                                           "base_uri_domains": [BASE]}}}]})
        elif method == "resources/templates/list":
            reply(rid, {"resourceTemplates": []})
        elif method == "resources/read":
            uri = (msg.get("params") or {}).get("uri", "")
            if uri != UI_URI:
                error(rid, -32602, f"unknown resource: {uri}")
                continue
            try:
                html = open(UI_FILE, encoding="utf-8").read()
            except OSError as e:
                error(rid, -32603, f"cannot read ui: {e}")
                continue
            reply(rid, {"contents": [{
                "uri": UI_URI, "mimeType": UI_MIME, "text": html,
                # MCP Apps: 允许面板内嵌本地看板 iframe，并给出高度提示，
                # 否则宿主按 0 高度渲染（白屏）。
                "_meta": {
                    "openai/widgetHeightHint": 820,
                    "openai/widgetMinFrameHeight": 420,
                    "openai/widgetCSP": {
                        "frame_domains": [BASE],
                        "connect_domains": [BASE],
                        "base_uri_domains": [BASE],
                    },
                    "openai/ui": {"preferredDisplayMode": "inline"},
                },
            }]})
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            try:
                text, struct = call(params.get("name"), params.get("arguments") or {})
                reply(rid, {"content": [{"type": "text", "text": text}],
                            "structuredContent": struct, "isError": False})
            except Exception as e:  # noqa: BLE001
                reply(rid, {"content": [{"type": "text", "text": f"出错：{e}"}], "isError": True})
        elif rid is not None:
            error(rid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()
