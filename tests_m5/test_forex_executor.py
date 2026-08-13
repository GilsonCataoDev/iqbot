import unittest
from datetime import datetime

import pandas as pd

from iqoption_m5.forex_estrategia import (
    plano_rompimento_reteste,
    planos_correcao_fibo_sr,
    planos_rompimento_reteste,
    planos_toque_lta_ltb,
)
from iqoption_m5.forex_executor import ExecutorForexSimulado
from iqoption_m5.forex_modelos import PlanoForex


class TestExecutorForexSimulado(unittest.TestCase):
    def _plano(self):
        return PlanoForex(
            ativo="EURUSD",
            lado="buy",
            sinal_em=datetime(2026, 1, 1),
            nivel=1.1000,
            stop=1.0990,
            alvo=1.1020,
            risco_preco=0.0010,
            motivo="teste",
        )

    def test_dimensiona_posicao_pelo_risco_da_banca(self):
        executor = ExecutorForexSimulado(banca=1000, risco_percentual=0.01, spread=0)
        posicao = executor.abrir(self._plano(), 1.1000, datetime(2026, 1, 1, 0, 5))

        self.assertAlmostEqual(posicao.risco_dinheiro, 10.0)
        self.assertAlmostEqual(posicao.quantidade, 10_000.0)

    def test_se_stop_e_alvo_ocorrem_no_mesmo_candle_assume_stop(self):
        executor = ExecutorForexSimulado(banca=1000, risco_percentual=0.01, spread=0)
        executor.abrir(self._plano(), 1.1000, datetime(2026, 1, 1, 0, 5))

        resultado = executor.atualizar(datetime(2026, 1, 1, 0, 10), 1.1030, 1.0980)

        self.assertEqual(resultado.motivo_saida, "stop")
        self.assertAlmostEqual(resultado.lucro, -10.0)

    def test_executor_rejeita_risco_acima_de_dois_porcento(self):
        with self.assertRaises(ValueError):
            ExecutorForexSimulado(risco_percentual=0.03)


class TestEstrategiaForex(unittest.TestCase):
    def test_sinal_atual_nao_muda_quando_futuro_muda(self):
        indice = pd.date_range("2026-01-01", periods=100, freq="5min")
        close = pd.Series([1.10 + i * 0.00001 for i in range(100)], index=indice)
        candles = pd.DataFrame(
            {
                "Open": close - 0.00002,
                "High": close + 0.00005,
                "Low": close - 0.00005,
                "Close": close,
            },
            index=indice,
        )
        antes = plano_rompimento_reteste("EURUSD", candles.iloc[:80])
        alterado = candles.copy()
        alterado.iloc[80:, :] *= 2

        depois = plano_rompimento_reteste("EURUSD", alterado.iloc[:80])

        self.assertEqual(antes, depois)

    def test_versao_linear_equivale_ao_calculo_do_ultimo_candle(self):
        indice = pd.date_range("2026-01-01", periods=100, freq="5min")
        close = pd.Series([1.10 + i * 0.00001 for i in range(100)], index=indice)
        candles = pd.DataFrame(
            {
                "Open": close - 0.00002,
                "High": close + 0.00005,
                "Low": close - 0.00005,
                "Close": close,
            },
            index=indice,
        )

        pontual = plano_rompimento_reteste("EURUSD", candles)
        linear = planos_rompimento_reteste("EURUSD", candles).iloc[-1]

        self.assertEqual(pontual, linear)

    def test_lta_nao_usa_pivo_antes_da_confirmacao(self):
        indice = pd.date_range("2026-01-01", periods=80, freq="5min")
        base = pd.Series([1.10 + i * 0.00001 for i in range(80)], index=indice)
        candles = pd.DataFrame(
            {
                "Open": base,
                "High": base + 0.0003,
                "Low": base - 0.0003,
                "Close": base + 0.00005,
            },
            index=indice,
        )
        candles.iloc[40, candles.columns.get_loc("Low")] = 1.0950

        planos = planos_toque_lta_ltb("EURUSD", candles, raio_pivo=2)

        # O fundo em 40 só pode participar da linha a partir do fechamento de 42.
        alterado = candles.copy()
        alterado.iloc[41:, :] *= 2
        planos_alterados = planos_toque_lta_ltb("EURUSD", alterado, raio_pivo=2)
        self.assertEqual(planos.iloc[40], planos_alterados.iloc[40])

    def test_correcao_fibo_nao_muda_com_candles_futuros(self):
        indice = pd.date_range("2026-01-01", periods=160, freq="5min")
        close = pd.Series([1.10 + i * 0.00001 for i in range(160)], index=indice)
        candles = pd.DataFrame(
            {"Open": close, "High": close + 0.0002, "Low": close - 0.0002, "Close": close},
            index=indice,
        )
        antes = planos_correcao_fibo_sr("EURUSD", candles.iloc[:120]).iloc[-1]
        alterado = candles.copy()
        alterado.iloc[120:, :] *= 2

        depois = planos_correcao_fibo_sr("EURUSD", alterado.iloc[:120]).iloc[-1]

        self.assertEqual(antes, depois)


if __name__ == "__main__":
    unittest.main()
