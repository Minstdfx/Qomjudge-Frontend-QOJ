"""Byte-preserving attachment responses; never apply page/JavaScript rewriting."""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from flask import Response
from werkzeug.utils import secure_filename

from qoj import QOJError


def attachment_disposition(value: str, name: str, content_type: str) -> str:
    """Keep the upstream filename (including RFC 5987 names), force download."""
    if not any(c in value for c in "\r\n\0") and re.match(r"^(?:inline|attachment)(?:\s*;|$)", value, re.I):
        if re.search(r";\s*filename\*?\s*=", value, re.I):
            return re.sub(r"^(?:inline|attachment)", "attachment", value, count=1, flags=re.I)
    name = re.sub(r'[\x00-\x1f\x7f/\\]', "_", str(name)).strip(" .")[:180] or "attachment"
    if not re.search(r"\.[A-Za-z0-9]{1,8}$", name):
        name += {"application/zip": ".zip", "application/x-zip-compressed": ".zip",
                 "application/pdf": ".pdf", "text/plain": ".txt"}.get(content_type.split(";")[0].lower(), "")
    fallback = secure_filename(name) or "attachment"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'


def attachment_response(upstream, name: str) -> Response:
    """Own and close the streamed requests.Response, including early disconnects."""
    status = upstream.status_code
    if urlsplit(upstream.url).path.startswith("/login"):
        upstream.close()
        raise QOJError("QOJ 登录已过期，请重新连接账号后下载附件。", 401)
    if status == 416:
        headers = {key: upstream.headers[key] for key in ("Content-Range", "Accept-Ranges") if key in upstream.headers}
        upstream.close()
        return Response(status=416, headers=headers)
    if not 200 <= status < 300:
        upstream.close()
        raise QOJError(f"QOJ 附件下载失败（HTTP {status}）。", status if status in (401, 403, 404, 429) else 502)
    kind = upstream.headers.get("Content-Type") or "application/octet-stream"
    disposition = upstream.headers.get("Content-Disposition", "")
    if any(html_type in kind.lower() for html_type in ("text/html", "application/xhtml+xml")) and not re.match(r"^attachment(?:\s*;|$)", disposition, re.I):
        upstream.close()
        raise QOJError("QOJ 返回了网页而不是附件，请检查登录状态及附件访问权限。", 502)

    headers = {key: upstream.headers[key] for key in (
        "Content-Length", "Content-Range", "Accept-Ranges", "Content-Encoding", "ETag", "Last-Modified"
    ) if key in upstream.headers}
    headers["Content-Type"] = kind
    headers["Content-Disposition"] = attachment_disposition(disposition, name, kind)
    headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    released = False

    def release():
        nonlocal released
        if not released:
            released = True
            upstream.close()

    def chunks():
        try:
            # iter_content() would decompress the body and invalidate the upstream
            # Content-Length/Range. Preserve raw bytes and Content-Encoding together.
            if hasattr(upstream.raw, "stream"):
                yield from upstream.raw.stream(64 * 1024, decode_content=False)
            else:
                while chunk := upstream.raw.read(64 * 1024):
                    yield chunk
        finally:
            release()

    response = Response(chunks(), status=status, headers=headers)
    response.call_on_close(release)
    return response
