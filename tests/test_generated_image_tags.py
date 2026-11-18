"""Unit coverage for Gemini's new inline generated-image tags."""
import json
import unittest

from gemini_web2api.generated_image import (
    _find_rpc_entry,
    _image_tag_id,
    _inline_image_tags,
    extract_generation_result,
)


class InlineImageTagTests(unittest.TestCase):
    def test_agent_tag_is_parsed(self):
        text = (
            '<Image alt="A blue bike" caption="Blue bike on brick" '
            'src="image_agent_tag_11023719095011671215"/>'
        )
        self.assertEqual(
            _inline_image_tags(text),
            [("11023719095011671215", "A blue bike", "Blue bike on brick")],
        )

    def test_generation_content_tag_is_parsed(self):
        text = '<Image alt="A bike" src="image_generation_content/16773786687783378518"/>'
        self.assertEqual(
            _inline_image_tags(text),
            [("16773786687783378518", "A bike", "")],
        )

    def test_tag_id_accepts_full_placeholder_urls(self):
        self.assertEqual(
            _image_tag_id("http://googleusercontent.com/image_generation_content/0_972"),
            "0_972",
        )
        self.assertEqual(
            _image_tag_id("http://googleusercontent.com/image_agent_tag/12345"),
            "12345",
        )

    def test_find_rpc_entry_accepts_agent_placeholder(self):
        context = [22, "a tag-only prompt"] + [None] * 21
        entry = [
            [[None, None, None, [None, None, None, None, None, "sig"]],
             ["http://googleusercontent.com/image_agent_tag/11023719095011671215", 0],
             None, context, None, None, None, None, "im_test", "fl33fyfl33fyfl33"],
            ["r_fa8", "rc_e1b0", "c_ef73", None, "fl33fyfl33fyfl33"],
            1, 0, 1,
        ]
        self.assertIs(_find_rpc_entry(entry), entry[0])

    def test_tag_only_response_keeps_rpc_payload(self):
        context = [22, "a tag-only prompt"] + [None] * 21
        entry = [
            [[None, None, None, [None, None, None, None, None, "sig"]],
             ["http://googleusercontent.com/image_agent_tag/11023719095011671215", 0],
             None, context, None, None, None, None, "im_test", "fl33fyfl33fyfl33"],
            ["r_fa8", "rc_e1b0", "c_ef73", None, "fl33fyfl33fyfl33"],
            1, 0, 1,
        ]
        rich = [None] * 7 + [[[entry]]]
        candidate = [
            "rc_e1b0",
            ['<Image alt="A blue bike" caption="Blue bike" '
             'src="image_agent_tag_11023719095011671215"/>'],
            None, None, None, None, None, None, None, None, None, None, rich,
        ]
        frame = [None, ["c_ef73", "r_fa8"], None, None, [candidate]]
        raw = ")]}'\n123\n" + json.dumps([["wrb.fr", None, json.dumps(frame)]]) + "\n"

        result = extract_generation_result(raw, lambda value: value)
        self.assertEqual(len(result.images), 1)
        image = result.images[0]
        self.assertEqual(image.url, "")
        self.assertEqual(image.image_id, "fl33fyfl33fyfl33")
        self.assertEqual(image.cid, "c_ef73")
        self.assertEqual(image.rid, "r_fa8")
        self.assertEqual(image.rcid, "rc_e1b0")
        self.assertIsNotNone(image.rpc_payload)
        self.assertEqual(image.rpc_payload["signature"], "sig")
        self.assertIn("<Image", result.text)


if __name__ == "__main__":
    unittest.main()
