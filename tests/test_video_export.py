import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from wxvideo import VideoIndex, resolve_videos, collect_videos


class VideoExportTests(unittest.TestCase):
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
