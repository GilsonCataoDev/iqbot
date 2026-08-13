import unittest

import pandas as pd

from iqoption_m5.investigacao import (
    Candidato,
    _niveis_pivos_confirmados,
    candidatos,
    purgar_sobrepostas,
    resultados,
    sinais,
)


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

    def test_grade_inclui_as_familias_experimentais(self):
        familias = {candidato.familia for candidato in candidatos("EURUSD")}

        self.assertIn("rompimento_forte", familias)
        self.assertIn("extremo_rejeicao", familias)
        self.assertIn("topo_fundo_rejeicao", familias)
        self.assertIn("topo_fundo_rompimento", familias)
        self.assertIn("topo_fundo_pullback", familias)
        self.assertIn("correcao_fibo_sr_binaria", familias)

    def test_pivo_so_fica_disponivel_apos_candles_da_direita(self):
        candles = self.candles.copy()
        pivo = 30
        candles.iloc[pivo, candles.columns.get_loc("Low")] = 90.0

        suporte, _ = _niveis_pivos_confirmados(candles, raio=2)

        self.assertNotEqual(suporte.iloc[pivo], 90.0)
        self.assertNotEqual(suporte.iloc[pivo + 1], 90.0)
        self.assertEqual(float(suporte.iloc[pivo + 2]), 90.0)

    def _candles_topo_fundo(self):
        indice = pd.date_range("2026-01-01", periods=90, freq="5min")
        close = pd.Series(100.0, index=indice)
        variacao = pd.Series(range(len(indice)), index=indice) * 0.0001
        candles = pd.DataFrame(
            {
                "Open": close - 0.02,
                "High": close + 0.10 + variacao,
                "Low": close - 0.10 + variacao,
                "Close": close,
                "Volume": 1.0,
            },
            index=indice,
        )
        candles.iloc[30, candles.columns.get_loc("Low")] = 99.50
        candles.iloc[30, candles.columns.get_loc("High")] = 100.05
        candles.iloc[40, candles.columns.get_loc("High")] = 100.50
        candles.iloc[40, candles.columns.get_loc("Low")] = 99.95
        return candles

    def _candidato_topo_fundo(self, familia):
        return Candidato(
            familia,
            {
                "raio": 2,
                "tolerancia_atr": 0.15,
                "corpo_min_atr": 0.30,
                "fechamento_extremo": 0.25,
                "janela_reteste": 6,
                "expiracao": 1,
            },
        )

    def test_topo_fundo_rejeicao_no_suporte_gera_call(self):
        candles = self._candles_topo_fundo()
        alvo = candles.index[55]
        candles.loc[alvo, ["Open", "High", "Low", "Close"]] = [99.62, 99.86, 99.48, 99.82]

        direcao = sinais(candles, self._candidato_topo_fundo("topo_fundo_rejeicao"))

        self.assertEqual(int(direcao.loc[alvo]), 1)

    def test_topo_fundo_rompimento_forte_gera_call(self):
        candles = self._candles_topo_fundo()
        alvo = candles.index[55]
        candles.loc[alvo, ["Open", "High", "Low", "Close"]] = [100.35, 100.72, 100.30, 100.68]

        direcao = sinais(candles, self._candidato_topo_fundo("topo_fundo_rompimento"))

        self.assertEqual(int(direcao.loc[alvo]), 1)

    def test_topo_fundo_pullback_apos_rompimento_gera_call(self):
        candles = self._candles_topo_fundo()
        rompimento = candles.index[55]
        reteste = candles.index[57]
        candles.loc[rompimento, ["Open", "High", "Low", "Close"]] = [
            100.35, 100.72, 100.30, 100.68
        ]
        candles.loc[candles.index[56], ["Open", "High", "Low", "Close"]] = [
            100.68, 100.76, 100.60, 100.70
        ]
        candles.loc[reteste, ["Open", "High", "Low", "Close"]] = [
            100.52, 100.72, 100.48, 100.68
        ]

        direcao = sinais(candles, self._candidato_topo_fundo("topo_fundo_pullback"))

        self.assertEqual(int(direcao.loc[rompimento]), 0)
        self.assertEqual(int(direcao.loc[reteste]), 1)

    def test_rompimento_forte_exige_fechamento_na_extremidade(self):
        candles = self.candles.copy()
        alvo = candles.index[60]
        candles.loc[alvo, ["Open", "High", "Low", "Close"]] = [105.78, 106.15, 105.72, 106.12]
        candidato = Candidato(
            "rompimento_forte",
            {
                "janela": 12,
                "corpo_minimo": 0.55,
                "fechamento_extremo": 0.20,
                "atr_minimo": 0.80,
                "atr_maximo": 2.00,
                "expiracao": 1,
            },
        )

        direcao = sinais(candles, candidato)

        self.assertEqual(int(direcao.loc[alvo]), 1)
        candles.loc[alvo, "Close"] = 105.8
        self.assertEqual(int(sinais(candles, candidato).loc[alvo]), 0)

    def test_extremo_com_varredura_e_rejeicao_gera_call(self):
        indice = pd.date_range("2026-01-01", periods=80, freq="5min")
        variacoes = [0.04 if numero % 2 == 0 else -0.03 for numero in range(80)]
        close = pd.Series(100.0, index=indice) + pd.Series(variacoes, index=indice).cumsum()
        candles = pd.DataFrame(
            {
                "Open": close.shift(1).fillna(100.0),
                "High": close + 0.08,
                "Low": close - 0.08,
                "Close": close,
                "Volume": 1.0,
            },
            index=indice,
        )
        alvo = indice[60]
        minima_anterior = candles.loc[indice[48:60], "Low"].min()
        candles.loc[alvo, ["Open", "High", "Low", "Close"]] = [
            minima_anterior + 0.03,
            minima_anterior + 0.05,
            minima_anterior - 0.35,
            minima_anterior + 0.02,
        ]
        candidato = Candidato(
            "extremo_rejeicao",
            {
                "janela_movimento": 1,
                "janela_nivel": 12,
                "janela_volatilidade": 48,
                "limiar_sigma": 1.5,
                "pavio_corpo": 1.5,
                "expiracao": 1,
            },
        )

        self.assertEqual(int(sinais(candles, candidato).loc[alvo]), 1)

    def test_novas_familias_nao_usam_candles_futuros(self):
        for candidato in (
            Candidato(
                "rompimento_forte",
                {
                    "janela": 12,
                    "corpo_minimo": 0.55,
                    "fechamento_extremo": 0.20,
                    "atr_minimo": 0.80,
                    "atr_maximo": 2.00,
                    "expiracao": 1,
                },
            ),
            Candidato(
                "extremo_rejeicao",
                {
                    "janela_movimento": 1,
                    "janela_nivel": 12,
                    "janela_volatilidade": 48,
                    "limiar_sigma": 1.5,
                    "pavio_corpo": 1.5,
                    "expiracao": 1,
                },
            ),
            self._candidato_topo_fundo("topo_fundo_rejeicao"),
            self._candidato_topo_fundo("topo_fundo_rompimento"),
            self._candidato_topo_fundo("topo_fundo_pullback"),
            Candidato(
                "correcao_fibo_sr_binaria",
                {
                    "raio_pivo": 2,
                    "fib_min": 0.50,
                    "fib_max": 0.618,
                    "tolerancia_sr_atr": 0.25,
                    "corpo_min_atr": 0.30,
                    "impulso_min_atr": 1.5,
                    "rr_minimo": 0.0,
                    "max_candles_correcao": 12,
                    "expiracao": 1,
                },
            ),
        ):
            with self.subTest(familia=candidato.familia):
                antes = sinais(self.candles, candidato).iloc[60]
                alterado = self.candles.copy()
                alterado.iloc[61:, :] *= 3
                depois = sinais(alterado, candidato).iloc[60]
                self.assertEqual(antes, depois)


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
