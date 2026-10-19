"""Multimodal: Scotty resumable upload for Gemini image input."""
import hashlib
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.request
import urllib.parse
from urllib.parse import urlparse

try:
    from curl_cffi import CurlOpt
except ImportError:
    CurlOpt = None

from .config import CONFIG
from .gemini import (
    HAS_CURL_CFFI, curl_requests, load_cookie, make_sapisidhash,
    _account_prefix, _get_ssl_ctx, log, safe_error_message,
    _google_browser_headers, _google_user_agent,
)

_MAX_REMOTE_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_REMOTE_IMAGE_REDIRECTS = 3
_REDIRECT_STATUS = {301, 302, 303, 307, 308}


def _get_page_tokens(timeout=30) -> dict:
    """Read the authenticated account page for attachment session tokens."""
    cookie_str, sapisid = load_cookie()
    account_prefix = _account_prefix()
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": _google_user_agent(),
    }
    headers.update(_google_browser_headers())
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    proxy = CONFIG.get("proxy")
    try:
        html = None
        if HAS_CURL_CFFI:
            request_args = {
                "headers": headers,
                "timeout": min(30, timeout),
                "impersonate": "chrome",
            }
            if proxy:
                request_args["proxy"] = proxy
            resp = curl_requests.get(
                f"https://gemini.google.com{account_prefix}/app", **request_args
            )
            try:
                resp.raise_for_status()
                html = resp.text
            finally:
                close = getattr(resp, "close", None)
                if close:
                    close()
        else:
            req = urllib.request.Request(f"https://gemini.google.com{account_prefix}/app", headers=headers)
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
                )
                resp = opener.open(req, timeout=min(30, timeout))
            else:
                resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=min(30, timeout))
            try:
                html = resp.read().decode("utf-8", errors="replace")
            finally:
                resp.close()
        tokens = {}
        for key, candidates in {
            "push_id": (r'"qKIAYe":\s*"([^"\\]*(?:\\.[^"\\]*)*)"',),
            "pctx": (r'"Ylro7b":\s*"([^"\\]*(?:\\.[^"\\]*)*)"',),
            "f_sid": (r'"FdrFJe":\s*"([^"\\]*(?:\\.[^"\\]*)*)"',),
            "at": (r'"SNlM0e":\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
                   r'"thykhd":\s*"([^"\\]*(?:\\.[^"\\]*)*)"'),
        }.items():
            for pattern in candidates:
                match = re.search(pattern, html)
                if match:
                    tokens[key] = json.loads('"' + match.group(1) + '"')
                    break
        build = re.search(
            r'boq_(?:assistant-bard-web-server|gemini-web-uiserver)_[0-9.]+_p[0-9]+',
            html,
        )
        if build:
            CONFIG["gemini_bl"] = build.group()
        return tokens
    except Exception as e:
        log(f"Page token fetch failed: {safe_error_message(e)}")
        return {}


_page_tokens_cache = {"tokens": {}, "ts": 0, "scope": None}
_page_tokens_lock = threading.Lock()


def _cached_page_tokens(max_age=600, timeout=30) -> dict:
    cookie, _ = load_cookie()
    scope = hashlib.sha256(
        (cookie + "\0" + str(CONFIG.get("auth_user")) + "\0" + str(CONFIG.get("proxy"))).encode()
    ).hexdigest()
    with _page_tokens_lock:
        now = time.monotonic()
        if (max_age <= 0 or _page_tokens_cache["scope"] != scope
                or not _page_tokens_cache["tokens"]
                or now - _page_tokens_cache["ts"] > max_age):
            tokens = _get_page_tokens(timeout=timeout)
            _page_tokens_cache.update(tokens=tokens, ts=now, scope=scope)
        return dict(_page_tokens_cache["tokens"])


