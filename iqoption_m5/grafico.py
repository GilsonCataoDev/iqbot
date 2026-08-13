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
        self.pasta_web = raiz_local / "IQOptionM5" / "grafico_web"
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
            self._toggle_cache = {ativo for ativo, ligado in dados.items() if ligado}
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
    ) -> dict:
        conversor = self._unix
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
        janela_niveis = indicadores.tail(60)
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
        fib = [
            {"nivel": nivel, "preco": suporte + amplitude * nivel}
            for nivel in (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
        ] if amplitude > 0 else []
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
                }
                for op in operacoes
            ],
            "alerta": alerta,
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
        }

    def atualizar(self, ativo: str, dados: dict) -> None:
        with self._lock:
            self._json_atomico(self.pasta_dados / f"{ativo}.json", dados)

    def fechar(self) -> None:
        if self.servidor is not None:
            self.servidor.shutdown()
            self.servidor.server_close()
            self.servidor = None
