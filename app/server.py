#!/usr/bin/env python3
"""事故复盘时间线 —— HTTP 服务端（仅依赖 Python 标准库）。

路由：
  GET    /                        单页应用
  GET    /static/*                前端静态资源
  GET    /api/reviews             复盘列表
  POST   /api/reviews             新建复盘  {title}
  GET    /api/reviews/{id}        复盘快照（输入 + 最近一次推算结果）
  PUT    /api/reviews/{id}        整体保存并重新推算  {base_version, title, events, relations}
  POST   /api/reviews/{id}/finalize  定稿  {base_version}
  GET    /api/reviews/{id}/versions       可回看版本列表（含不可回看的旧库历史版本标记）
  GET    /api/reviews/{id}/versions/{v}   某版本回看：当时输入 + 推算依据 + 状态
  GET    /api/reviews/{id}/versions/{v}/diagnosis 某版本冲突的最小不可满足约束组分析（只读）
  POST   /api/reviews/{id}/versions/{v}/restore  恢复为新草稿版本 {base_version}
  GET    /api/reviews/{id}/compare?from={v1}&to={v2}  两版本对照（只读，按事件持续身份配对）
  POST   /api/reviews/{id}/merge/preview              并入预览（只读）：{source_review_id, source_version, event_uids}
  POST   /api/reviews/{id}/merge/confirm              确认并入，另存为新草稿版本：{source_review_id, source_version, event_uids, base_version, digest}
  POST   /api/reviews/{id}/events/{uid}/attachments   上传附件（multipart：file + description + base_version），另存新版本
  DELETE /api/reviews/{id}/attachments/{aid}          移除当前附件关联（只影响新版本，旧版本仍可下载）
  GET    /api/reviews/{id}/attachments/{aid}/download           下载当前附件原件
  GET    /api/reviews/{id}/versions/{v}/attachments/{aid}/download  下载某历史版本留存的附件原件
  DELETE /api/reviews/{id}        删除复盘

并发控制：所有修改请求必须携带 base_version，与服务器当前版本一致才接受，
否则返回 409，客户端提示刷新。
"""

import json
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import attachments
import db
import tzutil

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR.parent / "data" / "timeline.db"))

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, **extra}


def validate_event(e):
    """校验并规范化单个事件输入，返回规范后的 dict。

    未指定时区（timezone 为空）：时间按既有朴素本地时间语义处理，不得携带
    UTC 偏移，以免静默改变旧语义。
    指定 IANA 时区：填写的时间必须携带 UTC 偏移，且偏移符合该时区当日规则；
    夏令时不存在的当地时刻或错误偏移一律拒绝（HTTP 400），整次保存不生效。
    """
    if not isinstance(e, dict):
        raise ApiError(400, "事件格式不正确")
    name = str(e.get("name") or "").strip()
    if not name:
        raise ApiError(400, "事件名称不能为空")
    if len(name) > 200:
        raise ApiError(400, "事件名称过长")

    try:
        timezone = tzutil.clean_tz_name(e.get("timezone"))
    except tzutil.TimezoneError as exc:
        raise ApiError(400, "事件「{}」的{}".format(name, exc))

    def norm_time(value, label):
        if value in (None, ""):
            return None
        if timezone:
            try:
                return tzutil.validate_local_time(str(value), timezone)
            except tzutil.TimezoneError as exc:
                raise ApiError(400, "事件「{}」的{}：{}".format(name, label, exc))
        # 未指定时区：维持旧语义；明确拒绝携带偏移的输入
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            raise ApiError(400, "事件「{}」的{}格式不正确".format(name, label))
        if parsed.tzinfo is not None:
            raise ApiError(
                400, "事件「{}」未指定时区，{}不得携带 UTC 偏移；如需跨地区时间，"
                     "请填写 IANA 时区".format(name, label))
        return parsed.isoformat(timespec="minutes")

    duration = e.get("duration_minutes")
    if duration in (None, ""):
        duration = None
    else:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            raise ApiError(400, "事件「{}」的持续分钟数必须是整数".format(name))
        if not 0 <= duration <= 24 * 60 * 30:
            raise ApiError(400, "事件「{}」的持续分钟数超出合理范围".format(name))

    earliest = norm_time(e.get("earliest_start"), "最早开始时间")
    latest = norm_time(e.get("latest_start"), "最晚开始时间")

    return {
        "key": str(e.get("key") or ""),
        "uid": str(e.get("uid") or "")[:64],
        "name": name,
        "earliest_start": earliest,
        "latest_start": latest,
        "duration_minutes": duration,
        "note": str(e.get("note") or "")[:1000],
        "timezone": timezone,
    }


