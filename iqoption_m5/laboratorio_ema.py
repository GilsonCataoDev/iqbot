"""Laboratório EMA em uma conexão IQ: M5 e M15, todos os sinais em PRACTICE.

O módulo concentra a complexidade multi-timeframe. Sua interface pública é
apenas ``executar_laboratorio_ema()``; os seis rastros internos ficam isolados
por setup/timeframe no SQLite compartilhado.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import pandas as pd

from .config import Configuracao, configuracao_ema_laboratorio_practice
from .auditoria_entrada import (
    enriquecer_decisao,
    qualificar_leitura_m5,
    retroalimentar_decisoes,
)
from .estrategia import EstrategiaReversaoM5
from .executor import ExecutorSeguro
from .grafico import GraficoM5
from .mercado_iq import MercadoIQ, MercadoIndisponivel
from .modelos import Autorizacao, Decisao, SnapshotMercado
from .noticias import CalendarioEconomico
from .recuperacao import recuperar_operacoes_pendentes
from .registro import RegistroSQLite
from .risco import GerenciadorRisco, kill_switch_ativo


@dataclass(frozen=True)
class RastroEma:
    nome: str
    config: Configuracao
    intravela: bool
    somente_sombra: bool = False


class ProgressoLaboratorio:
    """Relógio thread-safe do loop e do gráfico ao vivo.

    O Lab roda em uma única conexão IQ. Se uma chamada da biblioteca trava, o
    processo continua existindo, mas nenhum JSON novo chega ao navegador. Este
    relógio permite que outra thread detecte a ausência de progresso e force a
    reconexão sem depender da chamada que ficou presa.
    """

    def __init__(self, agora: float | None = None) -> None:
        self._lock = threading.Lock()
        self._ultimo = time.monotonic() if agora is None else float(agora)

    def marcar(self, agora: float | None = None) -> None:
        with self._lock:
            self._ultimo = time.monotonic() if agora is None else float(agora)

    def idade(self, agora: float | None = None) -> float:
        referencia = time.monotonic() if agora is None else float(agora)
        with self._lock:
            return max(0.0, referencia - self._ultimo)


def _armar_watchdog_apos_inicializacao(
    progresso: ProgressoLaboratorio, agora: float | None = None
) -> None:
    """Zera a idade depois da carga inicial, antes de ativar o watchdog.

    Conectar, baixar históricos e semear o gráfico pode levar mais que o
    limite do watchdog. Esse tempo não é estagnação do ciclo ao vivo.
    """
    progresso.marcar(agora)


def _reconectar_laboratorio_estagnado(
    mercado: MercadoIQ, progresso: ProgressoLaboratorio, *, agora: float,
    limite_s: float,
) -> bool:
    """Reconecta apenas após estagnação comprovada do Lab."""
    if progresso.idade(agora) <= limite_s:
        return False
    print(
        f"[WATCHDOG LAB] Sem candle/JSON novo há {progresso.idade(agora):.0f}s; "
        "reconectando a IQ..."
    )
    try:
        ok = bool(mercado.reconectar_se_necessario(forcar=True))
    except Exception as erro:
        print(f"[WATCHDOG LAB] Reconexão falhou: {erro!r}")
        return False
    if ok:
        progresso.marcar(agora)
        print("[WATCHDOG LAB] Reconexão concluída; aguardando velas novas.")
    return ok


def _vigiar_laboratorio(
    mercado: MercadoIQ, progresso: ProgressoLaboratorio, parar: threading.Event,
    limite_s: float,
) -> None:
    """Evita que um socket congelado deixe o gráfico parado por horas."""
    ultima_tentativa = float("-inf")
    while not parar.wait(5.0):
        agora = time.monotonic()
        if progresso.idade(agora) <= limite_s or agora - ultima_tentativa < limite_s:
            continue
        ultima_tentativa = agora
        _reconectar_laboratorio_estagnado(
            mercado, progresso, agora=agora, limite_s=limite_s
        )


def _config_rastro(base: Configuracao, timeframe: int, setup: str) -> Configuracao:
    """Configura apenas um setup por rastro; evita que sinais se confundam."""
    is_m15 = timeframe == 900
    expiracao = 30 if is_m15 else 15
    janela_intravela = 720 if is_m15 else 240
    return replace(
        base,
        timeframe_segundos=timeframe,
        expiracao_minutos=expiracao,
        entrada_max_segundos_no_candle=60 if is_m15 else 45,
        ema920_pullback_ativo=setup == "ema920_pullback",
        ema920_prime_ativo=setup == "ema920_prime",
        ema921_rsi_pullback_ativo=setup == "ema921_rsi_pullback",
        ema921_rsi_intravela_ativo=setup == "ema921_rsi_intravela",
        fibo_sr_retracao_ativo=setup == "fibo_sr_retracao",
        nzd_trend_pullback_ativo=setup == "nzd_trend_pullback_v1",
        # M15 só segue a favor do contexto H1. O filtro é medido em sombra
        # antes de qualquer nova ordem real nesse timeframe.
        filtro_h1_ativo=is_m15,
        entrada_intracandle_por_toque_ativo=setup in (
            "ema921_rsi_intravela", "fibo_sr_retracao",
        ),
        janela_entrada_por_setup=(
            {setup: janela_intravela}
            if setup in ("ema921_rsi_intravela", "fibo_sr_retracao") else None
        ),
        expiracao_por_setup=(
            {"ema921_rsi_intravela": 0}
            if setup == "ema921_rsi_intravela" else {setup: expiracao}
        ),
    )


def _rastros(base: Configuracao) -> list[RastroEma]:
    saida: list[RastroEma] = []
    for timeframe, rotulo in ((300, "M5"), (900, "M15")):
        setups = [
            ("ema920_pullback", "EMA9/20 fechado"),
            ("ema921_rsi_pullback", "EMA9/21 + RSI fechado"),
            ("ema921_rsi_intravela", "EMA9/21 + RSI intravela"),
            ("fibo_sr_retracao", "Fibo 38/50/62 + S/R + rejeição"),
        ]
        if timeframe == 300:
            setups.append(("ema920_prime", "EMA9/20 Prime"))
        for setup, nome in setups:
            saida.append(
                RastroEma(
                    nome=(
                        f"{rotulo} | {nome}"
                        + (
                            " (sombra H1)"
                            if timeframe == 900 and setup != "ema920_pullback"
                            else " (sombra de validação)"
                            if setup != "ema920_pullback"
                            else ""
                        )
                    ),
                    config=_config_rastro(base, timeframe, setup),
                    intravela=setup in ("ema921_rsi_intravela", "fibo_sr_retracao"),
                    # EMA9/20 fechado é o único setup que pode abrir ordem em
                    # M5 e M15. M15 exige H1 alinhado; EMA9/21 e intravela
                    # seguem em sombra, sem disputar uma posição principal.
                    somente_sombra=setup != "ema920_pullback",
                )
            )
    # Candidato separado: nunca envia ordem enquanto não completar uma amostra
    # nova. M5 entra apenas depois de o M15 confirmar a mesma direção.
    saida.append(
        RastroEma(
            nome="M5 | NZD tendência + M15 + ADX (sombra)",
            config=_config_rastro(base, 300, "nzd_trend_pullback_v1"),
            intravela=False,
            somente_sombra=True,
        )
    )
    return saida


def _setup_do_rastro(config: Configuracao) -> str:
    """Identifica o único setup ligado no rastro do laboratório.

    O laboratório isola um setup por rastro. Falhar explicitamente numa
    configuração inválida é melhor que encerrar o loop inteiro por `next()`.
    """
    ativos = [
        nome for nome, ligado in (
            ("ema920_pullback", config.ema920_pullback_ativo),
            ("ema920_prime", config.ema920_prime_ativo),
            ("ema921_rsi_pullback", config.ema921_rsi_pullback_ativo),
            ("ema921_rsi_intravela", config.ema921_rsi_intravela_ativo),
            ("fibo_sr_retracao", config.fibo_sr_retracao_ativo),
            ("nzd_trend_pullback_v1", config.nzd_trend_pullback_ativo),
        ) if ligado
    ]
    if len(ativos) != 1:
        raise RuntimeError(f"Rastro EMA inválido: esperava um setup ativo, encontrei {ativos!r}.")
    return ativos[0]


def _motivos(decisao) -> str:
    d = decisao.detalhes
    ema_curta = d.get("ema9")
    ema_longa = d.get("ema20", d.get("ema21"))
    partes = []
    if ema_curta is not None and ema_longa is not None:
        relacao = ">" if decisao.direcao == "call" else "<"
        partes.append(f"EMA9 {relacao} EMA longa")
    if d.get("rsi14") is not None:
        partes.append(f"RSI={d['rsi14']}")
    if d.get("adx14") is not None:
        partes.append(f"ADX={d['adx14']}")
    if d.get("separacao_atr") is not None:
        partes.append(f"separação={d['separacao_atr']} ATR")
    if d.get("toque_faixa"):
        partes.append("toque na faixa")
    if d.get("confirmacao") == "toque_ao_vivo":
        partes.append("sem esperar fechamento")
    return "; ".join(partes) or "critérios do setup atendidos"


def _alvo_sombra(snapshot: SnapshotMercado, rastro: RastroEma) -> pd.Timestamp:
    """Candle cujo fechamento equivale ao vencimento simulado da ordem."""
    inicio = pd.Timestamp(snapshot.candles.index[-1])
    if rastro.intravela:
        return inicio
    duracao = max(
        rastro.config.timeframe_segundos,
        rastro.config.expiracao_minutos * 60,
    )
    return inicio + pd.Timedelta(seconds=duracao - rastro.config.timeframe_segundos)


def _registrar_sombra(
    registro: RegistroSQLite,
    snapshot: SnapshotMercado,
    rastro: RastroEma,
    decisao: Decisao,
    motivo: str,
) -> None:
    registro.registrar_simulacao_bloqueada(
        ativo=decisao.ativo,
        direcao=decisao.direcao,
        setup=decisao.detalhes.get("setup", decisao.motivo),
        candle_hora=_alvo_sombra(snapshot, rastro),
        preco_entrada=decisao.preco,
        payout=float(snapshot.payout) if snapshot.payout is not None else 0.85,
        motivo=motivo,
        timeframe=rastro.config.timeframe_segundos,
    )


def _resolver_sombras(registro: RegistroSQLite, snapshot: SnapshotMercado) -> None:
    """Resolve sombras pelo fechamento do candle-alvo, sem tocar na banca."""
    candles = snapshot.candles
    for simulacao in registro.simulacoes_pendentes(snapshot.ativo):
        alvo = pd.Timestamp(simulacao["candle_hora"])
        if alvo not in candles.index:
            continue
        posicao = candles.index.get_loc(alvo)
        if not isinstance(posicao, int) or posicao >= len(candles.index) - 1:
            continue
        fechamento = float(candles.iloc[posicao]["Close"])
        entrada = float(simulacao["preco_entrada"])
        if abs(fechamento - entrada) < 1e-8:
            resultado = "equal"
        else:
            venceu = (
                fechamento > entrada
                if simulacao["direcao"] == "call"
                else fechamento < entrada
            )
            resultado = "win" if venceu else "loss"
        registro.resolver_simulacao(simulacao["id"], resultado)
        print(
            f"    [SOMBRA] {snapshot.ativo} {simulacao['direcao'].upper()} "
            f"[{simulacao['setup']}] {simulacao['motivo']} -> {resultado.upper()}"
        )


# Por ativo: (último candle avaliado, sinais acumulados na sessão).
# avaliar_todas() olha só o último candle fechado, então marcar o dia inteiro
# exige acumular a cada candle novo — varrer a janela toda custaria ~1.8s por
# ativo, inviável dentro do laço de 1s.
_sinais_fechado_cache: dict[str, tuple[object, list]] = {}


def _sinais_so_para_ver(
    estrategia: EstrategiaReversaoM5,
    snapshot: SnapshotMercado,
    indicadores: pd.DataFrame,
    setup: str | None,
) -> list:
    """Sinais que a estratégia marcaria, acumulados só para desenhar.

    Com o mercado fechado o bot pula a avaliação inteira e o gráfico ficava sem
    nada do dia. Aqui ela roda apenas para o desenho: não grava decisão, não
    registra sombra e não envia ordem. Entram como bloqueados, na camada que o
    operador já liga e desliga.

    Como nada é persistido, a marcação começa do zero a cada reinício — é o
    preço de não tocar no banco nem nas estatísticas.
    """
    if setup is None or len(indicadores) < 3:
        return []
    if snapshot.mercado_aberto:
        # Reabriu: as decisões reais voltam a ser gravadas e estas sairiam
        # duplicando o que o banco já traz.
        _sinais_fechado_cache.pop(snapshot.ativo, None)
        return []

    candle = indicadores.index[-2]
    ultimo, acumulado = _sinais_fechado_cache.get(snapshot.ativo, (None, []))
    if ultimo == candle:
        return acumulado

    try:
        novas = estrategia.avaliar_todas(snapshot.ativo, indicadores)
    except Exception:
        novas = []
    for decisao in novas:
        if decisao.detalhes.get("setup") != setup:
            continue
        decisao.detalhes["status_grafico"] = "bloqueado"
        decisao.detalhes["razao"] = list(decisao.detalhes.get("razao") or []) + [
            "mercado fechado — sinal apenas observado"
        ]
        acumulado.append(decisao)
    # Descarta o que já saiu da janela desenhada, senão a lista cresce sem fim.
    primeiro = indicadores.index[0]
    acumulado = [d for d in acumulado if d.candle_hora >= primeiro]
    _sinais_fechado_cache[snapshot.ativo] = (candle, acumulado)
    return acumulado


def _atualizar_grafico_laboratorio(
    grafico: GraficoM5,
    registro: RegistroSQLite,
    estrategia: EstrategiaReversaoM5,
    snapshot: SnapshotMercado,
    indicadores: pd.DataFrame,
    setup: str | None = None,
) -> dict:
    """Desenha o M5 e todos os sinais auditados (M5/M15) daquele ativo."""
    sinais = list(registro.decisoes_grafico(snapshot.ativo)) + _sinais_so_para_ver(
        estrategia, snapshot, indicadores, setup
    )
    dados = grafico.montar_dados(
        snapshot=snapshot,
        indicadores=indicadores,
        sinais=sinais,
        possivel=None,
        alerta=_alerta_ema920_m5(snapshot, sinais),
        operacoes=registro.operacoes_grafico(snapshot.ativo),
        desempenho=registro.resumo_desempenho(snapshot.ativo),
        desempenho_por_setup=registro.desempenho_por_setup(),
        desempenho_simulado_por_setup=registro.desempenho_simulado_por_setup(),
        movimentos_unicos=registro.resumo_movimentos_unicos(),
        stats_globais=registro.stats_globais(),
        entradas_detalhadas=registro.entradas_hoje_detalhadas(),
        fibo_contexto=estrategia.mapa_fibonacci_atual(indicadores),
    )
    grafico.atualizar(snapshot.ativo, dados)
    return dados


def _alerta_ema920_m5(snapshot: SnapshotMercado, sinais: list[Decisao]) -> dict | None:
    """Expõe somente o melhor candidato do Lab para alerta manual.

    EMA9/20 M5 em EURUSD/AUDCAD é o único rastro com desempenho histórico
    positivo dos dois lados no banco. O sinal precisa estar confirmado no último
    candle fechado; rastros intravela, M15 e sombra continuam apenas no gráfico.
    """
    if len(snapshot.candles.index) < 2:
        return None
    candle_fechado = pd.Timestamp(snapshot.candles.index[-2])
    candidatos = [
        sinal
        for sinal in sinais
        if sinal.motivo == "ema920_pullback"
        and str(sinal.detalhes.get("setup", "")).startswith("M5 ")
        and sinal.detalhes.get("status_grafico") == "confirmado"
        and pd.Timestamp(sinal.candle_hora) == candle_fechado
        and sinal.ativo in {"EURUSD", "AUDCAD"}
    ]
    if not candidatos:
        return None
    sinal = candidatos[-1]
    return {
        "id": f"ema920_m5:{sinal.ativo}:{candle_fechado.isoformat()}:{sinal.direcao}",
        "ativo": sinal.ativo,
        "direcao": sinal.direcao,
        "preco": float(sinal.preco),
        "hora": candle_fechado.isoformat(),
        "setup": "EMA9/20 M5",
        "fatores": list(sinal.detalhes.get("razao") or ["toque na faixa EMA9/20"]),
        "entradaConfirmada": True,
        "mensagem": "Alerta manual: EMA9/20 M5 confirmado. Não envia ordem REAL.",
    }


def _patch_candle_ao_vivo(
    dados_base: dict,
    snapshot: SnapshotMercado,
    conversor,
    atualizado_em: float,
) -> dict | None:
    """Troca somente o último OHLC no JSON já calculado do gráfico.

    Os indicadores e sinais continuam sendo recalculados no loop principal.
    Esta função serve exclusivamente para a vela em formação acompanhar a IQ
    a cada segundo, sem transformar a visualização em critério de entrada.
    """
    candles = list(dados_base.get("candles") or [])
    if not candles or snapshot.candles.empty:
        return None
    ultima = snapshot.candles.iloc[-1]
    hora = conversor(snapshot.candles.index[-1])
    novo = {
        "time": hora,
        "open": float(ultima.Open), "high": float(ultima.High),
        "low": float(ultima.Low), "close": float(ultima.Close),
    }
    if candles[-1]["time"] == hora:
        candles[-1] = novo
    elif hora > candles[-1]["time"]:
        candles.append(novo)
    else:
        return None
    return {
        **dados_base,
        "atualizado_em": atualizado_em,
        "mercadoAberto": bool(snapshot.mercado_aberto),
        "candles": candles,
    }


def _recuperar_pendencias_periodicas(
    mercado: MercadoIQ,
    registro: RegistroSQLite,
    config: Configuracao,
    agora_monotonico: float,
    ultima_tentativa: float,
    intervalo_segundos: float = 30.0,
) -> float:
    """Tenta recuperar resultados vencidos sem precisar reiniciar o Lab."""
    if agora_monotonico - ultima_tentativa < intervalo_segundos:
        return ultima_tentativa
    if registro.operacoes_abertas():
        recuperar_operacoes_pendentes(
            mercado, registro, config=config,
            timeout_segundos=8.0,
            incluir_desconhecidas=False,
        )
    return agora_monotonico


def executar_laboratorio_ema() -> None:
    """Executa rastros EMA, mantendo toques e NZD em amostra simulada."""
    base = configuracao_ema_laboratorio_practice()
    base.validar()
    rastros = _rastros(base)
    registro = RegistroSQLite(base.banco_sqlite, config=base)
    preenchidas = retroalimentar_decisoes(registro, base)
    if preenchidas:
        print(f"Dossiê: {preenchidas} decisões antigas receberam contexto do candle.")
    mercado = MercadoIQ(base)
    calendario = CalendarioEconomico(base.pasta_dados)
    estrategias = {r.nome: EstrategiaReversaoM5(r.config) for r in rastros}
    riscos = {r.nome: GerenciadorRisco(r.config) for r in rastros}
    executores = {
        r.nome: ExecutorSeguro(r.config, mercado, riscos[r.nome], registro)
        for r in rastros
    }
    # Fecha o candle do rastro; evita avaliar o mesmo setup fechado duas vezes.
    ultimo_fechado: dict[tuple[str, str], object] = {}
    # Toque intravela: uma tentativa por setup, ativo e candle em formação.
    # Guarda só o último candle disparado por (rastro, ativo) — a checagem é
    # sempre contra o candle atual, então um set acumulava tuplas de candles
    # já passados pelo tempo todo de vida do processo, sem nunca reconsultá-las.
    intravela_disparado: dict[tuple[str, str], object] = {}
    # O candidato NZD não repete a mesma direção em pullbacks muito próximos.
    ultimo_nzd_por_direcao: dict[str, dict[str, pd.Timestamp]] = {}
    ultima_atualizacao_noticias = 0.0
    ultima_recuperacao_pendencias = 0.0
    tendencias_h1: dict[str, str] = {}
    ultima_tendencia_h1: dict[str, float] = {}
    # O loop de estratégia avalia vários rastros e pode levar mais de um
    # segundo. Esta cópia permite mover só a vela atual entre avaliações.
    dados_grafico_ao_vivo: dict[str, dict] = {}
    lock_grafico_ao_vivo = threading.Lock()
    parar_grafico_ao_vivo = threading.Event()
    progresso = ProgressoLaboratorio()

    print("=" * 68)
    print("LABORATÓRIO EMA — PRACTICE | uma conexão IQ | M5 + M15")
    ativos_ordem = tuple(ativo for ativo in base.ativos if ativo not in base.ativos_somente_sombra)
    print(f"Ordens M5/M15: {', '.join(ativos_ordem)} (EMA9/20 fechado).")
    print(f"Em sombra para comparação: {', '.join(base.ativos_somente_sombra)}.")
    print("M15: EMA9/20 ativo com filtro H1; demais setups, NZD e candidatos ficam em sombra.")
    print("Rastros: EMA9/20, EMA9/21+RSI, Fibo+S/R em sombra e NZD M5/M15+ADX.")
    print(f"Banco único: {base.banco_sqlite}")
    print("=" * 68)

    try:
        mercado.iniciar()
        mercado.iniciar_timeframes_extras({900: base.limite_candles, 3600: base.limite_candles})
        calendario.atualizar()
        recuperar_operacoes_pendentes(mercado, registro, config=base)
        ultima_recuperacao_pendencias = time.monotonic()
        grafico = None
        if base.abrir_grafico:
            try:
                grafico = GraficoM5(base)
                url_grafico = grafico.iniciar(abrir_navegador=base.abrir_navegador)
                grafico.semear_historico(registro)
                print(f"Gráfico do Lab: {url_grafico}")
            except Exception as erro:
                print(f"[GRÁFICO] indisponível ({erro}); o Lab continuará operando.")
        if grafico is not None:
            def _grafico_ao_vivo() -> None:
                while not parar_grafico_ao_vivo.wait(1.0):
                    with lock_grafico_ao_vivo:
                        pendentes = list(dados_grafico_ao_vivo.items())
                    for ativo_grafico, dados_base in pendentes:
                        try:
                            snapshot_vivo = mercado.snapshot_timeframe(ativo_grafico, 300)
                            patch = _patch_candle_ao_vivo(
                                dados_base, snapshot_vivo, grafico._unix, time.time()
                            )
                            if patch is None:
                                continue
                            grafico.atualizar(ativo_grafico, patch)
                            progresso.marcar()
                            with lock_grafico_ao_vivo:
                                dados_grafico_ao_vivo[ativo_grafico] = patch
                        except Exception:
                            # A avaliação principal continua sendo a fonte de
                            # verdade; falha visual não pode afetar ordens.
                            continue

            threading.Thread(
                target=_grafico_ao_vivo, name="ema-lab-grafico-ao-vivo", daemon=True
            ).start()
        limite_watchdog_s = max(30.0, float(base.watchdog_timeout_minutos) * 60.0)
        _armar_watchdog_apos_inicializacao(progresso)
        threading.Thread(
            target=_vigiar_laboratorio,
            args=(mercado, progresso, parar_grafico_ao_vivo, limite_watchdog_s),
            name="ema-lab-watchdog",
            daemon=True,
        ).start()
        print(f"Conectado. Monitorando {', '.join(base.ativos)} a cada 1 segundo.")

        while True:
            if kill_switch_ativo():
                print("[KILL SWITCH] arquivo detectado — laboratório encerrado.")
                break
            inicio = time.monotonic()
            ultima_recuperacao_pendencias = _recuperar_pendencias_periodicas(
                mercado, registro, base, inicio, ultima_recuperacao_pendencias
            )
            if inicio - ultima_atualizacao_noticias >= 60:
                # A classe usa TTL de uma hora; nas demais voltas esta chamada
                # só consulta memória e não faz requisição de rede.
                calendario.atualizar()
                ultima_atualizacao_noticias = inicio
            snapshots: dict[tuple[str, int], object] = {}
            for rastro in rastros:
                for ativo in rastro.config.ativos:
                    chave_snapshot = (ativo, rastro.config.timeframe_segundos)
                    snapshot = snapshots.get(chave_snapshot)
                    if snapshot is None:
                        try:
                            snapshot = mercado.snapshot_timeframe(
                                ativo, rastro.config.timeframe_segundos
                            )
                            snapshots[chave_snapshot] = snapshot
                            progresso.marcar()
                        except MercadoIndisponivel as erro:
                            print(f"[{rastro.nome}] {ativo}: indisponível ({erro})")
                            continue
                    indicadores = estrategias[rastro.nome].calcular_indicadores(
                        snapshot.candles, ativo
                    )
                    if len(indicadores) < 30:
                        continue
                    # Um painel por ativo basta: desenha os candles M5 e
                    # marca as decisões dos dois timeframes pelo rótulo M5/M15.
                    if (
                        grafico is not None
                        and rastro.config.timeframe_segundos == 300
                        and rastro.config.ema920_pullback_ativo
                    ):
                        try:
                            # O setup deste rastro filtra os sinais que o
                            # gráfico mostra com o mercado fechado, para não
                            # marcar entrada que este bot não tomaria.
                            try:
                                setup_grafico = _setup_do_rastro(rastro.config)
                            except RuntimeError:
                                setup_grafico = None
                            dados_grafico = _atualizar_grafico_laboratorio(
                                grafico, registro, estrategias[rastro.nome],
                                snapshot, indicadores, setup_grafico,
                            )
                            with lock_grafico_ao_vivo:
                                dados_grafico_ao_vivo[ativo] = dados_grafico
                        except Exception as erro:
                            print(f"[GRÁFICO] {ativo}: atualização falhou ({erro})")
                    # Sombra é observação: só lê candle e grava resultado, não
                    # toca na banca. Fica antes do portão de ordem porque o
                    # estado de abertura da IQ vem errado com frequência, e a
                    # amostra não pode parar de crescer junto com ele.
                    _resolver_sombras(registro, snapshot)
                    # Abertura/payout decide se pode enviar ordem. O gráfico
                    # deve continuar recebendo candles mesmo quando esse
                    # estado vem fechado ou temporariamente incorreto da IQ.
                    if not snapshot.mercado_aberto:
                        continue
                    try:
                        setup = _setup_do_rastro(rastro.config)
                    except RuntimeError as erro:
                        print(f"[{rastro.nome}] configuração ignorada ({erro})")
                        continue
                    if rastro.config.timeframe_segundos == 900:
                        ultimo_h1 = ultima_tendencia_h1.get(ativo, 0.0)
                        if inicio - ultimo_h1 >= rastro.config.h1_atualizar_segundos:
                            try:
                                snapshot_h1 = snapshots.get((ativo, 3600))
                                if snapshot_h1 is None:
                                    snapshot_h1 = mercado.snapshot_timeframe(ativo, 3600)
                                    snapshots[(ativo, 3600)] = snapshot_h1
                                tendencias_h1[ativo] = estrategias[rastro.nome].calcular_tendencia_h1(
                                    snapshot_h1.candles
                                )
                                ultima_tendencia_h1[ativo] = inicio
                            except MercadoIndisponivel as erro:
                                print(f"[{rastro.nome}] {ativo}: contexto H1 indisponível ({erro})")
                        estrategias[rastro.nome].atualizar_contexto_h1(
                            ativo, tendencias_h1.get(ativo, "lateral")
                        )
                    if rastro.intravela:
                        segundo = snapshot.timestamp_servidor % rastro.config.timeframe_segundos
                        limite = rastro.config.janela_entrada_por_setup[setup]
                        candle_atual = indicadores.index[-1]
                        if segundo > limite or intravela_disparado.get((rastro.nome, ativo)) == candle_atual:
                            continue
                        decisoes = estrategias[rastro.nome].avaliar_reversoes(ativo, indicadores)
                    else:
                        candle_fechado = indicadores.index[-2]
                        chave = (rastro.nome, ativo)
                        if ultimo_fechado.get(chave) == candle_fechado:
                            continue
                        ultimo_fechado[chave] = candle_fechado
                        if setup == "nzd_trend_pullback_v1":
                            contexto = snapshots.get((ativo, 900))
                            if contexto is None:
                                try:
                                    contexto = mercado.snapshot_timeframe(ativo, 900)
                                    snapshots[(ativo, 900)] = contexto
                                except MercadoIndisponivel as erro:
                                    print(f"[{rastro.nome}] {ativo}: contexto M15 indisponível ({erro})")
                                    continue
                            indicadores_m15 = estrategias[rastro.nome].calcular_indicadores(
                                contexto.candles, f"{ativo}:M15"
                            )
                            candidato = estrategias[rastro.nome].avaliar_nzd_trend_pullback(
                                ativo, indicadores, indicadores_m15, len(indicadores) - 2
                            )
                            if candidato is not None:
                                anteriores = ultimo_nzd_por_direcao.setdefault(ativo, {})
                                ultimo = anteriores.get(candidato.direcao)
                                minimo = pd.Timedelta(
                                    seconds=3 * rastro.config.timeframe_segundos
                                )
                                if ultimo is not None and candidato.candle_hora - ultimo < minimo:
                                    candidato = None
                                else:
                                    anteriores[candidato.direcao] = candidato.candle_hora
                            decisoes = [candidato] if candidato is not None else []
                        else:
                            decisoes = estrategias[rastro.nome].avaliar_todas(ativo, indicadores)

                    for decisao in decisoes:
                        if decisao.detalhes.get("setup") != setup:
                            continue
                        decisao = enriquecer_decisao(decisao, indicadores)
                        # A qualificação é observacional: o M5 continua
                        # operando igual enquanto acumulamos uma amostra nova.
                        if rastro.config.timeframe_segundos == 300:
                            decisao = qualificar_leitura_m5(decisao)
                        autorizacao_risco = riscos[rastro.nome].avaliar(snapshot, decisao)
                        agora_utc = datetime.fromtimestamp(
                            snapshot.timestamp_servidor, tz=timezone.utc
                        )
                        noticia_high = any(
                            evento.impacto == "High"
                            for evento in calendario.janela_de_risco(
                                ativo, agora_utc, antes=30, depois=30
                            )
                        )
                        motivo_sombra = None
                        if ativo in base.ativos_somente_sombra:
                            motivo_sombra = "ativo_candidato_sombra"
                        elif rastro.somente_sombra:
                            if setup == "fibo_sr_retracao":
                                motivo_sombra = "fibo_sr_validacao"
                            elif setup == "nzd_trend_pullback_v1":
                                motivo_sombra = (
                                    "nzd_v1_noticia_high"
                                    if noticia_high else "nzd_v1_validacao"
                                )
                            else:
                                motivo_sombra = "m15_h1_validacao"
                        elif ativo == "NZDUSD" and noticia_high:
                            motivo_sombra = "nzd_noticia_high"
                        autorizacao = (
                            Autorizacao(False, motivo_sombra)
                            if motivo_sombra is not None
                            else autorizacao_risco
                        )
                        registro.registrar_decisao(
                            decisao, snapshot, autorizacao,
                            timeframe=rastro.config.timeframe_segundos,
                        )
                        if rastro.intravela:
                            intravela_disparado[(rastro.nome, ativo)] = indicadores.index[-1]
                        if motivo_sombra is not None:
                            _registrar_sombra(registro, snapshot, rastro, decisao, motivo_sombra)
                        status = (
                            "ENTRANDO" if autorizacao.permitida
                            else "SOMBRA" if motivo_sombra is not None
                            else f"BLOQUEADO: {autorizacao.motivo}"
                        )
                        print(
                            f"[{datetime.now():%H:%M:%S}] [{status}] {ativo} "
                            f"{decisao.direcao.upper()} | {rastro.nome} | "
                            f"@ {decisao.preco:.5f} | {_motivos(decisao)}"
                        )
                        if autorizacao.permitida:
                            executores[rastro.nome].executar(snapshot, decisao)
            time.sleep(max(0.1, 1.0 - (time.monotonic() - inicio)))
    except KeyboardInterrupt:
        print("Interrupção solicitada. Aguardando ordens abertas...")
    finally:
        parar_grafico_ao_vivo.set()
        for executor in executores.values():
            executor.aguardar_ordens()
        if 'grafico' in locals() and grafico is not None:
            grafico.fechar()
        mercado.fechar()
