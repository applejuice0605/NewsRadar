import hashlib,json,sys,tempfile,unittest,subprocess
from pathlib import Path
from datetime import date
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'TrendRadar')]
import podcast_dialogue as writer
import generate_dialogue_podcast as generator
import podcast_dialogue_episode as episode
class ReachedSynthesis(Exception):pass
class CachedRecovery(unittest.TestCase):
 def fixture(self):return json.loads((ROOT/'tests/fixtures/nov06-cached-writer.json').read_text())
 def seed(self,root,raw,materials):
  day=root/'private/2026-11-06';cache=day/'private';cache.mkdir(parents=True)
  prompt='fixture {{FACT_BRIEF}}';promptpath=root/'prompt.txt';promptpath.write_text(prompt)
  plan,_=writer.project_editorial_plan(materials['shared_editorial_plan']);brief=json.dumps(plan,ensure_ascii=False,sort_keys=True)
  config={'MODEL':'fixture/model','API_BASE':''};settings={'temperature':1.0,'max_tokens':9000,'system':writer.WRITER_SYSTEM}
  key=writer.cache_key(brief,prompt,config['MODEL'],'',settings)
  rawpath=cache/'raw'/f'{key}.txt';rawpath.parent.mkdir();rawpath.write_text(raw)
  manifest=cache/'cache'/f'{key}.json';manifest.parent.mkdir();manifest.write_text(json.dumps({'raw_response':raw,'response_metadata':{'finish_reason':'stop'},'request_settings':settings}))
  return day,rawpath,promptpath,config
 def execute(self,root,raw):
  fixture=self.fixture();materials=fixture['materials'];day,rawpath,prompt,config=self.seed(root,raw,materials);calls=[]
  def runner(command,**kwargs):
   calls.append(Path(command[1]).name)
   if Path(command[1]).name=='generate_dialogue_podcast.py':
    args=generator.parse_args(command[2:])
    with patch.object(generator,'_load_ai_config',return_value=config),patch.object(generator,'_client_factory',side_effect=AssertionError('cached run must not construct a model client')):
     generator.generate(args)
    return subprocess.CompletedProcess(command,0)
   raise ReachedSynthesis()
  with self.assertRaises(ReachedSynthesis):
   episode.build_dialogue_bundle(materials=materials,report_date=date(2026,11,6),stage=root/'stage',prompt_path=prompt,runtime_root=root/'runtime',private_root=root/'private',command_runner=runner)
  self.assertEqual(calls,['generate_dialogue_podcast.py','soulx_dialogue_synthesize.py'])
  self.assertEqual(rawpath.read_text(),raw)
  review=json.loads((day/'draft-review.json').read_text());self.assertTrue(review['cache_hit']);self.assertEqual(review['additional_model_calls'],0);self.assertFalse(review['warnings'])
  self.assertEqual(review['turn_count'],49);self.assertEqual(review['spoken_chars'],2538)
  self.assertEqual(review['raw_sha256'],hashlib.sha256(raw.encode()).hexdigest())
  self.assertTrue((day/'punctuation-normalization.json').exists())
 def test_real_cached_script_reaches_synthesis_without_model_call(self):
  with tempfile.TemporaryDirectory() as temp:self.execute(Path(temp),self.fixture()['raw'])
 def test_unbreakable_cached_script_never_reaches_synthesis(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);fixture=self.fixture();raw=fixture['raw'];lines=raw.splitlines()
   i=next(i for i,s in enumerate(lines) if s.startswith('[41:'))
   lines[i]=lines[i][:7]+('这是没有安全断句边界的解释'*6)+'。';raw='\n'.join(lines)
   materials=fixture['materials'];day,rawpath,prompt,config=self.seed(root,raw,materials);calls=[]
   def runner(command,**kwargs):
    calls.append(Path(command[1]).name)
    args=generator.parse_args(command[2:])
    with patch.object(generator,'_load_ai_config',return_value=config),patch.object(generator,'_client_factory',side_effect=AssertionError('no model call')):generator.generate(args)
    return subprocess.CompletedProcess(command,0)
   with self.assertRaisesRegex(episode.DialogueBundleError,'warnings'):
    episode.build_dialogue_bundle(materials=materials,report_date=date(2026,11,6),stage=root/'stage',prompt_path=prompt,runtime_root=root/'runtime',private_root=root/'private',command_runner=runner)
   self.assertEqual(calls,['generate_dialogue_podcast.py']);self.assertFalse((day/'synthesis-process.json').exists());self.assertEqual(rawpath.read_text(),raw)
if __name__=='__main__':unittest.main()
