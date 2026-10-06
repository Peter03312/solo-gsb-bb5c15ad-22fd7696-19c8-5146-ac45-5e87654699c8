"""并入规划（merge.py）单元测试：python tests/test_merge.py

覆盖：
- 仅复制两端均入选的“先于”关系，保留等待上下界；
- 复制事件保留时间范围/持续/备注/时区与 UTC 偏移；
- 保存载荷中复制事件不带 uid（目标分配新身份），目标事件沿用 uid；
- 暂存来源事件与其关系按既有口径跳过；合并冲突/暂存结论可规划；
- digest 对相同输入稳定、对不同选择/目标变化敏感；
- 选择为空/重复/非法、缺可靠身份的快照一律拒绝（纯规划层）。
"""
import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import merge  # noqa: E402


def ev(eid, name, es, ls, dur, uid=None, tz=None, note=""):
    row = {"id": eid, "name": name, "earliest_start": es, "latest_start": ls,
           "duration_minutes": dur, "note": note, "timezone": tz}
    if uid is not None:
        row["uid"] = uid
    return row


def snap(eid, uid, name, es, ls, dur, tz=None, note=""):
    return {"key": "id_{}".format(eid), "uid": uid, "name": name,
            "earliest_start": es, "latest_start": ls,
            "duration_minutes": dur, "note": note, "timezone": tz}


def rel(rid, a, b, min_g=0, max_g=None):
    return {"id": rid, "predecessor_id": a, "successor_id": b,
            "min_gap_minutes": min_g, "max_gap_minutes": max_g}


def src_data(events, relations):
    return {"version": 3, "kind": "save", "state": "draft", "title": "来源",
            "input": {"events": events, "relations": relations},
            "result": {"windows": {}, "conflicts": [], "drafts": []}}


def src_ev(key, uid, name, es, ls, dur, tz=None, note=""):
    return {"key": key, "uid": uid, "name": name, "earliest_start": es,
            "latest_start": ls, "duration_minutes": dur, "note": note,
            "timezone": tz}


def src_rel(a, b, min_g=0, max_g=None):
    r = {"predecessor_key": a, "successor_key": b,
         "min_gap_minutes": min_g, "max_gap_minutes": max_g}
    return r


def base_target():
    live = [ev(1, "目标事件", "2026-10-01T07:00", "2026-10-01T08:00", 20, uid="tuid1")]
    snaps = [snap(1, "tuid1", "目标事件", "2026-10-01T07:00", "2026-10-01T08:00", 20)]
    return snaps, live, []


def test_only_both_end_selected_relations_copied():
    snaps, live, _ = base_target()
    # 来源：A→B（10/60）、A→C；只选 A、B ⇒ 只复制 A→B
    se = [src_ev("id_1", "ua", "A", "2026-10-01T09:00", "2026-10-01T09:00", 15),
          src_ev("id_2", "ub", "B", "2026-10-01T09:00", "2026-10-01T12:00", 30),
          src_ev("id_3", "uc", "C", "2026-10-01T08:00", "2026-10-01T08:30", 10)]
    sr = [src_rel("id_1", "id_2", min_g=10, max_g=60), src_rel("id_1", "id_3")]
    plan = merge.build_plan(snaps, live, [], src_data(se, sr), ["ua", "ub"])
    assert [r["predecessor_uid"] for r in plan["copied_relations"]] == ["ua"]
    cr = plan["copied_relations"][0]
    assert cr["successor_uid"] == "ub" and cr["min_gap_minutes"] == 10
    assert cr["max_gap_minutes"] == 60
    # 推算：B 最早开始 = 9:00 + 15 + 10 = 9:25；窗口最晚 = 9:00 + 15 + 60 = 10:15
    wb = plan["result"]["windows"][merge.source_token("ub")]
    assert wb["earliest_start"] == "2026-10-01T09:25"
    assert wb["latest_start"] == "2026-10-01T10:15"
    # C 不参与推算（未复制）
    assert merge.source_token("uc") not in plan["result"]["windows"]


