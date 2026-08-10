"""Testa que imprimir_relatorio inclui aviso de multiplicidade."""

import io
import sys
import unittest

import pandas as pd

from iqoption_m5 import backtest


def _df_com_n_setups(n: int) -> pd.DataFrame:
    """Cria um DataFrame com N setups distintos, cada um com 10 operações."""
    linhas = []
    hora_base = pd.Timestamp("2026-01-01 10:00:00")
    for i in range(n):
        setup = f"setup_{i}"
        for j in range(10):
            linhas.append({
                "ativo": "EURUSD",
                "direcao": "call",
                "setup": setup,
                "fatores": "rsi",
                "hora_entrada": hora_base + pd.Timedelta(minutes=5 * (i * 10 + j)),
                "resultado": "ganho" if j % 2 == 0 else "perda",
                "hora_dia": 10,
            })
    return pd.DataFrame(linhas)


class TestMultiplicidade(unittest.TestCase):
    def _capturar_relatorio(self, df: pd.DataFrame, payout: float = 0.85) -> str:
        buf = io.StringIO()
        stdout_original = sys.stdout
        sys.stdout = buf
        try:
            backtest.imprimir_relatorio(df, payout)
        finally:
            sys.stdout = stdout_original
        return buf.getvalue()

    def test_relatorio_menciona_numero_de_estrategias(self):
        df = _df_com_n_setups(3)
        saida = self._capturar_relatorio(df)
        self.assertIn("3", saida)
        self.assertIn("estratégia", saida.lower())

    def test_relatorio_menciona_bonferroni(self):
        df = _df_com_n_setups(4)
        saida = self._capturar_relatorio(df)
        self.assertIn("Bonferroni", saida)

    def test_alpha_ajustado_correto_para_5_setups(self):
        df = _df_com_n_setups(5)
        saida = self._capturar_relatorio(df)
        # α ajustado = 0.05 / 5 = 0.0100
        self.assertIn("0.0100", saida)

    def test_df_vazio_nao_imprime_multiplicidade(self):
        df = backtest.para_dataframe([])
        saida = self._capturar_relatorio(df)
        # Com df vazio não há setups — não deve aparecer o bloco
        self.assertNotIn("Multiplicidade", saida)

    def test_1_estrategia_alpha_e_0_0500(self):
        df = _df_com_n_setups(1)
        saida = self._capturar_relatorio(df)
        # α = 0.05 / 1 = 0.0500
        self.assertIn("0.0500", saida)


if __name__ == "__main__":
    unittest.main()
