"""事故复盘证据附件：类型/大小校验与 multipart/form-data 解析（仅标准库）。

允许上传的现场证据：
  - 文本：.txt .text .log .md .csv .json .xml（必须是合法 UTF-8、不含 NUL）
  - 图片：.png（PNG 魔数）、.jpg/.jpeg（JPEG 魔数）
  - PDF：.pdf（%PDF- 魔数）
单文件大小不得超过 MAX_SIZE（20 MiB）。

校验以**扩展名 + 文件内容魔数**双重判定：扩展名不在允许清单，或扩展名与
实际内容不符（例如把可执行文件改名为 .png）一律拒绝，由调用方保证不落盘、
不写库。本模块只做纯解析与判定，不访问数据库或文件系统（落盘由 db 层
以内容寻址方式完成）。
"""

import os
import re
from urllib.parse import unquote_to_bytes

urllib_parse_unquote = lambda pct: unquote_to_bytes(pct).decode("utf-8", "replace")

MAX_SIZE = 20 * 1024 * 1024  # 单文件上限：20 MiB
# multipart 信封（boundary、头部）允许的额外开销，超过即判定请求体过大
MAX_ENVELOPE = 1 * 1024 * 1024
MAX_FILENAME_LEN = 255
MAX_DESCRIPTION_LEN = 2000

_TEXT_TYPES = {
    ".txt": "text/plain; charset=utf-8",
    ".text": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
}
_BINARY_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}
ALLOWED_EXTENSIONS = sorted(set(_TEXT_TYPES) | set(_BINARY_TYPES))

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


