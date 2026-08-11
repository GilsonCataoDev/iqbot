"""Detecção de regime de mercado a partir de indicadores já calculados.

Usa TendenciaMacro (EMA_Macro slope vs ATR) e a razão ATR/mediana-histórica
para classificar o mercado em cinco estados. Os indicadores são computados pela
EstrategiaReversaoM5 antes de chamar esta função — sem recalcular nada.

Thread-safe: detectar_regime é pura (sem estado externo).
"""
from __future__ import annotations

from enum import Enum

import pandas as pd


class Regime(str, Enum):
    """Estado do mercado no candle avaliado.

    Usar como string (herda str) permite comparações diretas com literais
    e serialização em JSON/SQLite sem conversão.
    """
    LATERAL          = "LATERAL"
    TENDENCIA_ALTA   = "TENDENCIA_ALTA"
    TENDENCIA_BAIXA  = "TENDENCIA_BAIXA"
    VOLATILIDADE_ALTA = "VOLATILIDADE_ALTA"
    INDEFINIDO       = "INDEFINIDO"


def detectar_regime(
    df: pd.DataFrame,
    indice: int,
    atr_regime_janela: int = 50,
    atr_max_multiplo_mediana: float = 2.0,
) -> Regime:
    """Detecta o regime de mercado no candle `indice` do DataFrame de indicadores.

    Args:
        df: DataFrame com colunas ATR e TendenciaMacro já calculadas.
        indice: posição do candle a avaliar (normalmente len(df) - 2).
        atr_regime_janela: janela histórica (candles) para calcular a mediana do ATR.
        atr_max_multiplo_mediana: se ATR atual > mediana * este valor → VOLATILIDADE_ALTA.

    Returns:
        Um valor de Regime.
    """
    if indice < 1 or indice >= len(df):
        return Regime.INDEFINIDO

    candle = df.iloc[indice]

    atr_val = candle.get("ATR")
    tendencia = candle.get("TendenciaMacro")

    if atr_val is None or pd.isna(atr_val):
        return Regime.INDEFINIDO
    if tendencia is None or pd.isna(tendencia):
        return Regime.INDEFINIDO

    atr = float(atr_val)

    # Volatilidade: ATR atual vs mediana histórica
    inicio = max(0, indice - atr_regime_janela)
    hist_atr = df["ATR"].iloc[inicio:indice].dropna()
    if len(hist_atr) >= 5:
        mediana = float(hist_atr.median())
        if mediana > 0 and atr > atr_max_multiplo_mediana * mediana:
            return Regime.VOLATILIDADE_ALTA

    if tendencia == "alta":
        return Regime.TENDENCIA_ALTA
    if tendencia == "baixa":
        return Regime.TENDENCIA_BAIXA
    if tendencia == "lateral":
        return Regime.LATERAL

    return Regime.INDEFINIDO
