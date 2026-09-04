"""Motor compartilhado: biblioteca de sinais + avaliadores corrigidos.

TODOS os bugs da auditoria estao corrigidos aqui:
  B1 EV via R realizado (nao WR * media(R:R))
  B2 sem lookahead (quantis expanding + shift)
  B3 SL checado ANTES do TP (conservador no empate intra-vela)
  B4 trades nao-sobrepostos no modo forex
  B5 holdout temporal (nao por par)
  B6 empates binarios excluidos (nao contam como perda)
  B7 n efetivo por timestamp unico para o IC

Convencao dos sinais:
  Sinal observado no FECHAMENTO da vela i -> acao no ABERTURA da vela i+1.
  Toda feature usa shift(1) ou rolling sobre velas <= i. Zero futuro.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Callable

import numpy as np
import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15

ATIVOS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD"]
SPREAD = {"EURJPY": 0.015, "USDJPY": 0.015}
SPREAD_PAD = 0.00012
PAYOUT = {"USDCAD": 0.87, "NZDUSD": 0.87}
PAYOUT_PAD = 0.85

TF_NOME = {60: "M1", 300: "M5", 900: "M15", 3600: "H1"}


def spread(a: str) -> float: return SPREAD.get(a, SPREAD_PAD)
def pip(a: str) -> float: return 0.01 if "JPY" in a else 0.0001
def payout(a: str) -> float: return PAYOUT.get(a, PAYOUT_PAD)
def breakeven_bin(a: str) -> float: return 1.0 / (1.0 + payout(a))


def carregar(tf: int = 900, ativos: list[str] | None = None) -> dict[str, pd.DataFrame]:
    cfg = dataclasses.replace(configuracao_scalping_m15(), timeframe_segundos=tf)
    out = {}
    for a in (ativos or ATIVOS):
        df = backtest.carregar_cache(cfg, a)
        if df is not None and len(df) > 1000:
            out[a] = df
    return out


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    H, L, C = df["High"], df["Low"], df["Close"]
    tr = pd.concat([H - L, (H - C.shift(1)).abs(), (L - C.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def wilson(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0: return 0.0, 1.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - m), min(1.0, c + m)


# ===========================================================================
# BIBLIOTECA DE SINAIS
# Cada funcao devolve (long, short): duas Series booleanas alinhadas a vela i.
# ===========================================================================

def s_reversao_sequencia(df, n=3):
    """Apos n velas seguidas na mesma direcao, aposta na REVERSAO."""
    C, O = df["Close"], df["Open"]
    alta = C > O
    baixa = C < O
    seq_alta = alta.rolling(n).sum() == n
    seq_baixa = baixa.rolling(n).sum() == n
    return seq_baixa, seq_alta   # apos queda -> long; apos alta -> short


def s_continuacao_sequencia(df, n=3):
    """Apos n velas seguidas, aposta na CONTINUACAO."""
    lo, sh = s_reversao_sequencia(df, n)
    return sh, lo


def s_vela_extrema(df, mult=2.0):
    """Vela com amplitude > mult*ATR: aposta reversao da direcao dela."""
    a = atr(df).shift(1)
    amp = df["High"] - df["Low"]
    grande = amp > mult * a
    subiu = df["Close"] > df["Open"]
    return grande & ~subiu, grande & subiu


def s_pin_bar(df, razao=2.0, jan_sr=60, tol_atr=0.5):
    """Pin bar (mecha >= razao*corpo) tocando extremo das jan_sr velas."""
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    a = atr(df)
    corpo = (C - O).abs().replace(0, np.nan)
    mi = pd.concat([O, C], axis=1).min(axis=1) - L
    ms = H - pd.concat([O, C], axis=1).max(axis=1)
    bull = (mi >= razao * corpo) & (mi > ms)
    bear = (ms >= razao * corpo) & (ms > mi)
    sr_lo = L.shift(1).rolling(jan_sr).min()
    sr_hi = H.shift(1).rolling(jan_sr).max()
    tol = tol_atr * a
    return (bull & (L <= sr_lo + tol)).fillna(False), (bear & (H >= sr_hi - tol)).fillna(False)


def s_pin_bar_puro(df, razao=2.0):
    """Pin bar sem exigir nivel — isola se o nivel adiciona algo."""
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    corpo = (C - O).abs().replace(0, np.nan)
    mi = pd.concat([O, C], axis=1).min(axis=1) - L
    ms = H - pd.concat([O, C], axis=1).max(axis=1)
    return ((mi >= razao * corpo) & (mi > ms)).fillna(False), \
           ((ms >= razao * corpo) & (ms > mi)).fillna(False)


def s_engolfante(df, n_seq=2):
    """Engolfante apos sequencia contraria."""
    O, C = df["Open"], df["Close"]
    alta, baixa = C > O, C < O
    seq_b = baixa.shift(1).rolling(n_seq).sum() == n_seq
    seq_a = alta.shift(1).rolling(n_seq).sum() == n_seq
    eng_bull = alta & (C >= O.shift(1)) & (O <= C.shift(1))
    eng_bear = baixa & (C <= O.shift(1)) & (O >= C.shift(1))
    return (seq_b & eng_bull).fillna(False), (seq_a & eng_bear).fillna(False)


def s_inside_bar(df):
    """Inside bar com filtro de tendencia EMA21/55."""
    H, L, C = df["High"], df["Low"], df["Close"]
    ins = (H < H.shift(1)) & (L > L.shift(1))
    e21 = C.ewm(span=21, adjust=False).mean().shift(1)
    e55 = C.ewm(span=55, adjust=False).mean().shift(1)
    return (ins & (e21 > e55)).fillna(False), (ins & (e21 < e55)).fillna(False)


def s_falso_rompimento(df, jan=10):
    """Acumulacao + rompimento -> aposta que o rompimento e falso.
    Limiar de acumulacao SEM lookahead (expanding quantile)."""
    H, L, C = df["High"], df["Low"], df["Close"]
    amp = H - L
    med = amp.shift(1).rolling(jan).mean()
    limiar = med.expanding(min_periods=500).quantile(0.25).shift(1)
    rhi = H.shift(1).rolling(jan).max()
    rlo = L.shift(1).rolling(jan).min()
    apert = med <= limiar
    return (apert & (C < rlo)).fillna(False), (apert & (C > rhi)).fillna(False)


def s_rompimento_real(df, jan=10):
    """O inverso: aposta que o rompimento CONTINUA."""
    lo, sh = s_falso_rompimento(df, jan)
    return sh, lo


def s_reversao_media(df, span=20, k=2.0):
    """Preco a mais de k desvios da EMA -> aposta na volta."""
    C = df["Close"]
    e = C.ewm(span=span, adjust=False).mean().shift(1)
    sd = C.rolling(span).std().shift(1)
    z = (C - e) / sd.replace(0, np.nan)
    return (z < -k).fillna(False), (z > k).fillna(False)


def s_momentum_ema(df, r=9, l=21):
    """Cruzamento de EMA — segue a tendencia."""
    C = df["Close"]
    er = C.ewm(span=r, adjust=False).mean().shift(1)
    el = C.ewm(span=l, adjust=False).mean().shift(1)
    cruz_up = (er > el) & (er.shift(1) <= el.shift(1))
    cruz_dn = (er < el) & (er.shift(1) >= el.shift(1))
    return cruz_up.fillna(False), cruz_dn.fillna(False)


def s_expansao_range(df, jan=10):
    """Apos compressao, segue a direcao da vela de expansao."""
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    amp = H - L
    med = amp.shift(1).rolling(jan).mean()
    limiar = med.expanding(min_periods=500).quantile(0.25).shift(1)
    apert = med <= limiar
    expandiu = amp > 1.5 * med
    return (apert & expandiu & (C > O)).fillna(False), \
           (apert & expandiu & (C < O)).fillna(False)


def s_hora_sessao(df, horas_long=(7, 8), horas_short=(20, 21)):
    """Vies puro de hora do dia — controle para efeito de sessao."""
    if not hasattr(df.index, "hour"):
        f = pd.Series(False, index=df.index)
        return f, f
    h = pd.Series(df.index.hour, index=df.index)
    return h.isin(horas_long), h.isin(horas_short)


def s_aleatorio(df, seed=42, taxa=0.02):
    """BASELINE: sinais aleatorios. Qualquer coisa que nao bata isso e ruido."""
    rng = np.random.default_rng(seed)
    r = rng.random(len(df))
    return pd.Series(r < taxa, index=df.index), pd.Series(r > 1 - taxa, index=df.index)


SINAIS: dict[str, Callable] = {
    "aleatorio (BASELINE)":        s_aleatorio,
    "reversao apos 3 velas":       lambda d: s_reversao_sequencia(d, 3),
    "reversao apos 4 velas":       lambda d: s_reversao_sequencia(d, 4),
    "reversao apos 5 velas":       lambda d: s_reversao_sequencia(d, 5),
    "continuacao apos 3 velas":    lambda d: s_continuacao_sequencia(d, 3),
    "vela extrema (rev)":          lambda d: s_vela_extrema(d, 2.0),
    "pin bar puro":                s_pin_bar_puro,
    "pin bar em S/R":              s_pin_bar,
    "engolfante apos 2":           lambda d: s_engolfante(d, 2),
    "inside bar + EMA":            s_inside_bar,
    "falso rompimento":            s_falso_rompimento,
    "rompimento real":             s_rompimento_real,
    "reversao a media (z>2)":      lambda d: s_reversao_media(d, 20, 2.0),
    "momentum EMA 9/21":           s_momentum_ema,
    "expansao apos compressao":    s_expansao_range,
    "vies de hora":                s_hora_sessao,
}


# ===========================================================================
# AVALIADOR BINARIO
# ===========================================================================

def avaliar_binario(frames: dict[str, pd.DataFrame], sinal_fn: Callable) -> pd.DataFrame:
    """Direcao da vela i+1 (Open->Close). Empates EXCLUIDOS (B6)."""
    linhas = []
    for ativo, df in frames.items():
        try:
            lo, sh = sinal_fn(df)
        except Exception:
            continue
        lo = lo.fillna(False).to_numpy()
        sh = sh.fillna(False).to_numpy()
        O = df["Open"].to_numpy()
        C = df["Close"].to_numpy()
        idx = df.index

        prox_o = np.roll(O, -1)
        prox_c = np.roll(C, -1)
        sobe = prox_c > prox_o
        cai = prox_c < prox_o
        valido = prox_c != prox_o          # B6: empate fora
        valido[-1] = False                 # ultima vela nao tem proxima

        for mask, direcao, acerto in ((lo, "long", sobe), (sh, "short", cai)):
            sel = mask & valido
            if not sel.any(): continue
            linhas.append(pd.DataFrame({
                "ativo": ativo,
                "quando": idx[sel],
                "direcao": direcao,
                "acerto": acerto[sel],
            }))
    return pd.concat(linhas, ignore_index=True) if linhas else pd.DataFrame()


# ===========================================================================
# AVALIADOR FOREX
# ===========================================================================

def mfe_mae(frames: dict[str, pd.DataFrame], sinal_fn: Callable,
            janela: int = 40, usar_spread: bool = False) -> pd.DataFrame:
    """Mede quanto o preco anda a FAVOR (MFE) vs CONTRA (MAE), em ATR.

    usar_spread=False (padrao): medida DIRECIONAL PURA. O spread deslocaria
    a entrada e enviesaria a razao para baixo em ~2*spread/ATR igualmente para
    QUALQUER sinal, inclusive entradas aleatorias — o que faz o baseline cair
    para ~0.82 no M15 e invalida qualquer limiar absoluto. O custo do spread
    entra na fase de geometria (simular_forex), que e onde ele pertence.

    Triagem: comparar sempre contra o baseline aleatorio do MESMO timeframe."""
    linhas = []
    for ativo, df in frames.items():
        try:
            lo, sh = sinal_fn(df)
        except Exception:
            continue
        a = atr(df).to_numpy()
        H, L, O = df["High"].to_numpy(), df["Low"].to_numpy(), df["Open"].to_numpy()
        sp = spread(ativo) if usar_spread else 0.0
        n = len(df)
        for mask, lado in ((lo.fillna(False).to_numpy(), "long"),
                           (sh.fillna(False).to_numpy(), "short")):
            for i in np.flatnonzero(mask):
                if i + 1 >= n - janela: continue
                av = a[i]
                if not np.isfinite(av) or av <= 0: continue
                hi = H[i+1:i+1+janela].max()
                lw = L[i+1:i+1+janela].min()
                if lado == "long":
                    ent = O[i+1] + sp
                    mfe, mae = (hi - ent) / av, (ent - lw) / av
                else:
                    ent = O[i+1] - sp
                    mfe, mae = (ent - lw) / av, (hi - ent) / av
                linhas.append({"ativo": ativo, "quando": df.index[i+1],
                               "lado": lado, "mfe": mfe, "mae": mae})
    return pd.DataFrame(linhas)


def simular_forex(frames: dict[str, pd.DataFrame], sinal_fn: Callable,
                  sl_atr: float, tp_atr: float, max_velas: int = 60) -> pd.DataFrame:
    """SL/TP em multiplos de ATR. SL checado ANTES do TP (B3).
    Trades nao-sobrepostos (B4): pula para a vela de saida."""
    linhas = []
    for ativo, df in frames.items():
        try:
            lo, sh = sinal_fn(df)
        except Exception:
            continue
        lo = lo.fillna(False).to_numpy()
        sh = sh.fillna(False).to_numpy()
        a = atr(df).to_numpy()
        H, L, O = df["High"].to_numpy(), df["Low"].to_numpy(), df["Open"].to_numpy()
        sp = spread(ativo)
        n = len(df)

        i = 20
        while i < n - 1:
            lado = "long" if lo[i] else ("short" if sh[i] else None)
            if lado is None:
                i += 1; continue
            av = a[i]
            if not np.isfinite(av) or av <= 0:
                i += 1; continue

            e = i + 1
            if lado == "long":
                ent = O[e] + sp
                slp, tpp = ent - sl_atr * av, ent + tp_atr * av
            else:
                ent = O[e] - sp
                slp, tpp = ent + sl_atr * av, ent - tp_atr * av
            risco = abs(ent - slp)
            reward = abs(tpp - ent)
            if risco <= 0 or reward <= 0:
                i += 1; continue
            rr = reward / risco

            res, j = None, e
            for j in range(e, min(n, e + max_velas)):
                hj, lj = H[j] - sp, L[j] + sp
                if lado == "long":
                    bate_sl, bate_tp = lj <= slp, hj >= tpp
                else:
                    bate_sl, bate_tp = hj >= slp, lj <= tpp
                # B3: SL primeiro — conservador quando os dois batem na vela
                if bate_sl: res = "perda"; break
                if bate_tp: res = "ganho"; break
            if res:
                linhas.append({"ativo": ativo, "quando": df.index[e], "lado": lado,
                               "rr": rr, "resultado": res})
            i = j + 1      # B4: nao sobrepoe
    return pd.DataFrame(linhas)


# ===========================================================================
# RELATORIOS
# ===========================================================================

def stats_binario(d: pd.DataFrame, be: float = 0.5405) -> dict:
    if d.empty: return {}
    n = len(d); w = int(d["acerto"].sum()); wr = w / n
    lo, hi = wilson(w, n)
    n_ef = d["quando"].nunique()                       # B7
    lo_e, hi_e = wilson(int(round(wr * n_ef)), n_ef)
    return {"n": n, "n_ef": n_ef, "w": w, "wr": wr,
            "lo": lo, "hi": hi, "lo_ef": lo_e, "hi_ef": hi_e,
            "edge": lo_e > be}


def stats_forex(d: pd.DataFrame) -> dict:
    if d.empty: return {}
    n = len(d)
    g = d[d["resultado"] == "ganho"]
    w = len(g)
    total_r = float(g["rr"].sum()) - (n - w)           # B1: R realizado
    ev = total_r / n
    wr = w / n
    rr_g = float(g["rr"].mean()) if w else 0.0
    lo, hi = wilson(w, n)
    n_ef = d["quando"].nunique()
    lo_e, hi_e = wilson(int(round(wr * n_ef)), n_ef)
    be = 1 / (1 + rr_g) if rr_g > 0 else 1.0
    return {"n": n, "n_ef": n_ef, "wr": wr, "rr_ganhos": rr_g,
            "ev": ev, "total_r": total_r, "lo_ef": lo_e, "hi_ef": hi_e,
            "be": be, "edge": ev > 0 and lo_e > be}


def split_temporal(d: pd.DataFrame, frac: float = 0.5) -> tuple[pd.DataFrame, pd.DataFrame]:
    """B5: split por TEMPO, nunca por par."""
    if d.empty or "quando" not in d: return d, d
    ds = d.sort_values("quando")
    c = int(len(ds) * frac)
    return ds.iloc[:c], ds.iloc[c:]