def test_copied_event_fields_preserved():
    snaps, live, _ = base_target()
    se = [src_ev("id_1", "ua", "纽约事件",
                 "2026-11-01T01:30-04:00", "2026-11-01T12:00-05:00", 30,
                 tz="America/New_York", note="重要备注")]
    plan = merge.build_plan(snaps, live, [], src_data(se, []), ["ua"])
    ce = plan["copied_events"][0]
    assert ce["timezone"] == "America/New_York"
    assert ce["earliest_start"] == "2026-11-01T01:30-04:00"   # UTC 偏移保留
    assert ce["latest_start"] == "2026-11-01T12:00-05:00"
    assert ce["duration_minutes"] == 30 and ce["note"] == "重要备注"
    w = plan["result"]["windows"][merge.source_token("ua")]
    assert w["timezone"] == "America/New_York"
    assert w["earliest_start"] == "2026-11-01T01:30-04:00"


def test_save_payload_new_identity_for_copied_and_keep_target():
    snaps, live, rels = (
        [snap(1, "tuid1", "T", "2026-10-01T07:00", "2026-10-01T08:00", 20)],
        [ev(1, "T", "2026-10-01T07:00", "2026-10-01T08:00", 20, uid="tuid1")],
        [])
    se = [src_ev("id_7", "ua", "A", "2026-10-01T09:00", "2026-10-01T09:00", 15, note="n"),
          src_ev("id_8", "ub", "B", "2026-10-01T09:00", "2026-10-01T12:00", 30)]
    sr = [src_rel("id_7", "id_8", min_g=5)]
    plan = merge.build_plan(snaps, live, rels, src_data(se, sr), ["ua", "ub"])
    events, relations = merge.save_payload(plan)
    by_key = {e["key"]: e for e in events}
    assert by_key["id_1"]["uid"] == "tuid1"          # 目标沿用身份
    copied = [e for e in events if e["key"] != "id_1"]
    assert len(copied) == 2
    assert all(e["uid"] == "" for e in copied)        # 复制事件在目标取新身份
    assert {e["name"] for e in copied} == {"A", "B"}
    assert all(e["key"].startswith("m_") for e in copied)
    # 复制关系改按新 key 引用且保留等待
    assert len(relations) == 1
    r = relations[0]
    key_a = next(e["key"] for e in copied if e["name"] == "A")
    key_b = next(e["key"] for e in copied if e["name"] == "B")
    assert r["predecessor_key"] == key_a and r["successor_key"] == key_b
    assert r["min_gap_minutes"] == 5 and r["max_gap_minutes"] is None


def test_draft_source_event_and_its_relations_skipped():
    snaps, live, _ = base_target()
    se = [src_ev("id_1", "ua", "完整A", "2026-10-01T09:00", "2026-10-01T09:00", 15),
          src_ev("id_2", "ub", "暂存B", None, None, None)]
    sr = [src_rel("id_1", "id_2", min_g=5)]
    plan = merge.build_plan(snaps, live, [], src_data(se, sr), ["ua", "ub"])
    # 两端都选了 ⇒ 关系被复制，但 B 暂存 ⇒ 推算时该关系跳过
    assert len(plan["copied_relations"]) == 1
    result = plan["result"]
    assert [d["id"] for d in result["drafts"]] == [merge.source_token("ub")]
    assert len(result["skipped_relations"]) == 1
    assert "暂存" in result["skipped_relations"][0]["reason"]
    assert result["status"] == "ok"


def test_conflicting_merge_is_planned_but_result_conflict():
    snaps = [snap(1, "tuid1", "T", "2026-10-01T11:00", "2026-10-01T11:00", 0)]
    live = [ev(1, "T", "2026-10-01T11:00", "2026-10-01T11:00", 0, uid="tuid1")]
    # 来源 A 固定 10:00，复制后 A→T（等待 0），但 T 输入固定 11:00：
    # 仅复制来源内部关系不会产生跨接；这里构造来源自身冲突
    se = [src_ev("id_1", "ua", "A", "2026-10-01T11:00", "2026-10-01T10:00", 0)]
    plan = merge.build_plan(snaps, live, [], src_data(se, []), ["ua"])
    assert plan["result"]["status"] == "conflict"
    assert any(c["type"] == "invalid_range" for c in plan["result"]["conflicts"])


