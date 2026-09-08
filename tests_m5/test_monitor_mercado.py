import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import monitor_mercado
from monitor_mercado import (
    ATIVOS, CLASSE, Estado, adquirir_trava_monitor, decisao_entrada, janela_validada_para,
    leitura_fluxo_sessao, plano_fibo, plano_orb_sessao,
    plano_varredura_liquidez, unidade_movimento, wilson_ci,
)


def test_painel_separa_agora_grafico_entradas_estudos_e_dados():
    for visao in ("agora", "grafico", "entradas", "estudos", "dados"):
        assert f'data-view="{visao}"' in monitor_mercado._HTML
    assert 'id="filtro-tipo"' in monitor_mercado._HTML
    assert 'id="saude"' in monitor_mercado._HTML


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
    assert "textoSimulacao(h.simulacao)" in monitor_mercado._HTML


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
