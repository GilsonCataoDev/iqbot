import numpy as np
import pandas as pd
from iqoption_m5.forex_estrategia import planos_reversao_media_rsi_bollinger

def test_mean_reversion_nao_muda_passado_com_futuro():
    rng=np.random.default_rng(91);close=1.10+np.cumsum(rng.normal(0,.0003,500));abertura=np.r_[close[0],close[:-1]]
    idx=pd.date_range("2026-01-01",periods=500,freq="15min")
    c=pd.DataFrame({"Open":abertura,"High":np.maximum(abertura,close)+.00015,"Low":np.minimum(abertura,close)-.00015,"Close":close,"Volume":1},index=idx)
    a=planos_reversao_media_rsi_bollinger("EURUSD",c.iloc[:350]);m=c.copy();m.iloc[350:,:4]*=1.5
    d=planos_reversao_media_rsi_bollinger("EURUSD",m).iloc[:350]
    chave=lambda p:None if p is None else(p.lado,p.sinal_em,round(p.stop,8))
    assert [chave(x) for x in a]==[chave(x) for x in d]
