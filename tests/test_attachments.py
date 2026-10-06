"""附件存储与校验单元测试：python tests/test_attachments.py

覆盖：
- 允许类型（文本/PNG/JPEG/PDF）的扩展名与内容魔数一致性校验；
- 改名伪装（txt→png、png→txt、.exe、空文件）一律拒绝；
- 文本必须是合法 UTF-8 且不含 NUL；
- 20MB 上限、文件名/说明长度与路径字符限制；
- 内容寻址存储：相同内容只占一份；临时文件与崩溃残留清理；
- multipart/form-data 解析：普通字段、单文件落临时文件、越界/畸形拒绝并清理。
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import attachments  # noqa: E402

PNG = attachments.PNG_SIGNATURE + b"\x00\x00IHDRrest-of-png"
JPEG = attachments.JPEG_SIGNATURE + b"\xe0\x00\x10JFIFrest"
PDF = b"%PDF-1.5\n%some pdf body"
TEXT = "事故现场日志 line1\n".encode("utf-8")


def check_accept(filename, data):
    root = tempfile.mkdtemp()
    sp = attachments.stage_upload(root)
    with open(sp, "wb") as f:
        f.write(data)
    try:
        sha, size, ctype = attachments.validate_staged_file(sp, filename)
        return sha, size, ctype
    finally:
        for p in (os.path.join(root, n) for n in os.listdir(root)):
            if os.path.exists(p):
                os.unlink(p)
        os.rmdir(root)


def check_reject(filename, data):
    try:
        check_accept(filename, data)
    except attachments.UploadError:
        return True
    return False


def test_allowed_types_pass():
    sha, size, ct = check_accept("a.txt", TEXT)
    assert ct == "text/plain; charset=utf-8" and size == len(TEXT) and len(sha) == 64
    for name, data in [
        ("a.png", PNG),
        ("a.jpg", JPEG),
        ("a.jpeg", JPEG),
        ("a.pdf", PDF),
        ("a.md", b"# title\n"),
        ("a.log", b"INFO ok\n"),
        ("a.csv", "名称,值\n告警,1\n".encode("utf-8")),
        ("a.json", b'{"k": "\xe4\xb8\xad"}'),
    ]:
        _, _, c = check_accept(name, data)
        expected = attachments.CONTENT_TYPE_BY_KIND[attachments.sniff_kind(data)]
        assert c == expected, name


def test_extension_must_match_content():
    # 文本内容伪装成图片 / 图片伪装成文本 / 不支持的扩展名 / 空文件
    assert check_reject("a.png", TEXT)
    assert check_reject("a.jpg", TEXT)
    assert check_reject("a.pdf", TEXT)
    assert check_reject("a.txt", PNG)
    assert check_reject("a.log", JPEG)
    assert check_reject("a.exe", b"MZ\x90\x00binary")
    assert check_reject("a.bin", TEXT)
    assert check_reject("a.txt", b"")
    assert check_reject("a.png", b"")
    # 二进制（含 NUL）的非图片/非 PDF 字节不能当文本
    assert check_reject("a.txt", b"abc\x00def")
    # 非法 UTF-8 不能当文本
    assert check_reject("a.txt", b"\xff\xfe\x00\x00abc")


def test_metadata_limits():
    for bad_name in ["", "   ", "../escape.txt", "a/b.txt", "a\\b.txt",
                     "x" * 201 + ".txt", ".", ".."]:
        try:
            attachments.validate_metadata(bad_name, "")
            raise AssertionError("应拒绝文件名: " + repr(bad_name))
        except attachments.UploadError:
            pass
    fn, desc = attachments.validate_metadata("  note.txt  ", None)
    assert fn == "note.txt" and desc == ""
    try:
        attachments.validate_metadata("a.txt", "x" * 1001)
        raise AssertionError("说明超长应拒绝")
    except attachments.UploadError:
        pass
    try:
        attachments.validate_metadata("noext", "")
        raise AssertionError("无扩展名应拒绝")
    except attachments.UploadError:
        pass


def test_size_limit():
    root = tempfile.mkdtemp()
    sp = attachments.stage_upload(root)
    with open(sp, "wb") as f:
        f.write(b"a" * (attachments.MAX_FILE_SIZE + 1))
    try:
        attachments.validate_staged_file(sp, "big.txt")
        raise AssertionError("超过 20MB 应拒绝")
    except attachments.UploadError as e:
        assert "20MB" in str(e)
    finally:
        os.unlink(sp)
        os.rmdir(root)


def test_content_addressing_dedup_and_sweep():
    root = tempfile.mkdtemp()
    s1 = attachments.stage_upload(root)
    with open(s1, "wb") as f:
        f.write(TEXT)
    import hashlib
    sha = hashlib.sha256(TEXT).hexdigest()
    assert attachments.store_blob(root, sha, s1) is True
    assert attachments.blob_path(root, sha).is_file()
    # 同内容再次入库：不覆盖、返回 False
    s2 = attachments.stage_upload(root)
    with open(s2, "wb") as f:
        f.write(TEXT)
    assert attachments.store_blob(root, sha, s2) is False
    os.unlink(s2)
    assert len(os.listdir(root)) == 1
    # sweep：未知哈希文件与临时文件被清，已知哈希保留
    unknown = os.path.join(root, "b" * 64)
    open(unknown, "wb").close()
    tmp = attachments.stage_upload(root)  # .upload-*
    removed = attachments.sweep_orphan_files(root, {sha})
    assert removed == 2
    assert attachments.blob_path(root, sha).is_file()
    os.unlink(attachments.blob_path(root, sha))
    os.rmdir(root)


class _Stream:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, n):
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


def build_body(boundary, parts):
    """parts: [(name, filename_or_None, content_type_or_None, data)]"""
    out = []
    for name, filename, ctype, data in parts:
        header = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
        if filename is not None:
            header += f"; filename=\"{filename}\""
        header += "\r\n"
        if ctype:
            header += f"Content-Type: {ctype}\r\n"
        out.append(header.encode() + b"\r\n" + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out)


def test_parse_multipart_ok():
    boundary = "BoUnDaRy123"
    body = build_body(boundary, [
        ("base_version", None, None, b"3"),
        ("description", None, None, "现场日志".encode()),
        ("file", "a.txt", "text/plain", TEXT),
    ])
    root = tempfile.mkdtemp()
    try:
        fields, files = attachments.parse_multipart(
            _Stream(body), len(body),
            f"multipart/form-data; boundary={boundary}", root)
        assert fields["base_version"] == "3"
        assert fields["description"] == "现场日志"
        assert len(files) == 1
        part = files[0]
        assert part.field_name == "file" and part.filename == "a.txt"
        assert part.size == len(TEXT)
        assert open(part.path, "rb").read() == TEXT
        attachments.cleanup_parts(files)
        assert not os.path.exists(part.path)
    finally:
        for n in os.listdir(root):
            os.unlink(os.path.join(root, n))
        os.rmdir(root)


def test_parse_multipart_rejects_and_cleans():
    boundary = "X"
    root = tempfile.mkdtemp()
    # 非 multipart
    try:
        attachments.parse_multipart(_Stream(b""), 0, "application/json", root)
        raise AssertionError
    except attachments.MultipartError:
        pass
    # 单文件超限：不生成临时文件
    big = b"a" * (attachments.MAX_FILE_SIZE + 1)
    body = build_body(boundary, [("file", "big.txt", "text/plain", big)])
    try:
        attachments.parse_multipart(
            _Stream(body), len(body), f"multipart/form-data; boundary={boundary}", root)
        raise AssertionError
    except attachments.UploadError:
        pass
    assert os.listdir(root) == []
    # 请求总量超限（Content-Length 层面直接拒绝，不读体）
    try:
        attachments.parse_multipart(
            _Stream(b"x"), attachments.MAX_REQUEST_SIZE + 1,
            f"multipart/form-data; boundary={boundary}", root)
        raise AssertionError
    except attachments.UploadError:
        pass
    # 畸形：没有结束边界
    bad = f"--{boundary}\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nx\r\n".encode()
    try:
        attachments.parse_multipart(
            _Stream(bad), len(bad), f"multipart/form-data; boundary={boundary}", root)
        raise AssertionError
    except attachments.MultipartError:
        pass
    os.rmdir(root)


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
