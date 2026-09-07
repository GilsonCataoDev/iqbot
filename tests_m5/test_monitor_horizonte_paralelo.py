"""Horizonte longo medido ao lado do oficial, nunca somado a ele.

Um trade fechado em 6h e outro em 12h são métodos diferentes. Medir os dois em
paralelo permite decidir com dado se vale trocar, sem invalidar a amostra já
acumulada sob a regra atual.
"""
from __future__ import annotations

import unittest

import monitor_mercado as M


class TestRotuloDoHorizonte(unittest.TestCase):
    def test_preserva_os_desfechos_ja_gravados(self):
        """24 velas de M15 tem de continuar produzindo '6h'."""
        self.assertEqual(M._rotulo_horas(M.HORIZONTE_VELAS), "6h")
        self.assertEqual(f"expirado_{M._rotulo_horas(24)}", "expirado_6h")
        self.assertEqual(f"nao_executada_{M._rotulo_horas(24)}", "nao_executada_6h")

    def test_horizonte_longo_tem_rotulo_proprio(self):
        self.assertEqual(M._rotulo_horas(M.HORIZONTE_LONGO_VELAS), "12h")

    def test_horizonte_quebrado_nao_vira_decimal_feio(self):
        self.assertEqual(M._rotulo_horas(2), "0.5h")


class TestResumoPorChave(unittest.TestCase):
    def setUp(self):
        self.itens = [
            {"simulacao": {"desfecho": "win_tp1", "resultado_r": 1.0},
             M.CHAVE_SIM_LONGA: {"desfecho": "loss_sl", "resultado_r": -1.0}},
            {"simulacao": {"desfecho": "expirado_6h", "resultado_r": 0.3},
             M.CHAVE_SIM_LONGA: {"desfecho": "win_tp1", "resultado_r": 1.0}},
        ]

    def test_le_a_chave_oficial_por_padrao(self):
        r = M.Estado._resumo_simulacoes(self.itens)
        self.assertEqual((r["wins"], r["losses"]), (1, 0))
        self.assertAlmostEqual(r["saldo_r"], 1.3)

    def test_le_a_chave_longa_quando_pedida(self):
        r = M.Estado._resumo_simulacoes(self.itens, M.CHAVE_SIM_LONGA)
        self.assertEqual((r["wins"], r["losses"]), (1, 1))
        self.assertAlmostEqual(r["saldo_r"], 0.0)

    def test_os_dois_horizontes_nao_se_somam(self):
        oficial = M.Estado._resumo_simulacoes(self.itens)
        longo = M.Estado._resumo_simulacoes(self.itens, M.CHAVE_SIM_LONGA)
        self.assertNotEqual(oficial["saldo_r"], longo["saldo_r"])
        self.assertEqual(oficial["avaliados_r"], 2)
        self.assertEqual(longo["avaliados_r"], 2, "cada um conta os proprios 2")

    def test_contagem_de_expirado_nao_fica_presa_em_6h(self):
        itens = [{"simulacao": {"desfecho": "expirado_12h"}},
                 {"simulacao": {"desfecho": "nao_executada_12h"}}]
        r = M.Estado._resumo_simulacoes(itens)
        self.assertEqual(r["expirados"], 1)
        self.assertEqual(r["nao_executadas"], 1)

    def test_chave_longa_ausente_nao_quebra(self):
        r = M.Estado._resumo_simulacoes(
            [{"simulacao": {"desfecho": "win_tp1", "resultado_r": 1.0}}],
            M.CHAVE_SIM_LONGA,
        )
        self.assertEqual(r["wins"], 0)
        self.assertEqual(r["avaliados_r"], 0)
        self.assertEqual(r["maturidade"], "INSUFICIENTE")


class TestAmostraPublicada(unittest.TestCase):
    def test_comparacao_vem_anexada_e_rotulada(self):
        estado = M.Estado.__new__(M.Estado)
        estado._aprendizado = [
            {"tipo": "fibo_m15",
             "simulacao": {"desfecho": "win_tp1", "resultado_r": 1.0},
             M.CHAVE_SIM_LONGA: {"desfecho": "loss_sl", "resultado_r": -1.0}},
        ]
        r = estado._amostra_por_tipo("fibo_m15")
        self.assertEqual(r["horizonte"], "6h")
        self.assertEqual(r["comparacao_longa"]["horizonte"], "12h")
        self.assertAlmostEqual(r["media_r"], 1.0)
        self.assertAlmostEqual(r["comparacao_longa"]["media_r"], -1.0)

    def test_tipo_sem_sinal_nao_quebra(self):
        estado = M.Estado.__new__(M.Estado)
        estado._aprendizado = []
        r = estado._amostra_por_tipo("orb_fvg_m15")
        self.assertEqual(r["sinais"], 0)
        self.assertEqual(r["maturidade"], "INSUFICIENTE")
        self.assertIn("comparacao_longa", r)


if __name__ == "__main__":
    unittest.main()
