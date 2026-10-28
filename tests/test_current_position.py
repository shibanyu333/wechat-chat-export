import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

import numpy as np
from PIL import Image
import engine


class ForwardView:
    def __init__(self,positions):
        self.data=np.random.default_rng(5).integers(20,230,(600,200,3),dtype=np.uint8)
        self.positions=iter(positions);self.position=0;self.calls=[]
        self.capture_warnings=[]
    def grab(self):
        rgb=Image.fromarray(self.data[self.position:self.position+200])
        return rgb,np.asarray(rgb).mean(axis=2).astype(np.float32)
    def scroll_settled(self,lines,**kwargs):
        self.calls.append(lines);self.position=next(self.positions,self.position);return self.grab()
    def scroll(self,lines,**kwargs):
        self.calls.append(lines)
        if lines>0:raise AssertionError('current-position mode must not scroll up')


class CurrentPositionTests(unittest.TestCase):
    def test_already_at_bottom_finishes_with_bounded_wait_and_preserves_first_frame(self):
        view=ForwardView([0]*50);initial,_=view.grab();ticks=iter(range(1000))
        with patch('engine.check_stop'),patch('engine.time.sleep'),patch('engine.time.time',side_effect=lambda:next(ticks)):
            result=engine.stitch_down(view,max_steps=40,strict_current=True,progress=lambda _:None)
        np.testing.assert_array_equal(np.asarray(result),np.asarray(initial))
        self.assertLessEqual(len(view.calls),10)
        self.assertTrue(all(value<0 for value in view.calls))

    def test_forward_join_keeps_exact_start_and_does_not_include_previous_messages(self):
        view=ForwardView([80,160,160]);ticks=iter(range(1000))
        with patch('engine.check_stop'),patch('engine.time.sleep'),patch('engine.time.time',side_effect=lambda:next(ticks)):
            result=engine.stitch_down(view,max_steps=3,strict_current=True,progress=lambda _:None)
        np.testing.assert_array_equal(np.asarray(result),view.data[:360])
        self.assertTrue(all(value<0 for value in view.calls))

    def test_small_real_scroll_keeps_original_prefix_instead_of_replacing_it(self):
        view=ForwardView([5])
        with patch('engine.check_stop'):
            result=engine.stitch_down(view,max_steps=1,strict_current=True,progress=lambda _:None)
        np.testing.assert_array_equal(np.asarray(result),view.data[:205])

    def test_same_position_updates_do_not_rewrite_the_locked_first_frame(self):
        view=ForwardView([0]);initial,_=view.grab()
        original=view.scroll_settled
        def changed(*args,**kwargs):
            rgb,gray=original(*args,**kwargs)
            return Image.new('RGB',rgb.size,'white'),np.full(gray.shape,200,np.float32)
        with patch.object(view,'scroll_settled',side_effect=changed),patch('engine.check_stop'),patch(
                'engine.match_shift',return_value=(0,0.99)):
            result=engine.stitch_down(view,max_steps=1,strict_current=True,progress=lambda _:None)
        np.testing.assert_array_equal(np.asarray(result),np.asarray(initial))

    def test_client_upward_jump_is_not_appended_as_newer_content(self):
        view=ForwardView([100]);initial,_=view.grab()
        with patch('engine.check_stop'),patch('engine.match_shift',side_effect=[(20,0.6),(80,0.99)]):
            result=engine.stitch_down(view,max_steps=1,strict_current=True,progress=lambda _:None)
        np.testing.assert_array_equal(np.asarray(result),np.asarray(initial))

    def test_unmatched_jump_stops_instead_of_scrolling_up_or_appending_wrong_range(self):
        view=ForwardView([300]);initial,_=view.grab();inspect=Mock(return_value=False)
        with patch('engine.check_stop'),patch('engine.match_shift',return_value=(0,0.1)):
            result=engine.stitch_down(view,max_steps=8,strict_current=True,on_frame=inspect,progress=lambda _:None)
        np.testing.assert_array_equal(np.asarray(result),np.asarray(initial))
        self.assertEqual(view.calls,[-3]);self.assertTrue(view.capture_warnings)
        self.assertEqual(inspect.call_count,1)

    def capture(self,from_top):
        image=Image.new('RGB',(200,200),'gray')
        view=SimpleNamespace(scale=2,calibrate_bottom=Mock(),scroll=Mock(),capture_warnings=[],
                             grab=Mock(return_value=(image,np.asarray(image).mean(axis=2))))
        with tempfile.TemporaryDirectory() as directory,patch('engine.preflight',return_value=(True,'ok')),patch(
                'engine.ensure_front',return_value=True),patch('engine.WeChatView',return_value=view),patch(
                'engine.detect_title',return_value='fixture'),patch('engine.bottom_fingerprint'),patch(
                'engine.scroll_to_top') as top,patch('voice.transcribe_down',return_value=(9,0)) as voice,patch(
                'engine.check_stop'),patch('engine.time.sleep'),patch('engine.stitch_down',return_value=image) as stitch,patch(
                'engine.parse_image',return_value=(image,[])),patch('engine.TMP_DIR',directory):
            engine.capture_and_parse(5,True,from_top=from_top,do_image=False,do_video=False,do_files=False,
                                     stitched_out=str(Path(directory)/'capture.png'),progress=lambda _:None)
        return view,top,voice,stitch

    def test_current_mode_does_not_calibrate_by_rolling_or_run_voice_prepass(self):
        view,top,voice,stitch=self.capture(False)
        view.calibrate_bottom.assert_not_called();view.scroll.assert_not_called()
        voice.assert_not_called();top.assert_not_called()
        self.assertTrue(stitch.call_args.kwargs['strict_current'])

    def test_whole_chat_voice_pass_returns_to_real_top_instead_of_count_plus_eight(self):
        view,top,voice,stitch=self.capture(True)
        self.assertEqual(top.call_count,2);voice.assert_called_once();view.scroll.assert_not_called()

    def test_new_voice_still_auto_transcribes_and_crops_to_saved_start(self):
        view=ForwardView([0]);view.win={'y':0};view.reg={'top_px':0};view.scale=1
        initial=view.grab()[0]
        final=Image.new('RGB',(200,240),'red');final.paste(initial,(0,20))
        calls=[]
        def stitch(v,*args,**kwargs):
            calls.append(kwargs)
            if len(calls)==1:kwargs['on_frame'](v)
            return final
        with tempfile.TemporaryDirectory() as directory,patch('engine.TMP_DIR',directory),patch(
                'voice.find_candidates',return_value=[(60,160)]),patch('voice.transcribe_down',return_value=(3,1)) as voice,patch(
                'engine._return_to_current_start',return_value=20) as restore,patch('engine.stitch_down',side_effect=stitch):
            result,count=engine._capture_current(view,8,True,lambda _:None)
        self.assertEqual(count,1);voice.assert_called_once();restore.assert_called_once()
        np.testing.assert_array_equal(np.asarray(result)[:200],np.asarray(initial))
        self.assertNotIn('on_frame',calls[1])

    def test_same_frame_does_not_repeat_voice_ocr_checks(self):
        view=ForwardView([0]*30);inspect=Mock(return_value=False);ticks=iter(range(1000))
        with patch('engine.check_stop'),patch('engine.time.sleep'),patch('engine.time.time',side_effect=lambda:next(ticks)):
            engine.stitch_down(view,max_steps=25,strict_current=True,on_frame=inspect,progress=lambda _:None)
        self.assertEqual(inspect.call_count,1)

    def test_start_locator_uses_pixels_and_rejects_ambiguous_matches(self):
        data=np.random.default_rng(3).uniform(20,230,(800,200)).astype(np.float32)
        anchor=engine._make_current_anchor(data[100:300])
        self.assertEqual(engine._locate_current_anchor(data[60:260],anchor),40)
        duplicate=np.concatenate([anchor['pixels'],anchor['pixels']],axis=0)
        full=np.zeros((duplicate.shape[0],200),np.float32)
        full[:,anchor['left']:anchor['right']:4]=duplicate
        self.assertIsNone(engine._locate_current_anchor(full,anchor))

    def test_return_does_not_add_arbitrary_extra_upward_scrolls(self):
        data=np.random.default_rng(7).uniform(20,230,(900,200)).astype(np.float32)
        anchor=engine._make_current_anchor(data[100:300])
        class View:
            position=420
            calls=[]
            def grab(self):return None,data[self.position:self.position+200]
            def scroll(self,direction,**kwargs):
                self.calls.append(direction);self.position-=80 if direction>0 else -80
        view=View()
        with patch('engine.check_stop'):
            trim=engine._return_to_current_start(view,anchor,10,lambda _:None)
        self.assertEqual(view.position+trim,100)
        self.assertLessEqual(len(view.calls),5)

    def test_flat_background_cannot_win_anchor_match_after_transcription_expansion(self):
        data=np.random.default_rng(13).uniform(20,130,(600,200)).astype(np.float32)
        anchor=engine._make_current_anchor(data[100:300])
        frame=np.full((300,200),47,np.float32)
        height=anchor['pixels'].shape[0]
        frame[20:20+height]=data[100:100+height]
        self.assertEqual(engine._locate_current_anchor(frame,anchor),20)

    def test_current_mode_with_only_converted_voices_is_really_one_forward_pass(self):
        view=ForwardView([80,160,160]);view.voice_skipped=1
        with tempfile.TemporaryDirectory() as directory,patch('engine.TMP_DIR',directory),patch(
                'voice.find_candidates',return_value=[]),patch('voice.transcribe_down') as voice,patch(
                'engine.check_stop'):
            result,count=engine._capture_current(view,3,True,lambda _:None)
        np.testing.assert_array_equal(np.asarray(result),view.data[:360])
        self.assertEqual(count,0);voice.assert_not_called()
        self.assertTrue(all(value<0 for value in view.calls))

    def test_forward_calibration_excludes_static_draft_without_any_probe_scroll(self):
        from wechat_ui import WeChatView
        view=object.__new__(WeChatView);view.reg={'top_px':0,'bottom_px':120};view.win={'y':0};view.scale=1
        before=np.zeros((120,200),np.float32);after=before.copy()
        before[:80]=np.random.default_rng(9).uniform(0,200,(80,200))
        after[:80]=np.roll(before[:80],-10,axis=0)
        before[95:115,:100]=after[95:115,:100]=100
        with patch.object(view,'scroll') as scroll:
            self.assertTrue(view.calibrate_forward_frames(before,after))
        self.assertLess(view.reg['bottom_px'],95);scroll.assert_not_called()


if __name__=='__main__':unittest.main()
