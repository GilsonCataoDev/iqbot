import logging
from dataclasses import replace as _dc_replace

import numpy as np
import pandas as pd

from .config import Configuracao
from .modelos import Decisao

logger = logging.getLogger(__name__)

PRIORIDADE_SETUP: dict[str, int] = {
    "rejeicao_m1_hierarquico": 0,  # nova — hierarquia M15→M5→M1 com scoring
    "retracao_intracandle":   1,
    "pullback_confluencia":   2,
    "ema920_pullback":         3,
    "ema920_prime":            3,
    "ema921_rsi_pullback":     3,
    "ema921_rsi_intravela":    3,
    "nzd_trend_pullback_v1":   3,
    "forex_reteste_m15":      3,
    "breakout_reteste":       4,
    "noticia_confirmada":     5,
    "fibo_sr_retracao":       6,
    "reversao_confluencia":   6,
    "reversao_bollinger_rsi": 7,
    "sr_rejeicao":            8,
    "engulfing_sr":           9,
    "pin_bar_sr":            10,
    "pullback":              11,
    "bollinger_squeeze":     12,
    "divergencia_rsi":       13,
    "macd_crossover":        14,
    "reversao_candle":       15,
}
_PRIORIDADE_DEFAULT = 99


class EstrategiaReversaoM5:
    """Transforma candles fechados em uma decisão; não conhece IQ nem ordens."""

    def __init__(self, config: Configuracao):
        self.config = config
        self._cache_indicadores: dict[str, pd.DataFrame] = {}
        self._cache_ultimo_fechado: dict[str, pd.Timestamp] = {}
        self._erros_consecutivos: dict[str, int] = {}
        self._estrategias_desativadas: set[str] = set()
        self._tendencia_h4: dict[str, str] = {}   # ativo -> "alta"|"baixa"|"lateral"
        self._tendencia_h1: dict[str, str] = {}   # ativo -> "alta"|"baixa"|"lateral"
        self._estrutura_m5: dict[str, str] = {}   # ativo -> "alta"|"baixa"|"lateral"
        self._contexto_m15: dict[str, str] = {}   # ativo -> "alta"|"baixa"|"lateral"

    def calcular_tendencia_h4(self, candles_h4: pd.DataFrame) -> str:
        """Tendência macro via EMA(h4_ema_periodo) no H4. Retorna 'alta', 'baixa' ou 'lateral'."""
        c = self.config
        min_c = c.h4_ema_periodo + c.h4_slope_janela + 1
        if candles_h4 is None or len(candles_h4) < min_c:
            return "lateral"
        close = candles_h4["Close"]
        ema = close.ewm(span=c.h4_ema_periodo, adjust=False).mean()
        inclinacao = ema.diff(c.h4_slope_janela).iloc[-1]
        atr_h4 = (candles_h4["High"] - candles_h4["Low"]).rolling(c.atr_periodo).mean().iloc[-1]
        if pd.isna(inclinacao) or pd.isna(atr_h4) or atr_h4 <= 0:
            return "lateral"
        limiar = c.slope_limiar_atr * atr_h4
        if inclinacao > limiar:
            return "alta"
        if inclinacao < -limiar:
            return "baixa"
        return "lateral"

    def atualizar_contexto_h4(self, ativo: str, tendencia: str) -> None:
        self._tendencia_h4[ativo] = tendencia

    def calcular_tendencia_h1(self, candles_h1: pd.DataFrame) -> str:
        """Calcula a tendência do H1 pela inclinação da EMA no H1.

        Usa EMA(h1_ema_periodo) e mede inclinação em h1_slope_janela candles.
        Retorna 'alta', 'baixa' ou 'lateral'.
        """
        c = self.config
        min_c = c.h1_ema_periodo + c.h1_slope_janela + 1
        if candles_h1 is None or len(candles_h1) < min_c:
            return "lateral"
        close = candles_h1["Close"]
        ema = close.ewm(span=c.h1_ema_periodo, adjust=False).mean()
        inclinacao = ema.diff(c.h1_slope_janela).iloc[-1]
        atr_h1 = (candles_h1["High"] - candles_h1["Low"]).rolling(c.atr_periodo).mean().iloc[-1]
        if pd.isna(inclinacao) or pd.isna(atr_h1) or atr_h1 <= 0:
            return "lateral"
        limiar = c.slope_limiar_atr * atr_h1
        if inclinacao > limiar:
            return "alta"
        if inclinacao < -limiar:
            return "baixa"
        return "lateral"

    def atualizar_contexto_h1(self, ativo: str, tendencia: str) -> None:
        self._tendencia_h1[ativo] = tendencia

    def calcular_estrutura_m5(self, candles_m5: pd.DataFrame) -> str:
        """Calcula a estrutura direcional do M5 pela inclinação da EMA.

        Usa EMA(m5_ema_periodo) e mede inclinação em m5_slope_janela candles.
        Retorna 'alta', 'baixa' ou 'lateral'.
        """
        c = self.config
        min_c = c.m5_ema_periodo + c.m5_slope_janela + 1
        if candles_m5 is None or len(candles_m5) < min_c:
            return "lateral"
        close = candles_m5["Close"]
        ema = close.ewm(span=c.m5_ema_periodo, adjust=False).mean()
        inclinacao = ema.diff(c.m5_slope_janela).iloc[-1]
        atr_m5 = (candles_m5["High"] - candles_m5["Low"]).rolling(c.atr_periodo).mean().iloc[-1]
        if pd.isna(inclinacao) or pd.isna(atr_m5) or atr_m5 <= 0:
            return "lateral"
        limiar = c.slope_limiar_atr * atr_m5
        if inclinacao > limiar:
            return "alta"
        if inclinacao < -limiar:
            return "baixa"
        return "lateral"

    def atualizar_contexto_m5(self, ativo: str, estrutura: str) -> None:
        self._estrutura_m5[ativo] = estrutura

    def calcular_contexto_m15(self, candles_m15: pd.DataFrame) -> str:
        """Calcula contexto de tendência M15 via EMA200. Retorna 'alta', 'baixa' ou 'lateral'."""
        c = self.config
        min_c = c.m15_ema200_periodo + c.m15_slope_janela + 1
        if candles_m15 is None or len(candles_m15) < min_c:
            return "lateral"
        close = candles_m15["Close"]
        ema200 = close.ewm(span=c.m15_ema200_periodo, adjust=False).mean()
        inclinacao = ema200.diff(c.m15_slope_janela).iloc[-1]
        atr_m15 = (candles_m15["High"] - candles_m15["Low"]).rolling(14).mean().iloc[-1]
        if pd.isna(inclinacao) or pd.isna(atr_m15) or atr_m15 <= 0:
            return "lateral"
        limiar = 0.05 * float(atr_m15)
        inclinacao_f = float(inclinacao)
        if inclinacao_f > limiar:
            return "alta"
        if inclinacao_f < -limiar:
            return "baixa"
        return "lateral"

    def atualizar_contexto_m15(self, ativo: str, contexto: str) -> None:
        self._contexto_m15[ativo] = contexto

    def _h4_permite(self, ativo: str, direcao: str) -> bool:
        if not self.config.filtro_h4_ativo:
            return True
        th4 = self._tendencia_h4.get(ativo, "lateral")
        if th4 == "lateral":
            return True
        return not (th4 == "alta" and direcao == "put") and not (th4 == "baixa" and direcao == "call")

    def _m15_permite(self, ativo: str, direcao: str) -> bool:
        if not self.config.filtro_m15_ativo:
            return True
        ctx = self._contexto_m15.get(ativo, "lateral")
        if ctx == "lateral":
            return True
        return not (ctx == "alta" and direcao == "put") and not (ctx == "baixa" and direcao == "call")

    def _m5_alinha(self, ativo: str, direcao: str) -> bool:
        if not self.config.filtro_m5_ativo:
            return True
        estrutura = self._estrutura_m5.get(ativo, "lateral")
        if estrutura == "lateral":
            return True
        return not (estrutura == "alta" and direcao == "put") and not (estrutura == "baixa" and direcao == "call")

    def _h1_permite(self, ativo: str, direcao: str) -> bool:
        if not self.config.filtro_h1_ativo:
            return True
        th1 = self._tendencia_h1.get(ativo, "lateral")
        if th1 == "lateral":
            return True
        return not (th1 == "alta" and direcao == "put") and not (th1 == "baixa" and direcao == "call")

    def calcular_indicadores(self, candles: pd.DataFrame, ativo: str = "") -> pd.DataFrame:
        if not ativo or len(candles) < 3:
            return self._calcular_do_zero(candles)

        ultimo_fechado = candles.index[-2]
        cacheado = self._cache_indicadores.get(ativo)
        ts_cacheado = self._cache_ultimo_fechado.get(ativo)

        if (
            cacheado is not None
            and ts_cacheado == ultimo_fechado
            and len(cacheado) == len(candles)
        ):
            return cacheado

        df = self._calcular_do_zero(candles)
        self._cache_indicadores[ativo] = df
        self._cache_ultimo_fechado[ativo] = ultimo_fechado
        return df

    def _calcular_do_zero(self, candles: pd.DataFrame) -> pd.DataFrame:
        c = self.config
        df = candles.copy()
        close, high, low = df["Close"], df["High"], df["Low"]

        media = close.rolling(c.bb_periodo).mean()
        desvio = close.rolling(c.bb_periodo).std()
        df["BandaMedia"] = media
        df["BandaSup"] = media + c.bb_desvio * desvio
        df["BandaInf"] = media - c.bb_desvio * desvio

        delta = close.diff()
        ganho = delta.clip(lower=0).rolling(c.rsi_periodo).mean()
        perda = (-delta.clip(upper=0)).rolling(c.rsi_periodo).mean()
        rs = ganho / perda.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.mask((perda == 0) & (ganho > 0), 100)
        rsi = rsi.mask((ganho == 0) & (perda > 0), 0)
        rsi = rsi.mask((ganho == 0) & (perda == 0), 50)
        df["RSI"] = rsi

        true_range = pd.concat(
            [
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["ATR"] = true_range.rolling(c.atr_periodo).mean()
        # ADX/DI: mede se há tendência suficiente para tratar a EMA como
        # suporte/resistência dinâmica, em vez de operar um cruzamento lateral.
        movimento_alta = high.diff()
        movimento_baixa = -low.diff()
        dm_mais = movimento_alta.where(
            (movimento_alta > movimento_baixa) & (movimento_alta > 0), 0.0
        )
        dm_menos = movimento_baixa.where(
            (movimento_baixa > movimento_alta) & (movimento_baixa > 0), 0.0
        )
        atr_seguro = df["ATR"].replace(0, np.nan)
        df["DI_Mais"] = 100 * dm_mais.rolling(c.atr_periodo).mean() / atr_seguro
        df["DI_Menos"] = 100 * dm_menos.rolling(c.atr_periodo).mean() / atr_seguro
        soma_di = (df["DI_Mais"] + df["DI_Menos"]).replace(0, np.nan)
        dx = 100 * (df["DI_Mais"] - df["DI_Menos"]).abs() / soma_di
        df["ADX"] = dx.rolling(c.atr_periodo).mean()
        df["EMA_Micro"] = close.ewm(span=c.ema_micro_periodo, adjust=False).mean()
        df["EMA_Macro"] = close.ewm(span=c.ema_macro_periodo, adjust=False).mean()

        inclinacao = df["EMA_Macro"].diff(c.macro_slope_janela)
        limiar = c.slope_limiar_atr * df["ATR"]
        df["TendenciaMacro"] = np.select(
            [inclinacao > limiar, inclinacao < -limiar],
            ["alta", "baixa"],
            default="lateral",
        )
        df["InclinacaoMacro"] = inclinacao

        ema_fast = close.ewm(span=c.macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=c.macd_slow, adjust=False).mean()
        df["MACD"] = ema_fast - ema_slow
        df["MACD_Signal"] = df["MACD"].ewm(span=c.macd_signal, adjust=False).mean()
        df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]
        return df

    def _avaliar_indicadores(self, ativo: str, df: pd.DataFrame, indice_confirmacao: int) -> Decisao | None:
        if not self.config.reversao_bollinger_rsi_ativo:
            return None
        minimo = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 1
        if indice_confirmacao < minimo or indice_confirmacao >= len(df):
            return None
        esticado = df.iloc[indice_confirmacao - 1]
        confirmacao = df.iloc[indice_confirmacao]
        campos = ["ATR", "RSI", "BandaInf", "BandaSup"]
        if any(pd.isna(esticado[c]) or pd.isna(confirmacao[c]) for c in campos):
            return None
        if not self.config.bollinger_aceitar_tendencia and confirmacao["TendenciaMacro"] != "lateral":
            return None

        inicio_atr = max(0, indice_confirmacao - self.config.atr_regime_janela)
        historico_atr = df["ATR"].iloc[inicio_atr:indice_confirmacao].dropna()
        if len(historico_atr):
            mediana = float(historico_atr.median())
            if mediana > 0 and confirmacao["ATR"] > self.config.atr_max_multiplo_mediana * mediana:
                return None

        rsi_sv = self.config.rsi_sobrevendido
        rsi_sc = self.config.rsi_sobrecomprado
        call = (
            esticado["Close"] < esticado["BandaInf"]
            and esticado["RSI"] < rsi_sv
            and confirmacao["Close"] >= confirmacao["BandaInf"]
            and confirmacao["RSI"] >= rsi_sv
        )
        put = (
            esticado["Close"] > esticado["BandaSup"]
            and esticado["RSI"] > rsi_sc
            and confirmacao["Close"] <= confirmacao["BandaSup"]
            and confirmacao["RSI"] <= rsi_sc
        )
        direcao = "call" if call else "put" if put else None
        if direcao is None:
            return None

        _rsi_e = float(esticado["RSI"])
        _rsi_c = float(confirmacao["RSI"])
        _banda = "inferior (sobrevenda)" if direcao == "call" else "superior (sobrecompra)"
        return Decisao(
            ativo=ativo,
            direcao=direcao,
            preco=float(confirmacao["Close"]),
            candle_hora=pd.Timestamp(df.index[indice_confirmacao]),
            motivo="retorno_bollinger_rsi_m5",
            detalhes={
                "setup": "reversao_bollinger_rsi",
                "rsi_estirado": _rsi_e,
                "rsi_confirmacao": _rsi_c,
                "atr": float(confirmacao["ATR"]),
                "tendencia": str(confirmacao["TendenciaMacro"]),
                "razao": [
                    f"Preço violou a Banda de Bollinger {_banda}",
                    f"RSI esticado em {_rsi_e:.1f} → mercado exausto",
                    f"Candle de confirmação: RSI voltou para {_rsi_c:.1f}",
                    f"Fechamento confirma reversão na direção {direcao.upper()}",
                ],
            },
        )

    def _atr_regime_valido(self, df: pd.DataFrame, indice: int) -> bool:
        atr = df.iloc[indice].get("ATR")
        if pd.isna(atr):
            return False
        atr_f = float(atr)
        inicio = max(0, indice - self.config.atr_regime_janela)
        historico = df["ATR"].iloc[inicio:indice].dropna()
        if not len(historico):
            return True
        mediana = float(historico.median())
        if mediana <= 0:
            return True
        # Bloqueia volatilidade excessiva
        if atr_f > self.config.atr_max_multiplo_mediana * mediana:
            return False
        # Bloqueia volatilidade muito baixa (spread domina no M1)
        if self.config.atr_min_multiplo_mediana > 0 and atr_f < self.config.atr_min_multiplo_mediana * mediana:
            return False
        return True

    def _pivos(self, df: pd.DataFrame, indice_fim: int) -> tuple[list[float], list[float]]:
        c = self.config
        inicio = max(0, indice_fim - c.pullback_janela)
        trecho = df.iloc[inicio:indice_fim]
        raio = c.pullback_pivo_raio
        suportes: list[float] = []
        resistencias: list[float] = []
        for posicao in range(raio, len(trecho) - raio):
            janela = trecho.iloc[posicao - raio:posicao + raio + 1]
            atual = trecho.iloc[posicao]
            if float(atual["Low"]) <= float(janela["Low"].min()):
                suportes.append(float(atual["Low"]))
            if float(atual["High"]) >= float(janela["High"].max()):
                resistencias.append(float(atual["High"]))
        return suportes, resistencias

    def niveis_sr_atuais(self, df: pd.DataFrame, indice: int) -> dict[str, list[float]]:
        """Retorna suportes e resistências visíveis ao redor do preço atual.

        Filtra para ±4 ATR do Close atual e agrupa níveis próximos (cluster 0.4 ATR).
        Usado para plotagem no gráfico — máx 6 linhas por lado.
        """
        raio_min = self.config.pullback_pivo_raio * 2 + 5
        if indice < raio_min or indice >= len(df):
            return {"suportes": [], "resistencias": []}

        vela = df.iloc[indice]
        try:
            preco_atual = float(vela["Close"])
            atr = float(vela["ATR"])
        except (KeyError, TypeError, ValueError):
            return {"suportes": [], "resistencias": []}
        if atr <= 0:
            return {"suportes": [], "resistencias": []}

        suportes, resistencias = self._pivos(df, indice)
        janela_display = 4.0 * atr
        cluster_min   = 0.4  * atr

        def _filtrar(niveis: list[float], reverso: bool) -> list[float]:
            niveis = [n for n in niveis if abs(n - preco_atual) <= janela_display]
            if not niveis:
                return []
            niveis = sorted(set(niveis), reverse=reverso)
            agrupados: list[float] = []
            for n in niveis:
                if not agrupados or abs(n - agrupados[-1]) > cluster_min:
                    agrupados.append(round(n, 5))
            return agrupados[:6]

        return {
            "suportes":     _filtrar(suportes,     reverso=True),
            "resistencias": _filtrar(resistencias, reverso=False),
        }

    def _fundos_swing(self, series: pd.Series, raio: int) -> list[int]:
        arr = series.to_numpy()
        return [
            i for i in range(raio, len(arr) - raio)
            if arr[i] == arr[i - raio : i + raio + 1].min()
        ]

    def _topos_swing(self, series: pd.Series, raio: int) -> list[int]:
        arr = series.to_numpy()
        return [
            i for i in range(raio, len(arr) - raio)
            if arr[i] == arr[i - raio : i + raio + 1].max()
        ]

    def _zona_fibonacci(
        self, df: pd.DataFrame, indice_recuo: int, tendencia: str
    ) -> tuple[float, float] | None:
        mapa = self._mapa_fibonacci(df, indice_recuo, tendencia)
        if mapa is None:
            return None
        return float(mapa["zona_inf"]), float(mapa["zona_sup"])

    def _perna_por_extremos(
        self, impulso: pd.DataFrame, tendencia: str
    ) -> tuple[int, int] | None:
        """Maior perna bruta da janela, sem exigir pivô confirmado."""
        if tendencia == "alta":
            pos_extremo = int(np.argmax(impulso["High"].to_numpy()))
            if pos_extremo == 0:
                return None
            pos_origem = int(np.argmin(impulso.iloc[:pos_extremo]["Low"].to_numpy()))
        else:
            pos_extremo = int(np.argmin(impulso["Low"].to_numpy()))
            if pos_extremo == 0:
                return None
            pos_origem = int(np.argmax(impulso.iloc[:pos_extremo]["High"].to_numpy()))
        return pos_origem, pos_extremo

    def _perna_por_pivo(
        self, impulso: pd.DataFrame, tendencia: str,
        amplitude_minima: float, raio: int,
    ) -> tuple[tuple[int, int] | None, bool]:
        """Perna entre pivô confirmado e o extremo da perna em andamento.

        A origem precisa ser um fundo/topo swing confirmado. O extremo, porém,
        acompanha a perna até o último candle fechado; exigir extremo
        confirmado fazia a Fibo parar antes do preço.

        Devolve (par, houve_pivo). Par None com houve_pivo True significa que
        existe estrutura na janela mas nenhuma perna passou nos mínimos — aí o
        chamador não deve cair para o critério frouxo.
        """
        fundos = self._fundos_swing(impulso["Low"], raio)
        topos = self._topos_swing(impulso["High"], raio)
        houve_pivo = bool(fundos or topos)
        origens = fundos if tendencia == "alta" else topos
        for pos_origem in reversed(origens):
            depois = impulso.iloc[pos_origem + 1 :]
            if depois.empty:
                continue
            if tendencia == "alta":
                pos_extremo = pos_origem + 1 + int(np.argmax(depois["High"].to_numpy()))
                amplitude = (
                    float(impulso.iloc[pos_extremo]["High"])
                    - float(impulso.iloc[pos_origem]["Low"])
                )
            else:
                pos_extremo = pos_origem + 1 + int(np.argmin(depois["Low"].to_numpy()))
                amplitude = (
                    float(impulso.iloc[pos_origem]["High"])
                    - float(impulso.iloc[pos_extremo]["Low"])
                )
            if amplitude >= amplitude_minima and (pos_extremo - pos_origem) >= 2 * raio + 1:
                return (pos_origem, pos_extremo), houve_pivo
        return None, houve_pivo

    def _mapa_fibonacci(
        self, df: pd.DataFrame, indice_recuo: int, tendencia: str,
        *, exigir_pivo_confirmado: bool = False,
    ) -> dict | None:
        """Mapa de Fibonacci da janela de pullback.

        `exigir_pivo_confirmado` escolhe a regra da perna: False mantém o
        critério das estratégias já avaliadas em backtest (extremos brutos da
        janela); True exige origem em pivô swing e não inventa perna quando há
        estrutura sem candidata válida.
        """
        c = self.config
        if tendencia not in {"alta", "baixa"}:
            return None
        inicio = max(0, indice_recuo - c.pullback_janela)
        impulso = df.iloc[inicio:indice_recuo]
        if len(impulso) < 10:
            return None
        atr = float(df.iloc[indice_recuo]["ATR"])
        if atr <= 0:
            return None

        par: tuple[int, int] | None = None
        if exigir_pivo_confirmado:
            par, houve_pivo = self._perna_por_pivo(
                impulso, tendencia,
                c.pullback_amplitude_min_atr * atr, c.pullback_pivo_raio,
            )
            if par is None and houve_pivo:
                return None
        if par is None:
            par = self._perna_por_extremos(impulso, tendencia)
        if par is None:
            return None

        pos_origem, pos_extremo = par
        if tendencia == "alta":
            origem = float(impulso.iloc[pos_origem]["Low"])
            extremo = float(impulso.iloc[pos_extremo]["High"])
            amplitude = extremo - origem
            preco_nivel = lambda nivel: extremo - amplitude * nivel
        else:
            origem = float(impulso.iloc[pos_origem]["High"])
            extremo = float(impulso.iloc[pos_extremo]["Low"])
            amplitude = origem - extremo
            preco_nivel = lambda nivel: extremo + amplitude * nivel

        if amplitude <= 0 or amplitude < c.pullback_amplitude_min_atr * atr:
            return None
        return self._finalizar_mapa_fibonacci(
            impulso, pos_origem, pos_extremo, tendencia, origem, extremo,
            amplitude, atr, preco_nivel,
        )

    def _mapa_fibonacci_visual(
        self, df: pd.DataFrame, indice_recuo: int, tendencia: str
    ) -> dict | None:
        """Fibonacci do gráfico: exige perna estrutural confirmada."""
        return self._mapa_fibonacci(
            df, indice_recuo, tendencia, exigir_pivo_confirmado=True
        )

    def _finalizar_mapa_fibonacci(
        self, impulso: pd.DataFrame, pos_origem: int, pos_extremo: int,
        tendencia: str, origem: float, extremo: float, amplitude: float,
        atr: float, preco_nivel,
    ) -> dict:
        c = self.config
        niveis = [
            {
                "nivel": nivel,
                "preco": float(preco_nivel(nivel)),
                "papel": (
                    "zona" if nivel in (c.pullback_fib_min, 0.5, c.pullback_fib_max)
                    else "extremo" if nivel in (0.0, 1.0)
                    else "secundario"
                ),
            }
            for nivel in (0.0, 0.236, c.pullback_fib_min, 0.5, c.pullback_fib_max, 0.786, 1.0)
        ]
        zona_a = float(preco_nivel(c.pullback_fib_min))
        zona_b = float(preco_nivel(c.pullback_fib_max))
        return {
            "tendencia": tendencia,
            "direcao": "call" if tendencia == "alta" else "put",
            "origem": origem,
            "extremo": extremo,
            "amplitude": amplitude,
            "amplitude_atr": round(amplitude / atr, 2),
            "inicio": impulso.index[pos_origem],
            "fim": impulso.index[pos_extremo],
            "zona_inf": min(zona_a, zona_b),
            "zona_sup": max(zona_a, zona_b),
            "fib786": float(preco_nivel(0.786)),
            "niveis": niveis,
        }

    def mapa_fibonacci_atual(self, df: pd.DataFrame, indice: int | None = None) -> dict | None:
        """Estado visual da retração atual: distância, S/R e confirmação."""
        if df.empty:
            return None
        indice = len(df) - 1 if indice is None else indice
        if indice < 10 or indice >= len(df):
            return None
        vela = df.iloc[indice]
        tendencia = str(vela.get("TendenciaMacro", "lateral"))
        mapa = self._mapa_fibonacci_visual(df, indice, tendencia)
        if mapa is None:
            return None
        try:
            abertura = float(vela["Open"])
            maxima = float(vela["High"])
            minima = float(vela["Low"])
            fechamento = float(vela["Close"])
            atr = float(vela["ATR"])
        except (KeyError, TypeError, ValueError):
            return None
        tolerancia = self.config.pullback_tolerancia_atr * atr
        zona_inf, zona_sup = mapa["zona_inf"], mapa["zona_sup"]
        tocou_zona = minima <= zona_sup + tolerancia and maxima >= zona_inf - tolerancia
        suportes, resistencias = self._pivos(df, indice)
        if mapa["direcao"] == "call":
            nivel_sr = min(suportes, key=lambda p: abs(p - minima), default=None)
            sr_ok = nivel_sr is not None and abs(nivel_sr - minima) <= tolerancia
            rejeitou = fechamento > abertura
            retracao_profunda = fechamento < zona_inf - tolerancia
            invalidada = fechamento < mapa["fib786"] - tolerancia
        else:
            nivel_sr = min(resistencias, key=lambda p: abs(p - maxima), default=None)
            sr_ok = nivel_sr is not None and abs(nivel_sr - maxima) <= tolerancia
            rejeitou = fechamento < abertura
            retracao_profunda = fechamento > zona_sup + tolerancia
            invalidada = fechamento > mapa["fib786"] + tolerancia
        distancia = max(zona_inf - fechamento, fechamento - zona_sup, 0.0)
        confirmado = bool(tocou_zona and sr_ok and rejeitou)
        if confirmado:
            estado = "CONFIRMADA — FIBO + S/R + REJEIÇÃO"
        elif tocou_zona and not sr_ok:
            estado = "NA ZONA — SEM S/R"
        elif tocou_zona:
            estado = "NA ZONA — ESPERAR REJEIÇÃO"
        elif invalidada:
            estado = "IMPULSO INVALIDADO"
        elif retracao_profunda:
            estado = "RETRAÇÃO PROFUNDA — ESPERAR"
        else:
            estado = "AGUARDAR RETRAÇÃO"
        return {
            **mapa,
            "preco_atual": fechamento,
            "tocou_zona": bool(tocou_zona),
            "sr_confluente": bool(sr_ok),
            "nivel_sr": float(nivel_sr) if sr_ok else None,
            "rejeicao": bool(rejeitou),
            "confirmado": confirmado,
            "distancia_zona_atr": round(distancia / atr, 2) if atr > 0 else None,
            "estado": estado,
        }

    def _contexto_pullback(self, df: pd.DataFrame, indice_recuo: int) -> dict | None:
        obrigatorias = {"Open", "High", "Low", "Close", "RSI", "ATR", "TendenciaMacro"}
        if indice_recuo < 10 or indice_recuo >= len(df) or not obrigatorias.issubset(df.columns):
            return None
        recuo = df.iloc[indice_recuo]
        if any(pd.isna(recuo[campo]) for campo in obrigatorias - {"TendenciaMacro"}):
            return None
        tendencia = str(recuo["TendenciaMacro"])
        if tendencia not in {"alta", "baixa"}:
            return None

        # Bloqueia quando EMA_Micro ≈ EMA_Macro (mercado lateral — EMAs convergindo)
        if self.config.bloquear_emas_proximas_atr > 0:
            _ema_m = recuo.get("EMA_Micro")
            _ema_M = recuo.get("EMA_Macro")
            _atr_r = recuo.get("ATR")
            if not any(pd.isna(x) for x in (_ema_m, _ema_M, _atr_r)) and float(_atr_r) > 0:
                if abs(float(_ema_m) - float(_ema_M)) < self.config.bloquear_emas_proximas_atr * float(_atr_r):
                    return None

        # Bloqueia quando EMA_Micro cruzou contra a tendência (mercado reverteu mas TendenciaMacro ainda não virou)
        if self.config.pullback_filtro_cruzamento_ema:
            _ema_m = recuo.get("EMA_Micro")
            _ema_M = recuo.get("EMA_Macro")
            if not any(pd.isna(x) for x in (_ema_m, _ema_M)):
                if tendencia == "alta" and float(_ema_m) < float(_ema_M):
                    return None
                if tendencia == "baixa" and float(_ema_m) > float(_ema_M):
                    return None

        inclinacao = recuo.get("InclinacaoMacro")
        if inclinacao is not None and not pd.isna(inclinacao):
            limite_slope = self.config.pullback_slope_forte_multiplo_atr * float(recuo["ATR"])
            if abs(float(inclinacao)) > limite_slope:
                return None

        tolerancia = self.config.pullback_tolerancia_atr * float(recuo["ATR"])
        zona = self._zona_fibonacci(df, indice_recuo, tendencia)
        tocou_fibo = bool(
            zona is not None
            and float(recuo["Low"]) <= zona[1] + tolerancia
            and float(recuo["High"]) >= zona[0] - tolerancia
        )
        suportes, resistencias = self._pivos(df, indice_recuo)
        if tendencia == "alta":
            nivel_sr = min(suportes, key=lambda p: abs(p - float(recuo["Low"])), default=None)
            tocou_sr = nivel_sr is not None and abs(nivel_sr - float(recuo["Low"])) <= tolerancia
            fatores = (["fibo"] if tocou_fibo else []) + (["suporte"] if tocou_sr else [])
            direcao = "call"
        else:
            nivel_sr = min(
                resistencias, key=lambda p: abs(p - float(recuo["High"])), default=None
            )
            tocou_sr = nivel_sr is not None and abs(nivel_sr - float(recuo["High"])) <= tolerancia
            fatores = (["fibo"] if tocou_fibo else []) + (["resistencia"] if tocou_sr else [])
            direcao = "put"
        if not fatores:
            return None
        return {
            "direcao": direcao,
            "tendencia": tendencia,
            "fatores": fatores,
            "zona_fib": zona,
            "nivel_sr": nivel_sr if tocou_sr else None,
        }

    def _avaliar_pullback_indicadores(
        self, ativo: str, df: pd.DataFrame, indice_confirmacao: int
    ) -> Decisao | None:
        if indice_confirmacao < 11 or indice_confirmacao >= len(df):
            return None
        contexto = self._contexto_pullback(df, indice_confirmacao - 1)
        if contexto is None:
            return None
        if not self._atr_regime_valido(df, indice_confirmacao - 1):
            return None
        if not self._atr_regime_valido(df, indice_confirmacao):
            return None
        recuo = df.iloc[indice_confirmacao - 1]
        confirmacao = df.iloc[indice_confirmacao]
        if str(confirmacao.get("TendenciaMacro", "")) != contexto["tendencia"]:
            return None
        if pd.isna(confirmacao.get("RSI")) or pd.isna(recuo.get("RSI")):
            return None
        rsi = float(confirmacao["RSI"])
        if not self.config.pullback_rsi_min <= rsi <= self.config.pullback_rsi_max:
            return None

        # §9: filtro RSI no candle de recuo (deve estar em zona extrema)
        if self.config.pullback_recuo_rsi_filtro and not pd.isna(recuo.get("RSI")):
            rsi_recuo = float(recuo["RSI"])
            if contexto["direcao"] == "call" and rsi_recuo >= self.config.rsi_sobrevendido:
                return None
            if contexto["direcao"] == "put" and rsi_recuo <= self.config.rsi_sobrecomprado:
                return None

        if contexto["direcao"] == "call":
            confirmou = (
                confirmacao["Close"] > confirmacao["Open"]
                and confirmacao["Close"] > recuo["Close"]
                and confirmacao["RSI"] > recuo["RSI"]
            )
        else:
            confirmou = (
                confirmacao["Close"] < confirmacao["Open"]
                and confirmacao["Close"] < recuo["Close"]
                and confirmacao["RSI"] < recuo["RSI"]
            )
        if not confirmou:
            return None

        atr_conf = float(confirmacao["ATR"])
        if self.config.pullback_alvo_min_atr > 0 and atr_conf > 0:
            janela_inicio = max(0, indice_confirmacao - self.config.pullback_janela)
            trecho = df.iloc[janela_inicio:indice_confirmacao]
            preco_conf = float(confirmacao["Close"])
            if contexto["direcao"] == "call":
                alvo_movimento = float(trecho["High"].max())
                espaco_ate_alvo = alvo_movimento - preco_conf
            else:
                alvo_movimento = float(trecho["Low"].min())
                espaco_ate_alvo = preco_conf - alvo_movimento
            if espaco_ate_alvo < self.config.pullback_alvo_min_atr * atr_conf:
                return None

        # §9: filtro de corpo mínimo no candle de confirmação
        if self.config.pullback_confirmacao_corpo_atr > 0:
            corpo_conf = abs(float(confirmacao["Close"]) - float(confirmacao["Open"]))
            if corpo_conf < self.config.pullback_confirmacao_corpo_atr * atr_conf:
                return None

        zona = contexto["zona_fib"]
        setup = "pullback_confluencia" if len(contexto["fatores"]) >= 2 else "pullback"
        if setup == "pullback" and not self.config.pullback_ativo:
            return None
        if setup == "pullback_confluencia" and not self.config.pullback_confluencia_ativo:
            return None
        return Decisao(
            ativo=ativo,
            direcao=contexto["direcao"],
            preco=float(confirmacao["Close"]),
            candle_hora=pd.Timestamp(df.index[indice_confirmacao]),
            motivo="pullback_tendencia_m5",
            detalhes={
                "setup": setup,
                "fatores": contexto["fatores"],
                "tendencia": contexto["tendencia"],
                "rsi_recuo": float(recuo["RSI"]),
                "rsi_confirmacao": rsi,
                "atr": atr_conf,
                "fib_min": zona[0] if zona else None,
                "fib_max": zona[1] if zona else None,
                "nivel_sr": contexto["nivel_sr"],
            },
        )

    def _avaliar_breakout_reteste(
        self, ativo: str, df: pd.DataFrame, indice_confirmacao: int
    ) -> Decisao | None:
        if not self.config.breakout_reteste_ativo:
            return None
        if indice_confirmacao < max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3:
            return None
        if indice_confirmacao >= len(df):
            return None
        romp = df.iloc[indice_confirmacao - 2]
        reteste = df.iloc[indice_confirmacao - 1]
        conf = df.iloc[indice_confirmacao]
        obrigatorias = ("Open", "High", "Low", "Close", "ATR", "TendenciaMacro")
        if any(pd.isna(v.get(campo)) for v in (romp, reteste, conf) for campo in obrigatorias):
            return None
        if not self._atr_regime_valido(df, indice_confirmacao):
            return None

        atr = float(conf["ATR"])
        if atr <= 0:
            return None
        tolerancia = self.config.pullback_tolerancia_atr * atr
        suportes, resistencias = self._pivos(df, indice_confirmacao - 2)
        tendencia = str(conf.get("TendenciaMacro", "lateral"))

        if tendencia == "alta" and resistencias:
            nivel = min(resistencias, key=lambda p: abs(p - float(romp["Close"])))
            rompeu = float(romp["Close"]) > nivel + tolerancia
            retestou = float(reteste["Low"]) <= nivel + tolerancia and float(reteste["Close"]) >= nivel - tolerancia
            confirmou = float(conf["Close"]) > float(conf["Open"]) and float(conf["Close"]) > float(reteste["High"])
            if rompeu and retestou and confirmou:
                return Decisao(
                    ativo=ativo,
                    direcao="call",
                    preco=float(conf["Close"]),
                    candle_hora=pd.Timestamp(df.index[indice_confirmacao]),
                    motivo="breakout_reteste_m5",
                    detalhes={
                        "setup": "breakout_reteste",
                        "nivel_sr": round(nivel, 6),
                        "tipo_sr": "resistencia_rompida",
                        "tendencia_macro": tendencia,
                        "atr": round(atr, 6),
                        "razao": [
                            f"rompeu resistencia {nivel:.5f}",
                            "voltou no reteste e segurou acima da zona",
                            "confirmou retomada com candle de alta",
                        ],
                    },
                )

        if tendencia == "baixa" and suportes:
            nivel = min(suportes, key=lambda p: abs(p - float(romp["Close"])))
            rompeu = float(romp["Close"]) < nivel - tolerancia
            retestou = float(reteste["High"]) >= nivel - tolerancia and float(reteste["Close"]) <= nivel + tolerancia
            confirmou = float(conf["Close"]) < float(conf["Open"]) and float(conf["Close"]) < float(reteste["Low"])
            if rompeu and retestou and confirmou:
                return Decisao(
                    ativo=ativo,
                    direcao="put",
                    preco=float(conf["Close"]),
                    candle_hora=pd.Timestamp(df.index[indice_confirmacao]),
                    motivo="breakout_reteste_m5",
                    detalhes={
                        "setup": "breakout_reteste",
                        "nivel_sr": round(nivel, 6),
                        "tipo_sr": "suporte_rompido",
                        "tendencia_macro": tendencia,
                        "atr": round(atr, 6),
                        "razao": [
                            f"rompeu suporte {nivel:.5f}",
                            "voltou no reteste e rejeitou a zona",
                            "confirmou continuacao com candle de baixa",
                        ],
                    },
                )
        return None

    def _avaliar_pin_bar(
        self, ativo: str, df: pd.DataFrame, indice_confirmacao: int
    ) -> Decisao | None:
        if indice_confirmacao < max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 1:
            return None
        if indice_confirmacao >= len(df):
            return None

        pin = df.iloc[indice_confirmacao - 1]
        conf = df.iloc[indice_confirmacao]

        for campo in ("Open", "High", "Low", "Close", "ATR", "EMA_Micro", "EMA_Macro"):
            if pd.isna(pin.get(campo)) or pd.isna(conf.get(campo)):
                return None

        total = float(pin["High"]) - float(pin["Low"])
        if total <= 0:
            return None
        body = abs(float(pin["Close"]) - float(pin["Open"]))
        if body / total > 0.35:
            return None

        sombra_inf = float(min(pin["Open"], pin["Close"])) - float(pin["Low"])
        sombra_sup = float(pin["High"]) - float(max(pin["Open"], pin["Close"]))
        ema_alta = float(pin["EMA_Micro"]) > float(pin["EMA_Macro"])

        ponto_wick_bull = float(pin["Low"])
        ponto_wick_bear = float(pin["High"])

        corpo_min = max(body, 0.0001)
        eh_bull_pin = (
            sombra_inf >= 2.0 * corpo_min
            and sombra_inf > sombra_sup
            and float(pin["Close"]) >= float(pin["Low"]) + 0.6 * total
        )
        eh_bear_pin = (
            sombra_sup >= 2.0 * corpo_min
            and sombra_sup > sombra_inf
            and float(pin["Close"]) <= float(pin["Low"]) + 0.4 * total
        )

        if not eh_bull_pin and not eh_bear_pin:
            return None

        if eh_bull_pin and not ema_alta:
            return None
        if eh_bear_pin and ema_alta:
            return None

        atr = float(pin["ATR"])
        tolerancia = max(self.config.pullback_tolerancia_atr * 2, 0.5) * atr
        suportes, resistencias = self._pivos(df, indice_confirmacao - 1)

        if eh_bull_pin:
            tocou = any(abs(s - ponto_wick_bull) <= tolerancia for s in suportes)
            if not tocou:
                return None
            direcao = "call"
        else:
            tocou = any(abs(r - ponto_wick_bear) <= tolerancia for r in resistencias)
            if not tocou:
                return None
            direcao = "put"

        if not self._atr_regime_valido(df, max(0, indice_confirmacao - 2)):
            return None

        if direcao == "call":
            confirmou = float(conf["Close"]) > float(conf["Open"])
        else:
            confirmou = float(conf["Close"]) < float(conf["Open"])
        if not confirmou:
            return None

        _tipo = "suporte" if eh_bull_pin else "resistência"
        _wick = ponto_wick_bull if eh_bull_pin else ponto_wick_bear
        _sombra_pct = sombra_inf / total * 100 if eh_bull_pin else sombra_sup / total * 100
        return Decisao(
            ativo=ativo,
            direcao=direcao,
            preco=float(conf["Close"]),
            candle_hora=pd.Timestamp(df.index[indice_confirmacao]),
            motivo="pin_bar_sr_m5",
            detalhes={
                "setup": "pin_bar_sr",
                "sombra_inf": round(sombra_inf, 6),
                "sombra_sup": round(sombra_sup, 6),
                "corpo": round(body, 6),
                "atr": round(atr, 6),
                "tendencia_ema": "alta" if ema_alta else "baixa",
                "razao": [
                    f"Pin Bar detectado no candle anterior (sombra {_sombra_pct:.0f}% do candle)",
                    f"Ponta da mecha ({_wick:.5f}) tocou {_tipo} → rejeição forte",
                    f"Corpo pequeno vs sombra grande = indecisão resolvida",
                    f"Candle de confirmação fechou na direção esperada ({direcao.upper()})",
                    f"EMA indica tendência de {'alta' if ema_alta else 'baixa'} — alinhado",
                ],
            },
        )

    def _avaliar_sr_rejeicao(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        if not self.config.sr_rejeicao_ativo:
            return None
        min_candles = self.config.pullback_pivo_raio * 2 + 10
        if indice < min_candles or indice >= len(df):
            return None
        vela = df.iloc[indice]
        if any(pd.isna(vela.get(c)) for c in ("Open", "High", "Low", "Close", "ATR")):
            return None
        if not self._atr_regime_valido(df, indice):
            return None

        atr = float(vela["ATR"])
        tolerancia = self.config.pullback_tolerancia_atr * atr
        v_open = float(vela["Open"])
        v_close = float(vela["Close"])
        v_high = float(vela["High"])
        v_low = float(vela["Low"])
        amplitude = v_high - v_low
        if amplitude <= 0:
            return None

        # §8: filtro de corpo mínimo (ambas direções)
        if self.config.sr_rejeicao_corpo_min_atr > 0:
            corpo = abs(v_close - v_open)
            if corpo < self.config.sr_rejeicao_corpo_min_atr * atr:
                return None

        _rsi_val = vela.get("RSI")
        _rsi_ok = _rsi_val is not None and not pd.isna(_rsi_val)

        meio = (v_high + v_low) / 2.0
        tendencia = str(vela.get("TendenciaMacro", "lateral"))
        suportes, resistencias = self._pivos(df, indice)

        if suportes and tendencia in {"alta", "lateral"}:
            sr = min(suportes, key=lambda p: abs(p - v_low))
            mecha_inf = (min(v_open, v_close) - v_low) / amplitude
            if (
                abs(sr - v_low) <= tolerancia
                and v_close > meio
                and (v_close > v_open or mecha_inf >= 0.35)
            ):
                # §8: exige RSI sobrevendido para CALL
                if (
                    self.config.sr_rejeicao_rsi_filtro
                    and _rsi_ok
                    and float(_rsi_val) >= self.config.rsi_sobrevendido
                ):
                    return None
                return Decisao(
                    ativo=ativo,
                    direcao="call",
                    preco=v_close,
                    candle_hora=pd.Timestamp(df.index[indice]),
                    motivo="sr_rejeicao_m5",
                    detalhes={
                        "setup": "sr_rejeicao",
                        "nivel_sr": round(sr, 6),
                        "tipo_sr": "suporte",
                        "mecha_inf_pct": round(mecha_inf * 100),
                        "atr": round(atr, 6),
                        "tendencia_macro": tendencia,
                        "razao": [
                            f"Suporte em {sr:.5f} identificado por pivôs recentes",
                            f"Preço tocou o suporte (Low {v_low:.5f} ≈ S/R)",
                            f"Mecha inferior de {mecha_inf*100:.0f}% do candle = rejeição do nível",
                            f"Fechou acima do meio do candle → compradores reagiram",
                            f"Tendência macro: {tendencia} — contexto favorável a CALL",
                            "Entrada CALL apostando na rejeição do suporte",
                        ],
                    },
                )

        if resistencias and tendencia in {"baixa", "lateral"}:
            sr = min(resistencias, key=lambda p: abs(p - v_high))
            mecha_sup = (v_high - max(v_open, v_close)) / amplitude
            if (
                abs(sr - v_high) <= tolerancia
                and v_close < meio
                and (v_close < v_open or mecha_sup >= 0.35)
            ):
                # §8: exige RSI sobrecomprado para PUT
                if (
                    self.config.sr_rejeicao_rsi_filtro
                    and _rsi_ok
                    and float(_rsi_val) <= self.config.rsi_sobrecomprado
                ):
                    return None
                return Decisao(
                    ativo=ativo,
                    direcao="put",
                    preco=v_close,
                    candle_hora=pd.Timestamp(df.index[indice]),
                    motivo="sr_rejeicao_m5",
                    detalhes={
                        "setup": "sr_rejeicao",
                        "nivel_sr": round(sr, 6),
                        "tipo_sr": "resistencia",
                        "mecha_sup_pct": round(mecha_sup * 100),
                        "atr": round(atr, 6),
                        "tendencia_macro": tendencia,
                        "razao": [
                            f"Resistência em {sr:.5f} identificada por pivôs recentes",
                            f"Preço tocou a resistência (High {v_high:.5f} ≈ S/R)",
                            f"Mecha superior de {mecha_sup*100:.0f}% do candle = rejeição do nível",
                            f"Fechou abaixo do meio do candle → vendedores reagiram",
                            f"Tendência macro: {tendencia} — contexto favorável a PUT",
                            "Entrada PUT apostando na rejeição da resistência",
                        ],
                    },
                )

        return None

    def _avaliar_fibo_sr_retracao(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        if not self.config.fibo_sr_retracao_ativo:
            return None
        if indice < 11 or indice >= len(df):
            return None
        if not self._atr_regime_valido(df, indice):
            return None
        contexto = self._contexto_pullback(df, indice)
        if contexto is None:
            return None
        if not ("fibo" in contexto["fatores"] and len(contexto["fatores"]) > 1):
            return None
        vela = df.iloc[indice]
        v_open = float(vela["Open"])
        v_close = float(vela["Close"])
        # Exige candle a favor da direcao (verde=call, vermelho=put) — confirmacao de
        # reversao no toque da zona, no mesmo padrao do _avaliar_sr_rejeicao. Estava
        # invertido (exigia candle CONTRA a direcao): backtest 25/08/2026 mediu
        # WR=23.8% (n=655, IC95%=[21%,27%]) com a condicao trocada.
        if contexto["direcao"] == "call" and v_close <= v_open:
            return None
        if contexto["direcao"] == "put" and v_close >= v_open:
            return None
        _fatores_str = " + ".join(contexto["fatores"])
        _dir = contexto["direcao"].upper()
        return Decisao(
            ativo=ativo,
            direcao=contexto["direcao"],
            preco=v_close,
            candle_hora=pd.Timestamp(df.index[indice]),
            motivo="fibo_sr_retracao_m5",
            detalhes={
                "setup": "fibo_sr_retracao",
                "tendencia": contexto["tendencia"],
                "fatores": contexto["fatores"],
                "nivel_sr": contexto["nivel_sr"],
                "zona_fib": list(contexto["zona_fib"]) if contexto.get("zona_fib") else None,
                "atr": round(float(vela["ATR"]), 6),
                "razao": [
                    f"Preço retraiu para zona de confluência: {_fatores_str}",
                    f"Nível Fibonacci + S/R em {contexto['nivel_sr']:.5f}",
                    f"Tendência macro: {contexto['tendencia']} — retração dentro da tendência",
                    f"Candle fechou na direção do setup ({_dir})",
                    f"Confluência de fatores aumenta a probabilidade de reversão",
                ],
            },
        )

    def _avaliar_retracao_intracandle(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """Detecta impulso + retração Fibo no candle FECHADO.

        Avalia o candle fechado (indice-1): se fez impulso ≥ 1 ATR e Close
        retornou para a zona Fibo 38.2-61.8% do impulso, sinaliza entrada
        no candle seguinte na direção do impulso original. Sem lookahead:
        todos os dados vêm do candle já concluído.

        CALL: candle fez High-Open ≥ 1 ATR (impulso de alta), Close voltou
              38-62% desse range mas ficou acima do Open (candle verde).
        PUT:  candle fez Open-Low ≥ 1 ATR (impulso de baixa), Close voltou
              38-62% desse range mas ficou abaixo do Open (candle vermelho).
        """
        c = self.config
        if not c.retracao_intracandle_ativo:
            return None
        min_candles = max(c.ema_macro_periodo, c.atr_regime_janela, c.pullback_pivo_raio * 2 + 10) + 3
        if indice < min_candles + 1 or indice >= len(df):
            return None
        # Avalia candle FECHADO (indice-1), entrada no candle atual (indice)
        vela = df.iloc[indice - 1]
        if any(pd.isna(vela.get(col)) for col in ("Open", "High", "Low", "Close", "ATR", "TendenciaMacro")):
            return None
        if not self._atr_regime_valido(df, indice - 1):
            return None

        v_open = float(vela["Open"])
        v_close = float(vela["Close"])
        v_high = float(vela["High"])
        v_low = float(vela["Low"])
        atr = float(vela["ATR"])
        tendencia = str(vela.get("TendenciaMacro", "lateral"))

        impulso_alta = v_high - v_open
        impulso_baixa = v_open - v_low
        tolerancia = c.pullback_tolerancia_atr * atr

        suportes, resistencias = self._pivos(df, indice - 1)

        # CALL: impulso de alta no candle fechado, Close retornou para zona Fibo
        if impulso_alta >= c.retracao_impulso_min_atr * atr and tendencia in ("alta", "lateral"):
            retracao = v_high - v_close
            fib_ratio = retracao / impulso_alta if impulso_alta > 0 else 0
            if c.retracao_fib_min <= fib_ratio <= c.retracao_fib_max:
                if v_close > v_open:
                    if c.retracao_exigir_sr:
                        sr = min(suportes, key=lambda p: abs(p - v_low), default=None)
                        if sr is None or abs(sr - v_low) > tolerancia:
                            return None
                    else:
                        sr = None
                    vela_atual = df.iloc[indice]
                    _sr_txt = f" com suporte em {sr:.5f}" if sr else ""
                    return Decisao(
                        ativo=ativo,
                        direcao="call",
                        preco=float(vela_atual["Close"]),
                        candle_hora=pd.Timestamp(df.index[indice]),
                        motivo="retracao_intracandle_m5",
                        detalhes={
                            "setup": "retracao_intracandle",
                            "impulso": round(impulso_alta, 6),
                            "fib_ratio": round(fib_ratio, 3),
                            "nivel_sr": round(sr, 6) if sr else None,
                            "atr": round(atr, 6),
                            "tendencia_macro": tendencia,
                            "razao": [
                                f"Candle anterior fez impulso de ALTA de {impulso_alta:.5f} (≥ 1 ATR)",
                                f"Preço retornou {fib_ratio*100:.1f}% do impulso (zona Fibo 38-62%)",
                                f"Fechou acima da abertura (candle verde) = força mantida",
                                f"Retração saudável{_sr_txt}",
                                "Entrada CALL: continuação do impulso de alta",
                            ],
                        },
                    )

        # PUT: impulso de baixa no candle fechado, Close retornou para zona Fibo
        if impulso_baixa >= c.retracao_impulso_min_atr * atr and tendencia in ("baixa", "lateral"):
            retracao = v_close - v_low
            fib_ratio = retracao / impulso_baixa if impulso_baixa > 0 else 0
            if c.retracao_fib_min <= fib_ratio <= c.retracao_fib_max:
                if v_close < v_open:
                    if c.retracao_exigir_sr:
                        sr = min(resistencias, key=lambda p: abs(p - v_high), default=None)
                        if sr is None or abs(sr - v_high) > tolerancia:
                            return None
                    else:
                        sr = None
                    vela_atual = df.iloc[indice]
                    _sr_txt = f" com resistência em {sr:.5f}" if sr else ""
                    return Decisao(
                        ativo=ativo,
                        direcao="put",
                        preco=float(vela_atual["Close"]),
                        candle_hora=pd.Timestamp(df.index[indice]),
                        motivo="retracao_intracandle_m5",
                        detalhes={
                            "setup": "retracao_intracandle",
                            "impulso": round(impulso_baixa, 6),
                            "fib_ratio": round(fib_ratio, 3),
                            "nivel_sr": round(sr, 6) if sr else None,
                            "atr": round(atr, 6),
                            "tendencia_macro": tendencia,
                            "razao": [
                                f"Candle anterior fez impulso de BAIXA de {impulso_baixa:.5f} (≥ 1 ATR)",
                                f"Preço retornou {fib_ratio*100:.1f}% do impulso (zona Fibo 38-62%)",
                                f"Fechou abaixo da abertura (candle vermelho) = pressão mantida",
                                f"Retração saudável{_sr_txt}",
                                "Entrada PUT: continuação do impulso de baixa",
                            ],
                        },
                    )

        return None

    def _avaliar_macd(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        if not self.config.macd_crossover_ativo:
            return None
        c = self.config
        min_candles = c.macd_slow + c.macd_signal + 5
        if indice < min_candles or indice >= len(df):
            return None
        vela = df.iloc[indice]
        vela_ant = df.iloc[indice - 1]
        for col in ("MACD", "MACD_Signal", "ATR"):
            if pd.isna(vela.get(col)) or pd.isna(vela_ant.get(col)):
                return None
        if not self._atr_regime_valido(df, indice):
            return None

        macd_cur = float(vela["MACD"])
        sig_cur = float(vela["MACD_Signal"])
        macd_prev = float(vela_ant["MACD"])
        sig_prev = float(vela_ant["MACD_Signal"])

        if macd_prev < sig_prev and macd_cur > sig_cur and macd_cur > 0:
            return Decisao(
                ativo=ativo,
                direcao="call",
                preco=float(vela["Close"]),
                candle_hora=pd.Timestamp(df.index[indice]),
                motivo="macd_crossover_m5",
                detalhes={
                    "setup": "macd_crossover",
                    "macd": round(macd_cur, 6),
                    "macd_signal": round(sig_cur, 6),
                    "atr": round(float(vela["ATR"]), 6),
                },
            )

        if macd_prev > sig_prev and macd_cur < sig_cur and macd_cur < 0:
            return Decisao(
                ativo=ativo,
                direcao="put",
                preco=float(vela["Close"]),
                candle_hora=pd.Timestamp(df.index[indice]),
                motivo="macd_crossover_m5",
                detalhes={
                    "setup": "macd_crossover",
                    "macd": round(macd_cur, 6),
                    "macd_signal": round(sig_cur, 6),
                    "atr": round(float(vela["ATR"]), 6),
                },
            )

        return None

    def _avaliar_macd_time(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """macd_crossover restrito a 00h-06h UTC — janela asiática onde o setup historicamente performa."""
        hora = pd.Timestamp(df.index[indice]).hour
        if hora > 6:
            return None
        d = self._avaliar_macd(ativo, df, indice)
        if d is None:
            return None
        return _dc_replace(d, motivo="macd_crossover_time_m5",
                           detalhes={**d.detalhes, "setup": "macd_crossover_time"})

    def _avaliar_macd_tendencia(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """macd_crossover só quando TendenciaMacro confirma a direção do cruzamento."""
        d = self._avaliar_macd(ativo, df, indice)
        if d is None:
            return None
        tendencia = str(df.iloc[indice].get("TendenciaMacro", "lateral"))
        if d.direcao == "call" and tendencia != "alta":
            return None
        if d.direcao == "put" and tendencia != "baixa":
            return None
        return _dc_replace(d, motivo="macd_crossover_tendencia_m5",
                           detalhes={**d.detalhes, "setup": "macd_crossover_tendencia"})

    def _avaliar_engulfing_sr(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        c = self.config
        if indice < 2 or indice >= len(df):
            return None
        if not self._atr_regime_valido(df, indice):
            return None
        mae = df.iloc[indice - 1]
        eng = df.iloc[indice]
        obrigatorias = ("Open", "High", "Low", "Close", "ATR", "TendenciaMacro")
        if any(pd.isna(mae.get(col)) or pd.isna(eng.get(col)) for col in obrigatorias):
            return None

        atr = float(eng["ATR"])
        tolerancia = c.pullback_tolerancia_atr * atr
        tendencia = str(eng.get("TendenciaMacro", "lateral"))

        inclinacao = eng.get("InclinacaoMacro")
        if inclinacao is not None and not pd.isna(inclinacao):
            if abs(float(inclinacao)) > c.pullback_slope_forte_multiplo_atr * atr:
                return None

        mae_o, mae_c = float(mae["Open"]), float(mae["Close"])
        eng_o, eng_c = float(eng["Open"]), float(eng["Close"])
        eng_low, eng_high = float(eng["Low"]), float(eng["High"])
        suportes, resistencias = self._pivos(df, indice)

        if (
            mae_c < mae_o and eng_c > eng_o
            and eng_o <= mae_c and eng_c >= mae_o
            and tendencia in {"alta", "lateral"}
            and suportes
        ):
            sr = min(suportes, key=lambda p: abs(p - eng_low))
            if abs(sr - eng_low) <= tolerancia:
                return Decisao(
                    ativo=ativo, direcao="call", preco=eng_c,
                    candle_hora=pd.Timestamp(df.index[indice]),
                    motivo="engulfing_sr_m5",
                    detalhes={"setup": "engulfing_sr", "nivel_sr": round(sr, 6),
                              "tipo_sr": "suporte", "tendencia_macro": tendencia,
                              "atr": round(atr, 6)},
                )

        if (
            mae_c > mae_o and eng_c < eng_o
            and eng_o >= mae_c and eng_c <= mae_o
            and tendencia in {"baixa", "lateral"}
            and resistencias
        ):
            sr = min(resistencias, key=lambda p: abs(p - eng_high))
            if abs(sr - eng_high) <= tolerancia:
                return Decisao(
                    ativo=ativo, direcao="put", preco=eng_c,
                    candle_hora=pd.Timestamp(df.index[indice]),
                    motivo="engulfing_sr_m5",
                    detalhes={"setup": "engulfing_sr", "nivel_sr": round(sr, 6),
                              "tipo_sr": "resistencia", "tendencia_macro": tendencia,
                              "atr": round(atr, 6)},
                )
        return None

    def _avaliar_divergencia_rsi(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        c = self.config
        raio = c.divergencia_rsi_janela_pivos
        if indice < raio * 2 + 5 or indice >= len(df):
            return None
        if not self._atr_regime_valido(df, indice):
            return None

        conf = df.iloc[indice]
        if any(pd.isna(conf.get(col)) for col in ("Open", "Close", "RSI", "ATR")):
            return None

        lookback = min(40, indice)
        janela = df.iloc[indice - lookback : indice]
        conf_close = float(conf["Close"])
        conf_open = float(conf["Open"])
        conf_rsi = float(conf["RSI"])

        fundos = self._fundos_swing(janela["Low"], raio)
        if len(fundos) >= 2:
            f1, f2 = fundos[-2], fundos[-1]
            if f2 - f1 >= 3:
                rsi_f1 = float(janela.iloc[f1]["RSI"])
                rsi_f2 = float(janela.iloc[f2]["RSI"])
                if (
                    not pd.isna(rsi_f1) and not pd.isna(rsi_f2)
                    and float(janela.iloc[f2]["Low"]) < float(janela.iloc[f1]["Low"])
                    and rsi_f2 > rsi_f1 and rsi_f2 < 40
                    and conf_close > conf_open and conf_rsi > rsi_f2
                ):
                    return Decisao(
                        ativo=ativo, direcao="call", preco=conf_close,
                        candle_hora=pd.Timestamp(df.index[indice]),
                        motivo="divergencia_rsi_m5",
                        detalhes={"setup": "divergencia_rsi", "tipo": "bullish",
                                  "rsi_pivo1": round(rsi_f1, 2), "rsi_pivo2": round(rsi_f2, 2),
                                  "preco_pivo1": round(float(janela.iloc[f1]["Low"]), 6),
                                  "preco_pivo2": round(float(janela.iloc[f2]["Low"]), 6),
                                  "atr": round(float(conf["ATR"]), 6)},
                    )

        topos = self._topos_swing(janela["High"], raio)
        if len(topos) >= 2:
            t1, t2 = topos[-2], topos[-1]
            if t2 - t1 >= 3:
                rsi_t1 = float(janela.iloc[t1]["RSI"])
                rsi_t2 = float(janela.iloc[t2]["RSI"])
                if (
                    not pd.isna(rsi_t1) and not pd.isna(rsi_t2)
                    and float(janela.iloc[t2]["High"]) > float(janela.iloc[t1]["High"])
                    and rsi_t2 < rsi_t1 and rsi_t2 > 60
                    and conf_close < conf_open and conf_rsi < rsi_t2
                ):
                    return Decisao(
                        ativo=ativo, direcao="put", preco=conf_close,
                        candle_hora=pd.Timestamp(df.index[indice]),
                        motivo="divergencia_rsi_m5",
                        detalhes={"setup": "divergencia_rsi", "tipo": "bearish",
                                  "rsi_pivo1": round(rsi_t1, 2), "rsi_pivo2": round(rsi_t2, 2),
                                  "preco_pivo1": round(float(janela.iloc[t1]["High"]), 6),
                                  "preco_pivo2": round(float(janela.iloc[t2]["High"]), 6),
                                  "atr": round(float(conf["ATR"]), 6)},
                    )
        return None

    def _avaliar_divergencia_rsi_time(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """divergencia_rsi restrita a 11h-13h UTC — janela London afternoon que performa bem."""
        hora = pd.Timestamp(df.index[indice]).hour
        if not (11 <= hora <= 13):
            return None
        d = self._avaliar_divergencia_rsi(ativo, df, indice)
        if d is None:
            return None
        return _dc_replace(d, motivo="divergencia_rsi_time_m5",
                           detalhes={**d.detalhes, "setup": "divergencia_rsi_time"})

    def _avaliar_divergencia_rsi_tendencia(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """divergencia_rsi com RSI extremo (< 25 / > 75) — zona de sobrecompra/venda real."""
        conf = df.iloc[indice] if indice < len(df) else None
        if conf is None or pd.isna(conf.get("RSI")):
            return None
        rsi = float(conf["RSI"])
        # Pré-filtra por extremo antes de rodar toda a lógica de divergência
        if not (rsi < 25 or rsi > 75):
            return None
        d = self._avaliar_divergencia_rsi(ativo, df, indice)
        if d is None:
            return None
        return _dc_replace(d, motivo="divergencia_rsi_tendencia_m5",
                           detalhes={**d.detalhes, "setup": "divergencia_rsi_tendencia"})

    def _avaliar_bollinger_squeeze(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        c = self.config
        janela = c.bollinger_squeeze_percentil_janela
        if indice < janela + 5 or indice >= len(df):
            return None
        if not self._atr_regime_valido(df, indice):
            return None

        vela = df.iloc[indice]
        if any(pd.isna(vela.get(col)) for col in ("BandaSup", "BandaInf", "Close", "Open", "RSI", "ATR")):
            return None

        trecho = df.iloc[max(0, indice - janela) : indice + 1]
        larguras = (trecho["BandaSup"] - trecho["BandaInf"]).to_numpy()
        if len(larguras) < 2:
            return None

        largura_atual = float(larguras[-1])
        percentil_20 = float(np.percentile(larguras[:-1], 20))
        if largura_atual >= percentil_20:
            return None

        atr = float(vela["ATR"])
        rsi = float(vela["RSI"])
        close = float(vela["Close"])
        open_ = float(vela["Open"])
        corpo = abs(close - open_)

        if (close > float(vela["BandaSup"])
            and corpo >= c.bollinger_squeeze_min_corpo_atr * atr
            and rsi > 50):
            return Decisao(
                ativo=ativo, direcao="call", preco=close,
                candle_hora=pd.Timestamp(df.index[indice]),
                motivo="bollinger_squeeze_m5",
                detalhes={"setup": "bollinger_squeeze", "largura_bb": round(largura_atual, 6),
                          "percentil_largura": round(percentil_20, 6), "rsi": round(rsi, 2),
                          "atr": round(atr, 6)},
            )

        if (close < float(vela["BandaInf"])
            and corpo >= c.bollinger_squeeze_min_corpo_atr * atr
            and rsi < 50):
            return Decisao(
                ativo=ativo, direcao="put", preco=close,
                candle_hora=pd.Timestamp(df.index[indice]),
                motivo="bollinger_squeeze_m5",
                detalhes={"setup": "bollinger_squeeze", "largura_bb": round(largura_atual, 6),
                          "percentil_largura": round(percentil_20, 6), "rsi": round(rsi, 2),
                          "atr": round(atr, 6)},
            )
        return None

    def _avaliar_rejeicao_m1_hierarquico(
        self, ativo: str, df: pd.DataFrame, indice: int
    ) -> Decisao | None:
        """Estratégia M1 hierárquica: M15 contexto → M5 estrutura → M1 candle de rejeição.

        Scoring 0-11 pontos; entra se score >= rejeicao_m1_score_minimo (padrão 9).
        """
        if not self.config.rejeicao_m1_hierarquico_ativo:
            return None

        minimo = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 5
        if indice < minimo or indice >= len(df):
            return None

        rejeicao = df.iloc[indice]
        confirmacao = df.iloc[indice + 1] if indice + 1 < len(df) else None

        campos = ("Open", "High", "Low", "Close", "ATR", "EMA_Micro", "EMA_Macro")
        if any(pd.isna(rejeicao.get(c)) for c in campos):
            return None

        r_open  = float(rejeicao["Open"])
        r_close = float(rejeicao["Close"])
        r_high  = float(rejeicao["High"])
        r_low   = float(rejeicao["Low"])
        r_ema20 = float(rejeicao["EMA_Micro"])  # EMA20 no M1
        r_ema50 = float(rejeicao["EMA_Macro"])  # EMA50 no M1
        atr     = float(rejeicao["ATR"])

        total_candle = r_high - r_low
        if total_candle <= 0 or atr <= 0:
            return None

        corpo     = abs(r_close - r_open)
        sombra_inf = min(r_open, r_close) - r_low
        sombra_sup = r_high - max(r_open, r_close)
        corpo_min  = max(corpo, atr * 0.001)

        # Candle de rejeição CALL: martelo (sombra inf ≥ 2× corpo, fecha acima da metade)
        eh_call = (
            sombra_inf >= 2.0 * corpo_min
            and sombra_inf > sombra_sup
            and r_close >= r_low + 0.5 * total_candle
        )
        # Candle de rejeição PUT: estrela cadente (sombra sup ≥ 2× corpo, fecha abaixo da metade)
        eh_put = (
            sombra_sup >= 2.0 * corpo_min
            and sombra_sup > sombra_inf
            and r_close <= r_low + 0.5 * total_candle
        )

        if not eh_call and not eh_put:
            return None

        direcao: str = "call" if eh_call else "put"

        # Confirma: próxima vela rompe high/low do candle de rejeição
        if confirmacao is not None and not any(
            pd.isna(confirmacao.get(c)) for c in ("High", "Low", "Close")
        ):
            if direcao == "call":
                if not (float(confirmacao["High"]) > r_high):
                    return None
            else:
                if not (float(confirmacao["Low"]) < r_low):
                    return None

        # --- Scoring ---
        score = 0
        det: dict = {}

        # +2: M15 contexto favorável (bloqueia se contra)
        ctx_m15 = self._contexto_m15.get(ativo, "lateral")
        if (direcao == "call" and ctx_m15 == "alta") or (direcao == "put" and ctx_m15 == "baixa"):
            score += 2
            det["m15_ctx"] = ctx_m15
        elif ctx_m15 != "lateral":
            return None  # sinal contra o contexto M15 — bloqueado

        # +2: M5 estrutura favorável
        est_m5 = self._estrutura_m5.get(ativo, "lateral")
        if (direcao == "call" and est_m5 == "alta") or (direcao == "put" and est_m5 == "baixa"):
            score += 2
            det["m5_est"] = est_m5

        # +2: nível S/R próximo do ponto de rejeição
        suportes, resistencias = self._pivos(df, indice)
        tolerancia = self.config.pullback_tolerancia_atr * atr
        nivel_sr = None
        if direcao == "call" and suportes:
            n_sr = min(suportes, key=lambda p: abs(p - r_low))
            if abs(n_sr - r_low) <= tolerancia:
                score += 2
                nivel_sr = n_sr
                det["sr"] = round(n_sr, 6)
        elif direcao == "put" and resistencias:
            n_sr = min(resistencias, key=lambda p: abs(p - r_high))
            if abs(n_sr - r_high) <= tolerancia:
                score += 2
                nivel_sr = n_sr
                det["sr"] = round(n_sr, 6)

        # +1: pullback toca EMA20 no M1 (ponto de rejeição ≈ EMA)
        if direcao == "call" and r_low <= r_ema20 + tolerancia:
            score += 1
            det["pullback_ema"] = round(r_ema20, 6)
        elif direcao == "put" and r_high >= r_ema20 - tolerancia:
            score += 1
            det["pullback_ema"] = round(r_ema20, 6)

        # +1: EMA20 alinhada com a direção no M1
        if direcao == "call" and r_ema20 > r_ema50:
            score += 1
            det["ema_alinhada"] = True
        elif direcao == "put" and r_ema20 < r_ema50:
            score += 1
            det["ema_alinhada"] = True

        # +2: candle de rejeição confirmado (critério já satisfeito acima)
        score += 2
        det["rejeicao"] = "martelo" if direcao == "call" else "estrela_cadente"
        det["sombra_inf"] = round(sombra_inf, 6)
        det["sombra_sup"] = round(sombra_sup, 6)

        # +1: RSI não exausto (40-65 para call; 35-60 para put)
        rsi = rejeicao.get("RSI")
        if rsi is not None and not pd.isna(rsi):
            rsi_f = float(rsi)
            if direcao == "call" and 30 <= rsi_f <= 65:
                score += 1
                det["rsi"] = round(rsi_f, 1)
            elif direcao == "put" and 35 <= rsi_f <= 70:
                score += 1
                det["rsi"] = round(rsi_f, 1)

        if score < self.config.rejeicao_m1_score_minimo:
            return None

        return Decisao(
            ativo=ativo,
            direcao=direcao,
            preco=float(r_close),
            candle_hora=pd.Timestamp(df.index[indice]),
            motivo="rejeicao_m1_hierarquico",
            detalhes={
                "setup": "rejeicao_m1_hierarquico",
                "score": score,
                "score_max": 11,
                "ctx_m15": ctx_m15,
                "est_m5": est_m5,
                "nivel_sr": nivel_sr,
                "corpo": round(corpo, 6),
                "atr": round(atr, 6),
                **det,
            },
        )

    def _estrutura_candle(self, df: pd.DataFrame, indice: int, direcao: str) -> tuple[bool, dict]:
        """Filtro de vela: força, fechamento e rejeição por pavio.

        Retorna (aprovado, métricas). Não usa o candle seguinte, evitando
        lookahead no backtest. Para CALL, rejeição é pavio inferior; para PUT,
        pavio superior. A confirmação exige corpo na direção do sinal e/ou
        fechamento no extremo favorável.
        """
        if indice < 0 or indice >= len(df):
            return False, {}
        v = df.iloc[indice]
        try:
            o, h, l, c = (float(v[x]) for x in ("Open", "High", "Low", "Close"))
        except (KeyError, TypeError, ValueError):
            return False, {}
        faixa = h - l
        if faixa <= 0:
            return False, {}
        corpo = abs(c - o)
        corpo_ratio = corpo / faixa
        pavio_sup = h - max(o, c)
        pavio_inf = min(o, c) - l
        pavio = pavio_inf if direcao == "call" else pavio_sup
        fechamento_favoravel = (c - l) / faixa if direcao == "call" else (h - c) / faixa
        corpo_favoravel = (c > o) if direcao == "call" else (c < o)
        pavio_ratio = pavio / corpo if corpo > 0 else float("inf")
        evidencias = {
            "corpo_direcao": bool(corpo_favoravel),
            "vela_forte": bool(corpo_ratio >= self.config.candle_forca_min_ratio),
            "fechamento_extremo": bool(fechamento_favoravel >= 1.0 - self.config.candle_fechamento_extremo_ratio),
            "pavio_rejeicao": bool(pavio_ratio >= self.config.candle_pavio_rejeicao_min_ratio),
        }
        # Confirmação (corpo + fechamento) ou rejeição (pavio + fechamento).
        score = int(evidencias["corpo_direcao"]) + int(evidencias["vela_forte"]) + int(
            evidencias["pavio_rejeicao"] or evidencias["fechamento_extremo"]
        )
        aprovado = (
            corpo_ratio >= self.config.candle_corpo_min_ratio
            and score >= self.config.candle_filtro_score_minimo
        ) or (
            evidencias["pavio_rejeicao"]
            and evidencias["fechamento_extremo"]
        )
        return aprovado, {
            "candle_score": score,
            "candle_corpo_ratio": round(corpo_ratio, 3),
            "candle_pavio_ratio": round(pavio_ratio, 2) if np.isfinite(pavio_ratio) else 99.0,
            "candle_fechamento_favoravel": round(fechamento_favoravel, 3),
            "candle_evidencias": [k for k, ok in evidencias.items() if ok],
        }

    def _filtrar_estrutura_candle(self, sinais: list[Decisao], df: pd.DataFrame, indice: int) -> list[Decisao]:
        if not self.config.filtro_candle_estrutura_ativo:
            return sinais
        aprovados = []
        for sinal in sinais:
            ok, metricas = self._estrutura_candle(df, indice, sinal.direcao)
            if ok:
                aprovados.append(_dc_replace(sinal, detalhes={**sinal.detalhes, **metricas}))
            else:
                logger.info("[CANDLE] %s: bloqueou %s (%s)", sinal.ativo, sinal.direcao, sinal.detalhes.get("setup", sinal.motivo))
        return aprovados

    def _avaliar_ema920_pullback(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """Pullback M1/M5 na faixa EMA9-EMA20, a favor da tendência."""
        if not self.config.ema920_pullback_ativo or indice < 25 or indice >= len(df):
            return None
        close = df["Close"]
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        v = df.iloc[indice]
        atr = v.get("ATR")
        if pd.isna(atr) or float(atr) <= 0:
            return None
        e9, e20, atr = float(ema9.iloc[indice]), float(ema20.iloc[indice]), float(atr)
        faixa_min, faixa_max = min(e9, e20), max(e9, e20)
        o, h, l, c = (float(v[x]) for x in ("Open", "High", "Low", "Close"))
        toca = l <= faixa_max + 0.15 * atr and h >= faixa_min - 0.15 * atr
        if not toca:
            return None
        inclinacao9 = float(ema9.iloc[indice] - ema9.iloc[indice - 3])
        inclinacao20 = float(ema20.iloc[indice] - ema20.iloc[indice - 3])
        bullish = e9 > e20 and inclinacao9 > 0 and inclinacao20 >= 0
        bearish = e9 < e20 and inclinacao9 < 0 and inclinacao20 <= 0
        faixa = h - l
        if faixa <= 0:
            return None
        corpo = abs(c - o)
        rejeicao_call = c > o and c > faixa_max and (c - l) / faixa >= 0.55
        rejeicao_put = c < o and c < faixa_min and (h - c) / faixa >= 0.55
        direcao = "call" if bullish and rejeicao_call else "put" if bearish and rejeicao_put else None
        if direcao is None:
            return None
        return Decisao(
            ativo=ativo, direcao=direcao, preco=c,
            candle_hora=pd.Timestamp(df.index[indice]), motivo="ema920_pullback",
            detalhes={"setup": "ema920_pullback", "ema9": e9, "ema20": e20,
                      "atr": atr, "toque_faixa": True,
                      "confirmacao": "fechamento_rejeicao", "corpo_ratio": round(corpo / faixa, 3)},
        )

    def _avaliar_ema921_rsi_pullback(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """Pullback na faixa EMA9/21 com RSI14 segurando a tendência."""
        if not self.config.ema921_rsi_pullback_ativo or indice < 25 or indice >= len(df):
            return None
        v = df.iloc[indice]
        atr, rsi = v.get("ATR"), v.get("RSI")
        if pd.isna(atr) or pd.isna(rsi) or float(atr) <= 0:
            return None
        close = df["Close"]
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        e9, e21 = float(ema9.iloc[indice]), float(ema21.iloc[indice])
        o, h, l, c = (float(v[x]) for x in ("Open", "High", "Low", "Close"))
        faixa_min, faixa_max = min(e9, e21), max(e9, e21)
        toca = l <= faixa_max + 0.15 * float(atr) and h >= faixa_min - 0.15 * float(atr)
        if not toca or h <= l:
            return None
        incl9 = float(ema9.iloc[indice] - ema9.iloc[indice - 3])
        incl21 = float(ema21.iloc[indice] - ema21.iloc[indice - 3])
        rsi_f = float(rsi)
        call = (
            e9 > e21 and incl9 > 0 and incl21 >= 0
            and 40 <= rsi_f <= 55 and c > o and c > faixa_max
            and (c - l) / (h - l) >= 0.55
        )
        put = (
            e9 < e21 and incl9 < 0 and incl21 <= 0
            and 45 <= rsi_f <= 60 and c < o and c < faixa_min
            and (h - c) / (h - l) >= 0.55
        )
        direcao = "call" if call else "put" if put else None
        if direcao is None:
            return None
        return Decisao(
            ativo=ativo, direcao=direcao, preco=c,
            candle_hora=pd.Timestamp(df.index[indice]), motivo="ema921_rsi_pullback",
            detalhes={
                "setup": "ema921_rsi_pullback", "ema9": e9, "ema21": e21,
                "rsi14": round(rsi_f, 1), "atr": float(atr), "toque_faixa": True,
                "confirmacao": "fechamento_rejeicao_rsi",
            },
        )

    def _avaliar_ema920_prime(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """EMA9/20 de pesquisa: primeiro reteste, impulso e espaço estrutural.

        É deliberadamente mais seletiva que a EMA9/20 normal. A variante fica
        em sombra no laboratório até que os resultados, por par, confirmem que
        remover sinais compensou a menor frequência.
        """
        cfg = self.config
        if not cfg.ema920_prime_ativo or indice < 25 or indice >= len(df):
            return None
        close = df["Close"]
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        vela = df.iloc[indice]
        atr = vela.get("ATR")
        if pd.isna(atr) or float(atr) <= 0:
            return None
        e9, e20, atr_f = float(ema9.iloc[indice]), float(ema20.iloc[indice]), float(atr)
        abertura, maxima, minima, fechamento = (float(vela[campo]) for campo in ("Open", "High", "Low", "Close"))
        faixa_min, faixa_max = min(e9, e20), max(e9, e20)
        range_vela = maxima - minima
        if range_vela <= 0:
            return None
        tocou = minima <= faixa_max + 0.15 * atr_f and maxima >= faixa_min - 0.15 * atr_f
        if not tocou:
            return None
        incl9 = float(ema9.iloc[indice] - ema9.iloc[indice - 3])
        incl20 = float(ema20.iloc[indice] - ema20.iloc[indice - 3])
        alta = e9 > e20 and incl9 > 0 and incl20 >= 0
        baixa = e9 < e20 and incl9 < 0 and incl20 <= 0
        corpo_ratio = abs(fechamento - abertura) / range_vela
        extremo = cfg.ema920_prime_fechamento_extremo_ratio
        call = (
            alta and fechamento > abertura and fechamento > faixa_max
            and corpo_ratio >= cfg.ema920_prime_corpo_min_ratio
            and (fechamento - minima) / range_vela >= 1 - extremo
        )
        put = (
            baixa and fechamento < abertura and fechamento < faixa_min
            and corpo_ratio >= cfg.ema920_prime_corpo_min_ratio
            and (maxima - fechamento) / range_vela >= 1 - extremo
        )
        direcao = "call" if call else "put" if put else None
        if direcao is None:
            return None

        # O toque atual é o reteste. Mais de um toque recente indica que o
        # preço já está serrilhando as médias, não retomando uma tendência.
        inicio_toques = max(0, indice - cfg.ema920_prime_janela_toques)
        anteriores = df.iloc[inicio_toques:indice]
        ema9_ant = ema9.iloc[inicio_toques:indice]
        ema20_ant = ema20.iloc[inicio_toques:indice]
        atr_ant = df["ATR"].iloc[inicio_toques:indice]
        toca_anterior = (
            (anteriores["Low"] <= np.maximum(ema9_ant, ema20_ant) + 0.15 * atr_ant)
            & (anteriores["High"] >= np.minimum(ema9_ant, ema20_ant) - 0.15 * atr_ant)
        )
        toques_anteriores = int(toca_anterior.fillna(False).sum())
        if toques_anteriores > cfg.ema920_prime_max_toques_anteriores:
            return None

        # Deve existir impulso mensurável antes do recuo; não aceita uma EMA
        # inclinada por ruído de poucos ticks.
        inicio_impulso = max(0, indice - cfg.ema920_prime_janela_impulso)
        antes = df.iloc[inicio_impulso:indice]
        if antes.empty:
            return None
        impulso = (
            float(antes["Close"].iloc[-1]) - float(antes["Low"].min())
            if direcao == "call"
            else float(antes["High"].max()) - float(antes["Close"].iloc[-1])
        )
        if impulso < cfg.ema920_prime_impulso_min_atr * atr_f:
            return None

        # Descarta entrada espremida contra o próximo pivô contrário.
        suportes, resistencias = self._pivos(df, indice)
        if direcao == "call":
            obstaculos = [nivel for nivel in resistencias if nivel > fechamento]
            espaco_sr = min(obstaculos) - fechamento if obstaculos else float("inf")
        else:
            obstaculos = [nivel for nivel in suportes if nivel < fechamento]
            espaco_sr = fechamento - max(obstaculos) if obstaculos else float("inf")
        if espaco_sr < cfg.ema920_prime_espaco_sr_min_atr * atr_f:
            return None

        return Decisao(
            ativo=ativo,
            direcao=direcao,
            preco=fechamento,
            candle_hora=pd.Timestamp(df.index[indice]),
            motivo="ema920_prime",
            detalhes={
                "setup": "ema920_prime",
                "ema9": e9,
                "ema20": e20,
                "atr": atr_f,
                "toque_faixa": True,
                "confirmacao": "primeiro_reteste_rejeicao_forte",
                "corpo_ratio": round(corpo_ratio, 3),
                "toques_anteriores": toques_anteriores,
                "impulso_atr": round(impulso / atr_f, 3),
                "espaco_sr_atr": round(espaco_sr / atr_f, 3) if np.isfinite(espaco_sr) else None,
            },
        )

    def _avaliar_ema921_rsi_intravela(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
        """Entrada no toque AO VIVO da faixa EMA9/21, sem confirmação de fechamento.

        A direção continua protegida por alinhamento/inclinação das médias e RSI
        de pullback. O preço precisa estar dentro da faixa (com pequena folga de
        ATR) no instante da leitura; um toque antigo seguido de afastamento não
        gera ordem tardia.
        """
        if not self.config.ema921_rsi_intravela_ativo or indice < 25 or indice >= len(df):
            return None
        v = df.iloc[indice]
        atr, rsi = v.get("ATR"), v.get("RSI")
        if pd.isna(atr) or pd.isna(rsi) or float(atr) <= 0:
            return None
        close = df["Close"]
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        e9, e21 = float(ema9.iloc[indice]), float(ema21.iloc[indice])
        atr_f, rsi_f = float(atr), float(rsi)
        o, h, l, preco = (float(v[x]) for x in ("Open", "High", "Low", "Close"))
        faixa_min, faixa_max = min(e9, e21), max(e9, e21)
        folga = float(self.config.ema921_rsi_intravela_tolerancia_atr) * atr_f
        # "tocou" exige histórico de preço na faixa e "agora_na_faixa" impede
        # entrar depois que o preço já percorreu boa parte do movimento.
        tocou = l <= faixa_max + folga and h >= faixa_min - folga
        agora_na_faixa = faixa_min - folga <= preco <= faixa_max + folga
        if not tocou or not agora_na_faixa:
            return None
        incl9 = float(ema9.iloc[indice] - ema9.iloc[indice - 3])
        incl21 = float(ema21.iloc[indice] - ema21.iloc[indice - 3])
        call = e9 > e21 and incl9 > 0 and incl21 >= 0 and 40 <= rsi_f <= 60
        put = e9 < e21 and incl9 < 0 and incl21 <= 0 and 40 <= rsi_f <= 60
        direcao = "call" if call else "put" if put else None
        if direcao is None:
            return None
        return Decisao(
            ativo=ativo, direcao=direcao, preco=preco,
            candle_hora=pd.Timestamp(df.index[indice]), motivo="ema921_rsi_intravela",
            detalhes={
                "setup": "ema921_rsi_intravela", "ema9": e9, "ema21": e21,
                "rsi14": round(rsi_f, 1), "atr": atr_f, "toque_faixa": True,
                "confirmacao": "toque_ao_vivo", "folga_atr": round(folga, 8),
                "nivel_sr": (faixa_min + faixa_max) / 2,
            },
        )

    def avaliar_nzd_trend_pullback(
        self,
        ativo: str,
        df_m5: pd.DataFrame,
        df_m15: pd.DataFrame,
        indice: int,
    ) -> Decisao | None:
        """Candidato NZDUSD em sombra: pullback M5 só com contexto M15 forte.

        A decisão é propositalmente separada das EMAs genéricas. Isso evita que
        uma melhoria experimental herde resultados ou envie ordem pelo setup
        anterior. O laboratório a registra como ``nzd_trend_pullback_v1``.
        """
        c = self.config
        if (
            not c.nzd_trend_pullback_ativo
            or ativo.upper() != "NZDUSD"
            or indice < 25
            or len(df_m15) < 25
            or indice >= len(df_m5)
        ):
            return None
        v = df_m5.iloc[indice]
        campos = ("ATR", "RSI", "ADX", "DI_Mais", "DI_Menos")
        if any(pd.isna(v.get(campo)) for campo in campos) or float(v["ATR"]) <= 0:
            return None

        close_m5 = df_m5["Close"]
        ema9_m5 = close_m5.ewm(span=9, adjust=False).mean()
        ema21_m5 = close_m5.ewm(span=21, adjust=False).mean()
        # M15 fechado: nunca usa a vela maior ainda em formação como filtro.
        indice_m15 = len(df_m15) - 2
        if indice_m15 < 3:
            return None
        close_m15 = df_m15["Close"]
        ema9_m15 = close_m15.ewm(span=9, adjust=False).mean()
        ema21_m15 = close_m15.ewm(span=21, adjust=False).mean()

        e9, e21 = float(ema9_m5.iloc[indice]), float(ema21_m5.iloc[indice])
        e9_15, e21_15 = float(ema9_m15.iloc[indice_m15]), float(ema21_m15.iloc[indice_m15])
        atr, rsi, adx = float(v["ATR"]), float(v["RSI"]), float(v["ADX"])
        di_mais, di_menos = float(v["DI_Mais"]), float(v["DI_Menos"])
        o, h, l, fechamento = (float(v[x]) for x in ("Open", "High", "Low", "Close"))
        faixa_min, faixa_max = min(e9, e21), max(e9, e21)
        toca = l <= faixa_max + 0.15 * atr and h >= faixa_min - 0.15 * atr
        if not toca or h <= l:
            return None

        separacao_atr = abs(e9 - e21) / atr
        incl9 = float(ema9_m5.iloc[indice] - ema9_m5.iloc[indice - 3])
        incl21 = float(ema21_m5.iloc[indice] - ema21_m5.iloc[indice - 3])
        tendencia_alta = (
            e9 > e21 and e9_15 > e21_15 and incl9 > 0 and incl21 > 0
            and di_mais > di_menos
        )
        tendencia_baixa = (
            e9 < e21 and e9_15 < e21_15 and incl9 < 0 and incl21 < 0
            and di_menos > di_mais
        )
        qualidade = separacao_atr >= c.nzd_trend_separacao_atr and adx >= c.nzd_trend_adx_minimo
        call = (
            tendencia_alta and qualidade and 40 <= rsi <= 55
            and fechamento > o and fechamento > faixa_max
            and (fechamento - l) / (h - l) >= 0.55
        )
        put = (
            tendencia_baixa and qualidade and 45 <= rsi <= 60
            and fechamento < o and fechamento < faixa_min
            and (h - fechamento) / (h - l) >= 0.55
        )
        direcao = "call" if call else "put" if put else None
        if direcao is None:
            return None
        return Decisao(
            ativo=ativo,
            direcao=direcao,
            preco=fechamento,
            candle_hora=pd.Timestamp(df_m5.index[indice]),
            motivo="nzd_trend_pullback_v1",
            detalhes={
                "setup": "nzd_trend_pullback_v1",
                "ema9": e9,
                "ema21": e21,
                "ema9_m15": e9_15,
                "ema21_m15": e21_15,
                "rsi14": round(rsi, 1),
                "adx14": round(adx, 1),
                "di_mais": round(di_mais, 1),
                "di_menos": round(di_menos, 1),
                "atr": atr,
                "separacao_atr": round(separacao_atr, 3),
                "toque_faixa": True,
                "confirmacao": "fechamento_rejeicao_m5_m15",
                "tendencia_m15": "alta" if direcao == "call" else "baixa",
            },
        )

    def _avaliar_todas_estrategias(
        self, ativo: str, df: pd.DataFrame, indice: int
    ) -> list[Decisao]:
        # Setups de continuação/confirmação — avaliados no candle FECHADO.
        # pin_bar_sr, sr_rejeicao e engulfing_sr foram movidos para
        # avaliar_reversoes(), onde são avaliados no candle EM FORMAÇÃO.
        c = self.config
        resultado = []
        for fn in (
            self._avaliar_indicadores,
            self._avaliar_pullback_indicadores,
            self._avaliar_breakout_reteste,
            self._avaliar_macd,
            self._avaliar_retracao_intracandle,
            self._avaliar_ema920_pullback,
            self._avaliar_ema920_prime,
            self._avaliar_ema921_rsi_pullback,
        ):
            nome = fn.__name__
            if nome in self._estrategias_desativadas:
                continue
            try:
                d = fn(ativo, df, indice)
                if d is not None:
                    resultado.append(d)
                self._erros_consecutivos[nome] = 0
            except Exception as exc:
                n = self._erros_consecutivos.get(nome, 0) + 1
                self._erros_consecutivos[nome] = n
                if n == 1:
                    logger.warning("[%s] erro #%d: %s", nome, n, exc)
                elif n == 2:
                    logger.warning("[%s] erro #%d (consecutivo): %s", nome, n, exc)
                elif n >= 3:
                    logger.error(
                        "[%s] desativada após %d erros consecutivos. Reinicie o processo para reativar.",
                        nome, n,
                    )
                    self._estrategias_desativadas.add(nome)
        # Nova estratégia de rejeição M1 hierárquica (M15→M5→M1)
        if c.rejeicao_m1_hierarquico_ativo:
            nome = self._avaliar_rejeicao_m1_hierarquico.__name__
            if nome not in self._estrategias_desativadas:
                try:
                    d = self._avaliar_rejeicao_m1_hierarquico(ativo, df, indice)
                    if d is not None:
                        resultado.append(d)
                    self._erros_consecutivos[nome] = 0
                except Exception as exc:
                    n = self._erros_consecutivos.get(nome, 0) + 1
                    self._erros_consecutivos[nome] = n
                    if n == 1:
                        logger.warning("[%s] erro #%d: %s", nome, n, exc)
                    elif n >= 3:
                        logger.error(
                            "[%s] desativada após %d erros consecutivos.", nome, n,
                        )
                        self._estrategias_desativadas.add(nome)

        for ativo_flag, fn in (
            (c.divergencia_rsi_ativo,           self._avaliar_divergencia_rsi),
            (c.bollinger_squeeze_ativo,          self._avaliar_bollinger_squeeze),
            (c.macd_crossover_time_ativo,        self._avaliar_macd_time),
            (c.macd_crossover_tendencia_ativo,   self._avaliar_macd_tendencia),
            (c.divergencia_rsi_time_ativo,       self._avaliar_divergencia_rsi_time),
            (c.divergencia_rsi_tendencia_ativo,  self._avaliar_divergencia_rsi_tendencia),
        ):
            if not ativo_flag:
                continue
            nome = fn.__name__
            if nome in self._estrategias_desativadas:
                continue
            try:
                d = fn(ativo, df, indice)
                if d is not None:
                    resultado.append(d)
                self._erros_consecutivos[nome] = 0
            except Exception as exc:
                n = self._erros_consecutivos.get(nome, 0) + 1
                self._erros_consecutivos[nome] = n
                if n == 1:
                    logger.warning("[%s] erro #%d: %s", nome, n, exc)
                elif n == 2:
                    logger.warning("[%s] erro #%d (consecutivo): %s", nome, n, exc)
                elif n >= 3:
                    logger.error(
                        "[%s] desativada após %d erros consecutivos. Reinicie o processo para reativar.",
                        nome, n,
                    )
                    self._estrategias_desativadas.add(nome)
        # Filtro H4: remove sinais contra a tendência macro do H4
        if self.config.filtro_h4_ativo:
            th4 = self._tendencia_h4.get(ativo, "lateral")
            if th4 != "lateral":
                antes = len(resultado)
                resultado = [d for d in resultado if self._h4_permite(ativo, d.direcao)]
                bloqueados = antes - len(resultado)
                if bloqueados:
                    logger.info("[H4] %s: bloqueou %d sinal(is) contra TendenciaH4=%s", ativo, bloqueados, th4)
        # Filtro M15: remove sinais contra o contexto M15 (exceto rejeicao_m1_hierarquico que já filtra internamente)
        if self.config.filtro_m15_ativo:
            ctx_m15 = self._contexto_m15.get(ativo, "lateral")
            if ctx_m15 != "lateral":
                antes = len(resultado)
                resultado = [
                    d for d in resultado
                    if d.detalhes.get("setup") == "rejeicao_m1_hierarquico"  # já filtrou internamente
                    or self._m15_permite(ativo, d.direcao)
                ]
                bloqueados = antes - len(resultado)
                if bloqueados:
                    logger.info("[M15] %s: bloqueou %d sinal(is) contra ContextoM15=%s", ativo, bloqueados, ctx_m15)
        # Filtro M5: remove sinais contra a estrutura direcional do M5
        if self.config.filtro_m5_ativo:
            est_m5 = self._estrutura_m5.get(ativo, "lateral")
            if est_m5 != "lateral":
                antes = len(resultado)
                resultado = [d for d in resultado if self._m5_alinha(ativo, d.direcao)]
                bloqueados = antes - len(resultado)
                if bloqueados:
                    logger.info("[M5] %s: bloqueou %d sinal(is) contra EstruturaM5=%s", ativo, bloqueados, est_m5)
        # Filtro H1: remove sinais contra a tendência do timeframe superior
        if self.config.filtro_h1_ativo:
            th1 = self._tendencia_h1.get(ativo, "lateral")
            if th1 != "lateral":
                antes = len(resultado)
                resultado = [d for d in resultado if self._h1_permite(ativo, d.direcao)]
                bloqueados = antes - len(resultado)
                if bloqueados:
                    logger.info("[H1] %s: bloqueou %d sinal(is) contra TendenciaH1=%s", ativo, bloqueados, th1)
        return self._filtrar_estrutura_candle(resultado, df, indice)

    def avaliar_reversoes(self, ativo: str, indicadores: pd.DataFrame) -> list[Decisao]:
        """Setups de reversão avaliados no candle EM FORMAÇÃO (entrada na mesma vela).

        fibo_sr_retracao: toca nível fibo+SR e já está rejeitando intra-candle.
        sr_rejeicao: toca suporte/resistência intra-candle e já está voltando.
        pin_bar_sr: pin bar no candle fechado, confirmação parcial no candle atual.
        pullback_confluencia: recuo já tocou Fibo+S/R e a vela atual começa a retomar.
        """
        minimo = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3
        if len(indicadores) < minimo:
            return []
        indice = len(indicadores) - 1  # candle em formação
        resultado = []
        fns_reversao = [self._avaliar_fibo_sr_retracao, self._avaliar_sr_rejeicao]
        if self.config.ema921_rsi_intravela_ativo:
            fns_reversao.append(self._avaliar_ema921_rsi_intravela)
        if self.config.entrada_intracandle_por_toque_ativo and self.config.pullback_confluencia_ativo:
            fns_reversao.append(self._avaliar_pullback_indicadores)
        if self.config.pin_bar_sr_ativo:
            fns_reversao.append(self._avaliar_pin_bar)
        for fn in fns_reversao:
            nome = fn.__name__
            if nome in self._estrategias_desativadas:
                continue
            try:
                d = fn(ativo, indicadores, indice)
                if d is not None:
                    resultado.append(d)
                self._erros_consecutivos[nome] = 0
            except Exception as exc:
                n = self._erros_consecutivos.get(nome, 0) + 1
                self._erros_consecutivos[nome] = n
                if n == 1:
                    logger.warning("[%s] erro #%d: %s", nome, n, exc)
                elif n == 2:
                    logger.warning("[%s] erro #%d (consecutivo): %s", nome, n, exc)
                elif n >= 3:
                    logger.error(
                        "[%s] desativada após %d erros consecutivos. Reinicie o processo para reativar.",
                        nome, n,
                    )
                    self._estrategias_desativadas.add(nome)
        if self.config.engulfing_sr_ativo:
            nome = self._avaliar_engulfing_sr.__name__
            if nome not in self._estrategias_desativadas:
                try:
                    d = self._avaliar_engulfing_sr(ativo, indicadores, indice)
                    if d is not None:
                        resultado.append(d)
                    self._erros_consecutivos[nome] = 0
                except Exception as exc:
                    n = self._erros_consecutivos.get(nome, 0) + 1
                    self._erros_consecutivos[nome] = n
                    if n == 1:
                        logger.warning("[%s] erro #%d: %s", nome, n, exc)
                    elif n == 2:
                        logger.warning("[%s] erro #%d (consecutivo): %s", nome, n, exc)
                    elif n >= 3:
                        logger.error(
                            "[%s] desativada após %d erros consecutivos. Reinicie o processo para reativar.",
                            nome, n,
                        )
                        self._estrategias_desativadas.add(nome)
        # Filtro H4: remove reversões contra a tendência macro do H4
        if self.config.filtro_h4_ativo:
            th4 = self._tendencia_h4.get(ativo, "lateral")
            if th4 != "lateral":
                antes = len(resultado)
                resultado = [d for d in resultado if self._h4_permite(ativo, d.direcao)]
                bloqueados = antes - len(resultado)
                if bloqueados:
                    logger.info("[H4] %s: bloqueou %d reversão(ões) contra TendenciaH4=%s", ativo, bloqueados, th4)
        # Filtro M15: remove reversões contra o contexto M15
        if self.config.filtro_m15_ativo:
            ctx_m15 = self._contexto_m15.get(ativo, "lateral")
            if ctx_m15 != "lateral":
                antes = len(resultado)
                resultado = [d for d in resultado if self._m15_permite(ativo, d.direcao)]
                bloqueados = antes - len(resultado)
                if bloqueados:
                    logger.info("[M15] %s: bloqueou %d reversão(ões) contra ContextoM15=%s", ativo, bloqueados, ctx_m15)
        # Filtro M5: remove reversões contra a estrutura direcional do M5
        if self.config.filtro_m5_ativo:
            est_m5 = self._estrutura_m5.get(ativo, "lateral")
            if est_m5 != "lateral":
                antes = len(resultado)
                resultado = [d for d in resultado if self._m5_alinha(ativo, d.direcao)]
                bloqueados = antes - len(resultado)
                if bloqueados:
                    logger.info("[M5] %s: bloqueou %d reversão(ões) contra EstruturaM5=%s", ativo, bloqueados, est_m5)
        # Filtro H1: remove sinais de reversão contra a tendência do timeframe superior
        if self.config.filtro_h1_ativo:
            th1 = self._tendencia_h1.get(ativo, "lateral")
            if th1 != "lateral":
                antes = len(resultado)
                resultado = [d for d in resultado if self._h1_permite(ativo, d.direcao)]
                bloqueados = antes - len(resultado)
                if bloqueados:
                    logger.info("[H1] %s: bloqueou %d reversão(ões) contra TendenciaH1=%s", ativo, bloqueados, th1)
        return self._filtrar_estrutura_candle(resultado, indicadores, indice)

    def _avaliar_estrategias(
        self, ativo: str, df: pd.DataFrame, indice_confirmacao: int
    ) -> Decisao | None:
        todas = self._avaliar_todas_estrategias(ativo, df, indice_confirmacao)
        if not todas:
            return None
        return min(
            todas,
            key=lambda d: (
                PRIORIDADE_SETUP.get(d.detalhes.get("setup", ""), _PRIORIDADE_DEFAULT),
                d.candle_hora,
            ),
        )

    def avaliar_todas(self, ativo: str, indicadores: pd.DataFrame) -> list[Decisao]:
        minimo = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3
        if len(indicadores) < minimo:
            return []
        return self._avaliar_todas_estrategias(ativo, indicadores, len(indicadores) - 2)

    def avaliar(self, ativo: str, candles: pd.DataFrame) -> Decisao | None:
        if len(candles) < max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3:
            return None
        df = self.calcular_indicadores(candles, ativo)
        return self._avaliar_estrategias(ativo, df, len(df) - 2)

    def sinais_historicos(self, ativo: str, candles: pd.DataFrame) -> list[Decisao]:
        df = self.calcular_indicadores(candles, ativo)
        inicio = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 1
        sinais = []
        for indice in range(inicio, len(df) - 1):
            # Continuação: candle fechado
            sinal = self._avaliar_estrategias(ativo, df, indice)
            if sinal is not None:
                sinais.append(sinal)
            # Reversão: mesmo índice (historicamente, cada candle já é "fechado",
            # mas representamos o sinal como se fosse detectado naquele candle)
            fns_rev = [self._avaliar_sr_rejeicao]
            if self.config.pin_bar_sr_ativo:
                fns_rev.append(self._avaliar_pin_bar)
            for fn in fns_rev:
                try:
                    d = fn(ativo, df, indice)
                    if d is not None:
                        sinais.append(d)
                except Exception:
                    pass
            if self.config.engulfing_sr_ativo:
                try:
                    d = self._avaliar_engulfing_sr(ativo, df, indice)
                    if d is not None:
                        sinais.append(d)
                except Exception:
                    pass
        return sinais

    def possivel_entrada(self, ativo: str, candles: pd.DataFrame) -> dict | None:
        if len(candles) < max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3:
            return None
        df = self.calcular_indicadores(candles, ativo)
        atual = df.iloc[-1]
        if any(pd.isna(atual[x]) for x in ("ATR", "RSI", "BandaInf", "BandaSup")):
            return None
        tolerancia = self.config.alerta_preco_tolerancia_atr * atual["ATR"]
        perto_inferior = atual["Close"] <= atual["BandaInf"] + tolerancia
        perto_superior = atual["Close"] >= atual["BandaSup"] - tolerancia
        if perto_inferior and atual["RSI"] <= self.config.rsi_sobrevendido + self.config.alerta_rsi_margem:
            return {
                "ativo": ativo, "direcao": "call_proxima", "preco": float(atual["Close"]),
                "hora": pd.Timestamp(df.index[-1]), "rsi": float(atual["RSI"]),
                "setup": "reversao_bollinger_rsi", "fatores": ["bollinger", "rsi"],
            }
        if perto_superior and atual["RSI"] >= self.config.rsi_sobrecomprado - self.config.alerta_rsi_margem:
            return {
                "ativo": ativo, "direcao": "put_proxima", "preco": float(atual["Close"]),
                "hora": pd.Timestamp(df.index[-1]), "rsi": float(atual["RSI"]),
                "setup": "reversao_bollinger_rsi", "fatores": ["bollinger", "rsi"],
            }
        contexto = self._contexto_pullback(df, len(df) - 1)
        if contexto is not None:
            return {
                "ativo": ativo,
                "direcao": "call_proxima" if contexto["direcao"] == "call" else "put_proxima",
                "preco": float(atual["Close"]),
                "hora": pd.Timestamp(df.index[-1]),
                "rsi": float(atual["RSI"]),
                "setup": "pullback",
                "fatores": contexto["fatores"],
            }
        return None
