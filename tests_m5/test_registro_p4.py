"""Testes para analise_atraso_por_setup (P4-B) e entradas_hoje_detalhadas(data=) (P4-C)."""
import json
from datetime import datetime, date, timedelta, timezone

import pandas as pd
import pytest

from iqoption_m5.registro import RegistroSQLite
from iqoption_m5.modelos import Decisao, ResultadoOrdem


# ── helpers ──────────────────────────────────────────────────────────────────

def _inserir_op(registro, setup, timeframe, resultado, atraso_ms=300, data_str=None):
    """Insere uma operação finalizada com atraso controlado."""
    if data_str:
        base = datetime.fromisoformat(data_str + "T10:00:00")
    else:
        base = datetime.now().replace(microsecond=0)
    candle = pd.Timestamp(base)
    decisao = Decisao("EURUSD", "call", 1.08, candle, setup, detalhes={"setup": setup})
    ordem_id = f"op-{setup}-{timeframe}-{atraso_ms}-{resultado}-{base.isoformat()}"
    # Registra abertura com atraso simulado (atraso_envio_ms calculado pelo método)
    # Para controlar atraso_ms, manipulamos candle_hora vs enviada_em
    from datetime import timedelta as _td
    enviada = base + _td(milliseconds=atraso_ms)
    registro.registrar_abertura(ordem_id, decisao, 5.0, 0.85, enviada,
                                 timeframe=timeframe, expiracao_minutos=5)
    lucro = 4.25 if resultado == "win" else -5.0
    registro.registrar_resultado(
        ResultadoOrdem(ordem_id, "EURUSD", "call", enviada, enviada, 5.0, 0.85, lucro, resultado)
    )


