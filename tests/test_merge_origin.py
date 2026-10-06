"""并入事件来源摘要（origin）持久化测试：python tests/test_merge_origin.py

跨复盘并入确认后，每个复制事件保留：
  - 来源复盘（id + 并入当时的标题）
  - 来源版本
  - 来源事件身份（uid + 并入当时的名称）
来源摘要与复制事件在目标取得的**全新持续身份（uid）**绑定。

覆盖：
- 预览的 copied_events 带来源摘要；只读，不写任何数据；
- 确认后目标 live 事件与不可变快照都携带来源摘要；非并入事件完全没有
  origin 字段；目标事件与推算结果不因来源摘要改变；
- 再次保存（改名/改时间，uid 回传）来源不丢失；客户端无法伪造 origin；
- 版本回看、恢复为新草稿、版本对照均可查看来源；
- 来源复盘删除后仍显示已保存的来源摘要（不回读、不改写来源历史）；
- 复制事件的新 uid 与来源 uid 不同，但 origin.source_event_uid 指向来源。
"""
import json
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import db  # noqa: E402
import merge  # noqa: E402


def ev(key, name, es, ls, dur, tz=None, note=""):
    return {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
            "duration_minutes": dur, "note": note, "timezone": tz}


def rel(a, b, min_g=0, max_g=None):
    return {"predecessor_key": a, "successor_key": b,
            "min_gap_minutes": min_g, "max_gap_minutes": max_g}


def setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def build_source(p, title="来源复盘甲"):
    rid = db.create_review(p, title)
    db.update_review(p, rid, 1, title, [
        ev("a", "来源告警", "2026-10-01T09:00", "2026-10-01T09:00", 15, note="源备注"),
        ev("b", "来源响应", "2026-10-01T09:00", "2026-10-01T12:00", 30),
    ], [rel("a", "b", 10, 60)])  # v2
    uids = {e["name"]: e["uid"] for e in db.get_bundle(p, rid)["events"]}
    return rid, uids


def build_target(p, title="目标复盘乙"):
    rid = db.create_review(p, title)
    db.update_review(p, rid, 1, title,
                     [ev("x", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20)], [])
    return rid


def _origin_for(review_bundle, name):
    return next(e["origin"] for e in review_bundle["events"] if e["name"] == name)


def test_save_payload_attaches_origin_only_to_copied_events():
    se = [{"key": "id_1", "uid": "ua", "name": "A",
           "earliest_start": "2026-10-01T09:00", "latest_start": "2026-10-01T09:00",
           "duration_minutes": 15, "note": "", "timezone": None}]
    src = {"version": 2, "kind": "save", "state": "draft", "title": "来源",
           "input": {"events": se, "relations": []},
           "result": {"windows": {}, "conflicts": [], "drafts": []}}
    snaps = [{"key": "id_1", "uid": "t1", "name": "T",
              "earliest_start": "2026-10-01T07:00", "latest_start": "2026-10-01T08:00",
              "duration_minutes": 20, "note": "", "timezone": None}]
    live = [{"id": 1, "uid": "t1", "name": "T",
             "earliest_start": "2026-10-01T07:00", "latest_start": "2026-10-01T08:00",
             "duration_minutes": 20, "note": "", "timezone": None}]
    plan = merge.build_plan(snaps, live, [], src, ["ua"])
    events, _ = merge.save_payload(plan)  # 无 source_meta：不带 origin（单测向后兼容）
    assert all("origin" not in e for e in events)

    events, _ = merge.save_payload(plan, source_meta={
        "review_id": 7, "review_title": "来源", "version": 2,
        "event_names": {"ua": "A"}})
    target = next(e for e in events if e["key"] == "id_1")
    copied = next(e for e in events if e["key"].startswith("m_"))
    assert "origin" not in target                       # 目标事件不带来源
    assert copied["origin"] == {
        "source_review_id": 7, "source_review_title": "来源",
        "source_version": 2, "source_event_uid": "ua",
        "source_event_name": "A"}
    assert copied["uid"] == ""                          # 新持续身份由服务端换发


def test_preview_carries_origin_but_writes_nothing():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    before = db.get_bundle(p, tid)
    pv = db.preview_merge(p, tid, sid, 2,
                          [suids["来源告警"], suids["来源响应"]])
    by_uid = {e["uid"]: e for e in pv["copied_events"]}
    o = by_uid[suids["来源告警"]]["origin"]
    assert o == {
        "source_review_id": sid, "source_review_title": "来源复盘甲",
        "source_version": 2, "source_event_uid": suids["来源告警"],
        "source_event_name": "来源告警"}
    # 预览只读：目标 live/版本均不变，且没有任何 origin_json 落库
    after = db.get_bundle(p, tid)
    assert before["review"] == after["review"]
    assert after["events"][0].get("origin") is None
    conn = sqlite3.connect(p)
    n = conn.execute("SELECT COUNT(*) FROM events WHERE origin_json IS NOT NULL").fetchone()[0]
    conn.close()
    assert n == 0


