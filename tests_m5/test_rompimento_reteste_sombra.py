"""Rompimento+reteste do forex observado no Monitor.

A estratégia vive em forex_estrategia.py e não tinha amostra nenhuma ao vivo:
os processos que a executam não rodam. Aqui ela entra em sombra, usando a
máquina de aferição que o Monitor já tem — sem virar ordem.
"""
from __future__ import annotations

import threading
import unittest

import pandas as pd

import monitor_mercado as M


def _velas(n=30, inicio=100.0, passo=0.0):
    idx = pd.date_range("2026-09-11 12:00", periods=n, freq="15min", tz="UTC")
    precos = [inicio + passo * i for i in range(n)]
    return pd.DataFrame(
        {"Open": precos, "High": [p + 0.2 for p in precos],
         "Low": [p - 0.2 for p in precos], "Close": precos},
        index=idx,
    )


def _estado(aprendizado=None):
    e = M.Estado.__new__(M.Estado)
    e._lock = threading.Lock()
    e._vistos = set()
    e._aprendizado = aprendizado or []
    e._salvar_aprendizado = lambda: None
    return e


class TestTraducaoDoPlano(unittest.TestCase):
    def test_sem_plano_devolve_none(self):
        """Mercado sem rompimento não pode inventar sinal."""
        self.assertIsNone(
            M._plano_rompimento_reteste_sombra("EURUSD", _velas(120), 0.001)
        )

    def test_dados_insuficientes_nao_quebram(self):
        self.assertIsNone(
            M._plano_rompimento_reteste_sombra("EURUSD", _velas(5), 0.001)
        )

    def test_atr_ausente_nao_quebra(self):
        self.assertIsNone(
            M._plano_rompimento_reteste_sombra("EURUSD", _velas(120), None)
        )


class TestResolucao(unittest.TestCase):
    """O simulador precisa tratar a entrada como mercado, não como pendente."""

    def setUp(self):
        self.h = {
            "tipo": "rompimento_reteste", "direcao": "buy",
            "vela": str(_velas(1).index[0]),
            "alvos_estudo": {"entrada": 100.0, "sl": 99.0, "tp1": 102.0},
        }

    def test_entrada_a_mercado_conta_como_preenchida(self):
        sim = M.Estado._resolver_tp_sl(self.h, _velas(30, 100.5, 0.15))
        self.assertTrue(sim["preenchida"],
                        "sem modo_entrada o simulador trata como mercado")

    def test_alvo_rende_o_rr_planejado(self):
        sim = M.Estado._resolver_tp_sl(self.h, _velas(30, 100.5, 0.15))
        self.assertEqual(sim["desfecho"], "win_tp1")
        self.assertEqual(sim["resultado_r"], 2.0)

    def test_stop_rende_menos_um(self):
        sim = M.Estado._resolver_tp_sl(self.h, _velas(30, 99.6, -0.1))
        self.assertEqual(sim["desfecho"], "loss_sl")
        self.assertEqual(sim["resultado_r"], -1.0)


class TestAmostraSeparada(unittest.TestCase):
    def test_conta_so_o_proprio_tipo(self):
        e = _estado([
            {"tipo": "rompimento_reteste",
             "simulacao": {"desfecho": "win_tp1", "resultado_r": 2.0}},
            {"tipo": "rompimento_reteste",
             "simulacao": {"desfecho": "loss_sl", "resultado_r": -1.0}},
            {"tipo": "fibo_m15",
             "simulacao": {"desfecho": "win_tp1", "resultado_r": 1.0}},
        ])
        r = e._amostra_rompimento_reteste()
        self.assertEqual(r["sinais"], 2, "nao pode recolher os outros estudos")
        self.assertEqual((r["wins"], r["losses"]), (1, 1))
        self.assertEqual(r["saldo_r"], 1.0)

    def test_traz_a_comparacao_de_horizonte(self):
        r = _estado()._amostra_rompimento_reteste()
        self.assertIn("comparacao_longa", r)
        self.assertEqual(r["horizonte"], "6h")

    def test_sem_sinal_nao_quebra(self):
        r = _estado()._amostra_rompimento_reteste()
        self.assertEqual(r["sinais"], 0)
        self.assertEqual(r["maturidade"], "INSUFICIENTE")


if __name__ == "__main__":
    unittest.main()
