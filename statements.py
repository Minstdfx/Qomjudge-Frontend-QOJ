"""Discover advertised statement PDFs and stream verified PDF bytes for viewing.

Native browser PDF viewers cannot run inside sandboxed HTML frames. This module
provides the narrow, byte-validated response used outside that HTML sandbox.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from flask import Response
from werkzeug.http import parse_options_header
from werkzeug.utils import secure_filename

from qoj import QOJError


def statement_pdfs(info: dict, problem_id: int) -> list[dict]:
    """Return only PDF iframe sources already advertised by this problem.

    The /qoj mount is translated back to its fixed upstream origin. Query order,
    revision and language parameters are otherwise preserved verbatim.
    """
    source = info.get("statement_html", "")
    if not isinstance(source, str) or isinstance(problem_id, bool):
        return []
    soup = BeautifulSoup(source, "html.parser")
    result, seen = [], set()
    for frame in soup.select("iframe[src]"):
        advertised = frame.get("src", "")
        if not isinstance(advertised, str) or any(ord(c) < 32 for c in advertised) or "\\" in advertised:
            continue
        try:
            parsed = urlsplit(advertised)
            if parsed.netloc:
                if (parsed.scheme != "https" or parsed.hostname != "qoj.ac"
                        or parsed.port not in (None, 443) or parsed.username or parsed.password):
                    continue
                path = parsed.path
            elif not parsed.scheme and parsed.path.startswith("/qoj/"):
                path = parsed.path[4:]
            else:
                continue
        except ValueError:
            continue
        decoded = unquote(unquote(path))
        if ("\\" in decoded or any(ord(c) < 32 for c in decoded)
                or any(segment in {".", ".."} for segment in decoded.split("/"))):
            continue
        if path == "/download.php":
            query = parse_qs(parsed.query, keep_blank_values=True)
            if query.get("type") != ["statement"] or query.get("id") != [str(problem_id)]:
                continue
        elif not re.fullmatch(
                r"/(?:images|pictures|uploads|source|storage|resources|static)/[A-Za-z0-9_%./@+\-]+\.pdf",
                path, re.I):
            continue
        url = urlunsplit(("https", "qoj.ac", path, parsed.query, ""))
        if url in seen:
            continue
        seen.add(url)
        name = frame.get("title") or (unquote(path.rsplit("/", 1)[-1]) if path != "/download.php" else "题面 PDF")
        result.append({"id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
                       "name": str(name), "url": url})
    return result


def _inline_disposition(value: str) -> str:
    """Normalize Unicode upstream filenames into a WSGI-safe RFC 5987 header."""
    name = "statement.pdf"
    if isinstance(value, str) and not any(c in value for c in "\r\n\0"):
        _, options = parse_options_header(value)
        if options.get("filename"):
            name = options["filename"]
    name = re.sub(r'[\x00-\x1f\x7f/\\]', "_", name).strip(" .")[:180] or "statement.pdf"
    fallback = secure_filename(name) or "statement.pdf"
    return f'inline; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'


def pdf_response(upstream) -> Response:
    """Validate a small prefix, then own and stream the upstream response.

    The caller requests a full GET with Accept-Encoding: identity and no Range.
    Neither content decoding nor full-file buffering is performed here. Closing
    an unconsumed response (including HEAD) also releases the upstream socket.
    """
    released = False

    def release():
        nonlocal released
        if not released:
            released = True
            upstream.close()

    def reject(message: str, status: int = 502):
        release()
        raise QOJError(message, status)

    if urlsplit(upstream.url).path.startswith("/login") or upstream.status_code == 401:
        reject("QOJ 登录已过期，请由队长重新连接账号后查看题面。", 401)
    if upstream.status_code != 200:
        reject(f"QOJ PDF 题面读取失败（HTTP {upstream.status_code}）。")
    encoding = upstream.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        reject("QOJ 返回了压缩的 PDF 响应，无法安全预览，请刷新后重试。")
    kind = upstream.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if kind not in ("application/pdf", "application/octet-stream"):
        reject("QOJ 返回的内容不是 PDF 题面，请检查账号连接及题目权限。")

    def read_raw(size):
        # urllib3 exposes decode_content; plain file-like fixtures do not.
        if hasattr(upstream.raw, "stream"):
            return upstream.raw.read(size, decode_content=False)
        return upstream.raw.read(size)

    try:
        prefix = b""
        while len(prefix) < 5:
            chunk = read_raw(5 - len(prefix))
            if not chunk:
                break
            prefix += chunk
    except Exception:
        reject("读取 QOJ PDF 题面时网络连接中断，请刷新后重试。")
    if prefix != b"%PDF-":
        reject("QOJ 返回了无效的 PDF 内容，请由队长检查账号连接及题目权限。")

    def chunks():
        try:
            yield prefix
            if hasattr(upstream.raw, "stream"):
                yield from upstream.raw.stream(64 * 1024, decode_content=False)
            else:
                while chunk := read_raw(64 * 1024):
                    yield chunk
        finally:
            release()

    headers = {
        "Content-Type": "application/pdf",
        "Content-Disposition": _inline_disposition(upstream.headers.get("Content-Disposition", "")),
        # No sandbox directive: that directive blocks Edge's native PDF viewer.
        "Content-Security-Policy": "default-src 'none'; object-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'",
        "X-Content-Type-Options": "nosniff",
    }
    length = upstream.headers.get("Content-Length", "")
    if re.fullmatch(r"[0-9]+", length):
        headers["Content-Length"] = length
    for key in ("ETag", "Last-Modified"):
        if key in upstream.headers:
            headers[key] = upstream.headers[key]
    response = Response(chunks(), status=200, headers=headers)
    response.call_on_close(release)
    return response
