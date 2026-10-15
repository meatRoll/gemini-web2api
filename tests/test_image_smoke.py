"""Verify that the manual image probe never retries or bypasses its cooldown."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

SMOKE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy/image-test/smoke.py"))


class ImageSmokeTests(unittest.TestCase):
    def test_png_is_valid_and_small(self):
        import struct
        import zlib
        png = SMOKE["test_png"]()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(png), 4096)
        offset, compressed = 8, bytearray()
        while offset < len(png):
            length = struct.unpack(">I", png[offset:offset + 4])[0]
            kind = png[offset + 4:offset + 8]
            data = png[offset + 8:offset + 8 + length]
            if kind == b"IDAT":
                compressed.extend(data)
            offset += 12 + length
        rows = zlib.decompress(compressed)
        self.assertEqual(len(rows), 128 * (256 * 3 + 1))

    def test_cooldown_is_persistent_and_failed_calls_cannot_bypass_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with mock.patch("time.time", return_value=1000):
                SMOKE["reserve_call"](path, "http://localhost/v1")
            with mock.patch("time.time", return_value=1059):
                with self.assertRaisesRegex(ValueError, "Cooldown"):
                    SMOKE["reserve_call"](path, "http://localhost/v1")
            with mock.patch("time.time", return_value=1060):
                SMOKE["reserve_call"](path, "http://localhost/v1")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_probe_makes_one_blind_request(self):
        reply = {"choices": [{"message": {"content": "A red square on the left, a blue circle on the right."}}]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(Path, "home", return_value=Path(directory)), \
                mock.patch("sys.argv", ["smoke.py"]), \
                mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(reply).encode())) as request, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            SMOKE["main"]()
        request.assert_called_once()
        body = json.loads(request.call_args.args[0].data)
        prompt = body["messages"][0]["content"][0]["text"].lower()
        self.assertNotIn("red", prompt)
        self.assertNotIn("blue", prompt)
        self.assertTrue(json.loads(output.getvalue())["verified"])

    def test_http_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(Path, "home", return_value=Path(directory)), \
                mock.patch("sys.argv", ["smoke.py"]), \
                mock.patch("urllib.request.urlopen", side_effect=HTTPError("http://localhost", 429, "rate limited", {}, None)) as request:
            with self.assertRaises(HTTPError):
                SMOKE["main"]()
            with self.assertRaisesRegex(ValueError, "Cooldown"):
                SMOKE["main"]()
            request.assert_called_once()
