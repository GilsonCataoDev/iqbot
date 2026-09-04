"""Valida pullback M15 confirmado a favor da tendência H1."""

from __future__ import annotations

import argparse
import pandas as pd

from backtest_forex_rompimento_reteste_h1 import ATIVOS, SPREAD, SPREAD_PADRAO, imprimir
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex


def executar(dados: dict[str, pd.DataFrame], multiplicador_spread: float = 1.0) -> pd.DataFrame:
    partes = []
    for ativo, candles in dados.items():
        resultados, _ = simular_forex(
            ativo, candles, banca=10_000.0, risco_percentual=0.0025,
            spread=SPREAD.get(ativo, SPREAD_PADRAO) * multiplicador_spread,
            estrategia="pullback_h1_confirmado",
        )
        if not resultados.empty:
            partes.append(resultados)
    return pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()


def relatorio(base: pd.DataFrame, estresse: pd.DataFrame) -> bool:
    print("\nPullback H1 confirmado | M15 | SL estrutural | TP=2R | 07-17 UTC")
    print("Entrada no Open seguinte; spread incluido; candle ambiguo conta como SL.")
    if base.empty:
        print("  Nenhuma operacao.")
        return False
    for ativo in ATIVOS:
        sub = base[base["ativo"] == ativo]
        if not sub.empty:
            imprimir(ativo, sub)
    print("\n  por direcao:")
    imprimir("BUY", base[base["lado"] == "buy"])
    imprimir("SELL", base[base["lado"] == "sell"])
    ordenado = base.sort_values("aberta_em")
    corte = ordenado.iloc[int(len(ordenado) * 0.70)]["aberta_em"]
    treino = ordenado[ordenado["aberta_em"] < corte]
    holdout = ordenado[ordenado["aberta_em"] >= corte]
    print("\n  validacao temporal:")
    imprimir("treino 70%", treino)
    m_hold = imprimir("holdout 30%", holdout)
    m_stress = imprimir("spread 1,5x", estresse)
    aprovado = (
        m_hold["n"] >= 50 and m_hold["lo"] > 1 / 3
        and m_hold["ev"] > 0.10 and m_stress["ev"] > 0.05
    )
    print("\nDECISAO:", "APROVADA PARA O MONITOR" if aprovado else "NAO APROVADA - manter fora do monitor")
    return aprovado


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", type=int, default=26_000)
    ap.add_argument("--ativos", nargs="*", default=ATIVOS)
    args = ap.parse_args()
    config = configuracao_scalping_m15()
    dados = {}
    for ativo in args.ativos:
        candles = backtest.carregar_cache(config, ativo)
        if candles is None or len(candles) < 500:
            print(f"{ativo}: historico insuficiente")
            continue
        dados[ativo] = candles.tail(args.candles).copy()
        print(f"{ativo}: {len(dados[ativo])} candles")
    base = executar(dados)
    estresse = executar(dados, 1.5)
    return 0 if relatorio(base, estresse) else 2


if __name__ == "__main__":
    raise SystemExit(main())
