"""Backtest de continuação após rompimento dos níveis 00/50."""

from __future__ import annotations

import pandas as pd

from backtest_forex_rompimento_reteste_h1 import SPREAD, SPREAD_PADRAO, imprimir
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex

ATIVOS = ["EURUSD", "USDJPY"]


def executar(dados, fator=1.0):
    partes = []
    for ativo, candles in dados.items():
        r, _ = simular_forex(
            ativo, candles, banca=10_000, risco_percentual=.0025,
            spread=SPREAD.get(ativo, SPREAD_PADRAO) * fator,
            estrategia="rompimento_numero_redondo",
        )
        if not r.empty:
            partes.append(r)
    return pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()


def main():
    config = configuracao_scalping_m15()
    dados = {a: backtest.carregar_cache(config, a).tail(26_000).copy() for a in ATIVOS}
    base, s15, s20 = executar(dados), executar(dados, 1.5), executar(dados, 2.0)
    print("\nRompimento numeros redondos | EURUSD/USDJPY | M15 | TP=2R")
    if base.empty:
        return 2
    for a in ATIVOS:
        imprimir(a, base[base.ativo == a])
    imprimir("BUY", base[base.lado == "buy"]); imprimir("SELL", base[base.lado == "sell"])
    ordenado = base.sort_values("aberta_em"); corte = ordenado.iloc[int(len(ordenado)*.7)].aberta_em
    imprimir("treino 70%", ordenado[ordenado.aberta_em < corte])
    mh = imprimir("holdout 30%", ordenado[ordenado.aberta_em >= corte])
    m15 = imprimir("spread 1,5x", s15); m20 = imprimir("spread 2,0x", s20)
    ok = mh["n"] >= 50 and mh["lo"] > 1/3 and mh["ev"] > .1 and m15["ev"] > .05 and m20["ev"] > 0
    print("DECISAO:", "APROVADA PARA OBSERVACAO" if ok else "NAO APROVADA")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
