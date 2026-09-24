"""Vigilante do Monitor Mercado — agente 4.

Verifica atividade recente, entradas validas, WR e silencio do monitor.
Fonte: diario/monitor_mercado/monitor_mercado.sqlite3
"""
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

BANCO = Path(__file__).resolve().parents[2] / "diario/monitor_mercado/monitor_mercado.sqlite3"
BREAK_EVEN_R = 1.0  # TP1 = 1R; exige WR > 50% para ser lucrativo no swing

SILENCIO_ALERTA_H = 4   # horas sem nenhum evento -> alerta
SILENCIO_WARN_H   = 2   # horas sem entrada valida -> aviso

agora = datetime.now(timezone.utc)
corte_24h = (agora - timedelta(hours=24)).isoformat(timespec="seconds")
corte_silencio = (agora - timedelta(hours=SILENCIO_ALERTA_H)).isoformat(timespec="seconds")
corte_warn = (agora - timedelta(hours=SILENCIO_WARN_H)).isoformat(timespec="seconds")

if not BANCO.exists():
    print("ERRO: banco do monitor nao encontrado —", BANCO)
    raise SystemExit(1)

conn = sqlite3.connect(BANCO)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== Vigilante do Monitor Mercado ===")
print(f"Horario: {agora.strftime('%Y-%m-%d %H:%M UTC')}\n")

# --- Atividade total 24h ---
cur.execute(
    "SELECT COUNT(*) n, MAX(quando) ultimo FROM monitor_eventos WHERE quando >= ?",
    (corte_24h,),
)
r = cur.fetchone()
total_24h = r["n"]
ultimo_evento = r["ultimo"]

if not ultimo_evento:
    print("!!! ALERTA: Nenhum evento nas ultimas 24h — monitor pode estar parado.\n")
else:
    ts = datetime.fromisoformat(ultimo_evento.replace("Z", "+00:00"))
    delta_min = int((agora - ts).total_seconds() / 60)
    print(f"Atividade 24h: {total_24h} eventos  |  Ultimo: {ultimo_evento} ({delta_min} min atras)")
    if ultimo_evento < corte_silencio:
        print(f"  !!! ALERTA: Silencio ha mais de {SILENCIO_ALERTA_H}h — verificar se monitor esta rodando.")

# --- Entradas validas 24h ---
cur.execute(
    """SELECT
         COUNT(*) n,
         SUM(CASE WHEN desfecho='win_tp1' THEN 1 ELSE 0 END) wins,
         SUM(CASE WHEN desfecho='loss_sl' THEN 1 ELSE 0 END) losses,
         SUM(CASE WHEN desfecho='aguardando' THEN 1 ELSE 0 END) pendentes,
         SUM(CASE WHEN desfecho LIKE 'ambigu%' THEN 1 ELSE 0 END) ambiguos,
         MAX(quando) ultima
       FROM monitor_eventos
       WHERE entrada_valida=1 AND quando >= ?""",
    (corte_24h,),
)
e = cur.fetchone()
n_ent = e["n"]
wins = e["wins"] or 0
losses = e["losses"] or 0
wl = wins + losses
wr = wins / wl * 100 if wl else 0

print(f"\n[Entradas validas 24h]")
if n_ent == 0:
    print("  Nenhuma entrada valida nas ultimas 24h.")
    if e["ultima"] and e["ultima"] < corte_warn:
        print(f"  Aviso: ultima entrada valida foi ha mais de {SILENCIO_WARN_H}h.")
else:
    outros = n_ent - wins - losses - (e["ambiguos"] or 0) - (e["pendentes"] or 0)
    flag = "OK" if wr >= 50 else "--"
    print(f"  Total: {n_ent}  |  Wins: {wins}  Losses: {losses}  Ambiguos: {e['ambiguos'] or 0}  Pendentes: {e['pendentes'] or 0}  Outros: {outros}")
    if wl:
        print(f"  WR resolvidos: {wr:.0f}%  {flag}  (break-even swing: 50%)")
    print(f"  Ultima entrada: {e['ultima'] or 'nenhuma'}")

# --- Estudos por tipo 24h ---
cur.execute(
    """SELECT tipo, COUNT(*) n,
         SUM(CASE WHEN desfecho='aguardando' THEN 1 ELSE 0 END) pend
       FROM monitor_eventos
       WHERE entrada_valida=0 AND quando >= ?
       GROUP BY tipo ORDER BY n DESC""",
    (corte_24h,),
)
estudos = cur.fetchall()

print(f"\n[Estudos por tipo 24h]")
if not estudos:
    print("  Nenhum estudo registrado.")
else:
    for r in estudos:
        tipo = r["tipo"] or "sem_tipo"
        print(f"  {tipo:<30} n={r['n']:>4}  pendentes={r['pend']:>3}")

# --- BOF candidatos 24h ---
cur.execute(
    "SELECT COUNT(*) n, COUNT(DISTINCT ativo) ativos FROM monitor_eventos "
    "WHERE tipo='bof_m30_m5_candidato' AND quando >= ?",
    (corte_24h,),
)
bof = cur.fetchone()
print(f"\n[BOF candidatos 24h]")
print(f"  Registros: {bof['n']}  |  Ativos distintos: {bof['ativos']}")
if bof["n"] > 50:
    print("  Aviso: volume alto de candidatos — verificar se dedup esta funcionando.")

# --- Entradas validas por ativo (historico) ---
cur.execute(
    """SELECT ativo,
         SUM(CASE WHEN desfecho='win_tp1' THEN 1 ELSE 0 END) wins,
         SUM(CASE WHEN desfecho='loss_sl' THEN 1 ELSE 0 END) losses
       FROM monitor_eventos
       WHERE entrada_valida=1
       GROUP BY ativo HAVING (wins+losses) >= 5
       ORDER BY (wins*1.0/(wins+losses)) DESC""",
)
por_ativo = cur.fetchall()
if por_ativo:
    print(f"\n[WR por ativo — entradas validas (historico, min n=5)]")
    for r in por_ativo:
        wl2 = r["wins"] + r["losses"]
        wr2 = r["wins"] / wl2 * 100
        flag = "OK" if wr2 >= 50 else "--"
        print(f"  {r['ativo']:<12} {wl2:>4} ops  WR={wr2:>4.0f}%  {flag}")

conn.close()
print()
