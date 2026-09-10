"""Compara expirações binárias do ema920_pullback no mesmo histórico M5.

O sinal é idêntico em todas as colunas; só muda quantos candles à frente o
resultado é medido. Assim a diferença observada é do horizonte, não do setup.

Uso:
    python analisar_expiracao_ema920.py
    python analisar_expiracao_ema920.py --pares EURUSD AUDCAD
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict
from dataclasses import replace

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_ema_laboratorio_practice
from iqoption_m5.estrategia import EstrategiaReversaoM5
from iqoption_m5.laboratorio_ema import _config_rastro

PARES_PADRAO = [
    "EURUSD", "AUDCAD", "NZDUSD", "GBPUSD",
    "USDJPY", "AUDUSD", "USDCAD", "EURJPY",
]
# minutos -> candles M5 à frente
EXPIRACOES = {5: 1, 15: 3, 30: 6, 60: 12}


def wilson(acertos: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = acertos / n
    den = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / den
    margem = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centro - margem), min(1.0, centro + margem)


def _config_ema920():
    """A config do rastro que realmente roda, nao uma aproximacao.

    Partir de configuracao_scalping_m15 trazia a parametrizacao inteira do
    scalping junto — inclusive ema_micro_periodo=21, quando o setup e
    EMA 9/20. O backtest media outra estrategia e devolvia 47,7% para algo
    que ao vivo da 61%.
    """
    return _config_rastro(configuracao_ema_laboratorio_practice(), 300, "ema920_pullback")


def simular(config, ativo: str, candles: pd.DataFrame) -> dict[int, list[str]]:
    """Um passe de sinais, todos os horizontes medidos sobre os mesmos sinais."""
    estrategia = EstrategiaReversaoM5(config)
    pos_de = {hora: pos for pos, hora in enumerate(candles.index)}
    saida: dict[int, list[str]] = {m: [] for m in EXPIRACOES}
    maior = max(EXPIRACOES.values())
    for decisao in estrategia.sinais_historicos(ativo, candles):
        pos = pos_de.get(decisao.candle_hora)
        # Exige espaço para o horizonte mais longo, senão as colunas comparam
        # amostras diferentes e a tabela deixa de ser comparável.
        if pos is None or pos + maior >= len(candles):
            continue
        abertura = float(candles.iloc[pos + 1]["Open"])
        for minutos, passos in EXPIRACOES.items():
            fechamento = float(candles.iloc[pos + passos]["Close"])
            if fechamento == abertura:
                saida[minutos].append("empate")
            elif decisao.direcao == "call":
                saida[minutos].append("ganho" if fechamento > abertura else "perda")
            else:
                saida[minutos].append("ganho" if fechamento < abertura else "perda")
    return saida


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pares", nargs="*", default=PARES_PADRAO)
    args = ap.parse_args()
    config = _config_ema920()

    por_exp: dict[int, dict] = defaultdict(lambda: {"ganhos": 0, "n": 0})
    por_par: dict[str, dict[int, dict]] = {}

    for ativo in args.pares:
        candles = backtest.carregar_cache(config, ativo)
        if candles is None or len(candles) < 100:
            print(f"  {ativo}: sem dados M5 — pulando")
            continue
        res = simular(config, ativo, candles)
        n = len(res[15])
        if not n:
            print(f"  {ativo}: 0 sinais — pulando")
            continue
        print(f"  {ativo}: {n} sinais, {len(candles)} candles M5")
        por_par[ativo] = {}
        for minutos, lista in res.items():
            g = sum(1 for r in lista if r == "ganho")
            v = sum(1 for r in lista if r != "empate")
            por_exp[minutos]["ganhos"] += g
            por_exp[minutos]["n"] += v
            por_par[ativo][minutos] = {"ganhos": g, "n": v}

    if not por_exp:
        print("Nenhum sinal gerado.")
        return

    print(f"\n{'='*72}\nMESMOS SINAIS, HORIZONTES DIFERENTES\n{'='*72}")
    print(f"{'exp':>6}  {'n':>6}  {'acerto':>8}  {'IC 95%':>18}   breakeven payout")
    print("-" * 72)
    for minutos in sorted(EXPIRACOES):
        d = por_exp[minutos]
        if not d["n"]:
            continue
        p = d["ganhos"] / d["n"]
        lo, hi = wilson(d["ganhos"], d["n"])
        # Payout cai com o horizonte na maioria das corretoras; mostramos o
        # payout que essa taxa precisaria para empatar, em vez de assumir um.
        preciso = (1 - p) / p if p else float("inf")
        print(f"{minutos:>4}min  {d['n']:>6}  {p*100:>7.1f}%  "
              f"[{lo*100:>5.1f}%,{hi*100:>5.1f}%]   precisa payout >= {preciso:.2f}")

    print(f"\n{'='*72}\nPOR PAR\n{'='*72}")
    cab = "  ".join(f"{m}min" for m in sorted(EXPIRACOES))
    print(f"{'par':<9}{'n':>6}   {cab}")
    print("-" * 72)
    for ativo, d in por_par.items():
        celulas = []
        for m in sorted(EXPIRACOES):
            x = d[m]
            celulas.append(f"{x['ganhos']/x['n']*100:>5.1f}%" if x["n"] else "    —")
        print(f"{ativo:<9}{d[15]['n']:>6}   " + "  ".join(celulas))


if __name__ == "__main__":
    main()
