"""Forward test FOREX — falso rompimento LONG M15 com SL/TP.

PERGUNTA: o edge de 21-22h UTC (+0.57R no backtest com spread real) existe
para frente, ou e artefato de Ago/2025-Ago/2026?

Registra em PAPEL, nao envia ordem. Nem daria: o endpoint place-order-temp
da IQ esta morto.

DECISOES DE DESENHO

1. Registra TODAS as horas, marcando `janela_boa`. Nao filtra so 21-22h de
   proposito: assim o proprio forward test JULGA a hipotese de horario em vez
   de assumi-la. Se fora da janela vier positivo tambem, a hipotese cai.

2. Geometria SL=1.0 / TP=2.0 ATR — a melhor do backtest com spread real
   (n=458, WR 52.4%, EV +0.5721R na janela). Fixa, sem reotimizar ao vivo.

3. Grava o SPREAD REAL no instante do sinal (bid/ask via get_realtime_candles).
   O backtest de 21-22h usou spread medido em 12-19h — fora do dominio. Aqui
   medimos na hora certa, e da para reprocessar depois com o numero correto.

4. Resolucao conservadora: SL checado ANTES do TP quando os dois cabem na
   mesma vela. Foi o bug B3 da auditoria, que inflava 42% do resultado.

Porta 8778.
"""
from __future__ import annotations

import csv
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
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.grafico import GraficoM5
from iqoption_m5.mercado_iq import MercadoIQ

ATIVOS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD",
          "EURGBP", "EURCHF", "EURCAD", "GBPJPY", "GBPCHF", "GBPCAD", "CADCHF"]
TF = 900
PORTA = 8778
INTERVALO_S = 30
JAN = 10
SL_ATR = 1.0
TP_ATR = 2.0
MAX_VELAS = 60          # 15h; alem disso o trade e abandonado
HORAS_JANELA = (21, 22)

# Referencia do backtest (spread real, 14 pares, 12 meses)
BT = {"janela": {"n": 458, "wr": 0.524, "ev": 0.5721},
      "todas": {"n": 4381, "wr": 0.337, "ev": 0.0100}}

CAB = ["registrado_utc", "ativo", "vela_sinal", "vela_entrada", "hora_utc",
       "janela_boa", "entrada", "sl", "tp", "atr_pips", "spread_pips",
       "status", "vela_saida", "velas_ate_saida", "r_realizado"]


class Diario:
    def __init__(self, arq: Path):
        self.arq = arq
        self.arq.parent.mkdir(parents=True, exist_ok=True)
        if not self.arq.exists():
            with open(self.arq, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CAB)
        self._lock = threading.Lock()

    def gravar(self, d: dict) -> None:
        with self._lock:
            with open(self.arq, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([d.get(c, "") for c in CAB])

    def ler(self) -> pd.DataFrame:
        try:
            return pd.read_csv(self.arq)
        except Exception:
            return pd.DataFrame(columns=CAB)


class Estado:
    def __init__(self, pasta: Path, diario: Diario):
        self.pasta = pasta
        self.diario = diario
        self.status = "iniciando"
        self.abertos: list[dict] = []
        self.spreads: dict[str, float] = {}
        self._lock = threading.Lock()

    def _stats(self, d: pd.DataFrame) -> dict:
        d = d[d["status"].isin(["ganho", "perda"])]
        if d.empty:
            return {"n": 0}
        r = pd.to_numeric(d["r_realizado"], errors="coerce").dropna()
        if r.empty:
            return {"n": 0}
        n = len(r)
        g = int((d["status"] == "ganho").sum())
        # IC bootstrap: o R realizado e bimodal (+rr ou -1), nao normal
        rng = np.random.default_rng(7)
        if n >= 10:
            idx = rng.integers(0, n, size=(5000, n))
            m = r.to_numpy()[idx].mean(axis=1)
            lo, hi = float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))
        else:
            lo = hi = float("nan")
        return {"n": n, "ganhos": g, "wr": g / n, "ev": float(r.mean()),
                "total": float(r.sum()), "lo": lo, "hi": hi,
                "veredito": ("EDGE" if lo > 0 else
                             ("REPROVADO" if hi < 0 else "inconclusivo"))}

    def salvar(self) -> None:
        d = self.diario.ler()
        jb = d[d["janela_boa"].astype(str).isin(["True", "true", "1"])] \
            if not d.empty else d
        fora = d[~d.index.isin(jb.index)] if not d.empty else d
        with self._lock:
            payload = {
                "ts": int(time.time()), "status": self.status,
                "abertos": list(self.abertos), "spreads": dict(self.spreads),
                "geral": self._stats(d) if not d.empty else {"n": 0},
                "janela": self._stats(jb) if not jb.empty else {"n": 0},
                "fora": self._stats(fora) if not fora.empty else {"n": 0},
                "bt": BT, "sl_atr": SL_ATR, "tp_atr": TP_ATR,
                "ultimos": (d.tail(15).to_dict("records") if not d.empty else []),
            }
        try:
            arq = self.pasta / "forexfwd.json"
            tmp = arq.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                           encoding="utf-8")
            os.replace(tmp, arq)
        except Exception:
            pass


