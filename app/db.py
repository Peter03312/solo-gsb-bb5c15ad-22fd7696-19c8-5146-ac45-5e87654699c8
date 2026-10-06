"""SQLite 持久化层。

原始输入（reviews / events / relations）与推算结果（computations）
分表保存；每次成功修改都会写入一条新的 computations 记录。
通过 reviews.version 实现乐观锁：携带旧版本号的提交会被拒绝。

版本回看：每次保存/定稿/恢复/并入成功时，除 computations 外另写一条不可变的
snapshots 记录，留存当时的标题、事件、“先于”关系输入，与该版本的推算结果
一一对应。旧库中只有 computations、没有 snapshots 的历史版本视为不可回看、
不可恢复（也不可作为并入来源），列表中明确标记，绝不用当前输入冒充。

事故现场附件（attachments）
---------------------------
- blobs 按 **SHA-256 内容寻址**去重：同一文件跨事件、跨版本、跨复盘只存一份；
- event_attachments 是附件与事件**持续身份（uid）**的当前关联（含当时上传的
  文件名/大小/说明/时间）。普通保存、改名不触碰关联；删除事件时随身份清掉
  当前关联（只影响新版本）；
- snapshot_attachments 是每个不可变版本留存的附件集合（同时冗余进
  snapshots.input_json["attachments"]）：移除当前附件、删除事件后，旧版本仍
  可下载原件；恢复历史版本即以该版附件集合重建当前关联；并入只复制入选事件
  的关联，来源删除后目标文件仍在（同一 blob 多份引用，GC 不会清理）；
- 任何写版本的操作（保存/定稿/恢复/并入/上传/移除附件）都在同一事务内核对
  base_version、替换/复制关联、写快照，失败整体回滚；磁盘文件只在事务内
  原子改名入库，失败清理临时文件；提交后仅清理“当前关联与所有历史快照均无
  引用”的 blob，启动时再扫描一次崩溃残留，保证不留孤儿文件。

跨复盘并入（merge）：预览只读组装合并输入并推算；确认在一个事务内完成版本
核对、digest 核对、整体替换、重新推算与新快照写入（kind=merge，记录来源
复盘/版本），失败回滚，绝不产生部分写入。复制事件由 _replace_input 分配
全新 uid，并固化与该新身份绑定的**来源摘要**（origin_json：来源复盘
id/标题、来源版本、来源事件 uid/名称）；摘要随之后的普通保存/恢复沿用，
目标原有事件与来源历史均不变，来源被删除后仍只展示已保存的摘要、不回读。
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
import attachments
import compare
import compute
import diagnose
import merge

LEGACY_REASON = "历史版本缺少完整输入快照，不可回看、不可恢复"
LEGACY_COMPARE_REASON = "历史版本缺少完整输入快照，无法比较"
LEGACY_MERGE_REASON = "来源版本缺少完整输入快照，不可并入"
LEGACY_DIAGNOSE_REASON = "该版本缺少完整输入快照，无法分析冲突约束"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'draft',      -- draft | finalized
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id       INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    earliest_start  TEXT,                          -- ISO 分钟精度，可空（暂存）
    latest_start    TEXT,                          -- 带时区事件保留 UTC 偏移
    duration_minutes INTEGER,
    note            TEXT NOT NULL DEFAULT '',
    timezone        TEXT,                          -- IANA 时区名；NULL = 未指定时区（旧语义）
    uid             TEXT,                          -- 跨版本持续身份，保存/恢复时沿用
    -- 跨复盘并入来源摘要（不可变 JSON 快照）：仅并入产生的事件非 NULL。
    -- 内容在并入确认时一次性固化（来源复盘 id/标题、来源版本、来源事件 uid/名称），
    -- 此后只读展示：来源复盘或来源版本被删除/改名后仍显示这里保存的摘要，
    -- 绝不回读来源、不改写来源历史；与推算完全无关。
    origin_json     TEXT
);
CREATE TABLE IF NOT EXISTS relations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    predecessor_id INTEGER NOT NULL,
    successor_id   INTEGER NOT NULL,
    min_gap_minutes INTEGER NOT NULL DEFAULT 0,    -- 前项结束到后项开始的最短等待
    max_gap_minutes INTEGER                        -- 最长等待，NULL = 不限
);
CREATE TABLE IF NOT EXISTS computations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    review_version INTEGER NOT NULL,
    status         TEXT NOT NULL,                  -- ok | conflict
    result_json    TEXT NOT NULL,
    computed_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    review_version INTEGER NOT NULL,
    kind           TEXT NOT NULL,                  -- save | finalize | restore | merge | attachment
    title          TEXT NOT NULL,                 -- 不可变的标题快照
    input_json     TEXT NOT NULL,                 -- 不可变输入：{events, relations, attachments}
    source_version INTEGER,                       -- restore/finalize/merge 沿用的来源版本
    source_review_id INTEGER,                     -- merge：来源复盘 id
    created_at     TEXT NOT NULL,
    UNIQUE(review_id, review_version)
);
-- 附件内容：按 SHA-256 内容寻址去重存储，文件本体存于存储目录 <sha256>。
CREATE TABLE IF NOT EXISTS blobs (
    sha256     TEXT PRIMARY KEY,                   -- 内容哈希（也作为存储文件名）
    size       INTEGER NOT NULL,                  -- 字节数
    kind       TEXT NOT NULL,                      -- text | png | jpeg | pdf
    created_at TEXT NOT NULL
);
-- 附件与事件持续身份（uid）的**当前**关联；一个 blob 可被多个事件/复盘关联。
-- review_id 冗余在关联行上（由 uid 唯一归属该复盘），便于按复盘删除与鉴权。
CREATE TABLE IF NOT EXISTS event_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id    INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    event_uid    TEXT NOT NULL,                    -- 事件持续身份：改名/普通保存保留
    blob_sha256  TEXT NOT NULL REFERENCES blobs(sha256),
    filename     TEXT NOT NULL,                    -- 上传当时的文件名（展示用副本）
    description  TEXT NOT NULL DEFAULT '',         -- 上传说明
    content_type TEXT NOT NULL,
    size         INTEGER NOT NULL,
    uploaded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_attachments_review
    ON event_attachments(review_id);
-- 每个不可变版本的附件集合留存（与 snapshots 一一对应、随复盘级联删除）。
-- 旧快照无附件：该表无记录，回看时视为空，绝不回填。
CREATE TABLE IF NOT EXISTS snapshot_attachments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    review_version INTEGER NOT NULL,
    event_uid      TEXT NOT NULL,
    blob_sha256    TEXT NOT NULL REFERENCES blobs(sha256),
    filename       TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    content_type   TEXT NOT NULL,
    size           INTEGER NOT NULL,
    uploaded_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_attachments_lookup
    ON snapshot_attachments(review_id, review_version);
"""


class NotFound(Exception):
    pass


