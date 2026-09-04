"""Persistencia duravel dos eventos do Monitor Mercado.

O painel continua podendo espelhar JSON para compatibilidade, mas o SQLite e a
fonte de verdade: nao corta o historico e permite exportacao/auditoria.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSAO = 1


class MonitorEventStore:
    """Um modulo pequeno que esconde migracao, schema e upsert atomico."""

    def __init__(self, caminho: Path, legado_json: Path | None = None):
        self.caminho = Path(caminho)
        self.legado_json = Path(legado_json) if legado_json else None
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._criar_schema()
        self._migrar_legado_se_vazio()

    def _conectar(self) -> sqlite3.Connection:
        conexao = sqlite3.connect(self.caminho, timeout=15)
        conexao.row_factory = sqlite3.Row
        return conexao

    def _criar_schema(self) -> None:
        with self._conectar() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS monitor_eventos (
                    id TEXT PRIMARY KEY,
                    quando TEXT NOT NULL,
                    ativo TEXT,
                    tipo TEXT,
                    entrada_valida INTEGER NOT NULL DEFAULT 0,
                    estado TEXT,
                    desfecho TEXT,
                    payload_json TEXT NOT NULL,
                    atualizado_em TEXT NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_monitor_quando ON monitor_eventos(quando)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_monitor_tipo ON monitor_eventos(tipo, desfecho)")

    def _migrar_legado_se_vazio(self) -> None:
        with self._conectar() as con:
            if con.execute("SELECT COUNT(*) FROM monitor_eventos").fetchone()[0]:
                return
        if not self.legado_json or not self.legado_json.exists():
            return
        try:
            itens = json.loads(self.legado_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.salvar([item for item in itens if isinstance(item, dict)])

    def carregar(self, limite: int | None = None) -> list[dict]:
        sql = "SELECT payload_json FROM monitor_eventos ORDER BY quando"
        params: tuple[object, ...] = ()
        if limite:
            sql = "SELECT payload_json FROM (SELECT quando, payload_json FROM monitor_eventos ORDER BY quando DESC LIMIT ?) ORDER BY quando"
            params = (int(limite),)
        with self._conectar() as con:
            linhas = con.execute(sql, params).fetchall()
        itens: list[dict] = []
        for linha in linhas:
            try:
                item = json.loads(linha["payload_json"])
                if isinstance(item, dict):
                    itens.append(item)
            except json.JSONDecodeError:
                continue
        return itens

    def salvar(self, itens: list[dict]) -> None:
        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        linhas = []
        for item in itens:
            identificador = str(item.get("id") or "").strip()
            quando = str(item.get("quando") or "").strip()
            if not identificador or not quando:
                continue
            simulacao = item.get("simulacao") or {}
            linhas.append((
                identificador, quando, item.get("ativo"), item.get("tipo"),
                int(bool(item.get("entrada_valida"))), item.get("estado_entrada"),
                simulacao.get("desfecho"),
                json.dumps(item, ensure_ascii=False, default=str), agora,
            ))
        if not linhas:
            return
        with self._conectar() as con:
            con.executemany("""
                INSERT INTO monitor_eventos
                    (id, quando, ativo, tipo, entrada_valida, estado, desfecho, payload_json, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    quando=excluded.quando, ativo=excluded.ativo, tipo=excluded.tipo,
                    entrada_valida=excluded.entrada_valida, estado=excluded.estado,
                    desfecho=excluded.desfecho, payload_json=excluded.payload_json,
                    atualizado_em=excluded.atualizado_em
            """, linhas)

    def resumo(self) -> dict:
        with self._conectar() as con:
            total, validos, pendentes, ambiguos = con.execute("""
                SELECT COUNT(*), SUM(entrada_valida),
                       SUM(CASE WHEN desfecho='aguardando' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN desfecho LIKE 'ambíguo%' THEN 1 ELSE 0 END)
                FROM monitor_eventos
            """).fetchone()
        return {
            "schema": SCHEMA_VERSAO, "armazenamento": "SQLite",
            "eventos": int(total or 0), "entradas_validas": int(validos or 0),
            "pendentes": int(pendentes or 0), "ambiguos": int(ambiguos or 0),
        }
