"""Entrada explícita para o perfil EMA M5 REAL conservador."""

from iqoption_m5.app import main
from iqoption_m5.config import configuracao_ema_m5_real
from iqoption_m5.execucao_unica import ExecucaoDuplicada, ExecucaoUnica
from iqoption_m5.log_arquivo import ativar as ativar_log


if __name__ == "__main__":
    # Log em arquivo antes de qualquer ordem. Num perfil real, o motivo de uma
    # recusa ou de um bloqueio nao pode morrer junto com a janela do terminal.
    try:
        with ExecucaoUnica("ema_m5_real", "iqoption_m5/dados"):
            caminho = ativar_log("ema_m5_real")
            print(f"Log desta sessão: {caminho}")
            main(configuracao_ema_m5_real())
    except ExecucaoDuplicada as erro:
        print(f"INÍCIO BLOQUEADO: {erro}. Feche a sessão anterior antes de reiniciar.")
