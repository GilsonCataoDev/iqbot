"""Analista de IA via Groq (Llama 3.3 70B) — opiniao complementar, nunca decisiva."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

import requests as _req

URL_GROQ = "https://api.groq.com/openai/v1/chat/completions"
MODELO = "llama-3.3-70b-versatile"
TIMEOUT_SEGUNDOS = 15
MAX_TOKENS_RESPOSTA = 400

_bloqueado_ate = 0.0
_lock_bloqueio = threading.Lock()
# Permite apenas 1 chamada à API por vez. Chamadas concorrentes retornam None
# imediatamente em vez de brigar pela cota e causar cascata de rate-limit.
_sem_ia = threading.Semaphore(1)

_HEADERS_BASE = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}


@dataclass(frozen=True)
class ParecerIA:
    texto: str
    confianca: str  # "alta", "media", "baixa"
    direcao_sugerida: str | None  # "CALL", "PUT" ou None
    gerado_em: float = field(default_factory=time.time)


def _obter_chave() -> str:
    chave = os.environ.get("GROQ_API_KEY", "").strip()
    if not chave:
        raise RuntimeError(
            "GROQ_API_KEY nao definida. Use o .bat ou defina a variavel de ambiente."
        )
    return chave


_chave_cache: str | None = None
_lock = threading.Lock()


def _chave() -> str:
    global _chave_cache
    if _chave_cache:
        return _chave_cache
    with _lock:
        if _chave_cache:
            return _chave_cache
        _chave_cache = _obter_chave()
        return _chave_cache


def _montar_prompt(contexto: dict) -> str:
    return f"""Analise tecnica de opcoes binarias M5. Responda APENAS o JSON abaixo, nada antes nem depois.

Dados:
ativo={contexto.get('ativo','?')} timeframe={contexto.get('timeframe','M5')} alerta={contexto.get('tipo','sem alerta')} {contexto.get('direcao','')}
RSI={contexto.get('rsi','?')} tendencia_macro={contexto.get('tendencia_macro','?')} tendencia_micro={contexto.get('tendencia_micro','?')}
corpo/ATR={contexto.get('corpo_atr','?')}x preco={contexto.get('preco','?')}
bollinger_sup={contexto.get('banda_sup','?')} bollinger_inf={contexto.get('banda_inf','?')}
EMA={contexto.get('ema_posicao','?')} motivos={contexto.get('motivos','nenhum')}
noticias={contexto.get('noticias','nenhuma')} sugestao_noticia={contexto.get('sugestao_noticia','nenhuma')}
horario={contexto.get('horario','?')}

Regras:
1. Se RSI>65 + preco perto/acima da bollinger_sup + tendencia alta = sobrecomprado, considere PUT
2. Se RSI<35 + preco perto/abaixo da bollinger_inf + tendencia baixa = sobrevendido, considere CALL
3. Se preco cruzou EMA + RSI confirmando = tendencia, siga a direcao
4. Se ha noticia forte proxima (<20min), reduza confianca
5. Confianca alta = 2+ indicadores concordam. Media = 1 indicador. Baixa = conflito ou incerteza
6. "texto" deve ter exatamente 1 frase objetiva dizendo o que fazer e por que

