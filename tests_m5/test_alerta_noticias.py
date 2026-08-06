import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from iqoption_m5.alerta import Alerta, anexar_noticia, detectar_reversao, explicar_decisao, para_grafico
from iqoption_m5.modelos import Decisao
from iqoption_m5.noticias import CalendarioEconomico, e_sintetico, moedas_do_ativo


class TestMoedasDoAtivo(unittest.TestCase):
    def test_par_normal(self):
        self.assertEqual(moedas_do_ativo("EURUSD"), ("EUR", "USD"))

    def test_par_otc_ignora_sufixo(self):
        self.assertEqual(moedas_do_ativo("USDJPY-OTC"), ("USD", "JPY"))

    def test_reconhece_ativo_sintetico(self):
        self.assertTrue(e_sintetico("EURUSD-OTC"))
        self.assertFalse(e_sintetico("EURUSD"))


class TestDeteccaoReversao(unittest.TestCase):
    def _indicadores(self, corpo, atr=0.001, rsi=50.0, tendencia="lateral"):
        indice = pd.date_range("2026-08-03 10:00", periods=3, freq="5min")
        abertura = 1.1000
        return pd.DataFrame(
            {
                "Open": [abertura] * 3,
                "Close": [abertura, abertura + corpo, abertura],
                "High": [abertura + 0.002] * 3,
                "Low": [abertura - 0.002] * 3,
                "ATR": [atr] * 3,
                "RSI": [rsi] * 3,
                "TendenciaMacro": [tendencia] * 3,
            },
            index=indice,
        )

    def test_queda_forte_gera_call(self):
        alerta = detectar_reversao("EURUSD", self._indicadores(corpo=-0.002))
        self.assertIsNotNone(alerta)
        self.assertEqual(alerta.direcao, "call")
        self.assertEqual(alerta.tipo, "entrada")

    def test_alta_forte_gera_put(self):
        alerta = detectar_reversao("EURUSD", self._indicadores(corpo=0.002))
        self.assertEqual(alerta.direcao, "put")

    def test_corpo_intermediario_apenas_aproxima(self):
        alerta = detectar_reversao("EURUSD", self._indicadores(corpo=-0.0012))
        self.assertEqual(alerta.tipo, "aproximando")

    def test_corpo_pequeno_nao_alerta(self):
        self.assertIsNone(detectar_reversao("EURUSD", self._indicadores(corpo=-0.0005)))

    def test_atr_invalido_nao_alerta(self):
        self.assertIsNone(detectar_reversao("EURUSD", self._indicadores(corpo=-0.002, atr=0)))

    def test_sugere_expiracao_medida_de_30_minutos(self):
        """A triagem mediu essa família com 6 velas M5; 5 min seria outro teste."""
        alerta = detectar_reversao("EURUSD", self._indicadores(corpo=-0.002))
        self.assertEqual(alerta.expiracao_sugerida_min, 30)

    def test_usa_a_ultima_vela_fechada_e_nao_a_em_formacao(self):
        indicadores = self._indicadores(corpo=-0.002)
        indicadores.iloc[-1, indicadores.columns.get_loc("Close")] = 99.0
        alerta = detectar_reversao("EURUSD", indicadores)
        self.assertEqual(alerta.hora, indicadores.index[-2])

    def test_motivos_sao_preenchidos(self):
        alerta = detectar_reversao("EURUSD", self._indicadores(corpo=-0.002, rsi=28))
        self.assertTrue(any("ATR" in motivo for motivo in alerta.motivos))
        self.assertTrue(any("RSI" in motivo for motivo in alerta.motivos))

    def test_expiracao_acompanha_o_timeframe(self):
        """Seis velas valem 30 min no M5 e 6 min no M1."""
        indicadores = self._indicadores(corpo=-0.002)
        self.assertEqual(detectar_reversao("EURUSD", indicadores, 300).expiracao_sugerida_min, 30)
        self.assertEqual(detectar_reversao("EURUSD", indicadores, 60).expiracao_sugerida_min, 6)

    def test_entrada_confirmada_usa_abertura_da_vela_em_formacao(self):
        indicadores = self._indicadores(corpo=-0.002)
        indicadores.iloc[-1, indicadores.columns.get_loc("Open")] = 1.0975
        alerta = detectar_reversao("EURUSD", indicadores)
        self.assertTrue(alerta.entrada_confirmada)
        self.assertAlmostEqual(alerta.preco_entrada, 1.0975)

    def test_entrada_apenas_estimada_quando_o_sinal_so_se_aproxima(self):
        indicadores = self._indicadores(corpo=-0.0012)
        alerta = detectar_reversao("EURUSD", indicadores)
        self.assertFalse(alerta.entrada_confirmada)
        self.assertAlmostEqual(alerta.preco_entrada, float(indicadores.iloc[-1]["Close"]))

    def test_texto_de_entrada_distingue_confirmada_de_estimada(self):
        confirmada = detectar_reversao("EURUSD", self._indicadores(corpo=-0.002))
        estimada = detectar_reversao("EURUSD", self._indicadores(corpo=-0.0012))
        self.assertIn("abertura da vela atual", confirmada.texto_entrada())
        self.assertIn("estimada", estimada.texto_entrada())


