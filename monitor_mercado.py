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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
from iqoption_m5.perfil_horario_xauusd import contexto_horario as _ctx_xauusd, regime_atr as _regime_atr_xauusd
from iqoption_m5.monitor_store import MonitorEventStore, SCHEMA_VERSAO
from iqoption_m5.noticias import CalendarioEconomico
from iqoption_m5.ia import segunda_opiniao_grafico

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
SIMULADOR_VERSAO = 3
# Horizonte oficial da simulacao TP/SL, em velas. 24 x M15 = 6h.
HORIZONTE_VELAS = 24
# Horizonte longo medido EM PARALELO, sem substituir o oficial: um trade
# fechado em 6h e outro em 12h sao metodos diferentes e nao podem ser somados.
# Rodando os dois lado a lado da para comparar com dado real depois.
HORIZONTE_LONGO_VELAS = 48
CHAVE_SIM_LONGA = "simulacao_longa"
# Horizonte para conferir a direcao mecanica da noticia. O mecanismo e de
# reprecificacao imediata, entao 4 velas de M15 = 1h; gravado em cada
# registro para que mudar isto depois nao misture medicoes.
HORIZONTE_NOTICIA_VELAS = 4
TF_BOF = 300
HORIZONTE_BOF_VELAS = 12
HORIZONTE_BOF_LONGO_VELAS = 24


def _rotulo_horas(velas: int, timeframe_s: int = TF) -> str:
    """24 velas de M15 viram "6h" — mantem os desfechos ja gravados."""
    horas = velas * timeframe_s / 3600
    return f"{int(horas)}h" if horas == int(horas) else f"{horas:g}h"


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


