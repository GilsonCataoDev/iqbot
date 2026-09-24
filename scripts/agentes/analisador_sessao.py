"""Analisador pós-sessão — agente 5.

Lê as operações reais do SQLite, monta contexto rico por trade e chama o
Groq para identificar padrões de loss e sugerir melhorias por ativo/setup.

Os insights são acumulados em diario/insights_llm.json — cada execução
alimenta a próxima, criando memória entre sessões.

Uso:
    python scripts/agentes/analisador_sessao.py [--dias N]  (padrão: 1)
    python scripts/agentes/analisador_sessao.py --dias 7
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests as _req

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

RAIZ = Path(__file__).resolve().parents[2]
BANCO = RAIZ / "iqoption_m5/dados/iqoption_m5_real_ema_laboratorio_real.sqlite3"
INSIGHTS = RAIZ / "diario/insights_llm.json"

_URL_GROQ = "https://api.groq.com/openai/v1/chat/completions"
_MODELO   = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"
_TIMEOUT  = 30.0
_MAX_TOKENS = 2000
# Abaixo disso por ativo:setup, o LLM acha padrão em ruído; filtro não é gravado.
MIN_AMOSTRA_FILTRO = 30
_METRICAS = ("corpo_ratio", "ema_separacao_atr", "range_atr", "volume_relativo")


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------

def _carregar_trades(dias: int) -> list[dict]:
    if not BANCO.exists():
        print(f"ERRO: banco não encontrado — {BANCO}")
        sys.exit(1)

    # hora_sinal é UTC sem fuso; enviada_em é horário local — comparar o corte
    # UTC com enviada_em deslocava a janela em 3h.
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            o.id_ordem, o.ativo, o.direcao, o.setup, o.timeframe,
            o.enviada_em, o.hora_sinal,
            o.payout, o.lucro, o.resultado_bruto,
            s.slippage_pips,
            d.detalhes_json, d.motivo_risco, d.motivo_estrategia
        FROM operacoes o
        LEFT JOIN slippage s ON s.id_ordem = o.id_ordem
        -- Chave única de decisoes. A janela de ±30s pegava também a decisão de
        -- rastros-sombra do mesmo ativo e duplicava trades com indicadores alheios.
        LEFT JOIN decisoes d
            ON d.ativo = o.ativo
            AND d.direcao = o.direcao
            AND d.setup = o.setup
            AND d.timeframe = o.timeframe
            AND d.candle_hora = o.hora_sinal
        WHERE o.status = 'finalizada'
          AND o.hora_sinal >= ?
          AND o.resultado_bruto IN ('win', 'loose')
        ORDER BY o.hora_sinal ASC
    """, (corte,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Formatação do contexto para o LLM
# ---------------------------------------------------------------------------

def _hora_brt(ts_utc: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
        return (dt - timedelta(hours=3)).strftime("%H:%M")
    except Exception:
        return ts_utc[:16] if ts_utc else "?"


def _resumir_trade(t: dict) -> dict:
    """Extrai o essencial do trade para análise."""
    det = {}
    if t.get("detalhes_json"):
        try:
            det = json.loads(t["detalhes_json"])
        except Exception:
            pass

    aud = det.get("auditoria", {})
    resultado = "WIN" if t["resultado_bruto"] == "win" else "LOSS"
    hora = _hora_brt(t.get("hora_sinal") or t.get("enviada_em") or "")

    resumo = {
        "ativo": t["ativo"],
        "setup": t["setup"],
        "direcao": t["direcao"],
        "hora_brt": hora,
        "resultado": resultado,
        "lucro": t["lucro"],
        "payout": t["payout"],
        "slippage_pips": round(t["slippage_pips"] or 0, 1),
        # indicadores chave
        "corpo_ratio": aud.get("corpo_ratio"),
        "pavio_contra": (
            aud.get("pavio_superior_ratio") if t["direcao"] == "call"
            else aud.get("pavio_inferior_ratio")
        ),
        "range_atr": aud.get("range_atr"),
        "ema_separacao_atr": aud.get("ema_separacao_atr") or det.get("ema_separacao_atr"),
        "volume_relativo": aud.get("volume_relativo"),
        "tags": aud.get("tags", []),
    }
    return resumo


def _media(valores: list) -> float | None:
    nums = [float(v) for v in valores if v is not None]
    return round(sum(nums) / len(nums), 3) if nums else None


def _comparativo(resumos: list[dict]) -> list[str]:
    """Médias e tags de wins vs losses, calculadas aqui para o LLM não fazer conta."""
    linhas = []
    for resultado in ("WIN", "LOSS"):
        grupo = [r for r in resumos if r["resultado"] == resultado]
        if not grupo:
            linhas.append(f"  {resultado} (n=0)")
            continue
        medias = " ".join(
            f"{m}={_media([r[m] for r in grupo])}" for m in _METRICAS
        )
        tags = Counter(t for r in grupo for t in (r["tags"] or []))
        top = ", ".join(f"{t}:{c}/{len(grupo)}" for t, c in tags.most_common(6))
        linhas.append(f"  {resultado} (n={len(grupo)}): {medias}")
        linhas.append(f"    tags: {top}")
    return linhas


def contagem_por_par(trades: list[dict]) -> Counter:
    return Counter(f"{t['ativo']}:{t['setup']}" for t in trades)


def _montar_contexto(trades: list[dict], insights_anteriores: dict) -> str:
    """Monta o texto completo de contexto para o prompt."""
    resumos = [_resumir_trade(t) for t in trades]

    # Estatísticas rápidas por (ativo, setup)
    stats: dict[str, dict] = {}
    for r in resumos:
        chave = f"{r['ativo']}:{r['setup']}"
        if chave not in stats:
            stats[chave] = {"wins": 0, "losses": 0, "loss_details": []}
        if r["resultado"] == "WIN":
            stats[chave]["wins"] += 1
        else:
            stats[chave]["losses"] += 1
            stats[chave]["loss_details"].append(r)

    linhas = ["=== TRADES DA SESSÃO ==="]
    for r in resumos:
        flag = "✓" if r["resultado"] == "WIN" else "✗"
        linhas.append(
            f"{flag} {r['ativo']} {r['direcao'].upper()} {r['hora_brt']} "
            f"[{r['setup']}] corpo={r['corpo_ratio']} "
            f"range_atr={r['range_atr']} ema_sep={r['ema_separacao_atr']} "
            f"vol={r['volume_relativo']} tags={r['tags']}"
        )

    linhas.append("\n=== ESTATÍSTICAS POR ATIVO/SETUP ===")
    for chave, s in stats.items():
        wl = s["wins"] + s["losses"]
        wr = round(s["wins"] / wl * 100) if wl else 0
        aviso = "" if wl >= MIN_AMOSTRA_FILTRO else f"  [AMOSTRA INSUFICIENTE: n<{MIN_AMOSTRA_FILTRO}]"
        linhas.append(f"{chave}: {s['wins']}W/{s['losses']}L  WR={wr}%{aviso}")

    linhas.append("\n=== MÉDIAS WIN vs LOSS (calculadas, use estes números) ===")
    linhas.append("GERAL:")
    linhas.extend(_comparativo(resumos))
    for chave in stats:
        linhas.append(f"{chave}:")
        linhas.extend(_comparativo([r for r in resumos if f"{r['ativo']}:{r['setup']}" == chave]))

    if insights_anteriores:
        linhas.append("\n=== MEMÓRIA DE SESSÕES ANTERIORES ===")
        for chave, ins in list(insights_anteriores.items())[-5:]:  # últimos 5 pares
            linhas.append(f"{chave}: {ins.get('resumo_llm', '')[:200]}")

    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Prompt e chamada LLM
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """Você é um analista quantitativo especializado em binary options M5.
Analise os trades da sessão e responda em JSON estrito com esta estrutura:
{
  "padroes_loss": ["padrão 1", "padrão 2"],
  "filtros_sugeridos": [{"ativo": "...", "setup": "...", "condicao": "...", "justificativa": "..."}],
  "pares_fortes": ["EURUSD:fibo_mtf_confirmado"],
  "pares_fracos": ["EURUSD:ema920_pullback"],
  "resumo": "2-3 frases objetivas sobre a sessão",
  "proxima_sessao": "1 frase de orientação para amanhã"
}
Seja objetivo. Use os indicadores fornecidos (corpo_ratio, range_atr, ema_separacao_atr, tags).
Para comparar wins e losses, use SOMENTE a seção "MÉDIAS WIN vs LOSS"; não recalcule
nem cite valores que não aparecem no contexto. Um padrão só vale se a diferença aparecer
nessas médias. Pares marcados com AMOSTRA INSUFICIENTE: não sugira filtro para eles,
apenas descreva a hipótese em padroes_loss. Se a amostra for pequena, diga isso."""


def _chamar_groq(contexto: str) -> dict | None:
    chave = os.environ.get("GROQ_API_KEY", "").strip()
    if not chave:
        print("GROQ_API_KEY não definida — análise LLM indisponível.")
        return None

    prompt = (
        f"Analise os trades abaixo e identifique padrões de loss, "
        f"o que diferencia wins de losses pelos indicadores, e sugira filtros concretos.\n\n"
        f"{contexto}"
    )

    try:
        r = _req.post(
            _URL_GROQ,
            headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
            json={
                "model": _MODELO,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": _MAX_TOKENS,
            },
            timeout=_TIMEOUT,
        )
    except Exception as e:
        print(f"Groq: erro de conexão — {e}")
        return None

    if r.status_code == 429:
        print("Groq: rate limit — tente em alguns minutos.")
        return None
    if r.status_code != 200:
        print(f"Groq: HTTP {r.status_code}")
        return None

    conteudo = r.json()["choices"][0]["message"]["content"].strip()
    # Remove blocos markdown ```json ... ``` se presentes
    if "```" in conteudo:
        partes = conteudo.split("```")
        for parte in partes:
            parte = parte.lstrip("json").strip()
            if parte.startswith("{"):
                conteudo = parte
                break
    ini, fim = conteudo.find("{"), conteudo.rfind("}") + 1
    if ini < 0 or fim <= ini:
        print(f"Groq: resposta não-JSON:\n{conteudo[:300]}")
        return None
    try:
        return json.loads(conteudo[ini:fim])
    except json.JSONDecodeError as e:
        print(f"Groq: parse error — {e}\n{conteudo[:300]}")
        return None


# ---------------------------------------------------------------------------
# Memória acumulada
# ---------------------------------------------------------------------------

def _carregar_insights() -> dict:
    if not INSIGHTS.exists():
        return {}
    try:
        return json.loads(INSIGHTS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _salvar_insights(insights: dict, analise: dict, trades: list[dict]) -> None:
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    horas = sorted(t["hora_sinal"] for t in trades if t.get("hora_sinal"))
    janela = f"{horas[0][:16]}Z..{horas[-1][:16]}Z" if horas else hoje
    contagem = contagem_por_par(trades)
    for par, n in contagem.items():
        chave = par
        if chave not in insights:
            insights[chave] = {"historico": []}
        ativo, setup = par.split(":", 1)
        filtros = [
            f for f in analise.get("filtros_sugeridos", [])
            if f.get("ativo") == ativo and f.get("setup") == setup
        ]
        amostra_ok = n >= MIN_AMOSTRA_FILTRO
        if filtros and not amostra_ok:
            print(f"  {par}: {len(filtros)} filtro(s) descartado(s) — n={n} < {MIN_AMOSTRA_FILTRO}")
        registro = {
            "data": hoje,
            "janela_utc": janela,
            "n": n,
            "padroes_loss": analise.get("padroes_loss", []),
            "filtros_sugeridos": filtros if amostra_ok else [],
            "resumo_llm": analise.get("resumo", ""),
        }
        # Rodar de novo sobre a mesma janela substitui, não acumula duplicata.
        anteriores = [
            h for h in insights[chave].get("historico", [])
            if h.get("janela_utc") != janela
        ]
        insights[chave]["historico"] = (anteriores + [registro])[-20:]
        insights[chave]["ultima_analise"] = hoje
        # Resumo consolidado do último insight
        insights[chave]["resumo_llm"] = analise.get("resumo", "")

    INSIGHTS.parent.mkdir(parents=True, exist_ok=True)
    INSIGHTS.write_text(json.dumps(insights, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nInsights salvos em: {INSIGHTS}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Analisador pós-sessão LLM")
    parser.add_argument("--dias", type=int, default=1, help="Janela de análise em dias (padrão: 1)")
    args = parser.parse_args()

    print(f"=== Analisador pós-sessão — últimos {args.dias} dia(s) ===")
    print(f"Banco: {BANCO}\n")

    trades = _carregar_trades(args.dias)
    if not trades:
        print("Nenhuma operação finalizada no período.")
        return

    wins   = sum(1 for t in trades if t["resultado_bruto"] == "win")
    losses = sum(1 for t in trades if t["resultado_bruto"] == "loose")
    lucro  = sum(t["lucro"] for t in trades)
    wr     = round(wins / len(trades) * 100, 1) if trades else 0

    print(f"Trades: {len(trades)}  |  {wins}W/{losses}L  WR={wr}%  lucro={lucro:+.2f}")
    print()

    insights_anteriores = _carregar_insights()
    contexto = _montar_contexto(trades, insights_anteriores)

    print("Contexto montado. Enviando ao Groq...\n")
    t0 = time.time()
    analise = _chamar_groq(contexto)
    elapsed = time.time() - t0

    if analise is None:
        print("Análise LLM indisponível. Contexto gerado:\n")
        print(contexto)
        return

    print(f"Análise recebida ({elapsed:.1f}s):\n")
    print("--- PADRÕES DE LOSS ---")
    for p in analise.get("padroes_loss", []):
        print(f"  • {p}")

    print(f"\n--- FILTROS SUGERIDOS (só pares com n>={MIN_AMOSTRA_FILTRO}) ---")
    contagem = contagem_por_par(trades)
    for f in analise.get("filtros_sugeridos", []):
        n = contagem.get(f"{f.get('ativo')}:{f.get('setup')}", 0)
        if n < MIN_AMOSTRA_FILTRO:
            continue
        print(f"  [{f.get('ativo')} {f.get('setup')} n={n}] {f.get('condicao')} — {f.get('justificativa')}")

    print("\n--- PARES FORTES ---", ", ".join(analise.get("pares_fortes", [])))
    print("--- PARES FRACOS  ---", ", ".join(analise.get("pares_fracos", [])))
    print(f"\n--- RESUMO ---\n  {analise.get('resumo', '')}")
    print(f"\n--- PRÓXIMA SESSÃO ---\n  {analise.get('proxima_sessao', '')}")

    _salvar_insights(insights_anteriores, analise, trades)


if __name__ == "__main__":
    main()
