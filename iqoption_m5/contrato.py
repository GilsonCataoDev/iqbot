"""Desfecho da opcao binaria como a corretora o apura.

O backtest media entrada na abertura da vela M5 e saida no fechamento de
outra. Contra 19 ordens reais isso errou 32% dos desfechos: a opcao abre ao
preco do instante da compra, nao da abertura da vela, e expira num marco de
relogio da IQ, nao em compra+N. Num horizonte de 15 minutos qualquer desvio
nas pontas inverte o resultado, e o erro nao aparece — os dois numeros
continuam plausiveis.

A regra de vencimento aqui e uma hipotese a ser conferida contra ordens
reais, nao um fato assumido: use `validar_medicao.py`.
"""
from __future__ import annotations

import pandas as pd

# A IQ oferece vencimento de binaria nos quartos de hora. Um pedido de "15
# minutos" as 14:09 nao expira as 14:24: ela escolhe o marco mais proximo do
# alvo (medido em producao: 14:09 pedindo 30min expirou 14:45).
MARCO_MINUTOS = 15
# Vencimento colado demais na compra e recusado; a IQ pula para o marco
# seguinte em vez de aceitar uma opcao de poucos segundos.
MINIMO_ATE_VENCIMENTO_S = 60


def marco_vencimento(
    compra: pd.Timestamp, minutos_alvo: int, marco_minutos: int = MARCO_MINUTOS,
) -> pd.Timestamp:
    """Marco de relogio em que a opcao realmente expira."""
    alvo = compra + pd.Timedelta(minutes=minutos_alvo)
    passo = pd.Timedelta(minutes=marco_minutos)
    anterior = alvo.floor(f"{marco_minutos}min")
    candidatos = [anterior, anterior + passo]
    escolhido = min(candidatos, key=lambda m: (abs(m - alvo), m))
    while (escolhido - compra).total_seconds() < MINIMO_ATE_VENCIMENTO_S:
        escolhido += passo
    return escolhido


def preco_em(candles: pd.DataFrame, quando: pd.Timestamp) -> float | None:
    """Ultimo preco negociado ate `quando`, ou None se estiver fora do dado.

    Devolve None em vez do preco mais proximo: extrapolar para fora da serie
    produziria um desfecho inventado, e um desfecho inventado nao se distingue
    de um medido depois que vira estatistica.
    """
    if candles.empty or quando < candles.index[0]:
        return None
    ultimo = candles.index[-1]
    if quando > ultimo + (ultimo - candles.index[-2] if len(candles) > 1 else pd.Timedelta(0)):
        return None
    pos = candles.index.searchsorted(quando, side="right") - 1
    if pos < 0:
        return None
    return float(candles.iloc[pos]["Close"])


def desfecho_binario(
    candles: pd.DataFrame,
    direcao: str,
    compra: pd.Timestamp,
    minutos_alvo: int,
    marco_minutos: int = MARCO_MINUTOS,
) -> dict | None:
    """Apura a opcao: strike no instante da compra, saida no vencimento real.

    `candles` precisa ser fino o bastante para o instante da compra significar
    algo — M1 ou menos. Com M5 o strike vira a media de uma janela de cinco
    minutos e o resultado volta a ser suposicao.
    """
    strike = preco_em(candles, compra)
    if strike is None:
        return None
    vencimento = marco_vencimento(compra, minutos_alvo, marco_minutos)
    final = preco_em(candles, vencimento)
    if final is None:
        return None
    if final == strike:
        resultado = "empate"
    else:
        subiu = final > strike
        acertou = subiu if direcao == "call" else not subiu
        resultado = "win" if acertou else "loss"
    return {
        "resultado": resultado,
        "strike": strike,
        "preco_final": final,
        "vencimento": vencimento,
        "duracao_s": (vencimento - compra).total_seconds(),
    }
