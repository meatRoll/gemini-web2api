"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import ssl
import os
import hashlib
import threading
import random

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG
from .tools import has_tool_call_marker, valid_tool_call_candidate

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0, "path": None}
_httpx_client = None
_log_lock = threading.Lock()


def _rotate_log_file(path: str, max_bytes: int, backup_count: int) -> None:
    if not path or max_bytes <= 0 or backup_count < 1:
        return
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return
        for index in range(backup_count, 0, -1):
            source = f"{path}.{index - 1}" if index > 1 else path
            target = f"{path}.{index}"
            if os.path.exists(target):
                os.remove(target)
            if os.path.exists(source):
                os.replace(source, target)
    except OSError:
        pass


def log(msg: str, force: bool = False):
    if not force and not CONFIG["log_requests"]:
        return
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    path = CONFIG.get("log_file")
    if path:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with _log_lock:
                _rotate_log_file(
                    path,
                    int(CONFIG.get("log_max_bytes", 2 * 1024 * 1024)),
                    int(CONFIG.get("log_backup_count", 2)),
                )
                with open(path, "a", encoding="utf-8") as stream:
                    stream.write(line)
            return
        except (OSError, ValueError):
            pass
    import sys
    sys.stderr.write(line)
    sys.stderr.flush()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_httpx_client():
    global _httpx_client
    if _httpx_client is None and HAS_HTTPX:
        proxy = CONFIG.get("proxy")
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        _httpx_client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True)
    return _httpx_client


def _retry_delay(attempt: int) -> float:
    """Back off between upstream retries to avoid amplifying overload errors."""
    base = max(0.0, float(CONFIG.get("retry_delay_sec", 5)))
    cap = max(base, float(CONFIG.get("retry_delay_max_sec", 30)))
    return min(cap, base * (2 ** attempt)) + random.uniform(0, min(1.0, base / 4))

