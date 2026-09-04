"""Re-verifica o falso rompimento com contabilidade CORRETA de EV.

BUG encontrado: EV = WR * media(R:R) - (1-WR) superestima quando a
distribuicao de R:R e assimetrica. Ganhos se concentram em R:R baixo
(TP perto) e perdas em R:R alto (TP longe) -> a media nao representa
o R:R dos vencedores.

CORRETO: R realizado por trade = +rr se ganho, -1 se perda. Somar.
"""
from __future__ import annotations

import dataclasses
import math
import sys

import numpy as np
import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from backtest_forex_falso_rompimento import simular_falso_rompimento, ATIVOS_PADRAO


def wilson(w, n, z=1.96):
    if n == 0: return 0.0, 1.0
    p = w/n; d = 1 + z*z/n
    c = (p + z*z/(2*n))/d
    m = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/d
    return max(0.0, c-m), min(1.0, c+m)


def relatorio_correto(d: pd.DataFrame, rot: str = "") -> dict:
    if d.empty or len(d) < 20:
        print(f"  {rot:30} amostra pequena (n={len(d)})")
        return {}
    n = len(d)
    ganhos = d[d["resultado"] == "ganho"]
    perdas = d[d["resultado"] == "perda"]
    w = len(ganhos)
    wr = w/n

    # CORRETO: soma do R realizado
    r_total = float(ganhos["rr"].sum()) - len(perdas)
    ev_real = r_total / n

    # ERRADO (o que estava sendo usado)
    rr_med_todos = float(d["rr"].mean())
    ev_falso = wr*rr_med_todos - (1-wr)

    rr_med_ganhos = float(ganhos["rr"].mean()) if w else 0.0
    lo, hi = wilson(w, n)

    print(f"  {rot:30} n={n:<5} WR={wr:5.1%} IC=[{lo:.1%},{hi:.1%}]")
    print(f"  {'':30} R:R medio (todos) = {rr_med_todos:5.2f}")
    print(f"  {'':30} R:R medio (so ganhos) = {rr_med_ganhos:5.2f}  <- o que importa")
    print(f"  {'':30} EV REAL   = {ev_real:+.4f}R/trade   (total {r_total:+.1f}R)")
    print(f"  {'':30} EV falso  = {ev_falso:+.4f}R/trade   <- formula bugada")
    veredito = "LUCRATIVO" if ev_real > 0 else "PERDEDOR"
    print(f"  {'':30} >> {veredito}")
    print()
    return {"n": n, "wr": wr, "ev_real": ev_real, "r_total": r_total}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--buf", type=float, default=0.50)
    ap.add_argument("--rr-min", type=float, default=2.0)
    ap.add_argument("--jan", type=int, default=10)
    ap.add_argument("--tf", type=int, default=900)
    a = ap.parse_args()

    cfg = dataclasses.replace(configuracao_scalping_m15(), timeframe_segundos=a.tf)
    print("="*78)
    print(f"FALSO ROMPIMENTO — verificacao com EV correto")
    print(f"jan={a.jan} buf_sl={a.buf}xATR rr_min={a.rr_min} tf={a.tf}s")
    print("="*78 + "\n")

    todas = []
    for ativo in ATIVOS_PADRAO:
        df = backtest.carregar_cache(cfg, ativo)
        if df is None: continue
        ops = simular_falso_rompimento(df, ativo, jan=a.jan,
                                       buf_sl_atr=a.buf, rr_min=a.rr_min)
        todas.extend(ops)

    d = pd.DataFrame(todas)
    if d.empty:
        print("Nenhuma operacao."); return 1

    relatorio_correto(d, "TOTAL (buy+sell)")
    for lado in ["buy", "sell"]:
        relatorio_correto(d[d["lado"] == lado], f"{lado.upper()} apenas")

    # Holdout no BUY (a estrategia que esta rodando ao vivo)
    b = d[d["lado"] == "buy"].sort_values("quando")
    if len(b) >= 60:
        c = len(b)//2
        print("  --- BUY: split temporal ---")
        relatorio_correto(b.iloc[:c], "BUY calibracao")
        relatorio_correto(b.iloc[c:], "BUY holdout")

    # Distribuicao de R:R do BUY
    if len(b) >= 20:
        print("  --- BUY: distribuicao R:R ---")
        for q in [0.10, 0.25, 0.50, 0.75, 0.90, 0.99]:
            print(f"    p{int(q*100):<3} = {b['rr'].quantile(q):8.2f}")
        print(f"    max  = {b['rr'].max():8.2f}")
        print()
        print("  --- BUY: lucro por faixa de R:R ---")
        bb = b.copy()
        bb["faixa"] = pd.cut(bb["rr"], [0,2,3,5,10,1e9],
                             labels=["0-2","2-3","3-5","5-10",">10"])
        for faixa, sub in bb.groupby("faixa", observed=True):
            g = sub[sub["resultado"]=="ganho"]
            lucro = float(g["rr"].sum()) - (len(sub)-len(g))
            print(f"    {str(faixa):>5}: n={len(sub):<4} ganhos={len(g):<4} "
                  f"WR={len(g)/len(sub):5.1%} lucro={lucro:+8.1f}R")
    return 0


if __name__ == "__main__":
    sys.exit(main())
