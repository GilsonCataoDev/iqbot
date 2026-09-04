"""Seleciona horário do pullback USDCAD somente no treino e congela no holdout."""
from __future__ import annotations

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex

JANELAS = [(0, 8), (8, 16), (16, 24)]  # America/New_York; blocos amplos e não sobrepostos


def rodar(candles, inicio, fim, spread=0.00012):
    return simular_forex(
        "USDCAD", candles, banca=10_000, risco_percentual=.0025, spread=spread,
        estrategia="pullback_h1_nova_york",
        parametros_estrategia={"hora_inicio": inicio, "hora_fim": fim, "retorno_risco": 2.0},
    )[0]


def metricas(resultados):
    n = len(resultados)
    wins = int((resultados.lucro > 0).sum()) if n else 0
    wr = wins / n if n else 0
    return n, wr, (wins * 2 - (n - wins)) / n if n else -999


def mostrar(nome, resultados):
    n, wr, ev = metricas(resultados)
    print(f"  {nome:22} n={n:<3} WR={wr:.1%} EV={ev:+.3f}R")
    return n, wr, ev


def main():
    candles = backtest.carregar_cache(configuracao_scalping_m15(), "USDCAD").tail(26_000).copy()
    corte = int(len(candles) * .70)
    treino = candles.iloc[:corte]
    # 220 candles anteriores aquecem EMA/H1, mas resultados anteriores ao corte são removidos.
    holdout = candles.iloc[max(0, corte - 220):]
    inicio_holdout = candles.index[corte]
    candidatos = []
    print("\nUSDCAD pullback H1 | seleção de horário no treino | America/New_York")
    for inicio, fim in JANELAS:
        resultados = rodar(treino, inicio, fim)
        n, _, ev = mostrar(f"treino {inicio:02d}-{fim:02d}h", resultados)
        candidatos.append((ev if n >= 15 else -999, inicio, fim))
    _, inicio, fim = max(candidatos)
    print(f"  janela congelada: {inicio:02d}-{fim:02d}h")
    base = rodar(holdout, inicio, fim)
    base = base[base.aberta_em >= inicio_holdout]
    s15 = rodar(holdout, inicio, fim, .00018)
    s15 = s15[s15.aberta_em >= inicio_holdout]
    s20 = rodar(holdout, inicio, fim, .00024)
    s20 = s20[s20.aberta_em >= inicio_holdout]
    n, _, ev = mostrar("holdout", base)
    mostrar("holdout spread 1,5x", s15)
    _, _, ev20 = mostrar("holdout spread 2,0x", s20)
    aprovado = n >= 50 and ev > .10 and ev20 > 0
    print("DECISAO:", "APROVADA PARA OBSERVACAO" if aprovado else "AMOSTRA INSUFICIENTE/REPROVADA")
    return 0 if aprovado else 2


if __name__ == "__main__":
    raise SystemExit(main())
