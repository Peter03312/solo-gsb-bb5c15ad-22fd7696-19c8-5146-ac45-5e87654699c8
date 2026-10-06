"""版本回看/恢复 测试：python tests/test_versions.py

覆盖：
- 保存/定稿均留存不可变输入快照（标题、事件、“先于”关系）；
- 恢复把旧版本输入另存为新草稿并重新推算，不覆盖旧版本；
- 恢复携带 base_version，过期沿用 409 语义；
- 冲突/暂存输入可以恢复，但不得定稿；
- 旧库缺少输入快照的历史版本明确标记为不可回看、不可恢复。
"""
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import db  # noqa: E402


def ev(key, name, es, ls, dur):
    return {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
            "duration_minutes": dur, "note": ""}


def rel(a, b, min_g=None, max_g=None):
    r = {"predecessor_key": a, "successor_key": b}
    if min_g is not None:
        r["min_gap_minutes"] = min_g
    if max_g is not None:
        r["max_gap_minutes"] = max_g
    return r


def setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def test_save_creates_immutable_snapshot():
    p = setup_db()
    rid = db.create_review(p, "初版")
    events = [ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
              ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)]
    rels = [{"predecessor_key": "a", "successor_key": "b"}]
    db.update_review(p, rid, 1, "二版标题", events, rels)

    detail = db.get_version(p, rid, 2)
    assert detail["title"] == "二版标题"
    assert detail["kind"] == "save" and detail["state"] == "draft"
    keys = [e["key"] for e in detail["input"]["events"]]
    # 快照事件 key 与推算结果窗口 id 可互相对应；未设置间隔的关系按默认值留存
    assert detail["input"]["relations"] == [
        {"predecessor_key": keys[0], "successor_key": keys[1],
         "min_gap_minutes": 0, "max_gap_minutes": None}]
    win_key = keys[1].replace("id_", "")
    assert detail["result"]["windows"][win_key]["earliest_start"] == "2026-10-01T10:15"

    vs = db.list_versions(p, rid)
    assert [v["version"] for v in vs] == [2, 1]
    assert all(v["reviewable"] and v["restorable"] for v in vs)


def test_finalize_records_finalized_version():
    p = setup_db()
    rid = db.create_review(p, "定稿版")
    events = [ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)]
    db.update_review(p, rid, 1, "定稿版", events, [])
    db.finalize_review(p, rid, 2)
    v3 = db.get_version(p, rid, 3)
    assert v3["kind"] == "finalize" and v3["state"] == "finalized"
    assert v3["source_version"] == 2
    assert v3["input"]["events"][0]["name"] == "A"


def test_restore_creates_new_draft_without_overwriting():
    p = setup_db()
    rid = db.create_review(p, "T")
    e1, e2 = (ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
              ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30))
    db.update_review(p, rid, 1, "T", [e1, e2],
                     [{"predecessor_key": "a", "successor_key": "b"}])  # v2
    e2b = dict(e2, latest_start="2026-10-01T11:00")
    db.update_review(p, rid, 2, "T-改", [e1, e2b],
                     [{"predecessor_key": "a", "successor_key": "b"}])  # v3
    db.restore_review(p, rid, 3, 2)                                      # v4

    bundle = db.get_bundle(p, rid)
    assert bundle["review"]["version"] == 4
    assert bundle["review"]["status"] == "draft"        # 恢复后始终是草稿
    assert bundle["events"][1]["latest_start"] == "2026-10-01T12:00"
    assert len(bundle["relations"]) == 1

    v4 = db.get_version(p, rid, 4)
    assert v4["kind"] == "restore" and v4["source_version"] == 2
    # 旧版本快照不被覆盖
    assert db.get_version(p, rid, 2)["input"]["events"][1]["latest_start"] == "2026-10-01T12:00"
    assert db.get_version(p, rid, 3)["input"]["events"][1]["latest_start"] == "2026-10-01T11:00"
    assert db.get_version(p, rid, 3)["title"] == "T-改"


