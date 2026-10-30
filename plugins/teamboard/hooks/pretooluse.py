#!/usr/bin/env python3
"""TeamBoard · PreToolUse 闸：子代理动手之前，必须已经在看板上挂号。

为什么要在宿主层拦：插件能管住「交付」，管不住「先干两小时再说」。
子代理不调看板工具也能读代码、跑命令，账上就是空的——这是整套约束唯一的漏点。
Codex 的 PreToolUse 钩子跑在工具真正执行之前，是唯一能堵住它的地方。

规则（故意窄，避免把整个 App 卡死）：
  1. 只拦「动手」类工具（Bash / apply_patch / 写文件）。查状态、派活、审批走 MCP，永远放行。
  2. 只拦已经在看板上登记过的工位会话：没登记的会话不管（它们由 unseated 报警处理）。
  3. 项目总监放行：它的活是调度，不是执行。
  4. 工位名下没有任何 doing 任务 → 拒绝，并给出该调的那一条命令。
  5. 任何异常（看板没起、超时、JSON 解析失败）一律放行：闸门故障不能变成全站瘫痪。

退出约定：输出 {"decision":"deny","reason":"..."} 即拦住；什么都不输出即放行。
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("TEAMBOARD_URL", "http://127.0.0.1:8787").rstrip("/")
TIMEOUT = 1.2
# 这些是「动手」工具：读代码、改文件、跑命令。查/派/批不在此列。
WORK_TOOLS = {"bash", "apply_patch", "applypatch", "exec", "exec_command",
              "shell", "run_command", "write", "write_file", "edit", "edit_file"}


def out(obj):
    if obj:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    return 0


def main():
    try:
        raw = sys.stdin.read() or "{}"
        d = json.loads(raw)
    except Exception:
        return out({})                      # 解析不了就放行

    tool = str(d.get("tool_name") or d.get("toolName") or "").split(".")[-1].lower()
    if tool not in WORK_TOOLS:
        return out({})

    sid = (d.get("session_id") or d.get("sessionId") or "").strip()
    if not sid:
        return out({})

    try:
        with urllib.request.urlopen(f"{BASE}/api/threads/{sid}", timeout=TIMEOUT) as f:
            info = json.loads(f.read() or b"{}")
    except Exception:
        return out({})                      # 看板没起：放行，别让闸门故障拖垮 App

    if not info.get("found"):
        return out({})                      # 没登记的会话不归这个闸管
    m = info.get("member") or {}
    if m.get("is_lead"):
        return out({})                      # 项目总监放行

    if info.get("doing"):
        return out({})                      # 已挂号：放行

    name = m.get("name") or "这个工位"
    mid = m.get("id") or "<member_id>"
    return out({"decision": "deny",
                "reason": (f"[TeamBoard] {name} 还没在看板上挂号。先认领一条任务再动手："
                           f"update_task(assignee_id='{mid}', status='doing', task='<任务id>')；"
                           f"没有可认领的就先找项目总监要活。"
                           f"看板看不见的工作＝没干，交付时也过不了闸。")})


if __name__ == "__main__":
    sys.exit(main())
