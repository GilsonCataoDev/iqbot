"""Backtest do setup Fibonacci — retração a 38.2% ou 61.8% a favor da tendência.

Modelagem fiel ao que o usuário descrevia manualmente:
  - Tendência: EMA(21) vs EMA(55) no mesmo timeframe
  - Swing: máximo e mínimo das últimas JAN velas (shift(1) → sem lookahead)
  - Nível Fib: 38.2% e 61.8% do range do swing
  - Sinal: fechamento toca o nível ± tolerância, na direção da tendência
  - Entrada: Open do candle SEGUINTE (como a binária executa)
  - Saída: Close do mesmo candle de entrada (expiração fim da vela)

Funciona com qualquer timeframe presente no histórico: 300s (M5) ou 900s (M15).
Uso:
    python backtest_fibo.py
    python backtest_fibo.py --tf 300        # M5
    python backtest_fibo.py --tf 900        # M15 (padrão)
    python backtest_fibo.py --jan 30 --tol 0.15
    python backtest_fibo.py --ativos EURUSD GBPUSD
"""
from __future__ import annotations

import argparse
import math
import sys

import numpy as np
import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15

PAYOUT = {
    "EURUSD": 0.85, "GBPUSD": 0.85, "USDJPY": 0.85,
    "AUDUSD": 0.85, "EURJPY": 0.85,
    "USDCAD": 0.87, "NZDUSD": 0.87,
}
PAYOUT_PADRAO = 0.85

FIB_382 = 0.382
FIB_618 = 0.618


def wilson(w, n, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - m), min(1.0, c + m)


def mostra(rot, w, n, be, indent=2):
    if n < 30:
        print(" " * indent + f"{rot:48} n={n:<5} amostra pequena")
        return
    wr = w / n
    lo, hi = wilson(w, n)
    lu = w * PAYOUT_PADRAO - (n - w)
    tag = "APROVADO" if lo > be else ("reprovado" if hi < be else "inconclusivo")
    print(" " * indent + f"{rot:48} n={n:<5} WR={wr:>5.1%} IC=[{lo:.1%},{hi:.1%}] "
          f"lucro={lu:+7.1f}u  {tag}")


def sinais(df: pd.DataFrame, jan: int = 20, tol_pct: float = 0.10) -> pd.DataFrame:
    """Gera sinais de entrada no Fibonacci.

    Tudo com shift(1): o swing range e os EMAs são calculados sobre velas
    ANTERIORES. Zero lookahead — o sinal ocorre no fechamento da vela i,
    a entrada é no Open da vela i+1.

    Parâmetros
    ----------
    jan : int
        Número de velas para calcular o swing (max High, min Low).
    tol_pct : float
        Tolerância para "toque no nível Fib" em % do range do swing.
        Ex: 0.10 = 10% do range acima/abaixo do nível conta como toque.
    """
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]

    # EMAs para detectar tendência — shift(1) pois usamos o fechamento anterior
    ema21 = C.ewm(span=21, adjust=False).mean().shift(1)
    ema55 = C.ewm(span=55, adjust=False).mean().shift(1)

    # Swing range das JAN velas ANTERIORES (shift(1) para não incluir a vela atual)
    swing_hi = H.shift(1).rolling(jan).max()
    swing_lo = L.shift(1).rolling(jan).min()
    rng = swing_hi - swing_lo

    # Níveis de Fibonacci
    fib382 = swing_lo + rng * FIB_382
    fib618 = swing_lo + rng * FIB_618
    tol = rng * tol_pct

    # Tendência de alta → compra nas retrações (fib382/fib618 como suporte)
    tendencia_alta = ema21 > ema55
    # Toca o nível: fechamento dentro da faixa [nivel - tol, nivel + tol]
    toca_382_suporte = (C >= fib382 - tol) & (C <= fib382 + tol)
    toca_618_suporte = (C >= fib618 - tol) & (C <= fib618 + tol)

    # Tendência de baixa → venda nas recuperações (fib382/fib618 como resistência)
    tendencia_baixa = ema21 < ema55
    toca_382_resist = toca_382_suporte   # mesmos preços, direção oposta
    toca_618_resist = toca_618_suporte

    out = pd.DataFrame(index=df.index)
    out["ema21"] = ema21
    out["ema55"] = ema55
    out["swing_hi"] = swing_hi
    out["swing_lo"] = swing_lo
    out["rng"] = rng
    out["fib382"] = fib382
    out["fib618"] = fib618

    # Sinais de CALL (alta) e PUT (baixa) em cada nível
    out["call_382"] = tendencia_alta & toca_382_suporte
    out["call_618"] = tendencia_alta & toca_618_suporte
    out["put_382"] = tendencia_baixa & toca_382_resist
    out["put_618"] = tendencia_baixa & toca_618_resist

    # Qualquer sinal ativo
    out["call"] = out["call_382"] | out["call_618"]
    out["put"] = out["put_382"] | out["put_618"]
    out["sinal"] = out["call"] | out["put"]

    # Resultado da vela SEGUINTE (entrada no Open, saída no Close)
    prox_o = O.shift(-1)
    prox_c = C.shift(-1)
    out["prox_sobe"] = prox_c > prox_o
    out["valido"] = prox_c != prox_o   # empate exato: não conta

    # Acerto: CALL → precisa subir; PUT → precisa cair
    out["acerta"] = np.where(
        out["call"] & out["valido"], out["prox_sobe"],
        np.where(out["put"] & out["valido"], ~out["prox_sobe"], False)
    )
    return out


