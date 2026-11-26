"""Offline speech editing must not trade factual qualifiers for fluency."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "TrendRadar"))
from trendradar.ai.podcast_narration import compose_narration, validate_ledger


def material(summary="官方称仍在测试。"):
    source = {"source": "团队公告", "title": "模型 K3.1 仅在测试集提升 3.5%", "summary": summary}
    return {"articles": [], "evidence_sources": {"N001": source}, "claims": [
        {"fact": source["title"], "source_ids": ["N001"],
         "interpretation": "如果测试可复现，才可能减少成本。",
         "uncertainty": "仅有标题，未核验正文；未披露成本是否包括人工。"}]}


class NarrationTest(unittest.TestCase):
    def render(self, pack):
        return compose_narration(pack, lambda text: text)

    def test_removes_field_labels_but_keeps_attribution_modality_and_limits(self):
        script, review = self.render(material())
        for label in ("条件性分析", "不确定性：", "来源摘要", "来源陈述："):
            self.assertNotIn(label, script)
        for text in ("团队公告有一篇文章，标题是", "K3.1", "3.5%", "仅在测试集", "如果测试可复现，才可能减少成本", "未披露成本是否包括人工"):
            self.assertIn(text, script)
        self.assertEqual(review["additional_model_calls"], 0)
        self.assertTrue(review["listening_review_required"])
        self.assertFalse(review["factual_truth_verified"])

    def test_title_only_is_a_title_not_an_announcement(self):
        script, _ = self.render(material(summary=""))
        self.assertIn("团队公告有一篇文章，标题是", script)
        self.assertNotIn("据团队公告报道", script)

    def test_exact_duplicate_summary_omitted_but_new_information_retained(self):
        source = material()["evidence_sources"]["N001"]
        pack = {"claims": [], "articles": [{**source, "summary": source["title"]}]}
        script, _ = self.render(pack)
        self.assertEqual(script.count(source["title"]), 1)
        pack["articles"][0]["summary"] += "。但还没有公布生产数据。"
        script, _ = self.render(pack)
        self.assertIn("但还没有公布生产数据", script)

    def test_punctuation_preserves_quoted_titles_versions_and_decimal_values(self):
        pack = material()
        pack["claims"][0]["fact"] = "版本 v1.7.2：延迟 3.5 毫秒；成本 1.7 万元"
        script, review = self.render(pack)
        self.assertIn("3.5 毫秒；成本 1.7 万元", script)
        self.assertIn("v1.7.2", review["pronunciation_review_terms"])
        self.assertIn("1.7", review["pronunciation_review_terms"])

    def test_does_not_split_inside_quotes_or_conditionals(self):
        pack = material()
        claim = pack["claims"][0]
        claim["fact"] = "标题中的原话是“并未发布；仍在测试”"
        claim["interpretation"] = "如果接口兼容；才可能迁移。"
        script, _ = self.render(pack)
        self.assertIn("“并未发布；仍在测试”", script)
        self.assertIn("如果接口兼容；才可能迁移", script)

    def test_long_conditional_is_reported_not_truncated_or_invented(self):
        pack = material()
        text = "如果" + "接口兼容性和回归质量仍需核查，" * 8 + "就不能下结论。"
        pack["claims"][0]["interpretation"] = text
        script, review = self.render(pack)
        self.assertIn(text, script)
        self.assertTrue(review["warnings"])
        self.assertNotIn("那么我们今天", script)

    def test_ledger_rejects_changed_numbers_negation_and_names(self):
        _, review = self.render(material())
        for old, new in (("3.5", "35"), ("仅", ""), ("K3.1", "K4")):
            ledger = copy.deepcopy(review["ledger"])
            ledger[0]["spoken_text"] = ledger[0]["spoken_text"].replace(old, new)
            with self.subTest(old=old), self.assertRaises(ValueError):
                validate_ledger(ledger)

    def test_specific_uncertainties_not_collapsed_into_generic_disclaimer(self):
        pack = material()
        pack["claims"].append({**pack["claims"][0], "uncertainty": "尚不清楚是否向第三方上传数据。"})
        script, _ = self.render(pack)
        self.assertIn("成本是否包括人工", script)
        self.assertIn("是否向第三方上传数据", script)

    def test_nonempty_summary_does_not_turn_a_headline_into_a_fact(self):
        pack = material(summary="这里没有提供上线证据。")
        script, _ = self.render(pack)
        self.assertIn("标题是", script)
        self.assertNotIn("据团队公告报道", script)
        source = pack["evidence_sources"]["N001"]
        script, _ = self.render({"claims": [], "articles": [source]})
        self.assertIn("标题是", script)
        self.assertIn("文中还提到，这里没有提供上线证据", script)

    def test_other_condition_markers_and_ascii_quotes_are_not_split(self):
        for text in ('假如服务中断；则暂停部署。', '当测试未通过时；不能发布。',
                     '在完成测试后；才会发布。', '要是完成了测试；仍需复核。',
                     'If tests pass; only then deploy.',
                     '前提是：若未经复测；不能发布。', '原话是"尚未发布；仍在测试"。'):
            pack = material()
            pack["claims"][0]["interpretation"] = text
            script, _ = self.render(pack)
            self.assertIn(text, script)

    def test_long_sentence_check_includes_attribution_wrapper(self):
        pack = material()
        pack["claims"][0]["fact"] = "待核验的标题" * 9
        pack["evidence_sources"]["N001"]["source"] = "这是一段较长的来源名称"
        _, review = self.render(pack)
        self.assertTrue(review["warnings"])
        self.assertIn("据这是一段较长的来源名称报道", review["warnings"][0]["text"])

    def test_repeated_boilerplate_not_added_to_explicitly_conditional_analysis(self):
        pack = material()
        pack["claims"] *= 3
        script, _ = self.render(pack)
        self.assertNotIn("这里的分析还不能当作定论", script)
        self.assertEqual(script.count("如果测试可复现，才可能减少成本"), 3)

    def test_claim_fragment_from_title_is_not_a_verified_announcement(self):
        pack = material(summary="尚无正文。")
        pack["evidence_sources"]["N001"]["title"] = "厂商称产品已上线"
        pack["claims"][0]["fact"] = "产品已上线"
        script, _ = self.render(pack)
        self.assertIn("有文章的标题提到", script)
        self.assertNotIn("据团队公告报道，产品已上线", script)
        self.assertNotIn("标题是", script)

    def test_numeric_signs_and_alphanumeric_terms_are_protected(self):
        for old, new in (("-3.5%", "3.5%"), ("−3.5%", "3.5%"), ("+3.5%", "3.5%")):
            unit = {"role": "fact", "source_text": old, "spoken_text": new}
            with self.subTest(old=old), self.assertRaises(ValueError):
                validate_ledger([unit])
        pack = material()
        pack["claims"][0]["fact"] = "3D 界面"
        _, review = self.render(pack)
        self.assertIn("3D", review["pronunciation_review_terms"])

    def test_deterministic_and_no_mutation_of_source(self):
        pack = material()
        before = copy.deepcopy(pack)
        self.assertEqual(self.render(pack), self.render(pack))
        self.assertEqual(pack, before)


if __name__ == "__main__":
    unittest.main()
