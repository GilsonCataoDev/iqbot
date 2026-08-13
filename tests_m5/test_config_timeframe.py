import unittest
from dataclasses import replace

from iqoption_m5.config import (
    Configuracao,
    configuracao_m1,
    configuracao_pesquisa_m5,
    configuracao_practice_m5,
)


class TestPerfilM5(unittest.TestCase):
    def test_padrao_continua_m5(self):
        config = Configuracao()
        config.validar()
        self.assertEqual(config.timeframe_segundos, 300)
        self.assertEqual(config.rotulo_timeframe, "M5")

    def test_padrao_e_somente_monitor(self):
        config = Configuracao()
        self.assertFalse(config.executar_ordens)
        self.assertFalse(config.executar_estrategias_nao_validadas)
        self.assertEqual(config.max_operacoes_dia, 5)
        self.assertEqual(config.max_perdas_consecutivas, 3)

    def test_practice_exige_perfil_explicito_para_enviar_ordens(self):
        config = configuracao_practice_m5()
        self.assertTrue(config.executar_ordens)
        self.assertEqual(config.conta, "PRACTICE")

    def test_pesquisa_mantem_amostra_ampla_sem_enviar_ordens(self):
        config = configuracao_pesquisa_m5()
        self.assertFalse(config.executar_ordens)
        self.assertEqual(config.max_operacoes_dia, 0)
        self.assertIsNone(config.horario_bloqueado)
        self.assertIsNone(config.horario_por_setup)
        self.assertEqual(len(config.ativos), 10)
        self.assertTrue(config.executar_estrategias_nao_validadas)
        self.assertTrue(config.engulfing_sr_ativo)
        self.assertTrue(config.divergencia_rsi_ativo)
        self.assertTrue(config.bollinger_squeeze_ativo)
        self.assertTrue(config.pin_bar_sr_ativo)
        self.assertTrue(config.pullback_ativo)

    def test_pesquisa_remove_horarios_herdados_da_base(self):
        base = replace(
            Configuracao(),
            horario_bloqueado=((22, 0), (7, 0)),
            horario_por_setup={"sr_rejeicao": (7, 20)},
        )

        config = configuracao_pesquisa_m5(base)

        self.assertIsNone(config.horario_bloqueado)
        self.assertIsNone(config.horario_por_setup)


class TestPerfilM1(unittest.TestCase):
    def setUp(self):
        self.config = configuracao_m1()

    def test_m1_e_valido(self):
        self.config.validar()
        self.assertEqual(self.config.rotulo_timeframe, "M1")

    def test_expiracao_acompanha_o_candle(self):
        self.assertEqual(self.config.expiracao_minutos, 1)

    def test_janela_de_entrada_encolhe(self):
        """15s do M5 seriam um quarto de um candle M1."""
        self.assertLess(self.config.entrada_max_segundos_no_candle, 15)
        self.assertLess(self.config.entrada_max_segundos_no_candle, self.config.timeframe_segundos)

    def test_usa_porta_diferente_do_m5(self):
        self.assertNotEqual(self.config.porta_grafico, Configuracao().porta_grafico)

    def test_historico_cobre_os_indicadores(self):
        minimo = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3
        self.assertGreaterEqual(self.config.limite_candles, minimo)

    def test_preserva_as_protecoes_da_base(self):
        self.assertEqual(self.config.conta, "PRACTICE")
        self.assertEqual(self.config.max_operacoes_dia, Configuracao().max_operacoes_dia)


class TestValidacaoRejeitaConfiguracaoRuim(unittest.TestCase):
    def test_timeframe_nao_suportado(self):
        with self.assertRaises(RuntimeError):
            replace(Configuracao(), timeframe_segundos=180).validar()

    def test_conta_real_continua_bloqueada(self):
        with self.assertRaises(RuntimeError):
            replace(Configuracao(), conta="REAL").validar()

    def test_expiracao_menor_que_o_candle(self):
        with self.assertRaises(RuntimeError):
            replace(Configuracao(), expiracao_minutos=1).validar()

    def test_janela_de_entrada_maior_que_o_candle(self):
        with self.assertRaises(RuntimeError):
            replace(configuracao_m1(), entrada_max_segundos_no_candle=90).validar()

    def test_historico_insuficiente_para_os_indicadores(self):
        with self.assertRaises(RuntimeError):
            replace(Configuracao(), limite_candles=20).validar()


if __name__ == "__main__":
    unittest.main()
