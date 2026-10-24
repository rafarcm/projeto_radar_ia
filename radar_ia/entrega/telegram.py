"""Envio pelo Telegram usando a API HTTP oficial de bots."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from radar_ia.modelos import Relatorio
from radar_ia.pdf import FUSO

LIMITE_MENSAGEM = 4096
API = "https://api.telegram.org/bot{token}/{metodo}"


class ErroTelegram(Exception):
    pass


def _credenciais() -> tuple[str, str]:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    faltando = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)) if not v]
    if faltando:
        raise ErroTelegram(f"variáveis ausentes no .env: {', '.join(faltando)}")
    return token, chat  # type: ignore[return-value]


def dividir_mensagem(texto: str, limite: int = LIMITE_MENSAGEM) -> list[str]:
    """Divide em partes <= limite, preferindo quebrar entre parágrafos e linhas."""
    partes: list[str] = []
    atual = ""
    for linha in texto.split("\n"):
        while len(linha) > limite:  # linha sozinha maior que o limite
            if atual:
                partes.append(atual)
                atual = ""
            corte = linha.rfind(" ", 0, limite)
            corte = corte if corte > 0 else limite
            partes.append(linha[:corte])
            linha = linha[corte:].lstrip()
        candidato = f"{atual}\n{linha}" if atual else linha
        if len(candidato) > limite:
            partes.append(atual)
            atual = linha
        else:
            atual = candidato
    if atual.strip():
        partes.append(atual)
    return [p for p in partes if p.strip()]


def texto_destaques(rel: Relatorio) -> str:
    data = rel.fim.astimezone(FUSO).strftime("%d/%m/%Y")
    linhas = [f"Radar IA - {data}", "", rel.visao_geral, "", "Destaques:"]
    for n, i in enumerate(rel.destaques, 1):
        linhas += ["", f"{n}. {i.titulo_pt}", i.url]
    linhas += ["", "Relatório completo no PDF a seguir. Resumos gerados por IA: confira a fonte."]
    return "\n".join(linhas)


def _transitorio(e: BaseException) -> bool:
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code == 429 or e.response.status_code >= 500
    return isinstance(e, httpx.TransportError)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20),
       retry=retry_if_exception(_transitorio), reraise=True)
def _chamar(metodo: str, token: str, **kwargs) -> dict:  # noqa: ANN003
    resp = httpx.post(API.format(token=token, metodo=metodo), timeout=60, **kwargs)
    if resp.status_code >= 400:
        # Mensagem própria: o erro padrão do httpx incluiria a URL com o token.
        descricao = ""
        try:
            descricao = resp.json().get("description", "")
        except ValueError:
            pass
        erro = httpx.HTTPStatusError(f"Telegram {metodo}: HTTP {resp.status_code} {descricao}",
                                     request=resp.request, response=resp)
        raise erro
    return resp.json()


def enviar_texto(texto: str, token: str, chat_id: str) -> None:
    for parte in dividir_mensagem(texto):
        _chamar("sendMessage", token, data={"chat_id": chat_id, "text": parte,
                                            "disable_web_page_preview": "true"})


def enviar_documento(pdf: Path, token: str, chat_id: str) -> None:
    # Bytes (e não o arquivo aberto) para que uma retentativa reenvie o conteúdo inteiro.
    _chamar("sendDocument", token, data={"chat_id": chat_id},
            files={"document": (pdf.name, pdf.read_bytes(), "application/pdf")})


def enviar_relatorio(rel: Relatorio, pdf: Path) -> None:
    token, chat = _credenciais()
    enviar_texto(texto_destaques(rel), token, chat)
    enviar_documento(pdf, token, chat)


def enviar_teste() -> None:
    token, chat = _credenciais()
    enviar_texto("Teste de entrega do radar-ia: a configuração do Telegram está funcionando.", token, chat)
