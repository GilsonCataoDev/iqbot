"""Modelos independentes para pesquisa de Forex/CFD."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PlanoForex:
    ativo: str
    lado: str
    sinal_em: datetime
    nivel: float
    stop: float
    alvo: float
    risco_preco: float
    motivo: str


@dataclass
class PosicaoForex:
    ativo: str
    lado: str
    aberta_em: datetime
    entrada: float
    stop: float
    alvo: float
    quantidade: float
    risco_dinheiro: float


@dataclass(frozen=True)
class ResultadoForex:
    ativo: str
    lado: str
    aberta_em: datetime
    fechada_em: datetime
    entrada: float
    saida: float
    quantidade: float
    lucro: float
    motivo_saida: str
