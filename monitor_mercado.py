"""Monitor de mercado — forex + crypto, com contexto de regime.

SEPARACAO DELIBERADA entre duas coisas:

  SINAL DE ENTRADA — so o que passou em teste rigoroso:
      falso rompimento LONG. Forex: WR 55.44% (n_ef=7526, EDGE).
      Crypto: WR 57.66% (n_ef=3195, EDGE). Ambos acima do breakeven 54.05%
      e bem acima do baseline aleatorio.

  CONTEXTO — exibido, NAO operado:
      regime (lateral / canal de alta / canal de baixa / tendencia), bandas do
      canal, tendencia do dia. Medido em 01/09/2026 e REPROVADO como sinal:
      operar A FAVOR do canal (51.14%) rende o MESMO que operar CONTRA (51.11%),
      logo a classificacao nao tem conteudo direcional. Serve para leitura de
      tela e para checar depois se filtra alguma coisa — nao para entrar.

Essa separacao existe porque o bot antigo tinha setups com "80% de WR" que eram
lookahead. Contexto bonito no painel vira sinal de entrada na cabeca de quem
opera; deixar o rotulo explicito e a protecao.

Porta 8777.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import motor_sinais as M
from estrategias_regime import regressao, classificar
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.grafico import GraficoM5
from iqoption_m5.mercado_iq import MercadoIQ
from iqoption_m5.monitor_store import MonitorEventStore, SCHEMA_VERSAO
from iqoption_m5.noticias import CalendarioEconomico

FOREX = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD",
         "EURGBP", "EURCHF", "EURCAD", "GBPJPY", "GBPCHF", "GBPCAD", "CADCHF"]
# Cripto normal, sem OTC. Fica 24/7 porque esse bloco foi medido separado.
CRYPTO = ["BTCUSD", "ETHUSD", "XRPUSD"]
# Ouro e uma classe própria: lê o mesmo contexto M15 e o plano Fibo, mas não
# herda a validação estatística do Forex. XAU/USD também recebe notícias USD.
COMMODITIES = ["XAUUSD"]
ATIVOS = FOREX + CRYPTO + COMMODITIES
CLASSE = {
    **{a: "forex" for a in FOREX},
    **{a: "crypto" for a in CRYPTO},
    **{a: "ouro" for a in COMMODITIES},
}

TF = 900
# Velas desenhadas no grafico. df_grafico ja carrega 300; o corte antigo
# em 60 mostrava so 15h de M15. 240 da 60h sem custo de download.
VELAS_GRAFICO = 240
PORTA = 8777
INTERVALO_S = 30
JAN = 10
SIMULADOR_VERSAO = 2


def adquirir_trava_monitor(porta: int = PORTA + 10_000) -> socket.socket:
    """Impede duas instâncias de gravarem os mesmos JSONs e sinais."""
    trava = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        trava.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        trava.bind(("127.0.0.1", porta))
        trava.listen(1)
        return trava
    except OSError:
        trava.close()
        raise


def janela_validada_para(classe: str, hora_utc: int) -> bool:
    """Só devolve elegibilidade onde o falso rompimento foi medido.

    Cripto foi avaliado 24/7. Ouro não deve receber, por semelhança, a janela
    de Forex: por enquanto seus sinais ficam como estudo e o plano Fibo serve
    apenas para leitura/registro.
    """
    return classe == "crypto" or (classe == "forex" and hora_utc in (21, 22))


def unidade_movimento(ativo: str) -> tuple[float, str]:
    """Unidade legível sem chamar variação de Bitcoin de 'pips'."""
    classe = CLASSE.get(ativo, "forex")
    if classe == "crypto":
        return 1.0, "USD"
    if classe == "ouro":
        return 0.01, "pontos"
    return M.pip(ativo), "pips"


def tendencia_dia(df: pd.DataFrame) -> dict:
    """Direcao do dia: preco vs abertura do dia, e EMA de 4h."""
    hoje = df.index.normalize() == df.index[-1].normalize()
    d = df[hoje]
    if len(d) < 2:
        return {"dir": "—", "pct": 0.0, "ema": "—"}
    ab = float(d["Open"].iloc[0])
    at = float(d["Close"].iloc[-1])
    pct = (at - ab) / ab * 100 if ab else 0.0
    e_r = df["Close"].ewm(span=16, adjust=False).mean()    # ~4h em M15
    e_l = df["Close"].ewm(span=96, adjust=False).mean()    # ~1 dia
    ema = "alta" if float(e_r.iloc[-1]) > float(e_l.iloc[-1]) else "baixa"
    return {"dir": "alta" if pct > 0.05 else ("baixa" if pct < -0.05 else "lateral"),
            "pct": round(pct, 2), "ema": ema}


def _tendencia_ema(fechamentos: pd.Series) -> str:
    """Direção objetiva de uma série fechada; ``lateral`` não serve ao Fibo."""
    if len(fechamentos) < 22:
        return "lateral"
    rapida = fechamentos.ewm(span=9, adjust=False).mean().iloc[-1]
    lenta = fechamentos.ewm(span=21, adjust=False).mean().iloc[-1]
    if not np.isfinite(rapida) or not np.isfinite(lenta):
        return "lateral"
    return "alta" if rapida > lenta else "baixa" if rapida < lenta else "lateral"


def _tendencia_h1_a_partir_m15(df: pd.DataFrame) -> str:
    """Calcula H1 de velas M15 já fechadas, sem buscar uma segunda fonte.

    A última hora costuma estar incompleta; ela é descartada para que uma vela
    M15 em formação não mude a direção H1 exibida.
    """
    try:
        h1 = df["Close"].resample("1h").last().dropna()
        return _tendencia_ema(h1.iloc[:-1])
    except (TypeError, ValueError, AttributeError):
        return "lateral"


def _ultimo_impulso(df: pd.DataFrame, atr: pd.Series, direcao: str) -> dict | None:
    """Último impulso de pelo menos 1.5 ATR, sem olhar candles futuros.

    O extremo final precisa ser o maior/menor desde o começo do impulso. Isso
    evita desenhar Fibo em todo candle pequeno de uma consolidação.
    """
    janela = df.tail(64)
    atr_janela = atr.reindex(janela.index)
    if len(janela) < 16:
        return None
    for fim in range(len(janela) - 2, 7, -1):
        atr_fim = float(atr_janela.iloc[fim])
        if not np.isfinite(atr_fim) or atr_fim <= 0:
            continue
        inicio = max(0, fim - 32)
        antes = janela.iloc[inicio:fim]
        if antes.empty:
            continue
        if direcao == "buy":
            inicio_rel = int(antes["Low"].values.argmin())
            comeco = inicio + inicio_rel
            base = float(janela["Low"].iloc[comeco])
            extremo = float(janela["High"].iloc[fim])
            confirmou_extremo = extremo >= float(janela["High"].iloc[comeco:fim + 1].max())
        else:
            inicio_rel = int(antes["High"].values.argmax())
            comeco = inicio + inicio_rel
            base = float(janela["High"].iloc[comeco])
            extremo = float(janela["Low"].iloc[fim])
            confirmou_extremo = extremo <= float(janela["Low"].iloc[comeco:fim + 1].min())
        amplitude = abs(extremo - base)
        if confirmou_extremo and amplitude >= 1.5 * atr_fim:
            return {
                "inicio": janela.index[comeco], "fim": janela.index[fim],
                "base": base, "extremo": extremo, "amplitude": amplitude,
                "atr": atr_fim,
            }
    return None


def plano_fibo(df: pd.DataFrame, atr: pd.Series, ativo: str,
                noticia: dict | None = None) -> dict:
    """Mapa Fibo M15 de estudo: zona, invalidação, alvos e confirmação.

    Não cria sinal operacional e não muda ``decisao_entrada``. O propósito é
    tornar a espera por retração e a relação risco/retorno observáveis antes de
    decidir se este filtro merece uma validação estatística própria.
    """
    sem_plano = {"disponivel": False, "estado": "SEM PLANO FIBO"}
    if len(df) < 96 or atr.empty:
        return sem_plano
    m15 = _tendencia_ema(df["Close"])
    h1 = _tendencia_h1_a_partir_m15(df)
    if m15 not in ("alta", "baixa") or h1 != m15:
        return {
            **sem_plano, "m15": m15, "h1": h1,
            "estado": "SEM ALINHAMENTO H1/M15",
            "motivo": "Fibo é apenas leitura quando M15 e H1 não apontam a mesma direção.",
        }
    direcao = "buy" if m15 == "alta" else "sell"
    impulso = _ultimo_impulso(df, atr, direcao)
    if impulso is None:
        return {
            **sem_plano, "m15": m15, "h1": h1, "direcao": direcao,
            "estado": "SEM IMPULSO LIMPO", "motivo": "A última perna não alcançou 1.5 ATR.",
        }

    base, extremo, amp, atr_atual = (
        impulso["base"], impulso["extremo"], impulso["amplitude"], impulso["atr"]
    )
    preco = float(df["Close"].iloc[-1])
    vela = df.iloc[-1]
    margem = 0.15 * atr_atual
    if direcao == "buy":
        fib382, fib500, fib618, fib786 = (
            extremo - amp * .382, extremo - amp * .500,
            extremo - amp * .618, extremo - amp * .786,
        )
        zona_inf, zona_sup = fib618, fib382
        entrada, sl, tp1 = fib618, fib786 - margem, extremo
        tp2, tp3 = extremo + amp * .272, extremo + amp * .618
        invalida = preco <= fib786
        na_zona = zona_inf <= preco <= zona_sup
        confirmou = (
            float(vela["Low"]) <= zona_sup and float(vela["Low"]) > sl
            and float(vela["Close"]) > float(vela["Open"])
            and float(vela["Close"]) >= fib500
        )
    else:
        fib382, fib500, fib618, fib786 = (
            extremo + amp * .382, extremo + amp * .500,
            extremo + amp * .618, extremo + amp * .786,
        )
        zona_inf, zona_sup = fib382, fib618
        entrada, sl, tp1 = fib618, fib786 + margem, extremo
        tp2, tp3 = extremo - amp * .272, extremo - amp * .618
        invalida = preco >= fib786
        na_zona = zona_inf <= preco <= zona_sup
        confirmou = (
            float(vela["High"]) >= zona_inf and float(vela["High"]) < sl
            and float(vela["Close"]) < float(vela["Open"])
            and float(vela["Close"]) <= fib500
        )
    risco = abs(entrada - sl)
    rr = abs(tp1 - entrada) / risco if risco > 0 else 0.0
    noticia_risco = (noticia or {}).get("estado") == "janela_risco"
    qualificada = bool(confirmou and rr >= 1.5 and not invalida and not noticia_risco)
    if invalida:
        estado = "IDEIA INVALIDADA"
        motivo = "Preço fechou além de 78.6% da retração."
    elif noticia_risco:
        estado = "NOTÍCIA — AGUARDAR"
        motivo = "Há notícia relevante na janela de risco."
    elif qualificada:
        estado = f"ENTRADA FIBO {direcao.upper()} — ESTUDO"
        motivo = "Zona tocada, rejeição confirmada e R:R mínimo de 1.5."
    elif na_zona:
        estado = "NA ZONA — ESPERAR REJEIÇÃO"
        motivo = "Preço chegou à retração; ainda falta candle de confirmação."
    else:
        estado = "AGUARDAR ZONA FIBO"
        motivo = "Aguarde o preço alcançar a zona 38.2%–61.8%."
    checklist = [
        {"nome": "Tendência H1 e M15 alinhadas", "ok": True},
        {"nome": "Impulso limpo de pelo menos 1.5 ATR", "ok": True},
        {"nome": "Preço na zona Fibo 38.2%–61.8%", "ok": na_zona},
        {"nome": "Rejeição a favor da tendência", "ok": confirmou},
        {"nome": "R:R até TP1 de pelo menos 1.5", "ok": rr >= 1.5},
        {"nome": "Sem notícia de alto impacto na janela", "ok": not noticia_risco},
    ]
    passo, unidade = unidade_movimento(ativo)
    arred = lambda valor: round(float(valor), 6)
    return {
        "disponivel": True, "direcao": direcao, "m15": m15, "h1": h1,
        "estado": estado, "motivo": motivo, "qualificada": qualificada,
        "na_zona": na_zona, "confirmou": confirmou, "rr": round(rr, 2),
        "zona_inf": arred(zona_inf), "zona_sup": arred(zona_sup),
        "fib382": arred(fib382), "fib500": arred(fib500),
        "fib618": arred(fib618), "fib786": arred(fib786),
        "impulso_inicio": str(impulso["inicio"]), "impulso_fim": str(impulso["fim"]),
        "amplitude_atr": round(amp / atr_atual, 2),
        "checklist": checklist,
        "alvos": {
            "entrada": arred(entrada), "sl": arred(sl), "tp1": arred(tp1),
            "tp2": arred(tp2), "tp3": arred(tp3),
            "risco_pips": round(risco / passo, 1),
            "risco_unidade": unidade, "modo_entrada": "limite",
        },
    }


def plano_varredura_liquidez(df: pd.DataFrame, atr: pd.Series, ativo: str,
                             noticia: dict | None = None) -> dict:
    """Varredura BUY objetiva, publicada somente como estudo em sombra.

    Diferente do falso rompimento antigo, exige que o pavio atravesse o piso
    anterior e que o candle feche novamente dentro do range. A entrada só é
    considerada caso o preço rompa a máxima do candle de varredura.
    """
    vazio = {
        "disponivel": False, "sinal_estudo": False, "direcao": "buy",
        "estado": "SEM VARREDURA", "motivo": "Aguardando estrutura suficiente.",
    }
    if CLASSE.get(ativo) != "forex" or len(df) < 60 or atr.empty:
        return vazio

    av = float(atr.reindex(df.index).iloc[-1])
    if not np.isfinite(av) or av <= 0:
        return vazio
    anteriores = df.iloc[-11:-1]
    if len(anteriores) != 10:
        return vazio

    amp = df["High"] - df["Low"]
    media_10 = amp.shift(1).rolling(10).mean()
    limiar = media_10.expanding(min_periods=40).quantile(.25).shift(1)
    media_atual, limiar_atual = float(media_10.iloc[-1]), float(limiar.iloc[-1])
    acumulacao = bool(
        np.isfinite(media_atual) and np.isfinite(limiar_atual)
        and media_atual <= limiar_atual
    )

    piso = float(anteriores["Low"].min())
    teto = float(anteriores["High"].max())
    vela = df.iloc[-1]
    abertura, maxima, minima, fechamento = map(
        float, (vela["Open"], vela["High"], vela["Low"], vela["Close"])
    )
    alcance = max(maxima - minima, 1e-12)
    varreu = minima < piso
    reclaim = fechamento > piso
    penetracao_atr = max(0.0, piso - minima) / av
    pavio_inferior = min(abertura, fechamento) - minima
    rejeicao = pavio_inferior / alcance >= .35 and (fechamento - minima) / alcance >= .60

    entrada = maxima + .02 * av
    sl = minima - .10 * av
    risco = entrada - sl
    recompensa = teto - entrada
    rr = recompensa / risco if risco > 0 else 0.0
    noticia_risco = (noticia or {}).get("estado") == "janela_risco"
    penetracao_ok = .05 <= penetracao_atr <= .75
    qualificada = bool(
        acumulacao and varreu and reclaim and rejeicao and penetracao_ok
        and rr >= 1.5 and not noticia_risco
    )

    if noticia_risco:
        estado, motivo = "NOTÍCIA — AGUARDAR", "Varredura ocorreu dentro da janela de notícia."
    elif qualificada:
        estado = "VARREDURA BUY — ESTUDO"
        motivo = "Pavio varreu o piso, fechou dentro do range e deixou alvo estrutural >= 1.5R."
    elif varreu and not reclaim:
        estado, motivo = "ROMPIMENTO, NÃO VARREDURA", "O candle não fechou novamente dentro do range."
    elif varreu:
        estado, motivo = "VARREDURA FRACA", "Faltou rejeição, acumulação ou espaço mínimo até o alvo."
    else:
        estado, motivo = "AGUARDAR VARREDURA", "O piso das dez velas anteriores ainda não foi varrido."

    passo, unidade = unidade_movimento(ativo)
    arred = lambda valor: round(float(valor), 6)
    return {
        "disponivel": True, "sinal_estudo": qualificada, "direcao": "buy",
        "estado": estado, "motivo": motivo, "acumulacao": acumulacao,
        "varreu": varreu, "reclaim": reclaim, "rejeicao": rejeicao,
        "penetracao_atr": round(penetracao_atr, 2), "rr": round(rr, 2),
        "piso_range": arred(piso), "teto_range": arred(teto),
        "h1": _tendencia_h1_a_partir_m15(df),
        "checklist": [
            {"nome": "Acumulação antes da varredura", "ok": acumulacao},
            {"nome": "Pavio atravessou o piso anterior", "ok": varreu},
            {"nome": "Fechamento voltou para dentro do range", "ok": reclaim},
            {"nome": "Pavio/rejeição forte", "ok": rejeicao},
            {"nome": "Varredura entre 0.05 e 0.75 ATR", "ok": penetracao_ok},
            {"nome": "Espaço até o teto de pelo menos 1.5R", "ok": rr >= 1.5},
            {"nome": "Sem notícia de alto impacto na janela", "ok": not noticia_risco},
        ],
        "alvos": {
            "entrada": arred(entrada), "sl": arred(sl), "tp1": arred(teto),
            "risco_pips": round(risco / passo, 1), "risco_unidade": unidade,
            "modo_entrada": "stop",
        },
    }


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval [lower%, upper%] para uma proporção wins/n.

    Retorna None quando n == 0.  Prefira esse intervalo ao de Wald porque ele
    permanece dentro de [0, 100] mesmo com amostras muito pequenas ou WR perto
    de 0%/100%.
    """
    if n <= 0:
        return None
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * (p * (1.0 - p) / n + z * z / (4.0 * n * n)) ** 0.5 / denom
    return (round((center - margin) * 100, 1), round((center + margin) * 100, 1))


