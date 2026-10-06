"""事故现场附件 HTTP API 端到端测试：python tests/test_attachments_api.py

覆盖：
- multipart 上传（文本/PNG/JPEG/PDF）→ 201、新版本、当前列表含名称/大小/SHA-256；
- 当前附件与历史版本附件两种下载路径，Content-Type/Content-Disposition/X-SHA-256；
- 非法扩展名、内容与扩展名不符、空文件、>20MB、缺字段 → 400 且无新版本/无孤儿文件；
- base_version 过期 → 409；未知事件 uid、复盘不存在 → 400/404；
- DELETE 移除当前附件（409 过期、404 不存在）；旧版本仍可下载；
- 并入 HTTP 流程携带附件关联，删除来源后目标仍可下载。
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

import attachments  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402

db.init_db(server.DB_PATH)
HTTPD = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
PORT = HTTPD.server_address[1]
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()

PNG = attachments.PNG_SIGNATURE + b"\x00IHDRpayload"
JPEG = attachments.JPEG_SIGNATURE + b"\xe0JFIFpayload"
PDF = b"%PDF-1.4\nbody"


def req(method, path, body=None, raw=False):
    url = "http://127.0.0.1:{}{}".format(PORT, path)
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload.decode("utf-8")))
    except urllib.error.HTTPError as e:
        payload = e.read()
        return e.code, (payload if raw else json.loads(payload.decode("utf-8")))


def multipart(path, fields, files):
    """files: {field: (filename, bytes, content_type)}；返回 (status, parsed)。"""
    boundary = "----testBoundary" + "ab" * 8
    chunks = []
    for k, v in fields.items():
        chunks.append(
            ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
             % (boundary, k, v)).encode("utf-8"))
    for k, (filename, content, ctype) in files.items():
        chunks.append(
            ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
             'Content-Type: %s\r\n\r\n' % (boundary, k, filename, ctype)).encode("utf-8"))
        chunks.append(content + b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(chunks)
    r = urllib.request.Request(
        "http://127.0.0.1:{}{}".format(PORT, path), data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return e.code, raw


def download(path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:{}{}".format(PORT, path)) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def make_review_event(title="附件HTTP"):
    s, b = req("POST", "/api/reviews", {"title": title})
    assert s == 201
    rid = b["review"]["id"]
    s, b = req("PUT", "/api/reviews/{}".format(rid), {
        "base_version": 1, "title": title,
        "events": [{"key": "a", "uid": "", "name": "告警",
                    "earliest_start": "2026-10-01T10:00",
                    "latest_start": "2026-10-01T10:00",
                    "duration_minutes": 15}],
        "relations": []})
    assert s == 200
    return rid, b["events"][0]["uid"], b["review"]["version"]


def storage_files():
    root = attachments.storage_dir(server.DB_PATH)
    return set(os.listdir(root))


def test_upload_each_type_and_metadata():
    rid, uid, ver = make_review_event()
    cases = [
        ("log.txt", b"line\n", "text/plain", "text/plain; charset=utf-8"),
        ("shot.png", PNG, "image/png", "image/png"),
        ("pic.jpg", JPEG, "image/jpeg", "image/jpeg"),
        ("doc.pdf", PDF, "application/pdf", "application/pdf"),
    ]
    for fname, data, _in_ct, expect_ct in cases:
        s, b = multipart(
            "/api/reviews/{}/events/{}/attachments".format(rid, uid),
            {"base_version": str(ver), "description": "说明-" + fname},
            {"file": (fname, data, "application/octet-stream")})
        assert s == 201, (fname, b)
        ver = b["review"]["version"]
        att = b["attachments"][-1]
        assert att["filename"] == fname
        assert att["size"] == len(data)
        assert len(att["blob_sha256"]) == 64
        assert att["event_uid"] == uid
        assert att["description"] == "说明-" + fname
    s, b = req("GET", "/api/reviews/{}".format(rid))
    assert len(b["attachments"]) == 4
    return rid, uid, ver


def test_download_current_and_version():
    rid, uid, ver = make_review_event()
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(rid, uid),
        {"base_version": str(ver)},
        {"file": ("note.txt", b"abcdef", "text/plain")})
    assert s == 201
    att = b["attachments"][0]
    upver = b["review"]["version"]
    # 当前下载
    st, data, headers = download(att["download_url"])
    assert st == 200 and data == b"abcdef"
    assert headers["Content-Type"].startswith("text/plain")
    assert "note.txt" in headers["Content-Disposition"]
    assert headers["X-SHA-256"] == att["blob_sha256"]
    # 历史版本下载
    s, vj = req("GET", "/api/reviews/{}/versions/{}".format(rid, upver))
    va = vj["input"]["attachments"][0]
    st, data, _h = download(va["download_url"])
    assert st == 200 and data == b"abcdef"
    # 非附件路径不可猜（id 必须属于版本）
    s, _ = req("GET", "/api/reviews/{}/versions/1".format(rid))  # v1 无附件
    st, _, _ = download(
        "/api/reviews/{}/versions/1/attachments/{}/download".format(rid, va["id"]))
    assert st == 404


def test_rejections_leave_nothing():
    rid, uid, ver = make_review_event()
    before = storage_files()
    bad_cases = [
        ("evil.exe", b"MZbinary", "application/octet-stream"),
        ("fake.txt", PNG, "image/png"),                     # 内容扩展名不符
        ("fake.png", b"not a png", "text/plain"),
        ("empty.txt", b"", "text/plain"),
        ("noext", b"abc", "text/plain"),
    ]
    for fname, data, ct in bad_cases:
        s, b = multipart(
            "/api/reviews/{}/events/{}/attachments".format(rid, uid),
            {"base_version": str(ver)}, {"file": (fname, data, ct)})
        assert s == 400, (fname, b)
    # 大文件
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(rid, uid),
        {"base_version": str(ver)},
        {"file": ("big.txt", b"a" * (20 * 1024 * 1024 + 1), "text/plain")})
    assert s == 400, b
    # 缺 base_version
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(rid, uid),
        {}, {"file": ("a.txt", b"abc", "text/plain")})
    assert s == 400
    # 没有文件部分
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(rid, uid),
        {"base_version": str(ver)}, {})
    assert s == 400
    # 非 multipart 请求头
    r = urllib.request.Request(
        "http://127.0.0.1:{}/api/reviews/{}/events/{}/attachments".format(PORT, rid, uid),
        data=b"hello", method="POST", headers={"Content-Type": "text/plain"})
    try:
        urllib.request.urlopen(r)
        raise AssertionError
    except urllib.error.HTTPError as e:
        assert e.code == 400
    # 版本没有推进、没有任何附件、没有残留临时文件
    s, cur = req("GET", "/api/reviews/{}".format(rid))
    assert cur["review"]["version"] == ver and cur["attachments"] == []
    after = storage_files()
    leftovers = [n for n in (after - before)
                 if n.startswith((".upload-", ".tmp-blob-")) or len(n) == 64]
    assert not leftovers, leftovers


def test_stale_and_unknown_paths():
    rid, uid, ver = make_review_event()
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(rid, uid),
        {"base_version": str(ver)}, {"file": ("a.txt", b"abc", "text/plain")})
    assert s == 201
    att = b["attachments"][0]
    upver = b["review"]["version"]
    # 过期上传
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(rid, uid),
        {"base_version": str(ver)}, {"file": ("b.txt", b"xyz", "text/plain")})
    assert s == 409 and b["current_version"] == upver
    # 未知事件
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(rid, "f" * 32),
        {"base_version": str(upver)}, {"file": ("b.txt", b"xyz", "text/plain")})
    assert s == 400
    # 复盘不存在
    s, b = multipart(
        "/api/reviews/9999/events/{}/attachments".format(uid),
        {"base_version": "1"}, {"file": ("b.txt", b"xyz", "text/plain")})
    assert s == 404
    # 过期移除 / 不存在附件
    s, b = req("DELETE", "/api/reviews/{}/attachments/{}".format(rid, att["id"]),
               {"base_version": ver})
    assert s == 409
    s, b = req("DELETE", "/api/reviews/{}/attachments/99999".format(rid),
               {"base_version": upver})
    assert s == 404


def test_remove_keeps_history_then_gc():
    rid, uid, ver = make_review_event()
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(rid, uid),
        {"base_version": str(ver)}, {"file": ("h.txt", b"evidence", "text/plain")})
    assert s == 201
    att = b["attachments"][0]
    upver = b["review"]["version"]
    s, vj = req("GET", "/api/reviews/{}/versions/{}".format(rid, upver))
    va = vj["input"]["attachments"][0]

    s, b = req("DELETE", "/api/reviews/{}/attachments/{}".format(rid, att["id"]),
               {"base_version": upver})
    assert s == 200 and b["attachments"] == []
    rmver = b["review"]["version"]
    # 旧版本仍可下载
    st, data, _ = download(va["download_url"])
    assert st == 200 and data == b"evidence"
    # 当前 404
    st, _, _ = download(att["download_url"])
    assert st == 404
    # 版本列表：新版本 0 附件，旧版本 1
    s, vl = req("GET", "/api/reviews/{}/versions".format(rid))
    counts = {v["version"]: v["attachment_count"] for v in vl["versions"]}
    assert counts[upver] == 1 and counts[rmver] == 0
    return rid, va["id"], upver, rmver


def test_merge_http_carries_attachments():
    # 来源：带 PNG 附件的事件
    s, b = req("POST", "/api/reviews", {"title": "来源"})
    srid = b["review"]["id"]
    s, b = req("PUT", "/api/reviews/{}".format(srid), {
        "base_version": 1, "title": "来源",
        "events": [{"key": "a", "uid": "", "name": "来源告警",
                    "earliest_start": "2026-10-01T09:00",
                    "latest_start": "2026-10-01T09:00",
                    "duration_minutes": 15}], "relations": []})
    suid = b["events"][0]["uid"]
    sver = b["review"]["version"]
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(srid, suid),
        {"base_version": str(sver), "description": "现场截图"},
        {"file": ("s.png", PNG, "image/png")})
    assert s == 201
    sver = b["review"]["version"]

    # 目标
    s, b = req("POST", "/api/reviews", {"title": "目标"})
    trid = b["review"]["id"]
    s, b = req("PUT", "/api/reviews/{}".format(trid), {
        "base_version": 1, "title": "目标",
        "events": [{"key": "t", "uid": "", "name": "目标事件",
                    "earliest_start": "2026-10-01T09:00",
                    "latest_start": "2026-10-01T12:00",
                    "duration_minutes": 15}], "relations": []})
    tbase = b["review"]["version"]

    s, pv = req("POST", "/api/reviews/{}/merge/preview".format(trid), {
        "source_review_id": srid, "source_version": sver, "event_uids": [suid]})
    assert s == 200 and pv["copied_attachment_count"] == 1
    assert pv["copied_events"][0]["attachments"][0]["filename"] == "s.png"
    s, b = req("POST", "/api/reviews/{}/merge/confirm".format(trid), {
        "source_review_id": srid, "source_version": sver, "event_uids": [suid],
        "base_version": tbase, "digest": pv["digest"]})
    assert s == 200, b
    atts = b["attachments"]
    assert len(atts) == 1 and atts[0]["filename"] == "s.png"
    # 新 uid，不沿用来源
    copied = [e for e in b["events"] if e["name"] == "来源告警"][0]
    assert copied["uid"] != suid and atts[0]["event_uid"] == copied["uid"]
    st, data, _ = download(atts[0]["download_url"])
    assert st == 200 and data == PNG

    # 删除来源复盘后，目标仍可下载
    s, _ = req("DELETE", "/api/reviews/{}".format(srid))
    assert s == 200
    st, data, _ = download(atts[0]["download_url"])
    assert st == 200 and data == PNG


def test_bundle_includes_attachments_field():
    rid, uid, ver = make_review_event()
    s, b = multipart(
        "/api/reviews/{}/events/{}/attachments".format(rid, uid),
        {"base_version": str(ver)}, {"file": ("a.txt", b"x", "text/plain")})
    assert s == 201
    s, b = req("GET", "/api/reviews/{}".format(rid))
    assert s == 200 and isinstance(b["attachments"], list) and len(b["attachments"]) == 1


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