class VersionNotFound(Exception):
    """指定的版本号在 computations 中不存在。"""


class LegacyVersion(Exception):
    """旧库历史版本：有推算记录但缺少完整输入快照，不可回看/恢复。"""


class VersionConflict(Exception):
    def __init__(self, current_version):
        super().__init__("stale version")
        self.current_version = current_version


class FinalizeRejected(Exception):
    pass


class InvalidCompareOrder(Exception):
    """版本对照的较早/较晚顺序无效（须 from_version < to_version）。"""


class MergeRejected(Exception):
    """并入输入不合法：来源/目标相同、事件不存在、选择非法等（HTTP 400）。"""


class AttachmentRejected(Exception):
    """附件操作被拒绝：事件不存在/数量超限/附件不属于该复盘等（HTTP 400）。"""


class AttachmentNotFound(Exception):
    """附件记录不存在或文件本体缺失（HTTP 404）。"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _new_uid():
    """生成事件持续身份：跨保存、改名、恢复保持稳定，与行号无关。"""
    return uuid.uuid4().hex


def make_origin(source_review_id, source_review_title, source_version,
                source_event_uid, source_event_name):
    """组装跨复盘并入事件的来源摘要（并入确认时一次性固化到目标事件）。

    摘要是纯展示用的不可变副本：来源复盘标题、来源事件名称都按并入当时的
    内容保存，之后来源复盘/版本被删除或改名也不回读、不改写。
    """
    return {
        "source_review_id": source_review_id,
        "source_review_title": source_review_title,
        "source_version": source_version,
        "source_event_uid": source_event_uid,
        "source_event_name": source_event_name,
    }


def _origin_of(row_or_event):
    """从 live 行（origin_json）或快照事件（origin）读取来源摘要；无则 None。"""
    raw = row_or_event.get("origin_json")
    if raw:
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None
    origin = row_or_event.get("origin")
    return origin if isinstance(origin, dict) else None


def _event_view(row):
    """live 事件行 → 对外视图：origin_json 展平为嵌套 origin（无来源则不带）。"""
    view = dict(row)
    raw = view.pop("origin_json", None)
    if raw:
        try:
            view["origin"] = json.loads(raw)
        except (TypeError, ValueError):
            pass
    return view


def _attachment_view(row, review_id, version=None):
    """附件关联行 → 对外视图（当前关联与快照行字段相同，通用）。

    给定 version 时下载地址走版本快照鉴权路径（旧版本移除附件后仍可下载）；
    否则走当前关联路径。
    """
    d = dict(row)
    if version is None:
        d["download_url"] = \
            "/api/reviews/{}/attachments/{}/download".format(review_id, d["id"])
    else:
        d["download_url"] = \
            "/api/reviews/{}/versions/{}/attachments/{}/download".format(
                review_id, version, d["id"])
    return d


def _current_attachments(conn, review_id):
    """当前附件关联（按 id 排序，稳定顺序）。"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM event_attachments WHERE review_id=? ORDER BY id",
        (review_id,)).fetchall()]


def _snapshot_attachment_rows(conn, review_id, version):
    """某历史版本留存的附件关联（旧快照无记录 → 空列表）。"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM snapshot_attachments WHERE review_id=? AND review_version=?"
        " ORDER BY id", (review_id, version)).fetchall()]


def _attachment_kind(content_type):
    return {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "application/pdf": "pdf",
    }.get(content_type, "text")


def init_db(db_path):
    """启动时初始化数据库（建目录、建表、老库结构迁移、清理崩溃残留文件）。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()
    # 存储目录中的崩溃残留（.upload-* 临时文件、无任何引用的 blob）只在启动
    # 与每次成功提交后清理；正常失败路径的临时文件在当次请求内即时删除。
    root = attachments.ensure_storage_dir(db_path)
    conn = connect(db_path)
    try:
        known = {r[0] for r in conn.execute("SELECT sha256 FROM blobs")}
    finally:
        conn.close()
    attachments.sweep_orphan_files(root, known)


