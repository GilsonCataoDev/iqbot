"""Trava entre processos para impedir duas sessões do mesmo perfil."""

from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path
from ctypes import wintypes


class ExecucaoDuplicada(RuntimeError):
    """Uma segunda instância tentou iniciar o mesmo perfil."""


class ExecucaoUnica:
    """Mantém uma trava Windows enquanto um perfil crítico está em execução."""

    def __init__(self, perfil: str, diretorio: str | Path = "dados") -> None:
        self.caminho = Path(diretorio) / f".{perfil}.lock"
        identificador = hashlib.sha256(
            str(self.caminho.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        self._nome_mutex = f"Local\\IQOptionM5_{perfil}_{identificador}"
        self._handle: int | None = None

    def __enter__(self) -> "ExecucaoUnica":
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, self._nome_mutex)
        if not handle:
            raise OSError("Não foi possível criar a trava da sessão.")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            raise ExecucaoDuplicada(
                f"Perfil ja esta em execucao: {self.caminho.stem.lstrip('.')}"
            )
        self._handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is None:
            return
        try:
            ctypes.windll.kernel32.CloseHandle(self._handle)
        finally:
            self._handle = None
