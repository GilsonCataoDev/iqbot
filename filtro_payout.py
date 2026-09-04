"""Mede se filtrar por payout (e/ou por par) melhora o edge do falso rompimento LONG.

DUAS COISAS DIFERENTES, medidas separado:

  FILTRO A — por PAYOUT. Legitimo: o payout e conhecido no instante da entrada,
    entao decidir com base nele nao usa informacao do futuro. Um par que paga
    0.82 tem breakeven 54.95%; um que paga 0.87 tem 53.48%. Mesmo WR, margens
    diferentes.

  FILTRO B — por PAR (escolher os pares com melhor WR historico). Isso E data
    snooping: escolhe com base no mesmo dado que mediu. So vale se sobreviver
    out-of-sample — seleciona na 1a metade do tempo, avalia na 2a.

Uso:
    python filtro_payout.py
    python filtro_payout.py --ao-vivo    (le payout real da IQ agora)
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

import motor_sinais as M

PARES = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD",
         "EURGBP", "EURCHF", "EURCAD", "GBPJPY", "GBPCHF", "GBPCAD", "CADCHF"]

# Snapshot lido da IQ em 31/08/2026. O payout VARIA — ver --ao-vivo.
PAYOUT_SNAP = {
    "EURUSD": 0.85, "GBPUSD": 0.85, "USDJPY": 0.85, "AUDUSD": 0.85,
    "EURJPY": 0.85, "USDCAD": 0.87, "NZDUSD": 0.87, "EURGBP": 0.85,
    "EURCHF": 0.82, "EURCAD": 0.87, "GBPJPY": 0.85, "GBPCHF": 0.85,
    "GBPCAD": 0.85, "CADCHF": 0.87,
}


def ler_payout_vivo() -> dict[str, float]:
    from iqoption_m5.config import configuracao_scalping_m15
    from iqoption_m5.mercado_iq import MercadoIQ
    import monitor_binaria as MB
    api = MercadoIQ(configuracao_scalping_m15()).conectar_somente_leitura()
    d = api.get_all_init_v2()
    out = {}
    for secao in ("turbo", "binary"):
        for det in d.get(secao, {}).get("actives", {}).values():
            if not isinstance(det, dict):
                continue
            nome = str(det.get("name", "")).split(".", 1)[-1].upper()
            if nome.endswith("-OP"):
                nome = nome[:-3]
            if nome not in PARES:
                continue
            if not (det.get("enabled") and not det.get("is_suspended")):
                continue
            v = MB._payout_de(det)
            if v and (nome not in out or v > out[nome]):
                out[nome] = v
    return out


def ev_unidades(wr: float, payout: float) -> float:
    return wr * payout - (1 - wr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ao-vivo", action="store_true")
    a = ap.parse_args()

    payouts = dict(PAYOUT_SNAP)
    if a.ao_vivo:
        print("Lendo payout ao vivo...")
        vivo = ler_payout_vivo()
        if vivo:
            for k, v in vivo.items():
                if abs(v - payouts.get(k, v)) > 1e-9:
                    print(f"  {k}: snapshot {payouts.get(k)} -> ao vivo {v}")
            payouts.update(vivo)

    frames = M.carregar(900, PARES)
    d = M.avaliar_binario(frames, M.SINAIS["falso rompimento"])
    lg = d[d["direcao"] == "long"].copy()
    lg["payout"] = lg["ativo"].map(payouts).fillna(0.85)
    lg["retorno"] = np.where(lg["acerto"], lg["payout"], -1.0)

    print("\n" + "=" * 92)
    print("PONTO DE PARTIDA — sem filtro nenhum")
    print("=" * 92)
    n = len(lg); w = int(lg["acerto"].sum()); wr = w / n
    nef = lg["quando"].nunique()
    lo, hi = M.wilson(int(round(wr * nef)), nef)
    ev = lg["retorno"].mean()
    print(f"  n={n}  n_ef={nef}  WR={wr:.2%}  IC=[{lo:.2%},{hi:.2%}]")
    print(f"  EV={ev:+.4f}u/trade   total={lg['retorno'].sum():+.1f}u")

    # ------------------------------------------------------------------
    print("\n" + "=" * 92)
    print("DETALHE POR PAR — mesmo WR rende diferente conforme o payout")
    print("=" * 92)
    print(f"  {'par':8} {'n':>5} {'WR':>7} {'payout':>7} {'breakeven':>10} "
          f"{'margem':>8} {'EV/trade':>9} {'total':>8}")
    linhas = []
    for at in PARES:
        s = lg[lg["ativo"] == at]
        if len(s) < 50:
            continue
        wr_a = s["acerto"].mean()
        pay = payouts.get(at, 0.85)
        be = 1 / (1 + pay)
        linhas.append({"par": at, "n": len(s), "wr": wr_a, "payout": pay,
                       "be": be, "margem": wr_a - be,
                       "ev": ev_unidades(wr_a, pay), "total": s["retorno"].sum()})
    t = pd.DataFrame(linhas).sort_values("margem", ascending=False)
    for _, x in t.iterrows():
        flag = "" if x["margem"] > 0 else "  <-- negativo"
        print(f"  {x['par']:8} {x['n']:>5} {x['wr']:>6.2%} {x['payout']:>7.2f} "
              f"{x['be']:>9.2%} {x['margem']:>+7.2%} {x['ev']:>+9.4f} "
              f"{x['total']:>+8.1f}{flag}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 92)
    print("FILTRO A — por PAYOUT (legitimo: conhecido na hora da entrada)")
    print("=" * 92)
    print(f"  {'regra':28} {'pares':>6} {'n':>6} {'n_ef':>6} {'WR':>7} "
          f"{'EV/trade':>9} {'total':>9}")
    for limiar in [0.0, 0.83, 0.85, 0.86, 0.87]:
        sel = [k for k, v in payouts.items() if v >= limiar and k in set(lg["ativo"])]
        s = lg[lg["ativo"].isin(sel)]
        if len(s) < 100:
            continue
        wr_s = s["acerto"].mean()
        nef_s = s["quando"].nunique()
        rot = "sem filtro" if limiar == 0 else f"payout >= {limiar:.2f}"
        print(f"  {rot:28} {len(sel):>6} {len(s):>6} {nef_s:>6} {wr_s:>6.2%} "
              f"{s['retorno'].mean():>+9.4f} {s['retorno'].sum():>+9.1f}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 92)
    print("FILTRO B — por PAR, testado OUT-OF-SAMPLE (senao e data snooping)")
    print("=" * 92)
    lg_ord = lg.sort_values("quando")
    corte = len(lg_ord) // 2
    p1, p2 = lg_ord.iloc[:corte], lg_ord.iloc[corte:]
    print(f"  1a metade ate {p1['quando'].max()}  |  2a metade a partir dai")

    # seleciona na 1a metade os pares com margem positiva
    bons = []
    for at in PARES:
        s = p1[p1["ativo"] == at]
        if len(s) < 30:
            continue
        if s["acerto"].mean() - 1 / (1 + payouts.get(at, 0.85)) > 0:
            bons.append(at)
    print(f"  pares selecionados na 1a metade ({len(bons)}): {', '.join(bons)}")

    def bloco(rot, s):
        if len(s) < 50:
            print(f"    {rot:34} amostra pequena"); return None
        wr_ = s["acerto"].mean(); nef_ = s["quando"].nunique()
        lo_, hi_ = M.wilson(int(round(wr_ * nef_)), nef_)
        print(f"    {rot:34} n={len(s):<5} WR={wr_:.2%} "
              f"IC=[{lo_:.2%},{hi_:.2%}] EV={s['retorno'].mean():+.4f}u")
        return s["retorno"].mean()

    print("\n  Na 2a metade (out-of-sample):")
    ev_sel = bloco("so os pares selecionados", p2[p2["ativo"].isin(bons)])
    ev_out = bloco("os pares descartados", p2[~p2["ativo"].isin(bons)])
    ev_all = bloco("todos os pares (sem selecao)", p2)

    print()
    if ev_sel is not None and ev_all is not None:
        if ev_sel > ev_all + 0.005:
            print("  >> A selecao de pares AJUDOU fora da amostra.")
        elif ev_sel < ev_all - 0.005:
            print("  >> A selecao de pares PIOROU fora da amostra — era ruido.")
        else:
            print("  >> A selecao de pares nao mudou nada fora da amostra.")
            print("     Escolher par pelo WR passado nao se sustenta: use todos.")

    # ------------------------------------------------------------------
    print("\n" + "=" * 92)
    print("SENSIBILIDADE — e se o payout cair?")
    print("=" * 92)
    print(f"  WR global = {wr:.2%}. Payout minimo para nao perder: {(1-wr)/wr:.4f}")
    print(f"  {'payout':>8} {'breakeven':>10} {'margem':>8} {'EV/trade':>9}")
    for pay in [0.80, 0.82, 0.84, 0.85, 0.86, 0.87, 0.88]:
        be = 1 / (1 + pay)
        e = ev_unidades(wr, pay)
        flag = "" if e > 0 else "  <-- perde"
        print(f"  {pay:>8.2f} {be:>9.2%} {wr-be:>+7.2%} {e:>+9.4f}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
