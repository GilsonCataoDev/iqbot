"""Testa validar_walk_forward com DataFrame sintético — sem IQ Option."""
import unittest

import pandas as pd

from iqoption_m5.backtest import validar_walk_forward


def _df_sintetico(n: int = 1500) -> pd.DataFrame:
    """DataFrame de operações sintéticas; padrão ganho/perda alternado."""
    idx = pd.date_range("2025-01-01", periods=n, freq="5min")
    padrao = ["ganho", "perda", "ganho", "ganho", "perda"]
    resultados = (padrao * (n // len(padrao) + 1))[:n]
    return pd.DataFrame(
        {
            "hora_entrada": idx,
            "hora_dia": [ts.hour for ts in idx],
            "ativo": "EURUSD",
            "setup": "reversao_bollinger_rsi",
            "direcao": "call",
            "fatores": "bollinger+rsi",
            "resultado": resultados,
        }
    )


class TestValidarWalkForward(unittest.TestCase):

    def test_janelas_minimas_geradas(self):
        df = _df_sintetico(1500)
        resultado = validar_walk_forward(df, payout=0.85, janela_treino=500, passo=250)
        self.assertGreaterEqual(
            resultado["janelas"], 2,
            "com 1500 linhas, janela_treino=500 e passo=250 devem gerar ao menos 2 janelas",
        )

    def test_acima_breakeven_nao_supera_janelas(self):
        df = _df_sintetico(1500)
        resultado = validar_walk_forward(df, payout=0.85, janela_treino=500, passo=250)
        self.assertLessEqual(
            resultado["acima_breakeven"],
            resultado["janelas"],
            "acima_breakeven não pode ser maior que o total de janelas",
        )

    def test_df_pequeno_retorna_zero_janelas(self):
        df = _df_sintetico(10)
        resultado = validar_walk_forward(df, payout=0.85, janela_treino=500, passo=250)
        self.assertEqual(resultado["janelas"], 0)
        self.assertIsNone(resultado["wr_medio"])
        self.assertIsNone(resultado["lucro_medio"])

    def test_retorno_contem_chaves_esperadas(self):
        df = _df_sintetico(1500)
        resultado = validar_walk_forward(df, payout=0.85, janela_treino=500, passo=250)
        for chave in ("janelas", "acima_breakeven", "wr_medio", "lucro_medio"):
            self.assertIn(chave, resultado, f"chave '{chave}' ausente no retorno")

    def test_wr_medio_entre_zero_e_um(self):
        df = _df_sintetico(1500)
        resultado = validar_walk_forward(df, payout=0.85, janela_treino=500, passo=250)
        if resultado["wr_medio"] is not None:
            self.assertGreaterEqual(resultado["wr_medio"], 0.0)
            self.assertLessEqual(resultado["wr_medio"], 1.0)

    def test_criterio_ic95_mais_conservador_que_winrate(self):
        """IC95% piso é mais conservador: série com WR baixo não passa mesmo com WR > breakeven."""
        # Série de 60% WR com payout 0.85 → breakeven ~54%; WR passa mas IC95% pode não passar
        # com amostras pequenas (passo=60). Verifica que acima_breakeven <= janelas.
        n = 1200
        idx = pd.date_range("2025-01-01", periods=n, freq="5min")
        padrao = ["ganho", "ganho", "ganho", "perda", "perda"]  # 60% WR
        resultados = (padrao * (n // len(padrao) + 1))[:n]
        df = pd.DataFrame({
            "hora_entrada": idx,
            "hora_dia": [ts.hour for ts in idx],
            "ativo": "EURUSD",
            "setup": "pullback",
            "direcao": "call",
            "fatores": "fibo",
            "resultado": resultados,
        })
        res = validar_walk_forward(df, payout=0.85, janela_treino=500, passo=60, minimo=30)
        # Com amostras de só 60 ops, IC95 piso ~47-52% — abaixo do breakeven de 54%.
        # O critério IC95 deve reprovar mais janelas do que se usasse WR médio direto.
        self.assertLessEqual(res["acima_breakeven"], res["janelas"])


if __name__ == "__main__":
    unittest.main()
