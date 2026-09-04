"""Estrategias por REGIME: lateral, canal de alta, canal de baixa.

Classificacao objetiva via regressao linear sobre as ultimas N velas:
  - inclinacao (normalizada por ATR) diz se ha canal e para que lado
  - dispersao dos residuos define a largura do canal
  - R2 diz se o canal e limpo ou se e so ruido

  |slope| baixo + R2 baixo   -> LATERAL
  slope > 0 + R2 razoavel    -> CANAL DE ALTA
  slope < 0 + R2 razoavel    -> CANAL DE BAIXA
  R2 alto + slope alto       -> TENDENCIA FORTE (canal nao segura)

Entradas testadas (todas com shift(1), zero lookahead):
  LATERAL      : compra no piso do range, vende no teto
  CANAL ALTA   : compra na banda inferior (a favor do canal)
  CANAL BAIXA  : vende na banda superior
  CONTRA-CANAL : o inverso, como controle — se render igual, o canal nao informa

Tudo passa pelo motor corrigido e e comparado ao baseline aleatorio.

Uso:
    python estrategias_regime.py
    python estrategias_regime.py --forex-tb   (mede tambem no modo forex SL/TP)
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

import motor_sinais as M

FOREX = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD",
         "EURGBP", "EURCHF", "EURCAD", "GBPJPY", "GBPCHF", "GBPCAD", "CADCHF"]
CRYPTO = ["BTCUSD", "ETHUSD", "XRPUSD"]

JAN = 40          # velas da regressao (10h de M15)
TOCA = 0.15       # quao perto da banda conta como toque (fracao da largura)


# ---------------------------------------------------------------------------
# Classificacao de regime
# ---------------------------------------------------------------------------

def regressao(df: pd.DataFrame, jan: int = JAN) -> pd.DataFrame:
    """Regressao linear movel. Tudo shift(1): nada usa a vela atual."""
    C = df["Close"]
    n = jan
    x = np.arange(n)
    x_c = x - x.mean()
    sxx = (x_c ** 2).sum()

    vals = C.shift(1).rolling(n)
    # slope via covariancia; media e desvio para R2 e bandas
    def _slope(w):
        return float((x_c * (w - w.mean())).sum() / sxx)

    def _resid(w):
        s = float((x_c * (w - w.mean())).sum() / sxx)
        pred = w.mean() + s * x_c
        return float(np.sqrt(((w - pred) ** 2).mean()))

    def _r2(w):
        s = float((x_c * (w - w.mean())).sum() / sxx)
        pred = w.mean() + s * x_c
        ss_res = ((w - pred) ** 2).sum()
        ss_tot = ((w - w.mean()) ** 2).sum()
        return float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    out = pd.DataFrame(index=df.index)
    out["slope"] = vals.apply(_slope, raw=True)
    out["disp"] = vals.apply(_resid, raw=True)
    out["r2"] = vals.apply(_r2, raw=True)
    out["centro"] = C.shift(1).rolling(n).mean()
    # projeta o centro para a vela atual (o canal continua)
    out["centro_hoje"] = out["centro"] + out["slope"] * (n / 2)
    a = M.atr(df)
    out["slope_atr"] = out["slope"] / a.replace(0, np.nan)   # inclinacao por vela em ATR
    out["banda_sup"] = out["centro_hoje"] + 2 * out["disp"]
    out["banda_inf"] = out["centro_hoje"] - 2 * out["disp"]
    out["larg"] = 4 * out["disp"]
    return out


def classificar(r: pd.DataFrame,
                slope_lat: float = 0.05, r2_min: float = 0.30) -> pd.Series:
    """LATERAL / CANAL_ALTA / CANAL_BAIXA / TENDENCIA / indefinido."""
    s, r2 = r["slope_atr"], r["r2"]
    cls = pd.Series("indefinido", index=r.index)
    cls[(s.abs() <= slope_lat) & (r2 < r2_min)] = "LATERAL"
    cls[(s > slope_lat) & (r2 >= r2_min)] = "CANAL_ALTA"
    cls[(s < -slope_lat) & (r2 >= r2_min)] = "CANAL_BAIXA"
    cls[(s.abs() > slope_lat * 4) & (r2 >= 0.75)] = "TENDENCIA"
    return cls


# ---------------------------------------------------------------------------
# Sinais
# ---------------------------------------------------------------------------

def _toques(df, r):
    """Quao perto o Low/High chegou das bandas, em fracao da largura."""
    larg = r["larg"].replace(0, np.nan)
    d_inf = (df["Low"] - r["banda_inf"]) / larg
    d_sup = (r["banda_sup"] - df["High"]) / larg
    return d_inf, d_sup


def s_lateral(df):
    """Range: compra tocando o piso, vende tocando o teto."""
    r = regressao(df)
    cls = classificar(r)
    d_inf, d_sup = _toques(df, r)
    lat = cls == "LATERAL"
    return ((lat & (d_inf.abs() <= TOCA)).fillna(False),
            (lat & (d_sup.abs() <= TOCA)).fillna(False))


def s_canal_alta(df):
    """Canal ascendente: compra na banda inferior (a favor do canal)."""
    r = regressao(df)
    cls = classificar(r)
    d_inf, _ = _toques(df, r)
    return ((cls == "CANAL_ALTA") & (d_inf.abs() <= TOCA)).fillna(False), \
           pd.Series(False, index=df.index)


def s_canal_baixa(df):
    """Canal descendente: vende na banda superior."""
    r = regressao(df)
    cls = classificar(r)
    _, d_sup = _toques(df, r)
    return pd.Series(False, index=df.index), \
           ((cls == "CANAL_BAIXA") & (d_sup.abs() <= TOCA)).fillna(False)


def s_canal_contra(df):
    """CONTROLE: opera contra o canal. Se render igual, o canal nao informa nada."""
    r = regressao(df)
    cls = classificar(r)
    d_inf, d_sup = _toques(df, r)
    return ((cls == "CANAL_BAIXA") & (d_inf.abs() <= TOCA)).fillna(False), \
           ((cls == "CANAL_ALTA") & (d_sup.abs() <= TOCA)).fillna(False)


def s_rompe_canal(df):
    """Rompimento do canal: fecha fora da banda, segue o rompimento."""
    r = regressao(df)
    cls = classificar(r)
    dentro = cls.isin(["LATERAL", "CANAL_ALTA", "CANAL_BAIXA"])
    C = df["Close"]
    return (dentro & (C > r["banda_sup"])).fillna(False), \
           (dentro & (C < r["banda_inf"])).fillna(False)


SINAIS = {
    "aleatorio (BASELINE)": M.s_aleatorio,
    "LATERAL: piso/teto":   s_lateral,
    "CANAL ALTA: compra piso": s_canal_alta,
    "CANAL BAIXA: vende teto": s_canal_baixa,
    "CONTRA-canal (controle)": s_canal_contra,
    "rompe canal (segue)":  s_rompe_canal,
    "falso rompimento (ref)": M.s_falso_rompimento,
}


# ---------------------------------------------------------------------------
# Relatorio
# ---------------------------------------------------------------------------

def distribuicao_regime(frames) -> None:
    print(f"\n{'='*86}")
    print("QUANTO TEMPO O MERCADO PASSA EM CADA REGIME")
    print(f"{'='*86}")
    tot = {}
    for a, df in frames.items():
        cls = classificar(regressao(df)).dropna()
        vc = cls.value_counts(normalize=True)
        for k, v in vc.items():
            tot.setdefault(k, []).append(v)
    print(f"  {'regime':14} {'% do tempo':>11}")
    for k in ["LATERAL", "CANAL_ALTA", "CANAL_BAIXA", "TENDENCIA", "indefinido"]:
        if k in tot:
            print(f"  {k:14} {np.mean(tot[k]):>10.1%}")


def avaliar(frames, rotulo: str, forex_tb: bool) -> None:
    print(f"\n{'='*86}")
    print(f"{rotulo} — modo BINARIA (direcao da proxima vela, breakeven 54.05%)")
    print(f"{'='*86}")
    print(f"  {'sinal':26} {'n':>7} {'n_ef':>6} {'WR':>7} {'IC95 (n_ef)':>18}  veredito")
    base_wr = None
    for nome, fn in SINAIS.items():
        d = M.avaliar_binario(frames, fn)
        if d.empty or len(d) < 100:
            print(f"  {nome:26} {len(d):>7}  amostra pequena")
            continue
        s = M.stats_binario(d, 0.5405)
        if "BASELINE" in nome:
            base_wr = s["wr"]
        vd = "EDGE" if s["lo_ef"] > 0.5405 else ("negativo" if s["hi_ef"] < 0.5405 else "---")
        extra = ""
        if base_wr is not None and "BASELINE" not in nome:
            extra = f"  ({(s['wr']-base_wr)*100:+.1f}pp vs acaso)"
        print(f"  {nome:26} {s['n']:>7} {s['n_ef']:>6} {s['wr']:>6.2%} "
              f" [{s['lo_ef']:.2%},{s['hi_ef']:.2%}]  {vd}{extra}")

    if not forex_tb:
        return
    print(f"\n  modo FOREX (SL/TP em ATR, EV com R realizado, SL antes do TP)")
    print(f"  {'sinal':26} {'SL':>4} {'TP':>4} {'n':>6} {'WR':>6} {'EV':>9}")
    for nome, fn in SINAIS.items():
        if "BASELINE" in nome:
            continue
        melhor = None
        for sl, tp in [(1.0, 2.0), (1.5, 2.0), (2.0, 2.0), (2.0, 3.0)]:
            d = M.simular_forex(frames, fn, sl, tp)
            if len(d) < 50:
                continue
            s = M.stats_forex(d)
            if melhor is None or s["ev"] > melhor[3]:
                melhor = (sl, tp, s["n"], s["ev"], s["wr"])
        if melhor:
            sl, tp, n, ev, wr = melhor
            print(f"  {nome:26} {sl:>4.1f} {tp:>4.1f} {n:>6} {wr:>5.1%} {ev:>+9.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--forex-tb", action="store_true")
    ap.add_argument("--so", choices=["forex", "crypto"], default=None)
    a = ap.parse_args()

    print("=" * 86)
    print("ESTRATEGIAS POR REGIME — lateral, canal de alta, canal de baixa")
    print("=" * 86)
    print(f"  regressao sobre {JAN} velas | toque = {TOCA:.0%} da largura do canal")
    print("  CONTRA-canal e o controle: se render igual ao canal, a classificacao")
    print("  nao esta informando nada.")

    if a.so != "crypto":
        fr = M.carregar(900, FOREX)
        distribuicao_regime(fr)
        avaliar(fr, f"FOREX ({len(fr)} pares)", a.forex_tb)
    if a.so != "forex":
        fc = M.carregar(900, CRYPTO)
        if fc:
            distribuicao_regime(fc)
            avaliar(fc, f"CRYPTO ({len(fc)} ativos)", a.forex_tb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
