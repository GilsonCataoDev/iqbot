"""Resultado publicado: direção pela conta, frase pela IA.

A direção nunca sai do modelo — ela vem de resultado_direcao(), que compara
actual com forecast e sabe de que lado do par a moeda está. A IA só descreve
o mecanismo em texto, e a ausência dela não pode mudar nada.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

import monitor_mercado as M
from iqoption_m5 import ia


class _CalComResultado:
    def __init__(self, **campos):
        self.campos = {"titulo": "CPI m/m", "moeda": "USD", "actual": "0.5",
                       "forecast": "0.2", "previous": "0.3", "direcao": "PUT"}
        self.campos.update(campos)

    def confirmacao_recente(self, ativo, agora):
        return self.campos

    def aviso(self, ativo, agora):
        return None


class TestDesvioDoPrevisto(unittest.TestCase):
    def test_percentual_simples(self):
        self.assertEqual(M._desvio_do_previsto("0.5", "0.2"), 150.0)

    def test_ignora_simbolos(self):
        self.assertEqual(M._desvio_do_previsto("3.2%", "3.0%"), 6.7)
        self.assertEqual(M._desvio_do_previsto("180K", "200K"), -10.0)

    def test_negativo_usa_modulo_do_previsto(self):
        self.assertEqual(M._desvio_do_previsto("-1.4", "-1.0"), -40.0)

    def test_previsto_zero_nao_tem_percentual(self):
        self.assertIsNone(M._desvio_do_previsto("0.3", "0"))

    def test_texto_nao_numerico(self):
        self.assertIsNone(M._desvio_do_previsto("n/d", "0.2"))
        self.assertIsNone(M._desvio_do_previsto(None, "0.2"))


class TestContextoNoticia(unittest.TestCase):
    def setUp(self):
        self._orig = ia.ler_resultado_noticia
        self.addCleanup(lambda: setattr(ia, "ler_resultado_noticia", self._orig))
        self.agora = datetime.now(timezone.utc)

    def _ctx(self, cal=None):
        return M.contexto_noticia(cal or _CalComResultado(), "XAUUSD", self.agora)

    def test_sem_ia_mantem_direcao_e_desvio(self):
        ia.ler_resultado_noticia = lambda *a, **k: None
        ctx = self._ctx()
        self.assertEqual(ctx["direcao"], "PUT")
        self.assertEqual(ctx["desvio_pct"], 150.0)
        self.assertNotIn("leitura_ia", ctx)

    def test_ia_anexa_frase_sem_tocar_na_direcao(self):
        ia.ler_resultado_noticia = lambda *a, **k: {
            "surpresa": "forte", "leitura": "Juros altos fortalecem o dolar.",
        }
        ctx = self._ctx()
        self.assertEqual(ctx["direcao"], "PUT", "direcao continua vindo da conta")
        self.assertEqual(ctx["surpresa"], "forte")
        self.assertIn("dolar", ctx["leitura_ia"])

    def test_ia_com_erro_nao_derruba(self):
        def explode(*a, **k):
            raise RuntimeError("sem rede")

        ia.ler_resultado_noticia = explode
        ctx = self._ctx()
        self.assertEqual(ctx["estado"], "resultado_publicado")
        self.assertEqual(ctx["direcao"], "PUT")

    def test_ia_nao_pode_inverter_a_direcao(self):
        """Mesmo devolvendo algo com 'direcao', a chave nao e copiada."""
        ia.ler_resultado_noticia = lambda *a, **k: {
            "surpresa": "fraca", "leitura": "x", "direcao": "CALL",
        }
        self.assertEqual(self._ctx()["direcao"], "PUT")


class TestLerResultadoNoticia(unittest.TestCase):
    def test_sem_modelo_devolve_none(self):
        orig = ia.MODELO_SEGUNDA_OPINIAO
        ia.MODELO_SEGUNDA_OPINIAO = ""
        try:
            self.assertIsNone(
                ia.ler_resultado_noticia("CPI m/m", "USD", "0.5", "0.2")
            )
        finally:
            ia.MODELO_SEGUNDA_OPINIAO = orig

    def test_sem_valor_publicado_nao_consulta(self):
        self.assertIsNone(ia.ler_resultado_noticia("CPI m/m", "USD", "", "0.2"))
        self.assertIsNone(ia.ler_resultado_noticia("CPI m/m", "USD", None, "0.2"))

    def test_titulo_vazio_nao_consulta(self):
        self.assertIsNone(ia.ler_resultado_noticia("   ", "USD", "0.5", "0.2"))


if __name__ == "__main__":
    unittest.main()
