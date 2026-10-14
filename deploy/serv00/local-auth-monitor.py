"""Monitor the local Gemini auth export once per minute using panel2 as the network exit."""
import argparse
import base64
import fcntl
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from gemini_web2api import gemini
from gemini_web2api.config import CONFIG, load_env
from gemini_web2api.models import resolve_model

REMOTE_CODE = """import base64,json,sys,urllib.error,urllib.request
p=json.load(sys.stdin)
r=urllib.request.Request(p['url'],data=base64.b64decode(p['body']),headers=p['headers'],method='POST')
try:
    with urllib.request.urlopen(r,timeout=35) as response:
        body=response.read()
        status=response.status
except urllib.error.HTTPError as error:
    body=error.read()
    status=error.code
json.dump({'status':status,'body':base64.b64encode(body).decode()},sys.stdout)
"""
PAGE_CODE = r"""import json,re,sys,urllib.request
p=json.load(sys.stdin)
req=urllib.request.Request('https://gemini.google.com'+p['path']+'/app',headers={
    'Cookie':p['cookie'],'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
with urllib.request.urlopen(req,timeout=25) as response:
    html=response.read().decode('utf8',errors='replace')
token=re.search(r'"SNlM0e"\s*:\s*"([^"]+)"',html)
build=re.search(r'boq_assistant-bard-web-server_[0-9.]+_p[0-9]+',html)
if not token or not build: raise SystemExit('missing page metadata')
json.dump({'xsrf_token':json.loads('"'+token.group(1)+'"'),'gemini_bl':build.group()},sys.stdout)
"""
ROTATE_CODE = """import json,sys,urllib.error,urllib.request
p=json.load(sys.stdin)
req=urllib.request.Request('https://accounts.google.com/RotateCookies',
    data=b'[000,"-0000000000000000000"]',method='POST',headers={
    'Cookie':p['cookie'],'Content-Type':'application/json',
    'Origin':'https://accounts.google.com',
    'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
try:
    with urllib.request.urlopen(req,timeout=25) as response:
        status=response.status
        headers=response.headers.get_all('Set-Cookie',[])
except urllib.error.HTTPError as error:
    status=error.code
    headers=error.headers.get_all('Set-Cookie',[])
cookies={}
for header in headers:
    part=header.split(';',1)[0]
    if '=' in part:
        name,value=part.split('=',1)
        cookies[name]=value
json.dump({'status':status,'cookies':cookies},sys.stdout)
"""


def run_on_panel2(code, payload, timeout):
    process = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "panel2.serv00.com", "python3 -c " + shlex.quote(code)],
        input=json.dumps(payload), text=True, capture_output=True, timeout=timeout,
    )
    if process.returncode:
        raise ConnectionError("SSH transport failed")
    return json.loads(process.stdout)


def refresh_page_metadata():
    cookie, _ = gemini.load_cookie()
    if not cookie:
        raise ValueError("Cookie missing")
    path = gemini._account_prefix()
    metadata = run_on_panel2(PAGE_CODE, {"cookie": cookie, "path": path}, 38)
    changed = metadata["xsrf_token"] != CONFIG["xsrf_token"]
    CONFIG.update(xsrf_token=metadata["xsrf_token"], gemini_bl=metadata["gemini_bl"])
    return "changed" if changed else "same"


def relay_request(request, **_kwargs):
    wire = {
        "url": request.full_url,
        "body": base64.b64encode(request.data).decode(),
        "headers": dict(request.header_items()),
    }
    result = run_on_panel2(REMOTE_CODE, wire, 48)
    body = io.BytesIO(base64.b64decode(result["body"]))
    if result["status"] != 200:
        raise HTTPError(request.full_url, result["status"], "upstream HTTP error", {}, body)
    return body


class RotationSession:
    def __init__(self):
        self.cookie, self.sapisid = gemini.load_cookie()
        self.next_rotation = time.monotonic() + 120

    def rotate_if_due(self):
        if time.monotonic() < self.next_rotation:
            return "waiting"
        self.next_rotation = time.monotonic() + 60
        result = run_on_panel2(ROTATE_CODE, {"cookie": self.cookie}, 38)
        if result["status"] != 200:
            return f"http_{result['status']}"
        jar = dict(part.strip().split('=', 1) for part in self.cookie.split(';') if '=' in part)
        changed = sum(jar.get(name) != value for name, value in result["cookies"].items())
        jar.update(result["cookies"])
        self.cookie = '; '.join(name + '=' + value for name, value in jar.items())
        if changed:
            self.next_rotation = time.monotonic() + 300
        return f"updated_{changed}"


def check(auth_file, refresh=False, rotation=None):
    CONFIG.update(cookie_file=str(auth_file), proxy=None, retry_attempts=1,
                  request_timeout_sec=35, log_requests=False, temporary_chats=True)
    _, model, think, error, extra = resolve_model(CONFIG["default_model"])
    if error:
        raise ValueError(error)
    start = time.monotonic()
    page_status = "not_checked"
    rotation_status = "not_checked"
    try:
        if refresh:
            page_status = refresh_page_metadata()
        if rotation:
            rotation_status = rotation.rotate_if_due()
        with mock.patch.object(gemini.urllib.request, "urlopen", side_effect=relay_request):
            if rotation:
                with mock.patch.object(gemini, "load_cookie",
                                       return_value=(rotation.cookie, rotation.sapisid)):
                    reply = gemini.generate("Reply exactly: OK", model, think, extra_fields=extra)
            else:
                reply = gemini.generate("Reply exactly: OK", model, think, extra_fields=extra)
        status = "ok" if reply.strip() else "empty"
    except HTTPError as error:
        status = f"http_{error.code}"
    except Exception as error:
        code = re.search(r"BardErrorInfo\s*\[(\d+)\]", str(error))
        status = f"bard_{code.group(1)}" if code else f"error_{type(error).__name__}"
    return status, time.monotonic() - start, page_status, rotation_status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("auth_file", type=Path)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--pid", type=Path, required=True)
    parser.add_argument("--refresh-page-token", action="store_true")
    parser.add_argument("--rotate-cookies", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    os.chdir(ROOT)
    load_env(str(ROOT / ".env.cookie"))
    if not args.auth_file.is_file():
        raise SystemExit("Auth file does not exist")
    CONFIG["cookie_file"] = str(args.auth_file)
    rotation = RotationSession() if args.rotate_cookies else None

    with args.pid.open("a+") as pid_file:
        try:
            fcntl.flock(pid_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Monitor already running")
        pid_file.seek(0)
        pid_file.truncate()
        pid_file.write(str(os.getpid()) + "\n")
        pid_file.flush()
        try:
            next_check = time.monotonic()
            while True:
                now = time.time()
                status, duration, page_status, rotation_status = check(
                    args.auth_file, args.refresh_page_token, rotation)
                age = (now - args.auth_file.stat().st_mtime) / 60
                with args.log.open("a") as log:
                    line = (f"{datetime.fromtimestamp(now, timezone.utc).isoformat()}\t"
                            f"{status}\t{duration:.1f}s\texport_age={age:.1f}m")
                    if args.refresh_page_token:
                        line += f"\tpage_token={page_status}"
                    if args.rotate_cookies:
                        line += f"\trotation={rotation_status}"
                    log.write(line + "\n")
                next_check += 60
                time.sleep(max(0, next_check - time.monotonic()))
        finally:
            pid_file.seek(0)
            pid_file.truncate()


if __name__ == "__main__":
    main()
