"""Testes determinísticos para iqoption_m5/timing.py.

Nenhum teste depende de horário real, rede ou IQ Option.
"""
import time
from unittest.mock import patch

import pytest

from iqoption_m5.timing import (
    MOTIVO_MERCADO_FECHADO,
    MOTIVO_ORDEM_PENDENTE,
    MOTIVO_PAYOUT_BAIXO,
    MOTIVO_PRECO_AFASTADO,
    MOTIVO_SINAL_EXPIRADO,
    LatenciaSinal,
    calcular_fechamento_candle,
    candle_esta_fechado,
    gerar_signal_id,
    obter_offset_servidor,
    segundo_no_candle,
    sinal_ainda_valido,
    validar_sinal_pre_envio,
)


# ---------------------------------------------------------------------------
# calcular_fechamento_candle
# ---------------------------------------------------------------------------

def test_fechamento_candle_m5_inicio():
    """ts no início do candle → próximo fechamento em exatamente timeframe segundos."""
    # ts=1000, timeframe=300 → 1000 % 300 == 100 → próximo mark = 1000 + (300-100) = 1200
    assert calcular_fechamento_candle(1000, 300) == 1200


def test_fechamento_candle_m5_no_mark():
    """ts exatamente num mark → próximo mark é em timeframe segundos."""
    # ts=1200, timeframe=300 → 1200 % 300 == 0 → próximo mark = 1200 + 300 = 1500
    assert calcular_fechamento_candle(1200, 300) == 1500


def test_fechamento_candle_m1():
    # ts=125, timeframe=60 → 125 % 60 == 5 → próximo mark = 125 + 55 = 180
    assert calcular_fechamento_candle(125, 60) == 180


# ---------------------------------------------------------------------------
# segundo_no_candle
# ---------------------------------------------------------------------------

def test_segundo_no_candle_zero():
    assert segundo_no_candle(300, 300) == 0


def test_segundo_no_candle_meio():
    assert segundo_no_candle(450, 300) == 150


def test_segundo_no_candle_fim():
    assert segundo_no_candle(599, 300) == 299


# ---------------------------------------------------------------------------
# candle_esta_fechado
# ---------------------------------------------------------------------------

def test_candle_fechado_quando_passou():
    # candle abriu em ts=1000, timeframe=300 → fechou em ts=1300
    # ts_servidor=1301 → já fechou
    assert candle_esta_fechado(1000, 1301, 300) is True


def test_candle_nao_fechado_durante():
    # ts_servidor=1299 → ainda em formação
    assert candle_esta_fechado(1000, 1299, 300) is False


def test_candle_fechado_exatamente_no_mark():
    # ts_servidor=1300 → fechou exatamente agora
    assert candle_esta_fechado(1000, 1300, 300) is True


# ---------------------------------------------------------------------------
# obter_offset_servidor
# ---------------------------------------------------------------------------

def test_offset_servidor_positivo():
    # servidor 2s à frente do local
    assert obter_offset_servidor(1000.0, 1002) == pytest.approx(2.0)


def test_offset_servidor_negativo():
    assert obter_offset_servidor(1005.0, 1003) == pytest.approx(-2.0)


def test_offset_servidor_zero():
    assert obter_offset_servidor(1000.0, 1000) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# sinal_ainda_valido
# ---------------------------------------------------------------------------

def test_sinal_valido_recente():
    ts = time.monotonic()
    assert sinal_ainda_valido(ts, max_age_segundos=30) is True


def test_sinal_expirado():
    ts = time.monotonic() - 31  # 31 segundos atrás
    assert sinal_ainda_valido(ts, max_age_segundos=30) is False


def test_sinal_exatamente_no_limite():
    # No exato limite não é mais válido (usa <, não <=)
    ts = time.monotonic() - 30.001
    assert sinal_ainda_valido(ts, max_age_segundos=30) is False


# ---------------------------------------------------------------------------
# gerar_signal_id
# ---------------------------------------------------------------------------

def test_signal_ids_unicos():
    ids = {gerar_signal_id() for _ in range(100)}
    assert len(ids) == 100


def test_signal_id_formato_uuid():
    sid = gerar_signal_id()
    import uuid
    # Deve ser UUID v4 válido
    parsed = uuid.UUID(sid)
    assert parsed.version == 4


# ---------------------------------------------------------------------------
# LatenciaSinal
# ---------------------------------------------------------------------------

def test_latencia_calculo():
    lat = LatenciaSinal(ativo="EURUSD", setup="macd_crossover")
    lat.ts_inicio_calculo = 1000.0
    lat.ts_fim_calculo = 1000.050  # 50ms
    lat.finalizar_calculo()
    assert lat.latencia_calculo_ms == pytest.approx(50.0, abs=0.1)


