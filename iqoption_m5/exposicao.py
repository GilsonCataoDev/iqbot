from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def _base_ativo(ativo: str) -> str:
    return ativo.upper().replace("-OTC", "")


class CoordenadorExposicao:
    """Reserva atômica compartilhada entre os processos M15/H1."""

    def __init__(
        self,
        banco: Path,
        conta: str,
        proprietario: str,
        ttl_segundos: int,
    ) -> None:
        self.banco = Path(banco)
        self.banco.parent.mkdir(parents=True, exist_ok=True)
        self.conta = conta.upper()
        self.proprietario = proprietario
        self.ttl_segundos = max(60, int(ttl_segundos))
        with self._conectar() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS exposicoes (
                    conta TEXT NOT NULL,
                    ativo TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    proprietario TEXT NOT NULL,
                    reservada_em REAL NOT NULL,
                    expira_em REAL NOT NULL,
                    PRIMARY KEY (conta, ativo)
                )
                """
            )

    def _conectar(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.banco, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _limpar_expiradas(db: sqlite3.Connection, agora: float) -> None:
        db.execute("DELETE FROM exposicoes WHERE expira_em <= ?", (agora,))

    def reservar(self, ativo: str, direcao: str) -> tuple[bool, str]:
        agora = time.time()
        ativo_base = _base_ativo(ativo)
        with self._conectar() as db:
            db.execute("BEGIN IMMEDIATE")
            self._limpar_expiradas(db, agora)
            mesmo_ativo = db.execute(
                "SELECT 1 FROM exposicoes WHERE conta=? AND ativo=?",
                (self.conta, ativo_base),
            ).fetchone()
            if mesmo_ativo:
                return False, "ativo_ja_exposto_global"
            mesma_direcao = db.execute(
                "SELECT 1 FROM exposicoes WHERE conta=? AND direcao=? LIMIT 1",
                (self.conta, direcao),
            ).fetchone()
            if mesma_direcao:
                return False, "direcao_ja_exposta_global"
            db.execute(
                """
                INSERT INTO exposicoes (
                    conta, ativo, direcao, proprietario, reservada_em, expira_em
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.conta,
                    ativo_base,
                    direcao,
                    self.proprietario,
                    agora,
                    agora + self.ttl_segundos,
                ),
            )
        return True, "autorizada"

    def liberar(self, ativo: str) -> None:
        with self._conectar() as db:
            db.execute(
                "DELETE FROM exposicoes WHERE conta=? AND ativo=? AND proprietario=?",
                (self.conta, _base_ativo(ativo), self.proprietario),
            )

    def liberar_ativo(self, ativo: str) -> None:
        """Libera exposição do ativo na conta, mesmo se veio de processo anterior."""
        with self._conectar() as db:
            db.execute(
                "DELETE FROM exposicoes WHERE conta=? AND ativo=?",
                (self.conta, _base_ativo(ativo)),
            )
