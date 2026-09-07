"""O log é diagnóstico: grava tudo, mas nunca pode derrubar o bot."""
from __future__ import annotations

import io
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from iqoption_m5 import log_arquivo


class TestEspelho(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pasta = Path(self._tmp.name)
        self.console = io.StringIO()

    def _espelho(self):
        # Windows nao apaga o tempdir com arquivo aberto: fecha no teardown.
        e = log_arquivo._Espelho(self.console, self.pasta, "teste")
        self.addCleanup(e._fechar)
        return e

    def test_escreve_no_console_e_no_arquivo(self):
        e = self._espelho()
        e.write("[mercado] falha ao consultar abertura turbo\n")
        self.assertIn("falha ao consultar", self.console.getvalue())
        self.assertIn("falha ao consultar",
                      e.caminho.read_text(encoding="utf-8"))

    def test_console_sai_sem_carimbo(self):
        e = self._espelho()
        e.write("linha\n")
        self.assertEqual(self.console.getvalue(), "linha\n")

    def test_arquivo_carimba_hora_no_inicio_da_linha(self):
        e = self._espelho()
        e.write("primeira\nsegunda\n")
        linhas = e.caminho.read_text(encoding="utf-8").splitlines()
        for linha in linhas:
            self.assertRegex(linha, r"^\d{2}:\d{2}:\d{2} ")
        self.assertTrue(linhas[0].endswith("primeira"))
        self.assertTrue(linhas[1].endswith("segunda"))

    def test_print_em_dois_pedacos_carimba_uma_vez_so(self):
        """print() escreve o texto e o \\n separadamente."""
        e = self._espelho()
        e.write("mensagem")
        e.write("\n")
        conteudo = e.caminho.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"\d{2}:\d{2}:\d{2} ", conteudo)), 1)
        self.assertTrue(conteudo.rstrip("\n").endswith("mensagem"))

    def test_grava_na_hora_sem_esperar_buffer(self):
        e = self._espelho()
        e.write("urgente\n")
        self.assertIn("urgente", e.caminho.read_text(encoding="utf-8"))

    def test_pasta_inacessivel_nao_derruba(self):
        e = log_arquivo._Espelho(self.console, self.pasta, "teste")
        e._arquivo = None  # simula falha de abertura
        e.write("segue vivo\n")
        e.flush()
        self.assertIn("segue vivo", self.console.getvalue())

    def test_arquivo_fechado_nao_derruba(self):
        e = self._espelho()
        e._arquivo.close()  # ValueError na proxima escrita
        e.write("ainda no console\n")
        self.assertIn("ainda no console", self.console.getvalue())

    def test_acentos_e_setas_sobrevivem(self):
        e = self._espelho()
        e.write("decisão → bloqueada por atraso\n")
        self.assertIn("decisão → bloqueada",
                      e.caminho.read_text(encoding="utf-8"))

    def test_vira_o_arquivo_quando_o_dia_brt_muda(self):
        real = log_arquivo._hoje_brt
        log_arquivo._hoje_brt = lambda: "2026-09-07"
        try:
            e = self._espelho()
            e.write("de ontem\n")
            ontem = e.caminho
            log_arquivo._hoje_brt = lambda: "2026-09-08"
            e.write("de hoje\n")
            hoje = e.caminho
        finally:
            log_arquivo._hoje_brt = real

        self.assertNotEqual(hoje, ontem)
        self.assertIn("de ontem", ontem.read_text(encoding="utf-8"))
        self.assertIn("de hoje", hoje.read_text(encoding="utf-8"))
        self.assertNotIn("de hoje", ontem.read_text(encoding="utf-8"))

    def test_ativar_duas_vezes_nao_empilha(self):
        import sys
        original = sys.stdout
        try:
            primeiro = log_arquivo.ativar("teste", self.pasta)
            self.assertIsNotNone(primeiro)
            self.assertIsNone(log_arquivo.ativar("teste", self.pasta))
        finally:
            sys.stdout = original
            sys.stderr = sys.__stderr__


if __name__ == "__main__":
    unittest.main()
