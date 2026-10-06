"""证据附件持久化测试：python tests/test_attachments_db.py

覆盖（直接调 db 层）：
- 上传另存 kind=attachment 新版本；描述符含文件名/大小/SHA-256/说明/时间；
- 附件关联事件持续身份：改名、普通保存后仍在；删除事件则当前关联移除；
- 移除当前附件只影响新版本：旧版本快照仍可下载原件；
- 恢复历史版本以该版附件集合生成新草稿；
- 跨复盘并入只复制附件关联（共享 blob），来源复盘删除后目标仍可下载；
- GC：仅当没有任何当前或历史引用时清理物理文件；
- 相同内容去重（一份物理文件）与重复上传幂等（不产生新版本）；
- base_version 过期抛 VersionConflict；事件不存在拒绝且不留文件/版本；
- 失败不产生部分版本或孤儿文件。
"""
import hashlib
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import db  # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a") + b"IHDR-body"
TXT = "值班记录\n".encode("utf-8")
PDF = b"%PDF-1.7\nbody"


def ev(key, name, es="2026-10-01T10:00", ls="2026-10-01T10:00", dur=15):
    return {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
            "duration_minutes": dur, "note": "", "timezone": None}


def fresh():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    # 默认存储在数据库旁的 attachments/；多个临时库同在 /tmp 会共享目录，
    # 测试中显式隔离（生产容器一个库独占 /data，不需要该变量）。
    store = tempfile.mkdtemp(prefix="att-store-")
    os.environ["ATTACHMENT_DIR"] = store
    db.init_db(tmp.name)
    return tmp.name


def make_review(p, title="附件复盘", events=None):
    rid = db.create_review(p, title)
    db.update_review(p, rid, 1, title, events or [ev("a", "告警")], [])  # v2
    return rid


def bundle_event(p, rid, name):
    b = db.get_bundle(p, rid)
    return next(e for e in b["events"] if e["name"] == name)


def count_files(p):
    d = db.storage_dir(p)
    n = 0
    for root, _dirs, files in os.walk(d):
        n += len(files)
    return n


