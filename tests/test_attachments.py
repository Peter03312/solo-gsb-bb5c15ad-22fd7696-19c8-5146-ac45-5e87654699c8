"""证据附件：类型/大小校验与 multipart 解析单元测试：python tests/test_attachments.py

覆盖：
- 允许类型：文本（.txt/.log/.md/.csv/.json/.xml/.text）须为合法 UTF-8 且无 NUL；
  PNG/JPEG 看魔数；PDF 看 %PDF-；
- 拒绝：扩展名不在清单、扩展名与内容不符（改名伪装）、空内容、>20 MiB（413）、
  非法文件名（路径穿越、控制字符）、说明超长；
- multipart 解析：普通字段 + 文件、文件内容末尾的 CRLF 原样保留、二进制内容
  含边界样式字节不受影响、结构不完整/无部件报错。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import attachments as att  # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a") + b"IHDR-rest"
JPEG = b"\xff\xd8\xff\xe0" + b"payload" + b"\xff\xd9"
PDF = b"%PDF-1.7\nstuff"


def expect_reject(filename, data):
    try:
        att.sniff(filename, data)
    except att.AttachmentError as e:
        return e
    raise AssertionError("应当拒绝: " + filename)


def test_allowed_types():
    assert att.sniff("a.txt", "你好\n".encode("utf-8")) == "text/plain; charset=utf-8"
    assert att.sniff("a.text", b"ok").startswith("text/plain")
    assert att.sniff("a.log", b"log line\n").startswith("text/plain")
    assert att.sniff("a.md", b"# title").startswith("text/markdown")
    assert att.sniff("a.csv", "甲,乙\n".encode("utf-8")).startswith("text/csv")
    assert att.sniff("a.json", "{}".encode("utf-8")).startswith("application/json")
    assert att.sniff("a.xml", b"<x/>").startswith("application/xml")
    assert att.sniff("a.png", PNG) == "image/png"
    assert att.sniff("a.jpg", JPEG) == "image/jpeg"
    assert att.sniff("a.jpeg", JPEG) == "image/jpeg"
    assert att.sniff("a.pdf", PDF) == "application/pdf"


def test_text_encoding_and_nul():
    e = expect_reject("a.txt", b"\xff\xfe not utf8")
    assert e.status == 400 and "UTF-8" in e.message
    e = expect_reject("a.log", b"abc\x00")
    assert e.status == 400 and "NUL" in e.message


def test_extension_must_match_magic():
    e = expect_reject("a.png", b"not a png")
    assert e.status == 400 and "PNG" in e.message
    assert expect_reject("a.jpg", b"not a jpg").status == 400
    assert expect_reject("a.pdf", b"not pdf").status == 400
    # 可执行内容改名伪装
    assert expect_reject("x.png", b"MZ\x90\x00").status == 400


def test_extension_not_allowed():
    assert expect_reject("a.exe", b"MZ").status == 400
    assert expect_reject("a.zip", PK := b"PK\x03\x04").status == 400
    assert expect_reject("a.gif", b"GIF89a").status == 400
    assert expect_reject("a", b"x").status == 400
    assert expect_reject("", b"x").status == 400


def test_empty_and_oversize():
    assert expect_reject("a.txt", b"").status == 400
    e = expect_reject("a.txt", b"x" * (att.MAX_SIZE + 1))
    assert e.status == 413 and "20 MiB" in e.message
    # 临界值合法
    assert att.sniff("a.bin".replace(".bin", ".txt"), b"x" * att.MAX_SIZE).startswith("text/plain")


def test_filename_normalization():
    assert att.normalize_filename("a.txt") == "a.txt"
    assert att.normalize_filename("../../etc/passwd") == "passwd"
    assert att.normalize_filename("C:\\Windows\\win.ini") == "win.ini"
    for bad in ("", "  ", ".", ".."):
        try:
            att.normalize_filename(bad)
            raise AssertionError("应拒绝空文件名")
        except att.AttachmentError as e:
            assert e.status == 400
    try:
        att.normalize_filename("a\x00.txt")
        raise AssertionError("控制字符应拒绝")
    except att.AttachmentError as e:
        assert e.status == 400


def test_description_limits():
    assert att.normalize_description(None) == ""
    assert att.normalize_description("  hi ") == "hi"
    try:
        att.normalize_description("x" * (att.MAX_DESCRIPTION_LEN + 1))
        raise AssertionError("说明超长应拒绝")
    except att.AttachmentError as e:
        assert e.status == 400


def _body(parts):
    boundary = "----testB"
    chunks = []
    for name, value, filename in parts:
        head = ['Content-Disposition: form-data; name="%s"' % name]
        if filename is not None:
            head[0] += '; filename="%s"' % filename
            head.append("Content-Type: application/octet-stream")
        chunks.append(b"--" + boundary.encode() + b"\r\n" +
                      "\r\n".join(head).encode() + b"\r\n\r\n" + value)
    body = b"\r\n".join(chunks) + b"\r\n--" + boundary.encode() + b"--\r\n"
    return body, boundary


def test_multipart_fields_and_file():
    body, boundary = _body([
        ("base_version", b"3", None),
        ("description", "说明".encode("utf-8"), None),
        ("file", b"content\n", "a.txt"),
    ])
    fields, files = att.parse_multipart(body, boundary)
    assert fields == {"base_version": "3", "description": "说明"}
    assert len(files) == 1
    assert files[0] == {"name": "file", "filename": "a.txt", "content": b"content\n"}


def test_multipart_preserves_trailing_crlf():
    body, boundary = _body([("file", b"line\r\n", "a.txt")])
    _f, files = att.parse_multipart(body, boundary)
    assert files[0]["content"] == b"line\r\n"  # 文件本身的结尾 CRLF 不得被吃掉


def test_multipart_binary_with_boundary_like_bytes():
    # 内容中出现边界前缀但后接额外字符（junk），不构成真边界，必须原样保留
    payload = b"xx\r\n------testBjunk" + b"\x00\xff" * 50
    body, boundary = _body([("file", payload, "p.png")])
    _f, files = att.parse_multipart(body, boundary)
    assert files[0]["content"] == payload


def test_multipart_malformed():
    for bad in (b"", b"random garbage without boundary"):
        try:
            att.parse_multipart(bad, "b")
            raise AssertionError("畸形表单应拒绝")
        except att.AttachmentError as e:
            assert e.status == 400


def test_multipart_utf8_and_rfc5987_filename():
    body, boundary = _body([("file", b"x", "值班日志.txt")])
    _f, files = att.parse_multipart(body, boundary)
    assert files[0]["filename"] == "值班日志.txt"
    raw = (b"------x\r\nContent-Disposition: form-data; name=\"file\";"
           b" filename=\"ascii.txt\"; filename*=UTF-8''%E5%80%BC%E7%8F%AD.txt\r\n\r\nz"
           b"\r\n------x--\r\n")
    _f, files = att.parse_multipart(raw, "----x")
    assert files[0]["filename"] == "值班.txt"


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
