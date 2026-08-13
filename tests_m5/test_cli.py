import unittest

from rodar_iqoption_m5 import analisar_argumentos, selecionar_configuracao


class TestCliSegura(unittest.TestCase):
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

    def test_perfis_conflitantes_sao_rejeitados(self):
        with self.assertRaises(SystemExit):
            selecionar_configuracao(
                analisar_argumentos(["--practice", "--real", "--confirmo"])
            )


if __name__ == "__main__":
    unittest.main()
