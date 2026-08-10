import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from pathlib import Path

from .modelos import (
    Autorizacao,
    Decisao,
    EstadoPersistido,
    OperacaoPendente,
    ResultadoOrdem,
    SnapshotMercado,
)


class RegistroSQLite:
    """Auditoria local: decisões bloqueadas e operações ficam no mesmo banco."""

    def __init__(self, banco: Path):
        self.caminho = banco
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._criar_schema()

    def _conectar(self):
        conexao = sqlite3.connect(self.caminho, timeout=10)
        conexao.execute("PRAGMA journal_mode=WAL")
        return conexao

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
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS decisoes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    registrado_em TEXT NOT NULL,
                    ativo TEXT NOT NULL,
                    candle_hora TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    preco REAL NOT NULL,
                    payout REAL,
                    mercado_aberto INTEGER NOT NULL,
                    permitida INTEGER NOT NULL,
                    motivo_risco TEXT NOT NULL,
                    motivo_estrategia TEXT NOT NULL,
                    detalhes_json TEXT NOT NULL,
                    UNIQUE(ativo, candle_hora, direcao)
                );

                CREATE TABLE IF NOT EXISTS operacoes (
                    id_ordem TEXT PRIMARY KEY,
                    ativo TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    enviada_em TEXT NOT NULL,
                    encerrada_em TEXT,
                    valor REAL NOT NULL,
                    payout REAL NOT NULL,
                    setup TEXT NOT NULL DEFAULT 'desconhecido',
                    preco_entrada REAL,
                    hora_sinal TEXT,
                    atraso_envio_ms INTEGER,
                    lucro REAL,
                    resultado_bruto TEXT,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS simulacoes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ativo TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    setup TEXT NOT NULL,
                    candle_hora TEXT NOT NULL,
                    preco_entrada REAL NOT NULL,
                    payout REAL NOT NULL,
                    resultado TEXT,
                    criado_em TEXT NOT NULL,
                    UNIQUE(ativo, candle_hora, direcao, setup)
                );

                CREATE TABLE IF NOT EXISTS slippage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    registrado_em TEXT NOT NULL,
                    ativo TEXT NOT NULL,
                    id_ordem TEXT NOT NULL,
                    preco_sinal REAL NOT NULL,
                    preco_execucao REAL NOT NULL,
                    slippage_pips REAL NOT NULL
                );
                """
            )
            colunas_operacoes = {
                linha[1] for linha in db.execute("PRAGMA table_info(operacoes)").fetchall()
            }
            if "setup" not in colunas_operacoes:
                db.execute(
                    "ALTER TABLE operacoes ADD COLUMN setup TEXT NOT NULL DEFAULT 'desconhecido'"
                )
            if "preco_entrada" not in colunas_operacoes:
                db.execute("ALTER TABLE operacoes ADD COLUMN preco_entrada REAL")
            if "hora_sinal" not in colunas_operacoes:
                db.execute("ALTER TABLE operacoes ADD COLUMN hora_sinal TEXT")
            if "atraso_envio_ms" not in colunas_operacoes:
                db.execute("ALTER TABLE operacoes ADD COLUMN atraso_envio_ms INTEGER")

    def registrar_decisao(
        self,
        decisao: Decisao,
        snapshot: SnapshotMercado,
        autorizacao: Autorizacao,
    ) -> None:
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO decisoes (
                    registrado_em, ativo, candle_hora, direcao, preco, payout,
                    mercado_aberto, permitida, motivo_risco, motivo_estrategia, detalhes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    decisao.ativo,
                    decisao.candle_hora.isoformat(),
                    decisao.direcao,
                    decisao.preco,
                    snapshot.payout,
                    int(snapshot.mercado_aberto),
                    int(autorizacao.permitida),
                    autorizacao.motivo,
                    decisao.motivo,
                    json.dumps(decisao.detalhes, ensure_ascii=False),
                ),
            )

    def registrar_abertura(
        self,
        id_ordem: object,
        decisao: Decisao,
        valor: float,
        payout: float,
        enviada_em: datetime,
    ) -> None:
        hora_sinal = decisao.candle_hora.isoformat()
        atraso_envio_ms = max(
            0,
            int(
                (enviada_em - decisao.candle_hora.to_pydatetime().replace(tzinfo=None)).total_seconds()
                * 1000
            ),
        )
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO operacoes (
                    id_ordem, ativo, direcao, enviada_em, valor, payout, setup, preco_entrada,
                    hora_sinal, atraso_envio_ms, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'aberta')
                """,
                (
                    str(id_ordem), decisao.ativo, decisao.direcao, enviada_em.isoformat(),
                    valor, payout, decisao.detalhes.get("setup", "desconhecido"), decisao.preco,
                    hora_sinal, atraso_envio_ms,
                ),
            )

    def registrar_resultado(self, resultado: ResultadoOrdem) -> None:
        with self._lock, self._sessao() as db:
            db.execute(
                """
                UPDATE operacoes
                SET encerrada_em=?, lucro=?, resultado_bruto=?, status='finalizada'
                WHERE id_ordem=?
                """,
                (
                    resultado.encerrada_em.isoformat(),
                    resultado.lucro,
                    str(resultado.resultado_bruto),
                    resultado.id_ordem,
                ),
            )

    def registrar_falha(self, decisao: Decisao, motivo: str) -> None:
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT INTO operacoes (
                    id_ordem, ativo, direcao, enviada_em, valor, payout, setup,
                    resultado_bruto, status
                ) VALUES (?, ?, ?, ?, 0, 0, ?, ?, 'falha_envio')
                """,
                (
                    f"falha-{decisao.ativo}-{decisao.candle_hora.isoformat()}-{datetime.now().timestamp()}",
                    decisao.ativo,
                    decisao.direcao,
                    datetime.now().isoformat(),
                    decisao.detalhes.get("setup", "desconhecido"),
                    motivo,
                ),
            )

    def estado_hoje(self) -> EstadoPersistido:
        hoje = datetime.now().date().isoformat()
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT status, lucro
                FROM operacoes
                WHERE date(enviada_em)=? AND status IN ('aberta', 'finalizada')
                ORDER BY enviada_em ASC
                """,
                (hoje,),
            ).fetchall()
        enviadas = len(linhas)
        finalizadas = sum(1 for status, _ in linhas if status == "finalizada")
        lucro = sum(float(valor) for status, valor in linhas if status == "finalizada" and valor is not None)
        perdas = 0
        for status, valor in reversed(linhas):
            if status != "finalizada" or valor is None:
                break
            if float(valor) < 0:
                perdas += 1
            else:
                break
        return EstadoPersistido(
            operacoes_enviadas=enviadas,
            operacoes_finalizadas=finalizadas,
            perdas_consecutivas=perdas,
            lucro_sessao=lucro,
            lucro_total=self.lucro_total(),
            ultimo_lucro=self.ultimo_lucro(),
            ordem_pendente=any(status == "aberta" for status, _ in linhas),
        )

    def ultimo_lucro(self) -> float:
        """Lucro da ultima operacao REAL finalizada — base do pyramid.

        Exclui ajustes manuais de saldo (setup='correcao_manual'): eles
        conciliam a banca com o valor real na corretora, mas nao sao uma
        entrada de verdade e nao podem alimentar o calculo de alavancagem.
        """
        with self._lock, self._sessao() as db:
            linha = db.execute(
                """
                SELECT lucro FROM operacoes
                WHERE status='finalizada' AND lucro IS NOT NULL
                  AND setup != 'correcao_manual'
                ORDER BY enviada_em DESC LIMIT 1
                """
            ).fetchone()
        return float(linha[0]) if linha and linha[0] is not None else 0.0

    def lucro_total(self) -> float:
        """Soma de todo o histórico, sem filtro de data — usado pro piso de banca."""
        with self._lock, self._sessao() as db:
            linha = db.execute(
                "SELECT SUM(lucro) FROM operacoes WHERE status='finalizada' AND lucro IS NOT NULL"
            ).fetchone()
        return float(linha[0]) if linha and linha[0] is not None else 0.0

    def operacoes_pendentes(self) -> list[OperacaoPendente]:
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT id_ordem, ativo, direcao, enviada_em, valor, payout
                FROM operacoes
                WHERE status='aberta'
                ORDER BY enviada_em ASC
                """
            ).fetchall()
        return [
            OperacaoPendente(
                id_ordem=str(id_ordem),
                ativo=ativo,
                direcao=direcao,
                enviada_em=datetime.fromisoformat(enviada_em),
                valor=float(valor),
                payout=float(payout),
            )
            for id_ordem, ativo, direcao, enviada_em, valor, payout in linhas
        ]

    def operacoes_grafico(self, ativo: str, limite: int = 50) -> list[dict]:
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT enviada_em, direcao, lucro, status, setup, preco_entrada
                FROM operacoes
                WHERE ativo=? AND status IN ('aberta', 'finalizada')
                ORDER BY enviada_em DESC LIMIT ?
                """,
                (ativo, limite),
            ).fetchall()
        return [
            {
                "hora": pd.Timestamp(enviada_em).floor("5min"),
                "direcao": direcao,
                "lucro": None if lucro is None else float(lucro),
                "status": status,
                "setup": setup or "desconhecido",
                "preco": None if preco_entrada is None else float(preco_entrada),
            }
            for enviada_em, direcao, lucro, status, setup, preco_entrada in reversed(linhas)
        ]

    def resumo_desempenho(self, ativo: str, limite: int = 200) -> dict:
        """Winrate das ultimas operacoes finalizadas — pra saber se vale confiar no sinal."""
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT lucro FROM operacoes
                WHERE ativo=? AND status='finalizada' AND lucro IS NOT NULL
                ORDER BY enviada_em DESC LIMIT ?
                """,
                (ativo, limite),
            ).fetchall()
        total = len(linhas)
        vitorias = sum(1 for (lucro,) in linhas if lucro > 0)
        return {
            "total": total,
            "vitorias": vitorias,
            "winrate": round(100 * vitorias / total, 1) if total else None,
        }

    def registrar_simulacao(
        self, ativo: str, direcao: str, setup: str, candle_hora, preco_entrada: float,
        payout: float, resultado: str | None,
    ) -> None:
        """Guarda o resultado de um sinal que NAO virou ordem real — pra
        medir winrate de pullback/bollinger sem arriscar dinheiro. Tabela
        totalmente separada de 'operacoes': nunca entra no calculo de
        lucro_total/banca_atual."""
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO simulacoes
                (ativo, direcao, setup, candle_hora, preco_entrada, payout, resultado, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ativo, direcao, setup, str(candle_hora), preco_entrada, payout, resultado,
                 datetime.now().isoformat()),
            )

    def desempenho_simulado_por_setup(self, limite: int = 500) -> dict[str, dict]:
        """Winrate por setup dos sinais SIMULADOS (sem dinheiro real) —
        pullback e bollinger, pra comparar com a reversao_candle real."""
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT setup, resultado, payout FROM simulacoes
                WHERE resultado IS NOT NULL
                ORDER BY criado_em DESC LIMIT ?
                """,
                (limite,),
            ).fetchall()
        agregado: dict[str, dict] = {}
        for setup, resultado, payout in linhas:
            item = agregado.setdefault(setup, {"total": 0, "vitorias": 0, "lucro": 0.0})
            item["total"] += 1
            if resultado == "win":
                item["vitorias"] += 1
                item["lucro"] += float(payout)
            elif resultado == "loss":
                item["lucro"] -= 1.0
        for item in agregado.values():
            item["winrate"] = round(100 * item["vitorias"] / item["total"], 1) if item["total"] else None
            item["lucro"] = round(item["lucro"], 2)
        return agregado

    def desempenho_por_setup(self, limite: int = 500) -> dict[str, dict]:
        """Winrate agrupado por estrategia (setup), todos os ativos juntos —
        pra comparar reversao_candle (validada) contra pullback/bollinger
        (nao validadas) com dinheiro real."""
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT setup, lucro FROM operacoes
                WHERE status='finalizada' AND lucro IS NOT NULL AND setup != 'correcao_manual'
                ORDER BY enviada_em DESC LIMIT ?
                """,
                (limite,),
            ).fetchall()
        agregado: dict[str, dict] = {}
        for setup, lucro in linhas:
            item = agregado.setdefault(setup, {"total": 0, "vitorias": 0, "lucro": 0.0})
            item["total"] += 1
            item["lucro"] += float(lucro)
            if lucro > 0:
                item["vitorias"] += 1
        for item in agregado.values():
            item["winrate"] = round(100 * item["vitorias"] / item["total"], 1) if item["total"] else None
            item["lucro"] = round(item["lucro"], 2)
        return agregado

    def funil_reversao_hoje(self, motivo_estrategia: str = "reversao_candle_curta") -> dict:
        """Quantos sinais confirmados da estrategia validada apareceram hoje
        e quantos viraram ordem de verdade — pra saber se a impressao de
        'perde muita entrada' e real ou so falta de visibilidade."""
        hoje = datetime.now().date().isoformat()
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT motivo_risco FROM decisoes
                WHERE motivo_estrategia=? AND date(candle_hora)=?
                """,
                (motivo_estrategia, hoje),
            ).fetchall()
        bloqueios: dict[str, int] = {}
        confirmados = len(linhas)
        executados = 0
        for (motivo,) in linhas:
            if motivo == "autorizada":
                executados += 1
            else:
                bloqueios[motivo] = bloqueios.get(motivo, 0) + 1
        return {"confirmados": confirmados, "executados": executados, "bloqueios": bloqueios}

    def registrar_slippage(
        self, ativo: str, id_ordem: str, preco_sinal: float, preco_execucao: float
    ) -> None:
        slippage_pips = abs(preco_execucao - preco_sinal)
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT INTO slippage (registrado_em, ativo, id_ordem, preco_sinal,
                    preco_execucao, slippage_pips)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (datetime.now().isoformat(), ativo, id_ordem,
                 preco_sinal, preco_execucao, slippage_pips),
            )

    def resumo_slippage(self, limite: int = 500) -> dict:
        """Retorna média e desvio padrão do slippage (em pips) por ativo.

        Calcula o desvio padrão em Python porque o SQLite padrão não tem
        função STDDEV nativa. Usa o mesmo campo `slippage_pips` gravado
        por `registrar_slippage` — que é a diferença absoluta entre o preço
        do sinal (fechamento do candle anterior) e o preço real do candle
        no momento da execução.

        Limitação: `preco_execucao` é o Open do candle corrente disponível
        no snapshot no momento da ordem, não o preço de fill da IQ Option
        (a iqoptionapi não expõe o preço de preenchimento da opção binária).
        """
        import math as _math
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                "SELECT ativo, slippage_pips FROM slippage ORDER BY id DESC LIMIT ?",
                (limite,),
            ).fetchall()

        por_ativo: dict[str, list[float]] = {}
        for ativo, pip in linhas:
            por_ativo.setdefault(ativo, []).append(float(pip))

        resultado: dict[str, dict] = {}
        for ativo, valores in por_ativo.items():
            n = len(valores)
            media = sum(valores) / n
            if n > 1:
                variancia = sum((v - media) ** 2 for v in valores) / (n - 1)
                desvio = _math.sqrt(variancia)
            else:
                desvio = 0.0
            resultado[ativo] = {
                "n": n,
                "media_pips": round(media, 6),
                "desvio_pips": round(desvio, 6),
            }
        return resultado

    def slippage_medio(self, ativo: str | None = None, limite: int = 200) -> float | None:
        with self._lock, self._sessao() as db:
            if ativo:
                linha = db.execute(
                    "SELECT AVG(slippage_pips) FROM slippage WHERE ativo=? ORDER BY id DESC LIMIT ?",
                    (ativo, limite),
                ).fetchone()
            else:
                linha = db.execute(
                    "SELECT AVG(slippage_pips) FROM (SELECT slippage_pips FROM slippage ORDER BY id DESC LIMIT ?)",
                    (limite,),
                ).fetchone()
        return float(linha[0]) if linha and linha[0] is not None else None

    def stats_globais(self) -> dict:
        """Entradas e winrate do dia atual, separados por OTC e mercado normal."""
        hoje = datetime.now().date().isoformat()
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT ativo, lucro FROM operacoes
                WHERE date(enviada_em)=? AND status='finalizada' AND lucro IS NOT NULL
                """,
                (hoje,),
            ).fetchall()
        stats: dict[str, dict] = {
            "otc": {"entradas": 0, "wins": 0, "lucro": 0.0},
            "normal": {"entradas": 0, "wins": 0, "lucro": 0.0},
        }
        for ativo, lucro in linhas:
            chave = "otc" if ativo.upper().endswith("-OTC") else "normal"
            stats[chave]["entradas"] += 1
            stats[chave]["lucro"] += float(lucro)
            if lucro > 0:
                stats[chave]["wins"] += 1
        for chave in ("otc", "normal"):
            n = stats[chave]["entradas"]
            stats[chave]["winrate"] = round(100 * stats[chave]["wins"] / n, 1) if n else None
            stats[chave]["lucro"] = round(stats[chave]["lucro"], 2)
        return stats

    def status_decisoes_grafico(self, ativo: str) -> dict[tuple[str, str], str]:
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT candle_hora, direcao, permitida
                FROM decisoes WHERE ativo=? ORDER BY id DESC LIMIT 100
                """,
                (ativo,),
            ).fetchall()
        return {
            (pd.Timestamp(candle_hora).isoformat(), direcao): "confirmado" if permitida else "bloqueado"
            for candle_hora, direcao, permitida in linhas
        }
