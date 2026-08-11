"""Testes para iqoption_m5/backtest_candle.py — sem IQ Option, sem rede."""
import math
import numpy as np
import pandas as pd
import pytest

from iqoption_m5.config import Configuracao
from iqoption_m5.backtest_candle import (
    simular_sobre_candles,
    comparar_configs,
    imprimir_comparacao_configs,
    _resumo_geral,
    _avaliar,
)
from iqoption_m5.backtest import Operacao, para_dataframe
from iqoption_m5.modelos import Decisao


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candles_sinteticos(n: int = 80, tendencia: str = "lateral") -> pd.DataFrame:
    """Série OHLC simples suficiente para calcular_indicadores gerar sinais."""
    idx = pd.date_range("2024-01-01", periods=n, freq="5min")
    base = 1.0800
    closes = np.full(n, base, dtype=float)

    if tendencia == "alta":
        closes = np.array([base + i * 0.0001 for i in range(n)])
    elif tendencia == "baixa":
        closes = np.array([base - i * 0.0001 for i in range(n)])

    opens  = closes - 0.0002
    highs  = closes + 0.0003
    lows   = closes - 0.0003

    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes},
        index=idx,
    )


def _op(resultado: str, payout: float = 0.82) -> Operacao:
    preco_entrada = 1.0800
    if resultado == "ganho":
        preco_saida = preco_entrada + 0.0010
        direcao = "call"
    elif resultado == "perda":
        preco_saida = preco_entrada - 0.0010
        direcao = "call"
    else:
        preco_saida = preco_entrada
        direcao = "call"
    return Operacao(
        ativo="EURUSD", direcao=direcao, setup="sr_rejeicao", fatores="-",
        hora_sinal=pd.Timestamp("2024-01-01 10:00"),
        hora_entrada=pd.Timestamp("2024-01-01 10:05"),
        preco_entrada=preco_entrada,
        preco_saida=preco_saida,
        resultado=resultado,
    )


# ---------------------------------------------------------------------------
# _resumo_geral
# ---------------------------------------------------------------------------

class TestResumoGeral:
    def test_vazio_retorna_zeros(self):
        r = _resumo_geral(pd.DataFrame(), 0.82)
        assert r["ops"] == 0
        assert r["wr"] == 0.0
        assert r["lucro"] == 0.0

    def test_todos_ganhos(self):
        ops = [_op("ganho")] * 10
        df = para_dataframe(ops)
        r = _resumo_geral(df, 0.82)
        assert r["wins"] == 10
        assert r["losses"] == 0
        assert math.isclose(r["wr"], 1.0)
        assert math.isclose(r["lucro"], 10 * 0.82)

    def test_todos_perdas(self):
        ops = [_op("perda")] * 10
        df = para_dataframe(ops)
        r = _resumo_geral(df, 0.82)
        assert r["wins"] == 0
        assert r["losses"] == 10
        assert math.isclose(r["lucro"], -10.0)

    def test_empates_excluidos_do_wr(self):
        ops = [_op("ganho")] * 5 + [_op("empate")] * 10
        df = para_dataframe(ops)
        r = _resumo_geral(df, 0.82)
        assert r["ops"] == 15
        assert r["wins"] == 5
        assert r["losses"] == 0
        assert math.isclose(r["wr"], 1.0)

    def test_ic95_dentro_de_limites(self):
        ops = [_op("ganho")] * 6 + [_op("perda")] * 4
        df = para_dataframe(ops)
        r = _resumo_geral(df, 0.82)
        assert 0.0 <= r["ic95_min"] <= r["wr"]
        assert r["wr"] <= r["ic95_max"] <= 1.0


# ---------------------------------------------------------------------------
# simular_sobre_candles — comportamento básico
# ---------------------------------------------------------------------------

class TestSimularSobreCandles:
    def test_serie_curta_retorna_lista_vazia(self):
        df = _candles_sinteticos(n=5)
        config = Configuracao()
        resultado = simular_sobre_candles(df, "EURUSD", config, 0.82)
        assert resultado == []

    def test_retorna_lista(self):
        """Com candles suficientes, retorna lista (possivelmente vazia se sem sinal)."""
        df = _candles_sinteticos(n=80)
        config = Configuracao()
        resultado = simular_sobre_candles(df, "EURUSD", config, 0.82)
        assert isinstance(resultado, list)

    def test_operacoes_tem_resultado_valido(self):
        df = _candles_sinteticos(n=120)
        config = Configuracao()
        resultado = simular_sobre_candles(df, "EURUSD", config, 0.82)
        for op in resultado:
            assert op.resultado in ("ganho", "perda", "empate")
            assert op.direcao in ("call", "put")


# ---------------------------------------------------------------------------
# comparar_configs
# ---------------------------------------------------------------------------

class TestCompararConfigs:
    def test_base_filtros_mesma_config_mesmos_resultados(self):
        """Com config idêntica, base e filtros devem dar o mesmo número de ops."""
        df = _candles_sinteticos(n=120)
        config = Configuracao()
        ops_b, ops_f = comparar_configs({"EURUSD": df}, config, config, 0.82)
        assert len(ops_b) == len(ops_f)

    def test_filtros_restritivos_reduzem_ops(self):
        """Config com filtros extremamente restritivos deve gerar <= ops da base."""
        df = _candles_sinteticos(n=120)
        config_base = Configuracao()
        config_filtros = Configuracao(
            sr_rejeicao_rsi_filtro=True,
            sr_rejeicao_corpo_min_atr=99.0,  # impossível de satisfazer
        )
        ops_b, ops_f = comparar_configs({"EURUSD": df}, config_base, config_filtros, 0.82)
        assert len(ops_f) <= len(ops_b)

    def test_multiplos_ativos(self):
        candles = {
            "EURUSD": _candles_sinteticos(n=120),
            "GBPUSD": _candles_sinteticos(n=120, tendencia="alta"),
        }
        config = Configuracao()
        ops_b, ops_f = comparar_configs(candles, config, config, 0.82)
        assert isinstance(ops_b, list)
        assert isinstance(ops_f, list)


# ---------------------------------------------------------------------------
# imprimir_comparacao_configs — apenas smoke test (não deve levantar)
# ---------------------------------------------------------------------------

class TestImprimirComparacao:
    def test_sem_operacoes_nao_levanta(self, capsys):
        imprimir_comparacao_configs([], [], 0.82, detalhar=False)
        out = capsys.readouterr().out
        assert "BACKTEST" in out

    def test_com_operacoes_imprime_veredicto(self, capsys):
        ops = [_op("ganho")] * 12 + [_op("perda")] * 8
        imprimir_comparacao_configs(ops, ops, 0.82, detalhar=False)
        out = capsys.readouterr().out
        assert "Win rate" in out or "WR" in out
