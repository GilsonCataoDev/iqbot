"""Vigilante de amostras — agente 3.

Monitora filtros de sombra e retestes. Alerta quando n atinge o
threshold de decisao (confirmar, remover ou expandir).

Uso: python vigilante_amostras.py [--conta practice|real|ambas]  (padrao: ambas)
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DADOS = Path(__file__).resolve().parents[2] / "iqoption_m5/dados"
BANCOS = {
    "practice": DADOS / "iqoption_m5_practice_ema_laboratorio_practice.sqlite3",
    "real":     DADOS / "iqoption_m5_real_ema_laboratorio_real.sqlite3",
}
BREAK_EVEN = 54.0

# threshold = n minimo para tomar decisao sobre o filtro/reteste
FILTROS_SOMBRA = {
    "noticia_high":       {"threshold": 80,  "desc": "Noticia high impact"},
    "put_horario_fraco":  {"threshold": 100, "desc": "PUT 05h-13h BRT"},
    "put_ativo_fraco":    {"threshold": 80,  "desc": "USDJPY/USDCAD PUT"},
    "m5_leitura_ausente": {"threshold": 80,  "desc": "Sem leitura M5"},
}

RETESTES = [
    {"setup": "ema920_pullback",       "ativo": "EURUSD", "threshold": 80,  "desc": "EURUSD reteste (reiniciado 18/09)"},
    {"setup": "ema920_pullback",       "ativo": "AUDCAD", "threshold": 200, "desc": "AUDCAD reteste"},
    {"setup": "fibo_mtf_confirmado",   "ativo": None,     "threshold": 30,  "desc": "Fibo MTF (todos ativos)"},
    {"setup": "nzd_trend_pullback_v1", "ativo": "NZDUSD", "threshold": 30,  "desc": "NZD reteste"},
    {"setup": "ema921_rsi_pullback",   "ativo": "USDCAD", "threshold": 50,  "desc": "USDCAD RSI pullback"},
]


def barra(n, th):
    pct = min(n / th * 100, 100)
    return "#" * int(pct / 10) + "." * (10 - int(pct / 10))


def vigiar(conta, banco):
    """Imprime a secao de uma conta e devolve a lista de alertas."""
    alertas = []
    tag = conta.upper()
    if not banco.exists():
        print(f"##### {tag} — banco nao encontrado: {banco.name}\n")
        return alertas

    atualizado = datetime.fromtimestamp(banco.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    print(f"##### {tag} — {banco.name} (ultima gravacao {atualizado})\n")

    conn = sqlite3.connect(banco)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # --- Filtros de sombra ---
    print(f"[Filtros de sombra {tag}]")
    cur.execute(
        "SELECT motivo, resultado, COUNT(*) n FROM simulacoes "
        "WHERE motivo IN ({}) GROUP BY motivo, resultado".format(
            ",".join(f"'{m}'" for m in FILTROS_SOMBRA)
        )
    )
    dados = defaultdict(lambda: {"win": 0, "loss": 0, "total": 0})
    for r in cur.fetchall():
        dados[r["motivo"]]["total"] += r["n"]
        if r["resultado"] == "win":
            dados[r["motivo"]]["win"] += r["n"]
        elif r["resultado"] == "loss":
            dados[r["motivo"]]["loss"] += r["n"]

    for motivo, cfg in FILTROS_SOMBRA.items():
        d = dados.get(motivo, {"win": 0, "loss": 0, "total": 0})
        wl = d["win"] + d["loss"]
        wr = d["win"] / wl * 100 if wl else 0
        th = cfg["threshold"]
        status = ">>> AVALIAR AGORA <<<" if d["total"] >= th else f"{d['total']}/{th}"
        print(f"  {motivo:<25} [{barra(d['total'], th)}] n={d['total']:>4}  WR={wr:>4.0f}%  {status}")
        if d["total"] >= th:
            decisao = "MANTER" if wr < BREAK_EVEN else "REMOVER (WR acima do break-even)"
            alertas.append(f"[{tag}][FILTRO] {motivo}: n={d['total']}, WR={wr:.0f}% -> {decisao}")

    # --- Retestes ---
    print(f"\n[Retestes {tag}]")
    for r in RETESTES:
        setup, ativo, th = r["setup"], r["ativo"], r["threshold"]
        if ativo:
            cur.execute(
                "SELECT lucro FROM operacoes WHERE setup=? AND ativo=? AND status='finalizada'",
                (setup, ativo),
            )
        else:
            cur.execute(
                "SELECT lucro FROM operacoes WHERE setup=? AND status='finalizada'", (setup,)
            )
        rows = cur.fetchall()
        w = sum(1 for x in rows if x["lucro"] > 0)
        n = len(rows)
        wr = w / n * 100 if n else 0
        status = ">>> AVALIAR AGORA <<<" if n >= th else f"{n}/{th}"
        label = f"{setup}/{ativo}" if ativo else setup
        print(f"  {label:<35} [{barra(n, th)}] n={n:>4}  WR={wr:>4.0f}%  {status}")
        if n >= th:
            decisao = "PROMOVER?" if wr >= BREAK_EVEN else "SUSPENDER?"
            alertas.append(f"[{tag}][RETESTE] {label}: n={n}, WR={wr:.0f}% -> {decisao}")

    conn.close()
    print()
    return alertas


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--conta", choices=["practice", "real", "ambas"], default="ambas")
    args = ap.parse_args()
    contas = list(BANCOS) if args.conta == "ambas" else [args.conta]

    print("=== Vigilante de Amostras ===\n")
    alertas = []
    for conta in contas:
        alertas += vigiar(conta, BANCOS[conta])

    if alertas:
        print("=== ALERTAS ===")
        for a in alertas:
            print(f"  {a}")
    else:
        print("Nenhum threshold atingido ainda.")


if __name__ == "__main__":
    main()
