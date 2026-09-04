"""APOSENTADO EM 02/09/2026 — use monitor_mercado.py (porta 8777).

Motivo: quatro parametros deste arquivo foram MEDIDOS e estao errados.

  1. SEM FILTRO DE HORARIO. A configuracao que ele roda so e positiva entre
     21-22h UTC (+0.076R a +0.212R). Em todas as horas juntas o EV e NEGATIVO
     (-0.098R a -0.022R) em todas as 8 geometrias testadas. Pior: a linha
     "melhor_horario_utc: 13:00-16:00" esta INVERTIDA — o bloco NY 13-17h e o
     de pior desempenho (50.50%).

  2. ALVO = meio do range (+0.2932R) e um dos piores. TP de 2.5 ATR da
     +0.4126R, 41% melhor. A correcao vai mais longe que os niveis estruturais.

  3. SPREAD configurado 2-6x acima do real. SPREAD_PADRAO=1.2 pips; medido ao
     vivo via get_realtime_candles: EURUSD 0.20 pip. O avaliar_execucao marca
     "CUIDADO" em situacoes que estao baratas.

  4. BUF_SL_ATR=0.50; a varredura de geometria deu melhor com SL de 1.0 ATR.

Mantido no repositorio como referencia do calendario de noticias e do
planos_pullback_h1_nova_york, que NAO foram testados pelo motor corrigido.
Se for reaproveitar algo, valide antes — ver iqoption-bugs-metodologia-backtest.

--- documentacao original abaixo ---

Monitor de sinais Forex — falso rompimento BUY no M15.

Fluxo semi-automatizado:
  1. Bot detecta sinal (acumulacao + falso rompimento para baixo)
  2. Alert no grafico com entry, SL, TP e R:R
  3. Voce abre manualmente no app IQ Option com esses precos
  4. IQ fecha automaticamente no SL/TP nativo
  5. Bot rastreia posicoes abertas via get_positions("forex")

Uso:
    call .env.bat && python monitor_forex.py

Porta padrao: 8775
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.grafico import GraficoM5
from iqoption_m5.mercado_iq import MercadoIQ
from iqoption_m5.noticias import CalendarioEconomico
from iqoption_m5.forex_estrategia import planos_pullback_h1_nova_york

ATIVOS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD"]
JAN = 10           # janela de acumulacao (candles)
BUF_SL_ATR = 0.50  # buffer do SL em multiplos do ATR14
RR_MIN = 2.0       # R:R minimo para gerar sinal
MIN_HIST_REGIME = 200  # candles passados antes de classificar o quartil de amplitude
PORTA = 8775
INTERVALO_POLL_S = 30   # checa novo candle M15 a cada 30s
INTERVALO_POS_S = 60    # atualiza posicoes abertas a cada 60s
INTERVALO_TICK_S = 1.0  # redesenha o candle atual com o stream local da IQ
TIMEFRAME_S = 900
SPREAD = {"EURJPY": 0.015, "USDJPY": 0.015, "AUDUSD": 0.00015, "NZDUSD": 0.00015}
SPREAD_PADRAO = 0.00012
CRIPTO_PREFIXOS = ("BTC", "ETH", "LTC", "XRP", "BCH", "EOS", "ETC", "DASH", "TRX", "ZEC", "ADA", "SOL", "DOT", "LINK", "UNI", "DOGE")


def eh_cripto(ativo: str) -> bool:
    return ativo.upper().replace("-OTC", "").startswith(CRIPTO_PREFIXOS)

SESSOES_UTC = {
    "ASIA": (0, 9), "LONDRES": (7, 16), "NOVA YORK": (13, 22),
}
MOEDA_SESSAO = {
    "EUR": "LONDRES", "GBP": "LONDRES", "USD": "NOVA YORK",
    "CAD": "NOVA YORK", "JPY": "ASIA", "AUD": "ASIA", "NZD": "ASIA",
}


def resumir_horario(ativo: str, agora: datetime | None = None) -> dict:
    """Classifica liquidez aproximada do par usando horário UTC, sem prometer volume."""
    agora = agora or datetime.now(timezone.utc)
    hora = agora.hour + agora.minute / 60
    ativas = [nome for nome, (inicio, fim) in SESSOES_UTC.items() if inicio <= hora < fim]
    base, cotacao = ativo[:3].upper(), ativo[3:6].upper()
    sessao_base, sessao_cotacao = MOEDA_SESSAO.get(base), MOEDA_SESSAO.get(cotacao)
    sobreposicao = len(ativas) >= 2
    if 21 <= hora < 22 or hora >= 22 or hora < 0.5:
        nivel, nota = "PERIGOSO", "Rollover/virada do dia: spread e execução podem piorar."
    elif sobreposicao:
        nivel, nota = "MELHOR", "Sobreposição de sessões: liquidez normalmente maior."
    elif sessao_base in ativas or sessao_cotacao in ativas:
        nivel, nota = "ATIVO", "Sessão de uma das moedas está ativa."
    else:
        nivel, nota = "LENTO", "Fora das sessões principais: espere confirmação mais forte."
    return {"ativo": ativo, "nivel": nivel, "sessoes_ativas": ativas,
            "moedas": f"{base}/{cotacao}", "melhor_horario_utc": "13:00–16:00",
            "nota": nota, "hora_utc": agora.strftime("%H:%M")}


def avaliar_execucao(ativo: str, agora: datetime | None = None) -> dict:
    """Aviso conservador de custo de execução usando spread estimado configurado."""
    if eh_cripto(ativo):
        return {"ativo": ativo, "spread_estimado_pips": None, "limite_pips": None,
                "status": "INFO", "unidade": "USD",
                "nota": "Spread varia por ativo; confirme o custo diretamente na IQ Option."}
    spread = SPREAD.get(ativo, SPREAD_PADRAO)
    fator = 100 if "JPY" in ativo else 10000
    pips = spread * fator
    limite = 2.0 if "JPY" in ativo else 1.5
    return {"ativo": ativo, "spread_estimado_pips": round(pips, 2),
            "limite_pips": limite, "status": "CUIDADO" if pips > limite else "OK",
            "unidade": "pips",
            "nota": "Spread estimado alto; confirme cotação antes de entrar." if pips > limite
                    else "Spread estimado dentro do limite configurado."}


# ---------------------------------------------------------------------------
# Modelo de sinal
# ---------------------------------------------------------------------------

@dataclass
class SinalForex:
    ativo: str
    lado: str           # sempre "buy" nesta estrategia
    entrada: float
    sl: float
    tp: float
    risco: float
    rr: float
    gerado_em: datetime
    candle_rompimento: datetime
    valido_ate: datetime
    preco_atual: float
    rr_atual: float
    acao: str = "ENTRAR_AGORA"
    setup: str = "falso_rompimento_buy"

    @property
    def id_sinal(self) -> str:
        return f"{self.ativo}:{self.setup}:{int(pd.Timestamp(self.candle_rompimento).value // 1_000_000_000)}"

    def to_dict(self) -> dict:
        return {
            "ativo": self.ativo,
            "lado": self.lado,
            "entrada": round(self.entrada, 5),
            "sl": round(self.sl, 5),
            "tp": round(self.tp, 5),
            "risco_pips": round(self.risco * (100 if "JPY" in self.ativo else 10000), 1),
            "rr": round(self.rr, 2),
            "preco_atual": round(self.preco_atual, 5),
            "rr_atual": round(self.rr_atual, 2),
            "acao": self.acao,
            "setup": self.setup,
            "id_sinal": self.id_sinal,
            "gerado_em": self.gerado_em.strftime("%H:%M:%S"),
            "candle_rompimento": self.candle_rompimento.strftime("%Y-%m-%d %H:%M"),
            "valido_ate": self.valido_ate.strftime("%H:%M:%S"),
        }


@dataclass
class LeituraMercado:
    ativo: str
    tendencia: str
    preco: float
    alvo: float
    alvo_alternativo: float
    invalidacao: float
    entrada_min: float
    entrada_max: float
    lado_entrada: str
    confluencias: list[str]
    contexto: str
    qualidade: int
    tendencia_m15: str = "LATERAL"
    rsi14: float = 50.0
    atr_pips: float = 0.0
    suporte: float = 0.0
    resistencia: float = 0.0
    rr_estimado: float = 0.0
    distancia_zona_pips: float = 0.0
    estado_preco: str = "AGUARDAR"

    def to_dict(self) -> dict:
        return {
            "ativo": self.ativo, "tendencia": self.tendencia,
            "preco": round(self.preco, 5), "alvo": round(self.alvo, 5),
            "alvo_alternativo": round(self.alvo_alternativo, 5),
            "invalidacao": round(self.invalidacao, 5),
            "entrada_min": round(self.entrada_min, 5),
            "entrada_max": round(self.entrada_max, 5),
            "lado_entrada": self.lado_entrada,
            "confluencias": self.confluencias, "contexto": self.contexto,
            "qualidade": self.qualidade,
            "tendencia_m15": self.tendencia_m15,
            "rsi14": round(self.rsi14, 1), "atr_pips": round(self.atr_pips, 1),
            "suporte": round(self.suporte, 5), "resistencia": round(self.resistencia, 5),
            "rr_estimado": round(self.rr_estimado, 2),
            "distancia_zona_pips": round(self.distancia_zona_pips, 1),
            "estado_preco": self.estado_preco,
        }


# ---------------------------------------------------------------------------
# Deteccao de sinal
# ---------------------------------------------------------------------------

def _atr14(df: pd.DataFrame) -> pd.Series:
    H, L, C = df["High"], df["Low"], df["Close"]
    tr = pd.concat([H - L, (H - C.shift(1)).abs(), (L - C.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean()


def _spread(ativo: str) -> float:
    return SPREAD.get(ativo, SPREAD_PADRAO)


def analisar_leitura_mercado(
    ativo: str, df: pd.DataFrame, agora: datetime | None = None,
) -> LeituraMercado | None:
    """Resume direção, zonas e alvo sem transformar a leitura em entrada."""
    agora = agora or datetime.now(timezone.utc)
    i = _indice_ultimo_fechado(df, agora)
    if i is None or i < 200:
        return None
    fechados = df.iloc[:i + 1].copy()
    atr = float(_atr14(fechados).iloc[-1])
    if not np.isfinite(atr) or atr <= 0:
        return None
    preco = float(fechados["Close"].iloc[-1])
    ema20 = fechados["Close"].ewm(span=20, adjust=False).mean()
    ema50 = fechados["Close"].ewm(span=50, adjust=False).mean()
    h1 = fechados.resample("1h", label="right", closed="left").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last",
    }).dropna()
    if len(h1) < 55:
        return None
    e20h = h1["Close"].ewm(span=20, adjust=False).mean()
    e50h = h1["Close"].ewm(span=50, adjust=False).mean()
    inclinacao = float(e20h.iloc[-1] - e20h.iloc[-4])
    alta_h1 = float(e20h.iloc[-1]) > float(e50h.iloc[-1]) and inclinacao > 0
    baixa_h1 = float(e20h.iloc[-1]) < float(e50h.iloc[-1]) and inclinacao < 0
    alinhado_alta = preco > float(ema20.iloc[-1]) > float(ema50.iloc[-1])
    alinhado_baixa = preco < float(ema20.iloc[-1]) < float(ema50.iloc[-1])
    tendencia_m15 = "ALTA" if alinhado_alta else ("BAIXA" if alinhado_baixa else "LATERAL")
    tendencia = "ALTA" if alta_h1 else ("BAIXA" if baixa_h1 else "LATERAL")

    janela = fechados.iloc[-48:-1]
    suporte, resistencia = float(janela["Low"].min()), float(janela["High"].max())
    amplitude = resistencia - suporte
    fibs = {"Fibo 38,2%": resistencia - .382 * amplitude,
            "Fibo 50%": resistencia - .5 * amplitude,
            "Fibo 61,8%": resistencia - .618 * amplitude}
    margem = .35 * atr
    confluencias: list[str] = []
    if abs(preco - float(ema20.iloc[-1])) <= margem: confluencias.append("EMA20 M15")
    if abs(preco - float(ema50.iloc[-1])) <= margem: confluencias.append("EMA50 M15")
    confluencias += [nome for nome, nivel in fibs.items() if abs(preco - nivel) <= margem]
    if abs(preco - suporte) <= margem: confluencias.append("suporte")
    if abs(preco - resistencia) <= margem: confluencias.append("resistência")

    if tendencia == "ALTA":
        projecao = preco + 2 * atr
        alvo = resistencia if resistencia > preco + atr else projecao
        alvo_alternativo = projecao if alvo == resistencia else resistencia
        invalidacao = min(float(fechados["Low"].iloc[-12:].min()), float(ema50.iloc[-1]))
        contexto = "Favorece compras em recuos; aguarde reação numa confluência."
        alinhado = alinhado_alta
        lado_entrada = "COMPRA"
    elif tendencia == "BAIXA":
        projecao = preco - 2 * atr
        alvo = suporte if suporte < preco - atr else projecao
        alvo_alternativo = projecao if alvo == suporte else suporte
        invalidacao = max(float(fechados["High"].iloc[-12:].max()), float(ema50.iloc[-1]))
        contexto = "Favorece vendas em repiques; aguarde rejeição numa confluência."
        alinhado = alinhado_baixa
        lado_entrada = "VENDA"
    else:
        alvo = resistencia if preco <= (suporte + resistencia) / 2 else suporte
        alvo_alternativo = suporte if alvo == resistencia else resistencia
        invalidacao = suporte if alvo == resistencia else resistencia
        contexto = "Sem direção dominante; suporte e resistência delimitam o range."
        alinhado = False
        lado_entrada = "COMPRA" if alvo == resistencia else "VENDA"

    niveis = {"EMA20": float(ema20.iloc[-1]), "EMA50": float(ema50.iloc[-1]),
              "suporte": suporte, "resistência": resistencia, **fibs}
    if lado_entrada == "COMPRA":
        candidatos = [(nome, nivel) for nome, nivel in niveis.items() if nivel <= preco]
    else:
        candidatos = [(nome, nivel) for nome, nivel in niveis.items() if nivel >= preco]
    if candidatos:
        def forca(item):
            _, nivel = item
            agrupados = sum(abs(nivel - outro) <= .35 * atr for _, outro in niveis.items())
            return agrupados, -abs(preco - nivel) / atr
        _, centro = max(candidatos, key=forca)
    else:
        centro = float(ema20.iloc[-1])
    entrada_min, entrada_max = centro - .20 * atr, centro + .20 * atr
    # Em cripto mostramos distância/ATR em dólares; pip de 0,0001 não faz sentido.
    fator_pip = 1 if eh_cripto(ativo) else (100 if "JPY" in ativo else 10_000)
    distancia = 0.0 if entrada_min <= preco <= entrada_max else min(abs(preco - entrada_min), abs(preco - entrada_max))
    if entrada_min <= preco <= entrada_max:
        estado_preco = "PREÇO NA ZONA"
    elif lado_entrada == "COMPRA":
        estado_preco = "AGUARDAR RECUO" if preco > entrada_max else "ABAIXO DA ZONA"
    else:
        estado_preco = "AGUARDAR REPIQUE" if preco < entrada_min else "ACIMA DA ZONA"
    centro_entrada = (entrada_min + entrada_max) / 2
    risco_estimado = centro_entrada - invalidacao if lado_entrada == "COMPRA" else invalidacao - centro_entrada
    retorno_estimado = alvo - centro_entrada if lado_entrada == "COMPRA" else centro_entrada - alvo
    rr_estimado = retorno_estimado / risco_estimado if risco_estimado > 0 and retorno_estimado > 0 else 0.0
    delta = fechados["Close"].diff()
    ganhos = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    perdas = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    rsi14 = 100.0 if perdas == 0 else 100 - 100 / (1 + ganhos / perdas)
    qualidade = min(100, 25 + (25 if alinhado else 0) + min(45, len(confluencias) * 15))
    if tendencia == "LATERAL": qualidade = min(qualidade, 45)
    return LeituraMercado(ativo, tendencia, preco, alvo, alvo_alternativo, invalidacao,
                          entrada_min, entrada_max, lado_entrada,
                          confluencias, contexto, qualidade,
                          tendencia_m15=tendencia_m15, rsi14=float(rsi14),
                          atr_pips=atr * fator_pip, suporte=suporte, resistencia=resistencia,
                          rr_estimado=rr_estimado, distancia_zona_pips=distancia * fator_pip,
                          estado_preco=estado_preco)


class AnalistaGemini:
    """Segunda opinião assíncrona; nunca controla execução de ordens."""

    def __init__(self, chave: str | None = None, intervalo_s: int = 900,
                 chave_groq: str | None = None):
        self._chave = chave if chave is not None else os.getenv("GEMINI_API_KEY", "").strip()
        self._chave_groq = chave_groq if chave_groq is not None else os.getenv("GROQ_API_KEY", "").strip()
        self._modelo_groq = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"
        self._intervalo_s = intervalo_s
        self._ultima: dict[str, float] = {}
        self._contexto_noticia: dict[str, str] = {}
        self._em_andamento: set[str] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gemini-forex")

    @property
    def ativo(self) -> bool:
        return bool(self._chave or self._chave_groq)

    @property
    def gemini_ativo(self) -> bool:
        return bool(self._chave)

    @property
    def groq_ativo(self) -> bool:
        return bool(self._chave_groq)

    def solicitar(self, leitura: LeituraMercado, callback, noticias: dict | None = None) -> None:
        if not self.ativo:
            callback(leitura.ativo, {"status": "DESATIVADA", "resumo": "Configure GEMINI_API_KEY ou GROQ_API_KEY."})
            return
        agora = time.time()
        assinatura_noticia = json.dumps(noticias or {}, sort_keys=True, ensure_ascii=False)
        with self._lock:
            noticia_igual = self._contexto_noticia.get(leitura.ativo) == assinatura_noticia
            if leitura.ativo in self._em_andamento or (noticia_igual and agora - self._ultima.get(leitura.ativo, 0) < self._intervalo_s):
                return
            self._em_andamento.add(leitura.ativo)
            self._ultima[leitura.ativo] = agora
            self._contexto_noticia[leitura.ativo] = assinatura_noticia
        self._pool.submit(self._executar, leitura, callback, noticias)

    def _executar(self, leitura: LeituraMercado, callback, noticias: dict | None = None) -> None:
        try:
            prompt = (
                "Você é uma segunda opinião conservadora para leitura de Forex M15/H1. "
                "Não invente preços e não mande executar ordem. Escolha apenas um dos alvos fornecidos. "
                "Se Notícias.status for EVITAR, seu status obrigatoriamente deve ser EVITAR. "
                "Se for DIRECIONAL, só use FAVORAVEL quando direcao_noticia coincidir com o lado técnico; "
                "se divergir, use EVITAR. Nunca antecipe notícia sem actual e forecast confirmados. "
                "Responda SOMENTE JSON com: status (FAVORAVEL, ESPERAR ou EVITAR), lado "
                "(COMPRA, VENDA ou NEUTRO), alvo_escolhido (número fornecido), resumo (máx. 180 caracteres).\n"
                f"Dados objetivos: {json.dumps(leitura.to_dict(), ensure_ascii=False)}\n"
                f"Notícias: {json.dumps(noticias or {}, ensure_ascii=False)}"
            )
            erro_final: Exception | None = None
            modelos = ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite") if self._chave else ()
            for modelo in modelos:
                try:
                    resposta = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent",
                        headers={"x-goog-api-key": self._chave, "Content-Type": "application/json"},
                        json={"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 400}},
                        timeout=(5, 15),
                    )
                except (requests.Timeout, requests.ConnectionError) as exc:
                    erro_final = exc
                    continue
                if resposta.status_code in {429, 503}:
                    erro_final = RuntimeError(f"Modelo ocupado: HTTP {resposta.status_code}")
                    continue
                resposta.raise_for_status()
                try:
                    texto = resposta.json()["candidates"][0]["content"]["parts"][0].get("text", "")
                    dados = _extrair_json_gemini(texto)
                    permitidos = {round(leitura.alvo, 5), round(leitura.alvo_alternativo, 5)}
                    alvo = round(float(dados.get("alvo_escolhido")), 5)
                    if dados.get("status") not in {"FAVORAVEL", "ESPERAR", "EVITAR"} or alvo not in permitidos:
                        raise ValueError("Resposta fora dos limites permitidos")
                except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    erro_final = exc
                    continue
                callback(leitura.ativo, {"status": dados["status"], "lado": dados.get("lado", "NEUTRO"),
                                         "alvo_escolhido": alvo, "resumo": str(dados.get("resumo", ""))[:180],
                                         "fonte": "GEMINI"})
                return
            if self._chave_groq:
                try:
                    callback(leitura.ativo, self._consultar_groq(prompt, leitura))
                    return
                except Exception as exc:
                    erro_final = exc
            raise erro_final or RuntimeError("Nenhum provedor de IA respondeu")
        except Exception as exc:
            callback(leitura.ativo, _analise_local(leitura, noticias, type(exc).__name__))
            with self._lock:
                # Evita insistir na API e consumir mais cota após uma falha.
                self._ultima[leitura.ativo] = time.time()
        finally:
            with self._lock:
                self._em_andamento.discard(leitura.ativo)

    def _consultar_groq(self, prompt: str, leitura: LeituraMercado) -> dict:
        resposta = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._chave_groq}", "Content-Type": "application/json"},
            json={"model": self._modelo_groq, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0, "max_tokens": 400,
                  "response_format": {"type": "json_object"}},
            timeout=(5, 20),
        )
        resposta.raise_for_status()
        texto = resposta.json()["choices"][0]["message"]["content"]
        dados = _extrair_json_gemini(texto)
        permitidos = {round(leitura.alvo, 5), round(leitura.alvo_alternativo, 5)}
        alvo = round(float(dados.get("alvo_escolhido")), 5)
        if dados.get("status") not in {"FAVORAVEL", "ESPERAR", "EVITAR"} or alvo not in permitidos:
            raise ValueError("Resposta da Groq fora dos limites permitidos")
        return {"status": dados["status"], "lado": dados.get("lado", "NEUTRO"),
                "alvo_escolhido": alvo, "resumo": str(dados.get("resumo", ""))[:180],
                "fonte": "GROQ"}


def _extrair_json_gemini(texto: str) -> dict:
    """Aceita JSON puro ou cercado por Markdown; rejeita conteúdo incompleto."""
    limpo = (texto or "").strip()
    inicio, fim = limpo.find("{"), limpo.rfind("}")
    if inicio < 0 or fim <= inicio:
        raise ValueError("Resposta sem objeto JSON completo")
    dados = json.loads(limpo[inicio:fim + 1])
    if not isinstance(dados, dict):
        raise ValueError("Resposta JSON não é objeto")
    return dados


def _analise_local(leitura: LeituraMercado, noticias: dict | None, motivo: str) -> dict:
    risco_noticia = (noticias or {}).get("status") == "EVITAR"
    direcao_noticia = (noticias or {}).get("direcao_noticia")
    lado_noticia = {"CALL": "COMPRA", "PUT": "VENDA"}.get(direcao_noticia)
    alinhada = leitura.tendencia == leitura.tendencia_m15 and leitura.tendencia != "LATERAL"
    if risco_noticia:
        status, resumo = "EVITAR", "Notícia sem resultado direcional confirmado dentro da janela de ±20 minutos."
    elif (noticias or {}).get("status") == "DIRECIONAL" and lado_noticia != leitura.lado_entrada:
        status, resumo = "EVITAR", "Notícia confirmada diverge da direção técnica; não perseguir o movimento."
    elif ((noticias or {}).get("status") == "DIRECIONAL" and leitura.estado_preco == "PREÇO NA ZONA"
          and alinhada and leitura.qualidade >= 65 and leitura.rr_estimado >= 1.5):
        status, resumo = "FAVORAVEL", "Notícia confirmada alinhada à técnica e preço na zona; exija reação antes da entrada."
    elif leitura.rr_estimado < 1.5:
        status, resumo = "EVITAR", f"R:R estimado baixo ({leitura.rr_estimado:.2f}x)."
    elif leitura.estado_preco == "PREÇO NA ZONA" and alinhada and leitura.qualidade >= 65:
        status, resumo = "FAVORAVEL", "Preço na zona, H1/M15 alinhados e contexto forte; confirme a reação do preço."
    else:
        status, resumo = "ESPERAR", f"{leitura.estado_preco.lower()}; aguarde confirmação e alinhamento do contexto."
    return {"status": status, "lado": leitura.lado_entrada,
            "alvo_escolhido": round(leitura.alvo, 5),
            "resumo": resumo, "fonte": "LOCAL", "falha_gemini": motivo}


def resumir_noticias(calendario: CalendarioEconomico, ativo: str,
                     agora: datetime | None = None) -> dict:
    agora = agora or datetime.now(timezone.utc)
    eventos = calendario.proximos(ativo, agora, horas=8)[:3]
    itens = []
    risco = False
    direcoes_confirmadas: list[str] = []
    for evento in eventos:
        minutos = evento.minutos_ate(agora)
        em_janela = -20 <= minutos <= 20
        risco = risco or em_janela
        resultado = evento.resultado_direcao(ativo)
        if em_janela and resultado and resultado.get("direcao"):
            direcoes_confirmadas.append(resultado["direcao"])
        itens.append({
            "titulo": evento.titulo, "moeda": evento.moeda,
            "impacto": evento.impacto.upper(), "minutos": round(minutos),
            "horario_utc": evento.quando.strftime("%H:%M"),
            "em_janela_risco": em_janela,
            "direcao_resultado": resultado.get("direcao") if resultado else None,
            "actual": evento.actual, "forecast": evento.forecast, "previous": evento.previous,
        })
    direcao_unica = direcoes_confirmadas[0] if direcoes_confirmadas and len(set(direcoes_confirmadas)) == 1 else None
    status = "DIRECIONAL" if direcao_unica else ("EVITAR" if risco else "LIVRE")
    return {"ativo": ativo, "status": status, "direcao_noticia": direcao_unica,
            "janela_minutos": 20, "eventos": itens}


def _indice_ultimo_fechado(df: pd.DataFrame, agora: datetime) -> int | None:
    """Localiza o candle fechado mais recente sem assumir que a ultima linha e parcial."""
    if len(df) == 0:
        return None
    agora_ts = pd.Timestamp(agora)
    if agora_ts.tzinfo is not None:
        agora_ts = agora_ts.tz_convert(None)
    indices = pd.DatetimeIndex(df.index)
    if indices.tz is not None:
        indices = indices.tz_convert(None)
    fechados = np.flatnonzero((indices + pd.Timedelta(seconds=TIMEFRAME_S)) <= agora_ts)
    return int(fechados[-1]) if len(fechados) else None


def _acao_no_preco(sinal: SinalForex, preco: float, agora: datetime) -> tuple[str, float]:
    risco = preco - sinal.sl
    reward = sinal.tp - preco
    rr = reward / risco if risco > 0 else -1.0
    if agora >= sinal.valido_ate:
        return "EXPIRADO", rr
    if preco <= sinal.sl or preco >= sinal.tp:
        return "CANCELADO", rr
    if rr < RR_MIN:
        return "AGUARDAR_PRECO", rr
    return "ENTRAR_AGORA", rr


def atualizar_preco_sinal(sinal: SinalForex, preco: float, agora: datetime | None = None) -> SinalForex:
    agora = agora or datetime.now(timezone.utc)
    acao, rr_atual = _acao_no_preco(sinal, preco, agora)
    return replace(sinal, preco_atual=preco, rr_atual=rr_atual, acao=acao)


def detectar_observacao_usdcad(df: pd.DataFrame, agora: datetime | None = None) -> SinalForex | None:
    """Candidato prospectivo; apenas alerta/salva, nunca envia ordem."""
    agora = agora or datetime.now(timezone.utc)
    i = _indice_ultimo_fechado(df, agora)
    if i is None or i < 220:
        return None
    fechados = df.iloc[:i + 1]
    plano = planos_pullback_h1_nova_york(
        "USDCAD", fechados, hora_inicio=8, hora_fim=16, retorno_risco=2.0
    ).iloc[-1]
    if plano is None or not isinstance(plano, object) or not hasattr(plano, "lado"):
        return None
    entrada = float(fechados["Close"].iloc[-1]) + (_spread("USDCAD") / 2 if plano.lado == "buy" else -_spread("USDCAD") / 2)
    risco = entrada - plano.stop if plano.lado == "buy" else plano.stop - entrada
    reward = plano.alvo - entrada if plano.lado == "buy" else entrada - plano.alvo
    if risco <= 0 or reward <= 0:
        return None
    candle = pd.Timestamp(fechados.index[-1])
    if candle.tzinfo is None:
        candle = candle.tz_localize(timezone.utc)
    return SinalForex(
        ativo="USDCAD", lado=plano.lado, entrada=entrada, sl=plano.stop, tp=plano.alvo,
        risco=risco, rr=reward / risco, gerado_em=agora,
        candle_rompimento=candle.to_pydatetime(),
        valido_ate=(candle + pd.Timedelta(minutes=30)).to_pydatetime(),
        preco_atual=entrada, rr_atual=reward / risco, acao="OBSERVAR",
        setup="pullback_h1_usdcad_ny_08_16",
    )


def detectar_sinal(
    ativo: str,
    df: pd.DataFrame,
    agora: datetime | None = None,
) -> SinalForex | None:
    """Retorna sinal se o ultimo candle fechado e um falso rompimento BUY valido."""
    if len(df) < JAN + 20:
        return None

    agora = agora or datetime.now(timezone.utc)
    H, L, O, C = df["High"], df["Low"], df["Open"], df["Close"]
    amp = H - L
    med_amp = amp.shift(1).rolling(JAN).mean()
    # Quartil calculado apenas com informacao disponivel antes do candle avaliado.
    limiar = med_amp.shift(1).expanding(min_periods=MIN_HIST_REGIME).quantile(0.25)

    rhi = H.shift(1).rolling(JAN).max()
    rlo = L.shift(1).rolling(JAN).min()
    meio = (rhi + rlo) / 2
    atr = _atr14(df)

    i = _indice_ultimo_fechado(df, agora)
    if i is None or i + 1 >= len(df):
        return None
    limiar_i = float(limiar.iloc[i])
    apertado = np.isfinite(limiar_i) and float(med_amp.iloc[i]) <= limiar_i
    rompe_baixo = apertado and float(C.iloc[i]) < float(rlo.iloc[i])
    if not rompe_baixo:
        return None

    atr_i = float(atr.iloc[i])
    if not np.isfinite(atr_i) or atr_i <= 0:
        return None

    # Mesmo modelo do backtest: BUY no ask estimado da vela seguinte.
    entrada_ref = float(O.iloc[i + 1]) + _spread(ativo)
    sl = float(L.iloc[i]) - BUF_SL_ATR * atr_i
    tp = float(meio.iloc[i])
    risco = entrada_ref - sl
    reward = tp - entrada_ref

    if risco <= 0 or reward <= 0:
        return None
    rr = reward / risco
    if rr < RR_MIN:
        return None

    candle_entrada = pd.Timestamp(df.index[i + 1])
    if candle_entrada.tzinfo is None:
        candle_entrada = candle_entrada.tz_localize(timezone.utc)
    else:
        candle_entrada = candle_entrada.tz_convert(timezone.utc)
    valido_ate = (candle_entrada + pd.Timedelta(seconds=TIMEFRAME_S)).to_pydatetime()
    preco_atual = float(C.iloc[-1]) + _spread(ativo)
    sinal = SinalForex(
        ativo=ativo,
        lado="buy",
        entrada=entrada_ref,
        sl=sl,
        tp=tp,
        risco=risco,
        rr=rr,
        gerado_em=agora,
        candle_rompimento=df.index[i].to_pydatetime(),
        valido_ate=valido_ate,
        preco_atual=preco_atual,
        rr_atual=rr,
    )
    return atualizar_preco_sinal(sinal, preco_atual, agora)


# ---------------------------------------------------------------------------
# Diario de trades — CSV automatico
# ---------------------------------------------------------------------------

class DiarioTrades:
    """Registra sinais e eventos de posicao sem inventar resultado final."""

    _CABECALHO_SINAIS = [
        "id_sinal", "data", "hora_utc", "ativo", "lado", "entrada_planejada",
        "sl", "tp", "rr_planejado", "candle_rompimento", "valido_ate_utc",
    ]
    _CABECALHO_TRADES = [
        "evento", "id_posicao", "id_sinal", "data", "hora_abertura_utc",
        "hora_evento_utc", "ativo", "lado", "entrada", "saida", "sl", "tp",
        "pnl", "resultado", "fonte_resultado",
    ]
    _CABECALHO_LEITURAS = [
        "data", "hora_utc", "ativo", "h1", "m15", "lado", "estado_preco",
        "preco", "entrada_min", "entrada_max", "alvo", "alvo_alternativo",
        "invalidacao", "rr", "qualidade", "rsi14", "atr_pips", "confluencias",
        "status_noticia", "direcao_noticia",
    ]
    _CABECALHO_IA = ["data", "hora_utc", "ativo", "fonte", "status", "lado",
                         "alvo_escolhido", "resumo", "status_noticia", "direcao_noticia"]

    def __init__(self, pasta: Path):
        self._arq_sinais = pasta / "forex_sinais.csv"
        self._arq_trades = pasta / "forex_trades.csv"
        self._arq_observacao = pasta / "forex_sinais_observacao.csv"
        self._arq_leituras = pasta / "forex_leituras.csv"
        self._arq_ia = pasta / "forex_analises_ia.csv"
        self._ultimo_sinal_por_ativo: dict[str, str] = {}
        self._ultima_leitura: dict[str, str] = {}
        self._lock = threading.Lock()
        self._garantir_cabecalho(self._arq_sinais, self._CABECALHO_SINAIS)
        self._garantir_cabecalho(self._arq_trades, self._CABECALHO_TRADES)
        self._garantir_cabecalho(self._arq_observacao, self._CABECALHO_SINAIS + ["setup"])
        self._garantir_cabecalho(self._arq_leituras, self._CABECALHO_LEITURAS)
        self._garantir_cabecalho(self._arq_ia, self._CABECALHO_IA)

    def registrar_leitura(self, leitura: LeituraMercado, noticias: dict) -> None:
        agora = datetime.now(timezone.utc)
        janela = agora.strftime("%Y-%m-%d %H:%M")[:-1]
        assinatura = f"{janela}|{leitura.estado_preco}|{leitura.lado_entrada}|{noticias.get('status')}|{noticias.get('direcao_noticia')}"
        with self._lock:
            if self._ultima_leitura.get(leitura.ativo) == assinatura:
                return
            self._ultima_leitura[leitura.ativo] = assinatura
            linha = [agora.strftime("%Y-%m-%d"), agora.strftime("%H:%M:%S"), leitura.ativo,
                     leitura.tendencia, leitura.tendencia_m15, leitura.lado_entrada,
                     leitura.estado_preco, leitura.preco, leitura.entrada_min, leitura.entrada_max,
                     leitura.alvo, leitura.alvo_alternativo, leitura.invalidacao,
                     round(leitura.rr_estimado, 2), leitura.qualidade, round(leitura.rsi14, 2),
                     round(leitura.atr_pips, 2), " | ".join(leitura.confluencias),
                     noticias.get("status", ""), noticias.get("direcao_noticia", "")]
            with open(self._arq_leituras, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(linha)

    def registrar_analise_ia(self, ativo: str, analise: dict, noticias: dict) -> None:
        agora = datetime.now(timezone.utc)
        linha = [agora.strftime("%Y-%m-%d"), agora.strftime("%H:%M:%S"), ativo,
                 analise.get("fonte", ""), analise.get("status", ""), analise.get("lado", ""),
                 analise.get("alvo_escolhido", ""), analise.get("resumo", ""),
                 noticias.get("status", ""), noticias.get("direcao_noticia", "")]
        with self._lock:
            with open(self._arq_ia, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(linha)

    def resumo_estatisticas(self) -> dict:
        """Resume apenas fechamentos oficiais já salvos; não conta posições pendentes."""
        por_par: dict[str, dict] = {}
        total = ganhos = perdas = 0
        if self._arq_trades.exists():
            with open(self._arq_trades, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("evento") != "fechada":
                        continue
                    try: pnl = float(row.get("pnl", ""))
                    except (TypeError, ValueError): continue
                    par = row.get("ativo", "?")
                    item = por_par.setdefault(par, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
                    item["trades"] += 1; item["pnl"] += pnl
                    total += 1
                    if pnl > 0: ganhos += 1; item["wins"] += 1
                    elif pnl < 0: perdas += 1; item["losses"] += 1
        for item in por_par.values():
            item["pnl"] = round(item["pnl"], 2)
            item["winrate"] = round(100 * item["wins"] / item["trades"], 1) if item["trades"] else 0
        return {"trades": total, "wins": ganhos, "losses": perdas,
                "winrate": round(100 * ganhos / total, 1) if total else 0,
                "por_par": por_par}

    def registrar_observacao(self, sinal: SinalForex) -> None:
        agora = datetime.now(timezone.utc)
        linha = [sinal.id_sinal, agora.strftime("%Y-%m-%d"), agora.strftime("%H:%M:%S"),
                 sinal.ativo, sinal.lado, sinal.entrada, sinal.sl, sinal.tp, round(sinal.rr, 2),
                 sinal.candle_rompimento.strftime("%Y-%m-%d %H:%M"),
                 sinal.valido_ate.strftime("%H:%M:%S"), sinal.setup]
        with self._lock:
            with open(self._arq_observacao, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(linha)
        print(f"[OBSERVACAO] {sinal.ativo} {sinal.lado.upper()} {sinal.setup} R:R={sinal.rr:.1f}")

    def _garantir_cabecalho(self, arq: Path, cabecalho: list[str]) -> None:
        if not arq.exists():
            with open(arq, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(cabecalho)

    def registrar_sinal(self, sinal: SinalForex) -> None:
        agora = datetime.now(timezone.utc)
        linha = [
            sinal.id_sinal, agora.strftime("%Y-%m-%d"), agora.strftime("%H:%M:%S"),
            sinal.ativo, sinal.lado, sinal.entrada, sinal.sl, sinal.tp,
            round(sinal.rr, 2), sinal.candle_rompimento.strftime("%Y-%m-%d %H:%M"),
            sinal.valido_ate.strftime("%H:%M:%S"),
        ]
        with self._lock:
            self._ultimo_sinal_por_ativo[sinal.ativo] = sinal.id_sinal
            with open(self._arq_sinais, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(linha)
        print(f"[DIARIO] Sinal registrado: {sinal.ativo} BUY R:R={sinal.rr:.1f}")

    @staticmethod
    def _ativo(pos: dict) -> str:
        bruto = str(pos.get("instrument_id") or pos.get("active") or "?").upper()
        return bruto.replace("-FOREX", "").replace(".", "")

    @staticmethod
    def _pnl(pos: dict) -> float | None:
        for chave in ("pnl_net", "pnl", "pnl_realized"):
            valor = pos.get(chave)
            if isinstance(valor, dict):
                valor = valor.get("realized", valor.get("floating"))
            try:
                if valor is not None:
                    return float(valor)
            except (TypeError, ValueError):
                continue
        return None

    def _id_sinal(self, pos: dict) -> str:
        return self._ultimo_sinal_por_ativo.get(self._ativo(pos), "")

    def registrar_abertura(self, pos: dict, hora_abertura: str) -> None:
        self._registrar_evento("aberta", pos, hora_abertura, fonte="get_positions")

    def registrar_pendente(self, pos: dict, hora_abertura: str) -> None:
        self._registrar_evento("fechamento_pendente", pos, hora_abertura, fonte="aguardando_historico_iq")

    def registrar_fechamento(self, pos: dict, hora_abertura: str) -> None:
        self._registrar_evento("fechada", pos, hora_abertura, fonte="historico_iq")

    def _registrar_evento(self, evento: str, pos: dict, hora_abertura: str, fonte: str) -> None:
        agora = datetime.now(timezone.utc)
        entrada = pos.get("open_price", 0)
        saida = pos.get("close_price", pos.get("current_price", 0))
        pnl = self._pnl(pos) if evento == "fechada" else None
        resultado = ""
        if pnl is not None:
            resultado = "ganho" if pnl > 0 else ("perda" if pnl < 0 else "zero")
        linha = [
            evento, pos.get("id", pos.get("external_id", "")), self._id_sinal(pos),
            agora.strftime("%Y-%m-%d"), hora_abertura, agora.strftime("%H:%M:%S"),
            self._ativo(pos), pos.get("side", "?"), entrada, saida,
            pos.get("stop_lose_value", ""), pos.get("take_profit_value", ""),
            "" if pnl is None else round(pnl, 2), resultado, fonte,
        ]
        with self._lock:
            with open(self._arq_trades, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(linha)
        print(f"[DIARIO] Posicao {evento}: {self._ativo(pos)} {resultado} PnL={pnl}")


# ---------------------------------------------------------------------------
# Rastreador de posicoes abertas
# ---------------------------------------------------------------------------

class RastreadorPosicoes:
    def __init__(self, api, diario: DiarioTrades):
        self._api = api
        self._diario = diario
        self._posicoes: list[dict] = []
        self._abertas: dict[str, dict] = {}
        self._pendentes: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _buscar_fechada(self, pid: str, inicio_ts: int) -> dict | None:
        """Busca o fechamento oficial com timeout; nunca usa P&L flutuante como final."""
        baixo = getattr(self._api, "api", None)
        if baixo is None or not hasattr(baixo, "get_position_history_v2"):
            return None
        try:
            baixo.position_history_v2 = None
            baixo.get_position_history_v2("forex", 100, 0, inicio_ts - 3600, int(time.time()) + 60)
            prazo = time.monotonic() + 5.0
            while baixo.position_history_v2 is None and time.monotonic() < prazo:
                time.sleep(0.05)
            resposta = baixo.position_history_v2 or {}
            posicoes = (resposta.get("msg") or {}).get("positions", []) if resposta.get("status") == 2000 else []
            return next(
                (p for p in posicoes if pid in {str(p.get("id", "")), str(p.get("external_id", ""))}),
                None,
            )
        except Exception as exc:
            print(f"[FOREX-POS] Historico indisponivel para id={pid}: {exc!r}")
            return None

    def atualizar(self) -> None:
        try:
            ok, dados = self._api.get_positions("forex")
            if not ok or not dados:
                return
            posicoes = dados.get("positions", [])
            ids_atuais = {str(p.get("id", "")): p for p in posicoes if p.get("id") is not None}
            agora = datetime.now(timezone.utc)

            with self._lock:
                ids_anteriores = set(self._abertas)
                novos = [pid for pid in ids_atuais if pid not in self._abertas]
                sumidos = [pid for pid in ids_anteriores if pid not in ids_atuais]
                for pid in novos:
                    self._abertas[pid] = {
                        "posicao": ids_atuais[pid],
                        "hora": agora.strftime("%H:%M:%S"),
                        "inicio_ts": int(agora.timestamp()),
                    }
                for pid in ids_anteriores & set(ids_atuais):
                    self._abertas[pid]["posicao"] = ids_atuais[pid]
                for pid in sumidos:
                    self._pendentes.setdefault(pid, self._abertas.pop(pid))
                self._posicoes = posicoes

            for pid in novos:
                info = self._abertas[pid]
                self._diario.registrar_abertura(info["posicao"], info["hora"])
                print(f"[DIARIO] Nova posicao detectada: {info['posicao'].get('instrument_id')} id={pid}")
            for pid in sumidos:
                info = self._pendentes[pid]
                self._diario.registrar_pendente(info["posicao"], info["hora"])

            for pid, info in list(self._pendentes.items()):
                fechada = self._buscar_fechada(pid, info["inicio_ts"])
                if fechada is not None:
                    self._diario.registrar_fechamento(fechada, info["hora"])
                    with self._lock:
                        self._pendentes.pop(pid, None)
        except Exception as e:
            print(f"[FOREX-POS] Erro ao buscar posicoes: {e!r}")

    def lista(self) -> list[dict]:
        with self._lock:
            return list(self._posicoes)


# ---------------------------------------------------------------------------
# Servidor de dados para o grafico
# ---------------------------------------------------------------------------

class ServidorDadosForex:
    def __init__(self, pasta_dados: Path):
        self._pasta = pasta_dados
        self._sinais: list[dict] = []
        self._posicoes: list[dict] = []
        self._leituras: dict[str, dict] = {}
        self._analises_ia: dict[str, dict] = {}
        self._noticias: dict[str, dict] = {}
        self._horarios: dict[str, dict] = {}
        self._execucao: dict[str, dict] = {}
        self._estatisticas: dict = {}
        self._candles: dict[str, list[dict]] = {}
        self._status: str = "iniciando"
        self._lock = threading.Lock()

    def atualizar_sinal(self, sinal: SinalForex | None, ativo: str) -> None:
        with self._lock:
            # Remove sinais antigos do mesmo ativo
            self._sinais = [s for s in self._sinais if s["ativo"] != ativo]
            if sinal:
                self._sinais.append(sinal.to_dict())
        self._salvar()

    def atualizar_posicoes(self, posicoes: list[dict]) -> None:
        with self._lock:
            self._posicoes = [self._resumir_posicao(p) for p in posicoes]
        self._salvar()

    def atualizar_leitura(self, leitura: LeituraMercado | None, ativo: str) -> None:
        with self._lock:
            if leitura is None: self._leituras.pop(ativo, None)
            else: self._leituras[ativo] = leitura.to_dict()
        self._salvar()

    def atualizar_ia(self, ativo: str, analise: dict) -> None:
        with self._lock:
            self._analises_ia[ativo] = {"ativo": ativo, **analise}
        self._salvar()

    def atualizar_noticias(self, ativo: str, noticias: dict) -> None:
        with self._lock:
            self._noticias[ativo] = noticias
        self._salvar()

    def atualizar_horario(self, ativo: str, horario: dict) -> None:
        with self._lock:
            self._horarios[ativo] = horario
        self._salvar()

    def atualizar_execucao(self, ativo: str, execucao: dict) -> None:
        with self._lock:
            self._execucao[ativo] = execucao
        self._salvar()

    def atualizar_estatisticas(self, estatisticas: dict) -> None:
        with self._lock:
            self._estatisticas = estatisticas
        self._salvar()

    def set_status(self, msg: str) -> None:
        self._status = msg
        self._salvar()

    def salvar_candles(self, ativo: str, df: pd.DataFrame) -> None:
        ultimos = df.tail(60)
        candles = [
            {
                "t": int(ts.timestamp()),
                "o": round(float(row["Open"]), 5),
                "h": round(float(row["High"]), 5),
                "l": round(float(row["Low"]), 5),
                "c": round(float(row["Close"]), 5),
            }
            for ts, row in ultimos.iterrows()
        ]
        with self._lock:
            self._candles[ativo] = candles
        self._salvar_arquivo_candles(ativo, candles)

    def atualizar_tick(self, ativo: str, tick: dict) -> None:
        """Substitui apenas o candle em formação usando o stream da IQ."""
        candle = {
            "t": int(tick.get("from", tick.get("t", 0))),
            "o": round(float(tick.get("open", tick.get("o"))), 5),
            "h": round(float(tick.get("max", tick.get("h"))), 5),
            "l": round(float(tick.get("min", tick.get("l"))), 5),
            "c": round(float(tick.get("close", tick.get("c"))), 5),
        }
        if candle["t"] <= 0:
            return
        with self._lock:
            candles = list(self._candles.get(ativo, []))
            candles = [x for x in candles if int(x["t"]) != candle["t"]]
            candles.append(candle)
            candles = sorted(candles, key=lambda x: x["t"])[-60:]
            self._candles[ativo] = candles
        self._salvar_arquivo_candles(ativo, candles)

    def _salvar_arquivo_candles(self, ativo: str, candles: list[dict]) -> None:
        try:
            arq = self._pasta / f"candles_{ativo}.json"
            tmp = arq.with_name(f".{arq.name}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(candles), encoding="utf-8")
            os.replace(tmp, arq)
        except Exception:
            pass

    @staticmethod
    def _resumir_posicao(p: dict) -> dict:
        return {
            "id": p.get("id", ""),
            "ativo": p.get("instrument_id", "?"),
            "lado": p.get("side", "?"),
            "entrada": p.get("open_price", 0),
            "pnl": p.get("pnl_net", {}).get("floating", 0) if isinstance(p.get("pnl_net"), dict) else p.get("pnl", 0),
            "sl": p.get("stop_lose_value", None),
            "tp": p.get("take_profit_value", None),
        }

    def _salvar(self) -> None:
        with self._lock:
            dados = {
                "timestamp": int(time.time()),
                "status": self._status,
                "sinais": list(self._sinais),
                "leituras": list(self._leituras.values()),
                "analises_ia": list(self._analises_ia.values()),
                "noticias": list(self._noticias.values()),
                "horarios": list(self._horarios.values()),
                "execucao": list(self._execucao.values()),
                "estatisticas": self._estatisticas,
                "posicoes": list(self._posicoes),
            }
        try:
            arq = self._pasta / "forex_monitor.json"
            arq.parent.mkdir(parents=True, exist_ok=True)
            tmp = arq.with_suffix(".tmp")
            tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, arq)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Loops principais
# ---------------------------------------------------------------------------

def loop_sinais(api, servidor: ServidorDadosForex, diario: DiarioTrades, config,
                gemini: AnalistaGemini | None = None,
                calendario: CalendarioEconomico | None = None) -> None:
    """Verifica M15 de todos os ativos a cada INTERVALO_POLL_S segundos."""
    import dataclasses
    cfg15 = dataclasses.replace(config, timeframe_segundos=900)
    ultimo_sinal: dict[str, SinalForex | None] = {a: None for a in ATIVOS}
    cancelados: set[str] = set()
    ticks_iniciados = False

    while True:
        for ativo in ATIVOS:
            try:
                # Baixa os ultimos 100 candles M15 (suficiente para deteccao)
                df = backtest.baixar_historico(api, cfg15, ativo, 300 if ativo == "USDCAD" else 100)
                servidor.salvar_candles(ativo, df)
                leitura = analisar_leitura_mercado(ativo, df)
                servidor.atualizar_leitura(leitura, ativo)
                noticias = resumir_noticias(calendario, ativo) if calendario is not None else {}
                servidor.atualizar_noticias(ativo, noticias)
                servidor.atualizar_horario(ativo, resumir_horario(ativo))
                servidor.atualizar_execucao(ativo, avaliar_execucao(ativo))
                if leitura is not None:
                    diario.registrar_leitura(leitura, noticias)
                if gemini is not None and leitura is not None:
                    def salvar_ia(par: str, analise: dict, contexto=noticias) -> None:
                        servidor.atualizar_ia(par, analise)
                        diario.registrar_analise_ia(par, analise, contexto)
                    gemini.solicitar(leitura, salvar_ia, noticias)
                sinal = detectar_sinal(ativo, df)
                if sinal is None and ativo == "USDCAD":
                    sinal = detectar_observacao_usdcad(df)

                if sinal:
                    anterior = ultimo_sinal[ativo]
                    if sinal.id_sinal in cancelados:
                        servidor.atualizar_sinal(None, ativo)
                        continue
                    if anterior is not None and anterior.id_sinal == sinal.id_sinal:
                        sinal = replace(sinal, gerado_em=anterior.gerado_em)
                    else:
                        if sinal.setup.startswith("pullback_h1_usdcad"):
                            diario.registrar_observacao(sinal)
                        else:
                            diario.registrar_sinal(sinal)
                        print(
                            f"[SINAL] {ativo} BUY | entrada={sinal.entrada:.5f} "
                            f"SL={sinal.sl:.5f} TP={sinal.tp:.5f} R:R={sinal.rr:.1f}"
                        )
                    ultimo_sinal[ativo] = sinal
                    if sinal.acao in {"CANCELADO", "EXPIRADO"}:
                        cancelados.add(sinal.id_sinal)
                        servidor.atualizar_sinal(None, ativo)
                    else:
                        servidor.atualizar_sinal(sinal, ativo)
                else:
                    ultimo_sinal[ativo] = None
                    servidor.atualizar_sinal(None, ativo)

            except Exception as e:
                print(f"[SINAL] {ativo} erro: {e!r}")

        if not ticks_iniciados:
            for ativo in ATIVOS:
                try:
                    api.start_candles_stream(ativo, TIMEFRAME_S, 60)
                except Exception as exc:
                    print(f"[TICK] {ativo}: stream indisponível ({type(exc).__name__})")
            threading.Thread(target=loop_ticks, args=(api, servidor),
                             name="forex-ticks", daemon=True).start()
            ticks_iniciados = True
        servidor.set_status(f"OK — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
        servidor.atualizar_estatisticas(diario.resumo_estatisticas())
        time.sleep(INTERVALO_POLL_S)


def loop_posicoes(api, servidor: ServidorDadosForex, rastreador: RastreadorPosicoes, diario: DiarioTrades) -> None:
    """Atualiza posicoes abertas a cada INTERVALO_POS_S segundos."""
    while True:
        rastreador.atualizar()
        servidor.atualizar_posicoes(rastreador.lista())
        time.sleep(INTERVALO_POS_S)


def loop_ticks(api, servidor: ServidorDadosForex) -> None:
    """Lê o cache WebSocket da IQ e atualiza o candle atual a cada segundo."""
    while True:
        for ativo in ATIVOS:
            try:
                bruto = api.get_realtime_candles(ativo, TIMEFRAME_S) or {}
                if bruto:
                    ts = max(bruto, key=lambda x: int(x))
                    tick = {"from": int(ts), **bruto[ts]}
                    servidor.atualizar_tick(ativo, tick)
            except Exception:
                pass
        time.sleep(INTERVALO_TICK_S)


# ---------------------------------------------------------------------------
# HTML da pagina de sinais
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>Monitor Forex</title>
<style>
*{box-sizing:border-box}
body{font-family:monospace;background:#0f172a;color:#e2e8f0;margin:0;padding:.75rem}
h1{color:#38bdf8;margin:0 0 .25rem;font-size:1.1rem;position:sticky;top:0;z-index:5;background:#0f172a;padding:.45rem 0;border-bottom:1px solid #1e293b}
.status{font-size:.75rem;color:#64748b;margin-bottom:.5rem}
.aviso{background:#422006;color:#fde68a;border:1px solid #92400e;padding:.45rem;border-radius:.3rem;font-size:.75rem;margin:.4rem 0}
.sec{color:#94a3b8;font-size:.8rem;margin:.9rem 0 .25rem;padding-bottom:.3rem;border-bottom:1px solid #334155;font-weight:bold;letter-spacing:.05em}
.card{background:#1e293b;border-radius:.4rem;padding:.75rem;margin:.35rem 0}
.buy{border-left:4px solid #22c55e}
.esperar{border-left:4px solid #f59e0b}
.acao{font-size:1rem;font-weight:bold;margin-bottom:.35rem}
.acao.entrar{color:#4ade80}.acao.esperar{color:#fbbf24}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:.4rem;margin-top:.4rem}
.item label{display:block;font-size:.65rem;color:#94a3b8}
.item span{font-size:.95rem;font-weight:bold}
.rr{color:#fbbf24}
.instrucao{margin-top:.6rem;font-size:.7rem;color:#7dd3fc;background:#0c4a6e;padding:.4rem;border-radius:.25rem}
.pos-card{background:#1e293b;border-radius:.4rem;padding:.6rem;margin:.35rem 0;border-left:4px solid #8b5cf6;font-size:.85rem}
.empty{color:#475569;font-style:italic;font-size:.85rem}
.leitura{border-left:4px solid #38bdf8}.alta{color:#4ade80}.baixa{color:#f87171}.lateral{color:#fbbf24}
.chips{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.55rem}.chip{background:#0f172a;border:1px solid #334155;border-radius:1rem;padding:.15rem .45rem;font-size:.68rem}
.noticia-risco{border-left:4px solid #ef4444}.noticia-livre{border-left:4px solid #22c55e}.evento{padding:.28rem 0;border-bottom:1px solid #334155;font-size:.75rem}.evento:last-child{border:0}

/* Seletor de par */
.tabs{display:flex;flex-wrap:wrap;gap:.3rem;margin:.35rem 0}
.tab{background:#1e293b;border:1px solid #334155;border-radius:.3rem;padding:.2rem .5rem;
     cursor:pointer;font-size:.75rem;color:#94a3b8;transition:all .15s}
.tab:hover{border-color:#38bdf8;color:#e2e8f0}
.tab.ativo{background:#0f4c75;border-color:#38bdf8;color:#fff;font-weight:bold}
.tab.sinal{border-color:#22c55e!important;color:#22c55e!important}
#tabs{position:sticky;top:2.5rem;z-index:4;background:#0f172a;padding:.3rem 0}
.card{box-shadow:0 2px 8px rgba(0,0,0,.18)}
@media(max-width:700px){.grid{grid-template-columns:1fr 1fr}body{padding:.45rem}.decisao{font-size:17px}}

/* Grafico */
#grafico-wrap{background:#0f172a;border:1px solid #1e293b;border-radius:.4rem;overflow:hidden;margin:.35rem 0}
canvas{display:block;width:100%;image-rendering:auto;cursor:default}
.grafico-bar{display:flex;justify-content:flex-end;gap:.25rem;padding:.25rem;background:#111b2e}
.zoom-btn{background:#1e293b;color:#cbd5e1;border:1px solid #334155;border-radius:.25rem;padding:.15rem .5rem;cursor:pointer;font:11px monospace}
.decisao{font-size:20px;font-weight:bold;padding:.35rem .5rem;border-left:4px solid #38bdf8;background:#132238;margin-bottom:.4rem}
.faixa{display:inline-block;padding:.2rem .4rem;margin:.15rem .2rem .15rem 0;border-radius:.25rem;background:#26364f}
</style>
</head>
<body>
<h1>Monitor Forex M15</h1>
<div class="status" id="status">carregando...</div>
<div class="aviso">PESQUISA — use conta PRACTICE. O backtest corrigido ainda não confirmou vantagem estatística.</div>

<div class="sec">PAR</div>
<div class="tabs" id="tabs"></div><label style="font-size:.72rem;color:#94a3b8"><input id="soOportunidades" type="checkbox"> só oportunidades (R:R ≥ 1,5 e qualidade ≥ 65)</label>

<div id="grafico-wrap"><div class="grafico-bar">
  <button class="zoom-btn" onclick="zoomGrafico(-10)">− afastar</button>
  <button class="zoom-btn" onclick="velasVisiveis=40;renderGrafico()">40 velas</button>
  <button class="zoom-btn" onclick="zoomGrafico(10)">+ aproximar</button>
</div><canvas id="cv" height="320"></canvas></div>

<div class="sec">LEITURA DO MERCADO</div>
<div id="leitura"><span class="empty">Calculando contexto...</span></div>

<div class="sec">NOTÍCIAS DO PAR</div>
<div id="noticias"><span class="empty">Consultando calendário...</span></div>

<div class="sec">HORÁRIO E LIQUIDEZ</div>
<div id="horario"><span class="empty">Calculando sessão...</span></div>

<div class="sec">EXECUÇÃO E HISTÓRICO</div>
<div id="estatisticas"><span class="empty">Calculando custos e resultados...</span></div>

<div class="sec">SIMULADOR DOS SINAIS</div>
<div id="simulador"><span class="empty">Execute SIMULAR_SINAIS.bat para carregar os resultados.</span></div>

<div class="sec">GESTÃO DA BANCA</div>
<div id="gestao" class="card">Saldo: <input id="saldo" type="number" value="60" min="1" step="1" style="width:65px"> R$ &bull; lote: <input id="lote" type="number" value="0.001" min="0.001" step="0.001" style="width:65px"> &bull; alavancagem: <input id="alavancagem" type="number" value="100" min="1" step="1" style="width:55px">x<div id="gestaoResumo" class="instrucao">Risco/operação: R$0,60 &bull; stop diário: R$1,20</div></div>

<div class="sec">SEGUNDA OPINIÃO</div>
<div id="analise-ia"><span class="empty">Aguardando análise...</span></div>

<div class="sec">ALERTA EXPERIMENTAL</div>
<div id="sinais"><span class="empty">Nenhum sinal no momento</span></div>

<div class="sec">POSICOES ABERTAS</div>
<div id="posicoes"><span class="empty">Nenhuma posicao aberta</span></div>

<script>
const ATIVOS = ["EURUSD","GBPUSD","USDJPY","AUDUSD","EURJPY","USDCAD","NZDUSD"];
let parAtivo = ATIVOS[0];
let dadosGlobais = {};
let candlesAtuais = [];
let simulacaoGlobal = { resumo: {}, sinais: [] };
let velasVisiveis = 40;
let ultimoAlerta = '';
function atualizarGestao(leitura) {
  const saldo = Math.max(0, Number(document.getElementById('saldo').value) || 0);
  const lote = Math.max(0.001, Number(document.getElementById('lote').value) || 0.001);
  const lev = Math.max(1, Number(document.getElementById('alavancagem').value) || 1);
  const cripto = leitura && !['EURUSD','GBPUSD','USDJPY','AUDUSD','EURJPY','USDCAD','NZDUSD'].includes(leitura.ativo);
  const margemUsd = cripto ? null : lote * 100000 / lev;
  const pip = cripto ? null : (leitura && leitura.ativo.includes('JPY') ? .01 : .0001);
  const stopPips = leitura && pip ? Math.abs(Number(leitura.preco)-Number(leitura.invalidacao))/pip : null;
  const perdaUsd = stopPips == null ? null : stopPips * lote * 10;
  const riscoPct = perdaUsd == null ? null : perdaUsd / Math.max(.01, saldo) * 100;
  const status = riscoPct == null ? 'CONFIRMAR MANUALMENTE' : (riscoPct <= 1 ? 'ALAVANCAGEM OK' : (riscoPct <= 2 ? 'ALAVANCAGEM ALTA' : 'NÃO OPERAR — RISCO EXCESSIVO'));
  const cor = status === 'ALAVANCAGEM OK' ? '#4ade80' : (status.includes('NÃO') ? '#f87171' : '#fbbf24');
  document.getElementById('gestaoResumo').innerHTML = `<b style="color:${cor}">${status}</b> &bull; risco limite: R$${(saldo*.01).toFixed(2)} &bull; stop diário: R$${(saldo*.02).toFixed(2)}<br>Margem estimada: ${margemUsd == null ? 'confirme na IQ' : 'US$'+margemUsd.toFixed(2)} &bull; perda no SL: ${perdaUsd == null ? 'confirme na IQ' : 'US$'+perdaUsd.toFixed(2)}${riscoPct == null ? '' : ' ('+riscoPct.toFixed(2)+'%)'}`;
}
['saldo','lote','alavancagem'].forEach(id => document.getElementById(id).addEventListener('input', () => atualizarGestao(dadosGlobais.leituras?.find(x=>x.ativo===parAtivo))));
const escapar = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function zoomGrafico(delta) {
  velasVisiveis = Math.max(20, Math.min(60, velasVisiveis - delta));
  renderGrafico();
}

function calcularEma(candles, periodo) {
  const k=2/(periodo+1); let anterior=null;
  return candles.map(c => { const v=Number(c.c); anterior=anterior===null?v:(v*k+anterior*(1-k)); return anterior; });
}
function renderSimulador() {
  const el = document.getElementById('simulador'); if (!el) return;
  const r = simulacaoGlobal.resumo || {}, s = (simulacaoGlobal.sinais || []).filter(x => x.ativo === parAtivo);
  const cor = x => x === 'WIN' ? '#4ade80' : (x === 'LOSS' ? '#f87171' : '#fbbf24');
  const lista = s.slice(-5).reverse().map(x => `<div class="evento"><b>${escapar(x.resultado_simulado)}</b> — binária 30m: ${escapar(x.binaria_30m || '—')} · 60m: ${escapar(x.binaria_60m || '—')} · ${x.candles_analisados || 0} candles</div>`).join('');
  el.innerHTML = `<div class="card"><div class="acao" style="color:#38bdf8">${parAtivo} · WIN <span style="color:#4ade80">${r.WIN||0}</span> · LOSS <span style="color:#f87171">${r.LOSS||0}</span> · EXPIRADOS <span style="color:#fbbf24">${r.EXPIRADO||0}</span></div><div class="instrucao">Expirado não é loss: TP/SL não foram tocados nos candles disponíveis. Planilha: diario/simulacao_resultados.xlsx</div>${lista || '<span class="empty">Sem simulação para este par.</span>'}</div>`;
}

function textoDecisao(l) {
  if (l.estado_preco === 'PREÇO NA ZONA' && Number(l.rr_estimado) >= 1.5 && Number(l.qualidade) >= 65) return ['ENTRAR SE HOUVER REAÇÃO', '#4ade80'];
  if (l.estado_preco === 'PREÇO NA ZONA') return ['ZONA FRACA — AGUARDAR', '#fbbf24'];
  if (String(l.estado_preco).includes('AGUARDAR')) return ['AGUARDAR PREÇO', '#fbbf24'];
  return ['EVITAR AGORA', '#f87171'];
}
function estadoEntrada(l, noticias) {
  const alinhada = l.tendencia === l.tendencia_m15 && l.tendencia !== 'LATERAL';
  const noticiaContra = noticias && noticias.status === 'DIRECIONAL' && ((noticias.direcao_noticia === 'CALL' && l.lado_entrada !== 'COMPRA') || (noticias.direcao_noticia === 'PUT' && l.lado_entrada !== 'VENDA'));
  if (noticiaContra || (noticias && noticias.status === 'EVITAR')) return ['BLOQUEADA', '#f87171'];
  if (l.estado_preco === 'PREÇO NA ZONA' && Number(l.rr_estimado) >= 1.5 && Number(l.qualidade) >= 65 && alinhada && (l.confluencias||[]).length >= 2) return ['ENTRADA CONFIRMADA', '#4ade80'];
  if (String(l.estado_preco).includes('AGUARDAR') || l.estado_preco === 'PREÇO NA ZONA') return ['APROXIMANDO DA ZONA', '#fbbf24'];
  return ['AGUARDAR CONFIGURAÇÃO', '#f87171'];
}
function planoBinaria(l) {
  const zona = l.estado_preco === 'PREÇO NA ZONA';
  const boa = Number(l.qualidade) >= 65 && (l.confluencias || []).length >= 2;
  const direcao = l.lado_entrada === 'COMPRA' ? 'CALL' : 'PUT';
  if (zona && boa) return { estado: 'ENTRADA BINÁRIA CONFIRMADA', cor: '#4ade80', texto: `${direcao} · expiração sugerida: 30 min (2 candles M15)` };
  if (zona || String(l.estado_preco).includes('AGUARDAR')) return { estado: 'AGUARDAR REAÇÃO', cor: '#fbbf24', texto: `${direcao} somente após reação na zona · 30–60 min` };
  return { estado: 'SEM ENTRADA BINÁRIA', cor: '#f87171', texto: 'Preço fora da zona ideal.' };
}

// ---- tabs ----
const tabsEl = document.getElementById('tabs');
ATIVOS.forEach(a => {
  const b = document.createElement('button');
  b.className = 'tab' + (a === parAtivo ? ' ativo' : '');
  b.textContent = a; b.dataset.par = a;
  b.onclick = () => { parAtivo = a; renderTabs(); carregarCandles(a); };
  tabsEl.appendChild(b);
});

function renderTabs() {
  const somente = document.getElementById('soOportunidades')?.checked;
  const leituras = dadosGlobais.leituras || [];
  const nota = a => { const l=leituras.find(x=>x.ativo===a); return l ? Number(l.qualidade||0)+Number(l.rr_estimado||0)*10 : -1; };
  [...tabsEl.querySelectorAll('.tab')].sort((a,b)=>nota(b.dataset.par)-nota(a.dataset.par)).forEach(b => {
    const l=leituras.find(x=>x.ativo===b.dataset.par);
    b.style.display = somente && !(l && Number(l.qualidade)>=65 && Number(l.rr_estimado)>=1.5) ? 'none' : '';
    b.className = 'tab' + (b.dataset.par === parAtivo ? ' ativo' : '');
    const temSinal = dadosGlobais.sinais && dadosGlobais.sinais.some(s => s.ativo === b.dataset.par);
    if (temSinal) b.classList.add('sinal');
    tabsEl.appendChild(b);
  });
}
document.getElementById('soOportunidades').addEventListener('change', renderTabs);

function copiarPlano(l) {
  const texto = `${l.ativo} ${l.lado_entrada}\nEntrada: ${l.entrada_min}–${l.entrada_max}\nAlvo: ${l.alvo}\nInvalidação/SL: ${l.invalidacao}\nR:R: ${l.rr_estimado}x`;
  navigator.clipboard?.writeText(texto).then(()=>alert('Plano copiado.')).catch(()=>{});
}

// ---- candles ----
async function carregarCandles(par) {
  try {
    const r = await fetch('candles_' + par + '.json?t=' + Date.now());
    candlesAtuais = await r.json();
    renderGrafico();
  } catch(e) { candlesAtuais = []; renderGrafico(); }
}

// ---- grafico ----
function renderGrafico() {
  const cv = document.getElementById('cv');
  const W = cv.parentElement.clientWidth || 800;
  const H = 320;
  cv.width = W; cv.height = H;
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#0d1526'; ctx.fillRect(0,0,W,H);

  const todos = (candlesAtuais || []).filter(c => [c.o,c.h,c.l,c.c].map(Number).every(Number.isFinite));
  const data = todos.slice(-velasVisiveis);
  if (!data || data.length === 0) {
    ctx.fillStyle='#475569'; ctx.font='14px monospace';
    ctx.fillText('Aguardando dados...', W/2-70, H/2); return;
  }

  const PAD = {l:8, r:82, t:24, b:28};
  const cw = (W - PAD.l - PAD.r) / data.length;
  const bodyW = Math.max(1, cw * 0.6);

  const sinal = dadosGlobais.sinais && dadosGlobais.sinais.find(s => s.ativo === parAtivo);
  const leitura = dadosGlobais.leituras && dadosGlobais.leituras.find(x => x.ativo === parAtivo);
  const sim = (simulacaoGlobal.sinais || []).filter(x => x.ativo === parAtivo).slice(-1)[0];
  const niveis = (sinal ? [sinal.entrada, sinal.sl, sinal.tp, sinal.preco_atual] : [])
    .concat(leitura ? [leitura.alvo, leitura.invalidacao, leitura.entrada_min, leitura.entrada_max] : [])
    .map(Number).filter(Number.isFinite);
  const candlesValidos = data.filter(c => [c.o,c.h,c.l,c.c].map(Number).every(Number.isFinite));
  if (candlesValidos.length === 0) {
    ctx.fillStyle='#475569'; ctx.font='14px monospace';
    ctx.fillText('Dados de candles inválidos', W/2-95, H/2); return;
  }
  const highs = candlesValidos.map(c=>Number(c.h)).concat(niveis);
  const lows = candlesValidos.map(c=>Number(c.l)).concat(niveis);
  let vmax = Math.max(...highs), vmin = Math.min(...lows);
  const range = vmax - vmin || 0.0001;
  vmax += range * 0.05; vmin -= range * 0.05;
  const toY = v => PAD.t + (vmax - v) / (vmax - vmin) * (H - PAD.t - PAD.b);

  const desenharEma = (valores, cor) => {
    const serie=valores.slice(-data.length); ctx.strokeStyle=cor; ctx.lineWidth=1.2; ctx.setLineDash([]);
    ctx.beginPath(); serie.forEach((v,i) => { const x=PAD.l+i*cw+cw/2,y=toY(v); i?ctx.lineTo(x,y):ctx.moveTo(x,y); }); ctx.stroke();
  };
  desenharEma(calcularEma(todos,20),'#38bdf8');
  desenharEma(calcularEma(todos,50),'#f59e0b');

  // faixa preferencial baseada na confluência mais forte a favor da tendência
  if (leitura && Number.isFinite(Number(leitura.entrada_min)) && Number.isFinite(Number(leitura.entrada_max))) {
    const y1=toY(leitura.entrada_max), y2=toY(leitura.entrada_min);
    const cor=leitura.lado_entrada==='COMPRA' ? 'rgba(34,197,94,.16)' : 'rgba(239,68,68,.16)';
    const borda=leitura.lado_entrada==='COMPRA' ? '#22c55e' : '#ef4444';
    ctx.fillStyle=cor; ctx.fillRect(PAD.l,y1,W-PAD.l-PAD.r,Math.max(2,y2-y1));
    ctx.strokeStyle=borda; ctx.setLineDash([5,3]); ctx.strokeRect(PAD.l,y1,W-PAD.l-PAD.r,Math.max(2,y2-y1)); ctx.setLineDash([]);
    ctx.fillStyle=borda; ctx.font='bold 9px monospace'; ctx.textAlign='left';
    ctx.fillText(`ZONA ${leitura.lado_entrada}`,PAD.l+6,Math.max(PAD.t+10,y1-4));
  }

  // linhas de grade
  ctx.strokeStyle = '#1e293b'; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = PAD.t + i * (H - PAD.t - PAD.b) / 4;
    ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(W - PAD.r, y); ctx.stroke();
    const v = vmax - i * (vmax - vmin) / 4;
    ctx.fillStyle='#475569'; ctx.font='9px monospace'; ctx.textAlign='right';
    ctx.fillText(v.toFixed(5), W - PAD.r - 2, y - 2);
  }

  // sinal: linhas de entry/SL/TP
  if (sinal) {
    const linhas = [
      {v: sinal.entrada, cor:'#93c5fd', rot:'ENTRY'},
      {v: sinal.sl,      cor:'#f87171', rot:'SL'},
      {v: sinal.tp,      cor:'#4ade80', rot:'TP'},
    ];
    linhas.filter(({v}) => Number.isFinite(Number(v))).forEach(({v,cor,rot}) => {
      const y = toY(v);
      ctx.strokeStyle = cor; ctx.lineWidth = 1;
      ctx.setLineDash([4,3]);
      ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(W - PAD.r - 32, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = cor; ctx.font = 'bold 9px monospace'; ctx.textAlign = 'left';
      ctx.fillText(rot, W - PAD.r - 30, y + 3);
    });
  }
  if (leitura) {
    const rotulos = [{v:leitura.alvo,cor:'#fbbf24',rot:'ALVO POSSÍVEL'},
     {v:leitura.invalidacao,cor:'#a78bfa',rot:'INVALIDAÇÃO'},
     {v:leitura.suporte,cor:'#2dd4bf',rot:'SUPORTE'},
     {v:leitura.resistencia,cor:'#fb7185',rot:'RESISTÊNCIA'}]
      .filter(({v}) => Number.isFinite(Number(v))).map(x=>({...x,y:toY(x.v)})).sort((a,b)=>a.y-b.y);
    let ultimoRotulo=-999;
    rotulos.forEach(({v,cor,rot,y}) => {
        const rotY=Math.min(H-PAD.b-2,Math.max(y,ultimoRotulo+12)); ultimoRotulo=rotY;
        ctx.strokeStyle=cor; ctx.lineWidth=1; ctx.setLineDash([7,4]);
        ctx.beginPath(); ctx.moveTo(PAD.l,y); ctx.lineTo(W-PAD.r-75,y); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle=cor; ctx.font='bold 9px monospace'; ctx.textAlign='left'; ctx.fillText(rot,W-PAD.r-73,rotY+3);
      });
  }

  if (sim) {
    const txt = `SIMULAÇÃO: ${sim.resultado_simulado}`;
    ctx.fillStyle = sim.resultado_simulado === 'WIN' ? '#166534' : (sim.resultado_simulado === 'LOSS' ? '#991b1b' : '#92400e');
    ctx.fillRect(PAD.l + 6, PAD.t + 4, 190, 18);
    ctx.fillStyle = '#fff'; ctx.font = 'bold 10px monospace'; ctx.textAlign = 'left'; ctx.fillText(txt, PAD.l + 12, PAD.t + 17);
  }

  // candles
  candlesValidos.forEach((c, i) => {
    const x = PAD.l + i * cw + cw / 2;
    const alta = c.c >= c.o;
    const cor = alta ? '#22c55e' : '#ef4444';
    const yO = toY(c.o), yC = toY(c.c), yH = toY(c.h), yL = toY(c.l);

    // sombra
    ctx.strokeStyle = cor; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, yH); ctx.lineTo(x, yL); ctx.stroke();

    // corpo
    const yTop = Math.min(yO, yC), bodyH = Math.max(1, Math.abs(yC - yO));
    ctx.fillStyle = cor;
    ctx.fillRect(x - bodyW/2, yTop, bodyW, bodyH);
  });

  // preço vivo e contagem para o fechamento M15, no estilo da plataforma
  const ultimo = candlesValidos[candlesValidos.length - 1];
  const precoVivo = Number(ultimo.c);
  if (Number.isFinite(precoVivo)) {
    const y = toY(precoVivo), alta = precoVivo >= Number(ultimo.o);
    const cor = alta ? '#22c55e' : '#ef4444';
    ctx.strokeStyle=cor; ctx.lineWidth=1; ctx.setLineDash([2,2]);
    ctx.beginPath(); ctx.moveTo(PAD.l,y); ctx.lineTo(W-PAD.r,y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle=cor; ctx.fillRect(W-PAD.r+3,y-9,PAD.r-6,18);
    ctx.fillStyle='#fff'; ctx.font='bold 10px monospace'; ctx.textAlign='center';
    ctx.fillText(precoVivo.toFixed(parAtivo.includes('JPY') ? 3 : 5),W-PAD.r/2+1,y+4);
    const restante = Math.max(0, 900 - (Math.floor(Date.now()/1000) % 900));
    const mm=String(Math.floor(restante/60)).padStart(2,'0'), ss=String(restante%60).padStart(2,'0');
    ctx.fillStyle='#94a3b8'; ctx.font='10px monospace'; ctx.textAlign='right';
    ctx.fillText(`M15 ${mm}:${ss}`,W-PAD.r-4,15);
  }

  // eixo tempo: primeiro e ultimo
  ctx.fillStyle='#475569'; ctx.font='9px monospace'; ctx.textAlign='center';
  if (data.length > 0) {
    const fmt = t => { const d=new Date(t*1000); return d.getUTCHours().toString().padStart(2,'0')+':'+d.getUTCMinutes().toString().padStart(2,'0'); };
    ctx.fillText(fmt(data[0].t),   PAD.l + cw/2,       H - 6);
    ctx.fillText(fmt(data[data.length-1].t), W - PAD.r - cw/2, H - 6);
  }
}

// ---- dados principais ----
async function atualizar() {
  try {
    const r = await fetch('forex_monitor.json?t=' + Date.now());
    dadosGlobais = await r.json();
    try { const sr = await fetch('simulacao_resultados.json?t=' + Date.now()); simulacaoGlobal = await sr.json(); } catch(e) {}
    document.getElementById('status').textContent = 'Atualizado: ' + dadosGlobais.status;

    renderTabs();

    const leituraDiv = document.getElementById('leitura');
    const leitura = dadosGlobais.leituras && dadosGlobais.leituras.find(x => x.ativo === parAtivo);
    const noticiasAtual = dadosGlobais.noticias && dadosGlobais.noticias.find(x => x.ativo === parAtivo);
    if (!leitura) {
      leituraDiv.innerHTML = '<span class="empty">Leitura ainda indisponível</span>';
    } else {
      const cls = leitura.tendencia.toLowerCase();
      const [decisao, corDecisao] = textoDecisao(leitura);
      const [estadoEntradaTxt, corEstadoEntrada] = estadoEntrada(leitura, noticiasAtual);
      const binaria = planoBinaria(leitura);
      const qualidadeTxt = Number(leitura.qualidade) >= 65 ? 'BOA' : (Number(leitura.qualidade) >= 50 ? 'MEDIANA' : 'FRACA');
      const confs = leitura.confluencias.length
        ? leitura.confluencias.map(x => `<span class="chip">${x}</span>`).join('')
        : '<span class="chip">nenhuma confluência próxima</span>';
      leituraDiv.innerHTML = `<div class="card leitura">
        <div class="decisao" style="color:${corEstadoEntrada}">${estadoEntradaTxt}</div><div class="instrucao" style="color:${corDecisao}">${decisao}</div>
        <div class="acao ${cls}">${leitura.ativo} — H1 ${leitura.tendencia} | M15 ${leitura.tendencia_m15}</div>
        <div><b>${leitura.estado_preco}</b> — ${leitura.contexto}</div><div class="grid">
          <div class="item"><label>Preço atual</label><span>${leitura.preco}</span></div>
          <div class="item"><label>Alvo possível</label><span style="color:#fbbf24">${leitura.alvo}</span></div>
          <div class="item"><label>Alvo alternativo</label><span>${leitura.alvo_alternativo}</span></div>
          <div class="item"><label>Invalidação</label><span style="color:#a78bfa">${leitura.invalidacao}</span></div>
          <div class="item"><label>Melhor zona para ${leitura.lado_entrada}</label><span style="color:#38bdf8">${leitura.entrada_min} – ${leitura.entrada_max}</span></div>
          <div class="item"><label>Distância até a zona</label><span>${leitura.distancia_zona_pips} ${leitura.ativo.startsWith('BTC')||leitura.ativo.startsWith('ETH')||leitura.ativo.startsWith('LTC')||leitura.ativo.startsWith('XRP')||leitura.ativo.startsWith('BCH')||leitura.ativo.startsWith('EOS')||leitura.ativo.startsWith('ETC')||leitura.ativo.startsWith('DASH')||leitura.ativo.startsWith('TRX')||leitura.ativo.startsWith('ZEC')||leitura.ativo.startsWith('ADA')||leitura.ativo.startsWith('SOL')||leitura.ativo.startsWith('DOT')||leitura.ativo.startsWith('LINK')||leitura.ativo.startsWith('UNI')||leitura.ativo.startsWith('DOGE') ? 'USD' : 'pips'}</span></div>
          <div class="item"><label>R:R estimado na zona</label><span>${leitura.rr_estimado}x</span></div>
          <div class="item"><label>RSI / ATR</label><span>${leitura.rsi14} / ${leitura.atr_pips} ${leitura.ativo.length > 3 && !leitura.ativo.includes('JPY') && !['EURUSD','GBPUSD','AUDUSD','USDCAD','NZDUSD','EURJPY'].includes(leitura.ativo) ? 'USD' : 'pips'}</span></div>
          <div class="item"><label>Suporte / Resistência</label><span>${leitura.suporte} / ${leitura.resistencia}</span></div>
          <div class="item"><label>Qualidade do contexto</label><span>${leitura.qualidade}/100</span></div>
        </div><div class="instrucao" style="color:${binaria.cor}"><b>${binaria.estado}</b> — ${binaria.texto}<br><small>Modo experimental: validar no simulador antes de operar.</small></div><div><b>Faixas:</b> <span class="faixa">Ideal: ${leitura.entrada_min}–${leitura.entrada_max}</span><span class="faixa">Aceitável: confirme perto da zona</span><span class="faixa">Atrasada: não entrar fora da zona</span></div><button class="zoom-btn" onclick='copiarPlano(${JSON.stringify(leitura)})'>copiar plano</button><div class="chips">${confs}</div><div class="instrucao">Qualidade ${qualidadeTxt}: ${leitura.contexto}</div></div>`;
      atualizarGestao(leitura);
    }
    const noticiasDiv = document.getElementById('noticias');
    const noticias = dadosGlobais.noticias && dadosGlobais.noticias.find(x => x.ativo === parAtivo);
    if (!noticias) {
      noticiasDiv.innerHTML = '<span class="empty">Consultando calendário...</span>';
    } else if (!noticias.eventos || noticias.eventos.length === 0) {
      noticiasDiv.innerHTML = `<div class="card noticia-livre"><b>${noticias.nota ? 'NÃO APLICÁVEL' : 'LIVRE'}</b> — ${noticias.nota || 'sem notícia relevante nas próximas 8 horas.'}</div>`;
    } else {
      const risco = noticias.status === 'EVITAR';
      const direcional = noticias.status === 'DIRECIONAL';
      const eventos = noticias.eventos.map(e => {
        const quando = e.minutos >= 0 ? `em ${e.minutos} min` : `há ${Math.abs(e.minutos)} min`;
        const resultado = e.direcao_resultado ? ` &bull; resultado favorece ${escapar(e.direcao_resultado)}` : '';
        const valores = (e.actual || e.forecast || e.previous) ? ` &bull; Actual: ${escapar(e.actual || '—')} | Forecast: ${escapar(e.forecast || '—')} | Previous: ${escapar(e.previous || '—')}` : '';
        return `<div class="evento"><b>${escapar(e.impacto)}</b> ${escapar(e.moeda)} — ${escapar(e.titulo)}
          &bull; ${escapar(e.horario_utc)} UTC &bull; ${quando}${valores}${resultado}</div>`;
      }).join('');
      const sugestao = direcional ? `<div class="instrucao" style="color:#fbbf24"><b>SUGESTÃO: ENTRAR A FAVOR — ${noticias.direcao_noticia}</b><br>Confirme reação do preço e zona técnica antes de abrir.</div>` : '';
      noticiasDiv.innerHTML = `<div class="card ${risco?'noticia-risco':'noticia-livre'}">
        <div class="acao" style="color:${risco?'#f87171':(direcional?'#fbbf24':'#4ade80')}">${risco?'EVITAR ±20 MIN':(direcional?'NOTÍCIA CONFIRMADA → '+noticias.direcao_noticia:'FORA DA JANELA DE RISCO')}</div>${eventos}${sugestao}</div>`;
    }
    const estatDiv = document.getElementById('estatisticas');
    const ex = dadosGlobais.execucao && dadosGlobais.execucao.find(x => x.ativo === parAtivo);
    const est = dadosGlobais.estatisticas || {};
    const porPar = est.por_par && est.por_par[parAtivo];
    if (ex) {
      const corEx = ex.status === 'CUIDADO' ? '#f87171' : '#4ade80';
      const spreadTxt = ex.spread_estimado_pips == null ? 'não calculado (confirme na IQ)' : `${ex.spread_estimado_pips} ${ex.unidade || 'pips'} (limite ${ex.limite_pips})`;
      estatDiv.innerHTML = `<div class="card"><div class="acao" style="color:${corEx}">EXECUÇÃO: ${ex.status}</div><div>${ex.nota}</div><div>Spread estimado: <b>${spreadTxt}</b></div><div class="instrucao">${porPar ? `Histórico ${parAtivo}: ${porPar.trades} fechadas · ${porPar.winrate}% win · PnL ${porPar.pnl}` : 'Ainda sem fechamentos oficiais para este par.'}</div></div>`;
    }
    const sinalAtivo = dadosGlobais.sinais && dadosGlobais.sinais.find(s => s.ativo === parAtivo);
    const iaAtiva = dadosGlobais.analises_ia && dadosGlobais.analises_ia.find(x => x.ativo === parAtivo);
    const alerta = sinalAtivo ? `${parAtivo}:sinal:${sinalAtivo.gerado_em}` : (iaAtiva && iaAtiva.status === 'FAVORAVEL' ? `${parAtivo}:ia:${iaAtiva.resumo}` : '');
    if (alerta && alerta !== ultimoAlerta) {
      ultimoAlerta = alerta;
      try { const ac = new (window.AudioContext || window.webkitAudioContext)(); const o=ac.createOscillator(), g=ac.createGain(); o.frequency.value=880; g.gain.value=.04; o.connect(g); g.connect(ac.destination); o.start(); o.stop(ac.currentTime+.18); } catch(e) {}
    }
    const horarioDiv = document.getElementById('horario');
    const horario = dadosGlobais.horarios && dadosGlobais.horarios.find(x => x.ativo === parAtivo);
    if (horario) {
      const corHorario = horario.nivel === 'PERIGOSO' ? '#f87171' : (horario.nivel === 'LENTO' ? '#fbbf24' : '#4ade80');
      const sessoes = horario.sessoes_ativas && horario.sessoes_ativas.length ? horario.sessoes_ativas.join(' + ') : 'nenhuma';
      horarioDiv.innerHTML = `<div class="card"><div class="acao" style="color:${corHorario}">${horario.nivel} — ${horario.moedas}</div>
        <div>UTC ${horario.hora_utc} &bull; sessões ativas: <b>${sessoes}</b></div>
        <div>${horario.nota}</div><div class="instrucao">Maior liquidez estimada: ${horario.melhor_horario_utc} UTC.</div></div>`;
    }
    const iaDiv = document.getElementById('analise-ia');
    const ia = dadosGlobais.analises_ia && dadosGlobais.analises_ia.find(x => x.ativo === parAtivo);
    if (!ia) {
      iaDiv.innerHTML = '<span class="empty">Aguardando análise...</span>';
    } else {
      const cor = ia.status === 'FAVORAVEL' ? '#4ade80' : (ia.status === 'EVITAR' ? '#f87171' : '#fbbf24');
      const fonte = ia.fonte === 'LOCAL' ? 'ANÁLISE LOCAL' : (ia.fonte || 'IA');
      iaDiv.innerHTML = `<div class="card"><div class="acao" style="color:${cor}">${ia.status} — ${fonte}</div>
        ${ia.lado ? `<b>${ia.lado}</b> &bull; ` : ''}${ia.resumo || ''}
        ${ia.alvo_escolhido ? `<div class="instrucao">Alvo escolhido entre os calculados: ${ia.alvo_escolhido}</div>` : ''}</div>`;
    }

    const sinDiv = document.getElementById('sinais');
    if (!dadosGlobais.sinais || dadosGlobais.sinais.length === 0) {
      sinDiv.innerHTML = '<span class="empty">Nenhum sinal no momento</span>';
    } else {
      sinDiv.innerHTML = dadosGlobais.sinais.map(s => {
        const precoAtual = Number.isFinite(Number(s.preco_atual)) ? s.preco_atual : s.entrada;
        const rrAtual = Number.isFinite(Number(s.rr_atual)) ? s.rr_atual : s.rr;
        const validoAte = s.valido_ate || '--';
        const formatoAntigo = !s.acao;
        const entrar = s.acao === 'ENTRAR_AGORA';
        const acao = formatoAntigo ? 'REINICIE O MONITOR' : (entrar ? `ENTRAR MANUAL ${s.lado.toUpperCase()}` : (s.acao === 'OBSERVAR' ? 'OBSERVAÇÃO' : 'AGUARDAR PREÇO'));
        const instrucao = formatoAntigo
          ? `Este sinal veio de uma versão antiga. Reinicie o MONITOR_FOREX.bat antes de decidir.`
          : entrar
          ? `Preço ainda válido. Abra BUY no app IQ &bull; SL = ${s.sl} &bull; TP = ${s.tp}`
          : `Não entre agora. Espere o R:R voltar a 2.0 ou descarte ao fim desta vela.`;
        return `
        <div class="card ${entrar ? 'buy' : 'esperar'}">
          <div class="acao ${entrar ? 'entrar' : 'esperar'}">${acao}</div>
          <b>${s.ativo} BUY</b> — ${s.gerado_em} UTC
          <div class="grid">
            <div class="item"><label>Entrada planejada</label><span>${s.entrada}</span></div>
            <div class="item"><label>Preço atual</label><span>${precoAtual}</span></div>
            <div class="item"><label>SL</label><span style="color:#f87171">${s.sl}</span></div>
            <div class="item"><label>TP</label><span style="color:#4ade80">${s.tp}</span></div>
            <div class="item"><label>Risco (pips)</label><span>${s.risco_pips}</span></div>
            <div class="item"><label>R:R atual</label><span class="rr">${rrAtual}x</span></div>
            <div class="item"><label>Válido até</label><span>${validoAte} UTC</span></div>
          </div>
          <div class="instrucao">${instrucao}</div>
        </div>`;
      }).join('');
      // muda para o par com sinal automaticamente
      if (!dadosGlobais.sinais.some(s => s.ativo === parAtivo)) {
        parAtivo = dadosGlobais.sinais[0].ativo;
        renderTabs();
      }
    }

    const posDiv = document.getElementById('posicoes');
    if (!dadosGlobais.posicoes || dadosGlobais.posicoes.length === 0) {
      posDiv.innerHTML = '<span class="empty">Nenhuma posicao aberta</span>';
    } else {
      posDiv.innerHTML = dadosGlobais.posicoes.map(p => {
        const pnl = Number(p.pnl || 0);
        const cor = pnl >= 0 ? '#4ade80' : '#f87171';
        return `<div class="pos-card">
          <b>${p.ativo}</b> ${p.lado.toUpperCase()} | Entrada: ${p.entrada} |
          P&L: <span style="color:${cor}">${pnl>0?'+':''}${pnl.toFixed(2)}</span>
          ${p.sl ? '| SL: '+p.sl : ''} ${p.tp ? '| TP: '+p.tp : ''}
        </div>`;
      }).join('');
    }

    renderGrafico();
    renderSimulador();
  } catch(e) {
    document.getElementById('status').textContent = 'Erro: ' + e;
  }
}

async function ciclo() {
  await atualizar();
  await carregarCandles(parAtivo);
}

ciclo();
setInterval(atualizar, 5000);
setInterval(() => carregarCandles(parAtivo), 1000);
window.addEventListener('resize', renderGrafico);
document.getElementById('cv').addEventListener('wheel', e => {
  e.preventDefault(); zoomGrafico(e.deltaY > 0 ? -5 : 5);
}, {passive:false});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------

def main() -> int:
    config = configuracao_scalping_m15()

    print("Conectando na IQ Option...")
    mercado = MercadoIQ(config)
    api = mercado.conectar_somente_leitura()

    # Reutiliza a infraestrutura de grafico mas com porta propria
    import dataclasses
    cfg_forex = dataclasses.replace(
        config,
        porta_grafico=PORTA,
        sufixo_banco="forex_monitor",
    )
    grafico = GraficoM5(cfg_forex)
    grafico.iniciar(abrir_navegador=False)

    # Sobrescreve o index.html padrao com o do monitor forex
    (grafico.pasta_web / "index.html").write_text(_HTML, encoding="utf-8")

    pasta_diario = Path.home() / "Documents" / "IQOptionM5" / "diario"
    pasta_diario.mkdir(parents=True, exist_ok=True)
    diario = DiarioTrades(pasta_diario)
    print(f"Diario em: {pasta_diario}")

    calendario = CalendarioEconomico(config.pasta_dados, ttl_segundos=60)
    calendario.atualizar(forcar=True)
    print(f"Historico de noticias: {calendario.arquivo_historico}")

    def coletar_noticias() -> None:
        while True:
            calendario.atualizar()
            time.sleep(60)

    threading.Thread(target=coletar_noticias, name="forex-noticias", daemon=True).start()

    servidor = ServidorDadosForex(grafico.pasta_web)
    gemini = AnalistaGemini()
    print("IA: Gemini=" + ("ATIVO" if gemini.gemini_ativo else "OFF") +
          " | Groq=" + ("ATIVO" if gemini.groq_ativo else "OFF"))
    rastreador = RastreadorPosicoes(api, diario)

    porta_real = grafico.porta or PORTA
    url = f"http://127.0.0.1:{porta_real}/index.html"
    print(f"Monitor forex: {url}")
    webbrowser.open(url)

    # Loop de posicoes em thread separada
    t_pos = threading.Thread(
        target=loop_posicoes, args=(api, servidor, rastreador, diario),
        name="forex-posicoes", daemon=True
    )
    t_pos.start()

    # Loop de sinais na thread principal
    print(f"Monitorando {len(ATIVOS)} pares M15... (Ctrl+C para parar)")
    try:
        loop_sinais(api, servidor, diario, config, gemini, calendario)
    except KeyboardInterrupt:
        print("\nParado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
