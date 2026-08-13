"""Triagem walk-forward de famílias de sinais M5 com payoff fixo."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product
from statistics import NormalDist

import numpy as np
import pandas as pd

from .backtest import breakeven, intervalo_wilson
from .forex_estrategia import planos_correcao_fibo_sr


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


def _niveis_pivos_confirmados(
    candles: pd.DataFrame, raio: int
) -> tuple[pd.Series, pd.Series]:
    """Últimos pivôs disponíveis em cada fechamento, sem antecipar confirmação.

    Um pivô na posição i precisa de ``raio`` candles à direita. Seu preço só
    aparece na série no fechamento de i+raio, quando já pode ser conhecido.
    """
    largura = raio * 2 + 1
    fundo = candles["Low"].eq(candles["Low"].rolling(largura, center=True).min())
    topo = candles["High"].eq(candles["High"].rolling(largura, center=True).max())
    suporte = candles["Low"].where(fundo).shift(raio).ffill()
    resistencia = candles["High"].where(topo).shift(raio).ffill()
    return suporte, resistencia


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
    for expiracao in (1, 2, 3):
        for janela in (6, 12, 24):
            saida.append(
                Candidato(
                    "rompimento_forte",
                    {
                        "janela": janela,
                        "corpo_minimo": 0.55,
                        "fechamento_extremo": 0.20,
                        "atr_minimo": 0.80,
                        "atr_maximo": 2.00,
                        "expiracao": expiracao,
                    },
                )
            )
        for janela_movimento, limiar in product((1, 3), (1.5, 2.0, 2.5)):
            saida.append(
                Candidato(
                    "extremo_rejeicao",
                    {
                        "janela_movimento": janela_movimento,
                        "janela_nivel": 12,
                        "janela_volatilidade": 48,
                        "limiar_sigma": limiar,
                        "pavio_corpo": 1.5,
                        "expiracao": expiracao,
                    },
                )
            )
        for raio, tolerancia in product((2, 3), (0.15, 0.30)):
            base = {
                "raio": raio,
                "tolerancia_atr": tolerancia,
                "corpo_min_atr": 0.30,
                "fechamento_extremo": 0.25,
                "janela_reteste": 6,
                "expiracao": expiracao,
            }
            saida.append(Candidato("topo_fundo_rejeicao", base))
            saida.append(Candidato("topo_fundo_rompimento", base))
            saida.append(Candidato("topo_fundo_pullback", base))
    for expiracao in (1, 3, 6):
        for tolerancia, impulso in product((0.25, 0.40), (1.5, 2.0)):
            saida.append(
                Candidato(
                    "correcao_fibo_sr_binaria",
                    {
                        "raio_pivo": 2,
                        "fib_min": 0.50,
                        "fib_max": 0.618,
                        "tolerancia_sr_atr": tolerancia,
                        "corpo_min_atr": 0.30,
                        "impulso_min_atr": impulso,
                        "rr_minimo": 0.0,
                        "max_candles_correcao": 12,
                        "expiracao": expiracao,
                    },
                )
            )
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
    elif candidato.familia == "rompimento_forte":
        janela = int(p["janela"])
        maxima = candles["High"].shift(1).rolling(janela).max()
        minima = candles["Low"].shift(1).rolling(janela).min()
        amplitude = (candles["High"] - candles["Low"]).replace(0, np.nan)
        corpo = close - candles["Open"]
        corpo_forte = corpo.abs() >= float(p["corpo_minimo"]) * amplitude
        amplitude_valida = amplitude.between(
            float(p["atr_minimo"]) * atr,
            float(p["atr_maximo"]) * atr,
        )
        extremo = float(p["fechamento_extremo"])
        call = (
            (close > maxima)
            & (corpo > 0)
            & corpo_forte
            & amplitude_valida
            & (close >= candles["High"] - extremo * amplitude)
        )
        put = (
            (close < minima)
            & (corpo < 0)
            & corpo_forte
            & amplitude_valida
            & (close <= candles["Low"] + extremo * amplitude)
        )
        direcao = pd.Series(
            np.select([call, put], [1, -1], default=0),
            index=candles.index,
        )
    elif candidato.familia == "extremo_rejeicao":
        janela_movimento = int(p["janela_movimento"])
        janela_nivel = int(p["janela_nivel"])
        janela_volatilidade = int(p["janela_volatilidade"])
        retornos = np.log(close / close.shift(1))
        movimento = np.log(close / close.shift(janela_movimento))
        # A volatilidade termina em t-1: o proprio extremo nao dilui seu limiar.
        sigma = retornos.shift(1).rolling(janela_volatilidade).std().replace(0, np.nan)
        limiar = float(p["limiar_sigma"]) * sigma
        maxima = candles["High"].shift(1).rolling(janela_nivel).max()
        minima = candles["Low"].shift(1).rolling(janela_nivel).min()
        corpo = (close - candles["Open"]).abs()
        pavio_inferior = np.minimum(candles["Open"], close) - candles["Low"]
        pavio_superior = candles["High"] - np.maximum(candles["Open"], close)
        meio = (candles["High"] + candles["Low"]) / 2
        razao_pavio = float(p["pavio_corpo"])
        call = (
            (movimento <= -limiar)
            & (candles["Low"] < minima)
            & (close > minima)
            & (close > meio)
            & (pavio_inferior >= razao_pavio * corpo)
        )
        put = (
            (movimento >= limiar)
            & (candles["High"] > maxima)
            & (close < maxima)
            & (close < meio)
            & (pavio_superior >= razao_pavio * corpo)
        )
        direcao = pd.Series(
            np.select([call, put], [1, -1], default=0),
            index=candles.index,
        )
    elif candidato.familia.startswith("topo_fundo_"):
        suporte, resistencia = _niveis_pivos_confirmados(candles, int(p["raio"]))
        tolerancia = float(p["tolerancia_atr"]) * atr
        corpo_direcional = close - candles["Open"]
        corpo_valido = corpo_direcional.abs() >= float(p["corpo_min_atr"]) * atr
        amplitude = (candles["High"] - candles["Low"]).replace(0, np.nan)
        extremo = float(p["fechamento_extremo"])

        if candidato.familia == "topo_fundo_rejeicao":
            call = (
                (candles["Low"] <= suporte + tolerancia)
                & (close > suporte)
                & (close > (candles["High"] + candles["Low"]) / 2)
                & (corpo_direcional > 0)
            )
            put = (
                (candles["High"] >= resistencia - tolerancia)
                & (close < resistencia)
                & (close < (candles["High"] + candles["Low"]) / 2)
                & (corpo_direcional < 0)
            )
        elif candidato.familia == "topo_fundo_rompimento":
            call = (
                (close > resistencia)
                & (close.shift(1) <= resistencia)
                & (corpo_direcional > 0)
                & corpo_valido
                & (close >= candles["High"] - extremo * amplitude)
            )
            put = (
                (close < suporte)
                & (close.shift(1) >= suporte)
                & (corpo_direcional < 0)
                & corpo_valido
                & (close <= candles["Low"] + extremo * amplitude)
            )
        else:
            rompimento_alta = (close > resistencia) & (close.shift(1) <= resistencia)
            rompimento_baixa = (close < suporte) & (close.shift(1) >= suporte)
            nivel_alta = resistencia.where(rompimento_alta)
            nivel_baixa = suporte.where(rompimento_baixa)
            janela_reteste = int(p["janela_reteste"])
            ultimo_alta = nivel_alta.ffill(limit=janela_reteste).shift(1)
            ultimo_baixa = nivel_baixa.ffill(limit=janela_reteste).shift(1)
            call = (
                ultimo_alta.notna()
                & (candles["Low"] <= ultimo_alta + tolerancia)
                & (close > ultimo_alta)
                & (corpo_direcional > 0)
                & corpo_valido
            )
            put = (
                ultimo_baixa.notna()
                & (candles["High"] >= ultimo_baixa - tolerancia)
                & (close < ultimo_baixa)
                & (corpo_direcional < 0)
                & corpo_valido
            )
        direcao = pd.Series(
            np.select([call, put], [1, -1], default=0),
            index=candles.index,
        )
    elif candidato.familia == "correcao_fibo_sr_binaria":
        planos = planos_correcao_fibo_sr(
            ativo,
            candles,
            raio_pivo=int(p["raio_pivo"]),
            fib_min=float(p["fib_min"]),
            fib_max=float(p["fib_max"]),
            tolerancia_sr_atr=float(p["tolerancia_sr_atr"]),
            corpo_min_atr=float(p["corpo_min_atr"]),
            impulso_min_atr=float(p["impulso_min_atr"]),
            rr_minimo=float(p["rr_minimo"]),
            max_candles_correcao=int(p["max_candles_correcao"]),
        )
        direcao = planos.map(
            lambda plano: 1 if getattr(plano, "lado", None) == "buy"
            else -1 if getattr(plano, "lado", None) == "sell"
            else 0
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
    direcoes_cache = {}
    for c in todos:
        expiracao = int(c.parametros["expiracao"])
        chave_direcao = (
            c.familia,
            tuple(sorted((chave, valor) for chave, valor in c.parametros.items() if chave != "expiracao")),
        )
        if chave_direcao not in direcoes_cache:
            direcoes_cache[chave_direcao] = sinais(candles, c, ativo)
        dados = resultados(candles, direcoes_cache[chave_direcao], expiracao=expiracao)
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
