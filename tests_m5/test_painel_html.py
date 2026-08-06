import unittest
from pathlib import Path


class TestPainelHtml(unittest.TestCase):
    def test_falha_transitoria_preserva_ultimo_grafico(self):
        html = (
            Path(__file__).resolve().parent.parent / "grafico_web" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("if (atualizando) return", html)
        self.assertNotIn("serieCandle.setData([])", html)
        self.assertIn("Mantendo o último gráfico", html)
        self.assertIn("chartPreco.priceScale('right').applyOptions({ autoScale: true })", html)


if __name__ == "__main__":
    unittest.main()
