"""Entrada explícita para o perfil EMA M5 REAL conservador."""

from iqoption_m5.app import main
from iqoption_m5.config import configuracao_ema_m5_real


if __name__ == "__main__":
    main(configuracao_ema_m5_real())
