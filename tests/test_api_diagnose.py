"""冲突最小约束分析 HTTP API 端到端测试：python tests/test_api_diagnose.py

覆盖：
- GET /api/reviews/{id}/versions/{v}/diagnosis 返回极小冲突约束组
  （invalid_range / cycle / window），每组去掉任一条后可满足；
- 无冲突版本返回空组；同一版本重复查询结果稳定；
- 版本不存在 404；旧库缺快照版本明确拒绝 422；
- 分析只读：不改输入、版本号与定稿状态；
- 暂存事件与被跳过关系不参与分析并给出排除原因；
- 带时区约束按绝对分钟判定，展示保留当地时间与偏移；
- 现有保存、回看、恢复、定稿、对照、并入接口行为保持不变。
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
os.environ["PORT"] = "0"

import db  # noqa: E402
import diagnose  # noqa: E402
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


def rel(a, b, mn=None, mx=None):
    r = {"predecessor_key": a, "successor_key": b}
    if mn is not None:
        r["min_gap_minutes"] = mn
    if mx is not None:
        r["max_gap_minutes"] = mx
    return r


def save_review(title, events, relations):
    status, bundle = req("POST", "/api/reviews", {"title": title})
    assert status == 201, bundle
    rid = bundle["review"]["id"]
    status, bundle = req("PUT", "/api/reviews/{}".format(rid), {
        "base_version": 1, "title": title, "events": events, "relations": relations})
    assert status == 200, bundle
    return rid, bundle["review"]["version"]


def assert_groups_minimal(payload, rid, ver):
    """端到端响应中的每组均整组不可满足、删任意一条可满足。

    约束 id 基于该版本不可变快照的事件 key（id_<行id>），因此直接取
    服务器快照重建约束图复核，与响应使用同一份不可变输入。
    """
    snap = db.get_version(server.DB_PATH, rid, ver)["input"]
    events, relations = snap["events"], snap["relations"]
    out = diagnose.analyze_input(events, relations)
    got = payload["analysis"]["groups"]
    assert [g["kind"] for g in got] == [g["kind"] for g in out["groups"]]
    drafts = [{"key": d["key"]} for d in out["drafts"]]
    nodes, edges = diagnose._build_graph(events, relations, drafts)
    by, n = {e["id"]: e for e in edges}, len(nodes)
    for g in got:
        ids = [c["id"] for c in g["constraints"]]
        assert not diagnose._feasible([by[i] for i in ids], n)
        for i in ids:
            assert diagnose._feasible([by[k] for k in ids if k != i], n), i


# ------------------------------------------------------------------ 路由

def test_diagnosis_invalid_range():
    events = [ev("a", "A", "2026-10-01T11:00", "2026-10-01T10:00", 10)]
    rid, ver = save_review("矛盾范围", events, [])
    status, payload = req("GET", f"/api/reviews/{rid}/versions/{ver}/diagnosis")
    assert status == 200, payload
    assert payload["version"] == ver and payload["read_only"] is True
    (g,) = payload["analysis"]["groups"]
    assert g["kind"] == "invalid_range"
    assert g["contradiction_minutes"] == 60
    assert {c["type"] for c in g["constraints"]} == {"earliest_bound", "latest_bound"}
    assert_groups_minimal(payload, rid, ver)


def test_diagnosis_cycle():
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T12:00", 30),
        ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    relations = [rel("a", "b"), rel("b", "a")]
    rid, ver = save_review("成环", events, relations)
    status, payload = req("GET", f"/api/reviews/{rid}/versions/{ver}/diagnosis")
    assert status == 200
    (g,) = payload["analysis"]["groups"]
    assert g["kind"] == "cycle"
    assert all(c["type"] == "min_wait" for c in g["constraints"])
    assert {e["name"] for e in g["events"]} == {"A", "B"}
    assert_groups_minimal(payload, rid, ver)


def test_diagnosis_window_multi_path_with_gaps_and_duration():
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:00", 0),
        ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 0),
        ev("c", "C", "2026-10-01T10:00", "2026-10-01T12:00", 0),
        ev("d", "D", "2026-10-01T10:00", "2026-10-01T12:00", 0),
    ]
    relations = [rel("a", "b", mn=60), rel("b", "d"),
                 rel("a", "c", mx=10), rel("c", "d", mx=10)]
    rid, ver = save_review("多路径矛盾", events, relations)
    status, payload = req("GET", f"/api/reviews/{rid}/versions/{ver}/diagnosis")
    assert status == 200
    (g,) = payload["analysis"]["groups"]
    assert g["kind"] == "window" and g["contradiction_minutes"] == 40
    # 每条等待约束都显式携带前项持续与等待增量
    for c in g["constraints"]:
        if c["type"] in ("min_wait", "max_wait"):
            assert "predecessor_duration_minutes" in c and "delta_minutes" in c
            assert "持续" in c["text"]
    assert_groups_minimal(payload, rid, ver)


def test_diagnosis_empty_group_when_ok():
    events = [
        ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    rid, ver = save_review("无冲突", events, [rel("a", "b")])
    status, payload = req("GET", f"/api/reviews/{rid}/versions/{ver}/diagnosis")
    assert status == 200
    assert payload["analysis"]["status"] == "ok"
    assert payload["analysis"]["groups"] == []


def test_repeated_query_stable():
    events = [
        ev("a", "A", "2026-10-01T10:00", "2026-10-01T10:10", 60),
        ev("b", "B", "2026-10-01T10:00", "2026-10-01T10:30", 5),
    ]
    rid, ver = save_review("稳定性", events, [rel("a", "b")])
    _, p1 = req("GET", f"/api/reviews/{rid}/versions/{ver}/diagnosis")
    _, p2 = req("GET", f"/api/reviews/{rid}/versions/{ver}/diagnosis")
    norm = lambda o: json.dumps(o, ensure_ascii=False, sort_keys=True)
    assert norm(p1) == norm(p2)


def test_diagnosis_is_read_only():
    events = [ev("a", "A", "2026-10-01T11:00", "2026-10-01T10:00", 10)]
    rid, ver = save_review("只读", events, [])
    before = req("GET", f"/api/reviews/{rid}")[1]
    status, _ = req("GET", f"/api/reviews/{rid}/versions/{ver}/diagnosis")
    assert status == 200
    after = req("GET", f"/api/reviews/{rid}")[1]
    assert after["review"]["version"] == before["review"]["version"]
    assert after["review"]["status"] == before["review"]["status"]
    assert after["events"] == before["events"]
    # 冲突版本仍不得定稿
    st, resp = req("POST", f"/api/reviews/{rid}/finalize", {"base_version": ver})
    assert st == 400 and "冲突" in resp["error"]


def test_version_not_found_404():
    status, bundle = req("POST", "/api/reviews", {"title": "404"})
    rid = bundle["review"]["id"]
    status, payload = req("GET", f"/api/reviews/{rid}/versions/999/diagnosis")
    assert status == 404 and payload["error"] == "该版本不存在"
    status, payload = req("GET", "/api/reviews/99999/versions/1/diagnosis")
    assert status == 404 and payload["error"] == "复盘不存在"


def test_legacy_version_without_snapshot_422():
    events = [ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)]
    rid, _ver = save_review("旧库", events, [])
    # v1 是 create_review 产生的空版本快照：模拟旧库删掉 v1 快照
    conn = sqlite3.connect(server.DB_PATH)
    conn.execute("DELETE FROM snapshots WHERE review_id=? AND review_version=1", (rid,))
    conn.commit()
    conn.close()
    status, payload = req("GET", f"/api/reviews/{rid}/versions/1/diagnosis")
    assert status == 422
    assert payload["analyzable"] is False and "快照" in payload["error"]
    # 有快照的 v2 仍可分析
    status, payload = req("GET", f"/api/reviews/{rid}/versions/2/diagnosis")
    assert status == 200


def test_drafts_and_skipped_relations_excluded_with_reasons():
    events = [
        ev("a", "完整", "2026-10-01T10:00", "2026-10-01T11:00", 10),
        {"key": "b", "name": "暂存", "earliest_start": None, "latest_start": None,
         "duration_minutes": 10, "note": ""},
    ]
    rid, ver = save_review("暂存分析", events, [rel("a", "b", mn=5, mx=30)])
    status, payload = req("GET", f"/api/reviews/{rid}/versions/{ver}/diagnosis")
    assert status == 200
    a = payload["analysis"]
    assert a["groups"] == []
    assert [d["name"] for d in a["drafts"]] == ["暂存"]
    assert "暂存" in a["drafts"][0]["reason"]
    (x,) = a["excluded_relations"]
    assert x["min_gap_minutes"] == 5 and x["max_gap_minutes"] == 30
    assert "暂存" in x["reason"]
    assert x["predecessor"]["name"] == "完整" and x["successor"]["name"] == "暂存"


def test_timezone_conflict_absolute_minutes_local_display():
    events = [
        ev("a", "上海告警", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00",
           15, "Asia/Shanghai"),
        ev("b", "纽约响应", "2026-09-30T21:00-04:00", "2026-09-30T21:30-04:00",
           30, "America/New_York"),
    ]
    rid, ver = save_review("跨时区冲突", events, [rel("a", "b")])
    status, payload = req("GET", f"/api/reviews/{rid}/versions/{ver}/diagnosis")
    assert status == 200
    (g,) = payload["analysis"]["groups"]
    assert g["kind"] == "window" and g["contradiction_minutes"] == 45
    locals_ = {c.get("local_time"): c for c in g["constraints"] if c.get("local_time")}
    assert locals_["2026-10-01T10:00+08:00"]["event"]["timezone"] == "Asia/Shanghai"
    assert locals_["2026-09-30T21:30-04:00"]["event"]["timezone"] == "America/New_York"
    assert_groups_minimal(payload, rid, ver)


def test_existing_flows_still_work_after_diagnosis():
    # 保存 → 回看 → 分析 → 再保存 → 恢复 → 对照 全链路可用
    rid = req("POST", "/api/reviews", {"title": "全链路"})[1]["review"]["id"]
    e1 = [ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)]
    _, b2 = req("PUT", f"/api/reviews/{rid}",
                {"base_version": 1, "title": "全链路", "events": e1, "relations": []})
    v2 = b2["review"]["version"]
    assert req("GET", f"/api/reviews/{rid}/versions/{v2}")[0] == 200
    assert req("GET", f"/api/reviews/{rid}/versions/{v2}/diagnosis")[0] == 200

    e2 = [ev("a", "A", "2026-10-01T10:00", "2026-10-01T12:00", 10)]
    _, b3 = req("PUT", f"/api/reviews/{rid}",
                {"base_version": v2, "title": "全链路2", "events": e2, "relations": []})
    v3 = b3["review"]["version"]
    st, b4 = req("POST", f"/api/reviews/{rid}/versions/{v2}/restore",
                 {"base_version": v3})
    assert st == 200 and b4["review"]["version"] == v3 + 1
    assert req("GET", f"/api/reviews/{rid}/compare?from={v2}&to={v3}")[0] == 200
    # 过期 base_version 仍是 409
    st, payload = req("PUT", f"/api/reviews/{rid}",
                      {"base_version": v3, "title": "过期", "events": e2, "relations": []})
    assert st == 409 and payload["current_version"] == v3 + 1


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
