"""Simula sinais salvos contra candles posteriores; não envia ordens."""
from __future__ import annotations
import csv, json, os, shutil, subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = Path.home() / "AppData" / "Local" / "IQOptionM5"
OUT = ROOT / "diario" / "simulacao_resultados.csv"
OUT_XLSX = ROOT / "diario" / "simulacao_resultados.xlsx"
OUT_JSON = ROOT / "diario" / "simulacao_resultados.json"
WEB = APP / "grafico_web_forex_monitor"


def parse_dt(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try: return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError): pass
    return None


def candles(ativo: str) -> list[dict]:
    arquivos = [APP / "grafico_web_forex_monitor" / f"candles_{ativo}.json",
                APP / "grafico_web_crypto_monitor" / f"candles_{ativo}.json"]
    for arq in arquivos:
        if arq.exists():
            try: return json.loads(arq.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError): return []
    return []


def simular(linha: dict, cripto: bool = False) -> dict:
    ativo = linha.get("ativo", "")
    entrada = float(linha.get("entrada_planejada" if not cripto else "preco", 0) or 0)
    sl = float(linha.get("sl" if not cripto else "invalidacao", 0) or 0)
    tp = float(linha.get("tp" if not cripto else "alvo", 0) or 0)
    inicio = parse_dt(linha.get("candle_rompimento", "") if not cripto else f"{linha.get('data','')} {linha.get('hora_utc','')}")
    resultado = "INCONCLUSIVO"
    candles_analisados = 0
    futuros = []
    if inicio and entrada and sl and tp:
        encontrou_futuro = False
        for c in candles(ativo):
            ts = datetime.fromtimestamp(int(c.get("t", 0)), timezone.utc)
            if ts <= inicio: continue
            encontrou_futuro = True
            candles_analisados += 1
            futuros.append(c)
            h, l = float(c["h"]), float(c["l"])
            compra = tp > entrada
            bate_tp, bate_sl = (h >= tp, l <= sl) if compra else (l <= tp, h >= sl)
            if bate_tp and bate_sl: resultado = "INCONCLUSIVO_AMBOS_NO_MESMO_CANDLE"; break
            if bate_tp: resultado = "WIN"; break
            if bate_sl: resultado = "LOSS"; break
        else: resultado = "EXPIRADO" if encontrou_futuro else "SEM_DADOS_FUTUROS"
    def binaria(n: int) -> str:
        if len(futuros) < n: return "SEM_DADOS_FUTUROS"
        fechamento = float(futuros[n - 1].get("c", 0) or 0)
        if not fechamento: return "SEM_DADOS_FUTUROS"
        lado = str(linha.get("lado", "COMPRA")).upper()
        venceu = fechamento > entrada if lado in {"COMPRA", "CALL", "BUY"} else fechamento < entrada
        return "WIN" if venceu else "LOSS"
    saida = dict(linha); saida.update({"resultado_simulado": resultado, "binaria_30m": binaria(2), "binaria_60m": binaria(4), "candles_analisados": candles_analisados, "simulacao_em_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
    return saida


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    saidas = []
    fontes = [(Path.home()/"Documents"/"IQOptionM5"/"diario"/"forex_sinais.csv", False),
              (Path.home()/"Documents"/"IQOptionM5"/"diario"/"crypto"/"crypto_sinais.csv", True)]
    for arq, cripto in fontes:
        if arq.exists():
            with open(arq, newline="", encoding="utf-8") as f:
                saidas.extend(simular(x, cripto) for x in csv.DictReader(f))
    campos = sorted({k for x in saidas for k in x}) or ["resultado_simulado"]
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos); w.writeheader(); w.writerows(saidas)
    resumo = {}
    for x in saidas: resumo[x["resultado_simulado"]] = resumo.get(x["resultado_simulado"], 0) + 1
    payload = {"atualizado_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "resumo": resumo, "sinais": saidas}
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        builder = ROOT / ".artifact_runtime" / ".artifact_builder.mjs"
        if builder.exists():
            node = os.environ.get("IQOPTION_NODE") or shutil.which("node") or r"C:\Users\gilso\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
            subprocess.run([node, str(builder), str(OUT), str(OUT_XLSX)], check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        WEB.mkdir(parents=True, exist_ok=True)
        (WEB / "simulacao_resultados.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    print(f"Simulados: {len(saidas)} | {resumo}")
    print(f"Planilha: {OUT_XLSX}")


if __name__ == "__main__": main()
