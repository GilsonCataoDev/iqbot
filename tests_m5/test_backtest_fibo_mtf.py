import pandas as pd

from backtest_fibo_mtf import _confirmacao, resumo


def test_confirmacao_rejeicao_e_engolfo():
    anterior = pd.Series({"Open": 10.0, "High": 10.2, "Low": 9.8, "Close": 9.9})
    bull = pd.Series({"Open": 9.8, "High": 10.4, "Low": 9.0, "Close": 10.3})
    bear = pd.Series({"Open": 10.2, "High": 11.1, "Low": 9.7, "Close": 9.7})
    assert _confirmacao(bull, anterior, "CALL")
    assert _confirmacao(bear, anterior, "PUT")


def test_resumo_vazio_nao_divide_por_zero():
    r = resumo([])
    assert r["n"] == 0
    assert r["wr"] == 0.0
