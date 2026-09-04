"""Valida a Second Entry EMA21 com regra objetiva, H1 e alvo 1R."""
from __future__ import annotations
import pandas as pd
from backtest_forex_rompimento_reteste_h1 import ATIVOS,SPREAD,SPREAD_PADRAO
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex
def metricas(r):
    n=len(r);w=int((r.lucro>0).sum()) if n else 0;return n,w/n if n else 0,(w-(n-w))/n if n else -999
def mostrar(nome,r):
    n,wr,ev=metricas(r);print(f"  {nome:22} n={n:<4} WR={wr:.1%} EV={ev:+.3f}R");return n,wr,ev
def executar(dados,fator=1):
    partes=[]
    for a,c in dados.items():
        r,_=simular_forex(a,c,banca=10_000,risco_percentual=.0025,spread=SPREAD.get(a,SPREAD_PADRAO)*fator,estrategia="segunda_entrada_ema21")
        if not r.empty:partes.append(r)
    return pd.concat(partes,ignore_index=True) if partes else pd.DataFrame()
def main():
    cfg=configuracao_scalping_m15();dados={a:backtest.carregar_cache(cfg,a).tail(26_000).copy() for a in ATIVOS}
    base,s15,s20=executar(dados),executar(dados,1.5),executar(dados,2)
    print("\nSecond Entry EMA21 | M15 + tendencia H1 | TP=1R")
    if base.empty:return 2
    for a in ATIVOS:mostrar(a,base[base.ativo==a])
    o=base.sort_values("aberta_em");c=o.iloc[int(len(o)*.7)].aberta_em
    mostrar("treino 70%",o[o.aberta_em<c]);nh,_,eh=mostrar("holdout 30%",o[o.aberta_em>=c]);_,_,e15=mostrar("spread 1,5x",s15);_,_,e20=mostrar("spread 2,0x",s20)
    ok=nh>=50 and eh>.08 and e15>.03 and e20>0
    print("DECISAO:","APROVADA PARA OBSERVACAO" if ok else "NAO APROVADA");return 0 if ok else 2
if __name__=="__main__":raise SystemExit(main())
