from datetime import datetime, timedelta
from dataclasses import replace
import threading

import pandas as pd

from iqoption_m5.config import configuracao_ema_laboratorio_practice, configuracao_ema_m5_real
from iqoption_m5.auditoria_entrada import qualificar_leitura_m5, recuperar_comparaveis
from iqoption_m5.executor import ExecutorSeguro
from iqoption_m5.estrategia import EstrategiaReversaoM5
from iqoption_m5.laboratorio_ema import (
    _alvo_sombra, _rastros, _recuperar_pendencias_periodicas, _setup_do_rastro,
    _patch_candle_ao_vivo, ProgressoLaboratorio, _reconectar_laboratorio_estagnado,
    _armar_watchdog_apos_inicializacao, _alerta_ema920_m5, _motivo_sombra,
    _registrar_sombra, _pausa_sequencia,
)
from iqoption_m5.mercado_iq import iniciar_com_timeout
from iqoption_m5.modelos import Autorizacao, Decisao, ResultadoOrdem, SnapshotMercado
from iqoption_m5.registro import RegistroSQLite


def test_laboratorio_tem_rastros_m5_e_m15_e_nzd_em_sombra():
    config = configuracao_ema_laboratorio_practice()
    rastros = _rastros(config)

    assert len(rastros) == 18
    assert config.ativos == (
        "EURUSD", "AUDCAD", "NZDUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY", "EURCHF",
    )
    assert config.ativos_somente_sombra == ()
    assert {r.config.timeframe_segundos for r in rastros} == {300, 900, 3600}
    assert sum(r.intravela for r in rastros) == 4
    fibos = [r for r in rastros if r.config.fibo_sr_retracao_ativo]
    assert {r.config.timeframe_segundos for r in fibos} == {300, 900}
    assert all(r.intravela for r in fibos)
    # fibo_sr_retracao perdeu o EURJPY para o ema920_pullback: sombra 41,2%
    # em n=119 contra 54,5% em n=299 do ema920 no mesmo par. Sem ativo
    # exclusivo, o setup roda em sombra na cesta inteira.
    assert all(r.somente_sombra for r in fibos)
    assert all(r.config.ativos == config.ativos for r in fibos)
    # NZDUSD: 45,8% em n=24 real e 48,2% em n=369 sombra, abaixo do
    # break-even (~53,9%) nas duas medidas.
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
    assert all(r.somente_sombra for r in rastros_m15)
    # ema921_rsi_intravela (AUDUSD) suspenso: taxa histórica 30%, abaixo do break-even.
    intravela_m5 = next(r for r in rastros if r.config.ema921_rsi_intravela_ativo and r.config.timeframe_segundos == 300)
    assert intravela_m5.somente_sombra
    rastros_executaveis = [r for r in rastros if not r.somente_sombra]
    assert len(rastros_executaveis) == 5
    assert {r.config.timeframe_segundos for r in rastros_executaveis} == {300, 3600}
    # AUDCAD e EURUSD: dois rastros ema920_pullback independentes promovidos
    # ao real. EURJPY fica em sombra acumulando IC95 na config atual.
    assert sum(r.config.ema920_pullback_ativo for r in rastros_executaveis) == 2
    assert {r.config.ativos[0] for r in rastros_executaveis
            if r.config.ema920_pullback_ativo} == {"AUDCAD", "EURUSD"}
    # EURJPY deve estar em sombra
    eurjpy_rastro = next(
        r for r in rastros
        if r.config.ema920_pullback_ativo and "EURJPY" in r.config.ativos
    )
    assert eurjpy_rastro.somente_sombra
    # fibo_mtf_confirmado partido em dois: EURUSD executavel, GBPUSD/USDJPY
    # em sombra por WR abaixo do break-even (41,7% n=12 e 45,8% n=24), e o
    # resto da cesta em sombra para medir o setup nos outros pares.
    fibo_mtf = [r for r in rastros if r.config.fibo_mtf_confirmado_ativo]
    assert len(fibo_mtf) == 3
    executavel = next(r for r in fibo_mtf if not r.somente_sombra)
    sombras_mtf = [r.config.ativos for r in fibo_mtf if r.somente_sombra]
    assert executavel.config.ativos == ("EURUSD",)
    assert sombras_mtf == [
        ("GBPUSD", "USDJPY"),
        ("AUDCAD", "NZDUSD", "AUDUSD", "USDCAD", "EURJPY", "EURCHF"),
    ]
    assert all(r.config.expiracao_por_setup == {"fibo_mtf_confirmado": 15}
               for r in fibo_mtf)
    assert config.bloquear_direcao_paralela
    assert config.pullback_fib_min == 0.382
    assert config.pullback_fib_max == 0.618
    prime = next(r for r in rastros if r.config.ema920_prime_ativo)
    assert prime.somente_sombra
    assert prime.config.timeframe_segundos == 300
    h1 = next(r for r in rastros if r.config.breakout_reteste_ativo)
    assert not h1.somente_sombra
    assert h1.config.ativos == ("EURCHF",)
    assert h1.config.timeframe_segundos == 3600
    assert h1.config.expiracao_por_setup == {"breakout_reteste": 60}