def test_restore_stale_base_version_conflicts():
    p = setup_db()
    rid = db.create_review(p, "T")
    events = [ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)]
    db.update_review(p, rid, 1, "T", events, [])  # v2
    try:
        db.restore_review(p, rid, 1, 2)
        assert False, "应当抛 VersionConflict"
    except db.VersionConflict as e:
        assert e.current_version == 2


def test_conflict_and_draft_inputs_restore_but_not_finalize():
    p = setup_db()
    rid = db.create_review(p, "T")
    bad = ev("x", "X", "2026-10-01T11:00", "2026-10-01T10:00", 5)
    db.update_review(p, rid, 1, "冲突", [bad], [])  # v2 conflict
    db.restore_review(p, rid, 2, 2)                # v3 恢复冲突输入
    assert db.get_bundle(p, rid)["computation"]["status"] == "conflict"
    try:
        db.finalize_review(p, rid, 3)
        assert False, "冲突版本不得定稿"
    except db.FinalizeRejected:
        pass

    draft = ev("y", "Y", None, None, None)
    db.update_review(p, rid, 3, "暂存", [draft], [])  # v4 暂存
    db.restore_review(p, rid, 4, 4)                  # v5 恢复暂存输入
    try:
        db.finalize_review(p, rid, 5)
        assert False, "含暂存事件不得定稿"
    except db.FinalizeRejected:
        pass


def test_legacy_versions_marked_and_blocked():
    p = setup_db()
    rid = db.create_review(p, "T")
    events = [ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)]
    db.update_review(p, rid, 1, "T", events, [])  # v2

    # 模拟旧库：v1 只有 computations、没有 snapshots
    conn = sqlite3.connect(p)
    conn.execute("DELETE FROM snapshots WHERE review_id=? AND review_version=1", (rid,))
    conn.commit()
    conn.close()

    rows = {v["version"]: v for v in db.list_versions(p, rid)}
    assert rows[1]["reviewable"] is False and rows[1]["restorable"] is False
    assert "不可回看" in rows[1]["reason"]
    assert rows[2]["reviewable"] is True

    try:
        db.get_version(p, rid, 1)
        assert False, "旧版本不可回看"
    except db.LegacyVersion:
        pass
    try:
        db.restore_review(p, rid, 2, 1)
        assert False, "旧版本不可恢复"
    except db.LegacyVersion:
        pass

    # 版本号不存在
    try:
        db.get_version(p, rid, 99)
        assert False
    except db.VersionNotFound:
        pass


def test_legacy_db_still_starts_and_writes_new_snapshots():
    # 旧库（无 snapshots 表）启动 init_db 后，新保存产生的版本可回看，
    # 旧 computations 版本标记不可回看。
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(
        "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT, updated_at TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " name TEXT, earliest_start TEXT, latest_start TEXT, duration_minutes INTEGER,"
        " note TEXT DEFAULT '');"
        "CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " predecessor_id INTEGER, successor_id INTEGER);"
        "CREATE TABLE computations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " review_version INTEGER, status TEXT, result_json TEXT, computed_at TEXT);"
        "INSERT INTO reviews VALUES (1,'旧','draft',1,'t','t');"
        "INSERT INTO computations VALUES (1,1,1,'ok','{}','t');")
    conn.commit()
    conn.close()

    db.init_db(tmp.name)  # 迁移：补建 snapshots 表
    rows = db.list_versions(tmp.name, 1)
    assert rows[0]["version"] == 1 and rows[0]["reviewable"] is False
    rid = db.create_review(tmp.name, "新")
    assert db.get_version(tmp.name, rid, 1)["reviewable"] is True


