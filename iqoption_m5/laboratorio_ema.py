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

from .config import Configuracao, configuracao_ema_laboratorio_practice, configuracao_ema_laboratorio_real
from .auditoria_entrada import (
    enriquecer_decisao,
    qualificar_leitura_m5,
    retroalimentar_decisoes,
)
from .estrategia import EstrategiaReversaoM5
from .executor import ExecutorSeguro
from .grafico import GraficoM5
from .mercado_iq import MercadoIQ, MercadoIndisponivel, iniciar_com_timeout
from .modelos import Autorizacao, Decisao, SnapshotMercado
from .noticias import CalendarioEconomico
from .recuperacao import recuperar_operacoes_pendentes
from .registro import RegistroSQLite
from .risco import GerenciadorRisco, kill_switch_ativo
from . import saude_stream


@dataclass(frozen=True)
class RastroEma:
    nome: str
    config: Configuracao
    intravela: bool
    somente_sombra: bool = False
    # Rótulo gravado em ``simulacoes.setup``. Dois rastros do mesmo setup nos
    # mesmos ativos colidiriam na chave única da tabela e um apagaria o outro;
    # o rótulo próprio mantém as amostras separadas. None = nome do setup.
    rotulo_sombra: str | None = None


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
    is_h1 = timeframe == 3600
    expiracao = 60 if is_h1 else 30 if is_m15 else 15
    janela_intravela = 2400 if is_h1 else 720 if is_m15 else 240
    return replace(
        base,
        timeframe_segundos=timeframe,
        expiracao_minutos=expiracao,
        entrada_max_segundos_no_candle=300 if is_h1 else 60 if is_m15 else 45,
        ema920_pullback_ativo=setup == "ema920_pullback",
        ema920_prime_ativo=setup == "ema920_prime",
        ema921_rsi_pullback_ativo=setup == "ema921_rsi_pullback",
        ema921_rsi_intravela_ativo=setup == "ema921_rsi_intravela",
        fibo_sr_retracao_ativo=setup == "fibo_sr_retracao",
        fibo_mtf_confirmado_ativo=setup == "fibo_mtf_confirmado",
        nzd_trend_pullback_ativo=setup == "nzd_trend_pullback_v1",
        breakout_reteste_ativo=setup == "breakout_reteste",
        # M15 só segue a favor do contexto H1. O filtro é medido em sombra
        # antes de qualquer nova ordem real nesse timeframe.
        filtro_h1_ativo=is_m15,
        # O Lab ainda não busca H4 para seus rastros. Não fingir um filtro
        # macro inexistente; a seletividade H1 vem do rompimento + reteste.
        filtro_h4_ativo=False,
        ema_micro_periodo=9 if is_h1 else base.ema_micro_periodo,
        ema_macro_periodo=21 if is_h1 else base.ema_macro_periodo,
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
    # Uma estratégia por ativo no re-teste PRACTICE. Isso impede que sinais
    # correlacionados sejam recusados pela reserva global e deixem o estudo
    # sem amostra, como ocorreu com a primeira campanha Fibo.
    # fibo_sr_retracao perdeu o EURJPY: a sombra dele acumulou 41,2% em n=119
    # (IC95 [32,7–50,2], abaixo do break-even) enquanto o ema920_pullback no
    # mesmo par mede 54,5% em n=299 — a melhor sombra de todos os ativos.
    # Sem ativo exclusivo o setup cai para sombra em toda a cesta, o que
    # também acelera a amostra que falta para decidi-lo.
    ativos_reteste_m5 = {
        "ema920_pullback": ("AUDCAD",),
        "ema921_rsi_pullback": ("USDCAD",),
        "ema921_rsi_intravela": ("AUDUSD",),
        "nzd_trend_pullback_v1": ("NZDUSD",),
    }
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
            # ema921_rsi_intravela (AUDUSD): taxa histórica 30%, abaixo do break-even — sombra até nova amostra.
            executavel_reteste = timeframe == 300 and setup in ativos_reteste_m5 and setup != "ema921_rsi_intravela"
            saida.append(
                RastroEma(
                    # O sufixo segue o status real do rastro. A versão anterior
                    # derivava do setup e marcava "sombra de validação" em
                    # rastro executável, o que enganava a leitura de log.
                    nome=(
                        f"{rotulo} | {nome}"
                        + (
                            ""
                            if executavel_reteste
                            else " (sombra H1)"
                            if timeframe == 900
                            else " (sombra de validação)"
                        )
                    ),
                    config=replace(
                        _config_rastro(base, timeframe, setup),
                        ativos=ativos_reteste_m5.get(setup, base.ativos),
                    ),
                    intravela=setup in ("ema921_rsi_intravela", "fibo_sr_retracao"),
                    # M15 passa a ser só observação: a amostra anterior foi
                    # negativa. No M5, os rastros do re-teste têm ativos
                    # exclusivos e portanto geram dados comparáveis.
                    somente_sombra=(
                        not executavel_reteste
                        or (base.confirmo_conta_real and setup != "ema920_pullback")
                    ),
                )
            )
    # NZD: WR 45.8% (n=24) abaixo do break-even — sombra de acompanhamento.
    saida.append(
        RastroEma(
            nome="M5 | NZD tendência + M15 + ADX (sombra WR<54%)",
            config=replace(
                _config_rastro(base, 300, "nzd_trend_pullback_v1"),
                ativos=ativos_reteste_m5["nzd_trend_pullback_v1"],
            ),
            intravela=False,
            somente_sombra=True,
        )
    )
    # ema920_pullback: EURUSD reteste independente do AUDCAD.
    # 60,3% em 117 ordens reais (set/01–11). Rastro isolado para que
    # movimentos correlacionados USD não disputem o slot do AUDCAD.
    saida.append(
        RastroEma(
            nome="M5 | EMA9/20 reteste EURUSD (PRACTICE)",
            config=replace(
                _config_rastro(base, 300, "ema920_pullback"),
                ativos=("EURUSD",),
            ),
            intravela=False,
            somente_sombra=False,
        )
    )
    # ema920_pullback: EURJPY reteste independente. Melhor sombra da cesta
    # (54,5% em n=299) e 83,3% em 12 ordens reais da config antiga. Rastro
    # próprio pelo mesmo motivo do EURUSD: sem slot exclusivo os sinais
    # disputam a reserva global e o estudo fica sem amostra.
    # Mantido em sombra até acumular IC95 próprio na configuração atual
    # (reativado em 2026-09-22; n insuficiente para promover ao real).
    saida.append(
        RastroEma(
            nome="M5 | EMA9/20 reteste EURJPY (SOMBRA)",
            config=replace(
                _config_rastro(base, 300, "ema920_pullback"),
                ativos=("EURJPY",),
            ),
            intravela=False,
            somente_sombra=True,
        )
    )
    # ema920_pullback na mesma configuração do real (sem filtro H1) nos pares
    # que não têm rastro próprio. Até 2026-09-24 esses pares só eram medidos
    # na variante com filtro H1, que não é a que opera — a sombra não dizia
    # se a EMA funcionaria neles como funciona em EURUSD/AUDCAD.
    # Rótulo próprio: o rastro H1 abaixo gera o mesmo setup nos mesmos pares.
    saida.append(
        RastroEma(
            nome="M5 | EMA9/20 cesta sem H1 (SOMBRA)",
            config=replace(
                _config_rastro(base, 300, "ema920_pullback"),
                ativos=("GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "EURCHF"),
            ),
            intravela=False,
            somente_sombra=True,
            rotulo_sombra="ema920_pullback_cesta",
        )
    )
    # Comparação: ema920_pullback M5 com filtro H1 ativo. O rastro principal
    # opera sem filtro H1; este acumula amostra paralela para decidir se o
    # filtro melhora o acerto antes de qualquer mudança no real.
    saida.append(
        RastroEma(
            nome="M5 | EMA9/20 + H1 (sombra comparação)",
            config=replace(
                _config_rastro(base, 300, "ema920_pullback"),
                filtro_h1_ativo=True,
            ),
            intravela=False,
            somente_sombra=True,
        )
    )
    # Campanha independente da EMA: é a regra que o backtest acabou de medir.
    # EURUSD mantido em PRACTICE. GBPUSD (41.7%, n=12) e USDJPY (45.8%, n=24)
    # abaixo do break-even (54.05%) — movidos para sombra até nova amostra.
    saida.append(
        RastroEma(
            nome="M5 | Fibo M15 50-61,8 + confirmação (PRACTICE)",
            config=replace(
                _config_rastro(base, 300, "fibo_mtf_confirmado"),
                ativos=("EURUSD",),
                entrada_max_segundos_no_candle=45,
                expiracao_minutos=15,
                expiracao_por_setup={"fibo_mtf_confirmado": 15},
            ),
            intravela=False,
            somente_sombra=base.confirmo_conta_real,
        )
    )
    # GBPUSD e USDJPY: sombra de acompanhamento enquanto WR abaixo do break-even.
    saida.append(
        RastroEma(
            nome="M5 | Fibo M15 50-61,8 GBPUSD/USDJPY (sombra WR<54%)",
            config=replace(
                _config_rastro(base, 300, "fibo_mtf_confirmado"),
                ativos=("GBPUSD", "USDJPY"),
                entrada_max_segundos_no_candle=45,
                expiracao_minutos=15,
                expiracao_por_setup={"fibo_mtf_confirmado": 15},
            ),
            intravela=False,
            somente_sombra=True,
        )
    )
    # Fibo sombra nos ativos restantes da cesta (AUDCAD, NZDUSD, AUDUSD,
    # USDCAD, EURJPY, EURCHF): coleta amostra paralela para avaliar se o
    # setup tem poder discriminante nesses pares antes de qualquer promoção.
    saida.append(
        RastroEma(
            nome="M5 | Fibo M15 50-61,8 cesta completa (sombra)",
            config=replace(
                _config_rastro(base, 300, "fibo_mtf_confirmado"),
                ativos=("AUDCAD", "NZDUSD", "AUDUSD", "USDCAD", "EURJPY", "EURCHF"),
                entrada_max_segundos_no_candle=45,
                expiracao_minutos=15,
                expiracao_por_setup={"fibo_mtf_confirmado": 15},
            ),
            intravela=False,
            somente_sombra=True,
        )
    )
    # H1 tem campanha própria, ativo exclusivo e uma única regra. A ordem só
    # existe depois de rompimento, reteste do nível e vela de confirmação.
    saida.append(
        RastroEma(
            nome="H1 | Rompimento + reteste EMA9/21 (PRACTICE)",
            config=replace(
                _config_rastro(base, 3600, "breakout_reteste"),
                ativos=("EURCHF",),
                expiracao_por_setup={"breakout_reteste": 60},
            ),
            intravela=False,
            somente_sombra=base.confirmo_conta_real,
        )
    )
    return saida


def _motivo_sombra(
    base: Configuracao,
    rastro: RastroEma,
    setup: str,
    ativo: str,
    noticia_high: bool,
    direcao: str | None = None,
    hora_utc: int | None = None,
    leitura_m5_ausente: bool = False,
    ema_sep: float | None = None,
    adx: float | None = None,
) -> str | None:
    """Por que este sinal observa em vez de mandar ordem. None = manda.

    A janela de notícia vale para qualquer par porque ``eventos_do_ativo``
    já resolve as moedas do símbolo — EURUSD recebe evento do BCE e do PPI
    americano. A versão anterior só olhava NZDUSD, que nunca chegava aqui
    por já estar em ``ativos_somente_sombra``: nenhum par tinha guarda.
    """
    if ativo in base.ativos_somente_sombra:
        return "ativo_candidato_sombra"
    # Um rastro de sombra não manda ordem por definição — o motivo é esse, e
    # não um filtro de qualidade. Os rótulos anteriores (m5_h1_validacao,
    # m15_h1_validacao, fibo_sr_validacao, nzd_v1_validacao) afirmavam
    # timeframe e filtro H1 que ninguém checava: m15_h1_validacao saiu 331
    # vezes em timeframe=300 e m5_h1_validacao 51 vezes em timeframe=900.
    # Isso inflava o peso aparente do filtro H1 em qualquer análise de funil.
    # setup e timeframe já são colunas de ``decisoes``; o motivo não precisa
    # repeti-los, só precisa ser verdadeiro.
    if rastro.somente_sombra:
        if setup == "nzd_trend_pullback_v1" and noticia_high:
            return "nzd_v1_noticia_high"
        return "rastro_sombra"
    # A campanha MTF mede a mesma regra do backtest; notícia vira contexto,
    # mas não remove silenciosamente observações do teste em PRACTICE.
    if setup == "fibo_mtf_confirmado":
        return None
    if noticia_high:
        return "noticia_high"
    # PUT entre 05h–13h BRT (08h–16h UTC): taxa histórica 41% vs CALL 66%, n=54.
    if direcao == "put" and hora_utc is not None:
        hora_brt = (hora_utc - 3) % 24
        if 5 <= hora_brt <= 13:
            return "put_horario_fraco"
    # PUT em USDJPY (36%, n=11) e USDCAD (40%, n=15): abaixo do break-even.
    if direcao == "put" and ativo in ("USDJPY", "USDCAD"):
        return "put_ativo_fraco"
    # Sem leitura M5 (auditoria ausente no candle): taxa histórica 44%, n=62.
    if leitura_m5_ausente:
        return "m5_leitura_ausente"
    return None


PAUSA_LOSSES_SEGUIDOS = 3
PAUSA_JANELA_MIN = 60


def _pausa_sequencia(
    seguidos: int, ultimo_loss: pd.Timestamp | None, agora_utc: datetime,
) -> dict | None:
    """Etiqueta de estudo: o sinal cairia numa pausa pós-sequência de losses?

    Só marca ``detalhes``; não bloqueia. A regra vira filtro apenas se a
    amostra marcada mostrar acerto abaixo do break-even.
    """
    if seguidos < PAUSA_LOSSES_SEGUIDOS or ultimo_loss is None:
        return None
    agora = pd.Timestamp(agora_utc)
    if agora.tzinfo is not None:
        agora = agora.tz_convert(None)
    minutos = (agora - ultimo_loss).total_seconds() / 60
    if minutos > PAUSA_JANELA_MIN:
        return None
    return {"losses_seguidos": seguidos, "minutos_desde_ultimo": round(minutos, 1)}


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
            ("fibo_mtf_confirmado", config.fibo_mtf_confirmado_ativo),
            ("nzd_trend_pullback_v1", config.nzd_trend_pullback_ativo),
            ("breakout_reteste", config.breakout_reteste_ativo),
        ) if ligado
    ]
    if len(ativos) != 1:
        raise RuntimeError(f"Rastro EMA inválido: esperava um setup ativo, encontrei {ativos!r}.")
    return ativos[0]


