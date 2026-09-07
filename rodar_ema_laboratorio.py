"""Iniciador único do laboratório EMA M5/M15 em conta PRACTICE."""

from iqoption_m5.laboratorio_ema import executar_laboratorio_ema
from iqoption_m5.log_arquivo import ativar as ativar_log


if __name__ == "__main__":
    caminho = ativar_log("lab_ema")
    print(f"Log desta sessão: {caminho}")
    executar_laboratorio_ema()