def contexto_noticia(calendario: CalendarioEconomico | None, ativo: str,
                      agora: datetime) -> dict:
    """Salva um fato do calendário; nunca deriva direção sem dado publicado."""
    if calendario is None:
        return {"estado": "sem_calendario", "texto": "Calendário não disponível."}
    confirmado = calendario.confirmacao_recente(ativo, agora)
    if confirmado:
        return {
            "estado": "resultado_publicado", "texto": confirmado["titulo"],
            "moeda": confirmado["moeda"], "actual": confirmado["actual"],
            "forecast": confirmado["forecast"], "direcao": confirmado["direcao"],
        }
    aviso = calendario.aviso(ativo, agora)
    return {
        "estado": "janela_risco" if aviso and not aviso.startswith("próximo:") else "sem_risco",
        "texto": aviso or "Sem evento relevante na janela de risco.",
    }


def dossie_candle(df: pd.DataFrame, atr: pd.Series, ativo: str, noticia: dict) -> dict:
    """Contexto observável do candle de sinal; não decide nem explica causalidade."""
    vela = df.iloc[-1]
    abertura, maxima, minima, fechamento = (float(vela[c]) for c in ("Open", "High", "Low", "Close"))
    amplitude = maxima - minima
    atr_atual = float(atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) else 0.0
    if amplitude <= 0 or atr_atual <= 0:
        return {"tags": [], "noticia": noticia}
    corpo = abs(fechamento - abertura) / amplitude
    pavio_sup = (maxima - max(abertura, fechamento)) / amplitude
    pavio_inf = (min(abertura, fechamento) - minima) / amplitude
    anteriores = df.iloc[max(0, len(df) - 4):-1]
    movimento_3 = ((fechamento - float(anteriores["Close"].iloc[0])) / atr_atual
                   if not anteriores.empty else 0.0)
    atr_ref = atr.iloc[max(0, len(atr) - 31):-1].median()
    atr_rel = float(atr_atual / atr_ref) if np.isfinite(atr_ref) and atr_ref > 0 else 1.0
    volume_rel = None
    if "Volume" in df:
        volume_ref = df["Volume"].iloc[max(0, len(df) - 21):-1].median()
        if np.isfinite(volume_ref) and volume_ref > 0 and np.isfinite(vela.get("Volume", np.nan)):
            volume_rel = float(vela["Volume"] / volume_ref)
    tipo = "alta" if fechamento > abertura else "baixa" if fechamento < abertura else "doji"
    tags = [
        f"vela_{tipo}",
        "vela_forte" if corpo >= .55 else "vela_fraca",
        "volatilidade_alta" if atr_rel >= 1.35 else "volatilidade_baixa" if atr_rel <= .70 else "volatilidade_normal",
        "movimento_alta" if movimento_3 >= .75 else "movimento_baixa" if movimento_3 <= -.75 else "movimento_neutro",
    ]
    return {
        "tipo": tipo, "corpo_pct": round(corpo * 100, 1),
        "pavio_sup_pct": round(pavio_sup * 100, 1), "pavio_inf_pct": round(pavio_inf * 100, 1),
        "range_atr": round(amplitude / atr_atual, 2), "movimento_3_atr": round(movimento_3, 2),
        "atr_relativo": round(atr_rel, 2),
        "volume_relativo": None if volume_rel is None else round(volume_rel, 2),
        "tags": tags, "noticia": noticia,
    }


def leitura_fluxo_sessao(df: pd.DataFrame, atr: pd.Series, ativo: str) -> dict:
    """Leitura de leilão usando o tick-volume disponível nos candles M15.

    O volume de cada candle é distribuído entre as faixas de preço atravessadas.
    POC/VAH/VAL são uma aproximação de pesquisa, não volume Forex centralizado.
    """
    indisponivel = {
        "disponivel": False, "estado": "FLUXO INDISPONÍVEL",
        "motivo": "São necessários candles e ATR válidos.",
    }
    if len(df) < 32 or atr.empty:
        return indisponivel
    try:
        atual = pd.Timestamp(df.index[-1])
        hora = int(atual.hour)
        if 0 <= hora < 7:
            sessao, inicio = "ÁSIA", atual.normalize()
        elif 7 <= hora < 13:
            sessao, inicio = "LONDRES", atual.normalize() + pd.Timedelta(hours=7)
        elif 13 <= hora < 16:
            sessao, inicio = "LONDRES + NOVA YORK", atual.normalize() + pd.Timedelta(hours=7)
        elif 16 <= hora < 22:
            sessao, inicio = "NOVA YORK", atual.normalize() + pd.Timedelta(hours=13)
        else:
            sessao, inicio = "TRANSIÇÃO / BAIXA LIQUIDEZ", atual.normalize()

        janela = df[df.index >= inicio].tail(96)
        if len(janela) < 4:
            janela = df.tail(32)
        colunas = janela[["Open", "High", "Low", "Close"]].apply(
            pd.to_numeric, errors="coerce"
        )
        valido = colunas.notna().all(axis=1)
        janela, colunas = janela.loc[valido], colunas.loc[valido]
        if len(janela) < 4:
            return indisponivel

        if "Volume" in janela:
            volumes = pd.to_numeric(janela["Volume"], errors="coerce").fillna(0.0).clip(lower=0.0)
        else:
            volumes = pd.Series(0.0, index=janela.index)
        fonte_volume = "tick-volume M15"
        if float(volumes.sum()) <= 0:
            volumes = pd.Series(1.0, index=janela.index)
            fonte_volume = "atividade uniforme (sem volume)"

        tipico = (colunas["High"] + colunas["Low"] + colunas["Close"]) / 3.0
        vwap = float((tipico * volumes).sum() / volumes.sum())
        minimo, maximo = float(colunas["Low"].min()), float(colunas["High"].max())
        if not np.isfinite(minimo + maximo) or maximo <= minimo:
            return indisponivel

        quantidade_faixas = 32
        bordas = np.linspace(minimo, maximo, quantidade_faixas + 1)
        perfil = np.zeros(quantidade_faixas, dtype=float)
        for (_, vela), volume in zip(colunas.iterrows(), volumes.to_numpy()):
            primeiro = int(np.clip(
                np.searchsorted(bordas, float(vela["Low"]), side="right") - 1,
                0, quantidade_faixas - 1,
            ))
            ultimo = int(np.clip(
                np.searchsorted(bordas, float(vela["High"]), side="left"),
                0, quantidade_faixas - 1,
            ))
            perfil[primeiro:ultimo + 1] += float(volume) / max(1, ultimo - primeiro + 1)
        centros = (bordas[:-1] + bordas[1:]) / 2.0
        poc = float(centros[int(np.argmax(perfil))])
        alvo_volume = float(perfil.sum()) * .70
        escolhidas, acumulado = [], 0.0
        for indice in np.argsort(perfil)[::-1]:
            escolhidas.append(int(indice))
            acumulado += float(perfil[indice])
            if acumulado >= alvo_volume:
                break
        val = float(bordas[min(escolhidas)])
        vah = float(bordas[max(escolhidas) + 1])

        vela = colunas.iloc[-1]
        fechamento = float(vela["Close"])
        anterior = float(colunas["Close"].iloc[-2])
        atr_atual = float(atr.reindex(df.index).iloc[-1])
        if not np.isfinite(atr_atual) or atr_atual <= 0:
            return indisponivel
        mediana_volume = float(volumes.iloc[:-1].tail(20).median())
        volume_rel = float(volumes.iloc[-1] / mediana_volume) if mediana_volume > 0 else 1.0
        movimento_atr = abs(fechamento - anterior) / atr_atual
        eficiencia = movimento_atr / max(.25, volume_rel)
        if eficiencia >= .85:
            eficiencia_estado = "MOVIMENTO EFICIENTE"
        elif eficiencia <= .30 and volume_rel >= 1.0:
            eficiencia_estado = "POSSÍVEL ABSORÇÃO"
        else:
            eficiencia_estado = "MOVIMENTO EQUILIBRADO"

        rejeita_vah = float(vela["High"]) >= vah and fechamento < vah
        rejeita_val = float(vela["Low"]) <= val and fechamento > val
        if rejeita_vah and not rejeita_val:
            estado, direcao = "REJEIÇÃO NA VAH", "sell"
        elif rejeita_val and not rejeita_vah:
            estado, direcao = "REJEIÇÃO NA VAL", "buy"
        elif fechamento > vah:
            estado, direcao = "ACEITAÇÃO ACIMA DA ÁREA DE VALOR", "buy"
        elif fechamento < val:
            estado, direcao = "ACEITAÇÃO ABAIXO DA ÁREA DE VALOR", "sell"
        else:
            estado, direcao = "ACEITAÇÃO DENTRO DA ÁREA DE VALOR", "neutro"

        m15 = _tendencia_ema(df["Close"])
        h1 = _tendencia_h1_a_partir_m15(df)
        tendencia_esperada = "alta" if direcao == "buy" else "baixa"
        if estado.startswith("REJEIÇÃO"):
            # Rejeição das bordas procura rotação de volta ao valor justo.
            lado_vwap = (fechamento <= vwap) if direcao == "buy" else (fechamento >= vwap)
        else:
            # Aceitação fora da área procura continuação para longe da VWAP.
            lado_vwap = (fechamento >= vwap) if direcao == "buy" else (fechamento <= vwap)
        tem_direcao = direcao in ("buy", "sell")
        pontos = 0
        pontos += 20 if tem_direcao else 0
        pontos += 20 if tem_direcao and m15 == tendencia_esperada else 0
        pontos += 20 if tem_direcao and h1 == tendencia_esperada else 0
        pontos += 15 if tem_direcao and lado_vwap else 0
        pontos += 15 if estado.startswith("REJEIÇÃO") else 0
        pontos += 10 if eficiencia >= .60 or volume_rel >= 1.20 else 0

        alvos = None
        rr = 0.0
        if estado.startswith("REJEIÇÃO"):
            margem = .25 * atr_atual
            if direcao == "buy":
                sl, tp1, tp2 = min(float(vela["Low"]), val) - margem, poc, vah
            else:
                sl, tp1, tp2 = max(float(vela["High"]), vah) + margem, poc, val
            risco = abs(fechamento - sl)
            recompensa = tp1 - fechamento if direcao == "buy" else fechamento - tp1
            rr = max(0.0, recompensa) / risco if risco > 0 else 0.0
            passo, unidade = unidade_movimento(ativo)
            alvos = {
                "entrada": round(fechamento, 6), "sl": round(sl, 6),
                "tp1": round(tp1, 6), "tp2": round(tp2, 6),
                "risco_pips": round(risco / passo, 1), "risco_unidade": unidade,
            }
        sinal_estudo = bool(
            estado.startswith("REJEIÇÃO") and pontos >= 70 and alvos and rr >= 1.2
        )
        checklist = [
            {"nome": "Rejeição em VAH/VAL", "ok": estado.startswith("REJEIÇÃO")},
            {"nome": "M15 alinhado", "ok": tem_direcao and m15 == tendencia_esperada},
            {"nome": "H1 alinhado", "ok": tem_direcao and h1 == tendencia_esperada},
            {"nome": "Preço do lado correto da VWAP", "ok": tem_direcao and lado_vwap},
            {"nome": "R:R até POC ≥ 1.2", "ok": bool(alvos and rr >= 1.2)},
        ]
        return {
            "disponivel": True, "sessao": sessao, "fonte_volume": fonte_volume,
            "estado": estado, "direcao": direcao, "m15": m15, "h1": h1,
            "vwap": round(vwap, 6), "poc": round(poc, 6),
            "vah": round(vah, 6), "val": round(val, 6),
            "volume_relativo": round(volume_rel, 2),
            "movimento_atr": round(movimento_atr, 2),
            "eficiencia": round(eficiencia, 2),
            "eficiencia_estado": eficiencia_estado,
            "qualidade": int(pontos), "rr": round(rr, 2),
            "sinal_estudo": sinal_estudo, "alvos": alvos,
            "checklist": checklist,
            "motivo": (
                "Confluência de rejeição, tendência e VWAP; registrar em sombra."
                if sinal_estudo else
                "Leitura de contexto; aguarde uma rejeição alinhada com R:R suficiente."
            ),
        }
    except (ValueError, TypeError, IndexError, KeyError):
        return indisponivel


