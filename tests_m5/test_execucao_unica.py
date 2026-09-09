"""Protege perfis que enviam dinheiro real contra duas instâncias locais."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from iqoption_m5.execucao_unica import ExecucaoDuplicada, ExecucaoUnica


def test_recusa_segunda_instancia_enquanto_primeira_perfil_real_esta_aberta(tmp_path):
    """O segundo processo não pode duplicar a mesma ordem da primeira sessão."""
    codigo = "\n".join(
        (
            "from iqoption_m5.execucao_unica import ExecucaoUnica",
            "import sys, time",
            "with ExecucaoUnica('ema_m5_real', sys.argv[1]):",
            "    print('TRAVADO', flush=True)",
            "    time.sleep(3)",
        )
    )
    primeiro = subprocess.Popen(
        [sys.executable, "-c", codigo, str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert primeiro.stdout is not None
        assert primeiro.stdout.readline().strip() == "TRAVADO"
        with pytest.raises(ExecucaoDuplicada):
            with ExecucaoUnica("ema_m5_real", tmp_path):
                pass
    finally:
        primeiro.terminate()
        primeiro.wait(timeout=5)
