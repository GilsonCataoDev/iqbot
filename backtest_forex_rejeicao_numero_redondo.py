"""Backtest de rejeição M15 nos níveis 00/50 de EURUSD e USDJPY."""

from __future__ import annotations

import pandas as pd

from backtest_forex_rompimento_reteste_h1 import SPREAD, SPREAD_PADRAO, imprimir
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex

ATIVOS = ["EURUSD", "USDJPY"]


def executar(dados: dict[str, pd.DataFrame], fator: float = 1.0) -> pd.DataFrame:
    partes = []
    for ativo, candles in dados.items():
        resultados, _ = simular_forex(
            ativo, candles, banca=10_000, risco_percentual=0.0025,
            spread=SPREAD.get(ativo, SPREAD_PADRAO) * fator,
            estrategia="rejeicao_numero_redondo",
        )
        if not resultados.empty:
            partes.append(resultados)
    return pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()


def main() -> int:
    config = configuracao_scalping_m15()
    dados = {}
    for ativo in ATIVOS:
        candles = backtest.carregar_cache(config, ativo)
        if candles is not None and len(candles) >= 500:
            dados[ativo] = candles.tail(26_000).copy()
    base, s15, s20 = executar(dados), executar(dados, 1.5), executar(dados, 2.0)
    print("\nRejeicao de numeros redondos | EURUSD/USDJPY | M15 | TP=2R")
    if base.empty:
        print("Nenhuma operacao.")
        return 2
    for ativo in ATIVOS:
        imprimir(ativo, base[base["ativo"] == ativo])
    imprimir("BUY", base[base["lado"] == "buy"])
    imprimir("SELL", base[base["lado"] == "sell"])
    ordenado = base.sort_values("aberta_em")
    corte = ordenado.iloc[int(len(ordenado) * .70)]["aberta_em"]
    holdout = ordenado[ordenado["aberta_em"] >= corte]
    imprimir("treino 70%", ordenado[ordenado["aberta_em"] < corte])
    mh = imprimir("holdout 30%", holdout)
    m15, m20 = imprimir("spread 1,5x", s15), imprimir("spread 2,0x", s20)
    aprovado = mh["n"] >= 50 and mh["lo"] > 1/3 and mh["ev"] > .10 and m15["ev"] > .05 and m20["ev"] > 0
    print("DECISAO:", "APROVADA PARA OBSERVACAO" if aprovado else "NAO APROVADA")
    return 0 if aprovado else 2


if __name__ == "__main__":
    raise SystemExit(main())
