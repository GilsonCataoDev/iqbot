import unittest
import time

import pandas as pd

from iqoption_m5.config import Configuracao
from iqoption_m5.mercado_iq import MercadoIQ, MercadoIndisponivel


def _opcao(commission):
    return {"option": {"profit": {"commission": commission}}}


class ApiSemListaDigital:
    def get_all_init_v2(self):
        return {
            "turbo": {
                "actives": {
                    "1": {"name": "turbo.EURUSD", "enabled": True,
                          "is_suspended": False, **_opcao(15)},
                    "2": {"name": "turbo.GBPUSD", "enabled": False,
                          "is_suspended": False, **_opcao(20)},
                    "3": {"name": "turbo.EURUSD-OTC", "enabled": True,
                          "is_suspended": False, **_opcao(10)},
                }
            }
        }

    def get_all_open_time(self):
        raise TypeError("'NoneType' object is not subscriptable")

    def get_all_profit(self):
        raise AssertionError(
            "BUG: get_all_profit usa o endpoint v1 morto (busy-wait de 30s que "
            "retenta pra sempre e nunca levanta). O payout tem de sair do v2."
        )


class TestMercadoIQ(unittest.TestCase):
    def test_snapshot_recupera_candle_direto_quando_stream_esta_atrasado(self):
        """O gráfico não pode congelar só porque o stream da IQ ficou velho."""
        agora = int(time.time())
        inicio_atual = agora - (agora % 300)

        def vela(inicio, fechamento):
            return {
                "from": inicio, "open": fechamento - .0001, "close": fechamento,
                "min": fechamento - .0002, "max": fechamento + .0002, "volume": 10,
            }

        class ApiStreamVelho:
            def get_server_timestamp(self):
                return agora

            def get_realtime_candles(self, ativo, timeframe):
                antiga = vela(inicio_atual - 1_200, 1.10)
                return {antiga["from"]: antiga}

            def get_candles(self, ativo, timeframe, quantidade, fim):
                return [
                    vela(inicio_atual - 600, 1.11),
                    vela(inicio_atual - 300, 1.12),
                    vela(inicio_atual, 1.13),
                ]

        mercado = MercadoIQ(Configuracao(ativos=("EURUSD",), limite_candles=3))
        mercado._api = ApiStreamVelho()
        mercado._mercado_aberto = {"EURUSD": True}
        mercado._payouts = {"EURUSD": .85}
        mercado._cache_atualizado = time.time()
        mercado._buffers["EURUSD"] = mercado._candles_para_df(
            [
                vela(inicio_atual - 1_800, 1.10), vela(inicio_atual - 1_500, 1.10),
                vela(inicio_atual - 1_200, 1.10),
            ]
        )

        snapshot = mercado.snapshot("EURUSD")

        self.assertEqual(float(snapshot.candles.iloc[-1]["Close"]), 1.13)
        self.assertGreaterEqual(int(snapshot.candles.index[-1].timestamp()), inicio_atual)

    def test_realtime_tem_timeout_para_nao_congelar_o_lab(self):
        class ApiRealtimeTravado:
            def get_realtime_candles(self, ativo, timeframe):
                time.sleep(.2)

        mercado = MercadoIQ(Configuracao())
        mercado._api = ApiRealtimeTravado()
        inicio = time.monotonic()
        with self.assertRaises(MercadoIndisponivel):
            mercado._buscar_realtime_com_timeout("EURUSD", 300, timeout=.02)
        self.assertLess(time.monotonic() - inicio, .12)

    def test_contexto_superior_descarta_candle_em_formacao(self):
        indice = pd.date_range("2026-08-25 08:00", periods=3, freq="1h")
        candles = pd.DataFrame({"Close": [1.0, 2.0, 99.0]}, index=indice)

        fechados = MercadoIQ._somente_fechados(
            candles, timeframe_segundos=3600, timestamp_servidor=indice[-1].timestamp() + 300
        )

        self.assertEqual(list(fechados["Close"]), [1.0, 2.0])

    def test_ids_separam_otc_explicito_de_mercado_normal_op(self):
        class ApiComNormalEOTC:
            def get_all_init_v2(self):
                return {
                    "turbo": {
                        "actives": {
                            "81": {
                                "name": "front.GBPUSD-OTC",
                                "enabled": True,
                                "is_suspended": True,
                            },
                            "1867": {
                                "name": "front.GBPUSD-op",
                                "enabled": True,
                                "is_suspended": False,
                            },
                        }
                    }
                }

        mercado = MercadoIQ(
            Configuracao(ativos=("GBPUSD-OTC", "GBPUSD"))
        )
        mercado._api = ApiComNormalEOTC()

        abertos, _ = mercado._obter_estado_mercado()

        self.assertEqual(mercado._ids_ativos["GBPUSD-OTC"], 81)
        self.assertEqual(mercado._ids_ativos["GBPUSD"], 1867)
        self.assertTrue(abertos["GBPUSD-OTC"])
        self.assertTrue(abertos["GBPUSD"])

    def test_cache_usa_apenas_turbo_quando_lista_digital_falha(self):
        mercado = MercadoIQ(Configuracao())
        mercado._api = ApiSemListaDigital()

        mercado._atualizar_cache_forcado()

        self.assertTrue(mercado._mercado_aberto["EURUSD"])
        self.assertFalse(mercado._mercado_aberto["GBPUSD"])
        self.assertFalse(mercado._mercado_aberto["USDJPY"])
        self.assertTrue(mercado._mercado_aberto["EURUSD-OTC"])
        # Payout vem de option.profit.commission do proprio v2: (100-c)/100.
        self.assertAlmostEqual(mercado._payouts["EURUSD"], 0.85)
        self.assertAlmostEqual(mercado._payouts["EURUSD-OTC"], 0.90)
        self.assertIsNone(mercado._payouts["USDJPY"])

    def test_refresh_de_cache_nao_repete_com_workers_concorrentes(self):
        """Sem lock, os 7 workers veem o cache vencido no mesmo instante e
        disparam 7 refreshes simultaneos — era a origem das rajadas de
        'get_all_init late 30 sec' oito a oito no log."""
        import threading

        mercado = MercadoIQ(Configuracao())
        mercado._api = ApiSemListaDigital()
        chamadas = []
        original = mercado._atualizar_cache_forcado

        def contando():
            chamadas.append(1)
            original()

        mercado._atualizar_cache_forcado = contando

        largada = threading.Event()

        def worker():
            largada.wait()
            mercado._atualizar_cache_se_preciso()

        threads = [threading.Thread(target=worker) for _ in range(7)]
        for t in threads:
            t.start()
        largada.set()
        for t in threads:
            t.join()

        self.assertEqual(len(chamadas), 1)

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

    def test_consulta_resultado_aceita_id_em_lista_da_iq(self):
        class ApiBaixoNivel:
            get_options_v2_data = None

            def get_options_v2(self, limite, tipos):
                self.get_options_v2_data = {
                    "msg": {"closed_options": [{
                        "id": [14217628554], "amount": 5.0,
                        "win": "loose", "win_amount": 0.0,
                    }]}
                }

        class ApiHistorico:
            def __init__(self):
                self.api = ApiBaixoNivel()

        mercado = MercadoIQ(Configuracao())
        mercado._api = ApiHistorico()
        self.assertEqual(
            mercado.consultar_resultado("14217628554", timeout_segundos=0.2),
            "loose",
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
