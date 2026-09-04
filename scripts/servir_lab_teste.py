"""Servidor local de teste para o Lab EMA.

Serve o grafico_web/index.html com dados mock (sem conexão IQ Option)
na porta 8785. Essa porta ativa modoLab automaticamente no frontend.
"""
from __future__ import annotations

import http.server
import json
import math
import os
import shutil
import socket
import socketserver
import tempfile
import time
import webbrowser
from pathlib import Path

PORTA = 8785
RAIZ = Path(tempfile.gettempdir()) / "lab_teste_8785"
RAIZ.mkdir(parents=True, exist_ok=True)

# ── Copiar index.html do projeto ─────────────────────────────────────────────
SRC_HTML = Path(__file__).resolve().parents[1] / "grafico_web" / "index.html"
(RAIZ / "index.html").write_bytes(SRC_HTML.read_bytes())

# ── Helpers de candles simulados ─────────────────────────────────────────────
def _candles(n: int = 60, preco_base: float = 1.084, tf: int = 300):
    agora = int(time.time())
    inicio = agora - n * tf
    candles = []
    preco = preco_base
    for i in range(n):
        t = inicio + i * tf
        delta = 0.0003 * math.sin(i * 0.3) + 0.0001 * ((i % 5) - 2)
        o = preco
        c = preco + delta
        h = max(o, c) + abs(delta) * 0.5
        l = min(o, c) - abs(delta) * 0.5
        candles.append({"time": t, "open": round(o, 5), "high": round(h, 5),
                         "low": round(l, 5), "close": round(c, 5)})
        preco = c
    return candles

def _serie_vazia(n: int, tf: int = 300) -> list:
    agora = int(time.time())
    return [{"time": agora - (n - 1 - i) * tf, "value": None} for i in range(n)]

def _ema(candles: list, periodo: int) -> list:
    k = 2 / (periodo + 1)
    resultado = []
    ema = candles[0]["close"]
    for c in candles:
        ema = c["close"] * k + ema * (1 - k)
        resultado.append({"time": c["time"], "value": round(ema, 5)})
    return resultado

def _rsi(candles: list, periodo: int = 14) -> list:
    if len(candles) < periodo + 1:
        return [{"time": c["time"], "value": 50.0} for c in candles]
    ganhos, perdas = [], []
    for i in range(1, len(candles)):
        d = candles[i]["close"] - candles[i-1]["close"]
        ganhos.append(max(0, d)); perdas.append(max(0, -d))
    ag = sum(ganhos[:periodo]) / periodo
    ap = sum(perdas[:periodo]) / periodo
    resultado = [{"time": c["time"], "value": None} for c in candles[:periodo]]
    for i in range(periodo, len(candles)):
        ag = (ag * (periodo - 1) + ganhos[i-1]) / periodo
        ap = (ap * (periodo - 1) + perdas[i-1]) / periodo
        rsi = 100 - 100 / (1 + ag / ap) if ap > 0 else 100.0
        resultado.append({"time": candles[i]["time"], "value": round(rsi, 1)})
    return resultado

def _banda(candles: list, janela: int = 20, desvios: float = 2.0):
    sup, inf, med = [], [], []
    for i, c in enumerate(candles):
        trecho = candles[max(0, i-janela+1):i+1]
        precos = [x["close"] for x in trecho]
        m = sum(precos) / len(precos)
        std = (sum((p - m)**2 for p in precos) / len(precos)) ** 0.5
        med.append({"time": c["time"], "value": round(m, 5)})
        sup.append({"time": c["time"], "value": round(m + desvios * std, 5)})
        inf.append({"time": c["time"], "value": round(m - desvios * std, 5)})
    return sup, inf, med

def _volume(candles: list) -> list:
    return [{"time": c["time"], "value": round(100 + abs(c["close"] - c["open"]) / 0.0001 * 10, 1)}
            for c in candles]