def plano_orb_sessao(df: pd.DataFrame, atr: pd.Series, ativo: str) -> dict:
    """ORB Londres/NY com FVG de três candles, somente como estudo sombra.

    A fonte atual entrega candles M15. Portanto, esta é uma adaptação objetiva
    do modelo de abertura: primeira vela M15 da sessão, fechamento além da
    faixa e desequilíbrio M15. Não é apresentada como a versão M1 do NQ nem
    como estratégia validada para Forex.
    """
    base = {
        "disponivel": False, "sinal_estudo": False,
        "estado": "ORB INDISPONÍVEL", "motivo": "Aguardando sessão e candles.",
    }
    if CLASSE.get(ativo) == "crypto":
        return {
            **base,
            "motivo": "Cripto funciona 24/7; ORB Londres/NY não será inventado para esta classe.",
        }
    if len(df) < 40 or atr.empty:
        return base
    try:
        dados = df[["Open", "High", "Low", "Close"]].apply(
            pd.to_numeric, errors="coerce"
        ).dropna()
        if len(dados) < 40:
            return base
        agora = pd.Timestamp(dados.index[-1])
        inicio_dia = agora.normalize()
        hora = int(agora.hour)
        if 7 <= hora < 13:
            sessao, inicio = "LONDRES", inicio_dia + pd.Timedelta(hours=7)
        elif 13 <= hora < 22:
            sessao, inicio = "NOVA YORK", inicio_dia + pd.Timedelta(hours=13)
        else:
            return {
                **base,
                "estado": "FORA DA JANELA ORB",
                "motivo": "O estudo observa Londres 07:00–12:45 e Nova York 13:00–21:45 UTC.",
            }

        abertura = dados[(dados.index >= inicio) &
                         (dados.index < inicio + pd.Timedelta(minutes=15))]
        posteriores = dados[dados.index >= inicio + pd.Timedelta(minutes=15)]
        if abertura.empty or len(posteriores) < 3:
            return {
                **base, "disponivel": True, "sessao": sessao,
                "estado": "FORMANDO ORB",
                "motivo": "Aguardando três candles M15 depois da faixa de abertura.",
            }

        orh = float(abertura["High"].max())
        orl = float(abertura["Low"].min())
        trio = posteriores.tail(3)
        primeira, ultima = trio.iloc[0], trio.iloc[-1]
        fechamento = float(ultima["Close"])
        rompe_buy = fechamento > orh
        rompe_sell = fechamento < orl
        fvg_buy = float(ultima["Low"]) > float(primeira["High"])
        fvg_sell = float(ultima["High"]) < float(primeira["Low"])
        direcao = "buy" if rompe_buy else "sell" if rompe_sell else "neutro"
        tem_fvg = fvg_buy if direcao == "buy" else fvg_sell if direcao == "sell" else False

        h1 = _tendencia_h1_a_partir_m15(df)
        h4_fechamentos = df["Close"].resample("4h").last().dropna()
        h4 = _tendencia_ema(h4_fechamentos.iloc[:-1])
        esperada = "alta" if direcao == "buy" else "baixa"
        vies_ok = direcao in ("buy", "sell") and h1 == esperada and h4 == esperada
        atr_atual = float(atr.reindex(df.index).iloc[-1])
        if not np.isfinite(atr_atual) or atr_atual <= 0:
            return base

        zona_inf = zona_sup = None
        alvos = None
        rr = 0.0
        if tem_fvg:
            if direcao == "buy":
                zona_inf, zona_sup = float(primeira["High"]), float(ultima["Low"])
                sl = min(float(trio["Low"].min()), zona_inf) - .15 * atr_atual
            else:
                zona_inf, zona_sup = float(ultima["High"]), float(primeira["Low"])
                sl = max(float(trio["High"].max()), zona_sup) + .15 * atr_atual
            risco = abs(fechamento - sl)
            if risco > 0:
                sinal = 1.0 if direcao == "buy" else -1.0
                tp1, tp2 = fechamento + sinal * risco, fechamento + sinal * 2.0 * risco
                rr = 2.0
                passo, unidade = unidade_movimento(ativo)
                alvos = {
                    "entrada": round(fechamento, 6), "sl": round(sl, 6),
                    "tp1": round(tp1, 6), "tp2": round(tp2, 6),
                    "risco_pips": round(risco / passo, 1), "risco_unidade": unidade,
                }

        sinal_estudo = bool(direcao in ("buy", "sell") and tem_fvg and vies_ok and alvos)
        if sinal_estudo:
            estado = "ORB + FVG — SOMBRA"
            motivo = "Fechamento fora da abertura, FVG M15 e H1/H4 alinhados. Registrar em sombra."
        elif direcao == "neutro":
            estado, motivo = "DENTRO DA ORB", "Aguardar fechamento além da máxima ou mínima da abertura."
        elif not tem_fvg:
            estado, motivo = "ROMPIMENTO SEM FVG", "Rompimento ocorreu, mas falta desequilíbrio M15 de três candles."
        else:
            estado, motivo = "ROMPIMENTO CONTRA O VIÉS", "H1 e H4 não confirmam a direção; não registrar entrada."
        checklist = [
            {"nome": "Fechamento fora da ORB", "ok": direcao in ("buy", "sell")},
            {"nome": "FVG M15 de 3 candles", "ok": bool(tem_fvg)},
            {"nome": "H1 alinhado", "ok": h1 == esperada if direcao != "neutro" else False},
            {"nome": "H4 alinhado", "ok": h4 == esperada if direcao != "neutro" else False},
            {"nome": "Alvo 2R calculado", "ok": bool(alvos and rr >= 2.0)},
        ]
        return {
            "disponivel": True, "sessao": sessao, "estado": estado,
            "motivo": motivo, "direcao": direcao, "orh": round(orh, 6),
            "orl": round(orl, 6), "inicio": str(inicio), "h1": h1, "h4": h4,
            "fvg": bool(tem_fvg),
            "fvg_inf": round(zona_inf, 6) if zona_inf is not None else None,
            "fvg_sup": round(zona_sup, 6) if zona_sup is not None else None,
            "rr": rr, "alvos": alvos, "checklist": checklist,
            "sinal_estudo": sinal_estudo,
        }
    except (ValueError, TypeError, IndexError, KeyError):
        return base


def decisao_entrada(acumulacao: bool, rompeu_piso: bool, janela_validada: bool) -> dict:
    """Expõe as regras objetivas sem transformar contexto em sinal.

    O estado e a checklist são apresentados ao usuário e também gravados no
    histórico, para que fique claro por que um mesmo desenho foi só estudo ou
    uma entrada elegível.
    """
    sinal = bool(acumulacao and rompeu_piso)
    checklist = [
        {"nome": "Acumulação: amplitude das 10 velas comprimida", "ok": bool(acumulacao)},
        {"nome": "Fechamento abaixo do piso das 10 velas", "ok": bool(rompeu_piso)},
        {"nome": "Janela validada para o ativo", "ok": bool(janela_validada)},
    ]
    if sinal and janela_validada:
        return {
            "sinal": True, "entrada_valida": True, "estado_entrada": "ENTRADA VÁLIDA",
            "motivo_entrada": "BUY: falso rompimento de baixa após acumulação, na janela testada.",
            "checklist": checklist,
        }
    if sinal:
        return {
            "sinal": True, "entrada_valida": False, "estado_entrada": "ESTUDO — NÃO OPERAR",
            "motivo_entrada": "O falso rompimento apareceu fora da janela testada; registrar, não executar.",
            "checklist": checklist,
        }
    return {
        "sinal": False, "entrada_valida": False, "estado_entrada": "AGUARDAR",
        "motivo_entrada": "Ainda não há falso rompimento completo.", "checklist": checklist,
    }


