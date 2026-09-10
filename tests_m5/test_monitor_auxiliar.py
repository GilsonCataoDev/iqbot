import unittest
import uuid

import pandas as pd
from pathlib import Path

from iqoption_m5.monitor_auxiliar import (
    atualizar_ativo_auxiliar,
    avaliar_combos,
    contexto_fibo,
    detectar_vela,
    indicadores_auxiliares,
)
from iqoption_m5.grafico import GraficoM5


def candles(valores):
    indice = pd.date_range("2026-09-09", periods=len(valores), freq="15min")
    return pd.DataFrame(
        [{"Open": o, "High": max(o, c) + .2, "Low": min(o, c) - .2, "Close": c, "Volume": 10}
         for o, c in valores], index=indice
    )


class TestMonitorAuxiliar(unittest.TestCase):
    def test_template_auxiliar_recebe_script_de_marcacoes_compartilhado(self):
        from tempfile import TemporaryDirectory
        from iqoption_m5.config import Configuracao
        with TemporaryDirectory() as temp:
            origem = Path(temp) / "auxiliar"
            origem.mkdir()
            (origem / "index.html").write_text("<html></html>", encoding="utf-8")
            grafico = GraficoM5(Configuracao(
                pasta_dados=Path(temp), porta_grafico=8797,
                sufixo_banco="teste_aux_" + uuid.uuid4().hex,
            ), origem)
            try:
                grafico.iniciar(abrir_navegador=False)
                self.assertTrue((grafico.pasta_web / "marcacoes.js").exists())
            finally:
                grafico.fechar()

    def test_erro_de_um_timeframe_nao_impede_payload_do_ativo(self):
        class MercadoComFalha:
            def snapshot_timeframe(self, ativo, tf):
                if tf == 900:
                    raise RuntimeError("stream M15 caiu")
                return type("Snap", (), {"candles": candles([(100, 101)] * 30)})()

        resultado = atualizar_ativo_auxiliar(MercadoComFalha(), "EURUSD", [])
        self.assertIn("candles", resultado["300"])
        self.assertIn("erro", resultado["900"])

    def test_engolfo_alta_e_reconhecido(self):
        df = candles([(10, 10.1), (10.2, 10.0), (9.9, 10.4), (10.4, 10.45)])
        leitura = detectar_vela(df)
        self.assertEqual(leitura["tipo"], "ENGOLFO DE ALTA")
        self.assertEqual(leitura["direcao"], "CALL")

    def test_fibo_manual_mede_zona_50_a_618_do_impulso(self):
        fibo = contexto_fibo(
            [{"tipo": "fibo", "preco_a": 100.0, "preco_b": 110.0}], 104.5
        )
        self.assertEqual(fibo["direcao_impulso"], "ALTA")
        self.assertAlmostEqual(fibo["niveis"]["0.618"], 103.82)
        self.assertTrue(fibo["zona50_618"]["dentro"])

    def test_fibo_correcao_exige_zona_e_confirmacao(self):
        df = candles([(100 + i * .2, 100.1 + i * .2) for i in range(28)])
        df.iloc[-2, df.columns.get_loc("Open")] = 105.1
        df.iloc[-2, df.columns.get_loc("Close")] = 105.6
        df.iloc[-2, df.columns.get_loc("Low")] = 104.4
        ind = indicadores_auxiliares(df)
        vela = {"direcao": "CALL", "forca": 2}
        fibo = {"zona50_618": {"dentro": True}}
        combos = {c["id"]: c for c in avaliar_combos(ind, vela, fibo)}
        self.assertTrue(combos["fibo_correcao"]["ativo"])

    def test_alta_sem_nenhuma_perda_marca_rsi_100_e_cala_o_pullback(self):
        """RSI 50 na alta esticada passaria no gate 40–65 e acenderia o combo."""
        df = candles([(100 + i * .5, 100.5 + i * .5) for i in range(20)])
        ind = indicadores_auxiliares(df)

        self.assertEqual(ind.iloc[-2].rsi, 100)
        combos = {c["id"]: c for c in avaliar_combos(ind, {"direcao": "CALL", "forca": 3}, None)}
        self.assertFalse(combos["ema_pullback"]["ativo"])

    def test_baixa_sem_nenhum_ganho_marca_rsi_zero(self):
        df = candles([(100 - i * .5, 99.5 - i * .5) for i in range(20)])

        self.assertEqual(indicadores_auxiliares(df).iloc[-2].rsi, 0)

    def test_sem_fibo_nao_ativa_combo_de_correcao(self):
        df = indicadores_auxiliares(candles([(100 + i * .2, 100.1 + i * .2) for i in range(28)]))
        combos = {c["id"]: c for c in avaliar_combos(df, {"direcao": "CALL", "forca": 3}, None)}
        self.assertFalse(combos["fibo_correcao"]["ativo"])
