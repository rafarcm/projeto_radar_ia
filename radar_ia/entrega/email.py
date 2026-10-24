"""Envio do relatório por e-mail (SMTP com TLS), com o PDF anexado."""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
from pathlib import Path

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from radar_ia.config import ConfigEmail
from radar_ia.modelos import Relatorio
from radar_ia.pdf import AVISO_IA, FUSO


class ErroEmail(Exception):
    pass


@dataclass
class CredenciaisSMTP:
    host: str
    porta: int
    usuario: str
    senha: str

    @classmethod
    def do_ambiente(cls) -> "CredenciaisSMTP":
        faltando = [v for v in ("SMTP_HOST", "SMTP_USUARIO", "SMTP_SENHA") if not os.getenv(v)]
        if faltando:
            raise ErroEmail(f"variáveis ausentes no .env: {', '.join(faltando)}")
        return cls(
            host=os.environ["SMTP_HOST"],
            porta=int(os.getenv("SMTP_PORTA") or 587),
            usuario=os.environ["SMTP_USUARIO"],
            senha=os.environ["SMTP_SENHA"],
        )


def montar_mensagem(rel: Relatorio, pdf: Path, cfg: ConfigEmail, remetente: str) -> EmailMessage:
    """E-mail com texto simples + HTML (visão geral e destaques) e o PDF anexo."""
    if not cfg.destinatarios:
        raise ErroEmail("nenhum destinatário: defina EMAIL_DESTINATARIOS no .env")
    data = rel.fim.astimezone(FUSO).strftime("%d/%m/%Y")
    msg = EmailMessage()
    msg["Subject"] = cfg.assunto.format(data=data)
    msg["From"] = remetente
    msg["To"] = ", ".join(cfg.destinatarios)

    texto = [f"Radar IA - {data}", "", "VISÃO GERAL", rel.visao_geral, "", "DESTAQUES"]
    for i in rel.destaques:
        texto += [f"- {i.titulo_pt}", f"  {i.url}"]
    texto += ["", f"O relatório completo está no PDF anexo ({len(rel.itens)} notícias).", "", AVISO_IA]
    msg.set_content("\n".join(texto))

    itens_html = "".join(
        f'<li><a href="{escape(i.url)}">{escape(i.titulo_pt)}</a> '
        f'<small style="color:#666">({escape(i.fonte)})</small></li>'
        for i in rel.destaques
    )
    msg.add_alternative(
        f"""<html><body style="font-family:Arial,sans-serif;max-width:640px">
<h2 style="color:#1f4e79">Radar IA - {data}</h2>
<h3>Visão geral</h3><p>{escape(rel.visao_geral)}</p>
<h3>Destaques</h3><ul>{itens_html}</ul>
<p>O relatório completo está no PDF anexo ({len(rel.itens)} notícias).</p>
<p style="color:#8a4b00;font-size:12px">{escape(AVISO_IA)}</p>
</body></html>""",
        subtype="html",
    )
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    return msg


def _transitorio(e: BaseException) -> bool:
    # Erro de autenticação ou destinatário recusado não adianta repetir.
    if isinstance(e, (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused)):
        return False
    return isinstance(e, (smtplib.SMTPException, OSError))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20),
       retry=retry_if_exception(_transitorio), reraise=True)
def enviar(msg: EmailMessage, cred: CredenciaisSMTP) -> None:
    contexto = ssl.create_default_context()
    if cred.porta == 465:
        with smtplib.SMTP_SSL(cred.host, cred.porta, context=contexto, timeout=30) as smtp:
            smtp.login(cred.usuario, cred.senha)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cred.host, cred.porta, timeout=30) as smtp:
            smtp.starttls(context=contexto)
            smtp.login(cred.usuario, cred.senha)
            smtp.send_message(msg)


def enviar_relatorio(rel: Relatorio, pdf: Path, cfg: ConfigEmail) -> None:
    cred = CredenciaisSMTP.do_ambiente()
    enviar(montar_mensagem(rel, pdf, cfg, cred.usuario), cred)


def enviar_teste(cfg: ConfigEmail) -> None:
    cred = CredenciaisSMTP.do_ambiente()
    if not cfg.destinatarios:
        raise ErroEmail("nenhum destinatário: defina EMAIL_DESTINATARIOS no .env")
    msg = EmailMessage()
    msg["Subject"] = "Radar IA - mensagem de teste"
    msg["From"] = cred.usuario
    msg["To"] = ", ".join(cfg.destinatarios)
    msg.set_content("Teste de entrega do radar-ia: a configuração de e-mail está funcionando.")
    enviar(msg, cred)
