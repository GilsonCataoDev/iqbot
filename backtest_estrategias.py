"""Backtest de 5 estrategias de price action — modo binario e forex.

Estrategias:
  1. Pin Bar em S/R (rejeicao de mecha em nivel)
  2. Inside Bar Breakout com filtro de tendencia (EMA21/55)
  3. Correcao Fibonacci com confirmacao de vela
  4. Engulfing em extremo (reversao apos sequencia direcional)
  5. Gap de sessao Londres (fade do movimento asiatico)

Para cada estrategia imprime:
  - WR e IC95% Wilson no modo binario (breakeven 54.05%)
  - WR, R:R medio e EV no modo forex com SL/TP adaptados
  - Classificacao: FOREX / BINARIA / NENHUMA / AMBAS

Uso:
    python backtest_estrategias.py
    python backtest_estrategias.py --ativos EURUSD GBPUSD
    python backtest_estrategias.py --estrategia 1 3   (so estrategias 1 e 3)
"""
from __future__ import annotations

import argparse
import math
import sys
from typing import Callable

import numpy as np
import pandas as pd

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15

ATIVOS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD"]
PAYOUT = {"USDCAD": 0.87, "NZDUSD": 0.87}
PAYOUT_PAD = 0.85
SPREAD = {"EURJPY": 0.015, "USDJPY": 0.015}
SPREAD_PAD = 0.00012

BE_BIN = 1 / (1 + PAYOUT_PAD)   # 54.05%


# ---------------------------------------------------------------------------
# Utilitarios
# ---------------------------------------------------------------------------

