"""Perfil de movimento por hora UTC, calculado do historico de qualquer ativo.

Generaliza ``perfil_horario_xauusd``, que traz a mesma tabela fixa para o ouro.
A diferenca e a origem: aqui os percentis saem das velas que o chamador ja tem
em memoria, entao cada ativo recebe a propria escala em vez de herdar a do
ouro. Isso importa porque a escala nao e comparavel — o ouro desloca perto de
0,4% em 6h e um par de forex uma fracao disso, de modo que reaproveitar a
tabela do ouro num par daria alvo inalcancavel e stop largo demais.

As definicoes seguem ``perfil_movimento.magnitude``, para que os dois modulos
falem da mesma coisa:

``amplitude``   topo a fundo da janela: o quanto o preco passeia.
``deslocamento`` |fim - inicio|: o quanto ele efetivamente sai do lugar.
``contra``      excursao adversa agnostica de direcao — por ancora, o MENOR
                entre o que machuca uma compra e o que machuca uma venda.
                Serve de piso para o stop sem presumir lado, que e como o
                perfil do ouro ja era usado; tomar o maior transformaria o
                piso em teto e engoliria o stop tecnico quase sempre.

A janela comeca na vela SEGUINTE a ancora. Usar a propria vela da decisao seria
ler um preco que ainda nao fechou.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Abaixo disso a distribuicao descreve uma semana de mercado, nao o ativo.
MINIMO_CONFIAVEL = 5000
HORIZONTE_PADRAO = 24  # 24 velas M15 = 6h

_SESSOES = ((range(0, 7), "ásia"), (range(7, 13), "europa"),
            (range(13, 18), "ny"), (range(18, 24), "noite"))


def sessao(hora_utc: int) -> str:
    for faixa, nome in _SESSOES:
        if hora_utc % 24 in faixa:
            return nome
    return "ásia"


def tabela(df: pd.DataFrame, horizonte: int = HORIZONTE_PADRAO,
           minimo: int = MINIMO_CONFIAVEL) -> dict[int, tuple[float, ...]] | None:
    """Percentis por hora UTC, ou None quando a amostra nao sustenta.

    Devolver None em vez de uma tabela fraca e deliberado: um alvo calibrado
    por poucas centenas de velas parece medido e nao e. Quem chama decide o
    que fazer sem perfil — no Monitor, nao emitir sinal.
    """
    if df is None or len(df) < max(minimo, horizonte + 2):
        return None
    if not {"High", "Low", "Close"} <= set(df.columns):
        return None
    futuro_max = df["High"].rolling(horizonte).max().shift(-horizonte)
    futuro_min = df["Low"].rolling(horizonte).min().shift(-horizonte)
    futuro_fim = df["Close"].shift(-horizonte)
    base = df["Close"]

    amplitude = (futuro_max - futuro_min) / base * 100
    deslocamento = (futuro_fim - base).abs() / base * 100
    contra_compra = (base - futuro_min) / base * 100
    contra_venda = (futuro_max - base) / base * 100
    contra = pd.concat([contra_compra, contra_venda], axis=1).min(axis=1)

    if isinstance(df.index, pd.DatetimeIndex):
        horas = df.index.hour
    else:
        return None
    quadro = pd.DataFrame({
        "hora": np.asarray(horas), "amp": amplitude,
        "desl": deslocamento, "contra": contra,
    }).dropna()
    if len(quadro) < minimo:
        return None

    saida: dict[int, tuple[float, ...]] = {}
    for hora, grupo in quadro.groupby("hora"):
        # Uma hora com pouquissima amostra ficaria com percentil instavel e
        # contaminaria o alvo daquela faixa; melhor nao ter a hora.
        if len(grupo) < 30:
            continue
        saida[int(hora)] = (
            round(float(grupo["amp"].quantile(.25)), 4),
            round(float(grupo["amp"].quantile(.50)), 4),
            round(float(grupo["amp"].quantile(.75)), 4),
            round(float(grupo["desl"].quantile(.50)), 4),
            round(float(grupo["desl"].quantile(.75)), 4),
            round(float(grupo["contra"].quantile(.50)), 4),
        )
    return saida or None


def contexto(tab: dict[int, tuple[float, ...]] | None, hora_utc: int,
             preco: float) -> dict | None:
    """Movimento esperado para a hora, em pontos do ativo. None sem perfil."""
    if not tab or preco <= 0:
        return None
    hora = hora_utc % 24
    valores = tab.get(hora)
    if valores is None:
        return None
    ap25, ap50, ap75, dp50, dp75, cp50 = valores

    def pts(pct: float) -> float:
        return round(preco * pct / 100, 5)

    return {
        "hora_utc": hora, "sessao": sessao(hora),
        "amp_p25_pct": ap25, "amp_p50_pct": ap50, "amp_p75_pct": ap75,
        "tp1_pct": dp50, "tp2_pct": dp75, "sl_min_pct": cp50,
        "amp_p25_pts": pts(ap25), "amp_p50_pts": pts(ap50),
        "amp_p75_pts": pts(ap75), "tp1_pts": pts(dp50), "tp2_pts": pts(dp75),
        "sl_min_pts": pts(cp50),
    }


def regime(tab: dict[int, tuple[float, ...]] | None, atr_pct: float,
           hora_utc: int) -> str:
    """Classifica o ATR atual contra a faixa normal da hora.

    Os limiares repetem os de ``perfil_horario_xauusd.regime_atr``: o ATR14 e
    amplitude de UMA vela e os percentis sao de 24, entao a comparacao usa uma
    fracao direta em vez do fator de escala exato.
    """
    if not tab:
        return "normal"
    valores = tab.get(hora_utc % 24)
    if valores is None:
        return "normal"
    ap25, _, ap75 = valores[:3]
    if atr_pct < ap25 * 0.10:
        return "quieto"
    if atr_pct > ap75 * 0.30:
        return "ativo"
    return "normal"
