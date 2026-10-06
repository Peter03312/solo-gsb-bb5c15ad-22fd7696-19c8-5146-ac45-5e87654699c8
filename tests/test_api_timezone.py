"""跨地区时区特性 HTTP API 端到端测试：python tests/test_api_timezone.py

覆盖：
- 带时区事件保存成功，推算窗口按各事件当地时间 + 偏移展示；
- 夏令时不存在的当地时刻、错误偏移、缺偏移整次保存拒绝（400，记录不变）；
- 未指定时区的事件携带偏移被拒绝（旧语义不被静默改变）；
- 非法 IANA 时区名拒绝；
- 带时区与未带时区事件之间建立关系明确报错（400）；
- 保存/版本回看/恢复/对照均保留时区与偏移，不改写旧快照；
- 冲突、暂存、定稿与过期版本 409 行为在带时区场景保持不变；
- 未带时区的旧输入/输出语义完全不变。
"""
import json
import os
import pathlib
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name
os.environ["PORT"] = "0"

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


def ev(key, name, es, ls, dur, tz=None):
    e = {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
         "duration_minutes": dur, "note": ""}
    if tz is not None:
        e["timezone"] = tz
    return e


def new_review(title="跨地区复盘"):
    status, bundle = req("POST", "/api/reviews", {"title": title})
    assert status == 201, bundle
    return bundle["review"]["id"]


def win_by_name(bundle, name):
    eid = next(e["id"] for e in bundle["events"] if e["name"] == name)
    return bundle["events"], bundle["computation"]["result"]["windows"][str(eid)]


def test_save_aware_events_and_local_windows():
    rid = new_review()
    payload = {
        "base_version": 1, "title": "跨地区复盘",
        "events": [
            ev("a", "上海告警", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00",
               15, "Asia/Shanghai"),
            ev("b", "纽约响应", "2026-09-30T20:00-04:00", "2026-10-01T12:00-04:00",
               30, "America/New_York"),
        ],
        "relations": [{"predecessor_key": "a", "successor_key": "b"}],
    }
    status, bundle = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 200, bundle
    events, w = win_by_name(bundle, "纽约响应")
    assert w["timezone"] == "America/New_York"
    # 上海 10:00+08 + 15 分钟 = 02:15 UTC = 纽约 09-30 22:15-04
    assert w["earliest_start"] == "2026-09-30T22:15-04:00", w
    # 事件行持久化时区与偏移
    a = next(e for e in events if e["name"] == "上海告警")
    assert a["timezone"] == "Asia/Shanghai"
    assert a["earliest_start"] == "2026-10-01T10:00+08:00"


def test_nonexistent_local_time_rejected_entire_save():
    rid = new_review()
    payload = {
        "base_version": 1, "title": "t",
        "events": [ev("b", "纽约", "2026-03-08T02:30-05:00", "2026-03-08T12:00-05:00",
                      5, "America/New_York")],
        "relations": [],
    }
    status, body = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 400, body
    assert "不存在" in body["error"], body
    # 整次保存拒绝：仍是初始空版本
    assert db.current_version(server.DB_PATH, rid) == 1


def test_wrong_offset_rejected():
    rid = new_review()
    # 7 月纽约是夏令时 -04:00，给出 -05:00
    payload = {
        "base_version": 1, "title": "t",
        "events": [ev("b", "纽约", "2026-07-01T21:00-05:00", "2026-07-02T12:00-04:00",
                      5, "America/New_York")],
        "relations": [],
    }
    status, body = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 400, body
    assert "不符合时区" in body["error"]


def test_dst_repeated_moment_both_offsets_accepted():
    rid = new_review()
    ver = 1
    for off in ("-04:00", "-05:00"):
        payload = {
            "base_version": ver, "title": "t",
            "events": [ev("b", "纽约", "2026-11-01T01:30" + off,
                          "2026-11-01T12:00-05:00", 5, "America/New_York")],
            "relations": [],
        }
        status, bundle = req("PUT", "/api/reviews/{}".format(rid), payload)
        assert status == 200, (off, bundle)
        ver = bundle["review"]["version"]
        saved = bundle["events"][0]
        assert saved["earliest_start"] == "2026-11-01T01:30" + off


def test_aware_time_without_offset_rejected():
    rid = new_review()
    payload = {
        "base_version": 1, "title": "t",
        "events": [ev("b", "上海", "2026-10-01T10:00", "2026-10-01T11:00",
                      5, "Asia/Shanghai")],
        "relations": [],
    }
    status, body = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 400, body
    assert "必须携带 UTC 偏移" in body["error"]


def test_naive_event_with_offset_rejected():
    rid = new_review()
    payload = {
        "base_version": 1, "title": "t",
        "events": [ev("b", "朴素", "2026-10-01T10:00+08:00", "2026-10-01T11:00+08:00",
                      5)],
        "relations": [],
    }
    status, body = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 400, body
    assert "未指定时区" in body["error"]


def test_bad_timezone_name_rejected():
    rid = new_review()
    payload = {
        "base_version": 1, "title": "t",
        "events": [ev("b", "X", "2026-10-01T10:00+09:00", "2026-10-01T11:00+09:00",
                      5, "Asia/Not_A_Zone")],
        "relations": [],
    }
    status, body = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 400, body
    assert "IANA" in body["error"]


def test_mixed_kind_relation_rejected():
    rid = new_review()
    payload = {
        "base_version": 1, "title": "t",
        "events": [
            ev("a", "带时区", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00",
               5, "Asia/Shanghai"),
            ev("b", "朴素", "2026-10-01T10:00", "2026-10-01T12:00", 5),
        ],
        "relations": [{"predecessor_key": "a", "successor_key": "b"}],
    }
    status, body = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 400, body
    assert "不得建立" in body["error"]
    assert db.current_version(server.DB_PATH, rid) == 1


