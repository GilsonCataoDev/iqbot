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
    configuracao_scalping_m1,
    configuracao_scalping_m15,
)
from dataclasses import replace

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
        help="perfil scalping R$60 REAL: anti-martingale 15/20/25, stop -R$30, meta +R$25. Exige --confirmo.",
    )
    parser.add_argument(
        "--scalping-practice",
        dest="scalping_practice",
        action="store_true",
        help="mesmo perfil scalping M5 R$60 mas na conta PRACTICE. Exige --confirmo.",
    )
    parser.add_argument(
        "--scalping-m15",
        dest="scalping_m15",
        action="store_true",
        help="perfil scalping M15 REAL: candles 15 min, anti-martingale 15/20/25. Exige --confirmo.",
    )
    parser.add_argument(
        "--scalping-m15-practice",
        dest="scalping_m15_practice",
        action="store_true",
        help="mesmo perfil scalping M15 mas na conta PRACTICE, sem limites de sessão. Exige --confirmo.",
    )
    parser.add_argument(
        "--scalping-m1",
        dest="scalping_m1",
        action="store_true",
        help="perfil scalping M1 REAL: candles 1 min, expiração 1 min, só pullback. Exige --confirmo.",
    )
    parser.add_argument(
        "--scalping-m1-practice",
        dest="scalping_m1_practice",
        action="store_true",
        help="mesmo perfil scalping M1 mas na conta PRACTICE, sem limites de sessão. Exige --confirmo.",
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
    parser.add_argument(
        "--no-browser",
        dest="no_browser",
        action="store_true",
        help="não abre o browser automaticamente (modo headless/VPS); gráfico ainda disponível via SSH tunnel",
    )
    return parser.parse_args(argv)


_PRATICA_SEM_LIMITES = dict(
    conta="PRACTICE",
    confirmo_conta_real=False,
    piso_banca=0.0,
    max_operacoes_dia=0,
    meta_diaria=0.0,
    stop_diario=-99999.0,
    parar_por_prejuizo=False,
    circuit_breaker_max_perdas=0,
    drawdown_maximo_percentual=0.0,
)


def selecionar_configuracao(argumentos) -> Configuracao:
    """Transforma argumentos validados em um único perfil de execução."""
    perfis_execucao = sum(bool(v) for v in (
        argumentos.practice, argumentos.real,
        argumentos.scalping, argumentos.scalping_practice,
        argumentos.scalping_m15, argumentos.scalping_m15_practice,
        argumentos.scalping_m1, argumentos.scalping_m1_practice,
    ))
    if perfis_execucao > 1:
        raise SystemExit("Escolha apenas um perfil por vez.")
    if perfis_execucao and not argumentos.confirmo:
        raise SystemExit(
            "Para enviar ordens, use o perfil desejado junto com --confirmo. "
            "Sem confirmação o programa permanece somente monitor."
        )
    if argumentos.scalping_m1_practice:
        return replace(configuracao_scalping_m1(), **_PRATICA_SEM_LIMITES)
    if argumentos.scalping_m1:
        return configuracao_scalping_m1()
    if argumentos.scalping_m15_practice:
        return replace(configuracao_scalping_m15(), **_PRATICA_SEM_LIMITES)
    if argumentos.scalping_m15:
        return configuracao_scalping_m15()
    if argumentos.scalping_practice:
        return replace(configuracao_scalping_60(), **_PRATICA_SEM_LIMITES)
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
    args = analisar_argumentos()
    config = selecionar_configuracao(args)
    if args.no_browser:
        config = replace(config, abrir_navegador=False)
    main(config)
