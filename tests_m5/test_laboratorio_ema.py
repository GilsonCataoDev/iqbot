from datetime import datetime, timedelta
import threading

import pandas as pd

from iqoption_m5.config import configuracao_ema_laboratorio_practice, configuracao_ema_m5_real
from iqoption_m5.auditoria_entrada import qualificar_leitura_m5, recuperar_comparaveis
from iqoption_m5.executor import ExecutorSeguro
from iqoption_m5.estrategia import EstrategiaReversaoM5
from iqoption_m5.laboratorio_ema import (
    _alvo_sombra, _rastros, _recuperar_pendencias_periodicas, _setup_do_rastro,
    _patch_candle_ao_vivo, ProgressoLaboratorio, _reconectar_laboratorio_estagnado,
    _armar_watchdog_apos_inicializacao, _alerta_ema920_m5,
    _iniciar_laboratorio_com_timeout,
)
from iqoption_m5.modelos import Autorizacao, Decisao, ResultadoOrdem, SnapshotMercado
from iqoption_m5.registro import RegistroSQLite


def test_laboratorio_tem_rastros_m5_e_m15_e_nzd_em_sombra():
    config = configuracao_ema_laboratorio_practice()
    rastros = _rastros(config)

    assert len(rastros) == 11
    assert config.ativos == (
        "EURUSD", "AUDCAD", "NZDUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY",
    )
    # Só o NZDUSD segue em sombra: ele já tem amostra e ela é negativa
    # (18 ordens, 44,4%, -15,2u). Os outros cinco acumulavam ZERO ordens,
    # entao a sombra nao produzia o dado que justificaria compará-los.
    assert config.ativos_somente_sombra == ("NZDUSD",)
    assert {r.config.timeframe_segundos for r in rastros} == {300, 900}
    assert sum(r.intravela for r in rastros) == 4
    fibos = [r for r in rastros if r.config.fibo_sr_retracao_ativo]
    assert {r.config.timeframe_segundos for r in fibos} == {300, 900}
    assert all(r.somente_sombra and r.intravela for r in fibos)
    nzd = next(r for r in rastros if r.config.nzd_trend_pullback_ativo)
    assert nzd.somente_sombra
    assert nzd.config.timeframe_segundos == 300
    rastros_m15 = [r for r in rastros if r.config.timeframe_segundos == 900]
    assert all(r.config.filtro_h1_ativo for r in rastros_m15)
    h1_shadow = next(
        r for r in rastros
        if r.config.ema920_pullback_ativo
        and r.config.timeframe_segundos == 300
        and r.config.filtro_h1_ativo
        and r.somente_sombra
    )
    assert h1_shadow is not None
    assert any(not r.somente_sombra and r.config.ema920_pullback_ativo for r in rastros_m15)
    rastros_executaveis = [r for r in rastros if not r.somente_sombra]
    assert len(rastros_executaveis) == 2
    assert {r.config.timeframe_segundos for r in rastros_executaveis} == {300, 900}
    assert all(r.config.ema920_pullback_ativo for r in rastros_executaveis)
    assert config.bloquear_direcao_paralela
    assert config.pullback_fib_min == 0.382
    assert config.pullback_fib_max == 0.618
    prime = next(r for r in rastros if r.config.ema920_prime_ativo)
    assert prime.somente_sombra
    assert prime.config.timeframe_segundos == 300


def test_todo_rastro_identifica_o_proprio_setup_sem_stopiteration():
    """EMA9/20 Prime também é um rastro válido e não pode derrubar o loop."""
    rastros = _rastros(configuracao_ema_laboratorio_practice())

    setups = [_setup_do_rastro(r.config) for r in rastros]

    assert "ema920_prime" in setups
    assert setups.count("fibo_sr_retracao") == 2
    assert len(setups) == len(rastros)


