"""Inicia o monitor. Use --m1 para candles de 1 minuto em vez de M5."""

import argparse
import logging

from iqoption_m5.app import main
from iqoption_m5.config import Configuracao, configuracao_m1, configuracao_real_m5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def analisar_argumentos():
    parser = argparse.ArgumentParser(description="Monitor IQ Option.")
    parser.add_argument(
        "--m1",
        action="store_true",
        help="usa candles de 1 minuto e expiração de 1 minuto (padrão: M5 com 5 minutos)",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="opera com DINHEIRO REAL (R$2/entrada, max 5/dia, stop R$10/dia, piso R$25). Exige --confirmo.",
    )
    parser.add_argument(
        "--confirmo",
        action="store_true",
        help="confirmação explícita exigida junto com --real",
    )
    return parser.parse_args()


if __name__ == "__main__":
    argumentos = analisar_argumentos()
    if argumentos.real and not argumentos.confirmo:
        raise SystemExit(
            "Pra operar com dinheiro real, rode com --real --confirmo (os dois juntos). "
            "Isso existe de propósito, pra ninguém ligar --real sem querer."
        )
    if argumentos.real:
        main(configuracao_real_m5())
    else:
        from dataclasses import replace
        config_practice = replace(
            configuracao_m1() if argumentos.m1 else Configuracao(),
            engulfing_sr_ativo=True,
            divergencia_rsi_ativo=True,
            bollinger_squeeze_ativo=True,
        )
        main(config_practice)
