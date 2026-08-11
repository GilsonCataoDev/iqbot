"""Testes determinísticos para separação OTC vs normal no backtest.

Cobre: para_dataframe, imprimir_relatorio, comparar_simulado_vs_real,
imprimir_comparacao. Sem rede, sem IQ Option, sem relógio real.
"""
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from iqoption_m5.backtest import (
    Operacao,
    _acumular,
    _finalizar_bucket,
    _novo_bucket,
    comparar_simulado_vs_real,
    imprimir_comparacao,
    imprimir_relatorio,
    para_dataframe,
    tabela_por,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _op(ativo, resultado="ganho", direcao="call"):
    return Operacao(
        ativo=ativo,
        direcao=direcao,
        setup="macd_crossover",
        fatores="-",
        hora_sinal=pd.Timestamp("2024-01-02 09:00:00"),
        hora_entrada=pd.Timestamp("2024-01-02 09:05:00"),
        preco_entrada=1.08500,
        preco_saida=1.08600 if resultado == "ganho" else (
            1.08500 if resultado == "empate" else 1.08400),
        resultado=resultado,
    )


def _ops_mistas():
    return [
        _op("EURUSD",     "ganho"),
        _op("EURUSD",     "ganho"),
        _op("EURUSD",     "perda"),
        _op("EURUSD-OTC", "ganho"),
        _op("EURUSD-OTC", "perda"),
        _op("EURUSD-OTC", "perda"),
    ]


def _db_com_ops(tmp_path: Path) -> str:
    db = str(tmp_path / "test.sqlite3")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE operacoes (
            id_ordem TEXT PRIMARY KEY,
            ativo TEXT, setup TEXT, lucro REAL, status TEXT,
            enviada_em TEXT
        )"""
    )
    rows = [
        # ativo,         setup,          lucro, status
        ("EURUSD",       "sr_rejeicao",   0.85, "finalizada"),
        ("EURUSD",       "sr_rejeicao",  -1.0,  "finalizada"),
        ("EURUSD",       "sr_rejeicao",   0.85, "finalizada"),
        ("EURUSD-OTC",   "macd_crossover", 0.85, "finalizada"),
        ("EURUSD-OTC",   "macd_crossover", -1.0, "finalizada"),
        ("GBPUSD",       "pullback",       0.85, "finalizada"),
        ("GBPUSD",       "correcao_manual", 0.0, "finalizada"),  # deve ser excluída
    ]
    for i, (ativo, setup, lucro, status) in enumerate(rows):
        conn.execute(
            "INSERT INTO operacoes VALUES (?,?,?,?,?,?)",
            (str(i), ativo, setup, lucro, status, "2024-01-02T10:00:00"),
        )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# para_dataframe — tipo_ativo derivado do nome
# ---------------------------------------------------------------------------

def test_tipo_ativo_normal():
    df = para_dataframe([_op("EURUSD")])
    assert df.loc[0, "tipo_ativo"] == "normal"


def test_tipo_ativo_otc():
    df = para_dataframe([_op("EURUSD-OTC")])
    assert df.loc[0, "tipo_ativo"] == "OTC"


def test_tipo_ativo_otc_minusculo():
    df = para_dataframe([_op("GBPUSD-otc")])
    # OTC detection usa "-OTC" case-sensitive; -otc fica como normal (par incomum)
    assert df.loc[0, "tipo_ativo"] == "normal"


def test_tipo_ativo_coluna_presente():
    df = para_dataframe(_ops_mistas())
    assert "tipo_ativo" in df.columns


def test_tipo_ativo_contagem():
    df = para_dataframe(_ops_mistas())
    assert (df["tipo_ativo"] == "OTC").sum() == 3
    assert (df["tipo_ativo"] == "normal").sum() == 3


def test_para_dataframe_vazio_tem_coluna_tipo_ativo():
    df = para_dataframe([])
    assert "tipo_ativo" in df.columns


# ---------------------------------------------------------------------------
# tabela_por — por tipo_ativo
# ---------------------------------------------------------------------------

def test_tabela_por_tipo_ativo():
    df = para_dataframe(_ops_mistas())
    tabela = tabela_por(df, "tipo_ativo", payout=0.85)
    assert "OTC" in tabela.index
    assert "normal" in tabela.index


def test_tabela_por_tipo_ativo_wr_separado():
    # EURUSD: 2 ganhos, 1 perda → WR 66.7%
    # EURUSD-OTC: 1 ganho, 2 perdas → WR 33.3%
    df = para_dataframe(_ops_mistas())
    tabela = tabela_por(df, "tipo_ativo", payout=0.85)
    assert tabela.loc["normal", "acerto_pct"] > tabela.loc["OTC", "acerto_pct"]


# ---------------------------------------------------------------------------
# imprimir_relatorio — inclui seção OTC vs normal
# ---------------------------------------------------------------------------

def test_imprimir_relatorio_inclui_otc_normal(capsys):
    df = para_dataframe(_ops_mistas())
    imprimir_relatorio(df, payout=0.85)
    saida = capsys.readouterr().out
    assert "OTC" in saida
    assert "normal" in saida


# ---------------------------------------------------------------------------
# _novo_bucket / _acumular / _finalizar_bucket
# ---------------------------------------------------------------------------

def test_novo_bucket_zeros():
    b = _novo_bucket()
    assert b["wins"] == 0 and b["losses"] == 0 and b["lucro_total"] == 0.0


def test_acumular_win():
    b = _novo_bucket()
    _acumular(b, 0.85)
    assert b["wins"] == 1 and b["losses"] == 0


def test_acumular_loss():
    b = _novo_bucket()
    _acumular(b, -1.0)
    assert b["losses"] == 1 and b["wins"] == 0


def test_acumular_empate():
    b = _novo_bucket()
    _acumular(b, 0.0)
    assert b["empates"] == 1


def test_finalizar_bucket_winrate():
    b = _novo_bucket()
    _acumular(b, 0.85)
    _acumular(b, 0.85)
    _acumular(b, -1.0)
    _finalizar_bucket(b)
    assert b["winrate"] == pytest.approx(2 / 3, abs=0.01)


def test_finalizar_bucket_sem_decididas():
    b = _novo_bucket()
    _acumular(b, 0.0)  # só empate
    _finalizar_bucket(b)
    assert b["winrate"] == 0.0


# ---------------------------------------------------------------------------
# comparar_simulado_vs_real — por_tipo_ativo
# ---------------------------------------------------------------------------

def test_comparar_retorna_por_tipo_ativo(tmp_path):
    db = _db_com_ops(tmp_path)
    resultado = comparar_simulado_vs_real(db, payout=0.85)
    assert "por_tipo_ativo" in resultado
    assert "OTC" in resultado["por_tipo_ativo"]
    assert "normal" in resultado["por_tipo_ativo"]


def test_comparar_exclui_correcao_manual(tmp_path):
    db = _db_com_ops(tmp_path)
    resultado = comparar_simulado_vs_real(db, payout=0.85)
    # rows: EURUSD×3 + EURUSD-OTC×2 + GBPUSD pullback×1 = 6 (correcao_manual excluída)
    assert resultado["global"]["operacoes"] == 6


def test_comparar_otc_contagem(tmp_path):
    db = _db_com_ops(tmp_path)
    resultado = comparar_simulado_vs_real(db, payout=0.85)
    otc = resultado["por_tipo_ativo"]["OTC"]
    assert otc["operacoes"] == 2  # 2 rows EURUSD-OTC


def test_comparar_normal_contagem(tmp_path):
    db = _db_com_ops(tmp_path)
    resultado = comparar_simulado_vs_real(db, payout=0.85)
    normal = resultado["por_tipo_ativo"]["normal"]
    # EURUSD sr_rejeicao×3 + GBPUSD pullback×1 = 4
    assert normal["operacoes"] == 4


def test_comparar_global_wins(tmp_path):
    db = _db_com_ops(tmp_path)
    resultado = comparar_simulado_vs_real(db, payout=0.85)
    # EURUSD: wins=2, losses=1 | EURUSD-OTC: wins=1, losses=1 | GBPUSD: wins=1
    # total wins=4, losses=2
    assert resultado["global"]["wins"] == 4
    assert resultado["global"]["losses"] == 2


# ---------------------------------------------------------------------------
# imprimir_comparacao — mostra seção OTC vs normal
# ---------------------------------------------------------------------------

def test_imprimir_comparacao_mostra_otc(tmp_path, capsys):
    db = _db_com_ops(tmp_path)
    resultado = comparar_simulado_vs_real(db, payout=0.85)
    imprimir_comparacao(resultado, payout=0.85)
    saida = capsys.readouterr().out
    assert "OTC" in saida
    assert "normal" in saida


def test_imprimir_comparacao_sem_dados(capsys):
    resultado = {
        "global": {"operacoes": 0, "wins": 0, "losses": 0, "empates": 0,
                   "winrate": 0.0, "ic95_min": 0.0, "ic95_max": 0.0, "lucro_total": 0.0},
        "por_setup": {},
        "por_tipo_ativo": {},
    }
    imprimir_comparacao(resultado, payout=0.85)
    saida = capsys.readouterr().out
    assert "sem operações" in saida
