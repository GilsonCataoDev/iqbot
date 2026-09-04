"""Backtest sequencial do executor paper Forex."""

from __future__ import annotations

import pandas as pd

from .forex_estrategia import (
    planos_correcao_fibo_sr,
    planos_pullback_fibo_estrutura_h1,
    planos_rompimento_reteste,
    planos_rompimento_reteste_h1,
    planos_rompimento_asia_londres,
    planos_rejeicao_numero_redondo,
    planos_rompimento_numero_redondo,
    planos_reversao_choque_m15,
    planos_continuacao_choque_m15,
    planos_segunda_entrada_ema21,
    planos_reversao_media_rsi_bollinger,
    planos_pullback_h1_confirmado,
    planos_pullback_h1_com_fibo,
    planos_pullback_h1_nova_york,
    planos_varredura_londres,
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
    parametros_estrategia: dict | None = None,
    ambiguidade_intrabar: str = "stop",
) -> tuple[pd.DataFrame, float]:
    executor = ExecutorForexSimulado(banca, risco_percentual, spread, ambiguidade_intrabar)
    parametros = parametros_estrategia or {}
    if estrategia == "rompimento_reteste":
        planos = planos_rompimento_reteste(ativo, candles, spread=spread, **parametros)
    elif estrategia == "rompimento_reteste_h1":
        planos = planos_rompimento_reteste_h1(ativo, candles, spread=spread, **parametros)
    elif estrategia == "varredura_londres":
        planos = planos_varredura_londres(ativo, candles, **parametros)
    elif estrategia == "pullback_h1_confirmado":
        planos = planos_pullback_h1_confirmado(ativo, candles, **parametros)
    elif estrategia == "pullback_h1_com_fibo":
        planos = planos_pullback_h1_com_fibo(ativo, candles, **parametros)
    elif estrategia == "rompimento_asia_londres":
        planos = planos_rompimento_asia_londres(ativo, candles, **parametros)
    elif estrategia == "pullback_h1_nova_york":
        planos = planos_pullback_h1_nova_york(ativo, candles, **parametros)
    elif estrategia == "rejeicao_numero_redondo":
        planos = planos_rejeicao_numero_redondo(ativo, candles, **parametros)
    elif estrategia == "rompimento_numero_redondo":
        planos = planos_rompimento_numero_redondo(ativo, candles, **parametros)
    elif estrategia == "reversao_choque_m15":
        planos = planos_reversao_choque_m15(ativo, candles, **parametros)
    elif estrategia == "continuacao_choque_m15":
        planos = planos_continuacao_choque_m15(ativo, candles, **parametros)
    elif estrategia == "segunda_entrada_ema21":
        planos = planos_segunda_entrada_ema21(ativo, candles, **parametros)
    elif estrategia == "reversao_media_rsi_bollinger":
        planos = planos_reversao_media_rsi_bollinger(ativo, candles, **parametros)
    elif estrategia == "toque_lta_ltb":
        planos = planos_toque_lta_ltb(ativo, candles, **parametros)
    elif estrategia == "correcao_fibo_sr":
        planos = planos_correcao_fibo_sr(ativo, candles, **parametros)
    elif estrategia == "pullback_fibo_estrutura_h1":
        planos = planos_pullback_fibo_estrutura_h1(ativo, candles, **parametros)
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
