"""Monitor visual e manual para estudo de Fibo, velas e confluências.

Não envia ordens. Uma única conexão IQ mantém M5, M15 e H1 para a tela.
"""
from __future__ import annotations

import dataclasses
import time
import webbrowser
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Configuracao
from .grafico import GraficoM5
from .mercado_iq import MercadoIQ, iniciar_com_timeout
from .registro import RegistroSQLite


ATIVOS_PADRAO = (
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURGBP",
    "XAUUSD", "BTCUSD", "ETHUSD",
)
TIMEFRAMES = {300: "M5", 900: "M15", 3600: "H1"}
NIVEIS_FIB = (0.382, 0.5, 0.618, 0.786)


def configuracao_monitor_auxiliar() -> Configuracao:
    """Perfil de leitura: sem execução e sem ativos OTC."""
    return dataclasses.replace(
        Configuracao(),
        ativos=ATIVOS_PADRAO,
        conta="PRACTICE",
        executar_ordens=False,
        timeframe_segundos=300,
        limite_candles=180,
        expiracao_minutos=5,
        porta_grafico=8787,
        sufixo_banco="monitor_auxiliar",
        abrir_grafico=False,
        abrir_navegador=False,
    )


def indicadores_auxiliares(candles: pd.DataFrame) -> pd.DataFrame:
    """Camadas pequenas e estáveis: EMA9/21, RSI14, ATR e níveis recentes."""
    df = candles.copy()
    close, high, low = df["Close"], df["High"], df["Low"]
    df["ema9"] = close.ewm(span=9, adjust=False).mean()
    df["ema21"] = close.ewm(span=21, adjust=False).mean()
    delta = close.diff()
    ganho = delta.clip(lower=0).rolling(14).mean()
    perda = (-delta.clip(upper=0)).rolling(14).mean()
    rs = ganho / perda.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    # Janela sem nenhuma perda faz o RS divergir: o RSI é 100, não o 50 neutro
    # que o fillna devolveria — e 50 passa no gate 40–65 do combo de pullback,
    # acendendo o alerta justamente na alta esticada onde ele deve calar.
    rsi = rsi.mask((perda == 0) & (ganho > 0), 100)
    rsi = rsi.mask((ganho == 0) & (perda > 0), 0)
    rsi = rsi.mask((ganho == 0) & (perda == 0), 50)
    df["rsi"] = rsi.fillna(50.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean().bfill().fillna(0.0)
    return df


def detectar_vela(candles: pd.DataFrame) -> dict:
    """Classifica o último candle fechado; não tenta prever sozinho."""
    if len(candles) < 4:
        return {"tipo": "SEM_DADOS", "direcao": "NEUTRA", "forca": 0, "texto": "Aguardando candles."}
    a, b = candles.iloc[-3], candles.iloc[-2]
    corpo = abs(float(b.Close - b.Open))
    amplitude = max(float(b.High - b.Low), 1e-12)
    superior = float(b.High - max(b.Open, b.Close))
    inferior = float(min(b.Open, b.Close) - b.Low)
    alta = b.Close > b.Open
    baixa = b.Close < b.Open
    # Engolfo é mais forte que uma vela de rejeição isolada.
    if alta and a.Close < a.Open and b.Close >= a.Open and b.Open <= a.Close:
        return {"tipo": "ENGOLFO DE ALTA", "direcao": "CALL", "forca": 3, "texto": "Corpo comprador engoliu a vela anterior."}
    if baixa and a.Close > a.Open and b.Close <= a.Open and b.Open >= a.Close:
        return {"tipo": "ENGOLFO DE BAIXA", "direcao": "PUT", "forca": 3, "texto": "Corpo vendedor engoliu a vela anterior."}
    if inferior >= max(corpo * 1.5, amplitude * .35) and b.Close >= b.Open:
        return {"tipo": "REJEIÇÃO DE BAIXA", "direcao": "CALL", "forca": 2, "texto": "Pavio inferior longo; compradores defenderam o nível."}
    if superior >= max(corpo * 1.5, amplitude * .35) and b.Close <= b.Open:
        return {"tipo": "REJEIÇÃO DE ALTA", "direcao": "PUT", "forca": 2, "texto": "Pavio superior longo; vendedores defenderam o nível."}
    if corpo / amplitude >= .65:
        return {"tipo": "VELA FORTE DE " + ("ALTA" if alta else "BAIXA"), "direcao": "CALL" if alta else "PUT", "forca": 1, "texto": "Corpo domina o candle; espere reteste, não persiga."}
    return {"tipo": "INDECISA", "direcao": "NEUTRA", "forca": 0, "texto": "Sem confirmação de vela."}


def contexto_fibo(marcacoes: list[dict], preco: float) -> dict | None:
    fibos = [m for m in marcacoes if m.get("tipo") == "fibo" and m.get("preco_b") is not None]
    if not fibos:
        return None
    m = fibos[-1]
    origem, extremo = float(m["preco_a"]), float(m["preco_b"])
    amplitude = extremo - origem
    if not amplitude:
        return None
    niveis = {str(n): extremo - amplitude * n for n in NIVEIS_FIB}
    z50, z618 = niveis["0.5"], niveis["0.618"]
    inferior, superior = min(z50, z618), max(z50, z618)
    return {
        "origem": origem, "extremo": extremo, "direcao_impulso": "ALTA" if amplitude > 0 else "BAIXA",
        "niveis": niveis, "zona50_618": {"min": inferior, "max": superior, "dentro": inferior <= preco <= superior},
    }


def contexto_fibo_m15_m5(candles_m5: pd.DataFrame, candles_m15: pd.DataFrame) -> dict | None:
    """Contexto automático do mesmo setup do Lab, sem depender da Fibo manual."""
    if len(candles_m5) < 3 or len(candles_m15) < 30:
        return None
    sinal = candles_m5.iloc[-2]
    contexto = candles_m15.loc[candles_m15.index < candles_m5.index[-2]].copy()
    if len(contexto) < 30:
        return None
    contexto["ema9"] = contexto.Close.ewm(span=9, adjust=False).mean()
    contexto["ema21"] = contexto.Close.ewm(span=21, adjust=False).mean()
    janela = contexto.iloc[-5:]
    media = (contexto.High - contexto.Low).rolling(14).mean().iloc[-1]
    topo, fundo = float(janela.High.max()), float(janela.Low.min())
    amplitude = topo - fundo
    if not pd.notna(media) or media <= 0 or amplitude < 1.5 * float(media):
        return None
    ultimo = janela.iloc[-1]
    if ultimo.ema9 > ultimo.ema21 and ultimo.Close > janela.Close.iloc[0]:
        direcao, n50, n618 = "CALL", topo - .50 * amplitude, topo - .618 * amplitude
    elif ultimo.ema9 < ultimo.ema21 and ultimo.Close < janela.Close.iloc[0]:
        direcao, n50, n618 = "PUT", fundo + .50 * amplitude, fundo + .618 * amplitude
    else:
        return None
    minimo, maximo = min(n50, n618), max(n50, n618)
    return {
        "direcao": direcao, "topo": topo, "fundo": fundo,
        "niveis": {"0.5": n50, "0.618": n618},
        "zona": {"min": minimo, "max": maximo, "dentro": minimo <= float(sinal.Close) <= maximo},
    }


def _prazo_binaria(timeframe_segundos: int) -> dict:
    """Prazo explícito para o alerta manual; não é instrução de execução."""
    if timeframe_segundos == 300:
        return {
            "janela_entrada": "até 30s após fechar a vela M5",
            "expiracao_min": 15,
            "rotulo": "M5 → expiração 15 min",
        }
    if timeframe_segundos == 900:
        return {
            "janela_entrada": "até 60s após fechar a vela M15",
            "expiracao_min": 30,
            "rotulo": "M15 → expiração 30 min",
        }
    return {
        "janela_entrada": "use H1 apenas como contexto; confirme no M5",
        "expiracao_min": None,
        "rotulo": "H1 não gera entrada binária direta",
    }


def avaliar_combos(
    ind: pd.DataFrame, vela: dict, fibo: dict | None, timeframe_segundos: int = 300,
    fibo_mtf: dict | None = None,
) -> list[dict]:
    """Devolve alertas explicáveis. Nenhum item representa recomendação de ordem."""
    u = ind.iloc[-2]
    tendencia = "CALL" if u.ema9 > u.ema21 else "PUT" if u.ema9 < u.ema21 else "NEUTRA"
    preco = float(u.Close)
    sr_min, sr_max = float(ind.Low.tail(25).min()), float(ind.High.tail(25).max())
    perto_sr = min(abs(preco - sr_min), abs(preco - sr_max)) <= max(float(u.atr) * .3, 1e-12)
    confirmada = vela["direcao"] == tendencia and vela["forca"] >= 2
    fib_zona = bool(fibo and fibo["zona50_618"]["dentro"])
    mtf_confirmado = bool(
        timeframe_segundos == 300 and fibo_mtf and fibo_mtf["zona"]["dentro"]
        and vela["direcao"] == fibo_mtf["direcao"] and vela["forca"] >= 2
    )
    prazo = _prazo_binaria(timeframe_segundos)
    retorno = [
        {"id": "fibo_m15_m5", "nome": "Fibo M15 + confirmação M5", "ativo": mtf_confirmado,
         "direcao": fibo_mtf["direcao"] if fibo_mtf else "NEUTRA",
         "motivo": "Impulso M15 + zona 50–61,8% + rejeição/engolfo M5" if mtf_confirmado else "Use no M5: aguarda impulso M15, zona 50–61,8% e confirmação M5."},
        {"id": "fibo_correcao", "nome": "Fibo: correção a favor", "ativo": fib_zona and confirmada,
         "direcao": tendencia, "motivo": "50–61,8% + EMA9/21 + vela de confirmação" if fib_zona and confirmada else "Precisa zona 50–61,8%, tendência e rejeição/engolfo."},
        {"id": "fibo_reversao", "nome": "Fibo: reversão no nível", "ativo": fib_zona and perto_sr and vela["forca"] >= 2,
         "direcao": vela["direcao"], "motivo": "50–61,8% + S/R + vela de rejeição/engolfo" if fib_zona and perto_sr and vela["forca"] >= 2 else "Precisa zona, S/R e rejeição; não antecipe."},
        {"id": "ema_pullback", "nome": "EMA9/21: pullback", "ativo": confirmada and 40 <= u.rsi <= 65,
         "direcao": tendencia, "motivo": "EMA alinhada + RSI controlado + confirmação" if confirmada and 40 <= u.rsi <= 65 else "Precisa tendência, RSI 40–65 e vela a favor."},
        {"id": "sweep_retomada", "nome": "Varredura: retomada", "ativo": perto_sr and vela["forca"] >= 2,
         "direcao": vela["direcao"], "motivo": "Nível recente + rejeição/engolfo" if perto_sr and vela["forca"] >= 2 else "Precisa tocar/varrer S/R e rejeitar com força."},
    ]
    return [{**item, **prazo} for item in retorno]


def montar_leitura(
    snapshot, marcacoes: list[dict], timeframe_segundos: int = 300,
    candles_m15: pd.DataFrame | None = None,
) -> dict:
    ind = indicadores_auxiliares(snapshot.candles)
    u = ind.iloc[-2]
    vela = detectar_vela(snapshot.candles)
    fibo = contexto_fibo(marcacoes, float(u.Close))
    fibo_mtf = (
        contexto_fibo_m15_m5(snapshot.candles, candles_m15)
        if timeframe_segundos == 300 and candles_m15 is not None else None
    )
    return {
        "candles": [
            {"time": int(pd.Timestamp(i).tz_localize("UTC").timestamp()) if pd.Timestamp(i).tzinfo is None else int(pd.Timestamp(i).timestamp()),
             "open": float(r.Open), "high": float(r.High), "low": float(r.Low), "close": float(r.Close)}
            for i, r in ind.iterrows()
        ],
        "ema9": [{"time": int(pd.Timestamp(i).tz_localize("UTC").timestamp()) if pd.Timestamp(i).tzinfo is None else int(pd.Timestamp(i).timestamp()), "value": float(v)} for i, v in ind.ema9.items()],
        "ema21": [{"time": int(pd.Timestamp(i).tz_localize("UTC").timestamp()) if pd.Timestamp(i).tzinfo is None else int(pd.Timestamp(i).timestamp()), "value": float(v)} for i, v in ind.ema21.items()],
        "rsi": float(u.rsi), "atr": float(u.atr), "preco": float(u.Close), "vela": vela,
        "fibo": fibo,
        "fibo_mtf": fibo_mtf,
        "combos": avaliar_combos(ind, vela, fibo, timeframe_segundos, fibo_mtf),
        "sr": {"suporte": float(ind.Low.tail(25).min()), "resistencia": float(ind.High.tail(25).max())},
    }


def atualizar_ativo_auxiliar(mercado, ativo: str, marcacoes: list[dict]) -> dict:
    """Isola falhas por timeframe para a tela nunca morrer por um stream ruim."""
    snapshots: dict[int, object] = {}
    for tf in TIMEFRAMES:
        try:
            snapshots[tf] = mercado.snapshot_timeframe(ativo, tf)
        except Exception as erro:
            snapshots[tf] = erro
    por_tf: dict[str, dict] = {}
    for tf, snapshot in snapshots.items():
        if isinstance(snapshot, Exception):
            por_tf[str(tf)] = {"erro": f"{type(snapshot).__name__}: {snapshot}"}
            continue
        try:
            por_tf[str(tf)] = montar_leitura(
                snapshot, marcacoes, tf,
                snapshots[900].candles if tf == 300 and not isinstance(snapshots[900], Exception) else None,
            )
        except Exception as erro:
            por_tf[str(tf)] = {"erro": f"{type(erro).__name__}: {erro}"}
    return por_tf


def executar_monitor_auxiliar() -> int:
    config = configuracao_monitor_auxiliar()
    origem = Path(__file__).resolve().parent.parent / "monitor_auxiliar_web"
    grafico = GraficoM5(config, pasta_web_origem=origem)
    registro = RegistroSQLite(config.banco_sqlite, config=config)
    from . import grafico as modulo_grafico
    modulo_grafico._handler_registro = registro
    url = grafico.iniciar(abrir_navegador=False)
    print("Monitor Auxiliar — somente leitura, sem ordens.")
    print(f"Tela única: {url}")
    webbrowser.open(url)
    mercado = MercadoIQ(config)
    conectado = False
    try:
        print("Conectando na IQ e carregando M5, M15 e H1...")
        iniciou, motivo = iniciar_com_timeout(mercado)
        if not iniciou:
            print(f"[CONEXÃO] {motivo} Monitor não iniciado; reabra quando a IQ normalizar.")
            return 1
        conectado = True
        mercado.iniciar_timeframes_extras({900: config.limite_candles, 3600: config.limite_candles})
        print("Conectado. Marque Fibo com dois cliques; a tela atualiza os alertas.")
        while True:
            for ativo in config.ativos:
                try:
                    por_tf = atualizar_ativo_auxiliar(
                        mercado, ativo, registro.marcacoes("auxiliar", ativo)
                    )
                    grafico.atualizar(ativo, {
                        "ativo": ativo, "atualizado_em": time.time(), "timeframes": por_tf,
                    })
                except Exception as erro:
                    # Um problema de banco/JSON de um ativo não encerra o
                    # processo web. A tela continua utilizável nos demais.
                    print(f" [auxiliar] {ativo}: atualização falhou ({erro})")
            time.sleep(2)
    except KeyboardInterrupt:
        print("Monitor auxiliar parado.")
        return 0
    finally:
        # A thread de inicialização que estourou o prazo pode ainda abrir a
        # conexão depois; fechar o que nunca conectamos corre contra ela.
        if conectado:
            mercado.fechar()
        grafico.fechar()
