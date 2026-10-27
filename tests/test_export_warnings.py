import tempfile
import unittest
import copy
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, Mock

from PIL import Image

from app import Api


class ExportWarningTests(unittest.TestCase):
    @staticmethod
    def capture_fixture():
        return {"im": Image.new("RGB", (300, 100)), "scale": 2.0, "title": "fixture",
                "msgs": [{"type": "text", "sender": "them", "text": "保留这条消息",
                          "x0": 20, "y0": 10, "x1": 220, "y1": 60}]}

    def test_attachment_access_does_not_block_capture_preflight(self):
        api = Api()
        with patch("app.preflight", return_value=(True, "ok")), patch.object(
                api, "check_storage", side_effect=PermissionError("denied")) as storage:
            self.assertEqual(api.check_env(True, True), {"ok": True, "msg": "ok"})
            storage.assert_not_called()
        with patch("app.preflight", return_value=(False, "缺少录屏权限")):
            self.assertEqual(api.check_env(True, True), {"ok": False, "msg": "缺少录屏权限"})

    def test_optional_metadata_write_failure_preserves_capture_and_export(self):
        api = Api()
        captured = self.capture_fixture()
        with tempfile.TemporaryDirectory() as temp:
            # 真实文件系统失败：同名目录使可选元数据文件无法写入。
            (Path(temp) / "_tmp" / "last_capture.json").mkdir(parents=True)
            with patch("app.data_root", return_value=temp), patch("app.clear_stop"), patch(
                    "app.window", Mock()), patch.object(api, "_to_front"), patch(
                    "app.capture_and_parse", return_value=captured):
                result = api.capture(5, False)
            self.assertTrue(result["ok"])
            self.assertEqual(result["messages"][0]["text"], "保留这条消息")
            self.assertIn("信息保存失败", result["warnings"][0])
            self.assertIs(api.res, captured)
            with patch("app.make_export_dir", return_value=temp):
                exported = api.export(None, None, ["md"], "fixture", want_files=False, want_videos=False)
            self.assertTrue(exported["ok"])
            self.assertIn("保留这条消息", (Path(temp) / "聊天记录.md").read_text())

    def test_restore_keeps_messages_when_video_lookup_fails_after_partial_changes(self):
        for error in (PermissionError("denied"), RuntimeError("invalid video index")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temp:
                api = Api()
                captured = self.capture_fixture()
                original_msgs = copy.deepcopy(captured["msgs"])
                path = Path(temp) / "_tmp" / "stitched_full.png"
                path.parent.mkdir()
                captured["im"].save(path)

                def fail_video_lookup(im, msgs, **kwargs):
                    msgs.clear()
                    raise error

                with patch("app.data_root", return_value=temp), patch(
                        "parse3.parse_image", return_value=(captured["im"], captured["msgs"])), patch(
                        "wxvideo.resolve_videos", side_effect=fail_video_lookup), patch.object(
                        api, "check_storage", side_effect=PermissionError("denied")) as storage:
                    result = api.restore_last(True, True)
                storage.assert_not_called()
                self.assertTrue(result["ok"])
                self.assertEqual(api.res["msgs"], original_msgs)
                self.assertEqual(result["messages"][0]["text"], "保留这条消息")
                self.assertTrue(result["warnings"])
                expected = "开启「微信」" if isinstance(error, PermissionError) else "invalid video index"
                self.assertIn(expected, result["warnings"][0])
                with patch("app.make_export_dir", return_value=temp):
                    exported = api.export(None, None, ["md"], "fixture", want_files=False, want_videos=False)
                self.assertTrue(exported["ok"])
                self.assertEqual(exported["warnings"], result["warnings"])
                self.assertIn("保留这条消息", (Path(temp) / "聊天记录.md").read_text())

    def test_invalid_optional_metadata_does_not_block_restoring_local_image(self):
        api = Api()
        captured = self.capture_fixture()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "_tmp" / "stitched_full.png"
            path.parent.mkdir()
            captured["im"].save(path)
            (path.parent / "last_capture.json").write_text("{")
            with patch("app.data_root", return_value=temp), patch(
                    "parse3.parse_image", return_value=(captured["im"], captured["msgs"])):
                result = api.restore_last(True, False)
            self.assertTrue(result["ok"])
            self.assertEqual(result["title"], "微信会话")
            self.assertIn("信息读取失败", result["warnings"][0])

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

    def test_copy_failures_are_all_listed_even_when_sources_are_located(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.txt"
            source.write_text("source")
            api = Api()
            api.res = {"im": Image.new("RGB", (300, 100)), "scale": 2.0, "title": "fixture",
                       "msgs": [{"type": "file", "sender": "them", "fname": f"attachment-{i}.txt"}
                                for i in range(12)]}

            def failed_copy(messages, directory):
                for message in messages:
                    message.update(fpath=str(source), fnote="复制失败: denied")
                return 0, len(messages)

            with patch("app.make_export_dir", return_value=temp), patch(
                    "app.resolve_and_collect", side_effect=failed_copy):
                result = api.export(None, None, ["md"], "fixture", want_videos=False)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["missing"]), 12)
            self.assertEqual(len(result["missing_details"]), 12)
            self.assertIn("附件核对.txt", result["outputs"])
            self.assertIn("attachment-11.txt", (Path(temp) / "附件核对.txt").read_text())

    def test_capture_forwards_file_download_preferences(self):
        api = Api()
        with patch("app.clear_stop"), patch("app.window", Mock()), patch.object(
                api, "_to_front"), patch("app.capture_and_parse", side_effect=RuntimeError("fixture stop")) as capture:
            api.capture(5, False, do_files=False, do_file_download=False)
        self.assertIs(capture.call_args.kwargs["do_files"], False)
        self.assertIs(capture.call_args.kwargs["do_file_download"], False)

    def test_restored_capture_can_download_before_its_first_export(self):
        api = Api()
        api.res = {"im": Image.new("RGB", (300, 100)), "scale": 2.0, "title": "fixture",
                   "msgs": [{"type": "file", "sender": "them", "fname": "example.zip"}]}
        with tempfile.TemporaryDirectory() as temp, patch("app.make_export_dir", return_value=temp), patch.object(
                api, "_download_selected_files", side_effect=RuntimeError("微信显示文件已过期")) as download, patch(
                "app.resolve_and_collect", return_value=(0, 1)):
            result = api.export(None, None, ["md"], "fixture", want_videos=False, do_file_download=True)
        download.assert_called_once()
        self.assertIn("过期", result["missing_details"][0]["reason"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for UI promise-rejection tests")
    def test_ui_recovers_buttons_when_capture_or_restore_rejects(self):
        # 运行实际页面脚本中的事件处理器，模拟桥接 API reject，而不是只检查源码含 finally。
        script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
function setup(overrides){
  const elements={};
  function element(){
    const classes=new Set();
    return {disabled:false,checked:false,value:'',textContent:'',innerHTML:'',style:{},children:[],dataset:{},
      classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),
        toggle:(x,on)=>on?classes.add(x):classes.delete(x)},
      appendChild(child){this.children.push(child);},scrollIntoView(){}};
  }
  const get=id=>elements[id]||(elements[id]=element());
  get('startBtn').textContent='开始抓取';get('restoreBtn').textContent='恢复上次抓取';
  get('fmtDocx').checked=true;get('wantFiles').checked=true;
  get('fileDownload').checked=true;
  get('stopBtn').classList.add('hidden');
  const context={document:{getElementById:get,createElement:element,querySelectorAll:()=>[]},
    Image:function(){return element();},localStorage:{getItem:()=>null,setItem(){}},
    pywebview:{api:{check_env:async()=>({ok:true}),...overrides}},
    alert(){throw Error('Unexpected modal alert');}};
  context.window=context;
  vm.createContext(context);vm.runInContext(script,context);
  return {get,context};
}
(async()=>{
  for(const [method, handler] of [['check_env','startBtn'],['capture','startBtn'],['restore_last','restoreBtn'],
                                 ['retry_missing_files','retryFilesBtn'],['export','exportBtn']]){
    const ui=setup({[method]:async()=>{throw Error(method+' rejected');}});
    await ui.get(handler).onclick();
    assert.equal(ui.get('startBtn').disabled,false,method);
    assert.equal(ui.get('restoreBtn').disabled,false,method);
    assert.equal(ui.get('stopBtn').classList.contains('hidden'),true,method);
    assert.match(ui.get('log').textContent,new RegExp(method+' rejected'));
  }
  for(const [method, handler] of [['capture','startBtn'],['restore_last','restoreBtn']]){
    const ui=setup({[method]:async()=>({ok:false,msg:'reported failure'})});
    await ui.get(handler).onclick();
    assert.equal(ui.get('startBtn').disabled,false);
    assert.equal(ui.get('restoreBtn').disabled,false);
    assert.match(ui.get('log').textContent,/reported failure/);
  }
  const warning='<video access failed>';
  const ui=setup({restore_last:async()=>({ok:true,title:'fixture',img:'',messages:[],warnings:[warning]})});
  await ui.get('restoreBtn').onclick();
  assert.equal(ui.get('result').children[0].textContent,warning);
  let captureArgs;
  const flags=setup({capture:async(...args)=>{captureArgs=args;return {ok:false,msg:'fixture'};}});
  await flags.get('startBtn').onclick();
  assert.equal(captureArgs[6],true);assert.equal(captureArgs[7],true);
  const missing=Array.from({length:12},(_,i)=>({name:i===11?'<last>.zip':'attachment-'+i,reason:'下载失败'}));
  const audit=setup({export:async()=>({ok:true,outputs:['聊天记录.md','附件核对.txt'],count:12,
    folder:'fixture',folder_name:'fixture',files:0,missing_details:missing})});
  await audit.get('exportBtn').onclick();
  assert.match(audit.get('result').innerHTML,/&lt;last&gt;.zip/);
  assert.match(audit.get('result').innerHTML,/12 项附件未完成/);
  assert.equal(audit.get('exportBtn').disabled,false);
  assert.equal(audit.get('stopBtn').classList.contains('hidden'),true);
  console.log('UI rejected/failed API paths restore controls and show warnings');
})().catch(error=>{console.error(error);process.exit(1);});
"""
        html = Path(__file__).resolve().parents[1] / "ui" / "index.html"
        result = subprocess.run([shutil.which("node"), "-e", script, str(html)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
