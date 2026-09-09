import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import pytest
import requests

import monitor_mercado
from iqoption_m5.ia import segunda_opiniao_grafico
from monitor_mercado import (
    ATIVOS, CLASSE, Estado, adquirir_trava_monitor, decisao_entrada, janela_validada_para,
    detectar_fvg_m15, leitura_fluxo_sessao, noticias_do_dia, plano_fibo, plano_ouro_movimento,
    plano_tp1_curto_forex, plano_orb_sessao,
    plano_bof_m30_m5, plano_varredura_liquidez, plano_confluencia_local, unidade_movimento, wilson_ci,
    candle_atual_para_grafico, sanitizar_candles_m15,
)


def test_painel_separa_agora_grafico_entradas_estudos_e_dados():
    for visao in ("agora", "grafico", "entradas", "estudos", "dados"):
        assert f'data-view="{visao}"' in monitor_mercado._HTML
    assert 'id="filtro-tipo"' in monitor_mercado._HTML
    assert 'id="saude"' in monitor_mercado._HTML
    assert 'id="confluencia"' in monitor_mercado._HTML
    assert 'function renderConfluencia()' in monitor_mercado._HTML
    assert 'function alternarNiveis()' in monitor_mercado._HTML
    assert 'niveisDetalhados' in monitor_mercado._HTML


def test_grafico_descarta_tick_fora_do_bloco_m15_que_achata_eurusd():
    indice = pd.date_range("2026-09-09 09:00", periods=4, freq="15min", tz="UTC")
    fechados = pd.DataFrame({
        "Open": [1.1640, 1.1641, 1.1642, 1.1643],
        "High": [1.1642, 1.1643, 1.1644, 1.1645],
        "Low": [1.1638, 1.1639, 1.1640, 1.1641],
        "Close": [1.1641, 1.1642, 1.1643, 1.1644],
    }, index=indice)
    # Reprodução do payload recebido: tick de 09:31 com preço impossível.
    atual = pd.DataFrame({
        "Open": [1.35534], "High": [1.355355],
        "Low": [1.35534], "Close": [1.355355],
    }, index=[pd.Timestamp("2026-09-09 09:31", tz="UTC")])

    assert candle_atual_para_grafico(fechados, atual) is None


def test_sanitizar_candles_m15_remove_tick_fora_do_bloco_antes_da_analise():
    indice = pd.date_range("2026-09-09 09:00", periods=4, freq="15min", tz="UTC")
    bons = pd.DataFrame({
        "Open": [1.1640, 1.1641, 1.1642, 1.1643],
        "High": [1.1642, 1.1643, 1.1644, 1.1645],
        "Low": [1.1638, 1.1639, 1.1640, 1.1641],
        "Close": [1.1641, 1.1642, 1.1643, 1.1644],
    }, index=indice)
    falso = pd.DataFrame({
        "Open": [1.35534], "High": [1.355355],
        "Low": [1.35534], "Close": [1.355355],
    }, index=[pd.Timestamp("2026-09-09 09:31", tz="UTC")])

    limpo = sanitizar_candles_m15(pd.concat([bons, falso]).sort_index())

    assert len(limpo) == len(bons)
    assert pd.Timestamp("2026-09-09 09:31", tz="UTC") not in limpo.index


def test_estado_publica_saude_do_sqlite(tmp_path):
    estado = Estado(
        tmp_path / "web", arquivo_aprendizado=tmp_path / "sinais.json",
        banco_aprendizado=tmp_path / "monitor.sqlite3",
    )
    estado.registrar_sinal("EURUSD", "2026-09-04 12:00:00", {
        "classe": "forex", "direcao": "buy", "entrada_valida": True,
    })
    estado.salvar()
    payload = json.loads((tmp_path / "web" / "mercado.json").read_text(encoding="utf-8"))
    assert payload["saudeDados"]["armazenamento"] == "SQLite"
    assert payload["saudeDados"]["eventos"] == 1


def test_registrar_sinal_informa_se_evento_foi_novo(tmp_path):
    estado = Estado(
        tmp_path / "web", arquivo_aprendizado=tmp_path / "sinais.json",
        banco_aprendizado=tmp_path / "monitor.sqlite3",
    )
    info = {"classe": "forex", "direcao": "buy", "entrada_valida": False}

    assert estado.registrar_sinal("EURUSD", "2026-09-07 12:00:00", info, "fibo_m15") is True
    assert estado.registrar_sinal("EURUSD", "2026-09-07 12:00:00", info, "fibo_m15") is False
    assert len(estado._aprendizado) == 1


def test_decisao_deixa_sinal_fora_da_janela_em_estudo():
    resultado = decisao_entrada(True, True, False)

    assert resultado["sinal"]
    assert not resultado["entrada_valida"]
    assert resultado["estado_entrada"] == "ESTUDO — NÃO OPERAR"
    assert [item["ok"] for item in resultado["checklist"]] == [True, True, False]


def test_decisao_so_valida_com_todas_as_regras():
    resultado = decisao_entrada(True, True, True)

    assert resultado["entrada_valida"]
    assert resultado["estado_entrada"] == "ENTRADA VÁLIDA"


