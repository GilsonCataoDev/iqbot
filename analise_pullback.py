"""Analisa o dump do backtest em busca de filtros que melhorem os pullbacks.

Uso:
    # 1. gera o dump (demora ~5min, faz cache offline):
    python backtest_m15.py --tf m15 --candles 20000 --dump sinais.pkl

    # 2. analisa:
    python analise_pullback.py sinais.pkl
"""
from __future__ import annotations

import math
import sys
import warnings

import pandas as pd

warnings.filterwarnings("ignore")

PAYOUT = 0.85
BE = 1 / (1 + PAYOUT)   # 54.05%
SETUPS_REV = {"sr_rejeicao", "pin_bar_sr", "engulfing_sr", "fibo_sr_retracao"}


def stats(df: pd.DataFrame):
    d = df[df.res != "empate"]
    n = len(d)
    if n < 20:
        return None
    wr = (d.res == "ganho").mean()
    lucro = (d.res == "ganho").sum() * PAYOUT - (d.res == "perda").sum()
    se = math.sqrt(wr * (1 - wr) / n)
    lo, hi = wr - 1.96 * se, wr + 1.96 * se
    return {"n": n, "wr": wr, "lucro": lucro, "lo": lo, "hi": hi}


def linha(lbl: str, df: pd.DataFrame, indent: int = 2) -> str:
    s = stats(df)
    if s is None:
        return " " * indent + f"{lbl:<38} n<20"
    wr, be, lucro, n, lo, hi = s["wr"], BE, s["lucro"], s["n"], s["lo"], s["hi"]
    if hi < be:
        v = " ← NEGATIVO"
    elif lo > be:
        v = " ← POSITIVO ✓"
    else:
        v = " ← inconclusivo"
    return (" " * indent +
            f"{lbl:<38} n={n:<5} WR={wr:.1%}  "
            f"IC=[{lo:.0%},{hi:.0%}]  lucro={lucro:+.1f}u{v}")


def secao(titulo: str, df: pd.DataFrame, col: str, n_bins: int = 5):
    """Divide df em quantis de `col` e mostra stats por fatia."""
    df2 = df.dropna(subset=[col]).copy()
    if len(df2) < 50:
        print(f"  {col}: poucos dados ({len(df2)})")
        return
    df2["_bin"] = pd.qcut(df2[col], n_bins, duplicates="drop")
    print(f"\n  Slice por {col} ({n_bins} quantis):")
    for label, grupo in df2.groupby("_bin", observed=True):
        intervalo = f"{label.left:.2f}–{label.right:.2f}"
        print(linha(f"  {intervalo}", grupo, indent=4))


