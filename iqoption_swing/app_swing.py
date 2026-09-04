from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .calendario_swing import NoticiaSwing, avaliar_noticias_ativo, verificar_noticias
from .config_swing import SwingConfig
from .estrategia_swing import AnaliseSwing, EstrategiaSwing
from .executor_swing import ExecutorSwing
from .grafico_swing import GraficoSwing
from .mercado_swing import MercadoSwing, MercadoSwingIndisponivel
from .radar_swing import avaliar_radar, radar_para_alerta
from .registro_swing import RegistroSwing


def _m15_atual(ts: int) -> int:
    """Retorna o timestamp de início do M15 corrente."""
    return (ts // 900) * 900


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

    grafico = GraficoSwing(config.ativos, config.porta_grafico)
    try:
        url = grafico.iniciar()
        print(f"[SWING] Gráfico aberto: {url}")
    except Exception as e:
        print(f"[SWING] Gráfico indisponível ({e!r}); o robô continua sem o painel.")
        grafico = None

    # Cache de estado anterior por ativo — detecta transições ESPERAR→ENTRAR etc.
    _estado_anterior: dict[str, str] = {}
    _radar_anterior: dict[str, str] = {}
    _noticia_bloqueada_atual = False
    _noticia_desc_atual = ""

    def _imprimir_analise(analise: AnaliseSwing) -> None:
        icons = {"ENTRAR": "▶ ENTRAR", "ESPERAR": "◌ ESPERAR", "EVITAR": "✕ EVITAR"}
        icon = icons.get(analise.estado, analise.estado)
        dir_str = analise.direcao.upper() if analise.direcao else "—"
        setup_str = f"  setup={analise.setup}" if analise.setup else ""
        print(f"[SWING] {icon:12}  {analise.ativo:8} {dir_str}{setup_str}")
        if analise.zona_entrada:
            z = analise.zona_entrada
            tp1_str = f"  TP1={analise.tp1:.5f}" if analise.tp1 else ""
            tp2_str = f"  TP2={analise.tp2:.5f}" if analise.tp2 else ""
            rr_str  = f"  R:R {analise.rr:.1f}" if analise.rr else ""
            print(f"         zona=[{z[0]:.5f}–{z[1]:.5f}]  stop={analise.invalidacao:.5f}{tp1_str}{tp2_str}{rr_str}")
        for p in analise.pendentes:
            print(f"         ⏳ {p}")
        for b in analise.bloqueadores:
            print(f"         ✕ {b}")

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
                    # Exclui o último candle: ainda em formação (High/Low incompletos)
                    apos = _apos_raw.iloc[:-1]
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

    ultimo_m15 = 0
    reconexoes = 0
    ultimo_check_monitor = 0.0
    ultimo_refresh_grafico = 0.0
    # Cache dos dados estruturais (S/R, Fib, canal, indicadores H4) por ativo.
    # Preenchido no fechamento do H1; reutilizado nos refreshes de tick entre closes.
    _cache_estrutural: dict[str, dict] = {}

    def _refresh_tick_grafico() -> None:
        """Atualiza só os candles H1 recentes no dashboard sem recalcular estrutura.

        Roda entre fechamentos de H1 (a cada loop ~60s) para o gráfico mostrar o
        candle atual sendo formado, em vez de ficar congelado por até 1 hora.
        """
        if grafico is None or not _cache_estrutural:
            return
        for ativo, cached in _cache_estrutural.items():
            try:
                df_recente = mercado.candles_h1_recente(ativo, n=8)
                df_base = cached["df_h1_ind"]
                df_merged = pd.concat([df_base[~df_base.index.isin(df_recente.index)], df_recente])
                df_merged = estrategia._adicionar_indicadores(df_merged)
                grafico.atualizar_ativo(
                    ativo, df_merged, cached["df_h4_ind"], cached["tendencia_d1"],
                    cached["suportes"], cached["resistencias"], cached["zona_fib"],
                    mercado_aberto=cached["mercado_aberto"],
                    analise=cached.get("analise"),
                )
            except Exception:
                pass  # silencioso — refresh de tick é best-effort

    def _radar_intravela() -> None:
        if not _cache_estrutural:
            return
        for ativo, cached in _cache_estrutural.items():
            analise = cached.get("analise")
            if analise is None or analise.estado not in {"ENTRAR", "ESPERAR"}:
                continue
            try:
                preco = mercado.preco_atual_forex(ativo)
            except Exception:
                preco = None
            if preco is None or preco <= 0:
                continue
            radar = avaliar_radar(
                analise,
                preco,
                noticia_bloqueada=cached.get("noticia", NoticiaSwing(False)).bloqueada,
                noticia_desc=cached.get("noticia", NoticiaSwing(False)).descricao,
                noticia_direcao=cached.get("noticia", NoticiaSwing(False)).direcao,
                tolerancia_pips=config.radar_tolerancia_toque_pips,
            )
            if radar is None:
                continue
            if grafico is not None:
                try:
                    grafico.atualizar_alerta_tick(ativo, radar_para_alerta(radar, analise))
                except Exception:
                    pass
            chave_ant = _radar_anterior.get(ativo)
            if chave_ant != radar.chave_alerta:
                dist = "" if radar.distancia_pips is None else f" | dist={radar.distancia_pips:.1f}p"
                alvo = "" if radar.alvo_provavel is None else f" | busca={radar.alvo_provavel:.5f}"
                print(
                    f"[SWING-RADAR] {ativo} {radar.estado} "
                    f"{radar.direcao or '-'} | preco={radar.preco_atual:.5f}"
                    f"{alvo}{dist} | {radar.mensagem}"
                )
                _radar_anterior[ativo] = radar.chave_alerta

    ultimo_check_conexao = 0.0

    while True:
        try:
            agora_loop = time.time()

            # Sonda de conexão a cada 60s. Sem isso o freeze é SILENCIOSO: com o
            # socket morto, timestamp_servidor() devolve o último valor em cache
            # (property da iqoptionapi, não levanta erro), então _m15_atual()
            # congela, nenhum ciclo novo dispara e nada é impresso. Foi assim que
            # o bot M15 ficou 25min parado em 2026-08-27.
            if agora_loop - ultimo_check_conexao >= 60.0:
                ultimo_check_conexao = agora_loop
                if not mercado.conexao_viva():
                    print("[SWING] Conexão morta detectada — reconectando...")
                    mercado.reconectar_se_necessario(forcar=True)

            ts = mercado.timestamp_servidor()
            h1_corrente = _m15_atual(ts)

            # Verificacoes pesadas ficam cadenciadas; o radar intravela roda leve.
            if agora_loop - ultimo_check_monitor >= config.intervalo_verificacao_monitor_segundos:
                ultimo_check_monitor = agora_loop
                try:
                    executor.verificar_pendentes()
                except Exception as e:
                    print(f"[SWING] Erro ao verificar pendentes: {e!r}")

                try:
                    _verificar_sinais_monitor()
                except Exception as e:
                    print(f"[SWING] Erro ao verificar monitor: {e!r}")

            # Entre ciclos M15: radar de preco a cada segundo, candles do grafico em ritmo leve.
            if h1_corrente == ultimo_m15 and _cache_estrutural:
                try:
                    _radar_intravela()
                except Exception as e:
                    print(f"[SWING] Erro no radar intravela: {e!r}")
            if (
                h1_corrente == ultimo_m15
                and _cache_estrutural
                and agora_loop - ultimo_refresh_grafico >= config.intervalo_loop_segundos
            ):
                ultimo_refresh_grafico = agora_loop
                try:
                    _refresh_tick_grafico()
                except Exception as e:
                    print(f"[SWING] Erro no refresh de tick: {e!r}")

            # Avalia sinais a cada virada de M15
            if h1_corrente > ultimo_m15:
                ultimo_m15 = h1_corrente
                hora_utc = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                print(f"\n[SWING] === M15 close: {hora_utc} ===")

                # Filtro de notícias — bloqueia hora inteira se evento de alto impacto
                _noticia_bloqueada_atual, _noticia_desc_atual = verificar_noticias(
                    janela_minutos=config.janela_noticia_minutos
                )
                if _noticia_bloqueada_atual:
                    print(f"[SWING] ⚠ NOTÍCIA ALTO IMPACTO: {_noticia_desc_atual} — entradas bloqueadas; radar continua")

                # Sinais pendentes (aguardando SL/TP) por ativo — usados pra manter
                # o alerta visivel no dashboard enquanto nao resolve, nao so no
                # ciclo em que foi encontrado.
                _pendentes_por_ativo = {p["ativo"]: p for p in registro.monitor_pendentes()}

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

                        # Avalia primeiro — aproveita os cálculos no gráfico
                        analise = estrategia.avaliar(ativo, df_d1, df_h4, df_h1)
                        noticia_ativo = avaliar_noticias_ativo(
                            ativo,
                            janela_minutos=config.janela_noticia_minutos,
                        )
                        try:
                            preco_ref_registro = mercado.preco_atual_forex(ativo)
                        except Exception:
                            preco_ref_registro = float(df_h1["Close"].iloc[-1])
                        registro.registrar_analise_monitor(
                            analise,
                            preco_atual=preco_ref_registro,
                            noticia_bloqueada=noticia_ativo.bloqueada,
                            noticia_desc=noticia_ativo.descricao,
                            noticia_direcao=noticia_ativo.direcao,
                        )

                        # Publica no dashboard: H1 (leitura fina) + S/R/Fib/canal do H4
                        if grafico is not None:
                            try:
                                df_h1_ind = estrategia._adicionar_indicadores(df_h1)
                                df_h4_ind = estrategia._adicionar_indicadores(df_h4)
                                tendencia_d1 = analise.detalhes.get("tendencia_d1")
                                suportes, resistencias = estrategia._pivos_h4(df_h4)
                                zona_fib = (
                                    estrategia._zona_fibonacci_h4(df_h4, tendencia_d1)
                                    if tendencia_d1 else None
                                )
                                grafico.atualizar_ativo(
                                    ativo, df_h1_ind, df_h4_ind, tendencia_d1,
                                    suportes, resistencias, zona_fib,
                                    mercado_aberto=mercado.aberto(ativo),
                                    analise=analise,
                                )
                                _cache_estrutural[ativo] = {
                                    "df_h1_ind": df_h1_ind,
                                    "df_h4_ind": df_h4_ind,
                                    "tendencia_d1": tendencia_d1,
                                    "suportes": suportes,
                                    "resistencias": resistencias,
                                    "zona_fib": zona_fib,
                                    "mercado_aberto": mercado.aberto(ativo),
                                    "analise": analise,
                                    "noticia": noticia_ativo,
                                }
                            except Exception as e:
                                print(f"[SWING] {ativo}: falha ao atualizar gráfico — {e!r}")

                        # Detecta transição de estado e alerta
                        estado_ant = _estado_anterior.get(ativo)
                        if estado_ant and estado_ant != analise.estado:
                            print(f"\n[SWING] *** TRANSIÇÃO {ativo}: {estado_ant} → {analise.estado} ***\n")
                        _estado_anterior[ativo] = analise.estado

                        sinais_hora.append(analise)

                    except MercadoSwingIndisponivel as e:
                        print(f"[SWING] {ativo}: dados indisponíveis — {e}")
                    except Exception as e:
                        print(f"[SWING] {ativo}: erro inesperado — {e!r}")

                # Ordena por prontidão e imprime painel
                _ordem = {"ENTRAR": 0, "ESPERAR": 1, "EVITAR": 2}
                sinais_hora.sort(key=lambda a: _ordem.get(a.estado, 9))
                print(f"\n[SWING] === PAINEL ({len(sinais_hora)} pares) ===")
                for analise in sinais_hora:
                    _imprimir_analise(analise)

                # Alerta de correlação USD
                analises_entrar = [a for a in sinais_hora if a.estado == "ENTRAR"]
                _usd_fraco = [a for a in analises_entrar if a.direcao == "compra" and "USD" in a.ativo and not a.ativo.startswith("USD")]
                _usd_forte = [a for a in analises_entrar if a.direcao == "venda"  and "USD" in a.ativo and not a.ativo.startswith("USD")]
                _usd_fraco += [a for a in analises_entrar if a.direcao == "venda"  and a.ativo.startswith("USD")]
                _usd_forte += [a for a in analises_entrar if a.direcao == "compra" and a.ativo.startswith("USD")]
                if len(_usd_fraco) >= 3:
                    print(f"\n[SWING] ⚠ CORRELAÇÃO: {len(_usd_fraco)} pares ENTRAR apontam USD FRACO — risco concentrado\n")
                if len(_usd_forte) >= 3:
                    print(f"\n[SWING] ⚠ CORRELAÇÃO: {len(_usd_forte)} pares ENTRAR apontam USD FORTE — risco concentrado\n")

                # Execução de ordens (apenas quando executar_ordens=True e validado)
                for analise in analises_entrar:
                    noticia_exec = _cache_estrutural.get(analise.ativo, {}).get(
                        "noticia",
                        NoticiaSwing(_noticia_bloqueada_atual, _noticia_desc_atual),
                    )
                    noticia_a_favor = (
                        noticia_exec.bloqueada
                        and noticia_exec.direcao in {"compra", "venda"}
                        and noticia_exec.direcao == analise.direcao
                    )
                    if noticia_exec.bloqueada and not (
                        config.permitir_entrada_a_favor_noticia and noticia_a_favor
                    ):
                        extra = " a favor, mas modo notícia está só em monitor" if noticia_a_favor else ""
                        print(f"[SWING] {analise.ativo}: IGNORADO — notícia alto impacto{extra}")
                        continue
                    if not config.executar_ordens:
                        continue
                    sinal_leg = analise.como_sinal_legado()
                    if sinal_leg is None:
                        continue
                    try:
                        if registro.tem_sinal_pendente(analise.ativo):
                            print(f"[SWING] {analise.ativo}: IGNORADO — já existe pendente")
                            continue
                        atr_h4_exec = analise.detalhes.get("atr_h4", 0.0)
                        executor.executar(sinal_leg, atr_h4=atr_h4_exec)
                    except MercadoSwingIndisponivel as e:
                        print(f"[SWING] {analise.ativo}: dados indisponíveis — {e}")
                    except Exception as e:
                        print(f"[SWING] {analise.ativo}: erro inesperado — {e!r}")

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

        time.sleep(config.intervalo_radar_segundos)