def test_simulador_usa_alvo_de_estudo_quando_nao_ha_entrada_valida():
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    h = {
        "vela": str(inicio), "alvos": None,
        "alvos_estudo": {"sl": 0.99, "tp1": 1.02},
    }
    candles = pd.DataFrame(
        {"Open": [1.0], "High": [1.03], "Low": [1.0], "Close": [1.02]},
        index=[inicio + pd.Timedelta(minutes=15)],
    )

    resultado = Estado._resolver_tp_sl(h, candles)

    assert resultado["desfecho"] == "win_tp1"
    assert resultado["bateu_tp1_sem_stop"] is True
    assert resultado["primeiro_toque"] == "tp1"
    assert resultado["vela_desfecho"] == str(candles.index[0])


def _serie_fibo_alta() -> tuple[pd.DataFrame, pd.Series]:
    indice = pd.date_range("2026-08-25", periods=120, freq="15min")
    fechamento = np.r_[
        np.linspace(100, 104, 88), np.linspace(104, 110, 29),
        [106.0, 106.4, 107.1],  # retração na zona e confirmação compradora
    ]
    abertura = np.r_[fechamento[0], fechamento[:-1]]
    candles = pd.DataFrame({
        "Open": abertura,
        "High": np.maximum(abertura, fechamento) + .2,
        "Low": np.minimum(abertura, fechamento) - .2,
        "Close": fechamento,
    }, index=indice)
    return candles, pd.Series(.55, index=indice)


def test_plano_fibo_m15_qualifica_retracao_com_tendencias_alinhadas():
    candles, atr = _serie_fibo_alta()

    plano = plano_fibo(candles, atr, "EURUSD", {"estado": "sem_risco"})

    assert plano["disponivel"]
    assert plano["direcao"] == "buy"
    assert plano["qualificada"]
    assert plano["rr"] >= 1.5
    assert plano["zona_inf"] < plano["zona_sup"]
    assert plano["alvos"]["tp2"] > plano["alvos"]["tp1"]


def test_confluencia_local_entrega_plano_visual_sem_virar_sinal_operacional():
    indice = pd.date_range("2026-08-25", periods=120, freq="15min")
    fechamento = np.linspace(100, 112, 120)
    abertura = np.r_[fechamento[0] - .10, fechamento[:-1] - .10]
    candles = pd.DataFrame({
        "Open": abertura, "High": np.maximum(abertura, fechamento) + .15,
        "Low": np.minimum(abertura, fechamento) - .15, "Close": fechamento,
    }, index=indice)
    # Deslocamento que deixa FVG comprador: a terceira mínima fica acima da
    # máxima da primeira vela do padrão de três candles.
    candles.iloc[-3] = [111.1, 111.3, 110.9, 111.0]
    candles.iloc[-2] = [111.0, 113.1, 110.9, 112.9]
    candles.iloc[-1] = [112.9, 113.4, 111.6, 113.2]
    atr = pd.Series(.50, index=indice)

    plano = plano_confluencia_local(candles, atr, "XAUUSD", {"estado": "sem_risco"})

    assert plano["disponivel"]
    assert plano["direcao"] == "buy"
    assert 0 <= plano["score"] <= 10
    assert len(plano["checklist"]) == 7
    assert plano["estrutura"]["minimo"] < plano["estrutura"]["equilibrio"] < plano["estrutura"]["maximo"]
    assert plano["alvos"]["sl"] < plano["alvos"]["entrada"] < plano["alvos"]["tp1"]


def test_tp1_curto_forex_usa_rejeicao_fibo_e_alvo_382():
    candles, atr = _serie_fibo_alta()
    # Para TP1 curto, o pavio de confirmação precisa deixar um stop tático
    # compacto. A estrutura Fibo original continua válida com um pavio maior.
    candles.loc[candles.index[-1], "Low"] = 106.9

    plano = plano_tp1_curto_forex(candles, atr, "EURUSD", {"estado": "sem_risco"})

    assert plano["disponivel"]
    assert plano["sinal_estudo"]
    assert plano["direcao"] == "buy"
    assert 1.2 <= plano["rr"] <= 3.0
    assert plano["alvos"]["tp1"] == pytest.approx(plano_fibo(candles, atr, "EURUSD")["fib382"])


def test_tp1_curto_nao_serve_para_cripto():
    candles, atr = _serie_fibo_alta()

    plano = plano_tp1_curto_forex(candles, atr, "BTCUSD")

    assert not plano["disponivel"]


def test_fvg_m15_mostra_gap_bullish_ainda_nao_preenchido():
    indice = pd.date_range("2026-09-08 10:00", periods=6, freq="15min")
    candles = pd.DataFrame({
        "Open": [100, 101, 103, 104, 104, 104],
        "High": [101, 102, 105, 105, 105, 105],
        "Low": [99, 100, 103, 102, 102, 102],
        "Close": [100, 101, 104, 104, 104, 104],
    }, index=indice)
    atr = pd.Series([2.0] * len(candles), index=indice)

    fvg = detectar_fvg_m15(candles, atr)

    assert fvg["disponivel"]
    assert fvg["direcao"] == "buy"
    assert fvg["zona_inf"] == pytest.approx(101.0)
    assert fvg["zona_sup"] == pytest.approx(103.0)
    assert fvg["estado"] == "FVG BUY ATIVO"