def analise_pullbacks(df: pd.DataFrame):
    pb = df[~df.setup.isin(SETUPS_REV)].copy()
    print("\n" + "=" * 70)
    print("ANÁLISE DE PULLBACKS — busca de filtros com edge positivo")
    print(f"Total de sinais de continuação: {len(pb)}")
    print("=" * 70)

    if pb.empty:
        print("  sem pullbacks no dump")
        return

    # --- visão geral ---
    s = stats(pb)
    if s:
        print(f"\n  Geral: n={s['n']}  WR={s['wr']:.1%}  IC=[{s['lo']:.0%},{s['hi']:.0%}]  lucro={s['lucro']:+.1f}u")

    # --- por setup ---
    print("\n  Por setup:")
    for k, g in pb.groupby("setup"):
        print(linha(str(k), g, indent=4))

    # --- por tendência H4 ---
    print("\n  Por tendência H4 (só operações A FAVOR da tendência):")
    if "h4" in pb.columns:
        favor = pb[(pb.h4 == "alta") & (pb.direcao == "call") |
                   (pb.h4 == "baixa") & (pb.direcao == "put")]
        contra = pb[(pb.h4 == "alta") & (pb.direcao == "put") |
                    (pb.h4 == "baixa") & (pb.direcao == "call")]
        lateral = pb[pb.h4 == "lateral"]
        print(linha("  H4 a favor", favor,   indent=4))
        print(linha("  H4 contra",  contra,  indent=4))
        print(linha("  H4 lateral", lateral, indent=4))

    # --- slice por RSI ---
    if "rsi" in pb.columns and pb.rsi.notna().sum() > 50:
        secao("RSI", pb, "rsi")
        # zonas fixas mais úteis para interpretar
        print("\n  RSI por zona fixa:")
        print(linha("  RSI < 35 (oversold)", pb[pb.rsi < 35],  indent=4))
        print(linha("  RSI 35-45",           pb[(pb.rsi >= 35) & (pb.rsi < 45)], indent=4))
        print(linha("  RSI 45-55 (neutro)",  pb[(pb.rsi >= 45) & (pb.rsi < 55)], indent=4))
        print(linha("  RSI 55-65",           pb[(pb.rsi >= 55) & (pb.rsi < 65)], indent=4))
        print(linha("  RSI > 65 (overbought)", pb[pb.rsi > 65], indent=4))
        # direção certa: CALL com RSI baixo, PUT com RSI alto
        print("\n  RSI alinhado com direção (CALL RSI<50, PUT RSI>50):")
        alinhado = pb[((pb.direcao == "call") & (pb.rsi < 50)) |
                      ((pb.direcao == "put")  & (pb.rsi > 50))]
        desalin  = pb[((pb.direcao == "call") & (pb.rsi >= 50)) |
                      ((pb.direcao == "put")  & (pb.rsi <= 50))]
        print(linha("  RSI alinhado",    alinhado, indent=4))
        print(linha("  RSI desalinhado", desalin,  indent=4))

    # --- slice por slope de EMA ---
    if "ema_slope" in pb.columns and pb.ema_slope.notna().sum() > 50:
        secao("ema_slope", pb, "ema_slope")
        print("\n  EMA slope (tendência forte vs fraca):")
        forte  = pb[pb.ema_slope.abs() > pb.ema_slope.abs().quantile(0.6)]
        fraco  = pb[pb.ema_slope.abs() <= pb.ema_slope.abs().quantile(0.4)]
        print(linha("  slope forte (top 40%)", forte, indent=4))
        print(linha("  slope fraco (bot 40%)", fraco, indent=4))

    # --- slice por EMA gap ---
    if "ema_gap" in pb.columns and pb.ema_gap.notna().sum() > 50:
        secao("ema_gap", pb, "ema_gap")
        print("\n  EMA gap (quão em tendência):")
        grande = pb[pb.ema_gap > pb.ema_gap.quantile(0.6)]
        pequeno= pb[pb.ema_gap <= pb.ema_gap.quantile(0.4)]
        print(linha("  gap grande (EMAs separadas)", grande,  indent=4))
        print(linha("  gap pequeno (EMAs juntas)",   pequeno, indent=4))

    # --- combinações promissoras ---
    print("\n" + "-" * 70)
    print("  COMBINAÇÕES (só as com n≥30):")
    candidatos = [
        ("H4 a favor + RSI alinhado",
         pb[((pb.h4 == "alta") & (pb.direcao == "call") & (pb.rsi < 50)) |
            ((pb.h4 == "baixa") & (pb.direcao == "put") & (pb.rsi > 50))]),
        ("H4 a favor + slope forte",
         pb[((pb.h4 == "alta") & (pb.direcao == "call")) |
            ((pb.h4 == "baixa") & (pb.direcao == "put"))]
           .pipe(lambda d: d[d.ema_slope.abs() > d.ema_slope.abs().quantile(0.5)])),
        ("RSI alinhado + slope forte",
         pb[((pb.direcao == "call") & (pb.rsi < 45)) |
            ((pb.direcao == "put")  & (pb.rsi > 55))]
           .pipe(lambda d: d[d.ema_slope.abs() > d.ema_slope.abs().quantile(0.5)]
                            if "ema_slope" in d.columns else d)),
        ("H4+H1 a favor + RSI alinhado + gap grande",
         pb[((pb.h4 == "alta") & (pb.h1.isin(["alta","lateral"])) & (pb.direcao == "call") & (pb.rsi < 50)) |
            ((pb.h4 == "baixa") & (pb.h1.isin(["baixa","lateral"])) & (pb.direcao == "put") & (pb.rsi > 50))]
           .pipe(lambda d: d[d.ema_gap > d.ema_gap.quantile(0.5)]
                            if "ema_gap" in d.columns else d)),
    ]
    for nome, subdf in candidatos:
        if len(subdf) >= 30:
            print(linha(f"  {nome}", subdf, indent=4))

    # --- estabilidade temporal da melhor combinação ---
    melhor = max(candidatos, key=lambda x: (stats(x[1]) or {"wr": 0})["wr"])
    nome_m, df_m = melhor
    sm = stats(df_m)
    if sm and sm["lo"] > BE:
        print(f"\n  *** MELHOR combinação com IC positivo: [{nome_m}]")
        df_m = df_m.sort_values("quando")
        meio = df_m.quando.quantile(0.5)
        print(linha("    1a metade (out-of-sample)", df_m[df_m.quando <= meio], indent=6))
        print(linha("    2a metade (out-of-sample)", df_m[df_m.quando > meio],  indent=6))
    else:
        print("\n  Nenhuma combinação atingiu IC95% acima do breakeven.")
        print("  Recomendação: desativar pullback, focar no sr_rejeicao.")


def main():
    if len(sys.argv) < 2:
        print("Uso: python analise_pullback.py sinais.pkl")
        sys.exit(1)
    pkl = sys.argv[1]
    print(f"Carregando {pkl}...")
    df = pd.read_pickle(pkl)
    print(f"  {len(df)} sinais totais")
    analise_pullbacks(df)


if __name__ == "__main__":
    main()