def load_cookie() -> tuple:
    """Load cookie from file with mtime-based caching."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        if cookie_file == _cookie_cache["path"] and mtime == _cookie_cache["mtime"] and _cookie_cache["str"]:
            return _cookie_cache["str"], _cookie_cache["sapisid"]
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
            for field in ("xsrf_token", "gemini_bl"):
                if data.get(field):
                    CONFIG[field] = data[field]
            if "auth_user" in data:
                CONFIG["auth_user"] = data["auth_user"]
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        _cookie_cache.update({"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime, "path": cookie_file})
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return _cookie_cache["str"], _cookie_cache["sapisid"]


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _build_headers(session_hash: str = None) -> dict:
    account_prefix = _account_prefix()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    if session_hash:
        # ponytail: x-goog-ext-525001261-jspb idx4 hash binds the request to the
        # model the user picked in the web UI (payload [79] is ignored upstream).
        # Server-side state, may expire with the web session; re-capture then.
        headers["x-goog-ext-525001261-jspb"] = json.dumps(
            [1, None, None, None, session_hash] + [None] * 13)
    return headers


def _session_hash(model_name: str):
    """Per-model session hash from config, captured from the web UI request headers."""
    return (CONFIG.get("session_hashes") or {}).get(model_name)


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url() -> str:
    reqid = int(time.time()) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def _extract_stream_deltas(line: str, emitted_raw_text: str) -> tuple:
    """Return the new cumulative text and incremental deltas from one line."""
    if "BardErrorInfo" in line:
        bard_err = re.search(r'BardErrorInfo(?:"\s*,\s*\[|\s*\[)(\d+)\]', line)
        if bard_err:
            raise RuntimeError(
                f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]"
            )

    deltas = []
    for text in _extract_texts_from_line(line):
        if text == emitted_raw_text or emitted_raw_text.startswith(text):
            continue
        if not text.startswith(emitted_raw_text):
            raise RuntimeError("Gemini stream content changed during retry")
        deltas.append(clean_text(text[len(emitted_raw_text):], strip=False))
        emitted_raw_text = text
    return emitted_raw_text, [delta for delta in deltas if delta]


def _iter_response_lines(response):
    """Yield complete response lines and flush a final unterminated line."""
    buf = ""
    for chunk in response.iter_text():
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            yield line
    if buf:
        yield buf


def _response_candidates(raw: str) -> list:
    candidates = []
    for line in raw.split("\n"):
        if '"wrb.fr"' in line:
            candidates.extend(_extract_texts_from_line(line))
    return candidates


def _valid_tool_call_candidate(text: str) -> bool:
    return valid_tool_call_candidate(text)


def _select_response_text(candidates: list) -> str:
    """Select a coherent final candidate, avoiding rewritten tool-call snapshots."""
    if not candidates:
        return ""
    tool_marked = [text for text in candidates if has_tool_call_marker(text)]
    if tool_marked:
        if not _valid_tool_call_candidate(tool_marked[-1]):
            raise RuntimeError("Gemini returned an incomplete or malformed tool_call")
        return tool_marked[-1]
    return max(candidates, key=len)


def _response_candidate_stats(raw: str):
    candidates = _response_candidates(raw)
    lengths = [len(text) for text in candidates]
    longest = max(candidates, key=len, default="")
    repeated_prefix = False
    if len(longest) >= 24:
        prefix = longest[:24]
        repeated_prefix = longest.count(prefix) > 1
    return len(candidates), sum(lengths), max(lengths, default=0), lengths[-12:], repeated_prefix


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    bard_err = re.search(r'BardErrorInfo(?:"\s*,\s*\[|\s*\[)(\d+)\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    # ponytail: regex probe — backend echoes actual model name (e.g. "3.5 Flash-Lite");
    # may miss if backend renames formats, check raw response then.
    m = re.search(r'"(\d\.\d (?:Flash(?:-Lite)?|Thinking|Pro))', raw)
    if m:
        log(f"actual model from response: {m.group(1)}")
    candidates = _response_candidates(raw)
    selected = _select_response_text(candidates)
    return clean_text(selected)



def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, session_hash: str = None, request_id: str = None, *, budget_sec: float = None, max_attempts: int = None, cancelled=None) -> str:
    """Non-streaming generation with retry."""
    load_cookie()
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields).encode()
    url = _get_url()
    headers = _build_headers(session_hash)
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")

    last_err = None
    trace_id = request_id or uuid.uuid4().hex[:10]
    log(
        f"upstream_start transport=buffered id={trace_id} "
        f"request_bytes={len(body)}"
    )
    deadline = time.monotonic() + budget_sec if budget_sec is not None else None
    attempts = max_attempts if max_attempts is not None else CONFIG["retry_attempts"]
    for attempt in range(attempts):
        try:
            if cancelled and cancelled():
                raise ConnectionAbortedError("Client disconnected before generation")
            timeout = CONFIG["request_timeout_sec"]
            if deadline is not None:
                timeout = min(timeout, deadline - time.monotonic())
                if timeout <= 0:
                    raise TimeoutError("Generation budget exhausted")
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
            try:
                if deadline is None:
                    raw = resp.read().decode("utf-8", errors="replace")
                else:
                    parts = []
                    while True:
                        if cancelled and cancelled():
                            raise ConnectionAbortedError("Client disconnected during generation")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Generation budget exhausted")
                        # urllib has no total read deadline; bound each underlying read.
                        sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                        if sock is not None:
                            sock.settimeout(remaining)
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        parts.append(chunk)
                    raw = b"".join(parts).decode("utf-8", errors="replace")
            finally:
                resp.close()
            candidate_count, candidate_chars, longest_chars, tail_lengths, repeated_prefix = _response_candidate_stats(raw)
            text = extract_response_text(raw)
            log(
                f"upstream_finish transport=buffered id={trace_id} attempt={attempt + 1} "
                f"raw_bytes={len(raw.encode('utf-8'))} frames={sum(1 for line in raw.split(chr(10)) if chr(34)+'wrb.fr'+chr(34) in line)} "
                f"candidates={candidate_count} candidate_chars={candidate_chars} "
                f"longest_chars={longest_chars} tail_lengths={tail_lengths} "
                f"repeated_prefix={repeated_prefix} extracted_chars={len(text)}"
            )
            return text
        except Exception as e:
            last_err = e
            log(
                f"upstream_error transport=buffered id={trace_id} attempt={attempt + 1} "
                f"type={type(e).__name__} error={str(e).replace(chr(10), ' ')[:300]}"
            )
            if isinstance(e, (TimeoutError, ConnectionAbortedError)):
                raise
            if attempt < attempts - 1:
                delay = _retry_delay(attempt)
                if deadline is not None and time.monotonic() + delay >= deadline:
                    raise TimeoutError("Generation budget exhausted") from e
                log(f"Retry {attempt+1}/{attempts} in {delay:.2f}s: {e}")
                time.sleep(delay)
    raise last_err


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, session_hash: str = None, request_id: str = None):
    """Streaming generation via httpx with retry on connection failure."""
    if not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields, session_hash, request_id=request_id)
        if text:
            yield text
        return

    load_cookie()
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields)
    url = _get_url()
    headers = _build_headers(session_hash)
    client = _get_httpx_client()

    last_err = None
    request_tag = request_id or uuid.uuid4().hex[:10]
    log(
        f"upstream_start transport=stream tag={request_tag} "
        f"request_bytes={len(body.encode('utf-8'))}"
    )
    emitted_raw_text = ""
    for attempt in range(CONFIG["retry_attempts"]):
        frame_count = 0
        response_bytes = 0
        text_frame_count = 0
        try:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                resp.raise_for_status()
                for line in _iter_response_lines(resp):
                    response_bytes += len(line.encode("utf-8"))
                    if '"wrb.fr"' in line:
                        frame_count += 1
                    previous_text = emitted_raw_text
                    emitted_raw_text, deltas = _extract_stream_deltas(line, emitted_raw_text)
                    if emitted_raw_text != previous_text:
                        text_frame_count += 1
                    for delta in deltas:
                        yield delta
            log(
                f"upstream_finish transport=stream tag={request_tag} attempt={attempt + 1} "
                f"response_bytes={response_bytes} frames={frame_count} "
                f"text_frames={text_frame_count} emitted_chars={len(emitted_raw_text)}"
            )
            return
        except Exception as e:
            last_err = e
            log(
                f"upstream_error transport=stream tag={request_tag} attempt={attempt + 1} "
                f"response_bytes={response_bytes} frames={frame_count} "
                f"text_frames={text_frame_count} emitted_chars={len(emitted_raw_text)} "
                f"type={type(e).__name__} error={str(e).replace(chr(10), ' ')[:300]}"
            )
            # Once bytes have been delivered, restarting would duplicate or
            # replace already visible model output. Let the API layer report a
            # terminal stream error instead.
            if emitted_raw_text:
                raise
            if attempt < CONFIG["retry_attempts"] - 1:
                delay = _retry_delay(attempt)
                log(f"Stream retry {attempt+1}/{CONFIG['retry_attempts']} in {delay:.2f}s: {e}")
                time.sleep(delay)
    raise last_err
