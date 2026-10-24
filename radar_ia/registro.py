"""Logging estruturado (chave=valor) que nunca exibe tokens ou senhas."""

from __future__ import annotations

import logging
import os
import re
import sys

VARIAVEIS_SECRETAS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "SMTP_SENHA",
    "TELEGRAM_BOT_TOKEN",
)
# Formato de token de bot do Telegram, para o caso de aparecer numa URL de erro.
_TOKEN_TELEGRAM = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")


def ocultar_segredos(texto: str) -> str:
    """Substitui qualquer valor secreto conhecido por '***'."""
    for nome in VARIAVEIS_SECRETAS:
        valor = os.getenv(nome)
        if valor and len(valor) >= 4:
            texto = texto.replace(valor, "***")
    return _TOKEN_TELEGRAM.sub("bot***", texto)


class FiltroSegredos(logging.Filter):
    """Aplica ocultar_segredos à mensagem final de cada registro."""

    def filter(self, record: logging.LogRecord) -> bool:
        mensagem = ocultar_segredos(record.getMessage())
        record.msg, record.args = mensagem, None
        return True


def configurar_logging(verbose: bool = False) -> None:
    """Configura o logging raiz; --verbose ativa DEBUG."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s nivel=%(levelname)s modulo=%(name)s %(message)s")
    )
    handler.addFilter(FiltroSegredos())
    raiz = logging.getLogger()
    raiz.handlers[:] = [handler]
    raiz.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx registra URLs completas (com o token do Telegram) em INFO.
    for ruidoso in ("httpx", "httpcore", "anthropic", "trafilatura", "urllib3"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
