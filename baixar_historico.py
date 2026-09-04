"""Baixa histórico de candles da IQ Option e salva em cache local.

Uso:
    python baixar_historico.py --candles 26000 --tf 900 --ativos EURUSD GBPUSD
    python baixar_historico.py --candles 26000 --tf 900   # todos os 7 pares
    python baixar_historico.py --candles 6000  --tf 3600  # H1

O cache é mesclado com o existente — rodar de novo só baixa o que falta.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.mercado_iq import MercadoIQ

ATIVOS_PADRAO = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", type=int, default=26000,
                    help="candles por ativo (26000 ≈ 12 meses de M15)")
    ap.add_argument("--tf", type=int, default=900,
                    choices=[60, 300, 900, 3600],
                    help="timeframe em segundos: 300=M5, 900=M15, 3600=H1")
    ap.add_argument("--ativos", nargs="*", default=ATIVOS_PADRAO)
    a = ap.parse_args()

    tf_nome = {60: "M1", 300: "M5", 900: "M15", 3600: "H1"}.get(a.tf, f"{a.tf}s")
    print(f"Timeframe: {tf_nome} | Meta: {a.candles} candles/par")
    print(f"Pares: {' '.join(a.ativos)}\n")

    config = configuracao_scalping_m15()
    cfg = dataclasses.replace(config, timeframe_segundos=a.tf)

    api = MercadoIQ(config).conectar_somente_leitura()

    erros = 0
    for ativo in a.ativos:
        try:
            cache = backtest.carregar_cache(cfg, ativo)
            ja_tem = len(cache) if cache is not None else 0
            if ja_tem >= a.candles:
                print(f"  {ativo}: já tem {ja_tem} candles — pulando.")
                continue
            print(f"  {ativo}: tem {ja_tem}, baixando ate {a.candles}...", end=" ", flush=True)
            df = backtest.baixar_historico(api, cfg, ativo, a.candles)
            print(f"OK ({len(df)} candles, {df.index[0]:%d/%m/%Y} a {df.index[-1]:%d/%m/%Y})")
        except Exception as e:
            print(f"ERRO: {e}")
            erros += 1

    print(f"\nConcluído. {erros} erros.")
    return 0 if erros == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
