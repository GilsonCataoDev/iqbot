"""Valida rompimento + reteste M15 a favor da tendência H1 como Forex SL/TP."""

from __future__ import annotations

import argparse
import math

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex


ATIVOS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD"]
SPREAD = {"USDJPY": 0.015, "EURJPY": 0.015, "AUDUSD": 0.00015, "NZDUSD": 0.00015}
SPREAD_PADRAO = 0.00012
RR = 2.0


def wilson(vitorias: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 1.0
    p = vitorias / total
    d = 1 + z * z / total
    c = (p + z * z / (2 * total)) / d
    m = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return max(0.0, c - m), min(1.0, c + m)


def metricas(resultados: pd.DataFrame) -> dict:
    n = len(resultados)
    ganhos = int((resultados["lucro"] > 0).sum()) if n else 0
    wr = ganhos / n if n else 0.0
    ev = (ganhos * RR - (n - ganhos)) / n if n else 0.0
    lo, hi = wilson(ganhos, n)
    return {"n": n, "ganhos": ganhos, "wr": wr, "ev": ev, "lo": lo, "hi": hi}


def imprimir(rotulo: str, resultados: pd.DataFrame) -> dict:
    m = metricas(resultados)
    print(
        f"  {rotulo:24} n={m['n']:<4} WR={m['wr']:.1%} "
        f"IC=[{m['lo']:.1%},{m['hi']:.1%}] EV={m['ev']:+.3f}R"
    )
    return m


def executar(dados: dict[str, pd.DataFrame], multiplicador_spread: float = 1.0) -> pd.DataFrame:
    partes = []
    for ativo, candles in dados.items():
        spread = SPREAD.get(ativo, SPREAD_PADRAO) * multiplicador_spread
        resultados, _ = simular_forex(
            ativo,
            candles,
            banca=10_000.0,
            risco_percentual=0.0025,
            spread=spread,
            estrategia="rompimento_reteste_h1",
        )
        if not resultados.empty:
            partes.append(resultados)
    return pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()


def relatorio(base: pd.DataFrame, estresse: pd.DataFrame) -> bool:
    print("\nRompimento + reteste M15 com tendência H1 | SL/TP=2R | sessão 07-17 UTC")
    print("Entrada no Open seguinte; spread incluído; se SL e TP tocam juntos, vale SL.")
    if base.empty:
        print("  Nenhuma operação.")
        return False

    for ativo in ATIVOS:
        sub = base[base["ativo"] == ativo]
        if not sub.empty:
            imprimir(ativo, sub)

    ordenado = base.sort_values("aberta_em")
    corte = ordenado.iloc[int(len(ordenado) * 0.70)]["aberta_em"]
    treino = ordenado[ordenado["aberta_em"] < corte]
    holdout = ordenado[ordenado["aberta_em"] >= corte]
    print("\n  validação temporal:")
    imprimir("treino 70%", treino)
    m_hold = imprimir("holdout 30%", holdout)
    m_stress = imprimir("spread 1,5x", estresse)

    aprovado = (
        m_hold["n"] >= 50
        and m_hold["lo"] > 1 / (1 + RR)
        and m_hold["ev"] > 0.10
        and m_stress["ev"] > 0.05
    )
    print("\nDECISÃO:", "APROVADA PARA O MONITOR" if aprovado else "NÃO APROVADA — manter fora do monitor")
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
            print(f"{ativo}: histórico insuficiente")
            continue
        dados[ativo] = candles.tail(args.candles).copy()
        print(f"{ativo}: {len(dados[ativo])} candles")
    base = executar(dados)
    estresse = executar(dados, multiplicador_spread=1.5)
    return 0 if relatorio(base, estresse) else 2


if __name__ == "__main__":
    raise SystemExit(main())