def test_gaps_preserved_in_save_review_restore():
    p = setup_db()
    rid = db.create_review(p, "间隔")
    e1 = ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15)
    e2 = ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)
    db.update_review(p, rid, 1, "间隔", [e1, e2], [rel("a", "b", min_g=10, max_g=60)])  # v2
    db.update_review(p, rid, 2, "间隔", [e1, e2], [rel("a", "b", min_g=0)])            # v3

    # 保存时的快照保留当时间隔设置
    r2 = db.get_version(p, rid, 2)["input"]["relations"][0]
    assert r2["min_gap_minutes"] == 10 and r2["max_gap_minutes"] == 60
    r3 = db.get_version(p, rid, 3)["input"]["relations"][0]
    assert r3["min_gap_minutes"] == 0 and r3["max_gap_minutes"] is None
    # v2 的推算应用了间隔：响应最早开始 = 10:00 + 15 + 10
    keys = [e["key"] for e in db.get_version(p, rid, 2)["input"]["events"]]
    win = db.get_version(p, rid, 2)["result"]["windows"][keys[1].replace("id_", "")]
    assert win["earliest_start"] == "2026-10-01T10:25"

    # live 输入与恢复都保留间隔
    db.restore_review(p, rid, 3, 2)  # v4 恢复 v2
    live = db.get_bundle(p, rid)["relations"][0]
    assert live["min_gap_minutes"] == 10 and live["max_gap_minutes"] == 60
    r4 = db.get_version(p, rid, 4)["input"]["relations"][0]
    assert r4["min_gap_minutes"] == 10 and r4["max_gap_minutes"] == 60
    # 旧版本快照不被覆盖
    assert db.get_version(p, rid, 3)["input"]["relations"][0]["max_gap_minutes"] is None


def test_restore_legacy_snapshot_without_gaps():
    # 旧快照（无间隔字段）恢复后按旧语义推算：最短等待 0、最长等待不限
    p = setup_db()
    rid = db.create_review(p, "T")
    e1 = ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:30", 15)
    e2 = ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 30)
    db.update_review(p, rid, 1, "T", [e1, e2], [rel("a", "b")])  # v2
    # 模拟旧库快照：删掉间隔字段
    conn = sqlite3.connect(p)
    snap = conn.execute(
        "SELECT input_json FROM snapshots WHERE review_id=? AND review_version=2",
        (rid,)).fetchone()[0]
    import json as _json
    data = _json.loads(snap)
    for r in data["relations"]:
        r.pop("min_gap_minutes", None)
        r.pop("max_gap_minutes", None)
    conn.execute("UPDATE snapshots SET input_json=? WHERE review_id=? AND review_version=2",
                 (_json.dumps(data, ensure_ascii=False), rid))
    conn.commit()
    conn.close()

    db.restore_review(p, rid, 2, 2)  # v3
    live = db.get_bundle(p, rid)["relations"][0]
    assert live["min_gap_minutes"] == 0 and live["max_gap_minutes"] is None
    win_b = db.get_bundle(p, rid)["computation"]["result"]["windows"]
    assert win_b[str(live["successor_id"])]["earliest_start"] == "2026-10-01T10:15"


def test_legacy_relations_table_migrated_with_defaults():
    # 旧库（relations 无间隔列）启动迁移后：旧行按默认值保留，推算沿用旧语义
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(
        "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT, updated_at TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " name TEXT, earliest_start TEXT, latest_start TEXT, duration_minutes INTEGER,"
        " note TEXT DEFAULT '');"
        "CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " predecessor_id INTEGER, successor_id INTEGER);"
        "CREATE TABLE computations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " review_version INTEGER, status TEXT, result_json TEXT, computed_at TEXT);"
        "INSERT INTO reviews VALUES (1,'旧','draft',1,'t','t');"
        "INSERT INTO events VALUES (1,1,'A','2026-10-01T10:00','2026-10-01T10:30',15,'');"
        "INSERT INTO events VALUES (2,1,'B','2026-10-01T10:00','2026-10-01T12:00',30,'');"
        "INSERT INTO relations VALUES (1,1,1,2);")
    conn.commit()
    conn.close()

    db.init_db(tmp.name)  # 迁移：补间隔列
    bundle = db.get_bundle(tmp.name, 1)
    r = bundle["relations"][0]
    assert r["min_gap_minutes"] == 0 and r["max_gap_minutes"] is None
    # 新保存可携带间隔，且旧关系行不受影响
    e1 = ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:30", 15)
    e2 = ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 30)
    db.update_review(tmp.name, 1, 1, "旧", [e1, e2], [rel("a", "b", min_g=20)])
    live = db.get_bundle(tmp.name, 1)["relations"][0]
    assert live["min_gap_minutes"] == 20


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
