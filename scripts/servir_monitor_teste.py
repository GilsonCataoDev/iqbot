"""Servidor local de teste para o Monitor Mercado.

Serve o HTML com um mercado.json simulado para validação visual sem
precisar de conexão com a IQ Option. Porta 8779 para não colidir com
o monitor real (8777).
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

PORTA = 8779
RAIZ = Path(tempfile.gettempdir()) / "monitor_teste_8779"
RAIZ.mkdir(parents=True, exist_ok=True)

# ── HTML ────────────────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor_mercado

(RAIZ / "index.html").write_text(monitor_mercado._HTML, encoding="utf-8")

# ── JSON simulado ───────────────────────────────────────────────────────────
MOCK_ATIVOS = {
    "EURUSD": {
        "classe": "forex", "disponivel": True, "preco": 1.08432,
        "estado_entrada": "ENTRADA VÁLIDA", "motivo_entrada": "BUY: falso rompimento de baixa após acumulação, na janela testada.",
        "sinal": True, "entrada_valida": True,
        "direcao": "buy", "regime": "LATERAL", "r2": 0.72,
        "pos_canal": 35.2, "atr_pips": 8.4, "unidade_movimento": "pips",
        "acumulacao": True, "rompeu_piso": True, "aperto": 0.91,
        "dist_atr": 0.0, "prox": 100, "hora": 21, "janela_boa": True,
        "vela": "2026-09-04 21:30:00",
        "dia": {"dir": "baixa", "pct": -0.12, "ema": "baixa"},
        "alvos": {"entrada": 1.08432, "sl": 1.08348, "tp1": 1.08642, "tp2": 1.08852, "risco_pips": 8.4, "risco_unidade": "pips"},
        "alvos_estudo": {"entrada": 1.08432, "sl": 1.08348, "tp1": 1.08642, "tp2": 1.08852, "risco_pips": 8.4, "risco_unidade": "pips"},
        "fibo": {"disponivel": False, "estado": "SEM ALINHAMENTO H1/M15"},
        "fluxo": {"disponivel": True, "estado": "REJEIÇÃO NA VAL", "direcao": "buy", "sessao": "NOVA YORK", "qualidade": 75,
                  "vwap": 1.08410, "poc": 1.08390, "vah": 1.08460, "val": 1.08350,
                  "m15": "baixa", "h1": "baixa", "sinal_estudo": True,
                  "eficiencia": 0.82, "eficiencia_estado": "MOVIMENTO EFICIENTE",
                  "volume_relativo": 1.35, "movimento_atr": 0.75, "rr": 1.8,
                  "fonte_volume": "tick-volume M15",
                  "alvos": {"entrada": 1.08432, "sl": 1.08310, "tp1": 1.08390, "tp2": 1.08350, "risco_pips": 12.2, "risco_unidade": "pips"},
                  "motivo": "Confluência de rejeição, tendência e VWAP; registrar em sombra.",
                  "checklist": [
                      {"nome": "Rejeição em VAH/VAL", "ok": True}, {"nome": "M15 alinhado", "ok": True},
                      {"nome": "H1 alinhado", "ok": True}, {"nome": "Preço do lado correto da VWAP", "ok": True},
                      {"nome": "R:R até POC ≥ 1.2", "ok": True},
                  ]},
        "orb": {"disponivel": True, "sessao": "NOVA YORK", "estado": "DENTRO DA ORB",
                "motivo": "Aguardar fechamento além da máxima ou mínima da abertura.",
                "direcao": "neutro", "orh": 1.08510, "orl": 1.08380,
                "fvg": False, "sinal_estudo": False, "h1": "baixa", "h4": "baixa",
                "checklist": [
                    {"nome": "Fechamento fora da ORB", "ok": False}, {"nome": "FVG M15 de 3 candles", "ok": False},
                    {"nome": "H1 alinhado", "ok": False}, {"nome": "H4 alinhado", "ok": False},
                    {"nome": "Alvo 2R calculado", "ok": False},
                ]},
        "liquidez": {"disponivel": False, "sinal_estudo": False, "estado": "AGUARDAR VARREDURA", "motivo": "O piso das dez velas anteriores ainda não foi varrido."},
        "dossie": {"tipo": "baixa", "corpo_pct": 62.0, "pavio_sup_pct": 20.0, "pavio_inf_pct": 18.0,
                   "range_atr": 0.95, "movimento_3_atr": -0.82, "atr_relativo": 1.12, "volume_relativo": 1.35,
                   "tags": ["vela_baixa", "vela_forte", "volatilidade_normal", "movimento_baixa"],
                   "noticia": {"estado": "sem_risco", "texto": "Sem evento relevante na janela de risco."}},
        "checklist": [
            {"nome": "Acumulação: amplitude das 10 velas comprimida", "ok": True},
            {"nome": "Fechamento abaixo do piso das 10 velas", "ok": True},
            {"nome": "Janela validada para o ativo", "ok": True},
        ],
    },
    "GBPUSD": {
        "classe": "forex", "disponivel": True, "preco": 1.26815,
        "estado_entrada": "AGUARDAR", "motivo_entrada": "Ainda não há falso rompimento completo.",
        "sinal": False, "entrada_valida": False,
        "regime": "CANAL_ALTA", "r2": 0.81, "pos_canal": 58.4, "atr_pips": 11.2,
        "unidade_movimento": "pips", "acumulacao": True, "rompeu_piso": False,
        "aperto": 0.87, "dist_atr": 1.3, "prox": 35, "hora": 21, "janela_boa": True,
        "dia": {"dir": "alta", "pct": 0.18, "ema": "alta"},
        "alvos": None, "alvos_estudo": None,
        "fibo": {"disponivel": False, "estado": "SEM IMPULSO LIMPO", "motivo": "A última perna não alcançou 1.5 ATR."},
        "fluxo": {"disponivel": False, "estado": "FLUXO INDISPONÍVEL", "motivo": "São necessários candles e ATR válidos."},
        "orb": {"disponivel": False, "estado": "FORA DA JANELA ORB", "motivo": "O estudo observa Londres e Nova York."},
        "liquidez": {"disponivel": False, "estado": "AGUARDAR VARREDURA", "motivo": "Aguardando estrutura."},
        "checklist": [
            {"nome": "Acumulação: amplitude das 10 velas comprimida", "ok": True},
            {"nome": "Fechamento abaixo do piso das 10 velas", "ok": False},
            {"nome": "Janela validada para o ativo", "ok": True},
        ],
    },
    "BTCUSD": {
        "classe": "crypto", "disponivel": True, "preco": 58240.0,
        "estado_entrada": "ESTUDO — NÃO OPERAR", "motivo_entrada": "O falso rompimento apareceu fora da janela testada; registrar, não executar.",
        "sinal": True, "entrada_valida": False,
        "regime": "TENDENCIA", "r2": 0.91, "pos_canal": 22.5, "atr_pips": 620.0,
        "unidade_movimento": "USD", "acumulacao": True, "rompeu_piso": True,
        "aperto": 0.78, "dist_atr": -0.1, "prox": 65, "hora": 14, "janela_boa": False,
        "dia": {"dir": "baixa", "pct": -1.82, "ema": "baixa"},
        "alvos": None,
        "alvos_estudo": {"entrada": 58240.0, "sl": 57620.0, "tp1": 59790.0, "tp2": 61340.0, "risco_pips": 620.0, "risco_unidade": "USD"},
        "fibo": {"disponivel": False, "estado": "SEM ALINHAMENTO H1/M15"},
        "fluxo": {"disponivel": True, "estado": "ACEITAÇÃO ABAIXO DA ÁREA DE VALOR", "direcao": "sell",
                  "sessao": "NOVA YORK", "qualidade": 55, "vwap": 58850.0, "poc": 58600.0,
                  "vah": 59100.0, "val": 58300.0, "m15": "baixa", "h1": "baixa",
                  "sinal_estudo": False, "eficiencia": 0.55, "eficiencia_estado": "MOVIMENTO EQUILIBRADO",
                  "volume_relativo": 0.95, "movimento_atr": 0.60, "rr": 0.0,
                  "fonte_volume": "tick-volume M15",
                  "alvos": None,
                  "motivo": "Leitura de contexto; aguarde uma rejeição alinhada com R:R suficiente.",
                  "checklist": [
                      {"nome": "Rejeição em VAH/VAL", "ok": False}, {"nome": "M15 alinhado", "ok": True},
                      {"nome": "H1 alinhado", "ok": True}, {"nome": "Preço do lado correto da VWAP", "ok": False},
                      {"nome": "R:R até POC ≥ 1.2", "ok": False},
                  ]},
        "orb": {"disponivel": False, "motivo": "Cripto funciona 24/7; ORB Londres/NY não será inventado para esta classe."},
        "liquidez": {"disponivel": False, "estado": "AGUARDAR VARREDURA", "motivo": "Aguardando estrutura."},
        "checklist": [
            {"nome": "Acumulação: amplitude das 10 velas comprimida", "ok": True},
            {"nome": "Fechamento abaixo do piso das 10 velas", "ok": True},
            {"nome": "Janela validada para o ativo", "ok": False},
        ],
    },
    "XAUUSD": {
        "classe": "ouro", "disponivel": True, "preco": 2318.45,
        "estado_entrada": "AGUARDAR", "motivo_entrada": "Ainda não há falso rompimento completo.",
        "sinal": False, "entrada_valida": False,
        "regime": "CANAL_ALTA", "r2": 0.65, "pos_canal": 70.1, "atr_pips": 12.3,
        "unidade_movimento": "pontos", "acumulacao": False, "rompeu_piso": False,
        "aperto": 1.21, "dist_atr": 2.8, "prox": 5, "hora": 14, "janela_boa": False,
        "dia": {"dir": "alta", "pct": 0.45, "ema": "alta"},
        "alvos": None, "alvos_estudo": None,
        "fibo": {"disponivel": True, "direcao": "buy", "m15": "alta", "h1": "alta",
                 "estado": "NA ZONA — ESPERAR REJEIÇÃO",
                 "motivo": "Preço chegou à retração; ainda falta candle de confirmação.",
                 "qualificada": False, "na_zona": True, "confirmou": False, "rr": 0.0,
                 "zona_inf": 2308.50, "zona_sup": 2314.20, "fib382": 2314.20, "fib500": 2311.35,
                 "fib618": 2308.50, "fib786": 2304.80, "impulso_inicio": "2026-09-03 08:00:00",
                 "impulso_fim": "2026-09-04 10:00:00", "amplitude_atr": 2.15,
                 "alvos": {"entrada": 2308.50, "sl": 2303.96, "tp1": 2326.80, "tp2": 2331.77, "tp3": 2338.84, "risco_pips": 452.0, "risco_unidade": "pontos", "modo_entrada": "limite"},
                 "checklist": [
                     {"nome": "Tendência H1 e M15 alinhadas", "ok": True},
                     {"nome": "Impulso limpo de pelo menos 1.5 ATR", "ok": True},
                     {"nome": "Preço na zona Fibo 38.2%–61.8%", "ok": True},
                     {"nome": "Rejeição a favor da tendência", "ok": False},
                     {"nome": "R:R até TP1 de pelo menos 1.5", "ok": False},
                     {"nome": "Sem notícia de alto impacto na janela", "ok": True},
                 ]},
        "fluxo": {"disponivel": False, "estado": "FLUXO INDISPONÍVEL", "motivo": "Aguardando sessão ativa."},
        "orb": {"disponivel": True, "sessao": "NOVA YORK", "estado": "DENTRO DA ORB",
                "motivo": "Aguardar fechamento além da máxima ou mínima da abertura.",
                "direcao": "neutro", "orh": 2320.5, "orl": 2315.2, "fvg": False, "sinal_estudo": False,
                "h1": "alta", "h4": "alta",
                "checklist": [
                    {"nome": "Fechamento fora da ORB", "ok": False}, {"nome": "FVG M15 de 3 candles", "ok": False},
                    {"nome": "H1 alinhado", "ok": True}, {"nome": "H4 alinhado", "ok": True},
                    {"nome": "Alvo 2R calculado", "ok": False},
                ]},
        "liquidez": {"disponivel": False, "estado": "SEM VARREDURA", "motivo": "Ouro não é forex; sem validação própria."},
        "checklist": [],
    },
}

MOCK_HISTORICO = [
    {
        "id": "falso_rompimento:EURUSD:2026-09-04 21:15:00",
        "tipo": "falso_rompimento", "ativo": "EURUSD", "classe": "forex",
        "direcao": "buy", "quando": "2026-09-04T21:15:00+00:00",
        "vela": "2026-09-04 21:15:00", "preco": 1.08398,
        "regime": "LATERAL", "hora": 21, "janela_boa": True,
        "entrada_valida": True, "estado_entrada": "ENTRADA VÁLIDA",
        "motivo_entrada": "BUY: falso rompimento de baixa após acumulação, na janela testada.",
        "alvos": {"entrada": 1.08398, "sl": 1.08314, "tp1": 1.08608, "tp2": 1.08818, "risco_pips": 8.4, "risco_unidade": "pips"},
        "alvos_estudo": {"entrada": 1.08398, "sl": 1.08314, "tp1": 1.08608, "tp2": 1.08818, "risco_pips": 8.4, "risco_unidade": "pips"},
        "resultado": "subiu", "var_pips": 12.3, "var_unidade": "pips",
        "simulacao": {"desfecho": "win_tp1", "versao": 2, "bateu_tp1_sem_stop": True,
                      "primeiro_toque": "tp1", "vela_desfecho": "2026-09-04 21:45:00",
                      "preco_saida": 1.08608, "resultado_r": 2.5, "preenchida": True},
        "checklist": [
            {"nome": "Acumulação", "ok": True}, {"nome": "Fechamento abaixo do piso", "ok": True}, {"nome": "Janela validada", "ok": True},
        ],
        "dossie": {"tipo": "baixa", "corpo_pct": 68.0, "pavio_sup_pct": 15.0, "pavio_inf_pct": 17.0,
                   "range_atr": 0.88, "movimento_3_atr": -0.91, "atr_relativo": 1.05, "volume_relativo": 1.42,
                   "tags": ["vela_baixa", "vela_forte", "volatilidade_normal", "movimento_baixa"],
                   "noticia": {"estado": "sem_risco", "texto": "Sem evento relevante."}},
        "comparaveis": {"amostra": 8, "wins": 5, "losses": 3, "winrate": 62.5},
    },
    {
        "id": "fibo_m15:XAUUSD:2026-09-04 10:00:00",
        "tipo": "fibo_m15", "ativo": "XAUUSD", "classe": "ouro",
        "direcao": "buy", "quando": "2026-09-04T10:00:00+00:00",
        "vela": "2026-09-04 10:00:00", "preco": 2310.20,
        "regime": "CANAL_ALTA", "hora": 10, "janela_boa": False,
        "entrada_valida": False, "estado_entrada": "FIBO — ESTUDO",
        "motivo_entrada": "Zona tocada, rejeição confirmada e R:R mínimo de 1.5.",
        "alvos": None,
        "alvos_estudo": {"entrada": 2308.50, "sl": 2303.96, "tp1": 2326.80, "tp2": 2331.77, "tp3": 2338.84, "risco_pips": 452.0, "risco_unidade": "pontos", "modo_entrada": "limite"},
        "resultado": None, "var_pips": None,
        "simulacao": {"desfecho": "aguardando", "versao": 2, "bateu_tp1_sem_stop": None,
                      "primeiro_toque": None, "vela_desfecho": None, "preco_saida": None, "resultado_r": None},
        "checklist": [
            {"nome": "Tendência H1 e M15 alinhadas", "ok": True},
            {"nome": "Impulso limpo de pelo menos 1.5 ATR", "ok": True},
            {"nome": "Preço na zona Fibo 38.2%–61.8%", "ok": True},
            {"nome": "Rejeição a favor da tendência", "ok": True},
            {"nome": "R:R até TP1 de pelo menos 1.5", "ok": True},
            {"nome": "Sem notícia de alto impacto na janela", "ok": True},
        ],
        "dossie": {"tipo": "alta", "corpo_pct": 55.0, "pavio_sup_pct": 18.0, "pavio_inf_pct": 27.0,
                   "range_atr": 1.02, "movimento_3_atr": 0.65, "atr_relativo": 0.98, "volume_relativo": None,
                   "tags": ["vela_alta", "vela_forte", "volatilidade_normal", "movimento_alta"],
                   "noticia": {"estado": "sem_risco", "texto": "Sem evento relevante."}},
        "comparaveis": {"amostra": 0, "wins": 0, "losses": 0, "winrate": None},
    },
    {
        "id": "falso_rompimento:BTCUSD:2026-09-04 08:00:00",
        "tipo": "falso_rompimento", "ativo": "BTCUSD", "classe": "crypto",
        "direcao": "buy", "quando": "2026-09-04T08:00:00+00:00",
        "vela": "2026-09-04 08:00:00", "preco": 58100.0,
        "regime": "TENDENCIA", "hora": 8, "janela_boa": False,
        "entrada_valida": False, "estado_entrada": "ESTUDO — NÃO OPERAR",
        "motivo_entrada": "Fora da janela testada.",
        "alvos": None,
        "alvos_estudo": {"entrada": 58100.0, "sl": 57480.0, "tp1": 59650.0, "tp2": 61200.0, "risco_pips": 620.0, "risco_unidade": "USD"},
        "resultado": "subiu", "var_pips": 520.0, "var_unidade": "USD",
        "simulacao": {"desfecho": "loss_sl", "versao": 2, "bateu_tp1_sem_stop": False,
                      "primeiro_toque": "stop", "vela_desfecho": "2026-09-04 09:30:00",
                      "preco_saida": 57480.0, "resultado_r": -1.0, "preenchida": True},
        "checklist": [
            {"nome": "Acumulação", "ok": True}, {"nome": "Fechamento abaixo do piso", "ok": True}, {"nome": "Janela validada", "ok": False},
        ],
        "dossie": {"tipo": "baixa", "corpo_pct": 45.0, "pavio_sup_pct": 30.0, "pavio_inf_pct": 25.0,
                   "range_atr": 1.15, "movimento_3_atr": -1.05, "atr_relativo": 1.25, "volume_relativo": 1.18,
                   "tags": ["vela_baixa", "vela_fraca", "volatilidade_alta", "movimento_baixa"],
                   "noticia": {"estado": "sem_risco", "texto": "Sem evento relevante."}},
        "comparaveis": {"amostra": 6, "wins": 4, "losses": 2, "winrate": 66.7},
    },
]

MOCK_AMOSTRAS_BASE = {"sinais": 0, "wins": 0, "losses": 0, "expirados": 0, "pendentes": 0,
                       "ambiguos": 0, "nao_executadas": 0, "winrate": None,
                       "ic_95": None, "amostra_suficiente": False, "maturidade": "INSUFICIENTE",
                       "avaliados_r": 0, "saldo_r": None, "media_r": None}

MOCK_PAYLOAD = {
    "ts": int(time.time()),
    "status": "TESTE — dados simulados (sem conexão IQ Option)",
    "schemaVersao": 1,
    "saudeDados": {
        "schema": 1, "armazenamento": "SQLite",
        "eventos": 3, "entradas_validas": 1, "pendentes": 1, "ambiguos": 0,
    },
    "historico": MOCK_HISTORICO,
    "ativos": MOCK_ATIVOS,
    "amostraEntrada": {"sinais": 1, "wins": 1, "losses": 0, "expirados": 0, "pendentes": 0,
                        "ambiguos": 0, "nao_executadas": 0, "winrate": 100.0,
                        "ic_95": [20.6, 100.0], "amostra_suficiente": False, "maturidade": "INSUFICIENTE",
                        "avaliados_r": 1, "saldo_r": 2.5, "media_r": 2.5},
    "amostraFibo": {"sinais": 1, "wins": 0, "losses": 0, "expirados": 0, "pendentes": 1,
                     "ambiguos": 0, "nao_executadas": 0, "winrate": None,
                     "ic_95": None, "amostra_suficiente": False, "maturidade": "INSUFICIENTE",
                     "avaliados_r": 0, "saldo_r": None, "media_r": None},
    "amostraFluxo": {**MOCK_AMOSTRAS_BASE},
    "amostraOrb": {**MOCK_AMOSTRAS_BASE},
    "amostraLiquidez": {**MOCK_AMOSTRAS_BASE},
}

(RAIZ / "mercado.json").write_text(
    json.dumps(MOCK_PAYLOAD, ensure_ascii=False, default=str), encoding="utf-8"
)

# Gráficos vazios para os ativos mock
import numpy as np
for ativo in MOCK_ATIVOS:
    candles = [{"t": int(time.time()) - (59 - i) * 900,
                "o": 1.084 + i * 0.0001 + (0.0002 if i % 3 == 0 else -0.0001),
                "h": 1.084 + i * 0.0001 + 0.0004,
                "l": 1.084 + i * 0.0001 - 0.0003,
                "c": 1.084 + i * 0.0001 + (0.0002 if i % 2 == 0 else -0.0001)}
               for i in range(60)]
    (RAIZ / f"mkt_{ativo}.json").write_text(
        json.dumps({"candles": candles, "sup": [None] * 60, "inf": [None] * 60}),
        encoding="utf-8"
    )

# ── Servidor ────────────────────────────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(RAIZ), **kw)
    def log_message(self, fmt, *args):
        pass

class Servidor(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

with Servidor(("127.0.0.1", PORTA), Handler) as srv:
    url = f"http://127.0.0.1:{PORTA}/index.html"
    print(f"Monitor de teste: {url}")
    print("Ctrl+C para parar.")
    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nParado.")
