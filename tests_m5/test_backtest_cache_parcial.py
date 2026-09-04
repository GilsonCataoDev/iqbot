"""O candle parcial não pode ser engolido pelo cache de indicadores.

`calcular_indicadores` guarda o resultado por nome de ativo e só invalida
quando `index[-2]` ou o tamanho mudam. O candle cheio e o parcial têm os dois
iguais — só a ÚLTIMA linha difere. Com a mesma chave, a segunda chamada devolve
a primeira, e os setups de reversão passam a ser avaliados com o Close final da
vela em que vão entrar. Era exatamente o lookahead que o método M1-parcial
existe pra remover.
"""
from __future__ import annotations

import contextlib
import io

import pandas as pd

from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.estrategia import EstrategiaReversaoM5


def _janela(n: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2026-08-25 00:00", periods=n, freq="15min")
    base = [1.1000 + i * 0.0001 for i in range(n)]
    return pd.DataFrame(
        {
            "Open": base,
            "High": [v + 0.0005 for v in base],
            "Low": [v - 0.0005 for v in base],
            "Close": [v + 0.0002 for v in base],
            "Volume": [100.0] * n,
        },
        index=idx,
    )


def test_mesma_chave_faz_o_cache_devolver_o_candle_cheio() -> None:
    """Documenta o comportamento do cache que causou o bug."""
    est = EstrategiaReversaoM5(configuracao_scalping_m15())
    win = _janela()
    win_p = win.copy()
    # Parcial: mesma abertura, mas o candle ainda nao andou.
    win_p.iloc[-1, win_p.columns.get_loc("Close")] = float(win.iloc[-1]["Open"])
    win_p.iloc[-1, win_p.columns.get_loc("High")] = float(win.iloc[-1]["Open"])

    with contextlib.redirect_stdout(io.StringIO()):
        ind = est.calcular_indicadores(win, "EURUSD-bt")
        ind_mesma = est.calcular_indicadores(win_p, "EURUSD-bt")

    # Mesma chave: o parcial e ignorado, volta o objeto do candle cheio.
    assert ind_mesma is ind
    assert float(ind_mesma.iloc[-1]["Close"]) == float(win.iloc[-1]["Close"])


def test_chave_separada_preserva_o_parcial() -> None:
    """O backtest tem de usar a chave '-parcial' pra o candle parcial valer."""
    est = EstrategiaReversaoM5(configuracao_scalping_m15())
    win = _janela()
    win_p = win.copy()
    parcial_close = float(win.iloc[-1]["Open"])
    win_p.iloc[-1, win_p.columns.get_loc("Close")] = parcial_close
    win_p.iloc[-1, win_p.columns.get_loc("High")] = parcial_close

    with contextlib.redirect_stdout(io.StringIO()):
        est.calcular_indicadores(win, "EURUSD-bt")
        ind_p = est.calcular_indicadores(win_p, "EURUSD-bt-parcial")

    assert float(ind_p.iloc[-1]["Close"]) == parcial_close
    assert float(ind_p.iloc[-1]["Close"]) != float(win.iloc[-1]["Close"])


def test_backtest_usa_chave_separada_para_o_parcial() -> None:
    """Trava a correção no proprio backtest, nao so na estrategia."""
    fonte = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "backtest_m15.py"
    ).read_text(encoding="utf-8")
    assert '-bt-parcial' in fonte, (
        "backtest_m15 precisa chamar calcular_indicadores com chave propria "
        "pro candle parcial, senao o cache devolve o candle cheio"
    )
