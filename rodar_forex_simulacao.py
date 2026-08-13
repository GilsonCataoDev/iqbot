"""Executa pesquisa Forex/CFD somente no histórico local."""

from iqoption_m5.backtest import carregar_cache
from iqoption_m5.config import configuracao_pesquisa_m5
from iqoption_m5.forex_backtest import simular_forex


def main() -> None:
    config = configuracao_pesquisa_m5()
    print("Forex/CFD paper — nenhuma ordem será enviada")
    for ativo in ("EURUSD", "GBPUSD", "USDJPY"):
        candles = carregar_cache(config, ativo)
        if candles is None or len(candles) < 1000:
            print(f"{ativo}: histórico insuficiente")
            continue
        spread = 0.01 if "JPY" in ativo else 0.00010
        for estrategia in ("rompimento_reteste", "toque_lta_ltb", "correcao_fibo_sr"):
            resultados, banca = simular_forex(
                ativo, candles, spread=spread, estrategia=estrategia
            )
            ganhos = int((resultados["lucro"] > 0).sum()) if not resultados.empty else 0
            total = len(resultados)
            print(
                f"{ativo} | {estrategia}: operações={total}, "
                f"acerto={ganhos / total:.2%}, banca_final={banca:.2f}"
                if total else f"{ativo} | {estrategia}: sem operações"
            )


if __name__ == "__main__":
    main()
