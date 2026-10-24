"""Entrega do relatório pelos canais ativos; a falha de um canal não afeta o outro."""

from __future__ import annotations

import logging
from pathlib import Path

from radar_ia.config import ConfigEntrega
from radar_ia.entrega import email, telegram
from radar_ia.modelos import Relatorio
from radar_ia.registro import ocultar_segredos

log = logging.getLogger(__name__)


def canais_ativos(cfg: ConfigEntrega) -> list[str]:
    return [nome for nome, ativo in (("email", cfg.email.ativo), ("telegram", cfg.telegram.ativo)) if ativo]


def entregar(rel: Relatorio, pdf: Path, cfg: ConfigEntrega) -> dict[str, str | None]:
    """Envia por cada canal ativo. Retorna {canal: None se ok, ou mensagem de erro}."""
    resultado: dict[str, str | None] = {}
    if cfg.email.ativo:
        resultado["email"] = _tentar("email", lambda: email.enviar_relatorio(rel, pdf, cfg.email))
    if cfg.telegram.ativo:
        resultado["telegram"] = _tentar("telegram", lambda: telegram.enviar_relatorio(rel, pdf))
    return resultado


def testar(cfg: ConfigEntrega) -> dict[str, str | None]:
    resultado: dict[str, str | None] = {}
    if cfg.email.ativo:
        resultado["email"] = _tentar("email", lambda: email.enviar_teste(cfg.email))
    if cfg.telegram.ativo:
        resultado["telegram"] = _tentar("telegram", telegram.enviar_teste)
    return resultado


def _tentar(canal: str, acao) -> str | None:  # noqa: ANN001
    try:
        acao()
    except Exception as e:  # noqa: BLE001 - registra e segue para o próximo canal
        msg = ocultar_segredos(f"{type(e).__name__}: {e}")
        log.error("entrega_falhou canal=%s erro=%s", canal, msg)
        return msg
    log.info("entrega_ok canal=%s", canal)
    return None