def test_ouro_movimento_aguarda_dado_usd_sem_chamar_noticia_de_bloqueio():
    indice = pd.date_range("2026-09-07 00:00", periods=104, freq="15min")
    base = np.linspace(4300.0, 4350.0, len(indice))
    candles = pd.DataFrame({
        "Open": base - .2, "High": base + .4, "Low": base - .5, "Close": base,
    }, index=indice)
    # Impulso e FVG BUY, seguido por retorno/rejeição dentro da zona.
    candles.iloc[-5:] = [[4345, 4346, 4344, 4345], [4346, 4349, 4346, 4348],
                         [4349, 4351, 4347, 4350], [4349, 4350, 4346.5, 4347],
                         [4347, 4350, 4346.5, 4349.5]]
    atr = pd.Series([2.0] * len(candles), index=indice)

    plano = plano_ouro_movimento(candles, atr, "XAUUSD", {"estado": "janela_risco"})

    assert plano["disponivel"]
    assert not plano["sinal_estudo"]
    assert plano["estado"] == "NOTÍCIA USD — AGUARDAR DADO"


def test_noticias_do_dia_expoe_como_usar_resultado_publicado():
    agora = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)
    class EventoFalso:
        titulo, moeda, impacto = "CPI m/m", "USD", "high"
        quando = agora
        actual, forecast, previous = "0.1", "0.3", "0.2"
        def resultado_direcao(self, ativo): return {"direcao": "BUY"}
        def sugestao(self, ativo): return None
    class CalendarioFalso:
        def eventos_do_ativo(self, ativo): return [EventoFalso()]

    agenda = noticias_do_dia(CalendarioFalso(), "XAUUSD", agora)

    assert agenda[0]["direcao"] == "BUY"
    assert "após confirmação M15" in agenda[0]["uso"]


def test_groq_grafico_nao_pode_inventar_direcao_contraria():
    resposta = Mock(status_code=200)
    resposta.json.return_value = {"choices": [{"message": {"content":
        '{"veredicto":"SELL","confianca":"ALTA","motivo":"Texto."}'
    }}]}
    contexto = {"plano": {"qualificada": True, "direcao": "BUY"}, "candles_fechados": []}
    with patch.dict("os.environ", {"GROQ_API_KEY": "teste"}, clear=False), \
         patch("iqoption_m5.ia._req.post", return_value=resposta):
        parecer = segunda_opiniao_grafico(contexto)

    assert parecer is not None
    assert parecer["veredicto"] == "AGUARDAR"


def test_groq_grafico_expoe_falha_de_conexao_sem_executar_ordem():
    contexto = {"plano": {"qualificada": False}, "candles_fechados": []}
    with patch.dict("os.environ", {"GROQ_API_KEY": "teste"}, clear=False), \
         patch("iqoption_m5.ia._req.post", side_effect=requests.ConnectionError("offline")):
        parecer = segunda_opiniao_grafico(contexto)

    assert parecer is not None
    assert parecer["status"] == "INDISPONÍVEL"
    assert parecer["veredicto"] == "AGUARDAR"
    assert "conexão" in parecer["motivo"].lower()


def test_groq_grafico_usa_openai_como_reserva_quando_groq_recusa():
    groq = Mock(status_code=503)
    openai = Mock(status_code=200)
    openai.json.return_value = {"choices": [{"message": {"content":
        '{"veredicto":"BUY","confianca":"MEDIA","motivo":"Rejeição confirmada."}'
    }}]}
    contexto = {"plano": {"qualificada": True, "direcao": "BUY"}, "candles_fechados": []}
    with patch.dict("os.environ", {"GROQ_API_KEY": "groq", "OPENAI_API_KEY": "openai"}, clear=False), \
         patch("iqoption_m5.ia._req.post", side_effect=[groq, openai]):
        parecer = segunda_opiniao_grafico(contexto)

    assert parecer is not None
    assert parecer["status"] == "DISPONÍVEL"
    assert parecer["fonte"] == "OPENAI"
    assert parecer["veredicto"] == "BUY"


def test_ia_grafico_so_exibe_tp_curto_ja_calculado_no_plano():
    resposta = Mock(status_code=200)
    resposta.json.return_value = {"choices": [{"message": {"content":
        '{"veredicto":"BUY","alvo":"TP1","confianca":"ALTA","motivo":"Tendência e rejeição alinhadas."}'
    }}]}
    contexto = {"plano": {"qualificada": True, "direcao": "BUY", "tp1": 1.23456}, "candles_fechados": []}
    with patch.dict("os.environ", {"GROQ_API_KEY": "teste"}, clear=False), \
         patch("iqoption_m5.ia._req.post", return_value=resposta):
        parecer = segunda_opiniao_grafico(contexto)

    assert parecer["alvo_codigo"] == "TP1"
    assert parecer["alvo_curto"] == pytest.approx(1.23456)


def test_simulador_fibo_sell_usa_low_para_tp_e_high_para_stop():
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    h = {
        "vela": str(inicio), "direcao": "sell", "alvos_estudo": {"sl": 1.02, "tp1": .98},
    }
    candles = pd.DataFrame(
        {"Open": [1.0], "High": [1.01], "Low": [.97], "Close": [.98]},
        index=[inicio + pd.Timedelta(minutes=15)],
    )

    assert Estado._resolver_tp_sl(h, candles)["desfecho"] == "win_tp1"


