"""Le o CSV do forward test binario e decide se ja da pra concluir.

A pergunta: o WR real e ~56.03% (estimativa do backtest) ou ~54.16%
(limite pessimista do IC)? A decisao usa o RETORNO REALIZADO em unidades,
com o payout real de cada trade — nao um breakeven arbitrado.

Inclui parada antecipada: se o IC bootstrap do retorno ja esta todo acima
ou todo abaixo de zero, nao precisa esperar os 250 trades.

Uso:
    python analisar_forward.py
    python analisar_forward.py --csv diario/binaria_forward.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import motor_sinais as M

BT_WR = 0.5603
BT_LO = 0.5416


def bootstrap_ic(ret: np.ndarray, n_boot: int = 20000, seed: int = 7):
    """IC95 percentil da media do retorno. Bootstrap porque o retorno e
    bimodal (+payout ou -1), nao normal."""
    if len(ret) < 10:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(ret), size=(n_boot, len(ret)))
    medias = ret[idx].mean(axis=1)
    return float(np.percentile(medias, 2.5)), float(np.percentile(medias, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="diario/binaria_forward.csv")
    a = ap.parse_args()

    p = Path(a.csv)
    if not p.exists():
        print(f"CSV nao encontrado: {p}")
        print("Rode MONITOR_BINARIA.bat primeiro.")
        return 1

    d = pd.read_csv(p)
    d = d[d["resultado"].isin(["ganho", "perda"])].copy()
    if d.empty:
        print("Nenhum trade resolvido ainda. Deixe o monitor rodando.")
        return 0

    d["retorno_u"] = pd.to_numeric(d["retorno_u"], errors="coerce")
    d["payout_real"] = pd.to_numeric(d["payout_real"], errors="coerce")
    d["slippage_pips"] = pd.to_numeric(d["slippage_pips"], errors="coerce")
    d = d.dropna(subset=["retorno_u"])

    n = len(d)
    w = int((d["resultado"] == "ganho").sum())
    wr = w / n
    lo_w, hi_w = M.wilson(w, n)
    ret = d["retorno_u"].to_numpy()
    media_ret = float(ret.mean())
    lo_r, hi_r = bootstrap_ic(ret)
    pay_med = float(d["payout_real"].mean())
    be = 1 / (1 + pay_med)

    print("=" * 78)
    print("FORWARD TEST — falso rompimento LONG M15 (binaria)")
    print("=" * 78)
    print(f"\n  Trades resolvidos : {n}")
    print(f"  Win rate          : {wr:.2%}   IC95 Wilson [{lo_w:.2%}, {hi_w:.2%}]")
    print(f"  Payout medio real : {pay_med:.4f}  ->  breakeven {be:.2%}")
    print(f"  Retorno por trade : {media_ret:+.4f}u")
    print(f"  IC95 bootstrap    : [{lo_r:+.4f}, {hi_r:+.4f}]  <- decide")
    print(f"  Total acumulado   : {ret.sum():+.2f}u")
    if d["slippage_pips"].notna().any():
        print(f"  Slippage mediano  : {d['slippage_pips'].median():+.2f} pips "
              f"(premissa do backtest era 0)")

    print(f"\n  Referencia do backtest: WR {BT_WR:.2%}, pior caso do IC {BT_LO:.2%}")
    if wr >= BT_WR:
        print("  -> WR ao vivo esta NA OU ACIMA da estimativa.")
    elif wr >= BT_LO:
        print("  -> WR ao vivo esta ENTRE o pior caso e a estimativa.")
    else:
        print("  -> WR ao vivo esta ABAIXO ate do pior caso do backtest.")

    # ---- decisao ----
    print(f"\n{'='*78}")
    print("DECISAO")
    print(f"{'='*78}")
    if lo_r > 0:
        print("  EDGE CONFIRMADO — o IC do retorno esta todo acima de zero.")
        print("  Pode parar de coletar. Proximo passo e dimensionar risco.")
    elif hi_r < 0:
        print("  REPROVADO — o IC do retorno esta todo abaixo de zero.")
        print("  Pode parar de coletar. A estrategia perde dinheiro.")
    else:
        print("  INCONCLUSIVO — o IC ainda cruza zero.")
        larg = hi_r - lo_r
        sd = larg / (2 * 1.96) * np.sqrt(n) if n > 0 else 0.92   # sd implicito

        # Duas projecoes, porque extrapolar so a media OBSERVADA e otimista:
        # ela pode estar alta por sorte. A referencia do backtest e a
        # estimativa conservadora.
        EV_BT = 0.041   # media esperada pelo backtest (WR 56.11%, payout ~0.855)
        print(f"\n  Quantos trades ainda faltam depende de qual media e a verdadeira:")
        for rot, mu in (("media observada agora", media_ret),
                        ("media do backtest", EV_BT)):
            if abs(mu) < 1e-6:
                print(f"    {rot:24} ~zero — pode nao convergir")
                continue
            n_prec = int((1.96 * sd / abs(mu)) ** 2)
            print(f"    {rot:24} ({mu:+.4f}u) -> n ~ {n_prec:<6} "
                  f"faltam ~{max(0, n_prec-n)}")
        if media_ret > EV_BT * 1.3:
            print(f"    >> A media atual esta ACIMA do backtest. Com n={n} isso e")
            print(f"       provavelmente sorte — use a projecao do backtest.")

    # ---- por classe de ativo ----
    # Forex e commodities entraram no teste juntos mas sao evidencias
    # separadas: o forex tem n=8065 de backtest por tras, os commodities
    # so n=1244. Misturar sem separar contaminaria a conclusao do forex.
    COMM = {"XAUUSD", "XAGUSD", "UKOUSD", "USOUSD"}
    CRYP = {"BTCUSD", "ETHUSD", "XRPUSD"}
    d["classe"] = np.where(d["ativo"].isin(CRYP), "crypto",
                  np.where(d["ativo"].isin(COMM), "commodity", "forex"))
    if d["classe"].nunique() > 1:
        print(f"\n  Por classe de ativo:")
        print(f"    {'classe':12} {'n':>5} {'WR':>7} {'IC95 bootstrap':>24} {'ret/trade':>10}")
        for cl in ["forex", "commodity", "crypto"]:
            s = d[d["classe"] == cl]
            if len(s) < 10:
                print(f"    {cl:12} {len(s):>5}  amostra pequena")
                continue
            r = s["retorno_u"].to_numpy()
            lo_c, hi_c = bootstrap_ic(r)
            print(f"    {cl:12} {len(s):>5} {(s['resultado']=='ganho').mean():>6.1%} "
                  f"  [{lo_c:+.4f}, {hi_c:+.4f}] {r.mean():>+10.4f}")
        print(f"    (backtest: forex 56.11%, commodities 55.55%, crypto 56.87%)")

    # ---- por par ----
    print(f"\n  Por par:")
    print(f"    {'par':8} {'n':>5} {'WR':>7} {'payout':>7} {'ret/trade':>10} {'total':>8}")
    for at in sorted(d["ativo"].unique()):
        s = d[d["ativo"] == at]
        print(f"    {at:8} {len(s):>5} {(s['resultado']=='ganho').mean():>6.1%} "
              f"{s['payout_real'].mean():>7.3f} {s['retorno_u'].mean():>+10.4f} "
              f"{s['retorno_u'].sum():>+8.2f}")

    # ---- evolucao ----
    if n >= 40:
        print(f"\n  Evolucao (blocos de {max(10, n//5)} trades):")
        bloco = max(10, n // 5)
        for i in range(0, n - bloco + 1, bloco):
            s = d.iloc[i:i+bloco]
            print(f"    trades {i+1:>4}-{i+bloco:<4}: WR={(s['resultado']=='ganho').mean():>6.1%} "
                  f"ret={s['retorno_u'].mean():>+.4f}u")

    return 0


if __name__ == "__main__":
    sys.exit(main())
