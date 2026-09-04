"""Backtest rápido: sinal do scalping M5 e expiração binária de 15 minutos."""
from __future__ import annotations
import csv, os, shutil, subprocess
from dataclasses import replace
from pathlib import Path
import pandas as pd
from iqoption_m5 import backtest
from iqoption_m5.config import Configuracao, configuracao_pesquisa_m5
from iqoption_m5.estrategia import EstrategiaReversaoM5

ROOT = Path(__file__).resolve().parent
ATIVOS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD")
OUT = ROOT / "diario" / "backtest_binaria_m5.csv"

def config_rapida() -> Configuracao:
    c = configuracao_pesquisa_m5(Configuracao(ativos=ATIVOS, timeframe_segundos=300, expiracao_minutos=15))
    campos = {k: False for k in (
        "macd_crossover_ativo","sr_rejeicao_ativo","fibo_sr_retracao_ativo","reversao_candle_ativo",
        "reversao_bollinger_rsi_ativo","engulfing_sr_ativo","divergencia_rsi_ativo","bollinger_squeeze_ativo",
        "pin_bar_sr_ativo","breakout_reteste_ativo","forex_reteste_m15_ativo","noticia_confirmada_ativo",
        "macd_crossover_time_ativo","macd_crossover_tendencia_ativo","divergencia_rsi_time_ativo",
        "divergencia_rsi_tendencia_ativo","pullback_ativo","pullback_confluencia_ativo","retracao_intracandle_ativo",
    )}
    campos.update({"pullback_confluencia_ativo": True, "fibo_sr_retracao_ativo": True, "sr_rejeicao_ativo": True})
    return replace(c, **campos)

def testar(ativo: str, df: pd.DataFrame, cfg: Configuracao, filtro: str = "nenhum") -> list[dict]:
    sinais = EstrategiaReversaoM5(cfg).sinais_historicos(ativo, df)
    pos = {pd.Timestamp(t): i for i, t in enumerate(df.index)}
    ema21 = df["Close"].ewm(span=21, adjust=False).mean()
    ema50 = df["Close"].ewm(span=50, adjust=False).mean()
    saidas = []
    for s in sinais:
        i = pos.get(pd.Timestamp(s.candle_hora))
        if i is None or i + 3 >= len(df): continue
        direcao = str(s.direcao).lower()
        if filtro in {"tendencia", "tendencia_confluencia"}:
            if direcao == "call" and not (ema21.iloc[i] > ema50.iloc[i]): continue
            if direcao == "put" and not (ema21.iloc[i] < ema50.iloc[i]): continue
        if filtro in {"confluencia", "tendencia_confluencia"}:
            fatores = s.detalhes.get("fatores") or []
            if len(fatores) < 2: continue
        entrada = float(df.iloc[i + 1]["Open"]); fechamento = float(df.iloc[i + 3]["Close"])
        resultado = "EMPATE" if fechamento == entrada else ("WIN" if (fechamento > entrada if direcao == "call" else fechamento < entrada) else "LOSS")
        saidas.append({"ativo": ativo, "setup": str(s.detalhes.get("setup", s.motivo)), "direcao": direcao.upper(), "hora_sinal": str(s.candle_hora), "entrada": entrada, "fechamento_15m": fechamento, "resultado_simulado": resultado, "expiracao_min": 15, "filtro": filtro})
    return saidas

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--candles", type=int, default=5000); ap.add_argument("--ativos", nargs="*", default=list(ATIVOS)); ap.add_argument("--payout", type=float, default=.85); ap.add_argument("--filtro", choices=["nenhum","tendencia","confluencia","tendencia_confluencia"], default="nenhum"); a = ap.parse_args()
    cfg = config_rapida(); todas = []
    for ativo in a.ativos:
        df = backtest.carregar_cache(cfg, ativo)
        if df is None or df.empty: print(f"{ativo}: sem cache"); continue
        df = df.tail(a.candles); r = testar(ativo, df, cfg, a.filtro); todas.extend(r); print(f"{ativo}: {len(df)} candles -> {len(r)} sinais ({a.filtro})")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    campos = ["ativo","setup","direcao","hora_sinal","entrada","fechamento_15m","resultado_simulado","expiracao_min","filtro"]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos); w.writeheader(); w.writerows(todas)
    decididas = [x for x in todas if x["resultado_simulado"] != "EMPATE"]; wins = sum(x["resultado_simulado"] == "WIN" for x in decididas); n = len(decididas); wr = wins/n*100 if n else 0; lucro = wins*a.payout-(n-wins)
    print(f"TOTAL: {n} decididos | WIN={wins} | WR={wr:.1f}% | breakeven={100/(1+a.payout):.1f}% | edge={wr-100/(1+a.payout):+.1f}pp | EV={lucro/n if n else 0:+.3f}R")
    builder = ROOT/".artifact_runtime"/".artifact_builder.mjs"; node = os.environ.get("IQOPTION_NODE") or shutil.which("node") or r"C:\Users\gilso\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    if builder.exists(): subprocess.run([node, str(builder), str(OUT), str(OUT.with_suffix(".xlsx"))], check=False, timeout=60)

if __name__ == "__main__": main()
