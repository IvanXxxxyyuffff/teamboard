#!/usr/bin/env python3
"""Board API smoke test: boots server.py on a throwaway port + DB, no side effects."""
import json, subprocess, sys, pathlib
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

        code, mem3 = call(base, "POST", f"/api/projects/{pid}/members",
                          {"name": "未确认", "role": "测试", "model": "gpt-test-large", "effort": "high"})
        check("model needs explicit user confirmation", code >= 400 and "确认" in json.dumps(mem3, ensure_ascii=False), mem3)

        code, bad = call(base, "POST", f"/api/projects/{pid}/tasks",
                         {"title": "裸任务", "assignee": mem2["id"], "estimate": 3})
        check("goal-bound task without milestone/deliverable/dod is rejected",
              code >= 400 and "milestone" in json.dumps(bad, ensure_ascii=False), bad)

        code, task = call(base, "POST", f"/api/projects/{pid}/tasks",
                          {"title": "做接口", "assignee": mem2["id"], "estimate": 3,
                           "milestone": "M1", "deliverable": "api.md", "dod": "200 on /x"})
        check("task created", code in (200, 201) and task.get("id"), task)

        code, state = call(base, "GET", f"/api/projects/{pid}")
        check("project state has stats/tasks/members",
              all(k in state for k in ("stats", "tasks", "members")), list(state)[:8])
        check("progress is numeric", isinstance(state["stats"].get("overall"), (int, float)), state["stats"])

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