def test_todo_rastro_identifica_o_proprio_setup_sem_stopiteration():
    """EMA9/20 Prime também é um rastro válido e não pode derrubar o loop."""
    rastros = _rastros(configuracao_ema_laboratorio_practice())

    setups = [_setup_do_rastro(r.config) for r in rastros]

    assert "ema920_prime" in setups
    assert setups.count("fibo_sr_retracao") == 2
    # EURUSD, GBPUSD/USDJPY e a cesta completa em sombra.
    assert setups.count("fibo_mtf_confirmado") == 3
    # M5 AUDCAD, M15, EURUSD, EURJPY, a cesta sem H1 e a sombra de
    # comparacao com filtro H1.
    assert setups.count("ema920_pullback") == 6
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

    ok, motivo = iniciar_com_timeout(MercadoTravado(), timeout_s=.01)

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
    from iqoption_m5.laboratorio_ema import _executar_laboratorio_ema

    fonte = inspect.getsource(_executar_laboratorio_ema)
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


def test_cesta_sem_h1_mede_config_do_real_sem_colidir_com_rastro_h1(tmp_path):
    rastros = _rastros(configuracao_ema_laboratorio_practice())
    cesta = next(r for r in rastros if r.rotulo_sombra == "ema920_pullback_cesta")
    h1 = next(r for r in rastros if r.nome == "M5 | EMA9/20 + H1 (sombra comparação)")

    assert cesta.somente_sombra
    assert not cesta.config.filtro_h1_ativo
    assert _setup_do_rastro(cesta.config) == "ema920_pullback"
    assert set(cesta.config.ativos) == {"GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "EURCHF"}

    # O mesmo sinal nos dois rastros precisa virar duas linhas, não uma.
    registro = RegistroSQLite(tmp_path / "cesta.sqlite3")
    inicio = pd.Timestamp("2026-09-01 12:00:00")
    snapshot = SnapshotMercado(
        "GBPUSD", pd.DataFrame({"Close": [1.3]}, index=[inicio]), 0.85, True, int(inicio.timestamp())
    )
    decisao = Decisao(
        "GBPUSD", "call", 1.3, inicio, "ema920_pullback", detalhes={"setup": "ema920_pullback"}
    )
    for rastro in (cesta, h1):
        _registrar_sombra(registro, snapshot, rastro, decisao, "rastro_sombra")
    with registro._sessao() as db:
        setups = sorted(linha[0] for linha in db.execute("SELECT setup FROM simulacoes"))
    assert setups == ["ema920_pullback", "ema920_pullback_cesta"]


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


def _rastro_que_opera(rastros):
    return next(
        r for r in rastros
        if not r.somente_sombra and r.config.timeframe_segundos == 300
    )


def test_noticia_high_manda_qualquer_par_para_sombra():
    """Antes só NZDUSD era checado — e ele nunca chegava neste ponto."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "EURUSD", noticia_high=True
    ) == "noticia_high"


def test_sem_noticia_o_par_que_opera_segue_mandando_ordem():
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "EURUSD", noticia_high=False
    ) is None


def test_ativo_ja_em_sombra_mantem_o_motivo_original_mesmo_com_noticia():
    """O rótulo do candidato não pode ser sobrescrito: a amostra dele
    é comparada por motivo e misturar as duas causas a invalida."""
    base = replace(configuracao_ema_laboratorio_practice(), ativos_somente_sombra=("NZDUSD",))
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "NZDUSD", noticia_high=True
    ) == "ativo_candidato_sombra"


def test_put_horario_fraco_vai_para_sombra():
    """PUT entre 05h–13h BRT bloqueado: taxa histórica 41%, CALL 66% no mesmo período."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    # 10h UTC = 07h BRT — dentro da faixa bloqueada
    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "AUDCAD",
        noticia_high=False, direcao="put", hora_utc=10,
    ) == "put_horario_fraco"


