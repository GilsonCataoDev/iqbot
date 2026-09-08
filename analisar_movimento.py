"""Perfil de movimento por ativo: quanto anda, e se dá para saber o lado.

Usa as velas M15 já gravadas em iqoption_m5/dados/historico. Não conecta na
corretora e não envia nada. `python analisar_movimento.py [horizonte_velas]`
"""
from __future__ import annotations

import glob
import os
import sys

import pandas as pd

from iqoption_m5.perfil_movimento import direcao_tem_memoria, magnitude, por_hora

PASTA = "iqoption_m5/dados/historico"
# Abaixo disso a distribuição é de uma semana de mercado, não do ativo.
MINIMO_CONFIAVEL = 5000


def main(horizonte: int = 24) -> int:
    linhas = []
    for arq in sorted(glob.glob(f"{PASTA}/*_900s.csv")):
        ativo = os.path.basename(arq).replace("_900s.csv", "")
        if "-OTC" in ativo:
            continue
        df = pd.read_csv(arq, parse_dates=["timestamp"]).set_index("timestamp")
        m = magnitude(df, horizonte)
        if not m.get("n"):
            continue
        linhas.append((ativo, m, direcao_tem_memoria(df, horizonte)))

    horas = horizonte * 15 / 60
    print(f"=== movimento nas proximas {horizonte} velas M15 ({horas:.0f}h) ===")
    print("amplitude = topo a fundo da janela | contra = pior ponto para quem "
          "compra no fechamento\n")
    print(f"{'ativo':9} {'n':>6} {'amplitude % (p25/p50/p75)':>27} "
          f"{'contra % (p50/p75/p90)':>25}")
    for ativo, m, _ in linhas:
        a, c = m["amplitude_pct"], m["excursao_contra_pct"]
        aviso = "  <- historico curto" if m["n"] < MINIMO_CONFIAVEL else ""
        print(f"{ativo:9} {m['n']:6} "
              f"{a['p25']:8.3f} {a['p50']:8.3f} {a['p75']:8.3f}   "
              f"{c['p50']:7.3f} {c['p75']:7.3f} {c['p90']:7.3f}{aviso}")

    print(f"\n=== a direcao das ultimas {horas:.0f}h prevê as proximas {horas:.0f}h? ===")
    print("IC cruzando 50% = sem memoria de direcao nessa escala\n")
    print(f"{'ativo':9} {'n':>6} {'acerto':>7} {'IC 95%':>18}  veredito")
    for ativo, m, d in linhas:
        if not d.get("n"):
            continue
        ic = d["ic_95"]
        v = ("SEGUE" if ic[0] > 50 else
             "REVERTE" if ic[1] < 50 else "sem memoria")
        aviso = "  <- historico curto" if m["n"] < MINIMO_CONFIAVEL else ""
        print(f"{ativo:9} {d['n']:6} {d['taxa']:6.2f}% "
              f"[{ic[0]:6.2f}, {ic[1]:6.2f}]  {v}{aviso}")
    return 0


def detalhe(ativo: str, horizonte: int = 24) -> int:
    """Perfil hora a hora de um ativo — para achar a janela que anda."""
    arq = f"{PASTA}/{ativo}_900s.csv"
    if not os.path.exists(arq):
        print(f"{arq} nao encontrado.")
        return 1
    df = pd.read_csv(arq, parse_dates=["timestamp"]).set_index("timestamp")
    m = magnitude(df, horizonte)
    print(f"=== {ativo}, {horizonte} velas M15 ===")
    print(f"n={m['n']}")
    for chave in ("amplitude_pct", "deslocamento_pct", "excursao_contra_pct"):
        print(f"{chave:22} {m[chave]}")
    d = direcao_tem_memoria(df, horizonte)
    print(f"direcao                acerto={d.get('taxa')}% IC={d.get('ic_95')}")
    print("\namplitude mediana % por hora (UTC da vela):")
    ph = por_hora(df, horizonte)
    for h in sorted(ph):
        barra = "#" * int(ph[h] / max(ph.values()) * 40)
        print(f"  {h:02d}h {ph[h]:7.3f}  {barra}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and not args[0].isdigit():
        sys.exit(detalhe(args[0], int(args[1]) if len(args) > 1 else 24))
    sys.exit(main(int(args[0]) if args else 24))
