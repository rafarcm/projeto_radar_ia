"""SQLite local: histórico do que já foi reportado e cache de traduções."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS reportados (
    url_normalizada TEXT PRIMARY KEY,
    titulo TEXT NOT NULL,
    reportado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traducoes (
    chave TEXT PRIMARY KEY,
    dados TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
"""


def chave_traducao(url_normalizada: str, conteudo: str, versao_glossario: int, modelo: str) -> str:
    """URL normalizada + hash do conteúdo + versão do glossário (+ modelo)."""
    h = hashlib.sha256(conteudo.encode("utf-8")).hexdigest()[:16]
    return f"{url_normalizada}|{h}|g{versao_glossario}|{modelo}"


class Armazenamento:
    def __init__(self, caminho: Path | str):
        if str(caminho) != ":memory:":
            Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(caminho), check_same_thread=False)
        self.con.executescript(_ESQUEMA)
        # Os itens são processados em paralelo (threads): uma operação por vez na conexão.
        self._trava = threading.Lock()

    def fechar(self) -> None:
        with self._trava:
            self.con.close()

    # ------------------------------------------------------------- histórico
    def ja_reportados(self, urls: Iterable[str]) -> set[str]:
        lista = list(urls)
        if not lista:
            return set()
        with self._trava:
            return self._consultar_reportados(lista)

    def _consultar_reportados(self, lista: list[str]) -> set[str]:
        achados: set[str] = set()
        for i in range(0, len(lista), 500):
            lote = lista[i : i + 500]
            marcas = ",".join("?" * len(lote))
            cur = self.con.execute(
                f"SELECT url_normalizada FROM reportados WHERE url_normalizada IN ({marcas})", lote
            )
            achados.update(r[0] for r in cur)
        return achados

    def marcar_reportados(self, itens: Iterable[tuple[str, str]]) -> None:
        agora = datetime.now(timezone.utc).isoformat()
        with self._trava, self.con:
            self.con.executemany(
                "INSERT OR IGNORE INTO reportados VALUES (?, ?, ?)",
                [(url, titulo, agora) for url, titulo in itens],
            )

    # ------------------------------------------------------ cache de traduções
    def obter_traducao(self, chave: str) -> dict | None:
        with self._trava:
            linha = self.con.execute("SELECT dados FROM traducoes WHERE chave = ?", (chave,)).fetchone()
        return json.loads(linha[0]) if linha else None

    def salvar_traducao(self, chave: str, dados: dict) -> None:
        with self._trava, self.con:
            self.con.execute(
                "INSERT OR REPLACE INTO traducoes VALUES (?, ?, ?)",
                (chave, json.dumps(dados, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )
