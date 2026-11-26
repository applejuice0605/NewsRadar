import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("podcast_dialogue", ROOT / "scripts" / "podcast_dialogue.py")
assert SPEC and SPEC.loader
dialogue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dialogue)
GEN_SPEC = importlib.util.spec_from_file_location(
    "generate_dialogue_podcast", ROOT / "scripts" / "generate_dialogue_podcast.py"
)
assert GEN_SPEC and GEN_SPEC.loader
generator = importlib.util.module_from_spec(GEN_SPEC)
GEN_SPEC.loader.exec_module(generator)


REPORT_DATE = date(2026, 10, 30)


def materials(community=False):
    rows = []
    for index in range(4):
        rows.append({
            "id": f"item-{index}", "title": f"标题 {index} 2030",
            "source": "测试源", "url": f"https://example.com/{index}",
            "published_at": REPORT_DATE.isoformat(),
            "source_genre": "community_post" if community and index == 3 else "source_report",
            "text": (f"来源正文 {index} 包含 2030 年的事件、背景和影响。" * 80),
        })
    return {"schema_version": 1, "date": REPORT_DATE.isoformat(), "items": rows}


def valid_dialogue(line=None):
    line = line or ("这是第{}轮，对新闻事实、范围和影响做自然解释，" +
                    "两位主持人继续推进上下文，避免重复并给听众足够信息。")
    turns = []
    for index in range(36):
        turns.append(["S1" if index % 3 else "S2", line.format(index) if "{}" in line else line])
    return {"hosts": dialogue.HOSTS, "text": turns,
            "turn_meta": {"0": {"kind": "overview"}, "12": {"kind": "question"},
                          "35": {"kind": "closing"}}}