def _confirmacao_fibo_m5(vela: pd.Series, anterior: pd.Series, direcao: str) -> bool:
    """Rejeição por pavio ou engolfo, avaliada somente após fechar a M5."""
    corpo = max(abs(float(vela.Close) - float(vela.Open)), 1e-12)
    if direcao == "call":
        rejeicao = (
            min(float(vela.Open), float(vela.Close)) - float(vela.Low) >= 1.5 * corpo
            and vela.Close > vela.Open
        )
        engolfo = vela.Close > vela.Open and vela.Open <= anterior.Close and vela.Close >= anterior.Open
    else:
        rejeicao = (
            float(vela.High) - max(float(vela.Open), float(vela.Close)) >= 1.5 * corpo
            and vela.Close < vela.Open
        )
        engolfo = vela.Close < vela.Open and vela.Open >= anterior.Close and vela.Close <= anterior.Open
    return bool(rejeicao or engolfo)


def _avaliar_fibo_m15_confirmado(
    ativo: str, candles_m5: pd.DataFrame, candles_m15: pd.DataFrame
) -> Decisao | None:
    """Regra do backtest: impulso M15, zona 50--61,8%, confirmação M5."""
    if len(candles_m5) < 3 or len(candles_m15) < 30:
        return None
    sinal_m5 = candles_m5.iloc[-2]
    # Índices são inícios das velas. Excluir a M15 ainda em curso evita olhar
    # o futuro ao tomar a decisão no fechamento da M5.
    contexto = candles_m15.loc[candles_m15.index < candles_m5.index[-2]].copy()
    if len(contexto) < 30:
        return None
    contexto["ema9"] = contexto.Close.ewm(span=9, adjust=False).mean()
    contexto["ema21"] = contexto.Close.ewm(span=21, adjust=False).mean()
    janela = contexto.iloc[-5:]
    faixa_media = (contexto.High - contexto.Low).rolling(14).mean().iloc[-1]
    topo, fundo = float(janela.High.max()), float(janela.Low.min())
    amplitude = topo - fundo
    if not pd.notna(faixa_media) or faixa_media <= 0 or amplitude < 1.5 * float(faixa_media):
        return None
    ultimo = janela.iloc[-1]
    if ultimo.ema9 > ultimo.ema21 and ultimo.Close > janela.Close.iloc[0]:
        direcao, fib50, fib618 = "call", topo - .50 * amplitude, topo - .618 * amplitude
        na_zona = fib618 <= float(sinal_m5.Close) <= fib50
    elif ultimo.ema9 < ultimo.ema21 and ultimo.Close < janela.Close.iloc[0]:
        direcao, fib50, fib618 = "put", fundo + .50 * amplitude, fundo + .618 * amplitude
        na_zona = fib50 <= float(sinal_m5.Close) <= fib618
    else:
        return None
    if not na_zona or not _confirmacao_fibo_m5(sinal_m5, candles_m5.iloc[-3], direcao):
        return None
    return Decisao(
        ativo=ativo, direcao=direcao, preco=float(sinal_m5.Close),
        candle_hora=pd.Timestamp(candles_m5.index[-2]), motivo="fibo_mtf_confirmado",
        detalhes={
            "setup": "fibo_mtf_confirmado", "fibo_50": round(fib50, 6),
            "fibo_618": round(fib618, 6), "m15_topo": round(topo, 6),
            "m15_fundo": round(fundo, 6), "razao": [
                "Impulso M15 alinhado com EMA 9/21",
                "Retração na zona Fibo 50–61,8%",
                f"Confirmação M5 por rejeição/engolfo → {direcao.upper()}",
                "Entrada na próxima M5; expiração de 15 minutos",
            ],
        },
    )


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
        setup=rastro.rotulo_sombra or decisao.detalhes.get("setup", decisao.motivo),
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
        desempenho_por_campanha=registro.desempenho_por_campanha(),
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