def _norm_gap(value, label, rel_desc):
    """规范化“先于”关系的间隔分钟数。

    返回 None 表示未设置；否则为非负整数。非法输入（负数、非整数、
    超出合理范围）抛 ApiError(400)，在写库前拒绝，记录保持不变。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ApiError(400, "关系{}的{}必须是非负整数分钟".format(rel_desc, label))
    if isinstance(value, int):
        iv = value
    elif isinstance(value, float) and value.is_integer():
        iv = int(value)
    elif isinstance(value, str):
        try:
            iv = int(value.strip())
        except ValueError:
            raise ApiError(400, "关系{}的{}必须是非负整数分钟".format(rel_desc, label))
    else:
        raise ApiError(400, "关系{}的{}必须是非负整数分钟".format(rel_desc, label))
    if iv < 0:
        raise ApiError(400, "关系{}的{}必须是非负整数分钟".format(rel_desc, label))
    if iv > 24 * 60 * 30:
        raise ApiError(400, "关系{}的{}超出合理范围（0～{} 分钟）".format(
            rel_desc, label, 24 * 60 * 30))
    return iv


def validate_payload(body):
    if not isinstance(body, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    if not isinstance(body.get("base_version"), int):
        raise ApiError(400, "缺少 base_version（用于并发版本校验）")
    title = str(body.get("title") or "").strip() or "未命名复盘"
    events = [validate_event(e) for e in body.get("events") or []]
    keys = [e["key"] for e in events]
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        raise ApiError(400, "事件 key 缺失或重复")
    key_set = set(keys)
    name_by_key = {e["key"]: e["name"] for e in events}
    tz_by_key = {e["key"]: bool(e.get("timezone")) for e in events}
    relations = []
    for r in body.get("relations") or []:
        if not isinstance(r, dict):
            raise ApiError(400, "“先于”关系格式不正确")
        a, b = str(r.get("predecessor_key") or ""), str(r.get("successor_key") or "")
        if a not in key_set or b not in key_set:
            raise ApiError(400, "“先于”关系引用了不存在的事件")
        if tz_by_key[a] != tz_by_key[b]:
            raise ApiError(
                400, "带时区事件与未带时区事件之间不得建立“先于”关系：「{}」{}，"
                     "「{}」{}；请为两端统一指定或清空时区".format(
                        name_by_key[a], "带时区" if tz_by_key[a] else "未带时区",
                        name_by_key[b], "带时区" if tz_by_key[b] else "未带时区"))
        desc = "「{}」先于「{}」".format(name_by_key[a], name_by_key[b])
        min_gap = _norm_gap(r.get("min_gap_minutes"), "最短等待", desc)
        if min_gap is None:
            min_gap = 0  # 未设置：沿用旧语义（前项结束不晚于后项开始）
        max_gap = _norm_gap(r.get("max_gap_minutes"), "最长等待", desc)
        if max_gap is not None and max_gap < min_gap:
            raise ApiError(400, "关系{}的最长等待不得短于最短等待".format(desc))
        relations.append({
            "predecessor_key": a,
            "successor_key": b,
            "min_gap_minutes": min_gap,
            "max_gap_minutes": max_gap,
        })
    return body["base_version"], title, events, relations


def validate_merge_body(body, require_base=False):
    """校验并入请求体，返回 (source_review_id, source_version, event_uids, base_version)。

    预览与确认共用来源参数校验；确认额外要求 base_version 为整数。
    来源/目标相同、事件不存在等业务校验在 db 层完成（拒绝且无部分写入）。
    """
    if not isinstance(body, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    sid = body.get("source_review_id")
    sver = body.get("source_version")
    if not isinstance(sid, int) or isinstance(sid, bool) or sid <= 0:
        raise ApiError(400, "缺少有效的 source_review_id（来源复盘 id）")
    if not isinstance(sver, int) or isinstance(sver, bool) or sver <= 0:
        raise ApiError(400, "缺少有效的 source_version（来源复盘的可回看版本号）")
    uids = body.get("event_uids")
    if not isinstance(uids, list) or not uids:
        raise ApiError(400, "缺少 event_uids：必须至少选择一个来源事件的 uid")
    if len(uids) > 10000:
        raise ApiError(400, "event_uids 数量超出合理范围")
    clean = []
    for u in uids:
        if not isinstance(u, str) or not u.strip():
            raise ApiError(400, "event_uids 必须是非空字符串（来源事件的持续身份 uid）")
        if len(u) > 64:
            raise ApiError(400, "event_uids 中存在非法的 uid")
        clean.append(u.strip())
    if len(set(clean)) != len(clean):
        raise ApiError(400, "event_uids 中存在重复的事件 uid")
    base_version = None
    if require_base:
        base_version = body.get("base_version")
        if not isinstance(base_version, int) or isinstance(base_version, bool):
            raise ApiError(400, "缺少 base_version（用于并发版本校验）")
        if not isinstance(body.get("digest"), str) or not body.get("digest", "").strip():
            raise ApiError(400, "缺少 digest（预览返回的合并内容核对值）")
    return sid, sver, clean, base_version


class Handler(BaseHTTPRequestHandler):
    server_version = "RetroTimeline/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 工具
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        ctype = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_attachment_download(self, path, row):
        """以原始字节流式回送附件原件（Content-Disposition 带上传时文件名）。"""
        size = os.path.getsize(path)
        filename = row.get("filename") or path.name
        # RFC 5987：filename* 承载 UTF-8 文件名，ASCII 回退给老式客户端
        ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "attachment"
        self.send_response(200)
        self.send_header("Content-Type", row.get("content_type") or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
                ascii_fallback.replace('"', ""), quote(filename)))
        self.send_header("X-SHA-256", row.get("blob_sha256", ""))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2 * 1024 * 1024:
            raise ApiError(400, "请求体为空或过大")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "请求体不是合法的 JSON")

    def _handle_attachment_upload(self, rid, event_uid):
        """multipart/form-data 上传附件：file（必填）+ description + base_version。

        校验（类型/大小/魔数一致性）与数据库事务全部失败时返回 4xx 且**不留下
        文件**：multipart 阶段产生的临时文件在解析/校验失败时立即删除；事务
        阶段失败由 db 层回滚并清理，关闭连接避免请求体残余影响复用。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 不合法")
        root = attachments.ensure_storage_dir(DB_PATH)
        try:
            fields, parts = attachments.parse_multipart(
                self.rfile, length, self.headers.get("Content-Type"), root)
        except attachments.MultipartError as e:
            self.close_connection = True
            raise ApiError(400, str(e))
        except attachments.UploadError as e:
            self.close_connection = True
            raise ApiError(400, str(e))
        try:
            if len(parts) != 1 or parts[0].field_name != "file":
                raise ApiError(400, "缺少上传文件字段 file（且一次只能上传一个文件）")
            part = parts[0]
            raw_base = fields.get("base_version", "")
            if isinstance(raw_base, str) and re.fullmatch(r"\d+", raw_base.strip()):
                base_version = int(raw_base.strip())
            else:
                raise ApiError(400, "缺少 base_version（用于并发版本校验）")
            description = fields.get("description", "")
            try:
                filename, description = attachments.validate_metadata(
                    part.filename, description)
            except attachments.UploadError as e:
                raise ApiError(400, str(e))
            try:
                sha256, size, content_type = attachments.validate_staged_file(
                    part.path, filename)
            except attachments.UploadError as e:
                raise ApiError(400, str(e))
            try:
                new_version, view = db.add_event_attachment(
                    DB_PATH, rid, base_version, event_uid, filename, description,
                    content_type, size, sha256, part.path)
            except db.AttachmentRejected as e:
                raise ApiError(400, str(e))
            bundle = db.get_bundle(DB_PATH, rid)
            bundle["added_attachment"] = view
            bundle["new_version"] = new_version
            self._send_json(201, bundle)
        finally:
            attachments.cleanup_parts(parts)

    def _handle_attachment_remove(self, rid, attachment_id):
        body = self._read_json()
        base_version = body.get("base_version")
        if not isinstance(base_version, int) or isinstance(base_version, bool):
            raise ApiError(400, "缺少 base_version（用于并发版本校验）")
        db.remove_event_attachment(DB_PATH, rid, base_version, attachment_id)
        self._send_json(200, db.get_bundle(DB_PATH, rid))

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if method == "GET" and path == "/":
                return self._send_file(STATIC_DIR / "index.html")
            m = re.fullmatch(r"/static/([\w.\-]+)", path)
            if method == "GET" and m:
                target = (STATIC_DIR / m.group(1)).resolve()
                if target.is_file() and STATIC_DIR in target.parents:
                    return self._send_file(target)
                raise ApiError(404, "not found")

            if path == "/api/reviews":
                if method == "GET":
                    return self._send_json(200, {"reviews": db.list_reviews(DB_PATH)})
                if method == "POST":
                    body = self._read_json()
                    title = str(body.get("title") or "").strip() or "未命名复盘"
                    rid = db.create_review(DB_PATH, title)
                    return self._send_json(201, db.get_bundle(DB_PATH, rid))

            m = re.fullmatch(r"/api/reviews/(\d+)", path)
            if m:
                rid = int(m.group(1))
                if method == "GET":
                    return self._send_json(200, db.get_bundle(DB_PATH, rid))
                if method == "PUT":
                    base_version, title, events, relations = validate_payload(self._read_json())
                    db.update_review(DB_PATH, rid, base_version, title, events, relations)
                    return self._send_json(200, db.get_bundle(DB_PATH, rid))
                if method == "DELETE":
                    db.delete_review(DB_PATH, rid)
                    return self._send_json(200, {"ok": True})

            m = re.fullmatch(r"/api/reviews/(\d+)/finalize", path)
            if m and method == "POST":
                rid = int(m.group(1))
                body = self._read_json()
                if not isinstance(body.get("base_version"), int):
                    raise ApiError(400, "缺少 base_version")
                db.finalize_review(DB_PATH, rid, body["base_version"])
                return self._send_json(200, db.get_bundle(DB_PATH, rid))

            m = re.fullmatch(r"/api/reviews/(\d+)/versions", path)
            if m and method == "GET":
                rid = int(m.group(1))
                return self._send_json(200, {
                    "current_version": db.current_version(DB_PATH, rid),
                    "versions": db.list_versions(DB_PATH, rid),
                })

            m = re.fullmatch(r"/api/reviews/(\d+)/versions/(\d+)", path)
            if m and method == "GET":
                rid, ver = int(m.group(1)), int(m.group(2))
                return self._send_json(200, db.get_version(DB_PATH, rid, ver))

            m = re.fullmatch(r"/api/reviews/(\d+)/versions/(\d+)/diagnosis", path)
            if m and method == "GET":
                rid, ver = int(m.group(1)), int(m.group(2))
                try:
                    return self._send_json(
                        200, db.analyze_version(DB_PATH, rid, ver))
                except db.LegacyVersion as e:
                    return self._send_json(422, {
                        "error": db.LEGACY_DIAGNOSE_REASON,
                        "analyzable": False,
                        "version": e.args[0] if e.args else ver,
                    })

            m = re.fullmatch(r"/api/reviews/(\d+)/versions/(\d+)/restore", path)
            if m and method == "POST":
                rid, ver = int(m.group(1)), int(m.group(2))
                body = self._read_json()
                if not isinstance(body.get("base_version"), int):
                    raise ApiError(400, "缺少 base_version（用于并发版本校验）")
                db.restore_review(DB_PATH, rid, body["base_version"], ver)
                return self._send_json(200, db.get_bundle(DB_PATH, rid))

            m = re.fullmatch(r"/api/reviews/(\d+)/compare", path)
            if m and method == "GET":
                rid = int(m.group(1))
                qs = parse_qs(urlparse(self.path).query)

                def version_param(name):
                    raw = qs.get(name, [None])[0]
                    try:
                        return int(raw)
                    except (TypeError, ValueError):
                        raise ApiError(
                            400, "对照参数 {} 缺失或不是有效版本号".format(name))

                from_v, to_v = version_param("from"), version_param("to")
                try:
                    return self._send_json(
                        200, db.compare_versions(DB_PATH, rid, from_v, to_v))
                except db.LegacyVersion as e:
                    return self._send_json(422, {
                        "error": db.LEGACY_COMPARE_REASON,
                        "comparable": False,
                        "version": e.args[0] if e.args else None,
                    })

            m = re.fullmatch(r"/api/reviews/(\d+)/merge/(preview|confirm)", path)
            if m and method == "POST":
                rid = int(m.group(1))
                action = m.group(2)
                body = self._read_json()
                if action == "preview":
                    sid, sver, uids, _ = validate_merge_body(body, require_base=False)
                    try:
                        return self._send_json(200, db.preview_merge(
                            DB_PATH, rid, sid, sver, uids))
                    except db.LegacyVersion:
                        return self._send_json(422, {
                            "error": db.LEGACY_MERGE_REASON,
                            "mergable": False,
                            "source_review_id": sid, "source_version": sver,
                        })
                sid, sver, uids, base_version = validate_merge_body(body, require_base=True)
                try:
                    db.merge_into_review(DB_PATH, rid, sid, sver, uids,
                                         base_version, body["digest"])
                except db.LegacyVersion:
                    return self._send_json(422, {
                        "error": db.LEGACY_MERGE_REASON,
                        "mergable": False,
                        "source_review_id": sid, "source_version": sver,
                    })
                return self._send_json(200, db.get_bundle(DB_PATH, rid))

            # 附件：上传（multipart/form-data）
            m = re.fullmatch(r"/api/reviews/(\d+)/events/([0-9a-fA-F]{8,64})/attachments", path)
            if m and method == "POST":
                return self._handle_attachment_upload(
                    int(m.group(1)), m.group(2).lower())

            # 附件：移除当前关联（旧版本快照不动）
            m = re.fullmatch(r"/api/reviews/(\d+)/attachments/(\d+)", path)
            if m and method == "DELETE":
                return self._handle_attachment_remove(
                    int(m.group(1)), int(m.group(2)))

            # 附件：下载当前附件原件
            m = re.fullmatch(r"/api/reviews/(\d+)/attachments/(\d+)/download", path)
            if m and method == "GET":
                try:
                    fpath, row, _blob = db.get_current_attachment(
                        DB_PATH, int(m.group(1)), int(m.group(2)))
                except db.AttachmentNotFound:
                    raise ApiError(404, "附件不存在或文件已不可用")
                return self._send_attachment_download(fpath, row)

            # 附件：下载某历史版本留存的原件（移除当前附件后旧版本仍可下载）
            m = re.fullmatch(
                r"/api/reviews/(\d+)/versions/(\d+)/attachments/(\d+)/download", path)
            if m and method == "GET":
                try:
                    fpath, row = db.get_version_attachment(
                        DB_PATH, int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except db.AttachmentNotFound:
                    raise ApiError(404, "附件不存在或文件已不可用")
                return self._send_attachment_download(fpath, row)

            raise ApiError(404, "not found")
        except ApiError as e:
            self._send_json(e.status, e.payload)
        except db.NotFound:
            self._send_json(404, {"error": "复盘不存在"})
        except db.VersionNotFound:
            self._send_json(404, {"error": "该版本不存在"})
        except db.LegacyVersion:
            self._send_json(422, {"error": db.LEGACY_REASON,
                                  "reviewable": False, "restorable": False})
        except db.VersionConflict as e:
            self._send_json(409, {
                "error": "该复盘已被其他成员保存了更新的版本，你的修改未保存，请刷新后重试",
                "current_version": e.current_version,
            })
        except db.InvalidCompareOrder as e:
            self._send_json(400, {"error": str(e)})
        except db.MergeRejected as e:
            self._send_json(400, {"error": str(e)})
        except db.FinalizeRejected as e:
            self._send_json(400, {"error": str(e)})
        except db.AttachmentNotFound:
            self._send_json(404, {"error": "附件不存在或文件已不可用"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main():
    db.init_db(DB_PATH)  # 启动时初始化数据库
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("事故复盘时间线服务已启动: http://{}:{}  (数据库: {})".format(HOST, PORT, DB_PATH))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
