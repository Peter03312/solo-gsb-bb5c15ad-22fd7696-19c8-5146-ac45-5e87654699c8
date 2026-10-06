"""证据附件 HTTP API 端到端测试：python tests/test_attachments_api.py

覆盖：
- multipart 上传文本/PNG/PDF：200 返回新 bundle（含文件名/大小/SHA-256/说明），
  Content-Length/sha 与下载字节一致；
- 当前下载与历史版本下载（含中文文件名 Content-Disposition）；
- 类型/大小不合格 400/413：不产生新版本、不留物理文件；
- base_version 过期 409；事件不存在 400；
- 移除附件 200 另存新版本，旧版本仍可下载、当前 404；
- 恢复历史版本后附件集合按该版重建；
- 跨复盘并入复制附件关联，来源删除后目标仍可下载；
- 删除复盘后无引用的物理文件被清理。
"""
import hashlib
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
_store = tempfile.mkdtemp(prefix="att-api-store-")
os.environ["DB_PATH"] = _tmp.name
os.environ["ATTACHMENT_DIR"] = _store
os.environ["PORT"] = "0"

import db  # noqa: E402
import server  # noqa: E402

db.init_db(server.DB_PATH)
HTTPD = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
PORT = HTTPD.server_address[1]
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()

BASE = "http://127.0.0.1:{}".format(PORT)
PNG = bytes.fromhex("89504e470d0a1a0a") + b"IHDR-png-body"
TXT = "现场日志 第一行\n".encode("utf-8")
PDF = b"%PDF-1.7\npdf-body"


def req(method, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), dict(e.headers)


def _multipart(fields, filename, content, field="file"):
    boundary = "----attTestBoundary42"
    parts = []
    for k, v in fields.items():
        parts.append(
            ("--" + boundary).encode() + b"\r\n" +
            'Content-Disposition: form-data; name="{}"'.format(k).encode() +
            b"\r\n\r\n" + str(v).encode("utf-8"))
    parts.append(
        ("--" + boundary).encode() + b"\r\n" +
        'Content-Disposition: form-data; name="{}"; filename="{}"'.format(
            field, filename).encode("utf-8") + b"\r\n" +
        b"Content-Type: application/octet-stream\r\n\r\n" + content)
    body = b"\r\n".join(parts) + b"\r\n--" + boundary.encode() + b"--\r\n"
    return body, "multipart/form-data; boundary=" + boundary


def upload(rid, uid, filename, content, base_version, description="说明"):
    body, ctype = _multipart({"base_version": base_version, "description": description},
                             filename, content)
    r = urllib.request.Request(
        "{}/api/reviews/{}/events/{}/attachments".format(BASE, rid, uid),
        data=body, method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), dict(e.headers)


def raw_get(path):
    try:
        with urllib.request.urlopen(BASE + path) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def setup_review():
    _, created, _ = req("POST", "/api/reviews", {"title": "附件 API"})
    rid = created["review"]["id"]
    payload = {
        "base_version": 1, "title": "附件 API",
        "events": [{"key": "a", "name": "告警", "earliest_start": "2026-10-01T10:00",
                    "latest_start": "2026-10-01T10:00", "duration_minutes": 15}],
        "relations": []}
    _, bundle, _ = req("PUT", "/api/reviews/{}".format(rid), payload)
    return rid, bundle["events"][0]["uid"]


def count_files():
    n = 0
    for _root, _d, files in os.walk(_store):
        n += len([f for f in files if ".tmp-" not in f])
    return n


def test_upload_and_download_current_and_version():
    rid, uid = setup_review()
    status, bundle, _ = upload(rid, uid, "值班日志.txt", TXT, 2, "03:10 群聊记录")
    assert status == 200, bundle
    assert bundle["review"]["version"] == 3
    a = bundle["events"][0]["attachments"][0]
    assert a["filename"] == "值班日志.txt"
    assert a["size_bytes"] == len(TXT)
    assert a["sha256"] == hashlib.sha256(TXT).hexdigest()
    assert a["description"] == "03:10 群聊记录"

    st, data, headers = raw_get("/api/reviews/{}/attachments/{}".format(rid, a["sha256"]))
    assert st == 200 and data == TXT
    lower_headers = {k.lower(): v for k, v in headers.items()}
    assert lower_headers["x-sha-256"] == a["sha256"]
    assert "UTF-8''" in lower_headers["content-disposition"]

    st, data, _h = raw_get(
        "/api/reviews/{}/versions/3/attachments/{}".format(rid, a["sha256"]))
    assert st == 200 and data == TXT


def test_invalid_uploads_rejected_without_files_or_versions():
    rid, uid = setup_review()
    before = count_files()
    cases = [("x.png", b"not png"), ("x.exe", b"MZ"), ("a.txt", b"\xff\xfe")]
    for fn, data in cases:
        st, resp, _ = upload(rid, uid, fn, data, 2)
        assert st == 400, (fn, resp)
    st, resp, _ = upload(rid, uid, "big.jpg",
                         b"\xff\xd8\xff" + b"0" * (20 * 1024 * 1024 + 1), 2)
    assert st == 413, resp
    assert req("GET", "/api/reviews/{}".format(rid))[1]["review"]["version"] == 2
    assert count_files() == before
    leftovers = [f for root, _d, fs in os.walk(_store) for f in fs if ".tmp-" in f]
    assert leftovers == []


