import numpy as np
import pandas as pd

from iqoption_m5.forex_estrategia import planos_pullback_h1_nova_york


def test_filtro_nova_york_remove_sinais_fora_da_janela(monkeypatch):
    indice = pd.date_range("2026-01-05 00:00", periods=96, freq="15min")
    close = 1.1 + np.arange(96) * 0.00001
    candles = pd.DataFrame({
        "Open": close, "High": close + 0.0001, "Low": close - 0.0001,
        "Close": close, "Volume": 1,
    }, index=indice)
    marcador = pd.Series([object()] * len(candles), index=candles.index, dtype="object")
    monkeypatch.setattr(
        "iqoption_m5.forex_estrategia.planos_pullback_h1_confirmado",
        lambda *args, **kwargs: marcador,
    )
    filtrado = planos_pullback_h1_nova_york("USDCAD", candles)
    horas = pd.DatetimeIndex(filtrado[filtrado.notna()].index).tz_localize("UTC").tz_convert("America/New_York").hour
    assert len(horas) > 0
    assert all((horas >= 8) & (horas < 12))
