"""Testa o comportamento de desativação após erros consecutivos."""
import unittest

import pandas as pd

from iqoption_m5.config import Configuracao
from iqoption_m5.estrategia import EstrategiaReversaoM5


def _df_base(n=60):
    idx = pd.date_range("2026-01-01", periods=n, freq="5min")
    return pd.DataFrame(
        {
            "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
            "Volume": 1.0, "ATR": 1.0, "RSI": 50.0,
            "BandaInf": 99.0, "BandaSup": 101.0, "BandaMedia": 100.0,
            "TendenciaMacro": "lateral", "InclinacaoMacro": 0.0,
            "EMA_Micro": 100.0, "EMA_Macro": 100.0,
            "MACD": 0.0, "MACD_Signal": 0.0, "MACD_Hist": 0.0,
        },
        index=idx,
    )


class TestEstrategiaErros(unittest.TestCase):

    def setUp(self):
        self.config = Configuracao()
        self.est = EstrategiaReversaoM5(self.config)
        self.df = _df_base()

    def _substituir_por_quebrada(self, nome_metodo: str):
        """Substitui o método por uma função que lança RuntimeError e retorna a original."""
        def funcao_quebrada(ativo, df, indice):
            raise RuntimeError("erro de teste simulado")
        funcao_quebrada.__name__ = nome_metodo
        original = getattr(self.est, nome_metodo)
        setattr(self.est, nome_metodo, funcao_quebrada)
        return original

    def test_funcao_desativada_apos_3_erros(self):
        original = self._substituir_por_quebrada("_avaliar_indicadores")
        try:
            for _ in range(3):
                self.est._avaliar_todas_estrategias("EURUSD", self.df, len(self.df) - 2)
            self.assertIn(
                "_avaliar_indicadores",
                self.est._estrategias_desativadas,
                "deve estar em _estrategias_desativadas após 3 erros",
            )
        finally:
            self.est._avaliar_indicadores = original

    def test_funcao_desativada_nao_e_chamada_depois(self):
        chamadas = []

        def funcao_quebrada(ativo, df, indice):
            chamadas.append(1)
            raise RuntimeError("erro de teste simulado")
        funcao_quebrada.__name__ = "_avaliar_indicadores"

        original = self.est._avaliar_indicadores
        self.est._avaliar_indicadores = funcao_quebrada
        try:
            # 3 chamadas — desativa na terceira
            for _ in range(3):
                self.est._avaliar_todas_estrategias("EURUSD", self.df, len(self.df) - 2)
            total_antes = len(chamadas)

            # Chamada extra — função já desativada não deve ser invocada
            self.est._avaliar_todas_estrategias("EURUSD", self.df, len(self.df) - 2)
            self.assertEqual(
                len(chamadas), total_antes,
                "função desativada não deve ser chamada após desativação",
            )
        finally:
            self.est._avaliar_indicadores = original

    def test_erros_nao_consecutivos_nao_desativam(self):
        """Um erro seguido de sucesso zera o contador; não deve desativar.

        Sequência de chamadas (1-indexado):
          ímpar → sucesso (zera contador)
          par   → erro   (incrementa contador, mas nunca chega a 3 seguidos)
        Com 6 chamadas: ok, err(1), ok(0), err(1), ok(0), err(1) — nunca atinge 3.
        """
        contador = [0]

        def funcao_intermitente(ativo, df, indice):
            contador[0] += 1
            if contador[0] % 2 == 0:  # par = erro
                raise RuntimeError("falha intermitente")
            return None
        funcao_intermitente.__name__ = "_avaliar_indicadores"

        original = self.est._avaliar_indicadores
        self.est._avaliar_indicadores = funcao_intermitente
        try:
            for _ in range(6):
                self.est._avaliar_todas_estrategias("EURUSD", self.df, len(self.df) - 2)
            self.assertNotIn(
                "_avaliar_indicadores",
                self.est._estrategias_desativadas,
                "erros não consecutivos não devem desativar",
            )
        finally:
            self.est._avaliar_indicadores = original


if __name__ == "__main__":
    unittest.main()
