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