def _serie_varredura_buy() -> tuple[pd.DataFrame, pd.Series]:
    indice = pd.date_range("2026-09-01", periods=81, freq="15min")
    fechamento = np.r_[
        np.linspace(99.0, 101.0, 70),
        np.linspace(100.0, 101.0, 10),
        100.02,
    ]
    abertura = fechamento.copy()
    candles = pd.DataFrame({
        "Open": abertura, "High": fechamento + .05,
        "Low": fechamento - .05, "Close": fechamento,
    }, index=indice)
    # Última vela varre o piso dos 10 candles anteriores e fecha dentro dele.
    candles.iloc[-1] = [99.98, 100.12, 99.78, 100.04]
    return candles, pd.Series(.50, index=indice)


def test_varredura_v2_exige_pavio_e_reentrada_no_range():
    candles, atr = _serie_varredura_buy()

    plano = plano_varredura_liquidez(candles, atr, "EURUSD", {"estado": "sem_risco"})

    assert plano["disponivel"]
    assert plano["sinal_estudo"]
    assert plano["direcao"] == "buy"
    assert plano["reclaim"]
    assert plano["rr"] >= 1.5
    assert plano["alvos"]["modo_entrada"] == "stop"


def test_varredura_v2_nao_aceita_fechamento_fora_do_range():
    candles, atr = _serie_varredura_buy()
    candles.iloc[-1, candles.columns.get_loc("Close")] = 99.70

    plano = plano_varredura_liquidez(candles, atr, "EURUSD", {"estado": "sem_risco"})

    assert not plano["sinal_estudo"]
    assert not plano["reclaim"]


def _serie_bof_sell() -> tuple[pd.DataFrame, pd.Series]:
    inicio = pd.Timestamp("2026-09-07 00:00:00")
    highs = [101, 102, 103, 102, 101, 100, 101, 102, 101.5, 102, 101.8, 102]
    lows = [99, 98, 97, 96, 95, 90, 95, 96, 95.5, 96, 95.8, 96]
    linhas, indice = [], []
    for bloco, (hi, lo) in enumerate(zip(highs, lows)):
        meio = (hi + lo) / 2
        for minuto in range(6):
            indice.append(inicio + pd.Timedelta(minutes=30 * bloco + 5 * minuto))
            linhas.append([meio, hi, lo, meio])
    # Varre a máxima M30 de 103, fecha dentro e confirma SELL no M5 seguinte.
    indice.extend([inicio + pd.Timedelta(hours=6),
                   inicio + pd.Timedelta(hours=6, minutes=5)])
    linhas.extend([[102.9, 103.4, 102.4, 102.7],
                   [102.7, 102.8, 101.5, 102.0]])
    candles = pd.DataFrame(linhas, columns=["Open", "High", "Low", "Close"], index=indice)
    return candles, pd.Series(1.0, index=indice)


def test_bof_m30_m5_xau_confirma_sell_sem_lookahead_e_com_alvo_preexistente():
    candles, atr = _serie_bof_sell()

    plano = plano_bof_m30_m5(candles, atr, "XAUUSD", {"estado": "sem_risco"})

    assert plano["disponivel"]
    assert plano["sinal_estudo"]
    assert plano["direcao"] == "sell"
    assert plano["nivel_m30"] == 103.0
    assert plano["rr"] >= 5.0
    assert plano["alvos"]["modo_entrada"] == "market_next_open"


def test_bof_m30_m5_tambem_aceita_forex_normal():
    candles, atr = _serie_bof_sell()

    plano = plano_bof_m30_m5(candles, atr, "EURUSD", {"estado": "sem_risco"})

    assert plano["disponivel"]
    assert plano["sinal_estudo"]
    assert plano["direcao"] == "sell"


def test_bof_m30_m5_nao_e_aplicado_em_cripto():
    candles, atr = _serie_bof_sell()

    plano = plano_bof_m30_m5(candles, atr, "BTCUSD", {"estado": "sem_risco"})

    assert not plano["disponivel"]
    assert not plano["sinal_estudo"]


def test_simulador_bof_entra_na_abertura_da_proxima_vela():
    inicio = pd.Timestamp("2026-09-07 06:05:00")
    h = {
        "tipo": "bof_m30_m5", "vela": str(inicio), "direcao": "sell",
        "timeframe_segundos": 300,
        "alvos_estudo": {
            "entrada": 102.0, "sl": 103.5, "tp1": 99.0,
            "modo_entrada": "market_next_open",
        },
    }
    candles = pd.DataFrame(
        {"Open": [101.8], "High": [102.0], "Low": [98.8], "Close": [99.0]},
        index=[inicio + pd.Timedelta(minutes=5)],
    )

    resultado = Estado._resolver_tp_sl(h, candles, horizonte_velas=12)

    assert resultado["desfecho"] == "win_tp1"
    assert resultado["preenchida"] is True
    assert resultado["vela_preenchimento"] == str(candles.index[0])


def test_estado_nao_resolve_bof_m5_com_candles_m15(tmp_path):
    inicio = pd.Timestamp("2026-09-07 06:05:00")
    estado = Estado(tmp_path / "web", arquivo_aprendizado=tmp_path / "sinais.json")
    estado.registrar_sinal("XAUUSD", str(inicio), {
        "classe": "ouro", "direcao": "sell", "entrada_valida": False,
        "timeframe_segundos": 300, "horizonte_velas": 12,
        "horizonte_longo_velas": 24,
        "alvos_estudo": {"entrada": 102.0, "sl": 103.5, "tp1": 99.0,
                          "modo_entrada": "market_next_open"},
    }, "bof_m30_m5")
    candles_m15 = pd.DataFrame(
        {"Open": [102.0], "High": [104.0], "Low": [98.0], "Close": [99.0]},
        index=[inicio + pd.Timedelta(minutes=15)],
    )

    estado.resolver("XAUUSD", candles_m15)

    assert estado.historico[0]["simulacao"]["desfecho"] == "aguardando"
    assert estado._amostra_bof()["horizonte"] == "1h"