def _migrate(conn):
    """老库结构升级（幂等）：relations 补充“先于”间隔设置列；events 补充持续身份列。

    升级前已存在的关系行由列默认值得到 最短等待=0、最长等待=NULL（不限），
    与旧语义“前项结束不晚于后项开始”完全一致。升级前已存在的事件行现场
    补发 uid，此后的保存即可沿用身份；已写入的旧快照不含 uid，对照时按
    “缺可靠身份”处理，只展示原貌，不回填、不猜配。

    events.timezone 同为可空迁移列：旧行与旧快照没有该列（视为未指定
    时区），继续按朴素本地时间的既有语义处理，绝不回填时区。

    events.origin_json 同为可空迁移列：仅并入产生的事件携带来源摘要，
    旧行与旧快照没有来源信息（视为非并入事件，不显示来源），绝不回填、
    不回读来源复盘。

    snapshots.source_review_id 同为可空迁移列：并入产生的版本用它记录
    来源复盘，旧快照该列为 NULL。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(relations)")}
    if "min_gap_minutes" not in cols:
        conn.execute(
            "ALTER TABLE relations ADD COLUMN min_gap_minutes INTEGER NOT NULL DEFAULT 0")
    if "max_gap_minutes" not in cols:
        conn.execute(
            "ALTER TABLE relations ADD COLUMN max_gap_minutes INTEGER")
    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "uid" not in event_cols:
        conn.execute("ALTER TABLE events ADD COLUMN uid TEXT")
        for (eid,) in conn.execute("SELECT id FROM events WHERE uid IS NULL").fetchall():
            conn.execute("UPDATE events SET uid=? WHERE id=?", (_new_uid(), eid))
    if "timezone" not in event_cols:
        conn.execute("ALTER TABLE events ADD COLUMN timezone TEXT")
    if "origin_json" not in event_cols:
        # 并入事件的来源摘要；旧行没有来源信息（非并入事件不显示来源）
        conn.execute("ALTER TABLE events ADD COLUMN origin_json TEXT")
    snap_cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    if "source_review_id" not in snap_cols:
        # 并入产生的版本需记录来源复盘；旧快照该列为 NULL
        conn.execute("ALTER TABLE snapshots ADD COLUMN source_review_id INTEGER")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------- 读取

def current_version(db_path, review_id):
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT version FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        return row["version"]
    finally:
        conn.close()


def list_reviews(db_path):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, status, version, updated_at FROM reviews ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_bundle(db_path, review_id):
    """返回复盘完整快照：元信息 + 原始输入 + 最近一次推算结果。"""
    conn = connect(db_path)
    try:
        review = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise NotFound(review_id)
        events = [_event_view(r) for r in conn.execute(
            "SELECT * FROM events WHERE review_id=? ORDER BY id", (review_id,)).fetchall()]
        relations = [dict(r) for r in conn.execute(
            "SELECT * FROM relations WHERE review_id=? ORDER BY id", (review_id,)).fetchall()]
        attachments = [_attachment_view(r, review_id)
                       for r in _current_attachments(conn, review_id)]
        comp = conn.execute(
            "SELECT * FROM computations WHERE review_id=? ORDER BY id DESC LIMIT 1",
            (review_id,)).fetchone()
        computation = None
        if comp is not None:
            computation = {
                "review_version": comp["review_version"],
                "status": comp["status"],
                "computed_at": comp["computed_at"],
                "result": json.loads(comp["result_json"]),
            }
        return {
            "review": dict(review),
            "events": events,
            "relations": relations,
            "attachments": attachments,
            "computation": computation,
        }
    finally:
        conn.close()


def list_versions(db_path, review_id):
    """列出某复盘可回看的版本（按版本号倒序）。

    以 computations 为准枚举每个版本；关联到 snapshots 的版本可回看，
    旧库中缺少完整输入快照的历史版本 reviewable/restorable 均为 false，
    并给出原因，不能也不会用当前输入冒充。
    """
    conn = connect(db_path)
    try:
        review = conn.execute("SELECT id FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise NotFound(review_id)
        comps = conn.execute(
            "SELECT * FROM computations WHERE review_id=? ORDER BY review_version DESC",
            (review_id,)).fetchall()
        snaps = {s["review_version"]: s for s in conn.execute(
            "SELECT * FROM snapshots WHERE review_id=?", (review_id,)).fetchall()}
        attach_counts = {}
        for r in conn.execute(
                "SELECT review_version, COUNT(*) FROM snapshot_attachments"
                " WHERE review_id=? GROUP BY review_version", (review_id,)).fetchall():
            attach_counts[r[0]] = r[1]
        versions = []
        for c in comps:
            v = c["review_version"]
            snap = snaps.get(v)
            result = json.loads(c["result_json"])
            if snap is None:
                versions.append({
                    "version": v,
                    "state": "draft",
                    "kind": None,
                    "reviewable": False,
                    "restorable": False,
                    "status": c["status"],
                    "has_drafts": bool(result.get("drafts")),
                    "computed_at": c["computed_at"],
                    "reason": LEGACY_REASON,
                    "source_version": None,
                    "attachment_count": 0,
                })
            else:
                versions.append({
                    "version": v,
                    "state": "finalized" if snap["kind"] == "finalize" else "draft",
                    "kind": snap["kind"],
                    "reviewable": True,
                    "restorable": True,
                    "status": c["status"],
                    "has_drafts": bool(result.get("drafts")),
                    "computed_at": c["computed_at"],
                    "created_at": snap["created_at"],
                    "title": snap["title"],
                    "source_version": snap["source_version"],
                    "source_review_id": snap["source_review_id"],
                    "attachment_count": attach_counts.get(v, 0),
                })
        return versions
    finally:
        conn.close()


def _version_snapshot(conn, review_id, version):
    """读取某版本的不可变输入快照与当时推算结果。

    版本号不存在抛 VersionNotFound；旧库缺少输入快照的版本抛 LegacyVersion。
    """
    comp = conn.execute(
        "SELECT * FROM computations WHERE review_id=? AND review_version=?",
        (review_id, version)).fetchone()
    if comp is None:
        raise VersionNotFound(version)
    snap = conn.execute(
        "SELECT * FROM snapshots WHERE review_id=? AND review_version=?",
        (review_id, version)).fetchone()
    if snap is None:
        raise LegacyVersion(version)
    input_data = json.loads(snap["input_json"])
    # 附件集合以 snapshot_attachments 表为准（与快照同一事务写入，含独立 id 供
    # 下载鉴权）；旧快照无该表记录，视为空，绝不回填当前附件。
    snapshot_attachments = _snapshot_attachment_rows(conn, review_id, version)
    input_data["attachments"] = [
        _attachment_view(r, review_id, version) for r in snapshot_attachments]
    return {
        "version": version,
        "state": "finalized" if snap["kind"] == "finalize" else "draft",
        "kind": snap["kind"],
        "title": snap["title"],
        "source_version": snap["source_version"],
        "source_review_id": snap["source_review_id"],
        "created_at": snap["created_at"],
        "status": comp["status"],
        "computed_at": comp["computed_at"],
        "input": input_data,
        "attachment_count": len(snapshot_attachments),
        "result": json.loads(comp["result_json"]),
    }


def get_version(db_path, review_id, version):
    """返回某版本的完整回看内容：不可变输入 + 当时推算依据 + 状态。

    旧库缺少输入快照的版本抛 LegacyVersion；版本不存在抛 VersionNotFound。
    """
    conn = connect(db_path)
    try:
        review = conn.execute("SELECT id FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise NotFound(review_id)
        data = _version_snapshot(conn, review_id, version)
        data["reviewable"] = True
        data["restorable"] = True
        return data
    finally:
        conn.close()


def compare_versions(db_path, review_id, from_version, to_version):
    """对照同一复盘的较早（from）与较晚（to）两个版本，返回只读差值视图。

    不改当前输入、版本号与定稿状态。顺序无效（from >= to）抛
    InvalidCompareOrder；版本不存在抛 VersionNotFound；旧库缺快照的版本
    抛 LegacyVersion；快照缺可靠事件身份时按原貌展示（pairable=False）。
    """
    if from_version >= to_version:
        raise InvalidCompareOrder(
            "较早版本必须小于较晚版本（from={} 不早于 to={}）".format(from_version, to_version))
    conn = connect(db_path)
    try:
        review = conn.execute("SELECT id FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise NotFound(review_id)
        from_data = _version_snapshot(conn, review_id, from_version)
        to_data = _version_snapshot(conn, review_id, to_version)
    finally:
        conn.close()
    result = compare.build_compare(from_data, to_data)
    result["review_id"] = review_id
    return result


def analyze_version(db_path, review_id, version):
    """对某可回看版本的不可变快照做最小不可满足冲突约束分析（只读）。

    不改当前输入、版本号与定稿状态，也不写任何记录：只读取该版本的
    snapshots + computations，由 diagnose 纯函数重算。
    版本不存在抛 VersionNotFound；旧库缺少输入快照的版本抛 LegacyVersion。
    """
    conn = connect(db_path)
    try:
        review = conn.execute("SELECT id FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise NotFound(review_id)
        data = _version_snapshot(conn, review_id, version)
    finally:
        conn.close()
    analysis = diagnose.analyze_snapshot(data)
    return {
        "review_id": review_id,
        "version": version,
        "kind": data["kind"],
        "state": data["state"],
        "title": data["title"],
        "status": data["status"],
        "computed_at": data["computed_at"],
        "read_only": True,
        "analysis": analysis,
    }


# ---------------------------------------------------------------------- 写入

def create_review(db_path, title):
    conn = connect(db_path)
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO reviews (title, status, version, created_at, updated_at) VALUES (?,?,?,?,?)",
            (title, "draft", 1, now, now))
        review_id = cur.lastrowid
        _save_version(conn, review_id, 1, title, [], [], "save", now)
        conn.commit()
        return review_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _snapshot_relations(relations, key_by_id):
    """快照用的关系列表（key 引用 + 当时的间隔设置），供回看与恢复使用。"""
    return [{
        "predecessor_key": key_by_id.get(r["predecessor_id"], ""),
        "successor_key": key_by_id.get(r["successor_id"], ""),
        "min_gap_minutes": r.get("min_gap_minutes") or 0,
        "max_gap_minutes": r.get("max_gap_minutes"),
    } for r in relations]


def _current_input(conn, review_id):
    """读取当前 live 输入，事件按插入顺序生成 key，关系用 key 引用。"""
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE review_id=? ORDER BY id", (review_id,)).fetchall()]
    relations = [dict(r) for r in conn.execute(
        "SELECT * FROM relations WHERE review_id=? ORDER BY id", (review_id,)).fetchall()]
    key_by_id = {e["id"]: "id_" + str(e["id"]) for e in events}
    snapshot_events = []
    for e in events:
        item = {
            "key": key_by_id[e["id"]],
            "uid": e.get("uid"),
            "name": e["name"],
            "earliest_start": e["earliest_start"],
            "latest_start": e["latest_start"],
            "duration_minutes": e["duration_minutes"],
            "note": e["note"],
            "timezone": e.get("timezone"),
        }
        origin = _origin_of(e)
        if origin is not None:
            item["origin"] = origin  # 并入事件：来源摘要随不可变快照留存
        snapshot_events.append(item)
    snapshot_relations = _snapshot_relations(relations, key_by_id)
    return events, relations, snapshot_events, snapshot_relations


def _save_version(conn, review_id, version, title, events, relations, kind, now=None,
                  source_version=None, source_review_id=None):
    """运行推算并写入该版本的 computations 与不可变 snapshots 记录。

    events/relations 为 compute 引擎所需的 live 行（含数字 id）；
    快照输入另外以 key 形式留存，供回看与恢复使用。

    附件快照：把当前 event_attachments 关联（由调用方在事务内先行替换/复制/
    裁剪完毕）整组复制到 snapshot_attachments，并冗余进
    input_json["attachments"]。旧版本的附件集合因此冻结——之后移除当前附件、
    删除事件都不影响旧版本下载。
    """
    now = now or _now()
    result = compute.compute(events, relations)
    conn.execute(
        "INSERT INTO computations (review_id, review_version, status, result_json, computed_at)"
        " VALUES (?,?,?,?,?)",
        (review_id, version, result["status"], json.dumps(result, ensure_ascii=False), now))
    key_by_id = {e["id"]: "id_" + str(e["id"]) for e in events}
    snapshot_events = []
    for e in events:
        item = {
            "key": key_by_id[e["id"]],
            "uid": e.get("uid"),
            "name": e["name"],
            "earliest_start": e["earliest_start"],
            "latest_start": e["latest_start"],
            "duration_minutes": e["duration_minutes"],
            "note": e["note"],
            "timezone": e.get("timezone"),
        }
        origin = _origin_of(e)
        if origin is not None:
            item["origin"] = origin  # 并入事件：来源摘要随不可变快照留存
        snapshot_events.append(item)
    snapshot_relations = _snapshot_relations(relations, key_by_id)
    # 当前附件关联整组固化为该版本的附件集合（上传时间/文件名/说明一并留存）
    current_atts = _current_attachments(conn, review_id)
    snapshot_atts = []
    for a in current_atts:
        conn.execute(
            "INSERT INTO snapshot_attachments (review_id, review_version, event_uid,"
            " blob_sha256, filename, description, content_type, size, uploaded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (review_id, version, a["event_uid"], a["blob_sha256"], a["filename"],
             a["description"], a["content_type"], a["size"], a["uploaded_at"]))
        snapshot_atts.append({
            "event_uid": a["event_uid"],
            "blob_sha256": a["blob_sha256"],
            "filename": a["filename"],
            "description": a["description"],
            "content_type": a["content_type"],
            "size": a["size"],
            "uploaded_at": a["uploaded_at"],
        })
    input_payload = {
        "events": snapshot_events,
        "relations": snapshot_relations,
        "attachments": snapshot_atts,
    }
    input_json = json.dumps(input_payload, ensure_ascii=False)
    conn.execute(
        "INSERT INTO snapshots (review_id, review_version, kind, title, input_json,"
        " source_version, source_review_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (review_id, version, kind, title, input_json, source_version,
         source_review_id, now))
    return result


def _replace_input(conn, review_id, events, relations):
    """整体替换 live 输入表。events 用客户端 key，relations 用 key 引用。

    每个事件可携带 uid（持续身份）：携带且未重复时沿用，否则现场分配新
    uid。行号每次保存都会重建，只有 uid 能跨版本识别同一事件。

    事件可携带服务端内部生成的嵌套 origin（并入事件的来源摘要）：原样
    固化为 origin_json；没有来源的事件该列为 NULL。客户端无法直接提供
    origin（HTTP 校验层只取固定字段），保存时由 _resolve_event_uids 按
    uid 从既有行重新关联，防止丢失或伪造。
    """
    conn.execute("DELETE FROM events WHERE review_id=?", (review_id,))
    conn.execute("DELETE FROM relations WHERE review_id=?", (review_id,))
    key_to_id = {}
    used_uids = set()
    for e in events:
        uid = e.get("uid") or None
        if not uid or uid in used_uids:
            uid = _new_uid()
        used_uids.add(uid)
        origin = e.get("origin")
        origin_json = json.dumps(origin, ensure_ascii=False) if isinstance(origin, dict) else None
        cur = conn.execute(
            "INSERT INTO events (review_id, name, earliest_start, latest_start,"
            " duration_minutes, note, timezone, uid, origin_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (review_id, e["name"], e.get("earliest_start"), e.get("latest_start"),
             e.get("duration_minutes"), e.get("note", ""), e.get("timezone"), uid,
             origin_json))
        key_to_id[e["key"]] = cur.lastrowid
    for r in relations:
        min_gap = r.get("min_gap_minutes")
        conn.execute(
            "INSERT INTO relations (review_id, predecessor_id, successor_id,"
            " min_gap_minutes, max_gap_minutes) VALUES (?,?,?,?,?)",
            (review_id, key_to_id[r["predecessor_key"]], key_to_id[r["successor_key"]],
             0 if min_gap is None else min_gap, r.get("max_gap_minutes")))


def _resolve_event_uids(conn, review_id, events):
    """保存前解析每个事件的持续身份（uid）与并入来源摘要（origin）。

    客户端回传的 uid 属于本复盘当前事件且未重复时沿用（改名、调整时间
    后仍是同一事件）；否则（新事件、未知或伪造的 uid）分配新 uid。
    绝不按名称或会重建的行号猜配。

    并入事件的来源摘要同样按 uid 从既有 live 行关联回来：客户端只回传
    uid、不回传 origin，服务端在此**先丢弃事件自带 origin，再**把与该
    身份绑定的已保存来源重新挂上（未知/新身份则无来源）。因此再次保存、
    改名、调整时间都不会丢失来源，而客户端无法伪造或篡改来源；来源摘要
    始终以服务端已保存的为准。
    """
    rows = conn.execute(
        "SELECT uid, origin_json FROM events WHERE review_id=?", (review_id,)).fetchall()
    known = {}
    for r in rows:
        if r["uid"]:
            origin = None
            if r["origin_json"]:
                try:
                    origin = json.loads(r["origin_json"])
                except (TypeError, ValueError):
                    origin = None
            known[r["uid"]] = origin
    used = set()
    for e in events:
        e.pop("origin", None)  # 客户端不得携带来源；合法来源只从服务端既有行关联
        uid = e.get("uid")
        if uid and uid in known and uid not in used:
            e["uid"] = uid
            origin = known[uid]
            if origin is not None:
                e["origin"] = origin  # 来源摘要随持续身份保留（再次保存不丢失）
        else:
            e["uid"] = _new_uid()
        used.add(e["uid"])


def _prune_dead_attachments(conn, review_id):
    """删除事件已不存在的**当前**附件关联（普通保存删事件时调用）。

    只影响当前关联与即将写入的新版本；snapshot_attachments 不动，旧版本
    仍可下载原件。
    """
    conn.execute(
        "DELETE FROM event_attachments WHERE review_id=? AND event_uid NOT IN"
        " (SELECT uid FROM events WHERE review_id=?)", (review_id, review_id))


def _unreferenced_blobs(conn):
    """返回当前事务状态下没有任何当前/历史引用的 blob sha256 列表。"""
    return [r[0] for r in conn.execute(
        "SELECT sha256 FROM blobs WHERE sha256 NOT IN"
        " (SELECT blob_sha256 FROM event_attachments)"
        " AND sha256 NOT IN (SELECT blob_sha256 FROM snapshot_attachments)"
    ).fetchall()]


def _delete_blob_files(db_path, shas):
    """提交成功后清理失去全部引用的 blob 文件；文件缺失/失败不影响数据。"""
    if not shas:
        return
    root = attachments.storage_dir(db_path)
    for sha in shas:
        try:
            attachments.blob_path(root, sha).unlink()
        except OSError:
            pass


def _commit_and_gc(conn, db_path):
    """提交事务并在成功后清理无引用 blob；失败回滚并向上抛出。

    返回失去引用的 sha 列表（文件已删除）。
    """
    dead = _unreferenced_blobs(conn)
    # 先在事务内移除将无引用的 blobs 行（若仍有引用，外键会阻止删除）
    for sha in dead:
        conn.execute("DELETE FROM blobs WHERE sha256=?", (sha,))
    conn.commit()
    _delete_blob_files(db_path, dead)
    return dead


def update_review(db_path, review_id, base_version, title, events, relations):
    """整体替换某复盘的输入并重新推算。base_version 过期则抛 VersionConflict。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        if row["version"] != base_version:
            raise VersionConflict(row["version"])

        _resolve_event_uids(conn, review_id, events)
        _replace_input(conn, review_id, events, relations)
        # 附件关联跟随事件持续身份：改名/普通保存保留；事件被删除时，其当前
        # 关联随之移除（旧快照中的关联不动）。
        _prune_dead_attachments(conn, review_id)
        live_events, live_relations, _, _ = _current_input(conn, review_id)

        new_version = base_version + 1
        # 输入发生变化：已定稿状态失效，回到草稿
        now = _now()
        conn.execute(
            "UPDATE reviews SET title=?, version=?, status='draft', updated_at=? WHERE id=?",
            (title, new_version, now, review_id))
        _save_version(conn, review_id, new_version, title, live_events, live_relations,
                      "save", now)
        _commit_and_gc(conn, db_path)
        return new_version
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _replace_live_attachments_from_snapshot(conn, review_id, version):
    """用某版本快照的附件集合重建当前关联（恢复历史版本时调用）。

    以该版附件集合生成新草稿：event_attachments 整体替换为快照留存（上传时间、
    文件名、说明一并沿用），随后由 _save_version 再固化进新版本快照。
    """
    conn.execute("DELETE FROM event_attachments WHERE review_id=?", (review_id,))
    rows = conn.execute(
        "SELECT event_uid, blob_sha256, filename, description, content_type, size,"
        " uploaded_at FROM snapshot_attachments WHERE review_id=? AND review_version=?",
        (review_id, version)).fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO event_attachments (review_id, event_uid, blob_sha256, filename,"
            " description, content_type, size, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
            (review_id, r["event_uid"], r["blob_sha256"], r["filename"], r["description"],
             r["content_type"], r["size"], r["uploaded_at"]))


