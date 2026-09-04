"""Backtesta o padrão 'falso rompimento' como operação FOREX real (SL/TP).

Diferença do binário:
  binário  — win = próxima vela fecha contra o rompimento (qualquer pip)
  forex    — win = preço chega no TP antes do SL

O padrão mostrou 53.8% WR no binário. Em forex, o SL/TP muda a conta:
  - SL natural: acima/abaixo do extremo da vela que rompeu (+ buffer)
  - TP natural: de volta ao centro da faixa de acumulação
  - A meta é manter WR > 33% com R:R >= 2 (ou WR > 40% com R:R >= 1.5)

Uso:
    python backtest_forex_falso_rompimento.py
    python backtest_forex_falso_rompimento.py --jan 15 --rr-min 1.5
    python backtest_forex_falso_rompimento.py --ativos EURUSD NZDUSD
"""
from __future__ import annotations

import argparse
import math
import sys

import numpy as np
import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15

ATIVOS_PADRAO = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD"]
SPREAD = {"EURJPY": 0.015, "USDJPY": 0.015, "AUDUSD": 0.00015, "NZDUSD": 0.00015}
SPREAD_PADRAO = 0.00012
MIN_HIST_REGIME = 200


def _spread(ativo: str) -> float:
    return SPREAD.get(ativo, SPREAD_PADRAO)


def wilson(w, n, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - m), min(1.0, c + m)


def mostra(rot, w, n, be, indent=2):
    if n < 20:
        print(" " * indent + f"{rot:52} n={n:<5} amostra pequena")
        return None
    wr = w / n
    lo, hi = wilson(w, n)
    tag = "APROVADO" if lo > be else ("reprovado" if hi < be else "inconclusivo")
    print(" " * indent + f"{rot:52} n={n:<5} WR={wr:>5.1%} IC=[{lo:.1%},{hi:.1%}] {tag}")
    return wr, lo, hi


