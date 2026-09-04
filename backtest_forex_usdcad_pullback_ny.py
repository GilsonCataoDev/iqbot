"""Backtest USDCAD: pullback H1 confirmado na manhã de Nova York."""

from __future__ import annotations

import pandas as pd

from backtest_forex_rompimento_reteste_h1 import SPREAD, SPREAD_PADRAO, imprimir
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex


def executar(candles: pd.DataFrame, fator_spread: float = 1.0) -> pd.DataFrame:
    resultados, _ = simular_forex(
        "USDCAD", candles, banca=10_000.0, risco_percentual=0.0025,
        spread=SPREAD.get("USDCAD", SPREAD_PADRAO) * fator_spread,
        estrategia="pullback_h1_nova_york",
    )
    return resultados


def main() -> int:
    config = configuracao_scalping_m15()
    candles = backtest.carregar_cache(config, "USDCAD")
    if candles is None or len(candles) < 500:
        print("USDCAD: historico insuficiente")
        return 2
    candles = candles.tail(26_000).copy()
    base, stress15, stress20 = executar(candles), executar(candles, 1.5), executar(candles, 2.0)
    print("\nUSDCAD pullback H1 | 08-12 America/New_York | TP=2R")
    if base.empty:
        print("Nenhuma operacao.")
        return 2
    imprimir("total", base)
    imprimir("BUY", base[base["lado"] == "buy"])
    imprimir("SELL", base[base["lado"] == "sell"])
    ordenado = base.sort_values("aberta_em")
    corte = ordenado.iloc[int(len(ordenado) * 0.70)]["aberta_em"]
    treino = ordenado[ordenado["aberta_em"] < corte]
    holdout = ordenado[ordenado["aberta_em"] >= corte]
    imprimir("treino 70%", treino)
    m_hold = imprimir("holdout 30%", holdout)
    m15 = imprimir("spread 1,5x", stress15)
    m20 = imprimir("spread 2,0x", stress20)
    aprovado = (
        m_hold["n"] >= 50 and m_hold["lo"] > 1 / 3 and m_hold["ev"] > 0.10
        and m15["ev"] > 0.05 and m20["ev"] > 0
    )
    print("DECISAO:", "APROVADA PARA OBSERVACAO" if aprovado else "NAO APROVADA")
    return 0 if aprovado else 2


if __name__ == "__main__":
    raise SystemExit(main())
