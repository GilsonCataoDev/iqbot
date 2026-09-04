import numpy as np
import pandas as pd

from iqoption_m5.forex_backtest import simular_forex
from iqoption_m5.forex_estrategia import planos_rompimento_reteste_h1


def _candles(n: int = 500) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    retornos = rng.normal(0.00001, 0.00025, n)
    close = 1.10 + np.cumsum(retornos)
    abertura = np.r_[close[0], close[:-1]]
    indice = pd.date_range("2026-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "Open": abertura,
            "High": np.maximum(abertura, close) + 0.00015,
            "Low": np.minimum(abertura, close) - 0.00015,
            "Close": close,
            "Volume": 100,
        },
        index=indice,
    )


def _chave(plano):
    if plano is None or (isinstance(plano, float) and pd.isna(plano)):
        return None
    return plano.lado, plano.sinal_em, round(plano.nivel, 8), round(plano.stop, 8)


def test_sinais_passados_nao_mudam_quando_futuro_muda() -> None:
    candles = _candles()
    antes = planos_rompimento_reteste_h1("EURUSD", candles.iloc[:350])
    alterado = candles.copy()
    alterado.iloc[350:, alterado.columns.get_indexer(["Open", "High", "Low", "Close"])] *= 1.5
    depois = planos_rompimento_reteste_h1("EURUSD", alterado).iloc[:350]

    assert [_chave(x) for x in antes] == [_chave(x) for x in depois]


def test_backtest_aceita_nova_estrategia() -> None:
    resultados, banca = simular_forex(
        "EURUSD", _candles(), estrategia="rompimento_reteste_h1", spread=0.00012
    )

    assert isinstance(resultados, pd.DataFrame)
    assert banca > 0
