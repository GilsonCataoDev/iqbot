"""CLI: backtest candle-a-candle para comparar configs de filtros.

Uso:
    python run_backtest_candle.py --ativos EURUSD GBPUSD --payout 0.85
    python run_backtest_candle.py --ativos EURUSD --payout 0.82 --db bot.db

Conecta na conta PRACTICE (nunca REAL). Busca + salva candles no banco SQLite.
Compara config BASE (todos filtros Off) versus config FILTROS (§8+§9 ligados).
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from iqoption_m5.config import Configuracao, configuracao_pesquisa_m5
from iqoption_m5.registro import RegistroSQLite
from iqoption_m5.backtest_candle import comparar_configs, imprimir_comparacao_configs


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backtest candle-a-candle IQ Option M5")
    p.add_argument(
        "--ativos", nargs="+", default=["EURUSD", "GBPUSD", "EURJPY"],
        help="Lista de ativos para testar (default: EURUSD GBPUSD EURJPY)",
    )
    p.add_argument(
        "--payout", type=float, default=0.82,
        help="Payout fracionário (ex: 0.82 = 82%%, default: 0.82)",
    )
    p.add_argument(
        "--candles", type=int, default=2000,
        help="Nº de candles M5 a buscar/usar por ativo (default: 2000 ≈ 7 dias)",
    )
    p.add_argument(
        "--db", type=str, default="bot.db",
        help="Caminho do banco SQLite (default: bot.db no diretório atual)",
    )
    p.add_argument(
        "--sem-detalhe", action="store_true",
        help="Omite os relatórios individuais por config (mostra só o comparativo)",
    )
    p.add_argument(
        "--usuario", type=str, required=True, help="E-mail IQ Option"
    )
    p.add_argument(
        "--senha", type=str, required=True, help="Senha IQ Option"
    )
    return p


# ---------------------------------------------------------------------------
# Conexão IQ Option
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Busca e persiste candles
# ---------------------------------------------------------------------------

def _buscar_candles(api, registro: RegistroSQLite, ativo: str, timeframe: int, quantidade: int):
    """Tenta carregar do banco; busca do server o que faltar."""
    armazenados = registro.total_candles_armazenados(ativo, timeframe)
    print(f"  {ativo}: {armazenados} candles armazenados", end="")

    # Busca da API (M5 = 300 s)
    fim = int(api.get_server_timestamp())
    lote = api.get_candles(ativo, timeframe, quantidade, fim)
    if not lote:
        print(" — API sem dados, usando apenas banco")
    else:
        import pandas as pd
        rows = []
        for c in lote:
            rows.append({
                "Open": float(c["open"]),
                "High": float(c["max"]),
                "Low":  float(c["min"]),
                "Close": float(c["close"]),
            })
        idx = pd.to_datetime([c["from"] for c in lote], unit="s", utc=True)
        df_api = pd.DataFrame(rows, index=idx).sort_index()
        novos = registro.salvar_candles(ativo, df_api, timeframe)
        print(f" + {novos} novos")

    df = registro.carregar_candles(ativo, timeframe, limite=quantidade)
    print(f"    → {len(df)} candles disponíveis para backtest")
    return df


# ---------------------------------------------------------------------------
# Configs: base vs com filtros §8+§9
# ---------------------------------------------------------------------------

def _config_base() -> Configuracao:
    return configuracao_pesquisa_m5(Configuracao(
        sr_rejeicao_rsi_filtro=False,
        sr_rejeicao_corpo_min_atr=0.0,
        pullback_recuo_rsi_filtro=False,
        pullback_confirmacao_corpo_atr=0.0,
    ))


def _config_filtros() -> Configuracao:
    """§8 + §9 ativados com parâmetros conservadores para primeiro teste."""
    return configuracao_pesquisa_m5(Configuracao(
        # §8: SR rejeição exige RSI em zona extrema + corpo mínimo de 0.5×ATR
        sr_rejeicao_rsi_filtro=True,
        sr_rejeicao_corpo_min_atr=0.5,
        # §9: pullback exige RSI extremo no recuo + corpo mínimo na confirmação
        pullback_recuo_rsi_filtro=True,
        pullback_confirmacao_corpo_atr=0.5,
    ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parser().parse_args()

    db_path = Path(args.db)
    registro = RegistroSQLite(str(db_path))
    timeframe = 300  # M5

    print(f"\n=== Backtest candle-a-candle M5 ===")
    print(f"Ativos: {args.ativos}")
    print(f"Payout: {args.payout:.0%} | Candles/ativo: {args.candles}")
    print(f"Banco: {db_path.resolve()}\n")

    api = _conectar(args.usuario, args.senha)

    candles_por_ativo: dict = {}
    print("Buscando candles...")
    for ativo in args.ativos:
        try:
            df = _buscar_candles(api, registro, ativo, timeframe, args.candles)
            if not df.empty:
                candles_por_ativo[ativo] = df
        except Exception as e:
            print(f"  {ativo}: ERRO — {e}")
        time.sleep(0.5)

    if not candles_por_ativo:
        print("\n[ERRO] Nenhum candle disponível. Abortando.")
        sys.exit(1)

    config_base    = _config_base()
    config_filtros = _config_filtros()

    print("\nSimulando...")
    ops_base, ops_filtros = comparar_configs(
        candles_por_ativo, config_base, config_filtros, args.payout
    )

    imprimir_comparacao_configs(
        ops_base,
        ops_filtros,
        args.payout,
        nome_base="§8+§9 desativados (base)",
        nome_filtros="§8+§9 ativados",
        detalhar=not args.sem_detalhe,
    )


if __name__ == "__main__":
    main()