def ler_spread(api, ativo: str) -> float | None:
    try:
        c = api.get_realtime_candles(ativo, 60)
        if not c:
            return None
        u = c[sorted(c.keys())[-1]]
        b, a = u.get("bid"), u.get("ask")
        if b and a and a > b:
            return (a - b) / M.pip(ativo)
    except Exception:
        pass
    return None


def loop(api, cfg, estado: Estado, diario: Diario) -> None:
    hist: dict[str, pd.DataFrame] = {}
    for a in ATIVOS:
        df = backtest.carregar_cache(cfg, a)
        if df is not None and len(df) >= 600:
            hist[a] = df
            print(f"  {a}: {len(df)} velas")
    if not hist:
        print("Sem historico."); return

    for a in hist:
        try:
            api.start_candles_stream(a, 60, 1)
        except Exception:
            pass
    time.sleep(3)

    abertos: list[dict] = []
    vistos: set[str] = set()

    while True:
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

                sp = ler_spread(api, a)
                if sp is not None:
                    estado.spreads[a] = round(sp, 2)

                # ---- resolve trades abertos deste ativo ----
                for t in [x for x in abertos if x["ativo"] == a]:
                    ini = pd.Timestamp(t["vela_entrada"])
                    velas = df[df.index >= ini]
                    if velas.empty:
                        continue
                    spp = t["spread_pips"] * M.pip(a) if t["spread_pips"] else 0.0
                    fechou = None
                    for ts, lin in velas.iterrows():
                        n_v = len(df[(df.index >= ini) & (df.index <= ts)])
                        # B3: SL antes do TP quando os dois cabem na vela
                        if float(lin["Low"]) + spp <= t["sl"]:
                            fechou = ("perda", ts, n_v, -1.0); break
                        if float(lin["High"]) - spp >= t["tp"]:
                            rr = (t["tp"] - t["entrada"]) / (t["entrada"] - t["sl"])
                            fechou = ("ganho", ts, n_v, rr); break
                        if n_v >= MAX_VELAS:
                            fechou = ("abandonado", ts, n_v, 0.0); break
                    if fechou:
                        st, ts, n_v, r = fechou
                        t.update({"status": st, "vela_saida": str(ts),
                                  "velas_ate_saida": n_v,
                                  "r_realizado": round(r, 4)})
                        diario.gravar(t)
                        abertos.remove(t)
                        print(f"[SAIDA] {a} {st} R={r:+.2f} em {n_v} velas")

                # ---- novo sinal ----
                lo, _ = M.s_falso_rompimento(df, JAN)
                lo = lo.fillna(False)
                if not bool(lo.iloc[-1]):
                    continue
                ts_sinal = df.index[-1]
                chave = f"{a}:{ts_sinal}"
                if chave in vistos:
                    continue
                vistos.add(chave)

                atr = M.atr(df)
                av = float(atr.iloc[-1])
                if not np.isfinite(av) or av <= 0:
                    continue
                ent = float(df["Close"].iloc[-1])   # ~Open da proxima vela
                ts_ent = ts_sinal + pd.Timedelta(seconds=TF)
                hora = int(ts_ent.hour)
                t = {
                    "registrado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "ativo": a, "vela_sinal": str(ts_sinal),
                    "vela_entrada": str(ts_ent), "hora_utc": hora,
                    "janela_boa": hora in HORAS_JANELA,
                    "entrada": round(ent, 6),
                    "sl": round(ent - SL_ATR * av, 6),
                    "tp": round(ent + TP_ATR * av, 6),
                    "atr_pips": round(av / M.pip(a), 1),
                    "spread_pips": estado.spreads.get(a),
                    "status": "aberto", "vela_saida": "",
                    "velas_ate_saida": 0, "r_realizado": "",
                }
                abertos.append(t)
                marca = "JANELA" if t["janela_boa"] else f"{hora}h"
                print(f"[SINAL] {a} LONG {marca} ent={ent:.5f} "
                      f"sl={t['sl']:.5f} tp={t['tp']:.5f} sp={t['spread_pips']}p")
            except Exception as e:
                print(f"[{a}] {e!r}")

        estado.abertos = list(abertos)
        estado.status = f"OK — {datetime.now(timezone.utc):%H:%M:%S} UTC"
        estado.salvar()
        time.sleep(INTERVALO_S)


