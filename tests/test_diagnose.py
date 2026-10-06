"""冲突最小不可满足约束分析 单元测试：python tests/test_diagnose.py

覆盖：
- 三类冲突（invalid_range / cycle / window，含多路径上下界矛盾）各自
  得到极小冲突约束组：整组不可满足，去掉其中任一条后该组可满足；
- 零权重环、无冲突版本返回空组；
- 每组约束含事件最早/最晚开始边界与“先于”关系的最短/最长等待（含前项
  持续时间）；
- 暂存事件与被跳过关系不参与分析并说明原因；
- 带时区按 UTC 绝对分钟判定，输出保留当地时间与偏移；未带时区沿用旧语义；
- 同一不可变快照重复查询结果稳定；多个相互独立的冲突各自成组。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import diagnose  # noqa: E402


def ev(key, name, es, ls, dur, tz=None):
    e = {"key": key, "uid": "u-" + key, "name": name,
         "earliest_start": es, "latest_start": ls,
         "duration_minutes": dur, "note": ""}
    if tz is not None:
        e["timezone"] = tz
    return e


def rel(a, b, mn=None, mx=None):
    r = {"predecessor_key": a, "successor_key": b}
    if mn is not None:
        r["min_gap_minutes"] = mn
    if mx is not None:
        r["max_gap_minutes"] = mx
    return r


def assert_minimal(out, events, relations):
    """每组整组不可满足，删去任意一条后该组可满足。"""
    drafts = [{"key": d["key"]} for d in out["drafts"]]
    nodes, edges = diagnose._build_graph(events, relations, drafts)
    by = {e["id"]: e for e in edges}
    n = len(nodes)
    assert out["groups"], "应当至少找到一组冲突"
    for g in out["groups"]:
        ids = [c["id"] for c in g["constraints"]]
        assert len(ids) == len(set(ids)), "组内约束不重复"
        assert not diagnose._feasible([by[i] for i in ids], n), "整组必须不可满足"
        for i in ids:
            rest = [by[k] for k in ids if k != i]
            assert diagnose._feasible(rest, n), "删去任意一条后该组必须可满足: " + i


def kinds(out):
    return [g["kind"] for g in out["groups"]]


# ------------------------------------------------------------------ 归因

def test_invalid_range_group():
    events = [ev("a", "A", "2026-10-01T11:00", "2026-10-01T10:00", 10)]
    out = diagnose.analyze_input(events, [])
    assert out["status"] == "conflict"
    assert kinds(out) == ["invalid_range"]
    g = out["groups"][0]
    assert g["contradiction_minutes"] == 60 and g["slack_minutes"] == -60
    types = sorted(c["type"] for c in g["constraints"])
    assert types == ["earliest_bound", "latest_bound"]
    assert [e["name"] for e in g["events"]] == ["A"]
    assert_minimal(out, events, [])


def test_cycle_group_all_min_wait():
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T12:00", 30),
        ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    relations = [rel("a", "b"), rel("b", "a")]
    out = diagnose.analyze_input(events, relations)
    assert kinds(out) == ["cycle"]
    g = out["groups"][0]
    assert all(c["type"] == "min_wait" for c in g["constraints"])
    assert {e["name"] for e in g["events"]} == {"A", "B"}
    assert g["contradiction_minutes"] == 60
    assert_minimal(out, events, relations)


def test_zero_weight_cycle_is_empty():
    # 零时长零间隔的环可满足，不产出冲突组
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T12:00", 0),
        ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 0),
    ]
    out = diagnose.analyze_input(events, [rel("a", "b"), rel("b", "a")])
    assert out["status"] == "ok" and out["groups"] == []


def test_simple_window_group_includes_duration_and_bounds():
    # A 最晚 10:10、持续 60；B 最早 10:00；A 先于 B ⇒ B ≥ 11:00 > B 最晚 10:30
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:10", 60),
        ev("b", "B", "2026-10-01T10:00", "2026-10-01T10:30", 5),
    ]
    relations = [rel("a", "b")]
    out = diagnose.analyze_input(events, relations)
    assert kinds(out) == ["window"]
    g = out["groups"][0]
    by_type = {c["type"]: c for c in g["constraints"]}
    # 最短等待约束含前项持续时间（60 分钟）
    assert by_type["min_wait"]["delta_minutes"] == 60
    assert by_type["min_wait"]["predecessor_duration_minutes"] == 60
    assert set(by_type) >= {"min_wait", "latest_bound"}
    assert g["contradiction_minutes"] == 30
    assert_minimal(out, events, relations)


def test_multi_path_group_with_gaps_and_duration():
    # README 四事件多路径矛盾：最短路径 +60 vs 最长路径 +20 ⇒ 矛盾 40 分钟
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:00", 0),
        ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 0),
        ev("c", "C", "2026-10-01T10:00", "2026-10-01T12:00", 0),
        ev("d", "D", "2026-10-01T10:00", "2026-10-01T12:00", 0),
    ]
    relations = [
        rel("a", "b", mn=60), rel("b", "d"),
        rel("a", "c", mx=10), rel("c", "d", mx=10),
    ]
    out = diagnose.analyze_input(events, relations)
    assert out["status"] == "conflict"
    assert kinds(out) == ["window"]
    g = out["groups"][0]
    assert g["contradiction_minutes"] == 40
    assert {e["name"] for e in g["events"]} == {"A", "B", "C", "D"}
    assert {c["type"] for c in g["constraints"]} == {"min_wait", "max_wait"}
    # 四条关系都在组中（两端在环上）
    assert len(g["constraints"]) == 4
    assert_minimal(out, events, relations)


def test_opposing_chains_max_gap_group():
    # A 固定 10:00 持续 30、最长等待 10 ⇒ B ≤ 10:40；B 最早 12:00
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:00", 30),
        ev("b", "B", "2026-10-01T12:00", "2026-10-01T13:00", 30),
    ]
    relations = [rel("a", "b", mx=10)]
    out = diagnose.analyze_input(events, relations)
    assert kinds(out) == ["window"]
    g = out["groups"][0]
    by_type = {c["type"]: c for c in g["constraints"]}
    assert by_type["max_wait"]["gap_minutes"] == 10
    # 最长等待约束增量 = 前项持续 30 + 最长等待 10
    assert by_type["max_wait"]["delta_minutes"] == 40
    assert g["contradiction_minutes"] == 80
    assert_minimal(out, events, relations)


def test_relative_only_contradiction_group():
    events = [
        ev("x0", "X0", "2026-10-01T10:00", "2026-10-01T12:00", 0),
        ev("x1", "X1", "2026-10-01T10:00", "2026-10-01T12:00", 0),
        ev("x2", "X2", "2026-10-01T10:00", "2026-10-01T12:00", 0),
    ]
    relations = [rel("x0", "x1", mn=60), rel("x1", "x2", mn=10),
                 rel("x0", "x2", mx=10)]
    out = diagnose.analyze_input(events, relations)
    assert kinds(out) == ["window"]
    assert not any(e["u"] == 0 or e["v"] == 0
                   for e in diagnose._build_graph(
                       events, relations, [])[1]
                   if e["id"] in {c["id"] for c in out["groups"][0]["constraints"]})
    assert_minimal(out, events, relations)


# ------------------------------------------------------------------ 边界/默认

def test_no_conflict_empty_group():
    events = [
        ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    out = diagnose.analyze_input(events, [rel("a", "b")])
    assert out["status"] == "ok" and out["groups"] == []
    assert out["drafts"] == [] and out["excluded_relations"] == []


def test_legacy_relation_without_gap_fields():
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T12:00", 30),
        ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    # 旧快照关系无间隔字段：按最短等待 0、最长等待不限参与分析
    out = diagnose.analyze_input(events, [
        {"predecessor_key": "a", "successor_key": "b"},
        {"predecessor_key": "b", "successor_key": "a"}])
    (g,) = out["groups"]
    assert g["kind"] == "cycle"
    assert all(c["delta_minutes"] == 30 for c in g["constraints"])


def test_multiple_independent_groups():
    # 两个互不相关的矛盾范围：各自成组
    events = [
        ev("a", "A", "2026-10-01T11:00", "2026-10-01T10:00", 10),
        ev("b", "B", "2026-10-02T11:00", "2026-10-02T10:00", 10),
    ]
    out = diagnose.analyze_input(events, [])
    assert [g["index"] for g in out["groups"]] == [1, 2]
    assert all(g["kind"] == "invalid_range" for g in out["groups"])
    # 组间不共享约束
    ids = [{c["id"] for c in g["constraints"]} for g in out["groups"]]
    assert not (ids[0] & ids[1])


def test_stable_for_same_snapshot():
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:10", 60),
        ev("b", "B", "2026-10-01T10:00", "2026-10-01T10:30", 5),
    ]
    relations = [rel("a", "b")]
    r1 = diagnose.analyze_input(events, relations)
    r2 = diagnose.analyze_input(events, relations)
    key = lambda o: json.dumps(o, ensure_ascii=False, sort_keys=True)
    assert key(r1) == key(r2)


# ------------------------------------------------------------------ 排除项

def test_draft_events_and_their_relations_excluded():
    events = [
        ev("a", "完整", "2026-10-01T10:00", "2026-10-01T11:00", 10),
        {"key": "b", "uid": "u-b", "name": "缺时长",
         "earliest_start": "2026-10-01T10:00", "latest_start": "2026-10-01T11:00",
         "duration_minutes": None, "note": "", "timezone": None},
    ]
    relations = [rel("a", "b", mn=5, mx=30)]
    out = diagnose.analyze_input(events, relations)
    # 唯一完整事件自身可行 → 空组
    assert out["groups"] == []
    (d,) = out["drafts"]
    assert d["key"] == "b" and d["uid"] == "u-b" and "暂存" in d["reason"]
    (x,) = out["excluded_relations"]
    assert x["predecessor"]["name"] == "完整" and x["successor"]["name"] == "缺时长"
    assert x["min_gap_minutes"] == 5 and x["max_gap_minutes"] == 30
    assert "暂存" in x["reason"]


def test_draft_excluded_but_active_conflict_still_analyzed():
    # 暂存事件的关系被跳过；A 自身的矛盾范围仍应定位
    events = [
        ev("a", "A", "2026-10-01T11:00", "2026-10-01T10:00", 10),
        {"key": "b", "uid": "u-b", "name": "暂存",
         "earliest_start": None, "latest_start": None,
         "duration_minutes": None, "note": "", "timezone": None},
    ]
    relations = [rel("a", "b")]
    out = diagnose.analyze_input(events, relations)
    assert kinds(out) == ["invalid_range"]
    assert [d["name"] for d in out["drafts"]] == ["暂存"]
    assert len(out["excluded_relations"]) == 1


def test_relation_to_missing_event_excluded():
    events = [ev("a", "完整", "2026-10-01T10:00", "2026-10-01T11:00", 10)]
    relations = [{"predecessor_key": "a", "successor_key": "zz"}]
    out = diagnose.analyze_input(events, relations)
    assert out["status"] == "ok" and out["groups"] == []
    (x,) = out["excluded_relations"]
    assert "不存在" in x["reason"]


# ------------------------------------------------------------------ 时区

def test_timezone_conflict_uses_absolute_minutes_keeps_local():
    # 上海 10:00+08（02:00 UTC）持续 15 ⇒ B ≥ 02:15 UTC；
    # 纽约最晚 21:30-04（01:30 UTC）→ 绝对分钟矛盾 45 分钟
    events = [
        ev("a", "上海告警", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00",
           15, "Asia/Shanghai"),
        ev("b", "纽约响应", "2026-09-30T21:00-04:00", "2026-09-30T21:30-04:00",
           30, "America/New_York"),
    ]
    relations = [rel("a", "b")]
    out = diagnose.analyze_input(events, relations)
    assert kinds(out) == ["window"]
    g = out["groups"][0]
    assert g["contradiction_minutes"] == 45
    bounds = [c for c in g["constraints"] if c["type"] in ("earliest_bound", "latest_bound")]
    by_local = {c["local_time"]: c for c in bounds}
    eb = by_local["2026-10-01T10:00+08:00"]
    lb = by_local["2026-09-30T21:30-04:00"]
    # 展示保留当地时间与偏移；机器字段为 UTC 绝对分钟，差值 45 分钟
    assert eb["event"]["timezone"] == "Asia/Shanghai"
    assert lb["event"]["timezone"] == "America/New_York"
    assert eb["absolute_minutes"] - lb["absolute_minutes"] == 30
    assert_minimal(out, events, relations)


def test_dst_fold_offsets_distinguish_absolute_minutes():
    # 纽约回退日同一面盘时间 01:30 的两个偏移对应不同绝对时刻（相差 60 分钟）
    events = [
        ev("a", "第一次", "2026-11-01T01:30-04:00", "2026-11-01T01:30-04:00",
           0, "America/New_York"),
        ev("b", "第二次", "2026-11-01T01:30-05:00", "2026-11-01T01:30-05:00",
           0, "America/New_York"),
    ]
    nodes, edges = diagnose._build_graph(events, [], [])
    mins = {e["id"]: e["minutes"] for e in edges}
    # -04:00 的第一次（05:30 UTC）比 -05:00 的第二次（06:30 UTC）早 60 分钟
    assert mins["b.earliest"] - mins["a.earliest"] == 60
    assert mins["b.latest"] - mins["a.latest"] == 60


def test_analyze_snapshot_wrapper():
    version_data = {"input": {
        "events": [ev("a", "A", "2026-10-01T11:00", "2026-10-01T10:00", 10)],
        "relations": []}}
    out = diagnose.analyze_snapshot(version_data)
    assert kinds(out) == ["invalid_range"]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, "->", e)
    sys.exit(1 if failed else 0)
