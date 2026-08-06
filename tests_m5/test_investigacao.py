import unittest

import pandas as pd

from iqoption_m5.investigacao import Candidato, purgar_sobrepostas, resultados, sinais


class TestInvestigacaoEstrategias(unittest.TestCase):
    def setUp(self):
        self.indice = pd.date_range("2026-01-01", periods=80, freq="5min")
        close = pd.Series([100 + numero * 0.1 for numero in range(80)], index=self.indice)
        self.candles = pd.DataFrame(
            {
                "Open": close - 0.05,
                "High": close + 0.1,
                "Low": close - 0.1,
                "Close": close,
                "Volume": 1.0,
            },
            index=self.indice,
        )

    def test_sinal_no_fechamento_nao_muda_quando_o_futuro_muda(self):
        candidato = Candidato(
            "impulso", {"janela": 3, "limiar_atr": 0.5, "expiracao": 1}
        )
        antes = sinais(self.candles, candidato).iloc[40]
        alterado = self.candles.copy()
        alterado.iloc[41:, alterado.columns.get_loc("Close")] *= 2

        depois = sinais(alterado, candidato).iloc[40]

        self.assertEqual(antes, depois)

    def test_expiracao_tres_compara_abertura_seguinte_com_terceiro_fechamento(self):
        direcao = pd.Series(0, index=self.indice, dtype="int8")
        direcao.iloc[10] = 1
        self.candles.iloc[11, self.candles.columns.get_loc("Open")] = 100.0
        self.candles.iloc[11, self.candles.columns.get_loc("Close")] = 99.0
        self.candles.iloc[13, self.candles.columns.get_loc("Close")] = 101.0

        operacoes = resultados(self.candles, direcao, expiracao=3)

        self.assertEqual(int(operacoes.iloc[0]["ganho"]), 1)


class TestPurgaDeSobreposicao(unittest.TestCase):
    def setUp(self):
        self.indice = pd.date_range("2026-01-01", periods=40, freq="5min")
        self.candles = pd.DataFrame({"Close": 100.0}, index=self.indice)

    def _dados(self, posicoes):
        return pd.DataFrame({"ganho": 1}, index=self.indice[posicoes])

    def test_expiracao_um_nao_descarta_nada(self):
        """Com expiração de uma vela não há sobreposição possível."""
        dados = self._dados([0, 1, 2, 3])
        purgado = purgar_sobrepostas(dados, self.candles, expiracao=1)
        self.assertEqual(len(purgado), 4)

    def test_sinais_seguidos_viram_uma_operacao_por_expiracao(self):
        dados = self._dados(list(range(12)))
        purgado = purgar_sobrepostas(dados, self.candles, expiracao=6)
        self.assertEqual(list(self.candles.index.get_indexer(purgado.index)), [0, 6])

    def test_sinais_ja_espacados_sao_mantidos(self):
        dados = self._dados([0, 10, 20])
        purgado = purgar_sobrepostas(dados, self.candles, expiracao=6)
        self.assertEqual(len(purgado), 3)

    def test_mantem_o_sinal_mais_antigo_do_grupo(self):
        """Escolher pelo tempo, nunca pelo resultado, evita viés de seleção."""
        dados = self._dados([4, 5, 6])
        purgado = purgar_sobrepostas(dados, self.candles, expiracao=6)
        self.assertEqual(list(self.candles.index.get_indexer(purgado.index)), [4])

    def test_dados_vazios_nao_quebram(self):
        vazio = pd.DataFrame({"ganho": []}, index=pd.DatetimeIndex([]))
        self.assertTrue(purgar_sobrepostas(vazio, self.candles, expiracao=6).empty)


if __name__ == "__main__":
    unittest.main()
