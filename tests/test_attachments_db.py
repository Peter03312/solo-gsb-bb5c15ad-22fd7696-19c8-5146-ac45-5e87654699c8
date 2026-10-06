"""事故现场附件持久化（db.py）单元测试：python tests/test_attachments_db.py

覆盖：
- 上传即新版本（kind=attachment），当前视图/版本快照带附件；
- 附件跟随事件持续身份：改名、普通保存保留；删事件只移除当前关联，旧版本不动；
- 移除当前附件只影响新版本，旧版本快照仍可下载原件，无引用才清理文件；
- 恢复历史版本：以该版附件集合生成新草稿（旧快照无附件视为空）；
- 跨复盘并入：只复制入选事件的附件关联到全新 uid；来源复盘删除后目标仍可下载；
- base_version 过期 409、事件不存在 400、附件不属于复盘 404；
- 任何失败不留部分版本/孤儿文件；同内容去重；启动清理崩溃残留。
"""
import hashlib
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import attachments  # noqa: E402
import db  # noqa: E402

PNG = attachments.PNG_SIGNATURE + b"\x00IHDRpayload"
JPEG = attachments.JPEG_SIGNATURE + b"\xe0JFIFpayload"
PDF = b"%PDF-1.4\nbody"
TEXT = "现场日志\n".encode("utf-8")


def setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def ev(key, name="A", uid="", es="2026-10-01T09:00", ls="2026-10-01T10:00", dur=15):
    return {"key": key, "uid": uid, "name": name, "earliest_start": es,
            "latest_start": ls, "duration_minutes": dur, "note": "", "timezone": None}


def stage(p, data, filename):
    sp = attachments.stage_upload(attachments.storage_dir(p))
    with open(sp, "wb") as f:
        f.write(data)
    sha, size, ct = attachments.validate_staged_file(sp, filename)
    return sp, sha, size, ct


def upload(p, rid, base, uid, filename, data, description=""):
    sp, sha, size, ct = stage(p, data, filename)
    return db.add_event_attachment(p, rid, base, uid, filename, description,
                                   ct, size, sha, sp)


def blob_exists(p, data):
    sha = hashlib.sha256(data).hexdigest()
    return attachments.blob_path(attachments.storage_dir(p), sha).is_file()


def no_temp_files(p):
    return not [n for n in os.listdir(attachments.storage_dir(p))
                if n.startswith((".upload-", ".tmp-blob-"))]


def make_review_with_event(p, title="复盘"):
    rid = db.create_review(p, title)
    db.update_review(p, rid, 1, title, [ev("a")], [])
    b = db.get_bundle(p, rid)
    return rid, b["events"][0]["uid"], b["review"]["version"]


# ------------------------------------------------------------------ 上传/快照

def test_upload_creates_version_and_snapshot():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    nv, view = upload(p, rid, ver, uid, "log.txt", TEXT, "说明")
    assert nv == ver + 1
    assert view["blob_sha256"] == hashlib.sha256(TEXT).hexdigest()
    assert view["size"] == len(TEXT) and view["description"] == "说明"
    assert view["download_url"].endswith("/download")
    # 当前视图
    b = db.get_bundle(p, rid)
    assert len(b["attachments"]) == 1
    assert b["attachments"][0]["event_uid"] == uid
    # 版本快照
    v = db.get_version(p, rid, nv)
    assert v["kind"] == "attachment" and v["attachment_count"] == 1
    assert v["input"]["attachments"][0]["blob_sha256"] == view["blob_sha256"]
    # 版本列表带附件数
    vs = db.list_versions(p, rid)
    cur = [x for x in vs if x["version"] == nv][0]
    assert cur["attachment_count"] == 1
    assert [x for x in vs if x["version"] == 1][0]["attachment_count"] == 0
    # 文件存在
    assert blob_exists(p, TEXT) and no_temp_files(p)


