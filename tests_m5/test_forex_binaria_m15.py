from datetime import datetime

from iqoption_m5.app import _alerta_plano_forex, _decisao_plano_forex
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_modelos import PlanoForex


def _plano() -> PlanoForex:
    return PlanoForex(
        ativo="EURUSD",
        lado="buy",
        sinal_em=datetime(2026, 1, 1, 12, 0),
        nivel=1.1000,
        stop=1.0980,
        alvo=1.1040,
        risco_preco=0.0020,
        motivo="rompimento_reteste_tendencia",
    )


def test_m15_liga_forex_reteste_e_desliga_reversoes_fracas() -> None:
    config = configuracao_scalping_m15()

    assert config.forex_reteste_m15_ativo
    assert not config.sr_rejeicao_ativo
    assert not config.pin_bar_sr_ativo
    assert not config.pullback_confluencia_ativo
    assert not config.breakout_reteste_ativo


def test_plano_forex_vira_alerta_com_sl_tp_visual() -> None:
    alerta = _alerta_plano_forex(_plano(), 1.1010, lambda ts: 123)

    assert alerta["direcao"] == "call"
    assert alerta["setup"] == "forex_reteste_m15"
    assert alerta["entradaConfirmada"] is True
    assert alerta["precoEntrada"] == 1.1010
    assert alerta["sl"] == 1.0980
    assert alerta["tp"] == 1.1040


def test_plano_forex_vira_decisao_binaria() -> None:
    decisao = _decisao_plano_forex(_plano(), 1.1010)

    assert decisao.direcao == "call"
    assert decisao.preco == 1.1010
    assert decisao.detalhes["setup"] == "forex_reteste_m15"
    assert decisao.detalhes["tp"] == 1.1040
