"""版本对照 测试：python tests/test_compare.py

覆盖：
- 跨版本按事件持续身份（uid）配对：改名、改时间、恢复后仍识别同一事件，
  绝不按名称或会重建的行号猜配（不回传 uid 时宁可报新增/删除）；
- 事件新增/删除/改名、输入时间范围与推算可行窗口变化；
- “先于”关系间隔变化、冲突与暂存状态变化；
- 对照只读：不改当前输入、版本号与定稿状态；
- 顺序无效 400、版本不存在 404、旧库缺快照 422；
- 有快照但缺可靠身份的旧版本：pairable=false，只展示各版本原貌；
- 保存/恢复沿用 base_version 过期 409 约定，既有接口行为不变。
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name

import db  # noqa: E402
import server  # noqa: E402

db.init_db(server.DB_PATH)
HTTPD = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
PORT = HTTPD.server_address[1]
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()


def req(method, path, body=None):
    url = "http://127.0.0.1:{}{}".format(PORT, path)
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def ev(key, name, es, ls, dur, uid=None):
    e = {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
         "duration_minutes": dur, "note": ""}
    if uid:
        e["uid"] = uid
    return e


def rel(a, b, min_g=None, max_g=None):
    r = {"predecessor_key": a, "successor_key": b}
    if min_g is not None:
        r["min_gap_minutes"] = min_g
    if max_g is not None:
        r["max_gap_minutes"] = max_g
    return r


def put(rid, base_version, title, events, relations):
    return req("PUT", "/api/reviews/{}".format(rid), {
        "base_version": base_version, "title": title,
        "events": events, "relations": relations})


def compare(rid, from_v, to_v):
    return req("GET", "/api/reviews/{}/compare?from={}&to={}".format(rid, from_v, to_v))


def uids_by_name(bundle):
    return {e["name"]: e["uid"] for e in bundle["events"]}


def test_identity_paired_across_rename_and_time_change():
    """改名 + 改时间范围后仍识别同一事件：renamed/changed，而非删除+新增。"""
    status, bundle = req("POST", "/api/reviews", {"title": "对照复盘"})
    rid = bundle["review"]["id"]
    status, bundle = put(rid, 1, "对照复盘", [
        ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ], [rel("a", "b", min_g=10, max_g=60)])
    assert status == 200, bundle
    uids = uids_by_name(bundle)

    # v3：告警改名并改时间；响应删除；新增“复盘会”
    status, bundle = put(rid, 2, "对照复盘", [
        ev("a", "告警触发", "2026-10-01T10:05", "2026-10-01T10:30", 15,
           uid=uids["告警"]),
        ev("c", "复盘会", "2026-10-01T13:00", "2026-10-01T14:00", 60),
    ], [])
    assert status == 200, bundle

    status, cmp = compare(rid, 2, 3)
    assert status == 200, cmp
    assert cmp["pairable"] is True
    assert cmp["events"]["renamed"] == [
        {"uid": uids["告警"], "from": "告警", "to": "告警触发"}]
    assert [e["name"] for e in cmp["events"]["added"]] == ["复盘会"]
    assert [e["name"] for e in cmp["events"]["removed"]] == ["响应"]
    # 改名事件的时间范围与推算窗口变化按同一身份呈现
    changed = {c["uid"]: c for c in cmp["events"]["changed"]}
    entry = changed[uids["告警"]]
    assert entry["name"] == {"from": "告警", "to": "告警触发"}
    assert entry["input_changes"]["earliest_start"] == {
        "from": "2026-10-01T10:00", "to": "2026-10-01T10:05"}
    assert entry["window"]["from"]["earliest_start"] == "2026-10-01T10:00"
    assert entry["window"]["to"]["earliest_start"] == "2026-10-01T10:05"
    assert entry["window_changes"]["earliest_start"] == {
        "from": "2026-10-01T10:00", "to": "2026-10-01T10:05"}
    s = cmp["summary"]
    assert (s["events_added"], s["events_removed"], s["events_renamed"]) == (1, 1, 1)
    # 关系 a→b 随事件 b 删除而消失
    assert len(cmp["relations"]["removed"]) == 1


def test_restore_keeps_identity_fully_unchanged():
    """恢复产生的版本与源版本对照：全部事件按原身份配对，无差值。"""
    status, bundle = req("POST", "/api/reviews", {"title": "恢复对照"})
    rid = bundle["review"]["id"]
    status, bundle = put(rid, 1, "恢复对照", [
        ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ], [rel("a", "b", min_g=10, max_g=60)])
    uids = uids_by_name(bundle)
    # v3：改名 + 去掉间隔（回传 uid，身份延续）
    status, bundle = put(rid, 2, "恢复对照", [
        ev("a", "告警改名", "2026-10-01T10:00", "2026-10-01T10:30", 15, uid=uids["告警"]),
        ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30, uid=uids["响应"]),
    ], [rel("a", "b")])
    assert status == 200
    # v4：恢复 v2
    status, bundle = req("POST", "/api/reviews/{}/versions/2/restore".format(rid),
                         {"base_version": 3})
    assert status == 200, bundle
    assert bundle["review"]["version"] == 4

    status, cmp = compare(rid, 2, 4)
    assert status == 200, cmp
    s = cmp["summary"]
    assert s["events_added"] == 0 and s["events_removed"] == 0
    assert s["events_renamed"] == 0 and s["events_changed"] == 0
    assert s["events_unchanged"] == 2
    assert s["relations_added"] == 0 and s["relations_removed"] == 0
    assert s["relations_changed"] == 0

    # v3 → v4（恢复 v2）：同一批事件身份可配对，改名被还原
    status, cmp = compare(rid, 3, 4)
    assert cmp["events"]["renamed"] == [
        {"uid": uids["告警"], "from": "告警改名", "to": "告警"}]
    assert cmp["events"]["added"] == [] and cmp["events"]["removed"] == []
    # 间隔设置恢复为 v2 的 10/60
    rc = cmp["relations"]["changed"]
    assert len(rc) == 1
    assert rc[0]["from"] == {"min_gap_minutes": 0, "max_gap_minutes": None}
    assert rc[0]["to"] == {"min_gap_minutes": 10, "max_gap_minutes": 60}


def test_relation_gap_changes():
    status, bundle = req("POST", "/api/reviews", {"title": "间隔对照"})
    rid = bundle["review"]["id"]
    status, bundle = put(rid, 1, "间隔对照", [
        ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ], [rel("a", "b", min_g=10, max_g=60)])
    uids = uids_by_name(bundle)
    status, bundle = put(rid, 2, "间隔对照", [
        ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15, uid=uids["告警"]),
        ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30, uid=uids["响应"]),
    ], [rel("a", "b", min_g=20)])
    assert status == 200

    status, cmp = compare(rid, 2, 3)
    assert cmp["relations"]["changed"] == [{
        "predecessor": {"uid": uids["告警"], "name": "告警"},
        "successor": {"uid": uids["响应"], "name": "响应"},
        "from": {"min_gap_minutes": 10, "max_gap_minutes": 60},
        "to": {"min_gap_minutes": 20, "max_gap_minutes": None},
    }]
    # 间隔变化引起窗口变化：响应最早开始 10:25 → 10:35
    changed = {c["uid"]: c for c in cmp["events"]["changed"]}
    assert changed[uids["响应"]]["window_changes"]["earliest_start"] == {
        "from": "2026-10-01T10:25", "to": "2026-10-01T10:35"}


def test_conflict_and_draft_status_changes():
    status, bundle = req("POST", "/api/reviews", {"title": "状态对照"})
    rid = bundle["review"]["id"]
    status, bundle = put(rid, 1, "状态对照", [
        ev("a", "正常", "2026-10-01T10:00", "2026-10-01T11:00", 10),
        ev("b", "待定", "2026-10-01T10:00", "2026-10-01T11:00", 10),
    ], [])
    uids = uids_by_name(bundle)
    # v3：a 输入范围自相矛盾（冲突），b 清空时间变为暂存
    status, bundle = put(rid, 2, "状态对照", [
        ev("a", "正常", "2026-10-01T11:00", "2026-10-01T10:00", 10, uid=uids["正常"]),
        ev("b", "待定", None, None, None, uid=uids["待定"]),
    ], [])
    assert status == 200
    assert bundle["computation"]["status"] == "conflict"

    status, cmp = compare(rid, 2, 3)
    assert cmp["conflicts"]["from"] == []
    assert len(cmp["conflicts"]["to"]) == 1
    c = cmp["conflicts"]["to"][0]
    assert c["type"] == "invalid_range"
    assert c["events"] == [{"uid": uids["正常"], "name": "正常"}]
    d = cmp["drafts"]
    assert (d["from_count"], d["to_count"]) == (0, 1)
    assert d["became"] == [{"uid": uids["待定"], "name": "待定"}]
    assert d["resolved"] == [] and d["remaining"] == []
    # 暂存事件无推算窗口
    changed = {c["uid"]: c for c in cmp["events"]["changed"]}
    assert changed[uids["待定"]]["draft"] == {"from": False, "to": True}
    assert changed[uids["待定"]]["window"]["to"] is None


def test_compare_is_readonly():
    status, bundle = req("POST", "/api/reviews", {"title": "只读对照"})
    rid = bundle["review"]["id"]
    status, bundle = put(rid, 1, "只读对照", [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)], [])
    uids = uids_by_name(bundle)
    status, bundle = put(rid, 2, "只读对照", [
        ev("a", "A2", "2026-10-01T10:00", "2026-10-01T11:00", 10, uid=uids["A"])], [])
    status, before = req("GET", "/api/reviews/{}".format(rid))

    status, cmp = compare(rid, 2, 3)
    assert status == 200
    status, after = req("GET", "/api/reviews/{}".format(rid))
    # 当前输入、版本号、定稿状态与推算结果均未改变
    assert before == after
    assert after["review"]["version"] == 3
    assert after["review"]["status"] == "draft"


def test_compare_errors():
    status, bundle = req("POST", "/api/reviews", {"title": "错误对照"})
    rid = bundle["review"]["id"]
    status, bundle = put(rid, 1, "错误对照", [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)], [])

    # 顺序无效：from >= to
    status, resp = compare(rid, 2, 2)
    assert status == 400 and "error" in resp, resp
    status, resp = compare(rid, 3, 1)
    assert status == 400 and "error" in resp, resp
    # 参数缺失/非法
    status, resp = req("GET", "/api/reviews/{}/compare?from=1".format(rid))
    assert status == 400 and "error" in resp
    status, resp = req("GET", "/api/reviews/{}/compare?from=1&to=abc".format(rid))
    assert status == 400 and "error" in resp
    # 版本不存在
    status, resp = compare(rid, 1, 99)
    assert status == 404 and "error" in resp
    # 复盘不存在
    status, resp = compare(99999, 1, 2)
    assert status == 404 and "error" in resp

    # 旧库缺快照版本：明确说明无法比较（422）
    conn = sqlite3.connect(server.DB_PATH)
    conn.execute("DELETE FROM snapshots WHERE review_id=? AND review_version=1", (rid,))
    conn.commit()
    conn.close()
    status, resp = compare(rid, 1, 2)
    assert status == 422, resp
    assert "无法比较" in resp["error"]
    status, resp = compare(rid, 2, 1)
    assert status == 400  # 顺序无效优先于快照检查


def test_unpairable_snapshot_shows_raw_versions_only():
    """有快照但缺可靠身份（旧格式快照）：只展示各版本原貌，不虚构差值。"""
    status, bundle = req("POST", "/api/reviews", {"title": "旧快照对照"})
    rid = bundle["review"]["id"]
    status, bundle = put(rid, 1, "旧快照对照", [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)], [])
    uids = uids_by_name(bundle)
    status, bundle = put(rid, 2, "旧快照对照", [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T12:00", 10, uid=uids["A"])], [])
    assert status == 200

    # 模拟本轮升级前保存的快照：input_json 中事件没有 uid
    conn = sqlite3.connect(server.DB_PATH)
    for ver in (2, 3):
        raw = conn.execute(
            "SELECT input_json FROM snapshots WHERE review_id=? AND review_version=?",
            (rid, ver)).fetchone()[0]
        data = json.loads(raw)
        for e in data["events"]:
            e.pop("uid", None)
        conn.execute(
            "UPDATE snapshots SET input_json=? WHERE review_id=? AND review_version=?",
            (json.dumps(data, ensure_ascii=False), rid, ver))
    conn.commit()
    conn.close()

    status, cmp = compare(rid, 2, 3)
    assert status == 200, cmp
    assert cmp["pairable"] is False
    assert "身份" in cmp["reason"]
    assert "events" not in cmp and "relations" not in cmp  # 不虚构逐事件差值
    # 各版本原貌完整呈现
    assert cmp["from"]["input"]["events"][0]["name"] == "A"
    assert cmp["from"]["input"]["events"][0]["latest_start"] == "2026-10-01T11:00"
    assert cmp["to"]["input"]["events"][0]["latest_start"] == "2026-10-01T12:00"
    assert cmp["from"]["result"]["status"] == "ok"
    assert cmp["to"]["result"]["windows"]


def test_no_uid_echo_means_no_guessing():
    """客户端不回传 uid：即使名称完全相同也绝不猜配，按删除+新增呈现。"""
    status, bundle = req("POST", "/api/reviews", {"title": "不猜配"})
    rid = bundle["review"]["id"]
    status, bundle = put(rid, 1, "不猜配", [
        ev("a", "同名", "2026-10-01T10:00", "2026-10-01T11:00", 10)], [])
    assert status == 200
    # v3：同名称同时间，但不回传 uid
    status, bundle = put(rid, 2, "不猜配", [
        ev("a", "同名", "2026-10-01T10:00", "2026-10-01T11:00", 10)], [])
    assert status == 200

    status, cmp = compare(rid, 2, 3)
    assert cmp["pairable"] is True
    assert cmp["events"]["renamed"] == [] and cmp["events"]["unchanged"] == []
    assert [e["name"] for e in cmp["events"]["added"]] == ["同名"]
    assert [e["name"] for e in cmp["events"]["removed"]] == ["同名"]


def test_save_restore_still_enforce_base_version_409():
    """既有约定不变：保存与恢复携带过期 base_version 仍返回 409。"""
    status, bundle = req("POST", "/api/reviews", {"title": "409 对照"})
    rid = bundle["review"]["id"]
    status, bundle = put(rid, 1, "409 对照", [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)], [])
    assert status == 200
    status, resp = put(rid, 1, "过期保存", [], [])
    assert status == 409 and resp["current_version"] == 2
    status, resp = req("POST", "/api/reviews/{}/versions/2/restore".format(rid),
                       {"base_version": 1})
    assert status == 409 and resp["current_version"] == 2
    # 对照不影响版本号
    status, cmp = compare(rid, 1, 2)
    assert status == 200
    status, cur = req("GET", "/api/reviews/{}".format(rid))
    assert cur["review"]["version"] == 2


# ---------------------------------------------------------------- db 层

def _setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def test_db_uid_stable_across_saves_and_restore():
    p = _setup_db()
    rid = db.create_review(p, "身份")
    e1 = ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15)
    e2 = ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)
    db.update_review(p, rid, 1, "身份", [e1, e2], [])  # v2
    live = db.get_bundle(p, rid)["events"]
    uid_a = [e for e in live if e["name"] == "告警"][0]["uid"]
    assert uid_a

    # 回传 uid 改名保存：身份不变
    e1b = ev("a", "告警改名", "2026-10-01T10:00", "2026-10-01T10:30", 15, uid=uid_a)
    uid_b = [e for e in live if e["name"] == "响应"][0]["uid"]
    e2b = ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30, uid=uid_b)
    db.update_review(p, rid, 2, "身份", [e1b, e2b], [])  # v3
    live2 = db.get_bundle(p, rid)["events"]
    assert [e for e in live2 if e["name"] == "告警改名"][0]["uid"] == uid_a

    # 伪造/未知 uid 不被沿用
    e1c = ev("a", "告警改名", "2026-10-01T10:00", "2026-10-01T10:30", 15,
             uid="forged-uid")
    db.update_review(p, rid, 3, "身份", [e1c, e2b], [])  # v4
    live3 = db.get_bundle(p, rid)["events"]
    assert [e for e in live3 if e["name"] == "告警改名"][0]["uid"] != "forged-uid"

    # 恢复沿用快照身份：v5 恢复 v2，告警 uid 与 v2 相同
    db.restore_review(p, rid, 4, 2)  # v5
    live4 = db.get_bundle(p, rid)["events"]
    assert [e for e in live4 if e["name"] == "告警"][0]["uid"] == uid_a
    snap5 = db.get_version(p, rid, 5)["input"]["events"]
    assert [e for e in snap5 if e["name"] == "告警"][0]["uid"] == uid_a


def test_db_compare_order_and_legacy():
    p = _setup_db()
    rid = db.create_review(p, "顺序")
    db.update_review(p, rid, 1, "顺序",
                     [ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)], [])
    try:
        db.compare_versions(p, rid, 2, 2)
        assert False, "应当抛 InvalidCompareOrder"
    except db.InvalidCompareOrder:
        pass
    try:
        db.compare_versions(p, rid, 1, 99)
        assert False, "应当抛 VersionNotFound"
    except db.VersionNotFound:
        pass
    conn = sqlite3.connect(p)
    conn.execute("DELETE FROM snapshots WHERE review_id=? AND review_version=1", (rid,))
    conn.commit()
    conn.close()
    try:
        db.compare_versions(p, rid, 1, 2)
        assert False, "应当抛 LegacyVersion"
    except db.LegacyVersion:
        pass


def test_db_legacy_events_get_uid_on_migrate():
    """旧库 events 行在启动迁移时补发 uid，此后保存即可跨版本配对。"""
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
        "INSERT INTO events VALUES (1,1,'A','2026-10-01T10:00','2026-10-01T11:00',10,'');")
    conn.commit()
    conn.close()

    db.init_db(tmp.name)  # 迁移：补 uid 列并给旧行发 uid
    bundle = db.get_bundle(tmp.name, 1)
    assert bundle["events"][0]["uid"]
    # 旧行 uid 在下次保存时经客户端回传即可延续身份
    e = ev("id_1", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10,
           uid=bundle["events"][0]["uid"])
    db.update_review(tmp.name, 1, 1, "旧", [e], [])
    assert db.get_bundle(tmp.name, 1)["events"][0]["uid"] == bundle["events"][0]["uid"]


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
    HTTPD.shutdown()
    sys.exit(1 if failed else 0)
