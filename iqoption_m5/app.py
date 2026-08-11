import math
import threading
import time
from dataclasses import replace

try:
    import winsound
except ImportError:
    winsound = None  # type: ignore[assignment]
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue

import pandas as pd

from .alerta import anexar_noticia, detectar_reversao, explicar_decisao, niveis_gatilho, para_grafico
from .config import Configuracao
from .candle_guard import CandleGuard, candle_ts_para_unix
from .regime import detectar_regime
from .timing import (
    LatenciaSinal,
    MOTIVO_CANDLE_DUPLICADO,
    MOTIVO_CANDLE_FORA_DE_ORDEM,
    MOTIVO_CANDLE_INCOMPLETO,
    MOTIVO_FILTRO_HORARIO,
    MOTIVO_SINAL_EXPIRADO,
    calcular_fechamento_candle,
    gerar_signal_id,
    obter_offset_servidor,
    segundo_no_candle as _seg_no_candle,
    sinal_ainda_valido,
)
from .estrategia import EstrategiaReversaoM5
from .executor import ExecutorSeguro
from .grafico import GraficoM5
from .ia import analisar as ia_analisar, montar_contexto as ia_contexto
from .mercado_iq import MercadoIQ, MercadoIndisponivel
from .modelos import Decisao

try:
    from plyer import notification as _notificacao
except ImportError:
    _notificacao = None


# ---------------------------------------------------------------------------
# Watchdog — detecta travamento ou desconexão do loop principal
# ---------------------------------------------------------------------------
_ultimo_heartbeat: float = time.time()
_lock_heartbeat = threading.Lock()


def _atualizar_heartbeat() -> None:
    global _ultimo_heartbeat
    with _lock_heartbeat:
        _ultimo_heartbeat = time.time()


def _watchdog(config) -> None:  # type: ignore[no-untyped-def]
    """Thread daemon que avisa se o loop principal parou de processar ativos."""
    while True:
        time.sleep(60)
        with _lock_heartbeat:
            desde = time.time() - _ultimo_heartbeat
        limite = config.watchdog_timeout_minutos * 60
        if desde > limite:
            print(
                f"[WATCHDOG] ALERTA: nenhum ativo processado há {desde/60:.1f} min "
                f"(limite: {config.watchdog_timeout_minutos} min). "
                "Verifique conexão ou trave do processo."
            )


# ---------------------------------------------------------------------------
def _alerta_sonoro(tipo: str) -> None:
    if winsound is None:
        return
    try:
        if tipo == "entrada":
            winsound.Beep(1000, 500)
            winsound.Beep(1200, 300)
            winsound.Beep(1400, 300)
        else:
            winsound.Beep(800, 400)
    except Exception:
        pass


def _notificar_desktop(titulo: str, mensagem: str) -> None:
    if _notificacao is None:
        return
    try:
        _notificacao.notify(
            title=titulo,
            message=mensagem,
            timeout=10,
            app_name="IQ Option Monitor",
        )
    except Exception:
        pass


from .noticias import CalendarioEconomico, e_sintetico
from .recuperacao import recuperar_operacoes_pendentes
from .registro import RegistroSQLite
from .risco import GerenciadorRisco, kill_switch_ativo


