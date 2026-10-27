import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Api
from wxfiles import FileIndex


class PreferencesTests(unittest.TestCase):
    def test_link_and_download_preferences_survive_new_api_instance(self):
        with tempfile.TemporaryDirectory() as directory, patch('app.data_root',return_value=directory):
            self.assertEqual(Api().get_preferences(),{})
            self.assertTrue(Api().save_preferences({'fileMode':'link','fileDownload':True,'unexpected':'ignored'}))
            self.assertEqual(Api().get_preferences(),{'fileMode':'link','fileDownload':True})

    def test_save_as_download_is_found_by_exact_name_without_recursive_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wechat=root/'wechat';wechat.mkdir();downloads=root/'Downloads';downloads.mkdir()
            (downloads/'example (1).pdf').write_bytes(b'%PDF-1.7\n%%EOF')
            with patch('wxfiles.file_roots',return_value=[str(wechat)]),patch(
                    'wxfiles.os.path.expanduser',return_value=str(downloads)):
                index=FileIndex();entry,confidence=index.find('example.pdf')
                self.assertEqual(Path(entry['path']).name,'example (1).pdf')
                self.assertEqual(confidence,1.0)
                self.assertIsNone(index.find('different.pdf')[0])
