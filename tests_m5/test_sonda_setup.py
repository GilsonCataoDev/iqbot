"""A sonda decide pelo payout real de cada par, não por um 0.87 fixo."""
from __future__ import annotations

import sonda_setup


def test_breakeven_sobe_quando_o_payout_cai() -> None:
    """Medido na IQ em 27/08/2026: a maioria dos pares paga 0.85, não 0.87.

    A diferença decide setup: um WR de 53.8% aprova a 0.87 e reprova a 0.85.
    """
    assert sonda_setup.breakeven_de(0.87) == 1 / 1.87
    assert sonda_setup.breakeven_de(0.85) == 1 / 1.85
    assert sonda_setup.breakeven_de(0.85) > sonda_setup.breakeven_de(0.87)
    # O breakeven de 53.5% que a sonda usava antes é otimista pros pares a 0.85.
    assert sonda_setup.breakeven_de(0.85) > 0.535


def test_bootstrap_detecta_edge_negativo_com_wr_acima_de_meio() -> None:
    """WR de 52% parece 'mais ganha que perde', mas a 0.85 perde dinheiro.

    É exatamente o caso que o breakeven fixo de 53.5% deixava passar perto do
    limite; o retorno realizado não deixa dúvida.
    """
    retornos = [0.85] * 52 + [-1.0] * 48
    media = sum(retornos) / len(retornos)
    assert media < 0
    lo, hi = sonda_setup.bootstrap_media(retornos, reps=4000)
    assert lo < media < hi


def test_bootstrap_e_deterministico() -> None:
    """Mesma amostra tem de dar a mesma decisão em duas execuções."""
    retornos = [0.85] * 60 + [-1.0] * 40
    assert (sonda_setup.bootstrap_media(retornos, reps=2000)
            == sonda_setup.bootstrap_media(retornos, reps=2000))


def test_bootstrap_de_amostra_vazia_nao_quebra() -> None:
    assert sonda_setup.bootstrap_media([]) == (0.0, 0.0)


def test_wilson_continua_correto_em_amostra_pequena() -> None:
    lo, hi = sonda_setup.wilson(2, 3)
    assert 0.0 < lo < 2 / 3 < hi < 1.0
    assert sonda_setup.wilson(0, 0) == (0.0, 1.0)
