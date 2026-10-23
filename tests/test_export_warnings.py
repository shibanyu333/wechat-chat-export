import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import Api


class ExportWarningTests(unittest.TestCase):
    def test_permission_failure_is_visible_in_result_and_document(self):
        api = Api()
        api.res = {"im": Image.new("RGB", (300, 100)), "scale": 2.0,
                   "title": "fixture", "msgs": [
                       {"type": "file", "sender": "them", "fname": "example.pdf"}]}
        with tempfile.TemporaryDirectory() as temp:
            with patch("app.make_export_dir", return_value=temp), patch(
                    "app.resolve_and_collect", side_effect=PermissionError("denied")):
                result = api.export(None, None, ["md"], "fixture", want_videos=False)
            self.assertTrue(result["ok"])
            self.assertEqual((result["files"], result["files_total"]), (0, 1))
            self.assertTrue(result["warnings"])
            self.assertIn("开启「微信」", (Path(temp) / "聊天记录.md").read_text())


if __name__ == "__main__":
    unittest.main()