def test_digest_stable_and_sensitive():
    snaps, live, _ = base_target()
    se = [src_ev("id_1", "ua", "A", "2026-10-01T09:00", "2026-10-01T09:00", 15),
          src_ev("id_2", "ub", "B", "2026-10-01T09:00", "2026-10-01T12:00", 30)]
    sr = [src_rel("id_1", "id_2", min_g=10)]
    d = src_data(se, sr)
    p1 = merge.build_plan(copy.deepcopy(snaps), copy.deepcopy(live), [], d, ["ua", "ub"])
    p2 = merge.build_plan(copy.deepcopy(snaps), copy.deepcopy(live), [],
                          copy.deepcopy(d), ["ua", "ub"])
    assert p1["digest"] == p2["digest"]
    # 选择不同 ⇒ digest 不同
    p3 = merge.build_plan(snaps, live, [], src_data(se, sr), ["ua"])
    assert p3["digest"] != p1["digest"]
    # 目标输入变化 ⇒ digest 不同
    changed = ev(1, "目标事件", "2026-10-01T06:00", "2026-10-01T08:00", 20, uid="tuid1")
    p4 = merge.build_plan(
        [snap(1, "tuid1", "目标事件", "2026-10-01T06:00", "2026-10-01T08:00", 20)],
        [changed], [], src_data(se, sr), ["ua", "ub"])
    assert p4["digest"] != p1["digest"]
    # 来源关系间隔变化 ⇒ digest 不同
    p5 = merge.build_plan(copy.deepcopy(snaps), copy.deepcopy(live), [],
                          src_data(se, [src_rel("id_1", "id_2", min_g=20)]), ["ua", "ub"])
    assert p5["digest"] != p1["digest"]


def test_invalid_selection_rejected():
    snaps, live, _ = base_target()
    se = [src_ev("id_1", "ua", "A", "2026-10-01T09:00", "2026-10-01T09:00", 15)]
    d = src_data(se, [])
    for bad in ([], None, ["ua", "ua"], [""], [42], ["missing-uid"]):
        try:
            merge.build_plan(snaps, live, [], d, bad if bad is not None else None)
            assert False, "应当拒绝：{!r}".format(bad)
        except merge.MergeError:
            pass


def test_snapshot_without_reliable_identity_rejected():
    snaps, live, _ = base_target()
    se = [{"key": "id_1", "name": "旧A", "earliest_start": "2026-10-01T09:00",
           "latest_start": "2026-10-01T09:00", "duration_minutes": 15}]  # 无 uid
    plan_input = src_data(se, [])
    try:
        merge.build_plan(snaps, live, [], plan_input, [None])
        assert False
    except merge.MergeError:
        pass


def test_target_relations_remain_in_plan():
    snaps = [snap(1, "t1", "T1", "2026-10-01T07:00", "2026-10-01T08:00", 10),
             snap(2, "t2", "T2", "2026-10-01T07:00", "2026-10-01T12:00", 10)]
    live = [ev(1, "T1", "2026-10-01T07:00", "2026-10-01T08:00", 10, uid="t1"),
            ev(2, "T2", "2026-10-01T07:00", "2026-10-01T12:00", 10, uid="t2")]
    rels = [rel(10, 1, 2, min_g=20, max_g=120)]
    se = [src_ev("id_1", "ua", "A", "2026-10-01T09:00", "2026-10-01T09:00", 5)]
    plan = merge.build_plan(snaps, live, rels, src_data(se, []), ["ua"])
    events, relations = merge.save_payload(plan)
    # 目标关系原样保留
    assert {"predecessor_key": "id_1", "successor_key": "id_2",
            "min_gap_minutes": 20, "max_gap_minutes": 120} in relations
    # 合并推算中 T2 最早 = 7:00 + 10 + 20 = 7:30
    w = plan["result"]["windows"][merge.target_token(2)]
    assert w["earliest_start"] == "2026-10-01T07:30"


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
