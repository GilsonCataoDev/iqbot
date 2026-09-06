"""estado_hoje() conta o dia BRT, não o dia UTC.

Entre 21h e meia-noite BRT o UTC já virou. Comparar a data local com
date(enviada_em) crua fazia o GerenciadorRisco recarregar um dia vazio num
restart noturno — perdas e contagem do dia sumiam.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from iqoption_m5.registro import RegistroSQLite


def _agora_brt() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)


class TestEstadoHojeFuso(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.registro = RegistroSQLite(Path(self._tmp.name) / "r.sqlite3")

    def _inserir(self, enviada_utc: datetime, status="finalizada", lucro=-5.0, valor=5.0):
        with self.registro._sessao() as db:
            db.execute(
                """INSERT INTO operacoes
                   (ativo, direcao, setup, timeframe, valor, payout, status,
                    enviada_em, resultado_bruto, lucro)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("EURUSD", "call", "M5 t", 300, valor, 0.86, status,
                 enviada_utc.isoformat(),
                 None if lucro is None else ("loss" if lucro < 0 else "win"),
                 lucro),
            )

    def test_operacao_da_noite_brt_conta_no_dia(self):
        """22h BRT já é o dia seguinte em UTC, mas ainda é hoje pro operador."""
        brt = _agora_brt().replace(hour=22, minute=30, second=0, microsecond=0)
        self._inserir(brt + timedelta(hours=3))  # de volta pra UTC
        estado = self.registro.estado_hoje()
        self.assertEqual(estado.operacoes_enviadas, 1)
        self.assertEqual(estado.perdas_consecutivas, 1)
        self.assertAlmostEqual(estado.lucro_sessao, -5.0)

    def test_operacao_de_ontem_a_noite_nao_conta(self):
        brt_ontem = _agora_brt().replace(hour=22, minute=0, second=0,
                                         microsecond=0) - timedelta(days=1)
        self._inserir(brt_ontem + timedelta(hours=3))
        self.assertEqual(self.registro.estado_hoje().operacoes_enviadas, 0)

    def test_madrugada_brt_conta_no_dia_certo(self):
        """01h BRT: mesmo dia UTC, precisa continuar contando."""
        brt = _agora_brt().replace(hour=1, minute=0, second=0, microsecond=0)
        self._inserir(brt + timedelta(hours=3))
        self.assertEqual(self.registro.estado_hoje().operacoes_enviadas, 1)

    def test_perdas_consecutivas_atravessam_a_virada_utc(self):
        """Sequência de perdas 20h->22h BRT nao pode ser cortada pelo UTC."""
        base = _agora_brt().replace(hour=20, minute=0, second=0, microsecond=0)
        for offset in (0, 60, 120):  # 20h, 21h, 22h BRT
            self._inserir(base + timedelta(minutes=offset) + timedelta(hours=3))
        estado = self.registro.estado_hoje()
        self.assertEqual(estado.operacoes_enviadas, 3)
        self.assertEqual(estado.perdas_consecutivas, 3,
                         "limite de perdas nao pode zerar as 21h")

    def test_ordem_aberta_da_noite_ainda_bloqueia(self):
        brt = _agora_brt().replace(hour=23, minute=0, second=0, microsecond=0)
        self._inserir(brt + timedelta(hours=3), status="aberta", lucro=None)
        self.assertTrue(self.registro.estado_hoje().ordem_pendente)


if __name__ == "__main__":
    unittest.main()
