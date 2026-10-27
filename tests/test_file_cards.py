import tempfile
import unittest
import zipfile
from pathlib import Path

from parse3 import take_file_card
from wxfiles import FileIndex, resolve_and_collect
from wxstorage import account_dirs, DATA_ACCESS_HELP
from unittest.mock import patch


def row(text, x=154):
    return {"text": text, "x": x}


class FileCardTests(unittest.TestCase):
    def test_wechat_data_permission_error_is_explicit(self):
        with patch("wxstorage.os.listdir", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(PermissionError, "开启「微信」"):
                account_dirs("/protected/wechat")

    def test_copy_failure_is_not_counted_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "example.pdf").write_bytes(b"%PDF-1.7\nfixture\n%%EOF\n")
            msgs = [{"type": "file", "fname": "example.pdf"}]
            with patch("wxfiles.file_roots", return_value=[str(source)]), patch(
                    "shutil.copy2", side_effect=PermissionError("denied")):
                self.assertEqual(resolve_and_collect(msgs, str(root / "文件")), (0, 1))
            self.assertIn("复制失败", msgs[0]["fnote"])

    def test_explicit_copy_allows_large_local_attachment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "example.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("fixture.txt", "large attachment test")
            entry = {"path": str(source), "name": source.name, "size": 216 * 1024 * 1024}
            msgs = [{"type": "file", "fname": source.name}]
            with patch("wxfiles.FileIndex") as index, patch("shutil.copy2") as copy:
                index.return_value.find.return_value = (entry, 1.0)
                self.assertEqual(resolve_and_collect(msgs, str(root / "文件")), (1, 1))
                copy.assert_called_once()

    def test_wrapped_image_attachment_excludes_avatar_text(self):
        card = take_file_card([
            row("头像文字", 40), row("card-01_副"), row("本.webp"),
            row("881.6K"), row("微信电脑版", 196),
        ])
        self.assertEqual(card["name"], "card-01_副本.webp")
        self.assertEqual(card["size"], "881.6K")

    def test_pdf_icon_repairs_misread_extension(self):
        card = take_file_card([
            row("头像文字", 40), row("example 网站功能简介•paf"),
            row("PDF", 549), row("385.2K"), row("微信电脑版", 196),
        ])
        self.assertEqual(card["name"], "example 网站功能简介.pdf")

    def test_pdf_icon_and_size_repair_hyphen_instead_of_extension_dot(self):
        card = take_file_card([
            row("头像文字", 40), row("example 网站功能简介-pdf"),
            row("PDF", 549), row("385.2K"), row("微信电脑版", 196),
        ])
        self.assertIsNotNone(card)
        self.assertEqual(card["name"], "example 网站功能简介.pdf")

    def test_hyphen_pdf_text_requires_both_pdf_icon_and_file_size(self):
        for rows in ([row("example-pdf"), row("385.2K")],
                     [row("example-pdf"), row("PDF")]):
            self.assertIsNone(take_file_card(rows))

    def test_zip_size_and_download_status_share_line(self):
        card = take_file_card([
            row("头像文字", 40), row("example-website.zip"),
            row("216.0M 未下载"), row("微信电脑版", 196),
        ])
        self.assertEqual(card, {
            "name": "example-website.zip", "size": "216.0M", "status": "未下载",
        })

    def test_mentions_and_unknown_extensions_are_not_attachments(self):
        for rows in ([row("请看 example.webp")],
                     [row("example.paf"), row("385.2K"), row("微信电脑版")]):
            self.assertIsNone(take_file_card(rows))

    def test_source_without_size_still_works(self):
        self.assertEqual(take_file_card([
            row("example.docx"), row("微信电脑版"),
        ])["name"], "example.docx")

    def test_recognized_files_are_copied_and_missing_zip_is_explained(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            content = b"attachment contents"
            (source / "card-01_副本.webp").write_bytes(content)
            msgs = [
                {"type": "file", "fname": "card-01_剛本.webp"},
                {"type": "file", "fname": "example.zip", "fstatus": "未下载"},
            ]
            with patch("wxfiles.file_roots", return_value=[str(source)]):
                self.assertEqual(resolve_and_collect(msgs, str(root / "文件")), (1, 2))
            self.assertEqual((root / msgs[0]["fcopy"]).read_bytes(), content)
            self.assertIn("微信显示未下载", msgs[1]["fnote"])


if __name__ == "__main__":
    unittest.main()
