"""Inicia o monitor. Use --m1 para candles de 1 minuto em vez de M5."""

import argparse
import logging

from iqoption_m5.app import main
from iqoption_m5.config import (
    Configuracao,
    configuracao_m1,
    configuracao_pesquisa_m5,
    configuracao_practice_m5,
    configuracao_real_m5,
    configuracao_scalping_60,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def analisar_argumentos(argv=None):
    parser = argparse.ArgumentParser(
        description="Pesquisa de estratégias IQ Option; sem perfil, nenhuma ordem é enviada."
    )
    parser.add_argument(
        "--m1",
        action="store_true",
        help="usa candles de 1 minuto e expiração de 1 minuto (padrão: M5 com 5 minutos)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="opera com DINHEIRO REAL usando o perfil protegido. Exige --confirmo.",
    )
    parser.add_argument(
        "--scalping",
        action="store_true",
        help="perfil scalping R$60 real: R$1/entrada, stop -R$6/dia, 5 pares selecionados. Exige --confirmo.",
    )
    parser.add_argument(
        "--practice",
        action="store_true",
        help="envia ordens na conta PRACTICE. Exige --confirmo; sem esta opção apenas monitora.",
    )
    parser.add_argument(
        "--confirmo",
        action="store_true",
        help="confirmação explícita exigida para qualquer perfil que envie ordens",
    )
    return parser.parse_args(argv)


def selecionar_configuracao(argumentos) -> Configuracao:
    """Transforma argumentos validados em um único perfil de execução."""
    perfis_execucao = sum(bool(v) for v in (argumentos.practice, argumentos.real, argumentos.scalping))
    if perfis_execucao > 1:
        raise SystemExit("Escolha apenas um perfil: --practice, --real ou --scalping.")
    if perfis_execucao and not argumentos.confirmo:
        raise SystemExit(
            "Para enviar ordens, use o perfil desejado junto com --confirmo. "
            "Sem confirmação o programa permanece somente monitor."
        )
    if argumentos.scalping:
        return configuracao_scalping_60()
    if argumentos.real:
        return configuracao_real_m5()
    if argumentos.practice:
        config = configuracao_practice_m5()
        if argumentos.m1:
            config = configuracao_m1(config)
        return config

    config = configuracao_pesquisa_m5()
    return configuracao_m1(config) if argumentos.m1 else config


if __name__ == "__main__":
    main(selecionar_configuracao(analisar_argumentos()))
