import logging
from dataclasses import replace as _dc_replace

import numpy as np
import pandas as pd

from .config import Configuracao
from .modelos import Decisao

logger = logging.getLogger(__name__)

PRIORIDADE_SETUP: dict[str, int] = {
    "pullback_confluencia":   1,
    "fibo_sr_retracao":       2,
    "reversao_confluencia":   3,
    "reversao_bollinger_rsi": 4,
    "sr_rejeicao":            5,
    "engulfing_sr":           6,
    "pin_bar_sr":             7,
    "pullback":               8,
    "bollinger_squeeze":      9,
    "divergencia_rsi":       10,
    "macd_crossover":        11,
    "reversao_candle":       12,
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

        return Decisao(
            ativo=ativo,
            direcao=direcao,
            preco=float(confirmacao["Close"]),
            candle_hora=pd.Timestamp(df.index[indice_confirmacao]),
            motivo="retorno_bollinger_rsi_m5",
            detalhes={
                "setup": "reversao_bollinger_rsi",
                "rsi_estirado": float(esticado["RSI"]),
                "rsi_confirmacao": float(confirmacao["RSI"]),
                "atr": float(confirmacao["ATR"]),
                "tendencia": str(confirmacao["TendenciaMacro"]),
            },
        )

    def _atr_regime_valido(self, df: pd.DataFrame, indice: int) -> bool:
        atr = df.iloc[indice].get("ATR")
        if pd.isna(atr):
            return False
        inicio = max(0, indice - self.config.atr_regime_janela)
        historico = df["ATR"].iloc[inicio:indice].dropna()
        if not len(historico):
            return True
        mediana = float(historico.median())
        return mediana <= 0 or float(atr) <= self.config.atr_max_multiplo_mediana * mediana

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
        c = self.config
        inicio = max(0, indice_recuo - c.pullback_janela)
        impulso = df.iloc[inicio:indice_recuo]
        if len(impulso) < 10:
            return None

        if tendencia == "alta":
            pos_extremo = int(np.argmax(impulso["High"].to_numpy()))
            if pos_extremo == 0:
                return None
            antes_extremo = impulso.iloc[:pos_extremo]
            pos_origem = int(np.argmin(antes_extremo["Low"].to_numpy()))
            origem = float(antes_extremo.iloc[pos_origem]["Low"])
            extremo = float(impulso.iloc[pos_extremo]["High"])
            amplitude = extremo - origem
            zona_baixa = extremo - amplitude * c.pullback_fib_max
            zona_alta = extremo - amplitude * c.pullback_fib_min
        elif tendencia == "baixa":
            pos_extremo = int(np.argmin(impulso["Low"].to_numpy()))
            if pos_extremo == 0:
                return None
            antes_extremo = impulso.iloc[:pos_extremo]
            pos_origem = int(np.argmax(antes_extremo["High"].to_numpy()))
            origem = float(antes_extremo.iloc[pos_origem]["High"])
            extremo = float(impulso.iloc[pos_extremo]["Low"])
            amplitude = origem - extremo
            zona_baixa = extremo + amplitude * c.pullback_fib_min
            zona_alta = extremo + amplitude * c.pullback_fib_max
        else:
            return None

        atr = float(df.iloc[indice_recuo]["ATR"])
        if amplitude <= 0 or atr <= 0 or amplitude < c.pullback_amplitude_min_atr * atr:
            return None
        return float(zona_baixa), float(zona_alta)

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

        # §9: filtro de corpo mínimo no candle de confirmação
        if self.config.pullback_confirmacao_corpo_atr > 0:
            corpo_conf = abs(float(confirmacao["Close"]) - float(confirmacao["Open"]))
            if corpo_conf < self.config.pullback_confirmacao_corpo_atr * float(confirmacao["ATR"]):
                return None

        zona = contexto["zona_fib"]
        setup = "pullback_confluencia" if len(contexto["fatores"]) >= 2 else "pullback"
        if setup == "pullback" and not self.config.pullback_ativo:
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
                "atr": float(confirmacao["ATR"]),
                "fib_min": zona[0] if zona else None,
                "fib_max": zona[1] if zona else None,
                "nivel_sr": contexto["nivel_sr"],
            },
        )

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
            },
        )

    def _avaliar_sr_rejeicao(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
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
                    },
                )

        return None

    def _avaliar_fibo_sr_retracao(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
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
        if contexto["direcao"] == "call" and v_close >= v_open:
            return None
        if contexto["direcao"] == "put" and v_close <= v_open:
            return None
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
                "atr": round(float(vela["ATR"]), 6),
            },
        )

    def _avaliar_macd(self, ativo: str, df: pd.DataFrame, indice: int) -> Decisao | None:
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

        if macd_prev < sig_prev and macd_cur > sig_cur:
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

        if macd_prev > sig_prev and macd_cur < sig_cur:
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
            self._avaliar_fibo_sr_retracao,
            self._avaliar_macd,
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
        return resultado

    def avaliar_reversoes(self, ativo: str, indicadores: pd.DataFrame) -> list[Decisao]:
        """Setups de reversão avaliados no candle EM FORMAÇÃO (entrada na mesma vela).

        sr_rejeicao: toca suporte/resistência intra-candle e já está voltando.
        pin_bar_sr: pin bar no candle fechado, confirmação parcial no candle atual.
        """
        minimo = max(self.config.ema_macro_periodo, self.config.atr_regime_janela) + 3
        if len(indicadores) < minimo:
            return []
        indice = len(indicadores) - 1  # candle em formação
        resultado = []
        fns_reversao = [self._avaliar_sr_rejeicao]
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
        return resultado

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
