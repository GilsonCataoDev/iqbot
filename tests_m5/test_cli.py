import unittest
from pathlib import Path

from iqoption_m5.modelos import EstadoPersistido
from iqoption_m5.risco import GerenciadorRisco
from iqoption_m5.config import configuracao_scalping_h1, configuracao_scalping_m15
from rodar_iqoption_m5 import analisar_argumentos, selecionar_configuracao


class TestCliSegura(unittest.TestCase):
    def test_iniciador_m15_exige_confirmacao_explicita(self):
        # Renomeado de TESTAR_M1_E_M15_PRACTICE.bat em 27/08/2026: o M1 esta
        # pausado por edge negativo, entao o nome antigo prometia o que o
        # arquivo nao faz.
        iniciador = (
            Path(__file__).resolve().parents[1] / "TESTAR_M15_PRACTICE.bat"
        ).read_text(encoding="utf-8")

        self.assertIn("set /p CONFIRMA=", iniciador)
        self.assertIn('if /I not "%CONFIRMA%"=="SIM"', iniciador)

    def test_iniciador_m15_barra_bot_duplicado(self):
        """Duas sessoes na mesma conta IQ colidem no WebSocket e travam as duas.

        Nao usar wmic: foi removido do Windows 11 e a guarda vira no-op
        silencioso (o `for /f` itera zero vezes e o bat segue direto).
        """
        iniciador = (
            Path(__file__).resolve().parents[1] / "TESTAR_M15_PRACTICE.bat"
        ).read_text(encoding="utf-8")

        self.assertIn("Get-CimInstance Win32_Process", iniciador)
        self.assertIn("rodar_iqoption_m5|rodar_swing_practice", iniciador)
        self.assertIn("if errorlevel 1", iniciador)
        # 'wmic' aparece so no comentario que explica por que nao usa-lo;
        # o que nao pode existir e a invocacao.
        self.assertNotIn("wmic process", iniciador.lower())

    def test_iniciador_m5_usa_practice_confirmado(self):
        iniciador = (Path(__file__).resolve().parents[1] / "INICIAR_IQ_M5.bat").read_text(
            encoding="utf-8"
        )

        self.assertIn("--practice --confirmo", iniciador)
        self.assertIn('set /p CONFIRMA=', iniciador)

    def test_sem_perfil_fica_somente_monitor(self):
        config = selecionar_configuracao(analisar_argumentos([]))
        self.assertFalse(config.executar_ordens)
        self.assertEqual(len(config.ativos), 10)
        self.assertTrue(config.executar_estrategias_nao_validadas)

    def test_practice_exige_confirmacao(self):
        with self.assertRaises(SystemExit):
            selecionar_configuracao(analisar_argumentos(["--practice"]))

    def test_practice_confirmado_ativa_ordens(self):
        config = selecionar_configuracao(
            analisar_argumentos(["--practice", "--confirmo"])
        )
        self.assertTrue(config.executar_ordens)
        self.assertEqual(config.conta, "PRACTICE")

    def test_scalping_m1_practice_nao_limita_quantidade_de_operacoes(self):
        config = selecionar_configuracao(
            analisar_argumentos(["--scalping-m1-practice", "--confirmo"])
        )

        self.assertEqual(config.conta, "PRACTICE")
        self.assertEqual(config.max_operacoes_dia, 0)
        self.assertEqual(config.meta_diaria, 0.0)
        self.assertFalse(config.parar_por_prejuizo)
        self.assertFalse(config.parar_por_perdas)
        self.assertEqual(config.circuit_breaker_max_perdas, 0)

    def test_scalping_m15_practice_nao_limita_quantidade_de_operacoes(self):
        config = selecionar_configuracao(
            analisar_argumentos(["--scalping-m15-practice", "--confirmo"])
        )

        self.assertEqual(config.conta, "PRACTICE")
        self.assertEqual(config.max_operacoes_dia, 0)
        self.assertEqual(config.meta_diaria, 0.0)
        self.assertFalse(config.parar_por_prejuizo)
        self.assertFalse(config.parar_por_perdas)
        self.assertEqual(config.circuit_breaker_max_perdas, 0)

    def test_ema921_intravela_m5_e_m15_sao_perfis_isolados(self):
        for opcao, timeframe, janela in (
            ("--ema921-rsi-intravela-m5-practice", 300, 240),
            ("--ema921-rsi-intravela-m15-practice", 900, 720),
        ):
            config = selecionar_configuracao(analisar_argumentos([opcao, "--confirmo"]))
            self.assertTrue(config.ema921_rsi_intravela_ativo)
            self.assertFalse(config.ema921_rsi_pullback_ativo)
            self.assertTrue(config.entrada_intracandle_por_toque_ativo)
            self.assertEqual(config.timeframe_segundos, timeframe)
            self.assertEqual(config.janela_entrada_por_setup["ema921_rsi_intravela"], janela)
            self.assertEqual(config.expiracao_por_setup["ema921_rsi_intravela"], 0)
            self.assertEqual(config.ativos, ("EURUSD", "AUDCAD", "NZDUSD"))

    def test_m15_practice_nao_bloqueia_pelo_prejuizo_historico(self):
        config = selecionar_configuracao(
            analisar_argumentos(["--scalping-m15-practice", "--confirmo"])
        )
        estado = EstadoPersistido(
            operacoes_enviadas=18,
            operacoes_finalizadas=18,
            lucro_sessao=-31.50,
            lucro_total=-31.50,
        )

        resumo = GerenciadorRisco(config, estado).resumo()

        self.assertFalse(resumo.encerrado)
        self.assertIsNone(resumo.motivo_encerramento)

    def test_m15_e_h1_operam_practice_e_liberam_noticia_confirmada(self):
        for config in (configuracao_scalping_m15(), configuracao_scalping_h1()):
            self.assertEqual(config.conta, "PRACTICE")
            self.assertFalse(config.confirmo_conta_real)
            self.assertTrue(config.bloquear_noticia_alto_impacto)
            self.assertTrue(config.permitir_noticia_confirmada_a_favor)
            self.assertTrue(config.noticia_confirmada_ativo)
            self.assertEqual(config.filtro_candle_entrada_atr, 0.35)

    def test_perfis_conflitantes_sao_rejeitados(self):
        with self.assertRaises(SystemExit):
            selecionar_configuracao(
                analisar_argumentos(["--practice", "--real", "--confirmo"])
            )


if __name__ == "__main__":
    unittest.main()
