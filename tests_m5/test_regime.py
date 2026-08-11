"""Testes determinísticos para iqoption_m5/regime.py.

Nenhum teste depende de rede, relógio real ou IQ Option.
Todos os DataFrames são sintéticos — ATR e TendenciaMacro injetados diretamente.
"""
import numpy as np
import pandas as pd
import pytest

from iqoption_m5.regime import Regime, detectar_regime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _df(tendencia: str, atr_atual: float, atr_hist: float, n: int = 60) -> pd.DataFrame:
    """DataFrame com histórico ATR=atr_hist e candle fechado (posição n-2) com atr_atual."""
    atrs = [atr_hist] * n
    atrs[n - 2] = atr_atual  # candle fechado = penúltimo (igual ao app.py: len(df)-2)
    df = pd.DataFrame({
        "ATR":            atrs,
        "TendenciaMacro": [tendencia] * n,
        "Close":          np.ones(n),
    })
    return df


def _indice(df: pd.DataFrame) -> int:
    return len(df) - 2  # candle fechado, igual ao app.py


# ---------------------------------------------------------------------------
# INDEFINIDO: dados insuficientes
# ---------------------------------------------------------------------------

def test_indice_negativo_indefinido():
    df = _df("lateral", 0.001, 0.001)
    assert detectar_regime(df, -1) == Regime.INDEFINIDO


def test_indice_zero_indefinido():
    df = _df("lateral", 0.001, 0.001)
    assert detectar_regime(df, 0) == Regime.INDEFINIDO


def test_indice_fora_do_df_indefinido():
    df = _df("lateral", 0.001, 0.001, n=10)
    assert detectar_regime(df, 100) == Regime.INDEFINIDO


def test_atr_nan_retorna_indefinido():
    df = _df("lateral", 0.001, 0.001)
    df.loc[_indice(df), "ATR"] = np.nan
    assert detectar_regime(df, _indice(df)) == Regime.INDEFINIDO


def test_tendencia_nan_retorna_indefinido():
    df = _df("lateral", 0.001, 0.001)
    df.loc[_indice(df), "TendenciaMacro"] = np.nan
    assert detectar_regime(df, _indice(df)) == Regime.INDEFINIDO


# ---------------------------------------------------------------------------
# LATERAL
# ---------------------------------------------------------------------------

def test_tendencia_lateral_atr_normal_retorna_lateral():
    df = _df("lateral", 0.001, 0.001)
    assert detectar_regime(df, _indice(df)) == Regime.LATERAL


def test_lateral_atr_ligeiramente_acima_nao_bloqueia():
    # atr_atual = 1.9 * mediana < 2.0 * mediana → ainda LATERAL
    df = _df("lateral", atr_atual=0.0019, atr_hist=0.001)
    assert detectar_regime(df, _indice(df)) == Regime.LATERAL


# ---------------------------------------------------------------------------
# TENDENCIA_ALTA
# ---------------------------------------------------------------------------

def test_tendencia_alta_retorna_tendencia_alta():
    df = _df("alta", 0.001, 0.001)
    assert detectar_regime(df, _indice(df)) == Regime.TENDENCIA_ALTA


def test_tendencia_alta_atr_normal_nao_volatilidade():
    df = _df("alta", 0.001, 0.001)
    regime = detectar_regime(df, _indice(df))
    assert regime != Regime.VOLATILIDADE_ALTA


# ---------------------------------------------------------------------------
# TENDENCIA_BAIXA
# ---------------------------------------------------------------------------

def test_tendencia_baixa_retorna_tendencia_baixa():
    df = _df("baixa", 0.001, 0.001)
    assert detectar_regime(df, _indice(df)) == Regime.TENDENCIA_BAIXA


# ---------------------------------------------------------------------------
# VOLATILIDADE_ALTA
# ---------------------------------------------------------------------------

def test_atr_acima_multiplo_retorna_volatilidade():
    # atr_atual = 3 * mediana → VOLATILIDADE_ALTA (multiplo padrão = 2.0)
    df = _df("lateral", atr_atual=0.003, atr_hist=0.001)
    assert detectar_regime(df, _indice(df)) == Regime.VOLATILIDADE_ALTA


def test_volatilidade_precede_tendencia():
    # Mesmo em tendência alta, se ATR explodiu → VOLATILIDADE_ALTA
    df = _df("alta", atr_atual=0.005, atr_hist=0.001)
    assert detectar_regime(df, _indice(df)) == Regime.VOLATILIDADE_ALTA


def test_multiplo_personalizado():
    df = _df("lateral", atr_atual=0.0015, atr_hist=0.001)
    # Com multiplo=1.0: 0.0015 > 1.0 * 0.001 → VOLATILIDADE_ALTA
    assert detectar_regime(df, _indice(df), atr_max_multiplo_mediana=1.0) == Regime.VOLATILIDADE_ALTA
    # Com multiplo=2.0 (padrão): 0.0015 < 2.0 * 0.001 → LATERAL
    assert detectar_regime(df, _indice(df), atr_max_multiplo_mediana=2.0) == Regime.LATERAL


def test_historico_curto_nao_classifica_volatilidade():
    """< 5 candles de histórico ATR → não tem mediana confiável → não dispara VOLATILIDADE_ALTA."""
    df = _df("lateral", atr_atual=0.999, atr_hist=0.001, n=5)
    # indice=3 → hist = iloc[max(0,3-50):3] = 3 candles, < 5 → sem filtro de volatilidade
    regime = detectar_regime(df, 3, atr_regime_janela=50)
    # Sem mediana confiável, retorna baseado em tendencia
    assert regime == Regime.LATERAL


# ---------------------------------------------------------------------------
# Integração: janela configurável
# ---------------------------------------------------------------------------

def test_janela_regime_menor_ignora_picos_antigos():
    """Com janela=5, só os 5 candles imediatos influenciam a mediana."""
    n = 60
    atrs = [0.001] * n
    atrs[n - 2] = 0.0025  # candle a avaliar (penúltimo)
    df = pd.DataFrame({
        "ATR": atrs,
        "TendenciaMacro": ["lateral"] * n,
        "Close": np.ones(n),
    })
    indice = n - 2
    # hist = df["ATR"].iloc[indice-5 : indice] = 5 candles todos com 0.001
    # mediana = 0.001; limite = 2 * 0.001 = 0.002; 0.0025 > 0.002 → VOLATILIDADE_ALTA
    assert detectar_regime(df, indice, atr_regime_janela=5) == Regime.VOLATILIDADE_ALTA


# ---------------------------------------------------------------------------
# Regime é str (herança) — serialização
# ---------------------------------------------------------------------------

def test_regime_e_str():
    assert Regime.LATERAL == "LATERAL"
    assert Regime.TENDENCIA_ALTA == "TENDENCIA_ALTA"
    assert Regime.VOLATILIDADE_ALTA == "VOLATILIDADE_ALTA"


def test_regime_in_tuple():
    permitidos = ("LATERAL", "TENDENCIA_ALTA")
    assert Regime.LATERAL.name in permitidos
    assert Regime.TENDENCIA_BAIXA.name not in permitidos


def test_todos_os_regimes_definidos():
    nomes = {r.name for r in Regime}
    assert nomes == {"LATERAL", "TENDENCIA_ALTA", "TENDENCIA_BAIXA", "VOLATILIDADE_ALTA", "INDEFINIDO"}
