from __future__ import annotations

import threading
import time
import winsound
from datetime import datetime, timezone

from .config_swing import SwingConfig
from .estrategia_swing import SinalSwing
from .mercado_swing import MercadoSwing
from .registro_swing import RegistroSwing


class ExecutorSwing:
    def __init__(self, config: SwingConfig, mercado: MercadoSwing, registro: RegistroSwing):
        self.config = config
        self.mercado = mercado
        self.registro = registro
        self._lock = threading.Lock()
        self._operacoes_hoje = 0
        self._perdas_consecutivas = 0
        self._lucro_dia = 0.0
        self._encerrado = False
        self._motivo_encerramento: str | None = None

    @property
    def encerrado(self) -> bool:
        return self._encerrado

    def _carregar_estado_dia(self) -> None:
        r = self.registro.resumo_dia()
        self._operacoes_hoje = r["operacoes_hoje"]
        self._lucro_dia = r["lucro_dia"]
        self._perdas_consecutivas = r["perdas_consecutivas"]

    def _checar_limites(self) -> tuple[bool, str]:
        if self._operacoes_hoje >= self.config.max_operacoes_dia:
            return False, f"limite_diario ({self.config.max_operacoes_dia} ops)"
        if self._perdas_consecutivas >= self.config.max_perdas_consecutivas:
            return False, f"perdas_consecutivas ({self._perdas_consecutivas})"
        if self._lucro_dia <= self.config.stop_diario:
            return False, f"stop_diario ({self._lucro_dia:.2f})"
        return True, "ok"

    def _calcular_sl_tp(
        self, sinal: SinalSwing, preco_entrada: float, atr_h4: float
    ) -> tuple[float, float, str]:
        """SL baseado na zona Fibonacci (melhor estrutura), fallback ATR.
        Retorna (sl, tp, metodo)."""
        zona = sinal.detalhes.get("zona_fib") if isinstance(sinal.detalhes, dict) else None
        buffer = atr_h4 * 0.3  # buffer abaixo/acima da zona
        if zona and len(zona) == 2 and buffer > 0:
            zona_low, zona_high = float(zona[0]), float(zona[1])
            if sinal.direcao == "call":
                sl = zona_low - buffer
            else:
                sl = zona_high + buffer
            sl_dist = abs(preco_entrada - sl)
            tp_dist = sl_dist * self.config.rr_ratio
            if sinal.direcao == "call":
                return sl, preco_entrada + tp_dist, "fib"
            return sl, preco_entrada - tp_dist, "fib"
        # fallback ATR
        sl_dist = atr_h4 * self.config.sl_atr_multiplo
        tp_dist = sl_dist * self.config.rr_ratio
        if sinal.direcao == "call":
            return preco_entrada - sl_dist, preco_entrada + tp_dist, "atr"
        return preco_entrada + sl_dist, preco_entrada - tp_dist, "atr"

    def _calcular_valor_forex(self) -> float:
        """Valor em USD da posição baseado em % da banca."""
        return round(self.config.banca_inicial * self.config.risco_percentual, 2)

    def executar(self, sinal: SinalSwing, atr_h4: float = 0.0) -> bool:
        if not self.config.executar_ordens:
            preco_ref = self.mercado.preco_atual_forex(sinal.ativo) or 0.0
            if preco_ref > 0 and atr_h4 > 0:
                sl, tp, metodo_sl = self._calcular_sl_tp(sinal, preco_ref, atr_h4)
                rr_real = abs(tp - preco_ref) / abs(sl - preco_ref) if sl != preco_ref else 0
                sl_label = "abaixo da zona Fib" if metodo_sl == "fib" else "1.5× ATR H4"
                direcao_str = "COMPRA" if sinal.direcao == "call" else "VENDA"
                zona = sinal.detalhes.get("zona_fib", ("-", "-")) if isinstance(sinal.detalhes, dict) else ("-", "-")
                det = sinal.detalhes if isinstance(sinal.detalhes, dict) else {}
                tp_sr = det.get("tp_sr")
                tp_sr_str = f"  Alvo S/R:  {tp_sr:.5f}  (nível estrutural)\n" if tp_sr else ""
                adx_str = f"ADX={det.get('adx_d1','?')}  RSI_H4={det.get('rsi_h4','?')}"

                # Filtro R:R
                rr_minimo = 1.5
                if rr_real < 1.0:
                    print(f">> [SWING] {sinal.ativo}: R:R {rr_real:.1f} insuficiente (< 1.0) — ignorado")
                    self.registro.registrar_bloqueio(sinal, f"rr_insuficiente:{rr_real:.1f}")
                    return False
                rr_aviso = f"  ⚠ R:R abaixo do ideal ({rr_real:.1f} < {rr_minimo})\n" if rr_real < rr_minimo else ""

                print(
                    f"\n{'='*60}\n"
                    f"  {sinal.ativo}  {direcao_str}  [{sinal.setup}]  score={sinal.pontuacao}/10\n"
                    f"  Entrada : ~{preco_ref:.5f}\n"
                    f"  Stop    :  {sl:.5f}  ({sl_label})\n"
                    f"  Target  :  {tp:.5f}  (R:R 1:{rr_real:.1f})\n"
                    f"{tp_sr_str}"
                    f"{rr_aviso}"
                    f"  Zona Fib:  {float(zona[0]):.5f} – {float(zona[1]):.5f}\n"
                    f"  D1: {det.get('tendencia_d1','?')} | {adx_str}\n"
                    f"{'='*60}\n"
                )
                freq = 1200 if rr_real >= rr_minimo else 800
                for _ in range(3):
                    winsound.Beep(freq, 300)
                    time.sleep(0.15)
                # Salva no forward-test tracker
                try:
                    self.registro.registrar_sinal_monitor(sinal, preco_ref, sl, tp)
                except Exception as _e_reg:
                    print(f">> [SWING] tracker erro: {_e_reg!r}")
            else:
                print(f">> [SWING] {sinal.ativo}: {sinal.direcao.upper()} ({sinal.setup}, {sinal.pontuacao}pts) — preço indisponível")
            self.registro.registrar_bloqueio(sinal, "modo_monitor")
            return False

        with self._lock:
            self._carregar_estado_dia()
            pode, motivo = self._checar_limites()
            if not pode:
                print(f">> [SWING] {sinal.ativo}: bloqueado — {motivo}")
                self.registro.registrar_bloqueio(sinal, motivo)
                return False

        if self.config.modo == "forex":
            return self._executar_forex(sinal, atr_h4)
        return self._executar_binaria(sinal)

    def _executar_forex(self, sinal: SinalSwing, atr_h4: float) -> bool:
        preco_ref = self.mercado.preco_atual_forex(sinal.ativo) or 0.0
        if preco_ref <= 0:
            print(f">> [SWING] {sinal.ativo}: preço indisponível para CFD")
            self.registro.registrar_bloqueio(sinal, "preco_indisponivel")
            return False

        if atr_h4 <= 0:
            print(f">> [SWING] {sinal.ativo}: ATR H4 inválido para calcular SL/TP")
            self.registro.registrar_bloqueio(sinal, "atr_invalido")
            return False

        sl, tp, _ = self._calcular_sl_tp(sinal, preco_ref, atr_h4)
        valor = self._calcular_valor_forex()
        direcao_api = "buy" if sinal.direcao == "call" else "sell"

        print(
            f">> [SWING-FX] {sinal.ativo}: {direcao_api.upper()} | setup={sinal.setup} | "
            f"score={sinal.pontuacao} | entrada≈{preco_ref:.5f} | SL={sl:.5f} | TP={tp:.5f} | "
            f"R:R=1:{self.config.rr_ratio} | val=${valor:.2f} | alavancagem={self.config.alavancagem}x"
        )

        ok, id_ordem = self.mercado.comprar_forex(
            sinal.ativo, direcao_api, valor, self.config.alavancagem, sl, tp
        )
        if not ok:
            print(f">> [SWING-FX] {sinal.ativo}: IQ recusou — {id_ordem}")
            self.registro.registrar_bloqueio(sinal, f"buy_recusado:{id_ordem}")
            return False

        self.registro.registrar_abertura(str(id_ordem), sinal, valor, None, 0)
        with self._lock:
            self._operacoes_hoje += 1
        print(f">> [SWING-FX] {sinal.ativo}: posição aberta id={id_ordem} ({self._operacoes_hoje}/{self.config.max_operacoes_dia})")
        return True

    def _executar_binaria(self, sinal: SinalSwing) -> bool:
        payout = self.mercado.payout(sinal.ativo)
        if payout is None or payout < self.config.payout_minimo:
            motivo = f"payout_insuficiente ({payout})"
            print(f">> [SWING] {sinal.ativo}: bloqueado — {motivo}")
            self.registro.registrar_bloqueio(sinal, motivo)
            return False

        ts_exp = self.mercado.timestamp_expiracao(sinal.setup)
        exp_dt = datetime.fromtimestamp(ts_exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(
            f">> [SWING] {sinal.ativo}: {sinal.direcao.upper()} | setup={sinal.setup} | "
            f"score={sinal.pontuacao} | valor=R${self.config.valor_por_ordem:.2f} | "
            f"payout={payout:.0%} | exp={exp_dt}"
        )

        ok, id_ordem = self.mercado.comprar(
            self.config.valor_por_ordem, sinal.ativo, sinal.direcao, ts_exp
        )
        if not ok:
            print(f">> [SWING] {sinal.ativo}: IQ recusou — {id_ordem}")
            self.registro.registrar_bloqueio(sinal, f"buy_recusado:{id_ordem}")
            return False

        self.registro.registrar_abertura(
            str(id_ordem), sinal, self.config.valor_por_ordem, payout, ts_exp
        )
        with self._lock:
            self._operacoes_hoje += 1
        print(f">> [SWING] {sinal.ativo}: ordem enviada id={id_ordem} ({self._operacoes_hoje}/{self.config.max_operacoes_dia})")
        return True

    def verificar_pendentes(self) -> None:
        """Verifica e registra resultado de ordens que já expiraram."""
        pendentes = self.registro.pendentes()
        agora = int(time.time())
        for id_ordem, ativo, ts_exp in pendentes:
            if agora < ts_exp + 60:
                continue  # ainda não expirou
            resultado = self.mercado.verificar_resultado(id_ordem)
            if resultado is None:
                continue
            resultado_norm = resultado.strip().lower()
            if resultado_norm in {"win", "won"}:
                lucro = self.config.valor_por_ordem * (self.mercado.payout(ativo) or 0.87)
            elif resultado_norm in {"loss", "loose"}:
                lucro = -self.config.valor_por_ordem
            elif resultado_norm in {"equal", "draw"}:
                lucro = 0.0
            else:
                continue
            self.registro.registrar_resultado(id_ordem, resultado_norm, lucro)
            icone = "WIN" if lucro > 0 else ("DRAW" if lucro == 0 else "LOSS")
            print(f">> [SWING] {ativo}: resultado={icone} lucro={lucro:+.2f} id={id_ordem}")
            with self._lock:
                self._lucro_dia += lucro
                if lucro < 0:
                    self._perdas_consecutivas += 1
                else:
                    self._perdas_consecutivas = 0
