#!/usr/bin/env python3
"""Board API smoke test: boots server.py on a throwaway port + DB, no side effects."""
import json, os, subprocess, sys, pathlib, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _util import PLUGIN, PYTHON, free_port, temp_home, env_for, call, wait_health, kill_board

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    home, port = temp_home(), free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([PYTHON, str(PLUGIN / "server.py"), "--port", str(port)],
                            env=env_for(home, port), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        try:
            wait_health(base)
        except Exception:
            # 起不来时把服务端的原始输出打出来：CI 上排查只能靠这一条
            try:
                proc.terminate()
                out = proc.communicate(timeout=5)[0] or b""
            except Exception:
                out = b""
            print("--- server output (tail) ---")
            print(out.decode("utf-8", "replace")[-3000:])
            print(f"--- exit code: {proc.returncode} ---")
            raise
        version = (PLUGIN / "VERSION").read_text().strip()
        code, health = call(base, "GET", "/api/health")
        check("health 200", code == 200, code)
        check("health reports app=teamboard", health.get("app") == "teamboard", health)
        check("version matches VERSION file", health.get("version") == version, health.get("version"))

        code, body = call(base, "POST", "/api/projects", {"name": "no deadline"})
        check("project without deadline is rejected", code >= 400 and "deadline" in str(body), body)

        code, proj = call(base, "POST", "/api/projects",
                          {"name": "t", "deadline": "2030-01-01 18:00",
                           "goal": "ship", "goal_criteria": "green tests"})
        check("project created", code == 201 and proj.get("project", {}).get("id"), code)
        pid = proj["project"]["id"]

        code, models = call(base, "GET", "/api/models")
        check("model catalog is pluggable (TEAMBOARD_CATALOG)", any(m["id"] == "gpt-test-large" for m in models), models)

        code, mem = call(base, "POST", f"/api/projects/{pid}/members",
                         {"name": "总监理", "role": "项目总监", "is_lead": True})
        check("lead member added", code in (200, 201) and mem.get("id"), mem)

        # 编制已成型（总监在岗）后，直接加人必须被挡下——否则人数会一直漂
        code, denied = call(base, "POST", f"/api/projects/{pid}/members",
                            {"name": "后端", "role": "后端工程", "model": "gpt-test-large",
                             "effort": "high", "model_confirmed": True})
        check("direct hire after roster formed needs approval",
              code >= 400 and denied.get("staff_request_required"), denied)

        code, req = call(base, "POST", f"/api/projects/{pid}/staff-requests",
                         {"kind": "add", "name": "后端", "role": "后端工程",
                          "model": "gpt-test-large", "effort": "high",
                          "reason": "测试：走审批加人"})
        check("staff request created", code in (200, 201) and req.get("id"), req)

        code, decided = call(base, "POST",
                             f"/api/staff-requests/{req['id']}/decide",
                             {"decision": "approve"})
        mem2 = (decided or {}).get("member") or {}
        check("approved hire lands with confirmed model+effort",
              code in (200, 201) and mem2.get("id") and mem2.get("model") == "gpt-test-large",
              decided)
        # 决定之后必须回执，否则后面的状态迁移全被 ack_gate 拦（这正是要的行为）
        call(base, "POST", f"/api/projects/{pid}/ack",
             {"kind": "staff", "id": req["id"], "note": "收到，继续"})

        code, mem3 = call(base, "POST", f"/api/projects/{pid}/members",
                          {"name": "未确认", "role": "测试", "model": "gpt-test-large", "effort": "high"})
        check("model needs explicit user confirmation", code >= 400 and "确认" in json.dumps(mem3, ensure_ascii=False), mem3)

        code, bad = call(base, "POST", f"/api/projects/{pid}/tasks",
                         {"title": "裸任务", "assignee": mem2["id"], "estimate": 3})
        check("goal-bound task without milestone/deliverable/dod is rejected",
              code >= 400 and "milestone" in json.dumps(bad, ensure_ascii=False), bad)

        doc = home / "api.md"
        doc.write_text("# api\n", encoding="utf-8")
        code, task = call(base, "POST", f"/api/projects/{pid}/tasks",
                          {"title": "做接口", "assignee": mem2["id"], "estimate": 3,
                           "milestone": "M1", "deliverable": str(doc), "dod": "200 on /x"})
        check("task created", code in (200, 201) and task.get("id"), task)

        code, state = call(base, "GET", f"/api/projects/{pid}")
        check("project state has stats/tasks/members",
              all(k in state for k in ("stats", "tasks", "members")), list(state)[:8])
        check("progress is numeric", isinstance(state["stats"].get("overall"), (int, float)), state["stats"])

        # 「修好了」不值钱：done 必须带一条能重跑的命令，不然就是无证据完成
        # 先真开工一次：没有执行记录的任务不许直接标完成（否则「写完了」＝「做完了」）
        call(base, "PATCH", f"/api/tasks/{task['id']}", {"status": "doing"})
        code, noev = call(base, "PATCH", f"/api/tasks/{task['id']}",
                          {"status": "done", "progress": 100, "evidence": "observed",
                           "evidence_path": str(doc)})
        check("done without verify_cmd is rejected",
              code >= 400 and noev.get("field") == "verify_cmd", noev)
        code, okv = call(base, "PATCH", f"/api/tasks/{task['id']}",
                         {"status": "done", "progress": 100, "evidence": "observed",
                          "evidence_path": str(doc), "verify_cmd": "curl -s localhost/x",
                          "verify_out": "200\nOK"})
        check("done with verify_cmd lands and stamps verified_at",
              code == 200 and okv.get("verify_cmd") and okv.get("verified_at"), okv)
        code, st2 = call(base, "GET", f"/api/projects/{pid}")
        check("a finished task with a command is not counted as evidence-less",
              okv["id"] not in [t["id"] for t in st2["tasks"] if not t.get("verify_cmd")],
              st2.get("done_without_evidence"))
        # 回滚成 backlog，后面的测试还要用它。回退进度＝重做，按规矩写理由（并记事件）
        call(base, "PATCH", f"/api/tasks/{task['id']}",
             {"status": "backlog", "progress": 0, "confirm_restart": True,
              "restart_reason": "测试用例回滚，供后续断言复用"})

        # 编制冻结：批准单只能用一次、人名/岗位不能直接改
        code, reuse = call(base, "POST", f"/api/projects/{pid}/members",
                           {"name": "蹭审批", "role": "测试", "approval_id": req["id"]})
        check("a staff approval can only be consumed once",
              code >= 400 and "用过" in json.dumps(reuse, ensure_ascii=False), reuse)

        code, renamed = call(base, "PATCH", f"/api/members/{mem2['id']}", {"name": "偷偷改名"})
        check("renaming a seat member needs approval",
              code >= 400 and renamed.get("staff_request_required"), renamed)

        code, renamed_role = call(base, "PATCH", f"/api/members/{mem2['id']}", {"role": "换个岗位"})
        check("changing a seat role needs approval",
              code >= 400 and renamed_role.get("staff_request_required"), renamed_role)

        # 交班简报：宿主进程不存在时必须当场判死，不能等心跳过期
        call(base, "PATCH", f"/api/tasks/{task['id']}",
             {"status": "doing", "progress": 10, "assignee_id": mem2["id"]})
        call(base, "POST", f"/api/members/{mem2['id']}/heartbeat", {"pid": 999999})
        code, brief = call(base, "GET", f"/api/projects/{pid}/resume-brief")
        check("resume brief answers who is gone",
              code == 200 and brief.get("total") and brief.get("verdict"), brief)
        dead = [x for x in brief.get("team", []) if x["id"] == mem2["id"]]
        check("dead host process marks the member stale at once",
              dead and dead[0]["state"] in ("stale", "offline")
              and dead[0]["dead_reason"] == "process", dead)
        check("orphan doing task is surfaced for handover",
              any(o["task_id"] == task["id"] for o in brief.get("orphans", [])), brief.get("orphans"))

        # 阻塞也要有人守着：任务标 blocked，但执行体早断了，就不是「卡住」而是「僵住」
        code, blocked = call(base, "PATCH", f"/api/tasks/{task['id']}",
                             {"status": "blocked", "blocked_reason": "等用户确认"})
        code, state = call(base, "GET", f"/api/projects/{pid}")
        dead = [x for x in state["members"] if x["id"] == mem2["id"]][0]
        check("a blocked seat whose host died reads as stale, not blocked",
              dead["state"] == "stale" and dead["dead_reason"] == "process", dead["state"])
        check("the blocked reason survives the downgrade",
              "等用户确认" in (dead.get("stuck") or ""), dead.get("stuck"))

        # 会话还在写 = 人在干活，两件事都靠这一条：
        #   ① blocked 是任务状态，不是人的状态（实测每几秒写一次会话却被画成「卡住·摸鱼中」）
        #   ② Codex 重启后库里还留着上一条死 pid，但会话已经由新进程接着写——
        #      pid 是记录过期，不是人没了。所以这里故意不刷新 pid，保持 999999 那条死记录。
        from datetime import datetime
        d = datetime.now()
        day = home / "sessions" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}"
        day.mkdir(parents=True, exist_ok=True)
        tid = "01a2b3c4-1111-7222-8333-444455556666"
        sess_file = day / f"rollout-2026-01-01T00-00-00-{tid}.jsonl"
        sess_file.write_text(
            json.dumps({"type": "session_meta",
                        "payload": {"id": tid, "parent_thread_id": ""}}) + "\n",
            encoding="utf-8")
        call(base, "POST", f"/api/projects/{pid}/members/{mem2['id']}/bind",
             {"thread_id": tid})
        # 建会话 ≠ 开工：重启 Codex 会在会话里补一笔 session_meta，mtime 是「刚刚」，
        # 但这一秒没有任何人在跑。只认工具/思考类事件（BUSY_TAIL）。
        code, state = call(base, "GET", f"/api/projects/{pid}")
        fresh = [x for x in state["members"] if x["id"] == mem2["id"]][0]
        check("a freshly created session is not 'working' yet",
              fresh["state"] != "working" and fresh.get("session_tail") == "session_meta",
              (fresh["state"], fresh.get("session_tail")))
        # 用户发完消息、agent 还没动手：也是「等指令」，不是「在干活」
        with sess_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg",
                                "payload": {"type": "user_message"}}) + "\n")
        os.utime(sess_file, None)
        code, state = call(base, "GET", f"/api/projects/{pid}")
        idle = [x for x in state["members"] if x["id"] == mem2["id"]][0]
        check("a user message with no run yet is not 'working'",
              idle["state"] != "working" and idle.get("session_awaiting") is True,
              (idle["state"], idle.get("session_awaiting")))
        # agent 真跑起来：尾部出现工具调用，才算在岗
        with sess_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "response_item",
                                "payload": {"type": "function_call_output"}}) + "\n")
        os.utime(sess_file, None)
        code, state = call(base, "GET", f"/api/projects/{pid}")
        live = [x for x in state["members"] if x["id"] == mem2["id"]][0]
        check("a blocked task with a live session reads as working",
              live["state"] == "working" and live["state_source"] == "session+blocked",
              (live["state"], live.get("state_source")))
        check("the blocked title rides along on the member row",
              live.get("blocked_count") == 1 and live.get("blocked_title"),
              (live.get("blocked_count"), live.get("blocked_title")))
        check("a resumed session outranks a stale host pid",
              live.get("dead_reason") != "process", live.get("dead_reason"))

        # 收工：Codex 退出/收尾时会补一笔 turn_aborted / task_complete。
        # 那笔会把 mtime 刷成「刚刚」，但人已经不在跑了——只认事件，不认 mtime。
        with sess_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg",
                                "payload": {"type": "turn_aborted"}}) + "\n")
        os.utime(sess_file, None)          # 退出补的这笔同样会刷新 mtime
        code, state = call(base, "GET", f"/api/projects/{pid}")
        stopped = [x for x in state["members"] if x["id"] == mem2["id"]][0]
        check("a terminal event stops the seat even with a fresh mtime",
              stopped["state"] != "working"
              and stopped.get("session_ended") == "turn_aborted",
              (stopped["state"], stopped.get("session_ended")))

        # 重新开工：再来一笔事件就得立刻回到工作中（不等人手动改状态）
        with sess_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg",
                                "payload": {"type": "token_count"}}) + "\n")
        code, state = call(base, "GET", f"/api/projects/{pid}")
        restarted = [x for x in state["members"] if x["id"] == mem2["id"]][0]
        check("a new event starts the seat again",
              restarted["state"] == "working" and not restarted.get("session_ended"),
              (restarted["state"], restarted.get("session_ended")))

        # 静默 90 秒就不算在跑：900s 的宽窗口会让停掉的子代理继续亮 15 分钟「工作中」
        old = time.time() - 130
        os.utime(sess_file, (old, old))
        time.sleep(2.2)          # 会话索引 2 秒缓存，等它过期再问
        code, state = call(base, "GET", f"/api/projects/{pid}")
        sleepy = [x for x in state["members"] if x["id"] == mem2["id"]][0]
        check("a session silent for >90s is not 'working'",
              sleepy["state"] != "working" and sleepy.get("session_silent") >= 90,
              (sleepy["state"], sleepy.get("session_silent")))
        os.utime(sess_file, None)

        # 会话自报身份：agent_path 是「这一轮谁在干」，比手写工位名可靠
        meta_line = json.dumps({"type": "session_meta", "payload": {
            "id": tid, "source": {"subagent": {"thread_spawn": {
                "depth": 1, "agent_path": "/root/srv_parity2",
                "agent_nickname": "Franklin"}}}}})
        body = sess_file.read_text(encoding="utf-8").splitlines()
        body.insert(0, meta_line)
        sess_file.write_text("\n".join(body) + "\n", encoding="utf-8")
        code, state = call(base, "GET", f"/api/projects/{pid}")
        ident = [x for x in state["members"] if x["id"] == mem2["id"]][0]
        check("agent_path is parsed from the session header",
              ident.get("agent_leaf") == "srv_parity2"
              and ident.get("agent_nickname") == "Franklin",
              (ident.get("agent_leaf"), ident.get("agent_nickname")))

        # health：三处判断读同一份数字
        code, hl = call(base, "GET", f"/api/projects/{pid}/health")
        check("health endpoint answers what does not line up",
              code == 200 and "summary" in hl and "counts" in hl, hl.get("counts"))

        # 全员停摆：一个干活的都没有，但任务还挂着 doing/blocked。
        # 这是这个项目最真实的故障——总监埋头干自己的，员工全停了它不知道。
        old = time.time() - 1000
        os.utime(sess_file, (old, old))
        time.sleep(2.2)          # 会话索引 2 秒缓存
        code, state = call(base, "GET", f"/api/projects/{pid}")
        i = state.get("idle") or {}
        check("all-idle team is flagged instead of looking fine",
              i.get("alert") is True and i.get("working") == 0
              and i.get("workers", 0) >= 1, i)
        code, denied = call(base, "POST", f"/api/projects/{pid}/approvals",
                            {"goal": "g", "goal_criteria": "c", "priority_ratio": "1:2:1",
                             "members": [], "tasks": []})
        check("a new plan is refused while everyone is idle",
              code in (400, 403) and denied.get("need_idle_fix"), denied)
        # 解闸动作必须放行，否则唯一出口也被锁上 → 死锁
        code, freed = call(base, "PATCH", f"/api/tasks/{task['id']}",
                           {"progress": 40})
        check("the gate leaves a way out (task updates still work)", code == 200, freed)
        # 模拟「Codex 刚退出」：补一笔收尾事件，mtime 是刚刚，但没人在跑。
        # 这正是实测里看板报「全部对得上」的那个窗口。
        with sess_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg",
                                "payload": {"type": "turn_aborted"}}) + "\n")
        os.utime(sess_file, None)
        time.sleep(2.2)
        code, state = call(base, "GET", f"/api/projects/{pid}")
        i = state.get("idle") or {}
        # 刚断（App 重启补的收尾事件把 mtime 刷成「刚刚」）也要立刻亮：
        # 只看 15 分钟阈值会在这段窗口里报「全部对得上」，实测踩过。
        check("just-dropped team warns at once but does not gate yet",
              i.get("warn") is True and i.get("alert") is False, i)

        # 自驱心跳：用户不说话时，谁来把这一轮重新拉起来
        code, lead = call(base, "POST", f"/api/projects/{pid}/members",
                          {"name": "总监乙", "role": "项目总监", "is_lead": True})
        code, state = call(base, "GET", f"/api/projects/{pid}")
        lead = [x for x in state["members"] if x.get("is_lead")][0]
        call(base, "POST", f"/api/projects/{pid}/members/{lead['id']}/bind",
             {"thread_id": "01a9f9f9-2222-7333-8444-555566667777"})
        code, ap = call(base, "POST", f"/api/projects/{pid}/autopilot", {"action": "enable"})
        check("autopilot writes a heartbeat that targets the lead thread",
              code == 200 and (ap.get("autopilot") or {}).get("found")
              and (ap.get("autopilot") or {}).get("has_board_check"), ap)

        code, st = call(base, "GET", f"/api/projects/{pid}")
        a = st.get("autopilot") or {}
        check("autopilot reports ready + interval once registered",
              a.get("ready") is True and a.get("interval_min") == 15, a)

        code, ap2 = call(base, "POST", f"/api/projects/{pid}/autopilot", {"action": "pause"})
        check("autopilot can be paused (status flips off ACTIVE)",
              code == 200 and (ap2.get("autopilot") or {}).get("status") == "PAUSED", ap2)

        # 依赖顺序是硬约束，不是看板上的一个红点：
        # 上游没完成就能开干/收工的话，「按计划推进」只是一句口号。
        code, up = call(base, "POST", f"/api/projects/{pid}/tasks",
                        {"title": "上游 A", "assignee": mem2["id"], "estimate": 1,
                         "milestone": "M1", "deliverable": str(doc), "dod": "A 通过"})
        code, down = call(base, "POST", f"/api/projects/{pid}/tasks",
                          {"title": "下游 B", "assignee": mem2["id"], "estimate": 1,
                           "milestone": "M1", "deliverable": str(doc), "dod": "B 通过",
                           "depends_on": up["id"]})
        code, blocked_by_dep = call(base, "PATCH", f"/api/tasks/{down['id']}",
                                    {"status": "doing"})
        check("a downstream task cannot start before its upstream is done",
              code >= 400 and blocked_by_dep.get("need_force"), blocked_by_dep)
        check("the refusal names the blocking upstream",
              (blocked_by_dep.get("upstream") or {}).get("title") == "上游 A",
              blocked_by_dep.get("upstream"))

        # 总监「13 小时没碰计划」必须是一个看得见的数，不等于「全部对得上」
        code, st2 = call(base, "GET", f"/api/projects/{pid}")
        bd = st2.get("board") or {}
        check("board tracks how long the plan has been untouched",
              bd.get("silent_sec", -9) >= 0 and "threshold_sec" in bd, bd)

        # 全员对齐：人在跑、看板上却没有他的活 → 不是「进度慢」，是「账是空的」。
        # 这一条和 idle 是两件事：idle 绿（有人在跑）时它才真正开始起作用。
        _, p2 = call(base, "POST", "/api/projects",
                     {"name": "align", "deadline": "2030-01-01 18:00",
                      "goal": "g", "goal_criteria": "c"})
        pid2 = p2["project"]["id"]
        code, w = call(base, "POST", f"/api/projects/{pid2}/members",
                       {"name": "工程甲", "role": "后端工程", "model": "gpt-test-large",
                        "effort": "high", "model_confirmed": True})
        check("alignment fixture: first seat lands", code in (200, 201) and w.get("id"), w)
        code, sreq2 = call(base, "POST", f"/api/projects/{pid2}/staff-requests",
                           {"kind": "add", "name": "验收乙", "role": "验收",
                            "reason": "对齐测试要第二个工位"})
        code, dec2 = call(base, "POST", f"/api/staff-requests/{sreq2['id']}/decide",
                          {"decision": "approve"})
        check("alignment fixture: second seat lands through approval",
              code in (200, 201) and (dec2.get("member") or {}).get("id"), dec2)
        call(base, "POST", f"/api/projects/{pid2}/ack",
             {"kind": "staff", "id": sreq2["id"], "note": "收到，继续"})

        # 一条 backlog：有活可派，但没人挂上去
        code, t2 = call(base, "POST", f"/api/projects/{pid2}/tasks",
                        {"title": "该挂上去的活", "assignee": w["id"], "estimate": 2,
                         "milestone": "M1", "deliverable": str(doc), "dod": "x"})
        sfile = day / "rollout-2026-01-02T00-00-00-01a2b3c4-9999-7222-8333-444455556666.jsonl"
        sfile.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "01a2b3c4-9999-7222-8333-444455556666"}}) + "\n"
            + json.dumps({"type": "response_item",
                          "payload": {"type": "function_call_output"}}) + "\n",
            encoding="utf-8")
        call(base, "POST", f"/api/projects/{pid2}/members/{w['id']}/bind",
             {"thread_id": "01a2b3c4-9999-7222-8333-444455556666"})
        # 把「最后一次碰看板」推到 20 分钟前：宽限期过了才算违规
        import sqlite3
        from datetime import datetime, timedelta, timezone
        con = sqlite3.connect(str(home / "board.db"))
        old_iso = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=20)
                   ).isoformat(timespec="seconds")
        con.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old_iso, t2["id"]))
        # 绑会话也是 20 分钟前的事：宽限期算的是「最后一次碰看板」，不是「刚绑上」
        con.execute("UPDATE members SET bound_at=? WHERE id=?",
                    (time.time() - 1200, w["id"]))
        con.commit()
        con.close()
        time.sleep(2.2)          # 会话索引 2 秒缓存
        code, st3 = call(base, "GET", f"/api/projects/{pid2}")
        al = st3.get("align") or {}
        check("a member working off-board is flagged as unlogged",
              al.get("ok") is False
              and any(x["name"] == "工程甲" for x in (al.get("unlogged") or [])), al)

        # 催办：闸只在他伸手时咬人，人不伸手项目就停。nudge 必须自己开口，
        # 而且要说「该谁做什么」——只报状态等于又一张没人看的状态卡。
        nd = st3.get("nudge") or {}
        kinds = {x.get("kind") for x in (nd.get("items") or [])}
        check("off-board work raises the nudge list",
              nd.get("level") == "shout" and (nd.get("count") or 0) >= 2
              and {"unlogged", "unassigned"} <= kinds, nd)
        check("the nudge carries an actionable sentence, not just a status word",
              bool(nd.get("must")) and "update_task" in json.dumps(nd, ensure_ascii=False),
              nd.get("must"))
        code, denied2 = call(base, "POST", f"/api/projects/{pid2}/approvals",
                             {"goal": "g", "goal_criteria": "c", "priority_ratio": "1:1:0",
                              "members": [], "tasks": []})
        check("a new plan is refused while the team is off-board",
              code in (400, 403) and denied2.get("need_align"), denied2)
        # 解闸：把活挂到人头上（这正是被要求的那一个动作）
        call(base, "PATCH", f"/api/tasks/{t2['id']}", {"status": "doing", "assignee_id": w["id"]})
        code, st4 = call(base, "GET", f"/api/projects/{pid2}")
        check("registering the task clears the alignment gate",
              (st4.get("align") or {}).get("ok") is True,
              {k: st4.get("align", {}).get(k) for k in ("unlogged", "ghost", "nosession")})
        # 催办不是常驻噪音：事做掉了，那一行必须自己消失
        kinds4 = {x.get("kind") for x in ((st4.get("nudge") or {}).get("items") or [])}
        check("the nudge item disappears once the work is registered",
              "unlogged" not in kinds4 and "unassigned" not in kinds4,
              st4.get("nudge"))

        # 回执闸：用户点了批准，总监必须回一句「收到」。不回执就改状态/提计划/导报告，
        # 决定永远停在库里——下一轮照旧按旧编制跑，而用户以为已经生效。
        _, p3 = call(base, "POST", "/api/projects",
                     {"name": "ack", "deadline": "2030-01-01 18:00",
                      "goal": "g", "goal_criteria": "c"})
        pid3 = p3["project"]["id"]
        code, w3 = call(base, "POST", f"/api/projects/{pid3}/members",
                        {"name": "工程丙", "role": "后端工程", "model": "gpt-test-large",
                         "effort": "high", "model_confirmed": True})
        code, t3 = call(base, "POST", f"/api/projects/{pid3}/tasks",
                        {"title": "回执测试任务", "assignee": w3["id"], "estimate": 1,
                         "milestone": "M1", "deliverable": str(doc), "dod": "x"})
        code, sr3 = call(base, "POST", f"/api/projects/{pid3}/staff-requests",
                         {"kind": "add", "name": "验收丁", "role": "验收",
                          "reason": "回执测试"})
        code, dec3 = call(base, "POST", f"/api/staff-requests/{sr3['id']}/decide",
                          {"decision": "approve"})
        check("ack fixture: a decision lands", code in (200, 201), dec3)
        code, st5 = call(base, "GET", f"/api/projects/{pid3}")
        need = st5.get("need_ack") or []
        check("a decision without a receipt shows up as need_ack",
              len(need) == 1 and need[0]["kind"] == "staff" and need[0]["id"] == sr3["id"], need)
        code, blocked_r = call(base, "PATCH", f"/api/tasks/{t3['id']}", {"status": "doing"})
        check("status changes are refused until the director acknowledges",
              code in (400, 403) and blocked_r.get("need_ack"), blocked_r)
        code, acked = call(base, "POST", f"/api/projects/{pid3}/ack",
                           {"kind": "staff", "id": sr3["id"], "note": "收到，下一步派活"})
        check("ack clears the receipt and returns the remaining count",
              code == 200 and acked.get("need_ack_left") == 0, acked)
        code, freed3 = call(base, "PATCH", f"/api/tasks/{t3['id']}", {"status": "doing"})
        check("after the ack the gate opens", code == 200, freed3)
        code, st6 = call(base, "GET", f"/api/projects/{pid3}")
        check("an acknowledged decision is not re-counted",
              not (st6.get("need_ack") or []), st6.get("need_ack"))

        # 续跑：软件退出后用户只会说一句「继续」。上次留下的 doing 人已经不在了，
        # 这时直接派新活就是把新任务叠在一张空账上——必须先对齐，不能绕过。
        con = sqlite3.connect(str(home / "board.db"))
        old_iso2 = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=20)
                    ).isoformat(timespec="seconds")
        con.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old_iso2, t3["id"]))
        con.commit()
        con.close()
        code, st7 = call(base, "GET", f"/api/projects/{pid3}")
        rp = st7.get("resume") or {}
        check("a leftover doing task makes the resume gate pending",
              rp.get("pending") is True and bool(rp.get("ghost")), rp)
        code, t5 = call(base, "POST", f"/api/projects/{pid3}/tasks",
                        {"title": "重启后想直接派的新活", "assignee": w3["id"], "estimate": 1,
                         "milestone": "M1", "deliverable": str(doc), "dod": "x"})
        code, blocked_resume = call(base, "PATCH", f"/api/tasks/{t5['id']}",
                                    {"status": "doing", "assignee_id": w3["id"]})
        check("new work is refused until the leftovers are reconciled",
              code in (400, 403) and blocked_resume.get("need_resume"), blocked_resume)
        # 处置：把烂尾收回 backlog 就算对齐，闸立刻放行
        call(base, "PATCH", f"/api/tasks/{t3['id']}", {"status": "backlog"})
        # 派活必须有主：没有 assignee 的 doing，谁也没法追
        code, t6 = call(base, "POST", f"/api/projects/{pid3}/tasks",
                        {"title": "没有主的活", "estimate": 1,
                         "milestone": "M1", "deliverable": str(doc), "dod": "x"})
        code, no_owner = call(base, "PATCH", f"/api/tasks/{t6['id']}", {"status": "doing"})
        check("starting a task without an owner is refused",
              code in (400, 403) and no_owner.get("need_assignee"), no_owner)
        code, freed5 = call(base, "PATCH", f"/api/tasks/{t5['id']}",
                            {"status": "doing", "assignee_id": w3["id"]})
        check("reconciling the leftover opens the resume gate", code == 200, freed5)

        # 断点：已经做过的活不许从头再来。接管/重启必须先复盘，进度只能往上。
        code, t7 = call(base, "POST", f"/api/projects/{pid3}/tasks",
                        {"title": "带断点的活", "assignee": w3["id"], "estimate": 2,
                         "milestone": "M1", "deliverable": str(doc), "dod": "x"})
        call(base, "PATCH", f"/api/tasks/{t7['id']}", {"progress": 40})
        code, need_note = call(base, "PATCH", f"/api/tasks/{t7['id']}",
                               {"status": "doing", "assignee_id": w3["id"]})
        check("a half-done task cannot be restarted without a resume note",
              code in (400, 403) and need_note.get("need_resume_note"), need_note)
        note = "读完 a.md：接口已对接完，下一步跑 e2e"
        code, with_note = call(base, "PATCH", f"/api/tasks/{t7['id']}",
                               {"status": "doing", "assignee_id": w3["id"],
                                "resume_note": note})
        check("a resume note opens it and lands in the checkpoint",
              code == 200 and (with_note.get("checkpoint") or "") == note, with_note.get("checkpoint"))
        code, back = call(base, "PATCH", f"/api/tasks/{t7['id']}", {"progress": 10})
        check("progress may not go backwards without an explicit restart",
              code in (400, 403) and back.get("need_confirm_restart"), back)
        code, no_reason = call(base, "PATCH", f"/api/tasks/{t7['id']}",
                               {"progress": 10, "confirm_restart": True})
        check("a restart must say why", code >= 400 and no_reason.get("field") == "restart_reason",
              no_reason)
        code, restarted = call(base, "PATCH", f"/api/tasks/{t7['id']}",
                               {"progress": 10, "confirm_restart": True,
                                "restart_reason": "接口换了，前 40% 作废"})
        check("a documented restart is allowed", code == 200 and restarted.get("progress") == 10,
              restarted.get("progress"))

        # 钩子用的查询：会话 → 工位。返回 found=false 时钩子放行，不能 404。
        code, th = call(base, "GET", "/api/threads/01a2b3c4-9999-7222-8333-444455556666")
        check("a bound thread resolves to its seat",
              code == 200 and th.get("found") and (th.get("member") or {}).get("name") == "工程甲",
              th)
        code, th2 = call(base, "GET", "/api/threads/ffffffff-0000-0000-0000-000000000000")
        check("an unknown thread answers found=false instead of 404",
              code == 200 and th2.get("found") is False, th2)

        # 交付面最硬的一条：没进过 doing 的任务不许标完成。
        # 「写完了」不等于「做完了」——没有执行记录，就没有任何人在它上面跑过。
        code, t4 = call(base, "POST", f"/api/projects/{pid3}/tasks",
                        {"title": "没开过工的任务", "assignee": w3["id"], "estimate": 1,
                         "milestone": "M1", "deliverable": str(doc), "dod": "x"})
        code, no_run = call(base, "PATCH", f"/api/tasks/{t4['id']}",
                            {"status": "done", "progress": 100, "evidence": "observed",
                             "evidence_path": str(doc), "verify_cmd": "true"})
        check("a task nobody ever started cannot be marked done",
              code in (400, 403) and no_run.get("need_execution"), no_run)
        code, done_direct = call(base, "POST", f"/api/projects/{pid3}/tasks",
                                 {"title": "直接建成完成", "assignee": w3["id"], "estimate": 1,
                                  "milestone": "M1", "deliverable": str(doc), "dod": "x",
                                  "status": "done"})
        check("a task cannot be created already-done",
              code >= 400 and "已完成" in json.dumps(done_direct, ensure_ascii=False),
              done_direct)
    finally:
        kill_board(base)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
