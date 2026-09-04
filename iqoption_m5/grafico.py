from __future__ import annotations

import functools
import http.server
import json
import os
import socket
import socketserver
import threading
import tempfile
import time
import webbrowser
from pathlib import Path

import pandas as pd

from .config import Configuracao
from .modelos import Decisao, SnapshotMercado


_handler_registro = None  # injetado por semear_historico


def _handle_opiniao_groq(handler) -> None:
    """Processa POST /opiniao_groq: consulta Groq e persiste a resposta."""
    try:
        from . import ia as _ia
        tamanho = int(handler.headers.get("Content-Length", 0))
        alerta_dados = json.loads(handler.rfile.read(tamanho)) if tamanho else {}
        resultado = _ia.segunda_opiniao_alerta(alerta_dados)
        if resultado is None:
            resultado = {"veredicto": "INCERTO", "motivo": "IA indisponível ou desativada.", "modelo": "", "latencia_ms": 0}
        if _handler_registro is not None:
            try:
                _handler_registro.registrar_opiniao_groq({**resultado, **{k: alerta_dados.get(k) for k in ("ativo", "setup", "direcao", "timeframe")}})
            except Exception:
                pass
        corpo = json.dumps(resultado, ensure_ascii=False).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.write(corpo)
    except Exception as e:
        handler.send_error(500, str(e))


def _handle_exportar(handler) -> None:
    """GET /exportar?data=YYYY-MM-DD — CSV das entradas do dia."""
    from urllib.parse import urlparse, parse_qs
    from datetime import date as _date
    try:
        params = parse_qs(urlparse(handler.path).query)
        data = params.get("data", [None])[0] or _date.today().isoformat()
        if _handler_registro is None:
            handler.send_error(503, "Registro nao disponivel")
            return
        entradas = _handler_registro.entradas_hoje_detalhadas(data=data)
        linhas = ["hora_brt,ativo,setup,direcao,resultado,lucro,veredicto_ia,motivo_ia"]
        for e in entradas:
            lucro = "" if e.get("lucro") is None else f"{e['lucro']:.2f}"
            st = e.get("status", "")
            if st == "finalizada":
                st = "win" if (e.get("lucro") or 0) > 0 else "loss"
            motivo = (e.get("motivoIA") or "").replace('"', "'")
            linhas.append(",".join([
                e.get("hora", ""),
                e.get("ativo", ""),
                f'"{e.get("setup", "")}"',
                e.get("direcao", ""),
                st,
                lucro,
                e.get("veredictoIA") or "",
                f'"{motivo}"',
            ]))
        corpo = "\n".join(linhas).encode("utf-8")
        data_safe = data.replace("-", "")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/csv; charset=utf-8")
        handler.send_header("Content-Disposition", f'attachment; filename="entradas_{data_safe}.csv"')
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.write(corpo)
    except Exception as e:
        handler.send_error(500, str(e))


