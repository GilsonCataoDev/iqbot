"""Testes para desempenho_por_setup e desempenho_simulado_por_setup.

Verifica separação por timeframe, IC95% de Wilson, maturidade e EV.
"""
from datetime import datetime

import pandas as pd
import pytest

from iqoption_m5.registro import RegistroSQLite, _wilson_ci, _maturidade
from iqoption_m5.modelos import Decisao, ResultadoOrdem


# ── helpers de wilson_ci e maturidade ────────────────────────────────────────

def test_wilson_ci_none_para_n_zero():
    assert _wilson_ci(0, 0) is None


def test_wilson_ci_nunca_extrapola_0_100():
    lo, hi = _wilson_ci(0, 5)
    assert lo >= 0
    hi2, _ = _wilson_ci(5, 5)[1], None
    lo3, hi3 = _wilson_ci(5, 5)
    assert hi3 <= 100


def test_wilson_ci_amostra_grande_estreita_intervalo():
    lo, hi = _wilson_ci(1000, 2000)
    assert hi - lo < 5


def test_maturidade_tiers():
    assert _maturidade(0)   == "INSUFICIENTE"
    assert _maturidade(29)  == "INSUFICIENTE"
    assert _maturidade(30)  == "OBSERVAR"
    assert _maturidade(99)  == "OBSERVAR"
    assert _maturidade(100) == "CANDIDATA"
    assert _maturidade(299) == "CANDIDATA"
    assert _maturidade(300) == "APROVADA"


# ── helpers de inserção ───────────────────────────────────────────────────────

def _inserir_operacoes(registro, setup, timeframe, wins, losses):
    """Insere wins+losses operações finalizadas no banco."""
    agora = datetime.now().replace(microsecond=0)
    candle = pd.Timestamp(agora)
    payout = 0.85
    for i in range(wins + losses):
        ordem_id = f"{setup}-tf{timeframe}-{i}"
        decisao = Decisao(
            "EURUSD", "call", 1.08, candle, setup,
            detalhes={"setup": setup},
        )
        registro.registrar_abertura(ordem_id, decisao, 5.0, payout, agora, timeframe=timeframe)
        lucro = 4.25 if i < wins else -5.0
        resultado = "win" if i < wins else "loss"
        registro.registrar_resultado(
            ResultadoOrdem(ordem_id, "EURUSD", "call", agora, agora, 5.0, payout, lucro, resultado)
        )


def _inserir_simulacoes(registro, setup, timeframe, wins, losses):
    """Insere wins+losses simulações resolvidas no banco.

    Varia o candle_hora por índice para contornar a constraint UNIQUE
    (ativo, direcao, setup, timeframe, candle_hora).
    """
    base = pd.Timestamp("2026-09-01 10:00:00")
    payout = 0.85
    for i in range(wins + losses):
        candle = base + pd.Timedelta(minutes=5 * i)
        registro.registrar_simulacao_bloqueada(
            "EURUSD", "call", setup, candle, 1.08, payout,
            "filtro_teste", timeframe=timeframe,
        )
    with registro._sessao() as db:
        ids = [r[0] for r in db.execute(
            "SELECT id FROM simulacoes WHERE setup=? AND timeframe=? AND resultado IS NULL",
            (setup, timeframe),
        ).fetchall()]
    for j, id_sim in enumerate(ids):
        resultado = "win" if j < wins else "loss"
        registro.resolver_simulacao(id_sim, resultado)


# ── desempenho_por_setup ──────────────────────────────────────────────────────

def test_desempenho_por_setup_agrupa_por_timeframe(tmp_path):
    registro = RegistroSQLite(tmp_path / "desempenho.sqlite3")
    _inserir_operacoes(registro, "ema920_pullback", 300, wins=9, losses=4)
    _inserir_operacoes(registro, "ema920_pullback", 900, wins=3, losses=5)

    resultado = registro.desempenho_por_setup()

    assert "ema920_pullback" in resultado
    tf = resultado["ema920_pullback"]
    assert set(tf.keys()) == {300, 900}
    assert tf[300]["total"] == 13
    assert tf[900]["total"] == 8


def test_desempenho_por_setup_tem_ic95_e_maturidade(tmp_path):
    registro = RegistroSQLite(tmp_path / "desempenho_ic.sqlite3")
    _inserir_operacoes(registro, "ema921_rsi", 300, wins=9, losses=4)

    resultado = registro.desempenho_por_setup()
    item = resultado["ema921_rsi"][300]

    assert item["ic_95"] is not None
    lo, hi = item["ic_95"]
    assert 0 <= lo <= hi <= 100
    assert item["maturidade"] == "INSUFICIENTE"
    assert item["amostra_suficiente"] is False


