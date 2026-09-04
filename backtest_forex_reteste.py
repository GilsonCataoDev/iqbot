"""Valida `forex_reteste_m15` como BINARIA, que e o que o bot executa.

Nao confundir com `iqoption_m5/forex_backtest.py`: aquele simula a operacao de
FOREX com SL/TP e dimensionamento. Aqui o SL/TP sao so o plano visual — a
execucao real e uma binaria CALL/PUT.

Modelo, fiel ao que o app faz (`_plano_forex_m15`):
  - o plano sai de `candles.iloc[:-1]`, ou seja, so de candles FECHADOS;
  - a entrada acontece nos primeiros `entrada_max_segundos_no_candle` (60s) da
    vela seguinte, entao o preenchimento e o Open dessa vela;
  - `forex_reteste_m15` nao esta em `expiracao_por_setup`, entao cai no calculo
    dinamico limitado por `expiracao_minutos` — na pratica, o fechamento da
    propria vela de entrada.

Por isso aqui NAO ha candle parcial nem escolha de preco de entrada: o sinal
nasce de vela fechada e o Open seguinte e um preenchimento honesto. Isso e o
oposto dos setups de reversao, onde o sinal nasce no meio da vela (ver
`backtest_m15.py`).

Uso:
    python backtest_forex_reteste.py --verificar-lookahead
    python backtest_forex_reteste.py
    python backtest_forex_reteste.py --ativos EURUSD GBPUSD
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_estrategia import (
    plano_rompimento_reteste,
    planos_rompimento_reteste,
)

# Payout medido na IQ em 27/08/2026 (ver memoria iqoption-payout-por-par).
PAYOUT = {
    "EURUSD": 0.85, "GBPUSD": 0.85, "USDJPY": 0.85,
    "AUDUSD": 0.85, "EURJPY": 0.85,
    "USDCAD": 0.87, "NZDUSD": 0.87,
}
PAYOUT_PADRAO = 0.85


# Reaproveita o Wilson da sonda em vez de manter uma segunda copia — duas
# implementacoes do mesmo IC divergem com o tempo.
from sonda_setup import wilson  # noqa: E402


def verificar_lookahead(ativo: str, candles: pd.DataFrame, amostras: int = 400) -> bool:
    """A versao vetorizada tem de bater com a chamada candle a candle.

    Este teste existe porque em 27/08/2026 descobrimos que o metodo M1-parcial do
    backtest de reversao era anulado por um cache: a versao "sem lookahead" e a
    circular eram a mesma conta. Uma funcao vetorizada que promete "so dados ate
    t" precisa ser checada, nao acreditada.
    """
    serie = planos_rompimento_reteste(ativo, candles)
    inicio = max(80, len(candles) - amostras)
    divergencias = 0
    comparados = 0
    for i in range(inicio, len(candles)):
        # A versao pontual so enxerga ate i (inclusive) — mesmo corte do app.
        pontual = plano_rompimento_reteste(ativo, candles.iloc[: i + 1])
        vetorial = serie.iloc[i]
        vet_ok = isinstance(vetorial, object) and vetorial is not None and not (
            isinstance(vetorial, float) and pd.isna(vetorial)
        )
        comparados += 1
        if (pontual is None) != (not vet_ok):
            divergencias += 1
            continue
        if pontual is not None and vet_ok:
            if (round(pontual.nivel, 8) != round(vetorial.nivel, 8)
                    or pontual.lado != vetorial.lado):
                divergencias += 1
    print(f"  {ativo}: {comparados} candles comparados, {divergencias} divergencias")
    return divergencias == 0


def rodar(ativo: str, candles: pd.DataFrame) -> list[dict]:
    """Um trade por plano: entra no Open do candle seguinte, sai no Close dele."""
    serie = planos_rompimento_reteste(ativo, candles)
    ops = []
    # i = candle do sinal (fechado). A entrada e o candle i+1.
    for i in range(80, len(candles) - 1):
        plano = serie.iloc[i]
        if plano is None or (isinstance(plano, float) and pd.isna(plano)):
            continue
        entrada_candle = candles.iloc[i + 1]
        abertura = float(entrada_candle["Open"])
        fechamento = float(entrada_candle["Close"])
        if fechamento == abertura:
            res = "empate"
        elif plano.lado == "buy":
            res = "ganho" if fechamento > abertura else "perda"
        else:
            res = "ganho" if fechamento < abertura else "perda"
        ops.append({
            "ativo": ativo,
            "quando": candles.index[i + 1],
            "lado": plano.lado,
            "entrada": abertura,
            "saida": fechamento,
            "res": res,
        })
    return ops


def relatorio(df: pd.DataFrame) -> None:
    if df.empty:
        print("\nNenhum sinal gerado.")
        return

    def linha(rotulo: str, sub: pd.DataFrame, payout: float) -> None:
        decididos = sub[sub["res"].isin(["ganho", "perda"])]
        n = len(decididos)
        if n == 0:
            print(f"  {rotulo:22} sem trades decididos")
            return
        w = int((decididos["res"] == "ganho").sum())
        wr = w / n
        lo, hi = wilson(w, n)
        be = 1 / (1 + payout)
        lucro = w * payout - (n - w)
        marca = "POSITIVO" if lo > be else ("NEGATIVO" if hi < be else "inconclusivo")
        print(f"  {rotulo:22} n={n:<5} WR={wr:>5.1%}  IC=[{lo:.0%},{hi:.0%}]  "
              f"breakeven={be:.1%}  lucro={lucro:+7.1f}u  -> {marca}")

    print("\n" + "=" * 78)
    print("forex_reteste_m15 como BINARIA (entrada no Open seguinte, saida no Close)")
    print("=" * 78)

    for ativo in sorted(df["ativo"].unique()):
        linha(ativo, df[df["ativo"] == ativo], PAYOUT.get(ativo, PAYOUT_PADRAO))

    # Agregado: usa o payout medio ponderado pelos trades de cada par.
    decididos = df[df["res"].isin(["ganho", "perda"])]
    if not decididos.empty:
        pay = decididos["ativo"].map(lambda a: PAYOUT.get(a, PAYOUT_PADRAO))
        print()
        linha("TOTAL", decididos, float(pay.mean()))

    metades = len(decididos) // 2
    if metades > 20:
        print()
        pay_medio = float(decididos["ativo"].map(
            lambda a: PAYOUT.get(a, PAYOUT_PADRAO)).mean())
        ordenado = decididos.sort_values("quando")
        linha("1a metade", ordenado.iloc[:metades], pay_medio)
        linha("2a metade", ordenado.iloc[metades:], pay_medio)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", type=int, default=5000)
    ap.add_argument("--ativos", nargs="*", default=None)
    ap.add_argument("--verificar-lookahead", action="store_true",
                    help="compara a versao vetorizada com a chamada candle a candle")
    a = ap.parse_args()

    config = configuracao_scalping_m15()
    ativos = a.ativos or list(config.ativos)

    dados: dict[str, pd.DataFrame] = {}
    for ativo in ativos:
        c = backtest.carregar_cache(config, ativo)
        if c is None:
            print(f"  {ativo}: sem cache — rode backtest_m15.py uma vez sem --offline")
            continue
        c = c.tail(a.candles)
        dados[ativo] = c
        print(f"  {ativo}: {len(c)} candles "
              f"({c.index[0]:%d/%m/%Y} a {c.index[-1]:%d/%m/%Y})")
    if not dados:
        return 1

    if a.verificar_lookahead:
        print("\n=== vetorizado x candle a candle ===")
        todos_ok = True
        for ativo, c in dados.items():
            if not verificar_lookahead(ativo, c):
                todos_ok = False
        print("\nRESULTADO:", "sem divergencia" if todos_ok
              else "DIVERGIU — a versao vetorizada NAO equivale a pontual")
        return 0 if todos_ok else 2

    ops = []
    for ativo, c in dados.items():
        ops.extend(rodar(ativo, c))
    relatorio(pd.DataFrame(ops))
    return 0


if __name__ == "__main__":
    sys.exit(main())
