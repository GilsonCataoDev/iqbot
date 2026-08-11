"""Testes determinísticos para melhorias §8: SR Rejeição (RSI + corpo mínimo).

Sem rede, sem IQ Option, sem relógio real.
"""
import numpy as np
import pandas as pd
import pytest

from iqoption_m5.config import Configuracao
from iqoption_m5.estrategia import EstrategiaReversaoM5


# ---------------------------------------------------------------------------
# Fixture: DataFrame com suporte claro e candle de rejeição
# ---------------------------------------------------------------------------

def _df_sr_call(
    n: int = 20,
    support: float = 1.0850,
    atr: float = 0.0010,
    rsi: float = 30.0,
    tendencia: str = "lateral",
    corpo_ratio: float = 2.0,   # body = corpo_ratio * atr
) -> pd.DataFrame:
    """Cria n candles com suporte em `support`.

    O último candle (índice n-1) toca o suporte pelo Low e fecha acima do meio
    — condição base para sr_rejeicao CALL.
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    highs  = np.full(n, support + atr * 3.0)
    lows   = np.full(n, support + atr * 0.5)
    closes = np.full(n, support + atr * 1.5)
    opens  = np.full(n, support + atr * 1.2)

    # Cria pivôs de suporte nos candles 3 e 7 (raio=2: vizinhos têm Low maior)
    for pivot_i in (3, 7):
        lows[pivot_i]   = support
        opens[pivot_i]  = support + atr * 0.5
        closes[pivot_i] = support + atr * 1.0
        highs[pivot_i]  = support + atr * 2.0

    # Candle de rejeição: Low toca suporte, corpo bullish acima do meio
    lows[n - 1]   = support
    highs[n - 1]  = support + atr * 3.0
    opens[n - 1]  = support + atr * 0.5
    closes[n - 1] = support + atr * corpo_ratio  # close acima do meio = support+1.5*atr

    rsil = np.full(n, rsi)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes,
         "ATR": atr, "RSI": rsil, "TendenciaMacro": tendencia,
         "EMA_Micro": closes * 1.0001, "EMA_Macro": closes * 0.9999,
         "InclinacaoMacro": 0.0},
        index=idx,
    )


def _df_sr_put(
    n: int = 20,
    resistance: float = 1.0900,
    atr: float = 0.0010,
    rsi: float = 70.0,
    tendencia: str = "lateral",
    corpo_ratio: float = 2.0,
) -> pd.DataFrame:
    """Cria n candles com resistência em `resistance`.

    O último candle toca a resistência pelo High e fecha abaixo do meio
    — condição base para sr_rejeicao PUT.
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    highs  = np.full(n, resistance - atr * 0.5)
    lows   = np.full(n, resistance - atr * 3.0)
    closes = np.full(n, resistance - atr * 1.5)
    opens  = np.full(n, resistance - atr * 1.2)

    for pivot_i in (3, 7):
        highs[pivot_i]  = resistance
        closes[pivot_i] = resistance - atr * 1.0
        opens[pivot_i]  = resistance - atr * 0.5
        lows[pivot_i]   = resistance - atr * 2.0

    highs[n - 1]  = resistance
    lows[n - 1]   = resistance - atr * 3.0
    opens[n - 1]  = resistance - atr * 0.5
    closes[n - 1] = resistance - atr * corpo_ratio  # close abaixo do meio

    rsil = np.full(n, rsi)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes,
         "ATR": atr, "RSI": rsil, "TendenciaMacro": tendencia,
         "EMA_Micro": closes * 0.9999, "EMA_Macro": closes * 1.0001,
         "InclinacaoMacro": 0.0},
        index=idx,
    )


def _est(extra: dict | None = None) -> EstrategiaReversaoM5:
    kwargs = extra or {}
    return EstrategiaReversaoM5(Configuracao(**kwargs))


# ---------------------------------------------------------------------------
# §8 — Filtro RSI: sr_rejeicao CALL
# ---------------------------------------------------------------------------

class TestSrRejeicaoRsiFiltroCall:
    def test_sem_filtro_passa_rsi_neutro(self):
        """Sem filtro, qualquer RSI gera sinal CALL."""
        est = _est({"sr_rejeicao_rsi_filtro": False})
        df = _df_sr_call(rsi=50.0)  # RSI neutro
        d = est._avaliar_sr_rejeicao("EURUSD", df, len(df) - 1)
        assert d is not None and d.direcao == "call"

    def test_com_filtro_rsi_sobrevendido_passa(self):
        """Com filtro ativo, RSI < rsi_sobrevendido(35) deve gerar CALL."""
        est = _est({"sr_rejeicao_rsi_filtro": True, "rsi_sobrevendido": 35.0})
        df = _df_sr_call(rsi=29.0)
        d = est._avaliar_sr_rejeicao("EURUSD", df, len(df) - 1)
        assert d is not None and d.direcao == "call"

    def test_com_filtro_rsi_na_borda_bloqueado(self):
        """RSI == rsi_sobrevendido (borderline) deve ser bloqueado."""
        est = _est({"sr_rejeicao_rsi_filtro": True, "rsi_sobrevendido": 35.0})
        df = _df_sr_call(rsi=35.0)
        d = est._avaliar_sr_rejeicao("EURUSD", df, len(df) - 1)
        assert d is None

    def test_com_filtro_rsi_acima_bloqueado(self):
        """RSI acima do limiar (mercado não oversold) deve ser bloqueado."""
        est = _est({"sr_rejeicao_rsi_filtro": True, "rsi_sobrevendido": 35.0})
        df = _df_sr_call(rsi=50.0)
        d = est._avaliar_sr_rejeicao("EURUSD", df, len(df) - 1)
        assert d is None

    def test_com_filtro_rsi_ausente_ignora_filtro(self):
        """Sem coluna RSI no DataFrame, o filtro não bloqueia (campo indisponível)."""
        est = _est({"sr_rejeicao_rsi_filtro": True})
        df = _df_sr_call(rsi=50.0).drop(columns=["RSI"])
        d = est._avaliar_sr_rejeicao("EURUSD", df, len(df) - 1)
        # sem RSI: _rsi_ok=False → filtro não bloqueia → sinal normal
        assert d is not None and d.direcao == "call"


