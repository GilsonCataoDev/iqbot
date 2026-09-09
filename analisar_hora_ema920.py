"""Mede taxa de acerto do ema920_pullback por hora UTC no histórico M5.

Uso:
    python analisar_hora_ema920.py
    python analisar_hora_ema920.py --pares EURUSD GBPUSD AUDCAD
    python analisar_hora_ema920.py --filtro-hora 6 18   # só opera 06-18h UTC
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from dataclasses import replace

PARES_PADRAO = [
    "EURUSD", "AUDCAD", "NZDUSD", "GBPUSD",
    "USDJPY", "AUDUSD", "USDCAD", "EURJPY",
]
PAYOUT = 0.86
BREAKEVEN = 1 / (1 + PAYOUT)  # ~53.8%


def wilson(acertos: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = acertos / n
    den = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / den
    margem = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centro - margem), min(1.0, centro + margem)


def _config_ema920():
    base = configuracao_scalping_m15()
    return replace(
        base,
        timeframe_segundos=300,
        expiracao_minutos=15,
        ema920_pullback_ativo=True,
        ema920_prime_ativo=False,
        ema921_rsi_pullback_ativo=False,
        ema921_rsi_intravela_ativo=False,
        fibo_sr_retracao_ativo=False,
        nzd_trend_pullback_ativo=False,
        filtro_h1_ativo=False,
    )


EXPIRACAO_CANDLES = 3  # 15min / 5min por candle


def _simular_15min(config, ativo: str, candles: pd.DataFrame) -> list[dict]:
    """Replica backtest.simular mas mede fechamento 15min (3 candles) à frente.

    backtest.simular usa posicao+1 (5min), que é o horizonte errado para
    binárias de 15min. Aqui: entrada=abertura[+1], saída=fechamento[+3].
    """
    from iqoption_m5.estrategia import EstrategiaReversaoM5
    estrategia = EstrategiaReversaoM5(config)
    posicao_por_hora = {hora: pos for pos, hora in enumerate(candles.index)}
    ops = []
    for decisao in estrategia.sinais_historicos(ativo, candles):
        pos = posicao_por_hora.get(decisao.candle_hora)
        if pos is None or pos + EXPIRACAO_CANDLES >= len(candles):
            continue
        abertura = float(candles.iloc[pos + 1]["Open"])
        fechamento = float(candles.iloc[pos + EXPIRACAO_CANDLES]["Close"])
        if fechamento == abertura:
            resultado = "empate"
        elif decisao.direcao == "call":
            resultado = "ganho" if fechamento > abertura else "perda"
        else:
            resultado = "ganho" if fechamento < abertura else "perda"
        ops.append({
            "hora_sinal": decisao.candle_hora,
            "resultado": resultado,
            "direcao": decisao.direcao,
        })
    return ops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pares", nargs="*", default=PARES_PADRAO)
    ap.add_argument("--filtro-hora", nargs=2, type=int, metavar=("INICIO", "FIM"),
                    help="Só contar sinais dentro de [INICIO, FIM) UTC")
    args = ap.parse_args()

    config = _config_ema920()

    # hora_utc -> {ganhos, total}
    por_hora: dict[int, dict] = defaultdict(lambda: {"ganhos": 0, "total": 0})
    total_ops = 0
    pares_ok = []

    for ativo in args.pares:
        candles = backtest.carregar_cache(config, ativo)
        if candles is None or len(candles) < 100:
            print(f"  {ativo}: sem dados M5 — pulando")
            continue
        ops = _simular_15min(config, ativo, candles)
        if not ops:
            print(f"  {ativo}: 0 operações geradas — pulando")
            continue
        pares_ok.append(ativo)
        for op in ops:
            h = pd.Timestamp(op["hora_sinal"]).hour
            por_hora[h]["total"] += 1
            if op["resultado"] == "ganho":
                por_hora[h]["ganhos"] += 1
        total_ops += len(ops)
        print(f"  {ativo}: {len(ops)} operações, {len(candles)} candles M5")

    if not por_hora:
        print("Nenhuma operação gerada.")
        return

    print(f"\nPares usados: {' '.join(pares_ok)}")
    print(f"Total operações: {total_ops}\n")

    # — tabela por hora —
    print(f"{'Hora':>5}  {'n':>5}  {'acerto%':>8}  {'IC 95%':>20}  {'status'}")
    print("-" * 65)

    horas_ruins = []
    horas_boas = []
    for h in range(24):
        d = por_hora[h]
        n = d["total"]
        g = d["ganhos"]
        if n == 0:
            print(f"  {h:02d}h  {'—':>5}")
            continue
        lo, hi = wilson(g, n)
        pct = g / n * 100
        ic_str = f"[{lo*100:.1f}%, {hi*100:.1f}%]"
        if hi < BREAKEVEN:
            status = "ABAIXO breakeven"
            horas_ruins.append(h)
        elif lo > BREAKEVEN:
            status = "ACIMA breakeven"
            horas_boas.append(h)
        else:
            status = "inconclusivo"
        print(f"  {h:02d}h  {n:>5}  {pct:>7.1f}%  {ic_str:>20}  {status}")

    # — resumo geral —
    g_total = sum(d["ganhos"] for d in por_hora.values())
    n_total = sum(d["total"] for d in por_hora.values())
    lo_g, hi_g = wilson(g_total, n_total)
    print(f"\nGlobal: {g_total}/{n_total} = {g_total/n_total*100:.1f}%  IC [{lo_g*100:.1f}%, {hi_g*100:.1f}%]")
    print(f"Breakeven (payout {PAYOUT}): {BREAKEVEN*100:.1f}%")

    # — impacto do filtro de horário —
    if args.filtro_hora:
        inicio_f, fim_f = args.filtro_hora
        horas_filtradas = [h for h in range(24) if not (inicio_f <= h < fim_f)]
    else:
        # Proposta: excluir 00-06h UTC
        horas_filtradas = list(range(0, 6))

    g_fil = sum(por_hora[h]["ganhos"] for h in horas_filtradas if h in por_hora)
    n_fil = sum(por_hora[h]["total"] for h in horas_filtradas if h in por_hora)
    g_sem = g_total - g_fil
    n_sem = n_total - n_fil

    print(f"\n— Simulação do filtro 00-06h UTC (excluir Ásia) —")
    if n_fil > 0:
        lo_f, hi_f = wilson(g_fil, n_fil)
        print(f"  Horas excluídas {horas_filtradas}: {g_fil}/{n_fil} = {g_fil/n_fil*100:.1f}%  IC [{lo_f*100:.1f}%, {hi_f*100:.1f}%]")
    if n_sem > 0:
        lo_s, hi_s = wilson(g_sem, n_sem)
        print(f"  Horas mantidas  06-23h: {g_sem}/{n_sem} = {g_sem/n_sem*100:.1f}%  IC [{lo_s*100:.1f}%, {hi_s*100:.1f}%]")
        print(f"  Operações cortadas: {n_fil} ({n_fil/n_total*100:.1f}% do total)")

    if horas_ruins:
        print(f"\nHoras com IC inteiro abaixo do breakeven: {horas_ruins}")
    if horas_boas:
        print(f"Horas com IC inteiro acima do breakeven:  {horas_boas}")


if __name__ == "__main__":
    main()
