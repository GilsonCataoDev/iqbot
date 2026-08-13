"""Estratégia experimental de rompimento, reteste e continuação para Forex."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .forex_modelos import PlanoForex


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


def _regime_m15(candles: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Estrutura M15 concluída, projetada sobre M5 sem antecipar o bloco atual."""
    m15 = candles.resample("15min", label="right", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna()
    max_atual = m15["High"].shift(1).rolling(20).max()
    min_atual = m15["Low"].shift(1).rolling(20).min()
    max_anterior = m15["High"].shift(21).rolling(20).max()
    min_anterior = m15["Low"].shift(21).rolling(20).min()
    meio = (max_atual + min_atual) / 2
    alta = (m15["Close"] > meio) & (max_atual > max_anterior) & (min_atual > min_anterior)
    baixa = (m15["Close"] < meio) & (max_atual < max_anterior) & (min_atual < min_anterior)
    return (
        alta.reindex(candles.index, method="ffill").fillna(False),
        baixa.reindex(candles.index, method="ffill").fillna(False),
    )


def plano_rompimento_reteste(
    ativo: str,
    candles: pd.DataFrame,
    janela_rompimento: int = 12,
    janela_reteste: int = 6,
    tolerancia_atr: float = 0.20,
    corpo_min_atr: float = 0.60,
    corpo_max_atr: float = 2.00,
    stop_folga_atr: float = 0.10,
    retorno_risco: float = 2.0,
    spread: float = 0.00010,
) -> PlanoForex | None:
    """Retorna plano conhecido no último fechamento, sem candles futuros.

    Primeiro procura um fechamento forte além do range anterior. Depois exige
    reteste do nível, fechamento de volta na direção do rompimento e alinhamento
    EMA20/EMA50. A entrada real pertence à abertura do candle seguinte.
    """
    minimo = max(50, janela_rompimento) + janela_reteste + 2
    if len(candles) < minimo:
        return None
    df = candles.copy()
    atr = _atr(df)
    regime_alta, regime_baixa = _regime_m15(df)
    maxima = df["High"].shift(1).rolling(janela_rompimento).max()
    minima = df["Low"].shift(1).rolling(janela_rompimento).min()
    corpo = df["Close"] - df["Open"]
    amplitude = (df["High"] - df["Low"]).replace(0, np.nan)
    corpo_valido = corpo.abs().between(corpo_min_atr * atr, corpo_max_atr * atr)
    margem = np.maximum(0.10 * atr, 2 * spread)
    rompe_alta = (
        (df["Close"] > maxima + margem) & (corpo > 0) & corpo_valido
        & (df["Close"] >= df["High"] - 0.25 * amplitude)
    )
    rompe_baixa = (
        (df["Close"] < minima - margem) & (corpo < 0) & corpo_valido
        & (df["Close"] <= df["Low"] + 0.25 * amplitude)
    )

    atual = len(df) - 1
    atr_atual = float(atr.iloc[atual])
    if not np.isfinite(atr_atual) or atr_atual <= 0:
        return None
    inicio = max(0, atual - janela_reteste)
    candidatos_alta = np.flatnonzero(rompe_alta.iloc[inicio:atual].to_numpy())
    candidatos_baixa = np.flatnonzero(rompe_baixa.iloc[inicio:atual].to_numpy())
    tolerancia = tolerancia_atr * atr_atual
    candle = df.iloc[atual]

    if len(candidatos_alta):
        indice_rompimento = inicio + int(candidatos_alta[-1])
        nivel = float(maxima.iloc[indice_rompimento])
        confirma = (
            float(candle["Low"]) <= nivel + tolerancia
            and float(candle["Close"]) > nivel
            and float(candle["Close"]) > (float(candle["High"]) + float(candle["Low"])) / 2
            and float(candle["Close"]) > float(candle["Open"])
            and bool(regime_alta.iloc[atual])
        )
        if confirma:
            entrada_referencia = float(candle["Close"])
            stop = float(candle["Low"]) - stop_folga_atr * atr_atual
            risco = entrada_referencia - stop
            if 0.60 * atr_atual <= risco <= 1.80 * atr_atual:
                return PlanoForex(
                    ativo=ativo,
                    lado="buy",
                    sinal_em=df.index[atual].to_pydatetime(),
                    nivel=nivel,
                    stop=stop,
                    alvo=entrada_referencia + retorno_risco * risco,
                    risco_preco=risco,
                    motivo="rompimento_reteste_tendencia",
                )

    if len(candidatos_baixa):
        indice_rompimento = inicio + int(candidatos_baixa[-1])
        nivel = float(minima.iloc[indice_rompimento])
        confirma = (
            float(candle["High"]) >= nivel - tolerancia
            and float(candle["Close"]) < nivel
            and float(candle["Close"]) < (float(candle["High"]) + float(candle["Low"])) / 2
            and float(candle["Close"]) < float(candle["Open"])
            and bool(regime_baixa.iloc[atual])
        )
        if confirma:
            entrada_referencia = float(candle["Close"])
            stop = float(candle["High"]) + stop_folga_atr * atr_atual
            risco = stop - entrada_referencia
            if 0.60 * atr_atual <= risco <= 1.80 * atr_atual:
                return PlanoForex(
                    ativo=ativo,
                    lado="sell",
                    sinal_em=df.index[atual].to_pydatetime(),
                    nivel=nivel,
                    stop=stop,
                    alvo=entrada_referencia - retorno_risco * risco,
                    risco_preco=risco,
                    motivo="rompimento_reteste_tendencia",
                )
    return None


def planos_rompimento_reteste(
    ativo: str,
    candles: pd.DataFrame,
    janela_rompimento: int = 12,
    janela_reteste: int = 6,
    tolerancia_atr: float = 0.20,
    corpo_min_atr: float = 0.60,
    corpo_max_atr: float = 2.00,
    stop_folga_atr: float = 0.10,
    retorno_risco: float = 2.0,
    spread: float = 0.00010,
) -> pd.Series:
    """Versão linear para backtests longos; cada plano usa apenas dados até t."""
    df = candles
    atr = _atr(df)
    regime_alta, regime_baixa = _regime_m15(df)
    maxima = df["High"].shift(1).rolling(janela_rompimento).max()
    minima = df["Low"].shift(1).rolling(janela_rompimento).min()
    corpo = df["Close"] - df["Open"]
    amplitude = (df["High"] - df["Low"]).replace(0, np.nan)
    corpo_valido = corpo.abs().between(corpo_min_atr * atr, corpo_max_atr * atr)
    margem = np.maximum(0.10 * atr, 2 * spread)
    rompe_alta = (
        (df["Close"] > maxima + margem) & (corpo > 0) & corpo_valido
        & (df["Close"] >= df["High"] - 0.25 * amplitude)
    )
    rompe_baixa = (
        (df["Close"] < minima - margem) & (corpo < 0) & corpo_valido
        & (df["Close"] <= df["Low"] + 0.25 * amplitude)
    )
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")
    ultimo_alta: tuple[int, float] | None = None
    ultimo_baixa: tuple[int, float] | None = None

    for i in range(len(df)):
        if bool(rompe_alta.iloc[i]):
            ultimo_alta = (i, float(maxima.iloc[i]))
        if bool(rompe_baixa.iloc[i]):
            ultimo_baixa = (i, float(minima.iloc[i]))
        atr_atual = float(atr.iloc[i])
        if not np.isfinite(atr_atual) or atr_atual <= 0:
            continue
        candle = df.iloc[i]
        tolerancia = tolerancia_atr * atr_atual
        if ultimo_alta and 0 < i - ultimo_alta[0] <= janela_reteste:
            nivel = ultimo_alta[1]
            if (
                float(candle["Low"]) <= nivel + tolerancia
                and float(candle["Close"]) > nivel
                and float(candle["Close"]) > (float(candle["High"]) + float(candle["Low"])) / 2
                and float(candle["Close"]) > float(candle["Open"])
                and bool(regime_alta.iloc[i])
            ):
                entrada = float(candle["Close"])
                stop = float(candle["Low"]) - stop_folga_atr * atr_atual
                risco = entrada - stop
                if 0.60 * atr_atual <= risco <= 1.80 * atr_atual:
                    saida.iloc[i] = PlanoForex(
                        ativo, "buy", df.index[i].to_pydatetime(), nivel, stop,
                        entrada + retorno_risco * risco, risco,
                        "rompimento_reteste_tendencia",
                    )
        if saida.iloc[i] is None and ultimo_baixa and 0 < i - ultimo_baixa[0] <= janela_reteste:
            nivel = ultimo_baixa[1]
            if (
                float(candle["High"]) >= nivel - tolerancia
                and float(candle["Close"]) < nivel
                and float(candle["Close"]) < (float(candle["High"]) + float(candle["Low"])) / 2
                and float(candle["Close"]) < float(candle["Open"])
                and bool(regime_baixa.iloc[i])
            ):
                entrada = float(candle["Close"])
                stop = float(candle["High"]) + stop_folga_atr * atr_atual
                risco = stop - entrada
                if 0.60 * atr_atual <= risco <= 1.80 * atr_atual:
                    saida.iloc[i] = PlanoForex(
                        ativo, "sell", df.index[i].to_pydatetime(), nivel, stop,
                        entrada - retorno_risco * risco, risco,
                        "rompimento_reteste_tendencia",
                    )
    return saida


def planos_toque_lta_ltb(
    ativo: str,
    candles: pd.DataFrame,
    raio_pivo: int = 2,
    tolerancia_atr: float = 0.20,
    stop_folga_atr: float = 0.10,
    rr_minimo: float = 1.20,
    idade_maxima_linha: int = 50,
    corpo_min_atr: float = 0.30,
) -> pd.Series:
    """Planos por toque em linha de tendência e alvo no último pivô oposto.

    A LTA nasce de dois fundos confirmados ascendentes; a LTB, de dois topos
    confirmados descendentes. O valor do pivô só entra ``raio_pivo`` candles
    depois de ocorrer, eliminando o repainting típico de ZigZag.
    """
    df = candles
    atr = _atr(df)
    regime_alta, regime_baixa = _regime_m15(df)
    largura = raio_pivo * 2 + 1
    fundo = df["Low"].eq(df["Low"].rolling(largura, center=True).min())
    topo = df["High"].eq(df["High"].rolling(largura, center=True).max())
    fundo_confirmado = df["Low"].where(fundo).shift(raio_pivo)
    topo_confirmado = df["High"].where(topo).shift(raio_pivo)
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")
    fundos: list[tuple[int, float]] = []
    topos: list[tuple[int, float]] = []
    linhas_usadas: set[tuple[str, int, int]] = set()

    for i in range(len(df)):
        if pd.notna(fundo_confirmado.iloc[i]):
            fundos.append((i - raio_pivo, float(fundo_confirmado.iloc[i])))
        if pd.notna(topo_confirmado.iloc[i]):
            topos.append((i - raio_pivo, float(topo_confirmado.iloc[i])))
        atr_atual = float(atr.iloc[i])
        if not np.isfinite(atr_atual) or atr_atual <= 0:
            continue
        candle = df.iloc[i]
        abertura = float(candle["Open"])
        fechamento = float(candle["Close"])
        maxima = float(candle["High"])
        minima = float(candle["Low"])
        meio = (maxima + minima) / 2
        tolerancia = tolerancia_atr * atr_atual
        corpo = abs(fechamento - abertura)

        if len(fundos) >= 2:
            (i1, p1), (i2, p2) = fundos[-2:]
            slope = (p2 - p1) / (i2 - i1) if i2 > i1 else 0.0
            linha = p2 + slope * (i - i2)
            alvo = next(
                (preco for indice, preco in reversed(topos) if i1 < indice < i2 and preco > fechamento),
                None,
            )
            tocou = abs(minima - linha) <= tolerancia
            respeitada = all(
                float(df["Close"].iloc[j]) >= p2 + slope * (j - i2) - tolerancia
                for j in range(i2 + 1, i)
            )
            chave_linha = ("lta", i1, i2)
            if (
                p2 > p1
                and 0 < slope <= 0.50 * atr_atual
                and i - i2 <= idade_maxima_linha
                and chave_linha not in linhas_usadas
                and respeitada
                and tocou
                and fechamento > linha
                and fechamento > abertura
                and fechamento > meio
                and corpo >= corpo_min_atr * atr_atual
                and bool(regime_alta.iloc[i])
                and alvo is not None
            ):
                stop = minima - stop_folga_atr * atr_atual
                risco = fechamento - stop
                recompensa = alvo - fechamento
                if 0.60 * atr_atual <= risco <= 1.80 * atr_atual and recompensa / risco >= rr_minimo:
                    saida.iloc[i] = PlanoForex(
                        ativo, "buy", df.index[i].to_pydatetime(), linha, stop,
                        alvo, risco, "toque_lta_target_topo",
                    )
                    linhas_usadas.add(chave_linha)

        if saida.iloc[i] is None and len(topos) >= 2:
            (i1, p1), (i2, p2) = topos[-2:]
            slope = (p2 - p1) / (i2 - i1) if i2 > i1 else 0.0
            linha = p2 + slope * (i - i2)
            alvo = next(
                (preco for indice, preco in reversed(fundos) if i1 < indice < i2 and preco < fechamento),
                None,
            )
            tocou = abs(maxima - linha) <= tolerancia
            respeitada = all(
                float(df["Close"].iloc[j]) <= p2 + slope * (j - i2) + tolerancia
                for j in range(i2 + 1, i)
            )
            chave_linha = ("ltb", i1, i2)
            if (
                p2 < p1
                and -0.50 * atr_atual <= slope < 0
                and i - i2 <= idade_maxima_linha
                and chave_linha not in linhas_usadas
                and respeitada
                and tocou
                and fechamento < linha
                and fechamento < abertura
                and fechamento < meio
                and corpo >= corpo_min_atr * atr_atual
                and bool(regime_baixa.iloc[i])
                and alvo is not None
            ):
                stop = maxima + stop_folga_atr * atr_atual
                risco = stop - fechamento
                recompensa = fechamento - alvo
                if 0.60 * atr_atual <= risco <= 1.80 * atr_atual and recompensa / risco >= rr_minimo:
                    saida.iloc[i] = PlanoForex(
                        ativo, "sell", df.index[i].to_pydatetime(), linha, stop,
                        alvo, risco, "toque_ltb_target_fundo",
                    )
                    linhas_usadas.add(chave_linha)
    return saida


def planos_correcao_fibo_sr(
    ativo: str,
    candles: pd.DataFrame,
    raio_pivo: int = 2,
    fib_min: float = 0.50,
    fib_max: float = 0.618,
    tolerancia_sr_atr: float = 0.25,
    corpo_min_atr: float = 0.30,
    impulso_min_atr: float = 2.0,
    stop_folga_atr: float = 0.10,
    rr_minimo: float = 1.20,
    max_candles_correcao: int = 12,
) -> pd.Series:
    """Correção na tendência com confluência Fibonacci + suporte/resistência.

    O impulso e os níveis são definidos por pivôs já confirmados. O alvo é o
    extremo do impulso; se o espaço restante não pagar ao menos ``rr_minimo``,
    não existe entrada.
    """
    if not 0 < fib_min < fib_max < 1:
        raise ValueError("Faixa de Fibonacci inválida.")
    df = candles
    atr = _atr(df)
    regime_alta, regime_baixa = _regime_m15(df)
    largura = raio_pivo * 2 + 1
    fundo = df["Low"].eq(df["Low"].rolling(largura, center=True).min())
    topo = df["High"].eq(df["High"].rolling(largura, center=True).max())
    fundo_confirmado = df["Low"].where(fundo).shift(raio_pivo)
    topo_confirmado = df["High"].where(topo).shift(raio_pivo)
    fundos: list[tuple[int, float]] = []
    topos: list[tuple[int, float]] = []
    impulsos_usados: set[tuple[str, int, int]] = set()
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")

    for i in range(len(df)):
        if pd.notna(fundo_confirmado.iloc[i]):
            fundos.append((i - raio_pivo, float(fundo_confirmado.iloc[i])))
        if pd.notna(topo_confirmado.iloc[i]):
            topos.append((i - raio_pivo, float(topo_confirmado.iloc[i])))
        atr_atual = float(atr.iloc[i])
        if not np.isfinite(atr_atual) or atr_atual <= 0:
            continue
        candle = df.iloc[i]
        abertura, fechamento = float(candle["Open"]), float(candle["Close"])
        maxima, minima = float(candle["High"]), float(candle["Low"])
        meio = (maxima + minima) / 2
        corpo = abs(fechamento - abertura)
        tolerancia = tolerancia_sr_atr * atr_atual

        if topos:
            i_topo, preco_topo = topos[-1]
            fundo_origem = next(((j, p) for j, p in reversed(fundos) if j < i_topo), None)
            if fundo_origem and 0 < i - i_topo <= max_candles_correcao:
                i_fundo, preco_fundo = fundo_origem
                amplitude = preco_topo - preco_fundo
                zona_baixa = preco_topo - fib_max * amplitude
                zona_alta = preco_topo - fib_min * amplitude
                confluencia_sr = False
                for j, nivel in reversed(topos[:-1]):
                    if j <= i_fundo:
                        break
                    if j < i_topo and zona_baixa - tolerancia <= nivel <= zona_alta + tolerancia:
                        confluencia_sr = True
                        break
                chave = ("buy", i_fundo, i_topo)
                toca_fibo = minima <= zona_alta and maxima >= zona_baixa
                if (
                    chave not in impulsos_usados
                    and amplitude >= impulso_min_atr * atr_atual
                    and bool(regime_alta.iloc[i])
                    and confluencia_sr
                    and toca_fibo
                    and fechamento > abertura
                    and fechamento > meio
                    and corpo >= corpo_min_atr * atr_atual
                ):
                    stop = minima - stop_folga_atr * atr_atual
                    risco = fechamento - stop
                    recompensa = preco_topo - fechamento
                    if 0.60 * atr_atual <= risco <= 1.80 * atr_atual and recompensa / risco >= rr_minimo:
                        saida.iloc[i] = PlanoForex(
                            ativo, "buy", df.index[i].to_pydatetime(),
                            (zona_baixa + zona_alta) / 2, stop, preco_topo, risco,
                            "correcao_tendencia_fibo_suporte",
                        )
                        impulsos_usados.add(chave)

        if saida.iloc[i] is None and fundos:
            i_fundo, preco_fundo = fundos[-1]
            topo_origem = next(((j, p) for j, p in reversed(topos) if j < i_fundo), None)
            if topo_origem and 0 < i - i_fundo <= max_candles_correcao:
                i_topo, preco_topo = topo_origem
                amplitude = preco_topo - preco_fundo
                zona_baixa = preco_fundo + fib_min * amplitude
                zona_alta = preco_fundo + fib_max * amplitude
                confluencia_sr = False
                for j, nivel in reversed(fundos[:-1]):
                    if j <= i_topo:
                        break
                    if j < i_fundo and zona_baixa - tolerancia <= nivel <= zona_alta + tolerancia:
                        confluencia_sr = True
                        break
                chave = ("sell", i_topo, i_fundo)
                toca_fibo = maxima >= zona_baixa and minima <= zona_alta
                if (
                    chave not in impulsos_usados
                    and amplitude >= impulso_min_atr * atr_atual
                    and bool(regime_baixa.iloc[i])
                    and confluencia_sr
                    and toca_fibo
                    and fechamento < abertura
                    and fechamento < meio
                    and corpo >= corpo_min_atr * atr_atual
                ):
                    stop = maxima + stop_folga_atr * atr_atual
                    risco = stop - fechamento
                    recompensa = fechamento - preco_fundo
                    if 0.60 * atr_atual <= risco <= 1.80 * atr_atual and recompensa / risco >= rr_minimo:
                        saida.iloc[i] = PlanoForex(
                            ativo, "sell", df.index[i].to_pydatetime(),
                            (zona_baixa + zona_alta) / 2, stop, preco_fundo, risco,
                            "correcao_tendencia_fibo_resistencia",
                        )
                        impulsos_usados.add(chave)
    return saida