# ---------------------------------------------------------------------------
# §8 — Filtro RSI: sr_rejeicao PUT
# ---------------------------------------------------------------------------

class TestSrRejeicaoRsiFiltroput:
    def test_sem_filtro_passa_rsi_neutro(self):
        est = _est({"sr_rejeicao_rsi_filtro": False})
        df = _df_sr_put(rsi=50.0)
        d = est._avaliar_sr_rejeicao("GBPUSD", df, len(df) - 1)
        assert d is not None and d.direcao == "put"

    def test_com_filtro_rsi_sobrecomprado_passa(self):
        """RSI > rsi_sobrecomprado(65) deve gerar PUT."""
        est = _est({"sr_rejeicao_rsi_filtro": True, "rsi_sobrecomprado": 65.0})
        df = _df_sr_put(rsi=71.0)
        d = est._avaliar_sr_rejeicao("GBPUSD", df, len(df) - 1)
        assert d is not None and d.direcao == "put"

    def test_com_filtro_rsi_na_borda_bloqueado(self):
        """RSI == rsi_sobrecomprado (borderline) deve ser bloqueado."""
        est = _est({"sr_rejeicao_rsi_filtro": True, "rsi_sobrecomprado": 65.0})
        df = _df_sr_put(rsi=65.0)
        d = est._avaliar_sr_rejeicao("GBPUSD", df, len(df) - 1)
        assert d is None

    def test_com_filtro_rsi_abaixo_bloqueado(self):
        est = _est({"sr_rejeicao_rsi_filtro": True, "rsi_sobrecomprado": 65.0})
        df = _df_sr_put(rsi=50.0)
        d = est._avaliar_sr_rejeicao("GBPUSD", df, len(df) - 1)
        assert d is None


# ---------------------------------------------------------------------------
# §8 — Filtro corpo mínimo
# ---------------------------------------------------------------------------

class TestSrRejeicaoCorpoMin:
    def test_corpo_min_desativado_corpo_pequeno_passa(self):
        """sr_rejeicao_corpo_min_atr=0 não bloqueia nenhum corpo."""
        est = _est({"sr_rejeicao_corpo_min_atr": 0.0})
        # corpo_ratio=0.6 → corpo = 0.6*atr - 0.5*atr = 0.1*atr (pequeno)
        df = _df_sr_call(corpo_ratio=0.6)
        d = est._avaliar_sr_rejeicao("EURUSD", df, len(df) - 1)
        assert d is not None

    def test_corpo_min_ativo_corpo_insuficiente_bloqueado(self):
        """Corpo menor que o mínimo exigido bloqueia o sinal."""
        atr = 0.0010
        # corpo = |close-open| = |support+0.6*atr - support-0.5*atr| = ... wait
        # com corpo_ratio=0.6 e open=support+0.5*atr: corpo = (support+0.6*atr)-(support+0.5*atr) = 0.1*atr
        # Exigir 0.2*atr → deve bloquear
        est = _est({"sr_rejeicao_corpo_min_atr": 0.2, "sr_rejeicao_rsi_filtro": False})
        df = _df_sr_call(atr=atr, corpo_ratio=0.6)
        d = est._avaliar_sr_rejeicao("EURUSD", df, len(df) - 1)
        assert d is None

    def test_corpo_min_ativo_corpo_suficiente_passa(self):
        """Corpo maior que o mínimo exigido gera sinal."""
        atr = 0.0010
        # corpo_ratio=2.0 → close=support+2.0*atr, open=support+0.5*atr → corpo=1.5*atr
        # Exigir 1.0*atr → deve passar
        est = _est({"sr_rejeicao_corpo_min_atr": 1.0, "sr_rejeicao_rsi_filtro": False})
        df = _df_sr_call(atr=atr, corpo_ratio=2.0)
        d = est._avaliar_sr_rejeicao("EURUSD", df, len(df) - 1)
        assert d is not None

    def test_corpo_min_e_rsi_combinados_bloqueam_quando_rsi_falha(self):
        """Ambos os filtros ativos: RSI fora do range bloqueia mesmo com corpo ok."""
        est = _est({
            "sr_rejeicao_corpo_min_atr": 1.0,
            "sr_rejeicao_rsi_filtro": True,
            "rsi_sobrevendido": 35.0,
        })
        df = _df_sr_call(corpo_ratio=2.0, rsi=50.0)  # corpo ok, RSI neutro
        d = est._avaliar_sr_rejeicao("EURUSD", df, len(df) - 1)
        assert d is None
