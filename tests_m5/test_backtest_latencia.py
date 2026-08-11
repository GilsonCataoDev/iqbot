"""Testes determinísticos para simular_com_latencia e funções auxiliares.

Não usa rede, IQ Option ou clock real.
"""
import pandas as pd
import pytest

from iqoption_m5.backtest import (
    ATRASOS_PADRAO_S,
    Operacao,
    _classificar_sessao,
    imprimir_relatorio_latencia,
    simular_com_latencia,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _op(resultado="ganho", hora_entrada="2024-01-02 09:05:00",
        ativo="EURUSD", direcao="call", setup="macd_crossover"):
    return Operacao(
        ativo=ativo,
        direcao=direcao,
        setup=setup,
        fatores="-",
        hora_sinal=pd.Timestamp(hora_entrada) - pd.Timedelta(minutes=5),
        hora_entrada=pd.Timestamp(hora_entrada),
        preco_entrada=1.08500,
        preco_saida=1.08600 if resultado == "ganho" else (
            1.08500 if resultado == "empate" else 1.08400),
        resultado=resultado,
    )


def _ops_mistas():
    """3 ganhos, 2 perdas, 1 empate (sem atraso → WR = 3/5 = 60%)."""
    return [
        _op("ganho",  "2024-01-02 09:05:00", "EURUSD", "call", "macd_crossover"),
        _op("ganho",  "2024-01-02 10:05:00", "GBPUSD", "call", "sr_rejeicao"),
        _op("ganho",  "2024-01-02 14:05:00", "EURUSD", "put",  "divergencia_rsi"),
        _op("perda",  "2024-01-02 15:05:00", "GBPUSD", "put",  "sr_rejeicao"),
        _op("perda",  "2024-01-02 20:05:00", "EURUSD", "call", "pullback"),
        _op("empate", "2024-01-02 22:05:00", "EURUSD", "call", "pullback"),
    ]


# ---------------------------------------------------------------------------
# _classificar_sessao
# ---------------------------------------------------------------------------

def test_sessao_asia():
    assert _classificar_sessao(3) == "asia"


def test_sessao_london():
    assert _classificar_sessao(10) == "london"


def test_sessao_us():
    # hora 17 está só na sessão us (london termina às 16h)
    assert _classificar_sessao(17) == "us"


def test_sessao_off():
    assert _classificar_sessao(23) == "off"


def test_sessao_overlap_london_us():
    # hora 14 pertence a ambas; london declarado antes → london
    assert _classificar_sessao(14) == "london"


def test_sessao_hora_zero():
    assert _classificar_sessao(0) == "asia"


# ---------------------------------------------------------------------------
# simular_com_latencia — estrutura do DataFrame
# ---------------------------------------------------------------------------

def test_retorna_dataframe_vazio_sem_operacoes():
    resultado = simular_com_latencia([], 0.85)
    assert resultado.empty


def test_indice_e_atrasos():
    resultado = simular_com_latencia(_ops_mistas(), 0.85, atrasos_s=[0, 5, 30])
    assert list(resultado.index) == [0, 5, 30]


def test_colunas_obrigatorias():
    resultado = simular_com_latencia(_ops_mistas(), 0.85)
    for col in ("sinais_totais", "bloq_expiracao", "bloq_candle",
                "operacoes", "acerto_pct", "ic95_min_pct", "ic95_max_pct",
                "lucro_unidades", "taxa_retorno_pct"):
        assert col in resultado.columns, f"Coluna ausente: {col}"


def test_atrasos_padrao_usados_quando_nenhum_passado():
    resultado = simular_com_latencia(_ops_mistas(), 0.85)
    assert list(resultado.index) == ATRASOS_PADRAO_S


# ---------------------------------------------------------------------------
# simular_com_latencia — bloqueio por max_age_s
# ---------------------------------------------------------------------------

def test_atraso_zero_nao_bloqueia():
    resultado = simular_com_latencia(_ops_mistas(), 0.85, atrasos_s=[0], max_age_s=30.0)
    assert resultado.loc[0, "bloq_expiracao"] == 0
    assert resultado.loc[0, "operacoes"] == 5  # 5 decididas (6 total - 1 empate)


def test_atraso_menor_que_max_age_nao_bloqueia():
    resultado = simular_com_latencia(_ops_mistas(), 0.85, atrasos_s=[15], max_age_s=30.0)
    assert resultado.loc[15, "bloq_expiracao"] == 0


def test_atraso_igual_max_age_bloqueia_tudo():
    """atraso >= max_age_s → todos os sinais expiram."""
    ops = _ops_mistas()
    resultado = simular_com_latencia(ops, 0.85, atrasos_s=[30], max_age_s=30.0)
    assert resultado.loc[30, "bloq_expiracao"] == len(ops)
    assert resultado.loc[30, "operacoes"] == 0
    assert resultado.loc[30, "lucro_unidades"] == 0.0


def test_atraso_maior_que_max_age_bloqueia_tudo():
    ops = _ops_mistas()
    resultado = simular_com_latencia(ops, 0.85, atrasos_s=[60], max_age_s=30.0)
    assert resultado.loc[60, "bloq_expiracao"] == len(ops)


# ---------------------------------------------------------------------------
# simular_com_latencia — bloqueio por janela do candle
# ---------------------------------------------------------------------------

def test_atraso_igual_timeframe_bloqueia_por_candle():
    ops = _ops_mistas()
    resultado = simular_com_latencia(ops, 0.85, atrasos_s=[300], timeframe_s=300, max_age_s=9999.0)
    assert resultado.loc[300, "bloq_candle"] == len(ops)
    assert resultado.loc[300, "operacoes"] == 0


def test_atraso_abaixo_timeframe_nao_bloqueia_candle():
    resultado = simular_com_latencia(_ops_mistas(), 0.85, atrasos_s=[299],
                                     timeframe_s=300, max_age_s=9999.0)
    assert resultado.loc[299, "bloq_candle"] == 0


# ---------------------------------------------------------------------------
# simular_com_latencia — WR idêntico entre atrasos válidos
# ---------------------------------------------------------------------------

def test_wr_identico_para_atrasos_validos():
    """Sem tick data, WR não varia entre atrasos que não bloqueiam."""
    resultado = simular_com_latencia(_ops_mistas(), 0.85,
                                     atrasos_s=[0, 1, 3, 5, 10, 15],
                                     max_age_s=30.0)
    acertos = resultado["acerto_pct"].unique()
    assert len(acertos) == 1, f"WR variou entre atrasos válidos: {acertos}"


def test_lucro_zero_quando_todos_bloqueados():
    resultado = simular_com_latencia(_ops_mistas(), 0.85, atrasos_s=[30], max_age_s=30.0)
    assert resultado.loc[30, "lucro_unidades"] == 0.0
    assert resultado.loc[30, "taxa_retorno_pct"] == 0.0


# ---------------------------------------------------------------------------
# simular_com_latencia — sinais_totais
# ---------------------------------------------------------------------------

def test_sinais_totais_constante():
    """sinais_totais deve ser o mesmo em todas as linhas (não muda com atraso)."""
    ops = _ops_mistas()
    resultado = simular_com_latencia(ops, 0.85)
    assert (resultado["sinais_totais"] == len(ops)).all()


# ---------------------------------------------------------------------------
# simular_com_latencia — taxa_retorno_pct
# ---------------------------------------------------------------------------

def test_taxa_retorno_pct_zero_quando_sem_operacoes():
    resultado = simular_com_latencia(_ops_mistas(), 0.85, atrasos_s=[30], max_age_s=30.0)
    assert resultado.loc[30, "taxa_retorno_pct"] == 0.0


def test_taxa_retorno_pct_calculada_sobre_totais():
    """taxa_retorno = lucro / sinais_totais * 100."""
    ops = _ops_mistas()
    resultado = simular_com_latencia(ops, 0.85, atrasos_s=[0])
    lucro = resultado.loc[0, "lucro_unidades"]
    esperado = round(lucro / len(ops) * 100, 2)
    assert resultado.loc[0, "taxa_retorno_pct"] == pytest.approx(esperado, abs=0.01)


# ---------------------------------------------------------------------------
# imprimir_relatorio_latencia — smoke test
# ---------------------------------------------------------------------------

def test_imprimir_latencia_vazio(capsys):
    imprimir_relatorio_latencia([], 0.85)
    out = capsys.readouterr().out
    assert "Nenhuma" in out


def test_imprimir_latencia_nao_levanta(capsys):
    imprimir_relatorio_latencia(_ops_mistas(), 0.85, atrasos_s=[0, 15, 30])
    out = capsys.readouterr().out
    assert "max_age" in out
    assert "atraso_s" in out


def test_imprimir_latencia_mostra_sessoes(capsys):
    imprimir_relatorio_latencia(_ops_mistas(), 0.85, atrasos_s=[0])
    out = capsys.readouterr().out
    assert "sessao" in out.lower() or "Por sessão" in out
