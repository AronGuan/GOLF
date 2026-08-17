"""分析 reanchor 后事件帧是否在解码集内（P1 修复依据）。"""
import json
import sys

sys.path.insert(0, "backend")

data = json.load(open("backend/_probe_out/probe_clublite_v1.json", encoding="utf-8"))
print("records:", len(data))
for r in data:
    if not r.get("reanchor_ok"):
        print(f"{r['name']:<14s} (no reanchor / segment fail)")
        continue
    before = {e["key"]: e["frame_index"] for e in r["events_before"]}
    after = {e["key"]: e["frame_index"] for e in r["events_after"]}
    decode = set(r["window"]["decode_frames"])
    moved = [k for k in after if after[k] != before[k]]
    outside = [k for k in after if after[k] not in decode]
    impact = after["impact"] - before["impact"]
    print(
        f"{r['name']:<14s} impact {before['impact']}->{after['impact']} "
        f"(d={impact:+d}) moved={moved} outside_decode={outside}"
    )
    print(f"    decode({len(decode)}): {sorted(decode)}")
