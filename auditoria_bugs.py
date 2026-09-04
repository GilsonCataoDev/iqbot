"""AUDITORIA — quantifica o impacto de cada bug metodologico encontrado.

Bugs auditados:
  B1. EV = WR * media(R:R)            -> superestima com R:R assimetrico
  B2. quantile(0.25) sobre TODO o df  -> LOOKAHEAD (usa o futuro)
  B3. TP checado antes do SL          -> otimista quando ambos batem na vela
  B4. Trades sobrepostos              -> IC Wilson estreito demais
  B5. Holdout por PAR e nao por TEMPO -> split invalido
  B6. Empate binario contado como perda
  B7. Correlacao entre pares          -> n efetivo << n nominal

Uso: python auditoria_bugs.py
"""
from __future__ import annotations

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


def _sp(a): return SPREAD.get(a, SPREAD_PAD)
def _pip(a): return 0.01 if "JPY" in a else 0.0001


def _atr(df, n=14):
    H, L, C = df["High"], df["Low"], df["Close"]
    tr = pd.concat([H-L, (H-C.shift(1)).abs(), (L-C.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def wilson(w, n, z=1.96):
    if n == 0: return 0.0, 1.0
    p = w/n; d = 1+z*z/n
    c = (p+z*z/(2*n))/d
    m = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/d
    return max(0.0, c-m), min(1.0, c+m)


def carregar(tf=900):
    cfg = dataclasses.replace(configuracao_scalping_m15(), timeframe_segundos=tf)
    return {a: df for a in ATIVOS
            if (df := backtest.carregar_cache(cfg, a)) is not None and len(df) > 1000}


# ---------------------------------------------------------------------------
# Motor de simulacao com flags para ligar/desligar cada bug
# ---------------------------------------------------------------------------

def simular(df, ativo, jan=10, buf=0.50, rr_min=2.0,
            lookahead_quantile=True,   # B2: True = bug original
            tp_primeiro=True,          # B3: True = bug original
            sobrepor=False):           # B4: True = permite trades sobrepostos
    """Falso rompimento com bugs configuraveis."""
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    sp = _sp(ativo)
    atr = _atr(df)
    amp = H - L
    med_amp = amp.shift(1).rolling(jan).mean()

    if lookahead_quantile:
        # BUG: quantile sobre TODA a serie (inclui o futuro)
        limiar = pd.Series(med_amp.quantile(0.25), index=df.index)
    else:
        # CORRETO: quantile expandindo, so com dados passados
        limiar = med_amp.expanding(min_periods=500).quantile(0.25).shift(1)

    rhi = H.shift(1).rolling(jan).max()
    rlo = L.shift(1).rolling(jan).min()
    meio = (rhi + rlo) / 2
    apertado = med_amp <= limiar
    rompe_alta = apertado & (C > rhi)
    rompe_baixa = apertado & (C < rlo)

    ops = []
    i = jan + 14
    n = len(df)
    while i < n - 1:
        a = float(atr.iloc[i])
        if not np.isfinite(a) or a <= 0:
            i += 1; continue
        ra, rb = bool(rompe_alta.iloc[i]), bool(rompe_baixa.iloc[i])
        if not (ra or rb):
            i += 1; continue

        e_idx = i + 1
        if ra:
            lado = "sell"
            ent = float(O.iloc[e_idx]) - sp
            sl = float(H.iloc[i]) + buf*a
            tp = float(meio.iloc[i])
            risco, reward = sl - ent, ent - tp
        else:
            lado = "buy"
            ent = float(O.iloc[e_idx]) + sp
            sl = float(L.iloc[i]) - buf*a
            tp = float(meio.iloc[i])
            risco, reward = ent - sl, tp - ent

        if risco <= 0 or reward <= 0 or reward/risco < rr_min:
            i += 1; continue
        rr = reward/risco

        res = None; j = e_idx
        for j in range(e_idx, min(n, e_idx + 50)):
            hi_j = float(H.iloc[j]) - sp
            lo_j = float(L.iloc[j]) + sp
            if lado == "buy":
                bate_tp, bate_sl = hi_j >= tp, lo_j <= sl
            else:
                bate_tp, bate_sl = lo_j <= tp, hi_j >= sl
            if bate_tp and bate_sl:
                # ambos na mesma vela: quem veio primeiro? nao da pra saber
                res = "ganho" if tp_primeiro else "perda"
                break
            if bate_tp: res = "ganho"; break
            if bate_sl: res = "perda"; break

        if res:
            ops.append({"ativo": ativo, "quando": df.index[e_idx], "lado": lado,
                        "rr": rr, "resultado": res, "saida_idx": j})
        i = (i + 1) if sobrepor else (j + 1)
    return ops


def ev_real(d):
    if d.empty: return 0.0, 0.0, 0
    g = d[d["resultado"] == "ganho"]
    total = float(g["rr"].sum()) - (len(d) - len(g))
    return total/len(d), total, len(d)


def resumo(d, rot):
    if len(d) < 20:
        print(f"    {rot:42} n={len(d):<5} amostra pequena"); return
    ev, tot, n = ev_real(d)
    g = int((d["resultado"] == "ganho").sum())
    wr = g/n
    rr_g = float(d[d["resultado"]=="ganho"]["rr"].mean()) if g else 0
    lo, hi = wilson(g, n)
    be = 1/(1+rr_g) if rr_g else 1
    tag = "OK" if lo > be else ("ruim" if hi < be else "inconclusivo")
    print(f"    {rot:42} n={n:<5} WR={wr:5.1%} EV={ev:+.4f}R tot={tot:+7.1f}R  {tag}")


def main():
    frames = carregar(900)
    print(f"Dados: {len(frames)} pares M15\n")

    # -------------------------------------------------------------------
    print("="*84)
    print("B2 + B3 + B4 — impacto de cada bug no FALSO ROMPIMENTO (BUY)")
    print("="*84)

    cenarios = [
        ("ORIGINAL (todos os bugs)",        dict(lookahead_quantile=True,  tp_primeiro=True,  sobrepor=False)),
        ("sem lookahead no quantile",       dict(lookahead_quantile=False, tp_primeiro=True,  sobrepor=False)),
        ("SL primeiro (conservador)",       dict(lookahead_quantile=True,  tp_primeiro=False, sobrepor=False)),
        ("CORRIGIDO (sem lookahead+SL 1o)", dict(lookahead_quantile=False, tp_primeiro=False, sobrepor=False)),
    ]

    guardado = {}
    for nome, kw in cenarios:
        todas = []
        for ativo, df in frames.items():
            todas.extend(simular(df, ativo, **kw))
        d = pd.DataFrame(todas)
        guardado[nome] = d
        print(f"\n  {nome}:")
        if d.empty:
            print("    sem operacoes"); continue
        resumo(d, "TOTAL")
        for lado in ["buy", "sell"]:
            resumo(d[d["lado"] == lado], f"{lado.upper()}")

    # -------------------------------------------------------------------
    print("\n" + "="*84)
    print("B5 — holdout por PAR (bug) vs por TEMPO (correto)")
    print("="*84)
    d = guardado["CORRIGIDO (sem lookahead+SL 1o)"]
    b = d[d["lado"] == "buy"].copy()
    if len(b) >= 60:
        print("\n  BUY corrigido:")
        c = len(b)//2
        print("    -- split por ORDEM DE CONCATENACAO (como estava = por par) --")
        resumo(b.iloc[:c], "primeira metade (pares 1-3)")
        resumo(b.iloc[c:], "segunda metade (pares 4-7)")
        bt = b.sort_values("quando")
        print("    -- split por TEMPO (correto) --")
        resumo(bt.iloc[:c], f"calibracao (ate {bt.iloc[c]['quando']:%d/%m/%Y})")
        resumo(bt.iloc[c:], "holdout")

    # -------------------------------------------------------------------
    print("\n" + "="*84)
    print("B7 — correlacao entre pares: n efetivo << n nominal")
    print("="*84)
    if not b.empty:
        print("\n  Resultado por par (se todos concordam, sao o MESMO trade repetido):")
        for ativo in sorted(b["ativo"].unique()):
            sub = b[b["ativo"] == ativo]
            resumo(sub, ativo)
        # concordancia temporal: trades no mesmo dia entre pares
        b2 = b.copy()
        b2["dia"] = pd.to_datetime(b2["quando"]).dt.normalize()
        por_dia = b2.groupby("dia").size()
        print(f"\n    dias com sinal: {len(por_dia)}")
        print(f"    sinais por dia: media={por_dia.mean():.2f} max={por_dia.max()}")
        print(f"    n nominal={len(b)}  |  n efetivo (dias unicos)={len(por_dia)}")
        # recalcula IC com n efetivo
        g = int((b["resultado"]=="ganho").sum())
        wr = g/len(b)
        lo_n, hi_n = wilson(g, len(b))
        lo_e, hi_e = wilson(int(wr*len(por_dia)), len(por_dia))
        print(f"    IC com n nominal : [{lo_n:.1%}, {hi_n:.1%}]")
        print(f"    IC com n efetivo : [{lo_e:.1%}, {hi_e:.1%}]  <- honesto")

    # -------------------------------------------------------------------
    print("\n" + "="*84)
    print("B6 — empates no modo binario")
    print("="*84)
    tot_e = tot_n = 0
    for ativo, df in frames.items():
        emp = int((df["Close"] == df["Open"]).sum())
        tot_e += emp; tot_n += len(df)
    print(f"\n    velas com Close == Open: {tot_e} de {tot_n} ({tot_e/tot_n:.3%})")
    print("    (contadas como PERDA no teste binario -> vies pequeno pra baixo)")

    # -------------------------------------------------------------------
    print("\n" + "="*84)
    print("B2 — quanto o limiar de acumulacao muda com/sem lookahead")
    print("="*84)
    for ativo, df in list(frames.items())[:3]:
        amp = df["High"] - df["Low"]
        med = amp.shift(1).rolling(10).mean()
        q_global = med.quantile(0.25)
        q_exp = med.expanding(min_periods=500).quantile(0.25).shift(1)
        pip = _pip(ativo)
        dif = (q_exp - q_global).abs().dropna()
        print(f"\n    {ativo}: limiar global={q_global/pip:.2f}p")
        print(f"      limiar expanding: p10={q_exp.quantile(0.10)/pip:.2f}p "
              f"p50={q_exp.quantile(0.50)/pip:.2f}p p90={q_exp.quantile(0.90)/pip:.2f}p")
        print(f"      diferenca mediana = {dif.median()/pip:.2f} pips "
              f"({dif.median()/q_global:.1%} do limiar)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
