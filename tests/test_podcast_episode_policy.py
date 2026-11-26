import json
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from podcast_dialogue import DialogueError
from podcast_episode_policy import read_slots, slot_dialogue, slot_instruction
import generate_dialogue_podcast as generator


class EpisodePolicyTest(unittest.TestCase):
    def test_program_owns_speaker_and_order(self):
        rows = {slot: "冻结事实。" for slot in range(1, 37)}
        original = read_slots("\n".join(f"{slot:02}: {line}" for slot, line in reversed(list(rows.items()))), set(rows))
        dialogue = slot_dialogue(original)
        self.assertEqual(dialogue["text"][:2], [["S1", rows[1]], ["S2", rows[2]]])
        self.assertIn("15 S1 选题2", slot_instruction(3))
        for raw in ("S3: 稿件", "01: 第一条\n01: 重复", "37: 超出分配"):
            with self.subTest(raw=raw), self.assertRaises(DialogueError):
                read_slots(raw, set(rows))

    def test_missing_slot_repaired_once_and_source_validation_runs(self):
        day = date(2026, 11, 21)
        line = "这是一段对来源事实的介绍。接下来继续说明报道给出的条件和适用范围。然后回到节目主题，解释相关变化。"
        rows = "\n".join(f"{slot:02}: {line}" for slot in range(1, 37) if slot != 8)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "materials.json"
            raw.write_text(json.dumps({"date": day.isoformat()}))
            prompt = root / "prompt"
            prompt.write_text("{{FACT_BRIEF}}")
            args = generator.parse_args(["--date", str(day), "--materials", str(raw), "--output-root",
                                         str(root / "private"), "--prompt", str(prompt), "--draft-only",
                                         "--format", "slot-lines"])
            audit = {"writer_output_format": "slot-lines", "additional_model_calls": 1}
            with patch.object(generator, "_validate_existing_materials"), \
                 patch.object(generator, "assess_materials", return_value={"adequate": True}), \
                 patch.object(generator, "build_fact_brief", return_value=("only frozen facts", {})), \
                 patch.object(generator, "_source_audit_text", return_value="only frozen facts"), \
                 patch.object(generator, "_load_ai_config", return_value={}), \
                 patch("podcast_episode_policy.writing_seconds", return_value=100), \
                 patch.object(generator, "one_call_draft", side_effect=[
                     (rows, audit.copy()), ("08: " + line, audit.copy())]) as api:
                result = generator.generate(args)
            self.assertEqual(api.call_count, 2)
            self.assertEqual(result["call"]["revision"]["slots"], [8])
            self.assertEqual(result["call"]["additional_model_calls"], 2)
            self.assertEqual(result["dialogue"]["text"][7], ["S2", line])
            self.assertNotIn("09", json.loads(api.call_args.kwargs["brief"])["slots"])

    def test_bulletin_keeps_frozen_sentences_and_uses_separate_limits(self):
        from podcast_episode_policy import bulletin_dialogue
        from podcast_dialogue import validate_dialogue
        sentence = "工作人员按已公开的安排开放阅读空间，并在服务台说明读者可使用的区域。"
        topics = [{"source": "示例报", "facts": [prefix + sentence for prefix in
                   ("馆方说明。", "当地公告称，", "运营方表示，", "根据公告，", "负责人透露，")]}]
        # Distinct frozen sentences, enough to meet a genuine brief's floor.
        topics.extend({"source": "示例报", "facts": [
            prefix + sentence for prefix in beginnings]} for beginnings in [
            ("学校回应，", "机构介绍，", "新闻稿显示，", "该单位解释，", "官方材料称，"),
            ("馆长提到，", "编辑查到，", "记者注意到，", "团队确认，", "负责人补充，")])
        source = "\n".join(fact for topic in topics for fact in topic["facts"])
        script, review = bulletin_dialogue({"topics": topics}, source)
        self.assertGreaterEqual(review["spoken_chars"], 500)
        for _, line in script["text"]:
            if line.startswith(("欢迎", "以下摘自", "以上是")):
                continue
            self.assertIn(line, source)
        with self.assertRaises(DialogueError):
            validate_dialogue(script, source_text=source, enforce_length=True)
        with self.assertRaises(DialogueError):
            bulletin_dialogue({"topics": topics[:1]}, source)