class Estado:
    def __init__(self, pasta: Path, arquivo_aprendizado: Path | None = None,
                 banco_aprendizado: Path | None = None):
        self.pasta = pasta
        # A lista completa precisa existir antes da primeira varredura. A IQ é
        # consultada em série e uma passagem pode levar minutos; começar vazio
        # fazia os ativos do fim da fila desaparecerem do painel a cada restart.
        self.dados: dict[str, dict] = {
            ativo: self._ativo_aguardando(ativo) for ativo in ATIVOS
        }
        self.status = "iniciando"
        # Historico das ultimas 24h. Sem isso o painel so mostra o instante
        # atual, e quem nao estava olhando na hora do sinal nao ve nada.
        self._arquivo_aprendizado = (
            arquivo_aprendizado
            or Path(__file__).resolve().parent / "diario" / "monitor_mercado" / "sinais_aprendizado.json"
        )
        self._banco_aprendizado = (
            banco_aprendizado or self._arquivo_aprendizado.with_name("monitor_mercado.sqlite3")
        )
        self._store = MonitorEventStore(self._banco_aprendizado, self._arquivo_aprendizado)
        self._aprendizado = self._carregar_aprendizado()
        corte = time.time() - 24 * 3600
        self.historico: list[dict] = [
            item for item in self._aprendizado
            if self._timestamp_recente(item.get("quando"), corte)
        ][-60:]
        self._vistos: set[str] = {str(h.get("id")) for h in self._aprendizado if h.get("id")}
        self._lock = threading.Lock()
        self._garantir_arquivos_grafico()

    @staticmethod
    def _timestamp_recente(valor: object, corte: float) -> bool:
        try:
            return datetime.fromisoformat(str(valor)).timestamp() > corte
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _ativo_aguardando(ativo: str, motivo: str = "Aguardando dados da IQ Option.") -> dict:
        return {
            "classe": CLASSE[ativo], "disponivel": False,
            "estado_entrada": "CARREGANDO", "motivo_entrada": motivo,
            "sinal": False, "entrada_valida": False, "prox": 0,
            "regime": "indefinido", "preco": None,
        }

    def _garantir_arquivos_grafico(self) -> None:
        """Troca somente gráficos ausentes/corrompidos por JSON vazio válido."""
        vazio = {"candles": [], "sup": [], "inf": [], "estado": "aguardando_dados"}
        self.pasta.mkdir(parents=True, exist_ok=True)
        for ativo in ATIVOS:
            arq = self.pasta / f"mkt_{ativo}.json"
            valido = False
            try:
                atual = json.loads(arq.read_text(encoding="utf-8"))
                valido = isinstance(atual, dict) and isinstance(atual.get("candles"), list)
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
            if valido:
                continue
            tmp = arq.with_name(
                f".{arq.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp.write_text(json.dumps(vazio), encoding="utf-8")
            os.replace(tmp, arq)

    def _carregar_aprendizado(self) -> list[dict]:
        itens_banco = self._store.carregar()
        try:
            bruto = itens_banco or json.loads(self._arquivo_aprendizado.read_text(encoding="utf-8"))
            itens = [item for item in bruto if isinstance(item, dict)]
            # Mantém o histórico antigo compatível com os novos campos. Para
            # resultados já resolvidos, a conclusão pode ser derivada sem
            # reprocessar candles; a vela exata permanece desconhecida.
            for item in itens:
                simulacao = item.get("simulacao")
                if not isinstance(simulacao, dict):
                    simulacao = {"desfecho": "aguardando"}
                    item["simulacao"] = simulacao
                desfecho = simulacao.get("desfecho", "aguardando")
                if "bateu_tp1_sem_stop" not in simulacao:
                    simulacao["bateu_tp1_sem_stop"] = (
                        True if desfecho == "win_tp1"
                        else False if desfecho in ("loss_sl", "ambíguo_mesma_vela")
                        else None
                    )
                simulacao.setdefault(
                    "primeiro_toque",
                    "tp1" if desfecho == "win_tp1"
                    else "stop" if desfecho == "loss_sl"
                    else "tp1_e_stop_mesma_vela" if desfecho == "ambíguo_mesma_vela"
                    else None,
                )
                simulacao.setdefault("vela_desfecho", None)
            return itens
        except (OSError, json.JSONDecodeError):
            return []

    def _salvar_aprendizado(self) -> None:
        self._store.salvar(self._aprendizado)
        try:
            self._arquivo_aprendizado.parent.mkdir(parents=True, exist_ok=True)
            temporario = self._arquivo_aprendizado.with_suffix(".tmp")
            temporario.write_text(json.dumps(self._aprendizado[-1500:], ensure_ascii=False), encoding="utf-8")
            temporario.replace(self._arquivo_aprendizado)
        except OSError:
            pass

    @staticmethod
    def _comparaveis(item: dict, historico: list[dict], limite: int = 8) -> dict:
        """Recuperação local por tags do contexto, sem alegar causa de resultado."""
        alvo = item.get("dossie") or {}
        tags_alvo = set(alvo.get("tags") or [])
        if not tags_alvo:
            return {"amostra": 0, "wins": 0, "losses": 0, "winrate": None}
        pontuados = []
        for outro in historico:
            if (outro.get("id") == item.get("id")
                    or outro.get("classe") != item.get("classe")
                    or outro.get("tipo", "falso_rompimento") != item.get("tipo", "falso_rompimento")
                    or outro.get("direcao", "buy") != item.get("direcao", "buy")):
                continue
            desfecho = (outro.get("simulacao") or {}).get("desfecho")
            if desfecho not in ("win_tp1", "loss_sl"):
                continue
            aud = outro.get("dossie") or {}
            tags = set(aud.get("tags") or [])
            if not tags:
                continue
            score_tags = len(tags & tags_alvo) / max(1, len(tags | tags_alvo))
            distancia, usados = 0.0, 0
            for campo in ("corpo_pct", "range_atr", "movimento_3_atr", "atr_relativo"):
                a, b = alvo.get(campo), aud.get(campo)
                if a is None or b is None:
                    continue
                escala = 100.0 if campo == "corpo_pct" else 1.0
                distancia += min(1.0, abs(float(a) - float(b)) / escala)
                usados += 1
            score_num = 1 - distancia / usados if usados else 0.0
            pontuados.append((.65 * score_tags + .35 * score_num, desfecho))
        melhores = [resultado for _, resultado in sorted(pontuados, reverse=True)[:limite]]
        wins, losses = melhores.count("win_tp1"), melhores.count("loss_sl")
        total = wins + losses
        return {"amostra": total, "wins": wins, "losses": losses,
                "winrate": round(wins * 100 / total, 1) if total else None}

    def atualiza(self, ativo: str, info: dict) -> None:
        with self._lock:
            self.dados[ativo] = {"disponivel": True, **info}

    def registrar_sinal(self, ativo: str, vela: str, info: dict,
                        tipo: str = "falso_rompimento") -> None:
        """Salva sinais de estudo por tipo, sem misturar Fibo e falso rompimento."""
        chave = f"{tipo}:{ativo}:{vela}"
        with self._lock:
            if chave in self._vistos:
                return
            self._vistos.add(chave)
            item = {
                "schema_versao": SCHEMA_VERSAO, "origem": "monitor_mercado",
                "modo": "entrada_validada" if info.get("entrada_valida") else "estudo",
                "timeframe": TF,
                "id": chave, "tipo": tipo, "ativo": ativo, "classe": info.get("classe"), "vela": vela,
                "direcao": info.get("direcao", "buy"),
                "quando": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "preco": info.get("preco"), "regime": info.get("regime"),
                "hora": info.get("hora"), "janela_boa": info.get("janela_boa"),
                "entrada_valida": info.get("entrada_valida", False),
                "estado_entrada": info.get("estado_entrada"),
                "motivo_entrada": info.get("motivo_entrada"),
                "checklist": info.get("checklist", []),
                # Os alvos de estudo continuam no arquivo para medir o sinal
                # fora da janela, mas nunca aparecem como ordem sugerida.
                "alvos": info.get("alvos"),
                "alvos_estudo": info.get("alvos_estudo"),
                "dossie": info.get("dossie", {}),
                "fibo": info.get("fibo"),
                "fluxo": info.get("fluxo"),
                "orb": info.get("orb"),
                "resultado": None,
                "simulacao": {
                    "versao": SIMULADOR_VERSAO, "desfecho": "aguardando",
                    "bateu_tp1_sem_stop": None,
                    "primeiro_toque": None,
                    "vela_desfecho": None,
                },
            }
            item["comparaveis"] = self._comparaveis(item, self._aprendizado)
            self.historico.append(item)
            self._aprendizado.append(dict(item))
            corte = time.time() - 24 * 3600
            self.historico = [h for h in self.historico
                              if datetime.fromisoformat(h["quando"]).timestamp() > corte][-60:]
            self._salvar_aprendizado()

    def resolver(self, ativo: str, df: pd.DataFrame) -> None:
        """Registra reação e simulação TP1/SL; não é resultado de ordem real."""
        with self._lock:
            pend = []
            for h in self._aprendizado:
                if h.get("ativo") != ativo:
                    continue
                simulacao = h.get("simulacao") or {}
                desfecho = simulacao.get("desfecho", "aguardando")
                expiracao_prematura = (
                    desfecho == "expirado_6h"
                    and int(simulacao.get("velas") or 0) < 24
                )
                precisa_resultado_r = (
                    desfecho in ("win_tp1", "loss_sl", "expirado_6h")
                    and simulacao.get("resultado_r") is None
                )
                versao_antiga = int(simulacao.get("versao") or 0) < SIMULADOR_VERSAO
                if (desfecho in ("aguardando", "sem_dados")
                        or expiracao_prematura or precisa_resultado_r or versao_antiga):
                    pend.append(h)
        alterou = False
        for h in pend:
            try:
                ts = pd.Timestamp(h["vela"]) + pd.Timedelta(seconds=TF)
                if ts not in df.index:
                    continue
                lin = df.loc[ts]
                o, c = float(lin["Open"]), float(lin["Close"])
                reacao = "subiu" if c > o else ("caiu" if c < o else "igual")
                simulacao = self._resolver_tp_sl(h, df)
                with self._lock:
                    h["resultado"] = reacao
                    passo, unidade = unidade_movimento(ativo)
                    h["var_pips"] = round((c - o) / passo, 1)
                    h["var_unidade"] = unidade
                    h["simulacao"] = simulacao
                    for visivel in self.historico:
                        if visivel.get("id") == h.get("id") and visivel is not h:
                            visivel.update(h)
                            break
                    alterou = True
            except Exception:
                continue
        if alterou:
            self._salvar_aprendizado()

    @staticmethod
    def _resolver_tp_sl(h: dict, df: pd.DataFrame, horizonte_velas: int = 24) -> dict:
        alvo = h.get("alvos_estudo") or h.get("alvos") or {}
        if not alvo or not h.get("vela"):
            return {
                "versao": SIMULADOR_VERSAO, "desfecho": "sem_dados",
                "bateu_tp1_sem_stop": None,
                "primeiro_toque": None, "vela_desfecho": None,
            }
        inicio = pd.Timestamp(h["vela"])
        futuras = df[df.index > inicio].head(horizonte_velas)
        if futuras.empty:
            return {
                "versao": SIMULADOR_VERSAO, "desfecho": "aguardando",
                "bateu_tp1_sem_stop": None,
                "primeiro_toque": None, "vela_desfecho": None,
            }
        sl, tp = float(alvo["sl"]), float(alvo["tp1"])
        entrada = float(alvo.get("entrada", h.get("preco", 0.0)) or 0.0)
        venda = h.get("direcao") == "sell"
        modo_entrada = alvo.get("modo_entrada")
        ordem_pendente = modo_entrada in ("limite", "stop") or h.get("tipo") == "fibo_m15"
        preenchida = not ordem_pendente
        vela_preenchimento = None

        def resultado_r(saida: float) -> float | None:
            risco = abs(entrada - sl)
            if not entrada or risco <= 0:
                return None
            movimento = entrada - saida if venda else saida - entrada
            return round(movimento / risco, 3)

        for numero, (instante, vela) in enumerate(futuras.iterrows(), start=1):
            acabou_de_preencher = False
            if not preenchida:
                if modo_entrada == "stop":
                    tocou_entrada = (
                        float(vela["Low"]) <= entrada if venda
                        else float(vela["High"]) >= entrada
                    )
                else:
                    tocou_entrada = float(vela["Low"]) <= entrada <= float(vela["High"])
                if not tocou_entrada:
                    continue
                preenchida = True
                acabou_de_preencher = True
                vela_preenchimento = str(instante)
            tocou_sl = float(vela["High"]) >= sl if venda else float(vela["Low"]) <= sl
            tocou_tp = float(vela["Low"]) <= tp if venda else float(vela["High"]) >= tp
            if tocou_sl and tocou_tp:
                return {
                    "versao": SIMULADOR_VERSAO,
                    "desfecho": "ambíguo_mesma_vela", "velas": numero,
                    "bateu_tp1_sem_stop": False,
                    "primeiro_toque": "tp1_e_stop_mesma_vela",
                    "vela_desfecho": str(instante),
                    "preco_saida": None, "resultado_r": None,
                    "preenchida": preenchida,
                    "vela_preenchimento": vela_preenchimento,
                }
            if acabou_de_preencher and tocou_tp:
                return {
                    "versao": SIMULADOR_VERSAO,
                    "desfecho": "ambíguo_entrada_tp_mesma_vela", "velas": numero,
                    "bateu_tp1_sem_stop": None,
                    "primeiro_toque": "entrada_e_tp1_mesma_vela",
                    "vela_desfecho": str(instante),
                    "preco_saida": None, "resultado_r": None,
                    "preenchida": True,
                    "vela_preenchimento": vela_preenchimento,
                }
            if acabou_de_preencher and modo_entrada == "stop" and tocou_sl:
                return {
                    "versao": SIMULADOR_VERSAO,
                    "desfecho": "ambíguo_entrada_stop_mesma_vela", "velas": numero,
                    "bateu_tp1_sem_stop": None,
                    "primeiro_toque": "entrada_e_stop_mesma_vela",
                    "vela_desfecho": str(instante),
                    "preco_saida": None, "resultado_r": None,
                    "preenchida": True,
                    "vela_preenchimento": vela_preenchimento,
                }
            if tocou_tp:
                return {
                    "versao": SIMULADOR_VERSAO,
                    "desfecho": "win_tp1", "velas": numero,
                    "bateu_tp1_sem_stop": True, "primeiro_toque": "tp1",
                    "vela_desfecho": str(instante),
                    "preco_saida": tp, "resultado_r": resultado_r(tp),
                    "preenchida": preenchida,
                    "vela_preenchimento": vela_preenchimento,
                }
            if tocou_sl:
                return {
                    "versao": SIMULADOR_VERSAO,
                    "desfecho": "loss_sl", "velas": numero,
                    "bateu_tp1_sem_stop": False, "primeiro_toque": "stop",
                    "vela_desfecho": str(instante),
                    "preco_saida": sl, "resultado_r": -1.0,
                    "preenchida": preenchida,
                    "vela_preenchimento": vela_preenchimento,
                }
        if len(futuras) < horizonte_velas:
            return {
                "versao": SIMULADOR_VERSAO,
                "desfecho": "aguardando", "velas": len(futuras),
                "bateu_tp1_sem_stop": None, "primeiro_toque": None,
                "vela_desfecho": str(futuras.index[-1]),
                "preco_saida": None, "resultado_r": None,
                "preenchida": preenchida,
                "vela_preenchimento": vela_preenchimento,
            }
        if not preenchida:
            return {
                "versao": SIMULADOR_VERSAO,
                "desfecho": "nao_executada_6h", "velas": len(futuras),
                "bateu_tp1_sem_stop": None, "primeiro_toque": None,
                "vela_desfecho": str(futuras.index[-1]),
                "preco_saida": None, "resultado_r": None,
                "preenchida": False, "vela_preenchimento": None,
            }
        preco_saida = float(futuras.iloc[-1]["Close"])
        return {
            "versao": SIMULADOR_VERSAO,
            "desfecho": "expirado_6h", "velas": len(futuras),
            "bateu_tp1_sem_stop": None, "primeiro_toque": None,
            "vela_desfecho": str(futuras.index[-1]),
            "preco_saida": preco_saida, "resultado_r": resultado_r(preco_saida),
            "preenchida": preenchida,
            "vela_preenchimento": vela_preenchimento,
        }

    @staticmethod
    def _resumo_simulacoes(itens: list[dict]) -> dict:
        desfechos = [str((h.get("simulacao") or {}).get("desfecho", "aguardando")) for h in itens]
        wins, losses = desfechos.count("win_tp1"), desfechos.count("loss_sl")
        resolvidos = wins + losses
        resultados_r = [
            float((h.get("simulacao") or {}).get("resultado_r"))
            for h in itens
            if (h.get("simulacao") or {}).get("resultado_r") is not None
        ]
        ic = wilson_ci(wins, resolvidos)
        # Maturidade segue o mesmo critério do plano: nunca promover por
        # amostra pequena. Menos de 30 resolvidos não sustenta nenhuma conclusão.
        if resolvidos <= 0:
            maturidade = "INSUFICIENTE"
        elif resolvidos < 30:
            maturidade = "INSUFICIENTE"
        elif resolvidos < 100:
            maturidade = "OBSERVAR"
        elif resolvidos < 300:
            maturidade = "CANDIDATA"
        else:
            maturidade = "APROVADA"
        return {
            "sinais": len(itens), "wins": wins, "losses": losses,
            "expirados": desfechos.count("expirado_6h"),
            "pendentes": desfechos.count("aguardando"),
            "ambiguos": sum(d.startswith("ambíguo") for d in desfechos),
            "nao_executadas": desfechos.count("nao_executada_6h"),
            "winrate": round(wins * 100 / resolvidos, 1) if resolvidos else None,
            "ic_95": list(ic) if ic else None,
            "amostra_suficiente": resolvidos >= 30,
            "maturidade": maturidade,
            "avaliados_r": len(resultados_r),
            "saldo_r": round(sum(resultados_r), 3) if resultados_r else None,
            "media_r": round(sum(resultados_r) / len(resultados_r), 3) if resultados_r else None,
        }

    def _amostra_entrada_validada(self) -> dict:
        """Resumo pequeno e honesto da amostra elegível do monitor."""
        validos = [
            h for h in self._aprendizado
            if (h.get("tipo", "falso_rompimento") == "falso_rompimento"
                and h.get("entrada_valida", h.get("janela_boa", False)))
        ]
        return self._resumo_simulacoes(validos)

    def _amostra_fibo(self) -> dict:
        """Amostra separada: Fibo não pode contaminar o sinal já validado."""
        itens = [h for h in self._aprendizado if h.get("tipo") == "fibo_m15"]
        return self._resumo_simulacoes(itens)

    def _amostra_fluxo(self) -> dict:
        """Amostra isolada da leitura de fluxo; nunca vira entrada validada."""
        itens = [h for h in self._aprendizado if h.get("tipo") == "fluxo_m15"]
        return self._resumo_simulacoes(itens)

    def _amostra_orb(self) -> dict:
        """Amostra isolada do ORB/FVG adaptado ao M15."""
        itens = [h for h in self._aprendizado if h.get("tipo") == "orb_fvg_m15"]
        return self._resumo_simulacoes(itens)

    def _amostra_liquidez(self) -> dict:
        """Amostra isolada da varredura v2; permanece sempre em sombra."""
        itens = [h for h in self._aprendizado if h.get("tipo") == "liquidity_sweep_v2"]
        return self._resumo_simulacoes(itens)

    def salvar(self) -> None:
        with self._lock:
            historico_saida = []
            for entrada in reversed(self.historico):
                item = dict(entrada)
                # A amostra cresce quando novos sinais são resolvidos; por isso
                # ela é calculada ao publicar, não fica congelada no momento
                # em que o alerta nasceu.
                item["comparaveis"] = self._comparaveis(item, self._aprendizado)
                historico_saida.append(item)
            payload = {"ts": int(time.time()), "status": self.status,
                       "schemaVersao": SCHEMA_VERSAO,
                       "saudeDados": self._store.resumo(),
                       "historico": historico_saida,
                       "ativos": dict(self.dados),
                       "amostraEntrada": self._amostra_entrada_validada(),
                       "amostraFibo": self._amostra_fibo(),
                       "amostraFluxo": self._amostra_fluxo(),
                       "amostraOrb": self._amostra_orb(),
                       "amostraLiquidez": self._amostra_liquidez()}
        try:
            arq = self.pasta / "mercado.json"
            tmp = arq.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                           encoding="utf-8")
            os.replace(tmp, arq)
        except Exception:
            pass

    def salvar_candles(self, ativo: str, df: pd.DataFrame, r: pd.DataFrame) -> None:
        u = df.tail(VELAS_GRAFICO)
        ru = r.tail(VELAS_GRAFICO)
        minimo = float(u["Low"].min())
        maximo = float(u["High"].max())
        amplitude = maximo - minimo
        centro = (maximo + minimo) / 2
        # Uma regressão instável pode projetar bandas milhares de pontos para
        # fora do preço (ocorreu no XAUUSD) e achatar todos os candles. A banda
        # é só auxílio visual: valores muito além da janela visível viram
        # lacuna, enquanto os candles reais permanecem intactos.
        margem = max(amplitude * 1.5, abs(centro) * 0.001, 1e-9)
        limite_inf = minimo - margem
        limite_sup = maximo + margem

        def banda_visivel(valor) -> float | None:
            if not np.isfinite(valor):
                return None
            numero = float(valor)
            return round(numero, 6) if limite_inf <= numero <= limite_sup else None

        out = {
            "candles": [{"t": int(ts.timestamp()), "o": float(x["Open"]),
                         "h": float(x["High"]), "l": float(x["Low"]),
                         "c": float(x["Close"])} for ts, x in u.iterrows()],
            "sup": [banda_visivel(v) for v in ru["banda_sup"]],
            "inf": [banda_visivel(v) for v in ru["banda_inf"]],
        }
        try:
            arq = self.pasta / f"mkt_{ativo}.json"
            tmp = arq.with_suffix(".tmp")
            tmp.write_text(json.dumps(out), encoding="utf-8")
            os.replace(tmp, arq)
        except Exception:
            pass