def test_stale_base_and_unknown_event():
    rid, uid = setup_review()
    st, resp, _ = upload(rid, uid, "a.pdf", PDF, 2)
    assert st == 200
    st, resp, _ = upload(rid, uid, "a.pdf", PDF, 2)  # base 过期
    assert st == 409 and resp["current_version"] == 3
    st, resp, _ = upload(rid, "a" * 32, "a.pdf", PDF, 3)  # 事件不存在
    assert st == 400, resp


def test_remove_then_history_still_downloadable():
    rid, uid = setup_review()
    _, bundle, _ = upload(rid, uid, "shot.png", PNG, 2, "截图")
    a = bundle["events"][0]["attachments"][0]
    st, bundle, _ = req("DELETE", "/api/reviews/{}/attachments/{}".format(rid, a["id"]),
                        {"base_version": 3})
    assert st == 200 and bundle["review"]["version"] == 4
    assert bundle["events"][0]["attachments"] == []
    st, _b, _h = raw_get("/api/reviews/{}/attachments/{}".format(rid, a["sha256"]))
    assert st == 404
    st, data, _h = raw_get(
        "/api/reviews/{}/versions/3/attachments/{}".format(rid, a["sha256"]))
    assert st == 200 and data == PNG
    # 版本列表标注附件数量
    versions = req("GET", "/api/reviews/{}/versions".format(rid))[1]["versions"]
    v3 = next(v for v in versions if v["version"] == 3)
    assert v3["attachment_count"] == 1 and v3["kind"] == "attachment"


def test_restore_rebuilds_attachments():
    rid, uid = setup_review()
    upload(rid, uid, "a.txt", TXT, 2, "d")   # v3 有附件
    _, b, _ = req("GET", "/api/reviews/{}".format(rid))
    aid = b["events"][0]["attachments"][0]["id"]
    req("DELETE", "/api/reviews/{}/attachments/{}".format(rid, aid), {"base_version": 3})  # v4
    st, bundle, _ = req("POST", "/api/reviews/{}/versions/3/restore".format(rid),
                        {"base_version": 4})
    assert st == 200
    assert bundle["events"][0]["attachments"][0]["filename"] == "a.txt"


def test_merge_copies_attachments_and_survives_source_deletion():
    # 来源 v3 带 png 附件
    srid, suid = setup_review()
    _, _b, _ = upload(srid, suid, "证据.png", PNG, 2, "来源现场")
    sha = hashlib.sha256(PNG).hexdigest()
    # 目标
    _, created, _ = req("POST", "/api/reviews", {"title": "目标"})
    tid = created["review"]["id"]
    _, _b2, _ = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 1, "title": "目标",
        "events": [{"key": "x", "name": "目标已有", "earliest_start": "2026-10-01T07:00",
                    "latest_start": "2026-10-01T08:00", "duration_minutes": 20}],
        "relations": []})
    pv = req("POST", "/api/reviews/{}/merge/preview".format(tid), {
        "source_review_id": srid, "source_version": 3, "event_uids": [suid]})[1]
    assert pv["copied_events"][0]["attachments"][0]["sha256"] == sha
    st, bundle, _ = req("POST", "/api/reviews/{}/merge/confirm".format(tid), {
        "source_review_id": srid, "source_version": 3, "event_uids": [suid],
        "base_version": 2, "digest": pv["digest"]})
    assert st == 200
    copied = next(e for e in bundle["events"] if e["name"] == "告警")
    assert copied["attachments"][0]["sha256"] == sha
    # 删除来源复盘
    req("DELETE", "/api/reviews/{}".format(srid))
    st, data, _h = raw_get("/api/reviews/{}/attachments/{}".format(tid, sha))
    assert st == 200 and data == PNG


def test_delete_review_gcs_files():
    rid, uid = setup_review()
    _, _b, _ = upload(rid, uid, "only.pdf", PDF, 2, "唯一引用")
    sha = hashlib.sha256(PDF).hexdigest()
    assert os.path.isfile(db.blob_path(server.DB_PATH, sha))
    req("DELETE", "/api/reviews/{}".format(rid))
    assert not os.path.exists(db.blob_path(server.DB_PATH, sha))


def test_download_unknown_paths():
    rid, uid = setup_review()
    st, _d, _h = raw_get("/api/reviews/9999/attachments/{}".format("a" * 64))
    assert st == 404
    st, _d, _h = raw_get("/api/reviews/{}/attachments/{}".format(rid, "b" * 64))
    assert st == 404
    st, _d, _h = raw_get(
        "/api/reviews/{}/versions/1/attachments/{}".format(rid, "c" * 64))
    assert st == 404  # 旧快照附件为空集合


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, "->", e or "(assert)")
        except Exception:
            import traceback
            failed += 1
            traceback.print_exc()
    sys.exit(1 if failed else 0)
