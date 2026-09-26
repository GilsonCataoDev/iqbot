"""CLI: backtest da estratégia ICT/SMC (sweep + OB + SMT + CHoCH + FVG) para Ouro.

Baseado no infográfico "ICT/SMC – Micro Gold Futures" (r/Forexstrategy):
sweep de liquidez -> Order Block -> SMT Divergence -> CHoCH/MSS ->
FVG/IFVG + reteste, operando só nas killzones de Londres/NY/Tóquio.

Conecta na conta PRACTICE (nunca REAL), busca + cacheia candles da IQ
Option para o ouro e para um ativo correlacionado (usado no filtro de SMT
Divergence) e roda o backtest candle-a-candle.

Uso:
    python rodar_backtest_ict_ouro.py --usuario email --senha senha
    python rodar_backtest_ict_ouro.py --usuario email --senha senha \
        --ativo XAUUSD --correlacionado XAGUSD --timeframe 300 --candles 5000

Os nomes exatos dos ativos de Ouro/Prata podem variar por conta na IQ
Option (ex.: "XAUUSD", "GOLD", "XAUUSD-OTC"). Confira em Forex/CFD antes de
rodar e ajuste com --ativo/--correlacionado se o nome padrão não existir na
sua conta.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from iqoption_m5.backtest import baixar_historico, carregar_cache
from iqoption_m5.config import Configuracao
from iqoption_m5.ict_backtest import simular_ict_smc


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backtest ICT/SMC para Ouro (IQ Option)")
    p.add_argument("--ativo", default="XAUUSD", help="Ativo de ouro (default: XAUUSD)")
    p.add_argument(
        "--correlacionado", default="XAGUSD",
        help="Ativo p/ filtro de SMT Divergence (default: XAGUSD/Prata; '' desativa o filtro)",
    )
    p.add_argument(
        "--timeframe", type=int, default=300, choices=[60, 300, 900],
        help="Timeframe em segundos (60=M1, 300=M5, 900=M15; default: 300)",
    )
    p.add_argument("--candles", type=int, default=5000, help="Nº de candles a buscar/usar (default: 5000)")
    p.add_argument("--banca", type=float, default=1000.0, help="Banca inicial (default: 1000.0)")
    p.add_argument(
        "--risco", type=float, default=0.0075,
        help="Risco por operação, fração da banca (default: 0.0075 = 0.75%%)",
    )
    p.add_argument("--spread", type=float, default=0.20, help="Spread do ouro em US$/onça (default: 0.20)")
    p.add_argument("--rr-minimo", type=float, default=1.3, help="Retorno:risco mínimo exigido (default: 1.3)")
    p.add_argument("--sem-smt", action="store_true", help="Desliga a exigência de SMT Divergence")
    p.add_argument("--sem-killzone", action="store_true", help="Desliga o filtro de killzones")
    p.add_argument("--usuario", required=True, help="E-mail IQ Option")
    p.add_argument("--senha", required=True, help="Senha IQ Option")
    return p


def _conectar(usuario: str, senha: str):
    from iqoptionapi.stable_api import IQ_Option
    api = IQ_Option(usuario, senha)
    status, reason = api.connect()
    if not status:
        print(f"[ERRO] Falha ao conectar: {reason}")
        sys.exit(1)
    api.change_balance("PRACTICE")  # NUNCA REAL
    time.sleep(1)
    print(f"[OK] Conectado como PRACTICE | saldo: {api.get_balance():.2f}")
    return api


def _obter_candles(api, config: Configuracao, ativo: str, total: int):
    candles = carregar_cache(config, ativo)
    faltam = total - (len(candles) if candles is not None else 0)
    if faltam > 0:
        print(f"[INFO] Baixando candles de {ativo} (faltam ~{faltam})...")
        candles = baixar_historico(api, config, ativo, total)
    return candles.tail(total)


def main() -> None:
    args = _parser().parse_args()
    config = Configuracao(timeframe_segundos=args.timeframe)
    api = _conectar(args.usuario, args.senha)

    candles_ouro = _obter_candles(api, config, args.ativo, args.candles)

    correlacionado = None
    if args.correlacionado:
        try:
            correlacionado = _obter_candles(api, config, args.correlacionado, args.candles)
        except Exception as exc:
            print(f"[AVISO] Não consegui buscar {args.correlacionado} para o filtro de SMT: {exc}")

    resultados, banca_final = simular_ict_smc(
        args.ativo,
        candles_ouro,
        correlacionado,
        banca=args.banca,
        risco_percentual=args.risco,
        spread=args.spread,
        rr_minimo=args.rr_minimo,
        exigir_smt=(not args.sem_smt) and correlacionado is not None,
        exigir_killzone=not args.sem_killzone,
    )

    total = len(resultados)
    if total == 0:
        print("Nenhuma operação gerada — tente mais --candles, --sem-smt ou --sem-killzone.")
        return

    ganhos = int((resultados["lucro"] > 0).sum())
    lucro_total = float(resultados["lucro"].sum())
    print(
        f"{args.ativo} | ICT/SMC: operações={total}, acerto={ganhos / total:.2%}, "
        f"lucro_total={lucro_total:.2f}, banca_final={banca_final:.2f}"
    )
    print(resultados[["aberta_em", "lado", "entrada", "saida", "lucro", "motivo_saida"]].to_string(index=False))


if __name__ == "__main__":
    main()
