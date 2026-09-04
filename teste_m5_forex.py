"""Forex no M5 com SPREAD REAL — o teste que nunca foi feito direito.

Por que agora: o estudo que declarou "forex morto" (300 configuracoes, 298
negativas) usou spread ASSUMIDO de 1.2 pips. Medido ao vivo via
get_realtime_candles, o spread real e ~0.4 pip. Isso muda o custo por
timeframe:

    tf    ATR     assumido      REAL
    M5    3.0p    43.8% DOMINA  9.7%  ok
    M15   5.8p    24.4% DOMINA  8.9%  ok

M5 foi descartado sem teste serio justamente pelo custo. Com 9.7% ele entra
na faixa jogavel — e tem ~3x mais sinais que M15, o que resolve o problema
de frequencia (1.5 sinal/dia no M15 e pouco para ser util).

IMPORTANTE: spread menor REDUZ CUSTO, nao cria edge. Se o sinal for neutro
no M5, continua neutro. O que muda e poder medir sem o custo mascarar tudo.

Uso:
    python teste_m5_forex.py
    python teste_m5_forex.py --tf 300 900   (compara M5 e M15)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import motor_sinais as M

PARES = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD",
         "EURGBP", "EURCHF", "EURCAD", "GBPJPY", "GBPCHF", "GBPCAD", "CADCHF"]
ARQ_SPREAD = Path(__file__).resolve().parent / "diario" / "spread_real.csv"


def spread_medido() -> dict[str, float]:
    """Spread real por par, em preco. Cai para o assumido se nao houver medida."""
    if not ARQ_SPREAD.exists():
        print("  AVISO: sem spread_real.csv — usando o assumido (pessimista).")
        return {}
    d = pd.read_csv(ARQ_SPREAD)
    med = d.groupby("ativo")["spread_pips"].median()
    print(f"  spread real: {len(d)} amostras, mediana geral "
          f"{d['spread_pips'].median():.2f} pips")
    return {a: float(v) * M.pip(a) for a, v in med.items()}


def aplicar_spread(novo: dict[str, float]):
    if not novo:
        return None
    antigo = (dict(M.SPREAD), M.SPREAD_PAD)
    M.SPREAD = dict(novo)
    M.SPREAD_PAD = float(np.median(list(novo.values())))
    return antigo


def restaurar(antigo):
    if antigo:
        M.SPREAD, M.SPREAD_PAD = antigo


def bloco_binario(frames, nome_tf: str) -> None:
    """Binaria nao tem spread — serve de controle da qualidade do sinal."""
    print(f"\n  BINARIA {nome_tf} (sem spread; breakeven 54.05%)")
    print(f"    {'sinal':26} {'n':>7} {'n_ef':>6} {'WR':>7}  veredito")
    base = None
    for nome in ["aleatorio (BASELINE)", "falso rompimento", "rompimento real",
                 "reversao a media (z>2)", "vela extrema (rev)"]:
        d = M.avaliar_binario(frames, M.SINAIS[nome])
        if d.empty or len(d) < 200:
            continue
        s = M.stats_binario(d, 0.5405)
        if "BASELINE" in nome:
            base = s["wr"]
        vd = "EDGE" if s["lo_ef"] > 0.5405 else ("neg" if s["hi_ef"] < 0.5405 else "---")
        dif = "" if base is None or "BASELINE" in nome else f"  ({(s['wr']-base)*100:+.1f}pp)"
        print(f"    {nome:26} {s['n']:>7} {s['n_ef']:>6} {s['wr']:>6.2%}  {vd}{dif}")


def bloco_forex(frames, nome_tf: str, horas: list[int] | None) -> list[dict]:
    rot = "todas as horas" if not horas else f"horas {horas} UTC"
    print(f"\n  FOREX {nome_tf} — {rot}")
    print(f"    {'SL':>4} {'TP':>4} {'n':>6} {'WR':>6} {'EV':>9} {'total':>9}")
    out = []
    for sl in [1.0, 1.5, 2.0]:
        for tp in [1.5, 2.0, 3.0, 4.0]:
            d = M.simular_forex(frames, M.SINAIS["falso rompimento"], sl, tp)
            d = d[d["lado"] == "long"].copy()
            if d.empty:
                continue
            if horas:
                d["h"] = pd.to_datetime(d["quando"]).dt.hour
                d = d[d["h"].isin(horas)]
            if len(d) < 60:
                continue
            g = d[d["resultado"] == "ganho"]
            tot = float(g["rr"].sum()) - (len(d) - len(g))
            ev = tot / len(d)
            marca = "  <-- positivo" if ev > 0 else ""
            print(f"    {sl:>4.1f} {tp:>4.1f} {len(d):>6} {len(g)/len(d):>5.1%} "
                  f"{ev:>+9.4f} {tot:>+9.1f}{marca}")
            out.append({"tf": nome_tf, "horas": rot, "sl": sl, "tp": tp,
                        "n": len(d), "ev": ev, "wr": len(g)/len(d)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", nargs="*", type=int, default=[300, 900])
    a = ap.parse_args()

    print("=" * 84)
    print("FOREX COM SPREAD REAL — M5 vs M15")
    print("=" * 84)
    novo = spread_medido()
    antigo = aplicar_spread(novo)
    if novo:
        amostra = sorted(novo.items())[:4]
        print("  aplicado: " + ", ".join(
            f"{k} {v/M.pip(k):.2f}p" for k, v in amostra) + " ...")

    todos = []
    try:
        for tf in a.tf:
            nome = M.TF_NOME.get(tf, str(tf))
            frames = M.carregar(tf, PARES)
            if not frames:
                print(f"\n  {nome}: sem dados"); continue
            n_med = int(np.median([len(v) for v in frames.values()]))
            print(f"\n{'='*84}")
            print(f"{nome} — {len(frames)} pares, ~{n_med} velas cada")
            print(f"{'='*84}")
            bloco_binario(frames, nome)
            todos += bloco_forex(frames, nome, None)
            todos += bloco_forex(frames, nome, [21, 22])
    finally:
        restaurar(antigo)

    if todos:
        t = pd.DataFrame(todos)
        pos = t[t["ev"] > 0].sort_values("ev", ascending=False)
        print(f"\n{'='*84}")
        print("CONFIGURACOES POSITIVAS")
        print(f"{'='*84}")
        if pos.empty:
            print("  Nenhuma. O spread menor nao criou edge onde nao havia.")
        else:
            print(f"  {'tf':5} {'janela':18} {'SL':>4} {'TP':>4} {'n':>6} "
                  f"{'WR':>6} {'EV':>9}")
            for _, x in pos.head(12).iterrows():
                print(f"  {x['tf']:5} {x['horas']:18} {x['sl']:>4.1f} {x['tp']:>4.1f} "
                      f"{x['n']:>6} {x['wr']:>5.1%} {x['ev']:>+9.4f}")
            print(f"\n  {len(pos)} de {len(t)} configuracoes positivas "
                  f"(esperado por acaso se nao houvesse edge: ~{len(t)/2:.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
