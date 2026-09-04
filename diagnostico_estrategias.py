"""Diagnostico: POR QUE as estrategias falham.

Tres perguntas:
  A. O R:R=24 da estrategia 5 e real ou outlier?
  B. O spread esta comendo o edge? (risco em pips vs spread)
  C. O sinal tem direcao mas o SL/TP esta errado?

Uso:
    python diagnostico_estrategias.py --parte A
    python diagnostico_estrategias.py --parte B
    python diagnostico_estrategias.py --parte C
"""
from __future__ import annotations

import argparse
import dataclasses
import math
import sys

import numpy as np
import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15

ATIVOS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD"]
SPREAD = {"EURJPY": 0.015, "USDJPY": 0.015}
SPREAD_PAD = 0.00012


def _spread(a): return SPREAD.get(a, SPREAD_PAD)
def _pip(a): return 0.01 if "JPY" in a else 0.0001


def _atr(df, n=14):
    H, L, C = df["High"], df["Low"], df["Close"]
    tr = pd.concat([H - L, (H - C.shift(1)).abs(), (L - C.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def wilson(w, n, z=1.96):
    if n == 0: return 0.0, 1.0
    p = w / n; d = 1 + z*z/n
    c = (p + z*z/(2*n))/d
    m = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/d
    return max(0.0, c-m), min(1.0, c+m)


def carregar(tf=900, ativos=None):
    cfg = dataclasses.replace(configuracao_scalping_m15(), timeframe_segundos=tf)
    out = {}
    for a in (ativos or ATIVOS):
        df = backtest.carregar_cache(cfg, a)
        if df is not None and len(df) > 500:
            out[a] = df
    return out


# ===========================================================================
# PARTE A — Distribuicao de R:R da estrategia 5 (gap Londres)
# ===========================================================================

def parte_a(frames):
    print("="*80)
    print("PARTE A — Estrategia 5 (Gap Londres): o R:R=24 e real?")
    print("="*80)

    todos = []
    for ativo, df in frames.items():
        atr = _atr(df)
        sp = _spread(ativo); pip = _pip(ativo)
        H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]

        for data in df.index.normalize().unique():
            asi = df[(df.index.normalize() == data) & (df.index.hour >= 0) & (df.index.hour < 7)]
            if len(asi) < 3:
                continue
            rhi, rlo = float(asi["High"].max()), float(asi["Low"].min())
            centro = (rhi + rlo) / 2

            lon = df[(df.index.normalize() == data) & (df.index.hour == 7)]
            if len(lon) == 0:
                lon = df[(df.index.normalize() == data) & (df.index.hour == 8)]
            if len(lon) == 0:
                continue

            for ts, row in lon.iterrows():
                i = df.index.get_loc(ts)
                if i + 1 >= len(df): continue
                op = float(row["Open"])
                atr_i = float(atr.iloc[i])
                if not np.isfinite(atr_i) or atr_i <= 0: continue

                if op > rhi:
                    lado = "sell"; sl_raw = op + atr_i*0.5; tp_raw = centro
                    entrada = float(O.iloc[i+1]) - sp
                    risco = sl_raw + sp - entrada
                    reward = entrada - (tp_raw - sp)
                elif op < rlo:
                    lado = "buy"; sl_raw = op - atr_i*0.5; tp_raw = centro
                    entrada = float(O.iloc[i+1]) + sp
                    risco = entrada - (sl_raw - sp)
                    reward = (tp_raw + sp) - entrada
                else:
                    continue

                if risco <= 0 or reward <= 0: continue
                rr = reward / risco

                # resolve
                res = None; nv = 0
                for j in range(i+1, min(len(df), i+61)):
                    nv = j - i
                    if lado == "buy":
                        if float(H.iloc[j]) - sp >= tp_raw + sp: res = "ganho"; break
                        if float(L.iloc[j]) + sp <= sl_raw - sp: res = "perda"; break
                    else:
                        if float(L.iloc[j]) + sp <= tp_raw - sp: res = "ganho"; break
                        if float(H.iloc[j]) - sp >= sl_raw + sp: res = "perda"; break
                if res:
                    todos.append({"ativo": ativo, "lado": lado, "rr": rr,
                                  "risco_pips": risco/pip, "reward_pips": reward/pip,
                                  "res": res, "n_velas": nv,
                                  "gap_pips": abs(op - (rhi if lado=="sell" else rlo))/pip})

    d = pd.DataFrame(todos)
    if d.empty:
        print("Sem operacoes."); return

    print(f"\n  n={len(d)}  WR={(d['res']=='ganho').mean():.1%}")
    print(f"\n  DISTRIBUICAO DO R:R:")
    for q in [0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]:
        print(f"    p{int(q*100):<3} = {d['rr'].quantile(q):>10.2f}")
    print(f"    max  = {d['rr'].max():>10.2f}")
    print(f"    media= {d['rr'].mean():>10.2f}   mediana={d['rr'].median():.2f}")

    print(f"\n  RISCO EM PIPS (o SL esta perto demais?):")
    for q in [0.10, 0.25, 0.50, 0.75, 0.90]:
        print(f"    p{int(q*100):<3} = {d['risco_pips'].quantile(q):>8.1f} pips")
    sp_pips = {a: _spread(a)/_pip(a) for a in frames}
    print(f"    spread tipico: {list(sp_pips.values())[0]:.1f} pips")
    frac = (d["risco_pips"] < 3).mean()
    print(f"    >> {frac:.1%} das ops tem risco < 3 pips (SL absurdamente apertado)")

    print(f"\n  TESTE: remover outliers de R:R")
    for cap in [3, 5, 10, 20]:
        sub = d[d["rr"] <= cap]
        if len(sub) < 20: continue
        wr = (sub["res"]=="ganho").mean()
        rrm = sub["rr"].mean()
        ev = wr*rrm - (1-wr)
        lo, hi = wilson(int((sub['res']=='ganho').sum()), len(sub))
        be = 1/(1+rrm)
        tag = "APROVADO" if lo > be else "reprovado"
        print(f"    R:R <= {cap:<3}  n={len(sub):<5} WR={wr:5.1%} R:R={rrm:5.2f} EV={ev:+.3f}R  {tag}")

    print(f"\n  TESTE: filtrar risco minimo (SL nao pode ser apertado demais)")
    for min_pips in [3, 5, 8, 12]:
        sub = d[d["risco_pips"] >= min_pips]
        if len(sub) < 20: continue
        wr = (sub["res"]=="ganho").mean()
        rrm = sub["rr"].mean()
        ev = wr*rrm - (1-wr)
        lo, hi = wilson(int((sub['res']=='ganho').sum()), len(sub))
        be = 1/(1+rrm)
        tag = "APROVADO" if lo > be else "reprovado"
        print(f"    risco >= {min_pips:<3}p n={len(sub):<5} WR={wr:5.1%} R:R={rrm:5.2f} EV={ev:+.3f}R  {tag}")

    print(f"\n  CONTRIBUICAO DE LUCRO por faixa de R:R (onde esta o lucro?):")
    d["faixa"] = pd.cut(d["rr"], [0,2,5,10,50,1e9], labels=["0-2","2-5","5-10","10-50",">50"])
    for faixa, sub in d.groupby("faixa", observed=True):
        g = (sub["res"]=="ganho").sum()
        lucro = (sub[sub["res"]=="ganho"]["rr"].sum()) - (sub["res"]=="perda").sum()
        print(f"    {str(faixa):>6}: n={len(sub):<5} ganhos={g:<4} lucro_total={lucro:+9.1f}R")


# ===========================================================================
# PARTE B — Spread vs tamanho do movimento por timeframe
# ===========================================================================

def parte_b():
    print("="*80)
    print("PARTE B — O spread esta comendo o edge? (custo por timeframe)")
    print("="*80)
    print("\n  Regra: se o spread for > 10% do ATR, o custo de transacao domina.\n")

    for tf, nome in [(60,"M1"), (300,"M5"), (900,"M15"), (3600,"H1")]:
        frames = carregar(tf)
        if not frames:
            print(f"  {nome:4} sem dados")
            continue
        print(f"  {nome}:")
        for ativo, df in frames.items():
            atr = _atr(df).dropna()
            if len(atr) == 0: continue
            atr_med = float(atr.median())
            sp = _spread(ativo); pip = _pip(ativo)
            pct = sp / atr_med * 100
            flag = "  <-- CUSTO DOMINA" if pct > 10 else ""
            print(f"    {ativo}  ATR_med={atr_med/pip:6.1f}p  spread={sp/pip:4.1f}p  "
                  f"custo={pct:5.1f}% do ATR{flag}")
        print()


# ===========================================================================
# PARTE C — O sinal tem direcao? Sweep de geometria SL/TP
# ===========================================================================

def sinais_fibo(df, n_impulso=5, impulso_min_atr=1.5, fib_lo=0.382, fib_hi=0.618):
    """Reaproveita a deteccao Fibo (que deu 58.1% WR) e retorna os indices."""
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    atr = _atr(df)
    buys, sells = [], []
    for i in range(n_impulso+2, len(df)-1):
        a = float(atr.iloc[i])
        if not np.isfinite(a) or a <= 0: continue
        seg = df.iloc[i-n_impulso:i]
        amp = float(seg["High"].max()) - float(seg["Low"].min())
        shi, slo = float(seg["High"].max()), float(seg["Low"].min())
        if amp < impulso_min_atr*a: continue
        pc = float(C.iloc[i])
        corpo = abs(float(C.iloc[i]) - float(O.iloc[i]))

        ret_alta = (shi - pc)/amp if amp > 0 else 0
        if float(C.iloc[i-n_impulso]) < float(C.iloc[i-1]) and fib_lo <= ret_alta <= fib_hi:
            mi = min(float(O.iloc[i]), float(C.iloc[i])) - float(L.iloc[i])
            pin = mi >= 2*(corpo+1e-10) and float(C.iloc[i]) > float(O.iloc[i])
            eng = (float(C.iloc[i]) > float(O.iloc[i]) and float(O.iloc[i]) <= float(C.iloc[i-1])
                   and float(C.iloc[i]) >= float(O.iloc[i-1]))
            if pin or eng: buys.append((i, slo, shi, a))

        ret_baixa = (pc - slo)/amp if amp > 0 else 0
        if float(C.iloc[i-n_impulso]) > float(C.iloc[i-1]) and fib_lo <= ret_baixa <= fib_hi:
            ms = float(H.iloc[i]) - max(float(O.iloc[i]), float(C.iloc[i]))
            pin = ms >= 2*(corpo+1e-10) and float(C.iloc[i]) < float(O.iloc[i])
            eng = (float(C.iloc[i]) < float(O.iloc[i]) and float(O.iloc[i]) >= float(C.iloc[i-1])
                   and float(C.iloc[i]) <= float(O.iloc[i-1]))
            if pin or eng: sells.append((i, slo, shi, a))
    return buys, sells


def parte_c(frames):
    print("="*80)
    print("PARTE C — O sinal Fibo tem direcao? (58.1% WR mas R:R=0.50)")
    print("="*80)

    # C1: MFE/MAE — quanto o preco anda a favor antes de andar contra?
    print("\n  C1. MFE/MAE: ate onde o preco vai a favor vs contra (em ATR)")
    print("      Se MFE >> MAE, o sinal tem direcao e so o SL/TP esta errado.\n")

    reg = []
    for ativo, df in frames.items():
        H, L, O = df["High"], df["Low"], df["Open"]
        sp = _spread(ativo)
        buys, sells = sinais_fibo(df)
        for lado, lista in (("buy", buys), ("sell", sells)):
            for (i, slo, shi, a) in lista:
                if i+1 >= len(df)-40: continue
                ent = float(O.iloc[i+1]) + (sp if lado=="buy" else -sp)
                jan = df.iloc[i+1:i+41]
                if lado == "buy":
                    mfe = (float(jan["High"].max()) - ent)/a
                    mae = (ent - float(jan["Low"].min()))/a
                else:
                    mfe = (ent - float(jan["Low"].min()))/a
                    mae = (float(jan["High"].max()) - ent)/a
                reg.append({"ativo": ativo, "lado": lado, "mfe": mfe, "mae": mae})

    r = pd.DataFrame(reg)
    if r.empty:
        print("      sem sinais"); return
    print(f"      n={len(r)}")
    print(f"      MFE mediana = {r['mfe'].median():.2f} ATR   (quanto anda a favor)")
    print(f"      MAE mediana = {r['mae'].median():.2f} ATR   (quanto anda contra)")
    razao = r['mfe'].median()/max(r['mae'].median(), 1e-9)
    print(f"      razao MFE/MAE = {razao:.2f}")
    if razao > 1.15:
        print("      >> TEM DIRECAO. O problema e a geometria do SL/TP.")
    elif razao < 0.87:
        print("      >> DIRECAO INVERTIDA. Operar ao contrario?")
    else:
        print("      >> SEM DIRECAO. O sinal e ruido — nao adianta mexer no SL/TP.")

    # C2: sweep de geometria
    print("\n  C2. Sweep SL/TP em multiplos de ATR (busca EV > 0)\n")
    print(f"      {'SL(ATR)':>8} {'TP(ATR)':>8} {'n':>6} {'WR':>7} {'R:R':>6} {'EV':>8}  tag")

    melhores = []
    for sl_m in [0.5, 0.75, 1.0, 1.5, 2.0]:
        for tp_m in [0.5, 1.0, 1.5, 2.0, 3.0]:
            g = p = 0; rrs = []
            for ativo, df in frames.items():
                H, L, O = df["High"], df["Low"], df["Open"]
                sp = _spread(ativo)
                buys, sells = sinais_fibo(df)
                for lado, lista in (("buy", buys), ("sell", sells)):
                    for (i, slo, shi, a) in lista:
                        if i+1 >= len(df)-1: continue
                        if lado == "buy":
                            ent = float(O.iloc[i+1]) + sp
                            sl_p, tp_p = ent - sl_m*a, ent + tp_m*a
                        else:
                            ent = float(O.iloc[i+1]) - sp
                            sl_p, tp_p = ent + sl_m*a, ent - tp_m*a
                        risco = abs(ent - sl_p); reward = abs(tp_p - ent)
                        if risco <= 0: continue
                        rrs.append(reward/risco)
                        for j in range(i+1, min(len(df), i+61)):
                            if lado == "buy":
                                if float(H.iloc[j]) - sp >= tp_p: g += 1; break
                                if float(L.iloc[j]) + sp <= sl_p: p += 1; break
                            else:
                                if float(L.iloc[j]) + sp <= tp_p: g += 1; break
                                if float(H.iloc[j]) - sp >= sl_p: p += 1; break
            n = g + p
            if n < 50: continue
            wr = g/n; rrm = float(np.mean(rrs))
            ev = wr*rrm - (1-wr)
            lo, _ = wilson(g, n); be = 1/(1+rrm)
            tag = "APROVADO" if lo > be else ""
            print(f"      {sl_m:>8.2f} {tp_m:>8.2f} {n:>6} {wr:>6.1%} {rrm:>6.2f} {ev:>+8.3f}  {tag}")
            melhores.append((ev, sl_m, tp_m, n, wr, rrm, tag))

    if melhores:
        melhores.sort(reverse=True)
        ev, sl_m, tp_m, n, wr, rrm, tag = melhores[0]
        print(f"\n      MELHOR: SL={sl_m}ATR TP={tp_m}ATR -> n={n} WR={wr:.1%} EV={ev:+.3f}R {tag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parte", default="ABC")
    ap.add_argument("--tf", type=int, default=900)
    ap.add_argument("--ativos", nargs="*", default=None)
    a = ap.parse_args()

    if "B" in a.parte.upper():
        parte_b()

    if "A" in a.parte.upper() or "C" in a.parte.upper():
        frames = carregar(a.tf, a.ativos)
        print(f"\nDados: {len(frames)} pares, tf={a.tf}s\n")
        if "A" in a.parte.upper():
            parte_a(frames)
        if "C" in a.parte.upper():
            parte_c(frames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
