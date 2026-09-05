import unittest
from pathlib import Path


class TestPainelHtml(unittest.TestCase):
    def test_falha_transitoria_preserva_ultimo_grafico(self):
        html = (
            Path(__file__).resolve().parent.parent / "grafico_web" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("if (!ativoAtual || atualizando) return", html)
        self.assertNotIn("serieCandle.setData([])", html)
        self.assertIn("atualização atrasada", html)
        self.assertIn("c.priceScale('right').applyOptions({ autoScale: true })", html)

    def test_painel_tem_leitura_rapida_e_alvo_provavel(self):
        html = (
            Path(__file__).resolve().parent.parent / "grafico_web" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("Leitura rápida", html)
        self.assertIn("renderizarLeituraRapida", html)
        self.assertIn("alvoProvavel", html)
        self.assertIn("linhaAlvoProvavel", html)
        self.assertIn("priceLineVisible: false", html)
        self.assertIn(".slice(0, 4)", html)

    def test_aviso_nunca_aparece_como_entrar(self):
        html = (
            Path(__file__).resolve().parent.parent / "grafico_web" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("radarEstado.includes('AVISO')", html)
        self.assertIn("mensagem.includes('aviso')", html)
        self.assertIn("direcaoNormal === null", html)
        self.assertIn("direcaoRaw === 'call' || direcaoRaw === 'compra'", html)

    def test_alerta_principal_tem_prioridade_sobre_alerta_proximo(self):
        html = (
            Path(__file__).resolve().parent.parent / "grafico_web" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("const srcPrioritario = al || ap", html)
        self.assertIn("const src = srcPrioritario", html)

    def test_lab_reduz_poluição_e_abre_lateral(self):
        html = (
            Path(__file__).resolve().parent.parent / "grafico_web" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("min-width: 290px", html)
        self.assertIn('id="camada-bloqueados"', html)
        self.assertIn('id="camada-auxiliares"', html)
        self.assertIn("compactarMarcadores", html)
        self.assertIn("modoLab", html)

    def test_fibo_e_desenhada_a_partir_das_ancoras_do_impulso(self):
        html = (
            Path(__file__).resolve().parent.parent / "grafico_web" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("inicio_time", html)
        self.assertIn("fim_time", html)
        self.assertIn("_ancorasFib", html)
        self.assertIn("Fib impulso", html)
        self.assertIn("f.papel === 'zona' || f.papel === 'extremo'", html)

        # A perna vem só das âncoras do backend. Casar preços contra os candles
        # desenhava uma perna plausível porém arbitrária — não pode voltar.
        self.assertNotIn("campoOrigem", html)
        self.assertNotIn("campoExtremo", html)

    def test_fibo_sombreia_a_zona_e_colore_pelo_estado(self):
        html = (
            Path(__file__).resolve().parent.parent / "grafico_web" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("addBaselineSeries", html)
        self.assertIn("zona_inf", html)
        self.assertIn("zona_sup", html)
        for cor in ("#26a69a", "#e0b040", "#5a6072", "#bc8cff"):
            self.assertIn(cor, html)


if __name__ == "__main__":
    unittest.main()
