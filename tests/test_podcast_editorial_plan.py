import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("podcast_editorial_plan", ROOT / "scripts/podcast_editorial_plan.py")
assert SPEC and SPEC.loader
plan_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan_module)


def report(source, text, *, published_at="2026-10-29", **extra):
    value = {
        "id": f"id-{source}",
        "title": f"标题 {source}",
        "source": source,
        "url": f"https://example.com/{source}",
        "published_at": published_at,
        "text": text,
        "source_genre": "source_report",
    }
    value.update(extra)
    return value


class PodcastEditorialPlanTest(unittest.TestCase):
    def test_brief_is_source_excerpts_and_discards_outlet_credit(self):
        text = "感谢IT之家网友 小明 的线索投递！某公司发布模型，支持本地工具调用。模型能够自动检索文档。目前仅向测试者开放。"
        result = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("新闻", text)]})
        topic = result["topics"][0]
        self.assertFalse(any("线索投递" in f for f in topic["facts"]))
        self.assertTrue(topic["brief"]["news_change"].startswith("某公司发布"))
        for values in [topic["brief"]["mechanism_facts"], topic["brief"]["declared_limits"]]:
            self.assertTrue(all(v in topic["facts"] and v in text for v in values))

    def test_chinese_lead_is_kept_and_tail_finance_is_excluded(self):
        text = (
            "某公司发布新模型，支持实时工具调用和多步任务。"
            "该模型可以在本地运行，并提供测试预览。"
            "目前仅向可信测试者开放，未来几周逐步扩大访问。"
            "公司完成 1400 万美元融资，估值约 1 亿美元。"
            "过去六个月年化收入增长 14 倍，并与客户开展合作。"
        )
        result = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("新闻", text)]})
        facts = result["topics"][0]["facts"]
        self.assertIn("某公司发布新模型，支持实时工具调用和多步任务。", facts)
        self.assertTrue(any("可信测试者" in fact for fact in facts))
        self.assertFalse(any("融资" in fact or "年化收入" in fact for fact in facts))
        self.assertTrue(all(fact in text for fact in facts))

    def test_english_split_does_not_break_model_version_or_decimal(self):
        text = (
            "The ML4/5.9.0 release improves tool use. "
            "It reaches 5.9.0 quality in internal testing. "
            "The research preview is available to trusted testers. "
            "Access will gradually expand to developers."
        )
        facts, _ = plan_module._select_facts(text)
        self.assertTrue(any("ML4/5.9.0" in fact for fact in facts))
        self.assertFalse(any(fact == "0" for fact in facts))
        self.assertTrue(all(fact in text for fact in facts))

    def test_community_is_excluded_and_only_first_three_reports_are_used(self):
        body = "发布新功能，支持自动处理任务。现在向测试者开放。未来会扩大使用。"
        items = [
            report("社区", body, url="https://www.reddit.com/r/ai/1", source_genre="community_post"),
            report("一", body), report("二", body), report("三", body), report("四", body),
        ]
        result = plan_module.build_editorial_plan({"date": "2026-10-29", "items": items})
        self.assertEqual([topic["source"] for topic in result["topics"]], ["一", "二", "三"])
        self.assertEqual(len(result["topics"]), 3)

    def test_question_seed_and_moves_are_editorial_metadata(self):
        body = "用户先完成 OAuth 授权，再可以检索文件。功能目前处于研究预览。未来逐步开放。"
        result = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("源", body)]})
        topic = result["topics"][0]
        self.assertEqual(topic["question_seed"], "少几次操作，会不会也少一道确认？")
        self.assertEqual(topic["category"], "permissions")
        self.assertEqual(len(topic["dialogue_moves"]), 6)
        self.assertEqual(len(set(tuple(result["topics"][i]["dialogue_moves"]) for i in range(1))) , 1)

    def test_office_question_does_not_turn_cross_product_facts_into_permission_training(self):
        body = "Buddy One 打通 Gmail、文档和会议。用户不用反复授权，系统会整合邮件和会议内容。"
        result = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("办公源", body)]})
        self.assertEqual(
            result["topics"][0]["question_seed"],
            "把分散在邮件、文档和会议里的信息接起来后，人实际省掉了哪一步？",
        )

    def test_world_model_question_is_about_editable_simulation_not_generic_availability(self):
        body = "新世界模型可以生成可交互虚拟世界。用户能在 simulation 中修改场景。当前处于研究预览。"
        result = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("世界模型源", body)]})
        self.assertEqual(
            result["topics"][0]["question_seed"],
            "看一段生成的视频，和自己能进去改场景，差别在哪里？",
        )

    def test_plan_never_exposes_title_url_or_id_and_audit_has_no_programme_numbers(self):
        text = "发布新模型，支持代码和代理任务。当前仅向可信测试者开放。未来会扩大访问。"
        result = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("源", text)]})
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("标题 源", rendered)
        self.assertNotIn("https://example.com/源", rendered)
        self.assertNotIn("id-源", rendered)
        self.assertNotIn("target_turns", result["audit_text"])
        self.assertNotIn("2200", result["audit_text"])
        self.assertNotIn("2400", result["audit_text"])
        self.assertIn(text.split("。", 1)[0] + "。", result["audit_text"])

    def test_same_input_is_deterministic_and_empty_source_fails(self):
        body = "模型支持多步任务。当前处于研究预览。未来逐步扩大访问。"
        payload = {"date": "2026-10-29", "items": [report("源", body)]}
        self.assertEqual(plan_module.build_editorial_plan(payload), plan_module.build_editorial_plan(payload))
        with self.assertRaises(ValueError):
            plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("空", "导航 登录 注册")]})

    def test_mechanism_signals_survive_customer_and_statistics_filters(self):
        simplismart = (
            "英伟达参与 Simplismart 的融资。"
            "软件位于 AI 模型与底层计算基础设施之间。"
            "软件根据模型的实际需求调整部署方式，自动扩缩计算资源并监控运行表现。"
            "客户包括 InVideo、HeyGen、Higgsfield 和 Ema。"
        )
        result = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("源", simplismart)]})
        facts = result["topics"][0]["facts"]
        self.assertTrue(any("位于 AI 模型与底层计算基础设施之间" in fact for fact in facts))
        self.assertTrue(any("自动扩缩计算资源" in fact for fact in facts))
        self.assertFalse(any("客户包括" in fact for fact in facts))

        gartner = (
            "调查显示，97%的组织尚未达到预期成本节约。"
            "人工智能释放的时间只有21%被重新投入创新工作。"
            "机构建议帮助员工把释放的时间用于更有价值的工作。"
            "没有据此得出人工智能无效的结论。"
        )
        facts = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("调查源", gartner)]})["topics"][0]["facts"]
        self.assertTrue(any("重新投入创新工作" in fact for fact in facts))
        self.assertLessEqual(sum("没有据此" in fact for fact in facts), 1)

    def test_carbon_prefers_long_horizon_mechanism_over_repeated_rollout(self):
        carbon = (
            "Today, we announce a new model. "
            "Carbon ranks #1 on a benchmark at 59. "
            "Designed for sustained agentic workflows and long-horizon engineering. "
            "Our teams have seen Carbon run for days to persist through dead ends and patch bugs. "
            "We are taking a deliberate approach to rollout and government testing. "
            "Starting this week, Carbon is available to a subset of cyber defenders. "
            "We will gradually phase access to trusted partners."
        )
        facts = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("Google", carbon)]})["topics"][0]["facts"]
        self.assertTrue(any("run for days" in fact for fact in facts))
        self.assertTrue(any("subset of cyber defenders" in fact for fact in facts))
        self.assertLessEqual(sum("available" in fact.lower() or "access" in fact.lower() for fact in facts), 2)
        self.assertFalse(any("ranks #1" in fact for fact in facts))

    def test_ithome_prefix_and_leading_quote_are_trimmed_as_substrings(self):
        text = "IT之家 10 月 29 日消息，某产品发布新能力。”他解释称，软件位于模型与基础设施之间。"
        facts = plan_module.build_editorial_plan({"date": "2026-10-29", "items": [report("IT之家", text)]})["topics"][0]["facts"]
        self.assertIn("某产品发布新能力。", facts)
        self.assertIn("他解释称，软件位于模型与基础设施之间。", facts)
        self.assertTrue(all(fact in text for fact in facts))


if __name__ == "__main__":
    unittest.main()