class AttachmentError(Exception):
    """附件请求不合法：status 为建议 HTTP 状态（400 格式问题 / 413 过大）。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def normalize_filename(raw):
    """清洗客户端给出的文件名：去路径、去控制字符、限长；非法则拒绝。"""
    if not isinstance(raw, str):
        raise AttachmentError(400, "缺少文件名")
    name = raw.replace("\\", "/")
    name = os.path.basename(name).strip()
    if not name or name in (".", ".."):
        raise AttachmentError(400, "文件名不能为空")
    if any(ord(c) < 32 for c in name):
        raise AttachmentError(400, "文件名包含非法控制字符")
    if len(name) > MAX_FILENAME_LEN:
        raise AttachmentError(
            400, "文件名过长（最多 {} 个字符）".format(MAX_FILENAME_LEN))
    return name


def normalize_description(raw):
    """说明可空；限长，去掉首尾空白。"""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise AttachmentError(400, "附件说明必须是文本")
    text = raw.strip()
    if len(text) > MAX_DESCRIPTION_LEN:
        raise AttachmentError(
            400, "附件说明过长（最多 {} 个字符）".format(MAX_DESCRIPTION_LEN))
    return text


def sniff(filename, data):
    """按扩展名 + 内容魔数判定类型，返回存储用 Content-Type。

    扩展名缺失/不在允许清单、内容与扩展名不符、文本不是合法 UTF-8 或含
    NUL 字节、超过 20 MiB 一律抛 AttachmentError，拒绝且不留文件。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise AttachmentError(400, "附件内容不正确")
    size = len(data)
    if size == 0:
        raise AttachmentError(400, "附件内容为空")
    if size > MAX_SIZE:
        raise AttachmentError(
            413, "附件过大：单文件不得超过 {} MiB（实际 {} 字节）".format(
                MAX_SIZE // (1024 * 1024), size))
    ext = os.path.splitext(filename)[1].lower()
    if ext in _TEXT_TYPES:
        if b"\x00" in data:
            raise AttachmentError(400, "文本附件包含 NUL 字节，不是合法文本")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise AttachmentError(
                400, "文本附件必须是合法 UTF-8 编码（文件「{}」无法按 UTF-8 解码）".format(
                    filename))
        return _TEXT_TYPES[ext]
    if ext == ".png":
        if not data.startswith(PNG_MAGIC):
            raise AttachmentError(
                400, "文件「{}」扩展名为 .png，但内容不是 PNG 图片".format(filename))
        return _BINARY_TYPES[".png"]
    if ext in (".jpg", ".jpeg"):
        if not data.startswith(JPEG_MAGIC) or not data.rstrip().endswith(b"\xff\xd9"):
            raise AttachmentError(
                400, "文件「{}」扩展名为 {}，但内容不是 JPEG 图片".format(
                    filename, ext))
        return _BINARY_TYPES[".jpg"]
    if ext == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise AttachmentError(
                400, "文件「{}」扩展名为 .pdf，但内容不是 PDF".format(filename))
        return _BINARY_TYPES[".pdf"]
    raise AttachmentError(
        400, "不支持的附件类型「{}」：仅支持文本（{}）、PNG、JPEG 或 PDF".format(
            ext or "无扩展名",
            "、".join(sorted(_TEXT_TYPES))))


# ------------------------------------------------------------ multipart 解析

def parse_multipart(body, boundary):
    """解析 multipart/form-data 请求体（标准库无第三方依赖的最小实现）。

    返回 (fields, files)：
      fields: {普通字段名: 字符串值}
      files:  [{"name": 字段名, "filename": 文件名, "content": bytes}]
    解析失败（结构不完整、缺部件头）抛 AttachmentError(400)。
    """
    if not boundary:
        raise AttachmentError(400, "缺少 multipart boundary")
    try:
        boundary = boundary.encode("ascii")
    except UnicodeEncodeError:
        raise AttachmentError(400, "multipart boundary 不合法")
    boundary = b"--" + boundary
    # 只在边界构成完整行时切分：后随 "--"（结束边界，group(1) 捕获）或
    # CRLF（下一部件），因此文件内容中的“边界前缀+junk”不会被误切。
    boundary_re = re.compile(re.escape(boundary) + rb"(?:(--)|" + b"\r\n)")
    matches = list(boundary_re.finditer(body))
    if not matches:
        raise AttachmentError(400, "multipart 表单结构不完整")
    if body[:matches[0].start()]:
        # 仅允许空前导（preamble）；curl/浏览器均直接以起始边界开头
        raise AttachmentError(400, "multipart 表单存在非法前导内容")

    fields, files = {}, []
    for i, m in enumerate(matches):
        closing = m.group(1) == b"--"
        if closing:
            break  # 结束边界之后的 epilogue 忽略
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else -1
        if seg_end < 0:
            raise AttachmentError(400, "multipart 表单缺少结束边界")
        seg = body[m.end():seg_end]
        header_blob, sep, content = seg.partition(b"\r\n\r\n")
        if not sep:
            raise AttachmentError(400, "multipart 表单结构不完整")
        # 部件内容后紧跟与下一边界之间的一个 CRLF 分隔，精确剥掉一个；
        # 文件本身以 CRLF 结尾时序列化后会有两个 CRLF，故文件内容原样保留。
        if not content.endswith(b"\r\n"):
            raise AttachmentError(400, "multipart 部件缺少边界前的 CRLF 分隔")
        content = content[:-2]
        headers = {}
        for line in header_blob.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get(b"content-disposition", b"")
        name, filename = _parse_disposition(disp)
        if name is None:
            continue
        if filename is None:
            fields[name] = content.decode("utf-8", errors="replace")
        else:
            files.append({"name": name, "filename": filename, "content": content})
    if not fields and not files:
        raise AttachmentError(400, "multipart 表单中没有任何字段")
    return fields, files


def _decode_header_value(raw):
    """解码 disposition 参数值：优先按 UTF-8（curl/浏览器对原始文件名的做法），
    回退 latin1。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin1")


def _unquote(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0:1] == b'"' and raw[-1:] == b'"':
        raw = raw[1:-1]
    return raw.replace(b"\\", b"")


def _parse_disposition(value):
    """从 Content-Disposition（字节）取 name 与 filename。

    支持 filename="..."（原始 UTF-8 或 latin1）与 RFC 5987 的
    filename*=UTF-8''%xx 形式；filename 不存在表示普通字段。
    """
    name = None
    filename = None
    for part in value.split(b";"):
        part = part.strip()
        if part.lower().startswith(b"name="):
            name = _decode_header_value(_unquote(part[5:]))
        elif part.lower().startswith(b"filename*="):
            raw = _unquote(part[9:])
            if b"'" in raw:
                _charset, _lang, pct = raw.split(b"'", 2)
                filename = urllib_parse_unquote(pct)
            else:
                filename = _decode_header_value(raw)
        elif part.lower().startswith(b"filename="):
            if filename is None:  # filename* 优先
                filename = _decode_header_value(_unquote(part[9:]))
    if name is None:
        return None, None
    return name, filename
