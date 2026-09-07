"""Sombra é observação: não pode parar quando a IQ diz que o mercado fechou.

_resolver_sombras só lê candle e grava resultado. Ficava depois do portão que
decide se dá para ENVIAR ORDEM, então a amostra parava de crescer sempre que o
estado de abertura vinha errado — com candles chegando normalmente.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

FONTE = Path(__file__).resolve().parent.parent / "iqoption_m5" / "laboratorio_ema.py"


class TestOrdemDoPortaoDeSombra(unittest.TestCase):
    def setUp(self):
        self.codigo = FONTE.read_text(encoding="utf-8")

    def test_resolver_sombras_vem_antes_do_portao_de_mercado(self):
        sombra = self.codigo.index("_resolver_sombras(registro, snapshot)")
        portao = self.codigo.index("if not snapshot.mercado_aberto:")
        self.assertLess(
            sombra, portao,
            "resolver sombra depois do portao faz a amostra parar junto com o "
            "flag de abertura da IQ, que erra com frequencia",
        )

    def test_portao_de_mercado_continua_existindo(self):
        """A correcao nao pode ter aberto caminho para enviar ordem."""
        trecho = self.codigo[self.codigo.index("if not snapshot.mercado_aberto:"):]
        self.assertRegex(trecho[:120], r"if not snapshot\.mercado_aberto:\s*\n\s*continue")


class TestIntravelaDisparadoLimitado(unittest.TestCase):
    def setUp(self):
        self.codigo = FONTE.read_text(encoding="utf-8")

    def test_nao_e_set_que_cresce_sem_limite(self):
        self.assertNotIn("intravela_disparado: set[", self.codigo)
        self.assertIn("intravela_disparado: dict[tuple[str, str], object]", self.codigo)

    def test_chave_nao_inclui_o_candle(self):
        """Chave por (rastro, ativo): o candle vai no valor, entao o dict nao cresce."""
        self.assertNotIn("intravela_disparado.add(", self.codigo)
        self.assertIn("intravela_disparado[(rastro.nome, ativo)]", self.codigo)
        self.assertIn("intravela_disparado.get((rastro.nome, ativo))", self.codigo)


class TestSemanticaEquivalente(unittest.TestCase):
    """O dict precisa se comportar como o set para o unico uso que existe:
    'este (rastro, ativo) ja disparou no candle ATUAL?'"""

    def test_dict_responde_igual_ao_set_para_o_candle_atual(self):
        disparado: dict[tuple[str, str], object] = {}
        chave = ("m5", "EURUSD")

        self.assertNotEqual(disparado.get(chave), "candle_1")
        disparado[chave] = "candle_1"
        self.assertEqual(disparado.get(chave), "candle_1")   # bloqueia repeticao
        self.assertNotEqual(disparado.get(chave), "candle_2")  # candle novo libera

        disparado[chave] = "candle_2"
        self.assertEqual(len(disparado), 1, "nao acumula candles passados")

    def test_ativos_diferentes_nao_se_bloqueiam(self):
        disparado: dict[tuple[str, str], object] = {}
        disparado[("m5", "EURUSD")] = "c1"
        self.assertNotEqual(disparado.get(("m5", "GBPUSD")), "c1")
        self.assertNotEqual(disparado.get(("m15", "EURUSD")), "c1")


if __name__ == "__main__":
    unittest.main()
