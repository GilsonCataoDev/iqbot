"""Testes determinísticos para melhorias §9: Pullback (corpo mínimo + RSI recuo).

Sem rede, sem IQ Option, sem relógio real.
"""
import numpy as np
import pandas as pd
import pytest

from iqoption_m5.config import Configuracao
from iqoption_m5.estrategia import EstrategiaReversaoM5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _est(extra: dict | None = None) -> EstrategiaReversaoM5:
    return EstrategiaReversaoM5(Configuracao(**(extra or {})))


def _df_pullback_call(
    n: int = 30,
    atr: float = 0.0010,
    rsi_recuo: float = 30.0,
    rsi_conf: float = 45.0,
    corpo_conf_ratio: float = 1.5,
) -> pd.DataFrame:
    """Série com tendência de alta, recuo (n-2) e confirmação (n-1).

    Invariantes para que `_avaliar_pullback_indicadores` chegue até os novos filtros:
    - rsi_conf > rsi_recuo (confirmação RSI cresce)
    - TendenciaMacro = "alta" em todos os candles
    - InclinacaoMacro pequena (não dispara slope forte)
    - Candle de confirmação bullish (close > open)
    """
    assert rsi_conf > rsi_recuo, "rsi_conf deve ser > rsi_recuo para confirmou=True"
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    base = 1.0800
    step = atr * 0.5

    closes = np.array([base + i * step for i in range(n)], dtype=float)
    opens  = closes - atr * 0.2
    highs  = closes + atr * 0.3
    lows   = closes - atr * 0.4

    # Calcula zona fibo igual ao que _zona_fibonacci calcularia
    pico    = float(highs[n - 3])           # high do candle anterior ao recuo
    origem  = float(lows[0])
    amp     = pico - origem
    zona_baixa = pico - amp * 0.618         # pullback_fib_max default

    # Candle de recuo (n-2): low entra na zona fibo
    ri = n - 2
    lows[ri]   = zona_baixa - atr * 0.1    # toca zona
    opens[ri]  = zona_baixa + atr * 0.5
    closes[ri] = zona_baixa + atr * 0.1    # close < open → recuo
    highs[ri]  = zona_baixa + atr * 0.8

    # Candle de confirmação (n-1): corpo bullish, close > recuo close
    ci = n - 1
    conf_open  = closes[ri] + atr * 0.05
    conf_close = conf_open + corpo_conf_ratio * atr
    opens[ci]  = conf_open
    closes[ci] = conf_close
    highs[ci]  = conf_close + atr * 0.2
    lows[ci]   = conf_open - atr * 0.1

    rsi_arr = np.full(n, 50.0)
    rsi_arr[ri] = rsi_recuo
    rsi_arr[ci] = rsi_conf

    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows, "Close": closes,
        "ATR": atr,
        "RSI": rsi_arr,
        "TendenciaMacro": "alta",
        "InclinacaoMacro": atr * 0.05,  # < 0.5*atr → não dispara slope forte
        "EMA_Micro": closes * 1.0001,
        "EMA_Macro": closes * 0.9999,
    }, index=idx)


# Config base que habilita pullback simples (1 fator) para isolar os novos filtros
_BASE = {
    "pullback_ativo": True,
    "pullback_rsi_min": 30.0,
    "pullback_rsi_max": 70.0,
}


# ---------------------------------------------------------------------------
# §9 — Filtro RSI recuo
# ---------------------------------------------------------------------------

class TestPullbackRecuoRsi:
    def test_sem_filtro_rsi_recuo_neutro_passa(self):
        """Sem filtro, RSI do recuo em zona neutra não bloqueia."""
        est = _est({**_BASE, "pullback_recuo_rsi_filtro": False})
        # rsi_recuo=40 (neutro, ≥ rsi_sobrevendido=35), rsi_conf=55 > rsi_recuo
        df = _df_pullback_call(rsi_recuo=40.0, rsi_conf=55.0)
        d = est._avaliar_pullback_indicadores("EURUSD", df, len(df) - 1)
        assert d is not None, "sem filtro deve passar independente do RSI do recuo"

    def test_com_filtro_rsi_recuo_sobrevendido_passa(self):
        """Com filtro ativo, recuo com RSI sobrevendido (<35) gera CALL."""
        est = _est({**_BASE, "pullback_recuo_rsi_filtro": True, "rsi_sobrevendido": 35.0})
        df = _df_pullback_call(rsi_recuo=29.0, rsi_conf=45.0)
        d = est._avaliar_pullback_indicadores("EURUSD", df, len(df) - 1)
        assert d is not None and d.direcao == "call"

    def test_com_filtro_rsi_recuo_neutro_bloqueado(self):
        """Com filtro, recuo com RSI neutro (≥35) é bloqueado."""
        est = _est({**_BASE, "pullback_recuo_rsi_filtro": True, "rsi_sobrevendido": 35.0})
        # rsi_recuo=40 ≥ rsi_sobrevendido=35 → bloqueia antes do confirmou
        df = _df_pullback_call(rsi_recuo=40.0, rsi_conf=55.0)
        d = est._avaliar_pullback_indicadores("EURUSD", df, len(df) - 1)
        assert d is None


