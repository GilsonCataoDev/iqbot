"""Monitor de cripto separado do monitor Forex (somente leitura/alerta)."""
from __future__ import annotations

import dataclasses
import csv
import json
import sys
import threading
import time
import webbrowser
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

from monitor_forex import (
    ServidorDadosForex, DiarioTrades, analisar_leitura_mercado, detectar_sinal,
    configuracao_scalping_m15, AnalistaGemini,
)
from iqoption_m5 import backtest
from iqoption_m5.grafico import GraficoM5
from iqoption_m5.mercado_iq import MercadoIQ

CRYPTO_ATIVOS_PADRAO = [
    "BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD", "BCHUSD", "EOSUSD", "ETCUSD",
    "DASHUSD", "TRXUSD", "ZECUSD", "ADAUSD", "SOLUSD", "DOTUSD", "LINKUSD",
    "UNIUSD", "DOGEUSD",
]
CRYPTO_ATIVOS = [x.strip().upper() for x in __import__("os").getenv(
    "CRYPTO_ATIVOS", ",".join(CRYPTO_ATIVOS_PADRAO)).split(",") if x.strip()]
PORTA_CRYPTO = 8776
_NEWS_CACHE = {"at": 0.0, "items": []}

def noticias_crypto(ativo: str) -> dict:
    """Busca manchetes públicas de cripto; falha fechada, sem bloquear o monitor."""
    agora = time.time()
    if agora - _NEWS_CACHE["at"] > 300:
        itens = []
        try:
            req = urllib.request.Request("https://www.coindesk.com/arc/outboundfeeds/rss/", headers={"User-Agent": "IQOptionM5-monitor/1.0"})
            xml = urllib.request.urlopen(req, timeout=5).read()
            raiz = ET.fromstring(xml)
            palavras = ("bitcoin", "btc", "ethereum", "eth", "crypto", "fed", "etf", "sec", "hack", "regulation")
            for item in raiz.findall(".//item")[:30]:
                titulo = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                publicado = item.findtext("pubDate") or ""
                try:
                    publicado_dt = parsedate_to_datetime(publicado).astimezone(timezone.utc)
                    publicado_ts = publicado_dt.timestamp()
                    publicado_txt = publicado_dt.strftime("%H:%M")
                except (TypeError, ValueError, OverflowError):
                    publicado_ts, publicado_txt = agora, "agora"
                baixo = titulo.lower()
                if any(p in baixo for p in palavras):
                    impacto = "HIGH" if any(p in baixo for p in ("hack", "sec", "regulation", "fed", "etf", "lawsuit")) else "MEDIUM"
                    itens.append({"titulo": titulo, "link": link, "impacto": impacto, "moeda": "CRYPTO", "horario_utc": publicado_txt, "publicado_ts": publicado_ts})
        except Exception:
            itens = []
        _NEWS_CACHE.update({"at": agora, "items": itens})
    chave = "btc" if ativo.upper().startswith("BTC") else ("eth" if ativo.upper().startswith("ETH") else ativo[:3].lower())
    eventos = []
    for x in _NEWS_CACHE["items"]:
        if not (chave in x["titulo"].lower() or "crypto" in x["titulo"].lower()): continue
        minutos = int(round((x.get("publicado_ts", agora) - agora) / 60))
        if abs(minutos) > 120: continue
        eventos.append({**x, "minutos": minutos})
    alto = any(x["impacto"] == "HIGH" and abs(x.get("minutos", 999)) <= 20 for x in eventos)
    return {"ativo": ativo, "status": "EVITAR" if alto else "LIVRE", "direcao_noticia": None,
            "janela_minutos": 20 if alto else 0, "eventos": eventos[:5],
            "nota": "Notícias cripto: aguarde confirmação do preço; notícia forte aumenta a volatilidade." if eventos else "Sem manchetes cripto relevantes no feed consultado."}