def _ativo_json(preco_base: float, tf: int, tendencia: str, tem_sinal: bool,
                wins: int, losses: int, lucro: float) -> dict:
    candles = _candles(80, preco_base, tf)
    sup, inf, med = _banda(candles)
    ema9 = _ema(candles, 9)
    ema20 = _ema(candles, 21)
    rsi = _rsi(candles)

    agora = int(time.time())
    entradas_detalhadas = []
    horas = [agora - 3600 * (k + 1) for k in range(wins + losses)]
    for i, t in enumerate(horas):
        ganhou = i < wins
        exp_min = tf // 60 * 5
        entradas_detalhadas.append({
            "hora": f"{time.strftime('%H:%M', time.localtime(t))} BRT",
            "ativo": "EURUSD",
            "direcao": "call" if ganhou else "put",
            "setup": "EMA 9/21 RSI",
            "preco_entrada": preco_base + 0.0001 * i,
            "expiracao_minutos": exp_min,
            "status": "fechada",
            "lucro": 0.85 if ganhou else -1.0,
            "timeframe": tf,
            "criterios": [
                "EMA 9 acima da EMA 21",
                "RSI acima de 50",
                f"Tendência {'alta' if ganhou else 'baixa'} confirmada no M5",
            ],
            "comparaveis": {"amostra": wins + losses, "wins": wins,
                             "losses": losses, "winrate": round(wins / (wins + losses) * 100, 1)},
            "leituraM5": {"modo": "sombra", "qualificada": ganhou,
                           "observacao": "Entrada no retorno da EMA 9 após cruzamento confirmado."},
        })

    alerta = {
        "direcao": "call" if tem_sinal else None,
        "setup": "EMA 9/21 RSI",
        "preco": preco_base,
        "mensagem": "Cruzamento EMA 9/21 confirmado com RSI acima de 55." if tem_sinal else "",
        "radarEstado": "ENTRAR" if tem_sinal else "AGUARDAR",
        "fatores": ["EMA 9 cruzou acima da EMA 21", "RSI: 63", "Tendência de alta no M15"],
        "alvoProvavel": round(preco_base + 0.003, 5),
        "rsi": 63,
    } if tem_sinal else None

    return {
        "candles": candles,
        "volume": _volume(candles),
        "bandaSup": sup, "bandaInf": inf, "bandaMedia": med,
        "emaMicro": ema9, "emaMacro": ema20,
        "rsi": rsi,
        "tendenciaMacro": tendencia,
        "timeframeSeg": tf,
        "atualizado_em": agora,
        "sinais": [],
        "entradasDetalhadas": entradas_detalhadas,
        "statsGlobais": {
            "normal": {"entradas": wins + losses, "wins": wins,
                        "lucro": round(lucro, 2), "finalizadas": wins + losses},
            "otc": {"entradas": 0, "wins": 0, "lucro": 0.0, "finalizadas": 0},
        },
        "movimentosUnicos": {
            "movimentos": wins + losses, "vitorias": wins,
            "finalizados": wins + losses, "mistos": 0,
            "winrate": round(wins / (wins + losses) * 100, 1) if wins + losses > 0 else None,
        },
        "alerta": alerta,
        "alertaProximo": None,
        "noticias": [
            {"moeda": "USD", "impacto": "Low", "minutos": 45, "evento": "ISM Manufacturing"},
        ],
        "fibContexto": {
            "direcao": "call", "estado": "NA ZONA — AGUARDAR REJEIÇÃO",
            "confirmado": False, "tocou_zona": True,
            "origem": preco_base - 0.005, "extremo": preco_base + 0.003,
            "inicio_time": agora - 3600, "fim_time": agora - 1800,
        },
        "planoForex": {
            "ativo": "EURUSD", "lado": "COMPRA",
            "entrada": round(preco_base, 5),
            "nivel": round(preco_base - 0.001, 5),
            "tp": round(preco_base + 0.003, 5), "tpPips": 30,
            "sl": round(preco_base - 0.001, 5), "slPips": 10,
            "rr": 3.0,
            "nota": "Entrada no retorno à EMA 9, com R:R de 3:1 até resistência anterior.",
        } if tem_sinal else None,
        "operacoesReais": [],
        "niveis": [
            {"preco": round(preco_base + 0.005, 5), "tipo": "resistencia"},
            {"preco": round(preco_base - 0.004, 5), "tipo": "suporte"},
        ],
        "desempenhoPorSetup": {
            "pullback": {
                "total": wins + losses,
                "winrate": round(wins / (wins + losses) * 100, 1) if wins + losses > 0 else None,
                "lucro": round(lucro, 2),
            },
            "pullback_confluencia": {
                "total": 8, "winrate": 50.0, "lucro": -0.4,
            },
            "sr_rejeicao": {
                "total": 5, "winrate": 60.0, "lucro": 1.25,
            },
        },
    }

# ── Gerar JSONs dos ativos ────────────────────────────────────────────────────
FONTE = "ema_laboratorio_practice"
PASTA_FONTE = RAIZ / FONTE
PASTA_FONTE.mkdir(parents=True, exist_ok=True)

ATIVOS = [
    {"id": "EURUSD",  "label": "EUR/USD",  "preco": 1.08432, "tend": "alta",  "sinal": True,  "w": 9,  "l": 4,  "lucro": 5.65},
    {"id": "GBPUSD",  "label": "GBP/USD",  "preco": 1.26815, "tend": "alta",  "sinal": False, "w": 5,  "l": 3,  "lucro": 1.25},
    {"id": "USDJPY",  "label": "USD/JPY",  "preco": 147.32,  "tend": "baixa", "sinal": False, "w": 3,  "l": 5,  "lucro": -2.15},
    {"id": "AUDUSD",  "label": "AUD/USD",  "preco": 0.6512,  "tend": "baixa", "sinal": False, "w": 2,  "l": 2,  "lucro": 0.0},
]

manifesto = {"ativos": [{"id": a["id"], "label": a["label"]} for a in ATIVOS]}
(PASTA_FONTE / "manifest.json").write_text(
    json.dumps(manifesto, ensure_ascii=False), encoding="utf-8")

for a in ATIVOS:
    dados = _ativo_json(a["preco"], 300, a["tend"], a["sinal"], a["w"], a["l"], a["lucro"])
    (PASTA_FONTE / f"{a['id']}.json").write_text(
        json.dumps(dados, ensure_ascii=False, default=str), encoding="utf-8")

print(f"Arquivos gerados em {PASTA_FONTE}")

# ── Servidor ──────────────────────────────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(RAIZ), **kw)
    def do_POST(self):
        # absorve POST /toggle sem erro
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(204)
        self.end_headers()
    def log_message(self, fmt, *args):
        pass

class Servidor(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

with Servidor(("127.0.0.1", PORTA), Handler) as srv:
    url = f"http://127.0.0.1:{PORTA}/index.html?fonte={FONTE}"
    print(f"Lab EMA de teste: {url}")
    print("Ctrl+C para parar.")
    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nParado.")