{{"texto": "...", "confianca": "alta|media|baixa", "direcao": "CALL|PUT|NEUTRO"}}"""


def analisar(contexto: dict) -> ParecerIA | None:
    global _bloqueado_ate
    if time.time() < _bloqueado_ate:
        return None

    # Não bloqueia: se outra chamada já está em andamento, descarta esta.
    if not _sem_ia.acquire(blocking=False):
        return None

    try:
        # Re-verifica após adquirir — pode ter mudado enquanto esperava o lock.
        if time.time() < _bloqueado_ate:
            return None

        print(f"    [IA] analisando {contexto.get('ativo', '?')}...")
        try:
            chave = _chave()
        except RuntimeError:
            print("    [IA] ERRO: chave Groq nao encontrada! Defina GROQ_API_KEY.")
            return None

        prompt = _montar_prompt(contexto)
        headers = {**_HEADERS_BASE, "Authorization": f"Bearer {chave}"}
        body = {
            "model": MODELO,
            "messages": [
                {"role": "system", "content": "Analista tecnico de opcoes binarias. Responda SOMENTE com JSON valido, sem texto extra. Portugues."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": MAX_TOKENS_RESPOSTA,
        }

        try:
            resp = _req.post(URL_GROQ, json=body, headers=headers, timeout=TIMEOUT_SEGUNDOS)
        except _req.RequestException as e:
            print(f"    [IA] erro de conexao: {e}")
            return None

        if resp.status_code == 429:
            with _lock_bloqueio:
                pausa = 300 if "tokens per day" in resp.text else 30
                _bloqueado_ate = time.time() + pausa
            print(f"    [IA] rate limit — pausando chamadas por {pausa}s")
            return None

        if resp.status_code != 200:
            print(f"    [IA] HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        try:
            dados = resp.json()
            conteudo = dados["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as e:
            print(f"    [IA] resposta invalida: {e}")
            return None

        resultado = _parsear_resposta(conteudo)
        if resultado:
            print(f"    [IA] OK: {resultado.texto}")
        return resultado
    finally:
        _sem_ia.release()


def _parsear_resposta(conteudo: str) -> ParecerIA | None:
    inicio = conteudo.find("{")
    fim = conteudo.rfind("}") + 1
    if inicio < 0 or fim <= inicio:
        return ParecerIA(texto=conteudo[:200], confianca="baixa", direcao_sugerida=None)
    try:
        obj = json.loads(conteudo[inicio:fim])
        direcao = str(obj.get("direcao", "")).upper()
        if direcao not in ("CALL", "PUT"):
            direcao = None
        return ParecerIA(
            texto=str(obj.get("texto", conteudo[:200])),
            confianca=str(obj.get("confianca", "baixa")),
            direcao_sugerida=direcao,
        )
    except (json.JSONDecodeError, TypeError):
        return ParecerIA(texto=conteudo[:200], confianca="baixa", direcao_sugerida=None)


def montar_contexto(
    ativo: str,
    timeframe: str,
    alerta,
    indicadores,
    noticias: list[dict],
) -> dict:
    """Monta o dict de contexto a partir dos dados do bot."""
    ultima = indicadores.iloc[-2]
    atual = indicadores.iloc[-1]

    corpo = abs(float(ultima["Close"]) - float(ultima["Open"]))
    atr = float(ultima.get("ATR", 1))
    corpo_atr = round(corpo / atr, 1) if atr > 0 else 0

    ema_micro = float(atual.get("EMA_Micro", 0))
    ema_macro = float(atual.get("EMA_Macro", 0))
    ema_posicao = "curta acima da longa (alta)" if ema_micro > ema_macro else "curta abaixo da longa (baixa)"

    noticias_texto = "nenhuma"
    sugestao_texto = "nenhuma"
    if noticias:
        partes = []
        for n in noticias[:3]:
            parte = f"{n.get('moeda','?')}: {n.get('titulo','?')} em {n.get('minutos',0)} min"
            if n.get('forecast'):
                parte += f" (prev: {n['forecast']}, ant: {n.get('previous','?')})"
            partes.append(parte)
            if n.get("acima"):
                sugestao_texto = f"acima da previsao = {n['acima']}, abaixo = {n['abaixo']}"
        noticias_texto = " | ".join(partes)

    return {
        "ativo": ativo,
        "timeframe": timeframe,
        "direcao": alerta.direcao if alerta else "",
        "tipo": alerta.tipo if alerta else "sem alerta",
        "rsi": f"{float(ultima.get('RSI', 50)):.0f}",
        "tendencia_macro": str(ultima.get("TendenciaMacro", "lateral")),
        "tendencia_micro": "alta" if ema_micro > ema_macro else "baixa",
        "corpo_atr": corpo_atr,
        "preco": f"{float(atual['Close']):.5f}",
        "banda_sup": f"{float(atual.get('BandaSup', 0)):.5f}",
        "banda_inf": f"{float(atual.get('BandaInf', 0)):.5f}",
        "ema_posicao": ema_posicao,
        "motivos": " / ".join(alerta.motivos) if alerta else "sem alerta",
        "noticias": noticias_texto,
        "sugestao_noticia": sugestao_texto,
        "horario": str(indicadores.index[-1]),
    }