class _ServidorReutilizavel(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        import sys
        excecao = sys.exc_info()[1]
        if isinstance(excecao, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class _HandlerSilencioso(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/exportar"):
            _handle_exportar(self)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/toggle"):
            try:
                tamanho = int(self.headers.get("Content-Length", 0))
                corpo = json.loads(self.rfile.read(tamanho)) if tamanho else {}
                arquivo = Path(self.directory) / "iqoption_m5" / "ativos_toggle.json"
                arquivo.parent.mkdir(parents=True, exist_ok=True)
                arquivo.write_text(json.dumps(corpo, ensure_ascii=False), encoding="utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception:
                self.send_error(500)
        elif self.path.startswith("/opiniao_groq"):
            _handle_opiniao_groq(self)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class GraficoM5:
    """Uma chamada atualiza o painel; HTTP, JSON e navegador ficam internos."""

    def __init__(self, config: Configuracao):
        self.config = config
        self.pasta_web_origem = Path(__file__).resolve().parent.parent / "grafico_web"
        raiz_local = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
        # Diretório separado por bot para evitar conflito de dados entre M1/M15/M5
        sufixo = config.sufixo_banco or "default"
        self.pasta_web = raiz_local / "IQOptionM5" / f"grafico_web_{sufixo}"
        self.pasta_dados = self.pasta_web / "iqoption_m5"
        self.servidor: _ServidorReutilizavel | None = None
        self.porta: int | None = None
        self._lock = threading.Lock()
        self._toggle_cache: set[str] | None = None
        self._toggle_cache_em: float = 0.0

    def ativos_ativos(self) -> set[str] | None:
        agora = time.time()
        if agora - self._toggle_cache_em < 3.0:
            return self._toggle_cache
        self._toggle_cache_em = agora
        arquivo = self.pasta_dados / "ativos_toggle.json"
        if not arquivo.exists():
            self._toggle_cache = None
            return None
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
            # O arquivo persiste entre reinícios. Quando um perfil recebe
            # novos ativos, eles não podem ficar invisíveis por não existirem
            # no toggle antigo: ausente significa ligado por padrão.
            self._toggle_cache = {
                ativo for ativo in self.config.ativos if dados.get(ativo, True)
            }
        except (OSError, json.JSONDecodeError):
            self._toggle_cache = None
        return self._toggle_cache

    @staticmethod
    def _unix(valor) -> int:
        timestamp = pd.Timestamp(valor)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        return int(timestamp.timestamp())

    @staticmethod
    def _json_atomico(caminho: Path, dados: dict) -> None:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        conteudo = json.dumps(dados, ensure_ascii=False, allow_nan=False).encode("utf-8")
        temporario = caminho.with_name(
            f".{caminho.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporario.write_bytes(conteudo)
            for tentativa in range(8):
                try:
                    os.replace(temporario, caminho)
                    return
                except PermissionError:
                    if tentativa == 7:
                        # No Windows o browser pode manter o arquivo aberto por um frame.
                        # Fallback: sobrescreve direto (não atômico mas não trava).
                        caminho.write_bytes(conteudo)
                        return
                    time.sleep(0.01 * (tentativa + 1))
        finally:
            try:
                temporario.unlink(missing_ok=True)
            except OSError:
                pass

    def iniciar(self, abrir_navegador: bool = True) -> str:
        origem_html = self.pasta_web_origem / "index.html"
        if not origem_html.exists():
            raise RuntimeError(f"Painel web não encontrado em {self.pasta_web_origem}")
        self.pasta_web.mkdir(parents=True, exist_ok=True)
        (self.pasta_web / "index.html").write_bytes(origem_html.read_bytes())
        manifesto = {"ativos": [{"id": ativo, "label": ativo} for ativo in self.config.ativos]}
        self._json_atomico(self.pasta_dados / "manifest.json", manifesto)
        handler = functools.partial(_HandlerSilencioso, directory=str(self.pasta_web))
        ultimo_erro = None
        for porta in range(self.config.porta_grafico, self.config.porta_grafico + 10):
            try:
                self.servidor = _ServidorReutilizavel(("127.0.0.1", porta), handler)
                self.porta = porta
                break
            except OSError as erro:
                ultimo_erro = erro
        if self.servidor is None or self.porta is None:
            raise RuntimeError(f"Nenhuma porta disponível para o gráfico: {ultimo_erro}")
        self.servidor.timeout = 1.0
        threading.Thread(target=self.servidor.serve_forever, name="grafico-m5", daemon=True).start()
        url = f"http://127.0.0.1:{self.porta}/index.html?fonte=iqoption_m5"
        if abrir_navegador:
            webbrowser.open(url)
        return url

    @staticmethod
    def _serie(df: pd.DataFrame, coluna: str, conversor) -> list[dict]:
        if coluna not in df:
            return []
        return [
            {"time": conversor(indice), "value": float(valor)}
            for indice, valor in df[coluna].dropna().items()
        ]

    def montar_dados(
        self,
        snapshot: SnapshotMercado,
        indicadores: pd.DataFrame,
        sinais: list[Decisao],
        possivel: dict | None,
        operacoes: list[dict],
        alerta: dict | None = None,
        noticias: list[dict] | None = None,
        explicacao: list[str] | None = None,
        parecer_ia: dict | None = None,
        gatilhos: dict | None = None,
        desempenho: dict | None = None,
        desempenho_por_setup: dict | None = None,
        desempenho_simulado_por_setup: dict | None = None,
        funil: dict | None = None,
        stats_globais: dict | None = None,
        entradas_detalhadas: list | None = None,
        niveis_sr: dict | None = None,
        plano_forex: dict | None = None,
        movimentos_unicos: dict | None = None,
        fibo_contexto: dict | None = None,
        analise_atraso: dict | None = None,
    ) -> dict:
        conversor = self._unix
        fibo_contexto_saida = dict(fibo_contexto) if fibo_contexto else None
        if fibo_contexto_saida:
            for campo in ("inicio", "fim"):
                if fibo_contexto_saida.get(campo) is not None:
                    fibo_contexto_saida[f"{campo}_time"] = conversor(
                        fibo_contexto_saida[campo]
                    )
                    fibo_contexto_saida[campo] = str(fibo_contexto_saida[campo])
        candles = [
            {
                "time": conversor(indice), "open": float(row.Open), "high": float(row.High),
                "low": float(row.Low), "close": float(row.Close),
            }
            for indice, row in indicadores.iterrows()
        ]
        volume = [
            {
                "time": conversor(indice), "value": float(row.Volume),
                "color": "#26a69a80" if row.Close >= row.Open else "#ef535080",
            }
            for indice, row in indicadores.iterrows()
        ]
        ultimo = indicadores.iloc[-1]
        tendencia_macro = str(ultimo.get("TendenciaMacro", "lateral"))
        tendencia_micro = "alta" if ultimo.get("EMA_Micro", 0) > ultimo.get("EMA_Macro", 0) else "baixa"
        # Janela de S/R equivalente a ~5h independente do timeframe.
        # M1=300, M5=60, M15=20 — tail(N) em DataFrame menor retorna tudo.
        candles_sr = max(20, round(18000 // max(1, self.config.timeframe_segundos)))
        janela_niveis = indicadores.tail(candles_sr)
        suporte = float(janela_niveis["Low"].min())
        resistencia = float(janela_niveis["High"].max())
        amplitude = resistencia - suporte
        if niveis_sr:
            niveis = (
                [{"tipo": "suporte",     "preco": p} for p in niveis_sr.get("suportes",     [])] +
                [{"tipo": "resistencia", "preco": p} for p in niveis_sr.get("resistencias", [])]
            )
        else:
            niveis = [
                {"tipo": "suporte",     "preco": suporte},
                {"tipo": "resistencia", "preco": resistencia},
            ]
        # Sem impulso confirmado não existe Fibo. Usar simplesmente a máxima e
        # a mínima da janela criava níveis visualmente convincentes, porém sem
        # relação com um movimento negociável.
        fib = list(fibo_contexto_saida.get("niveis", [])) if fibo_contexto_saida else []
        sinais_reversao = []
        pullbacks = []
        confluencias = []
        for sinal in sinais:
            item = {
                "time": conversor(sinal.candle_hora),
                "direcao": "compra" if sinal.direcao == "call" else "venda",
                "preco": sinal.preco,
                "status": sinal.detalhes.get("status_grafico", "confirmado"),
                "fatores": list(sinal.detalhes.get("fatores", [])),
                "setup": sinal.detalhes.get("setup", sinal.motivo),
                "razao": list(sinal.detalhes.get("razao", [])),
                "leituraM5": sinal.detalhes.get("leitura_m5"),
                "zonaFib": sinal.detalhes.get("zona_fib"),
                "nivelSr": sinal.detalhes.get("nivel_sr"),
            }
            if sinal.detalhes.get("setup") in ("pullback", "pullback_confluencia"):
                pullbacks.append(item)
                if len(item["fatores"]) >= 2:
                    confluencias.append(item)
            else:
                sinais_reversao.append(item)
        return {
            "par": snapshot.ativo,
            "timeframe": self.config.rotulo_timeframe,
            "timeframeSeg": self.config.timeframe_segundos,
            "janelaEntradaSeg": self.config.entrada_max_segundos_no_candle,
            "atualizado_em": time.time(),
            "mercadoAberto": bool(snapshot.mercado_aberto),
            "payout": snapshot.payout,
            "candles": candles,
            "volume": volume,
            "bandaSup": self._serie(indicadores, "BandaSup", conversor),
            "bandaInf": self._serie(indicadores, "BandaInf", conversor),
            "bandaMedia": self._serie(indicadores, "BandaMedia", conversor),
            "emaMicro": self._serie(indicadores, "EMA_Micro", conversor),
            "emaMacro": self._serie(indicadores, "EMA_Macro", conversor),
            "rsi": self._serie(indicadores, "RSI", conversor),
            "tendenciaMacro": tendencia_macro,
            "tendenciaMicro": tendencia_micro,
            "pullbacks": pullbacks,
            "niveis": niveis,
            "fib": fib,
            "fibContexto": fibo_contexto_saida,
            "confluencias": confluencias,
            "sinais": sinais_reversao,
            "alertaProximo": (
                {
                    "time": conversor(possivel["hora"]),
                    "direcao": "compra_proxima" if possivel["direcao"] == "call_proxima" else "venda_proxima",
                    "preco": possivel["preco"],
                    "rsi": possivel.get("rsi"),
                    "setup": possivel.get("setup", "reversao_bollinger_rsi"),
                    "fatores": list(possivel.get("fatores", [])),
                }
                if possivel else None
            ),
            "operacoesReais": [
                {
                    "time": conversor(op["hora"]), "direcao": op["direcao"],
                    "vitoria": op["lucro"] is not None and op["lucro"] > 0,
                    "aberta": op["status"] == "aberta", "lucro": op["lucro"],
                    "status": op["status"],
                    "setup": op.get("setup", ""), "preco": op.get("preco"),
                    "razao": list(op.get("razao", [])),
                }
                for op in operacoes
            ],
            "alerta": alerta,
            # Campo proprio: o plano forex tem de aparecer mesmo quando existe
            # outro alerta. Antes ele so ia como fallback de `alerta` e sumia
            # justamente quando havia mais coisa acontecendo na tela.
            "planoForex": plano_forex,
            "explicacao": list(explicacao or []),
            "noticias": list(noticias or []),
            "parecerIA": parecer_ia,
            "gatilhos": gatilhos,
            "desempenho": desempenho,
            "desempenhoPorSetup": desempenho_por_setup,
            "desempenhoSimuladoPorSetup": desempenho_simulado_por_setup,
            "funil": funil,
            "statsGlobais": stats_globais,
            "entradasDetalhadas": entradas_detalhadas,
            "niveisSR": niveis_sr,
            "movimentosUnicos": movimentos_unicos,
            "analiseAtraso": analise_atraso,
        }

    def atualizar(self, ativo: str, dados: dict) -> None:
        with self._lock:
            self._json_atomico(self.pasta_dados / f"{ativo}.json", dados)

    def semear_historico(self, registro) -> None:
        global _handler_registro
        _handler_registro = registro
        """Escreve historico_hoje.json com statsGlobais e entradasDetalhadas do SQLite.

        Também arquiva um snapshot datado (historico_YYYY-MM-DD.json) para o dia
        anterior (se ainda não existir) e para hoje, permitindo navegação histórica.

        Chamado logo após iniciar() para que o frontend mostre o histórico do dia
        imediatamente, mesmo antes do primeiro ciclo de candle carregar os JSONs ao vivo.
        """
        from datetime import date, timedelta
        try:
            hoje_str = date.today().isoformat()
            ontem_str = (date.today() - timedelta(days=1)).isoformat()
            stats = registro.stats_globais()
            entradas = registro.entradas_hoje_detalhadas()
            atraso = registro.analise_atraso_por_setup()
            precisao_ia = registro.precisao_ia()
            desemp_hora = registro.desempenho_por_hora()
        except Exception as erro:
            print(f"[grafico] semear_historico falhou: {erro}")
            return
        dados = {"statsGlobais": stats, "entradasDetalhadas": entradas,
                 "analiseAtraso": atraso, "precisaoIA": precisao_ia,
                 "desempenhoPorHora": desemp_hora}
        self._json_atomico(self.pasta_dados / "historico_hoje.json", dados)

        # Snapshot datado de hoje (pode ser parcial — atualizado a cada startup)
        self._json_atomico(
            self.pasta_dados / f"historico_{hoje_str}.json",
            {"data": hoje_str, "entradasDetalhadas": entradas, "statsGlobais": stats},
        )

        # Snapshot do dia anterior se ainda não foi arquivado
        arquivo_ontem = self.pasta_dados / f"historico_{ontem_str}.json"
        if not arquivo_ontem.exists():
            try:
                entradas_ontem = registro.entradas_hoje_detalhadas(data=ontem_str)
                if entradas_ontem:
                    stats_ontem = registro.stats_globais()
                    self._json_atomico(arquivo_ontem, {
                        "data": ontem_str,
                        "entradasDetalhadas": entradas_ontem,
                        "statsGlobais": stats_ontem,
                    })
            except Exception:
                pass

    def fechar(self) -> None:
        if self.servidor is not None:
            self.servidor.shutdown()
            self.servidor.server_close()
            self.servidor = None
