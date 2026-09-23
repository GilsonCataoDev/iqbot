"""Analisador contínuo — LLM analisa todas as estratégias e sugere mudanças.

Roda em loop (padrão: a cada 30 min). Lê stats de todos os bancos do lab,
chama Groq e salva sugestões estruturadas em sugestoes_llm.sqlite3.

Uso:
    python analisador_continuo.py            # loop padrão (30 min)
    python analisador_continuo.py --uma-vez  # roda uma vez e sai
    python analisador_continuo.py --minutos 60
"""
import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path

try:
    from groq import Groq
except ImportError:
    print("ERRO: pip install groq")
    raise SystemExit(1)

RAIZ = Path(__file__).resolve().parents[2]
BANCO_LAB   = RAIZ / "iqoption_m5/dados/iqoption_m5_practice_ema_laboratorio_practice.sqlite3"
BANCO_REAL  = RAIZ / "iqoption_m5/dados/iqoption_m5_real_ema_laboratorio_real.sqlite3"
BANCO_SAIDA = RAIZ / "iqoption_m5/dados/sugestoes_llm.sqlite3"
BREAK_EVEN  = 54.1  # payout 85% → precisa de 54.1% WR para ser lucrativo


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _carregar_env() -> None:
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


def _init_banco_saida() -> None:
    con = sqlite3.connect(BANCO_SAIDA)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sugestoes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            criado_em   TEXT NOT NULL,
            ciclo       INTEGER NOT NULL DEFAULT 1,
            setup       TEXT,
            ativo       TEXT,
            acao        TEXT NOT NULL,
            prioridade  TEXT NOT NULL DEFAULT 'media',
            razao       TEXT NOT NULL,
            dados_json  TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ciclos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            iniciado_em TEXT NOT NULL,
            concluido_em TEXT,
            n_sugestoes INTEGER DEFAULT 0,
            modelo      TEXT,
            tokens_usados INTEGER DEFAULT 0
        )
    """)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Coleta de stats
# ---------------------------------------------------------------------------

def _ic95(w: int, n: int) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    wr = w / n
    se = sqrt(wr * (1 - wr) / n) if n > 1 else 0.5
    return max(0.0, wr - 1.96 * se) * 100, min(1.0, wr + 1.96 * se) * 100


def _query(banco: Path, sql: str, params: tuple = ()) -> list[tuple]:
    if not banco.exists():
        return []
    try:
        con = sqlite3.connect(banco)
        rows = con.execute(sql, params).fetchall()
        con.close()
        return rows
    except Exception:
        return []


def _stats_setup_ativo(banco: Path) -> list[dict]:
    """WR por setup × ativo com IC95, tendência recente vs histórico."""
    rows = _query(banco, """
        SELECT setup, ativo,
               COUNT(*) as n,
               SUM(CASE WHEN resultado_bruto='win' THEN 1 ELSE 0 END) as w,
               SUM(CASE WHEN resultado_bruto IN ('loss','loose') THEN 1 ELSE 0 END) as l,
               MIN(DATE(enviada_em)), MAX(DATE(enviada_em))
        FROM operacoes
        WHERE resultado_bruto IN ('win','loss','loose')
        GROUP BY setup, ativo
        ORDER BY setup, n DESC
    """)
    resultado = []
    for setup, ativo, n, w, l, ini, fim in rows:
        ic_lo, ic_hi = _ic95(w, n)
        # Tendência: últimas 20 entradas
        ult = _query(banco, """
            SELECT resultado_bruto FROM operacoes
            WHERE setup=? AND ativo=? AND resultado_bruto IN ('win','loss','loose')
            ORDER BY enviada_em DESC LIMIT 20
        """, (setup, ativo))
        w20 = sum(1 for (r,) in ult if r == "win")
        n20 = len(ult)
        resultado.append({
            "setup": setup, "ativo": ativo,
            "n": n, "w": w, "l": l,
            "wr_pct": round(w / n * 100, 1) if n else 0,
            "ic95_lo": round(ic_lo, 1), "ic95_hi": round(ic_hi, 1),
            "periodo": f"{ini} a {fim}",
            "recente_20": {"w": w20, "n": n20, "wr_pct": round(w20/n20*100,1) if n20 else 0},
        })
    return resultado


def _stats_simulacoes(banco: Path) -> list[dict]:
    """Simulações (sombra) por setup × ativo — representa os bloqueados."""
    rows = _query(banco, """
        SELECT setup, ativo,
               COUNT(*) as n,
               SUM(CASE WHEN resultado='win' THEN 1 ELSE 0 END) as w,
               SUM(CASE WHEN resultado='loss' THEN 1 ELSE 0 END) as l,
               MIN(DATE(criado_em)), MAX(DATE(criado_em))
        FROM simulacoes
        WHERE resultado IN ('win','loss')
        GROUP BY setup, ativo
        ORDER BY setup, n DESC
    """)
    resultado = []
    for setup, ativo, n, w, l, ini, fim in rows:
        ic_lo, ic_hi = _ic95(w, n)
        resultado.append({
            "setup": setup, "ativo": ativo,
            "n": n, "w": w, "l": l,
            "wr_pct": round(w / n * 100, 1) if n else 0,
            "ic95_lo": round(ic_lo, 1), "ic95_hi": round(ic_hi, 1),
            "periodo": f"{ini} a {fim}",
        })
    return resultado


MOTIVOS_ESTRUTURAIS = {
    # Rastro/ativo estruturalmente em sombra — não são filtros de qualidade.
    # Incluir esses motivos na análise de filtros gera falsos AJUSTAR_FILTRO.
    "rastro_sombra", "ativo_candidato_sombra",
    # Rótulos legados de versões anteriores que significavam "ativo pausado":
    "nzd_ema_pausada", "nzd_v1_validacao",
}


def _stats_filtros(banco: Path) -> list[dict]:
    """WR das simulações por motivo de bloqueio — apenas filtros de qualidade."""
    placeholders = ",".join("?" * len(MOTIVOS_ESTRUTURAIS))
    rows = _query(banco, f"""
        SELECT motivo,
               COUNT(*) as n,
               SUM(CASE WHEN resultado='win' THEN 1 ELSE 0 END) as w,
               SUM(CASE WHEN resultado='loss' THEN 1 ELSE 0 END) as l
        FROM simulacoes
        WHERE resultado IN ('win','loss') AND motivo IS NOT NULL
          AND motivo NOT IN ({placeholders})
        GROUP BY motivo
        HAVING n >= 10
        ORDER BY n DESC
    """, tuple(MOTIVOS_ESTRUTURAIS))
    resultado = []
    for motivo, n, w, l in rows:
        ic_lo, ic_hi = _ic95(w, n)
        resultado.append({
            "motivo": motivo, "n": n, "w": w, "l": l,
            "wr_pct": round(w / n * 100, 1) if n else 0,
            "ic95_lo": round(ic_lo, 1), "ic95_hi": round(ic_hi, 1),
        })
    return resultado


def _stats_horario(banco: Path) -> list[dict]:
    """WR por hora UTC × direção — identifica janelas ruins."""
    rows = _query(banco, """
        SELECT strftime('%H', enviada_em) as hora, direcao,
               COUNT(*) as n,
               SUM(CASE WHEN resultado_bruto='win' THEN 1 ELSE 0 END) as w,
               SUM(CASE WHEN resultado_bruto IN ('loss','loose') THEN 1 ELSE 0 END) as l
        FROM operacoes
        WHERE resultado_bruto IN ('win','loss','loose')
        GROUP BY hora, direcao
        HAVING n >= 5
        ORDER BY hora, direcao
    """)
    resultado = []
    for hora, direcao, n, w, l in rows:
        ic_lo, ic_hi = _ic95(w, n)
        resultado.append({
            "hora_utc": int(hora), "direcao": direcao,
            "n": n, "wr_pct": round(w/n*100,1) if n else 0,
            "ic95_lo": round(ic_lo, 1),
        })
    return resultado


def _stats_real(banco: Path) -> dict:
    """Resumo do banco real: P&L, WR, operações hoje."""
    if not banco.exists():
        return {}
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = _query(banco, """
        SELECT COUNT(*), SUM(CASE WHEN resultado_bruto='win' THEN 1 ELSE 0 END),
               SUM(CASE WHEN resultado_bruto IN ('loss','loose') THEN 1 ELSE 0 END),
               SUM(CASE WHEN resultado_bruto='win' THEN lucro ELSE -valor END)
        FROM operacoes WHERE resultado_bruto IN ('win','loss','loose')
    """)
    hoje_q = _query(banco, """
        SELECT COUNT(*), SUM(CASE WHEN resultado_bruto='win' THEN 1 ELSE 0 END),
               SUM(CASE WHEN resultado_bruto='win' THEN lucro ELSE -valor END)
        FROM operacoes
        WHERE DATE(enviada_em)=? AND resultado_bruto IN ('win','loss','loose')
    """, (hoje,))
    t = total[0] if total else (0, 0, 0, 0)
    h = hoje_q[0] if hoje_q else (0, 0, 0)
    return {
        "total": {"n": t[0], "w": t[1], "l": t[2], "pnl": round(t[3] or 0, 2)},
        "hoje":  {"n": h[0], "w": h[1], "pnl": round(h[2] or 0, 2)},
    }


def coletar_dados() -> dict:
    return {
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "break_even_pct": BREAK_EVEN,
        "practice": {
            "operacoes": _stats_setup_ativo(BANCO_LAB),
            "simulacoes": _stats_simulacoes(BANCO_LAB),
            "filtros": _stats_filtros(BANCO_LAB),
            "horarios": _stats_horario(BANCO_LAB),
        },
        "real": _stats_real(BANCO_REAL),
    }


# ---------------------------------------------------------------------------
# Prompt e chamada LLM
# ---------------------------------------------------------------------------

SISTEMA = """Você é um analista quant especializado em estratégias de opções binárias no forex (M5).
Recebe estatísticas do laboratório de estratégias e retorna sugestões de melhoria em JSON.

