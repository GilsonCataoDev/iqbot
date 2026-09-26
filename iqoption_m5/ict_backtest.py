"""Backtest sequencial da estratégia ICT/SMC (Ouro) sobre o executor paper Forex."""
from __future__ import annotations

import pandas as pd

from .forex_executor import ExecutorForexSimulado
from .ict_estrategia import planos_ict_smc


def simular_ict_smc(
    ativo: str,
    candles: pd.DataFrame,
    correlacionado: pd.DataFrame | None = None,
    banca: float = 1000.0,
    risco_percentual: float = 0.0075,
    spread: float = 0.20,
    **parametros_estrategia,
) -> tuple[pd.DataFrame, float]:
    """Roda `planos_ict_smc` candle-a-candle contra o executor paper.

    `risco_percentual` default (0.75%) e `spread` (US$0,20/onça) seguem a
    faixa sugerida no infográfico (0,5%-1% por operação); ajuste conforme a
    corretora/ativo real usado.
    """
    executor = ExecutorForexSimulado(banca, risco_percentual, spread)
    planos = planos_ict_smc(ativo, candles, correlacionado, **parametros_estrategia)
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
