"""V2.1 candidate transport, independent of legacy audition compatibility."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from podcast_dialogue import validate_daily_transport,DialogueError
BODY='模型新增文档检索，可以在授权范围内查资料。用户少了手动查找的步骤，但结果仍需检查。这里说的是检索，不是让它替人作所有决定。'
def raw(turns=38):
 lines=['[SECTION:overview]']
 for i in range(turns):
  if i==4:lines.append('[SECTION:discussion]')
  if i==turns-3:lines.append('[SECTION:closing]')
  lines.append(f'[{i+1:02}:S{1+i%2}]{BODY}')
 return '\n'.join(lines)
class DailyProfileTests(unittest.TestCase):
 def test_valid_and_same_speaker(self):
  text=raw().replace('[02:S2]','[02:S1]')
  doc,review=validate_daily_transport(text,source_text=BODY)
  self.assertEqual(review['turn_count'],38)
  self.assertEqual(doc['text'][0][0],doc['text'][1][0])
  self.assertTrue(1800<=review['spoken_chars']<=2600)
 def test_exact_section_order_count_and_numbering(self):
  for text in [raw().replace('[SECTION:discussion]','[SECTION:discussion]\n[SECTION:discussion]'),raw().replace('[SECTION:discussion]','[SECTION:other]'),raw().replace('[02:S2]','[03:S2]'),raw().replace('[02:S2]','[S2]'),raw().replace('[02:S2]','[02:S3]'),raw().replace(':S2]',':S1]')]:
   with self.subTest(text=text[:80]),self.assertRaises(DialogueError):validate_daily_transport(text,source_text=BODY)
 def test_limits_do_not_count_transport_or_tags_as_spoken_words(self):
  for n in [34,45]:
   with self.assertRaises(DialogueError):validate_daily_transport(raw(n),source_text=BODY)
  _,a=validate_daily_transport(raw(),source_text=BODY)
  _,b=validate_daily_transport(raw().replace(BODY,'<|sigh|>'+BODY,1),source_text=BODY)
  self.assertEqual(a['spoken_chars'],b['spoken_chars'])
  with self.assertRaises(DialogueError):validate_daily_transport(raw().replace(BODY,'<|applause|>'+BODY,1),source_text=BODY)
  with self.assertRaises(DialogueError):validate_daily_transport(raw().replace(BODY,'<|sigh|>'+BODY,4),source_text=BODY)
  with self.assertRaises(DialogueError):validate_daily_transport(raw().replace(BODY,'<|coughing|>'+BODY,1),source_text=BODY)
if __name__=='__main__':unittest.main()
