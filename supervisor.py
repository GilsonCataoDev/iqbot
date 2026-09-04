"""Supervisor dos monitores: reinicia se cair E se travar.

Duas falhas ja observadas neste projeto:

  1. PROCESSO MORRE — aconteceu em 01/09/2026: os monitores eram filhos da
     sessao que os lancou e cairam junto, ficando 17h parados sem ninguem notar.

  2. PROCESSO VIVO MAS TRAVADO — aconteceu antes: o WebSocket da IQ ficou
     mudo, o processo seguia consumindo CPU e o loop rodava, mas nenhum dado
     novo saia. Um `while true; do python ...; done` NAO pega esse caso, porque
     o processo nunca termina.

Por isso a supervisao e por HEARTBEAT: cada monitor escreve um JSON a cada
ciclo, e o supervisor olha a idade desse arquivo. Parou de escrever ha mais
de N minutos -> mata e sobe de novo, mesmo que o processo pareca saudavel.

Protecao contra loop de crash: se reiniciar mais de MAX_REINICIOS vezes em
JANELA_MIN minutos, para de tentar aquele alvo e registra no log — melhor
ficar parado e visivel do que reiniciar em looping escondido.

Uso:
    SUPERVISOR.bat
    python supervisor.py --status     (so mostra o estado, nao supervisiona)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
LOCAL = Path(os.environ.get("LOCALAPPDATA") or "") / "IQOptionM5"
LOG = RAIZ / "diario" / "supervisor.log"

INTERVALO_CHECK_S = 60
MAX_REINICIOS = 5          # por alvo
JANELA_MIN = 30            # janela para contar reinicios
ESPERA_APOS_START_S = 360  # conectar + carregar 21 ativos leva ~90-150s;
                           # 120s era curto e o supervisor matava durante a carga


class Alvo:
    def __init__(self, nome: str, script: str, heartbeat: Path, max_parado_min: int):
        self.nome = nome
        self.script = script
        self.heartbeat = heartbeat
        self.max_parado_min = max_parado_min
        self.proc: subprocess.Popen | None = None
        self.reinicios: deque[float] = deque()
        self.desistiu = False
        self.iniciado_em = 0.0

    # -- estado ------------------------------------------------------------
    def vivo(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def parado_min(self) -> float | None:
        if not self.heartbeat.exists():
            return None
        idade = time.time() - self.heartbeat.stat().st_mtime
        return idade / 60

    def travado(self) -> bool:
        # so avalia depois da janela de carencia (conectar + carregar historico)
        if time.time() - self.iniciado_em < ESPERA_APOS_START_S:
            return False
        if not self.heartbeat.exists():
            return True
        mtime = self.heartbeat.stat().st_mtime
        # BUG CORRIGIDO: antes usava a idade ABSOLUTA do arquivo, entao logo
        # apos subir o supervisor via o JSON velho da execucao anterior e
        # matava um processo saudavel que ainda estava carregando. O que
        # importa e se ESTE processo ja escreveu — mtime posterior ao start.
        if mtime < self.iniciado_em:
            return True          # nao escreveu nada desde que subiu
        return (time.time() - mtime) / 60 > self.max_parado_min

    # -- acoes -------------------------------------------------------------
    def matar(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception as e:
                log(f"[{self.nome}] erro ao matar: {e!r}")
        self.proc = None

    def subir(self, motivo: str) -> bool:
        agora = time.time()
        # limpa reinicios fora da janela
        while self.reinicios and agora - self.reinicios[0] > JANELA_MIN * 60:
            self.reinicios.popleft()
        if len(self.reinicios) >= MAX_REINICIOS:
            if not self.desistiu:
                log(f"[{self.nome}] DESISTINDO — {len(self.reinicios)} reinicios "
                    f"em {JANELA_MIN}min. Investigue antes de religar.")
                self.desistiu = True
            return False

        self.matar()
        env = carregar_env()
        env["PYTHONUNBUFFERED"] = "1"
        if not env.get("IQ_OPTION_EMAIL") or not env.get("IQ_OPTION_SENHA"):
            log(f"[{self.nome}] SEM CREDENCIAIS no ambiente nem no .env.bat — "
                f"o monitor ficaria travado no prompt de email. Nao vou subir.")
            self.desistiu = True
            return False
        saida = RAIZ / "diario" / f"log_{self.script.replace('.py','')}.txt"
        saida.parent.mkdir(parents=True, exist_ok=True)
        try:
            f = open(saida, "a", encoding="utf-8", errors="replace")
            f.write(f"\n===== start {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC "
                    f"({motivo}) =====\n")
            f.flush()
            self.proc = subprocess.Popen(
                [sys.executable, "-u", self.script],
                cwd=str(RAIZ), env=env, stdout=f, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,   # input() falha rapido em vez de travar
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            log(f"[{self.nome}] falhou ao subir: {e!r}")
            return False
        self.reinicios.append(agora)
        self.iniciado_em = agora
        log(f"[{self.nome}] iniciado (pid={self.proc.pid}) — {motivo}")
        return True


def carregar_env() -> dict[str, str]:
    """Le .env.bat e devolve as variaveis.

    CAUSA RAIZ de 02/09/2026: os monitores subiam, nao encontravam
    IQ_OPTION_EMAIL no ambiente e BLOQUEAVAM em input() pedindo o email.
    Nunca escreviam heartbeat, e o supervisor os matava achando que estavam
    travados — um ciclo de reinicio que so o log do monitor revelava.
    Ler o .env.bat aqui torna o supervisor independente de como foi lancado.
    """
    env = dict(os.environ)
    arq = RAIZ / ".env.bat"
    if not arq.exists():
        return env
    for linha in arq.read_text(encoding="utf-8", errors="replace").splitlines():
        t = linha.strip()
        if not t.lower().startswith("set "):
            continue
        corpo = t[4:]
        if "=" not in corpo:
            continue
        k, v = corpo.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def log(msg: str) -> None:
    linha = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC  {msg}"
    print(linha, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass


def alvos() -> list[Alvo]:
    return [
        # o forward test escreve o JSON a cada ciclo de 20s; 10min de folga
        # cobre um ciclo lento sem disparar reinicio a toa
        Alvo("forward-binaria", "monitor_binaria.py",
             LOCAL / "grafico_web_binaria_fwd" / "binaria.json", 10),
        Alvo("monitor-mercado", "monitor_mercado.py",
             LOCAL / "grafico_web_mercado" / "mercado.json", 10),
        Alvo("forward-forex", "monitor_forex_fwd.py",
             LOCAL / "grafico_web_forex_fwd" / "forexfwd.json", 10),
    ]


def mostrar_status() -> int:
    print(f"{'alvo':18} {'heartbeat':>12}  arquivo")
    for a in alvos():
        p = a.parado_min()
        est = "AUSENTE" if p is None else f"{p:.1f} min"
        flag = "" if (p is not None and p <= a.max_parado_min) else "   <-- PARADO"
        print(f"{a.nome:18} {est:>12}  {a.heartbeat.name}{flag}")
    if LOG.exists():
        print(f"\nultimas linhas de {LOG.name}:")
        linhas = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        for l in linhas[-8:]:
            print("  " + l)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        return mostrar_status()

    lista = alvos()
    log("=" * 60)
    log(f"supervisor iniciado — {len(lista)} alvos, check a cada {INTERVALO_CHECK_S}s")
    log(f"reinicia se: processo morto, ou heartbeat parado > 10min")

    for al in lista:
        al.subir("start inicial")

    try:
        while True:
            time.sleep(INTERVALO_CHECK_S)
            for al in lista:
                if al.desistiu:
                    continue
                if not al.vivo():
                    rc = al.proc.returncode if al.proc else "?"
                    al.subir(f"processo morreu (rc={rc})")
                elif al.travado():
                    p = al.parado_min()
                    idade = "sem arquivo" if p is None else f"{p:.1f}min sem escrever"
                    al.subir(f"TRAVADO — {idade}")
    except KeyboardInterrupt:
        log("supervisor parando — encerrando alvos")
        for al in lista:
            al.matar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