def loop(api, cfg, estado: Estado, calendario: CalendarioEconomico | None = None) -> None:
    hist: dict[str, pd.DataFrame] = {}
    for a in ATIVOS:
        df = backtest.carregar_cache(cfg, a)
        if df is None or len(df) < 600:
            try:
                # Ativos adicionados recentemente não possuem cache. Antes eles
                # eram descartados para sempre e nunca chegavam a ser baixados.
                print(f"  {a}: preparando histórico inicial...")
                df = backtest.baixar_historico(api, cfg, a, 600)
            except Exception as erro:
                motivo = f"Sem candles normais disponíveis: {erro}"
                estado.atualiza(a, Estado._ativo_aguardando(a, motivo))
                estado.dados[a]["disponivel"] = False
                estado.salvar()
                print(f"  {a}: indisponível — {erro}")
                continue
        if df is not None and len(df) >= 600:
            hist[a] = df
            print(f"  {a}: {len(df)} velas")
        else:
            print(f"  {a}: cache insuficiente — fora")
    if not hist:
        print("Sem historico."); return

    while True:
        if calendario is not None:
            calendario.atualizar()
        for a, base in list(hist.items()):
            try:
                novo = backtest.baixar_historico(api, cfg, a, 60)
                if novo is None or novo.empty:
                    continue
                novo_grafico = novo.copy()
                novo = novo.iloc[:-1]
                if novo.empty:
                    continue
                df = pd.concat([base[~base.index.isin(novo.index)], novo]).sort_index()
                hist[a] = df

                # PERFORMANCE: regressao usa rolling().apply() com funcao
                # Python — sobre as 26k velas completas eram ~1.3M chamadas por
                # ciclo (17 ativos), e o loop nunca fechava uma passada. O
                # supervisor pegou isso como "travado". So precisamos do valor
                # atual e das ultimas 60 velas para o grafico, entao a janela
                # de 300 basta (regressao usa 40).
                dfr = df.tail(300)
                r = regressao(dfr)
                cls = classificar(r)
                lo, _ = M.s_falso_rompimento(df, JAN)
                lo = lo.fillna(False)
                atr = M.atr(df)

                # ---- proximidade do sinal ----
                # O painel ficava vazio 99% do tempo porque sinal e raro
                # (~26/dia em 17 ativos, concentrados fora de Londres/NY).
                # Aqui expomos as DUAS condicoes do falso rompimento separadas,
                # para dar pra ver o mercado se aproximando em vez de so o
                # instante do disparo. Nao e previsao: e o estado atual das
                # condicoes que o proprio sinal usa.
                amp = df["High"] - df["Low"]
                med_amp = amp.shift(1).rolling(JAN).mean()
                limiar = med_amp.expanding(min_periods=500).quantile(0.25).shift(1)
                rlo_s = df["Low"].shift(1).rolling(JAN).min()
                acum = bool(med_amp.iloc[-1] <= limiar.iloc[-1]) \
                    if np.isfinite(med_amp.iloc[-1]) and np.isfinite(limiar.iloc[-1]) else False
                # quao apertado esta vs o limiar (1.0 = exatamente no limiar)
                aperto = (float(med_amp.iloc[-1]) / float(limiar.iloc[-1])
                          if np.isfinite(limiar.iloc[-1]) and limiar.iloc[-1] > 0 else None)
                # distancia ate romper o piso, em ATR (negativo = ja rompeu)
                av0 = float(atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) else None
                dist = ((float(df["Close"].iloc[-1]) - float(rlo_s.iloc[-1])) / av0
                        if av0 and av0 > 0 and np.isfinite(rlo_s.iloc[-1]) else None)

                ult = df.iloc[-1]
                preco = float(ult["Close"])
                av = float(atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) else None
                agora = datetime.now(timezone.utc)
                noticia = contexto_noticia(calendario, a, agora)
                fibo = plano_fibo(df, atr, a, noticia)
                fluxo = leitura_fluxo_sessao(df, atr, a)
                orb = plano_orb_sessao(df, atr, a)
                liquidez = plano_varredura_liquidez(df, atr, a, noticia)

                # Alvos medidos em 01/09/2026 (janela 21-22h UTC, SL=1 ATR,
                # holdout confirma em todos os niveis). O EV cresce com o alvo
                # — "deixar correr" e melhor — mas o WR despenca junto:
                #   TP 2.5 ATR: WR 40.4%  EV +0.4126R  <- equilibrio pratico
                #   TP 5.0 ATR: WR 24.6%  EV +0.4753R  <- maior EV, 3 perdas
                #                                          seguidas viram rotina
                # Alvos ESTRUTURAIS ficaram todos piores: meio do range +0.2932R,
                # topo do range +0.3251R, maxima da vela de rompimento +0.2553R.
                # Por isso o alvo aqui e multiplo de ATR, nao nivel de preco.
                alvos_estudo = None
                if av and av > 0:
                    ent = preco
                    passo, unidade = unidade_movimento(a)
                    alvos_estudo = {
                        "entrada": round(ent, 6),
                        "sl": round(ent - 1.0 * av, 6),
                        "tp1": round(ent + 2.5 * av, 6),
                        "tp2": round(ent + 5.0 * av, 6),
                        "risco_pips": round(av / passo, 1),
                        "risco_unidade": unidade,
                    }
                bi = float(r["banda_inf"].iloc[-1]) if np.isfinite(r["banda_inf"].iloc[-1]) else None
                bs = float(r["banda_sup"].iloc[-1]) if np.isfinite(r["banda_sup"].iloc[-1]) else None
                pos_canal = None
                if bi is not None and bs is not None and bs > bi:
                    pos_canal = round((preco - bi) / (bs - bi) * 100, 1)
                rompeu_piso = bool(
                    np.isfinite(rlo_s.iloc[-1]) and float(df["Close"].iloc[-1]) < float(rlo_s.iloc[-1])
                )
                # Forex só tem janela comprovada em 21-22 UTC; cripto foi
                # estudado 24h. Ouro fica em estudo até ter amostra própria.
                janela_validada = janela_validada_para(CLASSE[a], int(df.index[-1].hour))
                leitura_entrada = decisao_entrada(acum, rompeu_piso, janela_validada)

                estado.atualiza(a, {
                    "classe": CLASSE[a],
                    "preco": round(preco, 6),
                    "regime": str(cls.iloc[-1]),
                    "slope_atr": round(float(r["slope_atr"].iloc[-1]), 4)
                                 if np.isfinite(r["slope_atr"].iloc[-1]) else None,
                    "r2": round(float(r["r2"].iloc[-1]), 3)
                          if np.isfinite(r["r2"].iloc[-1]) else None,
                    "banda_inf": round(bi, 6) if bi else None,
                    "banda_sup": round(bs, 6) if bs else None,
                    "pos_canal": pos_canal,
                    "dia": tendencia_dia(df),
                    "atr_pips": round(float(atr.iloc[-1]) / unidade_movimento(a)[0], 1)
                                if np.isfinite(atr.iloc[-1]) else None,
                    "unidade_movimento": unidade_movimento(a)[1],
                    **leitura_entrada,
                    "acumulacao": acum,
                    "rompeu_piso": rompeu_piso,
                    "piso_acumulacao": round(float(rlo_s.iloc[-1]), 6)
                                       if np.isfinite(rlo_s.iloc[-1]) else None,
                    "aperto": round(aperto, 2) if aperto is not None else None,
                    "dist_atr": round(dist, 2) if dist is not None else None,
                    # 0-100: quao perto de disparar. So para ordenar a tabela.
                    "prox": (0 if not acum or dist is None
                             else int(max(0, min(100, 100 - dist * 50)))),
                    # Só a entrada validada recebe preços operacionais. O
                    # plano de estudo é separado e não deve ser executado.
                    "alvos": alvos_estudo if leitura_entrada["entrada_valida"] else None,
                    "alvos_estudo": alvos_estudo,
                    # O edge de forex so aparece em 21-22h UTC (+0.41R); nas
                    # demais horas o EV e negativo em TODOS os alvos testados.
                    # Em crypto essa janela nao significa nada (opera 24/7) —
                    # o efeito e de sessao de forex.
                    "janela_boa": janela_validada,
                    "hora": int(df.index[-1].hour),
                    "vela": str(df.index[-1]),
                    "dossie": dossie_candle(df, atr, a, noticia),
                    "fibo": fibo,
                    "fluxo": fluxo,
                    "orb": orb,
                    "liquidez": liquidez,
                })
                # A análise continua usando somente candles fechados, mas o
                # desenho recebe também a vela atual para se mover como na IQ.
                atual = novo_grafico.tail(1)
                df_grafico = pd.concat([
                    df[~df.index.isin(atual.index)], atual
                ]).sort_index().tail(300)
                estado.salvar_candles(a, df_grafico, regressao(df_grafico))
                estado.resolver(a, df)
                # Heartbeat POR ATIVO, nao so no fim da passada. Antes, um
                # unico ativo lento (download travado) fazia o supervisor achar
                # que o monitor inteiro morreu, porque o JSON so era escrito
                # depois de percorrer os 17.
                estado.status = f"lendo {a} — {datetime.now(timezone.utc):%H:%M:%S} UTC"
                estado.salvar()
                if leitura_entrada["sinal"]:
                    estado.registrar_sinal(a, str(df.index[-1]), estado.dados[a])
                    print(f"[SINAL] {a} LONG @ {df.index[-1]} preco={preco} "
                          f"regime={cls.iloc[-1]}")
                # Fibo é um segundo estudo, independente do falso rompimento:
                # registra somente quando a retração e a rejeição se completam.
                # Não altera ``entrada_valida`` nem envia qualquer posição.
                if fibo.get("qualificada"):
                    estudo_fibo = {
                        **estado.dados[a],
                        "sinal": True, "direcao": fibo["direcao"],
                        "entrada_valida": False,
                        "estado_entrada": "FIBO — ESTUDO",
                        "motivo_entrada": fibo["motivo"],
                        "checklist": fibo["checklist"],
                        "alvos": None, "alvos_estudo": fibo["alvos"],
                    }
                    estado.registrar_sinal(a, str(df.index[-1]), estudo_fibo, tipo="fibo_m15")
                    print(f"[FIBO — ESTUDO] {a} {fibo['direcao'].upper()} @ {df.index[-1]} "
                          f"RR={fibo['rr']}")
                if fluxo.get("sinal_estudo"):
                    estudo_fluxo = {
                        **estado.dados[a],
                        "sinal": True, "direcao": fluxo["direcao"],
                        "entrada_valida": False,
                        "estado_entrada": "FLUXO — ESTUDO",
                        "motivo_entrada": fluxo["motivo"],
                        "checklist": fluxo["checklist"],
                        "alvos": None, "alvos_estudo": fluxo["alvos"],
                    }
                    estado.registrar_sinal(
                        a, str(df.index[-1]), estudo_fluxo, tipo="fluxo_m15"
                    )
                    print(f"[FLUXO — ESTUDO] {a} {fluxo['direcao'].upper()} @ {df.index[-1]} "
                          f"qualidade={fluxo['qualidade']} RR={fluxo['rr']}")
                if orb.get("sinal_estudo"):
                    estudo_orb = {
                        **estado.dados[a],
                        "sinal": True, "direcao": orb["direcao"],
                        "entrada_valida": False,
                        "estado_entrada": "ORB/FVG — ESTUDO",
                        "motivo_entrada": orb["motivo"],
                        "checklist": orb["checklist"],
                        "alvos": None, "alvos_estudo": orb["alvos"],
                    }
                    estado.registrar_sinal(
                        a, str(df.index[-1]), estudo_orb, tipo="orb_fvg_m15"
                    )
                    print(f"[ORB/FVG — ESTUDO] {a} {orb['direcao'].upper()} "
                          f"@ {df.index[-1]} sessão={orb['sessao']} RR={orb['rr']}")
                if liquidez.get("sinal_estudo"):
                    estudo_liquidez = {
                        **estado.dados[a],
                        "sinal": True, "direcao": "buy",
                        "entrada_valida": False,
                        "estado_entrada": "LIQUIDEZ V2 — ESTUDO",
                        "motivo_entrada": liquidez["motivo"],
                        "checklist": liquidez["checklist"],
                        "alvos": None, "alvos_estudo": liquidez["alvos"],
                    }
                    estado.registrar_sinal(
                        a, str(df.index[-1]), estudo_liquidez,
                        tipo="liquidity_sweep_v2",
                    )
                    print(f"[LIQUIDEZ V2 — ESTUDO] {a} BUY @ {df.index[-1]} "
                          f"RR={liquidez['rr']}")
            except Exception as e:
                print(f"[{a}] {e!r}")
        estado.status = f"OK — {datetime.now(timezone.utc):%H:%M:%S} UTC"
        estado.salvar()
        time.sleep(INTERVALO_S)


