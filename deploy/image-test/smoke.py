"""One image inference per invocation, with a persistent 60-second cooldown."""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib


def test_png():
    width, height = 256, 128
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if 24 <= x < 104 and 24 <= y < 104:
                rows.extend((230, 25, 25))
            elif (x - 192) ** 2 + (y - 64) ** 2 <= 40 ** 2:
                rows.extend((25, 25, 230))
            else:
                rows.extend((255, 255, 255))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


def reserve_call(path, base_url):
    """Record before sending; failed requests also consume the cooldown."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as state:
        os.chmod(path, 0o600)
        fcntl.flock(state, fcntl.LOCK_EX)
        state.seek(0)
        saved = state.read()
        data = json.loads(saved) if saved else {}
        now = time.time()
        elapsed = now - data.get(base_url, 0)
        if elapsed < 60:
            raise ValueError(f"Cooldown active; wait at least {int(60 - elapsed) + 1} seconds")
        data[base_url] = now
        state.seek(0)
        state.truncate()
        json.dump(data, state)
        state.flush()
        os.fsync(state.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--model", default="gemini-3.6-flash")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    # The prompt contains no hint about the picture's expected contents.
    body = {"model": args.model, "stream": args.stream, "messages": [{
        "role": "user", "content": [
            {"type": "text", "text": "List the visible shapes and their colors from left to right. Answer briefly in English."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(test_png()).decode()}},
        ],
    }]}
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("GEMINI_TEST_API_KEY")
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    state = Path.home() / ".cache/gemini-web2api-image-smoke.json"
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Keep this descriptor locked until process exit, including the HTTP read.
    lock_file = state.with_suffix(".lock").open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        raise ValueError("Another image smoke request is still running") from error
    reserve_call(state, base_url)
    started = time.monotonic()
    request = urllib.request.Request(base_url + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
    # urllib does not retry failed generation requests.
    with urllib.request.urlopen(request, timeout=180) as response:
        text = ""
        if args.stream:
            done = False
            for line in response:
                if not line.startswith(b"data: "):
                    continue
                data = line[6:].strip()
                if data == b"[DONE]":
                    done = True
                    break
                event = json.loads(data)
                if event.get("error"):
                    raise RuntimeError(json.dumps(event["error"]))
                for choice in event.get("choices", []):
                    text += choice.get("delta", {}).get("content") or ""
            if not done:
                raise RuntimeError("SSE terminated without [DONE]")
        else:
            data = json.load(response)
            text = data["choices"][0]["message"].get("content") or ""
    lower = text.lower()
    verified = all(word in lower for word in ("red", "square", "blue", "circle"))
    verified = verified and lower.index("red") < lower.index("blue")
    print(json.dumps({"generation_requests": 1, "stream": args.stream,
                      "duration_sec": round(time.monotonic() - started, 2),
                      "verified": verified, "reply": text}, ensure_ascii=False))
    if not verified:
        raise SystemExit("Image contents were not confirmed; stop and inspect, do not retry automatically")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}; stopped without retry", file=sys.stderr)
        raise SystemExit(1)
    except (RuntimeError, ValueError, OSError) as error:
        print(f"Stopped without retry: {str(error)[:300]}", file=sys.stderr)
        raise SystemExit(1)
