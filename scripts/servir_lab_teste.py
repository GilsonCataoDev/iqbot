"""Servidor local de teste para o Lab EMA.

Serve o grafico_web/index.html com dados mock (sem conexão IQ Option)
na porta 8785. Essa porta ativa modoLab automaticamente no frontend.
"""
from __future__ import annotations

import http.server
import json
import datetime as _datetime
import math
import os
import shutil
import socket
import socketserver
import tempfile
import time
import webbrowser
from pathlib import Path

import sys

# Porta 8785 liga modoLab automaticamente; em outra porta o modo vem do
# ?fonte=ema_laboratorio_practice que a URL abaixo ja carrega.
PORTA = int(sys.argv[1]) if len(sys.argv) > 1 else 8785

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iqoption_m5.registro import RegistroSQLite
RAIZ = Path(tempfile.gettempdir()) / f"lab_teste_{PORTA}"
RAIZ.mkdir(parents=True, exist_ok=True)

# ── Copiar index.html do projeto ─────────────────────────────────────────────
SRC_HTML = Path(__file__).resolve().parents[1] / "grafico_web" / "index.html"
(RAIZ / "index.html").write_bytes(SRC_HTML.read_bytes())
_SRC_JS = SRC_HTML.parent / "marcacoes.js"
if _SRC_JS.exists():
    (RAIZ / "marcacoes.js").write_bytes(_SRC_JS.read_bytes())

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
        atraso_ms = 320 + i * 50
        # Timestamps mock em BRT (HH:MM:SS)
        decisao_ts = time.localtime(t - atraso_ms // 1000 - 1)
        enviada_ts = time.localtime(t)
        vencimento_ts = time.localtime(t + exp_min * 60)
        ema9_v  = round(preco_base + 0.0002 * math.sin(i * 0.5), 5)
        ema21_v = round(preco_base - 0.0001 * math.sin(i * 0.3), 5)
        entradas_detalhadas.append({
            "hora": time.strftime("%H:%M", enviada_ts),
            "ativo": "EURUSD",
            "direcao": "call" if ganhou else "put",
            "setup": "ema921_rsi",
            "preco_entrada": round(preco_base + 0.0001 * i, 5),
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
            "sequencia": {
                "decisao_em": time.strftime("%H:%M:%S", decisao_ts),
                "enviada_em": time.strftime("%H:%M:%S", enviada_ts),
                "vencimento_em": time.strftime("%H:%M:%S", vencimento_ts),
                "atraso_ms": atraso_ms,
            },
            "indicadores": {
                "ema9": ema9_v,
                "ema_longa": ema21_v,
                "rsi": round(55 + 15 * math.sin(i * 0.7), 1),
                "atr": round(0.00045 + 0.00005 * (i % 3), 5),
                "corpo_pct": 60 + (i % 4) * 5,
                "tendencia": "alta" if ganhou else "baixa",
                "candle_tipo": "engulf" if i % 3 == 0 else "pin_bar" if i % 3 == 1 else "marubozu",
            },
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

    # Fibo da perna de alta: origem no fundo, extremo no topo.
    # Espelha _finalizar_mapa_fibonacci (preco_nivel = extremo - amplitude*n).
    _fib_origem = preco_base - 0.005
    _fib_extremo = preco_base + 0.003
    _fib_ampl = _fib_extremo - _fib_origem
    _fib_preco = lambda n: round(_fib_extremo - _fib_ampl * n, 5)
    _fib_niveis = [
        {"nivel": n, "preco": _fib_preco(n),
         "papel": "zona" if n in (0.382, 0.5, 0.618) else "extremo" if n in (0.0, 1.0) else "secundario"}
        for n in (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
    ]

    return {
        "fib": _fib_niveis,
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
            "tendencia": "alta", "direcao": "call",
            "estado": "NA ZONA — AGUARDAR REJEIÇÃO",
            "confirmado": False, "tocou_zona": True, "sr_confluente": False,
            "rejeicao": False,
            "origem": _fib_origem, "extremo": _fib_extremo,
            "amplitude": _fib_ampl, "amplitude_atr": 2.1,
            "zona_inf": _fib_preco(0.618), "zona_sup": _fib_preco(0.382),
            "fib786": _fib_preco(0.786),
            "niveis": _fib_niveis,
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
            "ema920_pullback": {
                300: {
                    "total": wins + losses, "vitorias": wins,
                    "winrate": round(wins / (wins + losses) * 100, 1) if wins + losses > 0 else None,
                    "lucro": round(lucro, 2),
                    "ic_95": [44.9, 82.1] if wins + losses >= 5 else None,
                    "amostra_suficiente": (wins + losses) >= 30,
                    "maturidade": "INSUFICIENTE" if (wins + losses) < 30 else "OBSERVAR",
                    "ev": round(lucro / (wins + losses), 3) if wins + losses > 0 else None,
                },
                900: {
                    "total": 6, "vitorias": 3,
                    "winrate": 50.0, "lucro": -0.9,
                    "ic_95": [15.3, 84.7], "amostra_suficiente": False,
                    "maturidade": "INSUFICIENTE", "ev": -0.150,
                },
            },
            "ema921_rsi": {
                300: {
                    "total": 35, "vitorias": 22,
                    "winrate": 62.9, "lucro": 3.25,
                    "ic_95": [45.1, 77.7], "amostra_suficiente": True,
                    "maturidade": "OBSERVAR", "ev": 0.093,
                },
            },
        },
        "analiseAtraso": {
            "ema921_rsi": {
                300: {
                    "rapido": {"n": 7,  "wins": 5, "winrate": 71.4, "media_ms": 280},
                    "lento":  {"n": 6,  "wins": 3, "winrate": 50.0, "media_ms": 820},
                },
            },
            "ema920_pullback": {
                300: {
                    "rapido": {"n": 9,  "wins": 6, "winrate": 66.7, "media_ms": 310},
                    "lento":  {"n": 4,  "wins": 2, "winrate": 50.0, "media_ms": 750},
                },
                900: {
                    "rapido": {"n": 4,  "wins": 2, "winrate": 50.0, "media_ms": 290},
                    "lento":  {"n": 2,  "wins": 0, "winrate": 0.0,  "media_ms": 680},
                },
            },
        },
        "precisaoIA": {
            "ACEITAR":  {"n": 14, "wins": 10, "winrate": 71.4},
            "REJEITAR": {"n":  8, "wins":  2, "winrate": 25.0},
            "INCERTO":  {"n":  6, "wins":  3, "winrate": 50.0},
        },
        "desempenhoSimuladoPorSetup": {
            "ema920_pullback": {
                300: {
                    "total": 28, "vitorias": 13,
                    "winrate": 46.4, "lucro": -2.1,
                    "ic_95": [27.9, 65.7], "amostra_suficiente": False,
                    "maturidade": "INSUFICIENTE", "ev": -0.075,
                },
            },
            "ema921_rsi": {
                300: {
                    "total": 42, "vitorias": 19,
                    "winrate": 45.2, "lucro": -3.8,
                    "ic_95": [30.2, 61.0], "amostra_suficiente": True,
                    "maturidade": "OBSERVAR", "ev": -0.090,
                },
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

# ── Snapshot datado de ontem (para testar navegação histórica) ────────────────
_ontem = (_datetime.date.today() - _datetime.timedelta(days=1)).isoformat()
_entradas_ontem = [
    {
        "hora": f"{h:02d}:{m:02d}",
        "ativo": "EURUSD",
        "direcao": "call" if i % 3 != 2 else "put",
        "setup": "ema921_rsi",
        "preco_entrada": 1.08400 + i * 0.0001,
        "expiracao_minutos": 25,
        "status": "fechada",
        "lucro": 0.85 if i % 3 != 2 else -1.0,
        "timeframe": 300,
        "criterios": ["EMA 9 acima da EMA 21", "RSI acima de 50"],
        "comparaveis": {"amostra": 13, "wins": 9, "losses": 4, "winrate": 69.2},
        "leituraM5": {"modo": "sombra", "qualificada": i % 3 != 2, "observacao": "Mock histórico."},
        "sequencia": {
            "decisao_em": f"{h:02d}:{m:02d}:{(i*7)%60:02d}",
            "enviada_em": f"{h:02d}:{m:02d}:{(i*7+1)%60:02d}",
            "vencimento_em": f"{h:02d}:{(m+25)%60:02d}:00",
            "atraso_ms": 300 + i * 40,
        },
        "indicadores": {
            "ema9": round(1.0843 + i * 0.0001, 5),
            "ema_longa": round(1.0830 + i * 0.0001, 5),
            "rsi": round(55 + i * 3.5, 1),
            "atr": 0.00043,
            "corpo_pct": 65,
            "tendencia": "alta",
            "candle_tipo": "engulf",
        },
    }
    for i, (h, m) in enumerate([(9, 5), (10, 15), (11, 20), (13, 0), (14, 30), (15, 45)])
]
_snapshot_ontem = {
    "data": _ontem,
    "entradasDetalhadas": _entradas_ontem,
    "statsGlobais": {
        "normal": {"entradas": 6, "wins": 4, "lucro": 2.40, "finalizadas": 6, "winrate": 66.7},
        "otc": {"entradas": 0, "wins": 0, "lucro": 0.0, "finalizadas": 0, "winrate": None},
    },
}
(PASTA_FONTE / f"historico_{_ontem}.json").write_text(
    json.dumps(_snapshot_ontem, ensure_ascii=False, default=str), encoding="utf-8")

# historico_hoje.json — fonte de precisaoIA e entradas para carregarHistoricoHoje()
_hoje = _datetime.date.today().isoformat()
_historico_hoje = {
    "data": _hoje,
    "entradasDetalhadas": [],
    "statsGlobais": {
        "normal": {"entradas": 9, "wins": 6, "lucro": 4.50, "finalizadas": 9},
        "otc": {"entradas": 2, "wins": 2, "lucro": 1.70, "finalizadas": 2},
    },
    "analiseAtraso": {},
    "precisaoIA": {
        "ACEITAR":  {"n": 14, "wins": 10, "winrate": 71.4},
        "REJEITAR": {"n":  8, "wins":  2, "winrate": 25.0},
        "INCERTO":  {"n":  6, "wins":  3, "winrate": 50.0},
    },
    "alertasDegradacao": {
        "PULLBACK_M5": {"n": 8, "wins": 2, "winrate": 25.0},
    },
    "historicoUltimosDias": [
        {"data": ((_datetime.date.today() - _datetime.timedelta(days=d)).isoformat()),
         "n": [12, 18, 15, 20, 10, 16, 14][d],
         "wins": [7, 12, 9, 14, 4, 10, 9][d],
         "winrate": [58.3, 66.7, 60.0, 70.0, 40.0, 62.5, 64.3][d],
         "lucro": [1.2, 3.4, 1.8, 5.1, -2.0, 2.6, 2.1][d]}
        for d in range(6, -1, -1)
    ],
    "desempenhoSetupHora": {
        "EMA 9/21 RSI": {
            "09": {"n": 8,  "wins": 6, "winrate": 75.0},
            "10": {"n": 12, "wins": 8, "winrate": 66.7},
            "11": {"n": 10, "wins": 5, "winrate": 50.0},
            "14": {"n": 6,  "wins": 2, "winrate": 33.3},
        },
        "PULLBACK_M5": {
            "10": {"n": 5, "wins": 1, "winrate": 20.0},
            "11": {"n": 7, "wins": 2, "winrate": 28.6},
            "15": {"n": 4, "wins": 3, "winrate": 75.0},
        },
    },
    "desempenhoPorHora": {
        "09": {"n": 12, "wins": 8,  "winrate": 66.7},
        "10": {"n": 18, "wins": 12, "winrate": 66.7},
        "11": {"n": 15, "wins": 9,  "winrate": 60.0},
        "12": {"n": 8,  "wins": 3,  "winrate": 37.5},
        "13": {"n": 10, "wins": 4,  "winrate": 40.0},
        "14": {"n": 20, "wins": 14, "winrate": 70.0},
        "15": {"n": 22, "wins": 15, "winrate": 68.2},
        "16": {"n": 14, "wins": 7,  "winrate": 50.0},
        "17": {"n": 9,  "wins": 5,  "winrate": 55.6},
        "18": {"n": 6,  "wins": 2,  "winrate": 33.3},
    },
}
(PASTA_FONTE / "historico_hoje.json").write_text(
    json.dumps(_historico_hoje, ensure_ascii=False, default=str), encoding="utf-8")

print(f"Arquivos gerados em {PASTA_FONTE}")

# ── Servidor ──────────────────────────────────────────────────────────────────
_REGISTRO = RegistroSQLite(RAIZ / "marcacoes_teste.db")

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(RAIZ), **kw)
    def end_headers(self):
        # Sem cache: o index.html e os JSONs mudam a cada reinicio do teste.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()
    def _json(self, corpo, status=200):
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):
        if self.path.split("?")[0] == "/marcacoes":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            self._json(_REGISTRO.marcacoes(
                (q.get("painel") or ["lab"])[0], (q.get("ativo") or [""])[0]))
            return
        if self.path.startswith("/exportar"):
            data = (self.path.split("data=")[1].split("&")[0] if "data=" in self.path
                    else _datetime.date.today().isoformat())
            linhas = ["hora_brt,ativo,setup,direcao,resultado,lucro,veredicto_ia,motivo_ia",
                      "10:05,EURUSD,\"EMA 9/21 RSI\",call,win,0.85,ACEITAR,\"Alinhado com tendência\"",
                      "11:20,EURUSD,\"EMA 9/21 RSI\",put,loss,-1.00,REJEITAR,\"RSI extremo\""]
            corpo = "\n".join(linhas).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="entradas_{data.replace("-","")}.csv"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(corpo)
        else:
            super().do_GET()
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path.startswith("/marcacoes/remover"):
            c = json.loads(body or b"{}")
            self._json({"removido": _REGISTRO.remover_marcacao(c["id"])})
            return
        if self.path.startswith("/marcacoes"):
            c = json.loads(body or b"{}")
            self._json({"id": _REGISTRO.salvar_marcacao(
                painel=c.get("painel", "lab"), ativo=c["ativo"], tipo=c["tipo"],
                preco_a=c["preco_a"], preco_b=c.get("preco_b"),
                tempo_a=c.get("tempo_a"), tempo_b=c.get("tempo_b"),
                direcao=c.get("direcao"), rotulo=c.get("rotulo"))})
            return
        if self.path.startswith("/opiniao_groq"):
            import random
            veredictos = [
                ("ACEITAR", "Sinal alinhado com tendência e RSI neutro."),
                ("REJEITAR", "RSI próximo a zona extrema — risco elevado."),
                ("INCERTO",  "Dados insuficientes para conclusão."),
            ]
            v, m = random.choice(veredictos)
            resp = json.dumps({"veredicto": v, "motivo": m,
                               "modelo": "mock-teste", "latencia_ms": 42}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(204)
            self.end_headers()
    def log_message(self, fmt, *args):
        pass

class Servidor(socketserver.ThreadingTCPServer):
    # SO_REUSEADDR no Windows permite DOIS processos ligarem na mesma porta:
    # o bot real e este mock respondiam alternadamente, dando 404 intermitente.
    # Sem reuse, subir com a porta ocupada falha na hora.
    allow_reuse_address = False
    daemon_threads = True

_sonda = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
if _sonda.connect_ex(("127.0.0.1", PORTA)) == 0:
    _sonda.close()
    raise SystemExit(
        f"Porta {PORTA} ja esta em uso (o bot real provavelmente esta rodando).\n"
        f"Pare o outro processo ou use outra porta antes de subir o mock."
    )
_sonda.close()

with Servidor(("127.0.0.1", PORTA), Handler) as srv:
    url = f"http://127.0.0.1:{PORTA}/index.html?fonte={FONTE}"
    print(f"Lab EMA de teste: {url}")
    print("Ctrl+C para parar.")
    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nParado.")