def test_call_no_horario_fraco_do_put_passa():
    """CALL no mesmo horário não é afetado pelo filtro PUT."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "AUDCAD",
        noticia_high=False, direcao="call", hora_utc=10,
    ) is None


def test_put_fora_do_horario_fraco_passa():
    """PUT após 14h BRT (17h+ UTC) não é bloqueado."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    # 17h UTC = 14h BRT — fora da faixa
    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "AUDCAD",
        noticia_high=False, direcao="put", hora_utc=17,
    ) is None


def test_put_horario_fraco_nao_afeta_fibo_mtf():
    """fibo_mtf_confirmado retorna None independente de horário e direção."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "fibo_mtf_confirmado", "GBPUSD",
        noticia_high=False, direcao="put", hora_utc=10,
    ) is None


def test_usdjpy_put_vai_para_sombra():
    """PUT em USDJPY bloqueado: taxa histórica 36%, n=11."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "USDJPY",
        noticia_high=False, direcao="put", hora_utc=20,
    ) == "put_ativo_fraco"


def test_usdcad_put_vai_para_sombra():
    """PUT em USDCAD bloqueado: taxa histórica 40%, n=15."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "USDCAD",
        noticia_high=False, direcao="put", hora_utc=20,
    ) == "put_ativo_fraco"


def test_put_ativo_fraco_call_nao_afetado():
    """CALL em USDJPY e USDCAD não é bloqueado pelo filtro put_ativo_fraco."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "USDJPY",
        noticia_high=False, direcao="call", hora_utc=20,
    ) is None
    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "USDCAD",
        noticia_high=False, direcao="call", hora_utc=20,
    ) is None


def test_m5_leitura_ausente_vai_para_sombra():
    """Sem leitura M5 (auditoria ausente): taxa histórica 44%, abaixo do break-even."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "AUDCAD",
        noticia_high=False, direcao="call", hora_utc=20,
        leitura_m5_ausente=True,
    ) == "m5_leitura_ausente"


def test_m5_leitura_presente_nao_bloqueada():
    """Com leitura M5 presente (qualificada ou não), ordem segue normal."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "ema920_pullback", "AUDCAD",
        noticia_high=False, direcao="call", hora_utc=20,
        leitura_m5_ausente=False,
    ) is None


def test_m5_ausente_nao_afeta_fibo_mtf():
    """fibo_mtf_confirmado é exempt mesmo sem leitura M5."""
    base = configuracao_ema_laboratorio_practice()
    rastro = _rastro_que_opera(_rastros(base))

    assert _motivo_sombra(
        base, rastro, "fibo_mtf_confirmado", "GBPUSD",
        noticia_high=False, direcao="call", hora_utc=20,
        leitura_m5_ausente=True,
    ) is None


