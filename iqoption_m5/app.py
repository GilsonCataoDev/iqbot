import threading
import time
import winsound
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone

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


def _alerta_sonoro(tipo: str) -> None:
    if tipo == "entrada":
        winsound.Beep(1000, 500)
        winsound.Beep(1200, 300)
        winsound.Beep(1400, 300)
    else:
        winsound.Beep(800, 400)


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
from .risco import GerenciadorRisco


def main(config: Configuracao | None = None) -> None:
    config = config or Configuracao()
    config.validar()
    registro = RegistroSQLite(config.banco_sqlite)
    mercado = MercadoIQ(config)
    estrategia = EstrategiaReversaoM5(config)
    grafico = GraficoM5(config) if config.abrir_grafico else None
    calendario = CalendarioEconomico(config.pasta_dados)
    ultimo_candle_processado = {ativo: None for ativo in config.ativos}
    candle_historico_grafico = {ativo: None for ativo in config.ativos}
    sinais_historicos_cache = {ativo: [] for ativo in config.ativos}
    ultimo_alerta_avisado = {ativo: None for ativo in config.ativos}
    ultimo_alerta_executado = {ativo: None for ativo in config.ativos}
    contagem_aproximando_hoje = {ativo: 0 for ativo in config.ativos}
    dia_contagem_aproximando = datetime.now().date()
    ultimo_alerta_som = {ativo: None for ativo in config.ativos}  # (tipo, direcao, momento_wall_clock)
    COOLDOWN_ALERTA_SEGUNDOS = config.timeframe_segundos * 2
    ultima_explicacao = {ativo: [] for ativo in config.ativos}
    parecer_ia = {ativo: None for ativo in config.ativos}
    ultimo_contexto_ia = {ativo: None for ativo in config.ativos}
    _lock_ia = threading.Lock()

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

    def _avaliar_ativo(ativo: str, snapshot, agora_utc: datetime) -> None:
        candle_fechado = snapshot.candles.index[-2]
        indicadores = estrategia.calcular_indicadores(snapshot.candles)

        segundo_no_candle = snapshot.timestamp_servidor % config.timeframe_segundos
        segundos_restantes = config.timeframe_segundos - segundo_no_candle

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

            # Esta e a unica familia com indicio real de vantagem na
            # triagem walk-forward (56-58% em GBPUSD/EURUSD/USDJPY).
            # Ate aqui ela so avisava; agora tambem manda ordem de
            # verdade, em paralelo ao pullback/bollinger, pra dar pra
            # comparar o desempenho real das duas.
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
                    if not par_validado:
                        setup_simulado = "reversao_confluencia" if alerta.confluencia else "reversao_candle_nao_validado"
                        print(
                            f"[{datetime.now():%H:%M:%S}] {ativo}: {setup_simulado} "
                            f"{alerta.direcao.upper()} @ {decisao_reversao.preco:.5f} | "
                            f"par nao validado, simulando (sem dinheiro real)"
                        )

                        def _simular_reversao(av=ativo, d=decisao_reversao, pay=snapshot.payout or 0.0, setup=setup_simulado):
                            timeout = config.expiracao_minutos * 60 + 90
                            resultado = mercado.resultado_por_candle(av, d.direcao, d.preco, d.candle_hora, timeout)
                            registro.registrar_simulacao(av, d.direcao, setup, d.candle_hora, d.preco, pay, resultado)
                            if resultado:
                                print(f"    [SIMULADO] {av} {setup}: resultado={resultado} (sem dinheiro real)")

                        threading.Thread(target=_simular_reversao, daemon=True).start()
                    else:
                        setup_real = "reversao_confluencia" if alerta.confluencia else "reversao_candle"
                        decisao_reversao = replace(decisao_reversao, detalhes={**decisao_reversao.detalhes, "setup": setup_real})
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
                print(f"    {alerta.instrucao_timing(segundos_restantes, config.timeframe_segundos, config.entrada_max_segundos_no_candle)}")
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
                        alerta.instrucao_timing(segundos_restantes, config.timeframe_segundos, config.entrada_max_segundos_no_candle),
                    )

                def _ia_alerta(av=ativo, al=alerta, ind=indicadores.copy(), nots=list(proximas_noticias)):
                    try:
                        ctx = ia_contexto(av, config.rotulo_timeframe, al, ind, nots)
                        resultado = ia_analisar(ctx)
                        if resultado:
                            with _lock_ia:
                                parecer_ia[av] = resultado
                            print(f"[{datetime.now():%H:%M:%S}] [IA] {av}: {resultado.texto}")
                            print(f"    [IA] direcao={resultado.direcao_sugerida or 'NEUTRO'} confianca={resultado.confianca}")
                    except Exception as e:
                        print(f"    [IA] erro: {e}")

                threading.Thread(target=_ia_alerta, daemon=True).start()

        if grafico is not None:
            try:
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
                )
                grafico.atualizar(ativo, dados_grafico)
            except Exception as erro:
                print(f"[{datetime.now():%H:%M:%S}] {ativo}: falha ao atualizar gráfico ({erro})")

        if ultimo_candle_processado[ativo] == candle_fechado:
            return
        ultimo_candle_processado[ativo] = candle_fechado

        ctx_candidato = ia_contexto(ativo, config.rotulo_timeframe, alerta, indicadores, list(proximas_noticias))
        chave_ctx = tuple(
            ctx_candidato.get(campo)
            for campo in ("tipo", "direcao", "rsi", "tendencia_macro", "tendencia_micro",
                          "corpo_atr", "ema_posicao", "motivos", "sugestao_noticia")
        )
        if chave_ctx != ultimo_contexto_ia[ativo]:
            ultimo_contexto_ia[ativo] = chave_ctx

            def _ia_candle(av=ativo, ctx=ctx_candidato):
                try:
                    resultado = ia_analisar(ctx)
                    if resultado:
                        with _lock_ia:
                            parecer_ia[av] = resultado
                        print(f"[{datetime.now():%H:%M:%S}] [IA] {av}: {resultado.texto}")
                        print(f"    [IA] direcao={resultado.direcao_sugerida or 'NEUTRO'} confianca={resultado.confianca}")
                except Exception as e:
                    print(f"    [IA] erro: {e}")

            threading.Thread(target=_ia_candle, daemon=True).start()
        else:
            print(f"    [IA] contexto igual ao anterior, pulando chamada para {ativo}")

        status = "aberto" if snapshot.mercado_aberto else "fechado"
        payout = f"{snapshot.payout:.0%}" if snapshot.payout is not None else "indisponível"
        decisoes_todas = estrategia.avaliar_todas(ativo, indicadores)
        if not decisoes_todas:
            ultima_explicacao[ativo] = []
            print(
                f"[{datetime.now():%H:%M:%S}] {ativo}: sem sinal | "
                f"mercado={status} payout={payout} candle={candle_fechado}"
            )
            return

        aviso_noticia = calendario.aviso(ativo, agora_utc)
        confirmacao = calendario.confirmacao_recente(ativo, agora_utc)

        with _lock_ia:
            ia_atual = parecer_ia.get(ativo)
        par_validado = not config.pares_validados or ativo in config.pares_validados

        todos_motivos: list[str] = []
        for decisao in decisoes_todas:
            setup_nome = decisao.detalhes.get("setup", decisao.motivo)
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
                def _simular(
                    av=ativo, d=decisao, pay=snapshot.payout or 0.0,
                    entrada_hora=pd.Timestamp(indicadores.index[-1]),
                    entrada_preco=float(indicadores.iloc[-1]["Open"]),
                    sn=setup_nome,
                ):
                    timeout = config.expiracao_minutos * 60 + 90
                    resultado = mercado.resultado_por_candle(av, d.direcao, entrada_preco, entrada_hora, timeout)
                    registro.registrar_simulacao(av, d.direcao, sn, entrada_hora, entrada_preco, pay, resultado)
                    if resultado:
                        print(f"    [SIMULADO] {av} {sn}: resultado={resultado} (par nao validado)")
                threading.Thread(target=_simular, daemon=True).start()
            elif ia_discorda and not favorece_noticia:
                print(
                    f"    [IA] BLOQUEOU [{setup_nome}]: IA sugere {ia_atual.direcao_sugerida} "
                    f"({ia_atual.confianca}) contra o sinal {decisao.direcao.upper()}"
                )
            elif autorizacao.permitida:
                if ia_discorda and favorece_noticia:
                    print("    [IA] discordou mas a noticia confirmada a favor do sinal tem prioridade")
                executor.executar(snapshot, decisao)

        ultima_explicacao[ativo] = todos_motivos

    try:
        while not risco.resumo().encerrado:
            if datetime.now().date() != dia_contagem_aproximando:
                dia_contagem_aproximando = datetime.now().date()
                contagem_aproximando_hoje = {ativo: 0 for ativo in config.ativos}
            ativos_toggle = grafico.ativos_ativos() if grafico else None
            agora_utc = datetime.now(timezone.utc)
            calendario.atualizar()

            # Fase 1: snapshots sequenciais (API tem lock interno)
            snapshots: dict = {}
            for ativo in config.ativos:
                if ativos_toggle is not None and ativo not in ativos_toggle:
                    continue
                try:
                    snapshots[ativo] = mercado.snapshot(ativo)
                except MercadoIndisponivel as e:
                    print(f"[{datetime.now():%H:%M:%S}] {ativo}: mercado indisponível ({e})")

            # Fase 2: avaliação paralela — indicadores + estratégia + execução
            with ThreadPoolExecutor(max_workers=max(len(snapshots), 1)) as pool:
                futs = [pool.submit(_avaliar_ativo, av, sn, agora_utc) for av, sn in snapshots.items()]
                for fut in as_completed(futs):
                    try:
                        fut.result()
                    except Exception as exc:
                        print(f"[{datetime.now():%H:%M:%S}] [worker] erro: {exc}")

            time.sleep(config.intervalo_loop_segundos)
    except KeyboardInterrupt:
        print("Interrupção solicitada. Aguardando eventual ordem aberta...")
    finally:
        executor.aguardar_ordens()
        mercado.fechar()
        if grafico is not None:
            grafico.fechar()
