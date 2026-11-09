import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from parse3 import parse_image


def row(text, x, y, w, h=28):
    return dict(text=text, x=x, y=y, w=w, h=h, conf=1.0)


class MediaDetectionTests(unittest.TestCase):
    def parse(self, image, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'chat.png'
            image.save(path)
            with patch('parse3.ocr_image_sliced', return_value=(rows, *image.size)):
                return parse_image(str(path))[1]

    def test_gray_photo_details_do_not_erase_photo_without_ocr_text(self):
        image = Image.new('RGB', (1000, 700), (30, 30, 31))
        draw = ImageDraw.Draw(image)
        draw.rectangle((100, 150, 540, 470), fill=(160, 95, 65))
        # Thin gray machine parts become a large filled box after closing.
        for y in range(150, 471, 8):
            draw.rectangle((100, y, 540, y+2), fill=(47, 47, 48))
        draw.rectangle((100, 150, 102, 470), fill=(47, 47, 48))
        draw.rounded_rectangle((100, 530, 540, 605), radius=10, fill=(47, 47, 48))
        messages = self.parse(image, [row('下一条正常消息', 120, 550, 280)])
        media = [m for m in messages if m['type'] == 'media']
        self.assertEqual(len(media), 1)
        self.assertLessEqual(media[0]['y0'], 155)
        self.assertGreaterEqual(media[0]['y1'], 465)

    def test_text_inside_photo_stays_in_photo_instead_of_a_chat_message(self):
        image = Image.new('RGB', (1000, 700), (30, 30, 31))
        draw = ImageDraw.Draw(image)
        draw.rectangle((100, 150, 540, 470), fill=(70, 95, 130))
        for y in range(260, 350, 8):
            draw.rectangle((125, y, 340, y+2), fill=(47, 47, 48))
        rows = [row('图片里的标题', 130, 275, 200)]
        messages = self.parse(image, rows)
        self.assertEqual([m['type'] for m in messages], ['media'])

    def test_short_message_survives_alongside_tall_multiline_bubbles(self):
        image = Image.new('RGB', (1000, 1300), (30, 30, 31))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((100, 40, 210, 115), radius=10, fill=(47, 47, 48))
        rows = [row('好', 125, 65, 30)]
        for top in (180, 530, 880):
            draw.rounded_rectangle((100, top, 740, top+270), radius=10, fill=(47, 47, 48))
            rows.extend(row(f'多行聊天内容{top}', 120, y, 260) for y in range(top+20, top+250, 50))
        messages = self.parse(image, rows)
        self.assertEqual(sum(m['type'] == 'text' for m in messages), 4)
        self.assertEqual(messages[0]['text'], '好')

    def test_separate_photo_is_not_removed_by_text_at_same_height(self):
        image = Image.new('RGB', (1000, 500), (30, 30, 31))
        draw = ImageDraw.Draw(image)
        draw.rectangle((100, 100, 400, 350), fill=(70, 95, 130))
        draw.rounded_rectangle((600, 100, 900, 350), radius=10, fill=(106, 206, 146))
        messages = self.parse(image, [row('右边的文字', 620, 130, 200)])
        self.assertEqual(sum(m['type'] == 'media' for m in messages), 1)
        self.assertEqual(sum(m['type'] == 'text' for m in messages), 1)


if __name__ == '__main__':
    unittest.main()
