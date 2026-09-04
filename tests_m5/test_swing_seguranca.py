from types import SimpleNamespace
from datetime import datetime

import pandas as pd
import pytest

import backtest_swing
from iqoption_swing.calendario_swing import direcao_noticia_para_ativo
from iqoption_swing.config_swing import SwingConfig
from iqoption_swing.estrategia_swing import AnaliseSwing
from iqoption_swing.mercado_swing import MercadoSwing
from iqoption_swing.radar_swing import avaliar_radar, radar_para_alerta
from iqoption_swing.registro_swing import RegistroSwing
from iqoption_swing.tocaia_swing import avaliar_tocaia, tocaia_para_alerta


def test_backtest_swing_libera_h4_e_d1_somente_no_fechamento() -> None:
    indice = pd.date_range("2026-08-24 00:00", periods=30, freq="1h")
    h1 = pd.DataFrame(
        {
            "Open": range(30), "High": range(1, 31), "Low": range(30),
            "Close": range(1, 31), "Volume": 1.0,
        },
        index=indice,
    )

    h4 = backtest_swing._resample_fechado(h1, "4h")
    d1 = backtest_swing._resample_fechado(h1, "1D")

    assert h4.index[0] == pd.Timestamp("2026-08-24 04:00")
    assert d1.index[0] == pd.Timestamp("2026-08-25 00:00")


def test_live_swing_descarta_contexto_em_formacao() -> None:
    indice = pd.date_range("2026-08-25 00:00", periods=3, freq="4h")
    candles = pd.DataFrame({"Close": [1.0, 2.0, 99.0]}, index=indice)

    fechados = MercadoSwing._somente_fechados(
        candles, 4 * 3600, indice[-1].timestamp() + 60
    )

    assert list(fechados.Close) == [1.0, 2.0]


def test_execucao_swing_fica_bloqueada_enquanto_edge_for_negativo() -> None:
    config = SwingConfig(executar_ordens=True)
    with pytest.raises(RuntimeError, match="estratégia Swing não validada"):
        config.validar()


def test_perdas_consecutivas_conta_apenas_cauda(tmp_path) -> None:
    registro = RegistroSwing(tmp_path / "swing.sqlite3")
    hoje = datetime.now().date().isoformat()
    with registro._sessao() as db:
        for i, lucro in enumerate([-1.0, 2.0, -1.0, -1.0]):
            db.execute(
                """INSERT INTO operacoes
                (id_ordem, ativo, direcao, setup, pontuacao, enviada_em,
                 ts_expiracao, valor, resultado, lucro)
                VALUES (?, 'EURUSD', 'call', 'teste', 8, ?, 0, 1, 'fim', ?)""",
                (str(i), f"{hoje}T0{i}:00:00", lucro),
            )

    assert registro.resumo_dia()["perdas_consecutivas"] == 2


def test_backtest_swing_usa_analise_nova_sem_pontuacao(monkeypatch) -> None:
    indice = pd.date_range("2026-01-01 00:00", periods=3000, freq="1h")
    close = [float(i) for i in range(3000)]
    h1 = pd.DataFrame(
        {
            "Open": [v - 0.2 for v in close],
            "High": [v + 3.0 for v in close],
            "Low": [v - 0.1 for v in close],
            "Close": close,
            "Volume": 1.0,
        },
        index=indice,
    )

    class EstrategiaFake:
        def __init__(self, config):
            self.config = config

        def avaliar(self, ativo, df_d1, df_h4, df_h1):
            preco = float(df_h4["Close"].iloc[-1])
            return SimpleNamespace(
                estado="ENTRAR",
                direcao="compra",
                setup="teste_analise",
                zona_entrada=(preco, preco),
                invalidacao=preco - 1.0,
                tp1=preco + 2.0,
                detalhes={},
            )

    monkeypatch.setattr(backtest_swing, "EstrategiaSwingCache", EstrategiaFake)

    cfg = SwingConfig(conta="PRACTICE", executar_ordens=False, ativos=("EURUSD",))
    resultado = backtest_swing.rodar({"EURUSD": h1}, cfg, passo_h4=200, workers=1)

    assert not resultado.empty
    assert set(resultado["direcao"]) == {"call"}
    assert set(resultado["setup"]) == {"teste_analise"}
    assert resultado["R"].max() == 2.0