def test_confirm_persists_origin_bound_to_new_identity():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    pv = db.preview_merge(p, tid, sid, 2, [suids["来源告警"]])
    db.merge_into_review(p, tid, sid, 2, [suids["来源告警"]], 2, pv["digest"])
    b = db.get_bundle(p, tid)
    target = next(e for e in b["events"] if e["name"] == "目标已有")
    alarm = next(e for e in b["events"] if e["name"] == "来源告警")
    assert "origin" not in target                                  # 非并入事件无来源
    assert alarm["uid"] != suids["来源告警"]                        # 全新持续身份
    # 预览展示的来源摘要与确认后持久化的完全一致（同一固化副本）
    preview_origin = pv["copied_events"][0]["origin"]
    assert alarm["origin"] == preview_origin
    assert alarm["origin"] == {
        "source_review_id": sid, "source_review_title": "来源复盘甲",
        "source_version": 2, "source_event_uid": suids["来源告警"],
        "source_event_name": "来源告警"}
    # 不可变快照（v3）同样保留，且与新 uid 绑定
    v3 = db.get_version(p, tid, 3)
    snap_alarm = next(e for e in v3["input"]["events"] if e["name"] == "来源告警")
    assert snap_alarm["uid"] == alarm["uid"]
    assert snap_alarm["origin"] == alarm["origin"]
    snap_target = next(e for e in v3["input"]["events"] if e["name"] == "目标已有")
    assert "origin" not in snap_target
    # 来源摘要不改变目标事件与推算结果
    win = b["computation"]["result"]["windows"][str(alarm["id"])]
    assert win["earliest_start"] == "2026-10-01T09:00"


def test_resave_keeps_origin_and_ignores_client_injection():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    pv = db.preview_merge(p, tid, sid, 2, [suids["来源告警"]])
    db.merge_into_review(p, tid, sid, 2, [suids["来源告警"]], 2, pv["digest"])
    b = db.get_bundle(p, tid)
    uid_alarm = next(e["uid"] for e in b["events"] if e["name"] == "来源告警")
    origin = _origin_for(b, "来源告警")

    # 改名 + 改时间 + 改备注后整体保存（模拟客户端回传 uid）；顺带在非并入
    # 事件与并入事件上各塞一个伪造 origin，服务端必须以前者无来源、后者沿用
    # 已保存来源为准。
    events = []
    for e in b["events"]:
        item = {
            "key": "id_" + str(e["id"]), "uid": e["uid"],
            "name": "告警（改名）" if e["name"] == "来源告警" else e["name"],
            "earliest_start": "2026-10-01T09:05" if e["name"] == "来源告警" else e["earliest_start"],
            "latest_start": "2026-10-01T09:30" if e["name"] == "来源告警" else e["latest_start"],
            "duration_minutes": e["duration_minutes"], "note": e["note"],
            "timezone": e.get("timezone")}
        item["origin"] = {"source_review_id": 999, "source_event_name": "伪造"}
        events.append(item)
    db.update_review(p, tid, 3, "目标复盘乙", events, [])  # v4
    b4 = db.get_bundle(p, tid)
    renamed = next(e for e in b4["events"] if e["name"] == "告警（改名）")
    target = next(e for e in b4["events"] if e["name"] == "目标已有")
    assert renamed["uid"] == uid_alarm
    assert renamed["origin"] == origin                 # 已保存来源原样保留
    assert "origin" not in target                      # 伪造来源被服务端拒绝
    # 新草稿快照（v4）也保留来源
    v4 = db.get_version(p, tid, 4)
    assert any(e.get("origin") == origin for e in v4["input"]["events"])


