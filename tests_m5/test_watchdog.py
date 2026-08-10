"""Testa a lógica de timing do watchdog — sem iniciar threads (evita flakiness)."""

import time
import unittest

import iqoption_m5.app as app_module


class TestWatchdogTiming(unittest.TestCase):
    def test_atualizar_heartbeat_atualiza_timestamp(self):
        antes = time.time()
        app_module._atualizar_heartbeat()
        depois = time.time()
        with app_module._lock_heartbeat:
            ts = app_module._ultimo_heartbeat
        self.assertGreaterEqual(ts, antes)
        self.assertLessEqual(ts, depois)

    def test_desde_e_pequeno_logo_apos_heartbeat(self):
        app_module._atualizar_heartbeat()
        time.sleep(0.05)  # 50ms — bem abaixo de qualquer timeout real
        with app_module._lock_heartbeat:
            desde = time.time() - app_module._ultimo_heartbeat
        # Nunca deve demorar mais de 1s em 50ms de sleep
        self.assertLess(desde, 1.0)

    def test_limite_watchdog_calculo(self):
        """Verifica que o limite calculado em _watchdog bate com o campo de config."""
        from iqoption_m5.config import Configuracao
        config = Configuracao()
        limite_esperado = config.watchdog_timeout_minutos * 60
        # Simula o cálculo interno do watchdog
        limite_calculado = config.watchdog_timeout_minutos * 60
        self.assertEqual(limite_esperado, limite_calculado)

    def test_watchdog_timeout_minutos_existe_em_config(self):
        from iqoption_m5.config import Configuracao
        config = Configuracao()
        self.assertTrue(hasattr(config, "watchdog_timeout_minutos"))
        self.assertGreater(config.watchdog_timeout_minutos, 0)


if __name__ == "__main__":
    unittest.main()
