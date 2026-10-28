import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,call

import numpy as np
from PIL import Image,ImageDraw
from voice import pending_voice_rows,transcribe_one,transcribe_in_view,find_candidates


def row(text,x,y,w=90,h=28):
    return dict(text=text,x=x,y=y,w=w,h=h)


class VoiceSkipTests(unittest.TestCase):
    def setUp(self):
        focus=patch('voice.ensure_front_or_raise');focus.start();self.addCleanup(focus.stop)
    def fixture(self,own=False):
        image=Image.new('RGB',(1000,550),(30,30,31));draw=ImageDraw.Draw(image)
        left,right=(550,900) if own else (100,450)
        draw.rounded_rectangle((left,30,right,200),10,fill=(106,206,146) if own else (47,47,48))
        draw.rounded_rectangle((left,280,right,355),10,fill=(106,206,146) if own else (47,47,48))
        first=row('11"',left+30,50);second=row('11"',left+30,300)
        rows=[first,row('已有转写正文',left+20,110,280),second,row('旁边的普通消息',left+20,425,280)]
        return image,rows,first,second

    def test_same_duration_converted_and_unconverted_messages_are_distinguished(self):
        for own in (False,True):
            image,rows,first,second=self.fixture(own)
            pending,skipped=pending_voice_rows(rows,np.asarray(image))
            self.assertEqual(pending,[second]);self.assertEqual(skipped,1)

    def test_one_character_transcription_is_kept_and_ui_labels_are_not_transcripts(self):
        image,rows,first,second=self.fixture()
        rows[1]['text']='嗯'
        self.assertEqual(pending_voice_rows(rows,np.asarray(image)),([second],1))
        rows[1]['text']='转文字'
        self.assertEqual(pending_voice_rows(rows,np.asarray(image)),([first,second],0))

    def test_completed_voice_has_no_clicks_in_view_scan(self):
        view=SimpleNamespace(scale=2,reg={'bottom_px':1000,'top_px':0},win={'y':0},voice_skipped=1)
        with patch('voice.find_candidates',return_value=[]),patch('voice.transcribe_one') as action:
            self.assertEqual(transcribe_in_view(view,edge_filter=False),0)
            action.assert_not_called()

    def test_context_menu_never_presses_retranscribe_or_hide_text(self):
        for label,clicked in [('重新转文字',False),('收起文字',False),('隐藏文字',False),('转文字',True)]:
            with tempfile.TemporaryDirectory() as directory:
                def screenshot(*args,**kwargs):Image.new('RGB',(1000,1000),'white').save(args[0][-1])
                with patch('voice.TMP_DIR',directory),patch('voice.subprocess.run',side_effect=screenshot),patch(
                        'voice.ocr_image',return_value=([row(label,10,10)],300,500)),patch(
                        'voice._click') as action,patch('voice._escape'),patch('voice.time.sleep'):
                    self.assertEqual(transcribe_one(SimpleNamespace(scale=1),100,100,wait=0),clicked)
                    self.assertEqual(sum(c.args[2]=='left' for c in action.call_args_list),int(clicked))

    def test_current_capture_reuses_the_exact_frame_instead_of_taking_another_screenshot(self):
        image,rows,_,_=self.fixture()
        view=SimpleNamespace(scale=1,win={'x':0,'y':0,'id':1},reg={'pane_x_px':0,'top_px':0,'W':1000,'bottom_px':550})
        with patch('voice.capture_window') as screenshot,patch('voice.ocr_image',return_value=(rows,1000,550)):
            candidates=find_candidates(view,image=image)
        screenshot.assert_not_called();self.assertEqual(view.voice_skipped,1);self.assertEqual(len(candidates),1)

    def test_conversion_rechecks_positions_after_the_first_bubble_expands(self):
        view=SimpleNamespace(scale=1,win={'y':0},reg={'top_px':0,'bottom_px':1000})
        with patch('voice.find_candidates',side_effect=[[(100,100),(100,300)],[(100,500)],[]]),patch(
                'voice.transcribe_one',return_value=True) as convert,patch('voice.check_stop'):
            self.assertEqual(transcribe_in_view(view,edge_filter=False),2)
        self.assertEqual([c.args[1:] for c in convert.call_args_list],[(100,100),(100,500)])

    def test_voice_conversion_does_not_inspect_or_press_wechat_accessibility_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            def screenshot(*args,**kwargs):Image.new('RGB',(1000,1000),'white').save(args[0][-1])
            with patch('wxattachments._ax_nodes',side_effect=AssertionError('no WeChat AX access')),patch(
                    'voice.subprocess.run',side_effect=screenshot),patch('voice.ocr_image',return_value=(
                    [row('转文字',10,10)],300,500)),patch('voice._click') as click,patch('voice.time.sleep'):
                self.assertTrue(transcribe_one(SimpleNamespace(scale=1,pid=1),100,100,wait=0))
                self.assertEqual(sum(c.args[2]=='left' for c in click.call_args_list),1)

    def test_voice_menu_capture_failure_closes_menu_without_clicking_an_action(self):
        with patch('voice.subprocess.run',side_effect=OSError('capture failed')),patch(
                'voice._click') as click,patch('voice._escape') as close,patch('voice.time.sleep'):
            with self.assertRaises(OSError):transcribe_one(SimpleNamespace(scale=1),100,100)
        close.assert_called_once()
        self.assertEqual(sum(c.args[2]=='left' for c in click.call_args_list),0)

    def test_a_submitted_voice_is_not_pressed_again_while_its_text_is_still_pending(self):
        view=SimpleNamespace(scale=1,win={'y':0},reg={'top_px':0,'bottom_px':1000})
        with patch('voice.find_candidates',return_value=[(100,200)]),patch('voice.transcribe_one',return_value=True) as convert,patch('voice.check_stop'):
            self.assertEqual(transcribe_in_view(view,edge_filter=False),1)
        convert.assert_called_once()


if __name__=='__main__':unittest.main()
