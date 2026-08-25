"""Agente de Regime via Groq — classifica mercado H4 por par, bloqueia contra-tendência.

Roda a cada H4 (4h) por ativo. Retorna RegimerMercado com direção permitida.
Nunca é decisivo: se falhar, o bot opera normalmente com o filtro EMA H4.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

import pandas as pd
import requests as _req

_URL = "https://api.groq.com/openai/v1/chat/completions"
_MODELO = "groq/compound-mini"
_TIMEOUT = 12.0
_MAX_TOKENS = 150

_bloqueado_ate = 0.0
_lock = threading.Lock()
_sem = threading.Semaphore(1)  # 1 chamada por vez


@dataclass(frozen=True)
class RegimeMercado:
    ativo: str
    regime: str          # "alta" | "baixa" | "lateral"
    confianca: int       # 1-10
    bloquear_call: bool
    bloquear_put: bool
    resumo: str
    gerado_em: float = field(default_factory=time.time)


def _chave() -> str:
    k = os.environ.get("GROQ_API_KEY", "").strip()
    if not k:
        raise RuntimeError("GROQ_API_KEY nao definida")
    return k


def _resumo_candles(df: pd.DataFrame, n: int = 8) -> str:
    """Formata os últimos N candles como texto compacto para o prompt."""
    ultimos = df.tail(n)
    linhas = []
    for ts, row in ultimos.iterrows():
        linhas.append(
            f"{str(ts)[:13]}h O={row['Open']:.5f} H={row['High']:.5f} "
            f"L={row['Low']:.5f} C={row['Close']:.5f}"
        )
    return "\n".join(linhas)


def _ema(serie: pd.Series, periodo: int) -> float:
    return float(serie.ewm(span=periodo, adjust=False).mean().iloc[-1])


def _rsi(close: pd.Series, periodo: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(periodo).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(periodo).mean().iloc[-1]
    if loss == 0:
        return 100.0
    return round(100 - 100 / (1 + gain / loss), 1)


def _montar_prompt(ativo: str, df_h4: pd.DataFrame, df_h1: pd.DataFrame) -> str:
    close_h4 = df_h4["Close"]
    ema9_h4 = _ema(close_h4, 9)
    ema21_h4 = _ema(close_h4, 21)
    rsi_h4 = _rsi(close_h4)
    preco_atual = float(close_h4.iloc[-1])
    atr_h4 = float((df_h4["High"] - df_h4["Low"]).rolling(14).mean().iloc[-1])

    close_h1 = df_h1["Close"]
    ema9_h1 = _ema(close_h1, 9)
    rsi_h1 = _rsi(close_h1)

    candles_txt = _resumo_candles(df_h4, n=6)

    return f"""Classifique o regime de mercado atual para {ativo}.

H4 (macro):
EMA9={ema9_h4:.5f} EMA21={ema21_h4:.5f} preco={preco_atual:.5f} RSI={rsi_h4} ATR={atr_h4:.5f}
Ultimos 6 candles H4:
{candles_txt}

H1 (micro):
EMA9_H1={ema9_h1:.5f} RSI_H1={rsi_h1}

Responda SOMENTE com JSON (sem texto fora):
{{"regime":"alta|baixa|lateral","confianca":1-10,"bloquear_call":true|false,"bloquear_put":true|false,"resumo":"1 frase objetiva"}}

Regras:
- "alta": preco>EMA9>EMA21 + RSI>50 + candles fazendo HH/HL -> bloquear_put=true
- "baixa": preco<EMA9<EMA21 + RSI<50 + candles fazendo LL/LH -> bloquear_call=true
- "lateral": EMA9≈EMA21 (diferenca<0.5*ATR) ou RSI entre 40-60 sem direção -> nao bloquear nada
- Confianca alta (8-10): todos os fatores concordam. Media (5-7): maioria. Baixa (1-4): conflito."""


def classificar(ativo: str, df_h4: pd.DataFrame, df_h1: pd.DataFrame) -> RegimeMercado | None:
    """Chama o agente e retorna o regime de mercado. Retorna None se falhar."""
    global _bloqueado_ate

    with _lock:
        if time.time() < _bloqueado_ate:
            return None

    if not _sem.acquire(blocking=False):
        return None

    try:
        prompt = _montar_prompt(ativo, df_h4, df_h1)
        try:
            chave = _chave()
        except RuntimeError as e:
            print(f"    [REGIME] {ativo}: {e}")
            return None

        body = {
            "model": _MODELO,
            "messages": [
                {
                    "role": "system",
                    "content": "Analista tecnico forex. Responda SOMENTE JSON valido. Sem texto antes ou depois.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": _MAX_TOKENS,
        }

        try:
            resp = _req.post(
                _URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {chave}",
                    "Content-Type": "application/json",
                },
                timeout=_TIMEOUT,
            )
        except _req.RequestException as e:
            print(f"    [REGIME] {ativo}: erro de conexao — {e}")
            return None

        if resp.status_code == 429:
            with _lock:
                pausa = 300 if "day" in resp.text else 60
                _bloqueado_ate = time.time() + pausa
            print(f"    [REGIME] rate limit — pausando {pausa}s")
            return None

        if resp.status_code != 200:
            print(f"    [REGIME] {ativo}: HTTP {resp.status_code}")
            return None

        try:
            conteudo = resp.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            return None

        return _parsear(ativo, conteudo)

    finally:
        _sem.release()


def _parsear(ativo: str, conteudo: str) -> RegimeMercado | None:
    ini = conteudo.find("{")
    fim = conteudo.rfind("}") + 1
    if ini < 0 or fim <= ini:
        print(f"    [REGIME] {ativo}: resposta nao JSON: {conteudo[:80]}")
        return None
    try:
        obj = json.loads(conteudo[ini:fim])
        regime = str(obj.get("regime", "lateral")).lower()
        if regime not in ("alta", "baixa", "lateral"):
            regime = "lateral"
        confianca = max(1, min(10, int(obj.get("confianca", 5))))
        bloquear_call = bool(obj.get("bloquear_call", False))
        bloquear_put = bool(obj.get("bloquear_put", False))
        # confiança baixa → não bloqueia
        if confianca < 6:
            bloquear_call = False
            bloquear_put = False
        resumo = str(obj.get("resumo", ""))[:150]
        return RegimeMercado(
            ativo=ativo,
            regime=regime,
            confianca=confianca,
            bloquear_call=bloquear_call,
            bloquear_put=bloquear_put,
            resumo=resumo,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"    [REGIME] {ativo}: parse error — {e}")
        return None
