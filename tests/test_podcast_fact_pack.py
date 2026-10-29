"""Offline podcast provenance, duration and publication regressions."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "TrendRadar"))
SPEC = importlib.util.spec_from_file_location("podcast_fact_pack_under_test", ROOT / "scripts/generate_daily_podcast.py")
assert SPEC and SPEC.loader
podcast = importlib.util.module_from_spec(SPEC)
# LiteLLM otherwise attempts to refresh its price map at import time.
with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
    SPEC.loader.exec_module(podcast)


def fact_pack():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    source = {"title": "模型团队发布推理工具新版本", "url": "https://example.test/release",
              "source": "团队公告", "summary": "公告称新版本增加批处理接口，目前仍需独立复测。"}
    return {"schema_version": 1, "date": today, "generated_at": today + "T08:00:00+08:00",
            "articles": [{"id": "article-1", **source, "assessment": "不应自动播报的评分",
                          "primary_materials": [{"url": source["url"]}], "progress": "未经证实的进展"}],
            "claims": [{"section": "core_trends", "fact": source["title"], "source_ids": ["N001"],
                        "interpretation": "可能便于批量调用，需实际测试。", "uncertainty": "仅依据公告，未独立复测。"}],
            "evidence_sources": {"N001": source}}


class FactPackValidationTest(unittest.TestCase):
    def validate(self, pack):
        return podcast.validate_fact_pack(pack, datetime.fromisoformat(fact_pack()["date"]).date())

    def test_embedded_raw_json_and_schema(self):
        pack = fact_pack()
        html = '<script type="application/json" id="newsradar-fact-pack">' + json.dumps(pack) + '</script>'
        self.assertEqual(self.validate(podcast.embedded_fact_pack(html)), pack)
        self.assertIsNone(podcast.embedded_fact_pack('<div>legacy</div>'))
        for text in ("null", "[]", "{bad json"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                podcast.embedded_fact_pack(html[:html.index('>') + 1] + text + '</script>')
        for broken in (html + html, html.replace('application/json', 'text/javascript'), html.rsplit('</script>', 1)[0]):
            with self.subTest(html=broken), self.assertRaises(ValueError):
                podcast.embedded_fact_pack(broken)

    def test_invalid_schema_date_sources_and_fact_quote_fail_closed(self):
        for field, value in (("schema_version", 2), ("schema_version", True), ("date", "2000-01-01"),
                             ("generated_at", "not-ISO"), ("generated_at", "2026-10-29T00:00:00"),
                             ("articles", {}), ("claims", {}), ("evidence_sources", [])):
            pack = fact_pack()
            pack[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.validate(pack)
        for change in ({"source_ids": ["N999"]}, {"source_ids": []}, {"source_ids": "N001"},
                       {"source_ids": ["N001", "N999"]}, {"fact": "虚构的性能提升90%"},
                       {"section": []}, {"interpretation": []}, {"uncertainty": None},
                       {"interpretation": "参考 N999"}, {"interpretation": "依据N999推测"}):
            pack = fact_pack()
            pack["claims"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(pack)
        pack = fact_pack()
        pack["evidence_sources"]["N002"] = {**pack["evidence_sources"]["N001"], "title": "无关新闻", "summary": ""}
        pack["claims"][0]["source_ids"].append("N002")
        with self.assertRaisesRegex(ValueError, "not a quote"):
            self.validate(pack)

    def test_safe_urls_cover_articles_sources_and_primary_materials(self):
        urls = ['javascript:alert(1)', '//evil.test/a', 'file:///etc/passwd', 'https://u:p@example.test/a',
                'https://[', 'https://example.test:bad/a', 'https://example.test/a\nb',
                'https://example.test/%0a', 'https://example.test\\@evil.test', 'https://<bad>/']
        for url in urls:
            self.assertFalse(podcast.safe_url(url), url)
            for location in ('article', 'source', 'material', 'material_string'):
                pack = fact_pack()
                if location == 'article':
                    pack['articles'][0]['url'] = url
                elif location == 'source':
                    pack['evidence_sources']['N001']['url'] = url
                else:
                    pack['articles'][0]['primary_materials'] = [url if location == 'material_string' else {'url': url}]
                with self.subTest(url=url, location=location), self.assertRaises(ValueError):
                    self.validate(pack)

    def test_article_only_bounds_and_no_analysis(self):
        for count in range(7):
            pack = fact_pack()
            pack['claims'] = []
            pack['articles'] = [{**pack['articles'][0], 'id': str(i)} for i in range(count)]
            if 3 <= count <= 5:
                script, kind, reason = podcast.script_from_fact_pack(self.validate(pack))
                self.assertEqual(kind, 'fact_pack_articles')
                self.assertIn('缺少通过来源校验的分析', reason)
                self.assertIn('来源摘要', script)
                self.assertNotIn('未经证实的进展', script)
                self.assertNotIn('评分', script)
                self.assertNotIn('条件性分析', script)
            else:
                with self.assertRaises(ValueError):
                    self.validate(pack)

    def test_sparse_claim_without_articles_is_valid_and_conditional(self):
        pack = fact_pack()
        pack['articles'] = []
        pack['claims'][0]['interpretation'] = ''
        script, kind, _ = podcast.script_from_fact_pack(self.validate(pack))
        self.assertEqual(kind, 'fact_pack_claims')
        self.assertLess(len(script), 120)
        self.assertGreater(len(script), 40)
        self.assertIn(pack['claims'][0]['fact'], script)
        self.assertNotIn('条件性分析', script)
        pack['claims'][0]['interpretation'] = '可能便于批量调用。'
        script, _, _ = podcast.script_from_fact_pack(self.validate(pack))
        self.assertIn('条件性分析，尚待验证', script)
        self.assertIn('未独立复测', script)

    def test_article_catalog_accepts_stable_ids_from_parent_schema(self):
        from trendradar.ai.fact_pack import make_fact_pack
        day = fact_pack()['date']
        articles = [{**fact_pack()['articles'][0], 'id': f'fact_{i}', 'date': day} for i in range(3)]
        pack = make_fact_pack(articles, day)
        self.validate(pack)
        script, kind, _ = podcast.script_from_fact_pack(pack)
        self.assertEqual(kind, 'fact_pack_articles')
        self.assertIn(articles[0]['title'], script)

    def test_spoken_text_omits_links_ids_and_markup(self):
        text = '[公告](https://example.test/x) <b>已发布</b> https://example.test/a。依据[N001]与N002报道'
        spoken = podcast.spoken_text(text)
        self.assertIn('公告', spoken)
        for token in ('http', 'N001', 'N002', '<b>', ']('):
            self.assertNotIn(token, spoken)

    def test_measured_chapters_use_duration_ratios_and_factual_titles(self):
        script = '据来源甲报道：模型发布新版本。' + '\n\n' + '据来源乙报道：接口增加批处理。'
        chapters = podcast.build_chapters(script, 73.5)
        self.assertEqual(chapters[0]['title'], '模型发布新版本')
        self.assertEqual(chapters[1]['title'], '接口增加批处理')
        self.assertEqual(chapters[0]['start_seconds'], 0)
        for chapter in chapters:
            self.assertAlmostEqual(chapter['start_seconds'], chapter['start_ratio'] * 73.5, places=3)
            self.assertTrue(chapter['timing_approximate'])
            self.assertLess(chapter['start_seconds'], 73.5)


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pack = fact_pack()
        self.pack_file = self.root / 'pack.json'
        self.pack_file.write_text(json.dumps(self.pack), encoding='utf-8')
        self.prompt = self.root / 'podcast_prompt.txt'
        self.prompt.write_text('prompt {content}', encoding='utf-8')
        self.args = argparse.Namespace(force=False, fact_pack=self.pack_file, input=self.root / 'missing.html',
                                       date=self.pack['date'], output_root=self.root / 'preview')
        self.config = {'enabled': True, 'min_script_chars': 1700, 'retention_days': 90}

    def run_main(self, duration=28.25, tts=None, writer=None, ai=None):
        async def fake_tts(text, output, voice, rate, volume):
            self.assertIn('.podcast-', str(output))
            output.write_bytes(b'fake-mp3')
        with ExitStack() as stack:
            stack.enter_context(patch.object(podcast, 'parse_args', return_value=self.args))
            stack.enter_context(patch.object(podcast, 'load_podcast_config', return_value=(self.config, {})))
            stack.enter_context(patch.object(podcast, 'CONFIG_PATH', self.root / 'config.yaml'))
            synth = stack.enter_context(patch.object(podcast, 'synthesize', side_effect=tts or fake_tts))
            stack.enter_context(patch.object(podcast, 'measure_duration', side_effect=duration if isinstance(duration, Exception) else None,
                                             return_value=duration))
            client = stack.enter_context(patch.object(podcast, 'AIClient', ai or Mock(side_effect=AssertionError('no AI for packs'))))
            expand = stack.enter_context(patch.object(podcast, 'expand_short_script', side_effect=AssertionError('no expansion')))
            if writer:
                stack.enter_context(patch.object(podcast, 'write_json_atomic', side_effect=writer))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            code = podcast.main()
        expand.assert_not_called()
        return code, synth, client

    def latest(self):
        return json.loads((self.args.output_root / 'latest.json').read_text())

    def asset(self, url):
        return self.args.output_root / url.removeprefix('/audio/')

    def test_direct_pack_short_script_and_immutable_bundle_metadata(self):
        self.pack['claims'][0]['interpretation'] = ''
        self.pack_file.write_text(json.dumps(self.pack))
        code, synth, client = self.run_main()
        self.assertEqual(code, 0)
        client.assert_not_called()
        synth.assert_called_once()
        metadata = self.latest()
        self.assertEqual(metadata['schema_version'], 3)
        self.assertEqual(metadata['source_kind'], 'fact_pack_claims')
        self.assertTrue(metadata['claim_grounded'])
        self.assertEqual(metadata['duration_seconds'], 28.25)
        self.assertEqual(metadata['voice'], 'zh-CN-YunyangNeural')
        self.assertEqual(len(metadata['fact_pack_hash']), 64)
        self.assertLess(metadata['script_chars'], 120)
        self.assertEqual(self.asset(metadata['audio_url']).read_bytes(), b'fake-mp3')
        transcript = self.asset(metadata['transcript_url'])
        self.assertIn(self.pack['claims'][0]['fact'], transcript.read_text())
        self.assertEqual(json.loads((transcript.parent / 'daily.json').read_text()), metadata)
        self.assertEqual(json.loads((self.args.output_root / self.pack['date'] / 'daily.json').read_text()), metadata)
        self.assertFalse(list(self.args.output_root.glob('.podcast-*')))

    def test_exactly_600_seconds_passes(self):
        self.assertEqual(self.run_main(duration=600)[0], 0)
        self.assertEqual(self.latest()['duration_seconds'], 600)

    def test_bad_duration_keeps_all_previous_files_and_skips_cleanup(self):
        self.assertEqual(self.run_main()[0], 0)
        before = self.latest()
        old_audio = self.asset(before['audio_url']).read_bytes()
        old_transcript = self.asset(before['transcript_url']).read_bytes()
        expired = self.args.output_root / '2000-01-01'
        expired.mkdir()
        (expired / 'daily.mp3').write_bytes(b'old')
        self.args.force = True
        for duration in (600.01, float('nan'), float('inf'), 0, -1, ValueError('probe unavailable')):
            with self.subTest(duration=duration):
                self.assertEqual(self.run_main(duration=duration)[0], 1)
                self.assertEqual(self.latest(), before)
                self.assertEqual(self.asset(before['audio_url']).read_bytes(), old_audio)
                self.assertEqual(self.asset(before['transcript_url']).read_bytes(), old_transcript)
                self.assertTrue(expired.exists())
                self.assertFalse(list(self.args.output_root.glob('.podcast-*')))

    def test_tts_partial_failure_cannot_replace_old_episode(self):
        self.run_main()
        before = self.latest()
        self.args.force = True
        async def fail(_text, output, *_args):
            output.write_bytes(b'partial')
            raise RuntimeError('TTS failed')
        self.assertEqual(self.run_main(tts=fail)[0], 1)
        self.assertEqual(self.latest(), before)
        self.assertFalse(list(self.args.output_root.glob('.podcast-*')))

    def test_commit_failure_rolls_back_date_index_and_keeps_latest(self):
        self.run_main()
        before = self.latest()
        self.args.force = True
        write = podcast.write_json_atomic
        def fail_latest(path, payload):
            if path == self.args.output_root.resolve() / 'latest.json':
                raise OSError('injected commit failure')
            return write(path, payload)
        self.assertEqual(self.run_main(writer=fail_latest)[0], 1)
        self.assertEqual(self.latest(), before)
        self.assertEqual(json.loads((self.args.output_root / self.pack['date'] / 'daily.json').read_text()), before)
        self.assertTrue(self.asset(before['audio_url']).is_file())

    def test_cache_includes_prompt_pack_and_voice_settings(self):
        self.run_main()
        first = self.latest()
        code, synth, _ = self.run_main()
        self.assertEqual(code, 0)
        synth.assert_not_called()
        self.prompt.write_text('revised {content}')
        self.assertEqual(self.run_main()[1].call_count, 1)
        self.assertNotEqual(self.latest()['cache_hash'], first['cache_hash'])
        for key, value in (('voice', 'zh-CN-XiaoxiaoNeural'), ('rate', '+10%'), ('volume', '-5%')):
            self.config[key] = value
            with self.subTest(key=key):
                self.assertEqual(self.run_main()[1].call_count, 1)
        self.pack['claims'][0]['uncertainty'] = '更新的不确定性说明'
        self.pack_file.write_text(json.dumps(self.pack))
        self.assertEqual(self.run_main()[1].call_count, 1)
        self.assertNotEqual(self.latest()['fact_pack_hash'], first['fact_pack_hash'])
        self.assertTrue(self.asset(first['audio_url']).is_file())

    def test_timestamp_only_pack_rebuild_keeps_hashes_url_and_cached_audio(self):
        for article_only in (False, True):
            with self.subTest(article_only=article_only):
                self.pack = fact_pack()
                if article_only:
                    self.pack['claims'] = []
                    self.pack['articles'] = [{**self.pack['articles'][0], 'id': str(i)} for i in range(3)]
                self.pack_file.write_text(json.dumps(self.pack))
                self.assertEqual(self.run_main()[0], 0)
                before = self.latest()
                index_bytes = (self.args.output_root / 'latest.json').read_bytes()
                self.pack['generated_at'] = self.pack['date'] + 'T09:00:00+08:00'
                self.pack_file.write_text(json.dumps(self.pack))
                code, synth, _ = self.run_main()
                self.assertEqual(code, 0)
                synth.assert_not_called()
                self.assertEqual((self.args.output_root / 'latest.json').read_bytes(), index_bytes)
                self.assertEqual(self.latest()['audio_url'], before['audio_url'])
                # Forcing generation proves source/cache hashes exclude the
                # timestamp while the independent input hash still records it.
                self.args.force = True
                self.assertEqual(self.run_main()[0], 0)
                after = self.latest()
                self.assertEqual(after['source_hash'], before['source_hash'])
                self.assertEqual(after['cache_hash'], before['cache_hash'])
                self.assertNotEqual(after['fact_pack_hash'], before['fact_pack_hash'])
                self.args.force = False

    def test_unspoken_enrichment_and_unused_records_do_not_invalidate_cache(self):
        self.assertEqual(self.run_main()[0], 0)
        before = self.latest()
        self.pack['fetched_at'] = self.pack['date'] + 'T09:00:00+08:00'
        self.pack['articles'][0]['primary_status'] = 'cached'
        self.pack['articles'][0]['assessment'] = 'updated but not narrated'
        self.pack['evidence_sources']['N001']['fetched_at'] = self.pack['fetched_at']
        self.pack['evidence_sources']['N001']['summary'] += '来源中未被 claim 引用的新说明。'
        self.pack['evidence_sources']['N002'] = {**self.pack['evidence_sources']['N001'], 'source': '未引用来源'}
        self.pack_file.write_text(json.dumps(self.pack))
        code, synth, _ = self.run_main()
        self.assertEqual(code, 0)
        synth.assert_not_called()
        self.assertEqual(self.latest(), before)
        self.pack['evidence_sources']['N001']['source'] = '修订的播报来源名称'
        self.pack_file.write_text(json.dumps(self.pack))
        self.assertEqual(self.run_main()[1].call_count, 1)
        self.assertNotEqual(self.latest()['source_hash'], before['source_hash'])

    def test_article_summary_change_invalidates_spoken_material_hash(self):
        self.pack['claims'] = []
        self.pack['articles'] = [{**self.pack['articles'][0], 'id': str(i)} for i in range(3)]
        self.pack_file.write_text(json.dumps(self.pack))
        self.assertEqual(self.run_main()[0], 0)
        before = self.latest()
        self.pack['articles'][0]['summary'] += '来源补充了接口仍在测试阶段。'
        self.pack_file.write_text(json.dumps(self.pack))
        self.assertEqual(self.run_main()[1].call_count, 1)
        self.assertNotEqual(self.latest()['source_hash'], before['source_hash'])

    def test_historical_publish_updates_date_index_without_replacing_current_latest(self):
        self.assertEqual(self.run_main()[0], 0)
        current = self.latest()
        latest_bytes = (self.args.output_root / 'latest.json').read_bytes()
        previous_day = (datetime.fromisoformat(self.pack['date']).date() - timedelta(days=1)).isoformat()
        self.args.date = self.pack['date'] = previous_day
        self.pack['generated_at'] = previous_day + 'T08:00:00+08:00'
        self.pack_file.write_text(json.dumps(self.pack))
        self.assertEqual(self.run_main()[0], 0)
        day_index = self.args.output_root / previous_day / 'daily.json'
        historical = json.loads(day_index.read_text())
        self.assertEqual(historical['date'], previous_day)
        self.assertTrue(self.asset(historical['audio_url']).is_file())
        self.assertTrue(self.asset(historical['transcript_url']).is_file())
        self.assertEqual((self.args.output_root / 'latest.json').read_bytes(), latest_bytes)
        self.assertEqual(self.latest(), current)
        # An existing historical date index must also advance on a rebuild.
        self.pack['claims'][0]['uncertainty'] += '历史材料补充说明。'
        self.pack_file.write_text(json.dumps(self.pack))
        self.assertEqual(self.run_main()[0], 0)
        rebuilt = json.loads(day_index.read_text())
        self.assertNotEqual(rebuilt['audio_url'], historical['audio_url'])
        self.assertEqual((self.args.output_root / 'latest.json').read_bytes(), latest_bytes)

    def test_equal_date_rebuild_and_invalid_latest_allow_publication(self):
        self.assertEqual(self.run_main()[0], 0)
        before = self.latest()
        self.args.force = True
        self.assertEqual(self.run_main()[0], 0)
        self.assertNotEqual(self.latest()['audio_url'], before['audio_url'])
        for invalid in ('{bad json', 'null', '[]', '{}', '{"date":"invalid"}', '{"date":42}'):
            with self.subTest(latest=invalid):
                (self.args.output_root / 'latest.json').write_text(invalid)
                self.assertEqual(self.run_main()[0], 0)
                self.assertEqual(self.latest()['date'], self.pack['date'])

    def test_autodetect_prefers_pack_over_html_and_rejects_invalid_without_fallback(self):
        self.args.fact_pack = None
        self.args.input = self.root / 'report.html'
        html = '<div class="ai-section">不能播报的旧分析</div><script type="application/json" id="newsradar-fact-pack">{}</script>'
        self.args.input.write_text(html.format(json.dumps(self.pack)))
        self.assertEqual(self.run_main()[0], 0)
        self.assertNotIn('不能播报的旧分析', self.asset(self.latest()['transcript_url']).read_text())
        self.args.input.write_text(html.format('{bad json'))
        before = self.latest()
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(self.latest(), before)

    def test_cache_does_not_reuse_an_incomplete_bundle(self):
        self.run_main()
        self.asset(self.latest()['transcript_url']).unlink()
        code, synth, _ = self.run_main()
        self.assertEqual(code, 0)
        synth.assert_called_once()
        self.assertTrue(self.asset(self.latest()['transcript_url']).is_file())

    def test_invalid_explicit_pack_preserves_previous_publication(self):
        self.run_main()
        before = self.latest()
        for value in ('null', '{bad json', json.dumps({**self.pack, 'date': '2000-01-01'})):
            with self.subTest(value=value):
                self.pack_file.write_text(value)
                code, synth, _ = self.run_main()
                self.assertEqual(code, 1)
                synth.assert_not_called()
                self.assertEqual(self.latest(), before)

    def test_explicit_pack_precedes_html_and_does_not_require_input_file(self):
        self.args.input.write_text('invalid embedded pack')
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.latest()['source_kind'], 'fact_pack_claims')

    def test_article_only_metadata_exposes_ui_reason(self):
        self.pack['claims'] = []
        self.pack['articles'] = [{**self.pack['articles'][0], 'id': str(i)} for i in range(3)]
        self.pack_file.write_text(json.dumps(self.pack))
        self.assertEqual(self.run_main()[0], 0)
        metadata = self.latest()
        self.assertEqual(metadata['source_kind'], 'fact_pack_articles')
        self.assertFalse(metadata['analysis_available'])
        self.assertIn('仅播报来源陈述', metadata['source_reason'])

    def test_legacy_only_on_production_path_marked_unverified_and_no_expansion(self):
        self.args.fact_pack = None
        self.args.date = None
        self.args.input = self.root / 'legacy.html'
        stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%m-%d')
        self.args.input.write_text(f'生成时间</span><span class="info-value">{stamp}</span>'
                                   '<div class="ai-section">AI 热点分析' + '来源材料中的已知陈述。' * 10 + '</div>')
        self.assertEqual(self.run_main()[0], 1)
        client = Mock()
        client.return_value.validate_config.return_value = (True, '')
        client.return_value.chat.return_value = '据来源报道，模型团队发布新版本，目前只有公告信息，尚无独立评测，后续表现仍需验证。'
        with patch.object(podcast, 'LATEST_DAILY', self.args.input):
            self.assertEqual(self.run_main(ai=client)[0], 0)
        metadata = self.latest()
        self.assertEqual(metadata['source_kind'], 'legacy_unverified')
        self.assertFalse(metadata['analysis_available'])
        self.assertFalse(metadata['claim_grounded'])
        self.assertIsNone(metadata['fact_pack_hash'])
        client.return_value.chat.assert_called_once()

    def test_cli_accepts_isolated_output_and_fact_pack(self):
        with patch.object(sys, 'argv', ['podcast', '--fact-pack', str(self.pack_file), '--output-root', str(self.args.output_root),
                                        '--date', self.pack['date']]):
            args = podcast.parse_args()
        self.assertEqual(args.fact_pack, self.pack_file)
        self.assertEqual(args.output_root, self.args.output_root)

    def test_cleanup_does_not_follow_date_symlinks(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.mp3').write_bytes(b'keep')
        self.args.output_root.mkdir()
        (self.args.output_root / '2000-01-01').symlink_to(outside, target_is_directory=True)
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        self.assertEqual(podcast.cleanup_audio_history(self.args.output_root, today, 90), [])
        self.assertTrue((outside / 'keep.mp3').exists())


class DurationProbeTest(unittest.TestCase):
    def test_ffprobe_reports_actual_seconds(self):
        result = subprocess.CompletedProcess([], 0, stdout='{"format":{"duration":"91.234"}}')
        with patch.object(podcast.shutil, 'which', return_value='/usr/bin/ffprobe'), \
             patch.object(podcast.subprocess, 'run', return_value=result) as run:
            self.assertEqual(podcast.measure_duration(Path('/tmp/staged.mp3')), 91.234)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertIn('format=duration', run.call_args.args[0])

    def test_mutagen_fallback_and_unmeasurable_failure(self):
        parent = types.ModuleType('mutagen')
        child = types.ModuleType('mutagen.mp3')
        child.MP3 = Mock(return_value=types.SimpleNamespace(info=types.SimpleNamespace(length=42.5)))
        with patch.dict(sys.modules, {'mutagen': parent, 'mutagen.mp3': child}), \
             patch.object(podcast.shutil, 'which', return_value=None):
            self.assertEqual(podcast.measure_duration(Path('/tmp/staged.mp3')), 42.5)
            child.MP3.side_effect = ValueError('invalid audio')
            with self.assertRaisesRegex(ValueError, 'cannot measure'):
                podcast.measure_duration(Path('/tmp/staged.mp3'))

    def test_tts_only_saves_to_supplied_staging_file(self):
        communicate = Mock()
        communicate.save = AsyncMock()
        with patch.object(podcast.edge_tts, 'Communicate', return_value=communicate) as factory:
            podcast.asyncio.run(podcast.synthesize('来源快讯', Path('/tmp/private-stage.mp3'), 'zh-CN-YunyangNeural', '+0%', '+0%'))
        factory.assert_called_once_with('来源快讯', voice='zh-CN-YunyangNeural', rate='+0%', volume='+0%')
        communicate.save.assert_awaited_once_with('/tmp/private-stage.mp3')


if __name__ == '__main__':
    unittest.main()
