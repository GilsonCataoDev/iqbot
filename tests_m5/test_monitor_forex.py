import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import numpy as np
import requests

from monitor_forex import (
    DiarioTrades,
    AnalistaGemini,
    LeituraMercado,
    _extrair_json_gemini,
    resumir_noticias,
    ServidorDadosForex,
    RastreadorPosicoes,
    SinalForex,
    _indice_ultimo_fechado,
    analisar_leitura_mercado,
    atualizar_preco_sinal,
    detectar_sinal,
)


class TestSinalForex(unittest.TestCase):
    def test_html_nao_deixa_campo_ausente_virar_nan(self):
        from monitor_forex import _HTML
        self.assertIn("filter(Number.isFinite)", _HTML)
        self.assertIn("const precoAtual = Number.isFinite", _HTML)
        self.assertIn("REINICIE O MONITOR", _HTML)
        self.assertIn("LEITURA DO MERCADO", _HTML)
        self.assertIn("ALVO POSSÍVEL", _HTML)
        self.assertIn("INVALIDAÇÃO", _HTML)
        self.assertIn("SEGUNDA OPINIÃO", _HTML)
        self.assertIn("ANÁLISE LOCAL", _HTML)
        self.assertIn("ia.fonte || 'IA'", _HTML)
        self.assertIn("M15 ${mm}:${ss}", _HTML)
        self.assertIn("setInterval(() => carregarCandles(parAtivo), 1000)", _HTML)
        self.assertIn("let velasVisiveis = 40", _HTML)
        self.assertIn("cursor:default", _HTML)
        self.assertIn("addEventListener('wheel'", _HTML)
        self.assertIn("ZONA ${leitura.lado_entrada}", _HTML)
        self.assertIn("Melhor zona para ${leitura.lado_entrada}", _HTML)
        self.assertIn("H1 ${leitura.tendencia} | M15 ${leitura.tendencia_m15}", _HTML)
        self.assertIn("R:R estimado na zona", _HTML)
        self.assertIn("calcularEma(todos,20)", _HTML)
        self.assertIn("NOTÍCIAS DO PAR", _HTML)
        self.assertIn("EVITAR ±20 MIN", _HTML)

    def test_leitura_expoe_tendencia_alvo_e_invalidacao(self):
        indice = pd.date_range("2026-01-01", periods=260, freq="15min")
        close = 1.08 + np.arange(260) * 0.00005
        df = pd.DataFrame({
            "Open": close - 0.00002, "High": close + 0.00010,
            "Low": close - 0.00010, "Close": close,
        }, index=indice)
        agora = indice[-1].to_pydatetime().replace(tzinfo=timezone.utc) + timedelta(minutes=15)
        leitura = analisar_leitura_mercado("EURUSD", df, agora)
        self.assertIsNotNone(leitura)
        self.assertEqual(leitura.tendencia, "ALTA")
        self.assertGreater(leitura.alvo, leitura.preco)
        self.assertLess(leitura.invalidacao, leitura.preco)
        self.assertEqual(leitura.lado_entrada, "COMPRA")
        self.assertLess(leitura.entrada_min, leitura.entrada_max)
        self.assertGreater(leitura.atr_pips, 0)
        self.assertGreaterEqual(leitura.rsi14, 0)
        self.assertLessEqual(leitura.rsi14, 100)

    def test_gemini_aceita_somente_alvo_calculado(self):
        leitura = LeituraMercado("EURUSD", "ALTA", 1.10, 1.12, 1.13, 1.09,
                                 1.095, 1.097, "COMPRA",
                                 ["EMA20 M15"], "compras em recuos", 65)
        resposta = Mock()
        resposta.raise_for_status.return_value = None
        resposta.json.return_value = {"candidates": [{"content": {"parts": [{"text":
            '{"status":"ESPERAR","lado":"COMPRA","alvo_escolhido":1.12,"resumo":"Aguardar recuo."}'
        }]}}]}
        recebido = {}
        with patch("monitor_forex.requests.post", return_value=resposta):
            AnalistaGemini("chave")._executar(leitura, lambda _a, d: recebido.update(d))
        self.assertEqual(recebido["status"], "ESPERAR")
        self.assertEqual(recebido["alvo_escolhido"], 1.12)

    def test_gemini_le_chave_com_nome_correto(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "teste"}, clear=False):
            self.assertTrue(AnalistaGemini().ativo)

    def test_groq_funciona_sem_gemini(self):
        leitura = LeituraMercado("EURUSD", "ALTA", 1.10, 1.12, 1.13, 1.09,
                                 1.095, 1.097, "COMPRA", [], "recuo", 65)
        resposta = Mock(status_code=200)
        resposta.raise_for_status.return_value = None
        resposta.json.return_value = {"choices": [{"message": {"content":
            '{"status":"ESPERAR","lado":"COMPRA","alvo_escolhido":1.12,"resumo":"Aguardar."}'
        }}]}
        recebido = {}
        with patch("monitor_forex.requests.post", return_value=resposta) as post:
            AnalistaGemini("", chave_groq="groq")._executar(leitura, lambda _a, d: recebido.update(d))
        self.assertIn("api.groq.com", post.call_args.args[0])
        self.assertEqual(recebido["fonte"], "GROQ")

    def test_groq_entra_quando_gemini_falha(self):
        leitura = LeituraMercado("EURUSD", "ALTA", 1.10, 1.12, 1.13, 1.09,
                                 1.095, 1.097, "COMPRA", [], "recuo", 65)
        ocupado = Mock(status_code=429)
        recebido = {}
        analista = AnalistaGemini("gemini", chave_groq="groq")
        retorno = {"status": "ESPERAR", "lado": "COMPRA", "alvo_escolhido": 1.12,
                   "resumo": "Aguardar.", "fonte": "GROQ"}
        with patch("monitor_forex.requests.post", return_value=ocupado), \
             patch.object(analista, "_consultar_groq", return_value=retorno) as groq:
            analista._executar(leitura, lambda _a, d: recebido.update(d))
        groq.assert_called_once()
        self.assertEqual(recebido["fonte"], "GROQ")

    def test_gemini_tenta_modelo_reserva_em_503(self):
        leitura = LeituraMercado("EURUSD", "ALTA", 1.10, 1.12, 1.13, 1.09,
                                 1.095, 1.097, "COMPRA",
                                 ["EMA20 M15"], "compras em recuos", 65)
        ocupado = Mock(status_code=503)
        sucesso = Mock(status_code=200)
        sucesso.raise_for_status.return_value = None
        sucesso.json.return_value = {"candidates": [{"content": {"parts": [{"text":
            '{"status":"ESPERAR","lado":"COMPRA","alvo_escolhido":1.12,"resumo":"Aguardar."}'
        }]}}]}
        recebido = {}
        with patch("monitor_forex.requests.post", side_effect=[ocupado, sucesso]) as post:
            AnalistaGemini("chave")._executar(leitura, lambda _a, d: recebido.update(d))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(recebido["status"], "ESPERAR")

    def test_gemini_tenta_reserva_apos_timeout(self):
        leitura = LeituraMercado("EURUSD", "ALTA", 1.10, 1.12, 1.13, 1.09,
                                 1.095, 1.097, "COMPRA", [], "recuo", 50)
        sucesso = Mock(status_code=200)
        sucesso.raise_for_status.return_value = None
        sucesso.json.return_value = {"candidates": [{"content": {"parts": [{"text":
            '{"status":"ESPERAR","lado":"COMPRA","alvo_escolhido":1.12,"resumo":"Aguardar."}'
        }]}}]}
        recebido = {}
        with patch("monitor_forex.requests.post", side_effect=[requests.ReadTimeout(), sucesso]) as post:
            AnalistaGemini("chave")._executar(leitura, lambda _a, d: recebido.update(d))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(recebido["status"], "ESPERAR")

    def test_gemini_tenta_reserva_apos_json_invalido(self):
        leitura = LeituraMercado("EURUSD", "ALTA", 1.10, 1.12, 1.13, 1.09,
                                 1.095, 1.097, "COMPRA", [], "recuo", 50)
        invalida = Mock(status_code=200)
        invalida.raise_for_status.return_value = None
        invalida.json.return_value = {"candidates": [{"content": {"parts": [{"text": "```json\n{"}]}}]}
        sucesso = Mock(status_code=200)
        sucesso.raise_for_status.return_value = None
        sucesso.json.return_value = {"candidates": [{"content": {"parts": [{"text":
            '{"status":"ESPERAR","lado":"COMPRA","alvo_escolhido":1.12,"resumo":"Aguardar."}'
        }]}}]}
        recebido = {}
        with patch("monitor_forex.requests.post", side_effect=[invalida, sucesso]) as post:
            AnalistaGemini("chave")._executar(leitura, lambda _a, d: recebido.update(d))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(recebido["status"], "ESPERAR")

    def test_extrai_json_cercado_por_markdown(self):
        dados = _extrair_json_gemini('```json\n{"status":"ESPERAR"}\n```')
        self.assertEqual(dados["status"], "ESPERAR")

    def test_noticia_na_janela_marca_evitar(self):
        agora = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        evento = Mock(titulo="Payroll", moeda="USD", impacto="High",
                      quando=agora + timedelta(minutes=10), actual=None, forecast="100K")
        evento.minutos_ate.side_effect = lambda instante: (evento.quando - instante).total_seconds() / 60
        evento.resultado_direcao.return_value = None
        calendario = Mock()
        calendario.proximos.return_value = [evento]
        resumo = resumir_noticias(calendario, "EURUSD", agora)
        self.assertEqual(resumo["status"], "EVITAR")
        self.assertTrue(resumo["eventos"][0]["em_janela_risco"])

    def test_noticia_publicada_vira_direcional(self):
        agora = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        evento = Mock(titulo="Payroll", moeda="USD", impacto="High",
                      quando=agora, actual="200K", forecast="100K")
        evento.minutos_ate.return_value = 0
        evento.resultado_direcao.return_value = {"direcao": "PUT"}
        calendario = Mock()
        calendario.proximos.return_value = [evento]
        resumo = resumir_noticias(calendario, "EURUSD", agora)
        self.assertEqual(resumo["status"], "DIRECIONAL")
        self.assertEqual(resumo["direcao_noticia"], "PUT")

    def test_diario_salva_leitura_e_decisao_da_ia(self):
        with tempfile.TemporaryDirectory() as pasta:
            diario = DiarioTrades(Path(pasta))
            leitura = LeituraMercado("EURUSD", "ALTA", 1.10, 1.12, 1.13, 1.09,
                                     1.095, 1.097, "COMPRA", ["EMA20"], "recuo", 70)
            noticias = {"status": "DIRECIONAL", "direcao_noticia": "CALL"}
            diario.registrar_leitura(leitura, noticias)
            diario.registrar_analise_ia("EURUSD", {"fonte": "GROQ", "status": "FAVORAVEL",
                                          "lado": "COMPRA", "alvo_escolhido": 1.12,
                                          "resumo": "Alinhado."}, noticias)
            self.assertEqual(len(Path(pasta, "forex_leituras.csv").read_text(encoding="utf-8").splitlines()), 2)
            self.assertIn("GROQ", Path(pasta, "forex_analises_ia.csv").read_text(encoding="utf-8"))

    def test_tick_substitui_candle_em_formacao(self):
        with tempfile.TemporaryDirectory() as pasta:
            servidor = ServidorDadosForex(Path(pasta))
            indice = pd.date_range("2026-01-01", periods=2, freq="15min")
            df = pd.DataFrame({"Open": [1.1, 1.2], "High": [1.2, 1.3],
                               "Low": [1.0, 1.1], "Close": [1.15, 1.25]}, index=indice)
            servidor.salvar_candles("EURUSD", df)
            ts = int(indice[-1].timestamp())
            servidor.atualizar_tick("EURUSD", {"from": ts, "open": 1.2,
                                                "max": 1.35, "min": 1.1, "close": 1.31})
            dados = json.loads((Path(pasta) / "candles_EURUSD.json").read_text())
            self.assertEqual(len(dados), 2)
            self.assertEqual(dados[-1]["c"], 1.31)

    def test_identifica_ultimo_candle_fechado_sem_assumir_penultimo(self):
        indice = pd.date_range("2026-01-01", periods=3, freq="15min")
        df = pd.DataFrame(index=indice)

        self.assertEqual(_indice_ultimo_fechado(df, datetime(2026, 1, 1, 0, 40, tzinfo=timezone.utc)), 1)
        self.assertEqual(_indice_ultimo_fechado(df, datetime(2026, 1, 1, 0, 45, tzinfo=timezone.utc)), 2)

    def test_preco_atual_define_entrar_aguardar_e_cancelar(self):
        agora = datetime(2026, 1, 1, tzinfo=timezone.utc)
        sinal = SinalForex(
            ativo="EURUSD", lado="buy", entrada=1.1000, sl=1.0990, tp=1.1030,
            risco=0.0010, rr=3.0, gerado_em=agora,
            candle_rompimento=agora, valido_ate=agora + timedelta(minutes=15),
            preco_atual=1.1000, rr_atual=3.0,
        )

        self.assertEqual(atualizar_preco_sinal(sinal, 1.1000, agora).acao, "ENTRAR_AGORA")
        self.assertEqual(atualizar_preco_sinal(sinal, 1.1015, agora).acao, "AGUARDAR_PRECO")
        self.assertEqual(atualizar_preco_sinal(sinal, 1.1030, agora).acao, "CANCELADO")

    def test_detector_usa_rompimento_fechado_e_abertura_seguinte(self):
        indice = pd.date_range("2026-01-01", periods=260, freq="15min")
        linhas = []
        for i in range(260):
            if i < 239:
                centro, amp = 1.1000, 0.0010
            else:
                centro, amp = 1.1001 + (i % 10) * 0.0002, 0.0001
            linhas.append({"Open": centro, "High": centro + amp / 2, "Low": centro - amp / 2, "Close": centro})
        linhas[258] = {"Open": 1.10010, "High": 1.10012, "Low": 1.10000, "Close": 1.10003}
        linhas[259] = {"Open": 1.10003, "High": 1.10008, "Low": 1.10002, "Close": 1.10004}
        df = pd.DataFrame(linhas, index=indice)
        agora = indice[259].to_pydatetime().replace(tzinfo=timezone.utc) + timedelta(minutes=5)

        sinal = detectar_sinal("EURUSD", df, agora)

        self.assertIsNotNone(sinal)
        self.assertAlmostEqual(sinal.entrada, 1.10015, places=5)
        self.assertEqual(sinal.candle_rompimento, indice[258].to_pydatetime())
        self.assertGreaterEqual(sinal.rr, 2.0)


