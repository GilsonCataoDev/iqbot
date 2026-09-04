import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from .forex_estrategia import plano_rompimento_reteste
from .forex_modelos import PlanoForex
from .grafico import GraficoM5
from .agente_regime import RegimeMercado, classificar as regime_classificar
from .ia import analisar as ia_analisar, montar_contexto as ia_contexto
from .mercado_iq import MercadoIQ, MercadoIndisponivel
from .modelos import Decisao
from iqoption_swing.tocaia_swing import avaliar_tocaia, tocaia_para_alerta

try:
    from plyer import notification as _notificacao
except ImportError:
    _notificacao = None


def _alerta_tocaia_auxiliar(
    config: Configuracao,
    ativo: str,
    indicadores: pd.DataFrame,
    dados_grafico: dict,
) -> dict | None:
    """Aviso auxiliar para M15/H1: espera região + gatilho, sem executar ordem."""
    if config.timeframe_segundos not in (900, 3600):
        return None
    tolerancia_pips = 8.0 if config.timeframe_segundos == 900 else 12.0
    tocaia = avaliar_tocaia(
        ativo,
        indicadores,
        fib=dados_grafico.get("fib"),
        canal=dados_grafico.get("canal"),
        niveis_sr=dados_grafico.get("niveisSR"),
        tolerancia_pips=tolerancia_pips,
    )
    if tocaia is None:
        return None
    alerta = tocaia_para_alerta(tocaia)
    tf = config.rotulo_timeframe
    tendencia = str(dados_grafico.get("tendenciaMacro") or "").lower()
    contra_tendencia = (
        (alerta["direcao"] == "put" and tendencia == "alta")
        or (alerta["direcao"] == "call" and tendencia == "baixa")
    )
    alerta["setup"] = f"tocaia_{tf.lower()}"
    alerta["mensagem"] = f"{tf}: {alerta['mensagem']}"
    alerta["fatores"] = [f"radar auxiliar {tf}", *alerta.get("fatores", [])]
    if contra_tendencia:
        direcao_original = alerta["direcao"].upper()
        alerta["direcao"] = "aviso"
        alerta["precoEntrada"] = alerta["preco"]
        alerta["radarEstado"] = "CUIDADO_TOPO" if direcao_original == "PUT" else "CUIDADO_FUNDO"
        alerta["fatores"] = [
            f"não é {direcao_original}: contra H4 {tendencia}",
            "aguardar rompimento a favor da tendência ou rejeição clara",
            *alerta["fatores"],
        ]
        alerta["mensagem"] = (
            f"{tf}: cuidado, região de {'topo' if direcao_original == 'PUT' else 'fundo'} "
            f"contra H4 {tendencia}; não é entrada imediata."
        )
    return alerta


def _plano_forex_m15(
    config: Configuracao,
    ativo: str,
    candles: pd.DataFrame,
) -> PlanoForex | None:
    if not config.forex_reteste_m15_ativo or config.timeframe_segundos != 900:
        return None
    if candles is None or len(candles) < 80:
        return None
    fechados = candles.iloc[:-1]  # último candle do stream pode estar formando
    if len(fechados) < 80:
        return None
    return plano_rompimento_reteste(ativo, fechados)


def _alerta_plano_forex(plano: PlanoForex, preco_atual: float, unix) -> dict:
    direcao = "call" if plano.lado == "buy" else "put"
    return {
        "id": f"{plano.ativo}:forex_reteste_m15:{plano.sinal_em.isoformat()}",
        "time": unix(pd.Timestamp(plano.sinal_em)),
        "direcao": direcao,
        "preco": preco_atual,
        "precoEntrada": preco_atual,
        "entradaConfirmada": True,
        "setup": "forex_reteste_m15",
        "radarEstado": "ENTRAR_FOREX_BINARIA",
        "sl": plano.stop,
        "tp": plano.alvo,
        "alvoProvavel": plano.alvo,
        "fatores": [
            "plano forex: rompimento + reteste",
            f"nível {plano.nivel:.5f}",
            f"SL visual {plano.stop:.5f}",
            f"TP visual {plano.alvo:.5f}",
        ],
        "mensagem": "Entrada forex visual; execução automática na binária practice.",
    }


def _pip_do_ativo(ativo: str) -> float:
    """Tamanho do pip: pares com JPY cotam com 2 casas, o resto com 4."""
    return 0.01 if "JPY" in ativo.upper() else 0.0001


