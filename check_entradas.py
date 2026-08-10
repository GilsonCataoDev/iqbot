"""
Snapshot de entradas recentes + avaliação de qualidade.
Uso: python check_entradas.py [minutos=20]
"""

import sqlite3, json, sys
from datetime import datetime, timedelta

DB = "iqoption_m5/dados/iqoption_m5_practice.sqlite3"
TIMEFRAME = 300
MIN_EXPIRACAO = 120
MAX_JANELA = 200
MARCACAO_ATR = 2.0

def parse_ts(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

minutos = int(sys.argv[1]) if len(sys.argv) > 1 else 20
desde = (datetime.now() - timedelta(minutes=minutos)).strftime("%Y-%m-%d %H:%M:%S")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Últimas operações
ops = conn.execute("""
    SELECT id_ordem, ativo, direcao, enviada_em, encerrada_em,
           setup, lucro, resultado_bruto, status,
           hora_sinal, atraso_envio_ms
    FROM operacoes
    WHERE enviada_em >= ?
    ORDER BY enviada_em DESC
""", (desde,)).fetchall()

# Últimas decisões (todas — para ver bloqueios)
decisoes = conn.execute("""
    SELECT ativo, candle_hora, direcao, motivo_estrategia, motivo_risco,
           detalhes_json, permitida, registrado_em
    FROM decisoes
    WHERE registrado_em >= ?
    ORDER BY registrado_em DESC
    LIMIT 40
""", (desde,)).fetchall()

conn.close()

agora = datetime.now().strftime("%H:%M:%S")
print(f"\n=== check_entradas @ {agora} (últimos {minutos}min) ===\n")

# ─── OPERAÇÕES ───
print(f"OPERAÇÕES ({len(ops)}):")
if not ops:
    print("  nenhuma")
else:
    wins = sum(1 for o in ops if o["resultado_bruto"] == "win")
    losses = sum(1 for o in ops if o["resultado_bruto"] == "loss")
    pendentes = sum(1 for o in ops if o["resultado_bruto"] is None)
    lucro_total = sum((o["lucro"] or 0) for o in ops)
    print(f"  {wins}W / {losses}L / {pendentes} pend  lucro={lucro_total:+.2f}\n")

    for op in ops:
        resultado = op["resultado_bruto"] or op["status"] or "pend"
        lucro = op["lucro"]
        lucro_s = f"{lucro:+.2f}" if lucro is not None else "?"
        res_s = f"[{resultado}] {lucro_s}"

        # Tenta calcular timing via decisão correspondente
        timing_s = ""
        flags = []

        # Busca decisão mais próxima do mesmo ativo/horário
        match_d = None
        for d in decisoes:
            if d["ativo"] == op["ativo"] and d["permitida"] == 1:
                dt_d = parse_ts(d["registrado_em"])
                dt_o = parse_ts(op["enviada_em"])
                if dt_d and dt_o and abs((dt_o - dt_d).total_seconds()) < 30:
                    match_d = d
                    break

        if match_d:
            candle_dt = parse_ts(match_d["candle_hora"])
            enviada_dt = parse_ts(op["enviada_em"])
            if candle_dt and enviada_dt:
                diff = (enviada_dt - candle_dt).total_seconds()
                seg = diff % TIMEFRAME
                exp = TIMEFRAME - seg
                timing_s = f"  [{seg:.0f}s no candle, {exp:.0f}s até exp]"
                if exp < MIN_EXPIRACAO:
                    flags.append(f"!! TARDE DEMAIS ({exp:.0f}s < {MIN_EXPIRACAO}s)")
                if seg > MAX_JANELA:
                    flags.append(f"!! FORA DA JANELA ({seg:.0f}s)")

            # Marcação
            try:
                det = json.loads(match_d["detalhes_json"] or "{}")
            except Exception:
                det = {}
            setup = op["setup"] or det.get("setup", "?")
            nivel_sr = det.get("nivel_sr")
            atr = det.get("atr", 0)
            preco = det.get("preco")
            if setup in ("sr_rejeicao", "pin_bar_sr", "engulfing_sr") and nivel_sr and atr and preco:
                dist = abs(float(preco) - float(nivel_sr))
                limite = MARCACAO_ATR * float(atr)
                if dist > limite:
                    flags.append(f"!! MARCAÇÃO INVÁLIDA dist={dist:.5f} > limite={limite:.5f}")

        setup_s = op["setup"] or "?"
        print(f"  {op['enviada_em'][11:19]}  {op['ativo']:<20} {op['direcao'].upper():<5} {setup_s:<25} {res_s}{timing_s}")
        for f in flags:
            print(f"             ⚠ {f}")

# ─── BLOQUEIOS ───
bloqueios = [d for d in decisoes if d["permitida"] == 0]
print(f"\nBLOQUEIOS RECENTES ({len(bloqueios)}):")
if not bloqueios:
    print("  nenhum")
else:
    from collections import Counter
    motivos = Counter(d["motivo_risco"] or d["motivo_estrategia"] or "?" for d in bloqueios)
    for motivo, cnt in motivos.most_common():
        print(f"  {cnt:2}x  {motivo}")

    print()
    for d in bloqueios[:8]:
        try:
            det = json.loads(d["detalhes_json"] or "{}")
            setup = det.get("setup", "?")
        except Exception:
            setup = "?"
        motivo = d["motivo_risco"] or d["motivo_estrategia"] or "?"
        print(f"  {d['registrado_em'][11:19]}  {d['ativo']:<20} {d['direcao']:<5} {setup:<20} -> {motivo}")

print()

# ─── ATRASO DE ENVIO ───
atrasos = [o["atraso_envio_ms"] for o in ops if o["atraso_envio_ms"] is not None]
print(f"ATRASO DE ENVIO (sinal→ordem) — {len(atrasos)} op(s) com dado:")
if not atrasos:
    print("  sem dados (coluna hora_sinal/atraso_envio_ms ausente ou banco antigo)")
else:
    media = sum(atrasos) / len(atrasos)
    atrasos_ord = sorted(atrasos)
    idx_p95 = max(0, int(len(atrasos_ord) * 0.95) - 1)
    p95 = atrasos_ord[idx_p95]
    tardios = sum(1 for a in atrasos if a > 15000)
    print(f"  média={media:.0f}ms  p95={p95}ms  tardios(>15s)={tardios}/{len(atrasos)}")
print()
