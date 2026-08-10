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

    _retry_ativos: set[str] = set()
    # mercado_fechado só é soft para OTC (pode reabrir em minutos por manutenção).
    # Mercado normal fechado é definitivo — não há ponto em retentar por 200s.
    _MOTIVOS_SOFT = {"payout_indisponivel"}

    def _avaliar_ativo(ativo: str, snapshot, agora_utc: datetime) -> None:
        print(f"[{datetime.now():%H:%M:%S}] [INICIO] {ativo}")
        candle_fechado = snapshot.candles.index[-2]
        indicadores = estrategia.calcular_indicadores(snapshot.candles, ativo)

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
                    if autorizacao_reversao.permitida:
                        executor.executar(snapshot, decisao_reversao)

            marca = (alerta.hora, alerta.tipo, alerta.direcao)
            if ultimo_alerta_avisado[ativo] != marca:
                ultimo_alerta_avisado[ativo] = marca
                if alerta.tipo == "aproximando":
                    contagem_aproximando_hoje[ativo] = contagem_aproximando_hoje.get(ativo, 0) + 1
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
                # Sinais históricos só recalculam quando o candle fecha
                if candle_historico_grafico[ativo] != candle_fechado:
                    sinais_historicos_cache[ativo] = estrategia.sinais_historicos(
                        ativo, snapshot.candles
                    )
                    candle_historico_grafico[ativo] = candle_fechado

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
        proxima_vela = pd.Timestamp(indicadores.index[-1])
        todos_motivos: list[str] = []
        houve_execucao = False
        algum_bloqueio_definitivo = False  # qualquer coisa que não seja bloqueio temporário

        for decisao in decisoes_todas:
            setup_nome = decisao.detalhes.get("setup", decisao.motivo)
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
                # Marcação: reversões só entram se o preço ainda está próximo do nível de S/R.
                # Evita entrada tardia quando OTC reabre mas o preço já se afastou do gatilho.
                _reversoes = ("sr_rejeicao", "pin_bar_sr", "engulfing_sr")
                if setup_nome in _reversoes:
                    _nivel_ref = decisao.detalhes.get("nivel_sr") or decisao.preco
                    _preco_atual = float(indicadores.iloc[-1]["Close"])
                    _atr = decisao.detalhes.get("atr", 0)
                    _distancia = abs(_preco_atual - _nivel_ref)
                    _limite = config.marcacao_tolerancia_atr * _atr
                    if _atr > 0 and _distancia > _limite:
                        print(
                            f"    [{setup_nome}] marcação inválida: preço={_preco_atual:.5f} "
                            f"nível={_nivel_ref:.5f} dist={_distancia:.5f} "
                            f"máx={_limite:.5f} ({config.marcacao_tolerancia_atr}×ATR) — cancelado"
                        )
                        algum_bloqueio_definitivo = True
                        continue
                if ia_discorda and favorece_noticia:
                    print(
                        "    [IA] discordou mas a noticia confirmada a favor do sinal tem prioridade"
                    )
                executor.executar(snapshot, decisao)
                houve_execucao = True
            elif autorizacao.motivo == "mercado_fechado" and e_sintetico(ativo):
                # OTC fechado temporariamente (manutenção IQ) — retry dentro da janela
                pass
            elif autorizacao.motivo not in _MOTIVOS_SOFT:
                algum_bloqueio_definitivo = True

        # Commit slot apenas quando a avaliação foi conclusiva.
        # Soft blocks (payout_indisponivel, mercado_fechado OTC): retry dentro da janela.
        # Hard blocks (mercado normal fechado, IA, etc.): commit imediato.
        if houve_execucao or algum_bloqueio_definitivo:
            ultimo_candle_processado[ativo] = candle_fechado
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
        grafico_thread_viva = False
        executor.aguardar_ordens()
        mercado.fechar()
        if grafico is not None:
            grafico.fechar()