def test_simulador_espera_gatilho_de_ordem_stop():
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    h = {
        "vela": str(inicio), "direcao": "buy",
        "alvos_estudo": {
            "entrada": 1.01, "sl": .99, "tp1": 1.04, "modo_entrada": "stop",
        },
    }
    indice = pd.date_range(inicio + pd.Timedelta(minutes=15), periods=3, freq="15min")
    candles = pd.DataFrame({
        "Open": [1.0, 1.0, 1.02], "High": [1.005, 1.02, 1.045],
        "Low": [.995, 1.0, 1.015], "Close": [1.0, 1.015, 1.04],
    }, index=indice)

    resultado = Estado._resolver_tp_sl(h, candles)

    assert resultado["desfecho"] == "win_tp1"
    assert resultado["vela_preenchimento"] == str(indice[1])


def test_simulador_salva_stop_antes_do_tp1():
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    h = {
        "vela": str(inicio), "direcao": "buy",
        "alvos_estudo": {"sl": .99, "tp1": 1.02},
    }
    candles = pd.DataFrame(
        {"Open": [1.0], "High": [1.01], "Low": [.98], "Close": [.99]},
        index=[inicio + pd.Timedelta(minutes=15)],
    )

    resultado = Estado._resolver_tp_sl(h, candles)

    assert resultado["desfecho"] == "loss_sl"
    assert resultado["bateu_tp1_sem_stop"] is False
    assert resultado["primeiro_toque"] == "stop"


def test_simulador_nao_inventa_ordem_quando_tp1_e_stop_ocorrem_na_mesma_vela():
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    h = {
        "vela": str(inicio), "direcao": "buy",
        "alvos_estudo": {"sl": .99, "tp1": 1.02},
    }
    candles = pd.DataFrame(
        {"Open": [1.0], "High": [1.03], "Low": [.98], "Close": [1.01]},
        index=[inicio + pd.Timedelta(minutes=15)],
    )

    resultado = Estado._resolver_tp_sl(h, candles)

    assert resultado["desfecho"] == "ambíguo_mesma_vela"
    assert resultado["bateu_tp1_sem_stop"] is False
    assert resultado["primeiro_toque"] == "tp1_e_stop_mesma_vela"


def test_simulador_aguarda_24_velas_antes_de_expirar():
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    h = {
        "vela": str(inicio), "direcao": "buy",
        "alvos_estudo": {"entrada": 1.0, "sl": .99, "tp1": 1.02},
    }
    candles = pd.DataFrame(
        {"Open": [1.0], "High": [1.01], "Low": [.995], "Close": [1.005]},
        index=[inicio + pd.Timedelta(minutes=15)],
    )

    resultado = Estado._resolver_tp_sl(h, candles, horizonte_velas=24)

    assert resultado["desfecho"] == "aguardando"
    assert resultado["velas"] == 1


def test_estado_recupera_sinal_pendente_apos_reinicio(tmp_path):
    aprendizado = tmp_path / "sinais.json"
    estado = Estado(tmp_path / "web", arquivo_aprendizado=aprendizado)
    estado.registrar_sinal("EURUSD", "2026-09-01 12:00:00", {
        "classe": "forex", "direcao": "buy", "preco": 1.0,
        "alvos_estudo": {"entrada": 1.0, "sl": .99, "tp1": 1.02},
    })

    restaurado = Estado(tmp_path / "web", arquivo_aprendizado=aprendizado)

    assert len(restaurado.historico) == 1
    assert restaurado.historico[0]["ativo"] == "EURUSD"


def test_monitor_impede_duas_instancias_na_mesma_porta():
    primeira = adquirir_trava_monitor(0)
    porta = primeira.getsockname()[1]
    try:
        try:
            adquirir_trava_monitor(porta)
            repetiu = True
        except OSError:
            repetiu = False
        assert not repetiu
    finally:
        primeira.close()


def test_resolver_reavalia_sinal_ate_tp_ou_fim_do_horizonte(tmp_path):
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    estado = Estado(tmp_path / "web", arquivo_aprendizado=tmp_path / "sinais.json")
    estado.registrar_sinal("EURUSD", str(inicio), {
        "classe": "forex", "direcao": "buy", "preco": 1.0,
        "alvos_estudo": {"entrada": 1.0, "sl": .99, "tp1": 1.02},
    })
    primeira = pd.DataFrame(
        {"Open": [1.0], "High": [1.01], "Low": [.995], "Close": [1.005]},
        index=[inicio + pd.Timedelta(minutes=15)],
    )
    estado.resolver("EURUSD", primeira)
    assert estado.historico[0]["simulacao"]["desfecho"] == "aguardando"

    segunda = pd.concat([primeira, pd.DataFrame(
        {"Open": [1.005], "High": [1.025], "Low": [1.0], "Close": [1.02]},
        index=[inicio + pd.Timedelta(minutes=30)],
    )])
    estado.resolver("EURUSD", segunda)

    assert estado.historico[0]["simulacao"]["desfecho"] == "win_tp1"
    assert estado.historico[0]["simulacao"]["resultado_r"] == 2.0


