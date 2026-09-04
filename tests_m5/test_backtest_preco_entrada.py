"""Setup de reversão não pode ser preenchido a um preço anterior ao sinal.

O sinal de reversão é detectado sobre o candle PARCIAL (primeiros N minutos),
então só existe no minuto N. Preencher no Open do candle usa um preço de N
minutos antes — lookahead no preço de entrada, não só no sinal.
"""
from __future__ import annotations

import pandas as pd

import backtest_m15
from iqoption_m5.config import configuracao_scalping_h1, configuracao_scalping_m15


def test_sentinela_zero_vira_um_candle_no_backtest() -> None:
    """expiracao_por_setup=0 (fim da vela) = sair no Close do candle de entrada.

    No backtest isso é 1 candle: i_saida = (i-1) + 1 = i.
    """
    for config in (configuracao_scalping_m15(), configuracao_scalping_h1()):
        assert (config.expiracao_por_setup or {}).get("pullback_confluencia") == 0
        assert backtest_m15._expiracao_candles(config, "pullback_confluencia") == 1


def test_expiracao_em_minutos_continua_convertendo_normal() -> None:
    config = configuracao_scalping_m15()
    # sr_rejeicao 15min num TF de 15min = 1 candle.
    assert backtest_m15._expiracao_candles(config, "sr_rejeicao") == 1
    # pin_bar_sr 30min = 2 candles.
    assert backtest_m15._expiracao_candles(config, "pin_bar_sr") == 2


def test_pullback_confluencia_e_reversao_no_backtest() -> None:
    """Pullback+Fib também nasce intravela; não pode preencher no Open."""
    assert "pullback_confluencia" in backtest_m15.SETUPS_REVERSAO


def test_flag_preco_entrada_existe_e_o_padrao_e_toque() -> None:
    """O padrão tem de ser o modelo honesto; 'open' só pra reproduzir histórico."""
    import argparse
    import contextlib
    import io

    # Reconstrói o parser executando main() com --help capturado.
    ajuda = io.StringIO()
    with contextlib.redirect_stdout(ajuda):
        with contextlib.suppress(SystemExit):
            import sys
            argv = sys.argv
            sys.argv = ["backtest_m15.py", "--help"]
            try:
                backtest_m15.main()
            finally:
                sys.argv = argv
    texto = ajuda.getvalue()
    assert "--preco-entrada" in texto
    assert "toque" in texto


def test_candle_parcial_de_9min_difere_da_abertura() -> None:
    """Se o parcial fosse igual ao Open, a correção não mudaria nada.

    Constrói M1 com tendência: o Close do minuto 9 está longe do Open.
    """
    inicio = pd.Timestamp("2026-08-25 08:00")
    idx = pd.date_range(inicio, periods=10, freq="1min")
    m1 = pd.DataFrame(
        {
            "Open": [1.1000 + i * 0.0002 for i in range(10)],
            "High": [1.1002 + i * 0.0002 for i in range(10)],
            "Low": [1.0999 + i * 0.0002 for i in range(10)],
            "Close": [1.1001 + i * 0.0002 for i in range(10)],
            "Volume": [10] * 10,
        },
        index=idx,
    )

    parcial_1 = backtest_m15._candle_parcial_m1(m1, inicio, 1)
    parcial_9 = backtest_m15._candle_parcial_m1(m1, inicio, 9)

    assert parcial_1 is not None and parcial_9 is not None
    # O Open do candle é 1.1000; no minuto 9 o preço já andou ~16 pips.
    assert parcial_9["Close"] > parcial_1["Close"] > parcial_9["Open"]
    assert abs(parcial_9["Close"] - parcial_9["Open"]) > 0.0010
