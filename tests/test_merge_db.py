"""并入持久化（db.py）单元测试：python tests/test_merge_db.py

覆盖：
- 预览只读：不改目标版本/输入，也不改来源任何数据；
- 确认一次性产生目标的新草稿版本（kind=merge，记录来源复盘/版本），
  目标原有事件 uid 不变、复制事件取得全新 uid，来源历史不变；
- 复制保留时间范围/持续/备注/时区/UTC 偏移，关系保留等待上下界，
  仅两端均入选的关系被复制；
- 目标未变化时确认的推算结果与预览一致；目标一变，预览 digest 立即变化，
  用旧 digest 确认被拒绝（400 语义 MergeRejected）；
- 版本过期抛 VersionConflict(409)；冲突/暂存合并结果可保存但不得定稿；
- 来源/目标相同、来源复盘或版本不存在、来源缺快照、选择非法均拒绝且无部分写入。
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import db  # noqa: E402


def ev(key, name, es, ls, dur, tz=None, note=""):
    return {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
            "duration_minutes": dur, "note": note, "timezone": tz}


def rel(a, b, min_g=0, max_g=None):
    r = {"predecessor_key": a, "successor_key": b,
         "min_gap_minutes": min_g, "max_gap_minutes": max_g}
    return r


def setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def build_source(p, title="来源复盘"):
    """来源复盘 v2：A→B（10/60）、A→C 无间隔。返回 (rid, {name: uid})。"""
    rid = db.create_review(p, title)
    events = [ev("a", "来源告警", "2026-10-01T09:00", "2026-10-01T09:00", 15, note="源备注"),
              ev("b", "来源响应", "2026-10-01T09:00", "2026-10-01T12:00", 30),
              ev("c", "来源未选", "2026-10-01T08:00", "2026-10-01T08:30", 10)]
    db.update_review(p, rid, 1, title, events,
                     [rel("a", "b", min_g=10, max_g=60), rel("a", "c")])
    bundle = db.get_bundle(p, rid)
    return rid, {e["name"]: e["uid"] for e in bundle["events"]}


def build_target(p, title="目标复盘"):
    rid = db.create_review(p, title)
    events = [ev("x", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20)]
    db.update_review(p, rid, 1, title, events, [])  # v2
    return rid


def test_preview_is_read_only():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    before_t = db.get_bundle(p, tid)
    before_s = db.get_bundle(p, sid)
    db.preview_merge(p, tid, sid, 2, [suids["来源告警"], suids["来源响应"]])
    after_t = db.get_bundle(p, tid)
    after_s = db.get_bundle(p, sid)
    assert after_t["review"]["version"] == 2
    assert [e["name"] for e in after_t["events"]] == ["目标已有"]
    assert after_s["review"]["version"] == 2
    assert len(after_s["events"]) == 3 and len(after_s["relations"]) == 2
    # 版本列表不新增
    assert [v["version"] for v in db.list_versions(p, tid)] == [2, 1]
    assert before_t["review"] == after_t["review"] and before_s["review"] == after_s["review"]


def test_confirm_creates_one_new_draft_and_preserves_everything():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    target_before = db.get_bundle(p, tid)
    t_uid = target_before["events"][0]["uid"]

    preview = db.preview_merge(p, tid, sid, 2,
                               [suids["来源告警"], suids["来源响应"]])
    assert preview["target"]["base_version"] == 2
    assert [e["name"] for e in preview["copied_events"]] == ["来源告警", "来源响应"]
    # 只复制 A→B（A→C 因 C 未选被排除），保留 10/60
    assert len(preview["copied_relations"]) == 1
    assert preview["copied_relations"][0]["min_gap_minutes"] == 10
    assert preview["copied_relations"][0]["max_gap_minutes"] == 60

    new_v = db.merge_into_review(p, tid, sid, 2,
                                 [suids["来源告警"], suids["来源响应"]], 2,
                                 preview["digest"])
    assert new_v == 3
    bundle = db.get_bundle(p, tid)
    assert bundle["review"]["version"] == 3 and bundle["review"]["status"] == "draft"
    names = [e["name"] for e in bundle["events"]]
    assert names == ["目标已有", "来源告警", "来源响应"]
    by_name = {e["name"]: e for e in bundle["events"]}
    # 目标事件身份不变；复制事件是全新 uid（不同于来源 uid）
    assert by_name["目标已有"]["uid"] == t_uid
    assert by_name["来源告警"]["uid"] != suids["来源告警"]
    assert by_name["来源响应"]["uid"] != suids["来源响应"]
    # 字段保留：时间范围/持续/备注
    a = by_name["来源告警"]
    assert a["earliest_start"] == "2026-10-01T09:00" and a["duration_minutes"] == 15
    assert a["note"] == "源备注"
    # 关系一条且等待上下界保留
    assert len(bundle["relations"]) == 1
    r = bundle["relations"][0]
    assert r["min_gap_minutes"] == 10 and r["max_gap_minutes"] == 60
    # 推算：来源响应最早 = 9:00 + 15 + 10
    res = bundle["computation"]["result"]
    win_b = res["windows"][str(r["successor_id"])]
    assert win_b["earliest_start"] == "2026-10-01T09:25"
    # 新版本快照 kind=merge 且记录来源
    v3 = db.get_version(p, tid, 3)
    assert v3["kind"] == "merge" and v3["state"] == "draft"
    assert v3["source_review_id"] == sid and v3["source_version"] == 2
    # 预览与实际推算结论一致（窗口值按各自 id 对齐比较）
    prev_win_b = preview["result"]["windows"]["s_" + suids["来源响应"]]
    assert prev_win_b["earliest_start"] == win_b["earliest_start"]
    assert prev_win_b["latest_start"] == win_b["latest_start"]
    assert preview["result"]["status"] == res["status"]
    # 来源历史完全不变
    sb = db.get_bundle(p, sid)
    assert sb["review"]["version"] == 2 and len(sb["events"]) == 3
    sv2 = db.get_version(p, sid, 2)
    assert len(sv2["input"]["events"]) == 3 and len(sv2["input"]["relations"]) == 2


def test_target_changed_invalidates_preview_digest():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    chosen = [suids["来源告警"], suids["来源响应"]]
    preview = db.preview_merge(p, tid, sid, 2, chosen)

    # 目标又保存了一版（v3）：预览基于的 v2 已过期
    db.update_review(p, tid, 2, "目标复盘",
                     [ev("x", "目标已有-改", "2026-10-01T06:00", "2026-10-01T06:30", 20)], [])
    # 仍带旧 base_version 确认 ⇒ 409
    try:
        db.merge_into_review(p, tid, sid, 2, chosen, 2, preview["digest"])
        assert False, "应抛 VersionConflict"
    except db.VersionConflict as e:
        assert e.current_version == 3
    # 带上新 base_version 但沿用旧 digest ⇒ 目标已变，digest 不符，拒绝且无写入
    try:
        db.merge_into_review(p, tid, sid, 2, chosen, 3, preview["digest"])
        assert False, "应抛 MergeRejected"
    except db.MergeRejected:
        pass
    # 重新预览（基于 v3）后确认成功，结论与新预览一致
    preview2 = db.preview_merge(p, tid, sid, 2, chosen)
    assert preview2["digest"] != preview["digest"]
    db.merge_into_review(p, tid, sid, 2, chosen, 3, preview2["digest"])
    bundle = db.get_bundle(p, tid)
    assert bundle["review"]["version"] == 4
    win = bundle["computation"]["result"]["windows"]
    prev_win = preview2["result"]["windows"]
    # 复制的“来源响应”在两次规划中绝对时间一致
    assert prev_win["s_" + suids["来源响应"]]["earliest_start"] == \
        win[str(bundle["events"][2]["id"])]["earliest_start"]


def test_stale_base_version_is_409():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    preview = db.preview_merge(p, tid, sid, 2, [suids["来源告警"]])
    try:
        db.merge_into_review(p, tid, sid, 2, [suids["来源告警"]], 1, preview["digest"])
        assert False
    except db.VersionConflict as e:
        assert e.current_version == 2


def test_conflict_and_draft_merge_saved_but_not_finalizable():
    p = setup_db()
    sid = db.create_review(p, "冲突来源")
    # 来源事件输入范围自相矛盾（invalid_range）
    bad = ev("z", "矛盾事件", "2026-10-01T11:00", "2026-10-01T10:00", 5)
    db.update_review(p, sid, 1, "冲突来源", [bad], [])  # v2 conflict
    suid = db.get_bundle(p, sid)["events"][0]["uid"]
    tid = build_target(p)
    preview = db.preview_merge(p, tid, sid, 2, [suid])
    assert preview["result"]["status"] == "conflict"
    db.merge_into_review(p, tid, sid, 2, [suid], 2, preview["digest"])  # 允许保存
    assert db.get_bundle(p, tid)["computation"]["status"] == "conflict"
    try:
        db.finalize_review(p, tid, 3)
        assert False, "冲突合并结果不得定稿"
    except db.FinalizeRejected:
        pass

    # 暂存事件并入：可保存草稿，不得定稿
    sid2 = db.create_review(p, "暂存来源")
    draft = ev("d", "暂存事件", None, None, None)
    db.update_review(p, sid2, 1, "暂存来源", [draft], [])
    duid = db.get_bundle(p, sid2)["events"][0]["uid"]
    cur = db.current_version(p, tid)
    pv = db.preview_merge(p, tid, sid2, 2, [duid])
    assert [d["id"].startswith("s_") for d in pv["result"]["drafts"]] == [True]
    db.merge_into_review(p, tid, sid2, 2, [duid], cur, pv["digest"])
    try:
        db.finalize_review(p, tid, cur + 1)
        assert False, "含暂存事件不得定稿"
    except db.FinalizeRejected:
        pass


def test_timezone_and_offset_preserved_on_merge():
    p = setup_db()
    sid = db.create_review(p, "时区来源")
    tz_event = ev("n", "纽约响应", "2026-11-01T01:30-04:00", "2026-11-01T12:00-05:00",
                  30, tz="America/New_York", note="dst")
    db.update_review(p, sid, 1, "时区来源", [tz_event], [])
    nuid = db.get_bundle(p, sid)["events"][0]["uid"]
    tid = build_target(p)
    preview = db.preview_merge(p, tid, sid, 2, [nuid])
    ce = preview["copied_events"][0]
    assert ce["timezone"] == "America/New_York"
    assert ce["earliest_start"] == "2026-11-01T01:30-04:00"
    assert ce["latest_start"] == "2026-11-01T12:00-05:00"
    db.merge_into_review(p, tid, sid, 2, [nuid], 2, preview["digest"])
    e = [e for e in db.get_bundle(p, tid)["events"] if e["name"] == "纽约响应"][0]
    assert e["timezone"] == "America/New_York"
    assert e["earliest_start"] == "2026-11-01T01:30-04:00"
    assert e["latest_start"] == "2026-11-01T12:00-05:00"
    # 新版本回看完整保留时区与偏移
    v = db.get_version(p, tid, 3)
    ve = [e for e in v["input"]["events"] if e["name"] == "纽约响应"][0]
    assert ve["timezone"] == "America/New_York"
    assert ve["earliest_start"].endswith("-04:00")
    assert v["result"]["windows"][ve["key"][3:]]["timezone"] == "America/New_York"


def test_rejected_paths_have_no_partial_writes():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    chosen = [suids["来源告警"]]

    def expect(fn, exc):
        try:
            fn()
            assert False, "应抛 {}".format(exc.__name__)
        except exc:
            pass

    # 来源与目标相同
    expect(lambda: db.preview_merge(p, tid, tid, 2, chosen), db.MergeRejected)
    # 来源复盘不存在 / 来源版本不存在
    expect(lambda: db.preview_merge(p, tid, 999, 2, chosen), db.NotFound)
    expect(lambda: db.preview_merge(p, tid, sid, 99, chosen), db.VersionNotFound)
    # 事件不存在
    expect(lambda: db.preview_merge(p, tid, sid, 2, ["no-such-uid"]), db.MergeRejected)
    # 空选择 / 重复
    expect(lambda: db.preview_merge(p, tid, sid, 2, []), db.MergeRejected)
    expect(lambda: db.preview_merge(p, tid, sid, 2, chosen + chosen), db.MergeRejected)

    # 旧库缺快照的来源版本：模拟删除来源 v1 快照
    import sqlite3
    conn = sqlite3.connect(p)
    conn.execute("DELETE FROM snapshots WHERE review_id=? AND review_version=1", (sid,))
    conn.commit()
    conn.close()
    expect(lambda: db.preview_merge(p, tid, sid, 1, chosen), db.LegacyVersion)

    # 全部拒绝后目标与来源均无任何变化（版本号、事件数）
    assert db.get_bundle(p, tid)["review"]["version"] == 2
    assert len(db.get_bundle(p, tid)["events"]) == 1
    assert db.get_bundle(p, sid)["review"]["version"] == 2
    assert len(db.get_bundle(p, sid)["events"]) == 3


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
