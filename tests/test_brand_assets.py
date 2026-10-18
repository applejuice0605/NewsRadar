import struct
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
BRAND = REPO / "docs" / "assets" / "brand"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    return struct.unpack(">II", data[16:24])


class BrandAssetTests(unittest.TestCase):
    def test_favicon_sizes_are_exact(self) -> None:
        for size in (16, 32, 48):
            self.assertEqual(png_size(BRAND / f"favicon-{size}.png"), (size, size))
        self.assertEqual(png_size(BRAND / "apple-touch-icon.png"), (180, 180))

    def test_share_image_uses_open_graph_ratio(self) -> None:
        self.assertEqual(png_size(BRAND / "newsradar-share-1200x630.png"), (1200, 630))

    def test_report_template_uses_wordmark_and_social_image(self) -> None:
        template = (REPO / "TrendRadar" / "trendradar" / "report" / "html.py").read_text(encoding="utf-8")
        self.assertIn("newsradar-mark-512.png", template)
        self.assertIn("newsradar-wordmark.png", template)
        self.assertIn("width: clamp(300px, 38vw, 380px)", template)
        self.assertIn("right: 0", template)
        self.assertNotIn("drop-shadow(5px 5px", template)
        self.assertIn("newsradar-share-1200x630.png", template)
        self.assertIn('alt="NewsRadar"', template)


if __name__ == "__main__":
    unittest.main()
