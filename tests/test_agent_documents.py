from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.documents import extract_document, scene_from_text


class DocumentTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reads_utf8_text(self) -> None:
        path = self.root / "worksheet.txt"
        path.write_text("画出函数 y=x^2-2x-3 的图像。", encoding="utf-8")
        result = extract_document(path)
        self.assertTrue(result.ok)
        self.assertIn("x^2-2x-3", result.text)
        self.assertEqual(result.metadata()["chars"], len(result.text))

    def test_reads_gb18030_text(self) -> None:
        path = self.root / "gb.txt"
        path.write_bytes("串联电路：电池、开关、电阻。".encode("gb18030"))
        result = extract_document(path)
        self.assertTrue(result.ok)
        self.assertIn("电池", result.text)

    def test_empty_text_is_reported(self) -> None:
        path = self.root / "empty.txt"
        path.write_text("   \n", encoding="utf-8")
        result = extract_document(path)
        self.assertFalse(result.ok)
        self.assertIn("为空", result.message)

    def test_scene_from_text_accepts_bare_and_wrapped(self) -> None:
        bare = '{"title":"T","blocks":[{"id":"a","primitives":[{"type":"circle","cx":50,"cy":50,"r":10}]}]}'
        wrapped = '{"scene":' + bare + "}"
        self.assertEqual(scene_from_text(bare)["title"], "T")
        self.assertEqual(scene_from_text(wrapped)["title"], "T")
        self.assertIsNone(scene_from_text("画一个三角形"))
        self.assertIsNone(scene_from_text('{"title":"T","blocks":[]}'))

    def test_pdf_without_text_layer_is_reported(self) -> None:
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf not installed")
        path = self.root / "scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        with path.open("wb") as handle:
            writer.write(handle)
        result = extract_document(path)
        self.assertFalse(result.ok)
        self.assertIn("扫描件", result.message)


if __name__ == "__main__":
    unittest.main()
