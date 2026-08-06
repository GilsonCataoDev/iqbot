import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from iqoption_m5.config import Configuracao
from iqoption_m5.estrategia import EstrategiaReversaoM5
from iqoption_m5.grafico import GraficoM5
from iqoption_m5.modelos import Decisao, SnapshotMercado


class TestGraficoM5(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Configuracao(pasta_dados=Path(self.temp.name))
        self.idx = pd.date_range("2026-01-01", periods=120, freq="5min")
        self.candles = pd.DataFrame(
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 10.0},
            index=self.idx,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_payload_contem_camadas_e_estado(self):
        estrategia = EstrategiaReversaoM5(self.config)
        indicadores = estrategia.calcular_indicadores(self.candles)
        snapshot = SnapshotMercado("EURUSD", self.candles, 0.85, True, 1_800_000_000)
        dados = GraficoM5(self.config).montar_dados(snapshot, indicadores, [], None, [])
        self.assertEqual(dados["par"], "EURUSD")
        self.assertEqual(dados["timeframe"], "M5")
        self.assertEqual(len(dados["candles"]), 120)
        self.assertTrue(dados["mercadoAberto"])
        self.assertAlmostEqual(dados["payout"], 0.85)
        self.assertTrue(dados["bandaSup"])
        self.assertTrue(dados["rsi"])
        self.assertEqual(len(dados["niveis"]), 2)
        self.assertEqual(len(dados["fib"]), 7)

    def test_sinais_historicos_nao_usam_candle_em_formacao(self):
        sinais = EstrategiaReversaoM5(self.config).sinais_historicos("EURUSD", self.candles)
        self.assertTrue(all(s.candle_hora < self.idx[-1] for s in sinais))

    def test_pullback_fibo_suporte_aparece_como_confluencia(self):
        estrategia = EstrategiaReversaoM5(self.config)
        indicadores = estrategia.calcular_indicadores(self.candles)
        snapshot = SnapshotMercado("EURUSD-OTC", self.candles, 0.90, True, 1_800_000_000)
        sinal = Decisao(
            ativo="EURUSD-OTC",
            direcao="call",
            preco=100.0,
            candle_hora=self.idx[-2],
            motivo="pullback_tendencia_m5",
            detalhes={"setup": "pullback", "fatores": ["fibo", "suporte"]},
        )
        dados = GraficoM5(self.config).montar_dados(snapshot, indicadores, [sinal], None, [])
        self.assertEqual(dados["sinais"], [])
        self.assertEqual(len(dados["pullbacks"]), 1)
        self.assertEqual(len(dados["confluencias"]), 1)
        self.assertEqual(dados["confluencias"][0]["fatores"], ["fibo", "suporte"])

    def test_servidor_publica_manifesto(self):
        grafico = GraficoM5(self.config)
        url = grafico.iniciar(abrir_navegador=False)
        try:
            manifesto_url = url.split("/index.html", 1)[0] + "/iqoption_m5/manifest.json"
            with urllib.request.urlopen(manifesto_url, timeout=3) as resposta:
                conteudo = resposta.read().decode("utf-8")
            self.assertIn("EURUSD", conteudo)
            self.assertIn("EURUSD-OTC", conteudo)
            self.assertIn("GBPUSD-OTC", conteudo)
            self.assertIn("USDJPY-OTC", conteudo)
        finally:
            grafico.fechar()

    def test_dados_vivos_nao_sao_gravados_dentro_do_onedrive_do_projeto(self):
        grafico = GraficoM5(self.config)
        pasta_projeto = Path(__file__).resolve().parent.parent / "grafico_web"
        substituir_real = __import__("os").replace

        def bloquear_onedrive(origem, destino):
            if Path(destino).resolve().is_relative_to(pasta_projeto.resolve()):
                raise PermissionError(5, "Acesso negado pelo OneDrive", str(destino))
            substituir_real(origem, destino)

        with patch("iqoption_m5.grafico.os.replace", side_effect=bloquear_onedrive):
            url = grafico.iniciar(abrir_navegador=False)
        try:
            self.assertNotEqual(grafico.pasta_web.resolve(), pasta_projeto.resolve())
            with urllib.request.urlopen(url, timeout=3) as resposta:
                self.assertIn("IQ Option M5", resposta.read().decode("utf-8"))
        finally:
            grafico.fechar()


if __name__ == "__main__":
    unittest.main()
