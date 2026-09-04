"""Reavalia os setups REAIS do bot com a estatistica corrigida.

Nao reimplementa os setups — le o dump (.pkl) que `backtest_m15.py --dump`
produz, que ja contem as decisoes da classe `EstrategiaReversaoM5` de verdade.
O que muda e a AVALIACAO:

  1. Separa `rev_parcial=True` (candle parcial via M1 — honesto) de
     `rev_parcial=False` (candle completo — CIRCULAR, o setup ve o Close da
     vela em que vai entrar). Os dados M1 cobrem so ~16% do periodo M15, entao
     a maioria das reversoes cai no caso circular.
  2. IC Wilson com n EFETIVO (timestamps unicos) em vez de n bruto — 7 pares
     correlacionados nao sao 7 observacoes independentes.
  3. Holdout estritamente temporal.
  4. BASELINE ALEATORIO com a MESMA mecanica de entrada/saida. Este e o teste
     decisivo: se entradas aleatorias no mesmo mecanismo ja dao WR alto, o vies
     esta no mecanismo e nao no setup.

Uso:
    python backtest_m15.py --tf m15 --offline --m1-minutos 8 --fibo --dump d.pkl
    python reavaliar_setups_bot.py --dump d.pkl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import motor_sinais as M
from iqoption_m5.config import configuracao_scalping_m15

SETUPS_REVERSAO = {"sr_rejeicao", "pin_bar_sr", "fibo_sr_retracao",
                   "retracao_intracandle"}


def linha(rot: str, d: pd.DataFrame, indent: int = 4) -> dict | None:
    d = d[d["res"] != "empate"]
    n = len(d)
    if n < 30:
        print(" " * indent + f"{rot:38} n={n:<5} amostra pequena")
        return None
    w = int((d["res"] == "ganho").sum())
    wr = w / n
    nef = d["quando"].nunique()
    lo_n, hi_n = M.wilson(w, n)
    lo_e, hi_e = M.wilson(int(round(wr * nef)), nef)
    be = 0.5405
    tag = "EDGE" if lo_e > be else ("negativo" if hi_e < be else "inconclusivo")
    print(" " * indent + f"{rot:38} n={n:<5} n_ef={nef:<5} WR={wr:6.2%} "
          f"IC_ef=[{lo_e:.2%},{hi_e:.2%}]  {tag}")
    return {"n": n, "nef": nef, "wr": wr, "lo": lo_e, "hi": hi_e, "tag": tag}


def baseline_aleatorio(ativos: list[str], n_amostras: int = 4000,
                       m1_minutos: int = 8, seed: int = 11) -> None:
    """Mesma mecanica dos setups de reversao, mas entrada ALEATORIA.

    Entra no Close do candle parcial no minuto k (aleatorio em 1..m1_minutos),
    sai no Close do M15. Direcao aleatoria. Se isto der ~50%, a mecanica e
    neutra e o WR dos setups e do sinal. Se der bem acima, o vies e mecanico.
    """
    import dataclasses
    from iqoption_m5 import backtest as BT
    cfg15 = dataclasses.replace(configuracao_scalping_m15(), timeframe_segundos=900)
    cfg1 = dataclasses.replace(configuracao_scalping_m15(), timeframe_segundos=60)

    rng = np.random.default_rng(seed)
    res = []
    for a in ativos:
        m15 = BT.carregar_cache(cfg15, a)
        m1 = BT.carregar_cache(cfg1, a)
        if m15 is None or m1 is None:
            continue
        # so velas M15 cobertas pelo M1
        cob = m15[(m15.index >= m1.index[0]) & (m15.index <= m1.index[-1] - pd.Timedelta(minutes=15))]
        if len(cob) < 100:
            continue
        idx = rng.choice(len(cob), size=min(n_amostras // max(1, len(ativos)), len(cob)),
                         replace=False)
        for i in idx:
            ts = cob.index[i]
            k = int(rng.integers(1, m1_minutos + 1))
            fim = ts + pd.Timedelta(minutes=k)
            slc = m1[(m1.index >= ts) & (m1.index < fim)]
            if len(slc) < k:
                continue
            entrada = float(slc.iloc[-1]["Close"])
            saida = float(cob.iloc[i]["Close"])
            if saida == entrada:
                continue
            direcao = "call" if rng.random() < 0.5 else "put"
            ganhou = (saida > entrada) if direcao == "call" else (saida < entrada)
            res.append({"ativo": a, "quando": ts,
                        "res": "ganho" if ganhou else "perda"})
    d = pd.DataFrame(res)
    print(f"\n  BASELINE ALEATORIO — mesma mecanica (entra no parcial, sai no Close M15)")
    if d.empty:
        print("    sem amostra (falta M1)"); return
    linha("entrada e direcao aleatorias", d)
    print("    ^ se isto estiver perto de 50%, a mecanica e neutra.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--sem-baseline", action="store_true")
    a = ap.parse_args()

    p = Path(a.dump)
    if not p.exists():
        print(f"dump nao encontrado: {p}")
        return 1
    d = pd.read_pickle(p)
    if not isinstance(d, pd.DataFrame):
        d = pd.DataFrame(d)
    print("=" * 96)
    print("REAVALIACAO DOS SETUPS DO BOT — estatistica corrigida")
    print("=" * 96)
    print(f"  sinais no dump: {len(d)}  |  pares: {d['ativo'].nunique()}")
    print(f"  periodo: {d['quando'].min()} a {d['quando'].max()}")

    if "rev_parcial" in d.columns:
        val = int(d["rev_parcial"].sum())
        print(f"  reversoes com candle parcial M1 (honesto): {val}")
        print(f"  reversoes SEM parcial (circular/invalido) : {len(d)-val}")

    print(f"\n{'='*96}")
    print("POR SETUP — separando avaliacao honesta de circular")
    print(f"{'='*96}")

    for setup in sorted(d["setup"].unique()):
        s = d[d["setup"] == setup]
        eh_rev = setup in SETUPS_REVERSAO
        print(f"\n  {setup}  ({'reversao' if eh_rev else 'continuacao'})")
        if eh_rev and "rev_parcial" in s.columns:
            hon = s[s["rev_parcial"] == True]
            cir = s[s["rev_parcial"] != True]
            r_h = linha("HONESTO (candle parcial M1)", hon)
            r_c = linha("CIRCULAR (candle completo)", cir)
            if r_h and r_c:
                print(f"    {'':38} diferenca circular-honesto: "
                      f"{(r_c['wr']-r_h['wr'])*100:+.2f}pp")
        else:
            linha("todos", s)

        # holdout temporal
        s2 = s[s["res"] != "empate"].sort_values("quando")
        if len(s2) >= 120:
            c = len(s2) // 2
            linha("  calibracao (1a metade temporal)", s2.iloc[:c])
            linha("  holdout    (2a metade temporal)", s2.iloc[c:])

    # consistencia por par no setup principal
    print(f"\n{'='*96}")
    print("CONSISTENCIA POR PAR (apenas avaliacao honesta)")
    print(f"{'='*96}")
    for setup in sorted(d["setup"].unique()):
        s = d[(d["setup"] == setup) & (d["res"] != "empate")]
        if setup in SETUPS_REVERSAO and "rev_parcial" in s.columns:
            s = s[s["rev_parcial"] == True]
        if len(s) < 60:
            continue
        print(f"\n  {setup}:")
        for at in sorted(s["ativo"].unique()):
            linha(at, s[s["ativo"] == at], indent=6)

    if not a.sem_baseline:
        print(f"\n{'='*96}")
        print("TESTE DECISIVO — a mecanica de entrada e neutra?")
        print(f"{'='*96}")
        baseline_aleatorio(sorted(d["ativo"].unique().tolist()))

    return 0


if __name__ == "__main__":
    sys.exit(main())
