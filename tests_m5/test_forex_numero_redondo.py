import numpy as np
import pandas as pd

from iqoption_m5.forex_estrategia import planos_rejeicao_numero_redondo


def test_numero_redondo_sem_lookahead():
    rng = np.random.default_rng(41)
    close = 1.10 + np.cumsum(rng.normal(0, .00025, 500))
    abertura = np.r_[close[0], close[:-1]]
    idx = pd.date_range("2026-01-01", periods=500, freq="15min")
    candles = pd.DataFrame({"Open": abertura, "High": np.maximum(abertura, close)+.0002,
                            "Low": np.minimum(abertura, close)-.0002, "Close": close, "Volume": 1}, index=idx)
    antes = planos_rejeicao_numero_redondo("EURUSD", candles.iloc[:350])
    alterado = candles.copy()
    alterado.iloc[350:, :4] *= 1.5
    depois = planos_rejeicao_numero_redondo("EURUSD", alterado).iloc[:350]
    chave = lambda p: None if p is None else (p.lado, p.sinal_em, round(p.stop, 8))
    assert [chave(p) for p in antes] == [chave(p) for p in depois]