def test_latencia_envio():
    lat = LatenciaSinal(
        ativo="EURUSD", setup="sr_rejeicao",
        ts_fechamento_esperado=1000,
        ts_recebimento_candle=1000.8,   # 800ms após fechamento esperado
        ts_fim_calculo=1001.0,
    )
    lat.ts_envio_ordem = 1001.2
    lat.finalizar_envio()
    assert lat.latencia_recebimento_ms == pytest.approx(800.0, abs=1.0)
    assert lat.latencia_envio_ms == pytest.approx(200.0, abs=1.0)


def test_latencia_confirmacao():
    lat = LatenciaSinal(
        ts_fechamento_esperado=1000,
        ts_envio_ordem=1001.5,
    )
    lat.ts_confirmacao_ordem = 1002.0
    lat.finalizar_confirmacao()
    assert lat.latencia_confirmacao_ms == pytest.approx(500.0, abs=1.0)
    assert lat.idade_sinal_ms == pytest.approx(2000.0, abs=1.0)


def test_latencia_para_dict_contem_campos():
    lat = LatenciaSinal(ativo="GBPUSD", setup="pullback_confluencia", timeframe=300)
    d = lat.para_dict()
    campos_obrigatorios = {
        "signal_id", "ativo", "setup", "timeframe",
        "latencia_calculo_ms", "latencia_envio_ms", "offset_servidor_s",
    }
    assert campos_obrigatorios.issubset(d.keys())


def test_resumo_legivel():
    lat = LatenciaSinal(ativo="USDJPY", setup="fibo_sr_retracao")
    lat.ts_inicio_calculo = 0.0
    lat.ts_fim_calculo = 0.012
    lat.ts_envio_ordem = 0.015
    lat.finalizar_calculo()
    lat.finalizar_envio()
    resumo = lat.resumo()
    assert "USDJPY" in resumo
    assert "fibo_sr_retracao" in resumo
    assert "calc=" in resumo


# ---------------------------------------------------------------------------
# validar_sinal_pre_envio
# ---------------------------------------------------------------------------

def _params_validos(**override):
    base = dict(
        ts_sinal_monotonic=time.monotonic(),
        max_age=30.0,
        preco_sinal=1.08500,
        preco_atual=1.08501,
        max_desvio_pips=5.0,
        payout=0.87,
        payout_minimo=0.75,
        mercado_aberto=True,
        ordem_pendente=False,
    )
    base.update(override)
    return base


def test_sinal_valido_passa():
    valido, motivo = validar_sinal_pre_envio(**_params_validos())
    assert valido is True
    assert motivo == "ok"


def test_sinal_expirado_bloqueado():
    valido, motivo = validar_sinal_pre_envio(
        **_params_validos(ts_sinal_monotonic=time.monotonic() - 31)
    )
    assert valido is False
    assert motivo == MOTIVO_SINAL_EXPIRADO


def test_mercado_fechado_bloqueado():
    valido, motivo = validar_sinal_pre_envio(**_params_validos(mercado_aberto=False))
    assert valido is False
    assert motivo == MOTIVO_MERCADO_FECHADO


def test_ordem_pendente_bloqueada():
    valido, motivo = validar_sinal_pre_envio(**_params_validos(ordem_pendente=True))
    assert valido is False
    assert motivo == MOTIVO_ORDEM_PENDENTE


def test_payout_baixo_bloqueado():
    valido, motivo = validar_sinal_pre_envio(**_params_validos(payout=0.70))
    assert valido is False
    assert motivo == MOTIVO_PAYOUT_BAIXO


def test_payout_none_bloqueado():
    valido, motivo = validar_sinal_pre_envio(**_params_validos(payout=None))
    assert valido is False
    assert motivo == MOTIVO_PAYOUT_BAIXO


def test_preco_afastado_bloqueado():
    # Desvio de 10 pips, limite 5
    valido, motivo = validar_sinal_pre_envio(
        **_params_validos(preco_sinal=1.08500, preco_atual=1.08510)
    )
    assert valido is False
    assert motivo == MOTIVO_PRECO_AFASTADO


def test_preco_dentro_limite_passa():
    # Desvio de 2 pips, limite 5
    valido, motivo = validar_sinal_pre_envio(
        **_params_validos(preco_sinal=1.08500, preco_atual=1.08502)
    )
    assert valido is True


def test_prioridade_expiracao_sobre_mercado():
    """Sinal expirado deve ser reportado antes de mercado fechado."""
    valido, motivo = validar_sinal_pre_envio(
        **_params_validos(
            ts_sinal_monotonic=time.monotonic() - 31,
            mercado_aberto=False,
        )
    )
    assert motivo == MOTIVO_SINAL_EXPIRADO
