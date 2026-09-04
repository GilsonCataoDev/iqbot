"""Forward test BINARIO — falso rompimento LONG no M15.

OBJETIVO: resolver se o WR verdadeiro e ~56.0% (estimativa) ou ~54.2%
(limite pessimista do IC). O backtest nao consegue: a margem sobre o
breakeven e de 0.11pp e 12 meses de dados nao bastam. So dados NOVOS
resolvem.

O QUE ESTE BOT FAZ:
  - Detecta o sinal com a MESMA funcao do backtest (motor_sinais), para
    que os numeros sejam comparaveis. Carrega o cache historico primeiro
    porque o limiar de acumulacao usa expanding(min_periods=500).
  - Registra o PAYOUT REAL no momento do sinal (nao o 0.85 assumido).
    Essa era a maior incerteza: a 56% de WR basta payout > 78.5%, mas no
    pior caso do IC precisa de > 84.6%.
  - Mede o SLIPPAGE: compara o preco no instante da deteccao com o Open
    da vela seguinte, que o backtest assumia como entrada.
  - NAO envia ordem. E registro em papel — risco zero e mede exatamente
    a premissa do backtest. Execucao real pode ser ligada depois.

Uso:
    MONITOR_BINARIA.bat
"""
from __future__ import annotations

import csv
import json
import math
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

# 7 originais (onde o sinal foi encontrado) + 7 crosses G10 que passaram no
# teste out-of-sample de 31/08/2026 (WR 56.17% sem reotimizar nada).
FOREX = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURJPY", "USDCAD", "NZDUSD",
         "EURGBP", "EURCHF", "EURCAD", "GBPJPY", "GBPCHF", "GBPCAD", "CADCHF"]

# Commodities — out-of-sample em CLASSE DE ATIVO diferente (01/09/2026).
# Combinado n=1244 WR=55.55% IC=[52.42%,58.54%]: consistente com os 56.11% do
# forex mas SEM forca estatistica propria (amostra 6x menor). Payout 0.87, o
# melhor da lista. Adicionam so ~4 sinais/dia.
# NAO tratar isoladamente: USOUSD deu 60.08% mas e o melhor de 4 — selecionar
# por isso e o mesmo data snooping que ja falhou no teste de selecao de pares.
# XAUUSD e o unico dos 18 ativos com EV negativo (53.16%, BE 53.48%).
COMMODITIES = ["XAUUSD", "XAGUSD", "UKOUSD", "USOUSD"]

# Crypto — out-of-sample em classe de ativo, e o melhor resultado dos tres
# blocos (01/09/2026): combinado n=2307 n_ef=1554 WR=56.87% IC=[54.41%,59.33%].
# ETHUSD passa sozinho (59.57%, EDGE). XRPUSD paga so 0.84 (breakeven 54.35%),
# margem fina.
# VANTAGEM PRATICA: opera 24/7. Dos 2307 sinais, 971 caem no fim de semana
# (WR 59.84%) — horas em que forex e commodities estao fechados. Isso preenche
# o vazio do sabado/domingo no forward test.
CRYPTO = ["BTCUSD", "ETHUSD", "XRPUSD"]

ATIVOS = FOREX + COMMODITIES + CRYPTO
TF = 900
PORTA = 8776
INTERVALO_S = 20
# n necessario, calculado em 31/08/2026 — NAO e 250 (erro de fator ~10 na 1a
# estimativa). O retorno binario e bimodal (+payout ou -1), sd ~ 0.92, e a media
# a distinguir de zero e ~+0.039u:
#     n = (1.96 * 0.92 / 0.039)^2 ~ 2100   (IC95 sair de zero)
#     n ~ 3400                             (com 80% de poder)
# Verificado por simulacao: nem n=600 decide em nenhum dos dois cenarios.
META_TRADES = 2100
JAN = 10

# Referencia do backtest, para comparar ao vivo
BT_WR = 0.5603
BT_IC = (0.5416, 0.5791)