def executar_laboratorio_ema_real() -> None:
    """Executa o laboratório EMA em conta REAL.

    Somente EURUSD+AUDCAD M5 ema920_pullback enviam ordens reais.
    EURJPY, M15, Fibo, NZD e demais rastros continuam em sombra.
    """
    _executar_laboratorio_ema(configuracao_ema_laboratorio_real())


def executar_laboratorio_ema() -> None:
    """Executa rastros EMA, mantendo toques e NZD em amostra simulada."""
    _executar_laboratorio_ema(configuracao_ema_laboratorio_practice())


def _executar_laboratorio_ema(base: Configuracao) -> None:
    """Núcleo compartilhado dos dois launchers (practice e real)."""
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
    # A hipótese Fibo entra na M5 seguinte e vence após 15min. Este intervalo
    # evita sinais sobrepostos e mantém a execução comparável ao backtest.
    ultimo_fibo_mtf: dict[str, pd.Timestamp] = {}
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
    # Rastreia o timestamp do servidor por (ativo, tf) para detectar stale por par.
    # Compartilhado com a thread de saúde do stream (leitura/escrita thread-safe
    # porque CPython garante atomicidade em atribuições de dict).
    ultimo_ts_stream: dict[tuple[str, int], int] = {}
    # Throttle do aviso [STALE]: imprime no máximo 1 vez a cada 60s por par.
    ultimo_aviso_stale: dict[tuple[str, int], float] = {}
    iniciado = False

    modo = base.conta
    print("=" * 68)
    print(f"LABORATÓRIO EMA — {modo} | uma conexão IQ | M5 + M15")
    ativos_ordem = tuple(ativo for ativo in base.ativos if ativo not in base.ativos_somente_sombra)
    print("Re-teste PRACTICE M5: EMA9/20=AUDCAD | EMA9/21=USDCAD/AUDUSD | Fibo=EURJPY | NZD=NZDUSD.")
    print("M15: somente sombra; a amostra anterior não passou no teste.")
    print("Fibo M15→M5 em PRACTICE: EURUSD, GBPUSD e USDJPY | expiração 15 min.")
    print(f"Em sombra para comparação: {', '.join(base.ativos_somente_sombra)}.")
    print("M15: EMA9/20 ativo com filtro H1; demais setups, NZD e candidatos ficam em sombra.")
    print("Rastros: EMA9/20, EMA9/21+RSI, Fibo+S/R em sombra e NZD M5/M15+ADX.")
    print(f"Banco único: {base.banco_sqlite}")
    print("=" * 68)

    try:
        iniciou, motivo_inicio = iniciar_com_timeout(mercado)
        if not iniciou:
            print(f"[CONEXÃO] {motivo_inicio} Laboratório não iniciado; reinicie quando a IQ normalizar.")
            return
        iniciado = True
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
        saude_stream.iniciar_monitor(
            ultimo_ts_stream,
            mercado,
            progresso,
            intervalo_s=600.0,
            parar=parar_grafico_ao_vivo,
        )
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
                        # Aviso antecipado de stale por par (antes de tentar o snapshot)
                        ts_anterior = ultimo_ts_stream.get(chave_snapshot, 0)
                        if ts_anterior > 0:
                            agora_stale = time.time()
                            atraso_par = agora_stale - ts_anterior
                            tf_par = rastro.config.timeframe_segundos
                            if atraso_par > tf_par * 2.5:
                                ultimo_aviso = ultimo_aviso_stale.get(chave_snapshot, 0.0)
                                if agora_stale - ultimo_aviso >= 60.0:
                                    ultimo_aviso_stale[chave_snapshot] = agora_stale
                                    print(
                                        f"[STALE] {ativo} tf={tf_par}s: "
                                        f"ultimo candle há {atraso_par:.0f}s — stream possivelmente congelado"
                                    )
                        try:
                            snapshot = mercado.snapshot_timeframe(
                                ativo, rastro.config.timeframe_segundos
                            )
                            snapshots[chave_snapshot] = snapshot
                            ultimo_ts_stream[chave_snapshot] = snapshot.timestamp_servidor
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
                    if rastro.config.filtro_h1_ativo:
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
                        if setup == "fibo_mtf_confirmado":
                            contexto = snapshots.get((ativo, 900))
                            if contexto is None:
                                try:
                                    contexto = mercado.snapshot_timeframe(ativo, 900)
                                    snapshots[(ativo, 900)] = contexto
                                except MercadoIndisponivel as erro:
                                    print(f"[{rastro.nome}] {ativo}: contexto M15 indisponível ({erro})")
                                    continue
                            candidato = _avaliar_fibo_m15_confirmado(
                                ativo, snapshot.candles, contexto.candles
                            )
                            if candidato is not None:
                                ultimo = ultimo_fibo_mtf.get(ativo)
                                if (
                                    ultimo is not None
                                    and candidato.candle_hora - ultimo < pd.Timedelta(minutes=20)
                                ):
                                    candidato = None
                                else:
                                    ultimo_fibo_mtf[ativo] = candidato.candle_hora
                            decisoes = [candidato] if candidato is not None else []
                        elif setup == "nzd_trend_pullback_v1":
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
                        _aud = decisao.detalhes.get("auditoria") or {}
                        motivo_sombra = _motivo_sombra(
                            base, rastro, setup, ativo, noticia_high,
                            direcao=decisao.direcao,
                            hora_utc=agora_utc.hour,
                            leitura_m5_ausente=decisao.detalhes.get("leitura_m5") is None,
                            ema_sep=_aud.get("ema_separacao_atr"),
                            adx=_aud.get("adx"),
                        )
                        if not rastro.somente_sombra:
                            pausa = _pausa_sequencia(
                                *registro.sequencia_losses(ativo, setup), agora_utc
                            )
                            if pausa is not None:
                                decisao = replace(
                                    decisao, detalhes={**decisao.detalhes, "pausa_sequencia": pausa}
                                )
                            # Mercado lateral: só observa (não bloqueia) até ter n≥30 em sombra.
                            # practice n=241: ema_sep<0.5 deu 68,7% vs 67,6% — sem discriminação.
                            if setup == "ema920_pullback":
                                ema_sep_v = _aud.get("ema_separacao_atr")
                                adx_v = _aud.get("adx")
                                lateral_sep = ema_sep_v is not None and ema_sep_v < 0.5
                                lateral_adx = adx_v is not None and adx_v < 20.0
                                if lateral_sep or lateral_adx:
                                    decisao = replace(
                                        decisao, detalhes={
                                            **decisao.detalhes,
                                            "mercado_lateral": {
                                                "ema_sep": ema_sep_v,
                                                "adx": adx_v,
                                                "lateral_sep": lateral_sep,
                                                "lateral_adx": lateral_adx,
                                            },
                                        }
                                    )
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
        if iniciado:
            mercado.fechar()
