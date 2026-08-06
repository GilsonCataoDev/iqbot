from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class SnapshotMercado:
    ativo: str
    candles: pd.DataFrame
    payout: float | None
    mercado_aberto: bool
    timestamp_servidor: int


@dataclass(frozen=True)
class Decisao:
    ativo: str
    direcao: str
    preco: float
    candle_hora: pd.Timestamp
    motivo: str
    detalhes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Autorizacao:
    permitida: bool
    motivo: str


@dataclass(frozen=True)
class ResultadoOrdem:
    id_ordem: str
    ativo: str
    direcao: str
    enviada_em: datetime
    encerrada_em: datetime
    valor: float
    payout: float
    lucro: float | None
    resultado_bruto: Any


@dataclass(frozen=True)
class OperacaoPendente:
    id_ordem: str
    ativo: str
    direcao: str
    enviada_em: datetime
    valor: float
    payout: float


@dataclass(frozen=True)
class ResumoRisco:
    operacoes_enviadas: int
    operacoes_finalizadas: int
    perdas_consecutivas: int
    lucro_sessao: float
    lucro_total: float
    ultimo_lucro: float
    banca_atual: float
    ordem_aberta: bool
    encerrado: bool
    motivo_encerramento: str | None


@dataclass(frozen=True)
class EstadoPersistido:
    operacoes_enviadas: int = 0
    operacoes_finalizadas: int = 0
    perdas_consecutivas: int = 0
    lucro_sessao: float = 0.0
    lucro_total: float = 0.0
    ultimo_lucro: float = 0.0
    ordem_pendente: bool = False