class TestExplicacao(unittest.TestCase):
    def setUp(self):
        indice = pd.date_range("2026-08-03 10:00", periods=3, freq="5min")
        self.indicadores = pd.DataFrame({"TendenciaMacro": ["lateral"] * 3}, index=indice)

    def test_explica_reversao_bollinger(self):
        decisao = Decisao(
            ativo="EURUSD", direcao="call", preco=1.1, candle_hora=pd.Timestamp("2026-08-03 10:05"),
            motivo="retorno_bollinger_rsi_m5",
            detalhes={"setup": "reversao_bollinger_rsi", "rsi_estirado": 25.0, "rsi_confirmacao": 35.0},
        )
        motivos = explicar_decisao(decisao, self.indicadores)
        self.assertTrue(any("Bollinger" in m for m in motivos))
        self.assertTrue(any("25" in m and "35" in m for m in motivos))

    def test_explica_pullback_com_fatores(self):
        decisao = Decisao(
            ativo="EURUSD", direcao="call", preco=1.1, candle_hora=pd.Timestamp("2026-08-03 10:05"),
            motivo="pullback_tendencia_m5",
            detalhes={"setup": "pullback", "fatores": ["fibo", "suporte"], "rsi_confirmacao": 52.0},
        )
        motivos = explicar_decisao(decisao, self.indicadores)
        self.assertTrue(any("Fibonacci" in m and "suporte" in m for m in motivos))


class TestCalendarioEconomico(unittest.TestCase):
    def setUp(self):
        self.pasta = Path(tempfile.mkdtemp())
        self.agora = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    def _calendario(self, eventos):
        calendario = CalendarioEconomico(self.pasta)
        calendario._eventos = calendario._converter(eventos)
        calendario._baixado_em = 9e18  # evita rede durante o teste
        return calendario

    def _evento(self, minutos, moeda="USD", impacto="High", titulo="Non-Farm Payrolls"):
        quando = self.agora + timedelta(minutes=minutos)
        return {"title": titulo, "country": moeda, "date": quando.isoformat(), "impact": impacto}

    def test_evento_proximo_entra_na_janela_de_risco(self):
        calendario = self._calendario([self._evento(10)])
        self.assertEqual(len(calendario.janela_de_risco("EURUSD", self.agora)), 1)

    def test_evento_distante_fica_fora_da_janela(self):
        calendario = self._calendario([self._evento(120)])
        self.assertEqual(calendario.janela_de_risco("EURUSD", self.agora), [])

    def test_evento_de_outra_moeda_e_ignorado(self):
        calendario = self._calendario([self._evento(10, moeda="AUD")])
        self.assertEqual(calendario.janela_de_risco("EURUSD", self.agora), [])

    def test_impacto_baixo_e_ignorado(self):
        calendario = self._calendario([self._evento(10, impacto="Low")])
        self.assertEqual(calendario.janela_de_risco("EURUSD", self.agora), [])

    def test_ativo_sintetico_nunca_recebe_noticia(self):
        """OTC tem preço gerado por algoritmo; notícia real não o move."""
        calendario = self._calendario([self._evento(10)])
        self.assertEqual(calendario.eventos_do_ativo("EURUSD-OTC"), [])
        self.assertIsNone(calendario.aviso("EURUSD-OTC", self.agora))

    def test_aviso_descreve_evento_iminente(self):
        calendario = self._calendario([self._evento(10)])
        self.assertIn("Non-Farm Payrolls", calendario.aviso("EURUSD", self.agora))

    def test_sem_evento_relevante_nao_ha_aviso(self):
        self.assertIsNone(self._calendario([]).aviso("EURUSD", self.agora))

    def test_cache_em_disco_e_usado_quando_a_rede_falha(self):
        arquivo = self.pasta / "calendario_economico.json"
        arquivo.write_text(json.dumps([self._evento(10)]), encoding="utf-8")
        calendario = CalendarioEconomico(self.pasta, url="http://127.0.0.1:1/inexistente")
        self.assertTrue(calendario.atualizar())
        self.assertEqual(len(calendario.eventos_do_ativo("EURUSD")), 1)

    def test_sem_rede_e_sem_cache_degrada_sem_quebrar(self):
        calendario = CalendarioEconomico(self.pasta, url="http://127.0.0.1:1/inexistente")
        self.assertFalse(calendario.atualizar())
        self.assertIsNone(calendario.aviso("EURUSD", self.agora))


class TestIntegracaoAlertaNoticia(unittest.TestCase):
    def test_anexar_noticia_preenche_o_campo(self):
        agora = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        calendario = CalendarioEconomico(Path(tempfile.mkdtemp()))
        calendario._eventos = calendario._converter(
            [{"title": "CPI", "country": "USD", "date": (agora + timedelta(minutes=5)).isoformat(), "impact": "High"}]
        )
        calendario._baixado_em = 9e18
        alerta = Alerta(
            ativo="EURUSD", tipo="entrada", direcao="call", preco=1.1,
            hora=pd.Timestamp("2026-08-03 11:55"), origem="reversao_candle", motivos=["teste"],
        )
        com_noticia = anexar_noticia(alerta, calendario, agora)
        self.assertIn("CPI", com_noticia.noticia)
        self.assertEqual(com_noticia.motivos, ["teste"])

    def test_para_grafico_aceita_alerta_ausente(self):
        self.assertIsNone(para_grafico(None, lambda valor: 0))


if __name__ == "__main__":
    unittest.main()