def rodar_par(ativo: str, df: pd.DataFrame, jan: int, tol: float) -> pd.DataFrame:
    s = sinais(df, jan=jan, tol_pct=tol)
    s = s[s["sinal"] & s["valido"]].copy()
    s["ativo"] = ativo
    s["payout"] = PAYOUT.get(ativo, PAYOUT_PADRAO)
    return s


def relatorio(ops: pd.DataFrame, jan: int, tol: float) -> None:
    print(f"\n{'='*84}")
    print(f"Fibonacci 38.2%/61.8% — jan={jan} velas, tolerância={tol:.0%} do range")
    print(f"Tendência: EMA(21) vs EMA(55). Entrada: Open+1, Saída: Close+1.")
    print(f"{'='*84}")

    if ops.empty:
        print("Nenhum sinal gerado.")
        return

    total_w = int(ops["acerta"].sum())
    total_n = len(ops)
    be_med = float((ops["payout"].apply(lambda p: 1/(1+p))).mean())

    mostra("TOTAL (todos pares e níveis)", total_w, total_n, be_med, indent=0)

    print("\n  por par:")
    for ativo in sorted(ops["ativo"].unique()):
        sub = ops[ops["ativo"] == ativo]
        be = 1 / (1 + PAYOUT.get(ativo, PAYOUT_PADRAO))
        mostra(ativo, int(sub["acerta"].sum()), len(sub), be)

    print("\n  por nível Fibonacci:")
    for col, rot in [("call_382", "CALL no 38.2% (suporte, alta)"),
                     ("call_618", "CALL no 61.8% (suporte, alta)"),
                     ("put_382",  "PUT  no 38.2% (resistência, baixa)"),
                     ("put_618",  "PUT  no 61.8% (resistência, baixa)")]:
        if col in ops.columns:
            sub = ops[ops[col] & ops["valido"]] if "valido" not in ops.columns else ops[ops[col]]
            mostra(rot, int(sub["acerta"].sum()), len(sub), be_med)

    # Estabilidade temporal: 1a vs 2a metade
    metades = len(ops) // 2
    if metades >= 30:
        print("\n  estabilidade (1a metade -> 2a metade):")
        ord_ops = ops.sort_index()
        p1 = ord_ops.iloc[:metades]
        p2 = ord_ops.iloc[metades:]
        mostra("1a metade", int(p1["acerta"].sum()), len(p1), be_med)
        mostra("2a metade", int(p2["acerta"].sum()), len(p2), be_med)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", type=int, default=900,
                    choices=[60, 300, 900, 3600],
                    help="timeframe em segundos: 60=M1, 300=M5, 900=M15, 3600=H1")
    ap.add_argument("--jan", type=int, default=20,
                    help="janela do swing em candles (padrão 20)")
    ap.add_argument("--tol", type=float, default=0.10,
                    help="tolerância de toque em fração do range (padrão 0.10)")
    ap.add_argument("--ativos", nargs="*", default=None)
    ap.add_argument("--max-candles", type=int, default=0,
                    help="limita candles por ativo (0 = todos)")
    a = ap.parse_args()

    config = configuracao_scalping_m15()
    ativos = a.ativos or list(config.ativos)

    tf_nome = {60: "M1", 300: "M5", 900: "M15", 3600: "H1"}.get(a.tf, f"{a.tf}s")
    print(f"Timeframe: {tf_nome}  |  Janela: {a.jan} velas  |  Tolerância: {a.tol:.0%}")

    # Carrega dados — reutiliza a função de cache já existente, mas override
    # do timeframe_segundos para suportar M5 sem reescrever a config
    import dataclasses
    cfg_tf = dataclasses.replace(config, timeframe_segundos=a.tf)

    frames: dict[str, pd.DataFrame] = {}
    for ativo in ativos:
        df = backtest.carregar_cache(cfg_tf, ativo)
        if df is None:
            print(f"  {ativo}: sem cache ({tf_nome}) — rode o bot ou baixe o histórico.")
            continue
        if a.max_candles > 0:
            df = df.tail(a.max_candles)
        frames[ativo] = df
        print(f"  {ativo}: {len(df)} candles ({df.index[0]:%d/%m/%Y} a {df.index[-1]:%d/%m/%Y})")

    if not frames:
        print("Sem dados. Rode o bot primeiro ou use --tf 900 (M15 tem histórico).")
        return 1

    todas = []
    for ativo, df in frames.items():
        ops = rodar_par(ativo, df, jan=a.jan, tol=a.tol)
        todas.append(ops)

    ops_total = pd.concat(todas) if todas else pd.DataFrame()
    relatorio(ops_total, jan=a.jan, tol=a.tol)

    # Holdout split: 60% calibração, 40% holdout
    if len(ops_total) >= 100:
        print(f"\n{'='*84}")
        print("HOLDOUT (últimos 40% por tempo — sem olhar antes de rodar)")
        print(f"{'='*84}")
        todas_datas = sorted(ops_total.index.unique())
        corte_idx = int(len(todas_datas) * 0.60)
        corte = todas_datas[corte_idx]
        cal = ops_total[ops_total.index <= corte]
        hold = ops_total[ops_total.index > corte]
        be_med = float((ops_total["payout"].apply(lambda p: 1/(1+p))).mean())
        mostra(f"calibração (até {corte:%d/%m/%Y})", int(cal["acerta"].sum()), len(cal), be_med)
        mostra(f"holdout    (após {corte:%d/%m/%Y})", int(hold["acerta"].sum()), len(hold), be_med)
        wr_cal = cal["acerta"].sum() / len(cal) if len(cal) else 0
        wr_hold = hold["acerta"].sum() / len(hold) if len(hold) else 0
        print(f"\n  queda calibracao->holdout: {(wr_hold - wr_cal)*100:+.1f}pp  "
              f"(positivo = melhorou; negativo = overfitting)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
