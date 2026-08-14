"""
Monitor ao vivo de entradas do bot.
Roda: python monitor_entradas.py
Mostra as últimas operações + avalia timing e qualidade das entradas.
"""

import sqlite3
import json
import time
import os
from datetime import datetime, timezone, timedelta

DB = "iqoption_m5/dados/iqoption_m5_practice.sqlite3"
TIMEFRAME = 300  # M5
MIN_EXPIRACAO = 120  # novo corte: min 2min até expiração
MAX_JANELA = 200    # entrada_max_segundos_no_candle
MARCACAO_ATR = 2.0  # marcacao_tolerancia_atr

def conectar():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def parse_ts(s):
    """Converte string ISO para datetime (sem tz = assume local)."""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def cor(texto, codigo):
    return f"\033[{codigo}m{texto}\033[0m"

def verde(t): return cor(t, "32")
def vermelho(t): return cor(t, "31")
def amarelo(t): return cor(t, "33")
def azul(t): return cor(t, "34")
def cinza(t): return cor(t, "90")
def negrito(t): return cor(t, "1")

def avaliar_entrada(op, decisao_json):
    """Retorna lista de flags de qualidade para uma operação."""
    flags = []

    candle_hora_str = decisao_json.get("candle_hora") if decisao_json else None
    enviada_em_str = op["enviada_em"]

    candle_dt = parse_ts(candle_hora_str)
    enviada_dt = parse_ts(enviada_em_str)

    segundo_no_candle = None
    segundos_ate_expiracao = None

    if candle_dt and enviada_dt:
        diff = (enviada_dt - candle_dt).total_seconds()
        segundo_no_candle = diff % TIMEFRAME
        segundos_ate_expiracao = TIMEFRAME - segundo_no_candle

        if segundo_no_candle > MAX_JANELA:
            flags.append(amarelo(f"FORA DA JANELA ({segundo_no_candle:.0f}s > {MAX_JANELA}s)"))
        if segundos_ate_expiracao < MIN_EXPIRACAO:
            flags.append(vermelho(f"TARDE DEMAIS ({segundos_ate_expiracao:.0f}s até expiração < {MIN_EXPIRACAO}s)"))
        elif segundos_ate_expiracao < 150:
            flags.append(amarelo(f"Apertado ({segundos_ate_expiracao:.0f}s até expiração)"))

    # Checa marcação (proximidade ao nível de S/R)
    detalhes_str = op.get("detalhes_raw") or ""
    try:
        det = json.loads(detalhes_str) if detalhes_str else {}
    except Exception:
        det = {}

    setup = op["setup"] or "?"
    nivel_sr = det.get("nivel_sr")
    atr = det.get("atr", 0)
    preco_entrada = op.get("preco_entrada")  # pode não existir; usa decisao

    if setup in ("sr_rejeicao", "pin_bar_sr", "engulfing_sr") and nivel_sr and atr:
        ref = nivel_sr
        preco = preco_entrada or decisao_json.get("preco") if decisao_json else None
        if preco:
            dist = abs(float(preco) - float(ref))
            limite = MARCACAO_ATR * float(atr)
            if dist > limite:
                flags.append(vermelho(f"MARCAÇÃO INVÁLIDA dist={dist:.5f} > {limite:.5f} ({MARCACAO_ATR}×ATR)"))
            else:
                pct = dist / limite * 100
                if pct > 80:
                    flags.append(amarelo(f"Perto do limite marcação ({pct:.0f}% do máximo)"))

    return flags, segundo_no_candle, segundos_ate_expiracao