class DiarioCrypto:
    CAB = ["data", "hora_utc", "ativo", "status", "lado", "preco", "zona_min", "zona_max",
           "alvo", "invalidacao", "rr", "qualidade"]
    def __init__(self, pasta: Path):
        self.arquivo = pasta / "crypto_sinais.csv"
        self._vistos: set[str] = set()
        pasta.mkdir(parents=True, exist_ok=True)
        if not self.arquivo.exists():
            with open(self.arquivo, "w", newline="", encoding="utf-8") as f: csv.writer(f).writerow(self.CAB)

    def registrar(self, leitura) -> None:
        if leitura is None or leitura.rr_estimado < 1.5: return
        agora = datetime.now(timezone.utc)
        chave = f"{leitura.ativo}:{agora.strftime('%Y%m%d%H%M')[:11]}:{leitura.lado_entrada}:{round(leitura.alvo,2)}"
        if chave in self._vistos: return
        self._vistos.add(chave)
        status = "ENTRADA_NA_ZONA" if leitura.estado_preco == "PREÇO NA ZONA" else "AGUARDAR_ZONA"
        with open(self.arquivo, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([agora.strftime("%Y-%m-%d"), agora.strftime("%H:%M:%S"), leitura.ativo,
                status, leitura.lado_entrada, leitura.preco, leitura.entrada_min, leitura.entrada_max,
                leitura.alvo, leitura.invalidacao, round(leitura.rr_estimado, 2), leitura.qualidade])


def filtrar_ativos_disponiveis(api) -> list[str]:
    """Usa a lista da IQ para não requisitar símbolos inexistentes/OTC."""
    try:
        abertos = api.get_all_open_time() or {}
        bloco = abertos.get("crypto", {})
        disponiveis = {str(k).upper().replace("-OTC", "") for k, v in bloco.items()
                       if isinstance(v, dict) and v.get("open", True) and "-OTC" not in str(k).upper()}
        filtrados = [a for a in CRYPTO_ATIVOS if a in disponiveis]
        return filtrados or CRYPTO_ATIVOS[:2]
    except Exception:
        return CRYPTO_ATIVOS[:2]


def loop_crypto(api_ref, servidor, config, diario, crypto_diario, ativos, gemini) -> None:
    cfg = dataclasses.replace(config, timeframe_segundos=900)
    ultimo_erro = None
    while True:
        api = api_ref["api"]
        falhas = 0
        for ativo in ativos:
            try:
                df = backtest.baixar_historico(api, cfg, ativo, 300)
                servidor.salvar_candles(ativo, df)
                leitura = analisar_leitura_mercado(ativo, df)
                servidor.atualizar_leitura(leitura, ativo)
                noticias = noticias_crypto(ativo)
                servidor.atualizar_noticias(ativo, noticias)
                if leitura is not None and gemini is not None:
                    gemini.solicitar(leitura, lambda par, analise: servidor.atualizar_ia(par, analise), noticias)
                if leitura is not None:
                    diario.registrar_leitura(leitura, {"status": "LIVRE", "direcao_noticia": ""})
                    crypto_diario.registrar(leitura)
                sinal = detectar_sinal(ativo, df)
                if sinal is not None:
                    diario.registrar_sinal(sinal)
                    servidor.atualizar_sinal(sinal, ativo)
                else:
                    servidor.atualizar_sinal(None, ativo)
                servidor.atualizar_horario(ativo, {
                    "ativo": ativo, "nivel": "24H", "sessoes_ativas": ["CRIPTO"],
                    "moedas": ativo[:3] + "/" + ativo[3:], "melhor_horario_utc": "24 horas",
                    "nota": "Cripto funciona 24/7; volatilidade e liquidez variam por horário.",
                    "hora_utc": datetime.now(timezone.utc).strftime("%H:%M")})
                servidor.atualizar_execucao(ativo, {"ativo": ativo, "status": "INFO",
                    "spread_estimado_pips": None, "limite_pips": None,
                    "nota": "Confirme o spread e o instrumento na IQ antes de operar."})
            except Exception as exc:
                falhas += 1
                chave_erro = f"{ativo}:{type(exc).__name__}"
                if chave_erro != ultimo_erro:
                    print(f"[CRIPTO] {ativo}: conexão indisponível; tentando reconectar...")
                    ultimo_erro = chave_erro
                if falhas < 2:
                    continue
                try:
                    novo = MercadoIQ(config).conectar_somente_leitura()
                    ativos[:] = filtrar_ativos_disponiveis(novo)
                    for par in ativos:
                        try: novo.start_candles_stream(par, 900, 60)
                        except Exception: pass
                    api_ref["api"] = novo
                    servidor.set_status("CRIPTO reconectando...")
                    break
                except Exception:
                    servidor.set_status(f"CRIPTO offline ({type(exc).__name__})")
        servidor.set_status(f"CRIPTO OK — {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
        time.sleep(30)


def main() -> int:
    config = configuracao_scalping_m15()
    mercado = MercadoIQ(config)
    api = mercado.conectar_somente_leitura()
    cfg = dataclasses.replace(config, porta_grafico=PORTA_CRYPTO, sufixo_banco="crypto_monitor")
    grafico = GraficoM5(cfg)
    grafico.iniciar(abrir_navegador=False)
    ativos = filtrar_ativos_disponiveis(api)
    print(f"Criptos normais disponíveis: {', '.join(ativos)}")
    html = __import__("monitor_forex")._HTML.replace("Monitor Forex M15", "Monitor Cripto M15/H1")
    html = html.replace("GESTÃO DA BANCA", "GESTÃO CRIPTO — RESERVA R$40")
    html = html.replace('id="saldo" type="number" value="60"', 'id="saldo" type="number" value="40"')
    html = html.replace('Saldo: <input', 'Reserva cripto (separada do Forex): <input')
    html = html.replace('["EURUSD","GBPUSD","USDJPY","AUDUSD","EURJPY","USDCAD","NZDUSD"]',
                        json.dumps(ativos))
    (grafico.pasta_web / "index.html").write_text(html, encoding="utf-8")
    servidor = ServidorDadosForex(grafico.pasta_web)
    for ativo in ativos:
        try: api.start_candles_stream(ativo, 900, 60)
        except Exception: pass
    pasta_diario = Path.home() / "Documents" / "IQOptionM5" / "diario" / "crypto"
    pasta_diario.mkdir(parents=True, exist_ok=True)
    diario = DiarioTrades(pasta_diario)
    crypto_diario = DiarioCrypto(pasta_diario)
    gemini = AnalistaGemini(intervalo_s=300)
    print("IA cripto: Gemini=" + ("ATIVO" if gemini.gemini_ativo else "OFF") + " | Groq=" + ("ATIVO" if gemini.groq_ativo else "OFF"))
    api_ref = {"api": api}
    threading.Thread(target=loop_crypto, args=(api_ref, servidor, config, diario, crypto_diario, ativos, gemini), daemon=True).start()
    url = f"http://127.0.0.1:{grafico.porta or PORTA_CRYPTO}/index.html"
    print(f"Monitor cripto: {url}")
    webbrowser.open(url)
    while True: time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
