"""Replay dos sinais Fibo M15 gravados como se fossem CFD, com spread.

Não envia nada. Só responde: a vantagem medida a preço médio sobrevive ao
custo de operar? Roda com `python analisar_fibo_paper_forex.py`.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

from iqoption_m5.forex_paper import avaliar

ARQ = Path("diario/monitor_mercado/sinais_aprendizado.json")


def _ic_media(v: list[float], z: float = 1.96) -> tuple[float, float] | None:
    n = len(v)
    if n < 3:
        return None
    m = sum(v) / n
    var = sum((x - m) ** 2 for x in v) / (n - 1)
    return (m - z * (var / n) ** .5, m + z * (var / n) ** .5)


def _linha(rotulo: str, rs: list[float]) -> str:
    if not rs:
        return f"{rotulo:26} —"
    m = sum(rs) / len(rs)
    ic = _ic_media(rs)
    txt = f"[{ic[0]:+.3f}, {ic[1]:+.3f}]" if ic else "amostra curta"
    veredito = ("VANTAGEM" if ic and ic[0] > 0 else
                "PREJUIZO" if ic and ic[1] < 0 else "inconclusivo")
    return (f"{rotulo:26} n={len(rs):4}  R={m:+.3f}  soma={sum(rs):+8.2f}  "
            f"IC={txt:22} {veredito}")


def main(tipo: str = "fibo_m15", lote: float = 0.1) -> int:
    if not ARQ.exists():
        print(f"{ARQ} nao encontrado.")
        return 1
    dados = json.loads(ARQ.read_text(encoding="utf-8"))
    itens = dados if isinstance(dados, list) else dados.get("sinais", [])

    brutos: list[float] = []
    liquidos: list[float] = []
    operaveis: list[float] = []
    descartados = 0
    por_ativo: dict[str, list] = collections.defaultdict(list)

    for s in itens:
        if s.get("tipo") != tipo:
            continue
        sim = s.get("simulacao") or {}
        r = sim.get("resultado_r")
        alvo = s.get("alvos_estudo") or s.get("alvos") or {}
        if r is None:
            continue
        v = avaliar(s.get("ativo", "?"), alvo.get("entrada"), alvo.get("sl"),
                    r, lote=lote)
        if v is None:
            descartados += 1
            continue
        brutos.append(v.r_sem_custo)
        liquidos.append(v.r_com_custo)
        if v.operavel:
            operaveis.append(v.r_com_custo)
        por_ativo[v.ativo].append(v)

    print(f"=== {tipo} como CFD, lote {lote} ===")
    print(f"sinais resolvidos: {len(brutos)}   sem entrada/sl utilizavel: {descartados}")
    print()
    print(_linha("preco medio (sem custo)", brutos))
    print(_linha("depois do spread", liquidos))
    print(_linha("so os operaveis", operaveis))
    print()
    # Dinheiro fica por ativo de proposito: somar iene com franco e dolar
    # produz um total que nao existe em moeda nenhuma.
    print(f"{'ativo':10} {'n':>3} {'oper':>5} {'custo_R':>8} {'R_bruto':>8} "
          f"{'R_liq':>8} {'$ cotacao':>12}")
    for a, vs in sorted(por_ativo.items(), key=lambda kv: -len(kv[1])):
        op = sum(1 for v in vs if v.operavel)
        custo = sum(v.custo_em_r for v in vs) / len(vs)
        bruto = sum(v.r_sem_custo for v in vs) / len(vs)
        liq = sum(v.r_com_custo for v in vs) / len(vs)
        cash = sum(v.lucro_por_lote for v in vs if v.operavel)
        marca = "" if op == len(vs) else "  <- stop menor que o spread"
        print(f"{a:10} {len(vs):3} {op:5} {custo:8.3f} {bruto:+8.3f} {liq:+8.3f} "
              f"{cash:>+12,.2f}{marca}")
    print()
    print("Piso, nao teto: mantivemos os desfechos do replay a preco medio. O")
    print("spread real tambem provoca stops que o preco medio nunca registrou.")
    return 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:] or [])))
