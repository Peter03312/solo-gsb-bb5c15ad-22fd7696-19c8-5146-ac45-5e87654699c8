"""并入历史事件链 HTTP API 端到端测试：python tests/test_merge_api.py

覆盖：
- 预览只读、确认后一次性产生新草稿版本；
- 复制事件字段与关系等待上下界保留、仅两端均入选关系被复制、
  复制事件取得新 uid；目标/来源原有数据不变；
- 预览与确认推算结论一致；目标一变旧 digest 确认返回 400，
  base_version 过期返回 409；
- 来源/目标相同、事件不存在、选择非法、来源缺快照 → 400/404/422；
- 冲突/暂存并入结果可保存（草稿），定稿仍被拒绝（400）；
- 新版本在版本列表中标注并入产生、可回看，回看含来源复盘/版本。
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


def ev(key, name, es, ls, dur, tz=None, note=""):
    d = {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
         "duration_minutes": dur, "note": note}
    if tz is not None:
        d["timezone"] = tz
    return d


def rel(a, b, min_g=None, max_g=None):
    r = {"predecessor_key": a, "successor_key": b}
    if min_g is not None:
        r["min_gap_minutes"] = min_g
    if max_g is not None:
        r["max_gap_minutes"] = max_g
    return r


def make_source():
    _, b = req("POST", "/api/reviews", {"title": "来源复盘"})
    sid = b["review"]["id"]
    status, b = req("PUT", "/api/reviews/{}".format(sid), {
        "base_version": 1, "title": "来源复盘",
        "events": [ev("a", "来源告警", "2026-10-01T09:00", "2026-10-01T09:00", 15, note="源备注"),
                   ev("b", "来源响应", "2026-10-01T09:00", "2026-10-01T12:00", 30),
                   ev("c", "来源未选", "2026-10-01T08:00", "2026-10-01T08:30", 10)],
        "relations": [rel("a", "b", min_g=10, max_g=60), rel("a", "c")]})
    assert status == 200
    uids = {e["name"]: e["uid"] for e in b["events"]}
    return sid, uids


def make_target():
    _, b = req("POST", "/api/reviews", {"title": "目标复盘"})
    tid = b["review"]["id"]
    status, b = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 1, "title": "目标复盘",
        "events": [ev("x", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20)],
        "relations": []})
    assert status == 200
    return tid, b


def preview(tid, sid, sver, uids):
    return req("POST", "/api/reviews/{}/merge/preview".format(tid),
               {"source_review_id": sid, "source_version": sver, "event_uids": uids})


def confirm(tid, sid, sver, uids, base, digest):
    return req("POST", "/api/reviews/{}/merge/confirm".format(tid),
               {"source_review_id": sid, "source_version": sver, "event_uids": uids,
                "base_version": base, "digest": digest})


def test_preview_read_only_and_confirm_creates_draft():
    sid, suids = make_source()
    tid, tbundle = make_target()
    target_uid = tbundle["events"][0]["uid"]
    chosen = [suids["来源告警"], suids["来源响应"]]

    status, pv = preview(tid, sid, 2, chosen)
    assert status == 200, pv
    assert pv["target"]["base_version"] == 2 and pv["source"]["version"] == 2
    assert [e["name"] for e in pv["copied_events"]] == ["来源告警", "来源响应"]
    assert pv["copied_events"][0]["note"] == "源备注"
    assert len(pv["copied_relations"]) == 1  # A→C 未复制
    cr = pv["copied_relations"][0]
    assert cr["min_gap_minutes"] == 10 and cr["max_gap_minutes"] == 60
    # 预览不推进版本
    _, cur = req("GET", "/api/reviews/{}".format(tid))
    assert cur["review"]["version"] == 2 and len(cur["events"]) == 1

    status, b = confirm(tid, sid, 2, chosen, 2, pv["digest"])
    assert status == 200, b
    assert b["review"]["version"] == 3 and b["review"]["status"] == "draft"
    by_name = {e["name"]: e for e in b["events"]}
    assert by_name["目标已有"]["uid"] == target_uid
    assert by_name["来源告警"]["uid"] != suids["来源告警"]   # 全新持续身份
    assert by_name["来源响应"]["uid"] != suids["来源响应"]
    assert by_name["来源告警"]["note"] == "源备注"
    assert len(b["relations"]) == 1
    r = b["relations"][0]
    assert r["min_gap_minutes"] == 10 and r["max_gap_minutes"] == 60
    win_b = b["computation"]["result"]["windows"][str(r["successor_id"])]
    # 预览与确认结论一致
    prev_b = pv["result"]["windows"]["s_" + suids["来源响应"]]
    assert win_b["earliest_start"] == prev_b["earliest_start"] == "2026-10-01T09:25"
    assert win_b["latest_start"] == prev_b["latest_start"] == "2026-10-01T10:15"

    # 版本列表：v3 标注并入产生、可回看
    _, vs = req("GET", "/api/reviews/{}/versions".format(tid))
    v3 = next(v for v in vs["versions"] if v["version"] == 3)
    assert v3["kind"] == "merge" and v3["reviewable"] and v3["restorable"]
    assert v3["source_review_id"] == sid and v3["source_version"] == 2
    status, detail = req("GET", "/api/reviews/{}/versions/3".format(tid))
    assert status == 200 and detail["source_review_id"] == sid

    # 来源复盘历史不变
    _, sb = req("GET", "/api/reviews/{}".format(sid))
    assert sb["review"]["version"] == 2 and len(sb["events"]) == 3 and len(sb["relations"]) == 2


def test_selecting_subset_drops_dangling_relations():
    sid, suids = make_source()
    tid, _ = make_target()
    # 只选 A、C：A→B（B 未选）与 A→C 中只应复制 A→C
    status, pv = preview(tid, sid, 2, [suids["来源告警"], suids["来源未选"]])
    assert status == 200
    names = [(c["predecessor"]["name"], c["successor"]["name"])
             for c in pv["copied_relations"]]
    assert names == [("来源告警", "来源未选")]
    assert pv["copied_relations"][0]["max_gap_minutes"] is None


def test_stale_base_version_409_and_changed_target_digest_400():
    sid, suids = make_source()
    tid, _ = make_target()
    chosen = [suids["来源告警"]]
    _, pv = preview(tid, sid, 2, chosen)

    # base_version 过期 → 409
    status, resp = confirm(tid, sid, 2, chosen, 1, pv["digest"])
    assert status == 409 and resp["current_version"] == 2

    # 目标保存新版本后，带新 base_version 但旧 digest → 400（结论无法保证一致）
    status, _ = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 2, "title": "目标复盘-改",
        "events": [ev("x", "目标已有", "2026-10-01T05:00", "2026-10-01T05:30", 20)],
        "relations": []})
    assert status == 200
    status, resp = confirm(tid, sid, 2, chosen, 3, pv["digest"])
    assert status == 400 and "预览不一致" in resp["error"]
    # 重新预览（基于 v3）后确认成功
    _, pv2 = preview(tid, sid, 2, chosen)
    assert pv2["digest"] != pv["digest"]
    status, b = confirm(tid, sid, 2, chosen, 3, pv2["digest"])
    assert status == 200 and b["review"]["version"] == 4


def test_bad_requests_and_missing_resources():
    sid, suids = make_source()
    tid, _ = make_target()

    def prev_code(body):
        return req("POST", "/api/reviews/{}/merge/preview".format(tid), body)[0]

    # 来源/目标相同 → 400
    status, resp = preview(tid, tid, 2, [suids["来源告警"]])
    assert status == 400 and "不能相同" in resp["error"]
    # 事件不存在 → 400
    status, resp = preview(tid, sid, 2, ["no-such-uid"])
    assert status == 400 and "不存在" in resp["error"]
    # 输入不合法：缺字段/空集合/重复/类型错误 → 400
    assert prev_code({"source_version": 2, "event_uids": ["x"]}) == 400
    assert prev_code({"source_review_id": sid, "event_uids": ["x"]}) == 400
    assert prev_code({"source_review_id": sid, "source_version": 2}) == 400
    assert prev_code({"source_review_id": sid, "source_version": 2,
                      "event_uids": []}) == 400
    assert prev_code({"source_review_id": sid, "source_version": 2,
                      "event_uids": ["x", "x"]}) == 400
    assert prev_code({"source_review_id": "1", "source_version": 2,
                      "event_uids": ["x"]}) == 400
    # 来源复盘不存在 → 404；来源版本不存在 → 404
    assert preview(tid, 9999, 1, ["x"])[0] == 404
    assert preview(tid, sid, 99, [suids["来源告警"]])[0] == 404
    # 目标复盘不存在 → 404
    assert req("POST", "/api/reviews/9999/merge/preview",
               {"source_review_id": sid, "source_version": 2,
                "event_uids": [suids["来源告警"]]})[0] == 404
    # 确认缺 digest / base_version → 400
    status, _ = req("POST", "/api/reviews/{}/merge/confirm".format(tid),
                    {"source_review_id": sid, "source_version": 2,
                     "event_uids": [suids["来源告警"]], "base_version": 2})
    assert status == 400

    # 目标无任何新增版本/输入（预览与失败确认均无写入）
    _, cur = req("GET", "/api/reviews/{}".format(tid))
    assert cur["review"]["version"] == 2 and len(cur["events"]) == 1


def test_legacy_source_version_422():
    sid, suids = make_source()
    tid, _ = make_target()
    # 删除来源 v1 的快照模拟旧库历史版本
    import sqlite3
    conn = sqlite3.connect(server.DB_PATH)
    conn.execute("DELETE FROM snapshots WHERE review_id=? AND review_version=1", (sid,))
    conn.commit()
    conn.close()
    status, resp = preview(tid, sid, 1, [suids["来源告警"]])
    assert status == 422 and resp["mergable"] is False
    assert "不可并入" in resp["error"]
    status, resp = confirm(tid, sid, 1, [suids["来源告警"]], 2, "digest")
    assert status == 422


def test_conflict_merge_savable_but_not_finalizable():
    _, b = req("POST", "/api/reviews", {"title": "冲突来源"})
    sid = b["review"]["id"]
    status, _ = req("PUT", "/api/reviews/{}".format(sid), {
        "base_version": 1, "title": "冲突来源",
        "events": [ev("z", "矛盾", "2026-10-01T11:00", "2026-10-01T10:00", 5)],
        "relations": []})
    assert status == 200
    zuid = req("GET", "/api/reviews/{}".format(sid))[1]["events"][0]["uid"]
    tid, _ = make_target()
    _, pv = preview(tid, sid, 2, [zuid])
    assert pv["result"]["status"] == "conflict"
    status, b = confirm(tid, sid, 2, [zuid], 2, pv["digest"])
    assert status == 200  # 冲突结果仍可保存为草稿
    assert b["review"]["status"] == "draft"
    status, resp = req("POST", "/api/reviews/{}/finalize".format(tid),
                       {"base_version": 3})
    assert status == 400  # 不得定稿


def test_timezone_offsets_preserved_over_http():
    _, b = req("POST", "/api/reviews", {"title": "时区来源"})
    sid = b["review"]["id"]
    status, _ = req("PUT", "/api/reviews/{}".format(sid), {
        "base_version": 1, "title": "时区来源",
        "events": [ev("n", "纽约响应", "2026-11-01T01:30-04:00",
                      "2026-11-01T12:00-05:00", 30, tz="America/New_York", note="dst")],
        "relations": []})
    assert status == 200
    nuid = req("GET", "/api/reviews/{}".format(sid))[1]["events"][0]["uid"]
    tid, _ = make_target()
    _, pv = preview(tid, sid, 2, [nuid])
    ce = pv["copied_events"][0]
    assert ce["timezone"] == "America/New_York"
    assert ce["earliest_start"] == "2026-11-01T01:30-04:00"
    assert ce["latest_start"] == "2026-11-01T12:00-05:00"
    status, b = confirm(tid, sid, 2, [nuid], 2, pv["digest"])
    assert status == 200
    e = [e for e in b["events"] if e["name"] == "纽约响应"][0]
    assert e["timezone"] == "America/New_York"
    assert e["earliest_start"] == "2026-11-01T01:30-04:00"
    assert e["latest_start"] == "2026-11-01T12:00-05:00"


def test_origin_provenance_preserved_across_detail_versions_resave():
    sid, suids = make_source()
    tid, tbundle = make_target()
    target_uid = tbundle["events"][0]["uid"]
    chosen = [suids["来源告警"]]

    status, pv = preview(tid, sid, 2, chosen)
    assert status == 200
    # 预览复制事件即携带来源摘要（来源复盘/版本/来源事件身份与名称）
    o = pv["copied_events"][0]["origin"]
    assert o == {
        "source_review_id": sid, "source_review_title": "来源复盘",
        "source_version": 2, "source_event_uid": suids["来源告警"],
        "source_event_name": "来源告警"}

    status, b = confirm(tid, sid, 2, chosen, 2, pv["digest"])
    assert status == 200
    by_name = {e["name"]: e for e in b["events"]}
    assert "origin" not in by_name["目标已有"]                 # 非并入事件无来源
    alarm = by_name["来源告警"]
    assert alarm["origin"] == o
    assert alarm["uid"] != suids["来源告警"]                    # 新持续身份仍关联来源

    # 版本回看 v3：不可变快照事件带来源摘要
    status, v3 = req("GET", "/api/reviews/{}/versions/3".format(tid))
    assert status == 200
    sv3 = {e["name"]: e for e in v3["input"]["events"]}
    assert sv3["来源告警"]["origin"] == o and "origin" not in sv3["目标已有"]

    # 版本对照 v2 → v3：新增事件条目带来源摘要
    status, cmp_ = req("GET", "/api/reviews/{}/compare?from=2&to=3".format(tid))
    assert status == 200 and cmp_["pairable"]
    added = {e["name"]: e for e in cmp_["events"]["added"]}
    assert added["来源告警"]["origin"] == o

    # 再次保存（改名，uid 回传）：来源不丢失；客户端夹带的伪造 origin 被拒绝
    put_events = [{
        "key": "id_" + str(e["id"]), "uid": e["uid"],
        "name": "告警改名" if e["name"] == "来源告警" else e["name"],
        "earliest_start": e["earliest_start"], "latest_start": e["latest_start"],
        "duration_minutes": e["duration_minutes"], "note": e["note"],
        "origin": {"source_review_id": 999, "source_event_name": "伪造"},
    } for e in b["events"]]
    status, b4 = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 3, "title": "目标复盘",
        "events": put_events, "relations": []})
    assert status == 200
    by4 = {e["name"]: e for e in b4["events"]}
    assert by4["告警改名"]["origin"] == o
    assert by4["告警改名"]["uid"] == alarm["uid"]
    assert "origin" not in by4["目标已有"]

    # 恢复 v3 为新草稿 v5：来源摘要随快照恢复
    status, b5 = req("POST", "/api/reviews/{}/versions/3/restore".format(tid),
                     {"base_version": 4})
    assert status == 200
    assert next(e for e in b5["events"] if e["name"] == "来源告警")["origin"] == o


def test_origin_still_shown_after_source_deleted():
    sid, suids = make_source()
    tid, _ = make_target()
    _, pv = preview(tid, sid, 2, [suids["来源告警"]])
    status, _ = confirm(tid, sid, 2, [suids["来源告警"]], 2, pv["digest"])
    assert status == 200
    # 删除来源复盘（其版本一并级联删除）：目标当前详情与历史回看仍显示摘要
    status, _ = req("DELETE", "/api/reviews/{}".format(sid))
    assert status == 200
    _, b = req("GET", "/api/reviews/{}".format(tid))
    alarm = next(e for e in b["events"] if e["name"] == "来源告警")
    assert alarm["origin"]["source_review_id"] == sid
    assert alarm["origin"]["source_review_title"] == "来源复盘"
    status, v3 = req("GET", "/api/reviews/{}/versions/3".format(tid))
    assert status == 200
    assert any(e.get("origin", {}).get("source_review_id") == sid
               for e in v3["input"]["events"])


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