def main(config: Configuracao | None = None) -> None:
    config = config or Configuracao()
    config.validar()
    registro = RegistroSQLite(config.banco_sqlite)
    mercado = MercadoIQ(config)
    estrategia = EstrategiaReversaoM5(config)
    grafico = GraficoM5(config) if config.abrir_grafico else None
    calendario = CalendarioEconomico(config.pasta_dados)

    # --- Worker do gráfico em thread dedicada (não bloqueia o loop) ---
    grafico_fila: Queue = Queue()
    grafico_thread_viva = True
    # Cache dos últimos dados completos por ativo (sinais, desempenho, alertas…)
    # O worker RT reutiliza esses campos e só substitui os dependentes de preço.
    _cache_dados_rt: dict[str, dict] = {}

    def _grafico_worker():
        while grafico_thread_viva:
            try:
                ativo, dados = grafico_fila.get(timeout=0.5)
                if grafico is not None:
                    _cache_dados_rt[ativo] = dados
                    grafico.atualizar(ativo, dados)
            except Empty:
                continue
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] [grafico-worker] erro: {e}")

    def _grafico_rt_worker():
        """Atualiza só o candle em formação (OHLC) a cada 1s — sem recalcular indicadores."""
        while grafico_thread_viva:
            time.sleep(1.0)
            if grafico is None:
                continue
            for ativo in config.ativos:
                base = _cache_dados_rt.get(ativo)
                if base is None:
                    continue  # aguarda primeira avaliação completa
                try:
                    sn = mercado.snapshot(ativo)
                    ultima = sn.candles.iloc[-1]
                    conv = grafico._unix
                    hora = conv(sn.candles.index[-1])
                    novo = {
                        "time": hora,
                        "open":  float(ultima.Open),
                        "high":  float(ultima.High),
                        "low":   float(ultima.Low),
                        "close": float(ultima.Close),
                    }
                    candles = list(base.get("candles", []))
                    if not candles:
                        continue
                    if candles[-1]["time"] == hora:
                        candles = candles[:-1] + [novo]
                    elif hora > candles[-1]["time"]:
                        candles = candles + [novo]
                    else:
                        continue  # dado mais antigo que o cache — ignora
                    grafico_fila.put((ativo, {
                        **base,
                        "atualizado_em": time.time(),
                        "mercadoAberto": bool(sn.mercado_aberto),
                        "candles": candles,
                    }))
                except Exception:
                    pass

    if grafico is not None:
        threading.Thread(target=_grafico_worker,    name="grafico-io", daemon=True).start()
        threading.Thread(target=_grafico_rt_worker, name="grafico-rt", daemon=True).start()

    ultimo_candle_processado = {ativo: None for ativo in config.ativos}
    _candle_guard = CandleGuard()  # deduplicação e validação de ordem/fechamento
    _cooldown_ativo: dict[str, float] = {}  # ativo -> unix timestamp da última ordem executada
    candle_historico_grafico = {ativo: None for ativo in config.ativos}
    sinais_historicos_cache = {ativo: [] for ativo in config.ativos}
    ultimo_alerta_avisado = {ativo: None for ativo in config.ativos}
    ultimo_alerta_executado = {ativo: None for ativo in config.ativos}
    contagem_aproximando_hoje = {ativo: 0 for ativo in config.ativos}
    dia_contagem_aproximando = datetime.now().date()
    ultimo_alerta_som = {ativo: None for ativo in config.ativos}
    COOLDOWN_ALERTA_SEGUNDOS = config.timeframe_segundos * 2
    ultima_explicacao = {ativo: [] for ativo in config.ativos}
    parecer_ia = {ativo: None for ativo in config.ativos}
    ultimo_contexto_ia = {ativo: None for ativo in config.ativos}
    _lock_ia = threading.Lock()

    # Throttles para evitar acúmulo de threads e I/O desnecessário
    _ultima_ia_por_ativo: dict[str, float] = {}
    _ultimo_grafico_por_ativo: dict[str, float] = {}

    def _pode_chamar_ia(ativo: str) -> bool:
        agora = time.time()
        if agora - _ultima_ia_por_ativo.get(ativo, 0) > 30.0:
            _ultima_ia_por_ativo[ativo] = agora
            return True
        return False

    def _pode_atualizar_grafico(ativo: str) -> bool:
        agora = time.time()
        if agora - _ultimo_grafico_por_ativo.get(ativo, 0) > 1.0:
            _ultimo_grafico_por_ativo[ativo] = agora
            return True
        return False

    print(f"IQ Option {config.rotulo_timeframe} — mercado normal e OTC")
    print(
        f"Conta={config.conta} | ordens={'ATIVAS' if config.executar_ordens else 'DESATIVADAS'} | "
        f"valor={config.valor_por_ordem} | máximo={config.max_operacoes_dia}/dia | "
        f"expiração={config.expiracao_minutos}min"
    )
    if config.timeframe_segundos != 300:
        print(
            "Aviso: a triagem walk-forward mediu apenas M5. Os números daquele "
            "estudo não valem para este timeframe."
        )

    mercado_conectado = False
    estado_inicial = registro.estado_hoje()
    if estado_inicial.ordem_pendente:
        print("Operação anterior pendente. Conectando para recuperar o resultado na IQ...")
        mercado.iniciar()
        mercado_conectado = True
        recuperar_operacoes_pendentes(mercado, registro)

    risco = GerenciadorRisco(config, registro.estado_hoje())
    executor = ExecutorSeguro(config, mercado, risco, registro)
    resumo_inicial = risco.resumo()
    if resumo_inicial.encerrado:
        print(
            f"Sessão já bloqueada pelo histórico de hoje: {resumo_inicial.motivo_encerramento}. "
            f"Enviadas={resumo_inicial.operacoes_enviadas}, lucro={resumo_inicial.lucro_sessao:+.2f}, "
            f"banca={resumo_inicial.banca_atual:.2f}"
        )
        if resumo_inicial.motivo_encerramento == "piso_banca_atingido":
            print(
                f"PISO DE BANCA ATINGIDO (R${config.piso_banca:.2f}). "
                "O bot não volta a operar sozinho — revise manualmente antes de reiniciar."
            )
        print(f"Banco: {config.banco_sqlite}")
        if mercado_conectado:
            mercado.fechar()
        return

    if not mercado_conectado:
        print("Conectando e carregando candles M5...")
        mercado.iniciar()

    if grafico is not None:
        try:
            url_grafico = grafico.iniciar()
            print(f"Gráfico M5 aberto: {url_grafico}")
        except Exception as erro:
            print(f"Gráfico indisponível ({erro}); o robô continuará protegido no terminal.")
            grafico = None

    # Inicia watchdog antes do loop principal — detecta trava/desconexão silenciosa.
    _atualizar_heartbeat()  # define timestamp inicial
    threading.Thread(
        target=_watchdog,
        args=(config,),
        name="watchdog",
        daemon=True,
    ).start()

    # Keepalive WebSocket — mantém a conexão ativa entre ciclos longos (Melhoria C)
    _keepalive_ativo = threading.Event()
    _keepalive_ativo.set()

    def _keepalive(api_ref, intervalo: float = 30.0) -> None:
        while _keepalive_ativo.is_set():
            time.sleep(intervalo)
            if not _keepalive_ativo.is_set():
                break
            try:
                api_ref.get_server_timestamp()
            except Exception as exc:
                print(f"[{datetime.now():%H:%M:%S}] [KEEPALIVE] falha: {exc}")

    if config.keepalive_intervalo_segundos > 0:
        threading.Thread(
            target=_keepalive,
            args=(mercado._api, config.keepalive_intervalo_segundos),
            name="keepalive",
            daemon=True,
        ).start()

    _retry_ativos: set[str] = set()
    # mercado_fechado só é soft para OTC (pode reabrir em minutos por manutenção).
    # Mercado normal fechado é definitivo — não há ponto em retentar por 200s.
    _MOTIVOS_SOFT = {"payout_indisponivel"}
    # Rastreia o início do 1º retry de mercado_fechado por ativo (Melhoria B)
    _retry_mercado_fechado_inicio: dict[str, float] = {}

    def _avaliar_ativo(ativo: str, snapshot, agora_utc: datetime) -> None:
        # --- Instrumentação de latência ---
        ts_recebimento = time.time()
        ts_inicio_calculo = time.monotonic()
        _offset_srv = obter_offset_servidor(ts_recebimento, snapshot.timestamp_servidor)

        # Fix: recaptura agora_utc aqui para eliminar drift entre ativos avaliados
        # em sequência no mesmo loop (pode ser vários segundos na versão anterior).
        agora_utc = datetime.now(timezone.utc)

        print(f"[{datetime.now():%H:%M:%S}] [INICIO] {ativo}")
        candle_fechado = snapshot.candles.index[-2]

        # --- CandleGuard: valida fechamento, duplicatas e ordem ---
        _candle_ts_unix = candle_ts_para_unix(candle_fechado)
        _guard_ok, _guard_motivo = _candle_guard.validar(
            ativo, config.timeframe_segundos, _candle_ts_unix, snapshot.timestamp_servidor
        )
        if not _guard_ok:
            if _guard_motivo == MOTIVO_CANDLE_DUPLICADO:
                # Candle já processado — silencioso (reconexão, polling rápido)
                print(f"[{datetime.now():%H:%M:%S}] [FIM] {ativo} ({MOTIVO_CANDLE_DUPLICADO})")
                return
            elif _guard_motivo == MOTIVO_CANDLE_FORA_DE_ORDEM:
                print(
                    f"[{datetime.now():%H:%M:%S}] {ativo}: {MOTIVO_CANDLE_FORA_DE_ORDEM} "
                    f"(recebido ts={_candle_ts_unix}, último={_candle_guard.ultimo_processado(ativo, config.timeframe_segundos)}) — ignorado"
                )
                return
            elif _guard_motivo == MOTIVO_CANDLE_INCOMPLETO:
                # Candle ainda não fechou segundo o servidor — aguarda sem marcar slot
                print(
                    f"[{datetime.now():%H:%M:%S}] {ativo}: {MOTIVO_CANDLE_INCOMPLETO} "
                    f"(ts={_candle_ts_unix}, servidor={snapshot.timestamp_servidor}) — retry"
                )
                _retry_ativos.add(ativo)
                return

        indicadores = estrategia.calcular_indicadores(snapshot.candles, ativo)

        ts_fim_calculo = time.monotonic()

        segundo_no_candle = snapshot.timestamp_servidor % config.timeframe_segundos
        segundos_restantes = config.timeframe_segundos - segundo_no_candle

        # Detecta lag de stream: só faz sentido quando o mercado está aberto.
        # Mercado fechado tem candles de horas atrás no buffer — lag esperado,
        # não stream lag. Risco já bloqueia como mercado_fechado nesses casos.
        if segundo_no_candle < config.entrada_max_segundos_no_candle and snapshot.mercado_aberto:
            ts_inicio_atual = snapshot.timestamp_servidor - segundo_no_candle
            ts_fechado_esperado = ts_inicio_atual - config.timeframe_segundos
            cf = candle_fechado
            ts_fechado_real = int(
                (cf if cf.tzinfo else cf.tz_localize("UTC")).timestamp()
            )
            if ts_fechado_real < ts_fechado_esperado - 1:
                lag = ts_fechado_esperado - ts_fechado_real
                print(
                    f"[{datetime.now():%H:%M:%S}] {ativo}: buffer desatualizado "
                    f"({lag}s atrás) — retry em 5s"
                )
                _retry_ativos.add(ativo)
                return

        proximas_noticias = []
        for evento in calendario.proximos(ativo, agora_utc, horas=8)[:6]:
            item = {
                "titulo": evento.titulo,
                "moeda": evento.moeda,
                "impacto": evento.impacto,
                "minutos": round(evento.minutos_ate(agora_utc)),
                "forecast": evento.forecast,
                "previous": evento.previous,
                "discurso": evento.e_discurso,
            }
            sugestao = evento.sugestao(ativo)
            if sugestao:
                item["acima"] = sugestao["acima_do_forecast"]
                item["abaixo"] = sugestao["abaixo_do_forecast"]
            proximas_noticias.append(item)

        alerta = detectar_reversao(ativo, indicadores, config.timeframe_segundos)
        if alerta is not None:
            alerta = anexar_noticia(alerta, calendario, agora_utc)

            par_validado = not config.pares_validados or ativo in config.pares_validados
            if alerta.tipo == "entrada" and alerta.entrada_confirmada:
                marca_execucao = (alerta.hora, alerta.direcao)
                if ultimo_alerta_executado[ativo] != marca_execucao:
                    ultimo_alerta_executado[ativo] = marca_execucao
                    decisao_reversao = Decisao(
                        ativo=ativo,
                        direcao=alerta.direcao,
                        preco=alerta.preco_entrada,
                        candle_hora=pd.Timestamp(indicadores.index[-1]),
                        motivo="reversao_candle_curta",
                        detalhes={"setup": "reversao_candle", "motivos": list(alerta.motivos)},
                    )
                    setup_real = "reversao_confluencia" if alerta.confluencia else "reversao_candle"
                    decisao_reversao = replace(
                        decisao_reversao,
                        detalhes={**decisao_reversao.detalhes, "setup": setup_real},
                    )
                    autorizacao_reversao = risco.avaliar(snapshot, decisao_reversao)
                    registro.registrar_decisao(decisao_reversao, snapshot, autorizacao_reversao)
                    print(
                        f"[{datetime.now():%H:%M:%S}] {ativo}: {setup_real} "
                        f"{alerta.direcao.upper()} @ {decisao_reversao.preco:.5f} | "
                        f"risco={autorizacao_reversao.motivo}"
                    )
                    _seg_ate_exp = config.timeframe_segundos - segundo_no_candle
                    # Fix: aplica filtro de horário também no path detectar_reversao.
                    _hora_utc = agora_utc.hour
                    _janela_rev = (config.horario_por_setup or {}).get(setup_real)
                    _fora_janela_rev = False
                    if _janela_rev is not None:
                        _h_ini_r, _h_fim_r = _janela_rev
                        _dentro_r = (
                            _h_ini_r <= _hora_utc <= _h_fim_r
                            if _h_ini_r <= _h_fim_r
                            else _hora_utc >= _h_ini_r or _hora_utc <= _h_fim_r
                        )
                        if not _dentro_r:
                            print(
                                f"    [{setup_real}] fora da janela horária "
                                f"({_h_ini_r:02d}h-{_h_fim_r:02d}h UTC, agora={_hora_utc:02d}h) — cancelado"
                            )
                            _fora_janela_rev = True
                    # Filtro de regime de mercado (reversao path)
                    _fora_regime_rev = False
                    if config.filtro_regime_ativo and config.regimes_por_setup:
                        _regimes_rev = config.regimes_por_setup.get(setup_real)
                        if _regimes_rev is not None:
                            _indice_rev = len(indicadores) - 2
                            _regime_rev = detectar_regime(
                                indicadores,
                                _indice_rev,
                                config.atr_regime_janela,
                                config.atr_max_multiplo_mediana,
                            )
                            if _regime_rev.name not in _regimes_rev:
                                print(
                                    f"    [{setup_real}] regime {_regime_rev.name} "
                                    f"não permitido {_regimes_rev} — cancelado"
                                )
                                _fora_regime_rev = True
                    if _fora_janela_rev or _fora_regime_rev:
                        pass  # bloqueado por horário ou regime
                    elif _seg_ate_exp < config.min_segundos_ate_expiracao:
                        print(
                            f"    [{setup_real}] tarde demais: "
                            f"{_seg_ate_exp:.0f}s até expiração — cancelado"
                        )
                    elif autorizacao_reversao.permitida:
                        # Instrumenta latência da reversao antes de enviar
                        _lat_rev = LatenciaSinal(
                            signal_id=decisao_reversao.signal_id,
                            ativo=ativo,
                            setup=setup_real,
                            timeframe=config.timeframe_segundos,
                            ts_recebimento_candle=ts_recebimento,
                            ts_inicio_calculo=ts_inicio_calculo,
                            ts_fim_calculo=ts_fim_calculo,
                            ts_fechamento_esperado=calcular_fechamento_candle(
                                snapshot.timestamp_servidor - config.timeframe_segundos,
                                config.timeframe_segundos,
                            ),
                            offset_servidor_s=_offset_srv,
                        )
                        _lat_rev.ts_envio_ordem = time.time()
                        _lat_rev.finalizar_envio()
                        executor.executar(snapshot, decisao_reversao)
                        _lat_rev.ts_confirmacao_ordem = time.time()
                        _lat_rev.finalizar_confirmacao()
                        print(f"    {_lat_rev.resumo()}")
                        try:
                            registro.registrar_latencia(_lat_rev)
                        except Exception as _e_lat:
                            print(f"    [lat] falha ao persistir: {_e_lat}")
                        try:
                            registro.marcar_conversao_pre_alerta(
                                ativo=ativo,
                                direcao=alerta.direcao,
                                ts_unix=snapshot.timestamp_servidor,
                                janela_s=config.timeframe_segundos,
                            )
                        except Exception as _e_conv:
                            print(f"    [conversao_pa] falha ao atualizar: {_e_conv}")

            marca = (alerta.hora, alerta.tipo, alerta.direcao)
            if ultimo_alerta_avisado[ativo] != marca:
                ultimo_alerta_avisado[ativo] = marca
                if alerta.tipo == "aproximando":
                    contagem_aproximando_hoje[ativo] = contagem_aproximando_hoje.get(ativo, 0) + 1
                    _setup_alerta = "reversao_confluencia" if alerta.confluencia else "reversao_candle"
                    try:
                        registro.registrar_pre_alerta(
                            ativo=ativo,
                            direcao=alerta.direcao,
                            nivel_sr=alerta.preco,
                            setup=_setup_alerta,
                            segundo_no_candle=segundo_no_candle,
                            ts_unix=snapshot.timestamp_servidor,
                        )
                    except Exception as _e_pa:
                        print(f"    [pre_alerta] falha ao persistir: {_e_pa}")
                print(f"[{datetime.now():%H:%M:%S}] {alerta.resumo()}")
                for motivo in alerta.motivos:
                    print(f"    - {motivo}")
                print(
                    f"    {alerta.instrucao_timing(segundos_restantes, config.timeframe_segundos, config.entrada_max_segundos_no_candle)}"
                )
                if alerta.expiracao_sugerida_min:
                    print(f"    expiracao medida para esse padrao: {alerta.expiracao_sugerida_min} min")
                if alerta.noticia:
                    print(f"    NOTICIA {alerta.noticia}")

                agora_ts = time.time()
                ultimo_som = ultimo_alerta_som[ativo]
                repetido_recente = (
                    ultimo_som is not None
                    and ultimo_som[0] == alerta.tipo
                    and ultimo_som[1] == alerta.direcao
                    and agora_ts - ultimo_som[2] < COOLDOWN_ALERTA_SEGUNDOS
                )
                if repetido_recente:
                    print("    (som/notificacao suprimidos: mesmo alerta recente)")
                else:
                    ultimo_alerta_som[ativo] = (alerta.tipo, alerta.direcao, agora_ts)
                    _alerta_sonoro(alerta.tipo)
                    _notificar_desktop(
                        f"{alerta.tipo.upper()} {alerta.direcao.upper()} {ativo}",
                        alerta.instrucao_timing(
                            segundos_restantes,
                            config.timeframe_segundos,
                            config.entrada_max_segundos_no_candle,
                        ),
                    )

                if _pode_chamar_ia(ativo):
                    def _ia_alerta(
                        av=ativo,
                        al=alerta,
                        ind=indicadores.copy(),
                        nots=list(proximas_noticias),
                    ):
                        try:
                            ctx = ia_contexto(av, config.rotulo_timeframe, al, ind, nots)
                            resultado = ia_analisar(ctx)
                            if resultado:
                                with _lock_ia:
                                    parecer_ia[av] = resultado
                                print(f"[{datetime.now():%H:%M:%S}] [IA] {av}: {resultado.texto}")
                                print(
                                    f"    [IA] direcao={resultado.direcao_sugerida or 'NEUTRO'} "
                                    f"confianca={resultado.confianca}"
                                )
                        except Exception as e:
                            print(f"    [IA] erro: {e}")

                    threading.Thread(target=_ia_alerta, daemon=True).start()

        # --- Gráfico em tempo real (atualiza a cada 3s, não só no fechamento) ---
        if grafico is not None and _pode_atualizar_grafico(ativo):
            try:
                # Sinais históricos e candles só recalculam/persistem quando o candle fecha
                if candle_historico_grafico[ativo] != candle_fechado:
                    sinais_historicos_cache[ativo] = estrategia.sinais_historicos(
                        ativo, snapshot.candles
                    )
                    candle_historico_grafico[ativo] = candle_fechado
                    # Persiste candles uma vez por candle fechado (não a cada tick)
                    try:
                        registro.salvar_candles(ativo, snapshot.candles, config.timeframe_segundos)
                    except Exception:
                        pass

                status_sinais = registro.status_decisoes_grafico(ativo)
                sinais_grafico = []
                for sinal in sinais_historicos_cache[ativo]:
                    status = status_sinais.get(
                        (sinal.candle_hora.isoformat(), sinal.direcao), "confirmado"
                    )
                    sinais_grafico.append(
                        replace(sinal, detalhes={**sinal.detalhes, "status_grafico": status})
                    )
                with _lock_ia:
                    ia_atual = parecer_ia.get(ativo)
                parecer_dict = None
                if ia_atual:
                    parecer_dict = {
                        "texto": ia_atual.texto,
                        "confianca": ia_atual.confianca,
                        "direcao": ia_atual.direcao_sugerida,
                        "segundosAtras": round(time.time() - ia_atual.gerado_em),
                    }
                dados_grafico = grafico.montar_dados(
                    snapshot=snapshot,
                    indicadores=indicadores,
                    sinais=sinais_grafico,
                    possivel=estrategia.possivel_entrada(ativo, snapshot.candles),
                    operacoes=registro.operacoes_grafico(ativo),
                    alerta=para_grafico(alerta, grafico._unix, segundos_restantes),
                    noticias=proximas_noticias,
                    explicacao=ultima_explicacao[ativo],
                    parecer_ia=parecer_dict,
                    gatilhos=niveis_gatilho(indicadores),
                    desempenho=registro.resumo_desempenho(ativo),
                    desempenho_por_setup=registro.desempenho_por_setup(),
                    desempenho_simulado_por_setup=registro.desempenho_simulado_por_setup(),
                    funil={
                        **registro.funil_reversao_hoje(),
                        "aproximando": contagem_aproximando_hoje.get(ativo, 0),
                    },
                    stats_globais=registro.stats_globais(),
                )
                grafico_fila.put((ativo, dados_grafico))
            except Exception as erro:
                print(f"[{datetime.now():%H:%M:%S}] {ativo}: falha ao enfileirar gráfico ({erro})")

        if ultimo_candle_processado[ativo] == candle_fechado:
            print(f"[{datetime.now():%H:%M:%S}] [FIM] {ativo}")
            return

        # Fora da janela de entrada o risco bloquearia qualquer ordem por
        # 'entrada_atrasada'. Se marcarmos o candle como processado aqui, o
        # próximo tick (já no candle seguinte) vai avaliar o candle recém-fechado
        # em vez do candle cujo sinal ainda estava pendente — entrada uma vela atrasada.
        if segundo_no_candle > config.entrada_max_segundos_no_candle:
            print(f"[{datetime.now():%H:%M:%S}] [FIM] {ativo} (fora da janela, slot preservado)")
            return

        # Corte por expiração: não entra se restar menos que o mínimo até o próximo mark de 5min.
        # Evita entradas tardias (ex: mercado_fechado retrying por 2min) onde a opção expira quase imediatamente.
        segundos_ate_expiracao = config.timeframe_segundos - segundo_no_candle
        if segundos_ate_expiracao < config.min_segundos_ate_expiracao:
            ultimo_candle_processado[ativo] = candle_fechado
            _candle_guard.registrar(ativo, config.timeframe_segundos, _candle_ts_unix)
            print(
                f"[{datetime.now():%H:%M:%S}] [FIM] {ativo} "
                f"(tarde demais: {segundos_ate_expiracao:.0f}s até expiração, mín {config.min_segundos_ate_expiracao}s)"
            )
            return

        ctx_candidato = ia_contexto(
            ativo, config.rotulo_timeframe, alerta, indicadores, list(proximas_noticias)
        )
        chave_ctx = tuple(
            ctx_candidato.get(campo)
            for campo in (
                "tipo",
                "direcao",
                "rsi",
                "tendencia_macro",
                "tendencia_micro",
                "corpo_atr",
                "ema_posicao",
                "motivos",
                "sugestao_noticia",
            )
        )
        if chave_ctx != ultimo_contexto_ia[ativo] and _pode_chamar_ia(ativo):
            ultimo_contexto_ia[ativo] = chave_ctx

            def _ia_candle(av=ativo, ctx=ctx_candidato):
                try:
                    resultado = ia_analisar(ctx)
                    if resultado:
                        with _lock_ia:
                            parecer_ia[av] = resultado
                        print(f"[{datetime.now():%H:%M:%S}] [IA] {av}: {resultado.texto}")
                        print(
                            f"    [IA] direcao={resultado.direcao_sugerida or 'NEUTRO'} "
                            f"confianca={resultado.confianca}"
                        )
                except Exception as e:
                    print(f"    [IA] erro: {e}")

            threading.Thread(target=_ia_candle, daemon=True).start()

        status = "aberto" if snapshot.mercado_aberto else "fechado"
        payout = f"{snapshot.payout:.0%}" if snapshot.payout is not None else "indisponível"

        # Continuação: avaliada no candle FECHADO (sinal confirmado no close)
        decisoes_continuacao = estrategia.avaliar_todas(ativo, indicadores)
        # Reversão: avaliada no candle EM FORMAÇÃO (entra na mesma vela que toca o nível)
        decisoes_reversao = estrategia.avaliar_reversoes(ativo, indicadores)
        decisoes_todas = decisoes_continuacao + decisoes_reversao

        if not decisoes_todas:
            ultima_explicacao[ativo] = []
            dentro_da_janela = segundo_no_candle < config.entrada_max_segundos_no_candle
            if dentro_da_janela and snapshot.mercado_aberto:
                # Ainda dentro da janela — aguarda toque intra-candle de reversão.
                # Não marca slot para não perder sinal que apareça nos próximos 5s.
                _retry_ativos.add(ativo)
                print(f"[{datetime.now():%H:%M:%S}] [FIM] {ativo}")
            else:
                ultimo_candle_processado[ativo] = candle_fechado
                _candle_guard.registrar(ativo, config.timeframe_segundos, _candle_ts_unix)
                print(
                    f"[{datetime.now():%H:%M:%S}] {ativo}: sem sinal | "
                    f"mercado={status} payout={payout} candle={candle_fechado}"
                )
                print(f"[{datetime.now():%H:%M:%S}] [FIM] {ativo}")
            return

        aviso_noticia = calendario.aviso(ativo, agora_utc)
        confirmacao = calendario.confirmacao_recente(ativo, agora_utc)

        with _lock_ia:
            ia_atual = parecer_ia.get(ativo)
        par_validado = not config.pares_validados or ativo in config.pares_validados

        # Reversões entram na mesma vela em formação — candle_hora já aponta para
        # indicadores.index[-1], que é o mesmo valor que proxima_vela abaixo.
        if config.cooldown_pos_ordem_por_ativo_candles > 0 and ativo in _cooldown_ativo:
            _elapsed = snapshot.timestamp_servidor - _cooldown_ativo[ativo]
            _required = config.cooldown_pos_ordem_por_ativo_candles * config.timeframe_segundos
            if _elapsed < _required:
                _restantes = math.ceil((_required - _elapsed) / config.timeframe_segundos)
                ultimo_candle_processado[ativo] = candle_fechado
                _candle_guard.registrar(ativo, config.timeframe_segundos, _candle_ts_unix)
                print(
                    f"[{datetime.now():%H:%M:%S}] [FIM] {ativo} "
                    f"(cooldown: {_restantes} candle(s) restante(s))"
                )
                return

        proxima_vela = pd.Timestamp(indicadores.index[-1])
        todos_motivos: list[str] = []
        houve_execucao = False
        algum_bloqueio_definitivo = False  # qualquer coisa que não seja bloqueio temporário

        for decisao in decisoes_todas:
            setup_nome = decisao.detalhes.get("setup", decisao.motivo)
            # Filtro de horário por setup (UTC).
            if config.horario_por_setup and setup_nome in config.horario_por_setup:
                _h_ini, _h_fim = config.horario_por_setup[setup_nome]
                _hora_utc = agora_utc.hour
                _dentro = (
                    _h_ini <= _hora_utc <= _h_fim
                    if _h_ini <= _h_fim
                    else _hora_utc >= _h_ini or _hora_utc <= _h_fim  # cruza meia-noite
                )
                if not _dentro:
                    print(
                        f"    [{setup_nome}] fora da janela horária "
                        f"({_h_ini:02d}h-{_h_fim:02d}h UTC, agora={_hora_utc:02d}h) — cancelado"
                    )
                    algum_bloqueio_definitivo = True
                    continue
            # Filtro de regime de mercado
            if config.filtro_regime_ativo and config.regimes_por_setup:
                _regimes_ok = config.regimes_por_setup.get(setup_nome)
                if _regimes_ok is not None:
                    _regime = detectar_regime(
                        indicadores,
                        len(indicadores) - 2,
                        config.atr_regime_janela,
                        config.atr_max_multiplo_mediana,
                    )
                    if _regime.name not in _regimes_ok:
                        print(
                            f"    [{setup_nome}] regime {_regime.name} "
                            f"não permitido {_regimes_ok} — cancelado"
                        )
                        algum_bloqueio_definitivo = True
                        continue
            # §11: janela de entrada por setup
            if config.janela_entrada_por_setup:
                _janela_setup = config.janela_entrada_por_setup.get(setup_nome)
                if _janela_setup is not None and segundo_no_candle > _janela_setup:
                    print(
                        f"    [{setup_nome}] fora da janela de entrada do setup "
                        f"({segundo_no_candle}s > {_janela_setup}s) — cancelado"
                    )
                    algum_bloqueio_definitivo = True
                    continue
            decisao = replace(decisao, candle_hora=proxima_vela)
            autorizacao = risco.avaliar(snapshot, decisao)
            registro.registrar_decisao(decisao, snapshot, autorizacao)
            motivos = explicar_decisao(decisao, indicadores)
            todos_motivos.extend(motivos)
            print(
                f"[{datetime.now():%H:%M:%S}] {ativo}: [{setup_nome}] {decisao.direcao.upper()} "
                f"@ {decisao.preco:.5f} | risco={autorizacao.motivo}"
            )
            for motivo in motivos:
                print(f"    - {motivo}")
            if aviso_noticia:
                print(f"    NOTÍCIA {aviso_noticia}")
            elif e_sintetico(ativo):
                print("    (ativo sintético: notícia econômica real não move esse preço)")
            if confirmacao:
                favorece_noticia = confirmacao["direcao"].lower() == decisao.direcao
                concordancia = "A FAVOR" if favorece_noticia else "CONTRA"
                print(
                    f"    NOTICIA CONFIRMADA {concordancia} do sinal: {confirmacao['titulo']} "
                    f"actual={confirmacao['actual']} forecast={confirmacao['forecast']} "
                    f"-> {confirmacao['direcao']}"
                )
            else:
                favorece_noticia = False

            ia_discorda = (
                ia_atual is not None
                and ia_atual.confianca != "baixa"
                and ia_atual.direcao_sugerida is not None
                and ia_atual.direcao_sugerida.lower() != decisao.direcao
            )
            if not par_validado:
                print(f"    [{setup_nome}] par nao validado, ignorado (use backtest offline)")
                algum_bloqueio_definitivo = True
            elif ia_discorda and not favorece_noticia:
                print(
                    f"    [IA] BLOQUEOU [{setup_nome}]: IA sugere {ia_atual.direcao_sugerida} "
                    f"({ia_atual.confianca}) contra o sinal {decisao.direcao.upper()}"
                )
                algum_bloqueio_definitivo = True
            elif autorizacao.permitida:
                # Marcação universal: valida proximidade do preço ao nível que gerou o sinal.
                # Para reversões usa nivel_sr; para continuação usa o preço do sinal (Close(N)).
                _nivel_ref = (
                    decisao.detalhes.get("nivel_sr")
                    or decisao.detalhes.get("nivel_fib")
                    or decisao.preco
                )
                _preco_atual = float(indicadores.iloc[-1]["Close"])
                _atr = decisao.detalhes.get("atr", 0)
                _distancia = abs(_preco_atual - _nivel_ref)
                _limite_marcacao = config.marcacao_tolerancia_atr * _atr
                if _atr > 0 and _distancia > _limite_marcacao:
                    print(
                        f"    [{setup_nome}] marcação inválida: preço={_preco_atual:.5f} "
                        f"nível={_nivel_ref:.5f} dist={_distancia:.5f} "
                        f"máx={_limite_marcacao:.5f} ({config.marcacao_tolerancia_atr}×ATR) — cancelado"
                    )
                    algum_bloqueio_definitivo = True
                    continue
                # Filtro de candle de entrada: cancela se N+1 abriu contra a direção do sinal.
                if config.filtro_candle_entrada_atr > 0 and _atr > 0:
                    _open_entrada = float(indicadores.iloc[-1]["Open"])
                    _close_sinal = float(indicadores.iloc[-2]["Close"])
                    _delta = _open_entrada - _close_sinal
                    _limite_filtro = config.filtro_candle_entrada_atr * _atr
                    _contra_direcao = (
                        (decisao.direcao == "call" and _delta < -_limite_filtro)
                        or (decisao.direcao == "put" and _delta > _limite_filtro)
                    )
                    if _contra_direcao:
                        print(
                            f"    [{setup_nome}] filtro de abertura: candle abriu contra "
                            f"a direção (delta={_delta:.5f}, lim={_limite_filtro:.5f}) — cancelado"
                        )
                        algum_bloqueio_definitivo = True
                        continue
                # Bloqueio por notícia de alto impacto (apenas ativos reais; OTC ignora).
                if config.bloquear_noticia_alto_impacto and not e_sintetico(ativo):
                    _noticias_high = [
                        e for e in calendario.janela_de_risco(ativo, agora_utc)
                        if e.impacto == "High"
                    ]
                    if _noticias_high:
                        print(
                            f"    [{setup_nome}] notícia HIGH impacto: "
                            f"{_noticias_high[0].titulo} — cancelado"
                        )
                        algum_bloqueio_definitivo = True
                        continue
                if ia_discorda and favorece_noticia:
                    print(
                        "    [IA] discordou mas a noticia confirmada a favor do sinal tem prioridade"
                    )
                # Instrumenta latência antes de enviar
                _lat = LatenciaSinal(
                    signal_id=decisao.signal_id,
                    ativo=ativo,
                    setup=setup_nome,
                    timeframe=config.timeframe_segundos,
                    ts_recebimento_candle=ts_recebimento,
                    ts_inicio_calculo=ts_inicio_calculo,
                    ts_fim_calculo=ts_fim_calculo,
                    ts_fechamento_esperado=calcular_fechamento_candle(
                        snapshot.timestamp_servidor - config.timeframe_segundos,
                        config.timeframe_segundos,
                    ),
                    offset_servidor_s=_offset_srv,
                )
                _lat.ts_envio_ordem = time.time()
                _lat.finalizar_envio()
                executor.executar(snapshot, decisao)
                _lat.ts_confirmacao_ordem = time.time()
                _lat.finalizar_confirmacao()
                print(f"    {_lat.resumo()}")
                try:
                    registro.registrar_latencia(_lat)
                except Exception as _e_lat:
                    print(f"    [lat] falha ao persistir: {_e_lat}")
                houve_execucao = True
                _retry_mercado_fechado_inicio.pop(ativo, None)
                if config.cooldown_pos_ordem_por_ativo_candles > 0:
                    _cooldown_ativo[ativo] = snapshot.timestamp_servidor
            elif autorizacao.motivo == "mercado_fechado" and e_sintetico(ativo):
                # OTC fechado temporariamente (manutenção IQ) — retry dentro da janela
                if config.max_retry_mercado_fechado_segundos > 0:
                    _inicio = _retry_mercado_fechado_inicio.setdefault(
                        ativo, float(snapshot.timestamp_servidor)
                    )
                    elapsed = float(snapshot.timestamp_servidor) - _inicio
                    if elapsed > config.max_retry_mercado_fechado_segundos:
                        print(
                            f"    [{setup_nome}] mercado_fechado: limite de "
                            f"{config.max_retry_mercado_fechado_segundos:.0f}s atingido — desistindo"
                        )
                        algum_bloqueio_definitivo = True
                        _retry_mercado_fechado_inicio.pop(ativo, None)
            elif autorizacao.motivo not in _MOTIVOS_SOFT:
                algum_bloqueio_definitivo = True

        # Commit slot apenas quando a avaliação foi conclusiva.
        # Soft blocks (payout_indisponivel, mercado_fechado OTC): retry dentro da janela.
        # Hard blocks (mercado normal fechado, IA, etc.): commit imediato.
        if houve_execucao or algum_bloqueio_definitivo:
            ultimo_candle_processado[ativo] = candle_fechado
            _candle_guard.registrar(ativo, config.timeframe_segundos, _candle_ts_unix)
        else:
            tempo_restante_janela = config.entrada_max_segundos_no_candle - segundo_no_candle
            if tempo_restante_janela > 5:
                _retry_ativos.add(ativo)
                print(
                    f"[{datetime.now():%H:%M:%S}] {ativo}: bloqueio temporário — "
                    f"retry em ~5s (janela: {tempo_restante_janela:.0f}s restantes)"
                )
            else:
                ultimo_candle_processado[ativo] = candle_fechado
                _candle_guard.registrar(ativo, config.timeframe_segundos, _candle_ts_unix)

        ultima_explicacao[ativo] = todos_motivos
        print(f"[{datetime.now():%H:%M:%S}] [FIM] {ativo}")

    _ultimo_teste_conexao: float = 0.0
    try:
        while not risco.resumo().encerrado:
            if kill_switch_ativo():
                print("[KILL SWITCH] arquivo kill_switch.json detectado — encerrando loop.")
                break
            # Reconexão periódica em background — throttle por tempo
            agora_loop = time.time()
            if agora_loop - _ultimo_teste_conexao > 60.0:
                _ultimo_teste_conexao = agora_loop
                threading.Thread(
                    target=mercado.reconectar_se_necessario, daemon=True
                ).start()

            if datetime.now().date() != dia_contagem_aproximando:
                dia_contagem_aproximando = datetime.now().date()
                contagem_aproximando_hoje = {ativo: 0 for ativo in config.ativos}

            ativos_toggle = grafico.ativos_ativos() if grafico else None
            agora_utc = datetime.now(timezone.utc)
            calendario.atualizar()

            # Fase 1: snapshots sequenciais
            snapshots: dict = {}
            ts_servidor_ref: int | None = None
            t_local_ref: float = time.time()
            for ativo in config.ativos:
                if ativos_toggle is not None and ativo not in ativos_toggle:
                    continue
                try:
                    sn = mercado.snapshot(ativo)
                    snapshots[ativo] = sn
                    ts_servidor_ref = sn.timestamp_servidor
                    t_local_ref = time.time()
                except MercadoIndisponivel as e:
                    print(f"[{datetime.now():%H:%M:%S}] {ativo}: mercado indisponível ({e})")

            # Fase 2: avaliação sequencial (sem ThreadPoolExecutor)
            for av, sn in snapshots.items():
                try:
                    _avaliar_ativo(av, sn, agora_utc)
                    _atualizar_heartbeat()  # ativo processado com sucesso — watchdog reset
                except Exception as exc:
                    print(f"[{datetime.now():%H:%M:%S}] {av}: erro na avaliação: {exc}")

            # --- Sleep anti-spinlock ---
            agora_epoch = time.time()
            if ts_servidor_ref is not None:
                elapsed = agora_epoch - t_local_ref
                seg_no_candle = (ts_servidor_ref + elapsed) % config.timeframe_segundos
            else:
                seg_no_candle = agora_epoch % config.timeframe_segundos

            seg_restantes = config.timeframe_segundos - seg_no_candle
            margem = 10.0

            if seg_restantes > margem + 5.0:
                sono = seg_restantes - margem
            else:
                sono = seg_restantes + 5.0

            # Se algum ativo ficou bloqueado por razão temporária dentro da janela,
            # acorda em 5s para retry sem esperar o próximo candle.
            if _retry_ativos:
                sono = min(sono, 5.0)
            _retry_ativos.clear()

            sono = max(3.0, sono)

            while sono > 0.5:
                pedaco = min(10.0, sono)
                time.sleep(pedaco)
                sono -= pedaco
                if sono > 0.5:
                    print(
                        f"[{datetime.now():%H:%M:%S}] ... dormindo, "
                        f"faltam {sono:.0f}s para próximo ciclo"
                    )
    except KeyboardInterrupt:
        print("Interrupção solicitada. Aguardando eventual ordem aberta...")
    finally:
        _keepalive_ativo.clear()
        grafico_thread_viva = False
        executor.aguardar_ordens()
        mercado.fechar()
        if grafico is not None:
            grafico.fechar()
