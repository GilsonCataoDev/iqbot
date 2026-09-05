import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from pathlib import Path

from .config import Configuracao
from .auditoria_entrada import recuperar_comparaveis, resumo_contexto
from .modelos import (
    Autorizacao,
    Decisao,
    EstadoPersistido,
    OperacaoPendente,
    ResultadoOrdem,
    SnapshotMercado,
)

VERSAO_SCHEMA = 4


def _wilson_ci(vitorias: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = vitorias / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / denom
    return (round((center - margin) * 100, 1), round((center + margin) * 100, 1))


def _maturidade(n: int) -> str:
    if n < 30:
        return "INSUFICIENTE"
    if n < 100:
        return "OBSERVAR"
    if n < 300:
        return "CANDIDATA"
    return "APROVADA"


class RegistroSQLite:
    """Auditoria local: decisões bloqueadas e operações ficam no mesmo banco."""

    def __init__(self, banco: Path, config: Configuracao | None = None):
        self.caminho = banco
        self.config = config
        self.campanha_id = uuid.uuid4().hex
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._criar_schema()
        self._registrar_campanha()

    def _conectar(self):
        conexao = sqlite3.connect(self.caminho, timeout=10)
        # sqlite3.Row aceita indice E nome de coluna: o desempacotamento em
        # tupla das consultas antigas continua valendo, e as consultas que
        # acessam row["coluna"] param de estourar TypeError.
        conexao.row_factory = sqlite3.Row
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
            versao_atual = int(db.execute("PRAGMA user_version").fetchone()[0])
            if versao_atual > VERSAO_SCHEMA:
                raise RuntimeError(
                    f"Banco usa schema {versao_atual}, mas este programa suporta até {VERSAO_SCHEMA}."
                )
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
                    timeframe INTEGER NOT NULL DEFAULT 0,
                    setup TEXT NOT NULL DEFAULT 'desconhecido',
                    detalhes_json TEXT NOT NULL,
                    UNIQUE(ativo, candle_hora, direcao, timeframe, setup)
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
                    timeframe INTEGER NOT NULL DEFAULT 0,
                    expiracao_minutos INTEGER NOT NULL DEFAULT 0,
                    preco_entrada REAL,
                    hora_sinal TEXT,
                    atraso_envio_ms INTEGER,
                    lucro REAL,
                    resultado_bruto TEXT,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS campanhas (
                    id TEXT PRIMARY KEY,
                    iniciada_em TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    timeframe INTEGER,
                    janela_entrada_segundos INTEGER,
                    config_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS simulacoes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ativo TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    setup TEXT NOT NULL,
                    timeframe INTEGER NOT NULL DEFAULT 0,
                    candle_hora TEXT NOT NULL,
                    preco_entrada REAL NOT NULL,
                    payout REAL NOT NULL,
                    resultado TEXT,
                    criado_em TEXT NOT NULL,
                    UNIQUE(ativo, candle_hora, direcao, timeframe, setup)
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

                CREATE TABLE IF NOT EXISTS latencias (
                    signal_id TEXT PRIMARY KEY,
                    ativo TEXT NOT NULL,
                    setup TEXT NOT NULL,
                    timeframe INTEGER NOT NULL,
                    id_ordem TEXT,
                    ts_abertura_candle INTEGER,
                    ts_fechamento_esperado INTEGER,
                    ts_recebimento_candle REAL,
                    ts_inicio_calculo REAL,
                    ts_fim_calculo REAL,
                    ts_envio_ordem REAL,
                    ts_confirmacao_ordem REAL,
                    ts_resultado REAL,
                    latencia_recebimento_ms REAL,
                    latencia_calculo_ms REAL,
                    latencia_envio_ms REAL,
                    latencia_confirmacao_ms REAL,
                    idade_sinal_ms REAL,
                    offset_servidor_s REAL,
                    registrado_em TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pre_alertas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    registrado_em TEXT NOT NULL,
                    ativo TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    nivel_sr REAL NOT NULL,
                    setup TEXT NOT NULL,
                    segundo_no_candle INTEGER NOT NULL,
                    ts_unix INTEGER NOT NULL,
                    converteu INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS candles_historico (
                    ativo TEXT NOT NULL,
                    timeframe INTEGER NOT NULL,
                    ts_unix INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    PRIMARY KEY (ativo, timeframe, ts_unix)
                );

                CREATE TABLE IF NOT EXISTS opinioes_groq (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ativo TEXT NOT NULL,
                    setup TEXT,
                    direcao TEXT,
                    timeframe INTEGER,
                    veredicto TEXT NOT NULL,
                    motivo TEXT,
                    modelo TEXT,
                    latencia_ms INTEGER,
                    criado_em TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS marcacoes_manuais (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ativo TEXT NOT NULL,
                    painel TEXT NOT NULL,
                    tipo TEXT NOT NULL,
                    preco_a REAL NOT NULL,
                    preco_b REAL,
                    tempo_a INTEGER,
                    tempo_b INTEGER,
                    direcao TEXT,
                    rotulo TEXT,
                    criado_em TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_marcacoes_ativo
                    ON marcacoes_manuais(painel, ativo);

                CREATE INDEX IF NOT EXISTS idx_operacoes_status_data
                    ON operacoes(status, enviada_em);
                CREATE INDEX IF NOT EXISTS idx_operacoes_ativo_data
                    ON operacoes(ativo, enviada_em);
                CREATE INDEX IF NOT EXISTS idx_decisoes_ativo_data
                    ON decisoes(ativo, candle_hora);
                CREATE INDEX IF NOT EXISTS idx_simulacoes_setup_data
                    ON simulacoes(setup, candle_hora);
                CREATE INDEX IF NOT EXISTS idx_opinioes_groq_data
                    ON opinioes_groq(criado_em);
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
            if "campanha_id" not in colunas_operacoes:
                db.execute("ALTER TABLE operacoes ADD COLUMN campanha_id TEXT")
            if "timeframe" not in colunas_operacoes:
                db.execute("ALTER TABLE operacoes ADD COLUMN timeframe INTEGER NOT NULL DEFAULT 0")
            if "expiracao_minutos" not in colunas_operacoes:
                db.execute("ALTER TABLE operacoes ADD COLUMN expiracao_minutos INTEGER NOT NULL DEFAULT 0")
            colunas_decisoes = {
                linha[1] for linha in db.execute("PRAGMA table_info(decisoes)").fetchall()
            }
            if "timeframe" not in colunas_decisoes or "setup" not in colunas_decisoes:
                # O schema antigo descartava decisões diferentes do mesmo par,
                # candle e direção. Reconstrói a tabela preservando o histórico.
                db.execute("DROP INDEX IF EXISTS idx_decisoes_ativo_data")
                db.executescript(
                    """
                    CREATE TABLE decisoes_nova (
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
                        timeframe INTEGER NOT NULL DEFAULT 0,
                        setup TEXT NOT NULL DEFAULT 'desconhecido',
                        detalhes_json TEXT NOT NULL,
                        UNIQUE(ativo, candle_hora, direcao, timeframe, setup)
                    );
                    INSERT INTO decisoes_nova (
                        registrado_em, ativo, candle_hora, direcao, preco, payout,
                        mercado_aberto, permitida, motivo_risco, motivo_estrategia,
                        timeframe, setup, detalhes_json
                    )
                    SELECT registrado_em, ativo, candle_hora, direcao, preco, payout,
                           mercado_aberto, permitida, motivo_risco, motivo_estrategia,
                           0, motivo_estrategia, detalhes_json
                    FROM decisoes;
                    DROP TABLE decisoes;
                    ALTER TABLE decisoes_nova RENAME TO decisoes;
                    CREATE INDEX idx_decisoes_ativo_data ON decisoes(ativo, candle_hora);
                    """
                )
            colunas_simulacoes = {
                linha[1] for linha in db.execute("PRAGMA table_info(simulacoes)").fetchall()
            }
            if "timeframe" not in colunas_simulacoes:
                # A chave antiga misturava M5 e M15 quando ambos geravam o
                # mesmo sinal. Migra sem perder histórico; o passado fica em
                # timeframe=0 (origem não recuperável), e a nova campanha
                # passa a separar os dois horizontes corretamente.
                motivo_origem = "motivo" if "motivo" in colunas_simulacoes else "'nao_bloqueado'"
                db.executescript(
                    f"""
                    CREATE TABLE simulacoes_nova (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ativo TEXT NOT NULL,
                        direcao TEXT NOT NULL,
                        setup TEXT NOT NULL,
                        timeframe INTEGER NOT NULL DEFAULT 0,
                        candle_hora TEXT NOT NULL,
                        preco_entrada REAL NOT NULL,
                        payout REAL NOT NULL,
                        resultado TEXT,
                        criado_em TEXT NOT NULL,
                        motivo TEXT NOT NULL DEFAULT 'nao_bloqueado',
                        UNIQUE(ativo, candle_hora, direcao, timeframe, setup)
                    );
                    INSERT INTO simulacoes_nova (
                        id, ativo, direcao, setup, timeframe, candle_hora,
                        preco_entrada, payout, resultado, criado_em, motivo
                    )
                    SELECT id, ativo, direcao, setup, 0, candle_hora,
                           preco_entrada, payout, resultado, criado_em, {motivo_origem}
                    FROM simulacoes;
                    DROP TABLE simulacoes;
                    ALTER TABLE simulacoes_nova RENAME TO simulacoes;
                    CREATE INDEX idx_simulacoes_setup_data
                        ON simulacoes(setup, candle_hora);
                    """
                )
                colunas_simulacoes.add("timeframe")
            if "motivo" not in colunas_simulacoes:
                # Shadow logging: por que o sinal NAO virou ordem real.
                db.execute(
                    "ALTER TABLE simulacoes ADD COLUMN motivo TEXT NOT NULL DEFAULT 'nao_bloqueado'"
                )
            db.execute(f"PRAGMA user_version={VERSAO_SCHEMA}")

    def _registrar_campanha(self) -> None:
        if self.config is None:
            return
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT INTO campanhas (
                    id, iniciada_em, config_hash, timeframe,
                    janela_entrada_segundos, config_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.campanha_id,
                    datetime.now().isoformat(),
                    self.config.config_hash,
                    self.config.timeframe_segundos,
                    self.config.entrada_max_segundos_no_candle,
                    json.dumps(
                        self.config.configuracao_auditavel(),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                ),
            )

    def registrar_latencia(self, lat, id_ordem: str | None = None) -> None:
        """Persiste um LatenciaSinal no banco. Seguro chamar múltiplas vezes
        (INSERT OR REPLACE): a última chamada (com confirmação/resultado) ganha."""
        from .timing import LatenciaSinal  # import local para não circular
        with self._lock, self._sessao() as db:
            d = lat.para_dict()
            db.execute(
                """
                INSERT OR REPLACE INTO latencias (
                    signal_id, ativo, setup, timeframe, id_ordem,
                    ts_abertura_candle, ts_fechamento_esperado, ts_recebimento_candle,
                    ts_inicio_calculo, ts_fim_calculo, ts_envio_ordem,
                    ts_confirmacao_ordem, ts_resultado,
                    latencia_recebimento_ms, latencia_calculo_ms, latencia_envio_ms,
                    latencia_confirmacao_ms, idade_sinal_ms, offset_servidor_s,
                    registrado_em
                ) VALUES (
                    :signal_id, :ativo, :setup, :timeframe, :id_ordem,
                    :ts_abertura_candle, :ts_fechamento_esperado, :ts_recebimento_candle,
                    :ts_inicio_calculo, :ts_fim_calculo, :ts_envio_ordem,
                    :ts_confirmacao_ordem, :ts_resultado,
                    :latencia_recebimento_ms, :latencia_calculo_ms, :latencia_envio_ms,
                    :latencia_confirmacao_ms, :idade_sinal_ms, :offset_servidor_s,
                    :registrado_em
                )
                """,
                {**d, "id_ordem": id_ordem, "registrado_em": datetime.now().isoformat()},
            )

    def registrar_pre_alerta(
        self,
        ativo: str,
        direcao: str,
        nivel_sr: float,
        setup: str,
        segundo_no_candle: int,
        ts_unix: int,
    ) -> None:
        """Persiste um pré-alerta (tipo='aproximando') para análise de conversão."""
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT INTO pre_alertas
                    (registrado_em, ativo, direcao, nivel_sr, setup,
                     segundo_no_candle, ts_unix, converteu)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    datetime.now().isoformat(),
                    ativo, direcao, nivel_sr, setup,
                    segundo_no_candle, ts_unix,
                ),
            )

    def marcar_conversao_pre_alerta(
        self,
        ativo: str,
        direcao: str,
        ts_unix: int,
        janela_s: int = 300,
    ) -> None:
        """Marca o pré-alerta mais recente de (ativo, direcao) dentro de janela_s como
        convertido — chamado quando o alerta vira uma entrada executada."""
        ts_inicio = ts_unix - janela_s
        with self._lock, self._sessao() as db:
            db.execute(
                """
                UPDATE pre_alertas
                   SET converteu = 1
                 WHERE ativo = ?
                   AND direcao = ?
                   AND ts_unix >= ?
                   AND ts_unix <= ?
                   AND converteu = 0
                   AND id = (
                       SELECT MAX(id) FROM pre_alertas
                        WHERE ativo = ?
                          AND direcao = ?
                          AND ts_unix >= ?
                          AND ts_unix <= ?
                          AND converteu = 0
                   )
                """,
                (ativo, direcao, ts_inicio, ts_unix,
                 ativo, direcao, ts_inicio, ts_unix),
            )

    def taxa_conversao_pre_alertas(self) -> list[dict]:
        """Retorna estatísticas de conversão de pré-alertas agrupadas por ativo e setup."""
        with self._sessao() as db:
            linhas = db.execute(
                """
                SELECT
                    ativo,
                    setup,
                    direcao,
                    COUNT(*) AS pre_alertas,
                    SUM(converteu) AS convertidos,
                    ROUND(AVG(converteu) * 100.0, 1) AS taxa_pct,
                    ROUND(AVG(segundo_no_candle), 1) AS segundo_medio
                FROM pre_alertas
                GROUP BY ativo, setup, direcao
                ORDER BY taxa_pct DESC, pre_alertas DESC
                """
            ).fetchall()
        colunas = ["ativo", "setup", "direcao", "pre_alertas",
                   "convertidos", "taxa_pct", "segundo_medio"]
        return [dict(zip(colunas, linha)) for linha in linhas]

    def registrar_decisao(
        self,
        decisao: Decisao,
        snapshot: SnapshotMercado,
        autorizacao: Autorizacao,
        timeframe: int | None = None,
    ) -> None:
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO decisoes (
                    registrado_em, ativo, candle_hora, direcao, preco, payout,
                    mercado_aberto, permitida, motivo_risco, motivo_estrategia,
                    timeframe, setup, detalhes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    int(timeframe if timeframe is not None else (self.config.timeframe_segundos if self.config else 0)),
                    decisao.detalhes.get("setup", decisao.motivo),
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
        timeframe: int | None = None,
        expiracao_minutos: int | None = None,
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
                    hora_sinal, atraso_envio_ms, campanha_id, timeframe, expiracao_minutos, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'aberta')
                """,
                (
                    str(id_ordem), decisao.ativo, decisao.direcao, enviada_em.isoformat(),
                    valor, payout, decisao.detalhes.get("setup", "desconhecido"), decisao.preco,
                    hora_sinal, atraso_envio_ms,
                    self.campanha_id if self.config is not None else None,
                    int(timeframe if timeframe is not None else (self.config.timeframe_segundos if self.config else 0)),
                    int(expiracao_minutos or 0),
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

    def registrar_resultado_desconhecido(self, resultado: ResultadoOrdem) -> None:
        """Fecha a espera operacional sem inventar um resultado financeiro.

        A operação continua elegível para reconciliação posterior, mas fica
        fora de lucro e win rate enquanto a corretora não devolver o desfecho.
        """
        with self._lock, self._sessao() as db:
            db.execute(
                """
                UPDATE operacoes
                SET encerrada_em=?, lucro=NULL, resultado_bruto=?,
                    status='resultado_desconhecido'
                WHERE id_ordem=?
                """,
                (
                    resultado.encerrada_em.isoformat(),
                    str(resultado.resultado_bruto),
                    resultado.id_ordem,
                ),
            )

    def registrar_falha(self, decisao: Decisao, motivo: str, valor: float = 0.0) -> None:
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT INTO operacoes (
                    id_ordem, ativo, direcao, enviada_em, valor, payout, setup,
                    resultado_bruto, campanha_id, status
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 'falha_envio')
                """,
                (
                    f"falha-{decisao.ativo}-{decisao.candle_hora.isoformat()}-{datetime.now().timestamp()}",
                    decisao.ativo,
                    decisao.direcao,
                    datetime.now().isoformat(),
                    valor,
                    decisao.detalhes.get("setup", "desconhecido"),
                    motivo,
                    self.campanha_id if self.config is not None else None,
                ),
            )

    def estado_hoje(self) -> EstadoPersistido:
        hoje = datetime.now().date().isoformat()
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT status, lucro, valor
                FROM operacoes
                WHERE date(enviada_em)=?
                  AND status IN ('aberta', 'finalizada', 'resultado_desconhecido')
                ORDER BY enviada_em ASC
                """,
                (hoje,),
            ).fetchall()
        enviadas = len(linhas)
        finalizadas = sum(1 for status, _, _ in linhas if status == "finalizada")
        valores_risco = [
            float(lucro) if status == "finalizada" and lucro is not None else -float(valor)
            for status, lucro, valor in linhas
            if status in ("finalizada", "resultado_desconhecido")
            and (lucro is not None or status == "resultado_desconhecido")
        ]
        lucro = sum(valores_risco)
        perdas = 0
        for status, valor_lucro, valor_ordem in reversed(linhas):
            if status == "resultado_desconhecido":
                valor_risco = -float(valor_ordem)
            elif status == "finalizada" and valor_lucro is not None:
                valor_risco = float(valor_lucro)
            else:
                break
            if valor_risco < 0:
                perdas += 1
            else:
                break
        return EstadoPersistido(
            operacoes_enviadas=enviadas,
            operacoes_finalizadas=finalizadas,
            perdas_consecutivas=perdas,
            lucro_sessao=lucro,
            lucro_total=self.lucro_total_risco(),
            ultimo_lucro=self.ultimo_lucro(),
            ordem_pendente=any(status == "aberta" for status, _, _ in linhas),
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

    def lucro_total_risco(self) -> float:
        """Saldo conservador: desconhecidos reservam a perda máxima."""
        with self._lock, self._sessao() as db:
            linha = db.execute(
                """
                SELECT SUM(
                    CASE
                        WHEN status='finalizada' AND lucro IS NOT NULL THEN lucro
                        WHEN status='resultado_desconhecido' THEN -valor
                        ELSE 0
                    END
                )
                FROM operacoes
                """
            ).fetchone()
        return float(linha[0]) if linha and linha[0] is not None else 0.0

    def operacoes_pendentes(self) -> list[OperacaoPendente]:
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT id_ordem, ativo, direcao, enviada_em, valor, payout, setup,
                       timeframe, expiracao_minutos
                FROM operacoes
                WHERE status IN ('aberta', 'resultado_desconhecido')
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
                setup=setup or "desconhecido",
                timeframe=int(timeframe or 0),
                expiracao_minutos=int(expiracao or 0),
            )
            for id_ordem, ativo, direcao, enviada_em, valor, payout, setup, timeframe, expiracao in linhas
        ]

    def operacoes_abertas(self) -> list[OperacaoPendente]:
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT id_ordem, ativo, direcao, enviada_em, valor, payout, setup,
                       timeframe, expiracao_minutos
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
                setup=setup or "desconhecido",
                timeframe=int(timeframe or 0),
                expiracao_minutos=int(expiracao or 0),
            )
            for id_ordem, ativo, direcao, enviada_em, valor, payout, setup, timeframe, expiracao in linhas
        ]

    def operacoes_grafico(self, ativo: str, limite: int = 50) -> list[dict]:
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT enviada_em, direcao, lucro, status, setup, preco_entrada
                FROM operacoes
                WHERE ativo=?
                  AND status IN ('aberta', 'finalizada', 'resultado_desconhecido')
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

    def decisoes_grafico(self, ativo: str, limite: int = 80):
        """Retorna sinais auditados para o gráfico, inclusive os bloqueados.

        A ordem confirma o que foi executado; esta lista mostra também os
        pontos que o laboratório enxergou, para comparar leitura e execução.
        """
        from .modelos import Decisao

        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT candle_hora, direcao, preco, permitida, motivo_risco,
                       setup, timeframe, detalhes_json
                FROM decisoes
                WHERE ativo=?
                ORDER BY candle_hora DESC, id DESC LIMIT ?
                """,
                (ativo, limite),
            ).fetchall()
        saida = []
        for hora, direcao, preco, permitida, motivo_risco, setup, timeframe, detalhes_json in reversed(linhas):
            try:
                detalhes = json.loads(detalhes_json)
            except (TypeError, json.JSONDecodeError):
                detalhes = {}
            rotulo_tf = f"M{max(1, int(timeframe or 300) // 60)}"
            detalhes["setup"] = f"{rotulo_tf} {setup or 'desconhecido'}"
            detalhes["status_grafico"] = "confirmado" if permitida else "bloqueado"
            detalhes.setdefault("razao", [str(motivo_risco)])
            saida.append(
                Decisao(
                    ativo=ativo,
                    direcao=direcao,
                    preco=float(preco),
                    candle_hora=pd.Timestamp(hora),
                    motivo=setup or "desconhecido",
                    detalhes=detalhes,
                )
            )
        return saida

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
        payout: float, resultado: str | None, timeframe: int | None = None,
    ) -> None:
        """Guarda o resultado de um sinal que NAO virou ordem real — pra
        medir winrate de pullback/bollinger sem arriscar dinheiro. Tabela
        totalmente separada de 'operacoes': nunca entra no calculo de
        lucro_total/banca_atual."""
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO simulacoes
                (ativo, direcao, setup, timeframe, candle_hora, preco_entrada, payout, resultado, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ativo, direcao, setup,
                    int(timeframe if timeframe is not None else (self.config.timeframe_segundos if self.config else 0)),
                    str(candle_hora), preco_entrada, payout, resultado, datetime.now().isoformat(),
                ),
            )

    def registrar_simulacao_bloqueada(
        self, ativo: str, direcao: str, setup: str, candle_hora,
        preco_entrada: float, payout: float, motivo: str, timeframe: int | None = None,
    ) -> None:
        """Shadow logging: registra um sinal que foi BLOQUEADO por algum filtro.

        Fica com resultado=NULL ate `resolver_simulacoes_pendentes` apurar o
        desfecho pelo candle. Permite medir se o filtro que bloqueou estava
        certo ou errado, sem arriscar dinheiro e sem perder amostra.
        """
        with self._lock, self._sessao() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO simulacoes
                (ativo, direcao, setup, timeframe, candle_hora, preco_entrada, payout,
                 resultado, criado_em, motivo)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    ativo, direcao, setup,
                    int(timeframe if timeframe is not None else (self.config.timeframe_segundos if self.config else 0)),
                    str(candle_hora), preco_entrada, payout, datetime.now().isoformat(), motivo,
                ),
            )

    def simulacoes_pendentes(self, ativo: str | None = None) -> list[dict]:
        """Sinais em shadow ainda sem desfecho apurado."""
        sql = ("SELECT id, ativo, direcao, setup, timeframe, candle_hora, preco_entrada, motivo "
               "FROM simulacoes WHERE resultado IS NULL")
        args: tuple = ()
        if ativo:
            sql += " AND ativo=?"
            args = (ativo,)
        sql += " ORDER BY candle_hora"
        with self._lock, self._sessao() as db:
            linhas = db.execute(sql, args).fetchall()
        campos = ["id", "ativo", "direcao", "setup", "timeframe", "candle_hora", "preco_entrada", "motivo"]
        return [dict(zip(campos, l)) for l in linhas]

    def resolver_simulacao(self, id_sim: int, resultado: str) -> None:
        with self._lock, self._sessao() as db:
            db.execute("UPDATE simulacoes SET resultado=? WHERE id=?", (resultado, id_sim))

    def desempenho_simulado_por_motivo(self, limite: int = 1000) -> dict[str, dict]:
        """WR dos sinais bloqueados, agrupado pelo filtro que bloqueou.

        Leitura: WR ALTO num motivo = o filtro esta barrando sinais bons
        (custa dinheiro). WR BAIXO = o filtro esta funcionando.
        Compare sempre com o breakeven = 1/(1+payout).
        """
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT motivo, resultado, payout FROM simulacoes
                WHERE resultado IS NOT NULL AND motivo != 'nao_bloqueado'
                ORDER BY criado_em DESC LIMIT ?
                """,
                (limite,),
            ).fetchall()
        agg: dict[str, dict] = {}
        for motivo, resultado, payout in linhas:
            it = agg.setdefault(motivo, {"total": 0, "vitorias": 0, "lucro_evitado": 0.0})
            it["total"] += 1
            if resultado == "win":
                it["vitorias"] += 1
                it["lucro_evitado"] += float(payout)
            elif resultado == "loss":
                it["lucro_evitado"] -= 1.0
        for it in agg.values():
            it["winrate"] = round(100 * it["vitorias"] / it["total"], 1) if it["total"] else None
            # lucro_evitado > 0 -> o filtro barrou lucro (esta atrapalhando)
            it["lucro_evitado"] = round(it["lucro_evitado"], 2)
        return agg

    def desempenho_simulado_por_setup(self, limite: int = 500) -> dict[str, dict]:
        """Winrate por setup/timeframe dos sinais SIMULADOS (sem dinheiro real).

        Retorna ``{setup: {timeframe_segundos: {total, vitorias, winrate, lucro,
        ic_95, maturidade, amostra_suficiente, ev}}}``.
        """
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT setup, COALESCE(timeframe, 0), resultado, payout
                FROM simulacoes
                WHERE resultado IS NOT NULL
                ORDER BY criado_em DESC LIMIT ?
                """,
                (limite,),
            ).fetchall()
        agregado: dict[str, dict] = {}
        for setup, timeframe, resultado, payout in linhas:
            tf_map = agregado.setdefault(setup, {})
            item = tf_map.setdefault(int(timeframe), {"total": 0, "vitorias": 0, "lucro": 0.0})
            item["total"] += 1
            if resultado == "win":
                item["vitorias"] += 1
                item["lucro"] += float(payout)
            elif resultado == "loss":
                item["lucro"] -= 1.0
        for tf_map in agregado.values():
            for item in tf_map.values():
                n, w = item["total"], item["vitorias"]
                item["winrate"] = round(100 * w / n, 1) if n else None
                item["lucro"] = round(item["lucro"], 2)
                ic = _wilson_ci(w, n)
                item["ic_95"] = list(ic) if ic else None
                item["amostra_suficiente"] = n >= 30
                item["maturidade"] = _maturidade(n)
                item["ev"] = round(item["lucro"] / n, 3) if n else None
        return agregado

    def desempenho_por_setup(self, limite: int = 500) -> dict[str, dict]:
        """Winrate por setup/timeframe das operações PRACTICE finalizadas.

        Retorna ``{setup: {timeframe_segundos: {total, vitorias, winrate, lucro,
        ic_95, maturidade, amostra_suficiente, ev}}}``.
        """
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT setup, COALESCE(timeframe, 0), lucro FROM operacoes
                WHERE status='finalizada' AND lucro IS NOT NULL
                  AND setup != 'correcao_manual'
                ORDER BY enviada_em DESC LIMIT ?
                """,
                (limite,),
            ).fetchall()
        agregado: dict[str, dict] = {}
        for setup, timeframe, lucro in linhas:
            tf_map = agregado.setdefault(setup, {})
            item = tf_map.setdefault(int(timeframe), {"total": 0, "vitorias": 0, "lucro": 0.0})
            item["total"] += 1
            item["lucro"] += float(lucro)
            if lucro > 0:
                item["vitorias"] += 1
        for tf_map in agregado.values():
            for item in tf_map.values():
                n, w = item["total"], item["vitorias"]
                item["winrate"] = round(100 * w / n, 1) if n else None
                item["lucro"] = round(item["lucro"], 2)
                ic = _wilson_ci(w, n)
                item["ic_95"] = list(ic) if ic else None
                item["amostra_suficiente"] = n >= 30
                item["maturidade"] = _maturidade(n)
                item["ev"] = round(item["lucro"] / n, 3) if n else None
        return agregado

    def resumo_movimentos_unicos(self) -> dict:
        """Agrupa ordens do mesmo par/direção/minuto em uma oportunidade.

        O Lab pode abrir EMA9/20 e EMA9/21 sobre o mesmo toque. O financeiro
        continua contabilizando todas as ordens, mas esta métrica não finge que
        elas foram oportunidades independentes.
        """
        hoje = datetime.now().date().isoformat()
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT ativo, direcao, strftime('%Y-%m-%dT%H:%M', enviada_em),
                       COUNT(*),
                       SUM(CASE WHEN status='finalizada' AND lucro > 0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='finalizada' AND lucro < 0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status='finalizada' AND lucro = 0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status != 'finalizada' OR lucro IS NULL THEN 1 ELSE 0 END)
                FROM operacoes
                WHERE date(enviada_em)=? AND status != 'falha_envio'
                GROUP BY ativo, direcao, strftime('%Y-%m-%dT%H:%M', enviada_em)
                """,
                (hoje,),
            ).fetchall()
        resumo = {
            "ordens": 0, "movimentos": len(linhas), "finalizados": 0,
            "vitorias": 0, "perdas": 0, "mistos": 0, "pendentes": 0,
        }
        for _, _, _, quantidade, wins, perdas, empates, pendentes in linhas:
            resumo["ordens"] += int(quantidade)
            if pendentes:
                resumo["pendentes"] += 1
            elif wins and not perdas:
                resumo["finalizados"] += 1
                resumo["vitorias"] += 1
            elif perdas and not wins:
                resumo["finalizados"] += 1
                resumo["perdas"] += 1
            elif wins or perdas or empates:
                resumo["finalizados"] += 1
                resumo["mistos"] += 1
        decisivos = resumo["vitorias"] + resumo["perdas"]
        resumo["winrate"] = (
            round(100 * resumo["vitorias"] / decisivos, 1) if decisivos else None
        )
        return resumo

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

    def registrar_opiniao_groq(self, dados: dict) -> None:
        """Persiste a segunda opinião retornada pelo Groq para auditoria futura."""
        with self._lock, self._sessao() as db:
            db.execute(
                """INSERT INTO opinioes_groq
                   (ativo, setup, direcao, timeframe, veredicto, motivo, modelo, latencia_ms, criado_em)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    dados.get("ativo", ""),
                    dados.get("setup"),
                    dados.get("direcao"),
                    dados.get("timeframe"),
                    dados.get("veredicto", "INCERTO"),
                    dados.get("motivo"),
                    dados.get("modelo"),
                    dados.get("latencia_ms"),
                    datetime.utcnow().isoformat(),
                ),
            )

    def precisao_ia(self, janela_segundos: int = 600) -> dict:
        """Win-rate por veredicto Groq cruzando opinioes_groq com operacoes.

        Busca operações finalizadas no mesmo ativo dentro de `janela_segundos`
        após a opinião ser emitida. Retorna dict {veredicto: {n, wins, winrate}}.
        """
        with self._lock, self._sessao() as db:
            rows = db.execute(
                """
                SELECT og.veredicto,
                       COUNT(*) AS n,
                       SUM(CASE WHEN op.resultado_bruto = 'win' THEN 1 ELSE 0 END) AS wins
                FROM opinioes_groq og
                JOIN operacoes op ON og.ativo = op.ativo
                    AND op.enviada_em >= og.criado_em
                    AND op.enviada_em < datetime(og.criado_em, '+' || :jan || ' seconds')
                    AND op.resultado_bruto IN ('win', 'loss')
                GROUP BY og.veredicto
                """,
                {"jan": janela_segundos},
            ).fetchall()
        resultado: dict = {}
        for row in rows:
            n, wins = row["n"], row["wins"]
            resultado[row["veredicto"]] = {
                "n": n,
                "wins": wins,
                "winrate": round(wins / n * 100, 1) if n else 0.0,
            }
        return resultado

    def stats_globais(self) -> dict:
        """Entradas e winrate do dia atual, separados por OTC e mercado normal.

        Conta TODAS as ordens do dia exceto falha_envio (inclusive as ainda abertas).
        Winrate e lucro calculados apenas sobre as finalizadas com lucro definido.
        """
        hoje = datetime.now().date().isoformat()
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT ativo, status, lucro FROM operacoes
                WHERE date(enviada_em)=? AND status != 'falha_envio'
                """,
                (hoje,),
            ).fetchall()
        stats: dict[str, dict] = {
            "otc":    {"entradas": 0, "wins": 0, "finalizadas": 0, "lucro": 0.0},
            "normal": {"entradas": 0, "wins": 0, "finalizadas": 0, "lucro": 0.0},
        }
        for ativo, status, lucro in linhas:
            chave = "otc" if ativo.upper().endswith("-OTC") else "normal"
            stats[chave]["entradas"] += 1
            if status == "finalizada" and lucro is not None:
                stats[chave]["finalizadas"] += 1
                stats[chave]["lucro"] += float(lucro)
                if lucro > 0:
                    stats[chave]["wins"] += 1
        for chave in ("otc", "normal"):
            n_fin = stats[chave]["finalizadas"]
            stats[chave]["winrate"] = (
                round(100 * stats[chave]["wins"] / n_fin, 1) if n_fin else None
            )
            stats[chave]["lucro"] = round(stats[chave]["lucro"], 2)
        return stats

    def analise_atraso_por_setup(self, limiar_ms: int = 500) -> dict:
        """Agrupa operações finalizadas por setup/timeframe separando atraso rápido vs lento.

        Retorna:
          {setup: {timeframe_int: {
            "rapido": {"n", "wins", "winrate", "media_ms"},
            "lento":  {"n", "wins", "winrate", "media_ms"},
          }}}

        Bucket rápido: atraso_envio_ms < limiar_ms; lento: >= limiar_ms.
        """
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT setup, COALESCE(timeframe, 0), atraso_envio_ms,
                       CASE WHEN resultado_bruto='win' THEN 1 ELSE 0 END
                FROM operacoes
                WHERE status='finalizada' AND resultado_bruto IN ('win', 'loss')
                ORDER BY setup, timeframe, atraso_envio_ms
                """,
            ).fetchall()

        acum: dict = {}
        for setup, tf, atraso_ms, ganhou in linhas:
            atraso_ms = int(atraso_ms or 0)
            bucket = "rapido" if atraso_ms < limiar_ms else "lento"
            chave_tf = acum.setdefault(setup, {}).setdefault(int(tf), {
                "rapido": {"n": 0, "wins": 0, "soma_ms": 0},
                "lento":  {"n": 0, "wins": 0, "soma_ms": 0},
            })
            chave_tf[bucket]["n"] += 1
            chave_tf[bucket]["wins"] += ganhou
            chave_tf[bucket]["soma_ms"] += atraso_ms

        saida: dict = {}
        for setup, tfs in acum.items():
            saida[setup] = {}
            for tf, buckets in tfs.items():
                saida[setup][tf] = {}
                for nome, b in buckets.items():
                    n, wins, soma = b["n"], b["wins"], b["soma_ms"]
                    saida[setup][tf][nome] = {
                        "n": n,
                        "wins": wins,
                        "winrate": round(wins / n * 100, 1) if n > 0 else None,
                        "media_ms": round(soma / n) if n > 0 else None,
                    }
        return saida

    def entradas_hoje_detalhadas(self, data: str | None = None) -> list[dict]:
        """Lista de entradas do dia com justificativa, correlacionada com decisoes.

        Retorna uma entrada por operação (exceto falha_envio), enriquecida com
        o detalhes_json da decisão correspondente (mesmo ativo+direcao, timestamp
        mais próximo dentro de 10 s).

        Args:
            data: data no formato YYYY-MM-DD. None usa a data atual (UTC local).
        """
        import json as _json

        hoje = data if data is not None else datetime.now().date().isoformat()
        with self._lock, self._sessao() as db:
            ops = db.execute(
                """
                SELECT id_ordem, ativo, direcao, status, lucro, enviada_em, setup,
                       timeframe, expiracao_minutos, preco_entrada, atraso_envio_ms
                FROM operacoes
                WHERE date(enviada_em)=? AND status != 'falha_envio'
                ORDER BY enviada_em ASC
                """,
                (hoje,),
            ).fetchall()
            decs = db.execute(
                """
                SELECT ativo, direcao, registrado_em, setup, timeframe, detalhes_json
                FROM decisoes
                WHERE date(registrado_em)=? AND permitida=1
                ORDER BY registrado_em ASC
                """,
                (hoje,),
            ).fetchall()
            historico_auditavel = db.execute(
                """
                SELECT o.id_ordem, o.ativo, o.direcao, o.setup, o.timeframe,
                       o.lucro, d.detalhes_json
                FROM operacoes o
                JOIN decisoes d ON d.ativo=o.ativo
                    AND d.direcao=o.direcao
                    AND d.setup=o.setup
                    AND d.timeframe=o.timeframe
                    AND d.permitida=1
                    AND ABS((julianday(d.registrado_em)-julianday(o.enviada_em))*86400) <= 10
                WHERE o.status='finalizada' AND o.lucro IS NOT NULL
                ORDER BY o.enviada_em DESC
                LIMIT 3000
                """
            ).fetchall()
            ops_opiniao = db.execute(
                """
                SELECT ativo, veredicto, motivo, criado_em
                FROM opinioes_groq
                WHERE date(criado_em) = ?
                ORDER BY criado_em ASC
                """,
                (hoje,),
            ).fetchall()

        # Índice de opiniões Groq: ativo → [(ts, veredicto, motivo)]
        opiniao_idx: dict[str, list] = {}
        for ativo_op, veredicto_op, motivo_op, criado_em_op in ops_opiniao:
            try:
                ts_op_gr = _dt.fromisoformat(criado_em_op).timestamp()
            except Exception:
                continue
            opiniao_idx.setdefault(ativo_op, []).append((ts_op_gr, veredicto_op, motivo_op))

        # Inclui setup/timeframe na chave. Sem isso, duas estratégias que
        # coincidam no mesmo minuto poderiam explicar a ordem errada.
        from datetime import datetime as _dt
        dec_idx: dict[tuple, list] = {}
        for ativo, direcao, reg_em, setup, timeframe, det_json in decs:
            try:
                ts = _dt.fromisoformat(reg_em).timestamp()
            except Exception:
                continue
            dec_idx.setdefault((ativo, direcao, setup, int(timeframe or 0)), []).append((ts, reg_em, det_json))

        historico_idx: dict[tuple, list[dict]] = {}
        for ordem, ativo, direcao, setup, timeframe, lucro_hist, detalhes_json in historico_auditavel:
            try:
                detalhes_hist = _json.loads(detalhes_json) if detalhes_json else {}
            except Exception:
                continue
            chave = (ativo, direcao, setup, int(timeframe or 0))
            historico_idx.setdefault(chave, []).append(
                {"id_ordem": str(ordem), "lucro": float(lucro_hist), "detalhes": detalhes_hist}
            )

        resultado = []
        for (
            id_ordem, ativo, direcao, status, lucro, enviada_em, setup,
            timeframe, expiracao, preco_entrada, atraso_envio_ms,
        ) in ops:
            try:
                ts_op = _dt.fromisoformat(enviada_em).timestamp()
            except Exception:
                ts_op = None

            # Opinião IA mais recente para este ativo dentro de 10 min antes da entrada
            veredicto_ia = motivo_ia = None
            if ts_op is not None:
                cands_gr = [c for c in opiniao_idx.get(ativo, [])
                            if ts_op - 600 <= c[0] <= ts_op + 5]
                if cands_gr:
                    melhor_gr = max(cands_gr, key=lambda x: x[0])
                    veredicto_ia, motivo_ia = melhor_gr[1], melhor_gr[2]

            # Busca decisão mais próxima (≤10 s)
            detalhes: dict = {}
            decisao_reg_em: str | None = None
            if ts_op is not None:
                candidatos = dec_idx.get((ativo, direcao, setup, int(timeframe or 0)), [])
                melhor = min(
                    candidatos, key=lambda x: abs(x[0] - ts_op), default=None
                )
                if melhor and abs(melhor[0] - ts_op) <= 10:
                    decisao_reg_em = melhor[1]
                    try:
                        detalhes = _json.loads(melhor[2]) if melhor[2] else {}
                    except Exception:
                        detalhes = {}

            # Justificativa legível
            motivos: list[str] = detalhes.get("motivos") or detalhes.get("razao") or []
            if not motivos:
                # Para sr_rejeicao e outros: monta do detalhes
                partes = []
                if detalhes.get("tipo_sr"):
                    partes.append(detalhes["tipo_sr"].capitalize())
                if detalhes.get("nivel_sr"):
                    partes.append(f"nível {detalhes['nivel_sr']:.5f}")
                if detalhes.get("mecha_sup_pct") is not None:
                    partes.append(f"mecha {detalhes['mecha_sup_pct']}%")
                if detalhes.get("tendencia_macro"):
                    partes.append(f"tendência {detalhes['tendencia_macro']}")
                if partes:
                    motivos = [", ".join(partes)]

            criterios: list[str] = list(motivos[:3])
            if detalhes.get("toque_faixa"):
                criterios.append("Preço tocou a faixa das médias")
            ema9, ema_longa = detalhes.get("ema9"), detalhes.get("ema20", detalhes.get("ema21"))
            if ema9 is not None and ema_longa is not None:
                relacao = ">" if direcao == "call" else "<"
                criterios.append(f"EMA9 {relacao} EMA longa")
            if detalhes.get("rsi14") is not None:
                criterios.append(f"RSI14 {float(detalhes['rsi14']):.1f}")
            if detalhes.get("corpo_ratio") is not None:
                criterios.append(f"Corpo {float(detalhes['corpo_ratio']) * 100:.0f}% do candle")
            if detalhes.get("toques_anteriores") is not None:
                criterios.append(f"Toques anteriores: {int(detalhes['toques_anteriores'])}")
            if detalhes.get("impulso_atr") is not None:
                criterios.append(f"Impulso: {float(detalhes['impulso_atr']):.2f} ATR")
            criterios.extend(resumo_contexto(detalhes))
            leitura_m5 = detalhes.get("leitura_m5") or {}
            if leitura_m5:
                criterios.extend(leitura_m5.get("criterios") or [])
            comparaveis = recuperar_comparaveis(
                detalhes,
                (
                    item for item in historico_idx.get(
                        (ativo, direcao, setup, int(timeframe or 0)), []
                    ) if item["id_ordem"] != str(id_ordem)
                ),
            )

            if status == "aberta":
                diagnostico = "AGUARDANDO — ordem aberta; o diagnóstico do resultado sai no vencimento."
            elif lucro is not None and lucro > 0:
                diagnostico = "WIN — o preço confirmou a direção do setup até o vencimento."
            elif lucro is not None and lucro < 0:
                diagnostico = (
                    "LOSS — a direção não se confirmou até o vencimento. "
                    "Os critérios abaixo descrevem o contexto; não provam uma causa única da perda."
                )
            elif lucro == 0:
                diagnostico = "EMPATE — a operação terminou sem variação financeira."
            else:
                diagnostico = "RESULTADO PENDENTE — a IQ ainda não devolveu um desfecho confiável."

            # ── Sequência de timestamps (UTC → BRT = UTC-3) ──────────────────
            from datetime import timedelta as _td
            _BRT = _td(hours=-3)
            try:
                dt_env = _dt.fromisoformat(enviada_em)
                enviada_brt = (dt_env + _BRT).strftime("%H:%M:%S")
                vencimento_brt = (dt_env + _BRT + _td(minutes=int(expiracao or 0))).strftime("%H:%M:%S")
            except Exception:
                enviada_brt = vencimento_brt = None
            try:
                decisao_brt = (_dt.fromisoformat(decisao_reg_em) + _BRT).strftime("%H:%M:%S") if decisao_reg_em else None
            except Exception:
                decisao_brt = None

            sequencia = {
                "decisao_em": decisao_brt,
                "enviada_em": enviada_brt,
                "vencimento_em": vencimento_brt,
                "atraso_ms": int(atraso_envio_ms or 0),
            }

            # ── Snapshot de indicadores ───────────────────────────────────────
            corpo_ratio = detalhes.get("corpo_ratio")
            indicadores = {
                "ema9": detalhes.get("ema9"),
                "ema_longa": detalhes.get("ema20") or detalhes.get("ema21"),
                "rsi": detalhes.get("rsi14"),
                "atr": detalhes.get("atr"),
                "corpo_pct": round(float(corpo_ratio) * 100) if corpo_ratio is not None else None,
                "tendencia": detalhes.get("tendencia_macro"),
                "candle_tipo": detalhes.get("tipo_candle") or detalhes.get("tipo"),
            }

            resultado.append({
                "hora": enviada_em[11:16],          # "HH:MM"
                "ativo": ativo,
                "direcao": direcao,
                "setup": setup or detalhes.get("setup", "?"),
                "status": status,
                "lucro": None if lucro is None else float(lucro),
                "timeframe": int(timeframe or 0),
                "expiracao_minutos": int(expiracao or 0),
                "preco_entrada": None if preco_entrada is None else float(preco_entrada),
                "atraso_envio_ms": int(atraso_envio_ms or 0),
                "criterios": criterios[:9],
                "diagnostico": diagnostico,
                "comparaveis": comparaveis,
                "leituraM5": leitura_m5,
                "sequencia": sequencia,
                "indicadores": indicadores,
                "veredictoIA": veredicto_ia,
                "motivoIA": motivo_ia,
            })

        return resultado

    def desempenho_por_hora(self) -> dict:
        """Win-rate por hora BRT (UTC-3) de toda a amostra histórica."""
        with self._lock, self._sessao() as db:
            rows = db.execute(
                """
                SELECT strftime('%H', datetime(enviada_em, '-3 hours')) AS hora,
                       COUNT(*) AS n,
                       SUM(CASE WHEN resultado_bruto = 'win' THEN 1 ELSE 0 END) AS wins
                FROM operacoes
                WHERE resultado_bruto IN ('win', 'loss')
                GROUP BY hora
                ORDER BY hora
                """
            ).fetchall()
        return {
            row["hora"]: {
                "n": row["n"],
                "wins": row["wins"],
                "winrate": round(row["wins"] / row["n"] * 100, 1) if row["n"] else 0.0,
            }
            for row in rows
        }

    def historico_ultimos_dias(self, n: int = 7) -> list:
        """Win-rate e lucro diário nos últimos `n` dias (BRT = UTC-3).

        Retorna lista [{data, n, wins, winrate, lucro}] ordenada por data asc.
        """
        with self._lock, self._sessao() as db:
            rows = db.execute(
                """
                SELECT date(datetime(enviada_em, '-3 hours')) AS data_brt,
                       COUNT(*) AS n,
                       SUM(CASE WHEN resultado_bruto = 'win' THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN lucro IS NOT NULL THEN lucro ELSE 0 END) AS lucro
                FROM operacoes
                WHERE resultado_bruto IN ('win', 'loss')
                  AND enviada_em >= datetime('now', '-' || :dias || ' days')
                GROUP BY data_brt
                ORDER BY data_brt ASC
                """,
                {"dias": n},
            ).fetchall()
        return [
            {
                "data": row["data_brt"],
                "n": row["n"],
                "wins": row["wins"],
                "winrate": round(row["wins"] / row["n"] * 100, 1) if row["n"] else 0.0,
                "lucro": round(float(row["lucro"]), 2),
            }
            for row in rows
        ]

    def desempenho_setup_hora(self) -> dict:
        """Win-rate cruzado por setup e hora BRT (UTC-3).

        Retorna {setup: {hora: {n, wins, winrate}}}.
        """
        with self._lock, self._sessao() as db:
            rows = db.execute(
                """
                SELECT setup,
                       strftime('%H', datetime(enviada_em, '-3 hours')) AS hora,
                       COUNT(*) AS n,
                       SUM(CASE WHEN resultado_bruto = 'win' THEN 1 ELSE 0 END) AS wins
                FROM operacoes
                WHERE resultado_bruto IN ('win', 'loss')
                  AND setup IS NOT NULL AND setup != ''
                GROUP BY setup, hora
                ORDER BY setup, hora
                """
            ).fetchall()
        resultado: dict = {}
        for row in rows:
            n, wins = row["n"], row["wins"]
            resultado.setdefault(row["setup"], {})[row["hora"]] = {
                "n": n,
                "wins": wins,
                "winrate": round(wins / n * 100, 1) if n else 0.0,
            }
        return resultado

    def alertas_degradacao_setup(
        self, min_entradas: int = 5, limiar: float = 40.0
    ) -> dict:
        """Setups com win-rate abaixo de `limiar`% na amostra histórica.

        Só considera setups com pelo menos `min_entradas` finalizadas.
        Retorna {setup: {n, wins, winrate}}.
        """
        with self._lock, self._sessao() as db:
            rows = db.execute(
                """
                SELECT setup,
                       COUNT(*) AS n,
                       SUM(CASE WHEN resultado_bruto = 'win' THEN 1 ELSE 0 END) AS wins
                FROM operacoes
                WHERE resultado_bruto IN ('win', 'loss')
                  AND setup IS NOT NULL AND setup != ''
                GROUP BY setup
                HAVING n >= :min_ent
                """,
                {"min_ent": min_entradas},
            ).fetchall()
        resultado: dict = {}
        for row in rows:
            n, wins = row["n"], row["wins"]
            wr = round(wins / n * 100, 1) if n else 0.0
            if wr < limiar:
                resultado[row["setup"]] = {"n": n, "wins": wins, "winrate": wr}
        return resultado

    # ------------------------------------------------------------------
    # Marcações manuais do operador (fibo e linha horizontal)
    # ------------------------------------------------------------------

    def marcacoes(self, painel: str, ativo: str) -> list[dict]:
        """Marcações que o operador desenhou neste painel para este ativo."""
        with self._lock, self._sessao() as db:
            linhas = db.execute(
                """
                SELECT id, ativo, painel, tipo, preco_a, preco_b,
                       tempo_a, tempo_b, direcao, rotulo, criado_em
                FROM marcacoes_manuais
                WHERE painel = ? AND ativo = ?
                ORDER BY id
                """,
                (painel, ativo),
            ).fetchall()
        return [dict(linha) for linha in linhas]

    def salvar_marcacao(
        self, painel: str, ativo: str, tipo: str, preco_a: float,
        preco_b: float | None = None, tempo_a: int | None = None,
        tempo_b: int | None = None, direcao: str | None = None,
        rotulo: str | None = None,
    ) -> int:
        """Grava uma marcação e devolve o id gerado.

        `tipo` é "fibo" (usa preco_a/preco_b como origem/extremo) ou
        "horizontal" (só preco_a).
        """
        if tipo not in {"fibo", "horizontal"}:
            raise ValueError(f"tipo de marcação desconhecido: {tipo}")
        if tipo == "fibo" and preco_b is None:
            raise ValueError("fibo exige preco_b")
        with self._lock, self._sessao() as db:
            cursor = db.execute(
                """
                INSERT INTO marcacoes_manuais (
                    ativo, painel, tipo, preco_a, preco_b,
                    tempo_a, tempo_b, direcao, rotulo, criado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ativo, painel, tipo, float(preco_a),
                 None if preco_b is None else float(preco_b),
                 tempo_a, tempo_b, direcao, rotulo,
                 datetime.utcnow().isoformat()),
            )
            return int(cursor.lastrowid)

    def remover_marcacao(self, marcacao_id: int) -> bool:
        """Apaga uma marcação. Devolve False se o id não existia."""
        with self._lock, self._sessao() as db:
            cursor = db.execute(
                "DELETE FROM marcacoes_manuais WHERE id = ?", (int(marcacao_id),)
            )
            return cursor.rowcount > 0

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

    # ------------------------------------------------------------------
    # Persistência de candles históricos (backtest candle-a-candle)
    # ------------------------------------------------------------------

    def salvar_candles(
        self, ativo: str, df: pd.DataFrame, timeframe: int
    ) -> int:
        """Persiste candles no banco. Retorna quantos foram inseridos (novos)."""
        if df.empty:
            return 0
        linhas = []
        for ts, row in df.iterrows():
            ts_unix = int(
                (ts if ts.tzinfo else ts.tz_localize("UTC")).timestamp()
            )
            linhas.append((
                ativo, timeframe, ts_unix,
                float(row["Open"]), float(row["High"]),
                float(row["Low"]),  float(row["Close"]),
            ))
        with self._lock, self._sessao() as db:
            cur = db.executemany(
                """
                INSERT OR IGNORE INTO candles_historico
                    (ativo, timeframe, ts_unix, open, high, low, close)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                linhas,
            )
            return cur.rowcount

    def carregar_candles(
        self, ativo: str, timeframe: int, limite: int = 5000
    ) -> pd.DataFrame:
        """Carrega os últimos `limite` candles do banco como DataFrame OHLC.

        O índice é um DatetimeIndex UTC.
        """
        with self._sessao() as db:
            linhas = db.execute(
                """
                SELECT ts_unix, open, high, low, close
                FROM candles_historico
                WHERE ativo=? AND timeframe=?
                ORDER BY ts_unix DESC
                LIMIT ?
                """,
                (ativo, timeframe, limite),
            ).fetchall()
        if not linhas:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
        linhas = linhas[::-1]  # ordem cronológica
        idx = pd.to_datetime([r[0] for r in linhas], unit="s", utc=True)
        return pd.DataFrame(
            {
                "Open":  [r[1] for r in linhas],
                "High":  [r[2] for r in linhas],
                "Low":   [r[3] for r in linhas],
                "Close": [r[4] for r in linhas],
            },
            index=idx,
        )

    def total_candles_armazenados(self, ativo: str, timeframe: int) -> int:
        with self._sessao() as db:
            return db.execute(
                "SELECT COUNT(*) FROM candles_historico WHERE ativo=? AND timeframe=?",
                (ativo, timeframe),
            ).fetchone()[0]
