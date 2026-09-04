"""Compara sessões por estratégia/par sem escolher horário usando o holdout."""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest_forex_rompimento_reteste_h1 import ATIVOS, SPREAD, SPREAD_PADRAO
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex

ESTRATEGIAS = [
    "rompimento_reteste", "rompimento_reteste_h1", "pullback_h1_confirmado",
    "pullback_h1_com_fibo",
    "toque_lta_ltb", "correcao_fibo_sr", "varredura_londres",
    "pullback_fibo_estrutura_h1",
    "rompimento_asia_londres", "rejeicao_numero_redondo",
    "rompimento_numero_redondo", "reversao_choque_m15", "continuacao_choque_m15",
]
SESSOES = {"asia_00_07": (0, 7), "londres_07_12": (7, 12),
           "overlap_12_17": (12, 17), "tarde_17_24": (17, 24)}
HORARIO_FIXO = {"varredura_londres", "rompimento_asia_londres"}


def metricas(df: pd.DataFrame) -> tuple[int, float, float]:
    n = len(df)
    wins = int((df["lucro"] > 0).sum()) if n else 0
    wr = wins / n if n else 0.0
    return n, wr, (wins * 2 - (n - wins)) / n if n else -999.0


def filtrar_sessao(df: pd.DataFrame, inicio: int, fim: int) -> pd.DataFrame:
    if df.empty:
        return df
    horas = pd.to_datetime(df["aberta_em"]).dt.hour
    return df[(horas >= inicio) & (horas < fim)]


def analisar(estrategia: str, ativo: str, candles: pd.DataFrame) -> dict:
    resultados, _ = simular_forex(
        ativo, candles, banca=10_000, risco_percentual=.0025,
        spread=SPREAD.get(ativo, SPREAD_PADRAO), estrategia=estrategia,
    )
    if resultados.empty:
        return {"estrategia": estrategia, "ativo": ativo, "sessao": "sem_sinais",
                "n_treino": 0, "ev_treino": -999, "n_holdout": 0,
                "wr_holdout": 0, "ev_holdout": -999, "status": "sem_sinais"}
    limite = candles.index[int(len(candles) * .70)]
    treino = resultados[pd.to_datetime(resultados["aberta_em"]) < limite]
    holdout = resultados[pd.to_datetime(resultados["aberta_em"]) >= limite]
    sessoes = SESSOES if estrategia not in HORARIO_FIXO else {"horario_fixo": (0, 24)}
    candidatos = []
    for nome, (inicio, fim) in sessoes.items():
        sub = filtrar_sessao(treino, inicio, fim)
        n, _, ev = metricas(sub)
        candidatos.append((ev if n >= 15 else -999, nome, inicio, fim, n, ev))
    _, nome, inicio, fim, n_t, ev_t = max(candidatos)
    teste = filtrar_sessao(holdout, inicio, fim)
    n_h, wr_h, ev_h = metricas(teste)
    status = (
        "candidata"
        if n_t >= 15 and ev_t > 0 and n_h >= 30 and ev_h > .10
        else ("inconclusiva" if ev_t > 0 and ev_h > 0 else "reprovada")
    )
    return {"estrategia": estrategia, "ativo": ativo, "sessao": nome,
            "n_treino": n_t, "ev_treino": round(ev_t, 4), "n_holdout": n_h,
            "wr_holdout": round(wr_h, 4), "ev_holdout": round(ev_h, 4), "status": status}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", type=int, default=26_000)
    ap.add_argument("--estrategias", nargs="*", default=ESTRATEGIAS)
    ap.add_argument("--ativos", nargs="*", default=ATIVOS)
    args = ap.parse_args()
    cfg = configuracao_scalping_m15()
    dados = {a: backtest.carregar_cache(cfg, a).tail(args.candles).copy() for a in args.ativos}
    linhas = []
    total = len(args.estrategias) * len(args.ativos); atual = 0
    for estrategia in args.estrategias:
        for ativo in args.ativos:
            atual += 1
            print(f"[{atual}/{total}] {estrategia} {ativo}", flush=True)
            linhas.append(analisar(estrategia, ativo, dados[ativo]))
    relatorio = pd.DataFrame(linhas).sort_values(["ev_holdout", "n_holdout"], ascending=False)
    destino = Path("iqoption_m5/dados/validacoes/matriz_horarios_forex.csv")
    destino.parent.mkdir(parents=True, exist_ok=True)
    relatorio.to_csv(destino, index=False, encoding="utf-8-sig")
    print("\nTOP HOLDOUT (horário escolhido só no treino)")
    print(relatorio.head(20).to_string(index=False))
    print(f"\nSalvo em: {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
