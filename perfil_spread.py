"""Perfil de spread real por hora, e o que ele faz com o edge de forex.

Contexto: o estudo de forex concluiu "sem edge" usando spread ASSUMIDO de 1.2
pips (majors) e 1.5 (JPY). Depois descobrimos que o edge existe nas horas 21-22h
UTC (+0.21R) e que ele sobrevive ate ~3.5x o spread assumido, morrendo acima de
~4.2 pips no EURUSD. As primeiras medidas reais deram 0.1-0.9 pip — MUITO abaixo
do assumido.

Se isso se confirmar no perfil por hora, duas coisas mudam:
  1. O edge de 21-22h fica com folga grande em vez de marginal.
  2. O estudo de forex inteiro (300 configuracoes, todas negativas) foi rodado
     com custo inflado em ~6x e merece ser reprocessado.

Este script:
  - Perfil de spread por hora (mediana, p90) por par e agregado
  - Compara a janela do edge (21-22h) com o resto
  - Reprocessa o EV do forex usando o spread MEDIDO no lugar do assumido

Uso:
    python perfil_spread.py                 (perfil so)
    python perfil_spread.py --reprocessar   (roda tambem o backtest com spread real)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import motor_sinais as M

ARQ = Path(__file__).resolve().parent / "diario" / "spread_real.csv"
HORAS_EDGE = [21, 22]
ASSUMIDO = {"EURJPY": 1.5, "USDJPY": 1.5, "GBPJPY": 1.5}
ASSUMIDO_PAD = 1.2


def assumido(a: str) -> float:
    return ASSUMIDO.get(a, ASSUMIDO_PAD)


def carregar() -> pd.DataFrame | None:
    if not ARQ.exists():
        print(f"Sem dados ainda: {ARQ}")
        print("O coletor roda junto com MONITOR_BINARIA.bat, amostra a cada 2min.")
        return None
    d = pd.read_csv(ARQ)
    d["ts"] = pd.to_datetime(d["ts_utc"])
    return d


def cobertura(d: pd.DataFrame) -> bool:
    horas = set(d["hora_utc"].unique())
    faltam = sorted(set(range(24)) - horas)
    dur = (d["ts"].max() - d["ts"].min()).total_seconds() / 3600
    print(f"  amostras: {len(d)}  |  janela: {dur:.1f}h  "
          f"({d['ts'].min():%d/%m %H:%M} a {d['ts'].max():%d/%m %H:%M} UTC)")
    print(f"  horas cobertas: {len(horas)}/24")
    if faltam:
        print(f"  PARCIAL — faltam as horas: {faltam}")
    falta_edge = [h for h in HORAS_EDGE if h not in horas]
    if falta_edge:
        print(f"  >> A janela do edge ({HORAS_EDGE}) AINDA NAO foi amostrada: {falta_edge}")
        print(f"     O numero que decide ainda nao existe. Rode de novo mais tarde.")
        return False
    return True


def perfil(d: pd.DataFrame) -> None:
    print(f"\n{'='*80}")
    print("SPREAD POR HORA (mediana em pips, agregado dos pares)")
    print(f"{'='*80}")
    g = d.groupby("hora_utc")["spread_pips"].agg(["count", "median", "quantile"])
    med = d.groupby("hora_utc")["spread_pips"].median()
    p90 = d.groupby("hora_utc")["spread_pips"].quantile(0.90)
    n = d.groupby("hora_utc")["spread_pips"].size()
    mx = float(med.max()) if len(med) else 1.0
    for h in sorted(med.index):
        marca = "  <-- janela do edge" if h in HORAS_EDGE else ""
        bar = "#" * int(med[h] / max(mx, 1e-9) * 42)
        print(f"  {h:02d}h  n={int(n[h]):>4}  mediana={med[h]:>5.2f}p  "
              f"p90={p90[h]:>5.2f}p  {bar}{marca}")

    edge = d[d["hora_utc"].isin(HORAS_EDGE)]["spread_pips"]
    resto = d[~d["hora_utc"].isin(HORAS_EDGE)]["spread_pips"]
    if len(edge) and len(resto):
        print(f"\n  janela do edge (21-22h): mediana {edge.median():.2f}p  p90 {edge.quantile(.9):.2f}p")
        print(f"  demais horas           : mediana {resto.median():.2f}p  p90 {resto.quantile(.9):.2f}p")
        print(f"  razao: {edge.median()/max(resto.median(),1e-9):.2f}x")

    print(f"\n{'='*80}")
    print("MEDIDO vs ASSUMIDO, por par")
    print(f"{'='*80}")
    print(f"  {'par':8} {'n':>5} {'mediana':>8} {'p90':>7} {'assumido':>9} {'razao':>7}")
    for a in sorted(d["ativo"].unique()):
        s = d[d["ativo"] == a]["spread_pips"]
        asm = assumido(a)
        print(f"  {a:8} {len(s):>5} {s.median():>7.2f}p {s.quantile(.9):>6.2f}p "
              f"{asm:>8.1f}p {s.median()/asm:>6.2f}x")

    # o numero que decide
    if len(edge):
        print(f"\n{'='*80}")
        print("VEREDITO")
        print(f"{'='*80}")
        LIMITE = 4.2   # onde o edge de 21-22h morre (medido por sensibilidade)
        print(f"  O edge de +0.21R nas horas 21-22h morre com spread acima de ~{LIMITE}p.")
        print(f"  Spread medido nessas horas: mediana {edge.median():.2f}p, p90 {edge.quantile(.9):.2f}p")
        folga = LIMITE / max(edge.quantile(.9), 1e-9)
        if edge.quantile(.9) < LIMITE:
            print(f"  >> SOBREVIVE com folga de {folga:.1f}x mesmo no p90.")
        else:
            print(f"  >> AMEACADO: o p90 ja passa do limite.")


def reprocessar(d: pd.DataFrame) -> None:
    """Roda o backtest de forex com o spread MEDIDO por par."""
    print(f"\n{'='*80}")
    print("REPROCESSANDO O FOREX COM SPREAD MEDIDO")
    print(f"{'='*80}")
    medido = d.groupby("ativo")["spread_pips"].median().to_dict()
    novo = {a: v * M.pip(a) for a, v in medido.items()}
    print("  substituindo o spread assumido pelo medido:")
    for a in sorted(novo):
        print(f"    {a}: {assumido(a):.1f}p -> {medido[a]:.2f}p")

    base_sp, base_pad = dict(M.SPREAD), M.SPREAD_PAD
    M.SPREAD = novo
    M.SPREAD_PAD = float(np.median(list(novo.values()))) if novo else base_pad

    P = list(novo.keys())
    fr = M.carregar(900, P)
    print(f"\n  {'SL':>4} {'TP':>4} | {'todas as horas':^22} | {'21-22h UTC':^22}")
    print(f"  {'':>4} {'':>4} | {'n':>6} {'WR':>6} {'EV':>8} | {'n':>6} {'WR':>6} {'EV':>8}")
    for sl, tp in [(1.0, 2.0), (1.5, 2.0), (2.0, 2.0), (2.0, 3.0), (3.0, 3.0)]:
        x = M.simular_forex(fr, M.SINAIS["falso rompimento"], sl, tp)
        x = x[x["lado"] == "long"].copy()
        if x.empty:
            continue
        x["h"] = pd.to_datetime(x["quando"]).dt.hour

        def st(y):
            if len(y) < 30:
                return None
            g = y[y["resultado"] == "ganho"]
            return len(y), len(g) / len(y), (float(g["rr"].sum()) - (len(y) - len(g))) / len(y)
        a_, b_ = st(x), st(x[x["h"].isin(HORAS_EDGE)])
        sa = f"{a_[0]:>6} {a_[1]:>5.1%} {a_[2]:>+8.4f}" if a_ else " " * 21
        sb = f"{b_[0]:>6} {b_[1]:>5.1%} {b_[2]:>+8.4f}" if b_ else " " * 21
        print(f"  {sl:>4.1f} {tp:>4.1f} | {sa} | {sb}")

    M.SPREAD, M.SPREAD_PAD = base_sp, base_pad
    print("\n  Comparar com o estudo original (spread assumido): todas as horas")
    print("  davam EV negativo em 298 de 300 configuracoes.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reprocessar", action="store_true")
    a = ap.parse_args()

    d = carregar()
    if d is None:
        return 1
    print("=" * 80)
    print("PERFIL DE SPREAD REAL")
    print("=" * 80)
    completo = cobertura(d)
    perfil(d)
    if a.reprocessar:
        if not completo:
            print("\n  (reprocessando com cobertura parcial — trate como preliminar)")
        reprocessar(d)
    elif completo:
        print("\n  Rode com --reprocessar para refazer o backtest de forex com estes numeros.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