_HTML = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Monitor Mercado</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script src="marcacoes.js"></script><style>
*{box-sizing:border-box}
body{font-family:ui-monospace,monospace;background:#0b1220;color:#e2e8f0;margin:0;padding:.8rem}
h1{color:#38bdf8;font-size:1.05rem;margin:0 0 .2rem}
.sub{color:#64748b;font-size:.72rem;margin-bottom:.6rem}
.aviso{background:#3b1d0e;border-left:3px solid #f59e0b;padding:.5rem .7rem;border-radius:.3rem;
  font-size:.72rem;color:#fcd34d;margin:.5rem 0}
.sec{color:#94a3b8;font-size:.72rem;letter-spacing:.08em;margin:.9rem 0 .35rem;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:.75rem}
th,td{text-align:left;padding:.28rem .45rem;border-bottom:1px solid #1e293b;white-space:nowrap}
th{color:#64748b;font-weight:600;font-size:.68rem}
tr.sig{background:#052e1a}
tr.study{background:#2b210b}
tr:hover{background:#16203450;cursor:pointer}
.lat{color:#94a3b8}.ca{color:#4ade80}.cb{color:#f87171}.tend{color:#fbbf24}.ind{color:#475569}
.up{color:#4ade80}.dn{color:#f87171}
.quente{color:#f97316;font-weight:700}.morno{color:#fbbf24}.frio{color:#64748b}
.badge{display:inline-block;padding:0 .35rem;border-radius:.2rem;font-size:.65rem;font-weight:700}
.b-sig{background:#22c55e;color:#052e1a}
.bar{display:inline-block;width:60px;height:7px;background:#1e293b;border-radius:3px;
  position:relative;vertical-align:middle}
.bar>i{position:absolute;top:-2px;width:3px;height:11px;background:#38bdf8;border-radius:1px}
#cv{width:100%;height:58vh;min-height:340px;background:#0d1526;border-radius:.4rem;margin:.4rem 0}
#cv-titulo{color:#94a3b8;font-size:.7rem;margin:.3rem 0 0}
#marc-barra{display:flex;gap:.3rem;align-items:center;margin:.35rem 0}
.btn-marc{background:#16233a;border:1px solid #1e293b;color:#94a3b8;border-radius:.25rem;font-size:.68rem;padding:.15rem .5rem;cursor:pointer}
.btn-marc:hover{color:#e2e8f0}
.btn-marc.on{background:#38bdf8;border-color:#38bdf8;color:#08131f}
#marc-dica{font-size:.66rem;color:#fbbf24}
#cv-aviso{display:none;color:#94a3b8;font-size:.72rem;padding:.6rem;background:#0d1526;border-radius:.4rem;margin:.4rem 0}
.tabs{display:flex;gap:.3rem;margin:.3rem 0}
.tab{background:#1e293b;border:1px solid #334155;border-radius:.3rem;padding:.15rem .5rem;
  cursor:pointer;font-size:.7rem;color:#94a3b8}
.tab.on{background:#0f4c75;border-color:#38bdf8;color:#fff}
.card{background:#151f33;border-radius:.4rem;padding:.7rem;margin:.4rem 0;border-left:4px solid #334155}
.card.okjan{border-left-color:#22c55e}
.card.nojan{border-left-color:#64748b;opacity:.75}
.checklist{display:flex;flex-wrap:wrap;gap:.3rem;margin:.45rem 0}
.check{font-size:.64rem;border:1px solid #334155;border-radius:.25rem;padding:.18rem .35rem;color:#94a3b8}
.check.ok{border-color:#166534;color:#86efac;background:#052e1a}.check.no{border-color:#854d0e;color:#fcd34d;background:#2b210b}
.estado{font-size:.68rem;font-weight:800;padding:.18rem .4rem;border-radius:.25rem}.estado.go{color:#052e1a;background:#22c55e}.estado.study{color:#fcd34d;background:#3b2a08}.estado.wait{color:#94a3b8;background:#26364e}
.amostra{font-size:.7rem;color:#cbd5e1;background:#0c2a3e;border-left:3px solid #38bdf8;padding:.35rem .5rem;border-radius:.2rem;margin:.45rem 0}
.alvos{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:.4rem;margin-top:.5rem}
.alvos label{display:block;font-size:.62rem;color:#94a3b8}
.alvos span{font-size:.92rem;font-weight:700}
.b-ok{background:#22c55e;color:#052e1a}
.b-no{background:#334155;color:#94a3b8}
.nota{margin-top:.5rem;font-size:.66rem;color:#7dd3fc;background:#0c2a3e;padding:.35rem .5rem;border-radius:.25rem}
.dossie{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:.45rem;margin-top:.5rem}
.dossie .item{background:#0d1728;border:1px solid #26364e;border-radius:.3rem;padding:.4rem}
.dossie label{display:block;color:#94a3b8;font-size:.62rem;margin-bottom:.12rem}
.dossie span{font-size:.76rem;font-weight:700}
.dossie-nota{font-size:.68rem;line-height:1.45;color:#cbd5e1;margin-top:.5rem;background:#0c2a3e;padding:.4rem .5rem;border-radius:.25rem}
.dossie-aviso{color:#fbbf24}.dossie-win{color:#4ade80}.dossie-loss{color:#f87171}
.fibo-zona{color:#c4b5fd;border-color:#6d28d9;background:#251145}
.fibo-card{border-left-color:#8b5cf6}.fibo-card.ok{border-left-color:#22c55e}
.orb-card{border-left-color:#f59e0b}.orb-card.ok{border-left-color:#22c55e}
.nav{display:flex;gap:.35rem;position:sticky;top:0;z-index:5;background:#0b1220;padding:.35rem 0}
.nav button,.filtro{background:#17233a;color:#94a3b8;border:1px solid #334155;border-radius:.3rem;padding:.3rem .55rem;font:inherit;font-size:.7rem}
.nav button.on{color:#fff;border-color:#38bdf8;background:#0f4c75}.view{display:none}.view.on{display:block}
.filtros{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;margin:.4rem 0}
.saude-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.45rem}
.saude-item{background:#101a2c;border:1px solid #26364e;border-radius:.35rem;padding:.55rem}
.saude-item label{display:block;color:#64748b;font-size:.64rem}.saude-item b{font-size:1rem;color:#7dd3fc}
.decisao-hero{background:#0d1a2e;border:2px solid #334155;border-radius:.5rem;padding:.85rem 1rem;margin:.4rem 0}
.decisao-hero.entrar{border-color:#22c55e;background:#041a0c}
.decisao-hero.estudo{border-color:#f59e0b;background:#1a1000}
.decisao-hero-topo{display:flex;justify-content:space-between;align-items:flex-start;gap:.5rem;margin-bottom:.55rem}
.decisao-hero-ativo{font-size:1.3rem;font-weight:800;letter-spacing:.02em}
.decisao-hero-dir{font-size:1.1rem;font-weight:700}
.decisao-hero.entrar .decisao-hero-dir{color:#22c55e}
.decisao-hero.estudo .decisao-hero-dir{color:#f59e0b}
.decisao-hero-motivo{font-size:.72rem;color:#94a3b8;margin:.25rem 0 .5rem;line-height:1.4}
.decisao-hero-alvos{display:grid;grid-template-columns:repeat(3,1fr);gap:.4rem;margin-top:.5rem}
.decisao-hero-chip{background:#0b1220;border:1px solid #26364e;border-radius:.3rem;padding:.4rem .5rem;text-align:center}
.decisao-hero-chip label{display:block;font-size:.6rem;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:.2rem}
.decisao-hero-chip .val{font-size:.88rem;font-weight:700;font-family:ui-monospace,monospace}
.decisao-hero-chip .val.entrada{color:#38bdf8}
.decisao-hero-chip .val.sl{color:#ef4444}
.decisao-hero-chip .val.tp{color:#22c55e}
.ic-badge{display:inline-block;font-size:.62rem;padding:.12rem .35rem;border-radius:.2rem;margin-left:.35rem;font-weight:700}
.ic-badge.insuf{background:#1e293b;color:#64748b}
.ic-badge.ok{background:#0c2a3e;color:#7dd3fc}
.maturidade-insuf{color:#64748b;font-size:.68rem;font-weight:700}
.maturidade-obs{color:#f59e0b;font-size:.68rem;font-weight:700}
.maturidade-cand{color:#38bdf8;font-size:.68rem;font-weight:700}
.maturidade-aprov{color:#22c55e;font-size:.68rem;font-weight:700}
.hist-alvos{font-size:.64rem;color:#64748b;font-family:ui-monospace,monospace;white-space:nowrap}
</style></head><body>
<h1>Monitor Mercado — Forex + Cripto + Ouro</h1>
<div class="sub"><span id="status">carregando...</span> &nbsp;·&nbsp; <span id="relogio"></span></div>

<div class="aviso">
<b>Sinal</b> = falso rompimento LONG, único validado em Forex (55.44%) e Cripto (57.66%). Ouro fica em estudo até ter amostra própria.<br>
<b>Regime, canal e tendencia do dia</b> = contexto para leitura, <b>nao sao sinal</b>.
Testado em 01/09/2026: operar a favor do canal (51.14%) rende o mesmo que contra (51.11%)
— a classificacao nao tem conteudo direcional.
</div>

<div class="nav" id="nav">
  <button data-view="agora" onclick="mudarVisao('agora')">Agora</button>
  <button data-view="grafico" onclick="mudarVisao('grafico')">Gráfico</button>
  <button data-view="entradas" onclick="mudarVisao('entradas')">Entradas</button>
  <button data-view="estudos" onclick="mudarVisao('estudos')">Estudos</button>
  <button data-view="dados" onclick="mudarVisao('dados')">Dados</button>
</div>

<section class="view" data-view="grafico">

<div class="sec">GRAFICO</div>
<div class="tabs" id="tabs"></div>
<div id="cv-titulo"></div>
<div id="marc-barra">
  <button class="btn-marc" id="btn-marc-fibo" onclick="marcarFibo()" title="Clique na origem e depois no extremo">✎ Fibo</button>
  <button class="btn-marc" id="btn-marc-linha" onclick="marcarLinha()" title="Um clique no preço">✎ Linha</button>
  <button class="btn-marc" id="btn-marc-limpar" onclick="limparMarcacoes()" title="Apagar marcações deste ativo">✕</button>
  <span id="marc-dica"></span>
</div>
<div id="cv-aviso"></div>
<div id="cv"></div>
</section>

<section class="view" data-view="estudos">
<div class="amostra" id="amostra">Amostra validada: carregando…</div>

<div class="sec">LEITURA DE FLUXO E SESSÃO — ESTUDO</div>
<div id="fluxo"><span class="empty">Selecione um ativo.</span></div>

<div class="sec">ORB LONDRES/NY + FVG M15 — ESTUDO SOMBRA</div>
<div id="orb"><span class="empty">Selecione um ativo.</span></div>

<div class="sec">VARREDURA DE LIQUIDEZ V2 — ESTUDO SOMBRA</div>
<div id="liquidez"><span class="empty">Selecione um ativo.</span></div>

<div class="sec">PLANO FIBO M15 — ESTUDO</div>
<div id="fibo"><span class="empty">Selecione um ativo.</span></div>
</section>

<section class="view" data-view="agora">

<div id="decisao-principal"></div>

<div class="sec">SINAIS ATIVOS</div>
<div id="sinais"></div>

<div class="sec">CONTEXTO — ordenado por proximidade do sinal</div>
<div id="tab"></div>
</section>

<section class="view" data-view="entradas">
<div class="filtros">
  <select class="filtro" id="filtro-tipo" onchange="render()"><option value="">Todos os tipos</option></select>
  <select class="filtro" id="filtro-resultado" onchange="render()"><option value="">Todos os resultados</option><option value="aguardando">Pendentes</option><option value="win_tp1">TP1</option><option value="loss_sl">Stop</option><option value="ambíguo">Ambíguos</option></select>
  <label><input type="checkbox" id="filtro-ativo" onchange="render()"> só ativo selecionado</label>
</div>

<div class="sec">SINAIS DAS ULTIMAS 24h</div>
<div id="hist"></div>

<div class="sec">DOSSIE DO SINAL</div>
<div id="dossie"><span class="empty">Clique num sinal do histórico quando houver um.</span></div>
</section>

<section class="view" data-view="dados">
<div class="sec">SAÚDE DOS DADOS</div>
<div class="card"><div class="saude-grid" id="saude"></div><div class="nota">Use EXPORTAR_DADOS_PAINEIS.bat para gerar a planilha completa.</div></div>
</section>

<script>
let D={}, sel=null, dossieSel=null;
let visao=localStorage.getItem('monitorMercadoVisao')||'agora';
let ultimaChaveAlerta='';
function mudarVisao(nome){
  visao=nome; localStorage.setItem('monitorMercadoVisao',nome);
  document.querySelectorAll('.view').forEach(e=>e.classList.toggle('on',e.dataset.view===nome));
  document.querySelectorAll('#nav button').forEach(e=>e.classList.toggle('on',e.dataset.view===nome));
  if(nome==='grafico') setTimeout(grafico,0);
}
const RC={LATERAL:'lat',CANAL_ALTA:'ca',CANAL_BAIXA:'cb',TENDENCIA:'tend',indefinido:'ind'};
const RN={LATERAL:'lateral',CANAL_ALTA:'canal alta',CANAL_BAIXA:'canal baixa',
          TENDENCIA:'tendencia',indefinido:'—'};
const FMT_HORA_BRT=new Intl.DateTimeFormat('pt-BR',{timeZone:'America/Sao_Paulo',hour:'2-digit',minute:'2-digit',hour12:false});
const FMT_DATA_BRT=new Intl.DateTimeFormat('pt-BR',{timeZone:'America/Sao_Paulo',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
function instanteUtc(valor){
  if(!valor) return null;
  const texto=String(valor).replace(' ','T');
  return new Date(/[zZ]|[+-][0-9][0-9]:[0-9][0-9]$/.test(texto)?texto:texto+'Z');
}
function horaBrt(valor){const d=instanteUtc(valor);return d&&!isNaN(d)?FMT_HORA_BRT.format(d):'—';}
function dataBrt(valor){const d=instanteUtc(valor);return d&&!isNaN(d)?FMT_DATA_BRT.format(d):'—';}

function linha(a,x){
  if(x.disponivel===false){
    return `<tr onclick="pick('${a}')"><td><b>${a}</b></td>
      <td><span class="estado wait">SEM DADOS</span></td><td>${x.classe||'—'}</td>
      <td colspan="9" class="ind">${x.motivo_entrada||'Aguardando dados da IQ Option.'}</td></tr>`;
  }
  const pc = x.pos_canal;
  const bar = pc==null?'—':`<span class="bar"><i style="left:${Math.max(0,Math.min(100,pc))*0.57}px"></i></span> ${pc}%`;
  const d = x.dia||{};
  const dc = d.dir==='alta'?'up':(d.dir==='baixa'?'dn':'lat');
  // proximidade: acumulacao ligada + quantos ATR faltam para romper o piso
  let prox='<span class="ind">—</span>';
  if(x.acumulacao){
    const dd = x.dist_atr;
    const cor = dd==null?'ind':(dd<=0.5?'quente':(dd<=1.5?'morno':'frio'));
    prox = `<span class="${cor}">acum · ${dd==null?'?':dd.toFixed(2)} ATR</span>`;
  } else if(x.aperto!=null){
    prox = `<span class="ind">${x.aperto.toFixed(2)}x limiar</span>`;
  }
  const estado=x.entrada_valida?'<span class="estado go">ENTRAR</span>':x.sinal?'<span class="estado study">ESTUDO</span>':'<span class="estado wait">AGUARDAR</span>';
  const linhaClasse=x.entrada_valida?'sig':x.sinal?'study':'';
  const fl=x.fluxo||{};
  const fluxo=fl.disponivel?`${fl.estado}<br><span class="ind">${fl.qualidade}/100 · ${fl.sessao}</span>`:'—';
  return `<tr class="${linhaClasse}" onclick="pick('${a}')">
    <td><b>${a}</b></td>
    <td>${estado}</td>
    <td>${x.classe}</td>
    <td>${fluxo}</td>
    <td>${prox}</td>
    <td class="${RC[x.regime]||'ind'}">${RN[x.regime]||'—'}</td>
    <td>${x.r2==null?'—':x.r2}</td>
    <td>${bar}</td>
    <td class="${dc}">${d.dir||'—'} ${d.pct>0?'+':''}${d.pct==null?'':d.pct}%</td>
    <td class="${d.ema==='alta'?'up':'dn'}">${d.ema||'—'}</td>
    <td>${x.atr_pips==null?'—':x.atr_pips+' '+(x.unidade_movimento||'pips')}</td>
    <td>${x.preco}</td></tr>`;
}

function histLinha(h){
  const r=h.resultado;
  const cor = r==='subiu'?'up':(r==='caiu'?'dn':'ind');
  const res = r==null?'<span class="ind">aguardando</span>'
            : `<span class="${cor}">${r} ${h.var_pips>0?'+':''}${h.var_pips??''} ${h.var_unidade||'pips'}</span>`;
  const hh = horaBrt(h.quando);
  const estado=h.entrada_valida?'<span class="estado go">válida</span>':'<span class="estado study">estudo</span>';
  const tipo=h.tipo==='fibo_m15'?'FIBO':h.tipo==='fluxo_m15'?'FLUXO':h.tipo==='orb_fvg_m15'?'ORB/FVG':h.tipo==='liquidity_sweep_v2'?'LIQUIDEZ V2':'SINAL';
  const av=h.alvos_estudo||h.alvos||{};
  const simDesfecho=(h.simulacao||{}).desfecho||'aguardando';
  const simCor=simDesfecho==='win_tp1'?'up':simDesfecho==='loss_sl'?'dn':'ind';
  const simTexto=simDesfecho==='win_tp1'?'TP1':simDesfecho==='loss_sl'?'SL':simDesfecho==='nao_executada_6h'?'n/exec':simDesfecho==='expirado_6h'?'exp6h':simDesfecho.startsWith('ambíguo')?'ambíg':'?';
  const alvosTexto=av.entrada!=null
    ?`<span class="hist-alvos">E ${av.entrada} · <span class="dn">SL ${av.sl}</span> · <span class="up">TP ${av.tp1}</span></span>`
    :'<span class="hist-alvos ind">—</span>';
  return `<tr onclick="dossie('${h.id||''}')"><td>${hh}</td><td><b>${h.ativo}</b></td>
    <td>${estado}</td>
    <td>${tipo}</td>
    <td>${h.janela_boa?'<span class="badge b-ok">18–19h BRT</span>':hh+' BRT'}</td>
    <td class="${RC[h.regime]||'ind'}">${RN[h.regime]||'—'}</td>
    <td>${h.preco}</td>
    <td>${alvosTexto}</td>
    <td><span class="${simCor}">${simTexto}</span></td>
    <td>${res}</td></tr>`;
}

function checklist(x){
  const itens=x.checklist||[];
  if(!itens.length) return '<span class="ind">Checklist indisponível neste registro antigo.</span>';
  return `<div class="checklist">${itens.map(i=>`<span class="check ${i.ok?'ok':'no'}">${i.ok?'✓':'✗'} ${i.nome}</span>`).join('')}</div>`;
}

function textoSimulacao(s){
  const d=(s||{}).desfecho||'aguardando';
  const rr=s&&s.resultado_r!=null?` · ${Number(s.resultado_r).toFixed(2)}R`:'';
  if(d==='win_tp1') return `<span class="dossie-win">TP1 atingido (simulado)${rr}</span>`;
  if(d==='loss_sl') return `<span class="dossie-loss">SL atingido (simulado)${rr}</span>`;
  if(d==='ambíguo_mesma_vela') return '<span class="dossie-aviso">TP e SL na mesma vela — ordem desconhecida</span>';
  if(d==='ambíguo_entrada_tp_mesma_vela') return '<span class="dossie-aviso">entrada e TP na mesma vela — sequência desconhecida</span>';
  if(d==='nao_executada_6h') return '<span class="ind">preço de entrada não foi tocado em 6h</span>';
  if(d==='expirado_6h') return `<span class="dossie-aviso">fechado por tempo após 6h${rr}</span>`;
  return `<span class="ind">aguardando candles futuros (${(s&&s.velas)||0}/24)</span>`;
}

function renderDossie(H){
  const el=document.getElementById('dossie');
  const h=H.find(x=>x.id===dossieSel)||H[0];
  if(!h){ el.innerHTML='<span class="empty">Ainda não há sinais registrados.</span>'; return; }
  dossieSel=h.id;
  const a=h.dossie||{}, n=a.noticia||{}, c=h.comparaveis||{};
  const vol=a.volume_relativo==null?'sem dado':a.volume_relativo+'× mediana';
  const comp=c.amostra
    ? `<span class="${c.winrate>=55?'dossie-win':'dossie-aviso'}">${c.wins}W / ${c.losses}L · ${c.winrate}%</span>`
    : '<span class="ind">amostra ainda pequena</span>';
  const reacao=h.resultado==null?'aguardando':`${h.resultado} ${h.var_pips>0?'+':''}${h.var_pips||0} ${h.var_unidade||'pips'}`;
  const estado=h.entrada_valida?'ENTRADA VÁLIDA':'ESTUDO — NÃO OPERAR';
  const direcao=h.direcao==='sell'?'SELL':'BUY';
  const av=h.alvos_estudo||h.alvos||{};
  const alvosHtml=av.entrada!=null?`
    <div style="margin:.5rem 0">
      <div class="sec" style="margin:.35rem 0 .2rem">PLANO (estudo — não executar)</div>
      <div class="alvos">
        <div><label>entrada</label><span style="color:#38bdf8">${av.entrada}</span></div>
        <div><label>invalidação / SL</label><span class="dn">${av.sl}</span></div>
        <div><label>alvo TP1</label><span class="up">${av.tp1}</span></div>
        ${av.tp2!=null?`<div><label>alvo TP2</label><span class="up">${av.tp2}</span></div>`:''}
        <div><label>risco</label><span>${av.risco_pips??'—'} ${av.risco_unidade||'pips'}</span></div>
        <div><label>simulação</label>${textoSimulacao(h.simulacao)}</div>
      </div>
    </div>`
    :`<div class="nota" style="margin:.4rem 0">Sem plano de alvos registrado para este sinal.</div>`;
  el.innerHTML=`<div class="card">
    <b>${h.ativo} ${direcao}</b> · ${dataBrt(h.vela)} BRT · <span class="estado ${h.entrada_valida?'go':'study'}">${estado}</span>
    ${checklist(h)}
    ${alvosHtml}
    <div class="dossie">
      <div class="item"><label>vela de sinal</label><span>${a.tipo||'—'} · corpo ${a.corpo_pct??'—'}%</span></div>
      <div class="item"><label>pavios</label><span>sup ${a.pavio_sup_pct??'—'}% · inf ${a.pavio_inf_pct??'—'}%</span></div>
      <div class="item"><label>range / volatilidade</label><span>${a.range_atr??'—'} ATR · ${a.atr_relativo??'—'}×</span></div>
      <div class="item"><label>movimento anterior</label><span>${a.movimento_3_atr??'—'} ATR · vol ${vol}</span></div>
      <div class="item"><label>reação da vela seguinte</label><span>${reacao}</span></div>
      <div class="item"><label>casos parecidos (${c.amostra||0})</label>${comp}</div>
      <div class="item"><label>notícia no contexto</label><span class="${n.estado==='janela_risco'?'dossie-aviso':''}">${n.texto||'sem calendário'}</span></div>
    </div>
    <div class="dossie-nota">Tags: ${(a.tags||[]).join(' · ')||'sem dados'}. Comparação usa apenas sinais antigos da mesma classe e com contexto parecido. Isto descreve padrões; não prova a causa de win ou loss.</div>
  </div>`;
}

function dossie(id){ dossieSel=id; render(); }

// contagem regressiva ate o fechamento da vela M15
function tickRelogio(){
  const now=new Date();
  const s=now.getUTCMinutes()*60+now.getUTCSeconds();
  const falta=900-(s%900);
  const m=String(Math.floor(falta/60)).padStart(2,'0');
  const ss=String(falta%60).padStart(2,'0');
  const el=document.getElementById('relogio');
  if(el) el.textContent=`proxima vela M15 em ${m}:${ss}`;
}
setInterval(tickRelogio,1000);

// alerta sonoro (WebAudio, sem arquivo externo)
let ultimoAlerta=0;
function bip(){
  if(Date.now()-ultimoAlerta<60000) return;
  ultimoAlerta=Date.now();
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const o=ctx.createOscillator(), g=ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value=880; g.gain.value=.08;
    o.start(); setTimeout(()=>{o.stop();ctx.close();},220);
  }catch(e){}
}

function cardSinal(a,x){
  const v=x.alvos_estudo||x.alvos;
  const entrar=Boolean(x.entrada_valida && x.alvos);
  if(!v) return `<div class="card nojan"><b>${a}</b> — ${x.estado_entrada||'AGUARDAR'} (ATR indisponível)</div>`;
  return `<div class="card ${entrar?'okjan':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <b style="font-size:.95rem">${a} LONG</b>
      <span class="estado ${entrar?'go':'study'}">${x.estado_entrada||'ESTUDO — NÃO OPERAR'}</span>
    </div>
    ${checklist(x)}
    ${entrar ? `<div class="alvos">
      <div><label>entrada</label><span>${v.entrada}</span></div>
      <div><label>stop (1 ATR)</label><span class="dn">${v.sl}</span></div>
      <div><label>alvo 1 (2.5 ATR)</label><span class="up">${v.tp1}</span></div>
      <div><label>alvo 2 (5 ATR)</label><span class="up">${v.tp2}</span></div>
      <div><label>risco</label><span>${v.risco_pips} ${v.risco_unidade||'pips'}</span></div>
      <div><label>regime (contexto)</label><span class="${RC[x.regime]||'ind'}" style="font-size:.8rem">${RN[x.regime]||'—'}</span></div>
    </div><div class="nota">TP1: 40.4% de acerto, EV +0.41R. TP2: 24.6%, EV +0.48R. Medido na janela validada.</div>`
    : `<div class="nota">NÃO ABRIR operação. ${x.motivo_entrada||'Sinal salvo apenas para estudo.'} Alvos permanecem ocultos para não sugerir uma entrada fora do teste.</div>`}
  </div>`;
}

function maturidadeBadge(x){
  const m=x.maturidade||'INSUFICIENTE';
  const cls={INSUFICIENTE:'maturidade-insuf',OBSERVAR:'maturidade-obs',CANDIDATA:'maturidade-cand',APROVADA:'maturidade-aprov'}[m]||'maturidade-insuf';
  const ic=x.ic_95;
  const icTexto=ic?`<span class="ic-badge ok">IC95% ${ic[0]}–${ic[1]}%</span>`:`<span class="ic-badge insuf">IC indisponível</span>`;
  const insuf=!x.amostra_suficiente?'<span style="color:#64748b;font-size:.64rem"> ⚠ AMOSTRA INSUFICIENTE (n&lt;30 resolvidos)</span>':'';
  return `<span class="${cls}">${m}</span>${icTexto}${insuf}`;
}
function linhaEstudo(label,x){
  const wr=x.winrate==null?'sem TP/SL resolvido':`${x.wins} TP / ${x.losses} SL · ${x.winrate}%`;
  const saldo=x.saldo_r==null?'R aguardando':`${x.saldo_r>0?'+':''}${x.saldo_r}R`;
  return `<div style="margin:.45rem 0;padding:.4rem .5rem;background:#0c1728;border-left:3px solid #334155;border-radius:.25rem">
    <b>${label}</b> · ${x.sinais||0} sinais ${maturidadeBadge(x)}<br>
    <span style="font-size:.7rem;color:#cbd5e1">${wr} · ${saldo} em ${x.avaliados_r||0} saídas · ${x.expirados||0} fechados por tempo · ${x.pendentes||0} pendentes · ${x.nao_executadas||0} não exec.</span>
  </div>`;
}
function renderAmostra(){
  const el=document.getElementById('amostra'); if(!el) return;
  el.innerHTML=
    linhaEstudo('Entradas válidas (falso rompimento)',D.amostraEntrada||{})+
    linhaEstudo('Fibo M15 — estudo',D.amostraFibo||{})+
    linhaEstudo('Fluxo de sessão — estudo',D.amostraFluxo||{})+
    linhaEstudo('ORB/FVG M15 — estudo sombra',D.amostraOrb||{})+
    linhaEstudo('Liquidez V2 — estudo sombra',D.amostraLiquidez||{})+
    '<div class="nota">Estudos não são misturados ao sinal validado. IC95% de Wilson; amostras abaixo de 30 resolvidos não sustentam conclusões.</div>';
}

function renderFluxo(){
  const el=document.getElementById('fluxo');
  const x=(D.ativos||{})[sel]||{}, f=x.fluxo||{};
  if(!f.disponivel){
    el.innerHTML=`<div class="card"><b>${f.estado||'FLUXO INDISPONÍVEL'}</b><div class="nota">${f.motivo||'Aguardando dados.'}</div></div>`;
    return;
  }
  const a=f.alvos||{}, dir=f.direcao==='buy'?'COMPRA':f.direcao==='sell'?'VENDA':'NEUTRO';
  const cor=f.sinal_estudo?'go':'study';
  el.innerHTML=`<div class="card ${f.sinal_estudo?'okjan':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>${sel} — ${f.sessao}</b><span class="estado ${cor}">${f.sinal_estudo?'SOMBRA '+dir:f.estado}</span></div>
    ${checklist(f)}
    <div class="alvos">
      <div><label>VWAP da sessão</label><span>${f.vwap}</span></div>
      <div><label>POC / preço de controle</label><span>${f.poc}</span></div>
      <div><label>área de valor</label><span>${f.val} — ${f.vah}</span></div>
      <div><label>tendência H1 / M15</label><span>${f.h1} / ${f.m15}</span></div>
      <div><label>eficiência</label><span>${f.eficiencia_estado} · ${f.eficiencia}</span></div>
      <div><label>atividade</label><span>${f.volume_relativo}× · ${f.movimento_atr} ATR</span></div>
      <div><label>qualidade experimental</label><span>${f.qualidade}/100</span></div>
      <div><label>R:R até POC</label><span>${f.rr||'—'}x</span></div>
      ${a.entrada!=null?`<div><label>entrada sombra</label><span>${a.entrada}</span></div><div><label>SL sombra</label><span class="dn">${a.sl}</span></div><div><label>TP1 POC</label><span class="up">${a.tp1}</span></div><div><label>TP2 área oposta</label><span class="up">${a.tp2}</span></div>`:''}
    </div>
    <div class="nota">${f.motivo} Fonte: ${f.fonte_volume}. É contexto experimental; não abre posição.</div>
  </div>`;
}

function renderOrb(){
  const el=document.getElementById('orb');
  const x=(D.ativos||{})[sel]||{}, o=x.orb||{};
  if(!o.disponivel){
    el.innerHTML=`<div class="card orb-card"><b>${o.estado||'ORB INDISPONÍVEL'}</b><div class="nota">${o.motivo||'Aguardando dados.'}</div></div>`;
    return;
  }
  const a=o.alvos||{}, dir=o.direcao==='buy'?'COMPRA':o.direcao==='sell'?'VENDA':'NEUTRO';
  el.innerHTML=`<div class="card orb-card ${o.sinal_estudo?'okjan ok':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>${sel} — ${o.sessao||'—'}</b><span class="estado ${o.sinal_estudo?'go':'study'}">${o.sinal_estudo?'SOMBRA '+dir:o.estado}</span></div>
    ${checklist(o)}
    <div class="alvos">
      <div><label>faixa ORB</label><span>${o.orl??'—'} — ${o.orh??'—'}</span></div>
      <div><label>tendência H1 / H4</label><span>${o.h1||'—'} / ${o.h4||'—'}</span></div>
      <div><label>FVG M15</label><span>${o.fvg?`${o.fvg_inf} — ${o.fvg_sup}`:'não formado'}</span></div>
      <div><label>direção observada</label><span>${dir}</span></div>
      <div><label>entrada sombra</label><span>${a.entrada??'—'}</span></div>
      <div><label>SL estrutural</label><span class="dn">${a.sl??'—'}</span></div>
      <div><label>TP1</label><span class="up">${a.tp1??'—'} ${a.tp1!=null?'(1R)':''}</span></div>
      <div><label>TP2</label><span class="up">${a.tp2??'—'} ${a.tp2!=null?'(2R)':''}</span></div>
    </div>
    <div class="nota">${o.motivo} Adaptação M15 para pesquisa; não envia ordem.</div>
  </div>`;
}

function renderLiquidez(){
  const el=document.getElementById('liquidez');
  const x=(D.ativos||{})[sel]||{}, l=x.liquidez||{}, a=l.alvos||{};
  if(!l.disponivel){
    el.innerHTML=`<div class="card"><b>${l.estado||'SEM VARREDURA'}</b><div class="nota">${l.motivo||'Aguardando dados.'}</div></div>`;
    return;
  }
  el.innerHTML=`<div class="card ${l.sinal_estudo?'okjan ok':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>${sel} — BUY</b><span class="estado ${l.sinal_estudo?'go':'study'}">${l.estado}</span></div>
    ${checklist(l)}
    <div class="alvos">
      <div><label>range anterior</label><span>${l.piso_range} — ${l.teto_range}</span></div>
      <div><label>penetração</label><span>${l.penetracao_atr} ATR</span></div>
      <div><label>gatilho acima da máxima</label><span>${a.entrada??'—'}</span></div>
      <div><label>SL abaixo do pavio</label><span class="dn">${a.sl??'—'}</span></div>
      <div><label>TP1 no teto do range</label><span class="up">${a.tp1??'—'}</span></div>
      <div><label>R:R estrutural</label><span>${l.rr??'—'}x</span></div>
    </div><div class="nota">${l.motivo} Ainda é estudo e não envia ordem.</div>
  </div>`;
}

function renderFibo(){
  const el=document.getElementById('fibo');
  const x=(D.ativos||{})[sel]||{}, f=x.fibo||{};
  if(!f.disponivel){
    el.innerHTML=`<div class="card fibo-card"><b>${f.estado||'SEM PLANO FIBO'}</b><div class="nota">${f.motivo||'Ainda não há impulso M15 limpo e alinhado para desenhar a retração.'}</div></div>`;
    return;
  }
  const a=f.alvos||{}, dir=f.direcao==='sell'?'VENDA':'COMPRA';
  const cls=f.qualificada?'okjan ok':'nojan';
  el.innerHTML=`<div class="card fibo-card ${cls}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>${sel} — ${dir}</b><span class="estado ${f.qualificada?'go':'study'}">${f.estado}</span></div>
    ${checklist(f)}
    <div class="alvos">
      <div><label>zona Fibo</label><span class="fibo-zona">${f.zona_inf} — ${f.zona_sup}</span></div>
      <div><label>melhor entrada (61.8%)</label><span>${a.entrada??'—'}</span></div>
      <div><label>invalidação (78.6%)</label><span class="dn">${f.fib786??'—'}</span></div>
      <div><label>SL + margem ATR</label><span class="dn">${a.sl??'—'}</span></div>
      <div><label>TP1: extremo anterior</label><span class="up">${a.tp1??'—'}</span></div>
      <div><label>TP2: extensão 127.2%</label><span class="up">${a.tp2??'—'}</span></div>
      <div><label>TP3: extensão 161.8%</label><span class="up">${a.tp3??'—'}</span></div>
      <div><label>R:R até TP1</label><span>${f.rr}x</span></div>
    </div>
    <div class="nota">Impulso: ${String(f.impulso_inicio||'').slice(0,16)} → ${String(f.impulso_fim||'').slice(0,16)} · ${f.amplitude_atr} ATR. Fibo é leitura em estudo: não abre posição e ainda não altera o sinal validado.</div>
  </div>`;
}

function renderDecisaoPrincipal(){
  const el=document.getElementById('decisao-principal'); if(!el) return;
  const A=D.ativos||{};
  const ks=Object.keys(A);
  // Prioridade: entrada_valida > sinal (estudo) > maior prox
  const entrar=ks.filter(k=>A[k].entrada_valida);
  const estudo=ks.filter(k=>A[k].sinal&&!A[k].entrada_valida);
  const candidato=entrar.length
    ? entrar.sort((a,b)=>(A[b].prox||0)-(A[a].prox||0))[0]
    : estudo.length
      ? estudo.sort((a,b)=>(A[b].prox||0)-(A[a].prox||0))[0]
      : ks.sort((a,b)=>(A[b].prox||0)-(A[a].prox||0))[0];
  if(!candidato){ el.innerHTML=''; return; }
  const x=A[candidato];
  const isEntrar=x.entrada_valida;
  const isEstudo=x.sinal&&!x.entrada_valida;
  const cls=isEntrar?'entrar':isEstudo?'estudo':'';
  const estadoTexto=x.estado_entrada||'AGUARDAR';
  const dir=(x.direcao||'buy')==='sell'?'SELL':'BUY';
  const v=x.alvos||x.alvos_estudo||{};
  const alvosHtml=v.entrada!=null?`<div class="decisao-hero-alvos">
    <div class="decisao-hero-chip"><label>Entrada</label><div class="val entrada">${v.entrada}</div></div>
    <div class="decisao-hero-chip"><label>Invalidação SL</label><div class="val sl">${v.sl}</div></div>
    <div class="decisao-hero-chip"><label>Alvo TP1</label><div class="val tp">${v.tp1}</div></div>
  </div>`:'';
  el.innerHTML=`<div class="decisao-hero ${cls}">
    <div class="decisao-hero-topo">
      <div>
        <div class="decisao-hero-ativo">${candidato} <span class="decisao-hero-dir">${dir}</span></div>
        <div style="font-size:.68rem;color:#64748b;margin-top:.12rem">${x.classe||''} · ${x.preco??'—'}</div>
      </div>
      <span class="estado ${isEntrar?'go':isEstudo?'study':'wait'}" style="font-size:.8rem;white-space:nowrap">${estadoTexto}</span>
    </div>
    <div class="decisao-hero-motivo">${x.motivo_entrada||''}</div>
    ${alvosHtml}
  </div>`;
}

function render(){
  const A=D.ativos||{};
  const ks=Object.keys(A).sort();
  const sig=ks.filter(k=>A[k].sinal);
  document.getElementById('sinais').innerHTML = sig.length
    ? sig.map(k=>cardSinal(k,A[k])).join('')
    : '<span style="color:#475569;font-style:italic;font-size:.8rem">nenhum sinal agora</span>';
  // ordena por proximidade do sinal: o que esta perto de disparar sobe
  const ord=[...ks].sort((x,y)=>(A[y].prox||0)-(A[x].prox||0));
  document.getElementById('tab').innerHTML =
    '<table><tr><th>ativo</th><th>decisão</th><th>classe</th><th>fluxo / sessão</th><th>proximidade</th><th>regime</th><th>R2</th><th>pos. no canal</th><th>dia</th><th>EMA 4h/1d</th><th>ATR</th><th>preço</th></tr>'
    + ord.map(k=>linha(k,A[k])).join('') + '</table>';

  const todos=D.historico||[];
  const tipo=document.getElementById('filtro-tipo')?.value||'';
  const resultado=document.getElementById('filtro-resultado')?.value||'';
  const soAtivo=document.getElementById('filtro-ativo')?.checked;
  const tipos=[...new Set(todos.map(h=>h.tipo||'falso_rompimento'))].sort();
  const seletorTipo=document.getElementById('filtro-tipo');
  if(seletorTipo && seletorTipo.options.length!==tipos.length+1){
    seletorTipo.innerHTML='<option value="">Todos os tipos</option>'+tipos.map(x=>`<option value="${x}">${x}</option>`).join('');
    seletorTipo.value=tipo;
  }
  const H=todos.filter(h=>(!tipo||(h.tipo||'falso_rompimento')===tipo)
    &&(!resultado||String((h.simulacao||{}).desfecho||'').startsWith(resultado))
    &&(!soAtivo||h.ativo===sel));
  document.getElementById('hist').innerHTML = H.length
    ? '<table><tr><th>hora</th><th>ativo</th><th>decisão</th><th>tipo</th><th>janela</th><th>regime</th><th>preço</th><th>entrada · SL · TP</th><th>sim.</th><th>vela seguinte</th></tr>'
      + H.map(histLinha).join('') + '</table>'
    : '<span style="color:#475569;font-style:italic;font-size:.8rem">nenhum sinal nas ultimas 24h</span>';
  renderDossie(todos);
  renderAmostra();
  const entradas=ks.filter(k=>A[k].entrada_valida);
  const chaveAlerta=entradas.join('|');
  if(chaveAlerta && chaveAlerta!==ultimaChaveAlerta) bip();
  ultimaChaveAlerta=chaveAlerta;
  const s=D.saudeDados||{};
  document.getElementById('saude').innerHTML=[
    ['Armazenamento',s.armazenamento||'—'],['Eventos',s.eventos??0],
    ['Entradas válidas',s.entradas_validas??0],['Pendentes',s.pendentes??0],
    ['Ambíguos',s.ambiguos??0],['Schema',D.schemaVersao??'—']
  ].map(([a,b])=>`<div class="saude-item"><label>${a}</label><b>${b}</b></div>`).join('');
  const t=document.getElementById('tabs');
  if(t.children.length!==ks.length){
    t.innerHTML = ks.map(k=>`<button class="tab" data-a="${k}" onclick="pick('${k}')">${k}</button>`).join('');
    if(!sel && ks.length) sel=ks[0];
  }
  [...t.children].forEach(b=>b.className='tab'+(b.dataset.a===sel?' on':''));
  renderDecisaoPrincipal();
  renderFluxo();
  renderOrb();
  renderLiquidez();
  renderFibo();
}

async function pick(a){ sel=a; _primeiroDesenho=true; render(); await grafico();
  if(window.Marcacoes) Marcacoes.carregar(); }

let chartM=null, sCandles=null, sSup=null, sInf=null;
let linhasNivelM=[], seriesZonaM=[];
let _primeiroDesenho=true;
let _observadorLargura=null;

function _marcAtualizar(itens, modo, aguardandoSegundo){
  const bF=document.getElementById('btn-marc-fibo');
  const bL=document.getElementById('btn-marc-linha');
  const dica=document.getElementById('marc-dica');
  if(bF) bF.classList.toggle('on', modo==='fibo');
  if(bL) bL.classList.toggle('on', modo==='horizontal');
  if(!dica) return;
  dica.textContent = modo==='fibo'
    ? (aguardandoSegundo?'clique no extremo':'clique na origem')
    : modo==='horizontal' ? 'clique no preço'
    : (itens.length ? `${itens.length} ${itens.length>1?'marcações':'marcação'}` : '');
}
function marcarFibo(){ Marcacoes.setModo('fibo'); }
function marcarLinha(){ Marcacoes.setModo('horizontal'); }
function limparMarcacoes(){
  if(!Marcacoes.itens.length) return;
  if(confirm(`Apagar ${Marcacoes.itens.length} marcação(ões) de ${sel}?`)) Marcacoes.limparTudo();
}

function _chartMonitor(){
  if(chartM) return chartM;
  const el=document.getElementById('cv');
  // Criar com largura 0 (aba escondida) deixa o barSpacing preso em 0.5 e
  // nem fitContent recupera. mudarVisao('grafico') rechama isto ao abrir.
  if(!el.clientWidth){
    // Aba em segundo plano ou painel oculto: tenta de novo assim que
    // o container ganhar largura, em vez de esperar o tick de 30s.
    if(!_observadorLargura){
      _observadorLargura=new ResizeObserver(()=>{ if(!chartM && el.clientWidth) grafico(); });
      _observadorLargura.observe(el);
    }
    return null;
  }
  chartM=LightweightCharts.createChart(el,{
    width:el.clientWidth, height:el.clientHeight || 340,
    layout:{background:{color:'#0d1526'},textColor:'#94a3b8',fontSize:10},
    grid:{vertLines:{color:'#16233a'},horzLines:{color:'#16233a'}},
    rightPriceScale:{borderColor:'#1e293b'},
    timeScale:{borderColor:'#1e293b',timeVisible:true,secondsVisible:false},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
  });
  sCandles=chartM.addCandlestickSeries({
    upColor:'#22c55e',downColor:'#ef4444',borderUpColor:'#22c55e',
    borderDownColor:'#ef4444',wickUpColor:'#22c55e',wickDownColor:'#ef4444',
  });
  const opBanda={color:'#f59e0b8c',lineWidth:1,lastValueVisible:false,
    priceLineVisible:false,crosshairMarkerVisible:false};
  sSup=chartM.addLineSeries(opBanda);
  sInf=chartM.addLineSeries(opBanda);
  new ResizeObserver(()=>{
    if(el.clientWidth>0 && el.clientHeight>0){
      chartM.applyOptions({width:el.clientWidth, height:el.clientHeight});
    }
  }).observe(el);
  if(window.Marcacoes){
    Marcacoes.iniciar({painel:'monitor', chart:chartM, serie:sCandles,
                       getAtivo:()=>sel, aoAtualizar:_marcAtualizar});
  }
  return chartM;
}

async function grafico(){
  if(!sel) return;
  const aviso=document.getElementById('cv-aviso');
  const titulo=document.getElementById('cv-titulo');
  let d;
  try{
    const resposta=await fetch('mkt_'+sel+'.json?t='+Date.now());
    if(!resposta.ok) throw new Error('HTTP '+resposta.status);
    d=await resposta.json();
  }catch(e){
    aviso.textContent=sel+' \u2014 gr\u00e1fico indispon\u00edvel; aguardando dados v\u00e1lidos';
    aviso.style.display=''; return;
  }
  const K=Array.isArray(d.candles)?d.candles:[];
  if(!K.length){
    aviso.textContent=sel+' \u2014 aguardando candles do mercado normal';
    aviso.style.display=''; return;
  }
  aviso.style.display='none';
  if(!_chartMonitor()) return;

  // Marcacoes do ativo anterior nao podem sobreviver a troca de aba.
  linhasNivelM.forEach(l=>sCandles.removePriceLine(l)); linhasNivelM=[];
  seriesZonaM.forEach(s=>chartM.removeSeries(s)); seriesZonaM=[];

  const A=(D.ativos||{})[sel]||{}, F=A.fibo||{}, FA=F.alvos||{}, FL=A.fluxo||{},
        FLA=FL.alvos||{}, O=A.orb||{}, OA=O.alvos||{}, L=A.liquidez||{}, LA=L.alvos||{};
  const candleVals=K.flatMap(k=>[Number(k.h),Number(k.l)]).filter(Number.isFinite);
  const candleMax=Math.max(...candleVals), candleMin=Math.min(...candleVals);
  const faixa=Math.max(candleMax-candleMin,Math.abs(candleMax)*1e-6,1e-9);
  // Um alvo distante nao pode achatar os candles. Niveis fora de 35% da
  // janela continuam no cartao de leitura, mas nao entram no grafico.
  const perto=v=>Number.isFinite(v)&&v>=candleMin-faixa*.35&&v<=candleMax+faixa*.35;

  sCandles.setData(K.map(k=>({time:k.t,open:Number(k.o),high:Number(k.h),
                              low:Number(k.l),close:Number(k.c)})));
  const serieBanda=arr=>(arr||[]).map((v,i)=>{
    const n=Number(v);
    return (v==null||!K[i]||!perto(n))?null:{time:K[i].t,value:n};
  }).filter(Boolean);
  sSup.setData(serieBanda(d.sup));
  sInf.setData(serieBanda(d.inf));

  function nivel(valor,cor,rotulo){
    if(!perto(valor)) return;
    linhasNivelM.push(sCandles.createPriceLine({
      price:valor, color:cor, lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.Dashed,
      axisLabelVisible:true, title:rotulo,
    }));
  }
  function zona(v1,v2,cor,rotulo){
    const a=Number(v1), b=Number(v2);
    if(!perto(a)||!perto(b)) return;
    const lo=Math.min(a,b), hi=Math.max(a,b);
    const s=chartM.addBaselineSeries({
      baseValue:{type:'price',price:lo},
      topFillColor1:cor, topFillColor2:cor, topLineColor:'transparent',
      bottomFillColor1:'transparent', bottomFillColor2:'transparent',
      bottomLineColor:'transparent', lastValueVisible:false,
      priceLineVisible:false, crosshairMarkerVisible:false,
      title:rotulo||'',
    });
    s.setData(K.map(k=>({time:k.t,value:hi})));
    seriesZonaM.push(s);
  }

  nivel(Number(A.piso_acumulacao),'#38bdf8','piso 10 velas');
  if(FL.disponivel){
    zona(FL.val,FL.vah,'#0ea5e914','');
    nivel(Number(FL.vwap),'#38bdf8','VWAP sess\u00e3o');
    nivel(Number(FL.poc),'#f59e0b','POC');
    nivel(Number(FL.vah),'#60a5fa','VAH');
    nivel(Number(FL.val),'#60a5fa','VAL');
    if(FL.sinal_estudo && FLA){
      nivel(Number(FLA.sl),'#ef4444','Fluxo SL sombra');
      nivel(Number(FLA.tp1),'#22c55e','Fluxo TP1 POC');
    }
  }
  if(O.disponivel){
    const orh=Number(O.orh), orl=Number(O.orl);
    if(Number.isFinite(orh) && Number.isFinite(orl)){
      zona(orh,orl,'#f59e0b12','');
      nivel(orh,'#f59e0b','ORB m\u00e1xima');
      nivel(orl,'#f59e0b','ORB m\u00ednima');
    }
    if(O.fvg) zona(O.fvg_inf,O.fvg_sup,'#22c55e1d','FVG M15');
    if(O.sinal_estudo && OA){
      nivel(Number(OA.entrada),'#22c55e','ORB entrada sombra');
      nivel(Number(OA.sl),'#ef4444','ORB SL');
      nivel(Number(OA.tp1),'#86efac','ORB TP1 1R');
      nivel(Number(OA.tp2),'#4ade80','ORB TP2 2R');
    }
  }
  if(L.sinal_estudo && LA){
    nivel(Number(L.piso_range),'#38bdf8','Liquidez: piso varrido');
    nivel(Number(LA.entrada),'#22c55e','Liquidez: gatilho BUY');
    nivel(Number(LA.sl),'#ef4444','Liquidez: SL sombra');
    nivel(Number(LA.tp1),'#86efac','Liquidez: TP1 range');
  }
  if(F.disponivel){
    zona(F.zona_inf,F.zona_sup,'#8b5cf622','ZONA FIBO 38.2-61.8%');
    nivel(Number(FA.entrada),'#a78bfa','Fibo entrada 61.8%');
    nivel(Number(F.fib786),'#f59e0b','Fibo invalida 78.6%');
    nivel(Number(FA.tp1),'#22c55e','Fibo TP1');
    nivel(Number(FA.tp2),'#86efac','Fibo TP2 127.2%');
  }
  if(A.entrada_valida && A.alvos){
    nivel(Number(A.alvos.entrada),'#22c55e','entrada BUY');
    nivel(Number(A.alvos.sl),'#ef4444','SL');
    nivel(Number(A.alvos.tp1),'#22c55e','TP1');
    nivel(Number(A.alvos.tp2),'#86efac','TP2');
  }
  titulo.textContent=`${sel} \u00b7 ${RN[A.regime]||'\u2014'} \u00b7 R2 ${A.r2??'\u2014'}`;
  // So depois de todas as series: enquadrar antes deixava a grade errada.
  if(_primeiroDesenho){ chartM.timeScale().fitContent(); _primeiroDesenho=false; }
}

async function tick(){
  try{
    D=await (await fetch('mercado.json?t='+Date.now())).json();
    const atualizado=new Date(Number(D.ts||0)*1000);
    document.getElementById('status').textContent='Atualizado: '+FMT_HORA_BRT.format(atualizado)+' BRT';
    render(); await grafico();
  }catch(e){ document.getElementById('status').textContent='Erro: '+e; }
}
tick(); setInterval(tick,30000);
mudarVisao(visao);
</script></body></html>"""


def main() -> int:
    import dataclasses
    try:
        trava_monitor = adquirir_trava_monitor()
    except OSError:
        print("Monitor Mercado já está em execução. Feche a janela antiga antes de reiniciar.")
        return 2
    base = configuracao_scalping_m15()
    cfg = dataclasses.replace(base, timeframe_segundos=TF)
    g = GraficoM5(dataclasses.replace(base, porta_grafico=PORTA,
                                      sufixo_banco="mercado"))
    g.iniciar(abrir_navegador=False)
    (g.pasta_web / "index.html").write_text(_HTML, encoding="utf-8")

    # O servidor vem do GraficoM5, entao os endpoints /marcacoes ja existem;
    # falta so o registro. Banco proprio do monitor (sufixo "mercado") para
    # nao misturar marcacao manual com o banco de operacoes.
    from iqoption_m5 import grafico as _grafico_mod
    from iqoption_m5.registro import RegistroSQLite
    _cfg_marc = dataclasses.replace(base, sufixo_banco="mercado")
    _grafico_mod._handler_registro = RegistroSQLite(_cfg_marc.banco_sqlite)

    estado = Estado(g.pasta_web)
    estado.salvar()
    url = f"http://127.0.0.1:{PORTA}/index.html"
    print(f"Painel: {url}")
    webbrowser.open(url)
    print("Conectando na IQ Option...")
    api = MercadoIQ(base).conectar_somente_leitura()
    calendario = CalendarioEconomico(base.pasta_dados)
    calendario.atualizar()
    print(f"\nCarregando {len(ATIVOS)} ativos...")
    try:
        loop(api, cfg, estado, calendario)
    except KeyboardInterrupt:
        print("\nParado.")
    return 0


if __name__ == "__main__":
    from iqoption_m5.log_arquivo import ativar as ativar_log
    print(f"Log desta sessao: {ativar_log('monitor_mercado')}")
    sys.exit(main())
