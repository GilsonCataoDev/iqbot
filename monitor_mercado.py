"""Monitor de mercado — forex + crypto, com contexto de regime.

SEPARACAO DELIBERADA entre duas coisas:

  SINAL DE ENTRADA — so o que passou em teste rigoroso:
      falso rompimento LONG. Forex: WR 55.44% (n_ef=7526, EDGE).
      Crypto: WR 57.66% (n_ef=3195, EDGE). Ambos acima do breakeven 54.05%
      e bem acima do baseline aleatorio.

  CONTEXTO — exibido, NAO operado:
      regime (lateral / canal de alta / canal de baixa / tendencia), bandas do
      canal, tendencia do dia. Medido em 01/09/2026 e REPROVADO como sinal:
      operar A FAVOR do canal (51.14%) rende o MESMO que operar CONTRA (51.11%),
      logo a classificacao nao tem conteudo direcional. Serve para leitura de
      tela e para checar depois se filtra alguma coisa — nao para entrar.

Essa separacao existe porque o bot antigo tinha setups com "80% de WR" que eram
lookahead. Contexto bonito no painel vira sinal de entrada na cabeca de quem
opera; deixar o rotulo explicito e a protecao.

Porta 8777.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import motor_sinais as M
from estrategias_regime import regressao, classificar
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.grafico import GraficoM5
from iqoption_m5.mercado_iq import MercadoIQ
from iqoption_m5.noticias import CalendarioEconomico

FOREX = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD",
         "EURGBP", "EURCHF", "EURCAD", "GBPJPY", "GBPCHF", "GBPCAD", "CADCHF"]
CRYPTO = ["BTCUSD", "ETHUSD", "XRPUSD"]
ATIVOS = FOREX + CRYPTO
CLASSE = {**{a: "forex" for a in FOREX}, **{a: "crypto" for a in CRYPTO}}

TF = 900
PORTA = 8777
INTERVALO_S = 30
JAN = 10


def tendencia_dia(df: pd.DataFrame) -> dict:
    """Direcao do dia: preco vs abertura do dia, e EMA de 4h."""
    hoje = df.index.normalize() == df.index[-1].normalize()
    d = df[hoje]
    if len(d) < 2:
        return {"dir": "—", "pct": 0.0, "ema": "—"}
    ab = float(d["Open"].iloc[0])
    at = float(d["Close"].iloc[-1])
    pct = (at - ab) / ab * 100 if ab else 0.0
    e_r = df["Close"].ewm(span=16, adjust=False).mean()    # ~4h em M15
    e_l = df["Close"].ewm(span=96, adjust=False).mean()    # ~1 dia
    ema = "alta" if float(e_r.iloc[-1]) > float(e_l.iloc[-1]) else "baixa"
    return {"dir": "alta" if pct > 0.05 else ("baixa" if pct < -0.05 else "lateral"),
            "pct": round(pct, 2), "ema": ema}


def contexto_noticia(calendario: CalendarioEconomico | None, ativo: str,
                      agora: datetime) -> dict:
    """Salva um fato do calendário; nunca deriva direção sem dado publicado."""
    if calendario is None:
        return {"estado": "sem_calendario", "texto": "Calendário não disponível."}
    confirmado = calendario.confirmacao_recente(ativo, agora)
    if confirmado:
        return {
            "estado": "resultado_publicado", "texto": confirmado["titulo"],
            "moeda": confirmado["moeda"], "actual": confirmado["actual"],
            "forecast": confirmado["forecast"], "direcao": confirmado["direcao"],
        }
    aviso = calendario.aviso(ativo, agora)
    return {
        "estado": "janela_risco" if aviso and not aviso.startswith("próximo:") else "sem_risco",
        "texto": aviso or "Sem evento relevante na janela de risco.",
    }


def dossie_candle(df: pd.DataFrame, atr: pd.Series, ativo: str, noticia: dict) -> dict:
    """Contexto observável do candle de sinal; não decide nem explica causalidade."""
    vela = df.iloc[-1]
    abertura, maxima, minima, fechamento = (float(vela[c]) for c in ("Open", "High", "Low", "Close"))
    amplitude = maxima - minima
    atr_atual = float(atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) else 0.0
    if amplitude <= 0 or atr_atual <= 0:
        return {"tags": [], "noticia": noticia}
    corpo = abs(fechamento - abertura) / amplitude
    pavio_sup = (maxima - max(abertura, fechamento)) / amplitude
    pavio_inf = (min(abertura, fechamento) - minima) / amplitude
    anteriores = df.iloc[max(0, len(df) - 4):-1]
    movimento_3 = ((fechamento - float(anteriores["Close"].iloc[0])) / atr_atual
                   if not anteriores.empty else 0.0)
    atr_ref = atr.iloc[max(0, len(atr) - 31):-1].median()
    atr_rel = float(atr_atual / atr_ref) if np.isfinite(atr_ref) and atr_ref > 0 else 1.0
    volume_rel = None
    if "Volume" in df:
        volume_ref = df["Volume"].iloc[max(0, len(df) - 21):-1].median()
        if np.isfinite(volume_ref) and volume_ref > 0 and np.isfinite(vela.get("Volume", np.nan)):
            volume_rel = float(vela["Volume"] / volume_ref)
    tipo = "alta" if fechamento > abertura else "baixa" if fechamento < abertura else "doji"
    tags = [
        f"vela_{tipo}",
        "vela_forte" if corpo >= .55 else "vela_fraca",
        "volatilidade_alta" if atr_rel >= 1.35 else "volatilidade_baixa" if atr_rel <= .70 else "volatilidade_normal",
        "movimento_alta" if movimento_3 >= .75 else "movimento_baixa" if movimento_3 <= -.75 else "movimento_neutro",
    ]
    return {
        "tipo": tipo, "corpo_pct": round(corpo * 100, 1),
        "pavio_sup_pct": round(pavio_sup * 100, 1), "pavio_inf_pct": round(pavio_inf * 100, 1),
        "range_atr": round(amplitude / atr_atual, 2), "movimento_3_atr": round(movimento_3, 2),
        "atr_relativo": round(atr_rel, 2),
        "volume_relativo": None if volume_rel is None else round(volume_rel, 2),
        "tags": tags, "noticia": noticia,
    }


class Estado:
    def __init__(self, pasta: Path):
        self.pasta = pasta
        self.dados: dict[str, dict] = {}
        self.status = "iniciando"
        # Historico das ultimas 24h. Sem isso o painel so mostra o instante
        # atual, e quem nao estava olhando na hora do sinal nao ve nada.
        self.historico: list[dict] = []
        self._arquivo_aprendizado = Path(__file__).resolve().parent / "diario" / "monitor_mercado" / "sinais_aprendizado.json"
        self._aprendizado = self._carregar_aprendizado()
        self._vistos: set[str] = {str(h.get("id")) for h in self._aprendizado if h.get("id")}
        self._lock = threading.Lock()

    def _carregar_aprendizado(self) -> list[dict]:
        try:
            bruto = json.loads(self._arquivo_aprendizado.read_text(encoding="utf-8"))
            return [item for item in bruto if isinstance(item, dict)][-1500:]
        except (OSError, json.JSONDecodeError):
            return []

    def _salvar_aprendizado(self) -> None:
        try:
            self._arquivo_aprendizado.parent.mkdir(parents=True, exist_ok=True)
            temporario = self._arquivo_aprendizado.with_suffix(".tmp")
            temporario.write_text(json.dumps(self._aprendizado[-1500:], ensure_ascii=False), encoding="utf-8")
            temporario.replace(self._arquivo_aprendizado)
        except OSError:
            pass

    @staticmethod
    def _comparaveis(item: dict, historico: list[dict], limite: int = 8) -> dict:
        """Recuperação local por tags do contexto, sem alegar causa de resultado."""
        alvo = item.get("dossie") or {}
        tags_alvo = set(alvo.get("tags") or [])
        if not tags_alvo:
            return {"amostra": 0, "wins": 0, "losses": 0, "winrate": None}
        pontuados = []
        for outro in historico:
            if outro.get("id") == item.get("id") or outro.get("classe") != item.get("classe"):
                continue
            desfecho = (outro.get("simulacao") or {}).get("desfecho")
            if desfecho not in ("win_tp1", "loss_sl"):
                continue
            aud = outro.get("dossie") or {}
            tags = set(aud.get("tags") or [])
            if not tags:
                continue
            score_tags = len(tags & tags_alvo) / max(1, len(tags | tags_alvo))
            distancia, usados = 0.0, 0
            for campo in ("corpo_pct", "range_atr", "movimento_3_atr", "atr_relativo"):
                a, b = alvo.get(campo), aud.get(campo)
                if a is None or b is None:
                    continue
                escala = 100.0 if campo == "corpo_pct" else 1.0
                distancia += min(1.0, abs(float(a) - float(b)) / escala)
                usados += 1
            score_num = 1 - distancia / usados if usados else 0.0
            pontuados.append((.65 * score_tags + .35 * score_num, desfecho))
        melhores = [resultado for _, resultado in sorted(pontuados, reverse=True)[:limite]]
        wins, losses = melhores.count("win_tp1"), melhores.count("loss_sl")
        total = wins + losses
        return {"amostra": total, "wins": wins, "losses": losses,
                "winrate": round(wins * 100 / total, 1) if total else None}

    def atualiza(self, ativo: str, info: dict) -> None:
        with self._lock:
            self.dados[ativo] = info

    def registrar_sinal(self, ativo: str, vela: str, info: dict) -> None:
        chave = f"{ativo}:{vela}"
        with self._lock:
            if chave in self._vistos:
                return
            self._vistos.add(chave)
            item = {
                "id": chave, "ativo": ativo, "classe": info.get("classe"), "vela": vela,
                "quando": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "preco": info.get("preco"), "regime": info.get("regime"),
                "hora": info.get("hora"), "janela_boa": info.get("janela_boa"),
                "alvos": info.get("alvos"), "dossie": info.get("dossie", {}),
                "resultado": None, "simulacao": {"desfecho": "aguardando"},
            }
            item["comparaveis"] = self._comparaveis(item, self._aprendizado)
            self.historico.append(item)
            self._aprendizado.append(dict(item))
            corte = time.time() - 24 * 3600
            self.historico = [h for h in self.historico
                              if datetime.fromisoformat(h["quando"]).timestamp() > corte][-60:]
            self._salvar_aprendizado()

    def resolver(self, ativo: str, df: pd.DataFrame) -> None:
        """Registra reação e simulação TP1/SL; não é resultado de ordem real."""
        with self._lock:
            pend = [h for h in self.historico
                    if h["ativo"] == ativo and h["resultado"] is None]
        for h in pend:
            try:
                ts = pd.Timestamp(h["vela"]) + pd.Timedelta(seconds=TF)
                if ts not in df.index:
                    continue
                lin = df.loc[ts]
                o, c = float(lin["Open"]), float(lin["Close"])
                reacao = "subiu" if c > o else ("caiu" if c < o else "igual")
                simulacao = self._resolver_tp_sl(h, df)
                with self._lock:
                    h["resultado"] = reacao
                    h["var_pips"] = round((c - o) / M.pip(ativo), 1)
                    h["simulacao"] = simulacao
                    for salvo in self._aprendizado:
                        if salvo.get("id") == h.get("id"):
                            salvo.update(h)
                            break
                    self._salvar_aprendizado()
            except Exception:
                continue

    @staticmethod
    def _resolver_tp_sl(h: dict, df: pd.DataFrame, horizonte_velas: int = 24) -> dict:
        alvo = h.get("alvos") or {}
        if not alvo or not h.get("vela"):
            return {"desfecho": "sem_dados"}
        inicio = pd.Timestamp(h["vela"])
        futuras = df[df.index > inicio].head(horizonte_velas)
        if futuras.empty:
            return {"desfecho": "aguardando"}
        sl, tp = float(alvo["sl"]), float(alvo["tp1"])
        for numero, (_, vela) in enumerate(futuras.iterrows(), start=1):
            tocou_sl, tocou_tp = float(vela["Low"]) <= sl, float(vela["High"]) >= tp
            if tocou_sl and tocou_tp:
                return {"desfecho": "ambíguo_mesma_vela", "velas": numero}
            if tocou_tp:
                return {"desfecho": "win_tp1", "velas": numero}
            if tocou_sl:
                return {"desfecho": "loss_sl", "velas": numero}
        return {"desfecho": "expirado_6h", "velas": len(futuras)}

    def salvar(self) -> None:
        with self._lock:
            historico_saida = []
            for entrada in reversed(self.historico):
                item = dict(entrada)
                # A amostra cresce quando novos sinais são resolvidos; por isso
                # ela é calculada ao publicar, não fica congelada no momento
                # em que o alerta nasceu.
                item["comparaveis"] = self._comparaveis(item, self._aprendizado)
                historico_saida.append(item)
            payload = {"ts": int(time.time()), "status": self.status,
                       "historico": historico_saida,
                       "ativos": dict(self.dados)}
        try:
            arq = self.pasta / "mercado.json"
            tmp = arq.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                           encoding="utf-8")
            os.replace(tmp, arq)
        except Exception:
            pass

    def salvar_candles(self, ativo: str, df: pd.DataFrame, r: pd.DataFrame) -> None:
        u = df.tail(60)
        ru = r.tail(60)
        out = {
            "candles": [{"t": int(ts.timestamp()), "o": float(x["Open"]),
                         "h": float(x["High"]), "l": float(x["Low"]),
                         "c": float(x["Close"])} for ts, x in u.iterrows()],
            "sup": [None if not np.isfinite(v) else round(float(v), 6)
                    for v in ru["banda_sup"]],
            "inf": [None if not np.isfinite(v) else round(float(v), 6)
                    for v in ru["banda_inf"]],
        }
        try:
            arq = self.pasta / f"mkt_{ativo}.json"
            tmp = arq.with_suffix(".tmp")
            tmp.write_text(json.dumps(out), encoding="utf-8")
            os.replace(tmp, arq)
        except Exception:
            pass


def loop(api, cfg, estado: Estado, calendario: CalendarioEconomico | None = None) -> None:
    hist: dict[str, pd.DataFrame] = {}
    for a in ATIVOS:
        df = backtest.carregar_cache(cfg, a)
        if df is not None and len(df) >= 600:
            hist[a] = df
            print(f"  {a}: {len(df)} velas")
        else:
            print(f"  {a}: cache insuficiente — fora")
    if not hist:
        print("Sem historico."); return

    while True:
        if calendario is not None:
            calendario.atualizar()
        for a, base in list(hist.items()):
            try:
                novo = backtest.baixar_historico(api, cfg, a, 60)
                if novo is None or novo.empty:
                    continue
                novo = novo.iloc[:-1]
                if novo.empty:
                    continue
                df = pd.concat([base[~base.index.isin(novo.index)], novo]).sort_index()
                hist[a] = df

                # PERFORMANCE: regressao usa rolling().apply() com funcao
                # Python — sobre as 26k velas completas eram ~1.3M chamadas por
                # ciclo (17 ativos), e o loop nunca fechava uma passada. O
                # supervisor pegou isso como "travado". So precisamos do valor
                # atual e das ultimas 60 velas para o grafico, entao a janela
                # de 300 basta (regressao usa 40).
                dfr = df.tail(300)
                r = regressao(dfr)
                cls = classificar(r)
                lo, _ = M.s_falso_rompimento(df, JAN)
                lo = lo.fillna(False)
                atr = M.atr(df)

                # ---- proximidade do sinal ----
                # O painel ficava vazio 99% do tempo porque sinal e raro
                # (~26/dia em 17 ativos, concentrados fora de Londres/NY).
                # Aqui expomos as DUAS condicoes do falso rompimento separadas,
                # para dar pra ver o mercado se aproximando em vez de so o
                # instante do disparo. Nao e previsao: e o estado atual das
                # condicoes que o proprio sinal usa.
                amp = df["High"] - df["Low"]
                med_amp = amp.shift(1).rolling(JAN).mean()
                limiar = med_amp.expanding(min_periods=500).quantile(0.25).shift(1)
                rlo_s = df["Low"].shift(1).rolling(JAN).min()
                acum = bool(med_amp.iloc[-1] <= limiar.iloc[-1]) \
                    if np.isfinite(med_amp.iloc[-1]) and np.isfinite(limiar.iloc[-1]) else False
                # quao apertado esta vs o limiar (1.0 = exatamente no limiar)
                aperto = (float(med_amp.iloc[-1]) / float(limiar.iloc[-1])
                          if np.isfinite(limiar.iloc[-1]) and limiar.iloc[-1] > 0 else None)
                # distancia ate romper o piso, em ATR (negativo = ja rompeu)
                av0 = float(atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) else None
                dist = ((float(df["Close"].iloc[-1]) - float(rlo_s.iloc[-1])) / av0
                        if av0 and av0 > 0 and np.isfinite(rlo_s.iloc[-1]) else None)

                ult = df.iloc[-1]
                preco = float(ult["Close"])
                av = float(atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) else None
                agora = datetime.now(timezone.utc)
                noticia = contexto_noticia(calendario, a, agora)

                # Alvos medidos em 01/09/2026 (janela 21-22h UTC, SL=1 ATR,
                # holdout confirma em todos os niveis). O EV cresce com o alvo
                # — "deixar correr" e melhor — mas o WR despenca junto:
                #   TP 2.5 ATR: WR 40.4%  EV +0.4126R  <- equilibrio pratico
                #   TP 5.0 ATR: WR 24.6%  EV +0.4753R  <- maior EV, 3 perdas
                #                                          seguidas viram rotina
                # Alvos ESTRUTURAIS ficaram todos piores: meio do range +0.2932R,
                # topo do range +0.3251R, maxima da vela de rompimento +0.2553R.
                # Por isso o alvo aqui e multiplo de ATR, nao nivel de preco.
                alvos = None
                if av and av > 0:
                    ent = preco
                    alvos = {
                        "entrada": round(ent, 6),
                        "sl": round(ent - 1.0 * av, 6),
                        "tp1": round(ent + 2.5 * av, 6),
                        "tp2": round(ent + 5.0 * av, 6),
                        "risco_pips": round(av / M.pip(a), 1),
                    }
                bi = float(r["banda_inf"].iloc[-1]) if np.isfinite(r["banda_inf"].iloc[-1]) else None
                bs = float(r["banda_sup"].iloc[-1]) if np.isfinite(r["banda_sup"].iloc[-1]) else None
                pos_canal = None
                if bi is not None and bs is not None and bs > bi:
                    pos_canal = round((preco - bi) / (bs - bi) * 100, 1)

                estado.atualiza(a, {
                    "classe": CLASSE[a],
                    "preco": round(preco, 6),
                    "regime": str(cls.iloc[-1]),
                    "slope_atr": round(float(r["slope_atr"].iloc[-1]), 4)
                                 if np.isfinite(r["slope_atr"].iloc[-1]) else None,
                    "r2": round(float(r["r2"].iloc[-1]), 3)
                          if np.isfinite(r["r2"].iloc[-1]) else None,
                    "banda_inf": round(bi, 6) if bi else None,
                    "banda_sup": round(bs, 6) if bs else None,
                    "pos_canal": pos_canal,
                    "dia": tendencia_dia(df),
                    "atr_pips": round(float(atr.iloc[-1]) / M.pip(a), 1)
                                if np.isfinite(atr.iloc[-1]) else None,
                    "sinal": bool(lo.iloc[-1]),
                    "acumulacao": acum,
                    "aperto": round(aperto, 2) if aperto is not None else None,
                    "dist_atr": round(dist, 2) if dist is not None else None,
                    # 0-100: quao perto de disparar. So para ordenar a tabela.
                    "prox": (0 if not acum or dist is None
                             else int(max(0, min(100, 100 - dist * 50)))),
                    "alvos": alvos,
                    # O edge de forex so aparece em 21-22h UTC (+0.41R); nas
                    # demais horas o EV e negativo em TODOS os alvos testados.
                    # Em crypto essa janela nao significa nada (opera 24/7) —
                    # o efeito e de sessao de forex.
                    "janela_boa": int(df.index[-1].hour) in (21, 22)
                                  and CLASSE[a] == "forex",
                    "hora": int(df.index[-1].hour),
                    "vela": str(df.index[-1]),
                    "dossie": dossie_candle(df, atr, a, noticia),
                })
                estado.salvar_candles(a, dfr, r)
                estado.resolver(a, df)
                # Heartbeat POR ATIVO, nao so no fim da passada. Antes, um
                # unico ativo lento (download travado) fazia o supervisor achar
                # que o monitor inteiro morreu, porque o JSON so era escrito
                # depois de percorrer os 17.
                estado.status = f"lendo {a} — {datetime.now(timezone.utc):%H:%M:%S} UTC"
                estado.salvar()
                if bool(lo.iloc[-1]):
                    estado.registrar_sinal(a, str(df.index[-1]), estado.dados[a])
                    print(f"[SINAL] {a} LONG @ {df.index[-1]} preco={preco} "
                          f"regime={cls.iloc[-1]}")
            except Exception as e:
                print(f"[{a}] {e!r}")
        estado.status = f"OK — {datetime.now(timezone.utc):%H:%M:%S} UTC"
        estado.salvar()
        time.sleep(INTERVALO_S)


_HTML = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Monitor Mercado</title><style>
*{box-sizing:border-box}
body{font-family:ui-monospace,monospace;background:#0b1220;color:#e2e8f0;margin:0;padding:.8rem}
h1{color:#38bdf8;font-size:1.05rem;margin:0 0 .2rem}
.sub{color:#64748b;font-size:.72rem;margin-bottom:.6rem}
.aviso{background:#3b1d0e;border-left:3px solid #f59e0b;padding:.5rem .7rem;border-radius:.3rem;
  font-size:.72rem;color:#fcd34d;margin:.5rem 0}
.sec{color:#94a3b8;font-size:.72rem;letter-spacing:.08em;margin:.9rem 0 .35rem;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:.75rem}
th,td{text-align:left;padding:.28rem .45rem;border-bottom:1px solid #1e293b;white-space:nowrap}
th{color:#64748b;font-weight:600;font-size:.68rem}
tr.sig{background:#052e1a}
tr:hover{background:#16203450;cursor:pointer}
.lat{color:#94a3b8}.ca{color:#4ade80}.cb{color:#f87171}.tend{color:#fbbf24}.ind{color:#475569}
.up{color:#4ade80}.dn{color:#f87171}
.quente{color:#f97316;font-weight:700}.morno{color:#fbbf24}.frio{color:#64748b}
.badge{display:inline-block;padding:0 .35rem;border-radius:.2rem;font-size:.65rem;font-weight:700}
.b-sig{background:#22c55e;color:#052e1a}
.bar{display:inline-block;width:60px;height:7px;background:#1e293b;border-radius:3px;
  position:relative;vertical-align:middle}
.bar>i{position:absolute;top:-2px;width:3px;height:11px;background:#38bdf8;border-radius:1px}
#cv{width:100%;height:300px;display:block;background:#0d1526;border-radius:.4rem;margin:.4rem 0}
.tabs{display:flex;gap:.3rem;margin:.3rem 0}
.tab{background:#1e293b;border:1px solid #334155;border-radius:.3rem;padding:.15rem .5rem;
  cursor:pointer;font-size:.7rem;color:#94a3b8}
.tab.on{background:#0f4c75;border-color:#38bdf8;color:#fff}
.card{background:#151f33;border-radius:.4rem;padding:.7rem;margin:.4rem 0;border-left:4px solid #334155}
.card.okjan{border-left-color:#22c55e}
.card.nojan{border-left-color:#64748b;opacity:.75}
.alvos{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:.4rem;margin-top:.5rem}
.alvos label{display:block;font-size:.62rem;color:#94a3b8}
.alvos span{font-size:.92rem;font-weight:700}
.b-ok{background:#22c55e;color:#052e1a}
.b-no{background:#334155;color:#94a3b8}
.nota{margin-top:.5rem;font-size:.66rem;color:#7dd3fc;background:#0c2a3e;padding:.35rem .5rem;border-radius:.25rem}
.dossie{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:.45rem;margin-top:.5rem}
.dossie .item{background:#0d1728;border:1px solid #26364e;border-radius:.3rem;padding:.4rem}
.dossie label{display:block;color:#94a3b8;font-size:.62rem;margin-bottom:.12rem}
.dossie span{font-size:.76rem;font-weight:700}
.dossie-nota{font-size:.68rem;line-height:1.45;color:#cbd5e1;margin-top:.5rem;background:#0c2a3e;padding:.4rem .5rem;border-radius:.25rem}
.dossie-aviso{color:#fbbf24}.dossie-win{color:#4ade80}.dossie-loss{color:#f87171}
</style></head><body>
<h1>Monitor Mercado — Forex + Crypto</h1>
<div class="sub"><span id="status">carregando...</span> &nbsp;·&nbsp; <span id="relogio"></span></div>

<div class="aviso">
<b>Sinal</b> = falso rompimento LONG, unico validado (forex 55.44%, crypto 57.66%, ambos EDGE).<br>
<b>Regime, canal e tendencia do dia</b> = contexto para leitura, <b>nao sao sinal</b>.
Testado em 01/09/2026: operar a favor do canal (51.14%) rende o mesmo que contra (51.11%)
— a classificacao nao tem conteudo direcional.
</div>

<div class="sec">GRAFICO</div>
<div class="tabs" id="tabs"></div>
<canvas id="cv"></canvas>

<div class="sec">SINAIS ATIVOS</div>
<div id="sinais"></div>

<div class="sec">CONTEXTO — ordenado por proximidade do sinal</div>
<div id="tab"></div>

<div class="sec">SINAIS DAS ULTIMAS 24h</div>
<div id="hist"></div>

<div class="sec">DOSSIE DO SINAL</div>
<div id="dossie"><span class="empty">Clique num sinal do histórico quando houver um.</span></div>

<script>
let D={}, sel=null, dossieSel=null;
const RC={LATERAL:'lat',CANAL_ALTA:'ca',CANAL_BAIXA:'cb',TENDENCIA:'tend',indefinido:'ind'};
const RN={LATERAL:'lateral',CANAL_ALTA:'canal alta',CANAL_BAIXA:'canal baixa',
          TENDENCIA:'tendencia',indefinido:'—'};

function linha(a,x){
  const pc = x.pos_canal;
  const bar = pc==null?'—':`<span class="bar"><i style="left:${Math.max(0,Math.min(100,pc))*0.57}px"></i></span> ${pc}%`;
  const d = x.dia||{};
  const dc = d.dir==='alta'?'up':(d.dir==='baixa'?'dn':'lat');
  // proximidade: acumulacao ligada + quantos ATR faltam para romper o piso
  let prox='<span class="ind">—</span>';
  if(x.acumulacao){
    const dd = x.dist_atr;
    const cor = dd==null?'ind':(dd<=0.5?'quente':(dd<=1.5?'morno':'frio'));
    prox = `<span class="${cor}">acum · ${dd==null?'?':dd.toFixed(2)} ATR</span>`;
  } else if(x.aperto!=null){
    prox = `<span class="ind">${x.aperto.toFixed(2)}x limiar</span>`;
  }
  return `<tr class="${x.sinal?'sig':''}" onclick="pick('${a}')">
    <td><b>${a}</b> ${x.sinal?'<span class="badge b-sig">SINAL</span>':''}</td>
    <td>${x.classe}</td>
    <td>${prox}</td>
    <td class="${RC[x.regime]||'ind'}">${RN[x.regime]||'—'}</td>
    <td>${x.r2==null?'—':x.r2}</td>
    <td>${bar}</td>
    <td class="${dc}">${d.dir||'—'} ${d.pct>0?'+':''}${d.pct==null?'':d.pct}%</td>
    <td class="${d.ema==='alta'?'up':'dn'}">${d.ema||'—'}</td>
    <td>${x.atr_pips==null?'—':x.atr_pips+'p'}</td>
    <td>${x.preco}</td></tr>`;
}

function histLinha(h){
  const r=h.resultado;
  const cor = r==='subiu'?'up':(r==='caiu'?'dn':'ind');
  const res = r==null?'<span class="ind">aguardando</span>'
            : `<span class="${cor}">${r} ${h.var_pips>0?'+':''}${h.var_pips??''}p</span>`;
  const hh = (h.quando||'').slice(11,16);
  return `<tr onclick="dossie('${h.id||''}')"><td>${hh}</td><td><b>${h.ativo}</b></td>
    <td>${h.janela_boa?'<span class="badge b-ok">21-22h</span>':h.hora+'h'}</td>
    <td class="${RC[h.regime]||'ind'}">${RN[h.regime]||'—'}</td>
    <td>${h.preco}</td><td>${res}</td></tr>`;
}

function textoSimulacao(s){
  const d=(s||{}).desfecho||'aguardando';
  if(d==='win_tp1') return '<span class="dossie-win">TP1 atingido (simulado)</span>';
  if(d==='loss_sl') return '<span class="dossie-loss">SL atingido (simulado)</span>';
  if(d==='ambíguo_mesma_vela') return '<span class="dossie-aviso">TP e SL na mesma vela — ordem desconhecida</span>';
  if(d==='expirado_6h') return '<span class="dossie-aviso">nem TP1 nem SL em 6h</span>';
  return '<span class="ind">aguardando candles futuros</span>';
}

function renderDossie(H){
  const el=document.getElementById('dossie');
  const h=H.find(x=>x.id===dossieSel)||H[0];
  if(!h){ el.innerHTML='<span class="empty">Ainda não há sinais registrados.</span>'; return; }
  dossieSel=h.id;
  const a=h.dossie||{}, n=a.noticia||{}, c=h.comparaveis||{};
  const vol=a.volume_relativo==null?'sem dado':a.volume_relativo+'× mediana';
  const comp=c.amostra
    ? `<span class="${c.winrate>=55?'dossie-win':'dossie-aviso'}">${c.wins}W / ${c.losses}L · ${c.winrate}%</span>`
    : '<span class="ind">amostra ainda pequena</span>';
  const reacao=h.resultado==null?'aguardando':`${h.resultado} ${h.var_pips>0?'+':''}${h.var_pips||0}p`;
  el.innerHTML=`<div class="card">
    <b>${h.ativo} LONG</b> · ${String(h.vela||'').replace('T',' ').slice(0,16)} UTC
    <div class="dossie">
      <div class="item"><label>vela de sinal</label><span>${a.tipo||'—'} · corpo ${a.corpo_pct??'—'}%</span></div>
      <div class="item"><label>pavios</label><span>sup ${a.pavio_sup_pct??'—'}% · inf ${a.pavio_inf_pct??'—'}%</span></div>
      <div class="item"><label>range / volatilidade</label><span>${a.range_atr??'—'} ATR · ${a.atr_relativo??'—'}×</span></div>
      <div class="item"><label>movimento anterior</label><span>${a.movimento_3_atr??'—'} ATR · vol ${vol}</span></div>
      <div class="item"><label>reação da vela seguinte</label><span>${reacao}</span></div>
      <div class="item"><label>TP1 / SL em até 6h</label>${textoSimulacao(h.simulacao)}</div>
      <div class="item"><label>casos parecidos (${c.amostra||0})</label>${comp}</div>
      <div class="item"><label>notícia no contexto</label><span class="${n.estado==='janela_risco'?'dossie-aviso':''}">${n.texto||'sem calendário'}</span></div>
    </div>
    <div class="dossie-nota">Tags: ${(a.tags||[]).join(' · ')||'sem dados'}. Comparação usa só sinais antigos da mesma classe (forex ou cripto) com contexto parecido. Isto descreve padrões; não prova a causa de win ou loss.</div>
  </div>`;
}

function dossie(id){ dossieSel=id; render(); }

// contagem regressiva ate o fechamento da vela M15
function tickRelogio(){
  const now=new Date();
  const s=now.getUTCMinutes()*60+now.getUTCSeconds();
  const falta=900-(s%900);
  const m=String(Math.floor(falta/60)).padStart(2,'0');
  const ss=String(falta%60).padStart(2,'0');
  const el=document.getElementById('relogio');
  if(el) el.textContent=`proxima vela M15 em ${m}:${ss}`;
}
setInterval(tickRelogio,1000);

// alerta sonoro (WebAudio, sem arquivo externo)
let ultimoAlerta=0;
function bip(){
  if(Date.now()-ultimoAlerta<60000) return;
  ultimoAlerta=Date.now();
  try{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    const o=ctx.createOscillator(), g=ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value=880; g.gain.value=.08;
    o.start(); setTimeout(()=>{o.stop();ctx.close();},220);
  }catch(e){}
}

function cardSinal(a,x){
  const v=x.alvos;
  if(!v) return `<div class="card"><b>${a}</b> — sinal (sem ATR ainda)</div>`;
  const boa=x.janela_boa;
  return `<div class="card ${boa?'okjan':'nojan'}">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <b style="font-size:.95rem">${a} LONG</b>
      <span class="badge ${boa?'b-ok':'b-no'}">${boa?'JANELA 21-22h':x.hora+'h — fora da janela'}</span>
    </div>
    <div class="alvos">
      <div><label>entrada</label><span>${v.entrada}</span></div>
      <div><label>stop (1 ATR)</label><span class="dn">${v.sl}</span></div>
      <div><label>alvo 1 (2.5 ATR)</label><span class="up">${v.tp1}</span></div>
      <div><label>alvo 2 (5 ATR)</label><span class="up">${v.tp2}</span></div>
      <div><label>risco</label><span>${v.risco_pips}p</span></div>
      <div><label>regime</label><span class="${RC[x.regime]||'ind'}" style="font-size:.8rem">${RN[x.regime]||'—'}</span></div>
    </div>
    <div class="nota">${boa
      ? 'alvo 1: acerta 40.4%, EV +0.41R &bull; alvo 2: acerta 24.6%, EV +0.48R (medido 21-22h UTC)'
      : 'fora de 21-22h o EV foi NEGATIVO em todos os alvos testados — contexto, nao entrada'}</div>
  </div>`;
}

function render(){
  const A=D.ativos||{};
  const ks=Object.keys(A).sort();
  const sig=ks.filter(k=>A[k].sinal);
  document.getElementById('sinais').innerHTML = sig.length
    ? sig.map(k=>cardSinal(k,A[k])).join('')
    : '<span style="color:#475569;font-style:italic;font-size:.8rem">nenhum sinal agora</span>';
  // ordena por proximidade do sinal: o que esta perto de disparar sobe
  const ord=[...ks].sort((x,y)=>(A[y].prox||0)-(A[x].prox||0));
  document.getElementById('tab').innerHTML =
    '<table><tr><th>ativo</th><th>classe</th><th>proximidade</th><th>regime</th><th>R2</th><th>pos. no canal</th><th>dia</th><th>EMA 4h/1d</th><th>ATR</th><th>preco</th></tr>'
    + ord.map(k=>linha(k,A[k])).join('') + '</table>';

  const H=D.historico||[];
  document.getElementById('hist').innerHTML = H.length
    ? '<table><tr><th>hora</th><th>ativo</th><th>janela</th><th>regime</th><th>preco</th><th>vela seguinte</th></tr>'
      + H.map(histLinha).join('') + '</table>'
    : '<span style="color:#475569;font-style:italic;font-size:.8rem">nenhum sinal nas ultimas 24h</span>';
  renderDossie(H);
  if(sig.length) bip();
  const t=document.getElementById('tabs');
  if(t.children.length!==ks.length){
    t.innerHTML = ks.map(k=>`<button class="tab" data-a="${k}" onclick="pick('${k}')">${k}</button>`).join('');
    if(!sel && ks.length) sel=ks[0];
  }
  [...t.children].forEach(b=>b.className='tab'+(b.dataset.a===sel?' on':''));
}

async function pick(a){ sel=a; render(); await grafico(); }

async function grafico(){
  if(!sel) return;
  let d; try{ d=await (await fetch('mkt_'+sel+'.json?t='+Date.now())).json(); }catch(e){ return; }
  const cv=document.getElementById('cv'), W=cv.clientWidth, H=300;
  cv.width=W; cv.height=H;
  const c=cv.getContext('2d');
  c.fillStyle='#0d1526'; c.fillRect(0,0,W,H);
  const K=d.candles||[]; if(!K.length) return;
  const P={l:6,r:56,t:16,b:18};
  const cw=(W-P.l-P.r)/K.length, bw=Math.max(1,cw*.6);
  let vals=K.flatMap(k=>[k.h,k.l]).concat((d.sup||[]).filter(v=>v!=null)).concat((d.inf||[]).filter(v=>v!=null));
  let mx=Math.max(...vals), mn=Math.min(...vals);
  const pad=(mx-mn)*.06||1e-6; mx+=pad; mn-=pad;
  const Y=v=>P.t+(mx-v)/(mx-mn)*(H-P.t-P.b);
  c.strokeStyle='#1e293b';
  for(let i=0;i<=4;i++){const y=P.t+i*(H-P.t-P.b)/4;c.beginPath();c.moveTo(P.l,y);c.lineTo(W-P.r,y);c.stroke();
    c.fillStyle='#475569';c.font='9px monospace';c.textAlign='left';
    c.fillText((mx-i*(mx-mn)/4).toPrecision(6),W-P.r+3,y+3);}
  // bandas do canal
  [['sup','#f59e0b'],['inf','#f59e0b']].forEach(([k,col])=>{
    const S=d[k]||[]; c.strokeStyle=col; c.globalAlpha=.55; c.lineWidth=1.2; c.beginPath();
    let started=false;
    S.forEach((v,i)=>{ if(v==null) return; const x=P.l+i*cw+cw/2, y=Y(v);
      if(!started){c.moveTo(x,y);started=true;} else c.lineTo(x,y); });
    c.stroke(); c.globalAlpha=1;
  });
  K.forEach((k,i)=>{
    const x=P.l+i*cw+cw/2, up=k.c>=k.o, col=up?'#22c55e':'#ef4444';
    c.strokeStyle=col; c.lineWidth=1;
    c.beginPath(); c.moveTo(x,Y(k.h)); c.lineTo(x,Y(k.l)); c.stroke();
    c.fillStyle=col;
    const yt=Math.min(Y(k.o),Y(k.c));
    c.fillRect(x-bw/2,yt,bw,Math.max(1,Math.abs(Y(k.c)-Y(k.o))));
  });
  const A=(D.ativos||{})[sel]||{};
  c.fillStyle='#94a3b8'; c.font='11px monospace'; c.textAlign='left';
  c.fillText(`${sel} · ${RN[A.regime]||'—'} · R2 ${A.r2??'—'}`,P.l+2,11);
}

async function tick(){
  try{
    D=await (await fetch('mercado.json?t='+Date.now())).json();
    document.getElementById('status').textContent='Atualizado: '+D.status;
    render(); await grafico();
  }catch(e){ document.getElementById('status').textContent='Erro: '+e; }
}
tick(); setInterval(tick,30000);
window.addEventListener('resize',grafico);
</script></body></html>"""


def main() -> int:
    import dataclasses
    base = configuracao_scalping_m15()
    cfg = dataclasses.replace(base, timeframe_segundos=TF)
    print("Conectando na IQ Option...")
    api = MercadoIQ(base).conectar_somente_leitura()

    g = GraficoM5(dataclasses.replace(base, porta_grafico=PORTA,
                                      sufixo_banco="mercado"))
    g.iniciar(abrir_navegador=False)
    (g.pasta_web / "index.html").write_text(_HTML, encoding="utf-8")

    estado = Estado(g.pasta_web)
    calendario = CalendarioEconomico(base.pasta_dados)
    calendario.atualizar()
    estado.salvar()
    url = f"http://127.0.0.1:{PORTA}/index.html"
    print(f"Painel: {url}")
    webbrowser.open(url)
    print(f"\nCarregando {len(ATIVOS)} ativos...")
    try:
        loop(api, cfg, estado, calendario)
    except KeyboardInterrupt:
        print("\nParado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
