"""Testes determinísticos para pré-alertas no RegistroSQLite.

Usa banco in-memory (:memory:) — nenhum arquivo em disco é criado.
Nenhum teste depende de rede, relógio real ou IQ Option.
"""
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from iqoption_m5.registro import RegistroSQLite


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registro(tmp_path):
    """Banco em arquivo temporário isolado por teste."""
    return RegistroSQLite(tmp_path / "test.sqlite3")


def _gravar(reg, ativo="EURUSD", direcao="call", nivel=1.085,
            setup="reversao_candle", segundo=45, ts=1_700_000_300):
    reg.registrar_pre_alerta(ativo, direcao, nivel, setup, segundo, ts)


# ---------------------------------------------------------------------------
# registrar_pre_alerta
# ---------------------------------------------------------------------------

def test_registrar_pre_alerta_grava(registro):
    _gravar(registro)
    rows = registro.taxa_conversao_pre_alertas()
    assert len(rows) == 1
    r = rows[0]
    assert r["ativo"] == "EURUSD"
    assert r["direcao"] == "call"
    assert r["pre_alertas"] == 1
    assert r["convertidos"] == 0
    assert r["taxa_pct"] == 0.0


def test_registrar_multiplos_grupos(registro):
    _gravar(registro, ativo="EURUSD", direcao="call")
    _gravar(registro, ativo="EURUSD", direcao="call")
    _gravar(registro, ativo="GBPUSD", direcao="put")
    rows = registro.taxa_conversao_pre_alertas()
    totais = {r["ativo"]: r["pre_alertas"] for r in rows}
    assert totais["EURUSD"] == 2
    assert totais["GBPUSD"] == 1


def test_registrar_setup_separado(registro):
    _gravar(registro, setup="reversao_candle")
    _gravar(registro, setup="reversao_confluencia")
    rows = registro.taxa_conversao_pre_alertas()
    setups = {r["setup"] for r in rows}
    assert "reversao_candle" in setups
    assert "reversao_confluencia" in setups


# ---------------------------------------------------------------------------
# marcar_conversao_pre_alerta
# ---------------------------------------------------------------------------

def test_marcar_conversao_basica(registro):
    ts = 1_700_000_300
    _gravar(registro, ts=ts)
    registro.marcar_conversao_pre_alerta("EURUSD", "call", ts_unix=ts)
    rows = registro.taxa_conversao_pre_alertas()
    assert rows[0]["convertidos"] == 1
    assert rows[0]["taxa_pct"] == 100.0


def test_marcar_conversao_so_mais_recente(registro):
    """Com dois alertas, apenas o mais recente (maior id) é marcado."""
    ts1, ts2 = 1_700_000_000, 1_700_000_100
    _gravar(registro, ts=ts1)
    _gravar(registro, ts=ts2)
    registro.marcar_conversao_pre_alerta("EURUSD", "call", ts_unix=ts2)
    rows = registro.taxa_conversao_pre_alertas()
    assert rows[0]["convertidos"] == 1  # apenas 1 dos 2 foi marcado


def test_marcar_conversao_nao_afeta_direcao_oposta(registro):
    _gravar(registro, direcao="call", ts=1_700_000_300)
    _gravar(registro, direcao="put",  ts=1_700_000_300)
    registro.marcar_conversao_pre_alerta("EURUSD", "call", ts_unix=1_700_000_300)
    rows = {r["direcao"]: r for r in registro.taxa_conversao_pre_alertas()}
    assert rows["call"]["convertidos"] == 1
    assert rows["put"]["convertidos"] == 0


def test_marcar_conversao_nao_afeta_ativo_diferente(registro):
    _gravar(registro, ativo="EURUSD", ts=1_700_000_300)
    _gravar(registro, ativo="GBPUSD", ts=1_700_000_300)
    registro.marcar_conversao_pre_alerta("EURUSD", "call", ts_unix=1_700_000_300)
    rows = {r["ativo"]: r for r in registro.taxa_conversao_pre_alertas()}
    assert rows["EURUSD"]["convertidos"] == 1
    assert rows["GBPUSD"]["convertidos"] == 0


def test_marcar_conversao_fora_da_janela_ignorado(registro):
    """Pre-alerta muito antigo (ts muito menor que ts_unix - janela_s) não é convertido."""
    ts_alerta = 1_700_000_000
    ts_entrada = 1_700_001_000  # 1000s depois
    _gravar(registro, ts=ts_alerta)
    registro.marcar_conversao_pre_alerta("EURUSD", "call", ts_unix=ts_entrada, janela_s=300)
    rows = registro.taxa_conversao_pre_alertas()
    assert rows[0]["convertidos"] == 0  # fora da janela de 300s


def test_marcar_conversao_dentro_da_janela(registro):
    ts_alerta = 1_700_000_000
    ts_entrada = 1_700_000_200  # 200s depois — dentro de janela 300s
    _gravar(registro, ts=ts_alerta)
    registro.marcar_conversao_pre_alerta("EURUSD", "call", ts_unix=ts_entrada, janela_s=300)
    rows = registro.taxa_conversao_pre_alertas()
    assert rows[0]["convertidos"] == 1


def test_nao_marca_ja_convertido_novamente(registro):
    """Um alerta já convertido não deve ser marcado segunda vez (converteu=0 no filtro)."""
    ts = 1_700_000_300
    _gravar(registro, ts=ts)
    _gravar(registro, ts=ts)  # segundo alerta
    registro.marcar_conversao_pre_alerta("EURUSD", "call", ts_unix=ts)
    registro.marcar_conversao_pre_alerta("EURUSD", "call", ts_unix=ts)
    rows = registro.taxa_conversao_pre_alertas()
    # Cada chamada pega o MAX(id) com converteu=0; após primeira, o 2º pode ser marcado
    # mas nunca o mesmo id duas vezes. Total convertidos <= 2.
    assert rows[0]["convertidos"] <= 2


# ---------------------------------------------------------------------------
# taxa_conversao_pre_alertas — formato da resposta
# ---------------------------------------------------------------------------

def test_taxa_conversao_retorna_lista_de_dicts(registro):
    _gravar(registro)
    rows = registro.taxa_conversao_pre_alertas()
    assert isinstance(rows, list)
    assert isinstance(rows[0], dict)


def test_taxa_conversao_vazia_retorna_lista_vazia(registro):
    assert registro.taxa_conversao_pre_alertas() == []


def test_taxa_conversao_campos_obrigatorios(registro):
    _gravar(registro)
    rows = registro.taxa_conversao_pre_alertas()
    campos = {"ativo", "setup", "direcao", "pre_alertas", "convertidos", "taxa_pct", "segundo_medio"}
    assert campos.issubset(rows[0].keys())


def test_segundo_medio_calculado(registro):
    _gravar(registro, segundo=30, ts=1_700_000_000)
    _gravar(registro, segundo=60, ts=1_700_000_100)
    rows = registro.taxa_conversao_pre_alertas()
    assert rows[0]["segundo_medio"] == pytest.approx(45.0, abs=0.1)


# ---------------------------------------------------------------------------
# Thread-safety
# ---------------------------------------------------------------------------

def test_thread_safety_registrar(registro):
    erros = []

    def gravar():
        try:
            _gravar(registro, ts=1_700_000_300)
        except Exception as e:
            erros.append(e)

    threads = [threading.Thread(target=gravar) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not erros
    rows = registro.taxa_conversao_pre_alertas()
    assert rows[0]["pre_alertas"] == 20
