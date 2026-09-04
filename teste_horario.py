"""Filtrar por HORARIO ajuda, ou e o mesmo data snooping que falhou nos pares?

O backtest mostrou WR 60.09% no bloco 17-24h contra 50.50% no bloco 13-17h.
Tentador. Mas selecionar pares pelo WR passado JA FALHOU out-of-sample (os
descartados renderam mais que os selecionados). Horario pode ser a mesma coisa.

Protocolo, na ordem certa:
  1. Separa o tempo em duas metades.
  2. Na 1a metade escolhe os horarios com EV positivo.
  3. Avalia essa escolha na 2a metade, que nunca foi olhada.
  4. Compara contra: horarios descartados, e todos sem filtro.
  5. TESTE DE PERMUTACAO: repete o procedimento com rotulos de hora
     EMBARALHADOS. Isso mede quanta "melhora" o proprio procedimento
     inventa a partir de ruido puro. Se a melhora real nao superar a
     melhora do embaralhado, e snooping.

Granularidades testadas: 24 horas (mais overfitavel) e 4 blocos de sessao.

Uso:
    python teste_horario.py
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import motor_sinais as M

FOREX = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD",
         "EURGBP", "EURCHF", "EURCAD", "GBPJPY", "GBPCHF", "GBPCAD", "CADCHF"]
COMM = ["XAUUSD", "XAGUSD", "UKOUSD", "USOUSD"]
PAYOUT = {"USDCAD": 0.87, "NZDUSD": 0.87, "EURCAD": 0.87, "CADCHF": 0.87,
          "EURCHF": 0.82, "XAUUSD": 0.87, "XAGUSD": 0.87,
          "UKOUSD": 0.87, "USOUSD": 0.87}

BLOCOS = {"asia 00-08h": range(0, 8), "londres 08-13h": range(8, 13),
          "ny 13-17h": range(13, 17), "noite 17-24h": range(17, 24)}


def carregar_sinais(ativos):
    fr = M.carregar(900, ativos)
    d = M.avaliar_binario(fr, M.SINAIS["falso rompimento"])
    lg = d[d["direcao"] == "long"].copy()
    lg["hora"] = pd.to_datetime(lg["quando"]).dt.hour
    lg["payout"] = lg["ativo"].map(PAYOUT).fillna(0.85)
    lg["retorno"] = np.where(lg["acerto"], lg["payout"], -1.0)
    lg["bloco"] = pd.cut(lg["hora"], [-1, 7, 12, 16, 23],
                         labels=list(BLOCOS.keys()))
    return lg.sort_values("quando")


def seleciona(p1: pd.DataFrame, col: str, n_min: int = 30) -> list:
    """Escolhe, na 1a metade, os grupos com retorno medio positivo."""
    bons = []
    for g, s in p1.groupby(col, observed=True):
        if len(s) >= n_min and s["retorno"].mean() > 0:
            bons.append(g)
    return bons


def avalia(p2: pd.DataFrame, col: str, bons: list) -> dict:
    dentro = p2[p2[col].isin(bons)]
    fora = p2[~p2[col].isin(bons)]
    return {
        "ev_dentro": dentro["retorno"].mean() if len(dentro) >= 30 else np.nan,
        "n_dentro": len(dentro),
        "ev_fora": fora["retorno"].mean() if len(fora) >= 30 else np.nan,
        "n_fora": len(fora),
        "ev_todos": p2["retorno"].mean(),
    }


def permutacao(lg: pd.DataFrame, col: str, n_perm: int = 300,
               seed: int = 3) -> np.ndarray:
    """Refaz seleciona+avalia com os rotulos EMBARALHADOS.

    Mede a melhora que o procedimento produz a partir de ruido puro. E a
    referencia honesta: a melhora real precisa superar esta distribuicao.
    """
    rng = np.random.default_rng(seed)
    corte = len(lg) // 2
    ganhos = []
    rot = lg[col].to_numpy()
    for _ in range(n_perm):
        emb = lg.copy()
        emb[col] = rng.permutation(rot)
        p1, p2 = emb.iloc[:corte], emb.iloc[corte:]
        bons = seleciona(p1, col)
        if not bons or len(bons) == emb[col].nunique():
            continue
        r = avalia(p2, col, bons)
        if np.isfinite(r["ev_dentro"]):
            ganhos.append(r["ev_dentro"] - r["ev_todos"])
    return np.array(ganhos)


def rodar(lg: pd.DataFrame, col: str, rotulo: str) -> None:
    print(f"\n{'='*88}")
    print(f"GRANULARIDADE: {rotulo}  ({lg[col].nunique()} grupos)")
    print(f"{'='*88}")

    corte = len(lg) // 2
    p1, p2 = lg.iloc[:corte], lg.iloc[corte:]
    print(f"  1a metade: ate {p1['quando'].max():%d/%m/%Y}  (n={len(p1)})")
    print(f"  2a metade: dai em diante          (n={len(p2)})")

    bons = seleciona(p1, col)
    if not bons:
        print("  nenhum grupo selecionado."); return
    print(f"\n  Selecionados na 1a metade ({len(bons)} de {lg[col].nunique()}):")
    print(f"    {', '.join(str(b) for b in bons)}")

    r = avalia(p2, col, bons)
    print(f"\n  Na 2a METADE (out-of-sample):")
    print(f"    {'grupos selecionados':28} n={r['n_dentro']:<6} EV={r['ev_dentro']:+.4f}u")
    print(f"    {'grupos descartados':28} n={r['n_fora']:<6} EV={r['ev_fora']:+.4f}u")
    print(f"    {'todos, sem filtro':28} n={len(p2):<6} EV={r['ev_todos']:+.4f}u")
    ganho = r["ev_dentro"] - r["ev_todos"]
    print(f"\n    ganho do filtro: {ganho:+.4f}u por trade")

    # teste de permutacao
    nulo = permutacao(lg, col)
    if len(nulo) < 30:
        print("    (permutacao sem amostra suficiente)"); return
    p_val = float((nulo >= ganho).mean())
    print(f"\n  TESTE DE PERMUTACAO ({len(nulo)} embaralhamentos):")
    print(f"    ganho medio so por ruido : {nulo.mean():+.4f}u")
    print(f"    percentil 95 do ruido    : {np.percentile(nulo,95):+.4f}u")
    print(f"    ganho real observado     : {ganho:+.4f}u")
    print(f"    p-valor                  : {p_val:.3f}")
    if p_val < 0.05:
        print("    >> O filtro supera o ruido. Efeito de horario REAL.")
    else:
        print("    >> O filtro NAO supera o que o embaralhamento produz.")
        print("       Selecionar horario e data snooping — nao usar.")


def main() -> int:
    print("=" * 88)
    print("FILTRO DE HORARIO — testado out-of-sample + permutacao")
    print("=" * 88)
    lg = carregar_sinais(FOREX + COMM)
    print(f"  {len(lg)} sinais LONG, {lg['ativo'].nunique()} ativos")
    print(f"  {lg['quando'].min():%d/%m/%Y} a {lg['quando'].max():%d/%m/%Y}")

    # panorama in-sample (so para contexto — NAO e evidencia)
    print(f"\n  Panorama in-sample por bloco (contexto, nao evidencia):")
    for b, s in lg.groupby("bloco", observed=True):
        wr = s["acerto"].mean()
        print(f"    {str(b):18} n={len(s):<6} WR={wr:6.2%} EV={s['retorno'].mean():+.4f}u")

    rodar(lg, "bloco", "4 blocos de sessao")
    rodar(lg, "hora", "24 horas individuais")

    print(f"\n{'='*88}")
    print("LEITURA")
    print(f"{'='*88}")
    print("  O teste de permutacao e o arbitro. Ele responde: 'quanta melhora")
    print("  este procedimento de selecao inventa a partir de dados sem sinal?'")
    print("  Se o ganho real nao superar isso, o filtro e ilusao — foi o que")
    print("  aconteceu com a selecao de pares, onde os descartados renderam mais.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
