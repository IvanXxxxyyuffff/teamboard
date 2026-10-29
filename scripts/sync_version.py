#!/usr/bin/env python3
"""Regenerate both plugin manifests from plugins/teamboard/VERSION."""
import json, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "teamboard"
v = (PLUGIN / "VERSION").read_text(encoding="utf-8").strip()

for rel in ("plugin.json", ".codex-plugin/plugin.json"):
    p = PLUGIN / rel
    data = json.loads(p.read_text(encoding="utf-8"))
    data["version"] = v
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{rel} -> {v}")
