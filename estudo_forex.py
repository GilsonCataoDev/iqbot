"""ESTUDO FOREX — qual sinal tem direcao suficiente para pagar o spread?

Duas fases, nessa ordem:

  FASE 1 (triagem MFE/MAE): o preco anda mais a FAVOR que CONTRA?
    Se MFE/MAE <= 1, NENHUMA geometria de SL/TP salva. Descarta sem gastar
    tempo otimizando. (Foi o que provou que o Fibo era anti-preditivo.)

  FASE 2 (geometria): so para quem passou na fase 1, varre SL/TP em ATR
    e mede EV com R REALIZADO, SL antes do TP, sem sobreposicao.

Correcoes: B1 B2 B3 B4 B5 B7 todas ativas (ver motor_sinais.py).

Uso:
    python estudo_forex.py
    python estudo_forex.py --tf 900 3600
    python estudo_forex.py --fase 1        (so triagem, rapido)
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

import motor_sinais as M

# Grade de geometria testada na fase 2
GRADE_SL = [1.0, 1.5, 2.0, 3.0]
GRADE_TP = [1.0, 1.5, 2.0, 3.0, 4.0]
LIMIAR_TRIAGEM = 1.05    # MFE/MAE minimo para valer a pena otimizar


def custo_spread(frames) -> dict[str, float]:
    out = {}
    for a, df in frames.items():
        am = M.atr(df).median()
        if am and np.isfinite(am):
            out[a] = M.spread(a) / float(am)
    return out


def fase1_triagem(frames, nome_tf) -> pd.DataFrame:
    print(f"\n{'='*94}")
    print(f"FASE 1 — TRIAGEM MFE/MAE ({nome_tf})")
    print(f"{'='*94}")
    cs = custo_spread(frames)
    print(f"  Custo do spread neste tf: {min(cs.values()):.1%} a {max(cs.values()):.1%} do ATR")
    print(f"  MFE/MAE medido SEM spread (direcao pura); o custo entra na fase 2.")

    # Baseline aleatorio do MESMO timeframe define o zero
    d0 = M.mfe_mae(frames, M.SINAIS["aleatorio (BASELINE)"])
    base = (float(d0["mfe"].median()) / float(d0["mae"].median())) if not d0.empty else 1.0
    limiar = base * LIMIAR_TRIAGEM
    print(f"  Baseline aleatorio = {base:.3f}  ->  criterio: MFE/MAE > {limiar:.3f}"
          f" ({(LIMIAR_TRIAGEM-1)*100:.0f}% acima do acaso)\n")
    print(f"  {'sinal':30} {'n':>7} {'MFE':>7} {'MAE':>7} {'MFE/MAE':>8} {'vs base':>8}  status")
    print(f"  {'-'*30} {'-'*7} {'-'*7} {'-'*7} {'-'*8} {'-'*8}  {'-'*10}")

    linhas = []
    for nome, fn in M.SINAIS.items():
        d = M.mfe_mae(frames, fn)
        if d.empty or len(d) < 100:
            print(f"  {nome:30} {len(d):>7} {'':>7} {'':>7} {'':>8} {'':>8}  amostra pequena")
            continue
        mfe = float(d["mfe"].median())
        mae = float(d["mae"].median())
        raz = mfe / mae if mae > 0 else 0.0
        dif = raz - base
        passa = raz > limiar
        st = "PASSA" if passa else ("abaixo do acaso" if dif < -0.02 else "neutro")
        print(f"  {nome:30} {len(d):>7} {mfe:>7.2f} {mae:>7.2f} {raz:>8.3f} "
              f"{dif:>+8.3f}  {st}")
        linhas.append({"sinal": nome, "n": len(d), "mfe": mfe, "mae": mae,
                       "razao": raz, "vs_base": dif, "passa": passa})
    return pd.DataFrame(linhas)


def fase2_geometria(frames, nome_tf, candidatos: list[str]) -> pd.DataFrame:
    print(f"\n{'='*94}")
    print(f"FASE 2 — GEOMETRIA SL/TP ({nome_tf})")
    print(f"{'='*94}")
    if not candidatos:
        print("  Nenhum sinal passou na triagem. Nada a otimizar.")
        return pd.DataFrame()

    linhas = []
    for nome in candidatos:
        fn = M.SINAIS[nome]
        print(f"\n  >> {nome}")
        print(f"     {'SL':>5} {'TP':>5} {'n':>6} {'n_ef':>6} {'WR':>7} "
              f"{'R:R_g':>6} {'EV':>9} {'total':>9}  status")
        melhor = None
        for sl in GRADE_SL:
            for tp in GRADE_TP:
                d = M.simular_forex(frames, fn, sl, tp)
                if d.empty or len(d) < 60:
                    continue
                s = M.stats_forex(d)
                st = "EDGE" if s["edge"] else ("+" if s["ev"] > 0 else "")
                print(f"     {sl:>5.1f} {tp:>5.1f} {s['n']:>6} {s['n_ef']:>6} "
                      f"{s['wr']:>6.1%} {s['rr_ganhos']:>6.2f} {s['ev']:>+9.4f} "
                      f"{s['total_r']:>+9.1f}  {st}")
                reg = {"tf": nome_tf, "sinal": nome, "sl": sl, "tp": tp, **s}
                linhas.append(reg)
                if melhor is None or s["ev"] > melhor["ev"]:
                    melhor = reg

        # Holdout temporal na melhor geometria
        if melhor and melhor["ev"] > 0:
            d = M.simular_forex(frames, fn, melhor["sl"], melhor["tp"])
            cal, hold = M.split_temporal(d)
            sc, sh = M.stats_forex(cal), M.stats_forex(hold)
            if sc and sh:
                print(f"     holdout na melhor (SL={melhor['sl']} TP={melhor['tp']}): "
                      f"cal EV={sc['ev']:+.4f}R  hold EV={sh['ev']:+.4f}R")
                # consistencia por par
                print(f"     por par:", end=" ")
                bons = 0
                for at in sorted(d["ativo"].unique()):
                    sp_ = M.stats_forex(d[d["ativo"] == at])
                    if sp_:
                        sinal_ev = "+" if sp_["ev"] > 0 else "-"
                        if sp_["ev"] > 0: bons += 1
                        print(f"{at}{sinal_ev}", end=" ")
                print(f" ({bons}/{d['ativo'].nunique()} positivos)")
    return pd.DataFrame(linhas)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", nargs="*", type=int, default=[900, 3600])
    ap.add_argument("--fase", type=int, default=2, choices=[1, 2])
    ap.add_argument("--ativos", nargs="*", default=None)
    a = ap.parse_args()

    print("="*94)
    print("ESTUDO FOREX — direcao suficiente para pagar o spread?")
    print("="*94)
    print("  Fase 1: MFE/MAE. Se o preco nao anda mais a favor que contra,")
    print("          nenhuma geometria de SL/TP resolve — descarta.")
    print("  Fase 2: varredura SL/TP so nos aprovados. EV com R realizado,")
    print("          SL antes do TP, trades nao-sobrepostos, holdout temporal.")

    resumo = []
    for tf in a.tf:
        frames = M.carregar(tf, a.ativos)
        if not frames:
            print(f"\n  tf={tf}: sem dados"); continue
        nome_tf = M.TF_NOME.get(tf, str(tf))
        t1 = fase1_triagem(frames, nome_tf)
        if t1.empty: continue
        cand = t1[t1["passa"]]["sinal"].tolist()
        # tira o baseline aleatorio dos candidatos
        cand = [c for c in cand if "BASELINE" not in c]
        print(f"\n  Passaram na triagem: {len(cand)} -> {cand if cand else 'nenhum'}")

        if a.fase >= 2:
            t2 = fase2_geometria(frames, nome_tf, cand)
            if not t2.empty:
                resumo.append(t2)

    if resumo:
        t = pd.concat(resumo, ignore_index=True)
        print(f"\n{'='*94}")
        print("MELHORES CONFIGURACOES FOREX (EV > 0, ordenado por EV)")
        print(f"{'='*94}")
        r = t[t["ev"] > 0].sort_values("ev", ascending=False)
        if r.empty:
            print("  NENHUMA configuracao com EV positivo.")
        else:
            print(f"  {'tf':>5} {'sinal':28} {'SL':>4} {'TP':>4} {'n':>6} "
                  f"{'WR':>7} {'EV':>9} {'total':>9}  status")
            for _, x in r.head(15).iterrows():
                st = "EDGE" if x["edge"] else ""
                print(f"  {x['tf']:>5} {x['sinal']:28} {x['sl']:>4.1f} {x['tp']:>4.1f} "
                      f"{x['n']:>6} {x['wr']:>6.1%} {x['ev']:>+9.4f} {x['total_r']:>+9.1f}  {st}")

        n_testes = len(t)
        n_edge = int(t["edge"].sum())
        print(f"\n  Configuracoes testadas: {n_testes} | com EDGE: {n_edge} | "
              f"esperado por acaso: {0.05*n_testes:.1f}")
        if n_edge <= 0.05 * n_testes:
            print("  >> Nao excede o acaso. Nenhum edge forex confirmado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
