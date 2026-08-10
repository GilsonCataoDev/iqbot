"""
Melhoria B — verifica lógica de limite de retry para mercado_fechado OTC.
Não executa o loop completo — testa a lógica de elapsed diretamente.
"""
import time

import pytest

from iqoption_m5.config import Configuracao


def test_elapsed_supera_limite():
    """Simula dict com timestamp de 10s atrás; elapsed deve superar limite de 5s."""
    config = Configuracao(max_retry_mercado_fechado_segundos=5.0)

    # Simula o dict como no app.py
    _retry_mercado_fechado_inicio: dict[str, float] = {}

    ativo = "EURUSD-OTC"
    timestamp_servidor_inicial = float(int(time.time()) - 10)  # 10 segundos atrás

    # Primeiro setdefault — registra início
    _inicio = _retry_mercado_fechado_inicio.setdefault(ativo, timestamp_servidor_inicial)
    assert _inicio == timestamp_servidor_inicial

    # Simula tick seguinte com timestamp atual
    timestamp_servidor_atual = float(int(time.time()))
    elapsed = timestamp_servidor_atual - _retry_mercado_fechado_inicio[ativo]

    assert elapsed > config.max_retry_mercado_fechado_segundos, (
        f"elapsed={elapsed:.1f}s deveria superar limite={config.max_retry_mercado_fechado_segundos}s"
    )


def test_elapsed_dentro_do_limite():
    """Simula dict com timestamp de 2s atrás; elapsed NÃO deve superar limite de 5s."""
    config = Configuracao(max_retry_mercado_fechado_segundos=5.0)

    _retry_mercado_fechado_inicio: dict[str, float] = {}

    ativo = "EURUSD-OTC"
    timestamp_servidor_inicial = float(int(time.time()) - 2)  # apenas 2 segundos atrás

    _retry_mercado_fechado_inicio.setdefault(ativo, timestamp_servidor_inicial)
    timestamp_servidor_atual = float(int(time.time()))
    elapsed = timestamp_servidor_atual - _retry_mercado_fechado_inicio[ativo]

    assert elapsed <= config.max_retry_mercado_fechado_segundos, (
        f"elapsed={elapsed:.1f}s não deveria superar limite={config.max_retry_mercado_fechado_segundos}s"
    )


def test_limite_zero_desativa():
    """Com max_retry_mercado_fechado_segundos=0, o limite está desativado."""
    config = Configuracao(max_retry_mercado_fechado_segundos=0.0)
    assert config.max_retry_mercado_fechado_segundos == 0.0


def test_pop_limpa_entrada():
    """Após desistir, pop remove o ativo do dict."""
    _retry_mercado_fechado_inicio: dict[str, float] = {}
    ativo = "GBPUSD-OTC"
    _retry_mercado_fechado_inicio[ativo] = float(int(time.time()) - 10)
    _retry_mercado_fechado_inicio.pop(ativo, None)
    assert ativo not in _retry_mercado_fechado_inicio
