"""O swing não pode culpar a conta por um endpoint que a IQ aposentou.

Medido em 27/08/2026: `get-instruments` responde
{"success":false,"reason":"Invalid contract"} em qualquer versão e para qualquer
tipo — forex, cfd E crypto. O timeout que isso gerava era logado como
"conta PRACTICE pode não suportar CFD", conclusão falsa: a conta enxerga 46
pares forex, 44 abertos.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from iqoption_swing.mercado_swing import MercadoSwing


FONTE = Path(__file__).resolve().parents[1] / "iqoption_swing" / "mercado_swing.py"


def test_nao_culpa_mais_a_conta_por_falta_de_cfd() -> None:
    texto = FONTE.read_text(encoding="utf-8")
    assert "pode não suportar CFD" not in texto, (
        "o timeout do get-instruments nao diz nada sobre a conta; "
        "esse texto ja induziu a conclusao errada uma vez"
    )


def test_usa_o_endpoint_vivo_e_nao_o_aposentado() -> None:
    fonte = inspect.getsource(MercadoSwing._buscar_ids_forex)
    assert "marginal-forex-instruments.get-underlying-list" in fonte
    assert '"version": "1.0"' in fonte
    # get_instruments da lib fala 'get-instruments', que a IQ nao aceita mais.
    assert "get_instruments" not in fonte


def test_pega_active_id_e_ignora_suspenso() -> None:
    import iqoption_swing.mercado_swing as m

    # O fake responde QUANDO o pedido e enviado, como o servidor faz — o metodo
    # limpa o slot antes de mandar, entao pre-popular nao serviria.
    class ApiFake:
        class _Interna:
            enviado = None

            def send_websocket_request(self, nome, msg, rid=""):
                type(self).enviado = msg
                with m._LOCK_UNDERLYING:
                    m._UNDERLYING["items"] = [
                        {"name": "EURUSD", "active_id": 1, "is_suspended": False},
                        {"name": "GBPUSD", "active_id": 5, "is_suspended": True},
                        {"name": "USDJPY", "active_id": 6, "is_suspended": False},
                        {"name": "AUDCAD", "active_id": 77, "is_suspended": False},
                    ]

        def __init__(self):
            self.api = self._Interna()

    mercado = MercadoSwing.__new__(MercadoSwing)
    mercado._api = ApiFake()

    class ConfigFake:
        ativos = ("EURUSD", "GBPUSD", "USDJPY")

    mercado.config = ConfigFake()
    try:
        ids = mercado._buscar_ids_forex(timeout=2.0)
    finally:
        with m._LOCK_UNDERLYING:
            m._UNDERLYING.pop("items", None)

    # GBPUSD sai por estar suspenso; AUDCAD sai por nao estar na config.
    assert ids == {"EURUSD": 1, "USDJPY": 6}
    enviado = ApiFake._Interna.enviado
    assert enviado["name"] == "marginal-forex-instruments.get-underlying-list"
    assert enviado["version"] == "1.0"
    assert enviado["body"] == {}


def test_sem_resposta_devolve_vazio_em_vez_de_travar() -> None:
    class ApiFake:
        class _Interna:
            def send_websocket_request(self, nome, msg, rid=""):
                pass

        def __init__(self):
            self.api = self._Interna()

    import iqoption_swing.mercado_swing as m

    mercado = MercadoSwing.__new__(MercadoSwing)
    mercado._api = ApiFake()

    class ConfigFake:
        ativos = ("EURUSD",)

    mercado.config = ConfigFake()
    with m._LOCK_UNDERLYING:
        m._UNDERLYING.pop("items", None)

    assert mercado._buscar_ids_forex(timeout=0.6) == {}
