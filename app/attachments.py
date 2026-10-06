"""事故现场附件：内容寻址存储、multipart 解析与上传校验（仅标准库）。

设计要点
========
- 文件按 **SHA-256 内容寻址**存放在存储目录（``<dir>/<sha256>``）：同一文件
  被多个事件、多个版本、多份复盘引用时只占一份磁盘空间。
- 入库流程：multipart 中的文件部分先落为同目录下的 ``.upload-*`` 临时文件，
  校验/数据库事务全部成功后由 db 层 ``os.replace`` 原子改名为内容哈希；
  任何失败（格式不符、超过 20MB、版本 409、事件不存在……）临时文件立即删除，
  **绝不留下文件**。事务提交后崩溃留下的游离文件由启动扫描清理（见
  ``sweep_orphan_files``）。
- 允许的类型：文本（.txt/.log/.md/.csv/.json，内容必须能按 UTF-8 解码且不含
  NUL）、PNG、JPEG（.jpg/.jpeg）、PDF。扩展名声明与内容魔数嗅探**必须一致**
  （把文本改名为 .png、把 PNG 改名为 .txt 一律拒绝）。
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

MAX_FILE_SIZE = 20 * 1024 * 1024          # 单文件不超过 20MB
MAX_REQUEST_SIZE = MAX_FILE_SIZE + 2 * 1024 * 1024  # 表单字段余量
MAX_FIELD_SIZE = 64 * 1024                # 普通表单字段（说明等）上限
READ_CHUNK = 1024 * 1024

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
PDF_SIGNATURE = b"%PDF-"

TEXT_EXTENSIONS = {".txt", ".log", ".md", ".csv", ".json"}
IMAGE_EXTENSIONS = {".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg"}
DOCUMENT_EXTENSIONS = {".pdf": "application/pdf"}

CONTENT_TYPE_BY_KIND = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "pdf": "application/pdf",
    "text": "text/plain; charset=utf-8",
}

_SAFE_FILENAME = re.compile(r"^[^/\x00-\x1f\\]+$")


class UploadError(ValueError):
    """上传内容不合法（HTTP 400）：格式、大小、文件名等不符。"""


class MultipartError(UploadError):
    """multipart/form-data 请求本身无法解析。"""


class UploadedPart:
    """解析出的文件部分：filename/content_type 来自表单头，path 为临时文件。"""

    __slots__ = ("field_name", "filename", "content_type", "path", "size")

    def __init__(self, field_name, filename, content_type, path, size):
        self.field_name = field_name
        self.filename = filename
        self.content_type = content_type or "application/octet-stream"
        self.path = path
        self.size = size


def storage_dir(db_path):
    """附件存储目录：ATTACH_DIR 环境变量优先，否则为数据库同目录的 attachments/。"""
    env = os.environ.get("ATTACH_DIR")
    if env:
        return Path(env)
    return Path(db_path).resolve().parent / "attachments"


def blob_path(root, sha256):
    return Path(root) / sha256


def ensure_storage_dir(db_path):
    root = storage_dir(db_path)
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------- 校验

def sniff_kind(data):
    """按内容魔数/可解码性嗅探类型：png | jpeg | pdf | text，无法识别返回 None。"""
    if data.startswith(PNG_SIGNATURE):
        return "png"
    if data.startswith(JPEG_SIGNATURE):
        return "jpeg"
    if data.startswith(PDF_SIGNATURE):
        return "pdf"
    if b"\x00" in data:
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "text"


def declared_kind(filename):
    """按扩展名声明的类型；扩展名不在允许集合返回 None。"""
    from os import path as _p
    ext = _p.splitext(filename)[1].lower()
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in IMAGE_EXTENSIONS:
        return "jpeg" if ext in (".jpg", ".jpeg") else "png"
    if ext in DOCUMENT_EXTENSIONS:
        return "pdf"
    return None


def validate_metadata(filename, description):
    """校验文件名与说明（不读取文件内容）。返回去掉首尾空白的文件名。"""
    if not isinstance(filename, str) or not filename.strip():
        raise UploadError("缺少附件文件名")
    filename = filename.strip()
    if len(filename) > 200:
        raise UploadError("附件文件名过长（最多 200 字符）")
    # 只要一个路径片段：拒绝分隔符与控制字符，绝不信任客户端给的目录
    if not _SAFE_FILENAME.match(filename) or filename in (".", ".."):
        raise UploadError("附件文件名不合法")
    if declared_kind(filename) is None:
        raise UploadError(
            "不支持的附件类型：仅支持文本（.txt/.log/.md/.csv/.json）、"
            "PNG（.png）、JPEG（.jpg/.jpeg）或 PDF（.pdf）")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise UploadError("附件说明必须是文本")
    if len(description) > 1000:
        raise UploadError("附件说明过长（最多 1000 字）")
    return filename, description


def validate_staged_file(part_path, filename):
    """读取临时文件，完成大小/内容嗅探/扩展名一致性校验。

    返回 (sha256, size, content_type)。不合格抛 UploadError——调用方负责
    删除临时文件（本函数不删除，统一由上层 finally 处理）。
    """
    size = os.path.getsize(part_path)
    if size == 0:
        raise UploadError("附件为空文件，拒绝上传")
    if size > MAX_FILE_SIZE:
        raise UploadError("附件超过 20MB 上限（实际 {} 字节），拒绝上传".format(size))
    expected = declared_kind(filename)
    if expected is None:
        raise UploadError(
            "不支持的附件类型：仅支持文本（.txt/.log/.md/.csv/.json）、"
            "PNG（.png）、JPEG（.jpg/.jpeg）或 PDF（.pdf）")
    with open(part_path, "rb") as f:
        head = f.read(512)
    expected = declared_kind(filename)
    actual = sniff_kind(head)
    # 短文件可能嗅不完整：再用完整内容兜底（20MB 上限内可接受）
    if actual is None or (expected in ("png", "jpeg", "pdf") and actual != expected):
        with open(part_path, "rb") as f:
            actual = sniff_kind(f.read())
    if actual != expected:
        # 无法识别（如二进制当文本）或扩展名与内容不一致：一律拒绝
        if actual is None:
            raise UploadError(
                "附件内容无法识别为允许的格式（文本须为合法 UTF-8 且不含 NUL 字节），"
                "拒绝上传")
        raise UploadError(
            "附件内容与扩展名 .{} 不符：实际内容为 {}，拒绝上传".format(
                os.path.splitext(filename)[1].lower().lstrip("."),
                CONTENT_TYPE_BY_KIND.get(actual, actual)))
    h = hashlib.sha256()
    with open(part_path, "rb") as f:
        for chunk in iter(lambda: f.read(READ_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest(), size, CONTENT_TYPE_BY_KIND[actual]


# ---------------------------------------------------------------- 存储

def stage_upload(root):
    """在存储目录内创建临时文件（与目标同文件系统，后续可原子改名）。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".upload-", dir=str(root))
    os.close(fd)
    return path


