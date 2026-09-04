"""Acompanha um setup em SONDA contra o critério de corte, com payout por par.

Um setup entra como sonda quando o backtest mostra edge positivo mas o método
de validação tem viés conhecido — caso do pin_bar_sr, que é de REVERSÃO e passa
pela reconstrução M1-parcial. Nesses casos o backtest superestima: sr_rejeicao
mede 80.3% no backtest e 62.0% ao vivo (n=50), deflação de ~18pp.

Por que não existe um breakeven único
-------------------------------------
O payout NÃO é 0.87 pra todos os pares. Medido na IQ em 27/08/2026: EURUSD,
GBPUSD, USDJPY, AUDUSD e EURJPY pagam 0.85 (breakeven 54.05%); USDCAD e NZDUSD
pagam 0.87 (breakeven 53.48%). Um breakeven fixo de 53.5% é otimista pra 5 dos
7 pares e aprovaria setup sem edge.

A decisão usa o RETORNO REALIZADO (lucro/valor), não o WR contra um breakeven
teórico. O retorno realizado já embute o payout que a IQ de fato pagou em cada
ordem — inclusive quando difere do payout que estava em cache na hora do sinal
(visto: EURJPY registrou 0.85 mas pagou 0.87). Testar se o retorno médio é
maior que zero é exatamente a pergunta que importa, e dispensa arbitrar
breakeven. O WR por par contra o breakeven do próprio par fica como
diagnóstico, pra mostrar qual par está puxando o resultado.

Uso:
    python sonda_setup.py                      # pin_bar_sr no M15 (padrão)
    python sonda_setup.py --setup sr_rejeicao
    python sonda_setup.py --tf h1 --n-minimo 50
"""
from __future__ import annotations

import argparse
import math
import random
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent / "iqoption_m5" / "dados"