def test_radar_swing_preve_busca_e_respeita_noticia() -> None:
    analise = AnaliseSwing(
        ativo="EURUSD",
        estado="ESPERAR",
        direcao="compra",
        setup="pullback_tendencia",
        zona_entrada=(1.1000, 1.1010),
        invalidacao=1.0970,
        tp1=1.1060,
        tp2=1.1120,
        rr=2.0,
        validade_candles_h4=2,
        bloqueadores=[],
        pendentes=["aguardar H1 confirmar"],
        detalhes={},
    )

    antes_da_zona = avaliar_radar(analise, 1.0985)
    na_zona = avaliar_radar(analise, 1.1005)
    noticia = avaliar_radar(analise, 1.1005, True, "CPI USD em 20min")
    noticia_a_favor = avaliar_radar(analise, 1.1005, True, "CPI EUR há 5min", "compra")
    quase_zona = avaliar_radar(analise, 1.0999, tolerancia_pips=2)

    assert antes_da_zona is not None
    assert antes_da_zona.estado == "BUSCAR_ENTRADA"
    assert antes_da_zona.alvo_provavel == 1.1000
    assert na_zona is not None
    assert na_zona.estado == "NA_ZONA"
    assert noticia is not None
    assert noticia.estado == "NOTICIA"
    assert noticia_a_favor is not None
    assert noticia_a_favor.estado == "NOTICIA_A_FAVOR"
    assert quase_zona is not None
    assert quase_zona.estado == "APROXIMANDO_ZONA"

    alerta = radar_para_alerta(na_zona, analise)
    assert alerta["id"].startswith("EURUSD:NA_ZONA")
    assert alerta["direcao"] == "call"
    assert alerta["entradaConfirmada"] is True
    assert "busca provavel 1.10600" in alerta["fatores"]


def test_registro_swing_grava_analise_monitor(tmp_path) -> None:
    registro = RegistroSwing(tmp_path / "swing.sqlite3")
    analise = AnaliseSwing(
        ativo="GBPUSD",
        estado="EVITAR",
        direcao="venda",
        setup="sr_rejeicao",
        zona_entrada=(1.2500, 1.2510),
        invalidacao=1.2550,
        tp1=1.2400,
        tp2=None,
        rr=2.0,
        validade_candles_h4=2,
        bloqueadores=["setup desligado"],
        pendentes=[],
        detalhes={"setup": "sr_rejeicao"},
    )

    registro.registrar_analise_monitor(
        analise,
        preco_atual=1.2520,
        noticia_bloqueada=True,
        noticia_desc="CPI USD há 5min",
        noticia_direcao="venda",
    )

    rows = registro.historico_analises_monitor()
    assert rows[0]["ativo"] == "GBPUSD"
    assert rows[0]["estado"] == "EVITAR"
    assert rows[0]["noticia_bloqueada"] == 1
    assert rows[0]["noticia_direcao"] == "venda"


def test_direcao_noticia_para_ativo_base_e_cotada() -> None:
    assert direcao_noticia_para_ativo("EURUSD", "EUR", "CPI", "3.0%", "2.8%") == "compra"
    assert direcao_noticia_para_ativo("EURUSD", "USD", "CPI", "3.0%", "2.8%") == "venda"
    assert direcao_noticia_para_ativo("USDJPY", "USD", "Unemployment Rate", "4.5%", "4.0%") == "venda"


def test_tocaia_swing_avisa_put_no_topo_com_rsi_alto() -> None:
    indice = pd.date_range("2026-08-26 12:00", periods=4, freq="1h")
    df = pd.DataFrame(
        {
            "Open": [0.8030, 0.8040, 0.8050, 0.8052],
            "High": [0.8040, 0.8050, 0.8060, 0.8058],
            "Low": [0.8028, 0.8038, 0.8048, 0.8050],
            "Close": [0.8040, 0.8050, 0.8054, 0.8053],
            "RSI": [55.0, 61.0, 69.0, 72.0],
        },
        index=indice,
    )
    fib = [{"nivel": 1.0, "preco": 0.80629}, {"nivel": 0.786, "preco": 0.80530}]
    canal = {"superior": [{"time": 1, "value": 0.8060}], "inferior": [{"time": 1, "value": 0.7980}]}
    niveis = {"suportes": [0.80530, 0.80453], "resistencias": [0.80629]}

    tocaia = avaliar_tocaia("USDCHF", df, fib=fib, canal=canal, niveis_sr=niveis)

    assert tocaia is not None
    assert tocaia.direcao == "put"
    assert tocaia.gatilho == 0.80530
    alerta = tocaia_para_alerta(tocaia)
    assert alerta["radarEstado"] == "TOCAIA_PUT"
    assert "gatilho se perder 0.80530" in alerta["mensagem"]
    assert alerta["alvoProvavel"] == 0.80453
    assert "gatilho 0.80530" in alerta["fatores"]


def test_tocaia_swing_nao_avisa_movimento_minusculo() -> None:
    indice = pd.date_range("2026-08-27 06:00", periods=4, freq="1h")
    df = pd.DataFrame(
        {
            "Open": [1.3588, 1.3584, 1.3580, 1.3579],
            "High": [1.3590, 1.3586, 1.3582, 1.3580],
            "Low": [1.3583, 1.3579, 1.3575, 1.3577],
            "Close": [1.3584, 1.3580, 1.3578, 1.35793],
            "RSI": [38.0, 35.0, 30.0, 31.2],
        },
        index=indice,
    )
    niveis = {"suportes": [1.35712], "resistencias": [1.35800]}

    tocaia = avaliar_tocaia("GBPUSD", df, niveis_sr=niveis)

    assert tocaia is None