def test_upload_creates_version_descriptor_and_file():
    p = fresh()
    rid = make_review(p)
    uid = bundle_event(p, rid, "告警")["uid"]
    ver = db.add_attachment(p, rid, 2, uid, "shot.png", "现场截图", PNG)  # v3
    assert ver == 3
    a = bundle_event(p, rid, "告警")["attachments"][0]
    assert a["filename"] == "shot.png" and a["size_bytes"] == len(PNG)
    assert a["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert a["description"] == "现场截图" and a["uploaded_at"]
    assert os.path.isfile(db.blob_path(p, a["sha256"]))
    v3 = db.get_version(p, rid, 3)
    assert v3["kind"] == "attachment"
    snap_a = v3["input"]["events"][0]["attachments"][0]
    assert snap_a["filename"] == "shot.png" and "id" not in snap_a
    # 版本列表带附件计数与类型
    item = next(x for x in db.list_versions(p, rid) if x["version"] == 3)
    assert item["attachment_count"] == 1 and item["kind"] == "attachment"
    # 下载信息
    info = db.get_current_attachment(p, rid, a["sha256"])
    assert info["content_type"] == "image/png" and os.path.isfile(info["path"])


def test_dedup_and_idempotent_reupload():
    p = fresh()
    rid = make_review(p)
    uid = bundle_event(p, rid, "告警")["uid"]
    db.add_attachment(p, rid, 2, uid, "a.txt", "d1", TXT)  # v3
    assert count_files(p) == 1
    # 同内容同文件名重复上传：幂等，不产生新版本
    v = db.add_attachment(p, rid, 3, uid, "a.txt", "d1", TXT)
    assert v == 3
    # 同内容不同文件名：新版本，新关联，但物理文件仍只一份
    v = db.add_attachment(p, rid, 3, uid, "b.txt", "d2", TXT)  # v4
    assert v == 4 and count_files(p) == 1


def test_rename_and_save_keeps_attachment():
    p = fresh()
    rid = make_review(p)
    uid = bundle_event(p, rid, "告警")["uid"]
    db.add_attachment(p, rid, 2, uid, "a.pdf", "pdf", PDF)  # v3
    b = db.get_bundle(p, rid)
    saved = [{
        "key": "id_" + str(e["id"]), "uid": e["uid"], "name": "告警（改名）",
        "earliest_start": e["earliest_start"], "latest_start": e["latest_start"],
        "duration_minutes": e["duration_minutes"], "note": e["note"],
        "timezone": e["timezone"]} for e in b["events"]]
    db.update_review(p, rid, 3, "附件复盘", saved, [])  # v4
    a = bundle_event(p, rid, "告警（改名）")
    assert a["uid"] == uid and a["attachments"][0]["sha256"] == hashlib.sha256(PDF).hexdigest()


def test_remove_only_affects_new_version_history_remains():
    p = fresh()
    rid = make_review(p)
    uid = bundle_event(p, rid, "告警")["uid"]
    db.add_attachment(p, rid, 2, uid, "a.txt", "d", TXT)  # v3
    sha = hashlib.sha256(TXT).hexdigest()
    aid = bundle_event(p, rid, "告警")["attachments"][0]["id"]
    db.remove_attachment(p, rid, 3, aid)  # v4
    assert bundle_event(p, rid, "告警")["attachments"] == []
    # 当前不可下载（404），旧版本 v3 仍可下载，物理文件保留
    try:
        db.get_current_attachment(p, rid, sha)
        raise AssertionError("当前应无附件")
    except db.AttachmentRejected as e:
        assert e.status == 404
    info = db.get_version_attachment(p, rid, 3, sha)
    assert info["filename"] == "a.txt" and os.path.isfile(info["path"])
    assert count_files(p) == 1


def test_restore_rebuilds_attachment_set():
    p = fresh()
    rid = make_review(p)
    uid = bundle_event(p, rid, "告警")["uid"]
    db.add_attachment(p, rid, 2, uid, "a.txt", "d", TXT)  # v3 有附件
    db.add_attachment(p, rid, 3, uid, "b.png", "p", PNG)  # v4 两个附件
    aid_txt = next(a for a in bundle_event(p, rid, "告警")["attachments"]
                   if a["filename"] == "a.txt")["id"]
    db.remove_attachment(p, rid, 4, aid_txt)  # v5 只剩 png
    # 恢复 v3（只有 txt）→ v6
    db.restore_review(p, rid, 5, 3)
    names = [a["filename"] for a in bundle_event(p, rid, "告警")["attachments"]]
    assert names == ["a.txt"]
    v6 = db.get_version(p, rid, 6)
    assert v6["kind"] == "restore" and v6["source_version"] == 3
    assert os.path.isfile(db.blob_path(p, hashlib.sha256(PNG).hexdigest()))  # v4 仍引用


def test_merge_copies_attachment_links_and_survives_source_deletion():
    p = fresh()
    sid = make_review(p, "来源", [ev("a", "源告警")])
    suid = bundle_event(p, sid, "源告警")["uid"]
    db.add_attachment(p, sid, 2, suid, "shot.png", "来源截图", PNG)  # 来源 v3
    sha = hashlib.sha256(PNG).hexdigest()

    tid = make_review(p, "目标", [ev("x", "目标已有", "2026-10-01T07:00", "2026-10-01T08:00", 20)])
    pv = db.preview_merge(p, tid, sid, 3, [suid])
    assert pv["copied_events"][0]["attachments"][0]["filename"] == "shot.png"
    db.merge_into_review(p, tid, sid, 3, [suid], 2, pv["digest"])  # 目标 v3
    copied = bundle_event(p, tid, "源告警")
    assert copied["uid"] != suid and copied["attachments"][0]["sha256"] == sha
    # 快照与当前都可下载
    assert os.path.isfile(db.get_current_attachment(p, tid, sha)["path"])
    before = count_files(p)
    db.delete_review(p, sid)
    # 来源删除：目标当前与目标历史快照仍可下载；物理文件保留且未复制
    assert os.path.isfile(db.get_current_attachment(p, tid, sha)["path"])
    v3 = db.get_version(p, tid, 3)
    assert v3["input"]["events"][-1]["attachments"][0]["sha256"] == sha
    assert count_files(p) == before


def test_gc_only_when_no_current_or_history_reference():
    p = fresh()
    rid = make_review(p)
    uid = bundle_event(p, rid, "告警")["uid"]
    db.add_attachment(p, rid, 2, uid, "a.txt", "d", TXT)  # v3
    sha = hashlib.sha256(TXT).hexdigest()
    aid = bundle_event(p, rid, "告警")["attachments"][0]["id"]
    db.remove_attachment(p, rid, 3, aid)  # v4：当前无，但 v3 快照仍引用
    assert os.path.isfile(db.blob_path(p, sha))
    db.delete_review(p, rid)  # 当前与全部历史都删除
    assert not os.path.exists(db.blob_path(p, sha))


def test_legacy_snapshot_has_empty_attachments():
    # 旧快照没有附件字段：回看 events 的 attachments 缺省为空，历史下载 404
    p = fresh()
    rid = db.create_review(p, "旧")  # v1 快照无附件
    v1 = db.get_version(p, rid, 1)
    assert v1["input"]["events"] == []
    try:
        db.get_version_attachment(p, rid, 1, "0" * 64)
        raise AssertionError("应 404")
    except db.AttachmentRejected as e:
        assert e.status == 404


def test_stale_version_conflict_and_bad_event_no_partial_write():
    p = fresh()
    rid = make_review(p)
    uid = bundle_event(p, rid, "告警")["uid"]
    db.add_attachment(p, rid, 2, uid, "a.txt", "d", TXT)  # v3
    # 过期 base_version
    try:
        db.add_attachment(p, rid, 2, uid, "b.png", "p", PNG)
        raise AssertionError("应 409")
    except db.VersionConflict as e:
        assert e.current_version == 3
    # 版本未增加，没有新文件
    assert db.current_version(p, rid) == 3 and count_files(p) == 1
    # 事件不属于该复盘
    try:
        db.add_attachment(p, rid, 3, "deadbeef" * 4, "b.png", "p", PNG)
        raise AssertionError("应拒绝")
    except db.AttachmentRejected as e:
        assert e.status == 400
    assert db.current_version(p, rid) == 3 and count_files(p) == 1
    # 移除不存在的附件
    try:
        db.remove_attachment(p, rid, 3, 99999)
        raise AssertionError("应拒绝")
    except db.AttachmentRejected as e:
        assert e.status == 404
    assert db.current_version(p, rid) == 3


def test_bad_content_rejected_before_persistence():
    p = fresh()
    rid = make_review(p)
    uid = bundle_event(p, rid, "告警")["uid"]
    for fn, data in [("x.png", b"not png"), ("x.exe", b"MZ"), ("a.txt", b"\x00\x01")]:
        try:
            db.add_attachment(p, rid, 2, uid, fn, "d", data)
            raise AssertionError("应拒绝 " + fn)
        except Exception as e:
            assert getattr(e, "status", None) in (400, 413), (fn, e)
    assert db.current_version(p, rid) == 2
    assert count_files(p) == 0
    # 拒绝路径不留临时文件
    leftovers = [f for root, _d, fs in os.walk(db.storage_dir(p)) for f in fs if ".tmp-" in f]
    assert leftovers == []


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
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print("ERROR", t.__name__, "->", repr(e))
    sys.exit(1 if failed else 0)
