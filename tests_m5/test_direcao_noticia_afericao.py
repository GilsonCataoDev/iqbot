"""Mede se a direção mecânica da notícia acerta.

É aposta direcional pura, não TP/SL: só compara o fechamento do horizonte com
o preço no momento da publicação. Fica separada das amostras dos estudos
porque método diferente não se soma.
"""
from __future__ import annotations

import threading
import unittest

import pandas as pd

import monitor_mercado as M

NOTICIA = {
    "estado": "resultado_publicado", "texto": "CPI m/m", "moeda": "USD",
    "actual": "0.5", "forecast": "0.2", "desvio_pct": 150.0, "direcao": "PUT",
}


def _estado():
    e = M.Estado.__new__(M.Estado)
    e._lock = threading.Lock()
    e._vistos = set()
    e._aprendizado = []
    e._salvar_aprendizado = lambda: None
    return e


def _velas(precos, inicio="2026-09-11 12:30"):
    idx = pd.date_range(inicio, periods=len(precos), freq="15min", tz="UTC")
    return pd.DataFrame({"Close": precos}, index=idx), idx


class TestRegistro(unittest.TestCase):
    def setUp(self):
        self.e = _estado()
        _, self.idx = _velas([0] * 6)

    def test_registra_quando_o_numero_sai(self):
        self.e.registrar_direcao_noticia("XAUUSD", str(self.idx[0]), 4400.0, NOTICIA)
        self.assertEqual(len(self.e._aprendizado), 1)
        item = self.e._aprendizado[0]
        self.assertEqual(item["tipo"], "direcao_noticia")
        self.assertEqual(item["direcao_prevista"], "PUT")
        self.assertEqual(item["horizonte_velas"], M.HORIZONTE_NOTICIA_VELAS)

    def test_nao_registra_sem_resultado_publicado(self):
        self.e.registrar_direcao_noticia(
            "XAUUSD", str(self.idx[0]), 4400.0, {"estado": "janela_risco"}
        )
        self.assertEqual(self.e._aprendizado, [])

    def test_nao_registra_sem_direcao(self):
        sem = {**NOTICIA, "direcao": None}
        self.e.registrar_direcao_noticia("XAUUSD", str(self.idx[0]), 4400.0, sem)
        self.assertEqual(self.e._aprendizado, [])

    def test_nao_duplica_o_mesmo_evento(self):
        for _ in range(3):
            self.e.registrar_direcao_noticia("XAUUSD", str(self.idx[0]), 4400.0, NOTICIA)
        self.assertEqual(len(self.e._aprendizado), 1)


class TestAfericao(unittest.TestCase):
    def setUp(self):
        self.e = _estado()

    def _rodar(self, precos, direcao="PUT"):
        df, idx = _velas(precos)
        self.e.registrar_direcao_noticia(
            "XAUUSD", str(idx[0]), precos[0], {**NOTICIA, "direcao": direcao}
        )
        self.e.resolver_direcoes_noticia("XAUUSD", df)
        return self.e._aprendizado[0]["afericao"]

    def test_put_acerta_quando_cai(self):
        a = self._rodar([4400, 4398, 4395, 4390, 4385, 4380])
        self.assertEqual(a["estado"], "aferido")
        self.assertTrue(a["acertou"])

    def test_put_erra_quando_sobe(self):
        a = self._rodar([4400, 4402, 4405, 4410, 4415, 4420])
        self.assertFalse(a["acertou"])

    def test_call_acerta_quando_sobe(self):
        a = self._rodar([4400, 4402, 4405, 4410, 4415, 4420], direcao="CALL")
        self.assertTrue(a["acertou"])

    def test_empate_nao_vira_erro(self):
        """Resultado indefinido nunca pode ser contado como perda."""
        a = self._rodar([4400, 4401, 4402, 4401, 4400, 4399])
        self.assertEqual(a["estado"], "empate")
        self.assertIsNone(a["acertou"])

    def test_fica_aguardando_sem_vela_do_horizonte(self):
        df, idx = _velas([4400, 4398])  # horizonte de 4 velas nao chegou
        self.e.registrar_direcao_noticia("XAUUSD", str(idx[0]), 4400.0, NOTICIA)
        self.e.resolver_direcoes_noticia("XAUUSD", df)
        self.assertEqual(self.e._aprendizado[0]["afericao"]["estado"], "aguardando")

    def test_usa_o_horizonte_gravado_no_registro(self):
        """Mudar a constante depois nao pode reinterpretar medicao antiga."""
        df, idx = _velas([4400, 4390, 4380, 4370, 4360, 4350])
        self.e.registrar_direcao_noticia("XAUUSD", str(idx[0]), 4400.0, NOTICIA)
        self.e._aprendizado[0]["horizonte_velas"] = 2
        self.e.resolver_direcoes_noticia("XAUUSD", df)
        a = self.e._aprendizado[0]["afericao"]
        self.assertEqual(a["preco_final"], 4380.0, "tem de usar a vela 2, nao a 4")


class TestAmostra(unittest.TestCase):
    def test_conta_acertos_e_erros_sem_os_empates(self):
        e = _estado()
        e._aprendizado = [
            {"tipo": "direcao_noticia", "afericao": {"estado": "aferido", "acertou": True}},
            {"tipo": "direcao_noticia", "afericao": {"estado": "aferido", "acertou": True}},
            {"tipo": "direcao_noticia", "afericao": {"estado": "aferido", "acertou": False}},
            {"tipo": "direcao_noticia", "afericao": {"estado": "empate", "acertou": None}},
            {"tipo": "direcao_noticia", "afericao": {"estado": "aguardando", "acertou": None}},
        ]
        r = e._amostra_direcao_noticia()
        self.assertEqual((r["acertos"], r["erros"]), (2, 1))
        self.assertEqual(r["acerto_pct"], 66.7)
        self.assertEqual((r["empates"], r["pendentes"]), (1, 1))
        self.assertEqual(r["maturidade"], "INSUFICIENTE")

    def test_nao_recolhe_os_estudos_de_tp_sl(self):
        e = _estado()
        e._aprendizado = [
            {"tipo": "fibo_m15", "simulacao": {"desfecho": "win_tp1"}},
            {"tipo": "direcao_noticia", "afericao": {"estado": "aferido", "acertou": True}},
        ]
        self.assertEqual(e._amostra_direcao_noticia()["sinais"], 1)

    def test_sem_registro_nao_quebra(self):
        r = _estado()._amostra_direcao_noticia()
        self.assertEqual(r["sinais"], 0)
        self.assertIsNone(r["acerto_pct"])
        self.assertEqual(r["maturidade"], "INSUFICIENTE")


if __name__ == "__main__":
    unittest.main()