def _lateral(ema_sep=None, adx=None, setup="ema920_pullback", rastro=None):
    base = configuracao_ema_laboratorio_practice()
    rastro = rastro or _rastro_que_opera(_rastros(base))
    return _motivo_sombra(
        base, rastro, setup, "AUDCAD", noticia_high=False,
        direcao="call", hora_utc=20, ema_sep=ema_sep, adx=adx,
    )


def test_lateral_nao_bloqueia_ema_sep():
    """ema_sep<0.5 não bloqueia mais: só marca detalhes no loop."""
    assert _lateral(ema_sep=0.49) is None
    assert _lateral(ema_sep=0.5) is None


def test_lateral_nao_bloqueia_adx():
    """ADX<20 não bloqueia mais: só marca detalhes no loop."""
    assert _lateral(ema_sep=1.0, adx=19.9) is None
    assert _lateral(ema_sep=1.0, adx=20.0) is None


def test_lateral_sem_medida_nao_bloqueia():
    """Auditoria ausente não pode virar bloqueio silencioso."""
    assert _lateral(ema_sep=None, adx=None) is None


def test_lateral_so_vale_para_ema920():
    assert _lateral(ema_sep=0.1, adx=5.0, setup="ema921_rsi_pullback") is None


def test_rastro_sombra_mantem_rotulo_mesmo_lateral():
    """Sinal de rastro que nunca opera não pode entrar na amostra do filtro."""
    base = configuracao_ema_laboratorio_practice()
    sombra = next(
        r for r in _rastros(base)
        if r.somente_sombra and r.config.ema920_pullback_ativo
    )
    assert _lateral(ema_sep=0.1, rastro=sombra) == "rastro_sombra"


def test_pausa_marca_apos_3_losses_recentes():
    agora = datetime(2026, 9, 24, 4, 0)
    pausa = _pausa_sequencia(3, pd.Timestamp("2026-09-24T03:30:00"), agora)
    assert pausa == {"losses_seguidos": 3, "minutos_desde_ultimo": 30.0}


def test_pausa_ignora_sequencia_curta_ou_antiga():
    agora = datetime(2026, 9, 24, 4, 0)
    assert _pausa_sequencia(2, pd.Timestamp("2026-09-24T03:50:00"), agora) is None
    assert _pausa_sequencia(3, pd.Timestamp("2026-09-24T02:30:00"), agora) is None
    assert _pausa_sequencia(0, None, agora) is None


def test_sequencia_losses_conta_so_os_consecutivos_mais_recentes(tmp_path):
    import sqlite3
    base = configuracao_ema_laboratorio_practice()
    caminho = tmp_path / "seq.sqlite3"
    registro = RegistroSQLite(caminho, config=base)
    resultados = [("03:00", "win"), ("03:10", "loose"), ("03:20", "loose"), ("03:30", "loose")]
    with sqlite3.connect(caminho) as db:
        for i, (hora, res) in enumerate(resultados):
            db.execute(
                "INSERT INTO operacoes (id_ordem, ativo, direcao, enviada_em, valor, payout,"
                " setup, hora_sinal, resultado_bruto, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(i), "AUDCAD", "call", f"2026-09-24T{hora}:05", 2.5, 0.85,
                 "ema920_pullback", f"2026-09-24T{hora}:00", res, "finalizada"),
            )
    assert registro.sequencia_losses("AUDCAD", "ema920_pullback") == (
        3, pd.Timestamp("2026-09-24T03:30:00")
    )
    assert registro.sequencia_losses("EURUSD", "ema920_pullback") == (0, None)


def test_pausa_aceita_agora_com_fuso_utc():
    from datetime import timezone
    agora = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)
    pausa = _pausa_sequencia(4, pd.Timestamp("2026-09-24T03:45:00"), agora)
    assert pausa["minutos_desde_ultimo"] == 15.0