def store_blob(root, sha256, staged_path):
    """把临时文件原子改名为内容哈希。

    返回 True 表示新写入；False 表示相同内容已存在（调用方负责清理临时文件）。
    """
    target = blob_path(root, sha256)
    if target.exists():
        return False
    tmp = target.with_name(".tmp-blob-" + os.urandom(8).hex())
    os.replace(staged_path, tmp)
    os.replace(tmp, target)
    return True


def sweep_orphan_files(root, known_shas):
    """清理目录中不在 known_shas 内的游离文件（崩溃残留），返回删除数量。

    只处理本模块写入的两类文件：正式内容文件（64 位十六进制哈希名）与
    ``.upload-*`` / ``.tmp-blob-*`` 临时文件。
    """
    root = Path(root)
    if not root.is_dir():
        return 0
    known = set(known_shas)
    removed = 0
    for p in root.iterdir():
        name = p.name
        is_blob = len(name) == 64 and all(c in "0123456789abcdef" for c in name)
        if is_blob:
            if name not in known and p.is_file():
                p.unlink()
                removed += 1
        elif name.startswith((".upload-", ".tmp-blob-")) and p.is_file():
            p.unlink()
            removed += 1
    return removed


# ---------------------------------------------------------- multipart 解析

def _parse_header_params(header):
    """取 multipart 边界等参数：返回 (主值, {参数名: 参数值})。"""
    parts = [p.strip() for p in header.split(";") if p.strip()]
    if not parts:
        raise MultipartError("无法解析 multipart 请求头")
    params = {}
    for p in parts[1:]:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        params[k.strip().lower()] = v.strip().strip('"')
    return parts[0].lower(), params