def _plano_forex_payload(plano: PlanoForex, preco_atual: float) -> dict:
    """Card do plano forex pro grafico: onde entrar, TP, SL e R:R.

    Os pips e o R:R sao calculados aqui, no Python, e nao no JS: a conta depende
    do tamanho do pip do par, que o front nao tem como saber.
    """
    pip = _pip_do_ativo(plano.ativo)
    dist_tp = abs(plano.alvo - preco_atual) / pip
    dist_sl = abs(preco_atual - plano.stop) / pip
    risco = abs(preco_atual - plano.stop)
    return {
        "id": f"{plano.ativo}:forex_reteste_m15:{plano.sinal_em.isoformat()}",
        "ativo": plano.ativo,
        "lado": "COMPRA" if plano.lado == "buy" else "VENDA",
        "direcao": "call" if plano.lado == "buy" else "put",
        "nivel": plano.nivel,
        "entrada": preco_atual,
        "tp": plano.alvo,
        "sl": plano.stop,
        "tpPips": round(dist_tp, 1),
        "slPips": round(dist_sl, 1),
        "rr": round(abs(plano.alvo - preco_atual) / risco, 2) if risco > 0 else None,
        "motivo": plano.motivo,
        "sinalEm": plano.sinal_em.isoformat(),
        "nota": "SL/TP sao o plano forex manual. A binaria M15 entra sozinha e expira no fim da vela.",
    }


def _decisao_plano_forex(plano: PlanoForex, preco_atual: float) -> Decisao:
    direcao = "call" if plano.lado == "buy" else "put"
    return Decisao(
        ativo=plano.ativo,
        direcao=direcao,
        preco=preco_atual,
        candle_hora=pd.Timestamp(plano.sinal_em),
        motivo="forex_reteste_m15",
        detalhes={
            "setup": "forex_reteste_m15",
            "nivel_sr": plano.nivel,
            "sl": plano.stop,
            "tp": plano.alvo,
            "alvoProvavel": plano.alvo,
            "risco_preco": plano.risco_preco,
            "razao": [
                "rompimento + reteste com plano forex",
                "SL/TP são visuais para validar espaço",
                "execução é binária CALL/PUT",
            ],
        },
    )


_SETUPS_INTRACANDLE_TOQUE = {
    "sr_rejeicao",
    "fibo_sr_retracao",
    "pin_bar_sr",
    "retracao_intracandle",
    "pullback_confluencia",
    "ema921_rsi_intravela",
}


def _limite_janela_operacional(config: Configuracao) -> int:
    limite = int(config.entrada_max_segundos_no_candle)
    if not config.entrada_intracandle_por_toque_ativo or not config.janela_entrada_por_setup:
        return limite
    extras = [
        int(janela)
        for setup, janela in config.janela_entrada_por_setup.items()
        if setup in _SETUPS_INTRACANDLE_TOQUE and janela is not None
    ]
    return max([limite, *extras]) if extras else limite


# ---------------------------------------------------------------------------
# Watchdog — detecta travamento ou desconexão do loop principal
# ---------------------------------------------------------------------------
_ultimo_heartbeat: float = time.time()
_lock_heartbeat = threading.Lock()


def _atualizar_heartbeat() -> None:
    global _ultimo_heartbeat
    with _lock_heartbeat:
        _ultimo_heartbeat = time.time()


def _watchdog(config, mercado=None) -> None:  # type: ignore[no-untyped-def]
    """Thread daemon que detecta E CORRIGE trava do loop principal.

    Antes só imprimia alerta. Em 2026-08-27 o WebSocket caiu às 10:31 e o bot
    ficou 25min repetindo o alerta sem fazer nada — porque a sonda de conexão
    usava uma property em cache que nunca falha (ver MercadoIQ.conexao_viva).
    Agora o watchdog força reconexão, que é a única ação capaz de destravar.

    Backoff: tenta reconectar no máximo uma vez por ciclo de timeout, para não
    entrar em loop de reconexão quando a IQ está fora do ar.
    """
    tentativas = 0
    ultima_tentativa = 0.0
    while True:
        time.sleep(60)
        with _lock_heartbeat:
            desde = time.time() - _ultimo_heartbeat
        limite = config.watchdog_timeout_minutos * 60
        if desde <= limite:
            tentativas = 0
            continue

        print(
            f"[WATCHDOG] ALERTA: nenhum ativo processado há {desde/60:.1f} min "
            f"(limite: {config.watchdog_timeout_minutos} min)."
        )
        if mercado is None:
            continue

        # Não martela: espera um ciclo de timeout inteiro entre tentativas.
        if time.time() - ultima_tentativa < limite:
            continue
        ultima_tentativa = time.time()
        tentativas += 1
        print(f"[WATCHDOG] Forçando reconexão (tentativa {tentativas})...")
        try:
            ok = mercado.reconectar_se_necessario(forcar=True)
        except Exception as e:
            ok = False
            print(f"[WATCHDOG] Erro ao reconectar: {e!r}")
        if ok:
            print("[WATCHDOG] Reconexão OK — se o loop não voltar no próximo "
                  "ciclo, o travamento não é de conexão.")
        else:
            print(f"[WATCHDOG] Reconexão falhou ({tentativas}x). Se persistir, "
                  "reinicie o bot pelo .bat.")


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
from .risco import GerenciadorRisco, kill_switch_ativo, _base_ativo as _base_ativo_risco