def test_desempenho_por_setup_insuficiente_com_menos_de_30(tmp_path):
    registro = RegistroSQLite(tmp_path / "insuf.sqlite3")
    _inserir_operacoes(registro, "ema920_pullback", 300, wins=5, losses=3)

    item = registro.desempenho_por_setup()["ema920_pullback"][300]

    assert item["amostra_suficiente"] is False
    assert item["maturidade"] == "INSUFICIENTE"


def test_desempenho_por_setup_observar_com_30_a_99(tmp_path):
    registro = RegistroSQLite(tmp_path / "observar.sqlite3")
    _inserir_operacoes(registro, "ema920_pullback", 300, wins=18, losses=17)

    item = registro.desempenho_por_setup()["ema920_pullback"][300]

    assert item["amostra_suficiente"] is True
    assert item["maturidade"] == "OBSERVAR"


def test_desempenho_por_setup_ev_e_lucro_corretos(tmp_path):
    registro = RegistroSQLite(tmp_path / "ev.sqlite3")
    _inserir_operacoes(registro, "ema920_pullback", 300, wins=9, losses=4)

    item = registro.desempenho_por_setup()["ema920_pullback"][300]
    lucro_esperado = 9 * 4.25 + 4 * (-5.0)

    assert abs(item["lucro"] - round(lucro_esperado, 2)) < 0.01
    assert item["ev"] == round(item["lucro"] / item["total"], 3)


def test_desempenho_por_setup_setups_distintos_separados(tmp_path):
    registro = RegistroSQLite(tmp_path / "multi.sqlite3")
    _inserir_operacoes(registro, "ema920_pullback", 300, wins=5, losses=3)
    _inserir_operacoes(registro, "ema921_rsi",      300, wins=3, losses=2)

    resultado = registro.desempenho_por_setup()

    assert "ema920_pullback" in resultado
    assert "ema921_rsi" in resultado
    assert resultado["ema920_pullback"][300]["total"] == 8
    assert resultado["ema921_rsi"][300]["total"] == 5


# ── desempenho_simulado_por_setup ─────────────────────────────────────────────

def test_desempenho_simulado_agrupa_por_timeframe(tmp_path):
    registro = RegistroSQLite(tmp_path / "sombra.sqlite3")
    _inserir_simulacoes(registro, "ema920_pullback", 300, wins=12, losses=18)
    _inserir_simulacoes(registro, "ema920_pullback", 900, wins=4,  losses=6)

    resultado = registro.desempenho_simulado_por_setup()

    assert "ema920_pullback" in resultado
    tf = resultado["ema920_pullback"]
    assert set(tf.keys()) == {300, 900}
    assert tf[300]["total"] == 30
    assert tf[900]["total"] == 10


def test_desempenho_simulado_tem_ic95_e_maturidade(tmp_path):
    registro = RegistroSQLite(tmp_path / "sombra_ic.sqlite3")
    _inserir_simulacoes(registro, "ema920_pullback", 300, wins=12, losses=18)

    item = registro.desempenho_simulado_por_setup()["ema920_pullback"][300]

    assert item["ic_95"] is not None
    lo, hi = item["ic_95"]
    assert 0 <= lo <= hi <= 100
    assert item["amostra_suficiente"] is True
    assert item["maturidade"] == "OBSERVAR"


def test_desempenho_simulado_insuficiente_com_menos_de_30(tmp_path):
    registro = RegistroSQLite(tmp_path / "sombra_insuf.sqlite3")
    _inserir_simulacoes(registro, "ema920_pullback", 300, wins=5, losses=3)

    item = registro.desempenho_simulado_por_setup()["ema920_pullback"][300]

    assert item["amostra_suficiente"] is False
    assert item["maturidade"] == "INSUFICIENTE"


def test_desempenho_simulado_banco_vazio_retorna_dict_vazio(tmp_path):
    registro = RegistroSQLite(tmp_path / "vazio.sqlite3")

    assert registro.desempenho_simulado_por_setup() == {}


def test_desempenho_por_setup_banco_vazio_retorna_dict_vazio(tmp_path):
    registro = RegistroSQLite(tmp_path / "vazio2.sqlite3")

    assert registro.desempenho_por_setup() == {}