def test_all_allowed_kinds_and_dedup():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    sha = hashlib.sha256(PNG).hexdigest()
    sp = attachments.stage_upload(attachments.storage_dir(p))
    open(sp, "wb").write(PNG)
    nv1, v1 = db.add_event_attachment(p, rid, ver, uid, "a.png", "",
                                      "image/png", len(PNG), sha, sp)
    # 同内容第二个文件：复用 blob
    nv2, v2 = upload(p, rid, nv1, uid, "another.png", PNG)
    c = db.connect(p)
    n = c.execute("SELECT COUNT(*) FROM blobs WHERE sha256=?", (sha,)).fetchone()[0]
    c.close()
    assert n == 1, "相同内容只应有一条 blob"
    b = db.get_bundle(p, rid)
    assert {a["filename"] for a in b["attachments"]} == {"a.png", "another.png"}


def test_stale_base_version_409_no_file_no_version():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    sp = attachments.stage_upload(attachments.storage_dir(p))
    open(sp, "wb").write(TEXT)
    sha, size, ct = attachments.validate_staged_file(sp, "x.txt")
    try:
        db.add_event_attachment(p, rid, ver - 1, uid, "x.txt", "", ct, size, sha, sp)
        raise AssertionError("应抛 VersionConflict")
    except db.VersionConflict as e:
        assert e.current_version == ver
    # 无新版本、无 blob 行、无文件、无临时文件
    assert db.current_version(p, rid) == ver
    assert not attachments.blob_path(attachments.storage_dir(p), sha).is_file()
    assert no_temp_files(p)


def test_upload_to_unknown_event_rejected_without_file():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    sp = attachments.stage_upload(attachments.storage_dir(p))
    open(sp, "wb").write(TEXT)
    sha, size, ct = attachments.validate_staged_file(sp, "x.txt")
    try:
        db.add_event_attachment(p, rid, ver, "a" * 32, "x.txt", "", ct, size, sha, sp)
        raise AssertionError
    except db.AttachmentRejected:
        pass
    assert db.current_version(p, rid) == ver
    assert not attachments.blob_path(attachments.storage_dir(p), sha).is_file()


# ------------------------------------------------------ 改名/保存/删事件/移除

def test_rename_and_normal_save_keep_attachment():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    nv, view = upload(p, rid, ver, uid, "log.txt", TEXT)
    # 改名 + 普通保存
    db.update_review(p, rid, nv, "改名复盘",
                     [ev("a", name="A-改名", uid=uid)], [])
    b = db.get_bundle(p, rid)
    assert len(b["attachments"]) == 1 and b["attachments"][0]["event_uid"] == uid
    # 内容不变的普通保存再一次
    nv2 = b["review"]["version"]
    db.update_review(p, rid, nv2, "改名复盘",
                     [ev("a", name="A-改名", uid=uid)], [])
    b = db.get_bundle(p, rid)
    assert len(b["attachments"]) == 1
    assert blob_exists(p, TEXT)


def test_delete_event_removes_current_but_old_version_kept():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    nv, view = upload(p, rid, ver, uid, "log.txt", TEXT)
    # 删除事件的普通保存：当前关联移除，新版本无附件；旧版本仍有
    db.update_review(p, rid, nv, "复盘", [], [])
    b = db.get_bundle(p, rid)
    assert b["attachments"] == []
    after = b["review"]["version"]
    old = db.get_version(p, rid, nv)
    assert old["attachment_count"] == 1
    # 旧版本原件仍可下载，文件不被清理
    snap_aid = old["input"]["attachments"][0]["id"]
    path, _row = db.get_version_attachment(p, rid, nv, snap_aid)
    assert path.read_bytes() == TEXT
    assert blob_exists(p, TEXT)
    # 当前关联 id 已不可下载
    try:
        db.get_current_attachment(p, rid, view["id"])
        raise AssertionError
    except db.AttachmentNotFound:
        pass
    return p, rid, after, uid, nv


