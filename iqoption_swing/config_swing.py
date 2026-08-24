from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SwingConfig:
    # "binaria" = buy_by_raw_expirationtime (original)
    # "forex"   = CFD com SL/TP e R:R
    modo: str = "forex"
    # Credenciais (lidas de env vars se vazias)
    email: str = ""
    senha: str = ""

    # Conta e execução
    conta: str = "PRACTICE"
    confirmo_conta_real: bool = False
    executar_ordens: bool = False

    # Ativos
    ativos: tuple[str, ...] = ("EURUSD", "GBPUSD", "USDJPY")

    # Banco
    caminho_banco: Path = Path("dados/scalping_swing.db")

    # Risco
    valor_por_ordem: float = 20.0       # binária: valor fixo; forex: ignorado (usa risco_percentual)
    risco_percentual: float = 0.01      # forex: % da banca por operação (1% = 0.01)
    banca_inicial: float = 500.0        # forex: referência de banca para cálculo de lote
    rr_ratio: float = 2.0               # forex: R:R mínimo (TP = rr_ratio × SL)
    sl_atr_multiplo: float = 1.5        # forex: SL = N × ATR do H4
    alavancagem: int = 100              # forex: alavancagem CFD (50, 100, 200...)
    max_operacoes_dia: int = 3
    max_perdas_consecutivas: int = 2
    stop_diario: float = -60.0
    payout_minimo: float = 0.80         # só usado em modo binária

    # Expiração por setup — usado apenas em modo="binaria"
    expiracao_por_setup: dict = field(default_factory=lambda: {
        "pullback_tendencia": "fim_semana",
        "sr_rejeicao": "fim_dia",
        "breakout_reteste": "fim_semana",
    })

    # Candles por timeframe
    d1_num_candles: int = 120   # ~4 meses
    h4_num_candles: int = 120   # ~20 dias
    h1_num_candles: int = 100   # ~4 dias

    # Scoring mínimo para executar
    pontuacao_minima: int = 8

    # Parâmetros D1
    d1_slope_min_atr: float = 0.05   # inclinação mínima da EMA50 em múltiplos de ATR

    # Parâmetros H4
    h4_amplitude_min_atr: float = 2.0  # impulso mínimo pro Fibonacci
    h4_tolerancia_atr: float = 0.5     # tolerância de zona em múltiplos de ATR
    h4_janela_toque: int = 8           # últimos N candles H4 pra verificar toque
    h4_janela_breakout: int = 20       # últimos N candles H4 pra detectar rompimento
    h4_sr_raio: int = 3               # raio pra pivôs de S/R

    # Parâmetros H1
    h1_corpo_min_atr: float = 0.3     # corpo mínimo do candle de confirmação em múltiplos de ATR

    # Loop
    intervalo_loop_segundos: float = 60.0
    porta_grafico: int = 8773
    sufixo_banco: str = "swing"

    def validar(self) -> None:
        if self.conta.upper() == "REAL" and not self.confirmo_conta_real:
            raise RuntimeError(
                "Para operar na conta REAL, defina confirmo_conta_real=True explicitamente."
            )