def test_versions_restore_compare_preserve_timezone():
    rid = new_review()
    payload = {
        "base_version": 1, "title": "跨地区",
        "events": [
            ev("a", "上海告警", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00",
               15, "Asia/Shanghai"),
            ev("b", "纽约响应", "2026-09-30T20:00-04:00", "2026-10-01T12:00-04:00",
               30, "America/New_York"),
        ],
        "relations": [{"predecessor_key": "a", "successor_key": "b"}],
    }
    status, bundle = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 200, bundle
    v2 = bundle["review"]["version"]

    # 回看：旧快照原样保留时区与偏移
    status, detail = req("GET", "/api/reviews/{}/versions/{}".format(rid, v2))
    assert status == 200, detail
    snap = {e["name"]: e for e in detail["input"]["events"]}
    assert snap["纽约响应"]["timezone"] == "America/New_York"
    assert snap["纽约响应"]["earliest_start"] == "2026-09-30T20:00-04:00"
    # 当时推算结果也带时区
    win = detail["result"]["windows"]
    b_row_id = snap["纽约响应"]["key"].replace("id_", "")
    assert win[b_row_id]["earliest_start"] == "2026-09-30T22:15-04:00"

    # 恢复为新草稿：时区/偏移保留，旧版本不被覆盖
    status, restored = req(
        "POST", "/api/reviews/{}/versions/{}/restore".format(rid, v2),
        {"base_version": v2})
    assert status == 200, restored
    v3 = restored["review"]["version"]
    assert v3 == v2 + 1
    tz_by_name = {e["name"]: e["timezone"] for e in restored["events"]}
    assert tz_by_name == {"上海告警": "Asia/Shanghai", "纽约响应": "America/New_York"}

    # 再保存一版把纽约改到洛杉矶（回传 uid），对照应捕获时区变化
    uid_a = next(e["uid"] for e in restored["events"] if e["name"] == "上海告警")
    uid_b = next(e["uid"] for e in restored["events"] if e["name"] == "纽约响应")
    payload2 = {
        "base_version": v3, "title": "跨地区",
        "events": [
            {"key": "a", "uid": uid_a, "name": "上海告警",
             "timezone": "Asia/Shanghai",
             "earliest_start": "2026-10-01T10:00+08:00",
             "latest_start": "2026-10-01T10:00+08:00", "duration_minutes": 15},
            {"key": "b", "uid": uid_b, "name": "纽约响应",
             "timezone": "America/Los_Angeles",
             "earliest_start": "2026-09-30T19:00-07:00",
             "latest_start": "2026-10-01T09:00-07:00", "duration_minutes": 30},
        ],
        "relations": [{"predecessor_key": "a", "successor_key": "b"}],
    }
    status, bundle4 = req("PUT", "/api/reviews/{}".format(rid), payload2)
    assert status == 200, bundle4
    v4 = bundle4["review"]["version"]

    status, cmp = req("GET", "/api/reviews/{}/compare?from={}&to={}".format(
        rid, v3, v4))
    assert status == 200, cmp
    changed = cmp["events"]["changed"]
    fields = set()
    for c in changed:
        fields |= set(c["input_changes"].keys())
    assert "timezone" in fields, fields

    # 旧快照（v2）不被改写
    status, detail2 = req("GET", "/api/reviews/{}/versions/{}".format(rid, v2))
    snap2 = {e["name"]: e for e in detail2["input"]["events"]}
    assert snap2["纽约响应"]["timezone"] == "America/New_York"


def test_aware_conflict_not_finalizable():
    rid = new_review()
    # A 固定 10:00+08（02:00 UTC），B 当地窗口早于约束推出的最早界 ⇒ 冲突
    payload = {
        "base_version": 1, "title": "t",
        "events": [
            ev("a", "A", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00",
               60, "Asia/Shanghai"),
            ev("b", "B", "2026-10-01T09:00+08:00", "2026-10-01T09:30+08:00",
               5, "Asia/Shanghai"),
        ],
        "relations": [{"predecessor_key": "a", "successor_key": "b"}],
    }
    status, bundle = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 200, bundle
    assert bundle["computation"]["status"] == "conflict"
    # 冲突输入保留但不得定稿
    status, body = req("POST", "/api/reviews/{}/finalize".format(rid),
                       {"base_version": bundle["review"]["version"]})
    assert status == 400, body


def test_stale_base_version_still_409_with_timezone():
    rid = new_review()
    p = {
        "base_version": 1, "title": "t",
        "events": [ev("a", "A", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00",
                      5, "Asia/Shanghai")],
        "relations": [],
    }
    status, b1 = req("PUT", "/api/reviews/{}".format(rid), p)
    assert status == 200, b1
    # 用过期 base_version=1 再保存
    status, body = req("PUT", "/api/reviews/{}".format(rid), p)
    assert status == 409, body
    assert body["current_version"] == b1["review"]["version"]


def test_naive_payloads_unchanged_semantics():
    rid = new_review("朴素复盘")
    payload = {
        "base_version": 1, "title": "朴素复盘",
        "events": [
            ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
            ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
        ],
        "relations": [{"predecessor_key": "a", "successor_key": "b"}],
    }
    status, bundle = req("PUT", "/api/reviews/{}".format(rid), payload)
    assert status == 200, bundle
    for e in bundle["events"]:
        assert not e.get("timezone")
    _, w = win_by_name(bundle, "响应")
    assert w["earliest_start"] == "2026-10-01T10:15"
    assert w.get("timezone") is None


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
