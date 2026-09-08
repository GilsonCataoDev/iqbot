"""Dos sinais ema920_pullback executados, quantos foram contra o H1?

O perfil real roda com filtro_h1_ativo=False: a estrategia exige tendencia no
timeframe de operacao (EMA9>EMA20 com as duas inclinadas), mas nada bloqueia
uma entrada que va contra o H1. Este script mede se isso custa alguma coisa.

Cada base sai separada de proposito — sao perfis e timeframes diferentes, e
juntar as amostras produziria um numero que nao corresponde a estrategia
nenhuma. Nao conecta na corretora; le apenas os bancos e o historico gravado.

    python analisar_contra_h1.py
"""
import glob, os, sqlite3, collections
import pandas as pd
from iqoption_m5.perfil_movimento import wilson

HIST = "iqoption_m5/dados/historico"

def h1_de(ativo):
    """H1 remontado do M15 gravado; usa o 3600s quando existe."""
    for arq, resample in ((f"{HIST}/{ativo}_3600s.csv", False),
                          (f"{HIST}/{ativo}_900s.csv", True)):
        if not os.path.exists(arq): continue
        d = pd.read_csv(arq, parse_dates=["timestamp"]).set_index("timestamp").sort_index()
        if resample:
            d = d.resample("1h").agg({"Open":"first","High":"max","Low":"min","Close":"last"}).dropna()
        e9 = d.Close.ewm(span=9, adjust=False).mean()
        e20 = d.Close.ewm(span=20, adjust=False).mean()
        return pd.DataFrame({"e9": e9, "e20": e20})
    return None

caches = {}
def tendencia_h1(ativo, quando):
    if ativo not in caches: caches[ativo] = h1_de(ativo)
    d = caches[ativo]
    if d is None: return None
    ant = d.loc[:quando]
    if ant.empty: return None
    # A vela H1 corrente ainda nao fechou: usa a ultima FECHADA.
    if len(ant) < 2: return None
    r = ant.iloc[-2]
    return "alta" if r.e9 > r.e20 else "baixa" if r.e9 < r.e20 else None

for base in sorted(glob.glob("iqoption_m5/dados/*.sqlite3")):
    try:
        c = sqlite3.connect(f"file:{base}?mode=ro", uri=True); c.row_factory = sqlite3.Row
        if not c.execute("select name from sqlite_master where type='table' and name='operacoes'").fetchone():
            continue
        ops = list(c.execute("""select ativo,direcao,enviada_em,lucro,timeframe from operacoes
                                where setup='ema920_pullback' and status='finalizada'
                                and lucro is not null"""))
    except Exception: continue
    if not ops: continue
    grupos = collections.defaultdict(lambda: [0, 0])   # [n, wins]
    sem_h1 = 0
    for o in ops:
        t = tendencia_h1(o["ativo"], pd.Timestamp(o["enviada_em"]))
        if t is None: sem_h1 += 1; continue
        favor = (o["direcao"] == "call" and t == "alta") or (o["direcao"] == "put" and t == "baixa")
        g = grupos["a favor do H1" if favor else "CONTRA o H1"]
        g[0] += 1; g[1] += 1 if o["lucro"] > 0 else 0
    print(f"\n=== {os.path.basename(base)} ===   ({len(ops)} operacoes, {sem_h1} sem H1 no historico)")
    for nome, (n, w) in sorted(grupos.items()):
        if not n: continue
        ic = wilson(w, n)
        print(f"  {nome:16} n={n:4}  acerto={100*w/n:5.1f}%  IC95=[{ic[0]:5.1f}, {ic[1]:5.1f}]")
