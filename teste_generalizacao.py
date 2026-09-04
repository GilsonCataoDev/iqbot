"""Teste out-of-sample ENTRE PARES do falso rompimento LONG (binaria).

O sinal foi descoberto e ajustado nos 7 pares originais. Aplicar os MESMOS
parametros (jan=10, quantile 0.25, LONG) a pares que ele nunca viu e um teste
out-of-sample genuino — e vale mais que acumular mais dados nos mesmos 7,
porque testa se o efeito e do MERCADO ou do conjunto que o produziu.

Nada e reotimizado aqui. Se precisar mexer em parametro para funcionar nos
novos pares, o teste falhou.

Uso:
    python teste_generalizacao.py
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import motor_sinais as M

ORIGINAIS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD"]
NOVOS = ["EURGBP", "EURCHF", "EURCAD", "GBPJPY", "GBPCHF", "GBPCAD", "CADCHF"]

# Referencia estabelecida nos originais
REF_WR = 0.5603
REF_IC = (0.5416, 0.5791)


def bloco(nome: str, pares: list[str]) -> pd.DataFrame | None:
    frames = M.carregar(900, pares)
    faltando = [p for p in pares if p not in frames]
    if faltando:
        print(f"  (sem cache: {', '.join(faltando)})")
    if not frames:
        print("  nenhum par disponivel.")
        return None

    d = M.avaliar_binario(frames, M.SINAIS["falso rompimento"])
    lg = d[d["direcao"] == "long"].copy()
    if lg.empty:
        print("  sem sinais.")
        return None

    print(f"\n  {nome}")
    print(f"    {'par':8} {'n':>6} {'WR':>7} {'BE':>7} {'IC95':>18}  ")
    print(f"    {'-'*8} {'-'*6} {'-'*7} {'-'*7} {'-'*18}")
    acima = 0
    for at in sorted(lg["ativo"].unique()):
        s = lg[lg["ativo"] == at]
        n = len(s); w = int(s["acerto"].sum()); wr = w / n
        lo, hi = M.wilson(w, n)
        be = M.breakeven_bin(at)
        ok = wr > be
        acima += ok
        print(f"    {at:8} {n:>6} {wr:>6.2%} {be:>6.2%}  [{lo:>5.2%},{hi:>6.2%}] {'+' if ok else '-'}")

    n = len(lg); w = int(lg["acerto"].sum()); wr = w / n
    nef = lg["quando"].nunique()
    lo, hi = M.wilson(int(round(wr * nef)), nef)
    print(f"    {'TOTAL':8} {n:>6} {wr:>6.2%} {'54.05%':>7}  [{lo:>5.2%},{hi:>6.2%}]")
    print(f"    -> {acima}/{lg['ativo'].nunique()} pares acima do proprio breakeven")
    print(f"    -> n efetivo (timestamps unicos) = {nef}")
    return lg


def main() -> int:
    print("=" * 80)
    print("GENERALIZACAO ENTRE PARES — falso rompimento LONG M15 (binaria)")
    print("=" * 80)
    print("  Mesmos parametros, zero reotimizacao.")
    print(f"  Referencia nos originais: WR {REF_WR:.2%}  IC [{REF_IC[0]:.2%}, {REF_IC[1]:.2%}]")

    a = bloco("ORIGINAIS (in-sample — onde o sinal foi encontrado)", ORIGINAIS)
    b = bloco("NOVOS (out-of-sample — nunca vistos)", NOVOS)

    print(f"\n{'='*80}")
    print("VEREDITO")
    print(f"{'='*80}")
    if b is None or len(b) < 200:
        print("  Amostra dos novos insuficiente. Rode BAIXAR_HISTORICO para eles.")
        return 1

    wr_a = a["acerto"].mean() if a is not None else np.nan
    n_b = len(b); w_b = int(b["acerto"].sum()); wr_b = w_b / n_b
    nef_b = b["quando"].nunique()
    lo_b, hi_b = M.wilson(int(round(wr_b * nef_b)), nef_b)

    print(f"  in-sample  (7 originais): WR {wr_a:.2%}")
    print(f"  out-sample (7 novos)    : WR {wr_b:.2%}  IC [{lo_b:.2%}, {hi_b:.2%}]  n_ef={nef_b}")
    queda = (wr_b - wr_a) * 100
    print(f"  variacao: {queda:+.2f}pp")

    print()
    if lo_b > 0.5405:
        print("  >> GENERALIZA. O IC dos pares novos fica acima do breakeven sem")
        print("     nenhum ajuste. E a evidencia mais forte possivel sem esperar meses.")
    elif hi_b < 0.5405:
        print("  >> NAO GENERALIZA. Nos pares novos o sinal fica abaixo do breakeven.")
        print("     O efeito era dos 7 pares originais, nao do mercado. Descartar.")
    else:
        print("  >> INCONCLUSIVO nos novos. O IC cruza o breakeven.")
        if wr_b >= 0.5405:
            print("     A estimativa pontual esta acima — consistente com edge real,")
            print("     mas sem forca estatistica. Combinar com os originais ajuda.")
        else:
            print("     A estimativa pontual esta ABAIXO do breakeven — sinal ruim.")

    # Combinado
    if a is not None:
        tudo = pd.concat([a, b], ignore_index=True)
        n = len(tudo); w = int(tudo["acerto"].sum()); wr = w / n
        nef = tudo["quando"].nunique()
        lo, hi = M.wilson(int(round(wr * nef)), nef)
        print(f"\n  COMBINADO (14 pares): n={n} n_ef={nef} WR={wr:.2%} IC=[{lo:.2%}, {hi:.2%}]")
        print(f"    {'passa o breakeven de 54.05%' if lo > 0.5405 else 'nao passa o breakeven'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
