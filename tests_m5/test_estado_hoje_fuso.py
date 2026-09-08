"""estado_hoje() compara data local com data local.

O banco grava hora LOCAL: executor.py usa datetime.now(), e todo o registro.py
faz o mesmo. Converter enviada_em como se fosse UTC desloca tudo três horas e
quebra entre meia-noite e 3h da manhã, quando as duas datas divergem.

Este arquivo existe porque essa conversão já foi introduzida uma vez, a partir
de testes que inseriam UTC e não representavam a produção.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from iqoption_m5.registro import RegistroSQLite

FONTE_REGISTRO = Path(__file__).resolve().parent.parent / "iqoption_m5" / "registro.py"
FONTE_EXECUTOR = Path(__file__).resolve().parent.parent / "iqoption_m5" / "executor.py"


class TestFonteDoTimestamp(unittest.TestCase):
    """Trava a premissa: se a produção mudar para UTC, estes testes avisam."""

    def test_executor_grava_hora_local(self):
        codigo = FONTE_EXECUTOR.read_text(encoding="utf-8")
        self.assertIn("enviada_em = datetime.now()", codigo)
        self.assertNotIn("enviada_em = datetime.utcnow()", codigo)

    def test_registro_nao_converte_como_se_fosse_utc(self):
        codigo = FONTE_REGISTRO.read_text(encoding="utf-8")
        self.assertNotIn("-3 hours", codigo,
                         "enviada_em ja e local; subtrair 3h desloca o dia")
        self.assertNotIn("timedelta(hours=3)", codigo)


class TestEstadoHoje(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.registro = RegistroSQLite(Path(self._tmp.name) / "r.sqlite3")

    def _inserir(self, quando: datetime, status="finalizada", lucro=-5.0, valor=5.0):
        with self.registro._sessao() as db:
            db.execute(
                """INSERT INTO operacoes
                   (ativo, direcao, setup, timeframe, valor, payout, status,
                    enviada_em, resultado_bruto, lucro)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("EURUSD", "call", "M5 t", 300, valor, 0.86, status,
                 quando.isoformat(),
                 None if lucro is None else ("loose" if lucro < 0 else "win"),
                 lucro),
            )

    def test_operacao_de_agora_conta(self):
        self._inserir(datetime.now())
        estado = self.registro.estado_hoje()
        self.assertEqual(estado.operacoes_enviadas, 1)
        self.assertAlmostEqual(estado.lucro_sessao, -5.0)

    def test_madrugada_conta_no_proprio_dia(self):
        """01h local: uma conversao para UTC jogaria isto para ontem."""
        agora = datetime.now()
        madrugada = agora.replace(hour=1, minute=0, second=0, microsecond=0)
        if madrugada > agora:
            self.skipTest("ainda nao passou da 01h local")
        self._inserir(madrugada)
        self.assertEqual(self.registro.estado_hoje().operacoes_enviadas, 1)

    def test_noite_conta_no_proprio_dia(self):
        """22h local: o caso simetrico, que a conversao tambem quebrava."""
        agora = datetime.now()
        noite = agora.replace(hour=22, minute=0, second=0, microsecond=0)
        if noite > agora:
            self.skipTest("ainda nao passou das 22h local")
        self._inserir(noite)
        self.assertEqual(self.registro.estado_hoje().operacoes_enviadas, 1)

    def test_operacao_de_ontem_nao_conta(self):
        self._inserir(datetime.now() - timedelta(days=1))
        self.assertEqual(self.registro.estado_hoje().operacoes_enviadas, 0)

    def test_perdas_consecutivas_somam_no_dia(self):
        # Avança a partir de agora: recuar minutos logo depois da meia-noite
        # jogaria as primeiras para ontem e o teste mediria outra coisa.
        base = datetime.now().replace(microsecond=0)
        for minutos in (0, 1, 2):
            self._inserir(base + timedelta(minutes=minutos))
        estado = self.registro.estado_hoje()
        self.assertEqual(estado.operacoes_enviadas, 3)
        self.assertEqual(estado.perdas_consecutivas, 3)

    def test_ordem_aberta_bloqueia(self):
        self._inserir(datetime.now(), status="aberta", lucro=None)
        self.assertTrue(self.registro.estado_hoje().ordem_pendente)


if __name__ == "__main__":
    unittest.main()
