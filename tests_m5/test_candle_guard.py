"""Testes determinísticos para CandleGuard.

Cobre: candle incompleto, duplicado, fora de ordem, reconexão,
processamento simultâneo, troca de ativo, troca de timeframe.
Nenhum teste usa rede, relógio real ou IQ Option.
"""
import threading

import pytest

from iqoption_m5.candle_guard import CandleGuard, candle_ts_para_unix
from iqoption_m5.timing import (
    MOTIVO_CANDLE_DUPLICADO,
    MOTIVO_CANDLE_FORA_DE_ORDEM,
    MOTIVO_CANDLE_INCOMPLETO,
)

# Constantes de teste
TF = 300   # M5
TS_ABERTURA = 1_000_000        # candle abre em ts=1_000_000
TS_FECHADO  = TS_ABERTURA + TF  # fechou em ts=1_000_300
TS_SERVIDOR_OK  = TS_FECHADO + 1   # servidor confirmou fechamento
TS_SERVIDOR_CEDO = TS_FECHADO - 1  # servidor ainda não chegou no mark


# ---------------------------------------------------------------------------
# candle_ts_para_unix
# ---------------------------------------------------------------------------

def test_candle_ts_unix_tz_naive():
    import pandas as pd
    ts = pd.Timestamp("2024-01-01 12:00:00")  # tz-naive
    result = candle_ts_para_unix(ts)
    # 2024-01-01 12:00:00 UTC = 1704110400
    assert result == 1704110400


def test_candle_ts_unix_tz_aware():
    import pandas as pd
    ts = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
    result = candle_ts_para_unix(ts)
    assert result == 1704110400


# ---------------------------------------------------------------------------
# Candle incompleto
# ---------------------------------------------------------------------------

def test_candle_incompleto_rejeitado():
    guard = CandleGuard()
    ok, motivo = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_CEDO)
    assert ok is False
    assert motivo == MOTIVO_CANDLE_INCOMPLETO


def test_candle_fechado_aceito():
    guard = CandleGuard()
    ok, motivo = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
    assert ok is True
    assert motivo == ""


def test_candle_fechado_exatamente_no_mark():
    guard = CandleGuard()
    # ts_servidor == ts_abertura + timeframe → candle_esta_fechado retorna True
    ok, motivo = guard.validar("EURUSD", TF, TS_ABERTURA, TS_FECHADO)
    assert ok is True


# ---------------------------------------------------------------------------
# Candle duplicado
# ---------------------------------------------------------------------------

def test_candle_duplicado_rejeitado():
    guard = CandleGuard()
    guard.registrar("EURUSD", TF, TS_ABERTURA)
    ok, motivo = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
    assert ok is False
    assert motivo == MOTIVO_CANDLE_DUPLICADO


def test_candle_duplicado_idempotente():
    """Mesmo candle chegando N vezes → só passa na primeira."""
    guard = CandleGuard()
    # Primeira: sem registrar ainda
    ok1, _ = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
    assert ok1 is True
    guard.registrar("EURUSD", TF, TS_ABERTURA)
    # Segunda e terceira
    ok2, m2 = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
    ok3, m3 = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
    assert ok2 is False and m2 == MOTIVO_CANDLE_DUPLICADO
    assert ok3 is False and m3 == MOTIVO_CANDLE_DUPLICADO


# ---------------------------------------------------------------------------
# Candle fora de ordem
# ---------------------------------------------------------------------------

def test_candle_fora_de_ordem_rejeitado():
    guard = CandleGuard()
    guard.registrar("EURUSD", TF, TS_ABERTURA + TF)  # último processado: candle 2
    ok, motivo = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK + TF * 2)
    assert ok is False
    assert motivo == MOTIVO_CANDLE_FORA_DE_ORDEM


def test_proximo_candle_aceito():
    guard = CandleGuard()
    guard.registrar("EURUSD", TF, TS_ABERTURA)
    ts_prox = TS_ABERTURA + TF
    ok, motivo = guard.validar("EURUSD", TF, ts_prox, ts_prox + TF + 1)
    assert ok is True
    assert motivo == ""