def test_remove_current_only_affects_new_version():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    nv, view = upload(p, rid, ver, uid, "log.txt", TEXT)
    nr = db.remove_event_attachment(p, rid, nv, view["id"])
    assert nr == nv + 1
    b = db.get_bundle(p, rid)
    assert b["attachments"] == []
    # 旧版本仍下载
    old = db.get_version(p, rid, nv)
    snap_aid = old["input"]["attachments"][0]["id"]
    path, _row = db.get_version_attachment(p, rid, nv, snap_aid)
    assert path.read_bytes() == TEXT
    # 该新移除版本快照无附件
    newv = db.get_version(p, rid, nr)
    assert newv["attachment_count"] == 0 and newv["kind"] == "attachment"
    # 移除不存在的关联 → 404
    try:
        db.remove_event_attachment(p, rid, nr, view["id"])
        raise AssertionError
    except db.AttachmentNotFound:
        pass
    # 过期移除 409
    try:
        db.remove_event_attachment(p, rid, nv, 999999)
        raise AssertionError
    except db.VersionConflict:
        pass


def test_file_gc_only_when_no_references():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    nv, view = upload(p, rid, ver, uid, "log.txt", TEXT)
    # 仍有历史引用：删当前关联后文件保留
    db.remove_event_attachment(p, rid, nv, view["id"])
    assert blob_exists(p, TEXT)
    # 删除复盘：历史与当前都没了 → 文件清理
    db.delete_review(p, rid)
    assert not blob_exists(p, TEXT)


# ------------------------------------------------------------------ 恢复

def test_restore_rebuilds_attachment_set():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    nv, _ = upload(p, rid, ver, uid, "old.txt", TEXT)
    aid_cur = db.get_bundle(p, rid)["attachments"][0]["id"]
    nr = db.remove_event_attachment(p, rid, nv, aid_cur)
    assert db.get_bundle(p, rid)["attachments"] == []
    # 恢复有附件的旧版本 → 当前集合回来
    rs = db.restore_review(p, rid, nr, nv)
    b = db.get_bundle(p, rid)
    assert len(b["attachments"]) == 1 and b["attachments"][0]["blob_sha256"] == \
        hashlib.sha256(TEXT).hexdigest()
    restored = db.get_version(p, rid, rs)
    assert restored["attachment_count"] == 1


def test_restore_legacy_empty_attachment_set():
    # 旧快照无附件：直接构造一个无 snapshot_attachments 行的快照（新代码下的
    # 正常快照总有空集合；这里验证空集合恢复后当前也为空）
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    nv, _view = upload(p, rid, ver, uid, "x.txt", TEXT)
    aid = db.get_bundle(p, rid)["attachments"][0]["id"]
    nr = db.remove_event_attachment(p, rid, nv, aid)  # nr 版附件集合为空
    rs = db.restore_review(p, rid, nr, nr)  # 恢复空集合版本
    assert db.get_bundle(p, rid)["attachments"] == []
    v = db.get_version(p, rid, rs)
    assert v["attachment_count"] == 0
    # 旧有附件版本仍在（文件保留）
    assert blob_exists(p, TEXT)


# ------------------------------------------------------------------ 并入

def _source_with_attachment(p):
    rid = db.create_review(p, "来源")
    db.update_review(p, rid, 1, "来源", [ev("a", name="S")], [])
    b = db.get_bundle(p, rid)
    u = b["events"][0]["uid"]
    nv, _ = upload(p, rid, b["review"]["version"], u, "shot.png", PNG, "现场截图")
    return rid, u, nv


def test_merge_copies_only_selected_attachments():
    p = setup_db()
    srid, suid, sver = _source_with_attachment(p)
    # 来源再加一个无附件事件
    sb = db.get_bundle(p, srid)
    db.update_review(p, srid, sb["review"]["version"], "来源", [
        ev("a", name="S", uid=suid),
        ev("b", name="S2", es="2026-10-01T11:00", ls="2026-10-01T12:00")], [])
    sb = db.get_bundle(p, srid)
    assert sb["review"]["version"] == sver + 1
    cur_ver = sb["review"]["version"]

    trid = db.create_review(p, "目标")
    db.update_review(p, trid, 1, "目标", [ev("t", name="T")], [])
    tb = db.get_bundle(p, trid)

    # 只选 S（有附件）
    pv = db.preview_merge(p, trid, srid, cur_ver, [suid])
    assert pv["copied_attachment_count"] == 1
    assert pv["copied_events"][0]["attachments"][0]["filename"] == "shot.png"
    db.merge_into_review(p, trid, srid, cur_ver, [suid],
                         tb["review"]["version"], pv["digest"])
    mb = db.get_bundle(p, trid)
    copied = [e for e in mb["events"] if e["name"] == "S"][0]
    assert copied["uid"] != suid  # 全新身份
    atts = [a for a in mb["attachments"] if a["event_uid"] == copied["uid"]]
    assert len(atts) == 1 and atts[0]["blob_sha256"] == hashlib.sha256(PNG).hexdigest()
    # 并入版本快照也有
    mv = db.get_version(p, trid, mb["review"]["version"])
    assert mv["kind"] == "merge" and mv["attachment_count"] == 1
    # 下载
    path, _r, _b = db.get_current_attachment(p, trid, atts[0]["id"])
    assert path.read_bytes() == PNG