def restore_review(db_path, review_id, base_version, source_version):
    """把某历史版本的不可变输入恢复为当前复盘的新草稿版本，重新推算。

    旧版本及原版本都不被覆盖；版本过期抛 VersionConflict；来源版本缺少
    完整输入快照（旧库历史版本）抛 LegacyVersion。当前附件关联整体替换为
    来源版本留存的附件集合（旧快照无附件即清空当前关联，绝不保留旧集合）。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        if row["version"] != base_version:
            raise VersionConflict(row["version"])

        snap = conn.execute(
            "SELECT * FROM snapshots WHERE review_id=? AND review_version=?",
            (review_id, source_version)).fetchone()
        if snap is None:
            # 可能是版本号不存在，也可能是旧库缺快照的历史版本
            comp = conn.execute(
                "SELECT 1 FROM computations WHERE review_id=? AND review_version=?",
                (review_id, source_version)).fetchone()
            if comp is not None:
                raise LegacyVersion(source_version)
            raise VersionNotFound(source_version)

        saved_input = json.loads(snap["input_json"])
        _replace_input(conn, review_id, saved_input["events"], saved_input["relations"])
        # 以该版附件集合重建当前关联（旧快照无附件 → 当前为空）
        _replace_live_attachments_from_snapshot(conn, review_id, source_version)
        live_events, live_relations, _, _ = _current_input(conn, review_id)

        new_version = base_version + 1
        title = snap["title"]
        now = _now()
        # 恢复产生的永远是新草稿：冲突或暂存输入允许恢复，但仍不得定稿
        conn.execute(
            "UPDATE reviews SET title=?, version=?, status='draft', updated_at=? WHERE id=?",
            (title, new_version, now, review_id))
        _save_version(conn, review_id, new_version, title, live_events, live_relations,
                      "restore", now, source_version=source_version)
        _commit_and_gc(conn, db_path)
        return new_version
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_merge_plan(conn, target_id, source_id, source_version, selected_uids):
    """在已有连接上取齐目标 live 输入与来源快照，构建并入规划（不写库）。"""
    target = conn.execute("SELECT * FROM reviews WHERE id=?", (target_id,)).fetchone()
    if target is None:
        raise NotFound(target_id)
    if source_id == target_id:
        raise MergeRejected("来源复盘与目标复盘不能相同")
    source = conn.execute("SELECT id, title FROM reviews WHERE id=?", (source_id,)).fetchone()
    if source is None:
        raise NotFound(source_id)
    source_data = _version_snapshot(conn, source_id, source_version)  # 旧库缺快照抛 LegacyVersion
    _live_events, _live_relations, snap_events, _snap_rels = _current_input(conn, target_id)
    try:
        plan = merge.build_plan(snap_events, _live_events, _live_relations,
                                source_data, selected_uids)
    except merge.MergeError as e:
        # 纯规划层的输入不合法统一转为 db 层的并入拒绝（HTTP 400，无部分写入）
        raise MergeRejected(str(e))
    # 仅复制**入选事件**在来源版本快照中的附件关联（不参与 digest/推算）。
    selected_set = {e.get("uid") for e in plan["copied_events"]}
    plan["copied_attachments"] = [dict(r) for r in conn.execute(
        "SELECT event_uid, blob_sha256, filename, description, content_type, size,"
        " uploaded_at FROM snapshot_attachments"
        " WHERE review_id=? AND review_version=? ORDER BY id",
        (source_id, source_version)).fetchall()
        if r["event_uid"] in selected_set]
    return plan, target, source, source_data


def _preview_payload(plan, target, source, source_data, target_id, source_id, source_version):
    """由规划组装预览响应（只读，不改任何版本）。"""
    src_by_uid = {e["uid"]: e for e in source_data["input"]["events"]}
    atts_by_uid = {}
    for a in plan.get("copied_attachments", []):
        atts_by_uid.setdefault(a["event_uid"], []).append(a)

    def copied_event_view(e):
        view = {
            "uid": e.get("uid"),
            "name": e.get("name"),
            "earliest_start": e.get("earliest_start"),
            "latest_start": e.get("latest_start"),
            "duration_minutes": e.get("duration_minutes"),
            "note": e.get("note") or "",
            "timezone": e.get("timezone"),
            # 入选事件随并入复制的附件（元信息；确认后只复制关联，文件共享）
            "attachments": [{
                "blob_sha256": a["blob_sha256"],
                "filename": a["filename"],
                "description": a["description"],
                "content_type": a["content_type"],
                "size": a["size"],
                "uploaded_at": a["uploaded_at"],
            } for a in atts_by_uid.get(e.get("uid"), [])],
        }
        # 预览即展示该事件确认后的来源摘要（只读；不参与 digest/推算）。
        # 始终指向**直接来源**版本中的事件身份：即使来源事件本身也是更早
        # 并入来的，也不沿用其传递来源（不回读、不改写来源历史）。
        # 标题取来源版本不可变快照中的标题（与回看该来源版本所见一致）。
        view["origin"] = make_origin(
            source_id, source_data.get("title") or source["title"],
            source_version, e.get("uid"), e.get("name"))
        return view

    copied_relations = []
    for r in plan["copied_relations"]:
        ae, be = src_by_uid[r["predecessor_uid"]], src_by_uid[r["successor_uid"]]
        copied_relations.append({
            "predecessor": {"uid": r["predecessor_uid"], "name": ae.get("name")},
            "successor": {"uid": r["successor_uid"], "name": be.get("name")},
            "min_gap_minutes": r["min_gap_minutes"],
            "max_gap_minutes": r["max_gap_minutes"],
        })
    copied_attachments = [{
        "event_uid": a["event_uid"],
        "blob_sha256": a["blob_sha256"],
        "filename": a["filename"],
        "description": a["description"],
        "content_type": a["content_type"],
        "size": a["size"],
        "uploaded_at": a["uploaded_at"],
    } for a in plan.get("copied_attachments", [])]
    return {
        "target": {
            "review_id": target_id,
            "title": target["title"],
            "base_version": target["version"],
            "status": target["status"],
        },
        "source": {
            "review_id": source_id,
            "title": source_data.get("title") or source["title"],
            "version": source_version,
            "kind": source_data.get("kind"),
            "state": source_data.get("state"),
        },
        "copied_events": [copied_event_view(e) for e in plan["copied_events"]],
        "copied_relations": copied_relations,
        "copied_attachments": copied_attachments,
        "copied_attachment_count": len(copied_attachments),
        "result": plan["result"],
        "digest": plan["digest"],
    }


def preview_merge(db_path, target_id, source_id, source_version, selected_uids):
    """并入预览：返回复制事件、两端均入选的关系与合并后推算结论。只读，不写库。

    来源/目标相同、事件不存在、来源版本缺快照、选择不合法均抛相应异常，
    不产生任何部分写入。
    """
    conn = connect(db_path)
    try:
        plan, target, source, source_data = _build_merge_plan(
            conn, target_id, source_id, source_version, selected_uids)
        return _preview_payload(plan, target, source, source_data,
                                target_id, source_id, source_version)
    finally:
        conn.close()


def merge_into_review(db_path, target_id, source_id, source_version,
                      selected_uids, base_version, digest):
    """确认并入：按目标 base_version 重新核对，一次性保存为目标的新草稿版本。

    目标版本未变化时重建规划的 digest 必须与预览一致（一致 ⇒ 合并输入相同
    ⇒ 推算结论与预览相同）；版本过期抛 VersionConflict；digest 不符抛
    MergeRejected。复制事件保留时间范围/持续/备注/时区与 UTC 偏移，在
    目标取得全新持续身份；关系保留等待上下界。目标原有事件与来源历史不变。
    冲突或暂存的合并结果允许保存为草稿，但仍不得定稿。

    每个复制事件在换发新 uid 的同时固化一份**来源摘要**（来源复盘 id/标题、
    来源版本、来源事件 uid/名称，均取并入当时的值）并写入 live 行与不可变
    快照；来源摘要之后只随 uid 关联（普通保存/恢复不丢失），不回读来源，
    来源复盘或版本删除后仍可展示，也不影响任何推算。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan, target, _source, _source_data = _build_merge_plan(
            conn, target_id, source_id, source_version, selected_uids)
        if target["version"] != base_version:
            raise VersionConflict(target["version"])
        if not isinstance(digest, str) or not digest.strip():
            raise MergeRejected("缺少预览 digest，无法核对合并内容")
        if plan["digest"] != digest.strip():
            raise MergeRejected(
                "合并内容与预览不一致（目标可能已重新保存，或选择/来源版本被改动），"
                "请重新预览后再确认")

        events, relations = merge.save_payload(
            plan, source_meta={
                "review_id": source_id,
                # 来源复盘标题按来源版本不可变快照中的标题固化（与预览、与
                # 回看该来源版本所见一致；之后来源改名/删除均不影响本摘要）
                "review_title": _source_data.get("title") or _source["title"],
                "version": source_version,
                "event_names": {
                    e.get("uid"): e.get("name")
                    for e in _source_data["input"].get("events", []) if e.get("uid")},
            })
        # save_payload 已为复制事件生成来源摘要，但身份解析（普通保存也用它）
        # 会清空事件自带 origin、只从既有 live 行关联。先解析身份（目标事件
        # 沿用、复制事件换发新 uid），再由这里——并入确认，来源摘要唯一的
        # 服务端注入点——把来源摘要重新挂到复制事件上。
        copied_origins = {e["key"]: e.get("origin") for e in events
                          if e.get("key", "").startswith("m_") and e.get("origin")}
        # save_payload 中复制事件顺序与 plan["copied_events"] 一致，临时 key 为
        # m_<i>_<来源uid前8位>；按同一顺序建立 来源uid → 临时key 的映射。
        copied_keys = [e["key"] for e in events
                       if str(e.get("key", "")).startswith("m_")]
        src_uid_to_key = {
            e["uid"]: copied_keys[i]
            for i, e in enumerate(plan["copied_events"]) if i < len(copied_keys)}
        _resolve_event_uids(conn, target_id, events)
        for e in events:
            origin = copied_origins.get(e.get("key"))
            if origin is not None:
                e["origin"] = origin  # 新持续身份与来源摘要绑定
        # _resolve_event_uids 把 m_ key 事件的 uid 换发为目标的全新值
        new_uid_by_key = {e["key"]: e["uid"] for e in events
                          if str(e.get("key", "")).startswith("m_")}
        src_uid_to_new_uid = {
            src: new_uid_by_key[key] for src, key in src_uid_to_key.items()
            if key in new_uid_by_key}
        _replace_input(conn, target_id, events, relations)
        # 仅复制入选事件的附件关联到复制事件的**全新 uid**：文件按内容寻址共享，
        # 来源（或整个来源复盘）随后删除，目标的当前关联与新快照仍引用同一 blob，
        # GC 不会清理。来源历史不回读、不改写。
        for a in plan.get("copied_attachments", []):
            new_uid = src_uid_to_new_uid.get(a["event_uid"])
            if new_uid is None:
                continue  # 理论不会发生：附件只属于 plan 中的入选事件
            conn.execute(
                "INSERT INTO event_attachments (review_id, event_uid, blob_sha256, filename,"
                " description, content_type, size, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
                (target_id, new_uid, a["blob_sha256"], a["filename"], a["description"],
                 a["content_type"], a["size"], a["uploaded_at"]))
        live_events, live_relations, _, _ = _current_input(conn, target_id)

        new_version = base_version + 1
        title = target["title"]
        now = _now()
        # 并入产生的永远是新草稿：冲突或暂存结果可保存，但仍不得定稿
        conn.execute(
            "UPDATE reviews SET title=?, version=?, status='draft', updated_at=? WHERE id=?",
            (title, new_version, now, target_id))
        _save_version(conn, target_id, new_version, title, live_events, live_relations,
                      "merge", now, source_version=source_version, source_review_id=source_id)
        _commit_and_gc(conn, db_path)
        return new_version
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_review(db_path, review_id, base_version):
    """定稿。存在约束冲突或暂存事件时拒绝；版本过期抛 VersionConflict。

    定稿不改动输入，沿用同一输入重新留存一条记录（kind=finalize），
    以记录定稿状态与推算依据。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        if row["version"] != base_version:
            raise VersionConflict(row["version"])
        comp = conn.execute(
            "SELECT * FROM computations WHERE review_id=? ORDER BY id DESC LIMIT 1",
            (review_id,)).fetchone()
        if comp is None or comp["status"] != "ok":
            raise FinalizeRejected("当前版本存在约束冲突，不得定稿")
        result = json.loads(comp["result_json"])
        if result.get("drafts"):
            raise FinalizeRejected("仍有 {} 个事件处于暂存状态（缺少时间范围），不得定稿".format(
                len(result["drafts"])))
        live_events, live_relations, _, _ = _current_input(conn, review_id)
        new_version = base_version + 1
        now = _now()
        conn.execute(
            "UPDATE reviews SET status='finalized', version=?, updated_at=? WHERE id=?",
            (new_version, now, review_id))
        _save_version(conn, review_id, new_version, row["title"], live_events, live_relations,
                      "finalize", now, source_version=base_version)
        _commit_and_gc(conn, db_path)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_review(db_path, review_id):
    conn = connect(db_path)
    try:
        # ON DELETE CASCADE 一并删除 events/relations/computations/snapshots 及
        # 该复盘的当前/快照附件关联；随后清理失去全部引用的 blob 文件。
        cur = conn.execute("DELETE FROM reviews WHERE id=?", (review_id,))
        if cur.rowcount == 0:
            conn.rollback()
            raise NotFound(review_id)
        _commit_and_gc(conn, db_path)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------- 事故现场附件

MAX_ATTACHMENTS_PER_EVENT = 100
MAX_ATTACHMENTS_PER_REVIEW = 1000


def _event_exists(conn, review_id, event_uid):
    """事件持续身份必须属于该复盘的当前 live 事件（附件只能挂在已保存事件上）。"""
    return conn.execute(
        "SELECT 1 FROM events WHERE review_id=? AND uid=?", (review_id, event_uid)
    ).fetchone() is not None


def add_event_attachment(db_path, review_id, base_version, event_uid, filename,
                         description, content_type, size, sha256, staged_path):
    """把已通过内容校验的临时附件原子入库并挂到事件持续身份，另存新版本。

    与所有写版本操作共用同一乐观锁：base_version 过期抛 VersionConflict（409，
    不产生版本也不留文件）；事件不属于该复盘/尚未保存抛 AttachmentRejected。

    磁盘文件在事务内由临时名 os.replace 为内容哈希：内容已存在则删除临时文件；
    事务失败回滚时，新写入的哈希文件由调用方在异常路径清理（无残留），
    启动扫描兜底崩溃窗口。

    返回 (new_version, 新关联视图 dict)。
    """
    conn = connect(db_path)
    stored_new = False
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        if row["version"] != base_version:
            raise VersionConflict(row["version"])
        if not _event_exists(conn, review_id, event_uid):
            raise AttachmentRejected(
                "事件不存在或尚未保存，请先保存该事件后再上传附件")
        count = conn.execute(
            "SELECT COUNT(*) FROM event_attachments WHERE review_id=?",
            (review_id,)).fetchone()[0]
        if count >= MAX_ATTACHMENTS_PER_REVIEW:
            raise AttachmentRejected(
                "单个复盘附件数量已达上限（{} 个）".format(MAX_ATTACHMENTS_PER_REVIEW))
        event_count = conn.execute(
            "SELECT COUNT(*) FROM event_attachments WHERE review_id=? AND event_uid=?",
            (review_id, event_uid)).fetchone()[0]
        if event_count >= MAX_ATTACHMENTS_PER_EVENT:
            raise AttachmentRejected(
                "单个事件附件数量已达上限（{} 个）".format(MAX_ATTACHMENTS_PER_EVENT))

        root = attachments.storage_dir(db_path)
        blob_exists = conn.execute(
            "SELECT 1 FROM blobs WHERE sha256=?", (sha256,)).fetchone() is not None
        if not blob_exists:
            # 原子改名入库；同一内容并发上传时若文件已落盘，按已存在处理
            stored_new = attachments.store_blob(root, sha256, staged_path)
            conn.execute(
                "INSERT INTO blobs (sha256, size, kind, created_at) VALUES (?,?,?,?)",
                (sha256, size, _attachment_kind(content_type), _now()))
        else:
            # 内容已存在：store_blob 不会被调用，临时文件由调用方 finally 清理
            try:
                os.unlink(staged_path)
            except OSError:
                pass

        now = _now()
        cur = conn.execute(
            "INSERT INTO event_attachments (review_id, event_uid, blob_sha256, filename,"
            " description, content_type, size, uploaded_at) VALUES (?,?,?,?,?,?,?,?)",
            (review_id, event_uid, sha256, filename, description, content_type, size, now))
        attachment_id = cur.lastrowid

        # 附件变更必须产生新版本：沿用当前输入与附件关联重算并写不可变快照
        title = conn.execute("SELECT title FROM reviews WHERE id=?", (review_id,)).fetchone()["title"]
        live_events, live_relations, _, _ = _current_input(conn, review_id)
        new_version = base_version + 1
        conn.execute(
            "UPDATE reviews SET version=?, updated_at=? WHERE id=?",
            (new_version, now, review_id))
        _save_version(conn, review_id, new_version, title, live_events, live_relations,
                      "attachment", now)
        _commit_and_gc(conn, db_path)
        committed = True
        view = {
            "id": attachment_id,
            "review_id": review_id,
            "event_uid": event_uid,
            "blob_sha256": sha256,
            "filename": filename,
            "description": description,
            "content_type": content_type,
            "size": size,
            "uploaded_at": now,
            "download_url": "/api/reviews/{}/attachments/{}/download".format(
                review_id, attachment_id),
        }
        return new_version, view
    except Exception:
        conn.rollback()
        if not committed and stored_new:
            # 事务失败（如并发去重、版本冲突的极小窗口）：仅当该 blob 行确实
            # 未被任何（并发成功的）事务写入时才清理文件，避免误删他人内容。
            check = connect(db_path)
            try:
                still_there = check.execute(
                    "SELECT 1 FROM blobs WHERE sha256=?", (sha256,)).fetchone()
            finally:
                check.close()
            if still_there is None:
                try:
                    attachments.blob_path(attachments.storage_dir(db_path), sha256).unlink()
                except OSError:
                    pass
        raise
    finally:
        conn.close()
        # store_blob 成功改走哈希路径后，原临时路径已不存在；内容已存在时上面已删
        try:
            os.unlink(staged_path)
        except OSError:
            pass


def remove_event_attachment(db_path, review_id, base_version, attachment_id):
    """移除当前附件关联并另存新版本。

    只删除 event_attachments 当前关联；snapshot_attachments 不动——旧版本仍可
    下载原件。若这是 blob 的最后一个当前/历史引用，提交后清理文件。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        if row["version"] != base_version:
            raise VersionConflict(row["version"])
        cur = conn.execute(
            "DELETE FROM event_attachments WHERE id=? AND review_id=?",
            (attachment_id, review_id))
        if cur.rowcount == 0:
            raise AttachmentNotFound(attachment_id)

        now = _now()
        title = conn.execute("SELECT title FROM reviews WHERE id=?", (review_id,)).fetchone()["title"]
        live_events, live_relations, _, _ = _current_input(conn, review_id)
        new_version = base_version + 1
        conn.execute(
            "UPDATE reviews SET version=?, updated_at=? WHERE id=?",
            (new_version, now, review_id))
        _save_version(conn, review_id, new_version, title, live_events, live_relations,
                      "attachment", now)
        _commit_and_gc(conn, db_path)
        return new_version
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_current_attachment(db_path, review_id, attachment_id):
    """按当前关联定位附件：(磁盘路径, 关联行 dict, blob 行)。

    仅当该关联属于该复盘且文件本体存在时返回；否则 AttachmentNotFound。
    """
    conn = connect(db_path)
    try:
        if conn.execute("SELECT id FROM reviews WHERE id=?", (review_id,)).fetchone() is None:
            raise NotFound(review_id)
        row = conn.execute(
            "SELECT * FROM event_attachments WHERE id=? AND review_id=?",
            (attachment_id, review_id)).fetchone()
        if row is None:
            raise AttachmentNotFound(attachment_id)
        blob = conn.execute("SELECT * FROM blobs WHERE sha256=?",
                            (row["blob_sha256"],)).fetchone()
        if blob is None:
            raise AttachmentNotFound(attachment_id)
        path = attachments.blob_path(attachments.storage_dir(db_path), row["blob_sha256"])
        if not path.is_file():
            raise AttachmentNotFound(attachment_id)
        return path, dict(row), dict(blob)
    finally:
        conn.close()


