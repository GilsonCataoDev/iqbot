"""A entrada de reversão tem de ser o PRIMEIRO minuto em que o setup arma.

O bot ao vivo reavalia o candle a cada poucos segundos e entra no primeiro
toque. Avaliar só no minuto N modelava a entrada mais tardia possível — para
reversão isso é pessimista, porque nesse ponto o movimento já reverteu e o
preço de entrada é o pior da vela.
"""
from __future__ import annotations

import inspect
from dataclasses import replace

import pandas as pd

import backtest_m15
from iqoption_m5.config import configuracao_scalping_h1, configuracao_scalping_m15


def test_janela_do_setup_usa_a_tabela_por_setup() -> None:
    config = configuracao_scalping_m15()
    assert backtest_m15._janela_do_setup(config, "sr_rejeicao") == 540
    assert backtest_m15._janela_do_setup(config, "pullback_confluencia") == 600


def test_janela_do_setup_cai_no_global_quando_setup_nao_esta_na_tabela() -> None:
    config = configuracao_scalping_m15()
    esperado = config.entrada_max_segundos_no_candle
    assert backtest_m15._janela_do_setup(config, "setup_inexistente") == esperado


def test_janela_do_setup_sem_tabela_usa_o_global() -> None:
    config = replace(configuracao_scalping_m15(), janela_entrada_por_setup=None)
    esperado = config.entrada_max_segundos_no_candle
    assert backtest_m15._janela_do_setup(config, "sr_rejeicao") == esperado


def test_h1_tem_janela_maior_que_m15() -> None:
    """H1 tem vela 4x maior, entao a janela intravela tambem e maior."""
    m15 = backtest_m15._janela_do_setup(configuracao_scalping_m15(), "sr_rejeicao")
    h1 = backtest_m15._janela_do_setup(configuracao_scalping_h1(), "sr_rejeicao")
    assert h1 > m15


def test_varredura_para_no_primeiro_minuto_que_arma() -> None:
    """`break` no primeiro achado — senao a entrada seria sempre a mais tardia."""
    fonte = inspect.getsource(backtest_m15.rodar_ativo)
    assert "for k in range(1, m1_minutos + 1)" in fonte
    assert "minuto_entrada = k" in fonte
    assert "break" in fonte


def test_cada_minuto_tem_chave_de_cache_propria() -> None:
    """Chave repetida faz o cache devolver o parcial errado ou o candle cheio.

    Ver tests_m5/test_backtest_cache_parcial.py para o mecanismo.
    """
    fonte = inspect.getsource(backtest_m15.rodar_ativo)
    assert 'f"{ativo}-bt-parcial-{k}"' in fonte


def test_varredura_respeita_a_janela_de_entrada() -> None:
    """Sinal fora da janela nao pode virar trade — ao vivo o risco bloqueia."""
    fonte = inspect.getsource(backtest_m15.rodar_ativo)
    assert "k * 60 <= _janela_do_setup(" in fonte


def test_parcial_de_cada_minuto_e_progressivo() -> None:
    """Base da varredura: o parcial de k minutos contem os k-1 anteriores."""
    inicio = pd.Timestamp("2026-08-25 08:00")
    idx = pd.date_range(inicio, periods=10, freq="1min")
    m1 = pd.DataFrame(
        {
            "Open": [1.1000 + i * 0.0001 for i in range(10)],
            "High": [1.1003 + i * 0.0001 for i in range(10)],
            "Low": [1.0998 + i * 0.0001 for i in range(10)],
            "Close": [1.1001 + i * 0.0001 for i in range(10)],
            "Volume": [5.0] * 10,
        },
        index=idx,
    )

    anterior = None
    for k in range(1, 10):
        cp = backtest_m15._candle_parcial_m1(m1, inicio, k)
        assert cp is not None
        # Open fixo, High/Low so podem alargar, Close acompanha o minuto k.
        assert cp["Open"] == 1.1000
        if anterior is not None:
            assert cp["High"] >= anterior["High"]
            assert cp["Low"] <= anterior["Low"]
            assert cp["Close"] > anterior["Close"]
        anterior = cp
