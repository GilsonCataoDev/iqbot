"""Regressao: o resultado deve ser apurado no candle de EXPIRACAO.

Bug encontrado em 25/08/2026: o executor resolvia contra decisao.candle_hora,
que e o candle de CONFIRMACAO (indice -2) e ja fechou quando a ordem sai.
Isso media a direcao do proprio candle que gerou o sinal — o bot se avaliava
sobre o proprio input. No M15 ao vivo, essa regra reproduzia 48/49 resultados
gravados, inflando o WR para 67% quando a expiracao real dava ~31%.
"""
import pandas as pd

from iqoption_m5.config import configuracao_scalping_m1, configuracao_scalping_m15


def _candle_expiracao(config, candle_hora, expiracao_min):
    """Mesma formula usada em ExecutorSeguro.executar."""
    n = max(1, round(expiracao_min * 60 / config.timeframe_segundos))
    return pd.Timestamp(candle_hora) + pd.Timedelta(seconds=n * config.timeframe_segundos)


def test_m15_expiracao_15min_avanca_um_candle():
    c = configuracao_scalping_m15()
    x = pd.Timestamp("2026-08-20 12:00")
    assert _candle_expiracao(c, x, 15) == pd.Timestamp("2026-08-20 12:15")


def test_m15_expiracao_30min_avanca_dois_candles():
    c = configuracao_scalping_m15()
    x = pd.Timestamp("2026-08-20 12:00")
    assert _candle_expiracao(c, x, 30) == pd.Timestamp("2026-08-20 12:30")


def test_m1_expiracao_2min_avanca_dois_candles():
    c = configuracao_scalping_m1()
    x = pd.Timestamp("2026-08-20 12:00")
    assert _candle_expiracao(c, x, 2) == pd.Timestamp("2026-08-20 12:02")


def test_nunca_resolve_no_proprio_candle_de_confirmacao():
    """O candle de expiracao tem de ser estritamente posterior a confirmacao."""
    x = pd.Timestamp("2026-08-20 12:00")
    for cfg, exp in [(configuracao_scalping_m15(), 15),
                     (configuracao_scalping_m15(), 30),
                     (configuracao_scalping_m1(), 2)]:
        assert _candle_expiracao(cfg, x, exp) > x
