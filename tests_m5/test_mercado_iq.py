import unittest

import pandas as pd

from iqoption_m5.config import Configuracao
from iqoption_m5.mercado_iq import MercadoIQ


class ApiSemListaDigital:
    def get_all_init_v2(self):
        return {
            "turbo": {
                "actives": {
                    "1": {"name": "turbo.EURUSD", "enabled": True, "is_suspended": False},
                    "2": {"name": "turbo.GBPUSD", "enabled": False, "is_suspended": False},
                    "3": {"name": "turbo.EURUSD-OTC", "enabled": True, "is_suspended": False},
                }
            }
        }

    def get_all_open_time(self):
        raise TypeError("'NoneType' object is not subscriptable")

    def get_all_profit(self):
        return {
            "EURUSD": {"turbo": 0.85},
            "GBPUSD": {"turbo": 0.80},
            "USDJPY": {"turbo": 0.75},
            "EURUSD-OTC": {"turbo": 0.90},
        }


class TestMercadoIQ(unittest.TestCase):
    def test_cache_usa_apenas_turbo_quando_lista_digital_falha(self):
        mercado = MercadoIQ(Configuracao())
        mercado._api = ApiSemListaDigital()

        mercado._atualizar_cache_forcado()

        self.assertTrue(mercado._mercado_aberto["EURUSD"])
        self.assertFalse(mercado._mercado_aberto["GBPUSD"])
        self.assertFalse(mercado._mercado_aberto["USDJPY"])
        self.assertTrue(mercado._mercado_aberto["EURUSD-OTC"])
        self.assertEqual(mercado._payouts["EURUSD"], 0.85)
        self.assertEqual(mercado._payouts["EURUSD-OTC"], 0.90)

    def test_consulta_resultado_de_ordem_de_conexao_anterior(self):
        class ApiBaixoNivel:
            get_options_v2_data = None

            def get_options_v2(self, limite, tipos):
                self.get_options_v2_data = {
                    "name": "options",
                    "msg": {
                        "closed_options": [
                            {
                                "id": 14129006730,
                                "win": "win",
                                "pnl_net": 0.82,
                                "deposit": 1.0,
                            }
                        ]
                    },
                }

        class ApiHistorico:
            def __init__(self):
                self.api = ApiBaixoNivel()

            def get_betinfo(self, id_ordem):
                raise AssertionError("BUG: get_betinfo entra em reconexão infinita")

        mercado = MercadoIQ(Configuracao())
        mercado._api = ApiHistorico()

        self.assertAlmostEqual(
            mercado.consultar_resultado("14129006730", timeout_segundos=0.2),
            0.82,
        )

    def test_consulta_resultado_ignora_id_coincidente_em_dict_nao_opcao(self):
        """Um numero de ordem pode coincidir com um 'id' de qualquer outro
        objeto aninhado na resposta (metadado, instrumento, etc). So aceitar
        como resultado de verdade se o dict tambem parecer uma opcao —
        senao a gente pega o campo errado e inventa um lucro absurdo (bug
        real que ja aconteceu: -1999998 numa aposta de R$2)."""
        class ApiBaixoNivel:
            get_options_v2_data = None

            def get_options_v2(self, limite, tipos):
                self.get_options_v2_data = {
                    "metadados": {"id": 555, "descricao": "instrumento qualquer"},
                    "closed_options": [
                        {
                            "id": 555,
                            "win": "win",
                            "pnl_net": 1.5,
                            "deposit": 1.0,
                            "amount": 1.0,
                        }
                    ],
                }

        class ApiHistorico:
            def __init__(self):
                self.api = ApiBaixoNivel()

        mercado = MercadoIQ(Configuracao())
        mercado._api = ApiHistorico()

        # Deve achar o registro de opcao de verdade (tem 'amount'), nao o
        # metadado que so coincidentemente tambem tem id=555.
        self.assertAlmostEqual(
            mercado.consultar_resultado("555", timeout_segundos=0.2),
            1.5,
        )

    def test_resultado_por_candle_compara_entrada_com_fechamento(self):
        """Nao depende do historico da IQ: CALL ganha se o candle de entrada
        fechar acima do preco de entrada, PUT ganha se fechar abaixo."""
        indice = pd.date_range("2026-01-01 10:00", periods=3, freq="5min")
        buffer = pd.DataFrame(
            {
                "Open": [1.1000, 1.1010, 1.1050],
                "High": [1.1020, 1.1060, 1.1060],
                "Low": [1.0990, 1.1000, 1.1030],
                "Close": [1.1010, 1.1050, 1.1040],
                "Volume": [100, 100, 100],
            },
            index=indice,
        )

        mercado = MercadoIQ(Configuracao())
        mercado._buffers["EURUSD"] = buffer

        # Candle de entrada e o do meio (indice[1]): fechou em 1.1050.
        # Existe candle seguinte no buffer (indice[2]) -> considera fechado.
        self.assertEqual(
            mercado.resultado_por_candle("EURUSD", "call", 1.1010, indice[1], timeout_segundos=0.5),
            "win",
        )
        self.assertEqual(
            mercado.resultado_por_candle("EURUSD", "put", 1.1010, indice[1], timeout_segundos=0.5),
            "loss",
        )

    def test_resultado_por_candle_espera_candle_de_entrada_fechar(self):
        """Se o candle de entrada ainda e o ultimo do buffer (ainda esta se
        formando), nao deve confiar nele — precisa esperar o proximo
        aparecer, senao o preco de fechamento ainda pode mudar."""
        indice = pd.date_range("2026-01-01 10:00", periods=2, freq="5min")
        buffer = pd.DataFrame(
            {
                "Open": [1.1000, 1.1010],
                "High": [1.1020, 1.1060],
                "Low": [1.0990, 1.1000],
                "Close": [1.1010, 1.1050],
                "Volume": [100, 100],
            },
            index=indice,
        )
        mercado = MercadoIQ(Configuracao())
        mercado._buffers["EURUSD"] = buffer

        self.assertIsNone(
            mercado.resultado_por_candle("EURUSD", "call", 1.1010, indice[1], timeout_segundos=0.3)
        )


if __name__ == "__main__":
    unittest.main()