def sanitizar_candles_m15(candles: pd.DataFrame, timeframe_s: int = TF) -> pd.DataFrame:
    """Remove ticks corrompidos antes de eles virarem histórico/análise M15.

    A API ocasionalmente mistura um tick em minuto intermediário ao lote M15.
    Além de não representar uma vela fechada, esse ponto pode estar em uma
    cotação errada e destruir a escala. A filtragem ocorre antes de qualquer
    indicador, Fibo ou escrita do gráfico.
    """
    if candles is None or candles.empty:
        return candles
    try:
        resultado = candles.copy().sort_index()
        instante = pd.DatetimeIndex(resultado.index)
        if instante.tz is None:
            instante = instante.tz_localize("UTC")
        # O dtype interno do pandas pode estar em microssegundos ou
        # nanossegundos conforme a versão; ``timestamp()`` evita supor a
        # unidade e preserva o alinhamento real do candle.
        segundos = np.asarray([int(valor.timestamp()) for valor in instante])
        alinhada = segundos % timeframe_s == 0
        ohlc = resultado[["Open", "High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce")
        estrutural = (
            ohlc.notna().all(axis=1)
            & (ohlc["High"] >= ohlc[["Open", "Close"]].max(axis=1))
            & (ohlc["Low"] <= ohlc[["Open", "Close"]].min(axis=1))
        )
        # Evita que uma cotação corrompida porém alinhada contamine a série.
        # 5% em 15 minutos é deliberadamente amplo para não apagar movimento
        # real de cripto ou notícia, mas bloqueia o salto EURUSD 1.164→1.355.
        basico = np.asarray(alinhada) & estrutural.to_numpy()
        # Calcula o salto somente entre velas já alinhadas. Caso contrário um
        # tick corrompido no minuto 31 faria a vela M15 seguinte parecer um
        # segundo salto inválido e a apagaria também.
        plausivel = pd.Series(False, index=resultado.index)
        fechados_basicos = ohlc.loc[basico, "Close"]
        plausivel.loc[fechados_basicos.index] = (
            fechados_basicos.pct_change().abs().fillna(0) <= .05
        )
        return resultado.loc[basico & plausivel.to_numpy()].copy()
    except (KeyError, TypeError, ValueError, OverflowError):
        return candles.copy().sort_index()


def candle_atual_para_grafico(
    fechados: pd.DataFrame,
    candidatos: pd.DataFrame,
    timeframe_s: int = TF,
) -> pd.DataFrame | None:
    """Devolve a vela em formação somente se ela puder ser desenhada com segurança.

    A IQ pode devolver um tick isolado com minuto e preço incompatíveis com o
    histórico M15. Ele não entra na análise (que usa ``fechados``), mas antes
    era anexado ao gráfico e esticava a escala inteira. Esta barreira é apenas
    visual: candle fora do bloco M15, OHLC inválido ou salto absurdo é omitido.
    """
    if fechados is None or fechados.empty or candidatos is None or candidatos.empty:
        return None
    atual = candidatos.tail(1).copy()
    try:
        instante = pd.Timestamp(atual.index[-1])
        if instante.tzinfo is None:
            instante = instante.tz_localize("UTC")
        if int(instante.timestamp()) % timeframe_s:
            return None
        valores = atual.iloc[0][["Open", "High", "Low", "Close"]].astype(float)
        if (not np.isfinite(valores.to_numpy()).all()
                or valores["High"] < max(valores["Open"], valores["Close"])
                or valores["Low"] > min(valores["Open"], valores["Close"])):
            return None

        referencia = fechados.tail(32)[["High", "Low", "Close"]].astype(float)
        amplitude_mediana = float((referencia["High"] - referencia["Low"]).median())
        ultimo_preco = float(referencia["Close"].iloc[-1])
        # Permite movimentos reais de notícia, mas não uma cotação corrompida
        # como EURUSD 1.164 → 1.355 em um tick M15.
        salto_maximo = max(12 * amplitude_mediana, abs(ultimo_preco) * .02, 1e-9)
        if float((valores - ultimo_preco).abs().max()) > salto_maximo:
            return None
        return atual
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


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


def detectar_fvg_m15(df: pd.DataFrame, atr: pd.Series) -> dict:
    """Retorna o FVG M15 mais recente que ainda não foi totalmente preenchido.

    Um FVG é o espaço entre a máxima do primeiro candle e a mínima do terceiro
    (BUY), ou o inverso (SELL). Exige tamanho mínimo de 0,15 ATR para não
    transformar ruído de cotação em uma zona. É leitura de confluência, não
    ordem nem sinal operacional.
    """
    vazio = {"disponivel": False, "estado": "SEM FVG ATIVO",
             "motivo": "Nenhum desequilíbrio M15 relevante e não preenchido."}
    if len(df) < 3 or atr.empty:
        return vazio
    try:
        dados = df[["High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce").dropna().tail(64)
        atr_local = atr.reindex(dados.index).ffill()
        if len(dados) < 3:
            return vazio
        atr_atual = float(atr_local.iloc[-1])
        if not np.isfinite(atr_atual) or atr_atual <= 0:
            return vazio
        preco = float(dados["Close"].iloc[-1])
        for fim in range(len(dados) - 1, 1, -1):
            primeiro, terceiro = dados.iloc[fim - 2], dados.iloc[fim]
            if float(terceiro["Low"]) > float(primeiro["High"]):
                direcao, zona_inf, zona_sup = "buy", float(primeiro["High"]), float(terceiro["Low"])
            elif float(terceiro["High"]) < float(primeiro["Low"]):
                direcao, zona_inf, zona_sup = "sell", float(terceiro["High"]), float(primeiro["Low"])
            else:
                continue
            tamanho = zona_sup - zona_inf
            if tamanho < .15 * atr_atual:
                continue
            posteriores = dados.iloc[fim + 1:]
            preenchido = (
                (not posteriores.empty and float(posteriores["Low"].min()) <= zona_inf)
                if direcao == "buy" else
                (not posteriores.empty and float(posteriores["High"].max()) >= zona_sup)
            )
            if preenchido:
                continue
            em_zona = zona_inf <= preco <= zona_sup
            if direcao == "buy":
                distancia = max(0.0, preco - zona_sup) / atr_atual
            else:
                distancia = max(0.0, zona_inf - preco) / atr_atual
            estado = f"FVG {direcao.upper()} {'NA ZONA' if em_zona else 'ATIVO'}"
            return {
                "disponivel": True, "direcao": direcao, "estado": estado,
                "zona_inf": round(zona_inf, 6), "zona_sup": round(zona_sup, 6),
                "em_zona": em_zona, "tamanho_atr": round(tamanho / atr_atual, 2),
                "distancia_atr": round(distancia, 2),
                "inicio": str(dados.index[fim - 2]), "fim": str(dados.index[fim]),
                "motivo": (
                    "Preço está dentro do desequilíbrio; espere rejeição fechada antes de decidir."
                    if em_zona else "FVG ainda aberto; é uma zona possível de retorno, não uma entrada."
                ),
            }
    except (ValueError, TypeError, IndexError, KeyError):
        return vazio
    return vazio


def detectar_zonas_fvg_m15(df: pd.DataFrame, atr: pd.Series) -> dict:
    """Detecta o FVG buy mais recente E o FVG sell mais recente separadamente.

    Cada zona inclui `rejeicao_confirmada`: True quando a última vela testou
    a zona e fechou para fora na direção esperada.

    Retorna:
        {"buy_zone": {...}, "sell_zone": {...}}
        cada sub-dict tem os mesmos campos de detectar_fvg_m15 + rejeicao_confirmada.
    """
    _vz = {"disponivel": False, "estado": "SEM FVG", "rejeicao_confirmada": False,
            "motivo": "Nenhum desequilíbrio M15 relevante."}
    resultado = {"buy_zone": dict(_vz), "sell_zone": dict(_vz)}
    if len(df) < 3 or atr.empty:
        return resultado
    try:
        dados = df[["Open", "High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce").dropna().tail(64)
        atr_local = atr.reindex(dados.index).ffill()
        if len(dados) < 3:
            return resultado
        atr_atual = float(atr_local.iloc[-1])
        if not np.isfinite(atr_atual) or atr_atual <= 0:
            return resultado
        ultima = dados.iloc[-1]
        ult_o, ult_h, ult_l, ult_c = (float(ultima[c]) for c in ("Open", "High", "Low", "Close"))
        preco = ult_c
        buy_found = sell_found = False
        for fim in range(len(dados) - 1, 1, -1):
            if buy_found and sell_found:
                break
            primeiro, terceiro = dados.iloc[fim - 2], dados.iloc[fim]
            if float(terceiro["Low"]) > float(primeiro["High"]):
                direcao, zona_inf, zona_sup = "buy", float(primeiro["High"]), float(terceiro["Low"])
            elif float(terceiro["High"]) < float(primeiro["Low"]):
                direcao, zona_inf, zona_sup = "sell", float(terceiro["High"]), float(primeiro["Low"])
            else:
                continue
            if (direcao == "buy" and buy_found) or (direcao == "sell" and sell_found):
                continue
            tamanho = zona_sup - zona_inf
            if tamanho < .15 * atr_atual:
                continue
            posteriores = dados.iloc[fim + 1:]
            preenchido = (
                (not posteriores.empty and float(posteriores["Low"].min()) <= zona_inf)
                if direcao == "buy" else
                (not posteriores.empty and float(posteriores["High"].max()) >= zona_sup)
            )
            if preenchido:
                continue
            em_zona = zona_inf <= preco <= zona_sup
            distancia = (
                max(0.0, preco - zona_sup) / atr_atual if direcao == "buy"
                else max(0.0, zona_inf - preco) / atr_atual
            )
            rejeicao = (
                ult_l <= zona_sup and ult_c > ult_o and ult_c >= zona_sup
                if direcao == "buy" else
                ult_h >= zona_inf and ult_c < ult_o and ult_c <= zona_inf
            )
            estado = (f"FVG {direcao.upper()} — REJEIÇÃO ✓" if rejeicao
                      else f"FVG {direcao.upper()} NA ZONA" if em_zona
                      else f"FVG {direcao.upper()} ATIVO")
            zona_dict = {
                "disponivel": True, "direcao": direcao, "estado": estado,
                "zona_inf": round(zona_inf, 6), "zona_sup": round(zona_sup, 6),
                "em_zona": em_zona, "tamanho_atr": round(tamanho / atr_atual, 2),
                "distancia_atr": round(distancia, 2),
                "rejeicao_confirmada": rejeicao,
                "inicio": str(dados.index[fim - 2]), "fim": str(dados.index[fim]),
                "motivo": (
                    "Rejeição confirmada — candle fechou para fora da zona." if rejeicao else
                    "Preço dentro do desequilíbrio; aguardar rejeição fechada." if em_zona else
                    "FVG aberto; zona de interesse, sem confirmação ainda."
                ),
            }
            if direcao == "buy":
                resultado["buy_zone"] = zona_dict
                buy_found = True
            else:
                resultado["sell_zone"] = zona_dict
                sell_found = True
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    return resultado


def _order_block_m15(df: pd.DataFrame, atr: pd.Series, direcao: str) -> dict | None:
    """Última vela contrária antes de deslocamento, sem usar candles futuros."""
    dados = df[["Open", "High", "Low", "Close"]].dropna().tail(48)
    if len(dados) < 6:
        return None
    av = float(atr.reindex(dados.index).iloc[-1])
    if not np.isfinite(av) or av <= 0:
        return None
    for pos in range(len(dados) - 4, 0, -1):
        vela = dados.iloc[pos]
        o, h, l, c = (float(vela[x]) for x in ("Open", "High", "Low", "Close"))
        depois = dados.iloc[pos + 1:min(pos + 4, len(dados))]
        if direcao == "buy":
            oposta = c < o
            deslocou = not depois.empty and float(depois["High"].max()) - h >= .75 * av
        else:
            oposta = c > o
            deslocou = not depois.empty and l - float(depois["Low"].min()) >= .75 * av
        if oposta and deslocou:
            return {"zona_inf": round(l, 6), "zona_sup": round(h, 6),
                    "vela": str(dados.index[pos])}
    return None


def plano_confluencia_local(df: pd.DataFrame, atr: pd.Series, ativo: str,
                            noticia: dict | None = None) -> dict:
    """Leitura SMC local: estrutura, FVG, OB e liquidez; nunca envia ordem."""
    vazio = {"disponivel": False, "qualificada": False, "direcao": "neutro",
             "score": 0, "nota": "—", "estado": "AGUARDANDO ESTRUTURA",
             "motivo": "São necessários candles M15 suficientes."}
    if len(df) < 96 or atr.empty:
        return vazio
    try:
        dados = df[["Open", "High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce").dropna()
        av = float(atr.reindex(dados.index).ffill().iloc[-1])
        if not np.isfinite(av) or av <= 0:
            return {**vazio, "motivo": "ATR M15 indisponível."}
        m15, h1 = _tendencia_ema(dados["Close"]), _tendencia_h1_a_partir_m15(dados)
        fvg = detectar_fvg_m15(dados, atr.reindex(dados.index).ffill())
        vela = dados.iloc[-1]
        o, h, l, preco = (float(vela[x]) for x in ("Open", "High", "Low", "Close"))
        anteriores = dados.iloc[-11:-1]
        teto, piso = float(anteriores["High"].max()), float(anteriores["Low"].min())
        faixa_estrutura = dados.tail(48)
        minimo_estrutura, maximo_estrutura = float(faixa_estrutura["Low"].min()), float(faixa_estrutura["High"].max())
        equilibrio = (minimo_estrutura + maximo_estrutura) / 2
        estrutura = {
            "minimo": round(minimo_estrutura, 6), "maximo": round(maximo_estrutura, 6),
            "equilibrio": round(equilibrio, 6),
            "supply_inf": round(maximo_estrutura - .25 * av, 6), "supply_sup": round(maximo_estrutura, 6),
            "demand_inf": round(minimo_estrutura, 6), "demand_sup": round(minimo_estrutura + .25 * av, 6),
        }
        varreu_baixo = l < piso and preco > piso
        varreu_cima = h > teto and preco < teto
        bos_buy, bos_sell = preco > teto, preco < piso
        direcao = "buy" if m15 == h1 == "alta" else "sell" if m15 == h1 == "baixa" else "neutro"
        if direcao == "neutro" and fvg.get("disponivel"):
            direcao = str(fvg.get("direcao"))
        if direcao not in ("buy", "sell"):
            return {**vazio, "m15": m15, "h1": h1,
                    "motivo": "M15 e H1 divergentes; sem viés estrutural."}
        ob = _order_block_m15(dados, atr, direcao)
        fvg_ok = bool(fvg.get("disponivel") and fvg.get("direcao") == direcao)
        liquidez_ok = varreu_baixo if direcao == "buy" else varreu_cima
        bos_ok = bos_buy if direcao == "buy" else bos_sell
        rejeicao = ((preco > o and (min(o, preco) - l) >= abs(preco - o) * .5)
                    if direcao == "buy" else
                    (preco < o and (h - max(o, preco)) >= abs(preco - o) * .5))
        zona = (fvg if fvg_ok else ob) or {}
        zona_inf, zona_sup = zona.get("zona_inf"), zona.get("zona_sup")
        em_zona = (zona_inf is not None and float(zona_inf) <= preco <= float(zona_sup))
        itens = [
            ("Tendência H1 e M15 alinhadas", m15 == h1 and m15 in ("alta", "baixa"), 2),
            ("FVG M15 na direção", fvg_ok, 2),
            ("Order block antes do deslocamento", ob is not None, 2),
            ("Liquidez varrida e retomada", liquidez_ok, 1),
            ("BOS no lado do viés", bos_ok, 1),
            ("Preço dentro da zona", em_zona, 1),
            ("Vela de rejeição fechada", rejeicao, 1),
        ]
        score = sum(peso for _, ok, peso in itens if ok)
        nota = "A" if score >= 8 else "B" if score >= 6 else "C" if score >= 4 else "D"
        noticia_risco = (noticia or {}).get("estado") == "janela_risco"
        qualificada = bool(score >= 7 and em_zona and rejeicao and not noticia_risco)
        if zona_inf is None or zona_sup is None:
            return {
                "disponivel": True, "qualificada": False, "direcao": direcao,
                "score": score, "nota": nota, "estado": f"CONFLUÊNCIA {nota} — SEM ZONA",
                "motivo": "Viés identificado, mas falta FVG ou order block objetivo para planejar entrada.",
                "m15": m15, "h1": h1, "fvg": fvg, "order_block": ob,
                "estrutura": estrutura, "bos": bos_ok, "liquidez": liquidez_ok,
                "zona_inf": None, "zona_sup": None, "em_zona": False,
                "checklist": [{"nome": nome, "ok": ok} for nome, ok, _ in itens],
                "alvos": None,
            }
        entrada = (float(zona_inf) + float(zona_sup)) / 2 if zona_inf is not None else preco
        sinal = 1 if direcao == "buy" else -1
        sl_base = min(float(zona_inf), l) if direcao == "buy" else max(float(zona_sup), h)
        sl = sl_base - sinal * .20 * av
        risco = abs(entrada - sl)
        tp1 = entrada + sinal * max(1.5 * risco, av)
        tp2, tp3 = entrada + sinal * 2 * risco, entrada + sinal * 3 * risco
        estado = (f"CONFLUÊNCIA {nota} — ZONA + REJEIÇÃO" if qualificada else
                  f"CONFLUÊNCIA {nota} — AGUARDAR")
        if noticia_risco:
            motivo = "Notícia de alto impacto na janela: leitura permanece, entrada fica em espera."
        elif not em_zona:
            motivo = "Há estrutura, mas o preço ainda não voltou à zona; não perseguir o movimento."
        elif not rejeicao:
            motivo = "Preço chegou à zona; aguarde fechamento M15 rejeitando antes de decidir."
        else:
            motivo = "Confluências locais alinhadas. Estudo visual: confirme spread e contexto antes de operar."
        return {
            "disponivel": True, "qualificada": qualificada, "direcao": direcao,
            "score": score, "nota": nota, "estado": estado, "motivo": motivo,
            "m15": m15, "h1": h1, "fvg": fvg, "order_block": ob,
            "estrutura": estrutura, "bos": bos_ok, "liquidez": liquidez_ok,
            "zona_inf": round(float(zona_inf), 6) if zona_inf is not None else None,
            "zona_sup": round(float(zona_sup), 6) if zona_sup is not None else None,
            "em_zona": em_zona, "checklist": [{"nome": nome, "ok": ok} for nome, ok, _ in itens],
            "alvos": {"entrada": round(entrada, 6), "sl": round(sl, 6),
                       "tp1": round(tp1, 6), "tp2": round(tp2, 6), "tp3": round(tp3, 6),
                       "rr": round(abs(tp1 - entrada) / risco, 2) if risco else None},
        }
    except (ValueError, TypeError, IndexError, KeyError):
        return vazio


def plano_ouro_movimento(df: pd.DataFrame, atr: pd.Series, ativo: str,
                         noticia: dict | None = None) -> dict:
    """Scanner M15 de continuação do ouro: tendência, FVG, rejeição e perfil horário.

    Dois níveis de sinal — nenhum dispara ordem automaticamente:
      sinal_tecnico    – H1/M15 alinhados + FVG + rejeição fechada (sem notícia)
      sinal_confluente – sinal_tecnico + dado USD publicado e alinhado

    Alvos calibrados pelo perfil histórico de 25.976 velas XAUUSD M15:
      TP1 = deslocamento p50 da hora UTC atual (~50% das velas chegam)
      TP2 = deslocamento p75 da hora UTC atual (~25% das velas chegam)
      SL  = máx(sl técnico, excursão contra p50 da hora)
    """
    vazio = {"disponivel": False, "sinal_tecnico": False, "sinal_confluente": False,
             "sinal_estudo": False,  # mantido por compatibilidade
             "estado": "OURO — AGUARDANDO ESTRUTURA",
             "motivo": "Exclusivo para XAUUSD M15."}
    if ativo != "XAUUSD" or len(df) < 96 or atr.empty:
        return vazio
    try:
        m15, h1 = _tendencia_ema(df["Close"]), _tendencia_h1_a_partir_m15(df)
        fvg = detectar_fvg_m15(df, atr)
        zonas = detectar_zonas_fvg_m15(df, atr)
        atr_atual = float(atr.reindex(df.index).iloc[-1])
        if not np.isfinite(atr_atual) or atr_atual <= 0:
            return {**vazio, "motivo": "ATR M15 indisponível."}

        vela = df.iloc[-1]
        abertura, maxima, minima, fechamento = (float(vela[c]) for c in ("Open", "High", "Low", "Close"))
        hora_utc = pd.Timestamp(df.index[-1]).tz_localize("UTC").hour if pd.Timestamp(df.index[-1]).tzinfo is None else pd.Timestamp(df.index[-1]).hour

        # — perfil horário —
        ctx = _ctx_xauusd(hora_utc, fechamento)
        atr_pct = atr_atual / fechamento * 100 if fechamento > 0 else 0.0
        regime = _regime_atr_xauusd(atr_pct, hora_utc)

        direcao = "buy" if m15 == "alta" and h1 == "alta" else "sell" if m15 == "baixa" and h1 == "baixa" else "neutro"
        fvg_ok = bool(fvg.get("disponivel") and fvg.get("direcao") == direcao)
        zona_inf, zona_sup = fvg.get("zona_inf"), fvg.get("zona_sup")
        if fvg_ok:
            rejeicao = (
                minima <= float(zona_sup) and fechamento > abertura and fechamento >= float(zona_sup)
                if direcao == "buy" else
                maxima >= float(zona_inf) and fechamento < abertura and fechamento <= float(zona_inf)
            )
        else:
            rejeicao = False
        corpo_atr = abs(fechamento - abertura) / atr_atual
        impulso_ok = float(fvg.get("tamanho_atr") or 0) >= .15
        corpo_min = 0.30 if ctx["sessao"] == "ásia" else 0.20

        # — visitas ao FVG: zona testada > 1x antes da vela atual perde força —
        fvg_fresco = True
        if fvg.get("disponivel") and fvg.get("fim") and zona_inf is not None and zona_sup is not None:
            try:
                fvg_fim_ts = pd.Timestamp(fvg["fim"])
                mask = (df.index > fvg_fim_ts) & (df.index < df.index[-1])
                anteriores = df.loc[mask]
                if fvg.get("direcao") == "buy":
                    visitas_previas = int((anteriores["Low"] <= zona_sup).sum())
                else:
                    visitas_previas = int((anteriores["High"] >= zona_inf).sum())
                fvg_fresco = visitas_previas <= 1
            except Exception:
                fvg_fresco = True

        noticia_estado = (noticia or {}).get("estado")
        noticia_bruta = str((noticia or {}).get("direcao", "")).lower()
        direcao_noticia = {"call": "buy", "buy": "buy", "put": "sell", "sell": "sell"}.get(noticia_bruta)
        dado_publicado = noticia_estado == "resultado_publicado" and direcao_noticia in ("buy", "sell")
        noticia_alinhada = not dado_publicado or direcao_noticia == direcao

        regime_ok = regime == "normal"

        # — RR preliminar: SL técnico vs TP1 histórico da hora —
        rr_ok = True
        rr_prelim = None
        _sl_dist_prelim = 0.0
        if direcao != "neutro":
            _buf = .15 * atr_atual
            _sl_tec = minima - _buf if direcao == "buy" else maxima + _buf
            _sl_dist_prelim = max(abs(fechamento - _sl_tec), ctx["sl_min_pts"])
            rr_prelim = ctx["tp1_pts"] / _sl_dist_prelim if _sl_dist_prelim > 0 else 0.0
            rr_ok = rr_prelim >= 1.0

        sinal_tecnico = bool(
            direcao != "neutro" and fvg_ok and impulso_ok and fvg_fresco
            and rejeicao and corpo_atr >= corpo_min and regime_ok
            and rr_ok and noticia_estado != "janela_risco"
        )
        sinal_confluente = bool(sinal_tecnico and dado_publicado and noticia_alinhada)

        checklist = [
            {"nome": "Tendência H1 e M15 alinhadas", "ok": direcao != "neutro"},
            {"nome": "FVG M15 na direção da tendência", "ok": fvg_ok},
            {"nome": "FVG não sobretestado (≤ 1 visita prévia)", "ok": fvg_fresco},
            {"nome": "Candle fechou rejeitando a zona", "ok": rejeicao},
            {"nome": f"Candle de confirmação ≥ {int(corpo_min * 100)}% ATR ({ctx['sessao']})", "ok": corpo_atr >= corpo_min},
            {"nome": f"ATR normal para {hora_utc:02d}h UTC ({ctx['sessao']})", "ok": regime_ok},
            {"nome": "RR TP1 ≥ 1,0", "ok": rr_ok},
            {"nome": "Sem notícia USD pendente (janela_risco)", "ok": noticia_estado != "janela_risco"},
            {"nome": "Dado USD publicado e alinhado", "ok": sinal_confluente},
        ]

        # — alvos calibrados pelo perfil histórico —
        alvos = None
        rr_tp1 = rr_tp2 = None
        if direcao in ("buy", "sell"):
            sinal_dir = 1.0 if direcao == "buy" else -1.0
            sl_dist = _sl_dist_prelim
            sl = fechamento - sinal_dir * sl_dist
            tp1 = fechamento + sinal_dir * ctx["tp1_pts"]
            tp2 = fechamento + sinal_dir * ctx["tp2_pts"]
            rr_tp1 = round(ctx["tp1_pts"] / sl_dist, 2) if sl_dist > 0 else None
            rr_tp2 = round(ctx["tp2_pts"] / sl_dist, 2) if sl_dist > 0 else None
            passo, unidade = unidade_movimento(ativo)
            alvos = {
                "entrada": round(fechamento, 2),
                "sl": round(sl, 2),
                "tp1": round(tp1, 2),
                "tp2": round(tp2, 2),
                "sl_pts": round(sl_dist, 1),
                "tp1_pts": ctx["tp1_pts"],
                "tp2_pts": ctx["tp2_pts"],
                "rr_tp1": rr_tp1,
                "rr_tp2": rr_tp2,
                "risco_unidade": unidade,
            }

        # — estado descritivo —
        if noticia_estado == "janela_risco":
            estado = "NOTÍCIA USD — AGUARDAR DADO"
            motivo = "O número ainda não saiu; sem actual vs forecast não há direção objetiva."
        elif dado_publicado and not noticia_alinhada:
            estado = "NOTÍCIA USD CONTRA A ESTRUTURA"
            motivo = "Dado USD aponta lado oposto ao setup técnico; sem confluência."
        elif regime == "quieto":
            estado = "OURO — MERCADO PARADO"
            motivo = (f"ATR abaixo do mínimo esperado para {hora_utc:02d}h UTC "
                      f"({ctx['sessao']}). Amplitude mediana da hora: {ctx['amp_p50_pts']:.0f} pts.")
        elif regime == "ativo":
            estado = "OURO — VOLATILIDADE ANORMAL"
            motivo = (f"ATR muito acima do normal para {hora_utc:02d}h UTC. "
                      "Provável evento de notícia; aguardar acomodação.")
        elif direcao == "neutro":
            estado = "OURO — SEM TENDÊNCIA H1/M15"
            motivo = "Aguardar as duas leituras apontarem para o mesmo lado."
        elif not fvg_ok:
            estado = "OURO — AGUARDAR FVG"
            motivo = "Aguardar um FVG M15 alinhado com a tendência; não perseguir impulso."
        elif not fvg_fresco:
            estado = "OURO — FVG SOBRETESTADO"
            motivo = "A zona FVG foi testada mais de uma vez antes desta vela; magnetismo reduzido."
        elif not rejeicao:
            estado = "OURO — FVG ATIVO"
            motivo = "Preço precisa testar a zona e fechar com rejeição na direção."
        elif corpo_atr < corpo_min:
            estado = "OURO — REJEIÇÃO FRACA"
            motivo = (f"Corpo do candle ({corpo_atr:.0%} ATR) abaixo do mínimo "
                      f"para {ctx['sessao']} ({corpo_min:.0%} ATR); aguardar confirmação.")
        elif not rr_ok:
            estado = "OURO — RR INSUFICIENTE"
            motivo = (f"RR TP1 {rr_prelim:.2f} < 1,0; SL técnico ({_sl_dist_prelim:.0f} pts) "
                      f"largo demais para o TP1 histórico da hora ({ctx['tp1_pts']:.0f} pts).")
        elif sinal_confluente:
            estado = "OURO MOVIMENTO — CONFLUENTE"
            motivo = (f"Estrutura + dado USD alinhados. "
                      f"TP1={ctx['tp1_pts']:.0f} pts (p50 {hora_utc:02d}h), "
                      f"TP2={ctx['tp2_pts']:.0f} pts (p75 {hora_utc:02d}h). "
                      f"RR TP1={rr_tp1}.")
        elif sinal_tecnico:
            estado = "OURO MOVIMENTO — TÉCNICO"
            motivo = (f"Estrutura técnica completa. Aguardar dado USD para confluência. "
                      f"TP1={ctx['tp1_pts']:.0f} pts, TP2={ctx['tp2_pts']:.0f} pts ({hora_utc:02d}h UTC).")
        else:
            estado = "OURO MOVIMENTO — ESTUDO"
            motivo = "Condições parciais; registrar para aferição."

        return {
            "disponivel": True,
            "sinal_tecnico": sinal_tecnico,
            "sinal_confluente": sinal_confluente,
            "sinal_estudo": sinal_tecnico,  # compatibilidade com código existente
            "direcao": direcao,
            "estado": estado, "motivo": motivo,
            "fvg": fvg,
            "buy_zone": zonas["buy_zone"],
            "sell_zone": zonas["sell_zone"],
            "noticia_direcao": direcao_noticia,
            "zona_inf": zona_inf, "zona_sup": zona_sup,
            "corpo_atr": round(corpo_atr, 2),
            "rr": rr_tp1, "alvos": alvos,
            "perfil_horario": {
                "hora_utc": hora_utc,
                "sessao": ctx["sessao"],
                "regime_atr": regime,
                "amp_p50_pts": ctx["amp_p50_pts"],
                "tp1_pts": ctx["tp1_pts"],
                "tp2_pts": ctx["tp2_pts"],
                "sl_min_pts": ctx["sl_min_pts"],
            },
            "checklist": checklist,
        }
    except (ValueError, TypeError, IndexError, KeyError):
        return vazio


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


def plano_tp1_curto_forex(df: pd.DataFrame, atr: pd.Series, ativo: str,
                           noticia: dict | None = None) -> dict:
    """Plano curto de continuação: rejeição na zona e saída no Fib 38,2%.

    Recebe candles fechados, ATR, ativo e contexto de notícia; devolve o plano
    completo ou o motivo para aguardar. Reaproveita a estrutura do Fibo, mas
    reduz o alvo para a primeira fatia da retração. É estudo isolado: nunca
    promove ``entrada_valida``.
    """
    vazio = {
        "disponivel": False, "qualificada": False, "sinal_estudo": False,
        "estado": "TP1 CURTO INDISPONÍVEL",
        "motivo": "Disponível somente para Forex normal.",
    }
    if CLASSE.get(ativo) != "forex":
        return vazio
    fibo = plano_fibo(df, atr, ativo, noticia)
    if not fibo.get("disponivel"):
        return {**vazio, "estado": fibo.get("estado", vazio["estado"]),
                "motivo": fibo.get("motivo", vazio["motivo"])}

    direcao = fibo["direcao"]
    preco = float(df["Close"].iloc[-1])
    vela = df.iloc[-1]
    atr_atual = float(atr.iloc[-1])
    if not np.isfinite(atr_atual) or atr_atual <= 0:
        return {**vazio, "estado": "ATR INDISPONÍVEL"}
    margem = .15 * atr_atual
    # É um stop tático, além do pavio de rejeição. A invalidação estrutural em
    # 78,6% continua visível no plano Fibo, mas seria larga demais para TP1.
    if direcao == "buy":
        sl = float(vela["Low"]) - margem
        tp1 = float(fibo["fib382"])
        recompensa = tp1 - preco
    else:
        sl = float(vela["High"]) + margem
        tp1 = float(fibo["fib382"])
        recompensa = preco - tp1
    risco = abs(preco - sl)
    rr = recompensa / risco if risco > 0 else 0.0
    risco_atr = risco / atr_atual
    noticia_risco = (noticia or {}).get("estado") == "janela_risco"
    qualificada = bool(
        fibo.get("na_zona") and fibo.get("confirmou") and not noticia_risco
        and recompensa > 0 and 1.2 <= rr <= 3.0 and .10 <= risco_atr <= .85
    )
    if noticia_risco:
        estado, motivo = "NOTÍCIA — AGUARDAR", "Notícia relevante na janela de risco."
    elif not fibo.get("na_zona"):
        estado, motivo = "AGUARDAR ZONA", "Preço ainda não retornou à zona 38,2%–61,8%."
    elif not fibo.get("confirmou"):
        estado, motivo = "NA ZONA — ESPERAR REJEIÇÃO", "Falta candle fechado rejeitando a zona."
    elif recompensa <= 0:
        estado, motivo = "TP1 JÁ PASSOU", "O preço já percorreu a primeira fatia; não perseguir."
    elif not (1.2 <= rr <= 3.0):
        estado, motivo = "R:R CURTO FORA DA FAIXA", "TP1 não compensa o risco estrutural."
    elif not (.10 <= risco_atr <= .85):
        estado, motivo = "STOP FORA DO TAMANHO", "Distância até a invalidação não cabe numa operação curta."
    else:
        estado = f"TP1 CURTO {direcao.upper()} — ESTUDO"
        motivo = "Zona Fibo, rejeição confirmada e saída no 38,2% da retração."
    passo, unidade = unidade_movimento(ativo)
    arred = lambda valor: round(float(valor), 6)
    checklist = [
        {"nome": "Tendência H1 e M15 alinhadas", "ok": fibo.get("h1") == fibo.get("m15")},
        {"nome": "Preço dentro da zona 38,2%–61,8%", "ok": bool(fibo.get("na_zona"))},
        {"nome": "Rejeição fechada a favor", "ok": bool(fibo.get("confirmou"))},
        {"nome": "R:R curto entre 1,2 e 3,0", "ok": 1.2 <= rr <= 3.0},
        {"nome": "Stop entre 0,10 e 0,85 ATR", "ok": .10 <= risco_atr <= .85},
        {"nome": "Sem notícia de alto impacto", "ok": not noticia_risco},
    ]
    return {
        "disponivel": True, "direcao": direcao, "estado": estado,
        "motivo": motivo, "qualificada": qualificada,
        "sinal_estudo": qualificada, "rr": round(rr, 2),
        "risco_atr": round(risco_atr, 2),
        "zona_inf": fibo["zona_inf"], "zona_sup": fibo["zona_sup"],
        "checklist": checklist,
        "alvos": {
            "entrada": arred(preco), "sl": arred(sl), "tp1": arred(tp1),
            "risco_pips": round(risco / passo, 1), "risco_unidade": unidade,
            "modo_entrada": "market_next_open",
        },
    }


class AnalistaGraficoGroq:
    """Uma interface: solicita uma leitura assíncrona por candle qualificado.

    Mantém rate limit, serialização do gráfico e falhas da rede internos. O
    Monitor recebe somente o parecer pronto por callback, sem travar a coleta
    de candles nem tratar a resposta como autorização de ordem.
    """

    def __init__(self, intervalo_s: int = 900):
        self._intervalo_s = intervalo_s
        self._ultima: dict[str, float] = {}
        self._chave_candle: dict[str, str] = {}
        self._andamento: set[str] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="groq-grafico")

    @staticmethod
    def _resumir_candles(df: pd.DataFrame) -> list[dict]:
        """Últimas 12 velas fechadas: suficientes para contexto, sem prompt gigante."""
        saida = []
        for _, vela in df.tail(12).iterrows():
            saida.append({
                "o": round(float(vela["Open"]), 6), "h": round(float(vela["High"]), 6),
                "l": round(float(vela["Low"]), 6), "c": round(float(vela["Close"]), 6),
            })
        return saida

    def solicitar(self, ativo: str, df: pd.DataFrame, plano: dict, noticia: dict,
                  callback) -> None:
        if not os.getenv("GROQ_API_KEY", "").strip():
            callback(ativo, {"status": "DESATIVADA", "veredicto": "AGUARDAR",
                              "motivo": "Configure GROQ_API_KEY para ativar.", "fonte": "GROQ"})
            return
        vela = str(df.index[-1])
        agora = time.time()
        with self._lock:
            if (ativo in self._andamento or self._chave_candle.get(ativo) == vela
                    or agora - self._ultima.get(ativo, 0) < self._intervalo_s):
                return
            self._andamento.add(ativo)
            self._ultima[ativo] = agora
            self._chave_candle[ativo] = vela
        callback(ativo, {"status": "ANALISANDO", "veredicto": "AGUARDAR",
                         "motivo": "Groq lendo o candle fechado e a zona.", "fonte": "GROQ",
                         "vela": vela})
        contexto = {
            "ativo": ativo, "timeframe": "M15", "vela": vela,
            "candles_fechados": self._resumir_candles(df),
            "plano": {
                "qualificada": bool(plano.get("qualificada")),
                "direcao": str(plano.get("direcao", "")).upper(),
                "zona": [plano.get("zona_inf"), plano.get("zona_sup")],
                "entrada": (plano.get("alvos") or {}).get("entrada"),
                "sl": (plano.get("alvos") or {}).get("sl"),
                "tp1": (plano.get("alvos") or {}).get("tp1"),
                "rr": plano.get("rr"), "risco_atr": plano.get("risco_atr"),
            },
            "noticia": {"estado": noticia.get("estado"), "texto": noticia.get("texto")},
        }
        self._pool.submit(self._executar, ativo, vela, contexto, callback)

    def _executar(self, ativo: str, vela: str, contexto: dict, callback) -> None:
        try:
            resposta = segunda_opiniao_grafico(contexto)
            callback(ativo, resposta or {
                "status": "INDISPONÍVEL", "veredicto": "AGUARDAR",
                "motivo": "Groq não respondeu; use somente a leitura mecânica.",
                "fonte": "GROQ", "vela": vela,
            })
        finally:
            with self._lock:
                self._andamento.discard(ativo)


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


def plano_bof_m30_m5(df_m5: pd.DataFrame, atr_m5: pd.Series, ativo: str,
                     noticia: dict | None = None) -> dict:
    """Breakout Failure M30→M5 para Forex normal e ouro, sempre em sombra.

    Os pivôs M30 usam duas velas fechadas de cada lado. O sinal nasce somente
    quando um M5 varre o pivô, fecha de volta no range e outro M5 confirma a
    reversão. Assim, nenhum extremo futuro participa da decisão.
    """
    vazio = {
        "disponivel": False, "sinal_estudo": False, "candidato": False,
        "direcao": "neutro", "etapa": "SEM DADOS M5",
        "motivo": "BOF é observado somente em Forex normal e XAUUSD.",
    }
    if CLASSE.get(ativo) not in ("forex", "ouro"):
        return vazio
    if len(df_m5) < 72 or atr_m5.empty:
        return {**vazio, "motivo": "Aguardando pelo menos 6 horas de candles M5."}

    colunas = ["Open", "High", "Low", "Close"]
    if any(c not in df_m5 for c in colunas):
        return {**vazio, "motivo": "Candles M5 incompletos."}
    base = df_m5[colunas].dropna().sort_index()
    if len(base) < 72:
        return {**vazio, "motivo": "Histórico M5 válido ainda insuficiente."}

    contagem = base["Close"].resample("30min").count()
    m30 = base.resample("30min").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last",
    })
    # Um bloco M30 parcial jamais vira nível estrutural.
    m30 = m30[contagem.reindex(m30.index).eq(6)].dropna()
    if len(m30) < 7:
        return {**vazio, "motivo": "Aguardando pivôs M30 confirmados."}

    pivos: list[dict] = []
    for i in range(2, len(m30) - 2):
        janela = m30.iloc[i - 2:i + 3]
        atual = m30.iloc[i]
        confirmado_em = m30.index[i + 2] + pd.Timedelta(minutes=30)
        if float(atual["High"]) > float(janela.drop(janela.index[2])["High"].max()):
            pivos.append({"lado": "high", "nivel": float(atual["High"]),
                          "pivo_em": m30.index[i], "confirmado_em": confirmado_em})
        if float(atual["Low"]) < float(janela.drop(janela.index[2])["Low"].min()):
            pivos.append({"lado": "low", "nivel": float(atual["Low"]),
                          "pivo_em": m30.index[i], "confirmado_em": confirmado_em})
    if not pivos:
        return {**vazio, "motivo": "Nenhuma máxima/mínima M30 confirmou pivô 2+2."}

    av = float(atr_m5.reindex(base.index).iloc[-1])
    if not np.isfinite(av) or av <= 0:
        return {**vazio, "motivo": "ATR M5 indisponível."}
    ultimo_fechamento = base.index[-1] + pd.Timedelta(minutes=5)
    candidatos: list[dict] = []

    # No máximo duas velas após a varredura para retornar ao range, seguidas
    # por uma confirmação. Olhar só as quatro últimas mantém o evento local.
    inicio_busca = max(0, len(base) - 4)
    for pivo in pivos:
        if pivo["confirmado_em"] > ultimo_fechamento:
            continue
        nivel = pivo["nivel"]
        for pos_sweep in range(inicio_busca, len(base)):
            sweep = base.iloc[pos_sweep]
            sweep_em = base.index[pos_sweep]
            if pivo["confirmado_em"] > sweep_em:
                continue
            extensao = ((float(sweep["High"]) - nivel) if pivo["lado"] == "high"
                        else (nivel - float(sweep["Low"])))
            if not (.10 * av <= extensao <= .75 * av):
                continue
            limite_reclaim = min(len(base) - 1, pos_sweep + 2)
            pos_reclaim = None
            for k in range(pos_sweep, limite_reclaim + 1):
                fechamento = float(base.iloc[k]["Close"])
                voltou = fechamento < nivel if pivo["lado"] == "high" else fechamento > nivel
                if voltou:
                    pos_reclaim = k
                    break
            etapa = "VARREU M30 — AGUARDAR RETORNO"
            confirmou = False
            pos_confirmacao = None
            if pos_reclaim is not None:
                etapa = "VOLTOU AO RANGE — AGUARDAR M5"
                if pos_reclaim + 1 < len(base):
                    conf = base.iloc[pos_reclaim + 1]
                    reclaim = base.iloc[pos_reclaim]
                    confirmou = (
                        float(conf["Close"]) < float(reclaim["Low"])
                        if pivo["lado"] == "high"
                        else float(conf["Close"]) > float(reclaim["High"])
                    )
                    if confirmou:
                        pos_confirmacao = pos_reclaim + 1
                        etapa = "CONFIRMOU M5"
                    else:
                        # A confirmação pertence obrigatoriamente ao M5
                        # seguinte. Se ele fechou sem romper, a tentativa acabou.
                        continue
            if confirmou and pos_confirmacao != len(base) - 1:
                continue
            candidatos.append({
                "pivo": pivo, "sweep": sweep, "sweep_em": sweep_em,
                "pos_sweep": pos_sweep, "pos_reclaim": pos_reclaim,
                "pos_confirmacao": pos_confirmacao, "confirmou": confirmou,
                "etapa": etapa, "extensao_atr": extensao / av,
            })

    if not candidatos:
        recente = max(pivos, key=lambda x: x["confirmado_em"])
        passo, _ = unidade_movimento(ativo)
        casas_preco = 3 if passo >= .01 else 5
        return {
            **vazio, "disponivel": True, "etapa": "NÍVEL M30",
            "motivo": "Nível confirmado; aguardando uma varredura curta no M5.",
            "nivel_m30": round(recente["nivel"], casas_preco),
            "lado_nivel": recente["lado"],
            "pivo_em": str(recente["pivo_em"]),
        }

    # Confirmação tem prioridade; depois, o evento mais recente.
    cand = max(candidatos, key=lambda x: (x["confirmou"], x["sweep_em"]))
    pivo, sweep = cand["pivo"], cand["sweep"]
    direcao = "sell" if pivo["lado"] == "high" else "buy"
    confirmado = bool(cand["confirmou"] and cand["pos_confirmacao"] == len(base) - 1)
    entrada = float(base.iloc[-1]["Close"])
    buffer = .10 * av
    sl = (float(sweep["High"]) + buffer if direcao == "sell"
          else float(sweep["Low"]) - buffer)
    risco = abs(entrada - sl)

    # O alvo precisa existir e estar confirmado antes da varredura; escolher o
    # mais próximo evita chamar retrospectivamente qualquer extremo de "alvo".
    alvos_pivo = [x["nivel"] for x in pivos
                  if x["confirmado_em"] <= cand["sweep_em"]
                  and ((direcao == "sell" and x["lado"] == "low" and x["nivel"] < entrada)
                       or (direcao == "buy" and x["lado"] == "high" and x["nivel"] > entrada))]
    tp = (max(alvos_pivo) if direcao == "sell" and alvos_pivo
          else min(alvos_pivo) if direcao == "buy" and alvos_pivo else None)
    recompensa = abs(tp - entrada) if tp is not None else 0.0
    rr = recompensa / risco if risco > 0 else 0.0
    noticia_risco = (noticia or {}).get("estado") == "janela_risco"
    elegivel = bool(confirmado and tp is not None and rr >= 5.0 and not noticia_risco)
    if noticia_risco and confirmado:
        etapa, motivo = "NOTÍCIA — BLOQUEADO", "BOF confirmou dentro da janela de notícia."
    elif elegivel:
        etapa, motivo = "ELEGÍVEL — SOMBRA", "Falha M30 confirmou no M5 e o alvo preexistente oferece pelo menos 5R."
    elif confirmado and tp is None:
        etapa, motivo = "SEM ALVO M30", "Confirmação ocorreu, mas não há pivô M30 preexistente como alvo."
    elif confirmado:
        etapa, motivo = "R:R INSUFICIENTE", f"Confirmação ocorreu, mas o alvo oferece apenas {rr:.2f}R."
    else:
        etapa, motivo = cand["etapa"], "Varredura detectada; a sequência ainda não confirmou uma reversão M5."

    passo, unidade = unidade_movimento(ativo)
    casas_preco = 3 if passo >= .01 else 5
    arred = lambda valor: round(float(valor), casas_preco) if valor is not None else None
    checklist = [
        {"nome": "Pivô M30 confirmado sem olhar o futuro", "ok": True},
        {"nome": "Varredura entre 0.10 e 0.75 ATR", "ok": True},
        {"nome": "Fechamento voltou ao range em até 2 M5", "ok": cand["pos_reclaim"] is not None},
        {"nome": "M5 seguinte rompeu a vela de retorno", "ok": confirmado},
        {"nome": "Alvo M30 preexistente oferece ≥ 5R", "ok": tp is not None and rr >= 5.0},
        {"nome": "Fora da janela de notícia", "ok": not noticia_risco},
    ]
    return {
        "disponivel": True, "sinal_estudo": elegivel, "candidato": True,
        "direcao": direcao, "etapa": etapa, "estado": etapa, "motivo": motivo,
        "nivel_m30": arred(pivo["nivel"]), "lado_nivel": pivo["lado"],
        "pivo_em": str(pivo["pivo_em"]), "varredura_em": str(cand["sweep_em"]),
        "extremo_varredura": arred(float(sweep["High"] if direcao == "sell" else sweep["Low"])),
        "extensao_atr": round(cand["extensao_atr"], 2), "rr": round(rr, 2),
        "checklist": checklist,
        "alvos": ({"entrada": arred(entrada), "sl": arred(sl), "tp1": arred(tp),
                    "risco_pips": round(risco / passo, 1), "risco_unidade": unidade,
                    "modo_entrada": "market_next_open"} if tp is not None else None),
    }


def _ic_media(valores: list[float], z: float = 1.96) -> list[float] | None:
    """IC 95% da media, pelo erro padrao. None quando a amostra nao sustenta."""
    n = len(valores)
    if n < 3:
        return None
    media = sum(valores) / n
    var = sum((v - media) ** 2 for v in valores) / (n - 1)
    erro = (var / n) ** 0.5
    return [round(media - z * erro, 3), round(media + z * erro, 3)]


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


def _numero_do_texto(valor) -> float | None:
    """'3.2%' -> 3.2 ; '-1.4K' -> -1.4 ; 'n/d' -> None."""
    if valor in (None, ""):
        return None
    limpo = str(valor).strip().replace("%", "").replace(",", "")
    for sufixo in ("K", "M", "B", "T"):
        if limpo.upper().endswith(sufixo):
            limpo = limpo[:-1]
            break
    try:
        return float(limpo)
    except ValueError:
        return None


def _desvio_do_previsto(actual, forecast) -> float | None:
    """Surpresa em % do previsto. Forecast zero não tem percentual definido."""
    a, f = _numero_do_texto(actual), _numero_do_texto(forecast)
    if a is None or f is None or f == 0:
        return None
    return round((a - f) / abs(f) * 100, 1)


def _leitura_ia_resultado(confirmado: dict) -> dict | None:
    """Frase da IA sobre o mecanismo. Falha em silêncio: é texto de apoio."""
    try:
        from iqoption_m5.ia import ler_resultado_noticia
        return ler_resultado_noticia(
            confirmado.get("titulo", ""), confirmado.get("moeda", ""),
            confirmado.get("actual"), confirmado.get("forecast"),
            confirmado.get("previous"),
        )
    except Exception:
        return None


def _plano_rompimento_reteste_sombra(ativo: str, df: pd.DataFrame,
                                     atr_atual: float | None) -> dict | None:
    """Plano do forex traduzido para o formato de estudo do monitor.

    Entrada a mercado no fechamento da vela que confirmou — sem modo_entrada,
    o simulador ja trata como preenchida, que e a semantica certa: a
    estrategia diz que a entrada real pertence a abertura seguinte.
    """
    try:
        from iqoption_m5.forex_estrategia import plano_rompimento_reteste
        plano = plano_rompimento_reteste(ativo, df)
    except Exception:
        return None
    if plano is None:
        return None
    try:
        entrada = float(df.iloc[-1]["Close"])
        risco = float(plano.risco_preco)
        if risco <= 0:
            return None
        passo, unidade = unidade_movimento(ativo)
        return {
            "direcao": "buy" if plano.lado == "buy" else "sell",
            "motivo": plano.motivo,
            "risco_atr": (round(risco / atr_atual, 2)
                          if atr_atual and atr_atual > 0 else None),
            "checklist": [
                {"nome": "Rompimento com corpo entre 0.6 e 2.0 ATR", "ok": True},
                {"nome": "Reteste do nível com fechamento de volta", "ok": True},
                {"nome": "EMA20/EMA50 alinhadas", "ok": True},
                {"nome": "Risco entre 0.60 e 1.80 ATR", "ok": True},
            ],
            "alvos": {
                "entrada": round(entrada, 6),
                "sl": round(float(plano.stop), 6),
                "tp1": round(float(plano.alvo), 6),
                "risco_pips": round(risco / passo, 1),
                "risco_unidade": unidade,
                "nivel_rompido": round(float(plano.nivel), 6),
            },
        }
    except Exception:
        return None


def contexto_noticia(calendario: CalendarioEconomico | None, ativo: str,
                      agora: datetime) -> dict:
    """Salva um fato do calendário; nunca deriva direção sem dado publicado."""
    if calendario is None:
        return {"estado": "sem_calendario", "texto": "Calendário não disponível."}
    confirmado = calendario.confirmacao_recente(ativo, agora)
    if confirmado:
        saida = {
            "estado": "resultado_publicado", "texto": confirmado["titulo"],
            "moeda": confirmado["moeda"], "actual": confirmado["actual"],
            "forecast": confirmado["forecast"], "direcao": confirmado["direcao"],
        }
        # Desvio pela conta, não pela IA: quando os dois lados são numéricos
        # isto é aritmética e não precisa de modelo nenhum.
        desvio = _desvio_do_previsto(confirmado.get("actual"), confirmado.get("forecast"))
        if desvio is not None:
            saida["desvio_pct"] = desvio
        # A IA entra só para o texto do mecanismo. A direção acima continua
        # vindo de resultado_direcao(), que sabe de que lado do par a moeda está.
        leitura = _leitura_ia_resultado(confirmado)
        if leitura:
            saida["surpresa"] = leitura["surpresa"]
            saida["leitura_ia"] = leitura["leitura"]
        return saida
    aviso = calendario.aviso(ativo, agora)
    return {
        "estado": "janela_risco" if aviso and not aviso.startswith("próximo:") else "sem_risco",
        "texto": aviso or "Sem evento relevante na janela de risco.",
    }


def noticias_do_dia(calendario: CalendarioEconomico | None, ativo: str,
                    agora: datetime) -> list[dict]:
    """Agenda serializável do dia para o gráfico, incluindo o sentido pós-dado.

    Antes da divulgação, a direção é condicional (acima/abaixo do previsto).
    Após existir actual, a direção vem de ``resultado_direcao`` — não da IA.
    """
    if calendario is None:
        return []
    try:
        saida = []
        for evento in calendario.eventos_do_ativo(ativo):
            if evento.quando.date() != agora.date():
                continue
            resultado = evento.resultado_direcao(ativo) if evento.actual is not None else None
            sugestao = evento.sugestao(ativo) if evento.actual is None else None
            if resultado:
                uso = f"Dado publicado: {resultado['direcao']} somente após confirmação M15."
                direcao = resultado["direcao"]
            elif sugestao:
                uso = (f"Se acima do previsto: {sugestao['acima_do_forecast']}; "
                       f"se abaixo: {sugestao['abaixo_do_forecast']}. Espere o actual.")
                direcao = None
            else:
                uso, direcao = "Sem regra objetiva para direção; espere reação M15.", None
            saida.append({
                "titulo": evento.titulo, "moeda": evento.moeda, "impacto": evento.impacto,
                "quando": evento.quando.isoformat(), "quando_ts": int(evento.quando.timestamp()),
                "actual": evento.actual, "forecast": evento.forecast, "previous": evento.previous,
                "direcao": direcao, "uso": uso,
            })
        return sorted(saida, key=lambda item: item["quando_ts"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return []


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

    def atualizar_ia(self, ativo: str, parecer: dict) -> None:
        """Atualiza somente a segunda opinião sem apagar a leitura do ativo."""
        with self._lock:
            atual = dict(self.dados.get(ativo) or self._ativo_aguardando(ativo))
            atual["ia_groq"] = dict(parecer)
            self.dados[ativo] = atual

    def registrar_sinal(self, ativo: str, vela: str, info: dict,
                        tipo: str = "falso_rompimento") -> bool:
        """Salva um sinal novo e informa se ele foi realmente persistido."""
        chave = f"{tipo}:{ativo}:{vela}"
        with self._lock:
            if chave in self._vistos:
                return False
            self._vistos.add(chave)
            timeframe_s = int(info.get("timeframe_segundos") or TF)
            sem_simulacao = bool(info.get("sem_simulacao"))
            item = {
                "schema_versao": SCHEMA_VERSAO, "origem": "monitor_mercado",
                "modo": "entrada_validada" if info.get("entrada_valida") else "estudo",
                "timeframe": timeframe_s, "timeframe_segundos": timeframe_s,
                "horizonte_velas": info.get("horizonte_velas"),
                "horizonte_longo_velas": info.get("horizonte_longo_velas"),
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
                "ouro_movimento": info.get("ouro_movimento"),
                "fluxo": info.get("fluxo"),
                "orb": info.get("orb"),
                "bof": info.get("bof"),
                "resultado": None,
                "simulacao": {
                    "versao": SIMULADOR_VERSAO,
                    "desfecho": "nao_elegivel" if sem_simulacao else "aguardando",
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
            return True

    def resolver(self, ativo: str, df: pd.DataFrame,
                 timeframe_s: int = TF) -> None:
        """Registra reação e simulação TP1/SL; não é resultado de ordem real."""
        with self._lock:
            pend = []
            for h in self._aprendizado:
                if h.get("ativo") != ativo:
                    continue
                if h.get("tipo") == "bof_m30_m5_candidato":
                    continue
                if int(h.get("timeframe_segundos") or h.get("timeframe") or TF) != timeframe_s:
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
                # Sinais gravados antes da medicao paralela ainda nao tem a
                # chave longa; reprocessa uma vez para preenche-la.
                longa = h.get(CHAVE_SIM_LONGA) or {}
                sem_longa = not longa or longa.get("desfecho") in (None, "aguardando")
                if (desfecho in ("aguardando", "sem_dados")
                        or expiracao_prematura or precisa_resultado_r
                        or versao_antiga or sem_longa):
                    pend.append(h)
        alterou = False
        for h in pend:
            try:
                ts = pd.Timestamp(h["vela"]) + pd.Timedelta(seconds=timeframe_s)
                if ts not in df.index:
                    continue
                lin = df.loc[ts]
                o, c = float(lin["Open"]), float(lin["Close"])
                reacao = "subiu" if c > o else ("caiu" if c < o else "igual")
                horizonte = int(h.get("horizonte_velas") or HORIZONTE_VELAS)
                horizonte_longo = int(h.get("horizonte_longo_velas") or HORIZONTE_LONGO_VELAS)
                simulacao = self._resolver_tp_sl(h, df, horizonte_velas=horizonte)
                # Horizonte longo em paralelo, em chave propria: o oficial
                # acima nao muda, entao a amostra ja acumulada segue valida.
                simulacao_longa = self._resolver_tp_sl(
                    h, df, horizonte_velas=horizonte_longo
                )
                with self._lock:
                    h["resultado"] = reacao
                    passo, unidade = unidade_movimento(ativo)
                    h["var_pips"] = round((c - o) / passo, 1)
                    h["var_unidade"] = unidade
                    h["simulacao"] = simulacao
                    h[CHAVE_SIM_LONGA] = simulacao_longa
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
    def _resolver_tp_sl(h: dict, df: pd.DataFrame,
                        horizonte_velas: int = HORIZONTE_VELAS) -> dict:
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
        ordem_pendente = modo_entrada in ("limite", "stop", "market_next_open") or h.get("tipo") == "fibo_m15"
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
                if modo_entrada == "market_next_open":
                    entrada = float(vela["Open"])
                    tocou_entrada = True
                elif modo_entrada == "stop":
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
            if acabou_de_preencher and modo_entrada != "market_next_open" and tocou_tp:
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
                "desfecho": f"nao_executada_{_rotulo_horas(horizonte_velas, int(h.get('timeframe_segundos') or h.get('timeframe') or TF))}", "velas": len(futuras),
                "bateu_tp1_sem_stop": None, "primeiro_toque": None,
                "vela_desfecho": str(futuras.index[-1]),
                "preco_saida": None, "resultado_r": None,
                "preenchida": False, "vela_preenchimento": None,
            }
        preco_saida = float(futuras.iloc[-1]["Close"])
        return {
            "versao": SIMULADOR_VERSAO,
            "desfecho": f"expirado_{_rotulo_horas(horizonte_velas, int(h.get('timeframe_segundos') or h.get('timeframe') or TF))}", "velas": len(futuras),
            "bateu_tp1_sem_stop": None, "primeiro_toque": None,
            "vela_desfecho": str(futuras.index[-1]),
            "preco_saida": preco_saida, "resultado_r": resultado_r(preco_saida),
            "preenchida": preenchida,
            "vela_preenchimento": vela_preenchimento,
        }

    @staticmethod
    def _resumo_simulacoes(itens: list[dict], chave: str = "simulacao") -> dict:
        desfechos = [str((h.get(chave) or {}).get("desfecho", "aguardando")) for h in itens]
        wins, losses = desfechos.count("win_tp1"), desfechos.count("loss_sl")
        resolvidos = wins + losses
        resultados_r = [
            float((h.get(chave) or {}).get("resultado_r"))
            for h in itens
            if (h.get(chave) or {}).get("resultado_r") is not None
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
            "expirados": sum(d.startswith("expirado_") for d in desfechos),
            "pendentes": desfechos.count("aguardando"),
            "ambiguos": sum(d.startswith("ambíguo") for d in desfechos),
            "nao_executadas": sum(d.startswith("nao_executada_") for d in desfechos),
            "winrate": round(wins * 100 / resolvidos, 1) if resolvidos else None,
            "ic_95": list(ic) if ic else None,
            "amostra_suficiente": resolvidos >= 30,
            "maturidade": maturidade,
            "avaliados_r": len(resultados_r),
            "saldo_r": round(sum(resultados_r), 3) if resultados_r else None,
            "media_r": round(sum(resultados_r) / len(resultados_r), 3) if resultados_r else None,
            # IC do R medio. O painel decide o peso do aviso por aqui: estudo
            # com intervalo acima de zero avisa alto, o que cruza zero avisa
            # baixo, o que esta abaixo fica mudo. O alerta segue a evidencia
            # sozinho, sem lista fixa para manter na mao.
            "media_r_ic": _ic_media(resultados_r),
        }

    def _amostra_entrada_validada(self) -> dict:
        """Resumo pequeno e honesto da amostra elegível do monitor."""
        validos = [
            h for h in self._aprendizado
            if (h.get("tipo", "falso_rompimento") == "falso_rompimento"
                and h.get("entrada_valida", h.get("janela_boa", False)))
        ]
        return self._resumo_simulacoes(validos)

    def _amostra_por_tipo(self, tipo: str) -> dict:
        """Resumo do horizonte oficial, com o longo anexado para comparação.

        Os dois nunca são somados: horizontes diferentes são métodos
        diferentes. Ficam lado a lado só para decidir, com dado, se vale
        trocar o oficial.
        """
        itens = [h for h in self._aprendizado if h.get("tipo") == tipo]
        primeiro = itens[0] if itens else {}
        timeframe_s = int(primeiro.get("timeframe_segundos") or primeiro.get("timeframe") or TF)
        horizonte = int(primeiro.get("horizonte_velas") or HORIZONTE_VELAS)
        horizonte_longo = int(primeiro.get("horizonte_longo_velas") or HORIZONTE_LONGO_VELAS)
        resumo = self._resumo_simulacoes(itens)
        resumo["horizonte"] = _rotulo_horas(horizonte, timeframe_s)
        longo = self._resumo_simulacoes(itens, CHAVE_SIM_LONGA)
        longo["horizonte"] = _rotulo_horas(horizonte_longo, timeframe_s)
        resumo["comparacao_longa"] = longo
        return resumo

    def _amostra_fibo(self) -> dict:
        """Amostra separada: Fibo não pode contaminar o sinal já validado."""
        return self._amostra_por_tipo("fibo_m15")

    def _amostra_tp1_curto(self) -> dict:
        """TP1 curto é medido isoladamente antes de qualquer recomendação."""
        return self._amostra_por_tipo("tp1_curto_forex")

    def _amostra_fluxo(self) -> dict:
        """Amostra isolada da leitura de fluxo; nunca vira entrada validada."""
        return self._amostra_por_tipo("fluxo_m15")

    def _amostra_orb(self) -> dict:
        """Amostra isolada do ORB/FVG adaptado ao M15."""
        return self._amostra_por_tipo("orb_fvg_m15")

    def _amostra_liquidez(self) -> dict:
        """Amostra isolada da varredura v2; permanece sempre em sombra."""
        return self._amostra_por_tipo("liquidity_sweep_v2")

    def _amostra_rompimento_reteste(self) -> dict:
        """Rompimento+reteste do forex, observado aqui por falta de amostra."""
        return self._amostra_por_tipo("rompimento_reteste")

    def _amostra_bof(self) -> dict:
        """BOF M30→M5 de Forex/ouro, isolado dos demais rompimentos."""
        return self._amostra_por_tipo("bof_m30_m5")

    def registrar_direcao_noticia(self, ativo: str, vela: str, preco: float,
                                  noticia: dict) -> None:
        """Guarda a direção mecânica para conferir depois se ela acertou.

        Não é TP/SL: é uma aposta direcional simples, medida pelo fechamento
        `HORIZONTE_NOTICIA_VELAS` velas adiante. Fica em estrutura própria
        para nunca ser somada às amostras dos estudos.
        """
        if noticia.get("estado") != "resultado_publicado":
            return
        direcao = noticia.get("direcao")
        if direcao not in ("CALL", "PUT"):
            return
        chave = f"direcao_noticia:{ativo}:{noticia.get('texto')}:{vela}"
        with self._lock:
            if chave in self._vistos:
                return
            self._vistos.add(chave)
            self._aprendizado.append({
                "schema_versao": SCHEMA_VERSAO, "origem": "monitor_mercado",
                "tipo": "direcao_noticia", "id": chave,
                "ativo": ativo, "vela": vela,
                "quando": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "evento": noticia.get("texto"), "moeda": noticia.get("moeda"),
                "actual": noticia.get("actual"), "forecast": noticia.get("forecast"),
                "desvio_pct": noticia.get("desvio_pct"),
                "direcao_prevista": direcao,
                "preco_publicacao": float(preco),
                "horizonte_velas": HORIZONTE_NOTICIA_VELAS,
                "afericao": {"estado": "aguardando", "preco_final": None,
                             "acertou": None, "var_pips": None},
            })
            self._salvar_aprendizado()

    def resolver_direcoes_noticia(self, ativo: str, df: pd.DataFrame) -> None:
        """Confere a direção prevista pelo fechamento do horizonte."""
        alterou = False
        with self._lock:
            pendentes = [
                h for h in self._aprendizado
                if h.get("tipo") == "direcao_noticia" and h.get("ativo") == ativo
                and (h.get("afericao") or {}).get("estado") == "aguardando"
            ]
        for h in pendentes:
            try:
                velas = int(h.get("horizonte_velas") or HORIZONTE_NOTICIA_VELAS)
                alvo = pd.Timestamp(h["vela"]) + pd.Timedelta(seconds=TF * velas)
                if alvo not in df.index:
                    continue
                final = float(df.loc[alvo]["Close"])
                inicial = float(h["preco_publicacao"])
                passo, _ = unidade_movimento(ativo)
                if final == inicial:
                    # Empate não vira erro: resultado indefinido não é perda.
                    estado, acertou = "empate", None
                else:
                    subiu = final > inicial
                    acertou = subiu if h["direcao_prevista"] == "CALL" else not subiu
                    estado = "aferido"
                with self._lock:
                    h["afericao"] = {
                        "estado": estado, "preco_final": final,
                        "acertou": acertou,
                        "var_pips": round((final - inicial) / passo, 1),
                        "vela_final": str(alvo),
                    }
                alterou = True
            except Exception:
                continue
        if alterou:
            with self._lock:
                self._salvar_aprendizado()

    def _amostra_direcao_noticia(self) -> dict:
        """Acerto da direção mecânica. Separado de tudo: método próprio."""
        itens = [h for h in self._aprendizado if h.get("tipo") == "direcao_noticia"]
        afs = [(h.get("afericao") or {}) for h in itens]
        acertos = sum(1 for a in afs if a.get("acertou") is True)
        erros = sum(1 for a in afs if a.get("acertou") is False)
        n = acertos + erros
        ic = wilson_ci(acertos, n)
        return {
            "sinais": len(itens), "acertos": acertos, "erros": erros,
            "empates": sum(1 for a in afs if a.get("estado") == "empate"),
            "pendentes": sum(1 for a in afs if a.get("estado") == "aguardando"),
            "acerto_pct": round(acertos * 100 / n, 1) if n else None,
            "ic_95": list(ic) if ic else None,
            "amostra_suficiente": n >= 30,
            "maturidade": ("INSUFICIENTE" if n < 30 else "OBSERVAR" if n < 100
                           else "CANDIDATA" if n < 300 else "APROVADA"),
            "horizonte": _rotulo_horas(HORIZONTE_NOTICIA_VELAS),
        }

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
                       "amostraTp1Curto": self._amostra_tp1_curto(),
                       "amostraFluxo": self._amostra_fluxo(),
                       "amostraOrb": self._amostra_orb(),
                       "amostraLiquidez": self._amostra_liquidez(),
                       "amostraRompimentoReteste": self._amostra_rompimento_reteste(),
                       "amostraBof": self._amostra_bof(),
                       "amostraDirecaoNoticia": self._amostra_direcao_noticia()}
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


def loop(api, cfg, estado: Estado, calendario: CalendarioEconomico | None = None,
         analista_groq: AnalistaGraficoGroq | None = None) -> None:
    hist: dict[str, pd.DataFrame] = {}
    cfg_bof = replace(cfg, timeframe_segundos=TF_BOF)
    hist_bof: dict[str, pd.DataFrame] = {}
    ultimo_bucket_bof: dict[str, int] = {}

    def receber_ia(ativo: str, parecer: dict) -> None:
        estado.atualizar_ia(ativo, parecer)
        estado.salvar()
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
        df = sanitizar_candles_m15(df)
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
                novo = sanitizar_candles_m15(novo)
                if novo.empty:
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
                noticias_dia = noticias_do_dia(calendario, a, agora)
                fibo = plano_fibo(df, atr, a, noticia)
                fvg = detectar_fvg_m15(df, atr)
                confluencia = plano_confluencia_local(df, atr, a, noticia)
                ouro_movimento = plano_ouro_movimento(df, atr, a, noticia)
                tp1_curto = plano_tp1_curto_forex(df, atr, a, noticia)
                fluxo = leitura_fluxo_sessao(df, atr, a)
                orb = plano_orb_sessao(df, atr, a)
                liquidez = plano_varredura_liquidez(df, atr, a, noticia)
                bof = {
                    "disponivel": False, "sinal_estudo": False,
                    "candidato": False, "direcao": "neutro",
                    "etapa": "FORA DO ESTUDO",
                    "motivo": "BOF M30→M5 é observado em Forex normal e ouro.",
                }
                df_bof = None
                if CLASSE.get(a) in ("forex", "ouro"):
                    try:
                        # São 15 ativos elegíveis. Atualizar todos a cada 30s
                        # sobrecarrega o WebSocket da IQ e causa reconnects.
                        # O estudo usa candle M5 fechado, então uma coleta por
                        # bloco de cinco minutos preserva exatamente o sinal.
                        bucket_bof = int(time.time() // TF_BOF)
                        if (a not in hist_bof
                                or ultimo_bucket_bof.get(a) != bucket_bof):
                            ultimo_bucket_bof[a] = bucket_bof
                            novo_bof = backtest.baixar_historico(api, cfg_bof, a, 180)
                            novo_bof = novo_bof.iloc[:-1]
                            base_bof = hist_bof.get(a)
                            df_bof = (novo_bof if base_bof is None else pd.concat([
                                base_bof[~base_bof.index.isin(novo_bof.index)], novo_bof,
                            ]).sort_index()).tail(1200)
                            hist_bof[a] = df_bof
                        else:
                            df_bof = hist_bof[a]
                        bof = plano_bof_m30_m5(df_bof, M.atr(df_bof), a, noticia)
                    except Exception as erro_bof:
                        bof = {**bof, "etapa": "M5 INDISPONÍVEL",
                               "motivo": f"Falha ao atualizar candles M5: {erro_bof}"}

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

                ia_anterior = estado.dados.get(a, {}).get("ia_groq")
                ia_atual = ia_anterior if tp1_curto.get("qualificada") else {
                    "status": "AGUARDAR", "veredicto": "AGUARDAR", "fonte": "GROQ",
                    "motivo": "A IA só analisa quando zona, rejeição e R:R curto passam no filtro.",
                }
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
                    "noticias_dia": noticias_dia,
                    "dossie": dossie_candle(df, atr, a, noticia),
                    "fibo": fibo,
                    "fvg": fvg,
                    "confluencia": confluencia,
                    "ouro_movimento": ouro_movimento,
                    "tp1_curto": tp1_curto,
                    "ia_groq": ia_atual,
                    "fluxo": fluxo,
                    "orb": orb,
                    "liquidez": liquidez,
                    "bof": bof,
                })
                if analista_groq is not None and tp1_curto.get("qualificada"):
                    analista_groq.solicitar(a, df, tp1_curto, noticia, receber_ia)
                # A análise continua usando somente candles fechados, mas o
                # desenho recebe também a vela atual para se mover como na IQ.
                atual = candle_atual_para_grafico(df, novo_grafico)
                if atual is None:
                    # Mantém os fechados — são a fonte da análise — quando o
                    # tick aberto não corresponde a uma vela M15 confiável.
                    df_grafico = df.tail(300)
                else:
                    df_grafico = pd.concat([
                        df[~df.index.isin(atual.index)], atual
                    ]).sort_index().tail(300)
                estado.salvar_candles(a, df_grafico, regressao(df_grafico))
                estado.resolver(a, df)
                if df_bof is not None:
                    estado.resolver(a, df_bof, timeframe_s=TF_BOF)
                # Direcao mecanica da noticia: registra quando o numero sai e
                # confere depois. Observacao pura, nao gera ordem.
                estado.registrar_direcao_noticia(a, str(df.index[-1]), preco, noticia)
                estado.resolver_direcoes_noticia(a, df)
                # Heartbeat POR ATIVO, nao so no fim da passada. Antes, um
                # unico ativo lento (download travado) fazia o supervisor achar
                # que o monitor inteiro morreu, porque o JSON so era escrito
                # depois de percorrer os 17.
                estado.status = f"lendo {a} — {datetime.now(timezone.utc):%H:%M:%S} UTC"
                estado.salvar()
                if leitura_entrada["sinal"]:
                    if estado.registrar_sinal(a, str(df.index[-1]), estado.dados[a]):
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
                    if estado.registrar_sinal(a, str(df.index[-1]), estudo_fibo, tipo="fibo_m15"):
                        print(f"[FIBO — ESTUDO] {a} {fibo['direcao'].upper()} @ {df.index[-1]} "
                              f"RR={fibo['rr']}")
                if tp1_curto.get("sinal_estudo"):
                    estudo_curto = {
                        **estado.dados[a],
                        "sinal": True, "direcao": tp1_curto["direcao"],
                        "entrada_valida": False,
                        "estado_entrada": "TP1 CURTO — ESTUDO",
                        "motivo_entrada": tp1_curto["motivo"],
                        "checklist": tp1_curto["checklist"],
                        "alvos": None, "alvos_estudo": tp1_curto["alvos"],
                        "horizonte_velas": 4, "horizonte_longo_velas": 8,
                    }
                    if estado.registrar_sinal(
                        a, str(df.index[-1]), estudo_curto, tipo="tp1_curto_forex"
                    ):
                        print(f"[TP1 CURTO — ESTUDO] {a} {tp1_curto['direcao'].upper()} "
                              f"@ {df.index[-1]} RR={tp1_curto['rr']}")
                if ouro_movimento.get("sinal_estudo"):
                    estudo_ouro = {
                        **estado.dados[a], "sinal": True,
                        "direcao": ouro_movimento["direcao"], "entrada_valida": False,
                        "estado_entrada": "OURO MOVIMENTO — ESTUDO",
                        "motivo_entrada": ouro_movimento["motivo"],
                        "checklist": ouro_movimento["checklist"],
                        "alvos": None, "alvos_estudo": ouro_movimento["alvos"],
                        "horizonte_velas": 4, "horizonte_longo_velas": 8,
                        "ouro_movimento": ouro_movimento,
                    }
                    if estado.registrar_sinal(a, str(df.index[-1]), estudo_ouro,
                                              tipo="ouro_movimento_m15"):
                        print(f"[OURO MOVIMENTO — ESTUDO] {a} {ouro_movimento['direcao'].upper()} "
                              f"@ {df.index[-1]} RR={ouro_movimento['rr']}")
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
                    if estado.registrar_sinal(
                        a, str(df.index[-1]), estudo_fluxo, tipo="fluxo_m15"
                    ):
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
                    if estado.registrar_sinal(
                        a, str(df.index[-1]), estudo_orb, tipo="orb_fvg_m15"
                    ):
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
                    if estado.registrar_sinal(
                        a, str(df.index[-1]), estudo_liquidez,
                        tipo="liquidity_sweep_v2",
                    ):
                        print(f"[LIQUIDEZ V2 — ESTUDO] {a} BUY @ {df.index[-1]} "
                              f"RR={liquidez['rr']}")
                if CLASSE.get(a) in ("forex", "ouro") and bof.get("candidato"):
                    vela_bof = str(df_bof.index[-1]) if df_bof is not None else str(df.index[-1])
                    info_bof = {
                        **estado.dados[a],
                        "sinal": True, "direcao": bof["direcao"],
                        "entrada_valida": False,
                        "estado_entrada": f"BOF — {bof['etapa']}",
                        "motivo_entrada": bof["motivo"],
                        "checklist": bof.get("checklist", []),
                        "alvos": None,
                        "alvos_estudo": bof.get("alvos") if bof.get("sinal_estudo") else None,
                        "preco": (round(float(df_bof.iloc[-1]["Close"]), 3)
                                  if df_bof is not None else estado.dados[a].get("preco")),
                        "timeframe_segundos": TF_BOF,
                        "horizonte_velas": HORIZONTE_BOF_VELAS,
                        "horizonte_longo_velas": HORIZONTE_BOF_LONGO_VELAS,
                        "bof": bof,
                    }
                    if bof.get("sinal_estudo"):
                        tipo_bof = "bof_m30_m5"
                    else:
                        tipo_bof = "bof_m30_m5_candidato"
                        info_bof["sem_simulacao"] = True
                    if estado.registrar_sinal(a, vela_bof, info_bof, tipo=tipo_bof):
                        print(f"[BOF M30→M5 — {'ESTUDO' if bof.get('sinal_estudo') else 'CANDIDATO'}] "
                              f"{a} {bof['direcao'].upper()} @ {vela_bof} etapa={bof['etapa']} "
                              f"RR={bof.get('rr', 0)}")
                # Rompimento+reteste do forex, em sombra. A estrategia vive em
                # forex_estrategia.py e nao tinha nenhuma amostra ao vivo: os
                # processos que a executam nao rodam. Aqui ela so observa,
                # usando a maquina de afericao que ja existe.
                plano_rr = _plano_rompimento_reteste_sombra(a, df, av)
                if plano_rr is not None:
                    estudo_rr = {
                        **estado.dados[a],
                        "sinal": True, "direcao": plano_rr["direcao"],
                        "entrada_valida": False,
                        "estado_entrada": "ROMPIMENTO+RETESTE — ESTUDO",
                        "motivo_entrada": plano_rr["motivo"],
                        "checklist": plano_rr["checklist"],
                        "alvos": None, "alvos_estudo": plano_rr["alvos"],
                    }
                    if estado.registrar_sinal(
                        a, str(df.index[-1]), estudo_rr,
                        tipo="rompimento_reteste",
                    ):
                        print(f"[ROMPIMENTO+RETESTE — ESTUDO] {a} "
                              f"{plano_rr['direcao'].upper()} @ {df.index[-1]} "
                              f"risco={plano_rr['risco_atr']}ATR")
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
#cv{width:100%;height:58vh;min-height:340px;background:#0d1526;border-radius:.4rem;margin:.4rem 0;position:relative;overflow:hidden}
#chart-hud{position:absolute;left:.7rem;top:.7rem;z-index:3;pointer-events:none;width:152px;background:#101a2ce8;border:1px solid #334155;border-radius:.35rem;font-size:.61rem;color:#cbd5e1;box-shadow:0 5px 18px #0008}
#chart-hud b{display:block;padding:.3rem .4rem;background:#17233a;color:#e2e8f0;font-size:.64rem}.hud-linha{display:flex;justify-content:space-between;gap:.4rem;padding:.16rem .4rem;border-top:1px solid #233149}.hud-linha span:last-child{font-weight:700}.hud-ok{color:#4ade80}.hud-wait{color:#fbbf24}.hud-no{color:#f87171}
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
.fibo-manual{margin:.45rem 0 .7rem;border-left:3px solid #a78bfa}.fibo-manual .nota{margin-top:.45rem}
.fvg-card{border-left-color:#14b8a6}.fvg-card.ok{border-left-color:#22c55e}
.conflu-card{border-left-color:#a78bfa}.conflu-card.ok{border-left-color:#22c55e}
.ouro-card{border-left-color:#fbbf24}.ouro-card.ok{border-left-color:#22c55e}
.curto-card{border-left-color:#f97316}.curto-card.ok{border-left-color:#22c55e}
.orb-card{border-left-color:#f59e0b}.orb-card.ok{border-left-color:#22c55e}
.bof-card{border-left-color:#06b6d4}.bof-card.ok{border-left-color:#22c55e}
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
  <button id="btn-som-monitor" type="button" onclick="alternarSomMonitor()" title="Ativa som para novas oportunidades">🔕 Som</button>
</div>

<section class="view" data-view="grafico">

<div class="sec">GRAFICO</div>
<div class="tabs" id="tabs"></div>
<div id="cv-titulo"></div>
<div id="marc-barra">
  <button class="btn-marc" id="btn-marc-fibo" onclick="marcarFibo()" title="Clique na origem e depois no extremo">✎ Fibo</button>
  <button class="btn-marc" id="btn-marc-linha" onclick="marcarLinha()" title="Um clique no preço">✎ Linha</button>
  <button class="btn-marc" id="btn-niveis" onclick="alternarNiveis()">☷ Essenciais</button>
  <button class="btn-marc" id="btn-marc-limpar" onclick="limparMarcacoes()" title="Apagar marcações deste ativo">✕</button>
  <span id="marc-dica"></span>
</div>
<div id="cv-aviso"></div>
<div id="cv"></div>
<div id="fibo-manual"><span class="empty">BINÁRIAS: use ✎ Fibo, clique no início do impulso e depois no extremo. A leitura guiada aparecerá aqui.</span></div>
</section>

<section class="view" data-view="estudos">
<div class="amostra" id="amostra">Amostra validada: carregando…</div>

<div class="sec">TP1 CURTO FOREX — ZONA + REJEIÇÃO — ESTUDO SOMBRA</div>
<div id="tp1-curto"><span class="empty">Selecione um par Forex.</span></div>

<div class="sec">LEITURA DE FLUXO E SESSÃO — ESTUDO</div>
<div id="fluxo"><span class="empty">Selecione um ativo.</span></div>

<div class="sec">ORB LONDRES/NY + FVG M15 — ESTUDO SOMBRA</div>
<div id="orb"><span class="empty">Selecione um ativo.</span></div>

<div class="sec">VARREDURA DE LIQUIDEZ V2 — ESTUDO SOMBRA</div>
<div id="liquidez"><span class="empty">Selecione um ativo.</span></div>

<div class="sec">BOF M30→M5 FOREX + OURO — ESTUDO SOMBRA</div>
<div id="bof"><span class="empty">Selecione um par Forex ou XAUUSD.</span></div>

<div class="sec">FVG M15 — CONFLUÊNCIA</div>
<div id="fvg"><span class="empty">Selecione um ativo.</span></div>

<div class="sec">OURO — MOVIMENTO A FAVOR M15</div>
<div id="ouro-movimento"><span class="empty">Selecione XAUUSD.</span></div>

<div class="sec">PLANO FIBO M15 — ESTUDO</div>
<div id="fibo"><span class="empty">Selecione um ativo.</span></div>
</section>

<section class="view" data-view="agora">

<div id="decisao-principal"></div>

<div class="sec">IA DO GRÁFICO — LEITURA DO ATIVO</div>
<button class="btn-marc" type="button" onclick="lerGroqSelecionado()">🤖 Analisar ativo + plano</button>
<div id="ia-grafico"><span class="empty">Aguardando leitura mecânica.</span></div>

<div class="sec">CONFLUÊNCIA LOCAL — ESTRUTURA, FVG, OB E LIQUIDEZ</div>
<div id="confluencia"><span class="empty">Selecione um ativo.</span></div>

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
let iaManualGrafico={}, candlesGroq=[];
let niveisDetalhados=localStorage.getItem('monitorNiveisDetalhados')==='1';
let visao=localStorage.getItem('monitorMercadoVisao')||'agora';
let ultimaChaveAlerta='';
let somMonitorAtivo=localStorage.getItem('monitorMercadoSom')==='1';
let ultimoEventoPossivel='';
let eventosSonorosInicializados=false;
let dadosMonitorAoVivo=true;
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
  const tipo=h.tipo==='fibo_m15'?'FIBO':h.tipo==='fluxo_m15'?'FLUXO':h.tipo==='orb_fvg_m15'?'ORB/FVG':h.tipo==='liquidity_sweep_v2'?'LIQUIDEZ V2':h.tipo==='bof_m30_m5'?'BOF M30→M5':h.tipo==='bof_m30_m5_xau'?'BOF M30→M5 (legado)':h.tipo==='bof_m30_m5_candidato'?'BOF candidato':'SINAL';
  const av=h.alvos_estudo||h.alvos||{};
  const simResumo=resumoSimulacao(h.simulacao,h);
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
    <td><span class="${simResumo.cor}">${simResumo.texto}</span></td>
    <td>${res}</td></tr>`;
}

function checklist(x){
  const itens=x.checklist||[];
  if(!itens.length) return '<span class="ind">Checklist indisponível neste registro antigo.</span>';
  return `<div class="checklist">${itens.map(i=>`<span class="check ${i.ok?'ok':'no'}">${i.ok?'✓':'✗'} ${i.nome}</span>`).join('')}</div>`;
}

function textoSimulacao(s,h={}){
  const d=(s||{}).desfecho||'aguardando';
  const rr=s&&s.resultado_r!=null?` · ${Number(s.resultado_r).toFixed(2)}R`:'';
  const total=Number(h.horizonte_velas||24);
  const horas=total*Number(h.timeframe_segundos||h.timeframe||900)/3600;
  const prazo=Number.isInteger(horas)?`${horas}h`:`${horas.toFixed(1)}h`;
  if(d==='win_tp1') return `<span class="dossie-win">TP1 atingido (simulado)${rr}</span>`;
  if(d==='loss_sl') return `<span class="dossie-loss">SL atingido (simulado)${rr}</span>`;
  if(d==='ambíguo_mesma_vela') return '<span class="dossie-aviso">TP e SL na mesma vela — ordem desconhecida</span>';
  if(d==='ambíguo_entrada_tp_mesma_vela') return '<span class="dossie-aviso">entrada e TP na mesma vela — sequência desconhecida</span>';
  if(d.startsWith('nao_executada_')) return `<span class="ind">preço de entrada não foi tocado em ${prazo}</span>`;
  if(d.startsWith('expirado_')) return `<span class="dossie-aviso">fechado por tempo após ${prazo}${rr}</span>`;
  const pendente=['limite','stop','market_next_open'].includes((h.alvos_estudo||h.alvos||{}).modo_entrada)
    || h.tipo==='fibo_m15';
  if(pendente && (!s || s.preenchida!==true))
    return `<span class="ind">aguardando preço de entrada (${(s&&s.velas)||0}/${total} velas); TP/SL só contam após o preenchimento</span>`;
  return `<span class="ind">em operação: aguardando TP/SL (${(s&&s.velas)||0}/${total} velas)</span>`;
}

function renderDossie(H){
  const el=document.getElementById('dossie');
  const h=H.find(x=>x.id===dossieSel)||H[0];
  if(!h){ el.innerHTML='<span class="empty">Ainda não há sinais registrados.</span>'; return; }
  dossieSel=h.id;
  const a=h.dossie||{}, n=a.noticia||{}, c=h.comparaveis||{};
  // Numero ja publicado: direcao vem da conta, a frase vem da IA e fica
  // marcada como tal. Nenhuma das duas e sugestao de operacao.
  function resultadoPublicado(x){
    if(x.estado!=='resultado_publicado') return '';
    const dir = x.direcao ? `<b style="color:${x.direcao==='CALL'?'#4ade80':'#f87171'}">${x.direcao}</b>` : '';
    const dsv = x.desvio_pct==null ? '' :
      ` · ${x.desvio_pct>0?'+':''}${x.desvio_pct}% vs previsto`;
    const sur = x.surpresa ? ` · surpresa <b>${x.surpresa}</b>` : '';
    const txt = x.leitura_ia
      ? `<div style="font-size:.64rem;color:#94a3b8;margin-top:.15rem">🤖 ${x.leitura_ia}</div>` : '';
    return `<div style="font-size:.66rem;color:#cbd5e1;margin-top:.2rem">`
      + `saiu ${x.actual} (previsto ${x.forecast})${dsv}${sur} → ${dir}</div>${txt}`;
  }
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
        <div><label>simulação</label>${textoSimulacao(h.simulacao,h)}</div>
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
      <div class="item"><label>notícia no contexto</label><span class="${n.estado==='janela_risco'?'dossie-aviso':''}">${n.texto||'sem calendário'}</span>${resultadoPublicado(n)}</div>
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
function atualizarBotaoSomMonitor(){
  const b=document.getElementById('btn-som-monitor');
  if(!b) return;
  b.textContent=somMonitorAtivo?'🔔 Som':'🔕 Som';
  b.classList.toggle('on',somMonitorAtivo);
}
function alternarSomMonitor(){
  somMonitorAtivo=!somMonitorAtivo;
  localStorage.setItem('monitorMercadoSom',somMonitorAtivo?'1':'0');
  atualizarBotaoSomMonitor();
  if(somMonitorAtivo) bip(true,'forte');
}
// Cada estudo publica o IC do R medio. O aviso segue esse intervalo, nao uma
// lista escrita na mao: assim, se o Fibo regredir ele se cala sozinho, e se o
// falso rompimento acumular amostra ele passa a avisar sem eu tocar em nada.
const AMOSTRA_DO_ESTUDO={fibo_m15:'amostraFibo', fluxo_m15:'amostraFluxo',
  tp1_curto_forex:'amostraTp1Curto',
  orb_fvg_m15:'amostraOrb', liquidity_sweep_v2:'amostraLiquidez',
  bof_m30_m5:'amostraBof', rompimento_reteste:'amostraRompimentoReteste'};
// Janela por peso: um aviso fraco nunca pode engolir um forte no rate limit.
const ultimoAlertaPorPeso={forte:0, fraco:0};

function pesoDoEstudo(tipo){
  const a=D[AMOSTRA_DO_ESTUDO[tipo]]||{};
  const ic=a.media_r_ic;
  if(!Array.isArray(ic)) return 'fraco';   // amostra curta demais para opinar
  if(ic[0]>0) return 'forte';              // vantagem sustentada pelo intervalo
  if(ic[1]<0) return 'mudo';               // prejuizo sustentado: nao avisa
  return 'fraco';                          // cruza zero: aparece, sem insistir
}

function bip(teste=false, peso='forte'){
  if((!somMonitorAtivo || !dadosMonitorAoVivo) && !teste) return;
  if(peso==='mudo' && !teste) return;
  if(!teste){
    // Janelas separadas, e a fraca e mais longa para nao virar ruido.
    const espera = peso==='forte' ? 60000 : 180000;
    if(Date.now()-ultimoAlertaPorPeso[peso] < espera) return;
    ultimoAlertaPorPeso[peso]=Date.now();
  }
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    if(ctx.state==='suspended') ctx.resume();
    // Forte: duas notas altas, da para reconhecer sem olhar a tela.
    // Fraco: uma nota baixa e curta, so para nao passar despercebido.
    const notas = peso==='forte' ? [[880,0],[1175,260]] : [[440,0]];
    const volume = peso==='forte' ? .09 : .04;
    const duracao = peso==='forte' ? 220 : 120;
    notas.forEach(([hz,atraso])=>setTimeout(()=>{
      const o=ctx.createOscillator(), g=ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.frequency.value=hz; g.gain.value=volume;
      o.start(); setTimeout(()=>{try{o.stop();}catch(e){}},duracao);
    },atraso));
    setTimeout(()=>{try{ctx.close();}catch(e){}}, 900);
  }catch(e){}
}

function alertarNovoEvento(){
  if(!dadosMonitorAoVivo) return;
  const ativos=(D.historico||[]).filter(h=>AMOSTRA_DO_ESTUDO[h.tipo]
    &&(h.simulacao||{}).desfecho==='aguardando');
  const recente=ativos[ativos.length-1];
  const id=recente&&(recente.id||`${recente.ativo}|${recente.tipo}|${recente.hora||recente.timestamp||''}`);
  if(!eventosSonorosInicializados){
    eventosSonorosInicializados=true; ultimoEventoPossivel=id||''; return;
  }
  if(id && id!==ultimoEventoPossivel){
    ultimoEventoPossivel=id;
    bip(false, pesoDoEstudo(recente.tipo));
  }
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

function cardTp1Curto(a,x){
  const c=x.tp1_curto||{}, v=c.alvos||{};
  const ia=x.ia_groq||{};
  if(!c.disponivel || !c.qualificada) return '';
  const dir=c.direcao==='sell'?'SELL':'BUY';
  return `<div class="card curto-card">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>${a} ${dir} — TP1 CURTO</b><span class="estado study">ESTUDO SOMBRA</span></div>
    ${checklist(c)}
    <div class="alvos"><div><label>entrada após fechamento</label><span>${v.entrada}</span></div><div><label>stop tático</label><span class="dn">${v.sl}</span></div><div><label>TP1 38,2%</label><span class="up">${v.tp1}</span></div><div><label>R:R</label><span>${c.rr}x</span></div></div>
    <div class="nota">${c.motivo} IA: ${ia.veredicto||'AGUARDAR'} · ${ia.motivo||'aguardando análise.'} Registrado e simulado por 1h; ainda não é entrada validada.</div>
  </div>`;
}

function renderIaGrafico(){
  const el=document.getElementById('ia-grafico');
  const x=(D.ativos||{})[sel]||{}, ia=iaManualGrafico[sel]||x.ia_groq||{};
  const c=x.classe==='forex'?(x.tp1_curto||{}):(x.classe==='ouro'&&x.ouro_movimento?.sinal_estudo?(x.ouro_movimento||{}):(x.fibo||{}));
  const a=c.alvos||{}, n=x.noticia||{};
  const planoNome=x.classe==='forex'?'TP1 curto':x.classe==='ouro'?'Ouro movimento':'Fibo M15';
  const v=ia.veredicto||'AGUARDAR';
  const texto={BUY:'BUY / CALL',SELL:'SELL / PUT',AGUARDAR:'AGUARDAR'}[v]||'AGUARDAR';
  const cor=v==='BUY'?'up':v==='SELL'?'dn':'morno';
  const status=ia.status||'AGUARDAR';
  const zona={NA_ZONA:'na zona',AGUARDAR_RETESTE:'aguardar reteste',ESTICADO:'preço esticado',SEM_ZONA:'sem zona'}[ia.zona]||'—';
  const estrutura={ALTA:'alta',BAIXA:'baixa',LATERAL:'lateral',INDEFINIDA:'indefinida'}[ia.estrutura]||'—';
  const planoAtivo=Boolean(c.qualificada||c.sinal_estudo);
  const aviso=v==='AGUARDAR' ? 'Não é sugestão de entrada.' : 'Confirme spread e preço na corretora; a IA não executa ordem.';
  el.innerHTML=`<div class="card ${v==='AGUARDAR'?'nojan':'okjan'}"><b class="${cor}">${texto}</b> <span class="estado study">${status}</span>
    <div class="alvos"><div><label>estrutura M15</label><span>${estrutura}</span></div><div><label>zona</label><span>${zona}</span></div><div><label>confiança</label><span>${ia.confianca||'—'}</span></div><div><label>plano técnico</label><span>${planoAtivo?planoNome+' calculado':'não qualificado'}</span></div><div><label>entrada</label><span>${a.entrada??'—'}</span></div><div><label>invalidação / SL</label><span class="dn">${a.sl??'—'}</span></div><div><label>TP1</label><span class="up">${ia.alvo_codigo==='TP1'?ia.alvo_curto:(a.tp1??'—')}</span></div><div><label>notícia</label><span>${n.estado||'sem alerta'}</span></div><div><label>fonte</label><span>${ia.fonte||'GROQ'}</span></div></div>
    <div class="nota"><b>Confirmação:</b> ${ia.confirmacao||'Aguardando a leitura da IA.'}<br>${ia.motivo||'Clique em Analisar ativo + plano para consultar a IA.'} ${aviso}</div></div>`;
}

async function lerGroqSelecionado(){
  const x=(D.ativos||{})[sel]||{};
  iaManualGrafico[sel]={status:'ANALISANDO',veredicto:'AGUARDAR',fonte:'GROQ',motivo:'Lendo estrutura, zona, candles fechados, notícia e plano técnico.'};
  renderIaGrafico();
  // Forex usa TP1 curto; ouro prioriza o scanner próprio quando qualificado.
  const p=x.classe==='forex'?(x.tp1_curto||{}):(x.classe==='ouro'&&x.ouro_movimento?.sinal_estudo?(x.ouro_movimento||{}):(x.fibo||{})), a=p.alvos||{};
  try{
    if(!candlesGroq.length){
      const r=await fetch('mkt_'+sel+'.json?t='+Date.now());
      const j=await r.json(); candlesGroq=Array.isArray(j.candles)?j.candles:[];
    }
    const f=x.fibo||{}, g=x.fvg||{};
    const contexto={ativo:sel,classe:x.classe||'',timeframe:'M15',vela:x.vela||'',candles_fechados:candlesGroq.slice(-12).map(k=>({o:k.o,h:k.h,l:k.l,c:k.c})),mercado:{preco:x.preco,tendencia_m15:f.m15||'—',tendencia_h1:f.h1||'—',fvg:g.disponivel?{direcao:g.direcao,zona:[g.zona_inf,g.zona_sup],em_zona:g.em_zona}:null},plano:{qualificada:Boolean(p.qualificada||p.sinal_estudo),direcao:(p.direcao||'').toUpperCase(),zona:[p.zona_inf,p.zona_sup],entrada:a.entrada,sl:a.sl,tp1:a.tp1,rr:p.rr,risco_atr:p.risco_atr},noticia:{estado:(x.noticia||{}).estado,texto:(x.noticia||{}).texto}};
    const r=await fetch('/opiniao_groq_grafico',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(contexto)});
    if(!r.ok) throw new Error('HTTP '+r.status);
    iaManualGrafico[sel]=await r.json();
  }catch(e){
    iaManualGrafico[sel]={status:'INDISPONÍVEL',veredicto:'AGUARDAR',fonte:'GROQ',motivo:'Falha ao consultar Groq: '+e.name};
  }
  renderIaGrafico();
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
  // Horizonte longo medido em paralelo: comparacao, nunca soma.
  const L=x.comparacao_longa;
  const cmp = L && L.avaliados_r ?
    `<div style="font-size:.62rem;color:#64748b;margin-top:.1rem">horizonte ${x.horizonte||'6h'}: ${x.media_r==null?'—':(x.media_r>0?'+':'')+x.media_r}R/sinal (${x.avaliados_r||0}) &nbsp;·&nbsp; ${L.horizonte}: ${L.media_r==null?'—':(L.media_r>0?'+':'')+L.media_r}R/sinal (${L.avaliados_r})</div>` : '';
  return `<div style="margin:.45rem 0;padding:.4rem .5rem;background:#0c1728;border-left:3px solid #334155;border-radius:.25rem">
    <b>${label}</b> · ${x.sinais||0} sinais ${maturidadeBadge(x)}<br>
    <span style="font-size:.7rem;color:#cbd5e1">${wr} · ${saldo} em ${x.avaliados_r||0} saídas · ${x.expirados||0} fechados por tempo · ${x.pendentes||0} pendentes · ${x.nao_executadas||0} não exec.</span>
    ${cmp}
  </div>`;
}
function linhaDirecaoNoticia(x){
  // Nao e TP/SL: mede so se a direcao mecanica apontou para o lado certo.
  const txt = x.acerto_pct==null ? 'sem aferição resolvida'
    : `${x.acertos} acertos / ${x.erros} erros · ${x.acerto_pct}%`;
  const ic = x.ic_95 ? `<span class="ic-badge ok">IC95% ${x.ic_95[0]}–${x.ic_95[1]}%</span>`
                     : '<span class="ic-badge insuf">IC indisponível</span>';
  const insuf = !x.amostra_suficiente
    ? '<span style="color:#64748b;font-size:.64rem"> ⚠ AMOSTRA INSUFICIENTE (n&lt;30)</span>' : '';
  return `<div style="margin:.45rem 0;padding:.4rem .5rem;background:#0c1728;border-left:3px solid #7c3aed;border-radius:.25rem">
    <b>Direção mecânica da notícia</b> · ${x.sinais||0} registros <span class="mat">${x.maturidade||'—'}</span>${ic}${insuf}<br>
    <span style="font-size:.7rem;color:#cbd5e1">${txt} · ${x.empates||0} empates · ${x.pendentes||0} aguardando · horizonte ${x.horizonte||'—'}</span>
    <div style="font-size:.62rem;color:#64748b;margin-top:.15rem">Sem TP/SL: só se o par foi para o lado previsto. Não entra nas amostras dos estudos.</div>
  </div>`;
}
function renderAmostra(){
  const el=document.getElementById('amostra'); if(!el) return;
  el.innerHTML=
    linhaEstudo('Entradas válidas (falso rompimento)',D.amostraEntrada||{})+
    linhaEstudo('TP1 curto Forex — zona + rejeição',D.amostraTp1Curto||{})+
    linhaEstudo('Fibo M15 — estudo',D.amostraFibo||{})+
    linhaEstudo('Fluxo de sessão — estudo',D.amostraFluxo||{})+
    linhaEstudo('ORB/FVG M15 — estudo sombra',D.amostraOrb||{})+
    linhaEstudo('Liquidez V2 — estudo sombra',D.amostraLiquidez||{})+
    linhaEstudo('Rompimento+reteste (forex) — estudo sombra',D.amostraRompimentoReteste||{})+
    linhaEstudo('BOF M30→M5 Forex + ouro — estudo sombra',D.amostraBof||{})+
    linhaDirecaoNoticia(D.amostraDirecaoNoticia||{})+
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

function renderBof(){
  const el=document.getElementById('bof');
  const x=(D.ativos||{})[sel]||{}, b=x.bof||{}, a=b.alvos||{};
  if(x.classe!=='forex' && x.classe!=='ouro'){
    el.innerHTML='<div class="card bof-card"><b>FORA DO ESTUDO</b><div class="nota">BOF M30→M5 está disponível nos pares Forex normais e no ouro.</div></div>';
    return;
  }
  if(!b.disponivel){
    el.innerHTML=`<div class="card bof-card"><b>${b.etapa||'BOF INDISPONÍVEL'}</b><div class="nota">${b.motivo||'Aguardando candles M5.'}</div></div>`;
    return;
  }
  const dir=b.direcao==='sell'?'VENDA':b.direcao==='buy'?'COMPRA':'AGUARDAR';
  el.innerHTML=`<div class="card bof-card ${b.sinal_estudo?'okjan ok':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>${sel} — ${dir}</b><span class="estado ${b.sinal_estudo?'go':'study'}">${b.etapa}</span></div>
    ${checklist(b)}
    <div class="alvos">
      <div><label>nível M30 confirmado</label><span>${b.nivel_m30??'—'} (${b.lado_nivel||'—'})</span></div>
      <div><label>extremo da varredura</label><span>${b.extremo_varredura??'—'} · ${b.extensao_atr??'—'} ATR</span></div>
      <div><label>referência no fechamento M5</label><span>${a.entrada??'—'} ${a.entrada!=null?'(executa na próxima abertura)':''}</span></div>
      <div><label>SL além da varredura</label><span class="dn">${a.sl??'—'}</span></div>
      <div><label>próximo pivô M30 / TP</label><span class="up">${a.tp1??'—'}</span></div>
      <div><label>R:R planejado</label><span>${b.rr??'—'}x</span></div>
    </div>
    <div class="nota">${b.motivo} Somente estudo: não toca alerta operacional nem envia ordem.</div>
  </div>`;
}

function renderFvg(){
  const el=document.getElementById('fvg');
  const x=(D.ativos||{})[sel]||{}, g=x.fvg||{};
  if(!g.disponivel){
    el.innerHTML=`<div class="card fvg-card"><b>${g.estado||'SEM FVG ATIVO'}</b><div class="nota">${g.motivo||'Nenhuma zona de desequilíbrio M15 relevante agora.'}</div></div>`;
    return;
  }
  const dir=g.direcao==='sell'?'VENDA':'COMPRA';
  const cor=g.direcao==='sell'?'dn':'up';
  el.innerHTML=`<div class="card fvg-card ${g.em_zona?'okjan ok':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>${sel} — ${dir}</b><span class="estado ${g.em_zona?'study':'wait'}">${g.estado}</span></div>
    <div class="alvos"><div><label>zona FVG</label><span class="${cor}">${g.zona_inf} — ${g.zona_sup}</span></div><div><label>tamanho</label><span>${g.tamanho_atr} ATR</span></div><div><label>distância</label><span>${g.distancia_atr} ATR</span></div></div>
    <div class="nota">${g.motivo} FVG não é entrada isolada: use apenas junto com tendência, zona e rejeição fechada.</div>
  </div>`;
}

function renderConfluencia(){
  const el=document.getElementById('confluencia');
  const x=(D.ativos||{})[sel]||{}, c=x.confluencia||{}, a=c.alvos||{};
  if(!c.disponivel){
    el.innerHTML=`<div class="card conflu-card"><b>${c.estado||'AGUARDANDO ESTRUTURA'}</b><div class="nota">${c.motivo||'Aguardando candles M15.'}</div></div>`;
    return;
  }
  const dir=c.direcao==='sell'?'VENDA':'COMPRA';
  el.innerHTML=`<div class="card conflu-card ${c.qualificada?'okjan ok':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>${sel} — ${dir}</b><span class="estado ${c.qualificada?'go':'study'}">${c.estado} · ${c.score||0}/10</span></div>
    ${checklist(c)}
    <div class="alvos"><div><label>viés M15 / H1</label><span>${c.m15||'—'} / ${c.h1||'—'}</span></div><div><label>zona SMC</label><span class="fibo-zona">${c.zona_inf??'—'} — ${c.zona_sup??'—'}</span></div><div><label>entrada no meio da zona</label><span>${a.entrada??'—'}</span></div><div><label>invalidação</label><span class="dn">${a.sl??'—'}</span></div><div><label>TP1 / TP2 / TP3</label><span class="up">${a.tp1??'—'} · ${a.tp2??'—'} · ${a.tp3??'—'}</span></div><div><label>R:R TP1</label><span>${a.rr??'—'}x</span></div></div>
    <div class="nota">${c.motivo} Leitura local em estudo: não cria sinal validado nem envia ordem.</div>
  </div>`;
}

function renderOuroMovimento(){
  const el=document.getElementById('ouro-movimento');
  const x=(D.ativos||{})[sel]||{}, o=x.ouro_movimento||{}, a=o.alvos||{}, noticias=x.noticias_dia||[];
  if(x.classe!=='ouro'){
    el.innerHTML='<div class="card ouro-card"><b>EXCLUSIVO XAUUSD</b><div class="nota">Este scanner procura continuação com tendência, FVG e candle de rejeição no ouro.</div></div>';
    return;
  }
  if(!o.disponivel){
    el.innerHTML=`<div class="card ouro-card"><b>${o.estado||'AGUARDANDO'}</b><div class="nota">${o.motivo||'Aguardando candles M15.'}</div></div>`;
    return;
  }
  const dir=o.direcao==='sell'?'VENDA':o.direcao==='buy'?'COMPRA':'—';
  const agenda=noticias.length?`<div class="nota" style="margin-top:.55rem"><b>NOTÍCIAS USD HOJE</b>${noticias.map(n=>`<br>• ${FMT_HORA_BRT.format(new Date(Number(n.quando_ts)*1000))} · ${n.impacto} ${n.moeda} — ${n.titulo}<br><span style="color:#94a3b8">${n.actual!=null?'actual '+n.actual+' · previsto '+(n.forecast??'—'):'programada'} · ${n.uso}</span>`).join('')}</div>`:'<div class="nota" style="margin-top:.55rem">Sem notícia relevante do ativo no calendário de hoje.</div>';
  el.innerHTML=`<div class="card ouro-card ${o.sinal_estudo?'okjan ok':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>XAUUSD — ${dir}</b><span class="estado ${o.sinal_estudo?'study':'wait'}">${o.estado}</span></div>
    ${checklist(o)}
    <div class="alvos"><div><label>zona FVG</label><span class="fibo-zona">${o.zona_inf??'—'} — ${o.zona_sup??'—'}</span></div><div><label>corpo confirmação</label><span>${o.corpo_atr??'—'} ATR</span></div><div><label>entrada após fechamento</label><span>${a.entrada??'—'}</span></div><div><label>SL técnico</label><span class="dn">${a.sl??'—'}</span></div><div><label>TP1</label><span class="up">${a.tp1??'—'}</span></div></div>
    <div class="nota">${o.motivo} Estudo em sombra: o Monitor registra o resultado, mas não abre ordem.</div>${agenda}
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

function renderFiboManual(){
  const el=document.getElementById('fibo-manual');
  if(!el) return;
  const itens=window.Marcacoes?.itens||[];
  const m=[...itens].reverse().find(x=>x.tipo==='fibo');
  if(!m){
    el.innerHTML='<span class="empty">FIBO GUIADA: use ✎ Fibo, clique no início do impulso M15 e depois no extremo. O monitor não escolhe o swing por você.</span>';
    return;
  }
  const origem=Number(m.preco_a), extremo=Number(m.preco_b);
  const amp=extremo-origem, modulo=Math.abs(amp);
  if(!Number.isFinite(amp)||modulo<=0){
    el.innerHTML='<div class="card fibo-manual"><b>FIBO GUIADA — MARCAÇÃO INVÁLIDA</b><div class="nota">Escolha dois preços diferentes: início e extremo do impulso.</div></div>';
    return;
  }
  // Origem baixa → extremo alto = retração para COMPRA. O inverso é retração
  // para VENDA. Isto impede que a UI chame de venda uma Fibo de impulso alto.
  const direcao=amp>0?'buy':'sell', nome=direcao==='buy'?'CALL':'PUT';
  const f50=extremo-amp*.5, f618=extremo-amp*.618, f786=extremo-amp*.786;
  const zonaInf=Math.min(f50,f618), zonaSup=Math.max(f50,f618);
  const x=(D.ativos||{})[sel]||{}, preco=Number(x.preco);
  const naZona=Number.isFinite(preco)&&preco>=zonaInf&&preco<=zonaSup;
  const invalida=direcao==='buy'?preco<f786:preco>f786;
  const k=(candlesGroq||[]).slice(-2), atual=k[k.length-1]||{};
  const o=Number(atual.o), h=Number(atual.h), l=Number(atual.l), c=Number(atual.c);
  const corpo=Math.abs(c-o), faixa=Math.max(h-l,1e-9);
  const pavio=direcao==='buy'?Math.min(o,c)-l:h-Math.max(o,c);
  const corOk=direcao==='buy'?c>o:c<o;
  const rejeicao=Number.isFinite(pavio)&&corOk&&pavio>=Math.max(corpo*.5,faixa*.18);
  const estado=invalida?'SETUP CANCELADO':naZona?(rejeicao?'ZONA ATIVA — CONFIRME NO M5':'NA ZONA — AGUARDE REJEIÇÃO NO M5'):'AGUARDE A ZONA 50%–61,8%';
  const cor=invalida?'nojan':naZona&&rejeicao?'okjan ok':'nojan';
  const conf=rejeicao?'M15 apoiou a ideia; falta fechar confirmação no M5':'M15 não confirmou apoio; não antecipar M5';
  el.innerHTML=`<div class="card fibo-manual ${cor}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>BINÁRIAS — FIBO GUIADA M15 — ${nome}</b><span class="estado ${naZona&&rejeicao?'study':'wait'}">${estado}</span></div>
    <div class="alvos"><div><label>impulso marcado</label><span>${origem.toFixed(3)} → ${extremo.toFixed(3)}</span></div><div><label>zona de retração</label><span class="fibo-zona">${zonaInf.toFixed(3)} — ${zonaSup.toFixed(3)}</span></div><div><label>preço atual</label><span>${Number.isFinite(preco)?preco.toFixed(3):'—'}</span></div><div><label>cancelar se passar 78,6%</label><span class="dn">${f786.toFixed(3)}</span></div><div><label>gatilho de entrada</label><span class="up">fechamento de rejeição no M5</span></div><div><label>expiração de estudo</label><span>15 min (3 velas M5)</span></div><div><label>apoio M15</label><span>${conf}</span></div></div>
    <div class="nota">${direcao==='buy'?'Impulso de alta: procurar CALL na correção.':'Impulso de baixa: procurar PUT na correção.'} Binárias não têm TP/SL: a operação fecha na expiração. Este cartão é estudo/practice e não envia ordem.</div>
  </div>`;
}

function resumoSimulacao(s,h={}){
  const x=s||{}, d=x.desfecho||'aguardando';
  if(d==='win_tp1') return {texto:'TP1 ✓',cor:'up'};
  if(d==='loss_sl') return {texto:'SL ✕',cor:'dn'};
  if(d.startsWith('nao_executada_')) return {texto:'n/exec',cor:'ind'};
  if(d.startsWith('expirado_')) return {texto:'expirou',cor:'ind'};
  if(d==='nao_elegivel') return {texto:'candidato',cor:'ind'};
  if(d.startsWith('ambíguo')) return {texto:'ambíg',cor:'ind'};
  const av=h.alvos_estudo||h.alvos||{};
  const pendente=['limite','stop','market_next_open'].includes(av.modo_entrada)
    || h.tipo==='fibo_m15';
  const velas=Number(x.velas||0), total=Number(h.horizonte_velas||24);
  if(pendente && x.preenchida!==true)
    return {texto:`aguarda entrada ${velas}/${total}`,cor:'ind'};
  return {texto:`em operação ${velas}/${total}`,cor:'ind'};
}

function renderTp1Curto(){
  const el=document.getElementById('tp1-curto');
  const x=(D.ativos||{})[sel]||{}, c=x.tp1_curto||{}, a=c.alvos||{};
  if(x.classe!=='forex'){
    el.innerHTML='<div class="card curto-card"><b>FORA DO ESTUDO</b><div class="nota">TP1 curto foi desenhado apenas para Forex normal; cripto e ouro seguem seus próprios ritmos.</div></div>';
    return;
  }
  if(!c.disponivel){
    el.innerHTML=`<div class="card curto-card"><b>${c.estado||'AGUARDANDO'}</b><div class="nota">${c.motivo||'Aguardando candles M15.'}</div></div>`;
    return;
  }
  const dir=c.direcao==='sell'?'VENDA':'COMPRA';
  el.innerHTML=`<div class="card curto-card ${c.qualificada?'okjan ok':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center"><b>${sel} — ${dir}</b><span class="estado ${c.qualificada?'go':'study'}">${c.estado}</span></div>
    ${checklist(c)}
    <div class="alvos"><div><label>zona</label><span class="fibo-zona">${c.zona_inf} — ${c.zona_sup}</span></div><div><label>entrada após fechamento</label><span>${a.entrada??'—'}</span></div><div><label>stop tático</label><span class="dn">${a.sl??'—'}</span></div><div><label>TP1 38,2%</label><span class="up">${a.tp1??'—'}</span></div><div><label>R:R</label><span>${c.rr??'—'}x</span></div><div><label>risco</label><span>${a.risco_pips??'—'} ${a.risco_unidade||'pips'} · ${c.risco_atr??'—'} ATR</span></div></div>
    <div class="nota">${c.motivo} Janela de aferição: 4 velas M15 (1h). Não abre posição.</div>
  </div>`;
}

function renderDecisaoPrincipal(){
  const el=document.getElementById('decisao-principal'); if(!el) return;
  const A=D.ativos||{};
  const ks=Object.keys(A);
  // Prioridade: entrada válida > TP1 curto em sombra > outros estudos.
  const entrar=ks.filter(k=>A[k].entrada_valida);
  const curtos=ks.filter(k=>A[k].tp1_curto&&A[k].tp1_curto.qualificada);
  const estudo=ks.filter(k=>A[k].sinal&&!A[k].entrada_valida);
  const candidato=entrar.length
    ? entrar.sort((a,b)=>(A[b].prox||0)-(A[a].prox||0))[0]
    : curtos.length
      ? curtos[0]
    : estudo.length
      ? estudo.sort((a,b)=>(A[b].prox||0)-(A[a].prox||0))[0]
      : ks.sort((a,b)=>(A[b].prox||0)-(A[a].prox||0))[0];
  if(!candidato){ el.innerHTML=''; return; }
  const x=A[candidato];
  if(!dadosMonitorAoVivo){
    el.innerHTML=`<div class="decisao-hero estudo"><div class="decisao-hero-ativo">DADOS ATRASADOS</div><div class="decisao-hero-motivo">O Monitor está sem atualização recente. Não use preço, alvo ou sinal até voltar a atualizar.</div></div>`;
    return;
  }
  const curto=Boolean(x.tp1_curto&&x.tp1_curto.qualificada&&!x.entrada_valida);
  const plano=curto?x.tp1_curto:x;
  const isEntrar=x.entrada_valida;
  const isEstudo=curto||(x.sinal&&!x.entrada_valida);
  const cls=isEntrar?'entrar':isEstudo?'estudo':'';
  const estadoTexto=curto?'TP1 CURTO — ESTUDO':(x.estado_entrada||'AGUARDAR');
  const dir=(plano.direcao||'buy')==='sell'?'SELL':'BUY';
  const v=plano.alvos||x.alvos_estudo||{};
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
    <div class="decisao-hero-motivo">${plano.motivo||x.motivo_entrada||''}</div>
    ${alvosHtml}
  </div>`;
}

function render(){
  const A=D.ativos||{};
  const ks=Object.keys(A).sort();
  const sig=ks.filter(k=>A[k].sinal);
  const curtos=ks.filter(k=>A[k].tp1_curto&&A[k].tp1_curto.qualificada);
  document.getElementById('sinais').innerHTML = sig.length
    ? sig.map(k=>cardSinal(k,A[k])).join('')+curtos.map(k=>cardTp1Curto(k,A[k])).join('')
    : curtos.length ? curtos.map(k=>cardTp1Curto(k,A[k])).join('')
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
  alertarNovoEvento();
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
  renderIaGrafico();
  renderFluxo();
  renderOrb();
  renderLiquidez();
  renderBof();
  renderFvg();
  renderConfluencia();
  renderOuroMovimento();
  renderFibo();
  renderFiboManual();
  renderTp1Curto();
}

async function pick(a){ sel=a; candlesGroq=[]; _primeiroDesenho=true; render(); await grafico();
  if(window.Marcacoes) Marcacoes.carregar(); }

let chartM=null, sCandles=null, sSup=null, sInf=null, sEma9=null, sEma21=null, sEma50=null;
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
  renderFiboManual();
}
function marcarFibo(){ Marcacoes.setModo('fibo'); }
function marcarLinha(){ Marcacoes.setModo('horizontal'); }
function alternarNiveis(){
  niveisDetalhados=!niveisDetalhados;
  localStorage.setItem('monitorNiveisDetalhados',niveisDetalhados?'1':'0');
  grafico();
}
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
  sEma9=chartM.addLineSeries({color:'#22d3ee',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
  sEma21=chartM.addLineSeries({color:'#f59e0b',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
  sEma50=chartM.addLineSeries({color:'#ef4444',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
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

function mediaExponencial(vals,periodo){
  const k=2/(periodo+1); let anterior=null;
  return vals.map(v=>{ anterior=anterior==null?v:(v*k+anterior*(1-k)); return anterior; });
}
function renderHudConfluencia(A){
  const el=document.getElementById('cv'); let hud=document.getElementById('chart-hud');
  if(!hud){ hud=document.createElement('div'); hud.id='chart-hud'; el.appendChild(hud); }
  const q=A.confluencia||{}, e=q.estrutura||{}, dir=q.direcao==='sell'?'SELL':q.direcao==='buy'?'BUY':'—';
  const classe=q.qualificada?'hud-ok':q.score>=6?'hud-wait':'hud-no';
  hud.innerHTML=`<b>CONFLUÊNCIA LOCAL</b><div class="hud-linha"><span>Viés</span><span class="${classe}">${dir} · ${q.score??0}/10</span></div><div class="hud-linha"><span>M15 / H1</span><span>${q.m15||'—'} / ${q.h1||'—'}</span></div><div class="hud-linha"><span>Zona</span><span>${q.em_zona?'DENTRO':'AGUARDAR'}</span></div><div class="hud-linha"><span>FVG / OB</span><span>${q.fvg?.disponivel?'FVG':'—'} / ${q.order_block?'OB':'—'}</span></div><div class="hud-linha"><span>BOS / Liq.</span><span>${q.bos?'✓':'—'} / ${q.liquidez?'✓':'—'}</span></div><div class="hud-linha"><span>Range</span><span>${e.equilibrio??'—'}</span></div>`;
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
  candlesGroq=K;
  aviso.style.display='none';
  if(!_chartMonitor()) return;

  // Marcacoes do ativo anterior nao podem sobreviver a troca de aba.
  linhasNivelM.forEach(l=>sCandles.removePriceLine(l)); linhasNivelM=[];
  seriesZonaM.forEach(s=>chartM.removeSeries(s)); seriesZonaM=[];

  const A=(D.ativos||{})[sel]||{}, F=A.fibo||{}, FA=F.alvos||{}, FL=A.fluxo||{},
        FLA=FL.alvos||{}, O=A.orb||{}, OA=O.alvos||{}, L=A.liquidez||{}, LA=L.alvos||{},
        B=A.bof||{}, BA=B.alvos||{}, G=A.fvg||{}, C=A.tp1_curto||{}, CA=C.alvos||{},
        Q=A.confluencia||{}, QA=Q.alvos||{};
  const btnNiveis=document.getElementById('btn-niveis');
  if(btnNiveis){ btnNiveis.textContent=niveisDetalhados?'☷ Detalhados':'☷ Essenciais'; btnNiveis.classList.toggle('on',niveisDetalhados); }
  const OM=A.ouro_movimento||{}, OMA=OM.alvos||{};
  const candleVals=K.flatMap(k=>[Number(k.h),Number(k.l)]).filter(Number.isFinite);
  const candleMax=Math.max(...candleVals), candleMin=Math.min(...candleVals);
  const faixa=Math.max(candleMax-candleMin,Math.abs(candleMax)*1e-6,1e-9);
  // Um alvo distante nao pode achatar os candles. Niveis fora de 35% da
  // janela continuam no cartao de leitura, mas nao entram no grafico.
  const perto=v=>Number.isFinite(v)&&v>=candleMin-faixa*.35&&v<=candleMax+faixa*.35;

  sCandles.setData(K.map(k=>({time:k.t,open:Number(k.o),high:Number(k.h),
                              low:Number(k.l),close:Number(k.c)})));
  const closes=K.map(k=>Number(k.c));
  const ema=(n)=>{ const valores=mediaExponencial(closes,n); return K.map((k,i)=>({time:k.t,value:valores[i]})); };
  sEma9.setData(ema(9)); sEma21.setData(ema(21)); sEma50.setData(ema(50));
  // Eventos já ocorridos ganham marcador no candle correspondente. Agenda
  // futura permanece no cartão do ouro, pois ainda não há candle para ancorar.
  const noticiasGrafico=(A.noticias_dia||[]).filter(n=>Number(n.quando_ts)>=Number(K[0]?.t)
    &&Number(n.quando_ts)<=Number(K[K.length-1]?.t)).map(n=>({
      time:Number(n.quando_ts), position:'aboveBar', shape:'circle', color:'#fbbf24',
      text:`NOTÍCIA ${n.moeda}: ${n.titulo}`,
    }));
  const ultimoTempo=Number(K[K.length-1]?.t), estruturaMarcadores=[];
  if(Q.fvg?.disponivel) estruturaMarcadores.push({time:ultimoTempo,position:Q.direcao==='sell'?'aboveBar':'belowBar',shape:'arrowUp',color:'#a78bfa',text:'FVG'});
  if(Q.order_block?.vela){
    const alvo=String(Q.order_block.vela).replace(' ','T').slice(0,16);
    const achado=K.find(k=>new Date(k.t*1000).toISOString().slice(0,16)===alvo);
    if(achado) estruturaMarcadores.push({time:achado.t,position:Q.direcao==='sell'?'aboveBar':'belowBar',shape:'circle',color:'#38bdf8',text:'OB'});
  }
  if(Q.bos) estruturaMarcadores.push({time:ultimoTempo,position:Q.direcao==='sell'?'aboveBar':'belowBar',shape:'arrowDown',color:'#14b8a6',text:'BOS'});
  if(typeof sCandles.setMarkers==='function') sCandles.setMarkers([...noticiasGrafico,...estruturaMarcadores]);
  const serieBanda=arr=>(arr||[]).map((v,i)=>{
    const n=Number(v);
    return (v==null||!K[i]||!perto(n))?null:{time:K[i].t,value:n};
  }).filter(Boolean);
  sSup.setData(serieBanda(d.sup));
  sInf.setData(serieBanda(d.inf));

  function nivel(valor,cor,rotulo){
    if(!niveisDetalhados && !String(rotulo).startsWith('Confluência') && rotulo!=='EQUILÍBRIO') return;
    if(!perto(valor)) return;
    linhasNivelM.push(sCandles.createPriceLine({
      price:valor, color:cor, lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.Dashed,
      axisLabelVisible:true, title:rotulo,
    }));
  }
  function zona(v1,v2,cor,rotulo){
    const zonasEssenciais=new Set(['Confluência local: zona','PREMIUM','DESCONTO','SUPPLY','DEMAND']);
    if(!niveisDetalhados && !zonasEssenciais.has(rotulo)) return;
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
  if((A.classe==='forex' || A.classe==='ouro') && B.disponivel){
    nivel(Number(B.nivel_m30),'#06b6d4','BOF nível M30');
    if(B.extremo_varredura!=null) nivel(Number(B.extremo_varredura),'#f59e0b','BOF varredura');
    if(B.sinal_estudo && BA){
      nivel(Number(BA.entrada),'#38bdf8','BOF entrada M5');
      nivel(Number(BA.sl),'#ef4444','BOF SL');
      nivel(Number(BA.tp1),'#22c55e','BOF alvo M30');
    }
  }
  if(F.disponivel){
    zona(F.zona_inf,F.zona_sup,'#8b5cf622','ZONA FIBO 38.2-61.8%');
    nivel(Number(FA.entrada),'#a78bfa','Fibo entrada 61.8%');
    nivel(Number(F.fib786),'#f59e0b','Fibo invalida 78.6%');
    nivel(Number(FA.tp1),'#22c55e','Fibo TP1');
    nivel(Number(FA.tp2),'#86efac','Fibo TP2 127.2%');
  }
  if(G.disponivel){
    const cor=G.direcao==='sell'?'#ef44441d':'#14b8a61d';
    zona(G.zona_inf,G.zona_sup,cor,'FVG M15 '+String(G.direcao||'').toUpperCase());
  }
  if(Q.disponivel && Q.zona_inf!=null){
    zona(Q.zona_inf,Q.zona_sup,Q.direcao==='sell'?'#ef444421':'#a78bfa21','Confluência local: zona');
    nivel(Number(QA.entrada),'#a78bfa','Confluência: entrada');
    nivel(Number(QA.sl),'#ef4444','Confluência: SL');
    nivel(Number(QA.tp1),'#22c55e','Confluência: TP1');
    nivel(Number(QA.tp2),'#86efac','Confluência: TP2');
  }
  const QS=Q.estrutura||{};
  if(QS.equilibrio!=null){
    zona(QS.equilibrio,QS.maximo,'#f59e0b14','PREMIUM');
    zona(QS.minimo,QS.equilibrio,'#06b6d414','DESCONTO');
    nivel(Number(QS.equilibrio),'#94a3b8','EQUILÍBRIO');
    zona(QS.supply_inf,QS.supply_sup,'#ef44441a','SUPPLY');
    zona(QS.demand_inf,QS.demand_sup,'#14b8a61a','DEMAND');
  }
  if(A.classe==='ouro' && OM.disponivel && OM.zona_inf!=null){
    zona(OM.zona_inf,OM.zona_sup,OM.direcao==='sell'?'#ef444424':'#fbbf2424','OURO: zona FVG');
    if(OM.sinal_estudo && OMA){
      nivel(Number(OMA.entrada),'#fbbf24','Ouro entrada estudo');
      nivel(Number(OMA.sl),'#ef4444','Ouro SL técnico');
      nivel(Number(OMA.tp1),'#22c55e','Ouro TP1 1.5R');
    }
  }
  if(C.qualificada && CA){
    zona(C.zona_inf,C.zona_sup,'#f973161f','TP1 curto: zona');
    nivel(Number(CA.entrada),'#38bdf8','TP1 curto entrada');
    nivel(Number(CA.sl),'#ef4444','TP1 curto SL tático');
    nivel(Number(CA.tp1),'#22c55e','TP1 curto saída');
  }
  if(A.entrada_valida && A.alvos){
    nivel(Number(A.alvos.entrada),'#22c55e','entrada BUY');
    nivel(Number(A.alvos.sl),'#ef4444','SL');
    nivel(Number(A.alvos.tp1),'#22c55e','TP1');
    nivel(Number(A.alvos.tp2),'#86efac','TP2');
  }
  titulo.textContent=`${sel} \u00b7 ${RN[A.regime]||'\u2014'} \u00b7 R2 ${A.r2??'\u2014'}`;
  renderHudConfluencia(A);
  // So depois de todas as series: enquadrar antes deixava a grade errada.
  if(_primeiroDesenho){ chartM.timeScale().fitContent(); _primeiroDesenho=false; }
}

async function tick(){
  try{
    D=await (await fetch('mercado.json?t='+Date.now())).json();
    const atualizado=new Date(Number(D.ts||0)*1000);
    const idade=Math.max(0,Date.now()-atualizado.getTime());
    dadosMonitorAoVivo=idade<=90000;
    document.getElementById('status').textContent=dadosMonitorAoVivo
      ? 'Atualizado: '+FMT_HORA_BRT.format(atualizado)+' BRT'
      : 'DADOS ATRASADOS — NÃO OPERAR';
    document.getElementById('status').style.color=dadosMonitorAoVivo?'':'#f87171';
    render(); await grafico();
  }catch(e){ document.getElementById('status').textContent='Erro: '+e; }
}
atualizarBotaoSomMonitor();
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
    analista_groq = AnalistaGraficoGroq()
    print("IA do gráfico: Groq=" + ("ATIVO" if os.getenv("GROQ_API_KEY", "").strip() else "OFF"))
    try:
        loop(api, cfg, estado, calendario, analista_groq)
    except KeyboardInterrupt:
        print("\nParado.")
    return 0


if __name__ == "__main__":
    from iqoption_m5.log_arquivo import ativar as ativar_log
    print(f"Log desta sessao: {ativar_log('monitor_mercado')}")
    sys.exit(main())
