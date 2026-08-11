"""Proteção contra candles incompletos, duplicados e fora de ordem.

Cada candle tem ID único: (ativo, timeframe, ts_abertura_unix).
A execução é idempotente: o mesmo evento pode chegar duas vezes (reconexão,
polling rápido) e no máximo uma avaliação passa.

Thread-safe: um único CandleGuard pode ser compartilhado entre threads.
"""
from __future__ import annotations

import threading

import pandas as pd

from .timing import (
    MOTIVO_CANDLE_DUPLICADO,
    MOTIVO_CANDLE_FORA_DE_ORDEM,
    MOTIVO_CANDLE_INCOMPLETO,
    candle_esta_fechado,
)


def candle_ts_para_unix(ts: pd.Timestamp) -> int:
    """Converte pd.Timestamp (tz-naive UTC) para Unix segundos.

    O buffer de candles usa tz-naive porque a IQ retorna 'from' como Unix int
    e pd.to_datetime(..., unit='s') produz tz-naive. Tratamos como UTC.
    """
    if ts.tzinfo is not None:
        return int(ts.timestamp())
    # tz-naive: assume UTC
    return int(ts.value // 1_000_000_000)


class CandleGuard:
    """Rastreia o último candle processado por (ativo, timeframe).

    Responsabilidades:
    - Rejeitar candle ainda em formação (fechamento não confirmado).
    - Rejeitar candle repetido (mesmo ativo, mesma abertura).
    - Rejeitar candle fora de ordem (abertura anterior ao último processado).
    - Confirmar processamento (registrar) após avaliação bem-sucedida.
    """

    def __init__(self) -> None:
        # (ativo, timeframe) -> ts_abertura_unix do último candle PROCESSADO
        self._ultimo: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    def _chave(self, ativo: str, timeframe: int) -> tuple[str, int]:
        return (ativo, timeframe)

    def validar(
        self,
        ativo: str,
        timeframe: int,
        candle_ts_unix: int,
        ts_servidor: int,
    ) -> tuple[bool, str]:
        """Valida se o candle pode ser processado.

        Args:
            ativo: nome do ativo (ex: "EURUSD").
            timeframe: segundos por candle (60 ou 300).
            candle_ts_unix: Unix timestamp de ABERTURA do candle candidato
                            (normalmente index[-2] do buffer).
            ts_servidor: timestamp atual do servidor IQ.

        Returns:
            (valido, motivo) — motivo é "" quando valido=True, ou uma das
            constantes MOTIVO_* de timing.py quando valido=False.
        """
        # 1. Candle fechado?
        if not candle_esta_fechado(candle_ts_unix, ts_servidor, timeframe):
            return False, MOTIVO_CANDLE_INCOMPLETO

        chave = self._chave(ativo, timeframe)
        with self._lock:
            ultimo = self._ultimo.get(chave)

            # 2. Duplicado?
            if ultimo is not None and candle_ts_unix == ultimo:
                return False, MOTIVO_CANDLE_DUPLICADO

            # 3. Fora de ordem?
            if ultimo is not None and candle_ts_unix < ultimo:
                return False, MOTIVO_CANDLE_FORA_DE_ORDEM

        return True, ""

    def registrar(self, ativo: str, timeframe: int, candle_ts_unix: int) -> None:
        """Marca o candle como processado. Chamar após avaliação bem-sucedida."""
        chave = self._chave(ativo, timeframe)
        with self._lock:
            self._ultimo[chave] = candle_ts_unix

    def ultimo_processado(self, ativo: str, timeframe: int) -> int | None:
        """Retorna o ts_abertura do último candle processado, ou None."""
        return self._ultimo.get(self._chave(ativo, timeframe))

    def resetar(self, ativo: str, timeframe: int) -> None:
        """Remove o estado de um ativo — útil após reconexão."""
        chave = self._chave(ativo, timeframe)
        with self._lock:
            self._ultimo.pop(chave, None)

    def resetar_tudo(self) -> None:
        """Remove todos os estados — reinicia deduplicação global."""
        with self._lock:
            self._ultimo.clear()
