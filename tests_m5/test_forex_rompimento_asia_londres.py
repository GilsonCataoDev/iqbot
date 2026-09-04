import numpy as np
import pandas as pd

from iqoption_m5.forex_backtest import simular_forex
from iqoption_m5.forex_estrategia import planos_rompimento_asia_londres


def _candles(n=500):
    rng = np.random.default_rng(31)
    close = 1.10 + np.cumsum(rng.normal(0, 0.00025, n))
    abertura = np.r_[close[0], close[:-1]]
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame({
        "Open": abertura, "High": np.maximum(abertura, close) + 0.00015,
        "Low": np.minimum(abertura, close) - 0.00015, "Close": close, "Volume": 100,
    }, index=idx)


def _chave(p):
    if p is None or (isinstance(p, float) and pd.isna(p)):
        return None
    return p.lado, p.sinal_em, round(p.nivel, 8), round(p.stop, 8)


def test_sem_lookahead():
    candles = _candles()
    antes = planos_rompimento_asia_londres("EURUSD", candles.iloc[:350])
    alterado = candles.copy()
    cols = alterado.columns.get_indexer(["Open", "High", "Low", "Close"])
    alterado.iloc[350:, cols] *= 1.5
    depois = planos_rompimento_asia_londres("EURUSD", alterado).iloc[:350]
    assert [_chave(x) for x in antes] == [_chave(x) for x in depois]


def test_executor_aceita_estrategia():
    resultados, banca = simular_forex(
        "EURUSD", _candles(), estrategia="rompimento_asia_londres", spread=0.00012
    )
    assert isinstance(resultados, pd.DataFrame)
    assert banca > 0
