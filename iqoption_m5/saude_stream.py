"""Saúde do stream — detecta dados parados e preço congelado no laboratório.

Dois níveis:
  1. Regra determinística: timestamp do servidor > 2.5x o timeframe = stream parado.
  2. LLM opcional (Groq): confirma suspeita e sugere ação quando há alertas.

Nunca bloqueia o loop principal — qualquer falha retorna vazio/None silenciosamente.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pandas as pd
import requests as _req

_URL = "https://api.groq.com/openai/v1/chat/completions"
_MODELO = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"
_TIMEOUT = 10.0
_MAX_TOKENS = 120

_lock = threading.Lock()
_bloqueado_ate = 0.0


# ---------------------------------------------------------------------------
# Detecção por regra (determinística)
# ---------------------------------------------------------------------------

def alertas_por_regra(
    ultimo_ts: dict[tuple[str, int], int],
    agora: float | None = None,
) -> list[dict]:
    """Retorna lista de streams parados com base no timestamp do servidor.

    Args:
        ultimo_ts: {(ativo, tf_segundos): unix_timestamp_servidor}
        agora: tempo atual unix (padrão: time.time())
    """
    agora = agora or time.time()
    problemas = []
    for (ativo, tf), ts in ultimo_ts.items():
        if ts <= 0:
            continue
        atraso = agora - ts
        if atraso > tf * 2.5:
            problemas.append({
                "ativo": ativo,
                "tf_s": tf,
                "atraso_s": int(atraso),
                "tipo": "stream_parado",
            })
    return problemas


def preco_congelado(df: pd.DataFrame, n: int = 5) -> bool:
    """True se os últimos N candles têm exatamente o mesmo Close.

    Stream 'vivo' que repete o mesmo preço = dado congelado mascarado.
    """
    if len(df) < n:
        return False
    return int(df["Close"].tail(n).nunique()) == 1


# ---------------------------------------------------------------------------
# Diagnóstico LLM (Groq — opcional, falha fechada)
# ---------------------------------------------------------------------------

def _chave_groq() -> str | None:
    return os.environ.get("GROQ_API_KEY", "").strip() or None


def checar_llm(
    alertas: list[dict],
    resumo_ctx: str,
) -> dict | None:
    """Envia alertas ao Groq e retorna dict com 'acao' e 'resumo'.

    Retorna None em qualquer falha — nunca levanta exceção.
    Acao possível: 'nenhuma' | 'reconectar' | 'aguardar'.
    """
    global _bloqueado_ate

    chave = _chave_groq()
    if not chave or not alertas:
        return None

    with _lock:
        if time.time() < _bloqueado_ate:
            return None

    prompt = (
        "Você é um monitor de stream forex em tempo real.\n"
        f"Contexto recente dos streams:\n{resumo_ctx}\n\n"
        f"Alertas detectados automaticamente:\n{json.dumps(alertas, ensure_ascii=False)}\n\n"
        'Responda SOMENTE JSON: {"acao":"nenhuma|reconectar|aguardar","resumo":"1 frase objetiva"}'
    )
    try:
        r = _req.post(
            _URL,
            headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
            json={
                "model": _MODELO,
                "messages": [
                    {"role": "system", "content": "Monitor de stream. Responda SOMENTE JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": _MAX_TOKENS,
            },
            timeout=_TIMEOUT,
        )
    except Exception:
        return None

    if r.status_code == 429:
        with _lock:
            pausa = 300 if "day" in r.text else 90
            _bloqueado_ate = time.time() + pausa
        return None

    if r.status_code != 200:
        return None

    try:
        conteudo = r.json()["choices"][0]["message"]["content"].strip()
        ini, fim = conteudo.find("{"), conteudo.rfind("}") + 1
        if ini < 0 or fim <= ini:
            return None
        obj = json.loads(conteudo[ini:fim])
        return {
            "acao": str(obj.get("acao", "nenhuma")),
            "resumo": str(obj.get("resumo", ""))[:200],
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Thread de monitoramento periódico
# ---------------------------------------------------------------------------

def iniciar_monitor(
    ultimo_ts: dict[tuple[str, int], int],
    mercado,
    progresso,
    intervalo_s: float = 600.0,
    parar: threading.Event | None = None,
) -> threading.Thread:
    """Inicia thread daemon que verifica saúde dos streams a cada `intervalo_s` segundos.

    Args:
        ultimo_ts: dict compartilhado — preenchido pelo loop principal do lab.
        mercado: instância MercadoIQ (para forçar reconexão se necessário).
        progresso: ProgressoLaboratorio (para marcar reset após reconexão).
        intervalo_s: intervalo de verificação (padrão 10 min).
        parar: Event de parada (usa o mesmo do gráfico ao vivo se disponível).
    """
    parar = parar or threading.Event()

    def _loop() -> None:
        # Aguarda o lab inicializar antes de começar a monitorar.
        time.sleep(min(intervalo_s, 120.0))
        while not parar.wait(intervalo_s):
            agora = time.time()
            alertas = alertas_por_regra(ultimo_ts, agora)

            # Verifica preço congelado nos streams ativos
            congelados = []
            for (ativo, tf), ts in list(ultimo_ts.items()):
                if ts <= 0:
                    continue
                try:
                    snap = mercado.snapshot_timeframe(ativo, tf)
                    if preco_congelado(snap.candles):
                        congelados.append({"ativo": ativo, "tf_s": tf, "tipo": "preco_congelado"})
                except Exception:
                    pass

            todos = alertas + congelados
            if not todos:
                continue

            print(f"\n[SAUDE STREAM] {len(todos)} problema(s) detectado(s):")
            for p in todos:
                print(f"  {p['ativo']} tf={p['tf_s']}s — {p['tipo']}"
                      + (f" ({p['atraso_s']}s de atraso)" if "atraso_s" in p else ""))

            # Resumo para LLM
            resumo_linhas = [
                f"{ativo} tf={tf}s ultimo_ts={ts} atraso={int(agora-ts)}s"
                for (ativo, tf), ts in ultimo_ts.items()
            ]
            diagnostico = checar_llm(todos, "\n".join(resumo_linhas))
            if diagnostico:
                acao = diagnostico.get("acao", "nenhuma")
                print(f"  [LLM] {diagnostico['resumo']} → ação: {acao}")
                if acao == "reconectar":
                    try:
                        ok = bool(mercado.reconectar_se_necessario(forcar=True))
                        if ok:
                            progresso.marcar()
                            print("  [SAUDE] Reconexão forçada pelo monitor LLM concluída.")
                    except Exception as e:
                        print(f"  [SAUDE] Reconexão falhou: {e!r}")
            else:
                # Sem LLM: reconecta se há stream parado há mais de 3 ciclos
                stream_parado = any(
                    p["tipo"] == "stream_parado" and p.get("atraso_s", 0) > 180
                    for p in todos
                )
                if stream_parado:
                    try:
                        ok = bool(mercado.reconectar_se_necessario(forcar=True))
                        if ok:
                            progresso.marcar()
                            print("  [SAUDE] Reconexão forçada pelo monitor (sem LLM) concluída.")
                    except Exception as e:
                        print(f"  [SAUDE] Reconexão falhou: {e!r}")

    t = threading.Thread(target=_loop, name="ema-lab-saude-stream", daemon=True)
    t.start()
    return t
