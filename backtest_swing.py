"""Backtest da estrategia swing sobre historico H1 (resample -> H4/D1).

Motivacao: com R:R 2.0 o breakeven e 33.3%. Provar WR=45% exige ~143 trades,
o que a ~3 sinais/semana levaria ~1 ano de forward test. O historico resolve
isso em minutos: cada run do bot ja baixa 120 candles H4 por par de graca.

Anti-lookahead: em cada ponto de avaliacao i so e visivel o historico ate i
(inclusive). A entrada usa o Close de i; a resolucao comeca em i+1.

Uso:
    python backtest_swing.py                       # baixa H1 da IQ e roda
    python backtest_swing.py --score-min 7         # varia o corte de score
    python backtest_swing.py --rr 1.5              # varia o R:R
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import io as _io
import math
import os
import pickle
import sys
from pathlib import Path

import pandas as pd

from iqoption_swing.config_swing import SwingConfig
from iqoption_swing.estrategia_swing import EstrategiaSwing

PARES = ("EURUSD", "GBPUSD", "USDJPY", "EURJPY", "GBPJPY", "AUDUSD",
         "USDCAD", "USDCHF", "NZDUSD", "EURGBP", "EURAUD", "GBPAUD")

AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
MAX_CANDLES_TRADE = 12  # mesmo timeout do monitor (_MONITOR_TIMEOUT_H4)


def _resample_fechado(df: pd.DataFrame, regra: str) -> pd.DataFrame:
    """Rotula o agregado no fechamento para não liberar H4/D1 futuro."""
    cols = {k: v for k, v in AGG.items() if k in df.columns}
    return df.resample(regra, closed="left", label="right").agg(cols).dropna()


def baixar_h1(ativos, n_total=5000, chunk=1000):
    """Baixa H1 em blocos, andando pra tras a partir de agora."""
    from iqoption_swing.mercado_swing import MercadoSwing
    cfg = SwingConfig(conta="PRACTICE", executar_ordens=False, ativos=tuple(ativos))
    m = MercadoSwing(cfg)
    m.iniciar()
    out = {}
    for ativo in ativos:
        partes = []
        ts = m._api.get_server_timestamp()
        while sum(len(p) for p in partes) < n_total:
            try:
                with m._lock:
                    dados = m._api.get_candles(ativo, 3600, chunk, ts)
            except Exception as e:
                print(f"  {ativo}: erro {e!r}")
                break
            if not dados:
                break
            df = m._para_df(dados)
            partes.append(df)
            ts = int(df.index[0].timestamp()) - 1
            if len(df) < chunk:
                break
        if partes:
            h1 = pd.concat(partes).sort_index()
            h1 = h1[~h1.index.duplicated(keep="last")]
            out[ativo] = h1
            print(f"  {ativo}: {len(h1)} candles H1  ({h1.index[0]} -> {h1.index[-1]})")
    return out


class EstrategiaSwingCache(EstrategiaSwing):
    """Igual a EstrategiaSwing, mas memoiza _adicionar_indicadores.

    No backtest o mesmo DataFrame e passado a ~2.4 metodos por avaliar(), e a
    janela D1 fica identica por 6 candles H4 seguidos. Recalcular EMA/ATR/RSI/ADX
    a cada chamada consome 94% do tempo. O cache devolve exatamente os mesmos
    valores — nao altera nenhum resultado, so evita trabalho repetido.
    """

    _cache: dict = {}
    _MAX = 64

    @staticmethod
    def _chave(df):
        # index inicial/final + tamanho + 2 valores identificam a janela sem colisao
        return (df.index[0], df.index[-1], len(df),
                float(df["Close"].iloc[-1]), float(df["High"].iloc[0]))

    @classmethod
    def _adicionar_indicadores(cls, df):
        try:
            k = cls._chave(df)
        except Exception:
            return EstrategiaSwing._adicionar_indicadores(df)
        hit = cls._cache.get(k)
        if hit is not None:
            return hit
        out = EstrategiaSwing._adicionar_indicadores(df)
        if len(cls._cache) >= cls._MAX:
            cls._cache.clear()
        cls._cache[k] = out
        return out


def resolver(df_h4_fut, direcao, sl, tp, max_cand=MAX_CANDLES_TRADE):
    """Percorre candles futuros ate bater SL ou TP. Retorna (resultado, n_candles).

    Conservador: se SL e TP forem tocados no mesmo candle, assume SL primeiro
    (nao da pra saber a ordem intra-candle com dados OHLC).
    """
    for k, (_, row) in enumerate(df_h4_fut.iloc[:max_cand].iterrows(), start=1):
        hi, lo = float(row["High"]), float(row["Low"])
        if direcao == "call":
            if lo <= sl:
                return "loss", k
            if hi >= tp:
                return "win", k
        else:
            if hi >= sl:
                return "loss", k
            if lo <= tp:
                return "win", k
    return "timeout", max_cand


def _rodar_ativo(args):
    """Processa um par. Top-level para ser picklavel pelo ProcessPoolExecutor."""
    ativo, h1, cfg, passo_h4 = args
    est = EstrategiaSwingCache(cfg)
    trades = []
    h4 = _resample_fechado(h1, "4h")
    d1 = _resample_fechado(h1, "1D")
    if len(h4) < 80 or len(d1) < 60:
        print(f"  {ativo}: historico curto (h4={len(h4)} d1={len(d1)}), pulando")
        return trades
    inicio = max(60, cfg.h4_num_candles // 2)  # warmup dos indicadores
    for i in range(inicio, len(h4) - 1, passo_h4):
        agora = h4.index[i]
        jan_h4 = h4.iloc[max(0, i - cfg.h4_num_candles + 1): i + 1]
        jan_d1 = d1[d1.index <= agora].iloc[-cfg.d1_num_candles:]
        # Índice H1 é a abertura; às 12:00 o candle 12:00 ainda não fechou.
        jan_h1 = h1[h1.index + pd.Timedelta(hours=1) <= agora].iloc[-cfg.h1_num_candles:]
        if len(jan_d1) < 55 or len(jan_h1) < 20:
            continue
        try:
            # a estrategia loga cada bloqueio; no backtest isso inunda a saida
            with contextlib.redirect_stdout(_io.StringIO()):
                analise = est.avaliar(ativo, jan_d1, jan_h4, jan_h1)
        except Exception as exc:
            raise RuntimeError(f"{ativo} {agora}: erro avaliando Swing") from exc
        if analise.estado != "ENTRAR" or analise.direcao not in {"compra", "venda"}:
            continue
        if (
            analise.zona_entrada is None
            or analise.invalidacao is None
            or analise.tp1 is None
        ):
            continue
        entrada = sum(analise.zona_entrada) / 2
        sl = float(analise.invalidacao)
        tp = float(analise.tp1)
        risco = abs(entrada - sl)
        ganho = abs(tp - entrada)
        if risco <= 0 or ganho <= 0:
            continue
        if analise.direcao == "compra" and not (sl < entrada < tp):
            continue
        if analise.direcao == "venda" and not (tp < entrada < sl):
            continue
        direcao_iq = "call" if analise.direcao == "compra" else "put"
        rr_trade = ganho / risco
        res, ncand = resolver(h4.iloc[i + 1:], direcao_iq, sl, tp)
        trades.append({
            "ativo": ativo, "quando": agora, "direcao": direcao_iq,
            "setup": analise.setup, "score": 10,
            "resultado": res, "candles": ncand,
            "R": rr_trade if res == "win" else (-1.0 if res == "loss" else 0.0),
        })
    return trades


def rodar(h1_por_ativo, cfg, passo_h4=1, workers=None):
    """Walk-forward sobre todos os pares. Pares sao independentes -> paraleliza."""
    tarefas = [(a, h1, cfg, passo_h4) for a, h1 in h1_por_ativo.items()]
    if workers is None:
        workers = min(len(tarefas), (os.cpu_count() or 2))
    trades = []
    if workers <= 1 or len(tarefas) == 1:
        for t in tarefas:
            trades.extend(_rodar_ativo(t))
    else:
        with cf.ProcessPoolExecutor(max_workers=workers) as ex:
            for parcial in ex.map(_rodar_ativo, tarefas):
                trades.extend(parcial)
    return pd.DataFrame(trades)


def relatorio(df, cfg):
    if df.empty:
        print("\nNENHUM sinal gerado no periodo.")
        return
    dec = df[df.resultado != "timeout"]
    n = len(dec)
    wr = (dec.resultado == "win").mean() if n else 0.0
    n_to = int((df.resultado == "timeout").sum())
    r_medio_win = float(df.loc[df.resultado == "win", "R"].mean()) if (df.resultado == "win").any() else cfg.rr_ratio
    be = 1.0 / (1.0 + r_medio_win)
    ev_total = float(df.R.mean())
    print("\n" + "=" * 68)
    print(f"RESULTADO  ({len(df)} sinais, {n} decididos, {n_to} timeout)")
    print("=" * 68)
    print(f"  WR = {wr:.1%}   breakeven(R medio win {r_medio_win:.2f}R) = {be:.1%}   edge = {(wr-be)*100:+.1f}pp")
    print(f"  EV = {ev_total:+.3f}R por trade | total = {df.R.sum():+.1f}R")
    if n:
        z = 1.96
        den = 1 + z * z / n
        centro = (wr + z * z / (2 * n)) / den
        margem = z * math.sqrt((wr * (1 - wr) + z * z / (4 * n)) / n) / den
        lo, hi = max(0.0, centro - margem), min(1.0, centro + margem)
        if hi < be:
            marca = "  <- todo o IC abaixo do breakeven: edge NEGATIVO"
        elif lo > be and ev_total > 0:
            marca = "  <- WR acima do breakeven e EV positivo"
        elif lo > be:
            marca = "  <- WR acima do breakeven, mas EV total ainda nao confirma"
        else:
            marca = "  <- IC cruza o breakeven: inconclusivo, falta amostra"
        print(f"  IC95% do WR: [{lo:.1%}, {hi:.1%}]{marca}")

    ordenado = df.sort_values("quando")
    meio = len(ordenado) // 2
    print("\n  --- estabilidade temporal ---")
    for nome, parte in (("1a metade", ordenado.iloc[:meio]), ("2a metade", ordenado.iloc[meio:])):
        decididos = parte[parte.resultado != "timeout"]
        wr_parte = (decididos.resultado == "win").mean() if len(decididos) else 0.0
        print(
            f"    {nome:<12} n={len(parte):<4} WR={wr_parte:.1%} "
            f"EV={parte.R.mean():+.3f}R total={parte.R.sum():+.1f}R"
        )

    for col, titulo in [("setup", "SETUP"), ("ativo", "PAR"), ("score", "SCORE")]:
        print(f"\n  --- por {titulo} ---")
        g = df.groupby(col).agg(n=("R", "size"), EV=("R", "mean"), R=("R", "sum"))
        wr_col = dec.groupby(col).resultado.apply(lambda s: (s == "win").mean())
        g = g.join(wr_col.rename("WR"))
        for k, r in g.sort_values("R", ascending=False).iterrows():
            wrs = f"{r.WR:.0%}" if pd.notna(r.WR) else "  -"
            print(f"    {str(k):<22} n={int(r.n):<4} WR={wrs:>4}  EV={r.EV:+.3f}R  tot={r.R:+7.1f}R")
    comparacoes = sum(df[col].nunique() for col in ("setup", "ativo", "score"))
    print(
        f"\n  Multiplicidade: {comparacoes} grupos comparados; "
        f"alpha Bonferroni={0.05/comparacoes:.5f}."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="dados/bt_h1.pkl")
    ap.add_argument("--candles", type=int, default=5000)
    ap.add_argument("--score-min", type=int, default=None)
    ap.add_argument("--rr", type=float, default=None)
    ap.add_argument("--workers", type=int, default=None,
                    help="processos paralelos (padrao: n de CPUs)")
    a = ap.parse_args()

    cache = Path(a.cache)
    if cache.exists():
        print(f"[bt] usando cache {cache}")
        h1 = pickle.loads(cache.read_bytes())
    else:
        print(f"[bt] baixando H1 da IQ ({a.candles} candles/par)...")
        h1 = baixar_h1(PARES, n_total=a.candles)
        if not h1:
            print("[bt] nada baixado.")
            sys.exit(1)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pickle.dumps(h1))
        print(f"[bt] cache salvo em {cache}")

    kw = {}
    if a.score_min is not None:
        kw["pontuacao_minima"] = a.score_min
    if a.rr is not None:
        kw["rr_ratio"] = a.rr
    cfg = SwingConfig(conta="PRACTICE", executar_ordens=False, ativos=PARES, **kw)
    print(f"[bt] rodando  score_min={cfg.pontuacao_minima}  R:R={cfg.rr_ratio}")
    relatorio(rodar(h1, cfg, workers=a.workers), cfg)


if __name__ == "__main__":
    main()
