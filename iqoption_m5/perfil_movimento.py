"""Quanto o ativo costuma andar — e se dá para saber pra que lado.

São duas perguntas diferentes, e a literatura de mercado mistura as duas o
tempo todo. Aqui elas ficam separadas de propósito:

  MAGNITUDE  — "quanto anda em 6h?" Isso o histórico responde bem. Volatilidade
               tem memória, tem hora do dia, e a distribuição é estável o
               bastante para dimensionar stop e alvo.
  DIREÇÃO    — "pra que lado?" Isso o histórico responde mal. A função de
               direção existe justamente para medir isso e mostrar o número,
               em vez de a gente afirmar de um lado ou do outro.

Nada aqui vira ordem. É régua de dimensionamento e teste de hipótese.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PERCENTIS = (10, 25, 50, 75, 90)


def _pct(serie: pd.Series) -> dict:
    s = serie.dropna()
    if s.empty:
        return {}
    return {f"p{p}": round(float(np.percentile(s, p)), 4) for p in PERCENTIS}


def magnitude(df: pd.DataFrame, horizonte: int) -> dict:
    """Distribuição do movimento nas próximas ``horizonte`` velas, em %.

    ``amplitude``  — do topo ao fundo da janela: o quanto o preço passeia.
    ``deslocamento`` — |fim - início|: o quanto ele efetivamente sai do lugar.
    ``excursao_contra`` — para quem entra comprado no fechamento, o pior ponto
      antes do fim da janela. É esse número que decide se um stop sobrevive ao
      ruído, não a amplitude.

    A diferença entre amplitude e deslocamento é o custo do vaivém: quando a
    primeira é muito maior que a segunda, o preço anda sem ir a lugar nenhum, e
    alvo largo não é alcançado mesmo com direção certa.
    """
    if len(df) < horizonte + 2:
        return {"n": 0}
    # shift(-1) porque a janela começa na vela SEGUINTE à âncora: usar a
    # própria vela da decisão seria olhar um preço que ainda não fechou.
    fut_max = df["High"].rolling(horizonte).max().shift(-horizonte)
    fut_min = df["Low"].rolling(horizonte).min().shift(-horizonte)
    fut_fim = df["Close"].shift(-horizonte)
    base = df["Close"]

    amplitude = (fut_max - fut_min) / base * 100
    deslocamento = (fut_fim - base).abs() / base * 100
    contra = (base - fut_min) / base * 100

    validos = amplitude.notna() & deslocamento.notna()
    return {
        "n": int(validos.sum()),
        "horizonte_velas": horizonte,
        "amplitude_pct": _pct(amplitude),
        "deslocamento_pct": _pct(deslocamento),
        "excursao_contra_pct": _pct(contra),
    }


def por_hora(df: pd.DataFrame, horizonte: int) -> dict[int, float]:
    """Amplitude mediana (%) por hora do dia, para achar a janela que anda."""
    if len(df) < horizonte + 2:
        return {}
    fut_max = df["High"].rolling(horizonte).max().shift(-horizonte)
    fut_min = df["Low"].rolling(horizonte).min().shift(-horizonte)
    amp = (fut_max - fut_min) / df["Close"] * 100
    horas = df.index.hour if isinstance(df.index, pd.DatetimeIndex) \
        else pd.to_datetime(df["timestamp"]).dt.hour
    agrupado = amp.groupby(np.asarray(horas)).median().dropna()
    return {int(h): round(float(v), 4) for h, v in agrupado.items()}


def direcao_tem_memoria(df: pd.DataFrame, horizonte: int) -> dict:
    """O movimento das últimas H velas prevê o das próximas H?

    Teste deliberadamente ingênuo — é o que "seguir a tendência" faz na prática.
    Devolve o acerto e o intervalo de Wilson. Se o intervalo cruza 50%, não há
    memória de direção nessa escala, e qualquer leitura direcional aqui está
    contando história em cima de ruído.
    """
    if len(df) < 2 * horizonte + 2:
        return {"n": 0}
    passado = df["Close"].diff(horizonte)
    futuro = df["Close"].shift(-horizonte) - df["Close"]
    valido = passado.notna() & futuro.notna() & (passado != 0) & (futuro != 0)
    p, f = passado[valido], futuro[valido]
    n = int(valido.sum())
    if n == 0:
        return {"n": 0}
    acertos = int((np.sign(p) == np.sign(f)).sum())
    return {"n": n, "acertos": acertos,
            "taxa": round(100 * acertos / n, 2),
            "ic_95": wilson(acertos, n)}


def wilson(acertos: int, n: int, z: float = 1.96) -> list[float] | None:
    """IC de proporção. Wilson, não Wald: perto de 50% e com n moderado o
    intervalo de Wald mente sobre a incerteza."""
    if n <= 0:
        return None
    p = acertos / n
    d = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / d
    meio = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** .5) / d
    return [round(100 * (centro - meio), 2), round(100 * (centro + meio), 2)]
