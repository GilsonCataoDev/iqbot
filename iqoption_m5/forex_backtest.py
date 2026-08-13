"""Backtest sequencial do executor paper Forex."""

from __future__ import annotations

import pandas as pd

from .forex_estrategia import (
    planos_correcao_fibo_sr,
    planos_rompimento_reteste,
    planos_toque_lta_ltb,
)
from .forex_executor import ExecutorForexSimulado


def simular_forex(
    ativo: str,
    candles: pd.DataFrame,
    banca: float = 1000.0,
    risco_percentual: float = 0.0025,
    spread: float = 0.00010,
    estrategia: str = "rompimento_reteste",
) -> tuple[pd.DataFrame, float]:
    executor = ExecutorForexSimulado(banca, risco_percentual, spread)
    if estrategia == "rompimento_reteste":
        planos = planos_rompimento_reteste(ativo, candles, spread=spread)
    elif estrategia == "toque_lta_ltb":
        planos = planos_toque_lta_ltb(ativo, candles)
    elif estrategia == "correcao_fibo_sr":
        planos = planos_correcao_fibo_sr(ativo, candles)
    else:
        raise ValueError(f"Estratégia Forex desconhecida: {estrategia}")
    plano_pendente = None
    for indice in range(60, len(candles)):
        candle = candles.iloc[indice]
        horario = candles.index[indice].to_pydatetime()
        if executor.posicao is not None:
            executor.atualizar(horario, float(candle["High"]), float(candle["Low"]))
        if plano_pendente is not None and executor.posicao is None:
            executor.abrir(plano_pendente, float(candle["Open"]), horario)
            executor.atualizar(horario, float(candle["High"]), float(candle["Low"]))
            plano_pendente = None
        if executor.posicao is None:
            plano_pendente = planos.iloc[indice]
    dados = pd.DataFrame([resultado.__dict__ for resultado in executor.resultados])
    return dados, executor.banca