def _expiracao_pendente_segundos(config: Configuracao, setup: str) -> int:
    minutos = (
        (config.expiracao_por_setup or {}).get(setup)
        if config.expiracao_por_setup
        else None
    )
    if minutos is None:
        minutos = config.expiracao_minutos
    # 0 significa fim da vela atual. Para recuperação após reinício não temos
    # o segundo exato da entrada; aguardar até um timeframe inteiro evita
    # consultar a IQ cedo e marcar um resultado provisório como definitivo.
    if float(minutos) == 0:
        return config.timeframe_segundos + 5
    return int(float(minutos) * 60 + 5)


def _segundos_ate_proxima_pendente_expirar(registro: RegistroSQLite, config: Configuracao) -> float | None:
    abertas = registro.operacoes_abertas()
    if not abertas:
        return None
    agora = datetime.now()
    restantes = []
    for ordem in abertas:
        idade = (agora - ordem.enviada_em).total_seconds()
        restantes.append(_expiracao_pendente_segundos(config, ordem.setup) - idade)
    return max(0.0, min(restantes)) if restantes else None


def _recuperar_pendencias_inicio(
    mercado: MercadoIQ,
    registro: RegistroSQLite,
    config: Configuracao,
    sleep_fn=time.sleep,
    espera_maxima_segundos: float = 900.0,
) -> None:
    estado_inicial = registro.estado_hoje()
    if not estado_inicial.ordem_pendente:
        return
    print("Operação anterior pendente. Conectando para recuperar o resultado na IQ...")
    mercado.iniciar()
    recuperar_operacoes_pendentes(mercado, registro, config)
    while registro.estado_hoje().ordem_pendente:
        restante = _segundos_ate_proxima_pendente_expirar(registro, config)
        if restante is None:
            break
        if restante > espera_maxima_segundos:
            print(
                f">> Ordem anterior ainda aberta; faltam ~{restante/60:.1f}min. "
                "Fechando agora — reinicie mais perto da expiração."
            )
            break
        # Se a IQ já passou do mark estimado mas ainda não publicou o P&L,
        # não martelar o endpoint a cada segundo. O relógio da corretora pode
        # atrasar alguns segundos; uma tentativa a cada 30s é suficiente.
        espera = 30.0 if restante <= 0 else min(30.0, max(1.0, restante + 2.0))
        print(
            f">> Ordem anterior ainda aberta; aguardando ~{espera:.0f}s "
            "para tentar recuperar de novo..."
        )
        sleep_fn(espera)
        recuperar_operacoes_pendentes(
            mercado, registro, config, incluir_desconhecidas=False
        )