_HTML = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Forward Forex</title><style>
*{box-sizing:border-box}
body{font-family:ui-monospace,monospace;background:#0b1220;color:#e2e8f0;margin:0;padding:.9rem}
h1{color:#38bdf8;font-size:1.05rem;margin:0 0 .2rem}
.sub{color:#64748b;font-size:.72rem;margin-bottom:.6rem}
.nota{background:#0c2a3e;border-left:3px solid #0ea5e9;padding:.55rem .7rem;border-radius:.3rem;
  font-size:.72rem;color:#7dd3fc;margin:.5rem 0}
.sec{color:#94a3b8;font-size:.72rem;letter-spacing:.08em;margin:1rem 0 .35rem;font-weight:700}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.5rem}
.c{background:#151f33;border-radius:.4rem;padding:.6rem;border-left:3px solid #334155}
.c.ok{border-left-color:#22c55e}.c.bad{border-left-color:#ef4444}.c.warn{border-left-color:#f59e0b}
.c label{display:block;font-size:.62rem;color:#94a3b8}
.c span{font-size:1.15rem;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:.73rem}
th,td{text-align:left;padding:.3rem .45rem;border-bottom:1px solid #1e293b;white-space:nowrap}
th{color:#64748b;font-size:.66rem}
.up{color:#4ade80}.dn{color:#f87171}.ind{color:#475569}
.badge{padding:0 .3rem;border-radius:.2rem;font-size:.62rem;font-weight:700}
.b-ok{background:#22c55e;color:#052e1a}.b-no{background:#334155;color:#94a3b8}
.empty{color:#475569;font-style:italic;font-size:.8rem}
</style></head><body>
<h1>Forward Test Forex — Falso Rompimento LONG M15</h1>
<div class="sub" id="status">carregando...</div>
<div class="nota">
Registro em papel, nenhuma ordem enviada. SL <b id="slv">1.0</b> ATR &bull;
TP <b id="tpv">2.0</b> ATR.<br>
Registra <b>todas as horas</b> de proposito — assim o teste JULGA a hipotese de
que o edge esta em 21-22h UTC, em vez de assumi-la.<br>
Backtest de referencia: janela 21-22h EV <b>+0.5721R</b> (n=458) &bull;
todas as horas EV +0.0100R (n=4381).
</div>

<div class="sec">JANELA 21-22h UTC — a hipotese sob teste</div>
<div class="cards" id="janela"></div>

<div class="sec">FORA DA JANELA — controle</div>
<div class="cards" id="fora"></div>

<div class="sec">TRADES ABERTOS</div>
<div id="abertos"></div>

<div class="sec">SPREAD AO VIVO (pips)</div>
<div id="spreads"></div>

<div class="sec">ULTIMOS FECHADOS</div>
<div id="ultimos"></div>

<script>
const f=(x,d=2)=>x==null||isNaN(x)?'—':Number(x).toFixed(d);
const pc=x=>x==null||isNaN(x)?'—':(Number(x)*100).toFixed(1)+'%';

function cards(e,ref){
  if(!e||!e.n) return '<span class="empty">nenhum trade fechado ainda</span>';
  const cls=e.veredito==='EDGE'?'ok':(e.veredito==='REPROVADO'?'bad':'warn');
  return `<div class="c ${cls}"><label>trades</label><span>${e.n}</span></div>
   <div class="c"><label>win rate</label><span>${pc(e.wr)}</span></div>
   <div class="c ${e.ev>0?'ok':'bad'}"><label>EV por trade</label><span>${e.ev>0?'+':''}${f(e.ev,3)}R</span></div>
   <div class="c ${e.total>0?'ok':'bad'}"><label>total</label><span>${e.total>0?'+':''}${f(e.total,1)}R</span></div>
   <div class="c"><label>IC95 bootstrap</label><span style="font-size:.78rem">${f(e.lo,3)} a ${f(e.hi,3)}</span></div>
   <div class="c ${cls}"><label>veredito</label><span style="font-size:.85rem">${e.veredito}</span></div>
   <div class="c"><label>backtest esperava</label><span style="font-size:.85rem">${ref==null?'—':'+'+f(ref,3)+'R'}</span></div>`;
}

async function tick(){
 try{
  const d=await (await fetch('forexfwd.json?t='+Date.now())).json();
  document.getElementById('status').textContent='Atualizado: '+d.status;
  document.getElementById('slv').textContent=d.sl_atr;
  document.getElementById('tpv').textContent=d.tp_atr;
  document.getElementById('janela').innerHTML=cards(d.janela,d.bt?.janela?.ev);
  document.getElementById('fora').innerHTML=cards(d.fora,d.bt?.todas?.ev);

  const A=d.abertos||[];
  document.getElementById('abertos').innerHTML=A.length
   ? '<table><tr><th>ativo</th><th>entrada em</th><th>janela</th><th>entrada</th><th>SL</th><th>TP</th><th>ATR</th><th>spread</th></tr>'
     +A.map(t=>`<tr><td><b>${t.ativo}</b></td><td>${(t.vela_entrada||'').slice(5,16)}</td>
     <td>${t.janela_boa?'<span class="badge b-ok">21-22h</span>':'<span class="badge b-no">'+t.hora_utc+'h</span>'}</td>
     <td>${t.entrada}</td><td class="dn">${t.sl}</td><td class="up">${t.tp}</td>
     <td>${t.atr_pips}p</td><td>${t.spread_pips??'—'}p</td></tr>`).join('')+'</table>'
   : '<span class="empty">nenhum trade aberto</span>';

  const S=d.spreads||{}, ks=Object.keys(S).sort();
  document.getElementById('spreads').innerHTML=ks.length
   ? '<table><tr>'+ks.map(k=>`<th>${k}</th>`).join('')+'</tr><tr>'
     +ks.map(k=>`<td class="${S[k]<=1?'up':(S[k]<=2?'':'dn')}">${f(S[k])}</td>`).join('')+'</tr></table>'
   : '<span class="empty">—</span>';

  const U=d.ultimos||[];
  document.getElementById('ultimos').innerHTML=U.length
   ? '<table><tr><th>ativo</th><th>entrada</th><th>janela</th><th>status</th><th>R</th><th>velas</th><th>spread</th></tr>'
     +U.slice().reverse().map(t=>`<tr><td><b>${t.ativo}</b></td>
     <td>${(t.vela_entrada||'').slice(5,16)}</td>
     <td>${String(t.janela_boa)==='True'?'<span class="badge b-ok">21-22h</span>':t.hora_utc+'h'}</td>
     <td class="${t.status==='ganho'?'up':(t.status==='perda'?'dn':'ind')}">${t.status}</td>
     <td class="${t.r_realizado>0?'up':'dn'}">${t.r_realizado>0?'+':''}${f(t.r_realizado)}</td>
     <td>${t.velas_ate_saida}</td><td>${t.spread_pips??'—'}p</td></tr>`).join('')+'</table>'
   : '<span class="empty">nenhum ainda</span>';
 }catch(e){document.getElementById('status').textContent='Erro: '+e;}
}
tick(); setInterval(tick,20000);
</script></body></html>"""


def main() -> int:
    import dataclasses
    base = configuracao_scalping_m15()
    cfg = dataclasses.replace(base, timeframe_segundos=TF)
    print("Conectando na IQ Option...")
    api = MercadoIQ(base).conectar_somente_leitura()

    g = GraficoM5(dataclasses.replace(base, porta_grafico=PORTA,
                                      sufixo_banco="forex_fwd"))
    g.iniciar(abrir_navegador=False)
    (g.pasta_web / "index.html").write_text(_HTML, encoding="utf-8")

    diario = Diario(Path(__file__).resolve().parent / "diario" / "forex_forward.csv")
    print(f"Diario: {diario.arq}")
    estado = Estado(g.pasta_web, diario)
    estado.salvar()

    url = f"http://127.0.0.1:{PORTA}/index.html"
    print(f"Painel: {url}")
    webbrowser.open(url)
    print(f"\nCarregando {len(ATIVOS)} pares...")
    try:
        loop(api, cfg, estado, diario)
    except KeyboardInterrupt:
        print("\nParado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
