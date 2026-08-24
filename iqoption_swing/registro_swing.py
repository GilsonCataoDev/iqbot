import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .estrategia_swing import SinalSwing


class RegistroSwing:
    def __init__(self, banco: Path):
        self.caminho = banco
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._criar_schema()

    def _conectar(self):
        c = sqlite3.connect(self.caminho, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    @contextmanager
    def _sessao(self):
        db = self._conectar()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _criar_schema(self) -> None:
        with self._sessao() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operacoes (
                    id_ordem TEXT PRIMARY KEY,
                    ativo TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    setup TEXT NOT NULL,
                    pontuacao INTEGER NOT NULL,
                    enviada_em TEXT NOT NULL,
                    ts_expiracao INTEGER NOT NULL,
                    valor REAL NOT NULL,
                    payout REAL,
                    resultado TEXT,
                    lucro REAL,
                    encerrada_em TEXT,
                    detalhes_json TEXT
                );

                CREATE TABLE IF NOT EXISTS sinais_bloqueados (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    registrado_em TEXT NOT NULL,
                    ativo TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    setup TEXT NOT NULL,
                    pontuacao INTEGER NOT NULL,
                    motivo TEXT NOT NULL,
                    detalhes_json TEXT
                );

                CREATE TABLE IF NOT EXISTS sinais_monitor (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    registrado_em TEXT NOT NULL,
                    ativo TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    setup TEXT NOT NULL,
                    pontuacao INTEGER NOT NULL,
                    entrada REAL NOT NULL,
                    sl REAL NOT NULL,
                    tp REAL NOT NULL,
                    resultado TEXT,          -- 'win' | 'loss' | 'expirado'
                    encerrado_em TEXT,
                    detalhes_json TEXT
                );
            """)

    def registrar_abertura(
        self,
        id_ordem: str,
        sinal: SinalSwing,
        valor: float,
        payout: float | None,
        ts_expiracao: int,
    ) -> None:
        with self._lock, self._sessao() as db:
            db.execute(
                """INSERT OR IGNORE INTO operacoes
                   (id_ordem, ativo, direcao, setup, pontuacao, enviada_em,
                    ts_expiracao, valor, payout, detalhes_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    id_ordem,
                    sinal.ativo,
                    sinal.direcao,
                    sinal.setup,
                    sinal.pontuacao,
                    datetime.now().isoformat(),
                    ts_expiracao,
                    valor,
                    payout,
                    json.dumps(sinal.detalhes, default=str),
                ),
            )

    def registrar_resultado(
        self, id_ordem: str, resultado: str, lucro: float | None
    ) -> None:
        with self._lock, self._sessao() as db:
            db.execute(
                """UPDATE operacoes SET resultado=?, lucro=?, encerrada_em=?
                   WHERE id_ordem=?""",
                (resultado, lucro, datetime.now().isoformat(), id_ordem),
            )

    def registrar_bloqueio(self, sinal: SinalSwing, motivo: str) -> None:
        with self._lock, self._sessao() as db:
            db.execute(
                """INSERT INTO sinais_bloqueados
                   (registrado_em, ativo, direcao, setup, pontuacao, motivo, detalhes_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    datetime.now().isoformat(),
                    sinal.ativo,
                    sinal.direcao,
                    sinal.setup,
                    sinal.pontuacao,
                    motivo,
                    json.dumps(sinal.detalhes, default=str),
                ),
            )

    # ------------------------------------------------------------------
    # Monitor forward-test (modo sem execução de ordens reais)
    # ------------------------------------------------------------------

    def registrar_sinal_monitor(
        self,
        sinal: "SinalSwing",
        entrada: float,
        sl: float,
        tp: float,
    ) -> int:
        """Salva sinal do modo monitor com entrada/SL/TP para rastreio de outcome."""
        with self._lock, self._sessao() as db:
            cur = db.execute(
                """INSERT INTO sinais_monitor
                   (registrado_em, ativo, direcao, setup, pontuacao,
                    entrada, sl, tp, detalhes_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                    sinal.ativo,
                    sinal.direcao,
                    sinal.setup,
                    sinal.pontuacao,
                    entrada,
                    sl,
                    tp,
                    json.dumps(sinal.detalhes, default=str),
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def monitor_pendentes(self) -> list[dict]:
        """Retorna sinais monitor sem resultado ainda."""
        with self._lock, self._sessao() as db:
            rows = db.execute(
                """SELECT id, registrado_em, ativo, direcao, setup, pontuacao, entrada, sl, tp
                   FROM sinais_monitor WHERE resultado IS NULL
                   ORDER BY registrado_em"""
            ).fetchall()
        return [
            {
                "id": r[0], "registrado_em": r[1], "ativo": r[2],
                "direcao": r[3], "setup": r[4], "pontuacao": r[5],
                "entrada": r[6], "sl": r[7], "tp": r[8],
            }
            for r in rows
        ]

    def tem_sinal_pendente(self, ativo: str) -> bool:
        """True se já existe sinal sem resolução para esse ativo no monitor."""
        with self._lock, self._sessao() as db:
            n = db.execute(
                "SELECT COUNT(*) FROM sinais_monitor WHERE ativo=? AND resultado IS NULL",
                (ativo,),
            ).fetchone()[0]
        return n > 0

    def resolver_sinal_monitor(self, id_sinal: int, resultado: str) -> None:
        with self._lock, self._sessao() as db:
            db.execute(
                "UPDATE sinais_monitor SET resultado=?, encerrado_em=? WHERE id=?",
                (resultado, datetime.now().isoformat(), id_sinal),
            )

    def resumo_monitor(self) -> dict:
        """Totais de W/L do forward test."""
        with self._lock, self._sessao() as db:
            rows = db.execute(
                "SELECT resultado, COUNT(*) FROM sinais_monitor WHERE resultado IS NOT NULL GROUP BY resultado"
            ).fetchall()
        totais = {r[0]: r[1] for r in rows}
        wins  = totais.get("win", 0)
        losses = totais.get("loss", 0) + totais.get("expirado", 0)
        total = wins + losses
        wr = wins / total if total else 0.0
        return {"wins": wins, "losses": losses, "total": total, "wr": wr}

    def historico_monitor(self, limit: int = 200) -> list[dict]:
        """Retorna todos os sinais (pendentes + resolvidos) para relatório."""
        with self._lock, self._sessao() as db:
            rows = db.execute(
                """SELECT id, registrado_em, ativo, direcao, setup, pontuacao,
                          entrada, sl, tp, resultado, encerrado_em
                   FROM sinais_monitor ORDER BY registrado_em DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        keys = ["id","registrado_em","ativo","direcao","setup","pontuacao",
                "entrada","sl","tp","resultado","encerrado_em"]
        return [dict(zip(keys, r)) for r in rows]

    def pendentes(self) -> list[tuple[str, str, int]]:
        """Retorna (id_ordem, ativo, ts_expiracao) de ordens sem resultado."""
        with self._lock, self._sessao() as db:
            rows = db.execute(
                "SELECT id_ordem, ativo, ts_expiracao FROM operacoes WHERE resultado IS NULL"
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def resumo_dia(self) -> dict:
        hoje = datetime.now().strftime("%Y-%m-%d")
        with self._lock, self._sessao() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM operacoes WHERE enviada_em LIKE ?", (f"{hoje}%",)
            ).fetchone()[0]
            perdas_consec = db.execute(
                """SELECT COUNT(*) FROM (
                     SELECT lucro FROM operacoes
                     WHERE resultado IS NOT NULL
                     ORDER BY enviada_em DESC LIMIT 10
                   ) WHERE lucro < 0"""
            ).fetchone()[0]
            lucro = db.execute(
                "SELECT COALESCE(SUM(lucro),0) FROM operacoes WHERE enviada_em LIKE ? AND lucro IS NOT NULL",
                (f"{hoje}%",),
            ).fetchone()[0]
        return {"operacoes_hoje": total, "lucro_dia": lucro, "perdas_consecutivas": perdas_consec}
