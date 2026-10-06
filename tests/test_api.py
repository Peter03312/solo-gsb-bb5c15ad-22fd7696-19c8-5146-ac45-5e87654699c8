"""HTTP API 端到端测试：python tests/test_api.py

覆盖：
- 保存携带间隔设置的“先于”关系并重新推算；
- 非法间隔（负数、非整数、最长等待短于最短等待）返回 400 且记录不变；
- base_version 过期返回 409，不覆盖较新版本；
- 版本回看/恢复保留每条关系当时的间隔设置；
- 间隔导致的约束冲突保留输入但不得定稿；
- 多路径上下界矛盾按时间窗冲突归因（不误报关系成环），网页与 API 结论一致。
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


def new_review_with_relation(relation):
    """新建复盘并保存 A→B 关系，返回 (review_id, 保存响应)。"""
    status, bundle = req("POST", "/api/reviews", {"title": "间隔复盘"})
    assert status == 201, bundle
    rid = bundle["review"]["id"]
    payload = {
        "base_version": 1,
        "title": "间隔复盘",
        "events": [ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
                   ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)],
        "relations": [relation],
    }
    return rid, req("PUT", "/api/reviews/{}".format(rid), payload)


def test_save_with_gaps_and_compute():
    rid, (status, bundle) = new_review_with_relation(rel("a", "b", min_g=10, max_g=60))
    assert status == 200, bundle
    r = bundle["relations"][0]
    assert r["min_gap_minutes"] == 10 and r["max_gap_minutes"] == 60
    res = bundle["computation"]["result"]
    assert res["status"] == "ok"
    win_b = res["windows"][str(r["successor_id"])]
    assert win_b["earliest_start"] == "2026-10-01T10:25"  # 10:00 + 15 + 10
    assert win_b["latest_start"] == "2026-10-01T11:45"    # 10:30 + 15 + 60
    assert "最短等待 10 分钟" in win_b["earliest_chain"][-1]["text"]


def test_relation_without_gaps_keeps_legacy_semantics():
    rid, (status, bundle) = new_review_with_relation(rel("a", "b"))
    assert status == 200, bundle
    r = bundle["relations"][0]
    assert r["min_gap_minutes"] == 0 and r["max_gap_minutes"] is None
    res = bundle["computation"]["result"]
    win_b = res["windows"][str(r["successor_id"])]
    assert win_b["earliest_start"] == "2026-10-01T10:15"  # 旧语义：10:00 + 15


def test_invalid_gaps_rejected_without_changes():
    rid, (status, bundle) = new_review_with_relation(rel("a", "b", min_g=10, max_g=60))
    assert status == 200
    base = bundle["review"]["version"]
    events = [ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
              ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)]

    def put(relation):
        return req("PUT", "/api/reviews/{}".format(rid), {
            "base_version": base, "title": "间隔复盘",
            "events": events, "relations": [relation]})

    bad_inputs = [
        rel("a", "b", min_g=30, max_g=10),   # 最长等待短于最短等待
        rel("a", "b", min_g=-1),             # 负数
        rel("a", "b", min_g="abc"),          # 非整数
        rel("a", "b", min_g=1.5),            # 非整数
        rel("a", "b", max_g=-5),             # 最长等待为负
    ]
    for relation in bad_inputs:
        status, resp = put(relation)
        assert status == 400, (relation, resp)
        assert "error" in resp
    # 记录未被改动：版本未推进，间隔仍是 10/60
    status, cur = req("GET", "/api/reviews/{}".format(rid))
    assert cur["review"]["version"] == base
    r = cur["relations"][0]
    assert r["min_gap_minutes"] == 10 and r["max_gap_minutes"] == 60


def test_stale_base_version_conflict():
    rid, (status, bundle) = new_review_with_relation(rel("a", "b", min_g=5))
    assert status == 200
    status, resp = req("PUT", "/api/reviews/{}".format(rid), {
        "base_version": 1, "title": "过期提交", "events": [], "relations": []})
    assert status == 409, resp
    assert resp["current_version"] == 2
    # 较新版本未被覆盖
    _, cur = req("GET", "/api/reviews/{}".format(rid))
    assert cur["review"]["title"] == "间隔复盘"
    assert len(cur["relations"]) == 1


def test_versions_and_restore_preserve_gaps():
    rid, (status, bundle) = new_review_with_relation(rel("a", "b", min_g=10, max_g=60))
    assert status == 200
    # v3：改成无间隔
    status, bundle = req("PUT", "/api/reviews/{}".format(rid), {
        "base_version": 2, "title": "间隔复盘",
        "events": [ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
                   ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)],
        "relations": [rel("a", "b")]})
    assert status == 200

    # 回看 v2：当时的间隔设置仍在
    status, v2 = req("GET", "/api/reviews/{}/versions/2".format(rid))
    assert status == 200, v2
    r = v2["input"]["relations"][0]
    assert r["min_gap_minutes"] == 10 and r["max_gap_minutes"] == 60

    # 恢复 v2：新草稿保留间隔；旧版本不被覆盖
    status, bundle = req("POST", "/api/reviews/{}/versions/2/restore".format(rid),
                         {"base_version": 3})
    assert status == 200, bundle
    assert bundle["review"]["version"] == 4
    r = bundle["relations"][0]
    assert r["min_gap_minutes"] == 10 and r["max_gap_minutes"] == 60
    status, v3 = req("GET", "/api/reviews/{}/versions/3".format(rid))
    assert v3["input"]["relations"][0]["max_gap_minutes"] is None

    # 恢复时 base_version 过期 → 409
    status, resp = req("POST", "/api/reviews/{}/versions/2/restore".format(rid),
                       {"base_version": 3})
    assert status == 409 and resp["current_version"] == 4


def test_gap_conflict_kept_but_not_finalizable():
    # 最长等待 10 分钟与输入范围冲突：B 最早 12:00，A 最晚结束 10:45 + 10 = 10:55
    status, bundle = req("POST", "/api/reviews", {"title": "冲突复盘"})
    rid = bundle["review"]["id"]
    status, bundle = req("PUT", "/api/reviews/{}".format(rid), {
        "base_version": 1, "title": "冲突复盘",
        "events": [ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:00", 15),
                   ev("b", "响应", "2026-10-01T12:00", "2026-10-01T13:00", 30)],
        "relations": [rel("a", "b", max_g=10)]})
    assert status == 200, bundle
    res = bundle["computation"]["result"]
    assert res["status"] == "conflict"
    assert res["conflicts"], res
    # 输入保留
    assert len(bundle["events"]) == 2 and len(bundle["relations"]) == 1
    # 不得定稿
    status, resp = req("POST", "/api/reviews/{}/finalize".format(rid),
                       {"base_version": bundle["review"]["version"]})
    assert status == 400, resp


def test_multi_path_bound_conflict_attribution():
    # 四个持续 0 分钟事件，A 固定 10:00，其余 10:00~12:00；
    # A→B 最短等待 60、B→D 无额外等待、A→C 与 C→D 最长等待各 10。
    # 关系图无有向环：推算应报告 D 的时间窗冲突（11:00 > 10:20），
    # 不得误报为“先于”关系成环；输入保留且不得定稿。
    status, bundle = req("POST", "/api/reviews", {"title": "多路径复盘"})
    assert status == 201
    rid = bundle["review"]["id"]
    status, bundle = req("PUT", "/api/reviews/{}".format(rid), {
        "base_version": 1, "title": "多路径复盘",
        "events": [
            ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:00", 0),
            ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 0),
            ev("c", "C", "2026-10-01T10:00", "2026-10-01T12:00", 0),
            ev("d", "D", "2026-10-01T10:00", "2026-10-01T12:00", 0),
        ],
        "relations": [
            rel("a", "b", min_g=60),
            rel("b", "d"),
            rel("a", "c", max_g=10),
            rel("c", "d", max_g=10),
        ]})
    assert status == 200, bundle
    res = bundle["computation"]["result"]
    assert res["status"] == "conflict"
    assert not any(c["type"] == "cycle" for c in res["conflicts"]), res["conflicts"]
    win = [c for c in res["conflicts"] if c["type"] == "window"]
    assert len(win) == 1, res["conflicts"]
    id_by_key = {e["name"]: e["id"] for e in bundle["events"]}
    assert win[0]["event_ids"] == [id_by_key["D"]]
    w_d = res["windows"][str(id_by_key["D"])]
    assert w_d["earliest_start"] == "2026-10-01T11:00"
    assert w_d["latest_start"] == "2026-10-01T10:20"
    assert win[0]["earliest_chain"] and win[0]["latest_chain"]
    # 输入保留；冲突版本不得定稿
    assert len(bundle["events"]) == 4 and len(bundle["relations"]) == 4
    status, resp = req("POST", "/api/reviews/{}/finalize".format(rid),
                       {"base_version": bundle["review"]["version"]})
    assert status == 400, resp
    # 版本回看展示同一结论（网页与 API 一致）
    status, v2 = req("GET", "/api/reviews/{}/versions/2".format(rid))
    assert status == 200
    assert v2["result"]["status"] == "conflict"
    assert not any(c["type"] == "cycle" for c in v2["result"]["conflicts"])


def test_draft_relation_skipped_with_reason():
    status, bundle = req("POST", "/api/reviews", {"title": "暂存复盘"})
    rid = bundle["review"]["id"]
    status, bundle = req("PUT", "/api/reviews/{}".format(rid), {
        "base_version": 1, "title": "暂存复盘",
        "events": [ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
                   ev("b", "待定", None, None, None)],
        "relations": [rel("a", "b", min_g=5)]})
    assert status == 200, bundle
    res = bundle["computation"]["result"]
    assert res["status"] == "ok"
    assert len(res["drafts"]) == 1
    assert len(res["skipped_relations"]) == 1
    s = res["skipped_relations"][0]
    assert "暂存" in s["reason"]
    assert s["min_gap_minutes"] == 5


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
