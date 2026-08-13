import unittest

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import Configuracao
from iqoption_m5.modelos import Decisao


class EstrategiaFalsa:
    """Devolve sinais fixos, para testar só a resolução das operações."""

    def __init__(self, decisoes):
        self._decisoes = decisoes

    def sinais_historicos(self, ativo, candles):
        return self._decisoes


class TestEstatistica(unittest.TestCase):
    def test_breakeven_de_payout_85(self):
        self.assertAlmostEqual(backtest.breakeven(0.85), 0.5405, places=4)

    def test_payout_maior_reduz_breakeven(self):
        self.assertLess(backtest.breakeven(0.92), backtest.breakeven(0.85))

    def test_wilson_sem_amostra_e_zero(self):
        self.assertEqual(backtest.intervalo_wilson(0, 0), (0.0, 0.0))

    def test_wilson_encolhe_com_mais_amostra(self):
        inferior_pequeno, superior_pequeno = backtest.intervalo_wilson(6, 10)
        inferior_grande, superior_grande = backtest.intervalo_wilson(600, 1000)
        self.assertLess(superior_grande - inferior_grande, superior_pequeno - inferior_pequeno)

    def test_wilson_fica_dentro_de_zero_e_um(self):
        inferior, superior = backtest.intervalo_wilson(10, 10)
        self.assertGreaterEqual(inferior, 0.0)
        self.assertLessEqual(superior, 1.0)


class TestBacktestRealista(unittest.TestCase):
    def test_aplica_payout_spread_e_slippage(self):
        instante = pd.Timestamp("2026-03-02 10:00")
        operacao = backtest.Operacao(
            ativo="EURUSD",
            direcao="call",
            setup="teste",
            fatores="",
            hora_sinal=instante,
            hora_entrada=instante,
            preco_entrada=1.0,
            preco_saida=1.1,
            resultado="ganho",
        )
        simulador = backtest.BacktestRealista(
            backtest.CustoOperacao(payout=0.85, spread_pips=0.02, slippage_pips=0.01)
        )

        resumo = simulador.resumo([operacao], valor=1.0)

        self.assertAlmostEqual(resumo["lucro_total"], 0.82)


class TestSimulacao(unittest.TestCase):
    def setUp(self):
        self.config = Configuracao()
        self.indice = pd.date_range("2026-03-02 10:00", periods=4, freq="5min")
        self.candles = pd.DataFrame(
            {
                "Open": [100.0, 100.0, 100.0, 100.0],
                "High": [101.0, 101.0, 101.0, 101.0],
                "Low": [99.0, 99.0, 99.0, 99.0],
                "Close": [100.0, 100.0, 100.0, 100.0],
                "Volume": [1.0, 1.0, 1.0, 1.0],
            },
            index=self.indice,
        )

    def _decisao(self, posicao, direcao, setup="pullback", fatores=("fibo", "suporte")):
        return Decisao(
            ativo="EURUSD-OTC",
            direcao=direcao,
            preco=100.0,
            candle_hora=self.indice[posicao],
            motivo="teste",
            detalhes={"setup": setup, "fatores": list(fatores)},
        )

    def _simular(self, decisoes):
        return backtest.simular(
            self.config, "EURUSD-OTC", self.candles, EstrategiaFalsa(decisoes)
        )

    def test_call_ganha_quando_candle_seguinte_sobe(self):
        self.candles.loc[self.indice[1], "Close"] = 100.5
        operacoes = self._simular([self._decisao(0, "call")])
        self.assertEqual(operacoes[0].resultado, "ganho")

    def test_call_perde_quando_candle_seguinte_cai(self):
        self.candles.loc[self.indice[1], "Close"] = 99.5
        operacoes = self._simular([self._decisao(0, "call")])
        self.assertEqual(operacoes[0].resultado, "perda")

    def test_put_ganha_quando_candle_seguinte_cai(self):
        self.candles.loc[self.indice[1], "Close"] = 99.5
        operacoes = self._simular([self._decisao(0, "put")])
        self.assertEqual(operacoes[0].resultado, "ganho")

    def test_fechamento_igual_a_abertura_e_empate(self):
        operacoes = self._simular([self._decisao(0, "call")])
        self.assertEqual(operacoes[0].resultado, "empate")

    def test_entra_na_abertura_do_candle_seguinte(self):
        self.candles.loc[self.indice[1], "Open"] = 100.3
        operacoes = self._simular([self._decisao(0, "call")])
        self.assertEqual(operacoes[0].preco_entrada, 100.3)
        self.assertEqual(operacoes[0].hora_entrada, self.indice[1])

    def test_sinal_no_ultimo_candle_e_descartado(self):
        """Sem candle seguinte não há como saber o resultado; não pode virar operação."""
        operacoes = self._simular([self._decisao(len(self.indice) - 1, "call")])
        self.assertEqual(operacoes, [])

    def test_fatores_viram_texto_ordenado(self):
        self.candles.loc[self.indice[1], "Close"] = 100.5
        operacoes = self._simular([self._decisao(0, "call", fatores=("suporte", "fibo"))])
        self.assertEqual(operacoes[0].fatores, "fibo+suporte")


