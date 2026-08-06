"""Triagem walk-forward de famílias de sinais M5 com payoff fixo."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product
from statistics import NormalDist

import numpy as np
import pandas as pd

from .backtest import breakeven, intervalo_wilson


@dataclass(frozen=True)
class Candidato:
    familia: str
    parametros: dict[str, float | int]


def _atr(candles: pd.DataFrame, periodo: int = 14) -> pd.Series:
    anterior = candles["Close"].shift(1)
    tr = pd.concat(
        [
            candles["High"] - candles["Low"],
            (candles["High"] - anterior).abs(),
            (candles["Low"] - anterior).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(periodo).mean()


def _grade_nivel(ativo: str) -> tuple[float, float]:
    return (0.05, 0.10) if "JPY" in ativo else (0.0005, 0.0010)


def candidatos(ativo: str) -> list[Candidato]:
    saida: list[Candidato] = []
    for expiracao in (1, 3, 6):
        for janela, limiar in product((1, 3, 6), (0.5, 1.0)):
            saida.append(Candidato("impulso", {"janela": janela, "limiar_atr": limiar, "expiracao": expiracao}))
        for limiar in (0.5, 1.0, 1.5):
            saida.append(Candidato("reversao_candle", {"limiar_atr": limiar, "expiracao": expiracao}))
        for janela in (12, 24, 48):
            saida.append(Candidato("rompimento", {"janela": janela, "expiracao": expiracao}))
        for janela, z in product((20, 40), (1.5, 2.0)):
            saida.append(Candidato("reversao_zscore", {"janela": janela, "z": z, "expiracao": expiracao}))
        for grade, limiar in product(_grade_nivel(ativo), (0.0, 0.25)):
            base = {"grade": grade, "limiar_atr": limiar, "expiracao": expiracao}
            saida.append(Candidato("nivel_redondo_bounce", base))
            saida.append(Candidato("nivel_redondo_breakout", base))
        for limiar in (0.0, 0.25):
            base = {"limiar_atr": limiar, "expiracao": expiracao}
            saida.append(Candidato("nivel_anterior_bounce", base))
            saida.append(Candidato("nivel_anterior_breakout", base))
    return saida


def _niveis_dia_anterior(candles: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    datas = pd.Series(candles.index.normalize(), index=candles.index)
    diarios = candles.groupby(candles.index.normalize()).agg({"High": "max", "Low": "min"})
    maxima = datas.map(diarios["High"].shift(1))
    minima = datas.map(diarios["Low"].shift(1))
    return maxima, minima


def sinais(candles: pd.DataFrame, candidato: Candidato, ativo: str = "EURUSD") -> pd.Series:
    """Direção conhecida no fechamento de t: 1=CALL, -1=PUT, 0=sem entrada."""
    close = candles["Close"]
    atr = _atr(candles).replace(0, np.nan)
    p = candidato.parametros

    if candidato.familia == "impulso":
        movimento = close - close.shift(int(p["janela"]))
        valido = movimento.abs() >= float(p["limiar_atr"]) * atr
        direcao = np.sign(movimento).where(valido, 0)
    elif candidato.familia == "reversao_candle":
        corpo = close - candles["Open"]
        valido = corpo.abs() >= float(p["limiar_atr"]) * atr
        direcao = -np.sign(corpo).where(valido, 0)
    elif candidato.familia == "rompimento":
        janela = int(p["janela"])
        maxima = candles["High"].shift(1).rolling(janela).max()
        minima = candles["Low"].shift(1).rolling(janela).min()
        direcao = pd.Series(
            np.select([close > maxima, close < minima], [1, -1], default=0),
            index=candles.index,
        )
    elif candidato.familia == "reversao_zscore":
        janela = int(p["janela"])
        media = close.rolling(janela).mean()
        desvio = close.rolling(janela).std().replace(0, np.nan)
        zscore = (close - media) / desvio
        limite = float(p["z"])
        direcao = pd.Series(
            np.select([zscore < -limite, zscore > limite], [1, -1], default=0),
            index=candles.index,
        )
    elif candidato.familia.startswith("nivel_redondo_"):
        grade = float(p["grade"])
        nivel = (candles["Open"] / grade).round() * grade
        corpo = close - candles["Open"]
        valido = corpo.abs() >= float(p["limiar_atr"]) * atr
        if candidato.familia.endswith("bounce"):
            call = (candles["Open"] > nivel) & (candles["Low"] <= nivel) & (close > nivel) & (corpo > 0)
            put = (candles["Open"] < nivel) & (candles["High"] >= nivel) & (close < nivel) & (corpo < 0)
        else:
            call = (candles["Open"] < nivel) & (close > nivel)
            put = (candles["Open"] > nivel) & (close < nivel)
        direcao = pd.Series(np.select([call & valido, put & valido], [1, -1], default=0), index=candles.index)
    elif candidato.familia.startswith("nivel_anterior_"):
        maxima, minima = _niveis_dia_anterior(candles)
        corpo = close - candles["Open"]
        valido = corpo.abs() >= float(p["limiar_atr"]) * atr
        if candidato.familia.endswith("bounce"):
            call = (candles["Low"] <= minima) & (close > minima) & (corpo > 0)
            put = (candles["High"] >= maxima) & (close < maxima) & (corpo < 0)
        else:
            call = (candles["Open"] <= maxima) & (close > maxima)
            put = (candles["Open"] >= minima) & (close < minima)
        direcao = pd.Series(np.select([call & valido, put & valido], [1, -1], default=0), index=candles.index)
    else:
        raise ValueError(f"Família desconhecida: {candidato.familia}")
    return pd.Series(direcao, index=candles.index).fillna(0).astype("int8")


def resultados(candles: pd.DataFrame, direcao: pd.Series, expiracao: int = 1) -> pd.DataFrame:
    """Resolve sinal em t pela abertura/fechamento de t+1, sem usar t+1 no sinal."""
    movimento_futuro = candles["Close"].shift(-expiracao) - candles["Open"].shift(-1)
    mascara = direcao.ne(0) & movimento_futuro.notna() & movimento_futuro.ne(0)
    retorno = pd.DataFrame(index=candles.index[mascara])
    retorno["ganho"] = (
        np.sign(movimento_futuro[mascara]).astype("int8") == direcao[mascara]
    ).astype("int8")
    retorno["hora_entrada"] = candles.index.to_series().shift(-1)[mascara].to_numpy()
    return retorno


def purgar_sobrepostas(dados: pd.DataFrame, candles: pd.DataFrame, expiracao: int) -> pd.DataFrame:
    """Mantém apenas operações que não se sobrepõem no tempo.

    Um sinal em t ocupa as velas t+1 até t+expiracao. Com expiracao=6, sinais
    em velas seguidas viram operações que medem quase o mesmo movimento de
    preço — não são observações independentes, e o intervalo de confiança
    calculado sobre elas fica otimista demais. A varredura gulosa aqui aceita o
    sinal mais antigo e descarta todos que começariam antes de ele expirar.
    """
    if dados.empty or expiracao <= 1:
        return dados
    posicoes = candles.index.get_indexer(dados.index)
    manter: list[int] = []
    livre_a_partir_de = -math.inf
    for linha, posicao in enumerate(posicoes):
        if posicao >= livre_a_partir_de:
            manter.append(linha)
            livre_a_partir_de = posicao + expiracao
    return dados.iloc[manter]


def _metricas(ganhos: pd.Series, payout: float, z: float = 1.96) -> dict:
    total = int(len(ganhos))
    vitorias = int(ganhos.sum()) if total else 0
    taxa = vitorias / total if total else 0.0
    inferior, superior = intervalo_wilson(vitorias, total, z=z)
    perdas = total - vitorias
    payout_exigido = perdas / vitorias if vitorias else math.inf
    return {
        "operacoes": total,
        "acerto": taxa,
        "ic_min": inferior,
        "ic_max": superior,
        "lucro": vitorias * payout - perdas,
        "payout_exigido": payout_exigido,
    }


def _janelas_walk_forward(tamanho: int):
    limites = ((0.50, 0.65), (0.65, 0.80), (0.80, 1.00))
    for treino_fim, teste_fim in limites:
        yield slice(0, int(tamanho * treino_fim)), slice(
            int(tamanho * treino_fim), int(tamanho * teste_fim)
        )


def avaliar_ativo(
    ativo: str,
    candles: pd.DataFrame,
    payout: float = 0.85,
    minimo_treino: int = 100,
    purgar: bool = False,
) -> list[dict]:
    todos = candidatos(ativo)
    por_familia = sorted({c.familia for c in todos})
    sinais_cache = {}
    for c in todos:
        expiracao = int(c.parametros["expiracao"])
        dados = resultados(candles, sinais(candles, c, ativo), expiracao=expiracao)
        if purgar:
            dados = purgar_sobrepostas(dados, candles, expiracao)
        sinais_cache[str(c)] = dados
    linhas: list[dict] = []

    for familia in por_familia:
        escolhidos = [c for c in todos if c.familia == familia]
        ganhos_oos: list[pd.Series] = []
        lucros_folds: list[float] = []
        parametros_folds: list[str] = []

        for numero, (treino, teste) in enumerate(_janelas_walk_forward(len(candles)), start=1):
            treino_inicio, treino_fim = treino.start, treino.stop
            teste_inicio, teste_fim = teste.start, teste.stop
            melhor = None
            melhor_score = -math.inf
            for candidato in escolhidos:
                dados = sinais_cache[str(candidato)]
                posicoes = candles.index.get_indexer(dados.index)
                expiracao = int(candidato.parametros["expiracao"])
                amostra = dados[
                    (posicoes >= treino_inicio) & (posicoes < treino_fim - expiracao)
                ]["ganho"]
                metrica = _metricas(amostra, payout)
                if metrica["operacoes"] < minimo_treino:
                    continue
                # Lucro médio penalizado por baixa amostra; só usa o passado.
                score = metrica["lucro"] / math.sqrt(metrica["operacoes"])
                if score > melhor_score:
                    melhor, melhor_score = candidato, score
            if melhor is None:
                continue
            dados = sinais_cache[str(melhor)]
            posicoes = candles.index.get_indexer(dados.index)
            expiracao = int(melhor.parametros["expiracao"])
            amostra_teste = dados[
                (posicoes >= teste_inicio) & (posicoes < teste_fim - expiracao)
            ]["ganho"]
            metrica_teste = _metricas(amostra_teste, payout)
            ganhos_oos.append(amostra_teste)
            lucros_folds.append(float(metrica_teste["lucro"]))
            parametros_folds.append(str(melhor.parametros))

        agregado = pd.concat(ganhos_oos) if ganhos_oos else pd.Series(dtype="int8")
        metricas = _metricas(agregado, payout)
        linhas.append(
            {
                "ativo": ativo,
                "mercado": "OTC" if ativo.endswith("-OTC") else "normal",
                "familia": familia,
                **metricas,
                "folds_positivos": sum(valor > 0 for valor in lucros_folds),
                "folds_avaliados": len(lucros_folds),
                "lucros_folds": ";".join(f"{valor:.2f}" for valor in lucros_folds),
                "parametros_folds": " | ".join(parametros_folds),
            }
        )
    return linhas


def classificar(resultados_df: pd.DataFrame, payout: float = 0.85) -> pd.DataFrame:
    """Critério simultâneo conservador para os 24 testes ativo×família."""
    if resultados_df.empty:
        return resultados_df
    testes = len(resultados_df)
    z_bonferroni = NormalDist().inv_cdf(1 - 0.05 / (2 * testes))
    saida = resultados_df.copy()
    intervalos = [
        intervalo_wilson(int(round(linha.acerto * linha.operacoes)), int(linha.operacoes), z_bonferroni)
        for linha in saida.itertuples()
    ]
    saida["ic_simultaneo_min"] = [valor[0] for valor in intervalos]
    saida["breakeven"] = breakeven(payout)
    saida["promissor"] = (
        (saida["operacoes"] >= 300)
        & (saida["ic_simultaneo_min"] > saida["breakeven"])
        & (saida["lucro"] > 0)
        & (saida["folds_positivos"] == saida["folds_avaliados"])
        & (saida["folds_avaliados"] == 3)
    )
    return saida.sort_values(["promissor", "lucro"], ascending=[False, False])
