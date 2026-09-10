"""Strike e vencimento reais precisam sobreviver até o banco.

Sem eles o backtest supõe entrada na abertura da vela e saída no fechamento
de outra. Medido contra 19 ordens reais, esse par de suposições errou 32%
dos desfechos — a opção abre ao preço do instante da compra e expira num
marco de relógio da corretora.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pandas as pd

from iqoption_m5.config import Configuracao
from iqoption_m5.executor import ExecutorSeguro
from iqoption_m5.mercado_iq import MercadoIQ
from iqoption_m5.modelos import Decisao, SnapshotMercado
from iqoption_m5.registro import RegistroSQLite
from iqoption_m5.risco import GerenciadorRisco


class TestExtracaoDoContrato(unittest.TestCase):
    def test_le_strike_vencimento_e_preco_final_da_ordem_da_iq(self):
        contrato = MercadoIQ._extrair_contrato({
            "id": 42, "open_quote": 1.17321, "exp_time": 1_800_000_900,
            "close_quote": 1.17355, "win": "win",
        })

        self.assertAlmostEqual(contrato["strike"], 1.17321)
        self.assertEqual(contrato["expira_em"], 1_800_000_900)
        self.assertAlmostEqual(contrato["preco_expiracao"], 1.17355)

    def test_aceita_os_nomes_alternativos_que_a_iq_usa(self):
        contrato = MercadoIQ._extrair_contrato({
            "value": 1.2, "expiration_time": 1_800_000_000, "close_price": 1.3,
        })

        self.assertAlmostEqual(contrato["strike"], 1.2)
        self.assertAlmostEqual(contrato["preco_expiracao"], 1.3)

    def test_ordem_sem_nenhum_campo_util_nao_vira_registro_vazio(self):
        self.assertIsNone(MercadoIQ._extrair_contrato({"id": 1, "win": "win"}))

    def test_campo_ilegivel_nao_derruba_a_extracao(self):
        contrato = MercadoIQ._extrair_contrato({"open_quote": "n/d", "exp_time": 1_800_000_000})

        self.assertIsNone(contrato["strike"])
        self.assertEqual(contrato["expira_em"], 1_800_000_000)


class MercadoComContrato:
    """Dublê que devolve o contrato uma única vez, como o MercadoIQ."""

    def __init__(self):
        self.contratos = {"ordem-1": {
            "strike": 1.17321, "expira_em": 1_800_000_900, "preco_expiracao": 1.17355,
        }}

    def timestamp_servidor(self):
        return 1_800_000_000

    def comprar(self, valor, ativo, direcao, expiracao_minutos):
        return True, "ordem-1"

    def aguardar_resultado(self, id_ordem, expiracao_minutos=None):
        return 0.85

    def resultado_por_candle(self, *args, **kwargs):
        return 0.85

    def detalhes_contrato(self, id_ordem):
        return self.contratos.pop(str(id_ordem), None)


class MercadoSemContrato(MercadoComContrato):
    """Mercado que não oferece o método — não pode quebrar a ordem."""

    detalhes_contrato = None


class TestRegistroDoContrato(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Configuracao(pasta_dados=Path(self.temp.name), executar_ordens=True)
        idx = pd.date_range("2026-01-01", periods=60, freq="5min")
        self.snapshot = SnapshotMercado(
            ativo="EURUSD",
            candles=pd.DataFrame(
                {"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0, "Volume": 1.0}, index=idx
            ),
            payout=0.85, mercado_aberto=True, timestamp_servidor=1_800_000_000,
        )
        self.decisao = Decisao(
            ativo="EURUSD", direcao="call", preco=1.0, candle_hora=idx[-2], motivo="teste",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _executar(self, mercado):
        registro = RegistroSQLite(self.config.banco_sqlite, config=self.config)
        executor = ExecutorSeguro(self.config, mercado, GerenciadorRisco(self.config), registro)
        self.assertTrue(executor.executar(self.snapshot, self.decisao))
        executor.aguardar_ordens()

    def test_contrato_da_corretora_chega_ao_banco(self):
        self._executar(MercadoComContrato())

        with closing(sqlite3.connect(self.config.banco_sqlite)) as db:
            linha = db.execute(
                "SELECT strike, expira_em, preco_expiracao FROM contratos WHERE id_ordem=?",
                ("ordem-1",),
            ).fetchone()

        self.assertIsNotNone(linha, "a ordem finalizou sem gravar o contrato")
        self.assertAlmostEqual(linha[0], 1.17321)
        self.assertAlmostEqual(linha[2], 1.17355)
        # Guardado como ISO para casar com o resto da auditoria, não como epoch.
        self.assertEqual(linha[1], datetime.fromtimestamp(1_800_000_900).isoformat())

    def test_mercado_sem_o_metodo_ainda_registra_a_operacao(self):
        self._executar(MercadoSemContrato())

        with closing(sqlite3.connect(self.config.banco_sqlite)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM contratos").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM operacoes WHERE status='finalizada'").fetchone()[0], 1
            )


if __name__ == "__main__":
    unittest.main()
