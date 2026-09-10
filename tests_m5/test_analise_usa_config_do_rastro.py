"""Os scripts de análise têm de medir a estratégia que está no ar.

Ambos partiam de ``configuracao_scalping_m15``, que arrasta a parametrização
inteira do scalping — inclusive ``ema_micro_periodo=21`` num setup chamado
EMA 9/20. O backtest devolvia 47,7% para algo que ao vivo dá 61%, e a
divergência não aparecia em lugar nenhum: os dois números eram plausíveis.
"""
import pytest

from iqoption_m5.config import configuracao_ema_laboratorio_practice
from iqoption_m5.laboratorio_ema import _config_rastro

from analisar_hora_ema920 import _config_ema920 as config_hora
from analisar_expiracao_ema920 import _config_ema920 as config_expiracao


def _rastro_m5():
    return _config_rastro(configuracao_ema_laboratorio_practice(), 300, "ema920_pullback")


@pytest.mark.parametrize("config_analise", [config_hora, config_expiracao],
                         ids=["analisar_hora", "analisar_expiracao"])
def test_analise_mede_a_config_do_rastro_que_opera(config_analise):
    assert config_analise().configuracao_auditavel() == _rastro_m5().configuracao_auditavel()


@pytest.mark.parametrize("config_analise", [config_hora, config_expiracao],
                         ids=["analisar_hora", "analisar_expiracao"])
def test_ema920_usa_media_de_20_e_nao_21(config_analise):
    """O nome do setup é EMA 9/20; medir com 21 é medir outro indicador."""
    assert config_analise().ema_micro_periodo == 20


def _candles_sinteticos(n=400):
    """Serra com tendência: gera pullback suficiente para o setup disparar."""
    import numpy as np
    import pandas as pd
    t = np.arange(n)
    base = 1.10 + t * 0.00004 + np.sin(t / 7.0) * 0.0012
    return pd.DataFrame(
        {"Open": base, "High": base + 0.0004, "Low": base - 0.0004,
         "Close": base + np.sin(t / 3.0) * 0.0002, "Volume": 1.0},
        index=pd.date_range("2026-08-01", periods=n, freq="5min"),
    )


def test_backtest_e_execucao_ao_vivo_geram_os_mesmos_sinais():
    """sinais_historicos varre o histórico; avaliar_todas roda no candle atual.

    São caminhos distintos para o mesmo setup. Se divergirem, o backtest passa
    a medir uma estratégia que o bot não executa — e nada no resultado denuncia
    isso, porque os dois números continuam plausíveis.
    """
    from iqoption_m5.estrategia import EstrategiaReversaoM5

    cfg = _rastro_m5()
    df = _candles_sinteticos()
    est = EstrategiaReversaoM5(cfg)

    hist = {d.candle_hora: d.direcao for d in est.sinais_historicos("EURUSD", df)}

    vivo = {}
    inicio = max(cfg.ema_macro_periodo, cfg.atr_regime_janela) + 3
    for i in range(inicio, len(df) - 1):
        indicadores = est.calcular_indicadores(df.iloc[:i + 2], "EURUSD")
        for d in est.avaliar_todas("EURUSD", indicadores):
            if d.detalhes.get("setup") == "ema920_pullback":
                vivo[d.candle_hora] = d.direcao

    assert hist, "amostra sintética não gerou sinal; o teste perderia o sentido"
    assert hist == vivo