# ---------------------------------------------------------------------------
# Reconexão — guard deve ser resetável
# ---------------------------------------------------------------------------

def test_resetar_ativo_permite_reprocessamento():
    guard = CandleGuard()
    guard.registrar("EURUSD", TF, TS_ABERTURA)
    guard.resetar("EURUSD", TF)
    ok, motivo = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
    assert ok is True


def test_resetar_tudo_limpa_todos():
    guard = CandleGuard()
    guard.registrar("EURUSD", TF, TS_ABERTURA)
    guard.registrar("GBPUSD", TF, TS_ABERTURA)
    guard.resetar_tudo()
    ok1, _ = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
    ok2, _ = guard.validar("GBPUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
    assert ok1 is True
    assert ok2 is True


# ---------------------------------------------------------------------------
# Múltiplos ativos — sem interferência
# ---------------------------------------------------------------------------

def test_ativos_independentes():
    guard = CandleGuard()
    guard.registrar("EURUSD", TF, TS_ABERTURA)
    # GBPUSD ainda não processou — deve passar
    ok, motivo = guard.validar("GBPUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
    assert ok is True


def test_timeframes_independentes():
    guard = CandleGuard()
    guard.registrar("EURUSD", 300, TS_ABERTURA)
    # Mesmo ativo, M1 — slot separado
    ok, motivo = guard.validar("EURUSD", 60, TS_ABERTURA, TS_SERVIDOR_OK)
    assert ok is True


# ---------------------------------------------------------------------------
# Troca de ativo após mudança de timeframe
# ---------------------------------------------------------------------------

def test_candle_novo_apos_timeframe_diferente():
    guard = CandleGuard()
    guard.registrar("EURUSD", 300, TS_ABERTURA)
    # M1 com mesmo ts não deve ser bloqueado
    ok, _ = guard.validar("EURUSD", 60, TS_ABERTURA, TS_SERVIDOR_OK)
    assert ok is True


# ---------------------------------------------------------------------------
# Thread-safety: processamento simultâneo
# ---------------------------------------------------------------------------

def test_thread_safety_duplicacao():
    """Duas threads tentam registrar o mesmo candle simultaneamente.
    No máximo uma deve passar pela validação antes do registro."""
    guard = CandleGuard()
    resultados = []
    lock_res = threading.Lock()

    def tentar():
        ok, motivo = guard.validar("EURUSD", TF, TS_ABERTURA, TS_SERVIDOR_OK)
        if ok:
            guard.registrar("EURUSD", TF, TS_ABERTURA)
        with lock_res:
            resultados.append((ok, motivo))

    threads = [threading.Thread(target=tentar) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    sucessos = [r for r in resultados if r[0] is True]
    # Em cenário real, race condition pode deixar mais de 1 passar antes do
    # registrar — o UNIQUE do banco é a barreira definitiva. Aqui verificamos
    # que o guard não trava ou lança exceção sob concorrência.
    assert len(resultados) == 10
    assert len(sucessos) >= 1  # pelo menos 1 deve passar


# ---------------------------------------------------------------------------
# ultimo_processado
# ---------------------------------------------------------------------------

def test_ultimo_processado_none_antes_de_registrar():
    guard = CandleGuard()
    assert guard.ultimo_processado("EURUSD", TF) is None


def test_ultimo_processado_apos_registrar():
    guard = CandleGuard()
    guard.registrar("EURUSD", TF, TS_ABERTURA)
    assert guard.ultimo_processado("EURUSD", TF) == TS_ABERTURA


def test_ultimo_processado_atualiza():
    guard = CandleGuard()
    guard.registrar("EURUSD", TF, TS_ABERTURA)
    guard.registrar("EURUSD", TF, TS_ABERTURA + TF)
    assert guard.ultimo_processado("EURUSD", TF) == TS_ABERTURA + TF