def detect_image_mime(image_bytes: bytes, fallback: str = "image/png") -> str:
    """Infer a common raster image MIME type from its file signature."""
    if not isinstance(image_bytes, bytes):
        return fallback
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"BM"):
        return "image/bmp"
    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(image_bytes) >= 12 and image_bytes[4:8] == b"ftyp":
        brand = image_bytes[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic"
    return fallback


def upload_image(image_bytes: bytes, filename: str = "image.png", mime_type: str = "image/png") -> str:
    """Upload image via Scotty resumable upload. Returns file reference path."""
    cookie_str, sapisid = load_cookie()
    if not cookie_str:
        raise RuntimeError("A valid Gemini cookie is required for image input")
    tokens = _cached_page_tokens()
    push_id = tokens.get("push_id", "feeds/mcudyrk2a4khkz")
    pctx = tokens.get("pctx", "CgcSBWjK7pYx")

    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")

    # Step 1: Initiate resumable upload
    start_headers = {
        "Push-ID": push_id,
        "X-Tenant-Id": "bard-storage",
        "X-Client-Pctx": pctx,
        "X-Goog-Upload-Header-Content-Length": str(len(image_bytes)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if cookie_str:
        start_headers["Cookie"] = cookie_str
    if sapisid:
        start_headers["Authorization"] = make_sapisidhash(sapisid)

    start_url = "https://content-push.googleapis.com/upload/"
    req = urllib.request.Request(start_url, data=b"", headers=start_headers, method="POST")

    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx)
        )
        resp = opener.open(req, timeout=30)
    else:
        resp = urllib.request.urlopen(req, context=ctx, timeout=30)

    try:
        upload_url = resp.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise RuntimeError("Gemini upload returned no session URL")
    finally:
        resp.close()
    parsed_upload = urlparse(upload_url)
    if (parsed_upload.scheme != "https" or parsed_upload.hostname != "content-push.googleapis.com"
            or parsed_upload.username is not None or parsed_upload.password is not None
            or parsed_upload.port not in (None, 443)):
        raise RuntimeError("Gemini upload returned an unexpected session host")
    log("Image upload session started")

    # Step 2: Upload file data + finalize
    upload_headers = {
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
        "Content-Type": "application/octet-stream",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    req2 = urllib.request.Request(upload_url, data=image_bytes, headers=upload_headers, method="POST")
    if proxy:
        resp2 = opener.open(req2, timeout=60)
    else:
        resp2 = urllib.request.urlopen(req2, context=ctx, timeout=60)

    try:
        file_ref = resp2.read().decode().strip()
    finally:
        resp2.close()
    if not file_ref or not file_ref.startswith("/"):
        raise RuntimeError("Gemini upload returned an invalid file reference")

    log(f"Image uploaded: filename={filename} bytes={len(image_bytes)}")
    return file_ref


def _validate_remote_image_url(url):
    """Validate every redirect hop and return a public address to pin to curl."""
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise ValueError("invalid remote image URL")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid remote image URL") from error
    host = parsed.hostname
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)):
        raise ValueError("remote images require a public HTTPS URL on port 443")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        addresses = {ipaddress.ip_address(item[4][0]) for item in
                     socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("remote image host is not public")
    address = sorted(addresses, key=lambda item: (item.version, str(item)))[0]
    return url, host, address


def fetch_image_bytes(url: str) -> bytes:
    """Download a bounded public image, pinning DNS and checking every redirect.

    Remote downloads deliberately bypass proxies to preserve address validation.
    Gemini page, upload, and generation requests still use the configured proxy.
    """
    if not HAS_CURL_CFFI or CurlOpt is None:
        raise RuntimeError("curl_cffi is required for remote image input")
    current = url
    for redirect_count in range(_MAX_REMOTE_IMAGE_REDIRECTS + 1):
        current, host, address = _validate_remote_image_url(current)
        address_text = f"[{address}]" if address.version == 6 else str(address)
        session = curl_requests.Session(
            curl_options={CurlOpt.RESOLVE: [f"{host}:443:{address_text}"]},
            trust_env=False,
        )
        response = None
        try:
            response = session.get(
                current, headers={"User-Agent": "Mozilla/5.0"},
                timeout=min(30, CONFIG["request_timeout_sec"]), impersonate="chrome",
                allow_redirects=False, stream=True,
            )
            if response.status_code in _REDIRECT_STATUS:
                if redirect_count >= _MAX_REMOTE_IMAGE_REDIRECTS:
                    raise ValueError("remote image exceeded redirect limit")
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("remote image redirect has no location")
                current = urllib.parse.urljoin(current, location)
                continue
            if response.status_code != 200:
                raise RuntimeError(f"remote image fetch failed: HTTP {response.status_code}")
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            length = response.headers.get("Content-Length")
            if length is not None and not 0 <= int(length) <= _MAX_REMOTE_IMAGE_BYTES:
                raise ValueError("remote image exceeds 10 MiB size limit")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > _MAX_REMOTE_IMAGE_BYTES:
                    raise ValueError("remote image exceeds 10 MiB size limit")
            data = bytes(body)
            detected = detect_image_mime(data, "")
            if not detected or detected != ("image/jpeg" if mime == "image/jpg" else mime):
                raise ValueError("remote image content type does not match bytes")
            return data
        finally:
            if response is not None:
                response.close()
            session.close()