def test_simulador_expirado_registra_saida_e_resultado_em_r():
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    h = {
        "vela": str(inicio), "direcao": "buy",
        "alvos_estudo": {"entrada": 1.0, "sl": .99, "tp1": 1.02},
    }
    indice = pd.date_range(inicio + pd.Timedelta(minutes=15), periods=24, freq="15min")
    candles = pd.DataFrame({
        "Open": np.repeat(1.0, 24), "High": np.repeat(1.01, 24),
        "Low": np.repeat(.995, 24), "Close": np.repeat(1.005, 24),
    }, index=indice)

    resultado = Estado._resolver_tp_sl(h, candles)

    assert resultado["desfecho"] == "expirado_6h"
    assert resultado["preco_saida"] == 1.005
    assert resultado["resultado_r"] == .5


def test_fibo_nao_conta_tp_quando_ordem_limite_nao_foi_preenchida():
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    h = {
        "tipo": "fibo_m15", "vela": str(inicio), "direcao": "buy",
        "alvos_estudo": {
            "entrada": 1.0, "sl": .99, "tp1": 1.02, "modo_entrada": "limite",
        },
    }
    indice = pd.date_range(inicio + pd.Timedelta(minutes=15), periods=24, freq="15min")
    candles = pd.DataFrame({
        "Open": np.repeat(1.01, 24), "High": np.repeat(1.025, 24),
        "Low": np.repeat(1.005, 24), "Close": np.repeat(1.02, 24),
    }, index=indice)

    resultado = Estado._resolver_tp_sl(h, candles)

    assert resultado["desfecho"] == "nao_executada_6h"
    assert resultado["preenchida"] is False
    assert resultado["resultado_r"] is None


def test_monitor_inclui_cripto_e_ouro_sem_herdar_validacao_de_forex():
    assert {"BTCUSD", "ETHUSD", "XRPUSD", "XAUUSD"} <= set(ATIVOS)
    assert CLASSE["BTCUSD"] == "crypto"
    assert CLASSE["XAUUSD"] == "ouro"
    assert janela_validada_para("crypto", 8)
    assert janela_validada_para("forex", 21)
    assert not janela_validada_para("forex", 8)
    assert not janela_validada_para("ouro", 21)


def test_estado_publica_todos_os_ativos_antes_da_primeira_varredura(tmp_path):
    estado = Estado(tmp_path)

    estado.salvar()
    painel = json.loads((tmp_path / "mercado.json").read_text(encoding="utf-8"))

    assert set(painel["ativos"]) == set(ATIVOS)
    assert all("disponivel" in item for item in painel["ativos"].values())


def test_estado_substitui_grafico_corrompido_por_placeholder_valido(tmp_path):
    (tmp_path / "mkt_BTCUSD.json").write_bytes(b"\x00" * 20)

    Estado(tmp_path)
    grafico = json.loads((tmp_path / "mkt_BTCUSD.json").read_text(encoding="utf-8"))

    assert grafico == {"candles": [], "sup": [], "inf": [], "estado": "aguardando_dados"}


def test_painel_sobe_antes_da_conexao_com_a_iq():
    fonte = (Path(__file__).resolve().parents[1] / "monitor_mercado.py").read_text(
        encoding="utf-8"
    )
    main = fonte[fonte.index("def main()") :]

    assert main.index("g.iniciar(") < main.index("conectar_somente_leitura(")


def test_movimento_usa_unidade_correta_para_ouro_e_cripto():
    assert unidade_movimento("EURUSD") == (0.0001, "pips")
    assert unidade_movimento("XAUUSD") == (0.01, "pontos")
    assert unidade_movimento("BTCUSD") == (1.0, "USD")


def test_grafico_ouro_descarta_banda_anormal_que_achata_candles(tmp_path):
    indice = pd.date_range("2026-09-03 04:00", periods=60, freq="15min")
    fechamento = np.linspace(4420.0, 4506.0, 60)
    candles = pd.DataFrame({
        "Open": fechamento - 1,
        "High": fechamento + 2,
        "Low": fechamento - 2,
        "Close": fechamento,
    }, index=indice)
    regressao = pd.DataFrame({
        "banda_sup": np.r_[np.repeat(4510.0, 59), 5993.0],
        "banda_inf": np.r_[np.repeat(4410.0, 59), 2838.0],
    }, index=indice)

    Estado(tmp_path).salvar_candles("XAUUSD", candles, regressao)
    salvo = json.loads((tmp_path / "mkt_XAUUSD.json").read_text(encoding="utf-8"))

    assert salvo["sup"][-1] is None
    assert salvo["inf"][-1] is None
    assert max(v for v in salvo["sup"] if v is not None) < 4600
    assert min(v for v in salvo["inf"] if v is not None) > 4300


