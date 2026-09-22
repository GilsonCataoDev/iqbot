"""Relatório diário de performance do Lab EMA — agente 1."""
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict

BANCO = Path(__file__).resolve().parents[2] / "iqoption_m5/dados/iqoption_m5_practice_ema_laboratorio_practice.sqlite3"
BREAK_EVEN = 54.0

ontem = (date.today() - timedelta(days=1)).isoformat()

conn = sqlite3.connect(BANCO)
conn.row_factory = sqlite3.Row
cur = conn.cursor()


def wr(rows):
    w = sum(1 for r in rows if r["lucro"] > 0)
    l = sum(1 for r in rows if r["lucro"] < 0)
    n = w + l
    return w, l, n, w / n * 100 if n else 0


cur.execute(
    "SELECT * FROM operacoes WHERE date(enviada_em) = ? ORDER BY enviada_em",
    (ontem,),
)
ops = cur.fetchall()

fins = [o for o in ops if o["status"] == "finalizada"]
falhas = [o for o in ops if o["status"] == "nao_enviada"]
w, l, n, taxa = wr(fins)
lucro = sum(o["lucro"] for o in fins)

print(f"=== Lab EMA — {ontem} ===")
print(f"Finalizadas: {n}  |  Wins: {w}  Losses: {l}  |  WR: {taxa:.1f}%  |  Lucro: {lucro:+.2f} USD")
if falhas:
    print(f"Nao enviadas (bloqueio ou recusa): {len(falhas)}")

if not fins:
    print("Sem operacoes finalizadas ontem.")
    conn.close()
    raise SystemExit(0)

# Por setup
por_setup = defaultdict(list)
for o in fins:
    por_setup[o["setup"]].append(o)

print("\n[Por setup]")
for setup, rows in sorted(por_setup.items(), key=lambda x: -wr(x[1])[3]):
    sw, sl, sn, stx = wr(rows)
    slucro = sum(r["lucro"] for r in rows)
    flag = "OK" if stx >= BREAK_EVEN else "--"
    print(f"  {setup:<30} {sn:>3} ops  {stx:>5.1f}%  {flag}  {slucro:>+7.2f}")

# Por ativo
por_ativo = defaultdict(list)
for o in fins:
    por_ativo[o["ativo"]].append(o)

print("\n[Por ativo]")
for ativo, rows in sorted(por_ativo.items(), key=lambda x: -wr(x[1])[3]):
    aw, al, an, atx = wr(rows)
    alucro = sum(r["lucro"] for r in rows)
    flag = "OK" if atx >= BREAK_EVEN else "--"
    print(f"  {ativo:<12} {an:>3} ops  {atx:>5.1f}%  {flag}  {alucro:>+7.2f}")

# Filtros que bloquearam ontem
cur.execute(
    """SELECT motivo_risco, COUNT(*) n FROM decisoes
       WHERE date(registrado_em) = ?
         AND motivo_risco IS NOT NULL AND motivo_risco NOT IN ('', 'autorizada')
       GROUP BY motivo_risco ORDER BY n DESC""",
    (ontem,),
)
filtros = cur.fetchall()
if filtros:
    print("\n[Filtros ativos ontem]")
    for r in filtros:
        print(f"  {r['motivo_risco']:<28} n={r['n']}")

conn.close()
