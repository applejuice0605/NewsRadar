import importlib.util,json,subprocess,unittest,fcntl,tempfile
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
def load(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
runtime=load('update_runtime_health');site=load('generate_site_insights')
class CollectionLifecycle(unittest.TestCase):
 def setUp(self):self.now=datetime.fromisoformat('2026-11-05T16:34:54+08:00')
 def test_running_preserves_last_completed_result(self):
  for result in [True,False]:
   state={'collection':{'success':result,'last_completed_at':'2026-11-05T15:34:26+08:00','duration_seconds':159.9}}
   with patch.object(runtime,'now_local',return_value=self.now):runtime.start_event(state)
   public=site.build_public_health(state,now=self.now)
   self.assertIs(public['collection']['success'],result)
   self.assertEqual(public['collection']['last_completed_at'],'2026-11-05T15:34:26+08:00')
   self.assertTrue(public['collection']['running'])
 def test_first_run_does_not_invent_success(self):
  state={}
  with patch.object(runtime,'now_local',return_value=self.now):runtime.start_event(state)
  self.assertIsNone(site.build_public_health(state,now=self.now)['collection']['success'])
 def test_finished_failure_replaces_previous_success(self):
  state={'collection':{'success':True,'last_completed_at':'2026-11-05T15:34:26+08:00'}}
  with patch.object(runtime,'now_local',return_value=self.now):runtime.start_event(state)
  with patch.object(runtime,'now_local',return_value=self.now+timedelta(minutes=2)),patch.object(runtime,'crawl_snapshot',return_value={'database_updated':True}),patch.object(runtime,'notification_snapshot',return_value={'status':'unknown'}):runtime.finish_event(state,1,None)
  self.assertFalse(state['collection']['running']);self.assertFalse(state['collection']['success'])
 def test_update_holds_exclusive_lock_during_read_modify_write(self):
  with tempfile.TemporaryDirectory() as directory:
   target=Path(directory)/'state.json'
   def checked_load():
    with target.with_suffix('.lock').open('a') as second:
     with self.assertRaises(BlockingIOError):fcntl.flock(second,fcntl.LOCK_EX|fcntl.LOCK_NB)
    return {}
   with patch.object(runtime,'STATE_FILE',target),patch.object(runtime,'load_state',side_effect=checked_load),patch.object(runtime.sys if hasattr(runtime,'sys') else __import__('sys'),'argv',['health','start']):
    self.assertEqual(runtime.main(),0)
   self.assertTrue(json.loads(target.read_text())['collection']['running'])
 def test_public_snapshot_passes_actual_cloud_checker_while_running(self):
  state={'collection':{'success':True,'last_completed_at':'2026-11-05T15:34:26+08:00'}}
  with patch.object(runtime,'now_local',return_value=self.now):runtime.start_event(state)
  public=site.build_public_health(state,now=self.now)
  code="""import {inspectHealth} from './workers/dashboard-monitor.mjs';
const h=JSON.parse(process.argv[1]);const t=Date.parse('2026-11-05T17:00:42+08:00');
const env={ASSETS:{fetch:async req=>{const p=new URL(req.url).pathname;if(p==='/health.json')return Response.json(h);if(p==='/audio/latest.json')return Response.json({date:'2026-11-04',generated_at:'2026-11-04T23:47:38+08:00',audio_url:'/audio/2026-11-04/daily.mp3'});return new Response(new Uint8Array(32),{status:206,headers:{'Content-Type':'audio/mpeg','Content-Range':'bytes 0-31/1000'}});}}};
const r=await inspectHealth(env,t);if(r.status!=='healthy')throw Error(JSON.stringify(r));console.log(r.status);
"""
  result=subprocess.run(['node','--input-type=module','-e',code,json.dumps(public)],cwd=ROOT,capture_output=True,text=True)
  self.assertEqual(result.returncode,0,result.stderr)
if __name__=='__main__':unittest.main()
