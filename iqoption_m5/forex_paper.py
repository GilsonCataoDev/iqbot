"""Paper CFD: o que sobra de um estudo depois do spread.

A simulação do Monitor mede o Fibo M15 em múltiplos de R usando preço médio:
compra em ``entrada``, sai em ``sl`` ou ``tp1``, sem custo nenhum. Isso responde
"o sinal antecipa o movimento?" — e responde bem. Não responde "dá para operar
isso num CFD?", que é outra pergunta: no CFD você compra no ask e vende no bid,
e a diferença sai do bolso a cada ida e volta.

Este módulo faz a conta que falta. Não envia ordem, não sugere ordem, e não se
mistura com a binária: a régua aqui é R depois do custo, e a da binária é taxa
de acerto contra o payout. Somar as duas dá um número que não existe.

Limite conhecido e deliberado: mantemos os MESMOS desfechos que o replay a
preço médio encontrou. O spread real também provoca stops que o preço médio
nunca registrou, então o prejuízo medido aqui é um PISO, nunca um teto.
"""
from __future__ import annotations

from dataclasses import dataclass

# Spread de referência do varejo, em preço, por lado somado (ida + volta = 1x).
# São premissas, não medição: quando o operador tiver o spread real da conta,
# troca aqui. Valores propositalmente do lado otimista — se o estudo não passa
# nem com spread otimista, não passa.
SPREAD_ABSOLUTO: dict[str, float] = {
    "EURUSD": 0.00010, "GBPUSD": 0.00015, "AUDUSD": 0.00015,
    "NZDUSD": 0.00020, "USDCAD": 0.00020, "EURGBP": 0.00015,
    "EURCHF": 0.00020, "GBPCHF": 0.00030, "CADCHF": 0.00030,
    "EURCAD": 0.00030, "GBPCAD": 0.00040,
    "USDJPY": 0.015, "EURJPY": 0.020, "GBPJPY": 0.030,
    "XAUUSD": 0.30,
}
# Cripto cota spread proporcional; um valor fixo erra por ordem de grandeza.
SPREAD_PROPORCIONAL: dict[str, float] = {
    "BTCUSD": 0.0005, "ETHUSD": 0.0005, "XRPUSD": 0.0010,
}
# Fallback para ativo sem tabela: proporcional, para não subestimar em silêncio.
SPREAD_PROPORCIONAL_PADRAO = 0.0002

# Tamanho de contrato por lote. Só serve para traduzir R em dinheiro, e varia
# por classe: aplicar 100 mil unidades em BTC dá um número sem sentido.
UNIDADES_POR_LOTE = 100_000          # forex, padrão
CONTRATO: dict[str, float] = {"XAUUSD": 100.0,   # onças
                              "BTCUSD": 1.0, "ETHUSD": 1.0, "XRPUSD": 1.0}


def unidades_de(ativo: str) -> float:
    """Unidades por lote do ativo."""
    return CONTRATO.get(ativo, UNIDADES_POR_LOTE)


def spread_de(ativo: str, preco: float) -> float:
    """Spread em preço. Absoluto quando tabelado, proporcional quando não."""
    if ativo in SPREAD_ABSOLUTO:
        return SPREAD_ABSOLUTO[ativo]
    fracao = SPREAD_PROPORCIONAL.get(ativo, SPREAD_PROPORCIONAL_PADRAO)
    return abs(preco) * fracao


@dataclass(frozen=True)
class ResultadoPaper:
    ativo: str
    risco_preco: float
    spread: float
    custo_em_r: float          # quanto do R o spread come, só de ida e volta
    r_sem_custo: float
    r_com_custo: float
    operavel: bool             # False quando o spread engole o stop
    lucro_por_lote: float      # em moeda de cotação, para o lote informado


def avaliar(ativo: str, entrada: float, sl: float, r_sem_custo: float,
            spread: float | None = None,
            lote: float = 0.1) -> ResultadoPaper | None:
    """Converte um R medido a preço médio no R que sobraria num CFD.

    Devolve None quando falta preço ou o stop é degenerado — nunca chuta.
    """
    if not entrada or sl is None or r_sem_custo is None:
        return None
    risco = abs(float(entrada) - float(sl))
    if risco <= 0:
        return None
    s = spread_de(ativo, float(entrada)) if spread is None else float(spread)
    custo_r = s / risco
    return ResultadoPaper(
        ativo=ativo,
        risco_preco=risco,
        spread=s,
        custo_em_r=round(custo_r, 3),
        r_sem_custo=round(float(r_sem_custo), 3),
        r_com_custo=round(float(r_sem_custo) - custo_r, 3),
        # Stop menor que o spread não é operável: a posição já nasce além do
        # stop. Não é "R ruim", é ordem que a corretora não deixa existir.
        operavel=risco > s,
        lucro_por_lote=round(
            (float(r_sem_custo) * risco - s) * unidades_de(ativo) * lote, 2),
    )