def main(config: Configuracao | None = None) -> None:
    config = config or Configuracao()
    config.validar()
    registro = RegistroSQLite(config.banco_sqlite, config=config)
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
        """Atualiza candle em formação (OHLC) a cada 1s e entradas/stats a cada 15s."""
        _ultima_entradas: float = 0.0
        while grafico_thread_viva:
            time.sleep(1.0)
            if grafico is None:
                continue

            # Atualiza entradasDetalhadas e statsGlobais a cada 15s — sem esperar candle fechar.
            agora_rt = time.time()
            _entradas_frescas: dict | None = None
            if agora_rt - _ultima_entradas >= 15.0:
                try:
                    _entradas_frescas = {
                        "entradasDetalhadas": registro.entradas_hoje_detalhadas(),
                        "statsGlobais": registro.stats_globais(),
                    }
                    grafico.semear_historico(registro)
                    _ultima_entradas = agora_rt
                except Exception:
                    pass

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
                    patch = {
                        **base,
                        "atualizado_em": agora_rt,
                        "mercadoAberto": bool(sn.mercado_aberto),
                        "candles": candles,
                    }
                    if _entradas_frescas:
                        patch.update(_entradas_frescas)
                    grafico_fila.put((ativo, patch))
                except Exception:
                    # Snapshot falhou — reescreve com timestamp fresco para evitar 304.
                    patch = {**base, "atualizado_em": agora_rt}
                    if _entradas_frescas:
                        patch.update(_entradas_frescas)
                    grafico_fila.put((ativo, patch))

    if grafico is not None:
        threading.Thread(target=_grafico_worker,    name="grafico-io", daemon=True).start()
        threading.Thread(target=_grafico_rt_worker, name="grafico-rt", daemon=True).start()

    ultimo_candle_processado = {ativo: None for ativo in config.ativos}
    _candle_guard = CandleGuard()  # deduplicação e validação de ordem/fechamento
    _cooldown_ativo: dict[str, float] = {}
    _ultima_h4_por_ativo: dict[str, float] = {}
    _regime_por_ativo: dict[str, RegimeMercado | None] = {a: None for a in config.ativos}
    _ultima_h1_por_ativo: dict[str, float] = {}
    _ultima_m5_por_ativo: dict[str, float] = {}
    _ultima_m15_por_ativo: dict[str, float] = {}
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

    limite_diario = (
        f"{config.max_operacoes_dia}/dia"
        if config.max_operacoes_dia > 0
        else "sem limite diário"
    )
    tem_otc = any(ativo.upper().endswith("-OTC") for ativo in config.ativos)
    rotulo_mercado = "mercado normal e OTC" if tem_otc else "somente mercado normal"
    expiracao_rotulo = f"{config.expiracao_minutos}min"
    if (config.expiracao_por_setup or {}).get("ema921_rsi_intravela") == 0:
        expiracao_rotulo = "fim da vela atual (EMA intravela)"
    print(f"IQ Option {config.rotulo_timeframe} — {rotulo_mercado}")
    print(
        f"Conta={config.conta} | ordens={'ATIVAS' if config.executar_ordens else 'DESATIVADAS'} | "
        f"valor={config.valor_por_ordem} | máximo={limite_diario} | "
        f"expiração={expiracao_rotulo}"
    )
    if config.timeframe_segundos != 300:
        print(
            "Aviso: a triagem walk-forward mediu apenas M5. Os números daquele "
            "estudo não valem para este timeframe."
        )

    mercado_conectado = False
    estado_inicial = registro.estado_hoje()
    if estado_inicial.ordem_pendente:
        _recuperar_pendencias_inicio(mercado, registro, config)
        mercado_conectado = True

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
        print(f"Conectando e carregando candles {config.rotulo_timeframe}...")
        mercado.iniciar()

    if grafico is not None:
        try:
            url_grafico = grafico.iniciar(abrir_navegador=config.abrir_navegador)
            print(f"Gráfico aberto: {url_grafico}")
            grafico.semear_historico(registro)
            # Seed inicial: escreve JSON com candles atuais imediatamente, antes de qualquer
            # candle fechar. Elimina 404 no startup (M15 até 15min, H1 até 60min sem dados).
            for _ativo_seed in config.ativos:
                try:
                    _sn_seed = mercado.snapshot(_ativo_seed)
                    _conv = grafico._unix
                    _candles_seed = [
                        {"time": _conv(i), "open": float(r.Open), "high": float(r.High),
                         "low": float(r.Low), "close": float(r.Close)}
                        for i, r in _sn_seed.candles.iterrows()
                    ]
                    if _candles_seed:
                        _dados_seed = {
                            "par": _ativo_seed, "timeframe": config.rotulo_timeframe,
                            "timeframeSeg": config.timeframe_segundos,
                            "janelaEntradaSeg": config.entrada_max_segundos_no_candle,
                            "atualizado_em": time.time(),
                            "mercadoAberto": bool(_sn_seed.mercado_aberto),
                            "payout": _sn_seed.payout,
                            "candles": _candles_seed,
                            "volume": [], "bandaSup": [], "bandaInf": [], "bandaMedia": [],
                            "emaMicro": [], "emaMacro": [], "rsi": [],
                            "tendenciaMacro": "lateral", "tendenciaMicro": "lateral",
                            "pullbacks": [], "niveis": [], "fib": [], "confluencias": [],
                            "sinais": [], "alertaProximo": None, "operacoesReais": [],
                            "alerta": None, "explicacao": [], "noticias": [],
                            "parecerIA": None, "gatilhos": None, "desempenho": None,
                            "desempenhoPorSetup": None, "desempenhoSimuladoPorSetup": None,
                            "funil": None, "statsGlobais": None, "entradasDetalhadas": None,
                            "niveisSR": None,
                        }
                        grafico.atualizar(_ativo_seed, _dados_seed)
                except Exception:
                    pass
        except Exception as erro:
            print(f"Gráfico indisponível ({erro}); o robô continuará protegido no terminal.")
            grafico = None

    # Inicia watchdog antes do loop principal — detecta trava/desconexão silenciosa.
    _atualizar_heartbeat()  # define timestamp inicial
    threading.Thread(
        target=_watchdog,
        args=(config, mercado),
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
        ts_inicio_calculo = time.time()
        _offset_srv = obter_offset_servidor(ts_recebimento, snapshot.timestamp_servidor)

        # Fix: recaptura agora_utc aqui para eliminar drift entre ativos avaliados
        # em sequência no mesmo loop (pode ser vários segundos na versão anterior).
        agora_utc = datetime.now(timezone.utc)

        candle_fechado = snapshot.candles.index[-2]

        # --- CandleGuard: valida fechamento, duplicatas e ordem ---
        _candle_ts_unix = candle_ts_para_unix(candle_fechado)
        _guard_ok, _guard_motivo = _candle_guard.validar(
            ativo, config.timeframe_segundos, _candle_ts_unix, snapshot.timestamp_servidor
        )
        if not _guard_ok:
            if _guard_motivo == MOTIVO_CANDLE_DUPLICADO:
                # Candle já processado — silencioso (reconexão, polling rápido)
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

        print(f"[{datetime.now():%H:%M:%S}] [INICIO] {ativo}")
        indicadores = estrategia.calcular_indicadores(snapshot.candles, ativo)

        # Contexto H4/H1/M5/M15: usa pré-cache quando disponível, fallback para busca síncrona.
        with _ctx_cache_lock:
            _cached = _ctx_cache.pop(ativo, {})

        if config.filtro_h4_ativo:
            _agora_h4 = time.time()
            if _agora_h4 - _ultima_h4_por_ativo.get(ativo, 0) >= config.h4_atualizar_segundos:
                try:
                    _c = _cached.get("h4"); _candles_h4 = _c if _c is not None and not _c.empty else mercado.buscar_h4(ativo, config.h4_num_candles)
                    _tendencia_h4 = estrategia.calcular_tendencia_h4(_candles_h4)
                    estrategia.atualizar_contexto_h4(ativo, _tendencia_h4)
                    _ultima_h4_por_ativo[ativo] = _agora_h4
                    print(f"    [H4] {ativo}: TendenciaH4={_tendencia_h4}")
                    try:
                        _c = _cached.get("h1"); _candles_h1_regime = _c if _c is not None and not _c.empty else mercado.buscar_h1(ativo, config.h1_num_candles)
                        _regime = regime_classificar(ativo, _candles_h4, _candles_h1_regime)
                        if _regime:
                            _regime_por_ativo[ativo] = _regime
                            print(
                                f"    [REGIME] {ativo}: {_regime.regime.upper()} "
                                f"conf={_regime.confianca}/10 | {_regime.resumo}"
                            )
                    except Exception as _e_reg:
                        print(f"    [REGIME] {ativo}: erro — {_e_reg}")
                except Exception as _e_h4:
                    print(f"    [H4] falha ao buscar H4 para {ativo}: {_e_h4}")
        if config.filtro_m5_ativo:
            _agora_m5 = time.time()
            if _agora_m5 - _ultima_m5_por_ativo.get(ativo, 0) >= config.m5_atualizar_segundos:
                try:
                    _c = _cached.get("m5"); _candles_m5 = _c if _c is not None and not _c.empty else mercado.buscar_m5(ativo, config.m5_num_candles)
                    _estrutura_m5 = estrategia.calcular_estrutura_m5(_candles_m5)
                    estrategia.atualizar_contexto_m5(ativo, _estrutura_m5)
                    _ultima_m5_por_ativo[ativo] = _agora_m5
                    print(f"    [M5] {ativo}: EstruturaM5={_estrutura_m5}")
                except Exception as _e_m5:
                    print(f"    [M5] falha ao buscar M5 para {ativo}: {_e_m5}")
        if config.filtro_m15_ativo:
            _agora_m15 = time.time()
            if _agora_m15 - _ultima_m15_por_ativo.get(ativo, 0) >= config.m15_atualizar_segundos:
                try:
                    _c = _cached.get("m15"); _candles_m15 = _c if _c is not None and not _c.empty else mercado.buscar_m15(ativo, config.m15_num_candles)
                    _ctx_m15 = estrategia.calcular_contexto_m15(_candles_m15)
                    estrategia.atualizar_contexto_m15(ativo, _ctx_m15)
                    _ultima_m15_por_ativo[ativo] = _agora_m15
                    print(f"    [M15] {ativo}: ContextoM15={_ctx_m15}")
                except Exception as _e_m15:
                    print(f"    [M15] falha ao buscar M15 para {ativo}: {_e_m15}")
        if config.filtro_h1_ativo:
            _agora_h1 = time.time()
            if _agora_h1 - _ultima_h1_por_ativo.get(ativo, 0) >= config.h1_atualizar_segundos:
                try:
                    _c = _cached.get("h1"); _candles_h1 = _c if _c is not None and not _c.empty else mercado.buscar_h1(ativo, config.h1_num_candles)
                    _tendencia_h1 = estrategia.calcular_tendencia_h1(_candles_h1)
                    estrategia.atualizar_contexto_h1(ativo, _tendencia_h1)
                    _ultima_h1_por_ativo[ativo] = _agora_h1
                    print(f"    [H1] {ativo}: TendenciaH1={_tendencia_h1}")
                except Exception as _e_h1:
                    print(f"    [H1] falha ao buscar H1 para {ativo}: {_e_h1}")

        ts_fim_calculo = time.time()

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

        alerta = detectar_reversao(ativo, indicadores, config.timeframe_segundos) if config.reversao_candle_ativo else None
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

        plano_forex = _plano_forex_m15(config, ativo, snapshot.candles)
        alerta_forex = None
        plano_forex_card = None
        if plano_forex is not None:
            _preco_plano = float(indicadores.iloc[-1]["Close"])
            alerta_forex = _alerta_plano_forex(
                plano_forex,
                _preco_plano,
                grafico._unix if grafico is not None else lambda ts: int(pd.Timestamp(ts).timestamp()),
            )
            plano_forex_card = _plano_forex_payload(plano_forex, _preco_plano)

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
                niveis_sr_grafico = estrategia.niveis_sr_atuais(indicadores, len(indicadores) - 1)
                dados_grafico = grafico.montar_dados(
                    snapshot=snapshot,
                    indicadores=indicadores,
                    sinais=sinais_grafico,
                    possivel=estrategia.possivel_entrada(ativo, snapshot.candles),
                    operacoes=registro.operacoes_grafico(ativo),
                    alerta=(
                        para_grafico(alerta, grafico._unix, segundos_restantes)
                        if alerta is not None else alerta_forex
                    ),
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
                    entradas_detalhadas=registro.entradas_hoje_detalhadas(),
                    niveis_sr=niveis_sr_grafico,
                    plano_forex=plano_forex_card,
                )
                if dados_grafico.get("alerta") is None:
                    dados_grafico["alerta"] = _alerta_tocaia_auxiliar(
                        config, ativo, indicadores, dados_grafico
                    )
                grafico_fila.put((ativo, dados_grafico))
            except Exception as erro:
                print(f"[{datetime.now():%H:%M:%S}] {ativo}: falha ao enfileirar gráfico ({erro})")

        if ultimo_candle_processado[ativo] == candle_fechado:
            return

        limite_janela_operacional = _limite_janela_operacional(config)

        # Fora da janela de entrada o risco bloquearia qualquer ordem por
        # 'entrada_atrasada'. Se marcarmos o candle como processado aqui, o
        # próximo tick (já no candle seguinte) vai avaliar o candle recém-fechado
        # em vez do candle cujo sinal ainda estava pendente — entrada uma vela atrasada.
        if segundo_no_candle > limite_janela_operacional:
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
        aviso_noticia = calendario.aviso(ativo, agora_utc)
        confirmacao = calendario.confirmacao_recente(ativo, agora_utc)

        # Continuação: avaliada no candle FECHADO (sinal confirmado no close)
        decisoes_continuacao = estrategia.avaliar_todas(ativo, indicadores)
        # Reversão: avaliada no candle EM FORMAÇÃO (entra na mesma vela que toca o nível)
        decisoes_reversao = estrategia.avaliar_reversoes(ativo, indicadores)
        decisoes_forex = []
        if plano_forex is not None:
            decisoes_forex.append(
                _decisao_plano_forex(plano_forex, float(indicadores.iloc[-1]["Close"]))
            )
        decisoes_noticia = []
        if (
            config.noticia_confirmada_ativo
            and confirmacao
            and config.timeframe_segundos in (900, 3600)
            and not e_sintetico(ativo)
            and len(indicadores) >= 2
        ):
            direcao_noticia = str(confirmacao["direcao"]).lower()
            atual = indicadores.iloc[-1]
            anterior = indicadores.iloc[-2]
            movimento_ok = (
                direcao_noticia == "call"
                and float(atual["Close"]) > float(atual["Open"])
                and float(atual["Close"]) > float(anterior["Close"])
            ) or (
                direcao_noticia == "put"
                and float(atual["Close"]) < float(atual["Open"])
                and float(atual["Close"]) < float(anterior["Close"])
            )
            if movimento_ok:
                decisoes_noticia.append(
                    Decisao(
                        ativo=ativo,
                        direcao=direcao_noticia,
                        preco=float(atual["Close"]),
                        candle_hora=pd.Timestamp(indicadores.index[-1]),
                        motivo="noticia_confirmada_m5",
                        detalhes={
                            "setup": "noticia_confirmada",
                            "nivel_sr": float(atual["Close"]),
                            "atr": float(atual.get("ATR", 0) or 0),
                            "noticia": confirmacao["titulo"],
                            "actual": confirmacao["actual"],
                            "forecast": confirmacao["forecast"],
                            "razao": [
                                f"noticia confirmou {confirmacao['direcao']}",
                                f"actual={confirmacao['actual']} vs forecast={confirmacao['forecast']}",
                                "preco acompanhou a direcao no candle atual",
                            ],
                        },
                    )
                )
        decisoes_todas = decisoes_forex + decisoes_continuacao + decisoes_reversao + decisoes_noticia

        if not decisoes_todas:
            ultima_explicacao[ativo] = []
            dentro_da_janela = segundo_no_candle <= limite_janela_operacional
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

        with _lock_ia:
            ia_atual = parecer_ia.get(ativo)
        par_validado = not config.pares_validados or ativo in config.pares_validados

        # Reversões entram na mesma vela em formação — candle_hora já aponta para
        # indicadores.index[-1], que é o mesmo valor que proxima_vela abaixo.
        if config.cooldown_pos_ordem_por_ativo_candles > 0 and _base_ativo_risco(ativo) in _cooldown_ativo:
            _elapsed = snapshot.timestamp_servidor - _cooldown_ativo[_base_ativo_risco(ativo)]
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

        # --- Shadow logging -------------------------------------------------
        # Registra sinais BLOQUEADOS com resultado pendente, pra medir depois se
        # o filtro acertou. Nao envia ordem, nao mexe em banca: so coleta amostra.
        _payout_shadow = float(snapshot.payout) if snapshot.payout is not None else 0.85

        def _shadow(_dec, _motivo: str) -> None:
            try:
                registro.registrar_simulacao_bloqueada(
                    ativo=ativo,
                    direcao=_dec.direcao,
                    setup=_dec.detalhes.get("setup", _dec.motivo),
                    candle_hora=_dec.candle_hora,
                    preco_entrada=float(indicadores.iloc[-1]["Close"]),
                    payout=_payout_shadow,
                    motivo=_motivo,
                )
            except Exception as _e_sh:
                print(f"    [shadow] falha ao registrar: {_e_sh}")

        # Resolve shadows antigos deste ativo usando o buffer de candles.
        # Mesma regra do resultado real (mercado_iq.resultado_por_candle):
        # o candle so conta como fechado quando existe um candle posterior.
        try:
            _buf = snapshot.candles
            for _sim in registro.simulacoes_pendentes(ativo):
                _alvo = pd.Timestamp(_sim["candle_hora"])
                if _alvo not in _buf.index:
                    continue
                _pos = _buf.index.get_loc(_alvo)
                if not isinstance(_pos, int) or _pos >= len(_buf.index) - 1:
                    continue  # ainda em formacao
                _fech = float(_buf.iloc[_pos]["Close"])
                _ent = float(_sim["preco_entrada"])
                if abs(_fech - _ent) < 1e-8:
                    _res = "equal"
                else:
                    _venceu = (_fech > _ent) if _sim["direcao"] == "call" else (_fech < _ent)
                    _res = "win" if _venceu else "loss"
                registro.resolver_simulacao(_sim["id"], _res)
                print(
                    f"    [shadow] {ativo} {_sim['direcao'].upper()} "
                    f"[{_sim['setup']}] bloqueado por '{_sim['motivo']}' -> {_res.upper()}"
                )
        except Exception as _e_res:
            print(f"    [shadow] falha ao resolver: {_e_res}")

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
                    _shadow(decisao, "horario_setup")
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
            # Agente de regime: veto quando confiança >= 6 e direção é bloqueada
            _regime_ativo = _regime_por_ativo.get(ativo)
            _regime_veta = (
                _regime_ativo is not None
                and (
                    (decisao.direcao == "call" and _regime_ativo.bloquear_call)
                    or (decisao.direcao == "put" and _regime_ativo.bloquear_put)
                )
            )
            if not par_validado:
                print(f"    [{setup_nome}] par nao validado, ignorado (use backtest offline)")
                algum_bloqueio_definitivo = True
            elif _regime_veta and not favorece_noticia:
                print(
                    f"    [REGIME] BLOQUEOU [{setup_nome}]: regime={_regime_ativo.regime.upper()} "
                    f"conf={_regime_ativo.confianca}/10 veta {decisao.direcao.upper()} | {_regime_ativo.resumo}"
                )
                _shadow(decisao, "regime_llm")
                algum_bloqueio_definitivo = True
            elif ia_discorda and not favorece_noticia:
                _ia_bloqueia = config.ia_como_filtro and setup_nome not in config.ia_filtro_exceto_setups
                if _ia_bloqueia:
                    print(
                        f"    [IA] BLOQUEOU [{setup_nome}]: IA sugere {ia_atual.direcao_sugerida} "
                        f"({ia_atual.confianca}) contra o sinal {decisao.direcao.upper()}"
                    )
                    _shadow(decisao, "ia_filtro")
                    algum_bloqueio_definitivo = True
                else:
                    print(
                        f"    [IA] discorda [{setup_nome}]: sugere {ia_atual.direcao_sugerida} "
                        f"({ia_atual.confianca}) — setup excluido do filtro, seguindo sinal"
                    )
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
                    _shadow(decisao, "marcacao")
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
                        _shadow(decisao, "candle_abertura")
                        algum_bloqueio_definitivo = True
                        continue
                # Bloqueio por notícia de alto impacto (apenas ativos reais; OTC ignora).
                if config.bloquear_noticia_alto_impacto and not e_sintetico(ativo):
                    _noticias_high = [
                        e for e in calendario.janela_de_risco(ativo, agora_utc)
                        if e.impacto == "High"
                    ]
                    _noticia_liberada = (
                        config.permitir_noticia_confirmada_a_favor and favorece_noticia
                    )
                    if _noticias_high and not _noticia_liberada:
                        print(
                            f"    [{setup_nome}] notícia HIGH impacto: "
                            f"{_noticias_high[0].titulo} — cancelado"
                        )
                        _shadow(decisao, "noticia_high")
                        algum_bloqueio_definitivo = True
                        continue
                    if _noticias_high and _noticia_liberada:
                        print(
                            f"    [{setup_nome}] notícia HIGH a favor confirmada: "
                            f"{_noticias_high[0].titulo} — modo experimental liberou"
                        )
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
                    _cooldown_ativo[_base_ativo_risco(ativo)] = snapshot.timestamp_servidor
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
            tempo_restante_janela = limite_janela_operacional - segundo_no_candle
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

    # --- Pré-cache de contexto H4/H1/M5/M15 em thread dedicada ---
    # Roda fora do path crítico: atualiza caches antes do candle fechar,
    # eliminando 3-24s de latência no pior caso (quando vários timers vencem juntos).
    _ctx_cache_lock = threading.Lock()
    _ctx_cache: dict[str, dict] = {}  # ativo -> {"h4": candles, "h1": candles, ...}

    def _precache_contexto_worker():
        while not risco.resumo().encerrado:
            for _av in config.ativos:
                try:
                    _dados: dict = {}
                    if config.filtro_h4_ativo:
                        _agora = time.time()
                        if _agora - _ultima_h4_por_ativo.get(_av, 0) >= config.h4_atualizar_segundos * 0.8:
                            try:
                                _dados["h4"] = mercado.buscar_h4(_av, config.h4_num_candles)
                            except Exception:
                                pass
                    if config.filtro_h1_ativo:
                        _agora = time.time()
                        if _agora - _ultima_h1_por_ativo.get(_av, 0) >= config.h1_atualizar_segundos * 0.8:
                            try:
                                _dados["h1"] = mercado.buscar_h1(_av, config.h1_num_candles)
                            except Exception:
                                pass
                    if config.filtro_m5_ativo:
                        _agora = time.time()
                        if _agora - _ultima_m5_por_ativo.get(_av, 0) >= config.m5_atualizar_segundos * 0.8:
                            try:
                                _dados["m5"] = mercado.buscar_m5(_av, config.m5_num_candles)
                            except Exception:
                                pass
                    if config.filtro_m15_ativo:
                        _agora = time.time()
                        if _agora - _ultima_m15_por_ativo.get(_av, 0) >= config.m15_atualizar_segundos * 0.8:
                            try:
                                _dados["m15"] = mercado.buscar_m15(_av, config.m15_num_candles)
                            except Exception:
                                pass
                    if _dados:
                        with _ctx_cache_lock:
                            _ctx_cache[_av] = _dados
                except Exception:
                    pass
            time.sleep(30)

    threading.Thread(target=_precache_contexto_worker, name="precache-ctx", daemon=True).start()

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

            # Fase 1: snapshots em paralelo (reduz latência de 6s→2s com 3 ativos)
            snapshots: dict = {}
            ts_servidor_ref: int | None = None
            t_local_ref: float = time.time()
            _ativos_ciclo = [a for a in config.ativos if ativos_toggle is None or a in ativos_toggle]
            with ThreadPoolExecutor(max_workers=len(_ativos_ciclo) or 1) as _snap_pool:
                _snap_futures = {_snap_pool.submit(mercado.snapshot, a): a for a in _ativos_ciclo}
                for fut in as_completed(_snap_futures):
                    _av = _snap_futures[fut]
                    try:
                        sn = fut.result()
                        snapshots[_av] = sn
                        ts_servidor_ref = sn.timestamp_servidor
                        t_local_ref = time.time()
                    except MercadoIndisponivel as e:
                        print(f"[{datetime.now():%H:%M:%S}] {_av}: mercado indisponível ({e})")

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
            # acorda em 1s no setup de toque — reduz a distância entre o toque
            # real e a leitura. Os demais setups preservam o intervalo de 2s.
            if _retry_ativos:
                intervalo_retry = 1.0 if config.ema921_rsi_intravela_ativo else 2.0
                sono = min(sono, intervalo_retry)
            _retry_ativos.clear()

            sono = max(1.0, sono)

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
