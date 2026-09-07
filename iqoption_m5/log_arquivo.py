"""Espelha stdout/stderr num arquivo, sem alterar o que aparece no console.

O diagnóstico do bot sai por print() — mensagens como "[mercado] falha ao
consultar abertura turbo" — e morria junto com a janela do terminal. Aqui elas
ficam gravadas com hora BRT, que é o formato que o operador lê.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PASTA_PADRAO = Path(__file__).resolve().parent / "dados" / "logs"


def _hoje_brt() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=3)).date().isoformat()


class _Espelho:
    """Escreve no destino original e no arquivo do dia.

    Nunca deixa uma falha de escrita derrubar o bot: log é diagnóstico, não
    pode virar a causa de uma parada.
    """

    def __init__(self, original, pasta: Path, nome: str):
        self._original = original
        self._pasta = pasta
        self._nome = nome
        self._dia = None
        self._arquivo = None
        self._inicio_de_linha = True
        self._abrir()

    # -- arquivo ----------------------------------------------------------
    def _abrir(self) -> None:
        dia = _hoje_brt()
        if self._dia == dia and self._arquivo is not None:
            return
        self._fechar()
        try:
            self._pasta.mkdir(parents=True, exist_ok=True)
            self._arquivo = open(
                self._pasta / f"{self._nome}_{dia}.log",
                "a", encoding="utf-8", errors="replace",
            )
            self._dia = dia
        except OSError:
            self._arquivo = None

    def _fechar(self) -> None:
        if self._arquivo is not None:
            try:
                self._arquivo.close()
            except OSError:
                pass
            self._arquivo = None

    @property
    def caminho(self) -> Path:
        return self._pasta / f"{self._nome}_{self._dia or _hoje_brt()}.log"

    # -- carimbo ----------------------------------------------------------
    def _carimbar(self, texto: str) -> str:
        marca = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S ")
        pedacos = []
        for parte in texto.splitlines(keepends=True):
            if self._inicio_de_linha:
                pedacos.append(marca)
            pedacos.append(parte)
            self._inicio_de_linha = parte.endswith("\n")
        return "".join(pedacos)

    # -- interface de stream ----------------------------------------------
    def write(self, texto: str) -> int:
        escrito = self._original.write(texto)
        if texto:
            self._abrir()  # vira o arquivo quando o dia BRT muda
            if self._arquivo is not None:
                try:
                    self._arquivo.write(self._carimbar(texto))
                    # Sem buffer: uma queda nao pode levar junto o rastro.
                    self._arquivo.flush()
                except (OSError, ValueError):
                    pass
        return escrito

    def flush(self) -> None:
        self._original.flush()
        if self._arquivo is not None:
            try:
                self._arquivo.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return getattr(self._original, "isatty", lambda: False)()

    def fileno(self) -> int:
        return self._original.fileno()


def ativar(nome: str, pasta: Path | None = None) -> Path | None:
    """Liga o espelhamento. Chamar uma vez, no início do programa.

    Devolve o caminho do log, ou None se já estava ligado.
    """
    if isinstance(sys.stdout, _Espelho):
        return None
    destino = Path(pasta) if pasta else PASTA_PADRAO
    espelho_saida = _Espelho(sys.stdout, destino, nome)
    sys.stdout = espelho_saida
    # stderr no mesmo arquivo: traceback de excecao nao tratada fica junto do
    # que o bot estava fazendo na hora.
    sys.stderr = _Espelho(sys.stderr, destino, nome)
    return espelho_saida.caminho
