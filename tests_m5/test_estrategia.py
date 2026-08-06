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
        self.estrategia.calcular_indicadores = lambda candles, ativo="": candles.copy()

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
        estrategia_restrita.calcular_indicadores = lambda candles, ativo="": candles.copy()
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


class TestEstrategiasOpcionais(unittest.TestCase):
    """Testa as 3 estratégias opcionais com flags ligadas."""

    def _base_df(self, n=60):
        idx = pd.date_range("2026-03-01", periods=n, freq="5min")
        df = pd.DataFrame(
            {
                "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
                "Volume": 1.0, "ATR": 1.0, "RSI": 50.0,
                "BandaInf": 98.0, "BandaSup": 102.0,
                "TendenciaMacro": "lateral", "InclinacaoMacro": 0.0,
                "MACD": 0.0, "MACD_Signal": 0.0, "MACD_Hist": 0.0,
            },
            index=idx,
        )
        return df

    def _estrategia(self, **flags):
        from dataclasses import replace
        from iqoption_m5.config import Configuracao
        config = replace(Configuracao(), **flags)
        est = EstrategiaReversaoM5(config)
        est.calcular_indicadores = lambda candles, ativo="": candles.copy()
        return est

    # ------------------------------------------------------------------ #
    # engulfing_sr                                                         #
    # ------------------------------------------------------------------ #

    def _setup_engulfing_call(self, df):
        """Monta engulfing bullish em suporte no penúltimo candle (indice=len-2)."""
        # Pivô de suporte em i=40 (índice absoluto no df de 80 linhas)
        df.iloc[40, df.columns.get_loc("Low")] = 96.0
        i = len(df) - 2
        df.iloc[i - 1, df.columns.get_loc("Open")] = 100.5
        df.iloc[i - 1, df.columns.get_loc("Close")] = 99.5
        df.iloc[i, df.columns.get_loc("Open")] = 99.3
        df.iloc[i, df.columns.get_loc("Close")] = 100.7
        df.iloc[i, df.columns.get_loc("Low")] = 96.2
        df.iloc[i, df.columns.get_loc("High")] = 101.0
        df.iloc[i, df.columns.get_loc("TendenciaMacro")] = "alta"
        return df, i

    def test_engulfing_sr_call_em_suporte(self):
        """Engulfing bullish tocando suporte → CALL em avaliar_todas."""
        est = self._estrategia(engulfing_sr_ativo=True)
        df, _ = self._setup_engulfing_call(self._base_df(80))
        decisoes = est.avaliar_todas("EURUSD-OTC", df)
        eng = [d for d in decisoes if d.detalhes.get("setup") == "engulfing_sr"]
        self.assertTrue(eng, "engulfing bullish em suporte deve gerar CALL")
        self.assertEqual(eng[0].direcao, "call")

    def test_engulfing_sr_put_em_resistencia(self):
        """Engulfing bearish tocando resistência → PUT em avaliar_todas."""
        est = self._estrategia(engulfing_sr_ativo=True)
        df = self._base_df(80)
        df.iloc[40, df.columns.get_loc("High")] = 104.0
        i = len(df) - 2
        df.iloc[i - 1, df.columns.get_loc("Open")] = 99.5
        df.iloc[i - 1, df.columns.get_loc("Close")] = 100.5
        df.iloc[i, df.columns.get_loc("Open")] = 100.7
        df.iloc[i, df.columns.get_loc("Close")] = 99.3
        df.iloc[i, df.columns.get_loc("High")] = 103.8
        df.iloc[i, df.columns.get_loc("Low")] = 99.0
        df.iloc[i, df.columns.get_loc("TendenciaMacro")] = "baixa"

        decisoes = est.avaliar_todas("EURUSD-OTC", df)
        eng = [d for d in decisoes if d.detalhes.get("setup") == "engulfing_sr"]
        self.assertTrue(eng, "engulfing bearish em resistência deve gerar PUT")
        self.assertEqual(eng[0].direcao, "put")

    def test_engulfing_sr_desativado_por_padrao(self):
        """Com flag desligada, engulfing não deve aparecer em avaliar_todas."""
        est = self._estrategia(engulfing_sr_ativo=False)
        df, _ = self._setup_engulfing_call(self._base_df(80))
        decisoes = est.avaliar_todas("EURUSD-OTC", df)
        eng = [d for d in decisoes if d.detalhes.get("setup") == "engulfing_sr"]
        self.assertEqual(eng, [], "engulfing_sr desativado não deve disparar")

    # ------------------------------------------------------------------ #
    # divergencia_rsi                                                      #
    # ------------------------------------------------------------------ #

    def _df_com_tendencia(self, n=60):
        """DF base com Low estritamente crescente → sem swing lows naturais.

        Com Low[i] = 99.5 + i*0.01 (monotônico), a detecção de swing lows
        só captura pontos onde eu explicitamente coloco um valor muito abaixo
        dos vizinhos. Isso garante que apenas os pivôs que o teste quer
        testar sejam detectados.
        """
        idx = pd.date_range("2026-03-01", periods=n, freq="5min")
        closes = [100.0 + i * 0.01 for i in range(n)]
        df = pd.DataFrame(index=idx)
        df["Close"] = closes
        df["Open"] = [c - 0.02 for c in closes]
        df["High"] = [c + 0.5 for c in closes]
        df["Low"] = [99.5 + i * 0.01 for i in range(n)]  # estritamente crescente
        df["Volume"] = 1.0
        df["ATR"] = 1.0
        df["RSI"] = 50.0
        df["BandaInf"] = 98.0
        df["BandaSup"] = 102.0
        df["TendenciaMacro"] = "lateral"
        df["InclinacaoMacro"] = 0.0
        df["MACD"] = 0.0
        df["MACD_Signal"] = 0.0
        df["MACD_Hist"] = 0.0
        return df

    def test_divergencia_rsi_bullish(self):
        """Fundo mais baixo no preço + fundo mais alto no RSI → CALL em avaliar_todas.

        Usa Low único por candle (padrão onda lenta) para que a detecção de swing
        não confunda vizinhos de mesmo valor com fundos reais.
        """
        est = self._estrategia(divergencia_rsi_ativo=True)
        df = self._df_com_tendencia(60)
        # Primeiro fundo em row 25: Low bem abaixo dos vizinhos
        df.iloc[25, df.columns.get_loc("Low")] = 90.0
        df.iloc[25, df.columns.get_loc("RSI")] = 28.0
        # Segundo fundo em row 45: Low ainda mais baixo, RSI mais alto (divergência)
        df.iloc[45, df.columns.get_loc("Low")] = 88.0
        df.iloc[45, df.columns.get_loc("RSI")] = 33.0
        i = len(df) - 2  # 58
        df.iloc[i, df.columns.get_loc("Open")] = 99.5
        df.iloc[i, df.columns.get_loc("Close")] = 100.5
        df.iloc[i, df.columns.get_loc("RSI")] = 37.0

        decisoes = est.avaliar_todas("EURUSD-OTC", df)
        div = [d for d in decisoes if d.detalhes.get("setup") == "divergencia_rsi"]
        self.assertTrue(div, "divergência bullish deve gerar CALL")
        self.assertEqual(div[0].direcao, "call")

    def test_divergencia_rsi_bearish(self):
        """Topo mais alto no preço + topo mais baixo no RSI → PUT em avaliar_todas."""
        est = self._estrategia(divergencia_rsi_ativo=True)
        df = self._df_com_tendencia(60)
        df.iloc[25, df.columns.get_loc("High")] = 110.0
        df.iloc[25, df.columns.get_loc("RSI")] = 72.0
        df.iloc[45, df.columns.get_loc("High")] = 112.0  # High mais alto (divergência)
        df.iloc[45, df.columns.get_loc("RSI")] = 67.0    # RSI mais baixo (bearish div)
        i = len(df) - 2
        df.iloc[i, df.columns.get_loc("Open")] = 100.5
        df.iloc[i, df.columns.get_loc("Close")] = 99.5
        df.iloc[i, df.columns.get_loc("RSI")] = 63.0

        decisoes = est.avaliar_todas("EURUSD-OTC", df)
        div = [d for d in decisoes if d.detalhes.get("setup") == "divergencia_rsi"]
        self.assertTrue(div, "divergência bearish deve gerar PUT")
        self.assertEqual(div[0].direcao, "put")

    # ------------------------------------------------------------------ #
    # bollinger_squeeze                                                    #
    # ------------------------------------------------------------------ #

    def _setup_squeeze_df(self):
        """DF de 60 candles com squeeze no penúltimo (largura atual << histórico)."""
        df = self._base_df(60)
        df["BandaSup"] = 102.0
        df["BandaInf"] = 98.0   # largura histórica = 4.0
        i = len(df) - 2
        df.iloc[i, df.columns.get_loc("BandaSup")] = 100.3
        df.iloc[i, df.columns.get_loc("BandaInf")] = 99.8  # largura=0.5 < p20 das anteriores
        df.iloc[i, df.columns.get_loc("ATR")] = 0.5
        return df, i

    def test_bollinger_squeeze_call(self):
        """Squeeze de BB seguido de fechamento acima da banda → CALL em avaliar_todas."""
        est = self._estrategia(bollinger_squeeze_ativo=True)
        df, i = self._setup_squeeze_df()
        df.iloc[i, df.columns.get_loc("Close")] = 100.5
        df.iloc[i, df.columns.get_loc("Open")] = 99.9
        df.iloc[i, df.columns.get_loc("RSI")] = 58.0

        decisoes = est.avaliar_todas("EURUSD-OTC", df)
        bb = [d for d in decisoes if d.detalhes.get("setup") == "bollinger_squeeze"]
        self.assertTrue(bb, "squeeze + breakout bullish deve gerar CALL")
        self.assertEqual(bb[0].direcao, "call")

    def test_bollinger_squeeze_put(self):
        """Squeeze de BB seguido de fechamento abaixo da banda → PUT em avaliar_todas."""
        est = self._estrategia(bollinger_squeeze_ativo=True)
        df, i = self._setup_squeeze_df()
        df.iloc[i, df.columns.get_loc("Close")] = 99.5
        df.iloc[i, df.columns.get_loc("Open")] = 100.1
        df.iloc[i, df.columns.get_loc("RSI")] = 42.0

        decisoes = est.avaliar_todas("EURUSD-OTC", df)
        bb = [d for d in decisoes if d.detalhes.get("setup") == "bollinger_squeeze"]
        self.assertTrue(bb, "squeeze + breakout bearish deve gerar PUT")
        self.assertEqual(bb[0].direcao, "put")


if __name__ == "__main__":
    unittest.main()
