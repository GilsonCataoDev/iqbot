"""Desfecho da opcao binaria como a corretora o apura.

O backtest media entrada na abertura da vela M5 e saida no fechamento de
outra. Contra 19 ordens reais isso errou 32% dos desfechos: a opcao abre ao
preco do instante da compra, nao da abertura da vela, e expira num marco de
relogio da IQ, nao em compra+N. Num horizonte de 15 minutos qualquer desvio
nas pontas inverte o resultado, e o erro nao aparece — os dois numeros
continuam plausiveis.

Conferido contra 19 ordens que a IQ liquidou: 89% de concordancia, contra
68% do metodo antigo. `validar_medicao.py` repete a conferencia — refaca a
cada mudanca aqui, porque os dois numeros seguem plausiveis quando um
detalhe silencioso quebra.
"""
from __future__ import annotations

import pandas as pd

def vencimento(compra: pd.Timestamp, minutos: int) -> pd.Timestamp:
    """Instante em que a opcao expira: a compra mais os minutos pedidos.

    Houve a hipotese de que a IQ arredondasse para quartos de hora, o que
    faria uma opcao de "15 minutos" durar de 10 a 20. Conferido contra as 19
    ordens reais, ela nao se sustenta: as 19 registram expiracao_minutos=15 e
    a distancia entre enviada_em e encerrada_em e de 15 minutos em todas, com
    poucos segundos de folga do laco que busca o resultado. Arredondar piorou
    a concordancia de 89% para 79%.
    """
    return compra + pd.Timedelta(minutes=minutos)


def preco_em(candles: pd.DataFrame, quando: pd.Timestamp) -> float | None:
    """Preco no instante `quando`, ou None se estiver fora do dado.

    Usa a ABERTURA da barra que contem o instante, nao o fechamento. Numa
    barra OHLC o unico ponto com horario conhecido e a abertura: o fechamento
    vale para o fim da barra. Ler o Close de uma barra M1 para uma compra aos
    9 segundos devolveria o preco de 50 segundos depois — lookahead que
    inverte desfechos sem deixar rastro.

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
    return float(candles.iloc[pos]["Open"])


def desfecho_binario(
    candles: pd.DataFrame,
    direcao: str,
    compra: pd.Timestamp,
    minutos: int,
) -> dict | None:
    """Apura a opcao: strike no instante da compra, saida no vencimento.

    `candles` precisa ser fino o bastante para o instante da compra significar
    algo — M1 ou menos. Com M5 o strike vira a media de uma janela de cinco
    minutos e o resultado volta a ser suposicao.

    Conferido contra 19 ordens que a IQ liquidou, concorda em 17 (89%), contra
    68% do metodo que usava velas M5 fixas. As 2 restantes divergem por 2,9 e
    6,8 pips — margem grande demais para granularidade, o que sugere que a
    corretora liquida sobre um fluxo de cotacao diferente do que get_candles
    devolve. Ou seja: 89% e o teto conhecido deste metodo, nao 100%.
    """
    strike = preco_em(candles, compra)
    if strike is None:
        return None
    expira = vencimento(compra, minutos)
    final = preco_em(candles, expira)
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
        "vencimento": expira,
        "duracao_s": (expira - compra).total_seconds(),
    }