class DialoguePureFunctionsTest(unittest.TestCase):
    def test_plain_transport_drops_only_unambiguously_empty_turn(self):
        rows = ['S1: 冻结事实。', 'S2: 补充说明。'] * 18
        rows.insert(8, 'S2:')
        script, _ = dialogue.parse_and_validate("\n".join(rows), output_format="plain-turns")
        self.assertEqual(len(script["text"]), 36)
        rows[8] = "无标签的台词"
        with self.assertRaisesRegex(dialogue.DialogueError, "tagged speaker"):
            dialogue.parse_and_validate("\n".join(rows), output_format="plain-turns")

    def test_duration_equivalence_requires_matching_unit(self):
        draft = valid_dialogue("该次测量为58秒。")
        dialogue.validate_dialogue(draft, source_text="测量结果为 58.0 秒")
        with self.assertRaisesRegex(dialogue.DialogueError, "numeric_tokens_not_in_source"):
            dialogue.validate_dialogue(draft, source_text="测量结果为 58.0 毫秒")

    def test_writer_counts_spaces_as_soulx_does(self):
        from soulx_synthesize import _speech_length, normalize_speaker_input
        line = "GPU 词元 <|breathing|> 与测试。"
        self.assertEqual(dialogue.speech_char_count(line),
                         _speech_length(normalize_speaker_input(line)))

    def test_recover_brackets_preserves_all_strings_in_order(self):
        raw = '{"text":[["S1","事实30元"],"S2","追问？"],"S1","回答","S2","再见。"]}'
        restored, changed = dialogue.normalize_turn_transport(raw)
        self.assertTrue(changed)
        self.assertEqual(json.loads(restored), {'text': [
            ['S1', '事实30元'], ['S2', '追问？'], ['S1', '回答'], ['S2', '再见。']]})
        self.assertEqual(dialogue.normalize_turn_transport(restored), (restored, False))

    def test_recover_stray_quote_only_at_new_turn(self):
        raw = '{"text":[["S1","事实"],"["S2","追问？"]]}'
        restored, changed = dialogue.normalize_turn_transport(raw)
        self.assertTrue(changed)
        self.assertEqual(json.loads(restored), {'text': [['S1', '事实'], ['S2', '追问？']]})

    def test_recover_colon_only_between_speaker_and_line(self):
        raw = '{"text":["S1":"事实：原文","S2","追问？"]}'
        restored, changed = dialogue.normalize_turn_transport(raw)
        self.assertTrue(changed)
        self.assertEqual(json.loads(restored), {'text': [['S1', '事实：原文'], ['S2', '追问？']]})
        with self.assertRaises(dialogue.DialogueError):
            dialogue.normalize_turn_transport('{"text":["S1","事实":"S2","追问？"]}')

    def test_transport_recovery_rejects_ambiguous_or_truncated_strings(self):
        for raw in ('{"text":["S1","unfinished]}',
                    '{"text":["S1","line" "S2","other"]}',
                    '{"text":[["S1"],"line"],"S2","other"]}',
                    '{"text":[["S3","line"],"S2","other"]}',
                    '{"text":[["S1","line"],"S2"]}',
                    '{"text":[["S1","line"],"S2",2]}'):
            with self.subTest(raw=raw), self.assertRaises(dialogue.DialogueError):
                restored, _ = dialogue.normalize_turn_transport(raw)
                dialogue.parse_turns_json(restored)

    def test_flat_turns_have_same_canonical_metadata_as_nested_turns(self):
        turns = [['S1' if index % 2 == 0 else 'S2', '来源事实说明。'] for index in range(36)]
        nested = dialogue.parse_turns_json(json.dumps({'text': turns}, ensure_ascii=False))
        flat = dialogue.parse_turns_json(json.dumps({'text': [item for turn in turns for item in turn]}, ensure_ascii=False))
        self.assertEqual(flat, nested)

    def test_generator_recovers_fresh_and_cached_raw_without_changing_cache(self):
        from unittest.mock import patch
        turns = [['S1' if index % 2 == 0 else 'S2', '这是对来源事实的自然解释，继续展开变化及影响。']
                 for index in range(36)]
        raw = json.dumps({'text': turns}, ensure_ascii=False).replace('], [', '], ', 1)
        calls = []
        class Client:
            def chat(self, *_args, **_kwargs):
                calls.append(1)
                return raw
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material_path = root/'materials.json'
            material_path.write_text(json.dumps(materials(), ensure_ascii=False))
            prompt = root/'prompt.txt'
            prompt.write_text('{{FACT_BRIEF}}')
            args = generator.parse_args(['--materials', str(material_path), '--date', REPORT_DATE.isoformat(),
                '--output-root', str(root/'drafts'), '--prompt', str(prompt), '--format', 'turns-json', '--draft-only'])
            with patch.object(generator, '_validate_existing_materials'), patch.object(
                    generator, '_load_ai_config', return_value={'MODEL':'test/model'}), patch.object(
                    generator, '_client_factory', return_value=Client()):
                first = generator.generate(args)
                raw_path = next((root/'drafts'/REPORT_DATE.isoformat()/'private/raw').glob('*.txt'))
                saved = raw_path.read_bytes()
                args.cache_only = True
                second = generator.generate(args)
            self.assertEqual(first['dialogue'], second['dialogue'])
            self.assertEqual(raw_path.read_bytes(), saved)
            self.assertEqual(saved, raw.encode())
            self.assertEqual(calls, [1])
            self.assertTrue(second['call']['array_brackets_recovered'])
            self.assertEqual(second['call']['additional_model_calls'], 0)

    def test_restore_only_explicit_final_signoff_section(self):
        raw = '[SECTION:overview]\n[01:S1] 开场\n[SECTION:discussion]\n[02:S2] 讨论\n[03:S1] 好，今天就聊到这里。我是知远，再见。'
        restored, changed = generator.normalize_final_signoff_section(raw)
        self.assertTrue(changed)
        self.assertEqual(restored.replace('[SECTION:closing]\n', ''), raw)
        self.assertEqual(generator.normalize_final_signoff_section(restored), (restored, False))
        unfinished = raw.replace('好，今天就聊到这里。我是知远，再见。', '讨论仍未结束。')
        self.assertEqual(generator.normalize_final_signoff_section(unfinished), (unfinished, False))

    def test_currency_zero_fraction_matches_spoken_integer(self):
        self.assertEqual(dialogue.numeric_tokens("¥30.00 ￥120.00 ¥1,149.00"),
                         {"30", "120", "1149"})
        self.assertEqual(dialogue.numeric_tokens("¥30.50 version 4.0"),
                         {"30.50", "4.0"})

    def test_material_brief_excludes_title_rank_and_url(self):
        brief, report = dialogue.build_fact_brief(materials(), REPORT_DATE)
        self.assertTrue(report["adequate"])
        self.assertNotIn("标题 0", brief)
        self.assertNotIn("example.com", brief)
        self.assertIn('"priority":1', brief)
        self.assertIn('"source":"测试源"', brief)

    def test_community_material_does_not_count_for_adequacy(self):
        report = dialogue.assess_materials(materials(community=True), REPORT_DATE)
        self.assertTrue(report["adequate"])
        self.assertEqual(report["report_count"], 3)
        report = dialogue.assess_materials({**materials(), "items": materials()["items"][:2]}, REPORT_DATE)
        self.assertFalse(report["adequate"])

    def test_legacy_material_genre_is_inferred_from_source_id_and_host(self):
        payload = materials()
        for item in payload["items"]:
            item.pop("source_genre", None)
        payload["items"][-1]["source_id"] = "community:legacy-post"
        payload["items"][-1]["url"] = "https://juejin.cn/post/legacy"
        report = dialogue.assess_materials(payload, REPORT_DATE)
        self.assertTrue(report["adequate"])
        self.assertEqual(report["report_count"], 3)
        self.assertEqual(report["community_excluded_count"], 1)

    def test_fixed_hosts_and_non_alternating_turns_are_valid(self):
        normalized, report = dialogue.validate_dialogue(
            valid_dialogue(), source_text="2030 " + " ".join(str(i) for i in range(36)), enforce_length=False
        )
        self.assertEqual(normalized["hosts"], dialogue.HOSTS)
        self.assertEqual(report["turn_count"], 36)

    def test_turn_meta_accepts_topic_map_without_forcing_turn_alternation(self):
        value = valid_dialogue()
        value["turn_meta"] = {"0": {"topic": "overview", "kind": "overview"},
                               "4": {"topic_id": "storage", "kind": "question"}}
        normalized, _ = dialogue.validate_dialogue(
            value, source_text="2030 " + " ".join(str(i) for i in range(36))
        )
        self.assertEqual(normalized["turn_meta"]["0"]["topic"], "overview")
        self.assertEqual(normalized["turn_meta"]["4"]["topic_id"], "storage")

    def test_approved_voice_control_survives_normalization(self):
        value = valid_dialogue()
        value["text"][0][1] += " <|breathing|>"
        normalized, report = dialogue.validate_dialogue(
            value, source_text="2030 " + " ".join(str(i) for i in range(36))
        )
        self.assertIn("<|breathing|>", normalized["text"][0][1])
        self.assertEqual(report["voice_tag_count"], 1)

    def test_unknown_control_tag_is_rejected(self):
        value = valid_dialogue()
        value["text"][0][1] += " <|applause|>"
        with self.assertRaises(dialogue.DialogueError):
            dialogue.validate_dialogue(value, source_text="2030")
        value = valid_dialogue()
        value["text"][0][1] += " <|laugh-foo|>"
        with self.assertRaises(dialogue.DialogueError):
            dialogue.validate_dialogue(value, source_text="2030")

    def test_too_many_controls_and_metadata_are_rejected(self):
        value = valid_dialogue()
        value["text"][0][1] += " <|laughter|> <|sigh|> <|breathing|> <|coughing|>"
        with self.assertRaises(dialogue.DialogueError):
            dialogue.validate_dialogue(value, source_text="2030")
        value = valid_dialogue()
        value["text"][0][1] = "来源：测试源 https://example.com/a"
        with self.assertRaises(dialogue.DialogueError):
            dialogue.validate_dialogue(value, source_text="2030")

    def test_length_is_warning_for_draft_but_error_for_audio(self):
        value = valid_dialogue("短句{}")
        _, report = dialogue.validate_dialogue(
            value, source_text="2030 " + " ".join(str(i) for i in range(36)), enforce_length=False
        )
        self.assertFalse(report["length_ok"])
        with self.assertRaises(dialogue.DialogueError):
            dialogue.validate_dialogue(
                value, source_text="2030 " + " ".join(str(i) for i in range(36)), enforce_length=True
            )

    def test_new_numeric_token_is_rejected_and_human_review_is_explicit(self):
        value = valid_dialogue()
        value["text"][0][1] += " 9999"
        with self.assertRaises(dialogue.DialogueError):
            dialogue.validate_dialogue(value, source_text="2030")
        self.assertTrue(dialogue.numeric_tokens("中文数字二〇三〇无法完全机器核对") == set())

    def test_unpadded_day_is_allowed_only_in_supported_spoken_date(self):
        source = "2026-11-07"
        dialogue.validate_dialogue(valid_dialogue("11月7日起开放。"), source_text=source, enforce_length=False)
        for text in ("共有7款配置。", "11月8日起开放。"):
            with self.subTest(text=text), self.assertRaisesRegex(dialogue.DialogueError, "numeric_tokens_not_in_source"):
                dialogue.validate_dialogue(valid_dialogue(text), source_text=source, enforce_length=False)

    def test_previous_day_conversion_requires_relative_source_phrase(self):
        value = valid_dialogue("12月31日晚发布。")
        dialogue.validate_dialogue(value, source_text="2027-01-01\n昨晚发布。", enforce_length=False)
        with self.assertRaisesRegex(dialogue.DialogueError, "numeric_tokens_not_in_source"):
            dialogue.validate_dialogue(value, source_text="2027-01-01\n刚刚发布。", enforce_length=False)
        with self.assertRaisesRegex(dialogue.DialogueError, "numeric_tokens_not_in_source"):
            dialogue.validate_dialogue(valid_dialogue("共有31项。"), source_text="2027-01-01\n昨晚发布。", enforce_length=False)

    def test_iso_publication_date_parts_can_be_spoken_without_numeric_false_positive(self):
        value = valid_dialogue("节目日期是2026年10月30日，接着解释事实、背景和限制。")
        _, report = dialogue.validate_dialogue(value, source_text="published_at=2026-10-30")
        self.assertEqual(report["introduced_numeric_tokens"], [])

    def test_one_call_cache_keeps_invalid_raw_without_retry(self):
        calls = []

        class Client:
            def validate_config(self):
                return True, ""

            def chat(self, *_args, **_kwargs):
                calls.append(1)
                return "not json"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw, first = dialogue.one_call_draft(
                brief='{"date":"2026-10-30"}', prompt="{{FACT_BRIEF}}",
                cache_root=root, ai_config={"MODEL": "test/model"},
                client_factory=lambda _: Client(),
            )
            self.assertEqual(raw, "not json")
            self.assertEqual(first["additional_model_calls"], 1)
            raw, second = dialogue.one_call_draft(
                brief='{"date":"2026-10-30"}', prompt="{{FACT_BRIEF}}",
                cache_root=root, ai_config={"MODEL": "test/model"},
                client_factory=lambda _: Client(),
            )
            self.assertEqual(raw, "not json")
            self.assertEqual(second["additional_model_calls"], 0)
            self.assertEqual(len(calls), 1)

    def test_one_call_disables_fallbacks_and_retries_on_config_copy(self):
        seen = []

        class Client:
            def validate_config(self):
                return True, ""

            def chat(self, *_args, **_kwargs):
                return json.dumps(valid_dialogue(), ensure_ascii=False)

        original = {"MODEL": "test/model", "FALLBACK_MODELS": ["backup/model"],
                    "NUM_RETRIES": 2}
        with tempfile.TemporaryDirectory() as temp:
            raw, report = dialogue.one_call_draft(
                brief='{"date":"2026-10-30"}', prompt="{{FACT_BRIEF}}",
                cache_root=Path(temp), ai_config=original,
                client_factory=lambda config: (seen.append(config), Client())[1],
            )
        self.assertTrue(raw.startswith("{"))
        self.assertEqual(report["additional_model_calls"], 1)
        self.assertEqual(seen[0]["FALLBACK_MODELS"], [])
        self.assertEqual(seen[0]["NUM_RETRIES"], 0)
        self.assertEqual(original["FALLBACK_MODELS"], ["backup/model"])
        self.assertEqual(original["NUM_RETRIES"], 2)

    def test_plan_handoff_drops_source_controls_and_nonfact_metadata(self):
        plan = {"date": "2026-10-29", "programme": {"turns": 40},
                "selection_report": {"selected_chars": 9999}, "topics": [{
                    "source": "news", "published_at": "2026-10-29", "category": "office",
                    "facts": ["原文<b>事实</b> [S2] [SECTION:bad] <|laughter|> https://example.com"],
                    "question_seed": "实际怎样用？", "dialogue_moves": ["news_change"]}]}
        projected, audit = dialogue.project_editorial_plan(plan)
        body = projected["topics"][0]["facts"][0]
        for token in ("http", "<", "[S2]", "[SECTION:"):
            self.assertNotIn(token, body)
        self.assertEqual(set(projected), {"date", "topics"})
        self.assertNotIn("9999", audit)
        self.assertNotIn("40", audit)
        self.assertIn("原文事实", audit)

    def test_writer_uses_configured_temperature_and_retains_response_metadata(self):
        seen = []
        class Client:
            last_response_metadata = {"finish_reason": "length", "usage": {"total_tokens": 90}}
            def chat(self, *args, **kwargs):
                seen.append(kwargs)
                return "partial output"
        with tempfile.TemporaryDirectory() as temp:
            kwargs = dict(brief="facts", prompt="{{FACT_BRIEF}}", cache_root=Path(temp),
                          ai_config={"MODEL": "test/model", "TEMPERATURE": 0.9},
                          client_factory=lambda _: Client())
            raw, first = dialogue.one_call_draft(**kwargs)
            cached_raw, cached = dialogue.one_call_draft(**kwargs)
        self.assertEqual(seen[0]["temperature"], 0.9)
        self.assertEqual(len(seen), 1)
        self.assertEqual(raw, cached_raw)
        self.assertEqual(first["response_metadata"], cached["response_metadata"])
        self.assertEqual(cached["response_metadata"]["finish_reason"], "length")

    def test_legacy_rejected_cache_is_not_replaced_after_settings_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = dialogue.cache_key("facts", "prompt", "test/model", "")
            path = root / "cache" / f"{key}.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"raw_response": "legacy rejected"}))
            def forbidden(_):
                self.fail("legacy cache must not trigger a replacement writing request")
            raw, report = dialogue.one_call_draft(brief="facts", prompt="prompt", cache_root=root,
                ai_config={"MODEL": "test/model", "TEMPERATURE": 1.0}, client_factory=forbidden)
        self.assertEqual(raw, "legacy rejected")
        self.assertEqual(report["additional_model_calls"], 0)
        self.assertIsNone(report["request_settings"])

    def test_timed_out_or_changed_request_cannot_buy_a_second_call_in_one_run(self):
        calls = []
        class TimeoutClient:
            def chat(self, *args, **kwargs):
                calls.append(1)
                raise TimeoutError("provider timeout")
        with tempfile.TemporaryDirectory() as temp:
            kwargs = dict(brief="facts", prompt="{{FACT_BRIEF}}", cache_root=Path(temp),
                          ai_config={"MODEL": "test/model"}, client_factory=lambda _: TimeoutClient())
            with self.assertRaises(TimeoutError):
                dialogue.one_call_draft(**kwargs)
            kwargs["prompt"] = "changed prompt {{FACT_BRIEF}}"
            with self.assertRaises(dialogue.DialogueError):
                dialogue.one_call_draft(**kwargs)
        self.assertEqual(calls, [1])

    def test_force_cannot_overwrite_existing_raw_cache(self):
        calls = []

        class Client:
            def validate_config(self):
                return True, ""

            def chat(self, *_args, **_kwargs):
                calls.append(1)
                return "replacement must never be called"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_raw = "saved invalid response"
            dialogue.one_call_draft(
                brief='{"date":"2026-10-30"}', prompt="{{FACT_BRIEF}}",
                cache_root=root, ai_config={"MODEL": "test/model"},
                client_factory=lambda _: Client(),
            )
            # Replace the generated cache with a deliberately invalid but
            # validly persisted raw response, then invoke legacy force=True.
            cache_file = next((root / "cache").glob("*.json"))
            record = json.loads(cache_file.read_text(encoding="utf-8"))
            record["raw_response"] = first_raw
            cache_file.write_text(json.dumps(record), encoding="utf-8")
            raw_file = next((root / "raw").glob("*.txt"))
            raw_file.write_text(first_raw, encoding="utf-8")
            before = raw_file.read_bytes()
            raw, report = dialogue.one_call_draft(
                brief='{"date":"2026-10-30"}', prompt="{{FACT_BRIEF}}",
                cache_root=root, ai_config={"MODEL": "test/model"},
                client_factory=lambda _: Client(), force=True,
            )
            self.assertEqual(raw, first_raw)
            self.assertTrue(report["force_ignored"])
            self.assertEqual(raw_file.read_bytes(), before)
            self.assertEqual(calls, [1])

    def test_changed_prompt_replays_attempt_cache_without_a_second_call(self):
        calls = []
        class Client:
            def chat(self, *_args, **_kwargs):
                calls.append(1)
                return "replacement"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kwargs = dict(brief="facts", prompt="old {{FACT_BRIEF}}", cache_root=root,
                          ai_config={"MODEL": "test/model"}, client_factory=lambda _: Client())
            dialogue.one_call_draft(**kwargs)
            kwargs["prompt"] = "new {{FACT_BRIEF}}"
            raw, report = dialogue.one_call_draft(**kwargs)
        self.assertEqual(raw, "replacement")
        self.assertTrue(report["cache_hit"])
        self.assertEqual(report["additional_model_calls"], 0)
        self.assertEqual(calls, [1])

    def test_cache_only_missing_fails_before_client_or_attempt(self):
        created = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(dialogue.DialogueError, "cache-only"):
                dialogue.one_call_draft(
                    brief="facts", prompt="prompt", cache_root=root,
                    ai_config={"MODEL": "test/model"},
                    client_factory=lambda _: created.append(1), cache_only=True)
            self.assertEqual(created, [])
            self.assertFalse((root / "writer-attempt.json").exists())

    def test_native_speaker_lines_round_trip(self):
        value = valid_dialogue()
        lines = ["[SECTION:overview]"]
        for index, (speaker, body) in enumerate(value["text"]):
            if index == 2:
                lines.append("[SECTION:workflows]")
            if index == len(value["text"]) - 2:
                lines.append("[SECTION:closing]")
            lines.append(f"[{speaker}]{body}")
        canonical, report = dialogue.parse_and_validate(
            "\n".join(lines), source_text=" ".join(x[1] for x in value["text"]),
            output_format="speaker-lines",
        )
        self.assertEqual(canonical["text"], value["text"])
        self.assertEqual(report["writer_output_format"], "speaker-lines")
        self.assertEqual(canonical["turn_meta"]["2"]["kind"], "topic_transition")

    def test_turns_json_injects_hosts_and_program_sections(self):
        turns = [["S1" if index % 2 == 0 else "S2",
                  f"这是第{index}轮，解释来源事实并继续推进讨论。"] for index in range(30)]
        canonical, report = dialogue.parse_and_validate(
            json.dumps({"text": turns}, ensure_ascii=False),
            source_text=" ".join(str(index) for index in range(30)),
            output_format="turns-json")
        self.assertEqual(canonical["hosts"], dialogue.HOSTS)
        self.assertEqual(report["writer_output_format"], "turns-json")
        self.assertEqual(canonical["turn_meta"]["0"]["topic"], "overview")
        self.assertEqual(canonical["turn_meta"]["3"]["topic"], "overview")
        self.assertEqual(canonical["turn_meta"]["4"]["topic"], "discussion")
        self.assertEqual(canonical["turn_meta"]["28"]["topic"], "closing")
        self.assertEqual(canonical["turn_meta"]["29"]["kind"], "closing")

    def test_turns_json_rejects_writer_controls_and_bad_turns(self):
        turns = [["S1", "这是一段足够长的事实解释。"] for _ in range(30)]
        for value in (
            {"text": turns, "hosts": dialogue.HOSTS},
            {"text": [["S3", "错误主持人"]] + turns[1:]},
            {"text": [[{"bad": "speaker"}, "不可接受"]] + turns[1:]},
            {"text": turns[:29]},
        ):
            with self.subTest(value=value):
                with self.assertRaises(dialogue.DialogueError):
                    dialogue.parse_and_validate(json.dumps(value, ensure_ascii=False),
                                                output_format="turns-json")

    def test_turns_json_fresh_and_cached_normalization_have_same_canonical_script(self):
        body = "这是一个需要安全换气的长句，" * 10 + "最后结束。"
        raw_value = {"text": [["S1" if index % 2 == 0 else "S2", body]
                              for index in range(30)]}
        raw = json.dumps(raw_value, ensure_ascii=False)
        calls = []

        class Client:
            def chat(self, *_args, **_kwargs):
                calls.append(1)
                return raw

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kwargs = dict(brief="facts", prompt="{{FACT_BRIEF}}", cache_root=root,
                          ai_config={"MODEL": "test/model"}, client_factory=lambda _: Client(),
                          output_format="turns-json")
            fresh, first = dialogue.one_call_draft(**kwargs)
            cached, second = dialogue.one_call_draft(**kwargs)
        self.assertEqual(fresh, cached)
        self.assertEqual(first["additional_model_calls"], 1)
        self.assertEqual(second["additional_model_calls"], 0)
        self.assertEqual(calls, [1])
        fresh_normalized, _ = generator.normalize_turns_json_sentences(fresh)
        cached_normalized, _ = generator.normalize_turns_json_sentences(cached)
        self.assertEqual(fresh_normalized, cached_normalized)
        fresh_script, _ = dialogue.parse_and_validate(
            fresh_normalized, output_format="turns-json")
        cached_script, _ = dialogue.parse_and_validate(
            cached_normalized, output_format="turns-json")
        self.assertEqual(fresh_script, cached_script)

    def test_turns_json_keeps_numeric_gate(self):
        turns = [["S1", "事实解释，包含没有来源的数字9999。"] for _ in range(30)]
        with self.assertRaisesRegex(dialogue.DialogueError, "numeric_tokens_not_in_source"):
            dialogue.parse_and_validate(json.dumps({"text": turns}, ensure_ascii=False),
                                        source_text="2030", output_format="turns-json")

    def test_projected_brief_cannot_add_claims_outside_selected_facts(self):
        fact = "模型支持文档检索。"
        plan = {"date": "2026-10-29", "topics": [{"facts": [fact], "brief": {
            "news_change": "模型保证永不出错。", "mechanism_facts": [fact, "虚构新功能"],
            "declared_limits": ["虚构限制"]}}]}
        projected, audit = dialogue.project_editorial_plan(plan)
        self.assertEqual(projected["topics"][0]["brief"], {"news_change": fact, "mechanism_facts": [fact], "declared_limits": []})
        self.assertNotIn("虚构", audit)

    def test_numbered_speaker_turns_are_checked_and_not_spoken(self):
        raw = "[SECTION:overview]\n[01:S1]第一句。\n[SECTION:closing]\n[02:S2]结束。"
        parsed = dialogue.parse_speaker_lines(raw)
        self.assertEqual(parsed["text"], [["S1", "第一句。"], ["S2", "结束。"]])
        for bad in (raw.replace("02:S2", "03:S2"), raw.replace("02:S2", "S2"),
                    raw.replace("第一句。", "第一句。[02:S2]正文不能带标记")):
            with self.assertRaises(dialogue.DialogueError):
                dialogue.parse_speaker_lines(bad)

    def test_repeated_active_discussion_label_preserves_every_spoken_word(self):
        raw = "[SECTION:overview]\n[S1]开头。\n[SECTION:discussion]\n[S2]第一个话题。\n[SECTION:discussion]\n[S1]接着说。\n[SECTION:closing]\n[S2]结束。"
        parsed = dialogue.parse_speaker_lines(raw)
        self.assertEqual(parsed["text"], [["S1", "开头。"], ["S2", "第一个话题。"], ["S1", "接着说。"], ["S2", "结束。"]])
        reopened = raw.replace("[S1]接着说。", "[SECTION:other]\n[S1]另一个话题。\n[SECTION:discussion]\n[S2]重开旧话题。")
        with self.assertRaises(dialogue.DialogueError):
            dialogue.parse_speaker_lines(reopened)

    def test_two_hosts_cannot_be_spoken_by_one_voice(self):
        value = valid_dialogue()
        value["text"][0][1] = "知远：它能接上下文。晓宁：先别急着发工牌。"
        with self.assertRaisesRegex(dialogue.DialogueError, "host label"):
            dialogue.validate_dialogue(value)

    def test_speaker_lines_fail_closed(self):
        for raw in (
            "[S1]没有section",
            "[SECTION:overview]\n[S1]正文 [S2]误入标记",
            "[SECTION:overview]\n[S1]正文 [SECTION:closing]误入标记",
            "[SECTION:overview]\n[S1]正文\n解释说明不该进入节目",
            "[SECTION:overview]\n[S3]未知主持人",
            "[SECTION:overview]\n[SECTION:closing]\n[S1]空段",
            "[SECTION:overview]\n[S1]正文\n[SECTION:overview]\n[S2]重复",
        ):
            with self.assertRaises(dialogue.DialogueError):
                dialogue.parse_speaker_lines(raw)

    def test_english_benchmark_does_not_inflate_sentence_load(self):
        value = valid_dialogue()
        value["text"][0][1] = "官方称，模型在Artificial Analysis Coding Agent Index中的表现值得继续观察，具体还要结合实际任务测试"
        _, report = dialogue.validate_dialogue(value, source_text=" ".join(x[1] for x in value["text"]))
        self.assertFalse(any(x.get("turn") == 0 and x["kind"] == "long_sentence" for x in report["warnings"]))

    def test_production_output_trees_are_rejected(self):
        for path in (
            Path("/Users/bruce/Coding/NewsRadar/TrendRadar/output"),
            Path("/Users/bruce/Coding/NewsRadar/TrendRadar/output/html/child"),
            Path("/Users/bruce/Coding/NewsRadar-production/TrendRadar/output/audio"),
        ):
            with self.assertRaises(generator.DialogueError):
                generator.ensure_private_output_root(path)


if __name__ == "__main__":
    unittest.main()
