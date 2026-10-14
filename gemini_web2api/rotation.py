"""Persist Google session-cookie rotations for the long-running web API."""
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

from .config import CONFIG
from .gemini import log

ROTATE_URL = "https://accounts.google.com/RotateCookies"
ROTATE_BODY = b'[000,"-0000000000000000000"]'
_rotation_thread = None


def rotation_worker_alive():
    if not CONFIG.get("cookie_rotation_interval_sec") or not CONFIG.get("cookie_file"):
        return True
    return _rotation_thread is not None and _rotation_thread.is_alive()


def rotate_cookie_file(path):
    """Rotate a JSON export; return a status without exposing cookie values."""
    with open(path, encoding="utf-8") as source:
        auth = json.load(source)
        original_mtime = os.fstat(source.fileno()).st_mtime_ns
    cookie = auth.get("cookie", "")
    if not cookie:
        return "missing_cookie"

    request = urllib.request.Request(
        ROTATE_URL, data=ROTATE_BODY, method="POST", headers={
            "Cookie": cookie,
            "Content-Type": "application/json",
            "Origin": "https://accounts.google.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            response_headers = response.headers.get_all("Set-Cookie", [])
    except urllib.error.HTTPError as error:
        return f"http_{error.code}"

    jar = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
    changed = 0
    for header in response_headers:
        first = header.split(";", 1)[0]
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        if name not in ("__Secure-1PSIDTS", "__Secure-3PSIDTS",
                        "__Secure-1PSIDCC", "__Secure-3PSIDCC"):
            continue
        changed += jar.get(name) != value
        jar[name] = value
    if not changed:
        return "no_change"

    auth["cookie"] = "; ".join(f"{name}={value}" for name, value in jar.items())
    fd = None
    temp_path = None
    try:
        if os.stat(path).st_mtime_ns != original_mtime:
            return "file_changed"
        fd, temp_path = tempfile.mkstemp(prefix=".gemini-auth-", dir=os.path.dirname(os.path.abspath(path)))
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            fd = None
            json.dump(auth, output)
            output.flush()
            os.fsync(output.fileno())
        # Never replace a newer browser export with the result of an older request.
        if os.stat(path).st_mtime_ns != original_mtime:
            return "file_changed"
        os.replace(temp_path, path)
        temp_path = None
        return f"updated_{changed}"
    finally:
        if fd is not None:
            os.close(fd)
        if temp_path is not None:
            os.unlink(temp_path)


def start_cookie_rotation():
    global _rotation_thread
    interval = int(CONFIG.get("cookie_rotation_interval_sec") or 0)
    path = CONFIG.get("cookie_file")
    if interval <= 0 or not path or rotation_worker_alive():
        return

    def run():
        while True:
            try:
                status = rotate_cookie_file(path)
                log(f"cookie_rotation status={status}")
            except Exception as error:
                log(f"cookie_rotation status=error type={type(error).__name__}")
                status = "error"
            time.sleep(interval if status.startswith("updated_") else 60)

    _rotation_thread = threading.Thread(target=run, name="cookie-rotation", daemon=True)
    _rotation_thread.start()
