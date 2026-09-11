"""Confere a apuracao de contrato.py contra ordens que a IQ ja liquidou.

O metodo antigo — entrada na abertura da vela M5, saida no fechamento da
terceira seguinte — concordou com a corretora em 13 de 19 ordens (68%). Esse
erro de um terco empurra qualquer taxa verdadeira para perto de 50%, e foi o
que fez sete meses de backtest darem todos entre 46% e 51%.

Aqui a pergunta e direta: usando strike no instante da compra e saida no
vencimento, quantas das mesmas ordens a apuracao acerta? Se nao subir bem
acima de 68%, contrato.py esta errado e nao adianta reprocessar nada em
cima dele. Resultado atual, com candles de 1s: 16/18 = 89%.

Uso:
    python validar_medicao.py
    python validar_medicao.py --tf 60 --banco <caminho.sqlite3>
"""
from __future__ import annotations

import argparse
import dataclasses
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_ema_laboratorio_practice
from iqoption_m5.contrato import desfecho_binario

BANCO_PADRAO = "iqoption_m5/dados/iqoption_m5_real_ema_m5_real.sqlite3"


def _sem_fuso(t: pd.Timestamp) -> pd.Timestamp:
    """O indice dos candles e naive; comparar com aware levanta TypeError."""
    return t.tz_localize(None) if t.tzinfo is not None else t


def _offset_utc(hora_sinal: pd.Timestamp, enviada_em: pd.Timestamp, timeframe_s: int) -> pd.Timedelta:
    """Quanto somar a `enviada_em` para chegar em UTC.

    `hora_sinal` vem do stream ao vivo, que carimba o FECHAMENTO da vela de
    confirmacao, e esta em UTC; `enviada_em` vem de datetime.now(), que e
    local. Em vez de fixar o fuso da maquina, deduz o deslocamento: a ordem
    sai ~10s depois desse carimbo, entao a diferenca arredondada para horas
    cheias e o offset.

    O arredondamento para hora cheia e o que torna isto robusto: mesmo que a
    referencia erre por minutos, o offset de fuso continua correto.
    """
    esperado = hora_sinal
    horas = round((esperado - enviada_em).total_seconds() / 3600)
    return pd.Timedelta(hours=horas)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", type=int, default=60, help="timeframe do cache usado na apuracao")
    ap.add_argument("--banco", default=BANCO_PADRAO)
    ap.add_argument("--timeframe-sinal", type=int, default=300)
    a = ap.parse_args()

    if not Path(a.banco).exists():
        print(f"Banco nao encontrado: {a.banco}")
        return 1

    cfg = dataclasses.replace(configuracao_ema_laboratorio_practice(), timeframe_segundos=a.tf)
    con = sqlite3.connect(a.banco)
    ordens = con.execute(
        "SELECT ativo, direcao, hora_sinal, enviada_em, expiracao_minutos, lucro, id_ordem "
        "FROM operacoes WHERE status='finalizada' AND lucro IS NOT NULL "
        "ORDER BY enviada_em"
    ).fetchall()
    con.close()
    if not ordens:
        print("Nenhuma ordem finalizada para conferir.")
        return 1

    caches: dict[str, pd.DataFrame | None] = {}
    print(f"{'compra (UTC)':<18}{'par':<8}{'dir':<6}{'IQ':<6}{'apurado':<9}{'dur':>6}  bate?")
    print("-" * 70)
    ok = dif = sem = empates = 0
    for ativo, direcao, hora_sinal, enviada_em, exp_min, lucro, _id in ordens:
        if ativo not in caches:
            caches[ativo] = backtest.carregar_cache(cfg, ativo)
        candles = caches[ativo]
        if candles is None or candles.empty:
            sem += 1
            continue
        sinal = _sem_fuso(pd.Timestamp(hora_sinal))
        enviada = _sem_fuso(pd.Timestamp(enviada_em))
        compra = enviada + _offset_utc(sinal, enviada, a.timeframe_sinal)
        d = desfecho_binario(candles, direcao, compra, int(exp_min or 15))
        if d is None:
            sem += 1
            continue
        real = "win" if lucro > 0 else "loss"
        bate = d["resultado"] == real
        # Empate medido nao e erro de direcao: com 5 casas decimais a
        # igualdade exata quase sempre e arredondamento, nao preco parado.
        # Conta-lo junto com as divergencias reais inflaria o erro e
        # esconderia quantas vezes a apuracao aponta para o lado errado.
        if d["resultado"] == "empate":
            empates += 1
        elif bate:
            ok += 1
        else:
            dif += 1
        print(f"{str(compra)[5:19]:<18}{ativo:<8}{direcao:<6}{real:<6}"
              f"{d['resultado']:<9}{d['duracao_s']/60:>5.0f}m  {'sim' if bate else 'NAO'}")

    total = ok + dif
    print("-" * 70)
    if total:
        print(f"  direcao correta: {ok}/{total} = {ok/total*100:.0f}%")
        print(f"  direcao errada:  {dif}/{total} = {dif/total*100:.0f}%")
        if empates:
            print(f"  empates medidos: {empates} (fora do denominador — delta zero "
                  f"em 5 casas e arredondamento, nao preco parado)")
        print(f"\n  metodo antigo (velas M5 fixas): 68%")
        veredicto = ("apuracao CONFIRMADA" if ok / total >= 0.9
                     else "melhor que o antigo, com residual nao explicado"
                     if ok / total > 0.68 else "a apuracao NAO se sustenta")
        print(f"  -> {veredicto}")
    if sem:
        print(f"  sem dado suficiente: {sem} ordem(ns) — baixe {a.tf}s cobrindo o periodo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