class TestResumo(unittest.TestCase):
    def _df(self, resultados):
        return pd.DataFrame(
            {
                "ativo": ["EURUSD-OTC"] * len(resultados),
                "direcao": ["call"] * len(resultados),
                "setup": ["pullback"] * len(resultados),
                "fatores": ["fibo"] * len(resultados),
                "hora_entrada": pd.date_range("2026-03-02 10:00", periods=len(resultados), freq="5min"),
                "resultado": resultados,
                "hora_dia": [10] * len(resultados),
            }
        )

    def test_empate_nao_conta_como_operacao_decidida(self):
        resumo = backtest._resumir(self._df(["ganho", "perda", "empate"]), 0.85)
        self.assertEqual(resumo["operacoes"], 2)
        self.assertEqual(resumo["empates"], 1)
        self.assertEqual(resumo["acerto_pct"], 50.0)

    def test_lucro_usa_o_payout(self):
        resumo = backtest._resumir(self._df(["ganho", "perda"]), 0.85)
        self.assertAlmostEqual(resumo["lucro_unidades"], -0.15, places=2)

    def test_tabela_por_grupo_respeita_minimo(self):
        df = self._df(["ganho", "perda"])
        self.assertTrue(backtest.tabela_por(df, "ativo", 0.85, minimo=5).empty)
        self.assertFalse(backtest.tabela_por(df, "ativo", 0.85, minimo=1).empty)

    def test_dataframe_vazio_tem_as_colunas_esperadas(self):
        df = backtest.para_dataframe([])
        self.assertIn("hora_dia", df.columns)
        self.assertTrue(df.empty)


class TestDivisaoTreinoTeste(unittest.TestCase):
    def _df(self, quantidade):
        return pd.DataFrame(
            {
                "hora_entrada": pd.date_range("2026-03-02 10:00", periods=quantidade, freq="5min"),
                "resultado": ["ganho"] * quantidade,
            }
        )

    def test_divide_na_fracao_pedida(self):
        treino, teste = backtest.dividir_treino_teste(self._df(100), 0.7)
        self.assertEqual(len(treino), 70)
        self.assertEqual(len(teste), 30)

    def test_teste_vem_depois_do_treino_no_tempo(self):
        """Corte cronológico: nenhuma operação do teste pode anteceder o treino."""
        treino, teste = backtest.dividir_treino_teste(self._df(100), 0.7)
        self.assertGreater(teste["hora_entrada"].min(), treino["hora_entrada"].max())

    def test_ordena_antes_de_cortar(self):
        df = self._df(100).sort_values("hora_entrada", ascending=False)
        treino, teste = backtest.dividir_treino_teste(df, 0.7)
        self.assertGreater(teste["hora_entrada"].min(), treino["hora_entrada"].max())


if __name__ == "__main__":
    unittest.main()
