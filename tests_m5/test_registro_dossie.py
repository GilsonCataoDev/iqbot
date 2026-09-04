"""Testes para os campos sequencia e indicadores em entradas_hoje_detalhadas."""
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from iqoption_m5.registro import RegistroSQLite
from iqoption_m5.modelos import Decisao, ResultadoOrdem


def _inserir_entrada_com_detalhes(
    registro: RegistroSQLite,
    setup: str = "ema921_rsi",
    timeframe: int = 300,
    detalhes: dict | None = None,
) -> str:
    """Insere decisao (raw SQL) + abertura + resultado. Retorna id_ordem."""
    agora_utc = datetime.now(timezone.utc).replace(tzinfo=None).replace(microsecond=0)
    candle = pd.Timestamp(agora_utc)
    detalhes = detalhes or {
        "setup": setup,
        "ema9": 1.08450,
        "ema21": 1.08320,
        "rsi14": 63.2,
        "atr": 0.00041,
        "corpo_ratio": 0.72,
        "tendencia_macro": "alta",
        "tipo_candle": "engulf",
    }
    # Insere decisao diretamente (evita depender de SnapshotMercado/Autorizacao)
    with registro._sessao() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO decisoes (
                registrado_em, ativo, candle_hora, direcao, preco, payout,
                mercado_aberto, permitida, motivo_risco, motivo_estrategia,
                timeframe, setup, detalhes_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agora_utc.isoformat(),
                "EURUSD", agora_utc.isoformat(), "call",
                1.08, 0.85, 1, 1, "", "",  # NOT NULL: usar string vazia
                timeframe, setup, json.dumps(detalhes, ensure_ascii=False),
            ),
        )

    decisao = Decisao("EURUSD", "call", 1.08, candle, setup, detalhes=detalhes)
    ordem_id = f"test-{setup}-{agora_utc.isoformat()}"
    registro.registrar_abertura(
        ordem_id, decisao, 5.0, 0.85, agora_utc,
        timeframe=timeframe, expiracao_minutos=5,
    )
    resultado = ResultadoOrdem(
        ordem_id, "EURUSD", "call", agora_utc, agora_utc, 5.0, 0.85, 4.25, "win"
    )
    registro.registrar_resultado(resultado)
    return ordem_id


def test_sequencia_presente(tmp_path):
    registro = RegistroSQLite(tmp_path / "dossie.sqlite3")
    _inserir_entrada_com_detalhes(registro)

    entradas = registro.entradas_hoje_detalhadas()
    assert len(entradas) == 1
    seq = entradas[0]["sequencia"]
    assert seq is not None
    assert "enviada_em" in seq
    assert "vencimento_em" in seq
    assert "atraso_ms" in seq


def test_sequencia_enviada_formato_hhmmss(tmp_path):
    registro = RegistroSQLite(tmp_path / "dossie2.sqlite3")
    _inserir_entrada_com_detalhes(registro)

    entradas = registro.entradas_hoje_detalhadas()
    seq = entradas[0]["sequencia"]
    assert seq["enviada_em"] is not None
    partes = seq["enviada_em"].split(":")
    assert len(partes) == 3
    assert all(p.isdigit() for p in partes)


def test_sequencia_vencimento_apos_enviada(tmp_path):
    registro = RegistroSQLite(tmp_path / "dossie3.sqlite3")
    _inserir_entrada_com_detalhes(registro, timeframe=300)

    entradas = registro.entradas_hoje_detalhadas()
    seq = entradas[0]["sequencia"]
    if seq["enviada_em"] and seq["vencimento_em"]:
        assert seq["vencimento_em"] > seq["enviada_em"]


def test_sequencia_decisao_presente_quando_ha_decisao(tmp_path):
    registro = RegistroSQLite(tmp_path / "dossie4.sqlite3")
    _inserir_entrada_com_detalhes(registro)

    entradas = registro.entradas_hoje_detalhadas()
    seq = entradas[0]["sequencia"]
    # Quando ha decisao correlacionada, decisao_em deve ser preenchido
    assert seq["decisao_em"] is not None


def test_sequencia_atraso_ms_inteiro(tmp_path):
    registro = RegistroSQLite(tmp_path / "dossie5.sqlite3")
    _inserir_entrada_com_detalhes(registro)

    entradas = registro.entradas_hoje_detalhadas()
    seq = entradas[0]["sequencia"]
    assert isinstance(seq["atraso_ms"], int)


def test_indicadores_presentes(tmp_path):
    registro = RegistroSQLite(tmp_path / "ind1.sqlite3")
    _inserir_entrada_com_detalhes(registro)

    entradas = registro.entradas_hoje_detalhadas()
    ind = entradas[0]["indicadores"]
    assert ind is not None
    assert "ema9" in ind
    assert "ema_longa" in ind
    assert "rsi" in ind
    assert "atr" in ind
    assert "corpo_pct" in ind
    assert "tendencia" in ind
    assert "candle_tipo" in ind


def test_indicadores_valores_corretos(tmp_path):
    registro = RegistroSQLite(tmp_path / "ind2.sqlite3")
    _inserir_entrada_com_detalhes(
        registro,
        detalhes={
            "setup": "ema921_rsi",
            "ema9": 1.08450,
            "ema21": 1.08320,
            "rsi14": 63.2,
            "atr": 0.00041,
            "corpo_ratio": 0.72,
            "tendencia_macro": "alta",
            "tipo_candle": "engulf",
        },
    )

    entradas = registro.entradas_hoje_detalhadas()
    ind = entradas[0]["indicadores"]
    assert ind["ema9"] == pytest.approx(1.08450)
    assert ind["ema_longa"] == pytest.approx(1.08320)
    assert ind["rsi"] == pytest.approx(63.2)
    assert ind["corpo_pct"] == 72          # round(0.72 * 100)
    assert ind["tendencia"] == "alta"
    assert ind["candle_tipo"] == "engulf"


def test_indicadores_ema21_fallback(tmp_path):
    """ema_longa usa ema21 quando ema20 ausente."""
    registro = RegistroSQLite(tmp_path / "ind3.sqlite3")
    _inserir_entrada_com_detalhes(
        registro,
        detalhes={"setup": "ema921_rsi", "ema9": 1.0, "ema21": 1.1},
    )

    entradas = registro.entradas_hoje_detalhadas()
    assert entradas[0]["indicadores"]["ema_longa"] == pytest.approx(1.1)


def test_indicadores_ema20_preferido(tmp_path):
    """ema_longa usa ema20 quando presente."""
    registro = RegistroSQLite(tmp_path / "ind4.sqlite3")
    _inserir_entrada_com_detalhes(
        registro,
        setup="ema920_pullback",
        detalhes={"setup": "ema920_pullback", "ema9": 1.0, "ema20": 1.2, "ema21": 1.1},
    )

    entradas = registro.entradas_hoje_detalhadas()
    assert entradas[0]["indicadores"]["ema_longa"] == pytest.approx(1.2)


def test_indicadores_none_quando_sem_detalhes(tmp_path):
    """Campos de indicadores ficam None quando detalhes nao tem os valores."""
    registro = RegistroSQLite(tmp_path / "ind5.sqlite3")
    _inserir_entrada_com_detalhes(registro, detalhes={"setup": "ema921_rsi"})

    entradas = registro.entradas_hoje_detalhadas()
    ind = entradas[0]["indicadores"]
    assert ind["ema9"] is None
    assert ind["rsi"] is None
    assert ind["corpo_pct"] is None