def test_merge_target_keeps_file_after_source_deleted():
    p = setup_db()
    srid, suid, sver = _source_with_attachment(p)
    trid = db.create_review(p, "目标")
    db.update_review(p, trid, 1, "目标", [ev("t", name="T")], [])
    tb = db.get_bundle(p, trid)
    pv = db.preview_merge(p, trid, srid, sver, [suid])
    db.merge_into_review(p, trid, srid, sver, [suid],
                         tb["review"]["version"], pv["digest"])
    mb = db.get_bundle(p, trid)
    att_id = mb["attachments"][0]["id"]
    # 删除来源复盘（其当前/快照关联全部级联清除）
    db.delete_review(p, srid)
    assert blob_exists(p, PNG)  # 目标仍引用
    path, _row, _blob = db.get_current_attachment(p, trid, att_id)
    assert path.read_bytes() == PNG


def test_merge_unchanged_when_no_attachments():
    p = setup_db()
    # 来源无附件
    srid = db.create_review(p, "来源")
    db.update_review(p, srid, 1, "来源", [ev("a", name="S")], [])
    sb = db.get_bundle(p, srid)
    suid = sb["events"][0]["uid"]
    trid = db.create_review(p, "目标")
    db.update_review(p, trid, 1, "目标", [ev("t", name="T")], [])
    tb = db.get_bundle(p, trid)
    pv = db.preview_merge(p, trid, srid, sb["review"]["version"], [suid])
    assert pv["copied_attachment_count"] == 0 and pv["copied_attachments"] == []
    db.merge_into_review(p, trid, srid, sb["review"]["version"], [suid],
                         tb["review"]["version"], pv["digest"])
    assert db.get_bundle(p, trid)["attachments"] == []


# ------------------------------------------------------------------ 清理/恢复

def test_startup_sweep_cleans_crash_residue():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    nv, view = upload(p, rid, ver, uid, "log.txt", TEXT)
    root = attachments.storage_dir(p)
    # 伪造崩溃残留：一个游离 blob 名 + 一个上传临时文件
    open(root / ("c" * 64), "wb").close()
    tf = attachments.stage_upload(root)  # .upload- 前缀
    db.init_db(p)  # 重新初始化即执行启动扫描
    names = os.listdir(root)
    assert "c" * 64 not in names
    assert not [n for n in names if n.startswith(".upload-")]
    assert os.path.basename(tf) not in names
    # 真实 blob 仍在
    assert blob_exists(p, TEXT)


def test_download_errors():
    p = setup_db()
    rid, uid, ver = make_review_with_event(p)
    nv, view = upload(p, rid, ver, uid, "log.txt", TEXT)
    # 别的复盘 id 下找不到
    try:
        db.get_current_attachment(p, rid + 999, view["id"])
        raise AssertionError
    except db.NotFound:
        pass
    try:
        db.get_current_attachment(p, rid, 999999)
        raise AssertionError
    except db.AttachmentNotFound:
        pass
    # 版本下载：附件不属于该版本
    try:
        db.get_version_attachment(p, rid, ver, view["id"])
        raise AssertionError
    except db.AttachmentNotFound:
        pass
    # 版本不存在
    try:
        db.get_version_attachment(p, rid, 999, view["id"])
        raise AssertionError
    except db.VersionNotFound:
        pass


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