def wilson(vitorias: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """IC de Wilson — correto em amostra pequena, ao contrário do IC normal."""
    if total == 0:
        return (0.0, 1.0)
    p = vitorias / total
    d = 1 + z * z / total
    centro = (p + z * z / (2 * total)) / d
    margem = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return (max(0.0, centro - margem), min(1.0, centro + margem))


def bootstrap_media(amostra: list[float], reps: int = 20000,
                    semente: int = 20260827) -> tuple[float, float]:
    """IC95% percentil da média. Não assume normalidade — o retorno é bimodal
    (ou +payout ou -1), então o t-test aproximaria mal em amostra pequena."""
    if not amostra:
        return (0.0, 0.0)
    rng = random.Random(semente)
    n = len(amostra)
    medias = []
    for _ in range(reps):
        medias.append(sum(rng.choices(amostra, k=n)) / n)
    medias.sort()
    return (medias[int(0.025 * reps)], medias[int(0.975 * reps)])


def breakeven_de(payout: float) -> float:
    """WR mínimo pra empatar: ganha payout, perde 1."""
    return 1.0 / (1.0 + payout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", default="pin_bar_sr")
    ap.add_argument("--tf", default="m15", choices=["m5", "m15", "h1", "m1"])
    ap.add_argument("--n-minimo", type=int, default=100,
                    help="amostra a partir da qual a decisão é tomada")
    a = ap.parse_args()

    sufixo = "" if a.tf == "m5" else f"_{a.tf}"
    db = BASE / f"iqoption_m5_practice_scalping{sufixo}.sqlite3"
    if not db.exists():
        print(f"banco não encontrado: {db}")
        return 1

    con = sqlite3.connect(db)
    linhas = con.execute(
        """SELECT lucro, ativo, enviada_em, payout, valor FROM operacoes
           WHERE status = 'finalizada' AND setup = ?
           ORDER BY enviada_em""",
        (a.setup,),
    ).fetchall()
    con.close()

    print(f"=== SONDA: {a.setup} @ {a.tf.upper()} ===")
    print(f"banco    : {db.name}")

    if not linhas:
        print("\nsem operacoes finalizadas ainda - a sonda ainda nao disparou.")
        return 0

    # Decididas = ganho ou perda. Empate não carrega informação de edge.
    decididas = []
    for lucro, ativo, enviada_em, payout, valor in linhas:
        lucro = lucro or 0
        if lucro == 0:
            continue
        # Retorno por unidade apostada. Se 'valor' faltar, reconstrói pelo
        # payout registrado — menos exato, mas melhor que descartar a ordem.
        if valor:
            retorno = lucro / valor
        else:
            retorno = (payout or 0.0) if lucro > 0 else -1.0
        decididas.append({
            "ativo": ativo, "venceu": lucro > 0, "retorno": retorno,
            "payout": payout, "lucro": lucro, "quando": enviada_em,
        })

    n = len(decididas)
    if n == 0:
        print("\nso empates - sem informacao.")
        return 0

    vitorias = sum(1 for d in decididas if d["venceu"])
    derrotas = n - vitorias
    pnl = sum(d["lucro"] for d in decididas)

    print(f"periodo  : {decididas[0]['quando'][:16]} -> {decididas[-1]['quando'][:16]}")
    print(f"amostra  : {n} decididos ({vitorias}W / {derrotas}L)  PnL {pnl:+.2f}")

    # ---------------- diagnóstico por par ----------------
    por_par: dict[str, list[dict]] = {}
    for d in decididas:
        por_par.setdefault(d["ativo"], []).append(d)

    print("\n--- por par (breakeven do proprio par) ---")
    print(f"{'par':<12}{'n':>4}{'W':>4}{'L':>4}{'WR':>8}{'payout':>9}"
          f"{'brkeven':>9}{'edge':>9}")
    for ativo in sorted(por_par):
        grupo = por_par[ativo]
        n_p = len(grupo)
        w_p = sum(1 for d in grupo if d["venceu"])
        wr_p = w_p / n_p
        payouts_p = [d["payout"] for d in grupo if d["payout"]]
        pay_p = sum(payouts_p) / len(payouts_p) if payouts_p else 0.0
        be_p = breakeven_de(pay_p) if pay_p else float("nan")
        edge_p = (wr_p - be_p) * 100
        print(f"{ativo:<12}{n_p:>4}{w_p:>4}{n_p - w_p:>4}{wr_p:>7.1%}"
              f"{pay_p:>9.2f}{be_p:>8.1%}{edge_p:>+8.1f}pp")

    # ---------------- agregado ----------------
    payouts = [d["payout"] for d in decididas if d["payout"]]
    pay_medio = sum(payouts) / len(payouts) if payouts else 0.0
    be_medio = breakeven_de(pay_medio) if pay_medio else float("nan")

    wr = vitorias / n
    lo_wr, hi_wr = wilson(vitorias, n)
    retornos = [d["retorno"] for d in decididas]
    ret_medio = sum(retornos) / n
    lo_r, hi_r = bootstrap_media(retornos)

    print("\n--- agregado ---")
    print(f"WR            : {wr:.1%}   IC95% Wilson [{lo_wr:.1%}, {hi_wr:.1%}]")
    print(f"payout medio  : {pay_medio:.3f}  -> breakeven {be_medio:.1%}"
          f"  (edge {(wr - be_medio) * 100:+.1f}pp)")
    print(f"retorno/unidade: {ret_medio:+.4f}  IC95% bootstrap "
          f"[{lo_r:+.4f}, {hi_r:+.4f}]")

    # ---------------- decisão ----------------
    # O teste decisivo é o retorno realizado: > 0 significa lucro por unidade
    # apostada, com o payout que a IQ de fato pagou.
    print()
    if n < a.n_minimo:
        print(f"AGUARDAR - {n}/{a.n_minimo} da amostra minima. "
              f"Faltam {a.n_minimo - n} operacoes decididas.")
        if hi_r < 0:
            print("  ATENCAO: mesmo com amostra parcial, o teto do IC do retorno "
                  "ja esta abaixo de zero. Considere desligar antes do prazo.")
    elif lo_r > 0:
        print("PROMOVER - IC95% do retorno inteiro acima de zero: "
              "edge positivo provado com o payout real.")
    elif hi_r < 0:
        print("DESLIGAR - IC95% do retorno inteiro abaixo de zero: "
              "edge negativo provado.")
    else:
        print("INCONCLUSIVO - o IC95% do retorno cruza zero. Sem vantagem "
              "demonstrada; manter em stake neutro e reavaliar com mais amostra.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
