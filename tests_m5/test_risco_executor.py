import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from iqoption_m5.config import Configuracao
from iqoption_m5.app import main
from iqoption_m5.executor import ExecutorSeguro
from iqoption_m5.modelos import Decisao, ResultadoOrdem, SnapshotMercado
from iqoption_m5.registro import RegistroSQLite
from iqoption_m5.recuperacao import recuperar_operacoes_pendentes
from iqoption_m5.risco import GerenciadorRisco


class MercadoFalso:
    def iniciar(self):
        pass

    def snapshot(self, ativo):
        raise NotImplementedError

    def comprar(self, valor, ativo, direcao, expiracao_minutos):
        return True, "ordem-teste-1"

    def aguardar_resultado(self, id_ordem):
        return 0.85

    def consultar_resultado(self, id_ordem):
        return 0.85

    def fechar(self):
        pass


class TestRiscoEExecutor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Configuracao(pasta_dados=Path(self.temp.name))
        idx = pd.date_range("2026-01-01", periods=60, freq="5min")
        candles = pd.DataFrame(
            {"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0, "Volume": 1.0},
            index=idx,
        )
        self.snapshot = SnapshotMercado(
            ativo="EURUSD",
            candles=candles,
            payout=0.85,
            mercado_aberto=True,
            timestamp_servidor=1_800_000_000,
        )
        self.decisao = Decisao(
            ativo="EURUSD",
            direcao="call",
            preco=1.0,
            candle_hora=idx[-2],
            motivo="teste",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_valor_percentual_banca_calcula_pela_banca_atual(self):
        cfg = replace(self.config, valor_percentual_banca=0.03, banca_inicial=100.0)
        risco = GerenciadorRisco(cfg)
        registro = RegistroSQLite(cfg.banco_sqlite)
        executor = ExecutorSeguro(cfg, MercadoFalso(), risco, registro)

        self.assertAlmostEqual(executor._valor_da_entrada(), 3.0)

        risco.reservar(self.snapshot, self.decisao)
        risco.registrar_resultado(50.0, self.decisao.ativo)  # banca sobe pra 150
        self.assertAlmostEqual(executor._valor_da_entrada(), 4.5)

    def test_valor_percentual_banca_respeita_minimo_de_dois_reais(self):
        cfg = replace(self.config, valor_percentual_banca=0.03, banca_inicial=10.0)
        risco = GerenciadorRisco(cfg)
        registro = RegistroSQLite(cfg.banco_sqlite)
        executor = ExecutorSeguro(cfg, MercadoFalso(), risco, registro)

        # 3% de R$10 = R$0.30, mas o minimo da IQ Option e R$2.
        self.assertAlmostEqual(executor._valor_da_entrada(), 2.0)

    def test_payout_baixo_nao_bloqueia_com_minimo_zero(self):
        risco = GerenciadorRisco(replace(self.config, payout_minimo=0.0))
        baixo = SnapshotMercado(
            ativo=self.snapshot.ativo,
            candles=self.snapshot.candles,
            payout=0.70,
            mercado_aberto=True,
            timestamp_servidor=self.snapshot.timestamp_servidor,
        )
        autorizacao = risco.avaliar(baixo, self.decisao)
        self.assertTrue(autorizacao.permitida)

    def test_meta_diaria_encerra_o_dia_ao_ser_atingida(self):
        """Sem trava de lucro o bot devolve o ganho nos dias bons: ele so
        parava por perda. Ao bater a meta, tem que encerrar a sessao."""
        risco = GerenciadorRisco(replace(self.config, meta_diaria=15.0))

        risco.reservar(self.snapshot, self.decisao)
        risco.registrar_resultado(10.0, self.decisao.ativo)
        self.assertFalse(risco.resumo().encerrado)
        self.assertTrue(risco.avaliar(self.snapshot, self.decisao).permitida)

        risco.reservar(self.snapshot, self.decisao)
        risco.registrar_resultado(5.0, self.decisao.ativo)
        resumo = risco.resumo()
        self.assertTrue(resumo.encerrado)
        self.assertEqual(resumo.motivo_encerramento, "meta_diaria_atingida")
        self.assertFalse(risco.avaliar(self.snapshot, self.decisao).permitida)

    def test_meta_diaria_desativada_por_padrao(self):
        risco = GerenciadorRisco(self.config)
        risco.reservar(self.snapshot, self.decisao)
        risco.registrar_resultado(500.0, self.decisao.ativo)
        self.assertFalse(risco.resumo().encerrado)

    def test_perdas_consecutivas_nao_encerram_por_padrao(self):
        risco = GerenciadorRisco(self.config)
        for _ in range(10):
            self.assertTrue(risco.reservar(self.snapshot, self.decisao).permitida)
            risco.registrar_resultado(-1.0, self.decisao.ativo)
        resumo = risco.resumo()
        self.assertFalse(resumo.encerrado)

    def test_perdas_nao_bloqueiam_antes_das_cinco_por_padrao(self):
        risco = GerenciadorRisco(self.config)
        for _ in range(2):
            self.assertTrue(risco.reservar(self.snapshot, self.decisao).permitida)
            risco.registrar_resultado(-1.0, self.decisao.ativo)

        resumo = risco.resumo()

        self.assertFalse(resumo.encerrado)
        self.assertTrue(risco.avaliar(self.snapshot, self.decisao).permitida)

    def test_executor_falso_grava_resultado(self):
        risco = GerenciadorRisco(self.config)
        registro = RegistroSQLite(self.config.banco_sqlite)
        executor = ExecutorSeguro(self.config, MercadoFalso(), risco, registro)
        self.assertTrue(executor.executar(self.snapshot, self.decisao))
        executor.aguardar_ordens()
        resumo = risco.resumo()
        self.assertEqual(resumo.operacoes_enviadas, 1)
        self.assertEqual(resumo.operacoes_finalizadas, 1)
        self.assertAlmostEqual(resumo.lucro_sessao, 0.85)
        with closing(sqlite3.connect(self.config.banco_sqlite)) as db:
            linha = db.execute("SELECT status, lucro FROM operacoes WHERE id_ordem='ordem-teste-1'").fetchone()
        self.assertEqual(linha, ("finalizada", 0.85))

    def test_limites_do_dia_persistem_apos_reinicio(self):
        registro = RegistroSQLite(self.config.banco_sqlite)
        agora = datetime.now()
        for numero in range(2):
            ordem = f"perda-{numero}"
            registro.registrar_abertura(ordem, self.decisao, 1.0, 0.85, agora)
            registro.registrar_resultado(
                ResultadoOrdem(
                    id_ordem=ordem,
                    ativo="EURUSD",
                    direcao="call",
                    enviada_em=agora,
                    encerrada_em=agora,
                    valor=1.0,
                    payout=0.85,
                    lucro=-1.0,
                    resultado_bruto=-1.0,
                )
            )
        estado = registro.estado_hoje()
        risco_reiniciado = GerenciadorRisco(self.config, estado)
        resumo = risco_reiniciado.resumo()
        self.assertEqual(resumo.operacoes_enviadas, 2)
        self.assertEqual(resumo.perdas_consecutivas, 2)
        self.assertFalse(resumo.encerrado)

    def test_operacao_guarda_qual_estrategia_gerou_entrada(self):
        registro = RegistroSQLite(self.config.banco_sqlite)
        pullback = Decisao(
            ativo="EURUSD-OTC",
            direcao="call",
            preco=1.0,
            candle_hora=self.decisao.candle_hora,
            motivo="pullback_tendencia_m5",
            detalhes={"setup": "pullback", "fatores": ["fibo", "suporte"]},
        )
        registro.registrar_abertura("pullback-1", pullback, 1.0, 0.90, datetime.now())
        with closing(sqlite3.connect(self.config.banco_sqlite)) as db:
            setup = db.execute(
                "SELECT setup FROM operacoes WHERE id_ordem='pullback-1'"
            ).fetchone()[0]
        self.assertEqual(setup, "pullback")

    def test_operacao_aberta_pode_ser_recuperada_apos_reinicio(self):
        registro = RegistroSQLite(self.config.banco_sqlite)
        agora = datetime.now()
        registro.registrar_abertura("pendente-1", self.decisao, 1.0, 0.85, agora)

        pendentes = registro.operacoes_pendentes()

        self.assertEqual(len(pendentes), 1)
        self.assertEqual(pendentes[0].id_ordem, "pendente-1")
        self.assertEqual(pendentes[0].ativo, "EURUSD")
        self.assertEqual(pendentes[0].valor, 1.0)
        self.assertEqual(pendentes[0].payout, 0.85)

        recuperadas, falhas = recuperar_operacoes_pendentes(
            MercadoFalso(), registro, timeout_segundos=1
        )

        self.assertEqual((recuperadas, falhas), (1, 0))
        self.assertFalse(registro.estado_hoje().ordem_pendente)
        with closing(sqlite3.connect(self.config.banco_sqlite)) as db:
            status, lucro = db.execute(
                "SELECT status, lucro FROM operacoes WHERE id_ordem='pendente-1'"
            ).fetchone()
        self.assertEqual(status, "finalizada")
        self.assertEqual(lucro, 0.85)

    def test_app_conecta_e_recupera_antes_de_aplicar_bloqueio(self):
        config = Configuracao(
            pasta_dados=Path(self.temp.name),
            abrir_grafico=False,
            max_perdas_consecutivas=1,
            parar_por_perdas=True,
        )
        registro = RegistroSQLite(config.banco_sqlite)
        registro.registrar_abertura(
            "reinicio-1", self.decisao, 1.0, 0.85, datetime.now()
        )

        class MercadoRecuperacao:
            def __init__(self):
                self.iniciado = False
                self.fechado = False

            def iniciar(self):
                self.iniciado = True

            def consultar_resultado(self, id_ordem):
                return -1.0

            def fechar(self):
                self.fechado = True

        mercado = MercadoRecuperacao()
        with patch("iqoption_m5.app.Configuracao", return_value=config), patch(
            "iqoption_m5.app.MercadoIQ", return_value=mercado
        ):
            main()

        self.assertTrue(mercado.iniciado)
        self.assertTrue(mercado.fechado)
        self.assertFalse(registro.estado_hoje().ordem_pendente)

    def test_resultado_nao_confirmado_mantem_bloqueio(self):
        registro = RegistroSQLite(self.config.banco_sqlite)
        registro.registrar_abertura(
            "sem-resposta-1", self.decisao, 1.0, 0.85, datetime.now()
        )

        class MercadoSemResposta:
            def consultar_resultado(self, id_ordem):
                return None

        recuperadas, falhas = recuperar_operacoes_pendentes(
            MercadoSemResposta(), registro, timeout_segundos=1
        )

        self.assertEqual((recuperadas, falhas), (0, 1))
        self.assertTrue(registro.estado_hoje().ordem_pendente)

    def test_ordem_antiga_sem_resposta_vira_perda_tecnica(self):
        registro = RegistroSQLite(self.config.banco_sqlite)
        registro.registrar_abertura(
            "antiga-sem-resposta",
            self.decisao,
            1.0,
            0.85,
            datetime.now() - timedelta(minutes=11),
        )

        class MercadoSemResposta:
            def consultar_resultado(self, id_ordem):
                return None

        recuperadas, falhas = recuperar_operacoes_pendentes(
            MercadoSemResposta(), registro, timeout_segundos=1
        )

        self.assertEqual((recuperadas, falhas), (1, 0))
        self.assertFalse(registro.estado_hoje().ordem_pendente)
        with closing(sqlite3.connect(self.config.banco_sqlite)) as db:
            lucro, bruto = db.execute(
                "SELECT lucro, resultado_bruto FROM operacoes "
                "WHERE id_ordem='antiga-sem-resposta'"
            ).fetchone()
        self.assertEqual(lucro, -1.0)
        self.assertEqual(bruto, "perda_tecnica_resultado_indisponivel")


if __name__ == "__main__":
    unittest.main()