# ---------------------------------------------------------------------------
# §9 — Filtro corpo mínimo da confirmação
# ---------------------------------------------------------------------------

class TestPullbackCorpoMin:
    def test_sem_filtro_corpo_pequeno_passa(self):
        """Sem filtro, qualquer corpo na confirmação é aceito."""
        est = _est({**_BASE, "pullback_confirmacao_corpo_atr": 0.0})
        # corpo_conf_ratio=0.01 → corpo = 0.01*atr (muito pequeno)
        df = _df_pullback_call(rsi_recuo=29.0, rsi_conf=45.0, corpo_conf_ratio=0.01)
        d = est._avaliar_pullback_indicadores("EURUSD", df, len(df) - 1)
        assert d is not None, "com filtro desativado deve passar independente do corpo"

    def test_com_filtro_corpo_insuficiente_bloqueado(self):
        """Corpo da confirmação menor que o mínimo bloqueia."""
        est = _est({**_BASE, "pullback_confirmacao_corpo_atr": 0.5})
        # corpo_conf_ratio=0.1 → corpo = 0.1*atr < 0.5*atr
        df = _df_pullback_call(rsi_recuo=29.0, rsi_conf=45.0, corpo_conf_ratio=0.1)
        d = est._avaliar_pullback_indicadores("EURUSD", df, len(df) - 1)
        assert d is None

    def test_com_filtro_corpo_suficiente_passa(self):
        """Corpo da confirmação acima do mínimo gera sinal."""
        est = _est({**_BASE, "pullback_confirmacao_corpo_atr": 0.5})
        # corpo_conf_ratio=1.5 → corpo = 1.5*atr > 0.5*atr
        df = _df_pullback_call(rsi_recuo=29.0, rsi_conf=45.0, corpo_conf_ratio=1.5)
        d = est._avaliar_pullback_indicadores("EURUSD", df, len(df) - 1)
        assert d is not None

    def test_rsi_recuo_e_corpo_min_combinados(self):
        """Ambos os filtros: falha em qualquer um bloqueia."""
        cfg = {**_BASE, "pullback_recuo_rsi_filtro": True, "rsi_sobrevendido": 35.0,
               "pullback_confirmacao_corpo_atr": 0.5}
        est = _est(cfg)

        # Ambos ok → passa
        df_ok = _df_pullback_call(rsi_recuo=29.0, rsi_conf=45.0, corpo_conf_ratio=1.5)
        assert est._avaliar_pullback_indicadores("EURUSD", df_ok, len(df_ok) - 1) is not None

        # RSI ok, corpo falha → bloqueia
        df_corpo = _df_pullback_call(rsi_recuo=29.0, rsi_conf=45.0, corpo_conf_ratio=0.1)
        assert est._avaliar_pullback_indicadores("EURUSD", df_corpo, len(df_corpo) - 1) is None

        # RSI falha (40 ≥ 35), corpo ok → bloqueia
        df_rsi = _df_pullback_call(rsi_recuo=40.0, rsi_conf=55.0, corpo_conf_ratio=1.5)
        assert est._avaliar_pullback_indicadores("EURUSD", df_rsi, len(df_rsi) - 1) is None


# ---------------------------------------------------------------------------
# Validação de config — defaults corretos
# ---------------------------------------------------------------------------

def test_config_defaults_nao_alteram_comportamento():
    c = Configuracao()
    assert c.pullback_confirmacao_corpo_atr == 0.0
    assert c.pullback_recuo_rsi_filtro is False
    assert c.sr_rejeicao_rsi_filtro is False
    assert c.sr_rejeicao_corpo_min_atr == 0.0
    assert c.janela_entrada_por_setup is None
