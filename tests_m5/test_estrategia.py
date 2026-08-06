import unittest

import pandas as pd

from iqoption_m5.config import Configuracao
from iqoption_m5.estrategia import EstrategiaReversaoM5


class TestEstrategiaM5(unittest.TestCase):
    def setUp(self):
        self.config = Configuracao()
        self.estrategia = EstrategiaReversaoM5(self.config)
        idx = pd.date_range("2026-01-01", periods=60, freq="5min")
        self.df = pd.DataFrame(
            {
                "Open": 100.0,
                "High": 101.0,
                "Low": 99.0,
                "Close": 100.0,
                "Volume": 1.0,
                "ATR": 1.0,
                "RSI": 50.0,
                "BandaInf": 99.0,
                "BandaSup": 101.0,
                "TendenciaMacro": "lateral",
            },
            index=idx,
        )
        self.estrategia.calcular_indicadores = lambda candles: candles.copy()

    def test_call_exige_reentrada_confirmada(self):
        self.df.iloc[-3, self.df.columns.get_loc("Close")] = 98.0
        self.df.iloc[-3, self.df.columns.get_loc("RSI")] = 25.0
        self.df.iloc[-2, self.df.columns.get_loc("Close")] = 99.2
        self.df.iloc[-2, self.df.columns.get_loc("RSI")] = 35.0
        decisao = self.estrategia.avaliar("EURUSD", self.df)
        self.assertIsNotNone(decisao)
        self.assertEqual(decisao.direcao, "call")

    def test_put_exige_reentrada_confirmada(self):
        self.df.iloc[-3, self.df.columns.get_loc("Close")] = 102.0
        self.df.iloc[-3, self.df.columns.get_loc("RSI")] = 75.0
        self.df.iloc[-2, self.df.columns.get_loc("Close")] = 100.8
        self.df.iloc[-2, self.df.columns.get_loc("RSI")] = 65.0
        decisao = self.estrategia.avaliar("GBPUSD", self.df)
        self.assertIsNotNone(decisao)
        self.assertEqual(decisao.direcao, "put")

    def test_tendencia_forte_bloqueia_bollinger_quando_configurado(self):
        # bollinger_aceitar_tendencia=False bloqueia o sinal bollinger_rsi;
        # outros setups (sr_rejeicao etc.) podem disparar no mesmo cenario.
        config_restrito = Configuracao(bollinger_aceitar_tendencia=False)
        estrategia_restrita = EstrategiaReversaoM5(config_restrito)
        estrategia_restrita.calcular_indicadores = lambda candles: candles.copy()
        self.df.iloc[-3, self.df.columns.get_loc("Close")] = 98.0
        self.df.iloc[-3, self.df.columns.get_loc("RSI")] = 25.0
        self.df.iloc[-2, self.df.columns.get_loc("Close")] = 99.2
        self.df.iloc[-2, self.df.columns.get_loc("RSI")] = 35.0
        self.df.iloc[-2, self.df.columns.get_loc("TendenciaMacro")] = "baixa"
        decisao = estrategia_restrita.avaliar("EURUSD", self.df)
        if decisao is not None:
            self.assertNotEqual(decisao.detalhes.get("setup"), "reversao_bollinger_rsi",
                "bollinger deve ser bloqueado quando bollinger_aceitar_tendencia=False")

    def test_tendencia_forte_aceita_por_padrao(self):
        self.df.iloc[-3, self.df.columns.get_loc("Close")] = 98.0
        self.df.iloc[-3, self.df.columns.get_loc("RSI")] = 25.0
        self.df.iloc[-2, self.df.columns.get_loc("Close")] = 99.2
        self.df.iloc[-2, self.df.columns.get_loc("RSI")] = 35.0
        self.df.iloc[-2, self.df.columns.get_loc("TendenciaMacro")] = "baixa"
        self.assertIsNotNone(self.estrategia.avaliar("EURUSD", self.df))

    def test_rsi_lateral_e_neutro(self):
        candles = self.df[["Open", "High", "Low", "Close", "Volume"]]
        calculado = EstrategiaReversaoM5(self.config).calcular_indicadores(candles)
        self.assertEqual(calculado["RSI"].iloc[-1], 50)

    def _cenario_pullback(self, direcao):
        idx = pd.date_range("2026-02-01", periods=60, freq="5min")
        passo = 0.12 if direcao == "call" else -0.12
        closes = [100.0 + i * passo if direcao == "call" else 110.0 + i * passo for i in range(60)]
        df = pd.DataFrame(index=idx)
        df["Close"] = closes
        df["Open"] = df["Close"] - 0.03 if direcao == "call" else df["Close"] + 0.03
        df["High"] = df[["Open", "Close"]].max(axis=1) + 0.18
        df["Low"] = df[["Open", "Close"]].min(axis=1) - 0.18
        df["Volume"] = 1.0
        df["ATR"] = 0.4
        df["RSI"] = 55.0 if direcao == "call" else 45.0
        df["BandaInf"] = df["Close"] - 2.0
        df["BandaSup"] = df["Close"] + 2.0
        df["TendenciaMacro"] = "alta" if direcao == "call" else "baixa"

        if direcao == "call":
            df.iloc[40, df.columns.get_loc("Low")] = 104.0
            df.loc[idx[-3], ["Open", "High", "Low", "Close", "RSI"]] = [104.4, 104.5, 104.0, 104.1, 45.0]
            df.loc[idx[-2], ["Open", "High", "Low", "Close", "RSI"]] = [104.1, 104.6, 104.0, 104.5, 52.0]
        else:
            df.iloc[40, df.columns.get_loc("High")] = 106.0
            df.loc[idx[-3], ["Open", "High", "Low", "Close", "RSI"]] = [105.6, 106.0, 105.5, 105.9, 55.0]
            df.loc[idx[-2], ["Open", "High", "Low", "Close", "RSI"]] = [105.9, 106.0, 105.4, 105.5, 48.0]
        return df

    def test_pullback_call_exige_fibo_suporte_e_confirmacao(self):
        df = self._cenario_pullback("call")
        decisao = self.estrategia.avaliar("EURUSD-OTC", df)
        self.assertIsNotNone(decisao)
        self.assertEqual(decisao.direcao, "call")
        # fibo + suporte juntos = confluencia dupla, setup separado pra medir
        # se isso acerta mais que so um fator.
        self.assertEqual(decisao.detalhes["setup"], "pullback_confluencia")
        self.assertEqual(set(decisao.detalhes["fatores"]), {"fibo", "suporte"})

    def test_pullback_put_exige_fibo_resistencia_e_confirmacao(self):
        df = self._cenario_pullback("put")
        decisao = self.estrategia.avaliar("GBPUSD-OTC", df)
        self.assertIsNotNone(decisao)
        self.assertEqual(decisao.direcao, "put")
        self.assertEqual(decisao.detalhes["setup"], "pullback_confluencia")
        self.assertEqual(set(decisao.detalhes["fatores"]), {"fibo", "resistencia"})

    def test_pullback_sem_confirmacao_aciona_fibo_sr_retracao(self):
        # Vela bearish em uptrend tocando Fib+S/R = fibo_sr_retracao (entra sem confirmação).
        # O pullback com confirmação não dispara; a nova estratégia sim.
        df = self._cenario_pullback("call")
        df.loc[df.index[-2], ["Open", "Close", "RSI"]] = [104.6, 104.2, 44.0]
        decisao = self.estrategia.avaliar("EURUSD-OTC", df)
        self.assertIsNotNone(decisao)
        self.assertEqual(decisao.direcao, "call")
        self.assertEqual(decisao.detalhes["setup"], "fibo_sr_retracao")


if __name__ == "__main__":
    unittest.main()
