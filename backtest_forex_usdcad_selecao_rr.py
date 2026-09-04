"""Escolhe R:R do pullback USDCAD somente no treino e congela no holdout."""
from __future__ import annotations
from iqoption_m5 import backtest
from iqoption_m5.config import configuracao_scalping_m15
from iqoption_m5.forex_backtest import simular_forex

def rodar(candles, rr, spread=.00012):
    return simular_forex("USDCAD",candles,banca=10_000,risco_percentual=.0025,spread=spread,
        estrategia="pullback_h1_nova_york",parametros_estrategia={"retorno_risco":rr})[0]
def ev(r,rr):
    return (((r.lucro>0).sum()*rr-(r.lucro<=0).sum())/len(r)) if len(r) else -999
def linha(nome,r,rr):
    wr=(r.lucro>0).mean() if len(r) else 0
    print(f"  {nome:18} n={len(r):<3} WR={wr:.1%} EV={ev(r,rr):+.3f}R")
def main():
    c=backtest.carregar_cache(configuracao_scalping_m15(),"USDCAD").tail(26_000).copy()
    corte=int(len(c)*.70); treino=c.iloc[:corte]; holdout=c.iloc[max(0,corte-220):]
    candidatos=[1.0,1.5,2.0]; resultados=[]
    print("\nUSDCAD pullback NY | selecao de R:R apenas no treino")
    for rr in candidatos:
        r=rodar(treino,rr); resultados.append((ev(r,rr),rr)); linha(f"treino RR={rr}",r,rr)
    _,escolhido=max(resultados); print(f"  R:R congelado: {escolhido}")
    h=rodar(holdout,escolhido); linha("holdout",h,escolhido)
    linha("holdout spread1,5",rodar(holdout,escolhido,.00018),escolhido)
    linha("holdout spread2,0",rodar(holdout,escolhido,.00024),escolhido)
    ok=len(h)>=50 and ev(h,escolhido)>.10
    print("DECISAO:","APROVADA PARA OBSERVACAO" if ok else "INCONCLUSIVA/REPROVADA")
    return 0 if ok else 2
if __name__=="__main__":raise SystemExit(main())
