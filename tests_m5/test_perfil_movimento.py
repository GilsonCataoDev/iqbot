"""O perfil separa magnitude de direcao; misturar as duas e o erro classico."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from iqoption_m5 import perfil_movimento as pm


def _serie(fechamentos, amplitude=0.0):
    """Velas sinteticas com High/Low simetricos em torno do Close."""
    idx = pd.date_range("2026-01-01", periods=len(fechamentos), freq="15min")
    c = pd.Series(list(fechamentos), index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "Close": c,
                         "High": c + amplitude, "Low": c - amplitude},
                        index=idx)


class TestMagnitude(unittest.TestCase):
    def test_serie_curta_devolve_n_zero_em_vez_de_estourar(self):
        self.assertEqual(pm.magnitude(_serie([1, 2, 3]), 24)["n"], 0)

    def test_amplitude_mede_topo_a_fundo_da_janela(self):
        # Vaivem de 1%: toda janela de 2 velas contem um 100 e um 101.
        df = _serie([100, 101] * 20)
        m = pm.magnitude(df, 2)
        self.assertAlmostEqual(m["amplitude_pct"]["p50"], 1.0, places=1)
        # E o preco nao sai do lugar: deslocamento praticamente zero.
        self.assertLess(m["deslocamento_pct"]["p50"], 0.2)

    def test_preco_parado_da_amplitude_zero(self):
        m = pm.magnitude(_serie([100] * 60), 24)
        self.assertEqual(m["amplitude_pct"]["p50"], 0.0)

    def test_janela_nao_inclui_a_propria_vela_da_ancora(self):
        """Ler o High da vela da decisao seria olhar preco que nao fechou."""
        df = _serie([100] * 3)
        df.loc[df.index[0], "High"] = 200.0   # pico so na PRIMEIRA vela
        m = pm.magnitude(df, 1)
        # A ancora 0 olha a vela 1 em diante. Se a janela incluisse a propria
        # vela da ancora, ela veria os 100% do proprio topo.
        self.assertEqual(m["amplitude_pct"]["p90"], 0.0)

    def test_excursao_contra_e_a_queda_e_nao_a_amplitude(self):
        # Cai 2% e depois sobe: quem comprou no fechamento sofre os 2% antes
        # de a alta chegar. A amplitude sozinha esconderia isso.
        df = _serie([100] + [98] * 2 + [105] * 3)
        m = pm.magnitude(df, 2)
        self.assertGreaterEqual(m["excursao_contra_pct"]["p90"], 1.0)


class TestDirecao(unittest.TestCase):
    def test_tendencia_perfeita_e_detectada_como_memoria(self):
        df = _serie(list(range(100, 300)))
        d = pm.direcao_tem_memoria(df, 4)
        self.assertEqual(d["taxa"], 100.0)
        self.assertGreater(d["ic_95"][0], 50)

    def test_zigue_zague_perfeito_aparece_como_reversao(self):
        base = [100, 104] * 100
        d = pm.direcao_tem_memoria(_serie(base), 1)
        self.assertEqual(d["taxa"], 0.0)
        self.assertLess(d["ic_95"][1], 50)

    def test_serie_curta_nao_inventa_taxa(self):
        self.assertEqual(pm.direcao_tem_memoria(_serie([1, 2, 3]), 24)["n"], 0)

    def test_ruido_puro_nao_vira_veredito(self):
        rng = np.random.default_rng(7)
        passos = rng.normal(0, 1, 4000).cumsum() + 1000
        d = pm.direcao_tem_memoria(_serie(passos), 24)
        ic = d["ic_95"]
        self.assertLess(ic[0], 50)
        self.assertGreater(ic[1], 50)


class TestWilson(unittest.TestCase):
    def test_sem_amostra_devolve_none(self):
        self.assertIsNone(pm.wilson(0, 0))

    def test_amostra_pequena_da_intervalo_largo(self):
        curto, longo = pm.wilson(5, 10), pm.wilson(500, 1000)
        self.assertGreater(curto[1] - curto[0], longo[1] - longo[0])

    def test_intervalo_nao_escapa_de_0_a_100(self):
        ic = pm.wilson(10, 10)
        self.assertLessEqual(ic[1], 100.0)
        self.assertGreaterEqual(pm.wilson(0, 10)[0], 0.0)


class TestPorHora(unittest.TestCase):
    def test_agrupa_por_hora_do_indice(self):
        ph = pm.por_hora(_serie([100] * 200, amplitude=0.5), 4)
        self.assertTrue(ph)
        self.assertTrue(all(0 <= h <= 23 for h in ph))


if __name__ == "__main__":
    unittest.main()
