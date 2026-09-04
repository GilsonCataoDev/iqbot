"""Valida RSI14 + Bollinger20/2 + z-score 1,25 no Forex M15."""
from __future__ import annotations
import pandas as pd
from backtest_forex_rompimento_reteste_h1 import ATIVOS,SPREAD,SPREAD_PADRAO
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex
def metricas(r):
    n=len(r);wr=(r.lucro>0).mean() if n else 0;g=r.loc[r.lucro>0,"lucro"].sum();p=-r.loc[r.lucro<0,"lucro"].sum();pf=g/p if p>0 else 0
    return n,wr,pf,r.lucro.sum()
def mostrar(nome,r):
    n,wr,pf,pnl=metricas(r);print(f"  {nome:22} n={n:<4} WR={wr:.1%} PF={pf:.2f} PnL={pnl:+.2f}");return n,wr,pf,pnl
def executar(dados,fator=1):
    partes=[]
    for a,c in dados.items():
        r,_=simular_forex(a,c,banca=10_000,risco_percentual=.0025,spread=SPREAD.get(a,SPREAD_PADRAO)*fator,estrategia="reversao_media_rsi_bollinger")
        if not r.empty:partes.append(r)
    return pd.concat(partes,ignore_index=True) if partes else pd.DataFrame()
def main():
    cfg=configuracao_scalping_m15();dados={a:backtest.carregar_cache(cfg,a).tail(26_000).copy() for a in ATIVOS}
    base,s15,s20=executar(dados),executar(dados,1.5),executar(dados,2)
    print("\nMean Reversion | RSI14 + BB20/2 + z1,25 | M15")
    if base.empty:return 2
    for a in ATIVOS:mostrar(a,base[base.ativo==a])
    o=base.sort_values("aberta_em");c=o.iloc[int(len(o)*.7)].aberta_em
    mostrar("treino 70%",o[o.aberta_em<c]);nh,_,pfh,_=mostrar("holdout 30%",o[o.aberta_em>=c]);_,_,pf15,_=mostrar("spread 1,5x",s15);_,_,pf20,_=mostrar("spread 2,0x",s20)
    ok=nh>=50 and pfh>1.2 and pf15>1.05 and pf20>1
    print("DECISAO:","APROVADA PARA OBSERVACAO" if ok else "NAO APROVADA");return 0 if ok else 2
if __name__=="__main__":raise SystemExit(main())
