"""O backtest do forex_reteste_m15 tem de modelar a BINÁRIA que o bot executa.

O plano nasce de candles fechados (`candles.iloc[:-1]` no app), a entrada cai no
Open da vela seguinte e a saída é o Close dessa mesma vela. Não há candle
parcial aqui — é o oposto dos setups de reversão.
"""
from __future__ import annotations

import inspect

import pandas as pd

import backtest_forex_reteste as bfr
from iqoption_m5.forex_modelos import PlanoForex


def _candles(closes: list[float], opens: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-08-01", periods=len(closes), freq="15min")
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [max(o, c) + 0.0005 for o, c in zip(opens, closes)],
            "Low": [min(o, c) - 0.0005 for o, c in zip(opens, closes)],
            "Close": closes,
            "Volume": [100.0] * len(closes),
        },
        index=idx,
    )


def test_payout_por_par_bate_com_o_medido_na_iq() -> None:
    """0.85 na maioria, 0.87 em USDCAD/NZDUSD — medido em 27/08/2026."""
    assert bfr.PAYOUT["EURUSD"] == 0.85
    assert bfr.PAYOUT["USDCAD"] == 0.87
    assert bfr.PAYOUT["NZDUSD"] == 0.87
    # Breakeven de 0.85 e 54.05%, nao os 53.5% que o projeto usava antes.
    assert 1 / (1 + bfr.PAYOUT["EURUSD"]) > 0.54


def test_entrada_e_o_open_do_candle_seguinte_ao_sinal(monkeypatch) -> None:
    """O sinal fecha em i; a entrada e o Open de i+1, nunca o de i."""
    candles = _candles(
        closes=[1.1000] * 100 + [1.1050, 1.1080],
        opens=[1.1000] * 100 + [1.1040, 1.1060],
    )
    plano = PlanoForex(
        ativo="EURUSD", lado="buy",
        sinal_em=candles.index[100].to_pydatetime(),
        nivel=1.1040, stop=1.1020, alvo=1.1100,
        risco_preco=0.0020, motivo="teste",
    )
    # Um unico plano, no candle 100.
    serie = pd.Series([None] * len(candles), index=candles.index, dtype=object)
    serie.iloc[100] = plano
    monkeypatch.setattr(bfr, "planos_rompimento_reteste", lambda a, c, **k: serie)

    ops = bfr.rodar("EURUSD", candles)

    assert len(ops) == 1
    op = ops[0]
    # candle 101: Open 1.1060, Close 1.1080 -> compra ganha
    assert op["entrada"] == 1.1060
    assert op["saida"] == 1.1080
    assert op["res"] == "ganho"
    assert op["quando"] == candles.index[101]


def test_venda_ganha_quando_o_candle_fecha_abaixo(monkeypatch) -> None:
    candles = _candles(
        closes=[1.1000] * 100 + [1.1050, 1.1010],
        opens=[1.1000] * 100 + [1.1040, 1.1060],
    )
    plano = PlanoForex(
        ativo="EURUSD", lado="sell",
        sinal_em=candles.index[100].to_pydatetime(),
        nivel=1.1040, stop=1.1080, alvo=1.0980,
        risco_preco=0.0040, motivo="teste",
    )
    serie = pd.Series([None] * len(candles), index=candles.index, dtype=object)
    serie.iloc[100] = plano
    monkeypatch.setattr(bfr, "planos_rompimento_reteste", lambda a, c, **k: serie)

    ops = bfr.rodar("EURUSD", candles)

    assert ops[0]["res"] == "ganho"   # 1.1010 < 1.1060


def test_candle_sem_movimento_e_empate(monkeypatch) -> None:
    candles = _candles(
        closes=[1.1000] * 100 + [1.1050, 1.1060],
        opens=[1.1000] * 100 + [1.1040, 1.1060],   # Open == Close no candle 101
    )
    plano = PlanoForex(
        ativo="EURUSD", lado="buy",
        sinal_em=candles.index[100].to_pydatetime(),
        nivel=1.1040, stop=1.1020, alvo=1.1100,
        risco_preco=0.0020, motivo="teste",
    )
    serie = pd.Series([None] * len(candles), index=candles.index, dtype=object)
    serie.iloc[100] = plano
    monkeypatch.setattr(bfr, "planos_rompimento_reteste", lambda a, c, **k: serie)

    assert bfr.rodar("EURUSD", candles)[0]["res"] == "empate"


def test_existe_verificacao_de_lookahead() -> None:
    """A versao vetorizada promete 'so dados ate t'; isso tem de ser checado.

    Em 27/08/2026 um cache anulou o metodo anti-lookahead do backtest de
    reversao sem que nada acusasse — promessa em docstring nao basta.
    """
    fonte = inspect.getsource(bfr.verificar_lookahead)
    assert "plano_rompimento_reteste" in fonte      # versao pontual
    assert "planos_rompimento_reteste" in fonte     # versao vetorizada
    assert "divergencias" in fonte


def test_wilson_vem_da_sonda_sem_copia_paralela() -> None:
    import sonda_setup
    assert bfr.wilson is sonda_setup.wilson
