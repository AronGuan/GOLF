"""从 probe_<tag>.json 抽取报告所需的 Markdown 表格（任务 A 实测报告用）。

用法::

    E:/project/golf/.tools/python312/python.exe backend/_probe_out/make_report_tables.py <tag>

输出：
    1. 切分结果总表（11 段）
    2. 正面 3 段「逐阶段完整指标数值表」
    3. 符号诊断表（每阶段 shoulder_turn / hip_turn / x_factor 裸值）

纯读取 JSON，不依赖 MediaPipe。
"""

from __future__ import annotations

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE_DIR, "_probe_out")

#: 任务 A 关注的 9 段核心视频（3 正面 + 6 DTL）
CORE_NAMES = [
    "正面1",
    "正面2",
    "正面3",
    "DTL-087d40a0",
    "DTL-0bb16a97",
    "DTL-470057ac",
    "DTL-4e8d0d7e",
    "DTL-707fb04a",
    "DTL-c6f67f38",
]


def _f(v) -> str:
    if v is None:
        return "-"
    return str(v)


def print_segment_table(data):
    print("\n## 切分结果总表\n")
    print(
        "| # | 视频 | 机位 | 切分 | 失败阶段 | "
        "上杆(s) | 下杆(s) | 送杆收杆(s) | 估计阶段数 |"
    )
    print("|---|------|------|------|---------|---------|---------|------------|-----------|")
    for i, r in enumerate(data, 1):
        name = r["name"]
        view = r["view"]
        seg = r.get("segment_ok", False)
        if not seg:
            print(
                f"| {i} | {name} | {view} | ❌失败 | "
                f"{r.get('stage_failed')}/{r.get('error_code')} | - | - | - | - |"
            )
            continue
        d = r.get("durations_sec", {}) or {}
        up = _f(d.get("addr_to_top"))
        down = _f(d.get("top_to_impact"))
        follow = _f(d.get("impact_to_finish"))
        est = r.get("estimated_count", 0)
        # 退化判定：上杆或下杆时长为 None 或明显异常（<0.1 或 >5）
        bad = (d.get("addr_to_top") in (None,)) or (d.get("top_to_impact") in (None,))
        flag = "✅成功" if not bad else "⚠️退化"
        print(
            f"| {i} | {name} | {view} | {flag} | - | "
            f"{up} | {down} | {follow} | {est} |"
        )


def print_faceon_metrics(data):
    for r in data:
        if not r["view"] == "face-on" or not r.get("segment_ok"):
            continue
        name = r["name"]
        print(f"\n### {name} —— 逐阶段完整指标\n")
        print("| 阶段 | 指标 | 数值 | 单位 | 参考区间 | 状态 |")
        print("|------|------|------|------|---------|------|")
        phases = r.get("phases", {})
        for key, block in phases.items():
            name_cn = block.get("name_cn", key)
            for m in block.get("metrics", []):
                ref = m.get("ref")
                ref_s = f"{ref[0]}~{ref[1]}" if ref and ref[0] is not None else "-"
                print(
                    f"| {name_cn} | {m.get('name', m.get('key'))} | "
                    f"{m.get('value')} | {m.get('unit', '')} | {ref_s} | "
                    f"{m.get('status', '')} |"
                )


def print_sign_table(data):
    print("\n## 符号诊断表（裸角度，未 clamp）\n")
    print("| 视频 | 阶段 | shoulder_turn | hip_turn | x_factor | raw_no_sign(sh) |")
    print("|------|------|--------------|---------|---------|-----------------|")
    for r in data:
        if not r.get("segment_ok"):
            continue
        diag = r.get("diag_raw_angles", {})
        for key, v in diag.items():
            print(
                f"| {r['name']} | {key} | {_f(v.get('shoulder_turn_signed'))} | "
                f"{_f(v.get('hip_turn_signed'))} | {_f(v.get('x_factor_raw'))} | "
                f"{_f(v.get('shoulder_rot_raw_no_sign'))} |"
            )


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "fixed"
    path = os.path.join(OUT_DIR, f"probe_{tag}.json")
    if not os.path.exists(path):
        print(f"NOT FOUND: {path}")
        return 1
    data = json.load(open(path, encoding="utf-8"))

    seg_ok = sum(1 for r in data if r.get("segment_ok"))
    total = len(data)
    # 退化：切分成功但上杆/下杆时长缺失
    deg = 0
    for r in data:
        if r.get("segment_ok"):
            d = r.get("durations_sec", {}) or {}
            if d.get("addr_to_top") is None or d.get("top_to_impact") is None:
                deg += 1
    print(f"# 探针 tag={tag} 汇总")
    print(f"\n切分成功 {seg_ok}/{total} = {seg_ok / max(1, total) * 100:.1f}%"
          f"（含退化 {deg} 段，净可用 {seg_ok - deg}/{total}）")

    print_segment_table(data)
    print_faceon_metrics(data)
    print_sign_table(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
