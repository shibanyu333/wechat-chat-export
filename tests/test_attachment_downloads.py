import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
from PIL import Image

from wxattachments import download_attachments, WeChatAttachmentUI
from wxfiles import FileIndex, download_complete, resolve_and_collect, write_attachment_report
from wechat_ui import StopRequested


def make_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fixture.txt", "downloaded attachment")


class FakeUI:
    def __init__(self, action=None, problem=None, located=True):
        self.action = action or (lambda message: None)
        self.issue = problem
        self.located = located
        self.clicks = []
        self.polls = 0
        self.prepared = self.restored = self.finished = 0

    def prepare(self):
        self.prepared += 1

    def locate(self, message):
        return 123 if self.located else None

    def activate_download(self, message, location):
        self.clicks.append(message["fname"])
        self.action(message)

    def problem(self, message, location):
        self.polls += 1
        return self.issue(message, self.polls) if callable(self.issue) else self.issue

    def finish_file(self):
        self.finished += 1

    def restore(self):
        self.restored += 1


class AttachmentDownloadTests(unittest.TestCase):
    def setUp(self):
        self.stop = patch("wxattachments.check_stop")
        self.stop.start()
        self.addCleanup(self.stop.stop)

    @staticmethod
    def message(name="example.zip", y=100):
        return {"type": "file", "fname": name, "fstatus": "未下载", "y0": y}

    def run_download(self, root, messages, ui, timeout=0.05):
        return download_attachments(None, None, messages, progress=lambda _: None,
                                    timeout=timeout, poll_interval=0, settle=0, ui=ui,
                                    index_factory=lambda: FileIndex([str(root)]))

    def test_missing_zip_is_downloaded_then_copied_and_linked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "wechat"
            root.mkdir()
            msgs = [self.message()]
            ui = FakeUI(action=lambda _: make_zip(root / "example.zip"))
            self.assertEqual(self.run_download(root, msgs, ui), (1, 1))
            self.assertEqual(ui.clicks, ["example.zip"])
            export = Path(temp) / "export"
            with patch("wxfiles.file_roots", return_value=[str(root)]):
                self.assertEqual(resolve_and_collect(msgs, str(export / "文件")), (1, 1))
            copied = export / msgs[0]["fcopy"]
            self.assertEqual(hashlib.sha256(copied.read_bytes()).digest(),
                             hashlib.sha256((root / "example.zip").read_bytes()).digest())
            report, missing = write_attachment_report(msgs, str(export))
            self.assertEqual(missing, [])
            self.assertIn("[已导出] example.zip", Path(report).read_text())

    def test_missing_file_downloads_then_link_mode_keeps_only_original_path(self):
        from render_md import render_md
        from render_docx import render
        from docx import Document
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'wechat';root.mkdir()
            messages=[dict(self.message(),sender='them',y1=180,x0=50,x1=300)]
            ui=FakeUI(action=lambda _:make_zip(root/'example.zip'))
            self.assertEqual(self.run_download(root,messages,ui),(1,1))
            with patch('wxfiles.file_roots',return_value=[str(root)]):
                self.assertEqual(resolve_and_collect(messages),(1,1))
            self.assertNotIn('fcopy',messages[0])
            out=Path(temp)/'export';out.mkdir()
            image=Image.new('RGB',(400,250),'white')
            render_md(image,messages,'测试',str(out/'chat.md'))
            render(image,messages,'测试',str(out/'chat.docx'))
            self.assertIn(str(root/'example.zip'),(out/'chat.md').read_text())
            document=Document(out/'chat.docx')
            self.assertTrue(any(str(root/'example.zip') in relation.target_ref
                                for relation in document.part.rels.values()))
            self.assertFalse((out/'文件').exists())

    def test_partial_zip_is_not_finished_just_because_its_size_pauses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "example.zip"

            def status(message, polls):
                if polls == 4:
                    make_zip(path)

            ui = FakeUI(action=lambda _: path.write_bytes(b"PK\x03\x04partial"), problem=status)
            msgs = [self.message()]
            self.assertEqual(self.run_download(root, msgs, ui), (1, 1))
            self.assertGreaterEqual(ui.polls, 5)
            self.assertTrue(download_complete(path))

    def test_existing_incomplete_zip_requests_download_instead_of_waiting_without_starting(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'example.zip';path.write_bytes(b'PK\x03\x04partial')
            ui=FakeUI(action=lambda _:make_zip(path))
            messages=[self.message()]
            self.assertEqual(self.run_download(root,messages,ui,timeout=.01),(1,1))
            self.assertEqual(ui.clicks,['example.zip'])

    def test_timeout_keeps_reason_and_partial_zip_is_not_exported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "wechat"
            root.mkdir()
            msgs = [self.message()]
            ui = FakeUI(action=lambda _: (root / "example.zip").write_bytes(b"PK\x03\x04partial"))
            self.assertEqual(self.run_download(root, msgs, ui, timeout=0.01), (0, 1))
            self.assertIn("超时", msgs[0]["fdownload_error"])
            with patch("wxfiles.file_roots", return_value=[str(root)]):
                self.assertEqual(resolve_and_collect(msgs, str(Path(temp) / "files")), (0, 1))
            self.assertIn("超时", msgs[0]["fnote"])
            self.assertNotIn("fpath", msgs[0])

    def test_already_downloaded_files_are_never_clicked_or_opened(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_zip(root / "example.zip")
            ui = FakeUI(action=lambda _: self.fail("Existing files must never be opened"))
            msgs = [self.message()]
            self.assertEqual(self.run_download(root, msgs, ui), (1, 1))
            self.assertEqual(ui.clicks, [])
            self.assertEqual(ui.prepared, 0)

    def test_same_attachment_appearing_twice_is_downloaded_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ui = FakeUI(action=lambda _: make_zip(root / "example.zip"))
            msgs = [self.message(y=100), self.message(y=300)]
            self.assertEqual(self.run_download(root, msgs, ui), (2, 2))
            self.assertEqual(ui.clicks, ["example.zip"])

    def test_unconfirmed_file_card_is_not_clicked(self):
        with tempfile.TemporaryDirectory() as temp:
            msgs = [self.message()]
            ui = FakeUI(located=False)
            self.assertEqual(self.run_download(Path(temp), msgs, ui), (0, 1))
            self.assertEqual(ui.clicks, [])
            self.assertIn("确认", msgs[0]["fdownload_error"])
            self.assertEqual(ui.restored, 1)

    def test_expired_file_has_explicit_reason_in_the_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            msgs = [self.message()]
            ui = FakeUI(problem="微信显示文件已过期或失效，无法下载")
            self.assertEqual(self.run_download(root, msgs, ui), (0, 1))
            with patch("wxfiles.file_roots", return_value=[str(root)]):
                resolve_and_collect(msgs)
            self.assertIn("过期", msgs[0]["fnote"])

    def test_stopping_marks_every_remaining_attachment_without_losing_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def stop(message):
                raise StopRequested("用户停止")

            msgs = [self.message("first.zip", 100), self.message("second.zip", 300)]
            ui = FakeUI(action=stop)
            with self.assertRaises(StopRequested):
                self.run_download(root, msgs, ui)
            self.assertEqual(len(msgs), 2)
            self.assertTrue(all("中断" in m["fdownload_error"] for m in msgs))
            self.assertEqual(ui.restored, 1)

    def test_save_as_path_is_supported_even_before_wechat_index_sees_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / "staging"
            staging.mkdir()

            def save_as(message):
                path = staging / "example.zip"
                make_zip(path)
                message["fdownload_path"] = str(path)

            msgs = [self.message()]
            ui = FakeUI(action=save_as)
            empty = root / "wechat"
            empty.mkdir()
            self.assertEqual(self.run_download(empty, msgs, ui), (1, 1))
            self.assertEqual(msgs[0]["fpath"], str(staging / "example.zip"))


class AttachmentIntegrityAndReportTests(unittest.TestCase):
    def test_new_incomplete_same_name_does_not_hide_an_existing_complete_zip_or_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);older=root/'older';newer=root/'newer';older.mkdir();newer.mkdir()
            make_zip(older/'example.zip');(older/'example.pdf').write_bytes(b'%PDF-1.7\nfixture\n%%EOF\n')
            for name in ('example.zip','example.pdf'):
                (newer/name).write_bytes(b'partial')
                os.utime(older/name,(100,100));os.utime(newer/name,(200,200))
            messages=[{'type':'file','fname':name} for name in ('example.zip','example.pdf')]
            with patch('wxfiles.file_roots',return_value=[str(root)]):
                self.assertEqual(resolve_and_collect(messages),(2,2))
            self.assertTrue(all(Path(m['fpath']).parent==older for m in messages))

    def test_complete_downloads_copy_is_found_even_when_wechat_has_an_incomplete_same_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);wechat=root/'wechat';downloads=root/'downloads';wechat.mkdir();downloads.mkdir()
            (wechat/'example.zip').write_bytes(b'partial');make_zip(downloads/'example.zip')
            messages=[{'type':'file','fname':'example.zip'}]
            with patch('wxfiles.file_roots',return_value=[str(wechat)]),patch(
                    'wxfiles.os.path.expanduser',return_value=str(downloads)):
                self.assertEqual(resolve_and_collect(messages),(1,1))
            self.assertEqual(messages[0]['fpath'],str(downloads/'example.zip'))

    def test_card_size_selects_the_complete_matching_copy_among_same_name_pdfs(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);older=root/'older';newer=root/'newer';older.mkdir();newer.mkdir()
            (older/'example.pdf').write_bytes(b'%PDF-1.7\n'+b'x'*1009+b'%%EOF\n')
            (newer/'example.pdf').write_bytes(b'%PDF-1.7\nshort\n%%EOF\n')
            os.utime(older/'example.pdf',(100,100));os.utime(newer/'example.pdf',(200,200))
            messages=[{'type':'file','fname':'example.pdf','fsize':'1.0K'}]
            with patch('wxfiles.file_roots',return_value=[str(root)]):
                self.assertEqual(resolve_and_collect(messages),(1,1))
            self.assertEqual(messages[0]['fpath'],str(older/'example.pdf'))

    def test_zip_and_pdf_need_complete_ending_structures_and_expected_size(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "example.zip"
            path.write_bytes(b"PK\x03\x04partial")
            self.assertFalse(download_complete(path))
            make_zip(path)
            self.assertTrue(download_complete(path))
            self.assertFalse(download_complete(path, "216.0M"))
            pdf = Path(temp) / "example.pdf"
            pdf.write_bytes(b"%PDF-1.7\npartial")
            self.assertFalse(download_complete(pdf))
            pdf.write_bytes(b"%PDF-1.7\nfixture\n%%EOF\n")
            self.assertTrue(download_complete(pdf))

    def test_report_lists_all_failures_without_the_old_eight_item_truncation(self):
        with tempfile.TemporaryDirectory() as temp:
            msgs = [{"type": "file", "fname": f"attachment-{i}.zip", "fnote": "下载失败"}
                    for i in range(18)] + [{"type": "video", "vdur": "0:06", "vnote": "视频已过期"}]
            report, missing = write_attachment_report(msgs, temp)
            text = Path(report).read_text()
            self.assertEqual(len(missing), 19)
            self.assertIn("attachment-17.zip", text)
            self.assertIn("视频已过期", text)
            self.assertIn("未完成 19 项", text)

    def test_copy_failure_is_listed_even_if_source_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "source.txt"
            p.write_text("source")
            msgs = [{"type": "file", "fname": p.name, "fpath": str(p), "fnote": "复制失败: denied"}]
            _, missing = write_attachment_report(msgs, temp, copy_files=True)
            self.assertEqual(missing[0]["reason"], "复制失败: denied")


class AttachmentPositionTests(unittest.TestCase):
    def test_file_currently_receiving_is_only_polled_without_opening_its_menu(self):
        for status in ('下载中','接收中'):
            ui=WeChatAttachmentUI.__new__(WeChatAttachmentUI)
            ui.view=SimpleNamespace(pid=1)
            with patch.object(ui,'_card',return_value={'name':'example.zip','status':status}),patch(
                    'wxattachments.ensure_front_or_raise'),patch('wxattachments._ax_named') as menu:
                ui.activate_download({'fname':'example.zip'},0)
            menu.assert_not_called()

    def test_download_navigation_returns_to_original_captured_position(self):
        class View:
            def __init__(self):
                self.image = Image.fromarray(np.random.default_rng(42).integers(0, 256, (1000, 200, 3), dtype=np.uint8))
                self.offset = 800
                self.reg = {"top_px": 0, "bottom_px": 200}

            def grab(self):
                image = self.image.crop((0, self.offset, 200, self.offset + 200))
                return image, np.asarray(image.convert("L"), dtype=np.float32)

            def scroll_settled(self, lines, steps=3, **kwargs):
                self.offset = max(0, min(800, self.offset + (-1 if lines > 0 else 1) * 60 * steps))
                return self.grab()

        view = View()
        ui = WeChatAttachmentUI(view, view.image)
        with patch("wxattachments.ensure_front_or_raise"), patch("wxattachments.check_stop"):
            ui.prepare()
            top = ui._seek(140, (100, 180))
            self.assertIsNotNone(top)
            self.assertGreaterEqual(100 - top, 8)
            ui.restore()
        self.assertLess(abs(view.offset - 800), 30)


if __name__ == "__main__":
    unittest.main()
