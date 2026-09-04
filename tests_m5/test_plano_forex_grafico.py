"""O plano forex tem de chegar ao gráfico com entrada, TP, SL e R:R.

Antes ele só ia como *fallback* do campo `alerta` — ou seja, sumia da tela
justamente quando havia outro alerta acontecendo. Agora vai em campo próprio.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from iqoption_m5.app import _pip_do_ativo, _plano_forex_payload
from iqoption_m5.forex_modelos import PlanoForex


def _plano(ativo: str = "EURUSD", lado: str = "buy", nivel: float = 1.1640,
           stop: float = 1.1630, alvo: float = 1.1670) -> PlanoForex:
    return PlanoForex(
        ativo=ativo, lado=lado, sinal_em=datetime(2026, 8, 27, 15, 30),
        nivel=nivel, stop=stop, alvo=alvo,
        risco_preco=abs(nivel - stop), motivo="rompimento + reteste",
    )


def test_pip_de_par_com_jpy_e_cem_vezes_maior() -> None:
    assert _pip_do_ativo("EURUSD") == 0.0001
    assert _pip_do_ativo("USDJPY") == 0.01
    assert _pip_do_ativo("EURJPY") == 0.01
    assert _pip_do_ativo("eurjpy") == 0.01


def test_payload_traz_tudo_que_a_pessoa_precisa_pra_executar() -> None:
    card = _plano_forex_payload(_plano(), preco_atual=1.1645)

    assert card["lado"] == "COMPRA"
    assert card["direcao"] == "call"
    assert card["entrada"] == 1.1645
    assert card["tp"] == 1.1670
    assert card["sl"] == 1.1630
    # 25 pips até o alvo, 15 até o stop.
    assert card["tpPips"] == 25.0
    assert card["slPips"] == 15.0
    assert card["rr"] == round(25 / 15, 2)


def test_pips_de_par_jpy_usam_a_escala_certa() -> None:
    card = _plano_forex_payload(
        _plano("USDJPY", nivel=147.00, stop=146.80, alvo=147.50),
        preco_atual=147.10,
    )
    # 0.40 de distância num par JPY = 40 pips, não 4000.
    assert card["tpPips"] == 40.0
    assert card["slPips"] == 30.0


def test_venda_inverte_o_lado() -> None:
    card = _plano_forex_payload(
        _plano(lado="sell", nivel=1.1640, stop=1.1660, alvo=1.1600),
        preco_atual=1.1645,
    )
    assert card["lado"] == "VENDA"
    assert card["direcao"] == "put"
    assert card["tpPips"] == 45.0
    assert card["slPips"] == 15.0


def test_rr_nao_explode_com_stop_no_preco() -> None:
    """Stop colado no preço daria divisão por zero."""
    card = _plano_forex_payload(
        _plano(stop=1.1645), preco_atual=1.1645,
    )
    assert card["rr"] is None


def test_grafico_expoe_plano_em_campo_proprio() -> None:
    fonte = (Path(__file__).resolve().parents[1] / "iqoption_m5" / "grafico.py").read_text(
        encoding="utf-8"
    )
    assert '"planoForex": plano_forex' in fonte, (
        "o plano precisa de campo proprio; como fallback de `alerta` ele some "
        "quando ha outro alerta na tela"
    )


def test_front_renderiza_o_card_e_prioriza_o_plano_nas_linhas() -> None:
    html = (Path(__file__).resolve().parents[1] / "grafico_web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="cartao-plano-forex"' in html
    assert "renderizarPlanoForex(dados)" in html
    # As linhas de SL/TP do grafico tem de sair do plano quando ele existe.
    assert "const fonteSLTP" in html
    assert "price: fonteSLTP.sl" in html
    assert "price: fonteSLTP.tp" in html