Regras:
- Break-even é {be}% (payout 85%). Só recomende promover ao real se IC95 inferior > {be}%.
- n < 80 é insuficiente para qualquer decisão definitiva — indique como MONITORAR.
- Considere tendência recente (últimas 20) vs histórico para identificar mudanças.
- A seção "Filtros de bloqueio" contém APENAS filtros de qualidade (ex: noticia_high, put_horario_fraco).
  Motivos estruturais como rastro_sombra e ativo_candidato_sombra já foram removidos — não aparecem lá.
- Filtros com WR > break-even ({be}%) são prejudiciais (bloqueando entradas lucrativas → AJUSTAR_FILTRO).
- Filtros com WR < break-even ({be}%) são benéficos (bloqueando entradas ruins → MANTER ou elogiar).
- Seja específico: setup, ativo, horário, direção quando relevante.

Retorne APENAS o JSON abaixo, sem texto extra:
{{
  "resumo": "2-3 frases sobre o estado geral das estratégias",
  "sugestoes": [
    {{
      "setup": "nome_do_setup ou null",
      "ativo": "EURUSD ou null (null = geral)",
      "acao": "PROMOVER_REAL | PAUSAR | MANTER | EXPANDIR_ATIVO | AJUSTAR_FILTRO | MONITORAR",
      "prioridade": "alta | media | baixa",
      "razao": "explicação objetiva em português com números"
    }}
  ]
}}
""".format(be=BREAK_EVEN)


def _compactar(dados: dict) -> str:
    """Serializa dados em formato compacto para caber no contexto."""
    linhas = [f"gerado_em: {dados['gerado_em']}  break_even: {dados['break_even_pct']}%"]

    linhas.append("\n## Operacoes ao vivo (setup|ativo|n|W|L|WR%|IC95lo|IC95hi|periodo|rec20_wr%)")
    for e in dados["practice"]["operacoes"][:20]:
        r20 = e["recente_20"]
        linhas.append(
            f"{e['setup']}|{e['ativo']}|{e['n']}|{e['w']}|{e['l']}"
            f"|{e['wr_pct']}|{e['ic95_lo']}|{e['ic95_hi']}|{e['periodo']}"
            f"|{r20['wr_pct']}({r20['n']})"
        )

    linhas.append("\n## Simulacoes/sombra (setup|ativo|n|W|L|WR%|IC95lo|IC95hi)")
    for e in dados["practice"]["simulacoes"][:20]:
        linhas.append(
            f"{e['setup']}|{e['ativo']}|{e['n']}|{e['w']}|{e['l']}"
            f"|{e['wr_pct']}|{e['ic95_lo']}|{e['ic95_hi']}|{e['periodo']}"
        )

    linhas.append(f"\n## Filtros de qualidade (motivo|n|WR%|IC95lo) — somente regras ativas; motivos estruturais excluídos. Abaixo de {BREAK_EVEN}% = filtro bom")
    for e in dados["practice"]["filtros"][:12]:
        linhas.append(f"{e['motivo']}|{e['n']}|{e['wr_pct']}|{e['ic95_lo']}")

    linhas.append("\n## Horarios UTC com n>=5 (hora|direcao|n|WR%|IC95lo)")
    for e in dados["practice"]["horarios"]:
        linhas.append(f"{e['hora_utc']}h|{e['direcao']}|{e['n']}|{e['wr_pct']}|{e['ic95_lo']}")

    real = dados.get("real", {})
    if real:
        t = real.get("total", {}); h = real.get("hoje", {})
        linhas.append(f"\n## Real: total n={t.get('n')} W={t.get('w')} PnL={t.get('pnl')}  hoje n={h.get('n')} PnL={h.get('pnl')}")

    return "\n".join(linhas)


def chamar_llm(dados: dict, modelo: str = "qwen/qwen3.8-27b") -> tuple[dict, int]:
    client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
    payload = _compactar(dados)
    resp = client.chat.completions.create(
        model=modelo,
        messages=[
            {"role": "system", "content": SISTEMA},
            {"role": "user",   "content": f"Dados do laboratório:\n{payload}"},
        ],
        temperature=0.2,
        max_tokens=2048,
        response_format={"type": "json_object"},
    )
    texto = resp.choices[0].message.content
    tokens = resp.usage.total_tokens if resp.usage else 0
    try:
        resultado = json.loads(texto)
    except json.JSONDecodeError:
        resultado = {"resumo": texto, "sugestoes": []}
    return resultado, tokens


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def salvar_sugestoes(resultado: dict, tokens: int, ciclo_id: int) -> int:
    con = sqlite3.connect(BANCO_SAIDA)
    agora = datetime.now(timezone.utc).isoformat()
    n = 0
    for s in resultado.get("sugestoes", []):
        con.execute("""
            INSERT INTO sugestoes (criado_em, ciclo, setup, ativo, acao, prioridade, razao, dados_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agora, ciclo_id,
            s.get("setup"), s.get("ativo"),
            s.get("acao", "MONITORAR"),
            s.get("prioridade", "media"),
            s.get("razao", ""),
            json.dumps(s, ensure_ascii=False),
        ))
        n += 1
    con.execute("""
        UPDATE ciclos SET concluido_em=?, n_sugestoes=?, tokens_usados=? WHERE id=?
    """, (agora, n, tokens, ciclo_id))
    con.commit()
    con.close()
    return n