class _BaixoFake:
    def __init__(self, fechada=None):
        self.fechada = fechada
        self.position_history_v2 = None

    def get_position_history_v2(self, *_args):
        posicoes = [] if self.fechada is None else [self.fechada]
        self.position_history_v2 = {"status": 2000, "msg": {"positions": posicoes}}


class _ApiFake:
    def __init__(self, aberta, fechada=None):
        self._respostas = [[aberta], []]
        self.api = _BaixoFake(fechada)

    def get_positions(self, _tipo):
        return True, {"positions": self._respostas.pop(0)}


class TestRastreadorForex(unittest.TestCase):
    def _aberta(self):
        return {
            "id": 123, "instrument_id": "EURUSD", "side": "buy",
            "open_price": 1.1000, "current_price": 1.0995, "pnl": -99,
            "stop_lose_value": 1.0990, "take_profit_value": 1.1020,
        }

    def test_fechamento_usa_pnl_oficial_do_historico(self):
        fechada = dict(self._aberta(), status="closed", close_price=1.1020, pnl_net=12.5)
        with tempfile.TemporaryDirectory() as pasta:
            diario = DiarioTrades(Path(pasta))
            rastreador = RastreadorPosicoes(_ApiFake(self._aberta(), fechada), diario)

            rastreador.atualizar()
            rastreador.atualizar()

            with open(Path(pasta) / "forex_trades.csv", newline="", encoding="utf-8") as f:
                linhas = list(csv.DictReader(f))
            self.assertEqual([x["evento"] for x in linhas], ["aberta", "fechamento_pendente", "fechada"])
            self.assertEqual(linhas[-1]["resultado"], "ganho")
            self.assertEqual(linhas[-1]["pnl"], "12.5")

    def test_sem_historico_nao_inventa_perda(self):
        with tempfile.TemporaryDirectory() as pasta:
            diario = DiarioTrades(Path(pasta))
            rastreador = RastreadorPosicoes(_ApiFake(self._aberta()), diario)

            rastreador.atualizar()
            rastreador.atualizar()

            with open(Path(pasta) / "forex_trades.csv", newline="", encoding="utf-8") as f:
                linhas = list(csv.DictReader(f))
            self.assertEqual([x["evento"] for x in linhas], ["aberta", "fechamento_pendente"])
            self.assertTrue(all(not x["resultado"] for x in linhas))


if __name__ == "__main__":
    unittest.main()