def test_restore_keeps_origin():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    pv = db.preview_merge(p, tid, sid, 2, [suids["来源告警"]])
    db.merge_into_review(p, tid, sid, 2, [suids["来源告警"]], 2, pv["digest"])  # v3
    origin = _origin_for(db.get_bundle(p, tid), "来源告警")
    # 再普通保存一版（v4），随后恢复 v3 → v5
    b3 = db.get_bundle(p, tid)
    db.update_review(p, tid, 3, "目标复盘乙", [
        {"key": "id_" + str(e["id"]), "uid": e["uid"], "name": e["name"],
         "earliest_start": e["earliest_start"], "latest_start": e["latest_start"],
         "duration_minutes": e["duration_minutes"], "note": e["note"],
         "timezone": e.get("timezone")} for e in b3["events"]], [])
    db.restore_review(p, tid, 4, 3)  # v5
    b5 = db.get_bundle(p, tid)
    assert _origin_for(b5, "来源告警") == origin
    v5 = db.get_version(p, tid, 5)
    assert v5["kind"] == "restore" and v5["source_version"] == 3
    assert any(e.get("origin") == origin for e in v5["input"]["events"])


def test_compare_shows_origin_for_copied_events():
    p = setup_db()
    sid, suids = build_source(p)
    tid = build_target(p)
    pv = db.preview_merge(p, tid, sid, 2, [suids["来源告警"]])
    db.merge_into_review(p, tid, sid, 2, [suids["来源告警"]], 2, pv["digest"])  # v3
    origin = _origin_for(db.get_bundle(p, tid), "来源告警")

    # v2（并入前）→ v3（并入后）：来源告警是“新增事件”，携带 origin
    cmp_ = db.compare_versions(p, tid, 2, 3)
    assert cmp_["pairable"]
    added = {e["name"]: e for e in cmp_["events"]["added"]}
    assert "来源告警" in added and added["来源告警"]["origin"] == origin
    assert all("origin" not in e for e in cmp_["events"]["unchanged"])

    # v3 → 普通保存 v4（改名）：改名条目与变化条目均能看到来源
    b3 = db.get_bundle(p, tid)
    events = []
    for e in b3["events"]:
        events.append({
            "key": "id_" + str(e["id"]), "uid": e["uid"],
            "name": "告警改名" if e["name"] == "来源告警" else e["name"],
            "earliest_start": e["earliest_start"], "latest_start": e["latest_start"],
            "duration_minutes": e["duration_minutes"], "note": e["note"],
            "timezone": e.get("timezone")})
    db.update_review(p, tid, 3, "目标复盘乙", events, [])  # v4
    cmp2 = db.compare_versions(p, tid, 3, 4)
    rn = next(r for r in cmp2["events"]["renamed"] if r["to"] == "告警改名")
    assert rn["origin"] == origin


def test_origin_survives_source_review_deletion_and_rename():
    p = setup_db()
    sid, suids = build_source(p, title="来源原名")
    tid = build_target(p)
    pv = db.preview_merge(p, tid, sid, 2, [suids["来源告警"]])
    db.merge_into_review(p, tid, sid, 2, [suids["来源告警"]], 2, pv["digest"])  # v3
    origin = _origin_for(db.get_bundle(p, tid), "来源告警")
    assert origin["source_review_title"] == "来源原名"

    db.delete_review(p, sid)
    # 当前详情、历史快照、对照都仍显示已保存摘要，不报错、不回读
    b = db.get_bundle(p, tid)
    assert _origin_for(b, "来源告警") == origin
    v3 = db.get_version(p, tid, 3)
    assert any(e.get("origin") == origin for e in v3["input"]["events"])
    cmp_ = db.compare_versions(p, tid, 2, 3)
    added = {e["name"]: e for e in cmp_["events"]["added"]}
    assert added["来源告警"]["origin"] == origin


def test_migrated_old_db_has_no_origin_on_existing_rows():
    # 旧库（events 无 origin_json 列）启动迁移后：旧行无来源（非并入事件），
    # 新保存/新并入行为与新库一致，且迁移幂等。
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(
        "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT, updated_at TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " name TEXT, earliest_start TEXT, latest_start TEXT, duration_minutes INTEGER,"
        " note TEXT DEFAULT '', uid TEXT);"
        "CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " predecessor_id INTEGER, successor_id INTEGER, min_gap_minutes INTEGER DEFAULT 0,"
        " max_gap_minutes INTEGER);"
        "CREATE TABLE computations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " review_version INTEGER, status TEXT, result_json TEXT, computed_at TEXT);"
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " review_version INTEGER, kind TEXT, title TEXT, input_json TEXT,"
        " source_version INTEGER, source_review_id INTEGER, created_at TEXT);"
        "INSERT INTO reviews VALUES (1,'旧','draft',1,'t','t');"
        "INSERT INTO events VALUES (1,1,'A','2026-10-01T10:00','2026-10-01T10:00',15,'','u1');")
    conn.commit()
    conn.close()
    db.init_db(tmp.name)
    db.init_db(tmp.name)  # 幂等
    b = db.get_bundle(tmp.name, 1)
    assert "origin" not in b["events"][0]


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
