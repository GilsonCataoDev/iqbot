"""O paper CFD existe para reprovar estudo que so parece bom sem custo."""
from __future__ import annotations

import unittest

from iqoption_m5 import forex_paper as fp


class TestSpread(unittest.TestCase):
    def test_par_tabelado_usa_valor_absoluto(self):
        self.assertEqual(fp.spread_de("EURUSD", 1.16), 0.00010)

    def test_cripto_e_proporcional_ao_preco(self):
        # Um spread fixo de forex em BTC erraria por ordem de grandeza.
        self.assertAlmostEqual(fp.spread_de("BTCUSD", 80_000.0), 40.0)

    def test_ativo_desconhecido_nao_vira_spread_zero(self):
        """Faltar na tabela nao pode virar 'de graca' em silencio."""
        self.assertGreater(fp.spread_de("XPTUSD", 1000.0), 0)


class TestAvaliar(unittest.TestCase):
    def test_custo_e_um_spread_por_ida_e_volta(self):
        v = fp.avaliar("EURUSD", 1.0000, 0.9990, 1.0)   # risco 10 pips
        self.assertAlmostEqual(v.custo_em_r, 0.1, places=3)
        self.assertAlmostEqual(v.r_com_custo, 0.9, places=3)

    def test_stop_curto_faz_o_spread_pesar_muito_mais(self):
        curto = fp.avaliar("EURUSD", 1.0000, 0.99980, 1.0)  # risco 2 pips
        longo = fp.avaliar("EURUSD", 1.0000, 0.99800, 1.0)  # risco 20 pips
        self.assertGreater(curto.custo_em_r, longo.custo_em_r)
        self.assertAlmostEqual(curto.custo_em_r, 0.5, places=2)

    def test_stop_menor_que_spread_e_marcado_como_inoperavel(self):
        """Nao e 'R ruim': e ordem que nasce alem do stop."""
        v = fp.avaliar("CADCHF", 0.5858, 0.585679, 1.5)  # risco 1.2 pips
        self.assertFalse(v.operavel)
        self.assertGreater(v.custo_em_r, 1.0)

    def test_stop_folgado_e_operavel(self):
        self.assertTrue(fp.avaliar("EURUSD", 1.0000, 0.9990, 1.0).operavel)

    def test_o_custo_piora_tambem_o_prejuizo(self):
        """Perder -1R sem custo vira pior que -1R; o spread nao devolve nada."""
        v = fp.avaliar("EURUSD", 1.0000, 0.9990, -1.0)
        self.assertLess(v.r_com_custo, -1.0)

    def test_sem_preco_devolve_none_em_vez_de_chutar(self):
        self.assertIsNone(fp.avaliar("EURUSD", None, 0.999, 1.0))
        self.assertIsNone(fp.avaliar("EURUSD", 1.0, None, 1.0))
        self.assertIsNone(fp.avaliar("EURUSD", 1.0, 0.999, None))

    def test_stop_degenerado_devolve_none(self):
        self.assertIsNone(fp.avaliar("EURUSD", 1.16, 1.16, 1.0))

    def test_spread_explicito_vence_a_tabela(self):
        """A tabela e premissa; o spread real da conta manda."""
        v = fp.avaliar("EURUSD", 1.0000, 0.9990, 1.0, spread=0.0005)
        self.assertAlmostEqual(v.custo_em_r, 0.5, places=3)

    def test_contrato_por_classe_nao_aplica_100k_em_cripto(self):
        self.assertEqual(fp.unidades_de("EURUSD"), 100_000)
        self.assertEqual(fp.unidades_de("BTCUSD"), 1.0)
        self.assertEqual(fp.unidades_de("XAUUSD"), 100.0)

    def test_dinheiro_segue_o_lote_e_desconta_o_spread(self):
        v = fp.avaliar("EURUSD", 1.0000, 0.9990, 1.0, lote=0.1)
        # 1R = 10 pips = 0.0010; menos 1 pip de spread = 0.0009
        # 0.0009 * 100.000 * 0.1 = 9.00
        self.assertAlmostEqual(v.lucro_por_lote, 9.00, places=2)


if __name__ == "__main__":
    unittest.main()
