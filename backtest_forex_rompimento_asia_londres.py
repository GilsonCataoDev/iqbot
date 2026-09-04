"""Backtest da continuação do rompimento da faixa asiática em Londres."""

from __future__ import annotations

import argparse
import pandas as pd

from backtest_forex_rompimento_reteste_h1 import SPREAD, SPREAD_PADRAO, imprimir
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex

ATIVOS = ["EURUSD", "GBPUSD"]


def executar(dados: dict[str, pd.DataFrame], fator_spread: float = 1.0) -> pd.DataFrame:
    partes = []
    for ativo, candles in dados.items():
        resultados, _ = simular_forex(
            ativo, candles, banca=10_000.0, risco_percentual=0.0025,
            spread=SPREAD.get(ativo, SPREAD_PADRAO) * fator_spread,
            estrategia="rompimento_asia_londres",
        )
        if not resultados.empty:
            partes.append(resultados)
    return pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()


def relatorio(base: pd.DataFrame, stress15: pd.DataFrame, stress20: pd.DataFrame) -> bool:
    print("\nRompimento Asia-Londres | EURUSD/GBPUSD | M15 | TP=2R")
    print("Faixa 00-08 Londres; entrada no Open seguinte; candle ambiguo vale SL.")
    if base.empty:
        print("  Nenhuma operacao.")
        return False
    for ativo in ATIVOS:
        imprimir(ativo, base[base["ativo"] == ativo])
    imprimir("BUY", base[base["lado"] == "buy"])
    imprimir("SELL", base[base["lado"] == "sell"])
    ordenado = base.sort_values("aberta_em")
    corte = ordenado.iloc[int(len(ordenado) * 0.70)]["aberta_em"]
    treino = ordenado[ordenado["aberta_em"] < corte]
    holdout = ordenado[ordenado["aberta_em"] >= corte]
    print("\n  validacao:")
    imprimir("treino 70%", treino)
    m_hold = imprimir("holdout 30%", holdout)
    m15 = imprimir("spread 1,5x", stress15)
    m20 = imprimir("spread 2,0x", stress20)
    aprovado = (
        m_hold["n"] >= 50 and m_hold["lo"] > 1 / 3 and m_hold["ev"] > 0.10
        and m15["ev"] > 0.05 and m20["ev"] > 0.0
    )
    print("\nDECISAO:", "APROVADA PARA OBSERVACAO" if aprovado else "NAO APROVADA")
    return aprovado


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", type=int, default=26_000)
    args = ap.parse_args()
    config = configuracao_scalping_m15()
    dados = {}
    for ativo in ATIVOS:
        candles = backtest.carregar_cache(config, ativo)
        if candles is not None and len(candles) >= 500:
            dados[ativo] = candles.tail(args.candles).copy()
            print(f"{ativo}: {len(dados[ativo])} candles")
    return 0 if relatorio(executar(dados), executar(dados, 1.5), executar(dados, 2.0)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
