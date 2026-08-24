from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from .calendario_swing import verificar_noticias
from .config_swing import SwingConfig
from .estrategia_swing import EstrategiaSwing
from .executor_swing import ExecutorSwing
from .mercado_swing import MercadoSwing, MercadoSwingIndisponivel
from .registro_swing import RegistroSwing


def _h1_atual(ts: int) -> int:
    """Retorna o timestamp de início do H1 corrente."""
    return (ts // 3600) * 3600


def main(config: SwingConfig) -> None:
    conta_str = config.conta.upper()
    print(
        f"[SWING] Iniciando bot swing | conta={conta_str} | "
        f"ativos={config.ativos} | ordens={'ATIVAS' if config.executar_ordens else 'MONITOR'}"
    )

    banco = Path("dados") / f"scalping_{config.sufixo_banco}.db"
    mercado = MercadoSwing(config)
    registro = RegistroSwing(banco)
    estrategia = EstrategiaSwing(config)
    executor = ExecutorSwing(config, mercado, registro)

    mercado.iniciar()

    # Expiração automática: sinais sem resolução após N candles H4 (~2 dias úteis)
    _MONITOR_TIMEOUT_H4 = 12

    def _verificar_sinais_monitor() -> None:
        """Checa candles H4 atuais contra SL/TP de sinais pendentes. Atualiza W/L."""
        pendentes = registro.monitor_pendentes()
        if not pendentes:
            return
        # Agrupa por ativo para economizar chamadas de API
        por_ativo: dict[str, list[dict]] = {}
        for s in pendentes:
            por_ativo.setdefault(s["ativo"], []).append(s)

        for ativo, sinais in por_ativo.items():
            try:
                df_h4 = mercado.candles_h4(ativo)
            except Exception as e:
                print(f"[SWING-MON] {ativo}: erro ao buscar H4 — {e}")
                continue

            for s in sinais:
                # Filtra candles posteriores ao sinal
                try:
                    ts_sinal = pd.Timestamp(s["registrado_em"])
                    _apos_raw = df_h4[df_h4.index > ts_sinal]
                    # Exclui o último candle: ainda em formação (open, High/Low incompletos)
                    apos = _apos_raw.iloc[:-1]
                    if not _apos_raw.empty:
                        _ult = _apos_raw.iloc[-1]
                        print(
                            f"[SWING-DBG] {s['ativo']} ts_sinal={ts_sinal} "
                            f"raw_apos={len(_apos_raw)} apos_filtrado={len(apos)} "
                            f"ultimo_candle_idx={_apos_raw.index[-1]} "
                            f"H={float(_ult['High']):.5f} L={float(_ult['Low']):.5f}"
                        )
                except Exception:
                    apos = df_h4

                direcao = s["direcao"]
                sl, tp = s["sl"], s["tp"]
                resultado = None

                for _, row in apos.iterrows():
                    if direcao == "call":
                        if float(row["Low"]) <= sl:
                            resultado = "loss"; break
                        if float(row["High"]) >= tp:
                            resultado = "win"; break
                    else:
                        if float(row["High"]) >= sl:
                            resultado = "loss"; break
                        if float(row["Low"]) <= tp:
                            resultado = "win"; break

                # Timeout: mais de N candles H4 sem resolver
                if resultado is None and len(apos) >= _MONITOR_TIMEOUT_H4:
                    resultado = "expirado"

                if resultado:
                    registro.resolver_sinal_monitor(s["id"], resultado)
                    icone = "✓ WIN" if resultado == "win" else ("✗ LOSS" if resultado == "loss" else "~ EXP")
                    print(
                        f"[SWING-MON] {icone}  {ativo} {direcao.upper()} | "
                        f"setup={s['setup']} score={s['pontuacao']} | "
                        f"entrada={s['entrada']:.5f} SL={sl:.5f} TP={tp:.5f} | "
                        f"sinal: {s['registrado_em'][:16]}"
                    )

        resumo = registro.resumo_monitor()
        if resumo["total"] > 0:
            print(
                f"[SWING-MON] Acumulado: {resumo['wins']}W / {resumo['losses']}L  "
                f"WR={resumo['wr']:.0%}  ({resumo['total']} sinais resolvidos)"
            )

    ultimo_h1 = 0
    reconexoes = 0

    while True:
        try:
            ts = mercado.timestamp_servidor()
            h1_corrente = _h1_atual(ts)

            # Verifica ordens pendentes a cada ciclo
            try:
                executor.verificar_pendentes()
            except Exception as e:
                print(f"[SWING] Erro ao verificar pendentes: {e!r}")

            # Verifica outcomes de sinais pendentes a cada ciclo
            try:
                _verificar_sinais_monitor()
            except Exception as e:
                print(f"[SWING] Erro ao verificar monitor: {e!r}")

            # Avalia sinais apenas na virada do H1
            if h1_corrente > ultimo_h1:
                ultimo_h1 = h1_corrente
                hora_utc = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                print(f"\n[SWING] === H1 close: {hora_utc} ===")

                # Filtro de notícias — bloqueia hora inteira se evento de alto impacto
                _noticia_bloqueada, _noticia_desc = verificar_noticias(janela_minutos=120)
                if _noticia_bloqueada:
                    print(f"[SWING] ⚠ NOTÍCIA ALTO IMPACTO: {_noticia_desc} — hora bloqueada")
                    time.sleep(config.intervalo_loop_segundos)
                    continue

                sinais_hora: list = []
                for ativo in config.ativos:
                    try:
                        if not mercado.aberto(ativo):
                            print(f"[SWING] {ativo}: mercado fechado, pulando")
                            continue

                        print(f"[SWING] {ativo}: buscando candles D1/H4/H1...")
                        df_d1 = mercado.candles_d1(ativo)
                        df_h4 = mercado.candles_h4(ativo)
                        df_h1 = mercado.candles_h1(ativo)

                        sinal = estrategia.avaliar(ativo, df_d1, df_h4, df_h1)
                        if sinal is None:
                            print(f"[SWING] {ativo}: sem sinal (score < {config.pontuacao_minima})")
                            continue

                        # ATR do H4 para cálculo de SL/TP
                        atr_h4 = 0.0
                        try:
                            df_h4_ind = estrategia._adicionar_indicadores(df_h4)
                            atr_series = df_h4_ind["ATR"].dropna()
                            if not atr_series.empty:
                                atr_h4 = float(atr_series.iloc[-1])
                        except Exception:
                            pass

                        print(
                            f"[SWING] {ativo}: SINAL {sinal.direcao.upper()} | "
                            f"setup={sinal.setup} | score={sinal.pontuacao} | atr_h4={atr_h4:.5f} | {sinal.detalhes}"
                        )
                        sinais_hora.append((sinal, atr_h4))

                    except MercadoSwingIndisponivel as e:
                        print(f"[SWING] {ativo}: dados indisponíveis — {e}")
                    except Exception as e:
                        print(f"[SWING] {ativo}: erro inesperado — {e!r}")

                # Alerta de correlação: 3+ pares apontando mesmo lado do USD
                _usd_fraco = [s for s, _ in sinais_hora if s.direcao == "call" and "USD" in s.ativo and not s.ativo.startswith("USD")]
                _usd_forte = [s for s, _ in sinais_hora if s.direcao == "put"  and "USD" in s.ativo and not s.ativo.startswith("USD")]
                _usd_fraco += [s for s, _ in sinais_hora if s.direcao == "put"  and s.ativo.startswith("USD")]
                _usd_forte += [s for s, _ in sinais_hora if s.direcao == "call" and s.ativo.startswith("USD")]
                if len(_usd_fraco) >= 3:
                    pares_str = ", ".join(s.ativo for s in _usd_fraco)
                    print(f"\n[SWING] ⚠ CORRELAÇÃO: {len(_usd_fraco)} pares apontam USD FRACO ({pares_str}) — risco concentrado\n")
                if len(_usd_forte) >= 3:
                    pares_str = ", ".join(s.ativo for s in _usd_forte)
                    print(f"\n[SWING] ⚠ CORRELAÇÃO: {len(_usd_forte)} pares apontam USD FORTE ({pares_str}) — risco concentrado\n")

                for sinal, atr_h4 in sinais_hora:
                    try:
                        executor.executar(sinal, atr_h4=atr_h4)

                    except MercadoSwingIndisponivel as e:
                        print(f"[SWING] {ativo}: dados indisponíveis — {e}")
                    except Exception as e:
                        print(f"[SWING] {ativo}: erro inesperado — {e!r}")

            reconexoes = 0

        except MercadoSwingIndisponivel as e:
            print(f"[SWING] mercado indisponível: {e!r}")
            reconexoes += 1
            if reconexoes <= 5:
                print(f"[SWING] tentando reconectar ({reconexoes}/5)...")
                if not mercado.reconectar_se_necessario():
                    print("[SWING] reconexão falhou, aguardando 60s...")
                    time.sleep(60)

        except KeyboardInterrupt:
            print("\n[SWING] Encerrado pelo usuário.")
            break

        except Exception as e:
            print(f"[SWING] erro no loop: {e!r}")

        time.sleep(config.intervalo_loop_segundos)
