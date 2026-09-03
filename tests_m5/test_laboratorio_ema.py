from datetime import datetime, timedelta

import pandas as pd

from iqoption_m5.config import configuracao_ema_laboratorio_practice, configuracao_ema_m5_real
from iqoption_m5.auditoria_entrada import recuperar_comparaveis
from iqoption_m5.executor import ExecutorSeguro
from iqoption_m5.laboratorio_ema import (
    _alvo_sombra, _rastros, _recuperar_pendencias_periodicas, _setup_do_rastro,
    _patch_candle_ao_vivo,
)
from iqoption_m5.modelos import Autorizacao, Decisao, ResultadoOrdem, SnapshotMercado
from iqoption_m5.registro import RegistroSQLite


def test_laboratorio_tem_rastros_m5_e_m15_e_nzd_em_sombra():
    config = configuracao_ema_laboratorio_practice()
    rastros = _rastros(config)

    assert len(rastros) == 8
    assert config.ativos_somente_sombra == ("NZDUSD",)
    assert {r.config.timeframe_segundos for r in rastros} == {300, 900}
    assert sum(r.intravela for r in rastros) == 2
    nzd = next(r for r in rastros if r.config.nzd_trend_pullback_ativo)
    assert nzd.somente_sombra
    assert nzd.config.timeframe_segundos == 300
    rastros_m15 = [r for r in rastros if r.config.timeframe_segundos == 900]
    assert all(r.somente_sombra and r.config.filtro_h1_ativo for r in rastros_m15)
    rastros_executaveis = [r for r in rastros if not r.somente_sombra]
    assert len(rastros_executaveis) == 1
    assert rastros_executaveis[0].config.timeframe_segundos == 300
    assert rastros_executaveis[0].config.ema920_pullback_ativo
    prime = next(r for r in rastros if r.config.ema920_prime_ativo)
    assert prime.somente_sombra
    assert prime.config.timeframe_segundos == 300


def test_todo_rastro_identifica_o_proprio_setup_sem_stopiteration():
    """EMA9/20 Prime também é um rastro válido e não pode derrubar o loop."""
    rastros = _rastros(configuracao_ema_laboratorio_practice())

    setups = [_setup_do_rastro(r.config) for r in rastros]

    assert "ema920_prime" in setups
    assert len(setups) == len(rastros)


