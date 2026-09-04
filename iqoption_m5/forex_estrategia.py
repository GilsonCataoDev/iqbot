"""Estratégia experimental de rompimento, reteste e continuação para Forex."""

from __future__ import annotations

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

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


def _regime_h1(candles: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Tendência H1 fechada projetada no M15, sem usar a hora em formação."""
    h1 = candles.resample("1h", label="right", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna()
    ema20 = h1["Close"].ewm(span=20, adjust=False).mean()
    ema50 = h1["Close"].ewm(span=50, adjust=False).mean()
    max_atual = h1["High"].shift(1).rolling(10).max()
    min_atual = h1["Low"].shift(1).rolling(10).min()
    max_anterior = h1["High"].shift(11).rolling(10).max()
    min_anterior = h1["Low"].shift(11).rolling(10).min()
    alta = (
        (h1["Close"] > ema50) & (ema20 > ema50) & (ema50 > ema50.shift(3))
        & (max_atual > max_anterior) & (min_atual > min_anterior)
    )
    baixa = (
        (h1["Close"] < ema50) & (ema20 < ema50) & (ema50 < ema50.shift(3))
        & (max_atual < max_anterior) & (min_atual < min_anterior)
    )
    return (
        alta.reindex(candles.index, method="ffill").fillna(False),
        baixa.reindex(candles.index, method="ffill").fillna(False),
    )


def planos_rompimento_reteste_h1(
    ativo: str,
    candles: pd.DataFrame,
    janela_rompimento: int = 20,
    janela_reteste: int = 3,
    tolerancia_atr: float = 0.20,
    corpo_rompimento_min_atr: float = 0.50,
    corpo_reteste_min_atr: float = 0.20,
    stop_folga_atr: float = 0.20,
    retorno_risco: float = 2.0,
    spread: float = 0.00010,
    hora_inicio_utc: int = 7,
    hora_fim_utc: int = 17,
) -> pd.Series:
    """Rompimento M15 + reteste em até 3 velas, alinhado à tendência H1."""
    df = candles
    atr = _atr(df)
    tendencia_alta, tendencia_baixa = _regime_h1(df)
    maxima = df["High"].shift(1).rolling(janela_rompimento).max()
    minima = df["Low"].shift(1).rolling(janela_rompimento).min()
    corpo = df["Close"] - df["Open"]
    amplitude = (df["High"] - df["Low"]).replace(0, np.nan)
    margem = np.maximum(0.10 * atr, 2 * spread)
    rompe_alta = (
        (df["Close"] > maxima + margem)
        & (corpo >= corpo_rompimento_min_atr * atr)
        & (df["Close"] >= df["High"] - 0.25 * amplitude)
        & tendencia_alta
    )
    rompe_baixa = (
        (df["Close"] < minima - margem)
        & (-corpo >= corpo_rompimento_min_atr * atr)
        & (df["Close"] <= df["Low"] + 0.25 * amplitude)
        & tendencia_baixa
    )
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")
    ultimo_alta: tuple[int, float] | None = None
    ultimo_baixa: tuple[int, float] | None = None
    usados: set[tuple[str, int]] = set()

    for i in range(len(df)):
        if bool(rompe_alta.iloc[i]):
            ultimo_alta = (i, float(maxima.iloc[i]))
        if bool(rompe_baixa.iloc[i]):
            ultimo_baixa = (i, float(minima.iloc[i]))
        atr_i = float(atr.iloc[i])
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue
        ts = pd.Timestamp(df.index[i])
        if not hora_inicio_utc <= ts.hour < hora_fim_utc:
            continue
        candle = df.iloc[i]
        abertura = float(candle["Open"])
        fechamento = float(candle["Close"])
        maxima_i = float(candle["High"])
        minima_i = float(candle["Low"])
        meio = (maxima_i + minima_i) / 2
        tolerancia = tolerancia_atr * atr_i

        if ultimo_alta and 0 < i - ultimo_alta[0] <= janela_reteste:
            indice_rompimento, nivel = ultimo_alta
            chave = ("buy", indice_rompimento)
            confirma = (
                chave not in usados
                and bool(tendencia_alta.iloc[i])
                and minima_i <= nivel + tolerancia
                and minima_i >= nivel - 0.50 * atr_i
                and fechamento > nivel
                and fechamento > abertura
                and fechamento > meio
                and fechamento - abertura >= corpo_reteste_min_atr * atr_i
            )
            if confirma:
                stop = minima_i - stop_folga_atr * atr_i
                risco = fechamento - stop
                if 0.40 * atr_i <= risco <= 1.50 * atr_i:
                    saida.iloc[i] = PlanoForex(
                        ativo, "buy", ts.to_pydatetime(), nivel, stop,
                        fechamento + retorno_risco * risco, risco,
                        "rompimento_reteste_h1",
                    )
                    usados.add(chave)

        if saida.iloc[i] is None and ultimo_baixa and 0 < i - ultimo_baixa[0] <= janela_reteste:
            indice_rompimento, nivel = ultimo_baixa
            chave = ("sell", indice_rompimento)
            confirma = (
                chave not in usados
                and bool(tendencia_baixa.iloc[i])
                and maxima_i >= nivel - tolerancia
                and maxima_i <= nivel + 0.50 * atr_i
                and fechamento < nivel
                and fechamento < abertura
                and fechamento < meio
                and abertura - fechamento >= corpo_reteste_min_atr * atr_i
            )
            if confirma:
                stop = maxima_i + stop_folga_atr * atr_i
                risco = stop - fechamento
                if 0.40 * atr_i <= risco <= 1.50 * atr_i:
                    saida.iloc[i] = PlanoForex(
                        ativo, "sell", ts.to_pydatetime(), nivel, stop,
                        fechamento - retorno_risco * risco, risco,
                        "rompimento_reteste_h1",
                    )
                    usados.add(chave)
    return saida


def plano_rompimento_reteste_h1(ativo: str, candles: pd.DataFrame, **kwargs) -> PlanoForex | None:
    """Plano no último candle fechado; útil para o monitor em tempo real."""
    if len(candles) < 220:
        return None
    plano = planos_rompimento_reteste_h1(ativo, candles, **kwargs).iloc[-1]
    return plano if isinstance(plano, PlanoForex) else None


def planos_pullback_h1_confirmado(
    ativo: str,
    candles: pd.DataFrame,
    janela_correcao: int = 5,
    tolerancia_atr: float = 0.25,
    corpo_min_atr: float = 0.25,
    stop_folga_atr: float = 0.15,
    retorno_risco: float = 2.0,
    hora_inicio_utc: int = 7,
    hora_fim_utc: int = 17,
) -> pd.Series:
    """Pullback M15 na EMA20, retomada confirmada e tendência estrutural H1."""
    df = candles
    atr = _atr(df)
    ema20 = df["Close"].ewm(span=20, adjust=False).mean()
    ema50 = df["Close"].ewm(span=50, adjust=False).mean()
    h1_alta, h1_baixa = _regime_h1(df)
    corpo = df["Close"] - df["Open"]
    amplitude = (df["High"] - df["Low"]).replace(0, np.nan)
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")
    ultimo_sinal = {"buy": -10_000, "sell": -10_000}

    for i in range(max(60, janela_correcao + 2), len(df)):
        ts = pd.Timestamp(df.index[i])
        if not hora_inicio_utc <= ts.hour < hora_fim_utc:
            continue
        atr_i = float(atr.iloc[i])
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue
        inicio = i - janela_correcao
        anteriores = df.iloc[inicio:i]
        candle = df.iloc[i]
        abertura, fechamento = float(candle["Open"]), float(candle["Close"])
        maxima_i, minima_i = float(candle["High"]), float(candle["Low"])
        margem = tolerancia_atr * atr_i
        corpo_forte = abs(fechamento - abertura) >= corpo_min_atr * atr_i

        correcao_buy = int((anteriores["Close"] < anteriores["Open"]).sum()) >= 2
        tocou_buy = float(anteriores["Low"].min()) <= float(ema20.iloc[i - 1]) + margem
        preservou_buy = float(anteriores["Close"].min()) > float(ema50.iloc[i - 1]) - margem
        confirma_buy = (
            bool(h1_alta.iloc[i])
            and float(ema20.iloc[i]) > float(ema50.iloc[i])
            and correcao_buy and tocou_buy and preservou_buy
            and fechamento > abertura
            and fechamento > float(df["High"].iloc[i - 1])
            and fechamento >= maxima_i - 0.25 * float(amplitude.iloc[i])
            and corpo_forte
            and i - ultimo_sinal["buy"] > janela_correcao
        )
        if confirma_buy:
            stop = float(df["Low"].iloc[inicio:i + 1].min()) - stop_folga_atr * atr_i
            risco = fechamento - stop
            if 0.40 * atr_i <= risco <= 1.80 * atr_i:
                saida.iloc[i] = PlanoForex(
                    ativo, "buy", ts.to_pydatetime(), float(ema20.iloc[i]), stop,
                    fechamento + retorno_risco * risco, risco, "pullback_h1_confirmado",
                )
                ultimo_sinal["buy"] = i
                continue

        correcao_sell = int((anteriores["Close"] > anteriores["Open"]).sum()) >= 2
        tocou_sell = float(anteriores["High"].max()) >= float(ema20.iloc[i - 1]) - margem
        preservou_sell = float(anteriores["Close"].max()) < float(ema50.iloc[i - 1]) + margem
        confirma_sell = (
            bool(h1_baixa.iloc[i])
            and float(ema20.iloc[i]) < float(ema50.iloc[i])
            and correcao_sell and tocou_sell and preservou_sell
            and fechamento < abertura
            and fechamento < float(df["Low"].iloc[i - 1])
            and fechamento <= minima_i + 0.25 * float(amplitude.iloc[i])
            and corpo_forte
            and i - ultimo_sinal["sell"] > janela_correcao
        )
        if confirma_sell:
            stop = float(df["High"].iloc[inicio:i + 1].max()) + stop_folga_atr * atr_i
            risco = stop - fechamento
            if 0.40 * atr_i <= risco <= 1.80 * atr_i:
                saida.iloc[i] = PlanoForex(
                    ativo, "sell", ts.to_pydatetime(), float(ema20.iloc[i]), stop,
                    fechamento - retorno_risco * risco, risco, "pullback_h1_confirmado",
                )
                ultimo_sinal["sell"] = i
    return saida


def plano_pullback_h1_confirmado(ativo: str, candles: pd.DataFrame, **kwargs) -> PlanoForex | None:
    if len(candles) < 220:
        return None
    plano = planos_pullback_h1_confirmado(ativo, candles, **kwargs).iloc[-1]
    return plano if isinstance(plano, PlanoForex) else None


def planos_pullback_h1_com_fibo(
    ativo: str,
    candles: pd.DataFrame,
    janela_correcao: int = 5,
    janela_impulso: int = 32,
    fib_min: float = 0.382,
    fib_max: float = 0.618,
    impulso_min_atr: float = 3.0,
    tolerancia_atr: float = 0.15,
    **kwargs,
) -> pd.Series:
    """Filtra o pullback H1 confirmado pela retração Fibonacci do impulso M15.

    A medição termina antes da correção e usa apenas informação disponível no
    fechamento do candle do sinal, evitando antecipação de dados futuros.
    """
    if not 0 < fib_min < fib_max < 1:
        raise ValueError("Faixa de Fibonacci inválida.")
    planos = planos_pullback_h1_confirmado(
        ativo, candles, janela_correcao=janela_correcao, **kwargs
    )
    atr = _atr(candles)
    filtrados = pd.Series([None] * len(candles), index=candles.index, dtype="object")
    inicio_minimo = janela_impulso + janela_correcao
    for i in range(inicio_minimo, len(candles)):
        plano = planos.iloc[i]
        if not isinstance(plano, PlanoForex):
            continue
        atr_i = float(atr.iloc[i])
        fim_impulso = i - janela_correcao
        impulso = candles.iloc[fim_impulso - janela_impulso:fim_impulso]
        correcao = candles.iloc[fim_impulso:i + 1]
        fundo, topo = float(impulso["Low"].min()), float(impulso["High"].max())
        amplitude = topo - fundo
        if not np.isfinite(atr_i) or amplitude < impulso_min_atr * atr_i:
            continue
        margem = tolerancia_atr * atr_i
        if plano.lado == "buy":
            zona_baixa = topo - fib_max * amplitude
            zona_alta = topo - fib_min * amplitude
            extremo = float(correcao["Low"].min())
            tocou = zona_baixa - margem <= extremo <= zona_alta + margem
        else:
            zona_baixa = fundo + fib_min * amplitude
            zona_alta = fundo + fib_max * amplitude
            extremo = float(correcao["High"].max())
            tocou = zona_baixa - margem <= extremo <= zona_alta + margem
        if tocou:
            filtrados.iloc[i] = PlanoForex(
                plano.ativo, plano.lado, plano.sinal_em,
                (zona_baixa + zona_alta) / 2, plano.stop, plano.alvo,
                plano.risco_preco, "pullback_h1_com_fibo",
            )
    return filtrados


def planos_pullback_h1_nova_york(
    ativo: str, candles: pd.DataFrame, hora_inicio: int = 8, hora_fim: int = 12,
    **kwargs,
) -> pd.Series:
    """Mesmo pullback congelado, limitado à manhã líquida de Nova York."""
    planos = planos_pullback_h1_confirmado(
        ativo, candles, hora_inicio_utc=0, hora_fim_utc=24, **kwargs
    )
    indice = pd.DatetimeIndex(candles.index)
    indice = indice.tz_localize("UTC") if indice.tz is None else indice.tz_convert("UTC")
    horas_ny = indice.tz_convert(ZoneInfo("America/New_York")).hour
    permitido = (horas_ny >= hora_inicio) & (horas_ny < hora_fim)
    return planos.where(permitido, None)


def planos_varredura_londres(
    ativo: str,
    candles: pd.DataFrame,
    stop_folga_atr: float = 0.20,
    varredura_min_atr: float = 0.05,
    range_min_atr: float = 1.0,
    range_max_atr: float = 8.0,
    retorno_risco: float = 2.0,
) -> pd.Series:
    """Varredura da faixa 00-08h e retorno para dentro entre 08-11h de Londres."""
    df = candles
    atr = _atr(df)
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")
    indice_utc = pd.DatetimeIndex(df.index)
    if indice_utc.tz is None:
        indice_utc = indice_utc.tz_localize("UTC")
    else:
        indice_utc = indice_utc.tz_convert("UTC")
    indice_londres = indice_utc.tz_convert(ZoneInfo("Europe/London"))
    datas = pd.Series(indice_londres.date, index=df.index)
    horas = pd.Series(indice_londres.hour, index=df.index)

    for data_local in pd.unique(datas):
        mascara_dia = datas.eq(data_local).to_numpy()
        mascara_asia = mascara_dia & (horas.to_numpy() < 8)
        mascara_trade = mascara_dia & (horas.to_numpy() >= 8) & (horas.to_numpy() < 11)
        pos_asia = np.flatnonzero(mascara_asia)
        pos_trade = np.flatnonzero(mascara_trade)
        if len(pos_asia) < 24 or not len(pos_trade):
            continue
        topo = float(df["High"].iloc[pos_asia].max())
        fundo = float(df["Low"].iloc[pos_asia].min())
        meio = (topo + fundo) / 2
        usado = False

        for i in pos_trade:
            atr_i = float(atr.iloc[i])
            if usado or not np.isfinite(atr_i) or atr_i <= 0:
                continue
            largura = topo - fundo
            if not range_min_atr * atr_i <= largura <= range_max_atr * atr_i:
                continue
            candle = df.iloc[i]
            abertura = float(candle["Open"])
            fechamento = float(candle["Close"])
            maxima_i = float(candle["High"])
            minima_i = float(candle["Low"])
            margem = varredura_min_atr * atr_i

            if minima_i < fundo - margem and fechamento > fundo and fechamento > abertura:
                stop = minima_i - stop_folga_atr * atr_i
                risco = fechamento - stop
                if risco > 0 and meio - fechamento >= retorno_risco * risco:
                    saida.iloc[i] = PlanoForex(
                        ativo, "buy", pd.Timestamp(df.index[i]).to_pydatetime(), fundo,
                        stop, fechamento + retorno_risco * risco, risco,
                        "varredura_asia_londres",
                    )
                    usado = True
            elif maxima_i > topo + margem and fechamento < topo and fechamento < abertura:
                stop = maxima_i + stop_folga_atr * atr_i
                risco = stop - fechamento
                if risco > 0 and fechamento - meio >= retorno_risco * risco:
                    saida.iloc[i] = PlanoForex(
                        ativo, "sell", pd.Timestamp(df.index[i]).to_pydatetime(), topo,
                        stop, fechamento - retorno_risco * risco, risco,
                        "varredura_asia_londres",
                    )
                    usado = True
    return saida


def plano_varredura_londres(ativo: str, candles: pd.DataFrame, **kwargs) -> PlanoForex | None:
    if len(candles) < 100:
        return None
    plano = planos_varredura_londres(ativo, candles, **kwargs).iloc[-1]
    return plano if isinstance(plano, PlanoForex) else None


def planos_rompimento_asia_londres(
    ativo: str,
    candles: pd.DataFrame,
    rompimento_min_atr: float = 0.10,
    corpo_min_atr: float = 0.40,
    stop_folga_atr: float = 0.15,
    range_min_atr: float = 1.0,
    range_max_atr: float = 8.0,
    retorno_risco: float = 2.0,
) -> pd.Series:
    """Continuação após fechamento além da faixa asiática na abertura de Londres."""
    df = candles
    atr = _atr(df)
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")
    indice_utc = pd.DatetimeIndex(df.index)
    indice_utc = indice_utc.tz_localize("UTC") if indice_utc.tz is None else indice_utc.tz_convert("UTC")
    indice_londres = indice_utc.tz_convert(ZoneInfo("Europe/London"))
    datas = pd.Series(indice_londres.date, index=df.index)
    horas = pd.Series(indice_londres.hour, index=df.index)

    for data_local in pd.unique(datas):
        dia = datas.eq(data_local).to_numpy()
        pos_asia = np.flatnonzero(dia & (horas.to_numpy() < 8))
        pos_trade = np.flatnonzero(dia & (horas.to_numpy() >= 8) & (horas.to_numpy() < 11))
        if len(pos_asia) < 24 or not len(pos_trade):
            continue
        topo = float(df["High"].iloc[pos_asia].max())
        fundo = float(df["Low"].iloc[pos_asia].min())
        usado = False

        for i in pos_trade:
            atr_i = float(atr.iloc[i])
            if usado or not np.isfinite(atr_i) or atr_i <= 0:
                continue
            largura = topo - fundo
            if not range_min_atr * atr_i <= largura <= range_max_atr * atr_i:
                continue
            candle = df.iloc[i]
            abertura, fechamento = float(candle["Open"]), float(candle["Close"])
            maxima_i, minima_i = float(candle["High"]), float(candle["Low"])
            amplitude = maxima_i - minima_i
            corpo = fechamento - abertura
            margem = rompimento_min_atr * atr_i

            if (
                fechamento > topo + margem
                and corpo >= corpo_min_atr * atr_i
                and amplitude > 0 and fechamento >= maxima_i - 0.25 * amplitude
            ):
                stop = min(topo - stop_folga_atr * atr_i, minima_i - stop_folga_atr * atr_i)
                risco = fechamento - stop
                if 0.50 * atr_i <= risco <= 2.0 * atr_i:
                    saida.iloc[i] = PlanoForex(
                        ativo, "buy", pd.Timestamp(df.index[i]).to_pydatetime(), topo,
                        stop, fechamento + retorno_risco * risco, risco,
                        "rompimento_asia_londres",
                    )
                    usado = True
            elif (
                fechamento < fundo - margem
                and -corpo >= corpo_min_atr * atr_i
                and amplitude > 0 and fechamento <= minima_i + 0.25 * amplitude
            ):
                stop = max(fundo + stop_folga_atr * atr_i, maxima_i + stop_folga_atr * atr_i)
                risco = stop - fechamento
                if 0.50 * atr_i <= risco <= 2.0 * atr_i:
                    saida.iloc[i] = PlanoForex(
                        ativo, "sell", pd.Timestamp(df.index[i]).to_pydatetime(), fundo,
                        stop, fechamento - retorno_risco * risco, risco,
                        "rompimento_asia_londres",
                    )
                    usado = True
    return saida


def plano_rompimento_asia_londres(ativo: str, candles: pd.DataFrame, **kwargs) -> PlanoForex | None:
    if len(candles) < 100:
        return None
    plano = planos_rompimento_asia_londres(ativo, candles, **kwargs).iloc[-1]
    return plano if isinstance(plano, PlanoForex) else None


def planos_rejeicao_numero_redondo(
    ativo: str,
    candles: pd.DataFrame,
    tolerancia_atr: float = 0.12,
    pavio_min_atr: float = 0.20,
    corpo_min_atr: float = 0.15,
    stop_folga_atr: float = 0.10,
    retorno_risco: float = 2.0,
    hora_inicio_utc: int = 7,
    hora_fim_utc: int = 17,
) -> pd.Series:
    """Rejeição de nível 00/50 conhecido na abertura do candle M15."""
    df = candles
    atr = _atr(df)
    passo = 0.50 if "JPY" in ativo.upper() else 0.0050
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")
    ultimo = -10_000
    for i in range(60, len(df)):
        ts = pd.Timestamp(df.index[i])
        if not hora_inicio_utc <= ts.hour < hora_fim_utc or i - ultimo <= 8:
            continue
        atr_i = float(atr.iloc[i])
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue
        candle = df.iloc[i]
        abertura, fechamento = float(candle["Open"]), float(candle["Close"])
        maxima_i, minima_i = float(candle["High"]), float(candle["Low"])
        nivel = round(abertura / passo) * passo
        tolerancia = tolerancia_atr * atr_i
        corpo = fechamento - abertura

        rejeita_baixo = (
            minima_i <= nivel + tolerancia and minima_i >= nivel - 0.40 * atr_i
            and fechamento > nivel + tolerancia
            and corpo >= corpo_min_atr * atr_i
            and min(abertura, fechamento) - minima_i >= pavio_min_atr * atr_i
        )
        if rejeita_baixo:
            stop = minima_i - stop_folga_atr * atr_i
            risco = fechamento - stop
            if 0.35 * atr_i <= risco <= 1.50 * atr_i:
                saida.iloc[i] = PlanoForex(
                    ativo, "buy", ts.to_pydatetime(), nivel, stop,
                    fechamento + retorno_risco * risco, risco, "rejeicao_numero_redondo",
                )
                ultimo = i
                continue

        rejeita_cima = (
            maxima_i >= nivel - tolerancia and maxima_i <= nivel + 0.40 * atr_i
            and fechamento < nivel - tolerancia
            and -corpo >= corpo_min_atr * atr_i
            and maxima_i - max(abertura, fechamento) >= pavio_min_atr * atr_i
        )
        if rejeita_cima:
            stop = maxima_i + stop_folga_atr * atr_i
            risco = stop - fechamento
            if 0.35 * atr_i <= risco <= 1.50 * atr_i:
                saida.iloc[i] = PlanoForex(
                    ativo, "sell", ts.to_pydatetime(), nivel, stop,
                    fechamento - retorno_risco * risco, risco, "rejeicao_numero_redondo",
                )
                ultimo = i
    return saida


def planos_rompimento_numero_redondo(
    ativo: str,
    candles: pd.DataFrame,
    margem_atr: float = 0.12,
    corpo_min_atr: float = 0.45,
    stop_folga_atr: float = 0.12,
    retorno_risco: float = 2.0,
    hora_inicio_utc: int = 7,
    hora_fim_utc: int = 17,
) -> pd.Series:
    """Continuação após cruzar e fechar forte além de um nível 00/50."""
    df = candles
    atr = _atr(df)
    passo = 0.50 if "JPY" in ativo.upper() else 0.0050
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")
    ultimo = -10_000
    for i in range(60, len(df)):
        ts = pd.Timestamp(df.index[i])
        if not hora_inicio_utc <= ts.hour < hora_fim_utc or i - ultimo <= 8:
            continue
        atr_i = float(atr.iloc[i])
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue
        c = df.iloc[i]
        abertura, fechamento = float(c["Open"]), float(c["Close"])
        maxima_i, minima_i = float(c["High"]), float(c["Low"])
        corpo = fechamento - abertura
        nivel_buy = (np.floor(abertura / passo) + 1) * passo
        nivel_sell = np.floor(abertura / passo) * passo
        margem = margem_atr * atr_i
        if abertura < nivel_buy and fechamento > nivel_buy + margem and corpo >= corpo_min_atr * atr_i:
            stop = min(nivel_buy - stop_folga_atr * atr_i, minima_i - stop_folga_atr * atr_i)
            risco = fechamento - stop
            if 0.45 * atr_i <= risco <= 1.80 * atr_i:
                saida.iloc[i] = PlanoForex(
                    ativo, "buy", ts.to_pydatetime(), float(nivel_buy), stop,
                    fechamento + retorno_risco * risco, risco, "rompimento_numero_redondo",
                )
                ultimo = i
        elif abertura > nivel_sell and fechamento < nivel_sell - margem and -corpo >= corpo_min_atr * atr_i:
            stop = max(nivel_sell + stop_folga_atr * atr_i, maxima_i + stop_folga_atr * atr_i)
            risco = stop - fechamento
            if 0.45 * atr_i <= risco <= 1.80 * atr_i:
                saida.iloc[i] = PlanoForex(
                    ativo, "sell", ts.to_pydatetime(), float(nivel_sell), stop,
                    fechamento - retorno_risco * risco, risco, "rompimento_numero_redondo",
                )
                ultimo = i
    return saida


def planos_reversao_choque_m15(
    ativo: str,
    candles: pd.DataFrame,
    choque_min_atr: float = 1.35,
    stop_folga_atr: float = 0.10,
    retorno_risco: float = 2.0,
    hora_inicio_utc: int = 7,
    hora_fim_utc: int = 17,
) -> pd.Series:
    """Reversão no Open seguinte a um deslocamento M15 excepcional."""
    df = candles
    atr = _atr(df)
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")
    ultimo = -10_000
    for i in range(60, len(df)):
        ts = pd.Timestamp(df.index[i])
        if not hora_inicio_utc <= ts.hour < hora_fim_utc or i - ultimo <= 8:
            continue
        atr_i = float(atr.iloc[i])
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue
        c = df.iloc[i]
        abertura, fechamento = float(c["Open"]), float(c["Close"])
        maxima_i, minima_i = float(c["High"]), float(c["Low"])
        amplitude = maxima_i - minima_i
        corpo = fechamento - abertura
        if amplitude <= 0:
            continue
        if corpo >= choque_min_atr * atr_i and fechamento >= maxima_i - 0.15 * amplitude:
            stop = maxima_i + stop_folga_atr * atr_i
            risco = stop - fechamento
            if 0.10 * atr_i <= risco <= 0.70 * atr_i:
                saida.iloc[i] = PlanoForex(
                    ativo, "sell", ts.to_pydatetime(), fechamento, stop,
                    fechamento - retorno_risco * risco, risco, "reversao_choque_m15",
                )
                ultimo = i
        elif -corpo >= choque_min_atr * atr_i and fechamento <= minima_i + 0.15 * amplitude:
            stop = minima_i - stop_folga_atr * atr_i
            risco = fechamento - stop
            if 0.10 * atr_i <= risco <= 0.70 * atr_i:
                saida.iloc[i] = PlanoForex(
                    ativo, "buy", ts.to_pydatetime(), fechamento, stop,
                    fechamento + retorno_risco * risco, risco, "reversao_choque_m15",
                )
                ultimo = i
    return saida


def planos_continuacao_choque_m15(
    ativo: str, candles: pd.DataFrame, choque_min_atr: float = 1.35,
    stop_folga_atr: float = 0.10, retorno_risco: float = 2.0,
    hora_inicio_utc: int = 7, hora_fim_utc: int = 17,
) -> pd.Series:
    """Continuação no Open seguinte a um deslocamento M15 anormal."""
    df=candles; atr=_atr(df); saida=pd.Series([None]*len(df),index=df.index,dtype="object"); ultimo=-10_000
    for i in range(60,len(df)):
        ts=pd.Timestamp(df.index[i])
        if not hora_inicio_utc<=ts.hour<hora_fim_utc or i-ultimo<=8: continue
        atr_i=float(atr.iloc[i])
        if not np.isfinite(atr_i) or atr_i<=0: continue
        c=df.iloc[i]; o=float(c["Open"]); f=float(c["Close"]); hi=float(c["High"]); lo=float(c["Low"])
        amp=hi-lo; corpo=f-o
        if amp<=0: continue
        meio=(hi+lo)/2
        if corpo>=choque_min_atr*atr_i and f>=hi-.15*amp:
            stop=meio-stop_folga_atr*atr_i; risco=f-stop
            if .50*atr_i<=risco<=1.30*atr_i:
                saida.iloc[i]=PlanoForex(ativo,"buy",ts.to_pydatetime(),hi,stop,f+retorno_risco*risco,risco,"continuacao_choque_m15"); ultimo=i
        elif -corpo>=choque_min_atr*atr_i and f<=lo+.15*amp:
            stop=meio+stop_folga_atr*atr_i; risco=stop-f
            if .50*atr_i<=risco<=1.30*atr_i:
                saida.iloc[i]=PlanoForex(ativo,"sell",ts.to_pydatetime(),lo,stop,f-retorno_risco*risco,risco,"continuacao_choque_m15"); ultimo=i
    return saida


def planos_segunda_entrada_ema21(
    ativo: str, candles: pd.DataFrame, janela: int = 10,
    tolerancia_atr: float = 0.35, stop_folga_atr: float = 0.10,
    retorno_risco: float = 1.0, hora_inicio_utc: int = 7, hora_fim_utc: int = 17,
) -> pd.Series:
    """Duas tentativas de pullback na EMA21, com retomada a favor do H1."""
    df=candles; atr=_atr(df); ema=df["Close"].ewm(span=21,adjust=False).mean()
    alta,baixa=_regime_h1(df); saida=pd.Series([None]*len(df),index=df.index,dtype="object"); ultimo=-10_000
    low_v=df["Low"].to_numpy(dtype=float); high_v=df["High"].to_numpy(dtype=float)
    open_v=df["Open"].to_numpy(dtype=float); close_v=df["Close"].to_numpy(dtype=float)
    ema_v=ema.to_numpy(dtype=float); atr_v=atr.to_numpy(dtype=float)
    piv_lows=np.flatnonzero((low_v[1:-1]<=low_v[:-2]) & (low_v[1:-1]<low_v[2:]))+1
    piv_highs=np.flatnonzero((high_v[1:-1]>=high_v[:-2]) & (high_v[1:-1]>high_v[2:]))+1
    for i in range(max(80,janela+2),len(df)):
        ts=pd.Timestamp(df.index[i])
        if not hora_inicio_utc<=ts.hour<hora_fim_utc or i-ultimo<=8: continue
        atr_i=atr_v[i]
        if not np.isfinite(atr_i) or atr_i<=0: continue
        inicio=i-janela
        lows=piv_lows[np.searchsorted(piv_lows,inicio+1):np.searchsorted(piv_lows,i-1)]
        highs=piv_highs[np.searchsorted(piv_highs,inicio+1):np.searchsorted(piv_highs,i-1)]
        o=open_v[i]; f=close_v[i]
        if bool(alta.iloc[i]) and len(lows)>=2:
            p1,p2=lows[-2:]
            perto=abs(low_v[p2]-ema_v[p2])<=tolerancia_atr*atr_i
            higher=low_v[p2]>=low_v[p1]-.10*atr_i
            if p2-p1>=2 and perto and higher and f>o and f>high_v[i-1]:
                stop=low_v[p2]-stop_folga_atr*atr_i; risco=f-stop
                if .35*atr_i<=risco<=1.50*atr_i:
                    saida.iloc[i]=PlanoForex(ativo,"buy",ts.to_pydatetime(),ema_v[p2],stop,f+retorno_risco*risco,risco,"segunda_entrada_ema21");ultimo=i;continue
        if bool(baixa.iloc[i]) and len(highs)>=2:
            p1,p2=highs[-2:]
            perto=abs(high_v[p2]-ema_v[p2])<=tolerancia_atr*atr_i
            lower=high_v[p2]<=high_v[p1]+.10*atr_i
            if p2-p1>=2 and perto and lower and f<o and f<low_v[i-1]:
                stop=high_v[p2]+stop_folga_atr*atr_i;risco=stop-f
                if .35*atr_i<=risco<=1.50*atr_i:
                    saida.iloc[i]=PlanoForex(ativo,"sell",ts.to_pydatetime(),ema_v[p2],stop,f-retorno_risco*risco,risco,"segunda_entrada_ema21");ultimo=i
    return saida


def planos_reversao_media_rsi_bollinger(
    ativo: str, candles: pd.DataFrame, periodo: int = 20,
    desvios: float = 2.0, z_min: float = 1.25, stop_atr: float = 1.5,
    hora_inicio_utc: int = 7, hora_fim_utc: int = 17,
) -> pd.Series:
    """RSI14 extremo + Bollinger + z-score; alvo na média conhecida no sinal."""
    df=candles; close=df["Close"]; media=close.rolling(periodo).mean(); desvio=close.rolling(periodo).std(ddof=0)
    z=(close-media)/desvio.replace(0,np.nan); superior=media+desvios*desvio; inferior=media-desvios*desvio
    delta=close.diff(); ganho=delta.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    perda=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    rsi=100-(100/(1+ganho/perda.replace(0,np.nan))); atr=_atr(df)
    saida=pd.Series([None]*len(df),index=df.index,dtype="object");ultimo=-10_000
    for i in range(60,len(df)):
        ts=pd.Timestamp(df.index[i])
        if not hora_inicio_utc<=ts.hour<hora_fim_utc or i-ultimo<=8:continue
        a=float(atr.iloc[i]);f=float(close.iloc[i]);m=float(media.iloc[i])
        if not np.isfinite(a) or a<=0 or not np.isfinite(m):continue
        if f<float(inferior.iloc[i]) and float(rsi.iloc[i])<25 and float(z.iloc[i])<-z_min:
            stop=f-stop_atr*a;risco=f-stop
            if m>f:
                saida.iloc[i]=PlanoForex(ativo,"buy",ts.to_pydatetime(),float(inferior.iloc[i]),stop,m,risco,"reversao_media_rsi_bollinger");ultimo=i
        elif f>float(superior.iloc[i]) and float(rsi.iloc[i])>75 and float(z.iloc[i])>z_min:
            stop=f+stop_atr*a;risco=stop-f
            if m<f:
                saida.iloc[i]=PlanoForex(ativo,"sell",ts.to_pydatetime(),float(superior.iloc[i]),stop,m,risco,"reversao_media_rsi_bollinger");ultimo=i
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


def planos_pullback_fibo_estrutura_h1(
    ativo: str,
    candles: pd.DataFrame,
    raio_pivo: int = 2,
    fib_min: float = 0.382,
    fib_max: float = 0.618,
    tolerancia_atr: float = 0.20,
    impulso_min_atr: float = 3.0,
    corpo_min_atr: float = 0.25,
    stop_folga_atr: float = 0.15,
    rr_minimo: float = 2.0,
    max_candles_correcao: int = 16,
) -> pd.Series:
    """Pullback Fibonacci M15 confirmado, somente a favor da estrutura H1.

    Usa apenas pivôs já confirmados. A zona precisa coincidir com EMA20 ou
    suporte/resistência anterior, e o candle deve rejeitar a zona. O alvo é o
    extremo do impulso e precisa oferecer ao menos ``rr_minimo``.
    """
    if not 0 < fib_min < fib_max < 1:
        raise ValueError("Faixa de Fibonacci inválida.")
    df = candles
    atr = _atr(df)
    ema20 = df["Close"].ewm(span=20, adjust=False).mean()
    h1_alta, h1_baixa = _regime_h1(df)
    largura = raio_pivo * 2 + 1
    fundos_raw = df["Low"].eq(df["Low"].rolling(largura, center=True).min())
    topos_raw = df["High"].eq(df["High"].rolling(largura, center=True).max())
    fundos_confirmados = df["Low"].where(fundos_raw).shift(raio_pivo)
    topos_confirmados = df["High"].where(topos_raw).shift(raio_pivo)
    fundos: list[tuple[int, float]] = []
    topos: list[tuple[int, float]] = []
    usados: set[tuple[str, int, int]] = set()
    saida = pd.Series([None] * len(df), index=df.index, dtype="object")

    for i in range(len(df)):
        if pd.notna(fundos_confirmados.iloc[i]):
            fundos.append((i - raio_pivo, float(fundos_confirmados.iloc[i])))
        if pd.notna(topos_confirmados.iloc[i]):
            topos.append((i - raio_pivo, float(topos_confirmados.iloc[i])))
        atr_i = float(atr.iloc[i])
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue
        candle = df.iloc[i]
        abertura, fechamento = float(candle["Open"]), float(candle["Close"])
        maxima, minima = float(candle["High"]), float(candle["Low"])
        amplitude_candle = maxima - minima
        corpo = abs(fechamento - abertura)
        tolerancia = tolerancia_atr * atr_i

        if bool(h1_alta.iloc[i]) and topos:
            i_topo, topo = topos[-1]
            origem = next(((j, p) for j, p in reversed(fundos) if j < i_topo), None)
            if origem and 0 < i - i_topo <= max_candles_correcao:
                i_fundo, fundo = origem
                impulso = topo - fundo
                zona_baixa = topo - fib_max * impulso
                zona_alta = topo - fib_min * impulso
                sr = any(
                    j < i_topo and zona_baixa - tolerancia <= nivel <= zona_alta + tolerancia
                    for j, nivel in topos[:-1]
                )
                ema = zona_baixa - tolerancia <= float(ema20.iloc[i]) <= zona_alta + tolerancia
                chave = ("buy", i_fundo, i_topo)
                confirma = (
                    chave not in usados and impulso >= impulso_min_atr * atr_i
                    and minima <= zona_alta and maxima >= zona_baixa
                    and (sr or ema) and fechamento > abertura
                    and amplitude_candle > 0 and fechamento >= maxima - 0.35 * amplitude_candle
                    and corpo >= corpo_min_atr * atr_i
                )
                if confirma:
                    stop = minima - stop_folga_atr * atr_i
                    risco = fechamento - stop
                    recompensa = topo - fechamento
                    if 0.40 * atr_i <= risco <= 1.80 * atr_i and recompensa / risco >= rr_minimo:
                        saida.iloc[i] = PlanoForex(
                            ativo, "buy", df.index[i].to_pydatetime(),
                            (zona_baixa + zona_alta) / 2, stop, topo, risco,
                            "pullback_fibo_estrutura_h1",
                        )
                        usados.add(chave)
                        continue

        if bool(h1_baixa.iloc[i]) and fundos:
            i_fundo, fundo = fundos[-1]
            origem = next(((j, p) for j, p in reversed(topos) if j < i_fundo), None)
            if origem and 0 < i - i_fundo <= max_candles_correcao:
                i_topo, topo = origem
                impulso = topo - fundo
                zona_baixa = fundo + fib_min * impulso
                zona_alta = fundo + fib_max * impulso
                sr = any(
                    j < i_fundo and zona_baixa - tolerancia <= nivel <= zona_alta + tolerancia
                    for j, nivel in fundos[:-1]
                )
                ema = zona_baixa - tolerancia <= float(ema20.iloc[i]) <= zona_alta + tolerancia
                chave = ("sell", i_topo, i_fundo)
                confirma = (
                    chave not in usados and impulso >= impulso_min_atr * atr_i
                    and maxima >= zona_baixa and minima <= zona_alta
                    and (sr or ema) and fechamento < abertura
                    and amplitude_candle > 0 and fechamento <= minima + 0.35 * amplitude_candle
                    and corpo >= corpo_min_atr * atr_i
                )
                if confirma:
                    stop = maxima + stop_folga_atr * atr_i
                    risco = stop - fechamento
                    recompensa = fechamento - fundo
                    if 0.40 * atr_i <= risco <= 1.80 * atr_i and recompensa / risco >= rr_minimo:
                        saida.iloc[i] = PlanoForex(
                            ativo, "sell", df.index[i].to_pydatetime(),
                            (zona_baixa + zona_alta) / 2, stop, fundo, risco,
                            "pullback_fibo_estrutura_h1",
                        )
                        usados.add(chave)
    return saida
