"""Iniciador do laboratório EMA M5/M15 em conta REAL.

Somente EURUSD+AUDCAD M5 ema920_pullback enviam ordens reais.
Todos os demais rastros (EURJPY, M15, Fibo, NZD…) continuam em sombra.
"""

from iqoption_m5.execucao_unica import ExecucaoDuplicada, ExecucaoUnica
from iqoption_m5.laboratorio_ema import executar_laboratorio_ema_real
from iqoption_m5.log_arquivo import ativar as ativar_log


if __name__ == "__main__":
    try:
        with ExecucaoUnica("lab_ema_real", "iqoption_m5/dados"):
            caminho = ativar_log("lab_ema_real")
            print(f"Log desta sessão: {caminho}")
            executar_laboratorio_ema_real()
    except ExecucaoDuplicada as erro:
        print(f"INÍCIO BLOQUEADO: {erro}. Feche a sessão anterior antes de reiniciar.")