def test_patch_ao_vivo_substitui_a_vela_em_formacao_sem_mudar_o_historico():
    """O gráfico do Lab deve mover o último candle antes do próximo fechamento."""
    inicio = pd.Timestamp("2026-09-02 12:00:00")
    snapshot = SnapshotMercado(
        "EURUSD",
        pd.DataFrame(
            {"Open": [1.1], "High": [1.103], "Low": [1.099], "Close": [1.102]},
            index=[inicio],
        ),
        0.85, True, int(inicio.timestamp()) + 45,
    )
    base = {"candles": [{"time": int(inicio.timestamp()), "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005}]}

    patch = _patch_candle_ao_vivo(base, snapshot, lambda x: int(pd.Timestamp(x).timestamp()), 123.0)

    assert len(patch["candles"]) == 1
    assert patch["candles"][-1]["close"] == 1.102
    assert patch["atualizado_em"] == 123.0


def test_perfil_ema_m5_real_isola_risco_e_setups_experimentais():
    config = configuracao_ema_m5_real()

    config.validar()
    assert config.conta == "REAL"
    assert config.ativos == ("EURUSD", "AUDCAD")
    assert config.valor_por_ordem == 2.50
    assert config.meta_diaria == 10.0
    assert config.stop_diario == -5.0
    assert config.max_operacoes_dia == 5
    assert config.ema920_pullback_ativo
    assert config.expiracao_por_setup == {"ema920_pullback": 15}
    assert not config.ema921_rsi_pullback_ativo
    assert not config.ema921_rsi_intravela_ativo
    candle = pd.Timestamp("2026-09-01 12:00:00")
    snapshot = SnapshotMercado(
        "EURUSD", pd.DataFrame({"Close": [1.0]}, index=[candle]), 0.85, True,
        int(candle.timestamp()) + 3,
    )
    decisao = Decisao("EURUSD", "call", 1.0, candle, "ema920_pullback", detalhes={"setup": "ema920_pullback"})
    assert ExecutorSeguro(config, None, None, None)._expiracao_dinamica(snapshot, decisao) == 15


def test_banco_guarda_mesmo_sinal_por_setup_e_timeframe(tmp_path):
    registro = RegistroSQLite(tmp_path / "laboratorio.sqlite3")
    candle = pd.Timestamp("2026-09-01 12:00:00")
    snapshot = SnapshotMercado(
        "EURUSD",
        pd.DataFrame({"Close": [1.0]}),
        0.85,
        True,
        int(candle.timestamp()),
    )
    autorizacao = Autorizacao(True, "ok")
    for setup, timeframe in (
        ("ema920_pullback", 300),
        ("ema921_rsi_pullback", 300),
        ("ema921_rsi_intravela", 300),
        ("ema921_rsi_pullback", 900),
    ):
        decisao = Decisao(
            "EURUSD", "call", 1.0, candle, setup, detalhes={"setup": setup}
        )
        registro.registrar_decisao(decisao, snapshot, autorizacao, timeframe=timeframe)

    with registro._sessao() as db:
        total = db.execute("SELECT COUNT(*) FROM decisoes").fetchone()[0]
    assert total == 4


def test_decisoes_grafico_mostra_setup_e_timeframe(tmp_path):
    registro = RegistroSQLite(tmp_path / "grafico.sqlite3")
    candle = pd.Timestamp("2026-09-01 12:00:00")
    snapshot = SnapshotMercado(
        "NZDUSD", pd.DataFrame({"Close": [0.59]}), 0.85, True, int(candle.timestamp())
    )
    decisao = Decisao(
        "NZDUSD", "call", 0.59, candle, "ema920_pullback",
        detalhes={"setup": "ema920_pullback", "fatores": ["toque na EMA"]},
    )
    registro.registrar_decisao(
        decisao, snapshot, Autorizacao(False, "nzd_noticia_high"), timeframe=900
    )

    sinais = registro.decisoes_grafico("NZDUSD")

    assert len(sinais) == 1
    assert sinais[0].detalhes["setup"] == "M15 ema920_pullback"
    assert sinais[0].detalhes["status_grafico"] == "bloqueado"


def test_sombra_usa_fechamento_equivalente_a_expiracao_do_rastro():
    rastros = _rastros(configuracao_ema_laboratorio_practice())
    m5_fechado = next(
        r for r in rastros
        if r.config.timeframe_segundos == 300 and r.config.ema920_pullback_ativo
    )
    m5_intravela = next(r for r in rastros if r.config.ema921_rsi_intravela_ativo and r.config.timeframe_segundos == 300)
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    snapshot = SnapshotMercado(
        "NZDUSD", pd.DataFrame({"Close": [0.59]}, index=[inicio]), 0.85, True, int(inicio.timestamp())
    )

    # M5 com expiração 15min compara o fechamento da terceira vela M5 (12:10).
    assert _alvo_sombra(snapshot, m5_fechado) == pd.Timestamp("2026-09-01 12:10:00")
    # Toque intravela vence no fechamento da própria vela.
    assert _alvo_sombra(snapshot, m5_intravela) == inicio


def test_laboratorio_pede_expiracao_configurada_para_sinais_fechados():
    """M5/M15 do laboratório não podem ser encurtados para um único candle."""
    rastros = _rastros(configuracao_ema_laboratorio_practice())
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    snapshot = SnapshotMercado(
        "EURUSD", pd.DataFrame({"Close": [1.0]}, index=[inicio]), 0.85, True, int(inicio.timestamp()) + 3
    )
    for timeframe, esperado in ((300, 15), (900, 30)):
        rastro = next(
            r for r in rastros
            if r.config.timeframe_segundos == timeframe and r.config.ema920_pullback_ativo
        )
        decisao = Decisao(
            "EURUSD", "call", 1.0, inicio, "ema920_pullback", detalhes={"setup": "ema920_pullback"}
        )
        executor = ExecutorSeguro(rastro.config, None, None, None)
        assert executor._expiracao_dinamica(snapshot, decisao) == esperado


def test_lab_recupera_pendente_vencida_sem_reinicio(tmp_path):
    config = configuracao_ema_laboratorio_practice()
    registro = RegistroSQLite(tmp_path / "pendente.sqlite3", config=config)
    candle = pd.Timestamp("2026-09-01 12:00:00")
    decisao = Decisao("AUDCAD", "call", 1.0, candle, "ema920_pullback", detalhes={"setup": "ema920_pullback"})
    enviada_em = datetime.now() - timedelta(minutes=16)
    registro.registrar_abertura("pendente-lab", decisao, 5.0, 0.85, enviada_em, timeframe=300, expiracao_minutos=15)

    class MercadoComResultado:
        def consultar_resultado(self, id_ordem):
            assert id_ordem == "pendente-lab"
            return 4.25

    ultima = _recuperar_pendencias_periodicas(
        MercadoComResultado(), registro, config, agora_monotonico=30.0, ultima_tentativa=0.0
    )

    assert ultima == 30.0
    assert not registro.operacoes_abertas()


def test_resumo_movimentos_unicos_nao_duplica_setups_do_mesmo_minuto(tmp_path):
    registro = RegistroSQLite(tmp_path / "movimentos.sqlite3")
    agora = datetime.now().replace(second=2, microsecond=0)
    candle = pd.Timestamp(agora)
    decisao = Decisao("EURUSD", "call", 1.0, candle, "ema920_pullback", detalhes={"setup": "ema920_pullback"})
    for ordem, setup in (("mov-1", "ema920_pullback"), ("mov-2", "ema921_rsi_pullback")):
        d = Decisao("EURUSD", "call", 1.0, candle, setup, detalhes={"setup": setup})
        registro.registrar_abertura(ordem, d, 5.0, 0.85, agora)
        registro.registrar_resultado(ResultadoOrdem(ordem, "EURUSD", "call", agora, agora, 5.0, 0.85, 4.25, "win"))

    resumo = registro.resumo_movimentos_unicos()

    assert resumo["ordens"] == 2
    assert resumo["movimentos"] == 1
    assert resumo["vitorias"] == 1
    assert resumo["winrate"] == 100.0


def test_entrada_detalhada_mostra_criterios_e_diagnostico(tmp_path):
    registro = RegistroSQLite(tmp_path / "auditoria.sqlite3")
    agora = datetime.now().replace(microsecond=0)
    candle = pd.Timestamp(agora)
    snapshot = SnapshotMercado("EURUSD", pd.DataFrame({"Close": [1.0]}), 0.85, True, int(agora.timestamp()))
    decisao = Decisao(
        "EURUSD", "call", 1.1, candle, "ema920_pullback",
        detalhes={"setup": "ema920_pullback", "toque_faixa": True, "ema9": 1.1, "ema20": 1.0, "corpo_ratio": 0.7},
    )
    registro.registrar_decisao(decisao, snapshot, Autorizacao(True, "autorizada"), timeframe=300)
    registro.registrar_abertura("audit-1", decisao, 5.0, 0.85, agora, timeframe=300, expiracao_minutos=15)
    registro.registrar_resultado(ResultadoOrdem("audit-1", "EURUSD", "call", agora, agora, 5.0, 0.85, 4.25, "win"))

    entrada = registro.entradas_hoje_detalhadas()[0]

    assert entrada["expiracao_minutos"] == 15
    assert entrada["timeframe"] == 300
    assert "WIN" in entrada["diagnostico"]
    assert any("Preço tocou" in criterio for criterio in entrada["criterios"])


def test_recuperacao_de_comparaveis_usa_tags_e_resultados():
    alvo = {"auditoria": {"tags": ["vela_alta", "vela_forte"], "corpo_ratio": 0.7, "range_atr": 1.1}}
    historico = [
        {"id_ordem": "w", "lucro": 4.25, "detalhes": {"auditoria": {"tags": ["vela_alta", "vela_forte"], "corpo_ratio": 0.72, "range_atr": 1.0}}},
        {"id_ordem": "l", "lucro": -5.0, "detalhes": {"auditoria": {"tags": ["vela_alta"], "corpo_ratio": 0.65, "range_atr": 1.2}}},
    ]

    resumo = recuperar_comparaveis(alvo, historico)

    assert resumo["amostra"] == 2
    assert resumo["wins"] == 1
    assert resumo["winrate"] == 50.0