# ---------------------------------------------------------------------------
# Payout real
# ---------------------------------------------------------------------------

def _payout_de(detalhe: dict) -> float | None:
    op = detalhe.get("option")
    if not isinstance(op, dict):
        return None
    lucro = op.get("profit")
    if not isinstance(lucro, dict):
        return None
    com = lucro.get("commission")
    if com is None:
        return None
    try:
        return round(1.0 - float(com) / 100.0, 4)
    except (TypeError, ValueError):
        return None


class ColetorSpread:
    """Registra bid/ask reais por hora, para os pares forex.

    Motivo: o backtest de forex assume spread FIXO (1.2 pips nos majors). O
    edge de 21-22h UTC depende disso — ele sobrevive ate ~3.5x o assumido e
    morre acima de ~4.2 pips no EURUSD. Como 21-22h e justamente a hora de
    liquidez mais fina, o spread real e a variavel que decide, e e a que o
    modelo trata pior. Aqui medimos em vez de supor.

    O stream de candles em tempo real (`get_realtime_candles`) traz `ask` e
    `bid` — os candles historicos nao trazem.
    """

    CAB = ["ts_utc", "hora_utc", "ativo", "bid", "ask", "spread_pips", "mid"]

    def __init__(self, api, ativos: list[str], arq: Path):
        self._api = api
        self._ativos = ativos
        self.arq = arq
        self.arq.parent.mkdir(parents=True, exist_ok=True)
        if not self.arq.exists():
            with open(self.arq, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.CAB)
        self._streams_ok = False
        self._lock = threading.Lock()
        self.ultimo: dict[str, float] = {}

    def _garantir_streams(self) -> None:
        if self._streams_ok:
            return
        for a in self._ativos:
            try:
                self._api.start_candles_stream(a, 60, 1)
            except Exception as e:
                print(f"[SPREAD] stream {a}: {e!r}")
        time.sleep(3)
        self._streams_ok = True

    def coletar(self) -> None:
        self._garantir_streams()
        agora = datetime.now(timezone.utc)
        linhas = []
        for a in self._ativos:
            try:
                c = self._api.get_realtime_candles(a, 60)
                if not c:
                    continue
                ult = c[sorted(c.keys())[-1]]
                bid, ask = ult.get("bid"), ult.get("ask")
                if not bid or not ask or ask <= bid:
                    continue
                sp_pips = (ask - bid) / M.pip(a)
                mid = (ask + bid) / 2
                linhas.append([agora.isoformat(timespec="seconds"), agora.hour, a,
                               round(bid, 5), round(ask, 5), round(sp_pips, 3),
                               round(mid, 5)])
                with self._lock:
                    self.ultimo[a] = sp_pips
            except Exception:
                continue
        if linhas:
            with self._lock:
                with open(self.arq, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerows(linhas)


def ler_payouts(api) -> dict[str, float]:
    """Payout real por ativo, via get_all_init_v2 (o v1 trava 30s)."""
    out: dict[str, float] = {}
    try:
        dados = api.get_all_init_v2()
        if not isinstance(dados, dict):
            return out
        por_secao: dict[str, dict[str, float]] = {"turbo": {}, "binary": {}}
        for secao in ("turbo", "binary"):
            parte = dados.get(secao, {}).get("actives", {})
            if not isinstance(parte, dict):
                continue
            for det in parte.values():
                if not isinstance(det, dict):
                    continue
                nome = str(det.get("name", "")).split(".", 1)[-1].upper()
                if nome.endswith("-OP"):
                    nome = nome[:-3]
                v = _payout_de(det)
                if v is not None:
                    por_secao[secao][nome] = v
        for a in ATIVOS:
            v = por_secao["turbo"].get(a) or por_secao["binary"].get(a)
            if v is not None:
                out[a] = v
    except Exception as e:
        print(f"[PAYOUT] erro: {e!r}")
    return out


# ---------------------------------------------------------------------------
# Diario
# ---------------------------------------------------------------------------

CAB = ["registrado_utc", "ativo", "candle_sinal", "candle_entrada",
       "preco_deteccao", "abertura_entrada", "fechamento_entrada",
       "slippage_pips", "payout_real", "resultado", "retorno_u"]


class Diario:
    def __init__(self, arq: Path):
        self.arq = arq
        self.arq.parent.mkdir(parents=True, exist_ok=True)
        if not self.arq.exists():
            with open(self.arq, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CAB)
        self._lock = threading.Lock()

    def gravar(self, linha: dict) -> None:
        with self._lock:
            with open(self.arq, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([linha.get(c, "") for c in CAB])

    def ler(self) -> pd.DataFrame:
        try:
            return pd.read_csv(self.arq)
        except Exception:
            return pd.DataFrame(columns=CAB)


# ---------------------------------------------------------------------------
# Estado / estatisticas
# ---------------------------------------------------------------------------

class Estado:
    def __init__(self, pasta_web: Path, diario: Diario):
        self.pasta = pasta_web
        self.diario = diario
        self.status = "iniciando"
        self.payouts: dict[str, float] = {}
        self.pendentes: list[dict] = []
        self._lock = threading.Lock()

    def salvar(self) -> None:
        d = self.diario.ler()
        res = d[d["resultado"].isin(["ganho", "perda"])] if not d.empty else d

        est = {"n": 0}
        if not res.empty:
            n = len(res)
            w = int((res["resultado"] == "ganho").sum())
            wr = w / n
            lo, hi = M.wilson(w, n)
            pay = pd.to_numeric(res["payout_real"], errors="coerce").fillna(0.85)
            # EV real: cada ganho rende o payout DAQUELE trade
            ret = pd.to_numeric(res["retorno_u"], errors="coerce").fillna(0.0)
            total_u = float(ret.sum())
            pay_med = float(pay.mean())
            be = 1.0 / (1.0 + pay_med)
            # payout minimo necessario para o WR observado e para o pior caso
            need_wr = (1 - wr) / wr if wr > 0 else 9.99
            need_lo = (1 - lo) / lo if lo > 0 else 9.99
            slip = pd.to_numeric(res["slippage_pips"], errors="coerce").dropna()
            est = {
                "n": n, "ganhos": w, "wr": wr, "lo": lo, "hi": hi,
                "payout_medio": pay_med, "breakeven": be,
                "total_u": total_u, "ev_u": total_u / n,
                "payout_necessario": need_wr, "payout_necessario_pessimista": need_lo,
                "slippage_med": float(slip.median()) if len(slip) else 0.0,
                "meta": META_TRADES, "progresso": min(1.0, n / META_TRADES),
                "veredito": ("LUCRATIVO" if lo > be else
                             ("REPROVADO" if hi < be else "inconclusivo")),
            }

        with self._lock:
            payload = {
                "ts": int(time.time()),
                "status": self.status,
                "payouts": dict(self.payouts),
                "pendentes": list(self.pendentes),
                "est": est,
                "backtest": {"wr": BT_WR, "ic": list(BT_IC)},
                "ultimos": (res.tail(12).to_dict("records") if not res.empty else []),
            }
        try:
            arq = self.pasta / "binaria.json"
            tmp = arq.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                           encoding="utf-8")
            os.replace(tmp, arq)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

def loop_spread(coletor: ColetorSpread, intervalo_s: int = 120) -> None:
    """Amostra bid/ask a cada 2min, 24h por dia — precisamos do perfil por hora."""
    while True:
        try:
            coletor.coletar()
        except Exception as e:
            print(f"[SPREAD] erro: {e!r}")
        time.sleep(intervalo_s)


def loop(api, cfg, estado: Estado, diario: Diario) -> None:
    # Historico base: necessario para o expanding(min_periods=500) bater com o backtest
    hist: dict[str, pd.DataFrame] = {}
    for a in ATIVOS:
        df = backtest.carregar_cache(cfg, a)
        if df is None or len(df) < 600:
            print(f"  {a}: cache insuficiente — pulando este par.")
            continue
        hist[a] = df
        print(f"  {a}: {len(df)} velas de base")
    if not hist:
        print("Sem historico. Rode BAIXAR_HISTORICO.bat antes.")
        return

    vistos: dict[str, pd.Timestamp] = {}   # ultimo candle de sinal por ativo
    pendentes: list[dict] = []

    while True:
        try:
            estado.payouts = ler_payouts(api) or estado.payouts
        except Exception:
            pass

        for a in list(hist.keys()):
            try:
                novo = backtest.baixar_historico(api, cfg, a, 60)
                if novo is None or novo.empty:
                    continue
                # Descarta a ultima vela: pode estar parcial
                novo = novo.iloc[:-1]
                if novo.empty:
                    continue

                base = hist[a]
                df = pd.concat([base[~base.index.isin(novo.index)], novo]).sort_index()
                hist[a] = df

                # ---- resolve pendentes deste ativo ----
                for p in [x for x in pendentes if x["ativo"] == a]:
                    ts_ent = pd.Timestamp(p["candle_entrada"])
                    if ts_ent not in df.index:
                        continue
                    linha = df.loc[ts_ent]
                    op, cl = float(linha["Open"]), float(linha["Close"])
                    if cl == op:
                        pendentes.remove(p)
                        continue                      # empate: descarta (como no backtest)
                    ganhou = cl > op                  # LONG
                    pay = float(p["payout_real"])
                    reg = {
                        "registrado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "ativo": a,
                        "candle_sinal": p["candle_sinal"],
                        "candle_entrada": p["candle_entrada"],
                        "preco_deteccao": p["preco_deteccao"],
                        "abertura_entrada": round(op, 5),
                        "fechamento_entrada": round(cl, 5),
                        "slippage_pips": round((p["preco_deteccao"] - op) / M.pip(a), 2),
                        "payout_real": pay,
                        "resultado": "ganho" if ganhou else "perda",
                        "retorno_u": round(pay if ganhou else -1.0, 4),
                    }
                    diario.gravar(reg)
                    pendentes.remove(p)
                    print(f"[RESULTADO] {a} {'GANHO' if ganhou else 'perda'} "
                          f"payout={pay:.2f} ret={reg['retorno_u']:+.2f}u")

                # ---- detecta novo sinal ----
                lo, _sh = M.s_falso_rompimento(df, JAN)   # LONG = falso rompimento p/ baixo
                lo = lo.fillna(False)
                if not bool(lo.iloc[-1]):
                    continue
                ts_sinal = df.index[-1]
                if vistos.get(a) == ts_sinal:
                    continue
                vistos[a] = ts_sinal

                pay = estado.payouts.get(a)
                if pay is None:
                    print(f"[SINAL] {a} ignorado: payout indisponivel")
                    continue

                preco = float(df["Close"].iloc[-1])
                ts_ent = ts_sinal + pd.Timedelta(seconds=TF)
                pendentes.append({
                    "ativo": a,
                    "candle_sinal": str(ts_sinal),
                    "candle_entrada": str(ts_ent),
                    "preco_deteccao": round(preco, 5),
                    "payout_real": pay,
                })
                need = (1 - BT_WR) / BT_WR
                ok = "OK" if pay > need else "PAYOUT BAIXO"
                print(f"[SINAL] {a} LONG @ {ts_sinal} | preco={preco:.5f} "
                      f"payout={pay:.2f} (min {need:.3f}) {ok}")

            except Exception as e:
                print(f"[{a}] erro: {e!r}")

        estado.pendentes = list(pendentes)
        estado.status = f"OK — {datetime.now(timezone.utc):%H:%M:%S} UTC"
        estado.salvar()
        time.sleep(INTERVALO_S)


# ---------------------------------------------------------------------------
# Pagina
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Forward Test Binaria</title>
<style>
*{box-sizing:border-box}
body{font-family:ui-monospace,monospace;background:#0b1220;color:#e2e8f0;margin:0;padding:1rem}
h1{color:#38bdf8;font-size:1.1rem;margin:0 0 .2rem}
.sub{color:#64748b;font-size:.75rem;margin-bottom:1rem}
.sec{color:#94a3b8;font-size:.75rem;letter-spacing:.08em;margin:1rem 0 .4rem;font-weight:700}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.5rem}
.c{background:#151f33;border-radius:.4rem;padding:.7rem;border-left:3px solid #334155}
.c label{display:block;font-size:.65rem;color:#94a3b8;margin-bottom:.2rem}
.c span{font-size:1.25rem;font-weight:700}
.ok{border-left-color:#22c55e}.bad{border-left-color:#ef4444}.warn{border-left-color:#f59e0b}
.bar{height:8px;background:#1e293b;border-radius:4px;overflow:hidden;margin:.4rem 0}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#38bdf8,#22c55e)}
table{width:100%;border-collapse:collapse;font-size:.75rem}
th,td{text-align:left;padding:.3rem .5rem;border-bottom:1px solid #1e293b}
th{color:#64748b;font-weight:600}
.g{color:#4ade80}.p{color:#f87171}
.empty{color:#475569;font-style:italic;font-size:.8rem}
.note{background:#0c2a3e;border-left:3px solid #0ea5e9;padding:.6rem;border-radius:.3rem;
      font-size:.72rem;color:#7dd3fc;margin:.5rem 0}
</style></head><body>
<h1>Forward Test — Falso Rompimento LONG M15 (binaria)</h1>
<div class="sub" id="status">carregando...</div>

<div class="note">
Registro em papel: o bot NAO envia ordem. Ele grava o sinal, o payout real
daquele instante e o resultado da vela seguinte. Backtest: WR 56.03%
IC [54.16%, 57.91%] — a meta e ver de que lado o WR real cai.
</div>

<div class="sec">RESULTADO ACUMULADO</div>
<div class="cards" id="stats"></div>
<div class="bar"><i id="prog" style="width:0%"></i></div>
<div class="sub" id="progtxt"></div>

<div class="sec">SINAIS AGUARDANDO RESOLUCAO</div>
<div id="pend"><span class="empty">nenhum</span></div>

<div class="sec">PAYOUT AO VIVO</div>
<div id="pay"><span class="empty">—</span></div>

<div class="sec">ULTIMOS RESULTADOS</div>
<div id="ult"><span class="empty">nenhum ainda</span></div>

<script>
const f=(x,d=2)=>x==null||isNaN(x)?'—':Number(x).toFixed(d);
const pc=(x,d=2)=>x==null||isNaN(x)?'—':(Number(x)*100).toFixed(d)+'%';

async function tick(){
 try{
  const r=await fetch('binaria.json?t='+Date.now()); const d=await r.json();
  document.getElementById('status').textContent='Atualizado: '+d.status;
  const e=d.est||{};

  if(!e.n){
    document.getElementById('stats').innerHTML='<span class="empty">Nenhum trade resolvido ainda.</span>';
  }else{
    const cls=e.veredito==='LUCRATIVO'?'ok':(e.veredito==='REPROVADO'?'bad':'warn');
    document.getElementById('stats').innerHTML=`
     <div class="c ${cls}"><label>Win rate</label><span>${pc(e.wr)}</span></div>
     <div class="c"><label>IC95</label><span style="font-size:.9rem">${pc(e.lo)} – ${pc(e.hi)}</span></div>
     <div class="c"><label>Trades</label><span>${e.n}</span></div>
     <div class="c"><label>Payout medio</label><span>${pc(e.payout_medio)}</span></div>
     <div class="c"><label>Breakeven</label><span>${pc(e.breakeven)}</span></div>
     <div class="c ${e.ev_u>0?'ok':'bad'}"><label>EV por trade</label><span>${e.ev_u>0?'+':''}${f(e.ev_u,4)}u</span></div>
     <div class="c ${e.total_u>0?'ok':'bad'}"><label>Total</label><span>${e.total_u>0?'+':''}${f(e.total_u,1)}u</span></div>
     <div class="c"><label>Slippage med.</label><span>${f(e.slippage_med,2)}p</span></div>
     <div class="c"><label>Payout necessario</label><span>${pc(e.payout_necessario)}</span></div>
     <div class="c ${cls}"><label>Veredito</label><span style="font-size:.95rem">${e.veredito}</span></div>`;
    document.getElementById('prog').style.width=(e.progresso*100)+'%';
    document.getElementById('progtxt').textContent=
      `${e.n} de ${e.meta} trades — no payout medio atual, precisa de WR > ${pc(e.breakeven)}`;
  }

  const p=d.pendentes||[];
  document.getElementById('pend').innerHTML = p.length? '<table><tr><th>par</th><th>vela do sinal</th><th>preco</th><th>payout</th></tr>'+
    p.map(x=>`<tr><td>${x.ativo}</td><td>${x.candle_sinal}</td><td>${x.preco_deteccao}</td><td>${pc(x.payout_real)}</td></tr>`).join('')+'</table>'
    : '<span class="empty">nenhum</span>';

  const py=d.payouts||{};
  const ks=Object.keys(py);
  document.getElementById('pay').innerHTML = ks.length? '<table><tr>'+ks.map(k=>`<th>${k}</th>`).join('')+'</tr><tr>'+
    ks.map(k=>{const v=py[k];const c=v>=0.85?'g':(v>=0.80?'':'p');return `<td class="${c}">${pc(v)}</td>`}).join('')+'</tr></table>'
    : '<span class="empty">—</span>';

  const u=d.ultimos||[];
  document.getElementById('ult').innerHTML = u.length? '<table><tr><th>par</th><th>vela</th><th>res</th><th>payout</th><th>retorno</th><th>slip</th></tr>'+
    u.slice().reverse().map(x=>`<tr><td>${x.ativo}</td><td>${x.candle_entrada}</td>
      <td class="${x.resultado==='ganho'?'g':'p'}">${x.resultado}</td>
      <td>${pc(x.payout_real)}</td>
      <td class="${x.retorno_u>0?'g':'p'}">${x.retorno_u>0?'+':''}${f(x.retorno_u,2)}u</td>
      <td>${f(x.slippage_pips,2)}p</td></tr>`).join('')+'</table>'
    : '<span class="empty">nenhum ainda</span>';
 }catch(err){document.getElementById('status').textContent='Erro: '+err;}
}
tick(); setInterval(tick,15000);
</script></body></html>"""


def main() -> int:
    import dataclasses
    base = configuracao_scalping_m15()
    cfg = dataclasses.replace(base, timeframe_segundos=TF)

    print("Conectando na IQ Option...")
    api = MercadoIQ(base).conectar_somente_leitura()

    cfg_g = dataclasses.replace(base, porta_grafico=PORTA, sufixo_banco="binaria_fwd")
    g = GraficoM5(cfg_g)
    g.iniciar(abrir_navegador=False)
    (g.pasta_web / "index.html").write_text(_HTML, encoding="utf-8")

    pasta_diario = Path(__file__).resolve().parent / "diario"
    diario = Diario(pasta_diario / "binaria_forward.csv")
    print(f"Diario: {diario.arq}")

    estado = Estado(g.pasta_web, diario)
    estado.salvar()

    # Coleta de spread real — so nos pares forex (commodities e binaria pura aqui)
    coletor = ColetorSpread(api, FOREX, pasta_diario / "spread_real.csv")
    print(f"Spread: {coletor.arq}")
    threading.Thread(target=loop_spread, args=(coletor,),
                     name="spread", daemon=True).start()

    url = f"http://127.0.0.1:{PORTA}/index.html"
    print(f"Painel: {url}")
    webbrowser.open(url)

    print(f"\nCarregando historico base ({len(ATIVOS)} pares)...")
    try:
        loop(api, cfg, estado, diario)
    except KeyboardInterrupt:
        print("\nParado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