def test_leitura_fluxo_publica_vwap_e_area_de_valor_sem_inventar_volume():
    indice = pd.date_range("2026-09-03 07:00", periods=32, freq="15min")
    fechamento = np.linspace(1.1000, 1.1060, len(indice))
    candles = pd.DataFrame({
        "Open": fechamento - .00015,
        "High": fechamento + .00030,
        "Low": fechamento - .00030,
        "Close": fechamento,
        "Volume": np.linspace(80, 140, len(indice)),
    }, index=indice)
    atr = pd.Series(.0008, index=indice)

    leitura = leitura_fluxo_sessao(candles, atr, "EURUSD")

    assert leitura["disponivel"]
    assert leitura["fonte_volume"] == "tick-volume M15"
    assert leitura["val"] <= leitura["poc"] <= leitura["vah"]
    assert leitura["val"] <= leitura["vwap"] <= leitura["vah"]
    assert 0 <= leitura["qualidade"] <= 100


def test_leitura_fluxo_sem_volume_fica_explicitamente_como_aproximacao():
    indice = pd.date_range("2026-09-03 00:00", periods=32, freq="15min")
    fechamento = np.linspace(100.0, 102.0, len(indice))
    candles = pd.DataFrame({
        "Open": fechamento - .05, "High": fechamento + .10,
        "Low": fechamento - .10, "Close": fechamento,
    }, index=indice)

    leitura = leitura_fluxo_sessao(candles, pd.Series(.3, index=indice), "XAUUSD")

    assert leitura["disponivel"]
    assert leitura["fonte_volume"] == "atividade uniforme (sem volume)"


def _serie_orb_buy() -> tuple[pd.DataFrame, pd.Series]:
    indice = pd.date_range(end="2026-09-04 13:45", periods=500, freq="15min")
    fechamento = np.linspace(1.1000, 1.1990, len(indice))
    candles = pd.DataFrame({
        "Open": fechamento - .0002, "High": fechamento + .0003,
        "Low": fechamento - .0003, "Close": fechamento,
    }, index=indice)
    candles.loc["2026-09-04 13:00"] = [1.1990, 1.2000, 1.1985, 1.1995]
    candles.loc["2026-09-04 13:15"] = [1.1996, 1.2002, 1.1996, 1.2001]
    candles.loc["2026-09-04 13:30"] = [1.2001, 1.2016, 1.1999, 1.2014]
    candles.loc["2026-09-04 13:45"] = [1.2014, 1.2022, 1.2008, 1.2020]
    return candles, pd.Series(.0010, index=indice)


def test_orb_exige_fechamento_fora_fvg_e_vies_h1_h4():
    candles, atr = _serie_orb_buy()

    orb = plano_orb_sessao(candles, atr, "EURUSD")

    assert orb["disponivel"]
    assert orb["sessao"] == "NOVA YORK"
    assert orb["direcao"] == "buy"
    assert orb["fvg"]
    assert orb["h1"] == orb["h4"] == "alta"
    assert orb["sinal_estudo"]
    assert orb["alvos"]["tp2"] > orb["alvos"]["tp1"] > orb["alvos"]["entrada"]


def test_orb_nao_confunde_pavio_com_rompimento():
    candles, atr = _serie_orb_buy()
    candles.loc["2026-09-04 13:45", "Close"] = 1.1998

    orb = plano_orb_sessao(candles, atr, "EURUSD")

    assert orb["direcao"] == "neutro"
    assert not orb["sinal_estudo"]


def test_orb_nao_inventa_abertura_para_cripto_24h():
    candles, atr = _serie_orb_buy()

    orb = plano_orb_sessao(candles, atr, "BTCUSD")

    assert not orb["disponivel"]
    assert "24/7" in orb["motivo"]


# ── Wilson CI ──────────────────────────────────────────────────────────────

def test_wilson_ci_retorna_none_para_n_zero():
    assert wilson_ci(0, 0) is None


def test_wilson_ci_50pct_amostra_pequena_tem_intervalo_amplo():
    lo, hi = wilson_ci(5, 10)
    assert lo < 50.0 < hi
    assert hi - lo > 30


def test_wilson_ci_limite_fisico_nunca_extrapola_0_100():
    lo, hi = wilson_ci(0, 1)
    assert lo >= 0.0
    hi2, lo2 = wilson_ci(1, 1)
    assert hi2 <= 100.0


def test_wilson_ci_amostra_grande_estreita_intervalo():
    # WR 50% com n=2000: IC esperado ≈ [47.8, 52.2] → largura ≈ 4.4%
    lo, hi = wilson_ci(1000, 2000)
    assert hi - lo < 5


def test_wilson_ci_coincide_aproximadamente_com_referencia_conhecida():
    # 55 wins em 100: referência teórica ≈ [44.9, 64.7]
    lo, hi = wilson_ci(55, 100)
    assert 44.0 <= lo <= 46.0
    assert 64.0 <= hi <= 66.0


# ── _resumo_simulacoes com IC e maturidade ─────────────────────────────────

def _itens_simulados(n_win: int, n_loss: int) -> list[dict]:
    itens = []
    for _ in range(n_win):
        itens.append({"simulacao": {"desfecho": "win_tp1", "resultado_r": 2.5}})
    for _ in range(n_loss):
        itens.append({"simulacao": {"desfecho": "loss_sl", "resultado_r": -1.0}})
    return itens