def novo_ciclo(modelo: str) -> int:
    con = sqlite3.connect(BANCO_SAIDA)
    cur = con.execute(
        "INSERT INTO ciclos (iniciado_em, modelo) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), modelo),
    )
    ciclo_id = cur.lastrowid
    con.commit()
    con.close()
    return ciclo_id


# ---------------------------------------------------------------------------
# Exibição
# ---------------------------------------------------------------------------

ICONES = {
    "PROMOVER_REAL":   "🚀",
    "PAUSAR":          "⏸️",
    "MANTER":          "✅",
    "EXPANDIR_ATIVO":  "➕",
    "AJUSTAR_FILTRO":  "⚙️",
    "MONITORAR":       "👁️",
}
PRIO = {"alta": "‼️", "media": "ℹ️", "baixa": "·"}


def imprimir_resultado(resultado: dict, ciclo: int, tokens: int) -> None:
    resumo = resultado.get("resumo", "")
    sugestoes = resultado.get("sugestoes", [])
    print(f"\n{'='*65}")
    print(f"  ANÁLISE LLM — ciclo {ciclo} | {datetime.now().strftime('%d/%m %H:%M')} | {tokens} tokens")
    print(f"{'='*65}")
    print(f"  {resumo}")
    print()
    altas   = [s for s in sugestoes if s.get("prioridade") == "alta"]
    demais  = [s for s in sugestoes if s.get("prioridade") != "alta"]
    for s in altas + demais:
        icone = ICONES.get(s.get("acao", ""), "•")
        prio  = PRIO.get(s.get("prioridade", "media"), "·")
        alvo  = " | ".join(filter(None, [s.get("setup"), s.get("ativo")]))
        print(f"  {prio} {icone}  [{s.get('acao')}] {alvo}")
        print(f"       {s.get('razao','')}")
    print(f"{'='*65}\n")


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def rodar_ciclo(ciclo_num: int, modelo: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Coletando dados (ciclo {ciclo_num})...")
    dados = coletar_dados()
    n_op = sum(e["n"] for e in dados["practice"]["operacoes"])
    n_sim = sum(e["n"] for e in dados["practice"]["simulacoes"])
    print(f"  {n_op} operações ao vivo | {n_sim} simulações | chamando {modelo}...")
    ciclo_id = novo_ciclo(modelo)
    try:
        resultado, tokens = chamar_llm(dados, modelo)
        n_sug = salvar_sugestoes(resultado, tokens, ciclo_id)
        imprimir_resultado(resultado, ciclo_num, tokens)
        print(f"  {n_sug} sugestão(ões) salva(s) em sugestoes_llm.sqlite3")
    except Exception as exc:
        print(f"  ERRO na chamada LLM: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uma-vez", action="store_true")
    parser.add_argument("--minutos", type=int, default=30)
    parser.add_argument("--modelo", default="qwen/qwen3.8-27b")
    args = parser.parse_args()

    _carregar_env()
    _init_banco_saida()

    print(f"Analisador contínuo | intervalo: {args.minutos} min | modelo: {args.modelo}")
    print(f"Sugestões → {BANCO_SAIDA}\n")

    ciclo = 1
    while True:
        rodar_ciclo(ciclo, args.modelo)
        if args.uma_vez:
            break
        ciclo += 1
        print(f"  Próxima análise em {args.minutos} min. Ctrl+C para sair.")
        time.sleep(args.minutos * 60)


if __name__ == "__main__":
    main()
