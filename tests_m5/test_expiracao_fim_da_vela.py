"""Setup intravela tem de expirar no fechamento da PRÓPRIA vela.

A IQ escolhe, entre os marcos :00/:15/:30/:45, o vencimento mais PRÓXIMO dos
minutos pedidos — não o próximo marco. Com minutos fixos isso joga a entrada
intravela pra 2-3 velas adiante, que é o oposto do que a estratégia quer.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import iqoptionapi.expiration as expiration_iq

from iqoption_m5.config import configuracao_scalping_h1, configuracao_scalping_m15
from iqoption_m5.executor import ExecutorSeguro
from iqoption_m5.mercado_iq import SnapshotMercado


VELA = int(datetime(2026, 8, 27, 14, 0, 0, tzinfo=timezone.utc).timestamp())


def _executor(config):
    return ExecutorSeguro.__new__(ExecutorSeguro)


def _snapshot(ts: int) -> SnapshotMercado:
    snap = SnapshotMercado.__new__(SnapshotMercado)
    object.__setattr__(snap, "timestamp_servidor", ts)
    return snap


class _Decisao:
    def __init__(self, setup: str) -> None:
        self.detalhes = {"setup": setup}


def _minutos(config, setup: str, segundo_na_vela: int) -> int:
    ex = _executor(config)
    ex.config = config
    return ExecutorSeguro._expiracao_dinamica(ex, _snapshot(VELA + segundo_na_vela), _Decisao(setup))


def _expira_em(minutos: int, ts: int) -> int:
    """Vencimento que a IQ escolheria. Fixa o relógio: get_expiration_time
    mistura o timestamp recebido com time.time() real."""
    with patch.object(expiration_iq.time, "time", lambda: ts):
        exp, _ = expiration_iq.get_expiration_time(ts, minutos)
    return exp


def test_pullback_intravela_expira_no_fim_da_propria_vela() -> None:
    config = configuracao_scalping_m15()
    fim_da_vela = VELA + 900
    for segundo in (5, 60, 300, 480, 540):
        minutos = _minutos(config, "pullback_confluencia", segundo)
        exp = _expira_em(minutos, VELA + segundo)
        assert exp == fim_da_vela, (
            f"entrada a {segundo}s pediu {minutos}min e expirou "
            f"{datetime.fromtimestamp(exp, timezone.utc):%H:%M}, "
            f"não no fechamento 14:15"
        )


def test_minutos_fixos_furariam_a_vela_no_fim_da_janela() -> None:
    """Prova de que o valor fixo antigo (30min) não serve: aos 9min ele expira
    3 velas depois. É o motivo do sentinela 0 existir."""
    ts = VELA + 540  # 9min dentro da vela, limite da janela de entrada
    assert _expira_em(30, ts) == VELA + 2700          # 14:45, 3 velas adiante
    assert _expira_em(15, ts) == VELA + 1800          # 14:30, ainda fora
    # Só o cálculo dinâmico acerta o fechamento da própria vela.
    assert _expira_em(6, ts) == VELA + 900            # 14:15


def test_pullback_intravela_h1_expira_no_fim_da_propria_vela() -> None:
    config = configuracao_scalping_h1()
    fim_da_vela = VELA + 3600
    # Janela do H1 vai ate 40min (2400s) dentro da vela.
    for segundo in (5, 600, 1200, 1800, 2400):
        minutos = _minutos(config, "pullback_confluencia", segundo)
        exp = _expira_em(minutos, VELA + segundo)
        assert exp == fim_da_vela, (
            f"entrada a {segundo}s pediu {minutos}min e expirou "
            f"{datetime.fromtimestamp(exp, timezone.utc):%H:%M}, nao em 15:00"
        )


def test_minutos_fixos_furariam_a_vela_h1() -> None:
    """Os 120min fixos antigos: entrada aos 40min expirava quase 3 velas
    depois, em 16:45."""
    ts = VELA + 2400                                # 14:40
    assert _expira_em(120, ts) == VELA + 165 * 60   # 16:45
    assert _expira_em(20, ts) == VELA + 3600        # 15:00, o dinamico acerta


def test_sentinela_zero_nao_afeta_setup_de_minutos_fixos() -> None:
    config = configuracao_scalping_m15()
    # sr_rejeicao continua com os 15min fixos dele, entre na vela onde entrar.
    assert _minutos(config, "sr_rejeicao", 5) == 15
    assert _minutos(config, "sr_rejeicao", 540) == 15


def test_fim_da_vela_ignora_teto_de_expiracao_minutos() -> None:
    """O teto existe pro caso genérico; no modo fim-da-vela ele só atrapalha,
    porque o alvo já cabe num timeframe por construção."""
    config = replace(configuracao_scalping_m15(), expiracao_minutos=5)
    assert _minutos(config, "pullback_confluencia", 5) == 15