def get_version_attachment(db_path, review_id, version, attachment_id):
    """按历史版本快照定位附件：(磁盘路径, 快照关联行 dict)。

    仅当该快照关联属于该复盘该版本且文件本体存在时返回——附件已从当前移除后，
    旧版本仍经此路径下载原件。复盘/版本不存在按 NotFound/VersionNotFound 抛出。
    """
    conn = connect(db_path)
    try:
        review = conn.execute("SELECT id FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise NotFound(review_id)
        snap = conn.execute(
            "SELECT 1 FROM snapshots WHERE review_id=? AND review_version=?",
            (review_id, version)).fetchone()
        if snap is None:
            comp = conn.execute(
                "SELECT 1 FROM computations WHERE review_id=? AND review_version=?",
                (review_id, version)).fetchone()
            if comp is not None:
                raise LegacyVersion(version)
            raise VersionNotFound(version)
        row = conn.execute(
            "SELECT * FROM snapshot_attachments WHERE id=? AND review_id=? AND review_version=?",
            (attachment_id, review_id, version)).fetchone()
        if row is None:
            raise AttachmentNotFound(attachment_id)
        path = attachments.blob_path(attachments.storage_dir(db_path), row["blob_sha256"])
        if not path.is_file():
            raise AttachmentNotFound(attachment_id)
        return path, dict(row)
    finally:
        conn.close()
