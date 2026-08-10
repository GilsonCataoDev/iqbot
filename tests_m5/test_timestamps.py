"""
Melhoria A — verifica que hora_sinal e atraso_envio_ms são gravados corretamente.
"""
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from iqoption_m5.modelos import Decisao
from iqoption_m5.registro import RegistroSQLite


def _banco_temp() -> Path:
    d = tempfile.mkdtemp()
    return Path(d) / "test_timestamps.sqlite3"


def _decisao_com_candle_atrasado(segundos_atras: int = 10) -> Decisao:
    candle_hora = pd.Timestamp(datetime.now() - timedelta(seconds=segundos_atras))
    return Decisao(
        ativo="EURUSD-OTC",
        direcao="call",
        preco=1.08500,
        candle_hora=candle_hora,
        motivo="reversao_candle_curta",
        detalhes={"setup": "reversao_candle"},
    )


def test_hora_sinal_gravada():
    banco = _banco_temp()
    registro = RegistroSQLite(banco)
    decisao = _decisao_com_candle_atrasado(10)
    enviada_em = datetime.now()

    registro.registrar_abertura(
        id_ordem="teste-001",
        decisao=decisao,
        valor=1.0,
        payout=0.85,
        enviada_em=enviada_em,
    )

    conn = sqlite3.connect(banco)
    row = conn.execute(
        "SELECT hora_sinal, atraso_envio_ms FROM operacoes WHERE id_ordem='teste-001'"
    ).fetchone()
    conn.close()

    assert row is not None, "Operação não foi gravada"
    hora_sinal, atraso_envio_ms = row
    assert hora_sinal is not None, "hora_sinal deve ser preenchida"
    assert atraso_envio_ms is not None, "atraso_envio_ms deve ser preenchido"


def test_atraso_nao_negativo():
    banco = _banco_temp()
    registro = RegistroSQLite(banco)
    decisao = _decisao_com_candle_atrasado(10)
    enviada_em = datetime.now()

    registro.registrar_abertura(
        id_ordem="teste-002",
        decisao=decisao,
        valor=1.0,
        payout=0.85,
        enviada_em=enviada_em,
    )

    conn = sqlite3.connect(banco)
    row = conn.execute(
        "SELECT atraso_envio_ms FROM operacoes WHERE id_ordem='teste-002'"
    ).fetchone()
    conn.close()

    atraso_envio_ms = row[0]
    assert atraso_envio_ms >= 0, f"atraso_envio_ms deve ser >= 0, got {atraso_envio_ms}"


def test_atraso_razoavel_para_teste_sintetico():
    """Para candle 10s atrás e enviada_em=agora, atraso deve ser ~10000ms e bem abaixo de 60s."""
    banco = _banco_temp()
    registro = RegistroSQLite(banco)
    decisao = _decisao_com_candle_atrasado(10)
    enviada_em = datetime.now()

    registro.registrar_abertura(
        id_ordem="teste-003",
        decisao=decisao,
        valor=1.0,
        payout=0.85,
        enviada_em=enviada_em,
    )

    conn = sqlite3.connect(banco)
    row = conn.execute(
        "SELECT atraso_envio_ms FROM operacoes WHERE id_ordem='teste-003'"
    ).fetchone()
    conn.close()

    atraso_envio_ms = row[0]
    assert atraso_envio_ms < 60_000, (
        f"atraso_envio_ms={atraso_envio_ms} deveria ser < 60000ms para teste sintético"
    )
