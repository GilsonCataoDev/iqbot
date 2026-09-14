"""Mede o ema920_pullback por periodo e por par, para separar regime de estrategia.

O backtest cobre 202 dias e da 47,4%. O Lab ao vivo cobre os ultimos 10 e da
61,4%. Enquanto os dois numeros forem lidos lado a lado como se medissem a
mesma coisa, nao da para saber se a estrategia funciona agora ou se os 10
dias foram sorte.
"""
from __future__ import annotations

import math
from collections import defaultdict

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_ema_laboratorio_practice
from iqoption_m5.estrategia import EstrategiaReversaoM5
from iqoption_m5.laboratorio_ema import _config_rastro

PARES = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY"]
EXPIRACAO_CANDLES = 3
JANELA_LAB = (pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-11"))
# Lab ao vivo na mesma janela: par -> (ordens, wins)
VIVO = {"EURUSD": (89, 59), "GBPUSD": (7, 2), "USDJPY": (10, 5),
        "AUDUSD": (2, 0), "USDCAD": (5, 2), "EURJPY": (8, 8)}


def wilson(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 1.0
    p = w / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - m), min(1.0, c + m)


def main() -> None:
    cfg = _config_rastro(configuracao_ema_laboratorio_practice(), 300, "ema920_pullback")
    por_mes = defaultdict(lambda: [0, 0])
    janela = [0, 0]
    janela_par = defaultdict(lambda: [0, 0])

    for ativo in PARES:
        candles = backtest.carregar_cache(cfg, ativo)
        if candles is None or len(candles) < 100:
            print(f"  {ativo}: sem dados — pulando")
            continue
        pos_de = {h: p for p, h in enumerate(candles.index)}
        n = 0
        for d in EstrategiaReversaoM5(cfg).sinais_historicos(ativo, candles):
            pos = pos_de.get(d.candle_hora)
            if pos is None or pos + EXPIRACAO_CANDLES >= len(candles):
                continue
            ab = float(candles.iloc[pos + 1]["Open"])
            fe = float(candles.iloc[pos + EXPIRACAO_CANDLES]["Close"])
            if fe == ab:
                continue
            ganhou = int((fe > ab) if d.direcao == "call" else (fe < ab))
            quando = pd.Timestamp(d.candle_hora)
            mes = por_mes[quando.strftime("%Y-%m")]
            mes[0] += 1
            mes[1] += ganhou
            if JANELA_LAB[0] <= quando < JANELA_LAB[1]:
                janela[0] += 1
                janela[1] += ganhou
                jp = janela_par[ativo]
                jp[0] += 1
                jp[1] += ganhou
            n += 1
        print(f"  {ativo}: {n} sinais")

    barra = "=" * 66
    print("\n" + barra)
    print("POR MES — a estrategia depende de regime?")
    print(barra)
    print(f"{'mes':<10}{'n':>7}{'acerto':>9}   IC 95%")
    print("-" * 66)
    for mes in sorted(por_mes):
        n, w = por_mes[mes]
        lo, hi = wilson(w, n)
        print(f"{mes:<10}{n:>7}{w / n * 100:>8.1f}%   [{lo * 100:.1f}%, {hi * 100:.1f}%]")

    print("\n" + barra)
    print("POR PAR NA JANELA DO LAB (01-10/09)")
    print(barra)
    print(f"{'par':<9}{'backtest':>26}{'lab ao vivo':>20}")
    print("-" * 66)
    for par in sorted(janela_par):
        n, w = janela_par[par]
        lo, hi = wilson(w, n)
        bt = f"n={n:<5} {w / n * 100:>5.1f}% [{lo * 100:.0f},{hi * 100:.0f}]"
        v = VIVO.get(par)
        lv = f"n={v[0]:<4} {v[1] / v[0] * 100:>5.1f}%" if v and v[0] else "—"
        print(f"{par:<9}{bt:>26}{lv:>20}")

    print("\n" + barra)
    n, w = janela
    if n:
        lo, hi = wilson(w, n)
        print(f"  backtest na janela: {w}/{n} = {w / n * 100:.1f}%   IC [{lo * 100:.1f}%, {hi * 100:.1f}%]")
    print(f"  lab ao vivo:        127/207 = 61.4%   IC [54.6%, 67.7%]")
    print(f"  breakeven:          53.8%")


if __name__ == "__main__":
    main()
