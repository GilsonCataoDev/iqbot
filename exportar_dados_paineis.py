"""Exporta Lab EMA e Monitor Mercado para uma planilha auditavel."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd


RAIZ = Path(__file__).resolve().parent
BANCO_LAB = RAIZ / "iqoption_m5" / "dados" / "iqoption_m5_practice_ema_laboratorio_practice.sqlite3"
BANCO_MONITOR = RAIZ / "diario" / "monitor_mercado" / "monitor_mercado.sqlite3"


def _tabelas(caminho: Path) -> dict[str, pd.DataFrame]:
    if not caminho.exists():
        return {}
    with sqlite3.connect(caminho) as con:
        nomes = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {nome: pd.read_sql_query(f'SELECT * FROM "{nome}"', con) for nome in nomes}


def _achatar_monitor(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "payload_json" not in df:
        return df
    registros = []
    for bruto in df["payload_json"]:
        try:
            item = json.loads(bruto)
        except (TypeError, json.JSONDecodeError):
            continue
        simulacao = item.pop("simulacao", {}) or {}
        alvos = item.pop("alvos", None) or item.pop("alvos_estudo", {}) or {}
        registros.append({
            **{k: v for k, v in item.items() if not isinstance(v, (dict, list))},
            **{f"sim_{k}": v for k, v in simulacao.items()},
            **{f"alvo_{k}": v for k, v in alvos.items()},
        })
    return pd.DataFrame(registros)


def exportar(destino: Path | None = None) -> Path:
    destino = destino or RAIZ / "relatorios" / f"paineis_{datetime.now():%Y%m%d_%H%M}.xlsx"
    destino.parent.mkdir(parents=True, exist_ok=True)
    lab = _tabelas(BANCO_LAB)
    monitor = _tabelas(BANCO_MONITOR)
    resumo = [
        {"origem": "Lab EMA", "tabela": nome, "registros": len(df)} for nome, df in lab.items()
    ] + [
        {"origem": "Monitor Mercado", "tabela": nome, "registros": len(df)} for nome, df in monitor.items()
    ]
    with pd.ExcelWriter(destino, engine="openpyxl") as writer:
        pd.DataFrame(resumo or [{"origem": "sem dados", "tabela": "—", "registros": 0}]).to_excel(
            writer, sheet_name="Resumo", index=False
        )
        for nome, df in lab.items():
            df.to_excel(writer, sheet_name=("Lab_" + nome)[:31], index=False)
        for nome, df in monitor.items():
            if nome == "monitor_eventos":
                df = _achatar_monitor(df)
            df.to_excel(writer, sheet_name=("Monitor_" + nome)[:31], index=False)
        for folha in writer.book.worksheets:
            folha.freeze_panes = "A2"
            folha.auto_filter.ref = folha.dimensions
            for coluna in folha.columns:
                largura = min(45, max(10, max(len(str(c.value or "")) for c in coluna) + 2))
                folha.column_dimensions[coluna[0].column_letter].width = largura
    return destino


if __name__ == "__main__":
    print(f"Planilha criada: {exportar()}")
