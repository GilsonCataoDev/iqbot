"""Executor paper de Forex: nenhuma chamada de envio à corretora."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .forex_modelos import PlanoForex, PosicaoForex, ResultadoForex


class ExecutorForexSimulado:
    def __init__(
        self,
        banca: float = 1000.0,
        risco_percentual: float = 0.0025,
        spread: float = 0.00010,
    ):
        if banca <= 0 or not 0 < risco_percentual <= 0.02 or spread < 0:
            raise ValueError("Configuração de risco/spread inválida.")
        self.banca = float(banca)
        self.risco_percentual = float(risco_percentual)
        self.spread = float(spread)
        self.posicao: PosicaoForex | None = None
        self.resultados: list[ResultadoForex] = []

    def abrir(self, plano: PlanoForex, abertura: float, horario: datetime) -> PosicaoForex:
        if self.posicao is not None:
            raise RuntimeError("Já existe posição Forex aberta.")
        entrada = abertura + self.spread / 2 if plano.lado == "buy" else abertura - self.spread / 2
        risco_preco = entrada - plano.stop if plano.lado == "buy" else plano.stop - entrada
        if risco_preco <= 0:
            raise ValueError("Stop precisa ficar do lado oposto da entrada.")
        risco_dinheiro = self.banca * self.risco_percentual
        quantidade = risco_dinheiro / risco_preco
        # Preserva o R:R do plano usando o preço efetivamente executado.
        rr = abs(plano.alvo - (plano.stop + plano.risco_preco if plano.lado == "buy" else plano.stop - plano.risco_preco)) / plano.risco_preco
        alvo = entrada + rr * risco_preco if plano.lado == "buy" else entrada - rr * risco_preco
        self.posicao = PosicaoForex(
            ativo=plano.ativo,
            lado=plano.lado,
            aberta_em=horario,
            entrada=entrada,
            stop=plano.stop,
            alvo=alvo,
            quantidade=quantidade,
            risco_dinheiro=risco_dinheiro,
        )
        return replace(self.posicao)

    def atualizar(self, horario: datetime, maxima: float, minima: float) -> ResultadoForex | None:
        p = self.posicao
        if p is None:
            return None
        if p.lado == "buy":
            stop_atingido = minima - self.spread / 2 <= p.stop
            alvo_atingido = maxima - self.spread / 2 >= p.alvo
        else:
            stop_atingido = maxima + self.spread / 2 >= p.stop
            alvo_atingido = minima + self.spread / 2 <= p.alvo
        if not stop_atingido and not alvo_atingido:
            return None
        # Conservador: se OHLC não revela a ordem intrabar, contabiliza o stop.
        motivo = "stop" if stop_atingido else "alvo"
        saida = p.stop if stop_atingido else p.alvo
        lucro = (
            (saida - p.entrada) * p.quantidade
            if p.lado == "buy"
            else (p.entrada - saida) * p.quantidade
        )
        resultado = ResultadoForex(
            ativo=p.ativo,
            lado=p.lado,
            aberta_em=p.aberta_em,
            fechada_em=horario,
            entrada=p.entrada,
            saida=saida,
            quantidade=p.quantidade,
            lucro=lucro,
            motivo_saida=motivo,
        )
        self.banca += lucro
        self.resultados.append(resultado)
        self.posicao = None
        return resultado
