import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from PIL import Image, ImageDraw
import ocr
from parse3 import parse_image
from render_md import render_md
from render_docx import render
from docx import Document


def line(text, x, y, w, h=28):
    return dict(text=text, x=x, y=y, w=w, h=h, conf=1.0)


class TextAndQuoteTests(unittest.TestCase):
    def test_accelerator_failure_retries_accurate_recognition_on_cpu(self):
        request=MagicMock();request.results.return_value=[]
        handler=MagicMock()
        handler.performRequests_error_.side_effect=[(False,'e5rt execution stream call failed 13'),(True,None)]
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'sample.png');Image.new('RGB',(300,120),'white').save(path)
            with patch.object(ocr.Vision,'VNRecognizeTextRequest') as factory,patch.object(
                    ocr.Vision,'VNImageRequestHandler') as handler_factory:
                factory.alloc.return_value.init.return_value=request
                handler_factory.alloc.return_value.initWithCGImage_options_.return_value=handler
                self.assertEqual(ocr.ocr_image(path)[0],[])
                request.setUsesCPUOnly_.assert_called_once_with(True)
                self.assertEqual(handler.performRequests_error_.call_count,2)

    def test_title_ocr_failure_does_not_block_capturing_the_message_body(self):
        from engine import detect_title
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            frame=Path(tmp)/'frame.png';Image.new('RGB',(600,300),'white').save(frame)
            view=SimpleNamespace(win={'id':1},scale=1,reg={'pane_x_px':0,'W':600,'top_px':100})
            with patch('engine.capture_window'),patch('wechat_ui._TMP',str(frame)),patch(
                    'engine.TMP_DIR',tmp),patch('engine.ocr_image',side_effect=RuntimeError('OCR failed')):
                self.assertIsNone(detect_title(view))
                self.assertFalse((Path(tmp)/'_title.png').exists())

    def test_vision_failure_is_not_silently_treated_as_no_text(self):
        request = MagicMock()
        request.results.return_value = []
        handler = MagicMock()
        handler.performRequests_error_.return_value = (False, 'recognition failed')
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'sample.png')
            Image.new('RGB', (300, 120), 'white').save(path)
            with patch.object(ocr.Vision, 'VNRecognizeTextRequest') as factory, patch.object(
                    ocr.Vision, 'VNImageRequestHandler') as handler_factory:
                factory.alloc.return_value.init.return_value = request
                handler_factory.alloc.return_value.initWithCGImage_options_.return_value = handler
                with self.assertRaisesRegex(RuntimeError, 'OCR'):
                    ocr.ocr_image(path)

    def test_empty_slice_is_retried_before_discarding_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'sample.png')
            Image.new('RGB', (400, 2100), 'black').save(path)
            calls = {}
            def recognize(fp):
                key = Path(fp).name
                calls[key] = calls.get(key, 0) + 1
                return ([] if calls[key] == 1 else [line('测试正文', 80, 100, 180)]), 400, 1800
            with patch('ocr.ocr_image', side_effect=recognize):
                rows, _, _ = ocr.ocr_image_sliced(path)
            self.assertGreaterEqual(len(rows), 1)
            self.assertTrue(all(value >= 2 for value in calls.values()))

    def fixture(self, root, sender='them'):
        im = Image.new('RGB', (1000, 600), (30, 30, 31))
        draw = ImageDraw.Draw(im)
        own = sender == 'me'
        box = (650, 40, 900, 115) if own else (100, 40, 540, 115)
        draw.rounded_rectangle(box, radius=10, fill=(106, 206, 146) if own else (47, 47, 48))
        rows = [line('这条是回复正文', box[0]+20, 65, 210)]
        qx = 900 if own else 100
        draw.rectangle((qx, 132, qx+3, 202), fill=(69,69,69))
        rows += [line('原作者：原消息第一行', 410 if own else 120, 138, 450, 25),
                 line('第二行内容', 410 if own else 120, 174, 220, 25)]
        # A real image farther below must stay an image even when it contains OCR text.
        draw.rectangle((100, 300, 500, 520), fill=(120, 70, 20))
        rows += [line('图片里的文字', 140, 340, 250)]
        path = str(root / 'capture.png');im.save(path)
        return path, rows

    def test_reply_and_quote_are_kept_as_separate_text_for_both_sides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for sender in ('them', 'me'):
                path, rows = self.fixture(root, sender)
                with patch('parse3.ocr_image_sliced', return_value=(rows,1000,600)):
                    im, messages = parse_image(path)
                texts = [m for m in messages if m['type']=='text']
                self.assertEqual(len(texts),1)
                message = texts[0]
                self.assertEqual(message['text'], '这条是回复正文')
                self.assertEqual(message['quote']['name'], '原作者')
                self.assertIn('第二行内容', message['quote']['text'])
                self.assertEqual(sum(m['type']=='media' for m in messages),1)
                md = root / 'chat.md';docx = root / 'chat.docx'
                render_md(im,messages,'测试会话',str(md))
                render(im,messages,'测试会话',str(docx))
                content=md.read_text()
                self.assertIn('引用',content);self.assertIn('原作者',content);self.assertIn('第二行内容',content)
                doc=Document(docx)
                alltext='\n'.join(t.text or '' for t in doc.element.iter() if t.tag.endswith('}t'))
                self.assertIn('原作者',alltext);self.assertIn('第二行内容',alltext)

    def test_no_ocr_cannot_be_reported_as_successful_image_only_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = self.fixture(Path(tmp))
            with patch('parse3.ocr_image_sliced', return_value=([],1000,600)):
                with self.assertRaisesRegex(RuntimeError, '文字|OCR'):
                    parse_image(path)

    def test_documents_show_attachment_names_and_visible_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);images=root/'图片';images.mkdir()
            image=Image.new('RGB',(500,300),'white')
            poster=images/'video_001.jpg';image.save(poster)
            file=root/'文件 示例.pdf';file.write_bytes(b'%PDF-1.7\n%%EOF')
            video=root/'视频 示例.mp4';video.write_bytes(b'video fixture')
            messages=[dict(type='media',sender='them',x0=30,y0=30,x1=250,y1=200),
                      dict(type='file',sender='them',fname=file.name,fpath=str(file)),
                      dict(type='video',sender='them',vpath=str(video),x0=30,y0=30,x1=250,y1=200)]
            with patch('wxvideo.save_poster',return_value=str(poster)):
                render_md(image,messages,'测试',str(root/'chat.md'),media_dir=str(images))
                render(image,messages,'测试',str(root/'chat.docx'),media_dir=str(images))
            md=(root/'chat.md').read_text()
            doc=Document(root/'chat.docx')
            text='\n'.join(t.text or '' for t in doc.element.iter() if t.tag.endswith('}t'))
            for content in (md,text):
                for label in ['图片名','文件名','视频名','封面图片名','位置','img_001.png',file.name,video.name]:
                    self.assertIn(label,content)
                self.assertIn(str(file),content)
                self.assertIn(str(video),content)

    def test_unreadable_quote_is_preserved_as_a_linked_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path, rows=self.fixture(root)
            rows=[rows[0],rows[-1]]
            with patch('parse3.ocr_image_sliced',return_value=(rows,1000,600)):
                im,messages=parse_image(path)
            message=next(m for m in messages if m['type']=='text')
            self.assertIn('preview_box',message['quote'])
            render_md(im,messages,'测试',str(root/'chat.md'))
            render(im,messages,'测试',str(root/'chat.docx'))
            self.assertIn('引用截图',(root/'chat.md').read_text())
            self.assertTrue((root/'chat_图片/quote_001.png').exists())


if __name__=='__main__':unittest.main()
