import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "apply_site_features", REPO / "scripts" / "apply_site_features.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


HTML = """<!doctype html><html><head>
<link rel="stylesheet" href="/assets/report-speech.css">
<script src="/assets/report-speech.js" defer></script>
</head><body><div class="header"><div class="header-title">热点新闻趋势</div></div>
<a href="/trends.html">趋势</a><a href="/status.html">状态</a>
<a href="/sources.html">候选</a><a href="/archive.html">历史</a>
<div class="footer">由 <span class="project-name">TrendRadar</span> 生成 ·
<a href="https://github.com/sansan0/TrendRadar" target="_blank" class="footer-link">GitHub 开源项目</a></div>
</body></html>"""


class SiteFeatureTests(unittest.TestCase):
    def create_site(self, root: Path) -> Path:
        site = root / "site"
        site.mkdir()
        (site / "index.html").write_text(HTML, encoding="utf-8")
        (site / "reports").mkdir()
        (site / "reports" / "daily.html").write_text(HTML, encoding="utf-8")
        (site / "audio").mkdir()
        (site / "audio" / "daily.mp3").write_bytes(b"audio")
        for filenames in MODULE.ROUTE_FILES.values():
            for filename in filenames:
                (site / filename).write_text("generated", encoding="utf-8")
        return site

    def test_production_defaults_remove_preview_features(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = self.create_site(Path(temporary))
            switches = MODULE.FEATURE_DEFAULTS["production"].copy()
            MODULE.apply_features(site, "production", switches)

            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("report-speech", page)
            self.assertNotIn("/trends.html", page)
            self.assertNotIn("/status.html", page)
            self.assertNotIn("/sources.html", page)
            self.assertNotIn("report-experience", page)
            self.assertNotIn("/events.html", page)
            self.assertIn("/archive.html", page)
            self.assertIn("/assets/brand/favicon-32.png", page)
            self.assertIn("newsradar-wordmark.png", page)
            self.assertIn("top:6px", page)
            self.assertIn("margin-top:60px", page)
            self.assertIn("margin-top:98px", page)
            self.assertIn("grid-template-columns:repeat(2", page)
            self.assertNotIn("drop-shadow(5px 5px", page)
            self.assertIn("https://github.com/applejuice0605/NewsRadar.git", page)
            self.assertNotIn("GitHub 开源项目", page)
            self.assertFalse((site / "audio").exists())
            self.assertFalse((site / "trends.html").exists())
            manifest = json.loads((site / "site-features.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["environment"], "production")
            self.assertFalse(any(manifest["features"].values()))

    def test_preview_defaults_keep_all_features(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = self.create_site(Path(temporary))
            switches = MODULE.FEATURE_DEFAULTS["preview"].copy()
            MODULE.apply_features(site, "preview", switches)

            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn("report-speech", page)
            self.assertIn("/trends.html", page)
            self.assertTrue((site / "audio" / "daily.mp3").exists())
            self.assertTrue((site / "sources.html").exists())
            self.assertIn("report-experience", page)
            self.assertIn("/events.html", page)
            self.assertTrue((site / "experience.json").exists())

    def test_preview_restores_features_removed_from_production_html(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = self.create_site(Path(temporary))
            stripped = HTML.replace('<link rel="stylesheet" href="/assets/report-speech.css">', "")
            stripped = stripped.replace('<script src="/assets/report-speech.js" defer></script>', "")
            stripped = stripped.replace('<a href="/trends.html">趋势</a>', "")
            stripped = stripped.replace('<a href="/status.html">状态</a>', "")
            stripped = stripped.replace('<a href="/sources.html">候选</a>', "")
            (site / "index.html").write_text(stripped, encoding="utf-8")

            MODULE.apply_features(site, "preview", MODULE.FEATURE_DEFAULTS["preview"].copy())

            page = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn('/assets/report-speech.css', page)
            self.assertIn('/assets/report-speech.js', page)
            self.assertIn('/trends.html', page)
            self.assertIn('/status.html', page)
            self.assertIn('/sources.html', page)
            self.assertIn('/events.html', page)
            self.assertIn('/search.html', page)

    def test_individual_override(self) -> None:
        switches = MODULE.FEATURE_DEFAULTS["production"].copy()
        switches["trends"] = MODULE.parse_switch("on", switches["trends"])
        self.assertTrue(switches["trends"])
        self.assertFalse(switches["speech"])


if __name__ == "__main__":
    unittest.main()