def test_mapa_fibo_do_lab_usa_impulso_fechado_e_zona_classica():
    indice = pd.date_range("2026-09-03 10:00", periods=21, freq="5min")
    fechamento = [100 + n * 0.5 for n in range(20)] + [106.0]
    candles = pd.DataFrame({
        "Open": [v - 0.2 for v in fechamento],
        "High": [v + 0.4 for v in fechamento],
        "Low": [v - 0.4 for v in fechamento],
        "Close": fechamento,
        "Volume": 10.0,
    }, index=indice)
    estrategia = EstrategiaReversaoM5(configuracao_ema_laboratorio_practice())
    indicadores = estrategia.calcular_indicadores(candles, "EURUSD")

    mapa = estrategia.mapa_fibonacci_atual(indicadores)

    assert mapa is not None
    assert [n["nivel"] for n in mapa["niveis"]] == [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    assert mapa["zona_inf"] < mapa["zona_sup"]
    assert mapa["amplitude_atr"] >= 1.5
    assert mapa["inicio"] < mapa["fim"]
    assert mapa["origem"] != mapa["extremo"]


def test_fibo_distingue_retracao_profunda_de_impulso_invalidado():
    indice = pd.date_range("2026-09-03 10:00", periods=31, freq="5min")
    fechamento = list(pd.Series(range(30), dtype=float).map(lambda n: 110 - n / 3)) + [107.0]
    candles = pd.DataFrame({
        "Open": [v + .10 for v in fechamento],
        "High": [v + .20 for v in fechamento],
        "Low": [v - .20 for v in fechamento],
        "Close": fechamento,
        "Volume": 10.0,
    }, index=indice)
    estrategia = EstrategiaReversaoM5(configuracao_ema_laboratorio_practice())
    indicadores = estrategia.calcular_indicadores(candles, "GBPUSD")
    indicadores.loc[indicadores.index[-1], "TendenciaMacro"] = "baixa"

    mapa = estrategia.mapa_fibonacci_atual(indicadores)

    assert mapa is not None
    assert mapa["zona_sup"] < mapa["preco_atual"] < mapa["fib786"]
    assert mapa["estado"] == "RETRAÇÃO PROFUNDA — ESPERAR"


def test_fibo_prefere_ultimo_swing_confirmado_ao_extremo_antigo_da_janela():
    indice = pd.date_range("2026-09-04 10:00", periods=30, freq="5min")
    high = [106, 108, 110, 109, 108, 106, 104, 102, 101, 103,
            104, 105, 106, 106, 107, 107, 107, 107, 108, 107.5,
            107, 106.5, 106, 105.5, 105, 105.4, 105.8, 106.2, 106.5, 106.8]
    low = [v - 1 for v in high]
    low[8] = 100
    low[24] = 104
    candles = pd.DataFrame({
        "Open": [(h + l) / 2 for h, l in zip(high, low)],
        "High": high,
        "Low": low,
        "Close": [(h + l) / 2 for h, l in zip(high, low)],
        "ATR": 1.0,
    }, index=indice)
    estrategia = EstrategiaReversaoM5(configuracao_ema_laboratorio_practice())

    mapa = estrategia._mapa_fibonacci_visual(candles, len(candles) - 1, "baixa")

    assert mapa is not None
    assert mapa["inicio"] == indice[18]
    assert mapa["fim"] == indice[24]
    assert mapa["origem"] == 108
    assert mapa["extremo"] == 104


def test_fibo_estende_extremo_ate_o_fim_da_perna_em_andamento():
    indice = pd.date_range("2026-09-04 16:00", periods=25, freq="5min")
    high = [110, 109.8, 109.5, 109.2, 109.0, 109.4, 109.8, 110.2, 110.0,
            109.7, 109.3, 109.0, 108.8, 109.1, 109.0, 108.7, 108.5, 108.2,
            108.0, 107.8, 107.5, 107.3, 107.0, 106.8, 106.7]
    low = [v - 0.4 for v in high]
    candles = pd.DataFrame({
        "Open": [(h + l) / 2 for h, l in zip(high, low)],
        "High": high, "Low": low,
        "Close": [(h + l) / 2 for h, l in zip(high, low)],
        "ATR": 0.5,
    }, index=indice)
    estrategia = EstrategiaReversaoM5(configuracao_ema_laboratorio_practice())

    mapa = estrategia._mapa_fibonacci_visual(candles, len(candles) - 1, "baixa")

    assert mapa is not None
    # indice_recuo fica fora do cálculo; o extremo deve alcançar o último
    # candle fechado, sem esperar duas velas futuras confirmarem um pivô.
    assert mapa["fim"] == indice[-2]


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


def test_alerta_ema920_m5_so_aceita_sinal_confirmado_no_ultimo_fechamento():
    inicio = pd.Timestamp("2026-09-02 12:00:00")
    indice = pd.date_range(inicio, periods=3, freq="5min")
    snapshot = SnapshotMercado(
        "EURUSD",
        pd.DataFrame({"Open": [1.1] * 3, "High": [1.101] * 3, "Low": [1.099] * 3, "Close": [1.1] * 3}, index=indice),
        0.85, True, int(indice[-1].timestamp()) + 45,
    )
    sinal = Decisao(
        "EURUSD", "call", 1.1, indice[-2], "ema920_pullback",
        detalhes={"setup": "M5 ema920_pullback", "status_grafico": "confirmado"},
    )

    alerta = _alerta_ema920_m5(snapshot, [sinal])

    assert alerta and alerta["setup"] == "EMA9/20 M5"
    assert alerta["entradaConfirmada"] is True


def test_watchdog_do_lab_reconecta_quando_nao_ha_progresso():
    class MercadoFalso:
        def __init__(self):
            self.chamadas = []

        def reconectar_se_necessario(self, forcar=False):
            self.chamadas.append(forcar)
            return True

    progresso = ProgressoLaboratorio(agora=100.0)
    mercado = MercadoFalso()

    assert not _reconectar_laboratorio_estagnado(mercado, progresso, agora=110.0, limite_s=30.0)
    assert _reconectar_laboratorio_estagnado(mercado, progresso, agora=131.0, limite_s=30.0)
    assert mercado.chamadas == [True]
    assert progresso.idade(131.0) == 0.0


def test_inicio_do_lab_expira_sem_deixar_o_processo_principal_preso():
    bloqueio = threading.Event()

    class MercadoTravado:
        def iniciar(self):
            bloqueio.wait()

    ok, motivo = _iniciar_laboratorio_com_timeout(MercadoTravado(), timeout_s=.01)

    assert not ok
    assert "tempo" in motivo.lower()


def test_watchdog_do_lab_ignora_tempo_gasto_na_inicializacao():
    """Carga de streams/histórico não pode parecer congelamento do loop."""
    progresso = ProgressoLaboratorio(agora=100.0)

    _armar_watchdog_apos_inicializacao(progresso, agora=170.0)

    assert progresso.idade(175.0) == 5.0


def test_grafico_atualiza_antes_do_bloqueio_de_mercado_fechado():
    """Estado de negociação bloqueia ordens, não o desenho dos candles."""
    import inspect
    from iqoption_m5.laboratorio_ema import executar_laboratorio_ema

    fonte = inspect.getsource(executar_laboratorio_ema)
    assert fonte.index("_atualizar_grafico_laboratorio(") < fonte.index(
        "if not snapshot.mercado_aberto:"
    )


def test_perfil_ema_m5_real_isola_risco_e_setups_experimentais():
    config = configuracao_ema_m5_real()

    config.validar()
    assert config.conta == "REAL"
    assert config.ativos == ("EURUSD", "AUDCAD")
    assert config.valor_por_ordem == 2.50
    assert config.meta_diaria == 15.0
    assert config.stop_diario == 0.0
    assert config.max_operacoes_dia == 0
    assert not config.parar_por_perdas
    assert not config.parar_por_prejuizo
    assert config.circuit_breaker_max_perdas == 0
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


def test_sombra_guarda_timeframe_para_comparar_m5_e_m15(tmp_path):
    registro = RegistroSQLite(tmp_path / "sombras.sqlite3")
    candle = pd.Timestamp("2026-09-01 12:00:00")
    for timeframe in (300, 900):
        registro.registrar_simulacao_bloqueada(
            "EURUSD", "call", "ema920_pullback", candle, 1.1, 0.85,
            "validacao", timeframe=timeframe,
        )

    with registro._sessao() as db:
        linhas = db.execute(
            "SELECT timeframe FROM simulacoes ORDER BY timeframe"
        ).fetchall()
    # sqlite3.Row nao compara igual a tupla; o que importa sao os valores.
    assert [linha[0] for linha in linhas] == [300, 900]


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


def test_leitura_m5_qualifica_sem_alterar_a_direcao_ou_a_ordem():
    decisao = Decisao(
        "EURUSD", "call", 1.1, pd.Timestamp("2026-09-03 12:00:00"), "ema920_pullback",
        detalhes={"auditoria": {"fechamento_posicao": 0.80, "atr_relativo": 1.0}},
    )

    qualificada = qualificar_leitura_m5(decisao)

    assert qualificada.direcao == "call"
    assert qualificada.preco == 1.1
    assert qualificada.detalhes["leitura_m5"]["qualificada"] is True
    assert qualificada.detalhes["leitura_m5"]["modo"] == "sombra"
