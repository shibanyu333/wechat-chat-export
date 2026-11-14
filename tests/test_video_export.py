import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from wxvideo import VideoIndex, resolve_videos, collect_videos


class VideoExportTests(unittest.TestCase):
    def test_portrait_video_with_bottom_gradient_is_recognized(self):
        rng = np.random.default_rng(84)
        poster = Image.new('RGB', (224, 485), 'white')
        draw = ImageDraw.Draw(poster)
        for y in (16, 38, 78, 90, 102, 144, 160, 198, 220, 240, 445, 460):
            for x in range(12, 180, 21):
                draw.rectangle((x, y, x+int(rng.integers(3, 16)),
                                y+int(rng.integers(2, 5))), fill=(70, 70, 70))
        texture = Image.fromarray(rng.integers(35, 180, (35, 40, 3), dtype=np.uint8))
        poster.paste(texture.resize((204, 150), Image.Resampling.BICUBIC), (10, 285))
        ImageDraw.Draw(poster).rectangle((10, 253, 214, 277), fill=(188, 161, 89))
        # The chat thumbnail has slightly different rounding and an overlay
        # across the entire bottom edge, not just behind the duration text.
        bubble = poster.resize((225, 491), Image.Resampling.BICUBIC)
        pixels = np.asarray(bubble, dtype=np.float32)
        alpha = np.clip((np.arange(491)/491-0.8)/0.2, 0, 1)*0.9
        bubble = Image.fromarray((pixels*(1-alpha[:, None, None])).astype(np.uint8))
        draw = ImageDraw.Draw(bubble)
        draw.ellipse((76, 210, 150, 284), fill=(190, 190, 190), outline='white', width=2)
        draw.text((175, 457), '0:21', fill='white')
        canvas = Image.new('RGB', (900, 700), (31, 31, 31))
        canvas.paste(bubble, (140, 100))
        messages = [{'type': 'media', 'sender': 'them',
                     'x0': 140, 'y0': 100, 'x1': 365, 'y1': 591}]
        with tempfile.TemporaryDirectory() as temp:
            month = Path(temp) / '2026-11'
            month.mkdir()
            poster.save(month / 'portrait_thumb.jpg')
            index = VideoIndex([temp])
            self.assertEqual(resolve_videos(canvas, messages, index), (1, 1))
        self.assertEqual(messages[0]['vstem'], 'portrait')
        self.assertGreaterEqual(messages[0]['vconf'], 0.93)

    def test_wide_image_is_not_matched_to_a_strip_of_unrelated_video(self):
        rng = np.random.default_rng(23)
        strip = Image.fromarray(rng.integers(0, 255, (16, 48, 3), dtype=np.uint8))
        poster = Image.new('RGB', (480, 360), 'black')
        poster.paste(strip.resize((480, 160)), (0, 100))
        bubble = strip.resize((480, 160))
        canvas = Image.new('RGB', (900, 500), (31, 31, 31))
        canvas.paste(bubble, (140, 100))
        messages = [{'type': 'media', 'sender': 'them',
                     'x0': 140, 'y0': 100, 'x1': 620, 'y1': 260}]
        with tempfile.TemporaryDirectory() as temp:
            month = Path(temp) / '2026-10'
            month.mkdir()
            poster.save(month / 'unrelated_thumb.jpg')
            index = VideoIndex([temp])
            self.assertIsNone(index.match(bubble)[0])
            self.assertEqual(resolve_videos(canvas, messages, index), (0, 0))
        self.assertEqual(messages[0]['type'], 'media')

    def test_portrait_video_can_still_match_a_height_limited_bubble(self):
        rng = np.random.default_rng(31)
        poster = Image.fromarray(rng.integers(0, 255, (85, 48, 3), dtype=np.uint8))
        bubble = poster.crop((0, 10, 48, 72)).resize((480, 620))
        with tempfile.TemporaryDirectory() as temp:
            month = Path(temp) / '2026-10'
            month.mkdir()
            poster.resize((480, 850)).save(month / 'portrait_thumb.jpg')
            entry, score, _ = VideoIndex([temp]).match(bubble)
            self.assertIsNotNone(entry)
            self.assertGreaterEqual(score, 0.93)

    def test_dark_duration_only_bubble_recovers_video(self):
        rng = np.random.default_rng(17)
        texture = rng.integers(5, 80, (27, 48, 3), dtype=np.uint8)
        clean = Image.fromarray(texture).resize((480, 270), Image.Resampling.BICUBIC)
        canvas = Image.new("RGB", (900, 500), (31, 31, 31))
        canvas.paste(clean, (140, 100))
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((340, 195, 420, 275), outline="white", width=3)
        draw.polygon(((369, 219), (369, 249), (394, 234)), fill="white")
        draw.rectangle((548, 330, 612, 364), fill="white")
        msgs = [{"type": "text", "sender": "them", "text": "0:06",
                 "x0": 148, "y0": 104, "x1": 620, "y1": 366}]
        with tempfile.TemporaryDirectory() as temp:
            month = Path(temp) / "2026-10"
            month.mkdir()
            clean.save(month / "known_thumb.jpg")
            idx = VideoIndex([temp])
            self.assertEqual(resolve_videos(canvas, msgs, idx), (1, 1))
        self.assertEqual(msgs[0]["type"], "video")
        self.assertEqual(msgs[0]["vstem"], "known")
        self.assertEqual(msgs[0]["vdur"], "0:06")
        self.assertGreaterEqual(msgs[0]["vconf"], 0.93)

    def test_plain_duration_message_is_not_a_video(self):
        im = Image.new("RGB", (900, 500), (31, 31, 31))
        msgs = [{"type": "text", "sender": "them", "text": "0:06",
                 "x0": 140, "y0": 100, "x1": 230, "y1": 170}]
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(resolve_videos(im, msgs, VideoIndex([temp])), (0, 0))
        self.assertEqual(msgs[0]["type"], "text")

    def test_video_copy_failure_is_not_counted_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mp4"
            source.write_bytes(b"video fixture")
            msgs = [{"type": "video", "vpath": str(source)}]
            with patch("wxvideo.shutil.copy2", side_effect=PermissionError("denied")):
                self.assertEqual(collect_videos(msgs, str(Path(temp) / "视频")), (0, 1))
            self.assertNotIn("vcopy", msgs[0])
            self.assertIn("复制失败", msgs[0]["vnote"])


if __name__ == "__main__":
    unittest.main()