def simular_falso_rompimento(
    df: pd.DataFrame,
    ativo: str,
    jan: int = 10,
    buf_sl_atr: float = 0.25,
    rr_min: float = 1.5,
) -> list[dict]:
    """
    Modelo de execução:
      1. Acumulação: amplitude média das `jan` velas anteriores no quartil inferior.
      2. Rompimento: Close cruza o range dessas `jan` velas.
      3. Entrada: Open da vela seguinte, na direção oposta ao rompimento.
      4. SL: extremo da vela de rompimento + buf_sl_atr * ATR14.
      5. TP: meio da faixa de acumulação (alvo natural de retorno).
      6. Skip se R:R < rr_min.
      7. Resolução: percorre velas seguintes verificando se High/Low atinge TP ou SL.
    """
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    sp = _spread(ativo)

    # ATR14 para calibrar o buffer do SL
    tr = pd.concat([H - L, (H - C.shift(1)).abs(), (L - C.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()

    # Swing da acumulação
    amp = H - L
    med_amp = amp.shift(1).rolling(jan).mean()
    # Sem look-ahead: cada candle usa somente amplitudes conhecidas antes dele.
    limiar_apert = med_amp.shift(1).expanding(min_periods=MIN_HIST_REGIME).quantile(0.25)

    rhi = H.shift(1).rolling(jan).max()
    rlo = L.shift(1).rolling(jan).min()
    meio_range = (rhi + rlo) / 2

    apertado = med_amp <= limiar_apert
    rompe_alta = apertado & (C > rhi)
    rompe_baixa = apertado & (C < rlo)

    ops = []
    i = jan + 14
    while i < len(df) - 1:
        atr_i = float(atr.iloc[i])
        if not np.isfinite(atr_i) or atr_i <= 0:
            i += 1
            continue

        buf = buf_sl_atr * atr_i
        meio = float(meio_range.iloc[i])
        entrada_candle_idx = i + 1

        if bool(rompe_alta.iloc[i]):
            # Rompe pra cima → vende (apostar que é falso)
            entrada = float(O.iloc[entrada_candle_idx]) - sp
            sl = float(H.iloc[i]) + buf          # acima da máxima do rompimento
            tp = meio                              # centro da acumulação abaixo
            risco = sl - entrada
            reward = entrada - tp
            if risco <= 0 or reward <= 0 or reward / risco < rr_min:
                i += 1
                continue
            # Resolve: procura próximas velas até TP ou SL
            for j in range(entrada_candle_idx, min(len(df), entrada_candle_idx + 50)):
                baixa_j = float(L.iloc[j]) + sp
                alta_j = float(H.iloc[j]) - sp
                if alta_j >= sl:
                    ops.append({"ativo": ativo, "quando": df.index[j],
                                "lado": "sell", "risco": risco, "reward": reward,
                                "rr": reward/risco, "resultado": "perda", "n_velas": j - entrada_candle_idx + 1})
                    i = j + 1
                    break
                if baixa_j <= tp:
                    ops.append({"ativo": ativo, "quando": df.index[j],
                                "lado": "sell", "risco": risco, "reward": reward,
                                "rr": reward/risco, "resultado": "ganho", "n_velas": j - entrada_candle_idx + 1})
                    i = j + 1
                    break
            else:
                i = entrada_candle_idx + 50

        elif bool(rompe_baixa.iloc[i]):
            # Rompe pra baixo → compra (apostar que é falso)
            entrada = float(O.iloc[entrada_candle_idx]) + sp
            sl = float(L.iloc[i]) - buf          # abaixo da mínima do rompimento
            tp = meio
            risco = entrada - sl
            reward = tp - entrada
            if risco <= 0 or reward <= 0 or reward / risco < rr_min:
                i += 1
                continue
            for j in range(entrada_candle_idx, min(len(df), entrada_candle_idx + 50)):
                alta_j = float(H.iloc[j]) - sp
                baixa_j = float(L.iloc[j]) + sp
                if baixa_j <= sl:
                    ops.append({"ativo": ativo, "quando": df.index[j],
                                "lado": "buy", "risco": risco, "reward": reward,
                                "rr": reward/risco, "resultado": "perda", "n_velas": j - entrada_candle_idx + 1})
                    i = j + 1
                    break
                if alta_j >= tp:
                    ops.append({"ativo": ativo, "quando": df.index[j],
                                "lado": "buy", "risco": risco, "reward": reward,
                                "rr": reward/risco, "resultado": "ganho", "n_velas": j - entrada_candle_idx + 1})
                    i = j + 1
                    break
            else:
                i = entrada_candle_idx + 50
        else:
            i += 1

    return ops


def relatorio(df: pd.DataFrame, rr_min: float) -> None:
    if df.empty:
        print("Nenhuma operação.")
        return

    be = 1 / (1 + rr_min)
    ganhos = int((df["resultado"] == "ganho").sum())
    n = len(df)
    rr_medio = float(df["rr"].mean())

    print(f"\n{'='*84}")
    print(f"Falso Rompimento — Forex com SL/TP  (R:R minimo={rr_min:.1f})")
    print(f"SL: extremo da vela de rompimento + buffer ATR")
    print(f"TP: centro da faixa de acumulacao (retorno natural)")
    print(f"Breakeven para R:R {rr_min:.1f}: {be:.1%}")
    print(f"{'='*84}")

    mostra("TOTAL (todos pares)", ganhos, n, be, indent=0)
    lucro_r = float(df.apply(lambda x: x["rr"] if x["resultado"] == "ganho" else -1.0, axis=1).sum())
    print(f"  R:R medio planejado: {rr_medio:.2f}  |  lucro exato: {lucro_r:+.1f}R | EV={lucro_r/n:+.3f}R/trade")
    print(f"  velas medias ate resolucao: {df['n_velas'].mean():.1f}")

    print("\n  por par:")
    for ativo in sorted(df["ativo"].unique()):
        sub = df[df["ativo"] == ativo]
        g = int((sub["resultado"] == "ganho").sum())
        mostra(ativo, g, len(sub), be)

    print("\n  por lado:")
    for lado in ["buy", "sell"]:
        sub = df[df["lado"] == lado]
        g = int((sub["resultado"] == "ganho").sum())
        mostra(f"  {lado}", g, len(sub), be)
        if len(sub):
            lucro_lado = float(sub.apply(lambda x: x["rr"] if x["resultado"] == "ganho" else -1.0, axis=1).sum())
            rr_lado = float(sub["rr"].mean())
            print(f"      R:R medio={rr_lado:.2f} | breakeven medio={1/(1+rr_lado):.1%} | EV={lucro_lado/len(sub):+.3f}R")

    # Split temporal
    datas = sorted(df["quando"].unique())
    corte = datas[len(datas) // 2]
    cal = df[df["quando"] <= corte]
    hold = df[df["quando"] > corte]
    g_cal = int((cal["resultado"] == "ganho").sum())
    g_hold = int((hold["resultado"] == "ganho").sum())
    print(f"\n  estabilidade temporal (corte {pd.Timestamp(corte):%d/%m/%Y}):")
    mostra(f"calibracao", g_cal, len(cal), be)
    mostra(f"holdout   ", g_hold, len(hold), be)
    if len(cal) and len(hold):
        wr_c = g_cal / len(cal)
        wr_h = g_hold / len(hold)
        print(f"  queda: {(wr_h - wr_c)*100:+.1f}pp")

    buy = df[df["lado"] == "buy"].sort_values("quando")
    if len(buy) >= 2:
        corte_buy = buy.iloc[len(buy) // 2]["quando"]
        print(f"\n  BUY-only (estrategia operacional; corte {pd.Timestamp(corte_buy):%d/%m/%Y}):")
        for nome, sub in (("calibracao", buy[buy["quando"] <= corte_buy]),
                          ("holdout", buy[buy["quando"] > corte_buy])):
            ganhos_sub = int((sub["resultado"] == "ganho").sum())
            mostra(nome, ganhos_sub, len(sub), 1 / (1 + float(sub["rr"].mean())))
            lucro_sub = float(sub.apply(
                lambda x: x["rr"] if x["resultado"] == "ganho" else -1.0, axis=1
            ).sum())
            print(f"      EV={lucro_sub/len(sub):+.3f}R/trade")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jan", type=int, default=10)
    ap.add_argument("--buf-sl-atr", type=float, default=0.25)
    ap.add_argument("--rr-min", type=float, default=1.5)
    ap.add_argument("--tf", type=int, default=900, choices=[300, 900, 3600])
    ap.add_argument("--ativos", nargs="*", default=ATIVOS_PADRAO)
    a = ap.parse_args()

    import dataclasses
    config = configuracao_scalping_m15()
    cfg = dataclasses.replace(config, timeframe_segundos=a.tf)
    tf_nome = {300: "M5", 900: "M15", 3600: "H1"}.get(a.tf, str(a.tf))
    print(f"Timeframe: {tf_nome} | jan={a.jan} | buf_sl={a.buf_sl_atr:.2f}xATR | R:R>={a.rr_min:.1f}")

    todas_ops = []
    for ativo in a.ativos:
        df = backtest.carregar_cache(cfg, ativo)
        if df is None:
            print(f"  {ativo}: sem dados")
            continue
        print(f"  {ativo}: {len(df)} candles ({df.index[0]:%d/%m/%Y} a {df.index[-1]:%d/%m/%Y})")
        ops = simular_falso_rompimento(df, ativo, jan=a.jan, buf_sl_atr=a.buf_sl_atr, rr_min=a.rr_min)
        todas_ops.extend(ops)

    relatorio(pd.DataFrame(todas_ops), a.rr_min)
    return 0


if __name__ == "__main__":
    sys.exit(main())
