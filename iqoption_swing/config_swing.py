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
    # A tese atual teve edge negativo no histórico; só muda após nova validação.
    estrategia_validada: bool = False

    # Ativos
    ativos: tuple[str, ...] = (
        "EURUSD",
        "GBPUSD",
        "AUDUSD",
        "NZDUSD",
        "USDCAD",
        "USDCHF",
        "USDJPY",
        "EURJPY",
        "GBPJPY",
        "EURGBP",
        "AUDJPY",
    )

    # Banco
    caminho_banco: Path = Path("dados/scalping_swing.db")

    # Risco
    valor_por_ordem: float = 20.0       # binária: valor fixo; forex: ignorado (usa risco_percentual)
    risco_percentual: float = 0.01      # forex: % da banca por operação (1% = 0.01)
    banca_inicial: float = 500.0        # forex: referência de banca para cálculo de lote
    rr_ratio: float = 2.0               # forex: R:R mínimo (TP = rr_ratio × SL)
    sl_atr_multiplo: float = 1.5        # forex: SL = N × ATR do H4
    sl_min_atr: float = 0.5             # piso do SL em múltiplos de ATR H4 (ver _calcular_sl_tp)
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
    # Setups liberados no Swing. Backtest recente favoreceu pullback/breakout;
    # S/R simples e divergência ficam desligados até novo forward-test validar.
    setups_ativos: tuple[str, ...] = ("pullback_tendencia", "breakout_reteste")

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
    intervalo_radar_segundos: float = 1.0
    intervalo_verificacao_monitor_segundos: float = 60.0
    radar_tolerancia_toque_pips: float = 2.0
    janela_noticia_minutos: int = 120
    permitir_entrada_a_favor_noticia: bool = False
    porta_grafico: int = 8773
    sufixo_banco: str = "swing"

    def validar(self) -> None:
        if self.modo not in {"forex", "binaria"}:
            raise RuntimeError("Modo Swing deve ser 'forex' ou 'binaria'.")
        if self.executar_ordens and not self.estrategia_validada:
            raise RuntimeError(
                "estratégia Swing não validada: execução bloqueada; use modo monitor"
            )
        if self.conta.upper() == "REAL" and not self.confirmo_conta_real:
            raise RuntimeError(
                "Para operar na conta REAL, defina confirmo_conta_real=True explicitamente."
            )
        if not 0 < self.risco_percentual <= 0.02:
            raise RuntimeError("risco_percentual Swing deve ficar entre 0 e 2%.")
        if self.rr_ratio <= 0 or self.sl_atr_multiplo <= 0:
            raise RuntimeError("R:R e múltiplo de ATR precisam ser positivos.")
        if self.max_operacoes_dia < 0:
            raise RuntimeError("max_operacoes_dia não pode ser negativo; 0 significa sem limite.")
        if self.intervalo_radar_segundos <= 0:
            raise RuntimeError("intervalo_radar_segundos precisa ser positivo.")
        if self.intervalo_verificacao_monitor_segundos <= 0:
            raise RuntimeError("intervalo_verificacao_monitor_segundos precisa ser positivo.")
        if self.radar_tolerancia_toque_pips < 0:
            raise RuntimeError("radar_tolerancia_toque_pips não pode ser negativo.")
        if self.janela_noticia_minutos <= 0:
            raise RuntimeError("janela_noticia_minutos precisa ser positivo.")