def _parse_part_headers(block):
    headers = {}
    for line in block.split(b"\r\n"):
        if b":" not in line:
            continue
        k, v = line.split(b":", 1)
        headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
    return headers


def _content_disposition(headers):
    """从 Content-Disposition 取 (name, filename)，非表单部分返回 None。"""
    raw = headers.get("content-disposition")
    if not raw:
        return None
    main, params = _parse_header_params(raw)
    if main != "form-data" or "name" not in params:
        return None
    return params["name"], params.get("filename")


def parse_multipart(stream, content_length, content_type_header,
                    root, max_total=MAX_REQUEST_SIZE, max_file=MAX_FILE_SIZE):
    """解析 multipart/form-data 请求。

    返回 (fields, files)：fields 为普通字段名→字符串值；files 为 UploadedPart
    列表（文件已落在存储目录的临时文件中，调用方最终负责清理）。

    超过请求总量/单文件上限、无法解析一律抛 MultipartError；抛错前已写入的
    临时文件会被尽力清理。单文件超限在写临时文件**之前**判定，不产生文件。
    """
    if not content_type_header:
        raise MultipartError("缺少 Content-Type: multipart/form-data 请求头")
    main, params = _parse_header_params(content_type_header)
    if main != "multipart/form-data" or not params.get("boundary"):
        raise MultipartError("不是 multipart/form-data 请求或缺少 boundary")
    if not isinstance(content_length, int) or content_length <= 0:
        raise MultipartError("缺少请求体")
    if content_length > max_total:
        raise UploadError("请求体超过上传上限（单文件不得超过 20MB）")

    body = b""
    remaining = content_length
    while remaining > 0:
        chunk = stream.read(min(READ_CHUNK, remaining))
        if not chunk:
            break
        body += chunk
        remaining -= len(chunk)
    if len(body) != content_length:
        raise MultipartError("请求体读取不完整")

    delimiter = b"--" + params["boundary"].encode("latin1")
    fields, files, staged = {}, [], []
    try:
        chunks = body.split(delimiter)
        if len(chunks) < 3 or not chunks[-1].startswith(b"--"):
            raise MultipartError("multipart 边界不完整")
        for chunk in chunks[1:-1]:
            if not chunk.startswith(b"\r\n"):
                raise MultipartError("multipart 分段格式不正确")
            chunk = chunk[2:]
            if chunk.endswith(b"\r\n"):
                chunk = chunk[:-2]
            sep = chunk.find(b"\r\n\r\n")
            if sep < 0:
                raise MultipartError("multipart 分段缺少头部分隔")
            headers = _parse_part_headers(chunk[:sep])
            payload = chunk[sep + 4:]
            disp = _content_disposition(headers)
            if disp is None:
                continue
            name, filename = disp
            if filename is not None:
                if len(payload) > max_file:
                    raise UploadError(
                        "附件超过 20MB 上限（实际 {} 字节），拒绝上传".format(len(payload)))
                part_path = stage_upload(root)
                with open(part_path, "wb") as f:
                    f.write(payload)
                part = UploadedPart(
                    name, filename, headers.get("content-type"), part_path, len(payload))
                files.append(part)
                staged.append(part_path)
            else:
                if len(payload) > MAX_FIELD_SIZE:
                    raise UploadError("表单字段 {} 过长".format(name))
                try:
                    fields[name] = payload.decode("utf-8")
                except UnicodeDecodeError:
                    raise UploadError("表单字段 {} 不是合法 UTF-8".format(name))
        return fields, files
    except Exception:
        for p in staged:
            try:
                os.unlink(p)
            except OSError:
                pass
        raise


def cleanup_parts(parts):
    """统一清理文件部分临时文件；已被原子改名入库的路径会自动跳过。"""
    for part in parts:
        try:
            os.unlink(part.path)
        except OSError:
            pass
