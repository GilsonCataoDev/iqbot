import unittest
from pathlib import Path

from iqoption_m5.modelos import EstadoPersistido
from iqoption_m5.risco import GerenciadorRisco
from rodar_iqoption_m5 import analisar_argumentos, selecionar_configuracao


class TestCliSegura(unittest.TestCase):
    def test_iniciador_m1_m15_exige_confirmacao_explicita(self):
        iniciador = (
            Path(__file__).resolve().parents[1] / "TESTAR_M1_E_M15_PRACTICE.bat"
        ).read_text(encoding="utf-8")

        self.assertIn("set /p CONFIRMA=", iniciador)
        self.assertIn('if /I not "%CONFIRMA%"=="SIM"', iniciador)

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

    def test_perfis_conflitantes_sao_rejeitados(self):
        with self.assertRaises(SystemExit):
            selecionar_configuracao(
                analisar_argumentos(["--practice", "--real", "--confirmo"])
            )


if __name__ == "__main__":
    unittest.main()
