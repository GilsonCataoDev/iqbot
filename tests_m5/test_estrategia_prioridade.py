"""Testa a seleção por prioridade de setup em _avaliar_estrategias."""
import unittest
from unittest.mock import patch

import pandas as pd

from iqoption_m5.config import Configuracao
from iqoption_m5.estrategia import (
    EstrategiaReversaoM5,
    PRIORIDADE_SETUP,
    _PRIORIDADE_DEFAULT,
)
from iqoption_m5.modelos import Decisao


def _decisao(setup: str, hora_offset: int = 0) -> Decisao:
    return Decisao(
        ativo="EURUSD",
        direcao="call",
        preco=100.0,
        candle_hora=pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=5 * hora_offset),
        motivo="teste",
        detalhes={"setup": setup},
    )


class TestEstrategiaPrioridade(unittest.TestCase):

    def setUp(self):
        self.est = EstrategiaReversaoM5(Configuracao())

    def test_retorna_setup_de_menor_prioridade_numerica(self):
        """pullback_confluencia (1) deve vencer macd_crossover (11) e sr_rejeicao (5)."""
        decisoes = [
            _decisao("macd_crossover", hora_offset=0),
            _decisao("pullback_confluencia", hora_offset=1),
            _decisao("sr_rejeicao", hora_offset=2),
        ]
        with patch.object(self.est, "_avaliar_todas_estrategias", return_value=decisoes):
            resultado = self.est._avaliar_estrategias("EURUSD", pd.DataFrame(), 0)
        self.assertIsNotNone(resultado)
        self.assertEqual(resultado.detalhes["setup"], "pullback_confluencia")

    def test_desempate_por_candle_hora(self):
        """Dois setups de mesma prioridade: o mais antigo (menor hora) vence."""
        d1 = _decisao("sr_rejeicao", hora_offset=0)   # hora mais antiga
        d2 = _decisao("sr_rejeicao", hora_offset=2)   # hora mais recente
        with patch.object(self.est, "_avaliar_todas_estrategias", return_value=[d2, d1]):
            resultado = self.est._avaliar_estrategias("EURUSD", pd.DataFrame(), 0)
        self.assertEqual(resultado.candle_hora, d1.candle_hora)

    def test_setup_desconhecido_usa_prioridade_default(self):
        nome = "setup_que_nao_existe_no_dict"
        self.assertNotIn(nome, PRIORIDADE_SETUP)
        prioridade = PRIORIDADE_SETUP.get(nome, _PRIORIDADE_DEFAULT)
        self.assertEqual(prioridade, _PRIORIDADE_DEFAULT)

    def test_setup_desconhecido_perde_para_setup_conhecido(self):
        """Setup desconhecido (prioridade 99) deve perder para qualquer setup do dict."""
        decisoes = [
            _decisao("setup_desconhecido", hora_offset=0),
            _decisao("reversao_candle", hora_offset=1),  # prioridade 12, mas < 99
        ]
        with patch.object(self.est, "_avaliar_todas_estrategias", return_value=decisoes):
            resultado = self.est._avaliar_estrategias("EURUSD", pd.DataFrame(), 0)
        self.assertEqual(resultado.detalhes["setup"], "reversao_candle")

    def test_retorna_none_se_lista_vazia(self):
        with patch.object(self.est, "_avaliar_todas_estrategias", return_value=[]):
            resultado = self.est._avaliar_estrategias("EURUSD", pd.DataFrame(), 0)
        self.assertIsNone(resultado)


if __name__ == "__main__":
    unittest.main()