def _inserir_decisao_op(registro, setup, timeframe, resultado, data_str=None, atraso_ms=200):
    """Insere decisao + operacao para testar entradas_hoje_detalhadas(data=)."""
    if data_str:
        ts = datetime.fromisoformat(data_str + "T12:00:00")
    else:
        ts = datetime.now().replace(microsecond=0)
    candle = pd.Timestamp(ts)
    with registro._sessao() as db:
        db.execute(
            """INSERT OR IGNORE INTO decisoes
               (registrado_em, ativo, candle_hora, direcao, preco, payout,
                mercado_aberto, permitida, motivo_risco, motivo_estrategia,
                timeframe, setup, detalhes_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts.isoformat(), "EURUSD", ts.isoformat(), "call", 1.08, 0.85,
             1, 1, "", "", timeframe, setup,
             json.dumps({"setup": setup, "ema9": 1.083, "rsi14": 60.0})),
        )
    decisao = Decisao("EURUSD", "call", 1.08, candle, setup, detalhes={"setup": setup})
    ordem_id = f"op2-{setup}-{data_str or 'hoje'}-{resultado}"
    from datetime import timedelta as _td
    enviada = ts + _td(milliseconds=atraso_ms)
    registro.registrar_abertura(ordem_id, decisao, 5.0, 0.85, enviada,
                                 timeframe=timeframe, expiracao_minutos=5)
    lucro = 4.25 if resultado == "win" else -5.0
    registro.registrar_resultado(
        ResultadoOrdem(ordem_id, "EURUSD", "call", enviada, enviada, 5.0, 0.85, lucro, resultado)
    )


# ── analise_atraso_por_setup ──────────────────────────────────────────────────

def test_atraso_banco_vazio(tmp_path):
    registro = RegistroSQLite(tmp_path / "a.sqlite3")
    assert registro.analise_atraso_por_setup() == {}


def test_atraso_separa_rapido_lento(tmp_path):
    registro = RegistroSQLite(tmp_path / "b.sqlite3")
    _inserir_op(registro, "ema921_rsi", 300, "win",  atraso_ms=200)
    _inserir_op(registro, "ema921_rsi", 300, "win",  atraso_ms=300)
    _inserir_op(registro, "ema921_rsi", 300, "loss", atraso_ms=700)
    _inserir_op(registro, "ema921_rsi", 300, "loss", atraso_ms=900)

    resultado = registro.analise_atraso_por_setup(limiar_ms=500)

    assert "ema921_rsi" in resultado
    tf = resultado["ema921_rsi"][300]
    assert tf["rapido"]["n"] == 2
    assert tf["rapido"]["wins"] == 2
    assert tf["lento"]["n"] == 2
    assert tf["lento"]["wins"] == 0


def test_atraso_winrate_calculado(tmp_path):
    registro = RegistroSQLite(tmp_path / "c.sqlite3")
    _inserir_op(registro, "ema921_rsi", 300, "win",  atraso_ms=100)
    _inserir_op(registro, "ema921_rsi", 300, "win",  atraso_ms=200)
    _inserir_op(registro, "ema921_rsi", 300, "loss", atraso_ms=300)
    _inserir_op(registro, "ema921_rsi", 300, "loss", atraso_ms=600)

    tf = registro.analise_atraso_por_setup(limiar_ms=500)["ema921_rsi"][300]

    assert tf["rapido"]["winrate"] == pytest.approx(66.7, abs=0.1)
    assert tf["lento"]["winrate"] == pytest.approx(0.0)


def test_atraso_media_ms(tmp_path):
    registro = RegistroSQLite(tmp_path / "d.sqlite3")
    _inserir_op(registro, "ema921_rsi", 300, "win", atraso_ms=200)
    _inserir_op(registro, "ema921_rsi", 300, "win", atraso_ms=400)

    tf = registro.analise_atraso_por_setup()["ema921_rsi"][300]

    assert tf["rapido"]["media_ms"] == 300   # (200+400)/2


def test_atraso_separa_timeframes(tmp_path):
    registro = RegistroSQLite(tmp_path / "e.sqlite3")
    _inserir_op(registro, "ema921_rsi", 300, "win",  atraso_ms=100)
    _inserir_op(registro, "ema921_rsi", 900, "loss", atraso_ms=200)

    resultado = registro.analise_atraso_por_setup()["ema921_rsi"]

    assert set(resultado.keys()) == {300, 900}
    assert resultado[300]["rapido"]["wins"] == 1
    assert resultado[900]["rapido"]["wins"] == 0


def test_atraso_separa_setups(tmp_path):
    registro = RegistroSQLite(tmp_path / "f.sqlite3")
    _inserir_op(registro, "ema921_rsi",     300, "win")
    _inserir_op(registro, "ema920_pullback", 300, "loss", atraso_ms=600)

    resultado = registro.analise_atraso_por_setup()

    assert "ema921_rsi" in resultado
    assert "ema920_pullback" in resultado
    assert resultado["ema921_rsi"][300]["rapido"]["wins"] == 1
    assert resultado["ema920_pullback"][300]["lento"]["wins"] == 0


def test_atraso_limiar_customizavel(tmp_path):
    registro = RegistroSQLite(tmp_path / "g.sqlite3")
    _inserir_op(registro, "ema921_rsi", 300, "win", atraso_ms=400)
    _inserir_op(registro, "ema921_rsi", 300, "win", atraso_ms=600)

    tf_500 = registro.analise_atraso_por_setup(limiar_ms=500)["ema921_rsi"][300]
    tf_700 = registro.analise_atraso_por_setup(limiar_ms=700)["ema921_rsi"][300]

    assert tf_500["rapido"]["n"] == 1 and tf_500["lento"]["n"] == 1
    assert tf_700["rapido"]["n"] == 2 and tf_700["lento"]["n"] == 0


# ── entradas_hoje_detalhadas(data=) ──────────────────────────────────────────

def test_entradas_data_hoje_sem_param(tmp_path):
    registro = RegistroSQLite(tmp_path / "h.sqlite3")
    _inserir_decisao_op(registro, "ema921_rsi", 300, "win")
    entradas = registro.entradas_hoje_detalhadas()
    assert len(entradas) == 1


def test_entradas_data_hoje_explícito(tmp_path):
    registro = RegistroSQLite(tmp_path / "i.sqlite3")
    hoje = date.today().isoformat()
    _inserir_decisao_op(registro, "ema921_rsi", 300, "win")
    entradas = registro.entradas_hoje_detalhadas(data=hoje)
    assert len(entradas) == 1


def test_entradas_data_passada_retorna_corretas(tmp_path):
    registro = RegistroSQLite(tmp_path / "j.sqlite3")
    ontem = (date.today() - timedelta(days=1)).isoformat()
    _inserir_decisao_op(registro, "ema921_rsi", 300, "win", data_str=ontem)
    # Hoje não tem entradas
    entradas_hoje = registro.entradas_hoje_detalhadas()
    entradas_ontem = registro.entradas_hoje_detalhadas(data=ontem)
    assert len(entradas_hoje) == 0
    assert len(entradas_ontem) == 1


def test_entradas_data_inexistente_retorna_vazio(tmp_path):
    registro = RegistroSQLite(tmp_path / "k.sqlite3")
    entradas = registro.entradas_hoje_detalhadas(data="2020-01-01")
    assert entradas == []
