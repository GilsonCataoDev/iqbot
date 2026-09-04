"""ESTUDO BINARIAS — qual sinal acerta a direcao da proxima vela?

Pergunta unica: P(vela i+1 fechar na direcao apostada) > breakeven?
  payout 0.85 -> breakeven 54.05%
  payout 0.87 -> breakeven 53.48%

Nao existe SL/TP. Spread nao entra (o custo e o payout).
O que importa e WR puro e a estabilidade dele.

Correcoes aplicadas: empates excluidos, IC com n efetivo, holdout temporal,
baseline aleatorio, e correcao para testes multiplos.

Uso:
    python estudo_binarias.py
    python estudo_binarias.py --tf 60 300 900 3600
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

import motor_sinais as M

BE = 0.5405   # payout 0.85 (o pior caso dos nossos pares)


def avaliar_tudo(tf: int, ativos=None) -> pd.DataFrame:
    frames = M.carregar(tf, ativos)
    if not frames:
        return pd.DataFrame()
    nome_tf = M.TF_NOME.get(tf, str(tf))
    print(f"\n{'='*94}")
    print(f"BINARIAS — {nome_tf}   ({len(frames)} pares, breakeven {BE:.2%})")
    print(f"{'='*94}")
    print(f"  {'sinal':30} {'n':>7} {'n_ef':>6} {'WR':>7} "
          f"{'IC95 (n efetivo)':>18} {'hold':>7}  veredito")
    print(f"  {'-'*30} {'-'*7} {'-'*6} {'-'*7} {'-'*18} {'-'*7}  {'-'*12}")

    linhas = []
    for nome, fn in M.SINAIS.items():
        d = M.avaliar_binario(frames, fn)
        if d.empty or len(d) < 100:
            print(f"  {nome:30} {len(d):>7} {'':>6} {'':>7} {'':>18} {'':>7}  amostra pequena")
            continue
        s = M.stats_binario(d, BE)
        cal, hold = M.split_temporal(d)
        s_h = M.stats_binario(hold, BE) if len(hold) >= 50 else {}
        wr_h = s_h.get("wr", float("nan"))

        if s["lo_ef"] > BE:
            vd = "EDGE"
        elif s["hi_ef"] < BE:
            vd = "negativo"
        else:
            vd = "inconclusivo"

        print(f"  {nome:30} {s['n']:>7} {s['n_ef']:>6} {s['wr']:>6.2%} "
              f"  [{s['lo_ef']:>5.2%},{s['hi_ef']:>6.2%}] {wr_h:>6.2%}  {vd}")
        linhas.append({"tf": nome_tf, "sinal": nome, **s, "wr_hold": wr_h})

    return pd.DataFrame(linhas)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", nargs="*", type=int, default=[60, 300, 900, 3600])
    ap.add_argument("--ativos", nargs="*", default=None)
    a = ap.parse_args()

    print("="*94)
    print("ESTUDO BINARIAS — direcao da proxima vela")
    print("="*94)
    print("  Regra: precisa de WR > 54.05% (payout 0.85) com IC95 inferior acima disso.")
    print("  IC calculado com n EFETIVO (timestamps unicos) — pares correlacionados")
    print("  nao contam como observacoes independentes.")

    todos = []
    for tf in a.tf:
        r = avaliar_tudo(tf, a.ativos)
        if not r.empty:
            todos.append(r)

    if not todos:
        print("\nSem dados."); return 1

    t = pd.concat(todos, ignore_index=True)

    print(f"\n{'='*94}")
    print("RANKING GERAL (por WR, so os com n_ef >= 200)")
    print(f"{'='*94}")
    r = t[t["n_ef"] >= 200].sort_values("wr", ascending=False)
    print(f"  {'tf':>5} {'sinal':30} {'n_ef':>6} {'WR':>7} {'IC inf':>8}  status")
    for _, x in r.head(15).iterrows():
        st = "EDGE" if x["lo_ef"] > BE else ("negativo" if x["hi_ef"] < BE else "inconclusivo")
        print(f"  {x['tf']:>5} {x['sinal']:30} {x['n_ef']:>6} {x['wr']:>6.2%} "
              f"{x['lo_ef']:>7.2%}  {st}")

    # Baseline aleatorio como referencia
    base = t[t["sinal"].str.contains("BASELINE")]
    if not base.empty:
        print(f"\n  BASELINE aleatorio por tf:")
        for _, x in base.iterrows():
            print(f"    {x['tf']:>5}: WR={x['wr']:.2%}  (isto e o 'zero' — nada abaixo disso importa)")

    # Correcao para testes multiplos
    n_testes = len(t)
    aprovados = t[t["lo_ef"] > BE]
    print(f"\n{'='*94}")
    print("TESTES MULTIPLOS")
    print(f"{'='*94}")
    print(f"  Sinais testados: {n_testes}")
    print(f"  Aprovados a 95%: {len(aprovados)}")
    print(f"  Esperado por acaso (5% de {n_testes}): {0.05*n_testes:.1f}")
    if len(aprovados) <= 0.05 * n_testes:
        print("  >> Os aprovados NAO excedem o que o acaso produziria. Nenhum edge real.")
    else:
        print("  >> Ha mais aprovados que o acaso. Candidatos:")
        for _, x in aprovados.iterrows():
            print(f"     {x['tf']} / {x['sinal']}: WR={x['wr']:.2%} hold={x['wr_hold']:.2%}")

    print(f"\n{'='*94}")
    print("CONCLUSAO BINARIAS")
    print(f"{'='*94}")
    melhor = t.loc[t["lo_ef"].idxmax()] if not t.empty else None
    if melhor is not None:
        print(f"  Melhor candidato: {melhor['tf']} / {melhor['sinal']}")
        print(f"    WR={melhor['wr']:.2%}  IC95=[{melhor['lo_ef']:.2%}, {melhor['hi_ef']:.2%}]  "
              f"holdout={melhor['wr_hold']:.2%}")
        print(f"    breakeven={BE:.2%}")
        if melhor["lo_ef"] > BE:
            print("    >> Passa. Validar com mais dados antes de operar.")
        else:
            print("    >> NAO passa o breakeven. Binaria descartada para este conjunto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
