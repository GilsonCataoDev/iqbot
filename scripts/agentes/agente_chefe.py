"""Agente chefe — otimizador de prompts via Groq.

Recebe uma tarefa de análise, consulta o schema do banco uma vez,
e retorna prompts MINIMOS e queries SQL prontas para os sub-agentes.
Reduz custo: Groq (llama) planeja; Claude executa apenas o necessario.

Uso:
    python agente_chefe.py "analise losses por hora e ativo nos ultimos 30 dias"
    python agente_chefe.py "qual setup tem melhor WR no monitor esta semana"
"""
import sys
import json
import sqlite3
import os
from pathlib import Path

try:
    from groq import Groq
except ImportError:
    print("ERRO: pip install groq")
    raise SystemExit(1)

RAIZ = Path(__file__).resolve().parents[2]


def _carregar_env() -> None:
    """Le .env.bat e injeta as variaveis no ambiente atual."""
    arq = RAIZ / ".env.bat"
    if not arq.exists():
        return
    for linha in arq.read_text(encoding="utf-8", errors="replace").splitlines():
        t = linha.strip()
        if not t.lower().startswith("set "):
            continue
        corpo = t[4:]
        if "=" not in corpo:
            continue
        k, v = corpo.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_carregar_env()

BANCOS = {
    "lab": Path(__file__).resolve().parents[2] / "iqoption_m5/dados/iqoption_m5_practice_ema_laboratorio_practice.sqlite3",
    "monitor": Path(__file__).resolve().parents[2] / "diario/monitor_mercado/monitor_mercado.sqlite3",
}


def schema_banco(caminho: Path) -> str:
    """Extrai schema + valores distintos de colunas categoricas."""
    conn = sqlite3.connect(caminho)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tabelas = [r[0] for r in cur.fetchall()]
    linhas = []
    # Colunas que valem enumerar valores distintos
    ENUM_COLS = {"desfecho", "tipo", "estado", "resultado", "status", "setup",
                 "timeframe", "direcao", "motivo", "resultado_bruto", "ativo"}
    for t in tabelas:
        cur.execute(f"PRAGMA table_info({t})")
        colunas = cur.fetchall()
        cols_str = ", ".join(r[1] for r in colunas)
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        n = cur.fetchone()[0]
        linhas.append(f"  {t} (n={n}): {cols_str}")
        # Valores distintos para colunas categoricas
        for col in colunas:
            nome = col[1]
            if nome in ENUM_COLS:
                try:
                    cur.execute(f"SELECT DISTINCT {nome} FROM {t} WHERE {nome} IS NOT NULL LIMIT 12")
                    vals = [str(r[0]) for r in cur.fetchall()]
                    if vals:
                        linhas.append(f"    {nome} valores: {', '.join(vals)}")
                except Exception:
                    pass
    conn.close()
    return "\n".join(linhas)


def detectar_banco(tarefa: str) -> tuple[str, Path]:
    """Escolhe o banco mais relevante para a tarefa."""
    tarefa_lower = tarefa.lower()
    if any(p in tarefa_lower for p in ["monitor", "xauusd", "swing", "ouro", "gold", "sinal", "falso_rompimento", "bof"]):
        return "monitor", BANCOS["monitor"]
    return "lab", BANCOS["lab"]


def otimizar(tarefa: str, banco_nome: str, schema: str) -> dict:
    """Chama Groq para planejar queries minimas."""
    client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))

    sistema = """Voce e um planejador de analise de dados de trading.
Dado: (1) uma tarefa de analise, (2) o schema do banco SQLite.
Retorne um JSON com o plano MINIMO para responder a tarefa:

{
  "resumo": "o que a analise vai responder em 1 frase",
  "banco": "lab|monitor",
  "subtarefas": [
    {
      "label": "nome curto",
      "pergunta": "o que essa query responde",
      "sql": "SELECT ... FROM ... WHERE ... (SQLite valido, sem aspas duplas em strings)",
      "prioridade": 1
    }
  ],
  "prompt_agente": "prompt completo e MINIMO para um agente Claude executar TODAS as queries acima e sintetizar, sem explorar schema (ja incluso abaixo)"
}

Regras:
- Maximo 4 subtarefas. Se a tarefa pode ser respondida com 1-2 queries, use 1-2.
- Cada SQL deve ser valido no SQLite (strings com aspas simples, json_extract para JSON).
- prompt_agente deve ter menos de 300 palavras e incluir os SQLs literalmente.
- Nao inclua queries redundantes. Nao explore o schema (ele ja e conhecido).
- Hora BRT: (CAST(substr(quando,12,2) AS INTEGER) - 3 + 24) % 24
- Break-even lab (binario 86% payout): 54% WR. Break-even monitor (swing TP1=1R): 50% WR.
- No monitor: direcao='buy' ou 'sell' (nunca 'compra'/'venda'). Use json_extract(payload_json,'$.direcao') para filtrar.
- Use os valores reais de ativo/desfecho/tipo que aparecem no schema abaixo — nao invente valores."""

    usuario = f"""Tarefa: {tarefa}

Schema do banco '{banco_nome}':
{schema}

Retorne apenas o JSON, sem markdown."""

    resposta = client.chat.completions.create(
        model="qwen/qwen3.8-27b",
        messages=[
            {"role": "system", "content": sistema},
            {"role": "user", "content": usuario},
        ],
        temperature=0.1,
        max_tokens=1500,
    )
    texto = resposta.choices[0].message.content.strip()
    # Remove markdown se presente
    if texto.startswith("```"):
        texto = texto.split("```")[1]
        if texto.startswith("json"):
            texto = texto[4:]
    return json.loads(texto.strip())


def main():
    tarefa = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    if not tarefa:
        print("Uso: python agente_chefe.py <descricao da tarefa>")
        raise SystemExit(1)

    banco_nome, banco_path = detectar_banco(tarefa)
    if not banco_path.exists():
        print(f"ERRO: banco '{banco_nome}' nao encontrado em {banco_path}")
        raise SystemExit(1)

    print(f"[Agente Chefe] Tarefa: {tarefa}")
    print(f"[Agente Chefe] Banco: {banco_nome}")
    print("[Agente Chefe] Lendo schema...")
    schema = schema_banco(banco_path)

    print("[Agente Chefe] Planejando via Groq (llama-3.3-70b)...")
    plano = otimizar(tarefa, banco_nome, schema)

    print()
    print("=" * 60)
    print(f"RESUMO: {plano.get('resumo', '')}")
    print()
    print(f"SUBTAREFAS ({len(plano.get('subtarefas', []))}):")
    for i, s in enumerate(plano.get("subtarefas", []), 1):
        print(f"  {i}. [{s.get('label')}] {s.get('pergunta')}")
        print(f"     SQL: {s.get('sql', '')[:120]}...")

    print()
    print("PROMPT OTIMIZADO PARA AGENTE CLAUDE:")
    print("-" * 60)
    print(plano.get("prompt_agente", ""))
    print("-" * 60)
    print()

    # Salva plano para uso em workflows
    saida = Path(__file__).parent / "plano_chefe.json"
    saida.write_text(json.dumps(plano, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Agente Chefe] Plano salvo em: {saida}")
    return plano


if __name__ == "__main__":
    main()
