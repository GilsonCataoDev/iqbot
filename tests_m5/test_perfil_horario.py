import numpy as np
import pandas as pd
import pytest

from iqoption_m5.perfil_horario import (
    MINIMO_CONFIAVEL, contexto, regime, sessao, tabela,
)


def _serie(n: int, inicio="2026-01-01", passo="15min", preco=100.0):
    indice = pd.date_range(inicio, periods=n, freq=passo)
    ruido = np.sin(np.arange(n) / 7.0) * 0.5
    fechamento = preco + ruido
    return pd.DataFrame({
        "Open": fechamento - 0.05, "High": fechamento + 0.2,
        "Low": fechamento - 0.2, "Close": fechamento,
    }, index=indice)


def test_sem_amostra_devolve_none_em_vez_de_tabela_fraca():
    """Alvo calibrado por poucas velas parece medido e nao e."""
    assert tabela(_serie(500)) is None
    assert tabela(None) is None


def test_respeita_minimo_configuravel():
    df = _serie(2000)

    assert tabela(df) is None
    assert tabela(df, minimo=1000) is not None


def test_tabela_cobre_as_horas_com_amostra():
    tab = tabela(_serie(MINIMO_CONFIAVEL + 200), minimo=MINIMO_CONFIAVEL)

    assert tab
    assert set(tab) <= set(range(24))
    for valores in tab.values():
        ap25, ap50, ap75, dp50, dp75, cp50 = valores
        assert ap25 <= ap50 <= ap75
        assert dp50 <= dp75
        assert cp50 >= 0


def test_contexto_converte_percentual_em_pontos_do_ativo():
    tab = {10: (0.5, 1.0, 1.5, 0.4, 0.8, 0.2)}

    ctx = contexto(tab, 10, preco=4000.0)

    assert ctx["tp1_pct"] == 0.4
    assert ctx["tp1_pts"] == pytest.approx(16.0)
    assert ctx["sl_min_pts"] == pytest.approx(8.0)
    assert ctx["sessao"] == "europa"


def test_contexto_sem_perfil_ou_hora_devolve_none():
    assert contexto(None, 10, 100.0) is None
    assert contexto({10: (1, 1, 1, 1, 1, 1)}, 11, 100.0) is None
    assert contexto({10: (1, 1, 1, 1, 1, 1)}, 10, 0.0) is None


def test_regime_classifica_contra_a_faixa_da_hora():
    tab = {10: (1.0, 1.5, 2.0, 0.4, 0.8, 0.2)}

    assert regime(tab, 0.05, 10) == "quieto"   # < 10% do p25
    assert regime(tab, 0.3, 10) == "normal"
    assert regime(tab, 0.9, 10) == "ativo"     # > 30% do p75


def test_regime_sem_perfil_nao_inventa_anormalidade():
    assert regime(None, 99.0, 10) == "normal"
    assert regime({10: (1, 1, 1, 1, 1, 1)}, 99.0, 11) == "normal"


def test_sessoes_cobrem_as_24_horas():
    assert {sessao(h) for h in range(24)} == {"ásia", "europa", "ny", "noite"}
    assert sessao(3) == "ásia" and sessao(10) == "europa"
    assert sessao(15) == "ny" and sessao(20) == "noite"


def test_contra_e_agnostico_de_direcao():
    """Por ancora vale o MENOR dos dois lados, senao o piso vira teto.

    Reproduz a tabela fixa do ouro: tomar so a excursao de compra dava
    contra_p50 cerca de 2,4x maior que o perfil publicado.
    """
    n = 6000
    indice = pd.date_range("2026-01-01", periods=n, freq="15min")
    # Alta persistente: quem compra quase nao sofre, quem vende sofre muito.
    fechamento = 100 + np.arange(n) * 0.01
    df = pd.DataFrame({
        "Open": fechamento, "High": fechamento + 0.01,
        "Low": fechamento - 0.01, "Close": fechamento,
    }, index=indice)

    tab = tabela(df, minimo=1000)

    # O lado da venda sofre o deslocamento inteiro; o minimo pega o da compra.
    for valores in tab.values():
        assert valores[5] < valores[3]
