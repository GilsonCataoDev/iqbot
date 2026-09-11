"""Backtest do ema920_pullback apurado pelo contrato real.

Substitui as analises anteriores, que mediam entrada na abertura da vela M5
e saida no fechamento da terceira seguinte — 32% de erro contra ordens que a
IQ liquidou, o que achatava sete meses inteiros na faixa de 46% a 51%.

Aqui a apuracao usa contrato.desfecho_binario: strike no instante da compra,
saida no vencimento. A compra e simulada no mesmo ponto em que o bot compra:
poucos segundos depois de fechar a vela de sinal.

CORRECAO DE VIES: a apuracao acerta 83% em M1 e 89% em 1s, nao 100%. Uma
medicao que erra a taxa `e` observa

    medido = verdadeiro*(1-e) + (1-verdadeiro)*e

e puxa qualquer taxa para perto de 50%. O inverso recupera a estimativa:

    verdadeiro = (medido - e) / (1 - 2e)

Sem isso o backtest subestima a vantagem de forma sistematica — o mesmo
erro de ontem, so que menor.

Uso:
    python analisar_ema920_valido.py
    python analisar_ema920_valido.py --tf 1 --pares EURUSD AUDCAD
"""
from __future__ import annotations

import argparse
import dataclasses
import math
from collections import defaultdict

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_ema_laboratorio_practice
from iqoption_m5.contrato import desfecho_binario
from iqoption_m5.estrategia import EstrategiaReversaoM5
from iqoption_m5.laboratorio_ema import _config_rastro

PARES_PADRAO = ["EURUSD", "AUDCAD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY"]
# Medido em validar_medicao.py contra 18 ordens liquidadas pela IQ.
ERRO_APURACAO = {1: 0.11, 60: 0.17}
# A compra sai ~10s depois de a vela de confirmacao FECHAR.
#
# Cuidado com o carimbo: as duas series estao deslocadas em uma vela. No cache
# baixado o indice e a ABERTURA (baixar_historico usa candle["from"]); no
# stream ao vivo o carimbo gravado em hora_sinal e o FECHAMENTO. Provado por
# casamento exato de float: a decisao com candle_hora=15:35 registrou
# ema20=1.1623687627060648, identico ao EMA_Micro do cache na vela 15:30.
#
# Por isso soma-se o timeframe aqui. Comprar em candle_hora+10s usando o
# indice do cache seria comprar na abertura da vela cujo fechamento gerou o
# sinal — lookahead puro, que sozinho levou EURUSD de 47,6% para 69,5%.
SEGUNDOS_APOS_COMPRA = 10
PAYOUT = 0.86
BREAKEVEN = 1 / (1 + PAYOUT)


def wilson(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 1.0
    p = w / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - m), min(1.0, c + m)


def momento_compra(candle_hora, timeframe_s: int):
    """Instante da compra a partir do carimbo do cache (rotulado na abertura).

    Precisa cair DEPOIS de a vela de confirmacao fechar. Comprar antes disso
    e usar o fechamento que gerou o sinal para entrar a um preco anterior a
    ele — lookahead que sozinho levou EURUSD de 47,6% para 69,5%.
    """
    return (pd.Timestamp(candle_hora)
            + pd.Timedelta(seconds=timeframe_s)
            + pd.Timedelta(seconds=SEGUNDOS_APOS_COMPRA))


def corrigir_vies(medido: float, erro: float) -> float:
    """Desfaz o achatamento que a taxa de erro da apuracao provoca."""
    if erro >= 0.5:
        return medido
    return (medido - erro) / (1 - 2 * erro)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", type=int, default=60, choices=[1, 60],
                    help="granularidade da apuracao (1s=89% de acerto, M1=83%)")
    ap.add_argument("--pares", nargs="*", default=PARES_PADRAO)
    ap.add_argument("--expiracao", type=int, default=15)
    a = ap.parse_args()

    cfg_sinal = _config_rastro(configuracao_ema_laboratorio_practice(), 300, "ema920_pullback")
    cfg_fino = dataclasses.replace(cfg_sinal, timeframe_segundos=a.tf)
    erro = ERRO_APURACAO[a.tf]

    por_par: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    por_mes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    empates = sem_dado = 0

    for ativo in a.pares:
        m5 = backtest.carregar_cache(cfg_sinal, ativo)
        fino = backtest.carregar_cache(cfg_fino, ativo)
        if m5 is None or len(m5) < 100:
            print(f"  {ativo}: sem M5 — pulando")
            continue
        if fino is None or fino.empty:
            print(f"  {ativo}: sem {a.tf}s — pulando (baixe primeiro)")
            continue
        n = 0
        for d in EstrategiaReversaoM5(cfg_sinal).sinais_historicos(ativo, m5):
            compra = momento_compra(d.candle_hora, cfg_sinal.timeframe_segundos)
            r = desfecho_binario(fino, d.direcao, compra, a.expiracao)
            if r is None:
                sem_dado += 1
                continue
            if r["resultado"] == "empate":
                empates += 1
                continue
            ganhou = int(r["resultado"] == "win")
            por_par[ativo][0] += 1
            por_par[ativo][1] += ganhou
            mes = compra.strftime("%Y-%m")
            por_mes[mes][0] += 1
            por_mes[mes][1] += ganhou
            n += 1
        print(f"  {ativo}: {n} operacoes apuradas")

    total_n = sum(v[0] for v in por_par.values())
    if not total_n:
        print("\nNenhuma operacao apurada — falta cache fino cobrindo o periodo.")
        return
    total_w = sum(v[1] for v in por_par.values())

    barra = "=" * 70
    print(f"\n{barra}\nAPURADO PELO CONTRATO REAL ({a.tf}s, erro medido {erro:.0%})\n{barra}")
    print(f"{'par':<10}{'n':>8}{'medido':>9}{'corrigido':>11}   IC 95% (medido)")
    print("-" * 70)
    for par in sorted(por_par):
        n, w = por_par[par]
        lo, hi = wilson(w, n)
        print(f"{par:<10}{n:>8}{w/n*100:>8.1f}%{corrigir_vies(w/n, erro)*100:>10.1f}%"
              f"   [{lo*100:.1f}%, {hi*100:.1f}%]")
    lo, hi = wilson(total_w, total_n)
    print("-" * 70)
    print(f"{'TOTAL':<10}{total_n:>8}{total_w/total_n*100:>8.1f}%"
          f"{corrigir_vies(total_w/total_n, erro)*100:>10.1f}%   [{lo*100:.1f}%, {hi*100:.1f}%]")

    if por_mes:
        print(f"\n{barra}\nPOR MES (corrigido)\n{barra}")
        for mes in sorted(por_mes):
            n, w = por_mes[mes]
            print(f"  {mes}  n={n:<7} medido {w/n*100:>5.1f}%   "
                  f"corrigido {corrigir_vies(w/n, erro)*100:>5.1f}%")

    print(f"\n  breakeven (payout {PAYOUT}): {BREAKEVEN*100:.1f}%")
    print(f"  lab ao vivo, mesma estrategia: 61,3% em 212 ordens")
    if empates or sem_dado:
        print(f"  empates {empates} | sem dado fino {sem_dado} (ambos fora do denominador)")
    print("\n  A correcao supoe que o erro da apuracao e simetrico entre win e loss.")
    print("  Ele foi medido em 18 ordens: trate a coluna corrigida como estimativa,")
    print("  nao como numero exato.")


if __name__ == "__main__":
    main()