def test_resumo_inclui_ic95_e_maturidade():
    # 14 resolvidos: abaixo do limiar de 30 → INSUFICIENTE
    itens = _itens_simulados(10, 4)
    r = Estado._resumo_simulacoes(itens)

    assert r["winrate"] == pytest.approx(71.4, abs=0.1)
    assert r["ic_95"] is not None
    lo, hi = r["ic_95"]
    assert lo < 71.4 < hi
    assert r["maturidade"] == "INSUFICIENTE"
    assert r["amostra_suficiente"] is False


def test_resumo_maturidade_observar_com_30_a_99_resolvidos():
    itens = _itens_simulados(18, 17)  # 35 resolvidos
    r = Estado._resumo_simulacoes(itens)

    assert r["maturidade"] == "OBSERVAR"
    assert r["amostra_suficiente"] is True


def test_resumo_maturidade_candidata_com_100_a_299():
    itens = _itens_simulados(80, 70)  # 150 resolvidos
    r = Estado._resumo_simulacoes(itens)

    assert r["maturidade"] == "CANDIDATA"


def test_resumo_maturidade_aprovada_com_300_ou_mais():
    itens = _itens_simulados(200, 150)  # 350 resolvidos
    r = Estado._resumo_simulacoes(itens)

    assert r["maturidade"] == "APROVADA"


def test_resumo_sem_resolvidos_ic_none_e_insuficiente():
    itens = [{"simulacao": {"desfecho": "aguardando"}}] * 5
    r = Estado._resumo_simulacoes(itens)

    assert r["ic_95"] is None
    assert r["maturidade"] == "INSUFICIENTE"
    assert r["amostra_suficiente"] is False
    assert r["winrate"] is None


def test_resumo_nao_inverte_resultado_desconhecido_em_loss():
    itens = [{"simulacao": {"desfecho": "aguardando"}}]
    r = Estado._resumo_simulacoes(itens)

    assert r["losses"] == 0
    assert r["wins"] == 0


# ── HTML do Monitor ────────────────────────────────────────────────────────

def test_html_tem_cartao_decisao_principal():
    assert 'id="decisao-principal"' in monitor_mercado._HTML
    assert "renderDecisaoPrincipal" in monitor_mercado._HTML
    assert 'id="btn-som-monitor"' in monitor_mercado._HTML
    assert "alertarNovoEvento" in monitor_mercado._HTML
    assert "dadosMonitorAoVivo" in monitor_mercado._HTML
    assert 'id="tp1-curto"' in monitor_mercado._HTML
    assert "renderTp1Curto" in monitor_mercado._HTML
    assert 'id="fvg"' in monitor_mercado._HTML
    assert "renderFvg" in monitor_mercado._HTML
    assert "FVG M15" in monitor_mercado._HTML
    assert 'id="ouro-movimento"' in monitor_mercado._HTML
    assert "renderOuroMovimento" in monitor_mercado._HTML
    assert "OURO — MOVIMENTO A FAVOR M15" in monitor_mercado._HTML
    assert "NOTÍCIAS USD HOJE" in monitor_mercado._HTML
    assert "noticiasGrafico" in monitor_mercado._HTML
    assert 'id="ia-grafico"' in monitor_mercado._HTML
    assert "renderIaGrafico" in monitor_mercado._HTML
    assert "lerGroqSelecionado" in monitor_mercado._HTML
    assert "Analisar momento + TP curto" in monitor_mercado._HTML
    assert "TP curto aprovado" in monitor_mercado._HTML
    assert "/opiniao_groq_grafico" in monitor_mercado._HTML
    assert "ouro prioriza o scanner próprio quando qualificado" in monitor_mercado._HTML


def test_html_tem_entrada_sl_tp_na_tabela_de_historico():
    assert "entrada · SL · TP" in monitor_mercado._HTML
    assert "hist-alvos" in monitor_mercado._HTML


def test_html_mostra_ic_e_maturidade_nos_estudos():
    assert "maturidadeBadge" in monitor_mercado._HTML
    assert "ic_95" in monitor_mercado._HTML
    assert "AMOSTRA INSUFICIENTE" in monitor_mercado._HTML


def test_html_dossie_exibe_plano_completo():
    assert "PLANO (estudo" in monitor_mercado._HTML
    assert "Invalidação / SL" in monitor_mercado._HTML or "Invalidação" in monitor_mercado._HTML
    assert "textoSimulacao(h.simulacao,h)" in monitor_mercado._HTML


def test_estado_publica_campos_ic_e_maturidade_no_json(tmp_path):
    estado = Estado(
        tmp_path / "web", arquivo_aprendizado=tmp_path / "sinais.json",
        banco_aprendizado=tmp_path / "monitor.sqlite3",
    )
    for i in range(15):
        desfecho = "win_tp1" if i < 9 else "loss_sl"
        estado._aprendizado.append({
            "id": f"fibo:EURUSD:{i}", "quando": "2026-09-04T12:00:00+00:00",
            "ativo": "EURUSD", "tipo": "fibo_m15", "entrada_valida": False,
            "simulacao": {"desfecho": desfecho, "resultado_r": 2.5 if desfecho == "win_tp1" else -1.0},
        })
    estado.salvar()

    payload = json.loads((tmp_path / "web" / "mercado.json").read_text(encoding="utf-8"))
    fibo = payload["amostraFibo"]

    assert "maturidade" in fibo
    assert fibo["maturidade"] == "INSUFICIENTE"
    assert "amostra_suficiente" in fibo
    assert fibo["amostra_suficiente"] is False
    assert "ic_95" in fibo
