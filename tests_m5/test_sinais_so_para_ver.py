"""Sinais desenhados com o mercado fechado: só para ver, nunca para operar."""
from __future__ import annotations

import unittest

import pandas as pd

from iqoption_m5 import laboratorio_ema as lab


class _Decisao:
    def __init__(self, candle_hora, setup="ema920_pullback", direcao="call"):
        self.candle_hora = candle_hora
        self.direcao = direcao
        self.preco = 1.08
        self.detalhes = {"setup": setup}


class _Estrategia:
    """Devolve um sinal no candle pedido, para exercitar o acúmulo."""

    def __init__(self, por_candle=None):
        self.chamadas = 0
        self._por_candle = por_candle or {}

    def avaliar_todas(self, ativo, indicadores):
        self.chamadas += 1
        return self._por_candle.get(indicadores.index[-2], [])


class _Snapshot:
    def __init__(self, ativo="EURUSD", aberto=False):
        self.ativo = ativo
        self.mercado_aberto = aberto


def _indicadores(n, fim=None):
    idx = pd.date_range(end=fim or "2026-09-07 15:00", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({"Close": [1.08] * n}, index=idx)


class TestSinaisSoParaVer(unittest.TestCase):
    def setUp(self):
        lab._sinais_fechado_cache.clear()
        self.addCleanup(lab._sinais_fechado_cache.clear)

    def test_mercado_aberto_nao_desenha_nada(self):
        ind = _indicadores(50)
        est = _Estrategia({ind.index[-2]: [_Decisao(ind.index[-2])]})
        self.assertEqual(
            lab._sinais_so_para_ver(est, _Snapshot(aberto=True), ind, "ema920_pullback"), []
        )
        self.assertEqual(est.chamadas, 0, "nao deve avaliar com mercado aberto")

    def test_marca_como_bloqueado_e_explica_o_motivo(self):
        ind = _indicadores(50)
        alvo = ind.index[-2]
        est = _Estrategia({alvo: [_Decisao(alvo)]})
        saida = lab._sinais_so_para_ver(est, _Snapshot(), ind, "ema920_pullback")
        self.assertEqual(len(saida), 1)
        self.assertEqual(saida[0].detalhes["status_grafico"], "bloqueado")
        self.assertIn("mercado fechado", saida[0].detalhes["razao"][-1])

    def test_filtra_setup_de_outro_rastro(self):
        ind = _indicadores(50)
        alvo = ind.index[-2]
        est = _Estrategia({alvo: [_Decisao(alvo, setup="outro_setup")]})
        self.assertEqual(
            lab._sinais_so_para_ver(est, _Snapshot(), ind, "ema920_pullback"), []
        )

    def test_nao_reavalia_no_mesmo_candle(self):
        ind = _indicadores(50)
        est = _Estrategia()
        for _ in range(5):
            lab._sinais_so_para_ver(est, _Snapshot(), ind, "ema920_pullback")
        self.assertEqual(est.chamadas, 1, "o laco roda a cada 1s; avaliar so no candle novo")

    def test_acumula_ao_longo_dos_candles(self):
        """avaliar_todas ve so o ultimo fechado; o dia inteiro vem do acumulo."""
        base = _indicadores(50)
        alvos = [base.index[-2], base.index[-1]]
        est = _Estrategia({a: [_Decisao(a)] for a in alvos})

        primeira = lab._sinais_so_para_ver(est, _Snapshot(), base, "ema920_pullback")
        self.assertEqual(len(primeira), 1)

        # Chega candle novo: o anterior precisa continuar desenhado.
        seguinte = _indicadores(51, fim="2026-09-07 15:05")
        segunda = lab._sinais_so_para_ver(est, _Snapshot(), seguinte, "ema920_pullback")
        self.assertEqual(len(segunda), 2, "sinal antigo nao pode sumir do grafico")

    def test_descarta_o_que_saiu_da_janela(self):
        ind = _indicadores(50)
        antigo = _Decisao(ind.index[0] - pd.Timedelta(hours=5))
        lab._sinais_fechado_cache["EURUSD"] = (None, [antigo])
        saida = lab._sinais_so_para_ver(_Estrategia(), _Snapshot(), ind, "ema920_pullback")
        self.assertEqual(saida, [], "fora da janela desenhada nao pode acumular para sempre")

    def test_reabertura_limpa_o_acumulado(self):
        ind = _indicadores(50)
        alvo = ind.index[-2]
        est = _Estrategia({alvo: [_Decisao(alvo)]})
        lab._sinais_so_para_ver(est, _Snapshot(), ind, "ema920_pullback")
        self.assertIn("EURUSD", lab._sinais_fechado_cache)

        lab._sinais_so_para_ver(est, _Snapshot(aberto=True), ind, "ema920_pullback")
        self.assertNotIn("EURUSD", lab._sinais_fechado_cache,
                         "com mercado aberto o banco ja traz as decisoes reais")

    def test_estrategia_que_estoura_nao_derruba_o_grafico(self):
        class Explode:
            def avaliar_todas(self, ativo, indicadores):
                raise RuntimeError("indicador faltando")

        self.assertEqual(
            lab._sinais_so_para_ver(Explode(), _Snapshot(), _indicadores(50), "x"), []
        )

    def test_sem_setup_nao_avalia(self):
        est = _Estrategia()
        self.assertEqual(
            lab._sinais_so_para_ver(est, _Snapshot(), _indicadores(50), None), []
        )
        self.assertEqual(est.chamadas, 0)


if __name__ == "__main__":
    unittest.main()