def _spread(ativo: str) -> float:
    return SPREAD.get(ativo, SPREAD_PAD)


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    H, L, C = df["High"], df["Low"], df["Close"]
    tr = pd.concat([H - L, (H - C.shift(1)).abs(), (L - C.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def wilson(w: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 1.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - m), min(1.0, c + m)


def linha_bin(rot: str, w: int, n: int, be: float, indent: int = 2) -> str:
    if n < 30:
        return " " * indent + f"{rot:55} n={n:<5} amostra pequena"
    wr = w / n
    lo, hi = wilson(w, n)
    tag = "APROVADO" if lo > be else ("reprovado" if hi < be else "inconclusivo")
    return " " * indent + f"{rot:55} n={n:<5} WR={wr:5.1%} IC=[{lo:.1%},{hi:.1%}] {tag}"


def linha_forex(rot: str, w: int, n: int, rr_med: float, rr_min: float, indent: int = 2) -> str:
    if n < 20:
        return " " * indent + f"{rot:55} n={n:<5} amostra pequena"
    wr = w / n
    be = 1 / (1 + rr_med)
    lo, hi = wilson(w, n)
    ev = wr * rr_med - (1 - wr)
    tag = "APROVADO" if lo > be else ("reprovado" if hi < be else "inconclusivo")
    return " " * indent + f"{rot:55} n={n:<5} WR={wr:5.1%} R:R={rr_med:.2f} EV={ev:+.3f}R {tag}"


def simular_forex(df: pd.DataFrame, ativo: str, sinais_idx: list[int],
                  sl_arr: pd.Series, tp_arr: pd.Series, lado: list[str]) -> list[dict]:
    """Walk-forward: percorre velas ate TP ou SL ser atingido."""
    sp = _spread(ativo)
    H, L = df["High"], df["Low"]
    ops = []
    n = len(df)
    for i, sl_raw, tp_raw, lad in zip(sinais_idx, sl_arr, tp_arr, lado):
        entry_idx = i + 1
        if entry_idx >= n - 1:
            continue
        if lad == "buy":
            entrada = float(df["Open"].iloc[entry_idx]) + sp
            sl = float(sl_raw) - sp
            tp = float(tp_raw) + sp
            risco = entrada - sl
            reward = tp - entrada
        else:
            entrada = float(df["Open"].iloc[entry_idx]) - sp
            sl = float(sl_raw) + sp
            tp = float(tp_raw) - sp
            risco = sl - entrada
            reward = entrada - tp
        if risco <= 0 or reward <= 0:
            continue
        rr = reward / risco
        resultado = None
        for j in range(entry_idx, min(n, entry_idx + 60)):
            if lad == "buy":
                if float(H.iloc[j]) - sp >= tp:
                    resultado = "ganho"; break
                if float(L.iloc[j]) + sp <= sl:
                    resultado = "perda"; break
            else:
                if float(L.iloc[j]) + sp <= tp:
                    resultado = "ganho"; break
                if float(H.iloc[j]) - sp >= sl:
                    resultado = "perda"; break
        if resultado:
            ops.append({"resultado": resultado, "rr": rr, "n_velas": j - entry_idx + 1})
    return ops


# ---------------------------------------------------------------------------
# 1. Pin Bar em S/R
# ---------------------------------------------------------------------------

def estrategia_pin_bar(df: pd.DataFrame, ativo: str, jan_sr: int = 60, tol_sr: float = 0.5):
    """
    Bullish pin: mecha inferior >= 2x corpo, mecha inf > mecha sup,
    Low toca dentro de tol*ATR de um minimo das ultimas jan_sr velas.
    Bearish pin: inverso.
    """
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    atr = _atr(df)
    corpo = (C - O).abs()
    min_oc = pd.concat([O, C], axis=1).min(axis=1)
    max_oc = pd.concat([O, C], axis=1).max(axis=1)
    mecha_inf = min_oc - L
    mecha_sup = H - max_oc

    corpo_pos = corpo.replace(0, np.nan)
    bullish = (mecha_inf >= 2 * corpo_pos) & (mecha_inf > mecha_sup)
    bearish = (mecha_sup >= 2 * corpo_pos) & (mecha_sup > mecha_inf)

    sr_lo = L.shift(1).rolling(jan_sr).min()
    sr_hi = H.shift(1).rolling(jan_sr).max()
    tol = tol_sr * atr

    toca_sup = bullish & (L <= sr_lo + tol) & (L >= sr_lo - tol)
    toca_res = bearish & (H >= sr_hi - tol) & (H <= sr_hi + tol)

    sinal = toca_sup | toca_res
    idx = sinal[sinal].index.tolist()
    pos = [df.index.get_loc(i) for i in idx]

    # Binario
    prox_o = O.shift(-1)
    prox_c = C.shift(-1)
    acerta_bin = []
    for i in pos:
        if i + 1 >= len(df): continue
        lad = "buy" if bool(toca_sup.iloc[i]) else "sell"
        sobe = float(prox_c.iloc[i]) > float(prox_o.iloc[i])
        cai = float(prox_c.iloc[i]) < float(prox_o.iloc[i])
        acerta_bin.append((lad == "buy" and sobe) or (lad == "sell" and cai))

    # Forex
    sl_s = pd.Series([float(L.iloc[i]) - float(atr.iloc[i]) * 0.5 if bool(toca_sup.iloc[i])
                      else float(H.iloc[i]) + float(atr.iloc[i]) * 0.5 for i in pos], dtype=float)
    tp_s = pd.Series([float(sr_hi.iloc[i]) if bool(toca_sup.iloc[i])
                      else float(sr_lo.iloc[i]) for i in pos], dtype=float)
    lados = ["buy" if bool(toca_sup.iloc[i]) else "sell" for i in pos]
    ops_fx = simular_forex(df, ativo, pos, sl_s, tp_s, lados)

    return acerta_bin, ops_fx


# ---------------------------------------------------------------------------
# 2. Inside Bar + EMA (tendencia)
# ---------------------------------------------------------------------------

def estrategia_inside_bar(df: pd.DataFrame, ativo: str):
    """
    Inside bar: High[i] < High[i-1] AND Low[i] > Low[i-1].
    Filtro EMA21 > EMA55 -> BUY; < -> SELL.
    SL: extremo oposto da inside bar. TP: 2.0 * risco.
    """
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    atr = _atr(df)
    ema21 = C.ewm(span=21, adjust=False).mean().shift(1)
    ema55 = C.ewm(span=55, adjust=False).mean().shift(1)

    inside = (H < H.shift(1)) & (L > L.shift(1))
    alta = ema21 > ema55
    baixa = ema21 < ema55

    buy_sig = inside & alta
    sell_sig = inside & baixa
    sinal = buy_sig | sell_sig

    pos = [i for i in range(len(df)) if bool(sinal.iloc[i])]

    prox_o = O.shift(-1)
    prox_c = C.shift(-1)
    acerta_bin = []
    for i in pos:
        if i + 1 >= len(df): continue
        lad = "buy" if bool(buy_sig.iloc[i]) else "sell"
        sobe = float(prox_c.iloc[i]) > float(prox_o.iloc[i])
        cai = float(prox_c.iloc[i]) < float(prox_o.iloc[i])
        acerta_bin.append((lad == "buy" and sobe) or (lad == "sell" and cai))

    sl_s = pd.Series([float(L.iloc[i]) - float(atr.iloc[i]) * 0.3 if bool(buy_sig.iloc[i])
                      else float(H.iloc[i]) + float(atr.iloc[i]) * 0.3 for i in pos], dtype=float)
    # TP = 2x risco
    rr_alvo = 2.0
    tp_s = []
    sp = _spread(ativo)
    for i, sl_v in zip(pos, sl_s):
        if i + 1 >= len(df):
            tp_s.append(float(sl_v)); continue
        lad = "buy" if bool(buy_sig.iloc[i]) else "sell"
        entrada = float(O.iloc[i + 1])
        risco = abs(entrada - float(sl_v))
        tp_s.append(entrada + risco * rr_alvo if lad == "buy" else entrada - risco * rr_alvo)
    tp_s = pd.Series(tp_s, dtype=float)
    lados = ["buy" if bool(buy_sig.iloc[i]) else "sell" for i in pos]
    ops_fx = simular_forex(df, ativo, pos, sl_s, tp_s, lados)

    return acerta_bin, ops_fx


# ---------------------------------------------------------------------------
# 3. Correcao Fibonacci + confirmacao de vela
# ---------------------------------------------------------------------------

def estrategia_fibo_confirmado(df: pd.DataFrame, ativo: str,
                                impulso_min_atr: float = 1.5,
                                fib_lo: float = 0.382, fib_hi: float = 0.618,
                                n_impulso: int = 5):
    """
    Detecta impulso de alta (n_impulso velas, amplitude > impulso_min_atr * ATR).
    Aguarda retração 38-61% do impulso.
    Confirmação: pin bar bullish OU engolfante bullish na zona.
    SL: abaixo do swing low. TP: topo do impulso.
    """
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    atr = _atr(df)

    pos_buy, pos_sell = [], []
    swing_lo_buy, swing_hi_buy = [], []
    swing_lo_sell, swing_hi_sell = [], []

    for i in range(n_impulso + 2, len(df) - 1):
        atr_i = float(atr.iloc[i])
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue

        # Impulso de alta: Close[i-n] ate Close[i-1] subiu > impulso_min_atr * ATR
        seg = df.iloc[i - n_impulso: i]
        impulso_amp = float(seg["High"].max()) - float(seg["Low"].min())
        swing_hi_v = float(seg["High"].max())
        swing_lo_v = float(seg["Low"].min())

        if impulso_amp < impulso_min_atr * atr_i:
            continue

        preco_atual = float(C.iloc[i])
        ret = (swing_hi_v - preco_atual) / impulso_amp if impulso_amp > 0 else 0

        # Retração de alta (preco caiu apos impulso de alta)
        impulso_alta = float(C.iloc[i - n_impulso]) < float(C.iloc[i - 1])
        if impulso_alta and (fib_lo <= ret <= fib_hi):
            # Confirmacao: pin bar bullish (mecha inf >= 2x corpo)
            corpo = abs(float(C.iloc[i]) - float(O.iloc[i]))
            mecha_inf = min(float(O.iloc[i]), float(C.iloc[i])) - float(L.iloc[i])
            pin_bull = mecha_inf >= 2 * (corpo + 1e-10) and float(C.iloc[i]) > float(O.iloc[i])
            # Ou engolfante bullish
            engolf_bull = (float(C.iloc[i]) > float(O.iloc[i]) and
                           float(O.iloc[i]) <= float(C.iloc[i - 1]) and
                           float(C.iloc[i]) >= float(O.iloc[i - 1]))
            if pin_bull or engolf_bull:
                pos_buy.append(i)
                swing_lo_buy.append(swing_lo_v)
                swing_hi_buy.append(swing_hi_v)

        # Retração de baixa
        impulso_baixa = float(C.iloc[i - n_impulso]) > float(C.iloc[i - 1])
        ret_baixa = (preco_atual - swing_lo_v) / impulso_amp if impulso_amp > 0 else 0
        if impulso_baixa and (fib_lo <= ret_baixa <= fib_hi):
            corpo = abs(float(C.iloc[i]) - float(O.iloc[i]))
            mecha_sup = float(H.iloc[i]) - max(float(O.iloc[i]), float(C.iloc[i]))
            pin_bear = mecha_sup >= 2 * (corpo + 1e-10) and float(C.iloc[i]) < float(O.iloc[i])
            engolf_bear = (float(C.iloc[i]) < float(O.iloc[i]) and
                           float(O.iloc[i]) >= float(C.iloc[i - 1]) and
                           float(C.iloc[i]) <= float(O.iloc[i - 1]))
            if pin_bear or engolf_bear:
                pos_sell.append(i)
                swing_lo_sell.append(swing_lo_v)
                swing_hi_sell.append(swing_hi_v)

    # Binario
    acerta_bin = []
    prox_o, prox_c = O.shift(-1), C.shift(-1)
    for i in pos_buy:
        if i + 1 >= len(df): continue
        acerta_bin.append(float(prox_c.iloc[i]) > float(prox_o.iloc[i]))
    for i in pos_sell:
        if i + 1 >= len(df): continue
        acerta_bin.append(float(prox_c.iloc[i]) < float(prox_o.iloc[i]))

    # Forex buy
    ops_fx = []
    if pos_buy:
        sl_b = pd.Series([lo - float(atr.iloc[i]) * 0.5 for i, lo in zip(pos_buy, swing_lo_buy)], dtype=float)
        tp_b = pd.Series(swing_hi_buy, dtype=float)
        ops_fx += simular_forex(df, ativo, pos_buy, sl_b, tp_b, ["buy"] * len(pos_buy))
    if pos_sell:
        sl_s = pd.Series([hi + float(atr.iloc[i]) * 0.5 for i, hi in zip(pos_sell, swing_hi_sell)], dtype=float)
        tp_s = pd.Series(swing_lo_sell, dtype=float)
        ops_fx += simular_forex(df, ativo, pos_sell, sl_s, tp_s, ["sell"] * len(pos_sell))

    return acerta_bin, ops_fx


# ---------------------------------------------------------------------------
# 4. Engulfing em extremo
# ---------------------------------------------------------------------------

def estrategia_engulfing(df: pd.DataFrame, ativo: str, n_seq: int = 3):
    """
    Bullish engulfing apos n_seq velas de queda:
      Close engolfa Open anterior, sequencia bearish antes.
    SL: minima da vela engolfante - 0.3 ATR. TP: 2x risco.
    """
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    atr = _atr(df)

    pos_buy, pos_sell = [], []
    for i in range(n_seq + 1, len(df) - 1):
        # Sequencia de baixa
        seq_baixa = all(float(C.iloc[i - k]) < float(O.iloc[i - k]) for k in range(1, n_seq + 1))
        eng_bull = (float(C.iloc[i]) > float(O.iloc[i]) and
                    float(C.iloc[i]) >= float(O.iloc[i - 1]) and
                    float(O.iloc[i]) <= float(C.iloc[i - 1]))
        if seq_baixa and eng_bull:
            pos_buy.append(i)

        seq_alta = all(float(C.iloc[i - k]) > float(O.iloc[i - k]) for k in range(1, n_seq + 1))
        eng_bear = (float(C.iloc[i]) < float(O.iloc[i]) and
                    float(C.iloc[i]) <= float(O.iloc[i - 1]) and
                    float(O.iloc[i]) >= float(C.iloc[i - 1]))
        if seq_alta and eng_bear:
            pos_sell.append(i)

    prox_o, prox_c = O.shift(-1), C.shift(-1)
    acerta_bin = []
    for i in pos_buy:
        if i + 1 >= len(df): continue
        acerta_bin.append(float(prox_c.iloc[i]) > float(prox_o.iloc[i]))
    for i in pos_sell:
        if i + 1 >= len(df): continue
        acerta_bin.append(float(prox_c.iloc[i]) < float(prox_o.iloc[i]))

    ops_fx = []
    sp = _spread(ativo)
    rr_alvo = 2.0
    for i in pos_buy:
        if i + 1 >= len(df): continue
        sl_v = float(L.iloc[i]) - float(atr.iloc[i]) * 0.3
        entrada = float(O.iloc[i + 1]) + sp
        risco = entrada - sl_v
        tp_v = entrada + risco * rr_alvo
        ops_fx += simular_forex(df, ativo, [i], pd.Series([sl_v]), pd.Series([tp_v]), ["buy"])
    for i in pos_sell:
        if i + 1 >= len(df): continue
        sl_v = float(H.iloc[i]) + float(atr.iloc[i]) * 0.3
        entrada = float(O.iloc[i + 1]) - sp
        risco = sl_v - entrada
        tp_v = entrada - risco * rr_alvo
        ops_fx += simular_forex(df, ativo, [i], pd.Series([sl_v]), pd.Series([tp_v]), ["sell"])

    return acerta_bin, ops_fx


# ---------------------------------------------------------------------------
# 5. Gap de sessao Londres
# ---------------------------------------------------------------------------

def estrategia_gap_londres(df: pd.DataFrame, ativo: str):
    """
    Range asiatico: candles das 00h-07h UTC.
    Se a 08h UTC abre fora do range e fecha dentro em ate 2 velas -> fade.
    Binario: prox vela retorna pro range.
    Forex: SL alem do extremo do gap, TP = centro do range asiatico.
    """
    if not hasattr(df.index, "hour"):
        return [], []

    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    atr = _atr(df)

    pos_buy, pos_sell = [], []
    sl_buy, sl_sell = [], []
    tp_buy, tp_sell = [], []

    datas = df.index.normalize().unique()
    for data in datas:
        asiatico = df[(df.index.normalize() == data) &
                      (df.index.hour >= 0) & (df.index.hour < 7)]
        if len(asiatico) < 3:
            continue
        rhi = float(asiatico["High"].max())
        rlo = float(asiatico["Low"].min())
        centro = (rhi + rlo) / 2

        abertura_lon = df[(df.index.normalize() == data) & (df.index.hour == 7)]
        if len(abertura_lon) == 0:
            abertura_lon = df[(df.index.normalize() == data) & (df.index.hour == 8)]
        if len(abertura_lon) == 0:
            continue

        for ts, row in abertura_lon.iterrows():
            i = df.index.get_loc(ts)
            op = float(row["Open"])
            cl = float(row["Close"])
            atr_i = float(atr.iloc[i]) if i < len(atr) else 0
            if not np.isfinite(atr_i) or atr_i <= 0:
                continue
            # Gap para cima (abriu acima do range asiatico)
            if op > rhi and cl < rhi:  # ja voltou nessa vela
                pos_sell.append(i)
                sl_sell.append(op + atr_i * 0.5)
                tp_sell.append(centro)
            elif op > rhi and cl > rhi:  # ainda fora
                pos_sell.append(i)
                sl_sell.append(op + atr_i * 0.5)
                tp_sell.append(centro)
            # Gap para baixo
            elif op < rlo and cl > rlo:
                pos_buy.append(i)
                sl_buy.append(op - atr_i * 0.5)
                tp_buy.append(centro)
            elif op < rlo and cl < rlo:
                pos_buy.append(i)
                sl_buy.append(op - atr_i * 0.5)
                tp_buy.append(centro)

    prox_o, prox_c = O.shift(-1), C.shift(-1)
    acerta_bin = []
    for i in pos_buy:
        if i + 1 >= len(df): continue
        acerta_bin.append(float(prox_c.iloc[i]) > float(prox_o.iloc[i]))
    for i in pos_sell:
        if i + 1 >= len(df): continue
        acerta_bin.append(float(prox_c.iloc[i]) < float(prox_o.iloc[i]))

    ops_fx = []
    if pos_buy:
        ops_fx += simular_forex(df, ativo, pos_buy,
                                pd.Series(sl_buy, dtype=float),
                                pd.Series(tp_buy, dtype=float),
                                ["buy"] * len(pos_buy))
    if pos_sell:
        ops_fx += simular_forex(df, ativo, pos_sell,
                                pd.Series(sl_sell, dtype=float),
                                pd.Series(tp_sell, dtype=float),
                                ["sell"] * len(pos_sell))

    return acerta_bin, ops_fx


# ---------------------------------------------------------------------------
# Runner e relatorio
# ---------------------------------------------------------------------------

ESTRATEGIAS = {
    1: ("Pin Bar em S/R",               estrategia_pin_bar),
    2: ("Inside Bar + EMA",             estrategia_inside_bar),
    3: ("Fibonacci + confirmacao vela", estrategia_fibo_confirmado),
    4: ("Engulfing em extremo",         estrategia_engulfing),
    5: ("Gap sessao Londres (fade)",    estrategia_gap_londres),
}


def rodar_estrategia(num: int, nome: str, fn: Callable,
                     frames: dict[str, pd.DataFrame]) -> None:
    print(f"\n{'='*84}")
    print(f"Estrategia {num}: {nome}")
    print(f"{'='*84}")

    todas_bin: list[bool] = []
    todas_fx: list[dict] = []

    for ativo, df in frames.items():
        try:
            bin_res, fx_res = fn(df, ativo)
            todas_bin.extend(bin_res)
            todas_fx.extend(fx_res)
        except Exception as e:
            print(f"  {ativo}: ERRO — {e!r}")

    # Binario
    n_bin = len(todas_bin)
    w_bin = sum(todas_bin)
    be_bin = BE_BIN
    print(f"\n  [BINARIA] {linha_bin('TOTAL', w_bin, n_bin, be_bin, indent=0)}")

    # Forex
    n_fx = len(todas_fx)
    if n_fx > 0:
        w_fx = sum(1 for o in todas_fx if o["resultado"] == "ganho")
        rr_med = sum(o["rr"] for o in todas_fx) / n_fx
        be_fx = 1 / (1 + rr_med)
        ev = (w_fx / n_fx) * rr_med - (1 - w_fx / n_fx)
        print(f"  [FOREX]   {linha_forex('TOTAL', w_fx, n_fx, rr_med, 1.5, indent=0)}")
    else:
        print("  [FOREX]   sem operacoes")
        rr_med = 0; w_fx = 0; be_fx = 1; ev = -1

    # Classificacao
    lo_bin, hi_bin = wilson(w_bin, n_bin) if n_bin >= 30 else (0, 1)
    lo_fx, hi_fx = wilson(w_fx, n_fx) if n_fx >= 20 else (0, 1)
    edge_bin = n_bin >= 30 and lo_bin > be_bin
    edge_fx = n_fx >= 20 and lo_fx > be_fx and ev > 0

    if edge_bin and edge_fx:
        cls = "AMBAS (raro)"
    elif edge_bin:
        cls = "BINARIA"
    elif edge_fx:
        cls = "FOREX"
    else:
        cls = "sem edge detectado"

    print(f"\n  >> CLASSIFICACAO: {cls}")

    # Holdout split
    if n_bin >= 60:
        corte = n_bin // 2
        cal_b = sum(todas_bin[:corte]); hld_b = sum(todas_bin[corte:])
        print(f"  [BIN holdout] cal={cal_b/corte:.1%} hold={hld_b/(n_bin-corte):.1%}")
    if n_fx >= 40:
        corte_fx = n_fx // 2
        cal_fx = [o for o in todas_fx[:corte_fx] if o["resultado"] == "ganho"]
        hld_fx = [o for o in todas_fx[corte_fx:] if o["resultado"] == "ganho"]
        ev_c = len(cal_fx) / corte_fx * rr_med - (1 - len(cal_fx) / corte_fx)
        ev_h = len(hld_fx) / (n_fx - corte_fx) * rr_med - (1 - len(hld_fx) / (n_fx - corte_fx))
        print(f"  [FX holdout]  cal EV={ev_c:+.3f}R  hold EV={ev_h:+.3f}R")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ativos", nargs="*", default=ATIVOS)
    ap.add_argument("--estrategia", nargs="*", type=int, default=list(ESTRATEGIAS.keys()))
    ap.add_argument("--tf", type=int, default=900)
    a = ap.parse_args()

    import dataclasses
    config = configuracao_scalping_m15()
    cfg = dataclasses.replace(config, timeframe_segundos=a.tf)
    tf_nome = {900: "M15", 300: "M5", 3600: "H1"}.get(a.tf, str(a.tf))
    print(f"Timeframe: {tf_nome} | Pares: {' '.join(a.ativos)}")

    frames: dict[str, pd.DataFrame] = {}
    for ativo in a.ativos:
        df = backtest.carregar_cache(cfg, ativo)
        if df is None:
            print(f"  {ativo}: sem cache")
            continue
        frames[ativo] = df
        print(f"  {ativo}: {len(df)} candles ({df.index[0]:%d/%m/%Y} a {df.index[-1]:%d/%m/%Y})")

    if not frames:
        print("Sem dados.")
        return 1

    for num in a.estrategia:
        if num not in ESTRATEGIAS:
            print(f"Estrategia {num} invalida.")
            continue
        nome, fn = ESTRATEGIAS[num]
        rodar_estrategia(num, nome, fn, frames)

    print(f"\n{'='*84}")
    print("FIM. Interpretacao:")
    print("  APROVADO bin  = WR IC95% inferior > 54.05% (payout 0.85)")
    print("  APROVADO forex = WR IC95% inferior > breakeven do R:R realizado e EV > 0")
    print(f"{'='*84}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