def obter_ultimas_operacoes(conn, minutos=15, seen_ids=None):
    """Retorna operações dos últimos N minutos, excluindo IDs já vistos."""
    desde = (datetime.now() - timedelta(minutes=minutos)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()
    cur.execute("""
        SELECT
            o.id_ordem, o.ativo, o.direcao, o.enviada_em, o.encerrada_em,
            o.valor, o.payout, o.setup, o.lucro, o.resultado_bruto, o.status,
            d.candle_hora, d.preco, d.detalhes_json as detalhes_raw, d.motivo_estrategia
        FROM operacoes o
        LEFT JOIN decisoes d ON (
            d.ativo = o.ativo
            AND d.permitida = 1
            AND d.candle_hora = (
                SELECT candle_hora FROM decisoes
                WHERE ativo = o.ativo AND permitida = 1
                ORDER BY id DESC LIMIT 1
            )
        )
        WHERE o.enviada_em >= ?
        ORDER BY o.enviada_em DESC
        LIMIT 30
    """, (desde,))
    rows = cur.fetchall()
    if seen_ids is None:
        return rows
    return [r for r in rows if r["id_ordem"] not in seen_ids]

def obter_bloqueios_recentes(conn, minutos=5):
    """Retorna decisões bloqueadas recentes — verifica se os novos filtros estão atuando."""
    desde = (datetime.now() - timedelta(minutes=minutos)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()
    cur.execute("""
        SELECT ativo, candle_hora, direcao, motivo_estrategia, motivo_risco, detalhes_json
        FROM decisoes
        WHERE registrado_em >= ? AND permitida = 0
        ORDER BY registrado_em DESC
        LIMIT 20
    """, (desde,))
    return cur.fetchall()

def imprimir_operacao(op, novo=False):
    marca = verde("● NOVO") if novo else cinza("○")
    resultado = op["resultado_bruto"] or op["status"] or "?"
    cor_res = verde if resultado == "win" else (vermelho if resultado == "loss" else cinza)
    lucro = op["lucro"]
    lucro_str = (verde(f"+{lucro:.2f}") if lucro and lucro > 0 else vermelho(f"{lucro:.2f}") if lucro else cinza("?"))

    try:
        det = json.loads(op["detalhes_raw"] or "{}")
    except Exception:
        det = {}

    flags, seg_candle, seg_exp = avaliar_entrada(op, {
        "candle_hora": op["candle_hora"],
        "preco": op["preco"],
    })

    timing = ""
    if seg_candle is not None:
        timing = cinza(f" [{seg_candle:.0f}s→{seg_exp:.0f}s exp]")

    print(f"  {marca} {negrito(op['ativo'])} {azul(op['direcao'].upper())} "
          f"{op['setup'] or '?'} | {cor_res(resultado)} {lucro_str}{timing}")
    print(f"     entrada: {op['enviada_em']}  candle: {op['candle_hora'] or '?'}")
    for f in flags:
        print(f"     ⚠ {f}")

def loop_monitor(intervalo=90):
    seen_ids = set()
    primeira_vez = True

    print(negrito("\n=== Monitor de Entradas IQ Option M5 ==="))
    print(cinza(f"Banco: {DB}"))
    print(cinza(f"Verificando a cada {intervalo}s | Ctrl+C para parar\n"))

    while True:
        agora = datetime.now().strftime("%H:%M:%S")
        try:
            conn = conectar()

            # Operações novas
            if primeira_vez:
                ops = obter_ultimas_operacoes(conn, minutos=30)
                print(negrito(f"[{agora}] Últimas 30 min de operações:"))
            else:
                ops = obter_ultimas_operacoes(conn, minutos=intervalo//60 + 3, seen_ids=seen_ids)
                if ops:
                    print(negrito(f"\n[{agora}] {len(ops)} nova(s) entrada(s):"))
                else:
                    print(cinza(f"[{agora}] Sem novas entradas."))

            for op in ops:
                imprimir_operacao(op, novo=not primeira_vez)
                seen_ids.add(op["id_ordem"])

            # Bloqueios recentes (mostra só na primeira vez ou se novo)
            bloqueios = obter_bloqueios_recentes(conn, minutos=5)
            if bloqueios:
                print(cinza(f"\n  Bloqueios últimos 5min ({len(bloqueios)}):"))
                for b in bloqueios[:5]:
                    motivo = b["motivo_risco"] or b["motivo_estrategia"] or "?"
                    try:
                        det = json.loads(b["detalhes_json"] or "{}")
                        setup = det.get("setup", "?")
                    except Exception:
                        setup = "?"
                    print(cinza(f"    {b['ativo']} {b['direcao']} {setup} → {motivo}"))

            conn.close()
            primeira_vez = False

        except Exception as e:
            print(vermelho(f"[{agora}] Erro: {e}"))

        print()
        time.sleep(intervalo)

if __name__ == "__main__":
    try:
        loop_monitor(intervalo=90)
    except KeyboardInterrupt:
        print("\nMonitor encerrado.")
