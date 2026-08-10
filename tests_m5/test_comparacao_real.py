"""Testa comparar_simulado_vs_real com banco SQLite temporário."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from iqoption_m5 import backtest


def _criar_banco(caminho: str) -> None:
    conn = sqlite3.connect(caminho)
    conn.execute(
        """
        CREATE TABLE operacoes (
            id_ordem TEXT PRIMARY KEY,
            ativo TEXT NOT NULL,
            direcao TEXT NOT NULL,
            enviada_em TEXT NOT NULL,
            encerrada_em TEXT,
            valor REAL NOT NULL,
            payout REAL NOT NULL,
            setup TEXT NOT NULL DEFAULT 'desconhecido',
            lucro REAL,
            resultado_bruto TEXT,
            status TEXT NOT NULL
        )
        """
    )
    operacoes = [
        # setup_a: 3 wins, 1 loss
        ("1", "EURUSD", "call", "2026-01-01T10:00:00", "2026-01-01T10:05:00", 1.0, 0.85, "setup_a",  0.85, "win",  "finalizada"),
        ("2", "EURUSD", "call", "2026-01-01T10:05:00", "2026-01-01T10:10:00", 1.0, 0.85, "setup_a",  0.85, "win",  "finalizada"),
        ("3", "EURUSD", "call", "2026-01-01T10:10:00", "2026-01-01T10:15:00", 1.0, 0.85, "setup_a",  0.85, "win",  "finalizada"),
        ("4", "EURUSD", "put",  "2026-01-01T10:15:00", "2026-01-01T10:20:00", 1.0, 0.85, "setup_a", -1.0,  "loss", "finalizada"),
        # setup_b: 1 win, 1 loss
        ("5", "EURUSD", "call", "2026-01-01T11:00:00", "2026-01-01T11:05:00", 1.0, 0.85, "setup_b",  0.85, "win",  "finalizada"),
        ("6", "EURUSD", "put",  "2026-01-01T11:05:00", "2026-01-01T11:10:00", 1.0, 0.85, "setup_b", -1.0,  "loss", "finalizada"),
        # correcao_manual deve ser ignorada
        ("7", "EURUSD", "call", "2026-01-01T12:00:00", "2026-01-01T12:05:00", 1.0, 0.85, "correcao_manual", 5.0, "ajuste", "finalizada"),
        # operação aberta (sem resultado) — deve ser ignorada
        ("8", "EURUSD", "call", "2026-01-01T13:00:00", None,                  1.0, 0.85, "setup_a", None, None, "aberta"),
    ]
    conn.executemany(
        "INSERT INTO operacoes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        operacoes,
    )
    conn.commit()
    conn.close()


class TestCompararSimuladoVsReal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp.close()
        _criar_banco(self._tmp.name)
        self.resultado = backtest.comparar_simulado_vs_real(self._tmp.name, 0.85)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_retorna_chaves_esperadas(self):
        self.assertIn("por_setup", self.resultado)
        self.assertIn("global", self.resultado)

    def test_por_setup_tem_setups_corretos(self):
        por_setup = self.resultado["por_setup"]
        self.assertIn("setup_a", por_setup)
        self.assertIn("setup_b", por_setup)
        # correcao_manual excluída
        self.assertNotIn("correcao_manual", por_setup)

    def test_por_setup_tem_chaves_corretas(self):
        item = self.resultado["por_setup"]["setup_a"]
        for chave in ("operacoes", "wins", "losses", "winrate", "ic95_min", "ic95_max", "lucro_total"):
            self.assertIn(chave, item, f"faltou chave '{chave}' em por_setup")

    def test_winrate_entre_zero_e_um(self):
        for setup, item in self.resultado["por_setup"].items():
            self.assertGreaterEqual(item["winrate"], 0.0, f"{setup}: winrate < 0")
            self.assertLessEqual(item["winrate"], 1.0, f"{setup}: winrate > 1")

    def test_ic95_entre_zero_e_um(self):
        for setup, item in self.resultado["por_setup"].items():
            self.assertGreaterEqual(item["ic95_min"], 0.0)
            self.assertLessEqual(item["ic95_max"], 1.0)
            self.assertLessEqual(item["ic95_min"], item["ic95_max"])

    def test_setup_a_winrate_correto(self):
        # 3 wins / 4 decididas = 0.75
        item = self.resultado["por_setup"]["setup_a"]
        self.assertEqual(item["wins"], 3)
        self.assertEqual(item["losses"], 1)
        self.assertAlmostEqual(item["winrate"], 0.75, places=2)

    def test_global_exclui_operacoes_abertas_e_correcao_manual(self):
        g = self.resultado["global"]
        # setup_a: 4 decididas + setup_b: 2 decididas = 6 decididas + 0 empates = 6 ops
        self.assertEqual(g["wins"] + g["losses"] + g["empates"], g["operacoes"])
        self.assertEqual(g["operacoes"], 6)

    def test_global_winrate_entre_zero_e_um(self):
        g = self.resultado["global"]
        self.assertGreaterEqual(g["winrate"], 0.0)
        self.assertLessEqual(g["winrate"], 1.0)

    def test_banco_vazio_retorna_zeros(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            caminho = f.name
        try:
            _criar_banco(caminho)
            # Remove todas as ops finalizadas
            conn = sqlite3.connect(caminho)
            conn.execute("DELETE FROM operacoes")
            conn.commit()
            conn.close()
            resultado = backtest.comparar_simulado_vs_real(caminho, 0.85)
            self.assertEqual(resultado["global"]["operacoes"], 0)
            self.assertEqual(resultado["por_setup"], {})
        finally:
            Path(caminho).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
