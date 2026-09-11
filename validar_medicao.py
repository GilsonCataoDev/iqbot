"""Confere a apuracao de contrato.py contra ordens que a IQ ja liquidou.

O metodo antigo — entrada na abertura da vela M5, saida no fechamento da
terceira seguinte — concordou com a corretora em 13 de 19 ordens (68%). Esse
erro de um terco empurra qualquer taxa verdadeira para perto de 50%, e foi o
que fez sete meses de backtest darem todos entre 46% e 51%.

Aqui a pergunta e direta: usando strike no instante da compra e saida no
marco de vencimento, quantas das mesmas ordens a apuracao acerta? Se nao
subir bem acima de 68%, a hipotese de vencimento em contrato.py esta errada
e nao adianta reprocessar nada em cima dela.

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

    `hora_sinal` vem do indice do candle, que e UTC; `enviada_em` vem de
    datetime.now(), que e local. Em vez de fixar o fuso da maquina, deduz o
    deslocamento: a ordem sai logo depois de o candle de sinal fechar, entao
    a diferenca arredondada para horas cheias e o offset.
    """
    esperado = hora_sinal + pd.Timedelta(seconds=timeframe_s)
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
    ok = dif = sem = 0
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
        ok += bate
        dif += (not bate)
        print(f"{str(compra)[5:19]:<18}{ativo:<8}{direcao:<6}{real:<6}"
              f"{d['resultado']:<9}{d['duracao_s']/60:>5.0f}m  {'sim' if bate else 'NAO'}")

    total = ok + dif
    print("-" * 70)
    if total:
        print(f"  concordam: {ok}/{total} = {ok/total*100:.0f}%")
        print(f"  divergem:  {dif}/{total} = {dif/total*100:.0f}%")
        print(f"\n  metodo antigo (velas M5 fixas): 68%")
        veredicto = ("hipotese de vencimento CONFIRMADA" if ok / total >= 0.9
                     else "melhorou, mas ainda ha erro sistematico" if ok / total > 0.68
                     else "hipotese de vencimento NAO se sustenta")
        print(f"  -> {veredicto}")
    if sem:
        print(f"  sem dado suficiente: {sem} ordem(ns) — baixe {a.tf}s cobrindo o periodo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
