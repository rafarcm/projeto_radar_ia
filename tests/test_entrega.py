import logging

import pytest
from conftest import relatorio_exemplo

from radar_ia import entrega
from radar_ia.config import ConfigEmail, ConfigEntrega, ConfigTelegram, carregar_config
from radar_ia.entrega import email, telegram
from radar_ia.registro import FiltroSegredos, ocultar_segredos


@pytest.fixture
def pdf(tmp_path):
    caminho = tmp_path / "radar-ia_2026-10-23.pdf"
    caminho.write_bytes(b"%PDF-1.4 falso")
    return caminho


def test_monta_email_com_anexo_texto_e_html(pdf):
    rel = relatorio_exemplo()
    cfg = ConfigEmail(ativo=True, destinatarios=["a@x.com", "b@x.com"], assunto="Radar IA - {data}")
    msg = email.montar_mensagem(rel, pdf, cfg, "eu@x.com")
    assert msg["Subject"] == "Radar IA - 23/10/2026"
    assert msg["To"] == "a@x.com, b@x.com"
    texto = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert rel.visao_geral in texto and rel.destaques[0].url in texto
    assert f'href="{rel.destaques[0].url}"' in html
    anexos = list(msg.iter_attachments())
    assert len(anexos) == 1
    assert anexos[0].get_filename() == pdf.name
    assert anexos[0].get_content_type() == "application/pdf"
    assert anexos[0].get_content() == pdf.read_bytes()


def test_email_sem_destinatarios(pdf):
    with pytest.raises(email.ErroEmail, match="destinatário"):
        email.montar_mensagem(relatorio_exemplo(), pdf, ConfigEmail(ativo=True), "eu@x.com")


def test_divide_mensagens_longas_do_telegram():
    linhas = [f"Linha {n} " + "x" * 50 for n in range(300)]
    partes = telegram.dividir_mensagem("\n".join(linhas), limite=1000)
    assert len(partes) > 1
    assert all(len(p) <= 1000 for p in partes)
    assert "\n".join(partes) == "\n".join(linhas)  # nada se perde
    gigante = telegram.dividir_mensagem("palavra " * 2000, limite=500)
    assert all(len(p) <= 500 for p in gigante)


def test_texto_destaques_telegram():
    rel = relatorio_exemplo()
    texto = telegram.texto_destaques(rel)
    assert "Destaques:" in texto and rel.destaques[0].url in texto


def test_falha_em_um_canal_nao_impede_o_outro(monkeypatch, pdf):
    enviados = []

    def email_quebrado(*_):
        raise ConnectionError("smtp fora do ar")

    monkeypatch.setattr(email, "enviar_relatorio", email_quebrado)
    monkeypatch.setattr(telegram, "enviar_relatorio", lambda rel, p: enviados.append(p))
    cfg = ConfigEntrega(email=ConfigEmail(ativo=True, destinatarios=["a@x.com"]), telegram=ConfigTelegram(ativo=True))
    resultado = entrega.entregar(relatorio_exemplo(), pdf, cfg)
    assert "smtp fora do ar" in resultado["email"]
    assert resultado["telegram"] is None
    assert enviados == [pdf]
    assert pdf.exists()  # o PDF nunca é apagado


def test_canais_inativos_nao_enviam(pdf):
    assert entrega.entregar(relatorio_exemplo(), pdf, ConfigEntrega()) == {}


def test_telegram_sem_credenciais(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(telegram.ErroTelegram, match="TELEGRAM_BOT_TOKEN"):
        telegram.enviar_teste()


def test_telegram_envia_texto_e_documento(monkeypatch, pdf):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    metodos = []
    monkeypatch.setattr(telegram, "_chamar", lambda metodo, token, **kw: metodos.append((metodo, kw)) or {})
    telegram.enviar_relatorio(relatorio_exemplo(), pdf)
    assert [m for m, _ in metodos] == ["sendMessage", "sendDocument"]
    assert metodos[1][1]["files"]["document"][1] == pdf.read_bytes()


def test_segredos_nunca_aparecem_nos_logs(monkeypatch, caplog):
    monkeypatch.setenv("SMTP_SENHA", "senha-super-secreta")
    token = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
    assert "senha-super-secreta" not in ocultar_segredos("falhou com senha-super-secreta")
    assert token not in ocultar_segredos(f"https://api.telegram.org/bot{token}/sendMessage")
    log = logging.getLogger("teste_segredos")
    log.addFilter(FiltroSegredos())
    with caplog.at_level(logging.INFO, logger="teste_segredos"):
        log.info("url=%s senha=%s", f"https://api.telegram.org/bot{token}/x", "senha-super-secreta")
    assert token not in caplog.text and "senha-super-secreta" not in caplog.text


def test_destinatarios_vem_do_ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_DESTINATARIOS", " a@x.com, b@x.com ,")
    cfg = carregar_config(tmp_path / "inexistente.yaml")
    assert cfg.entrega.email.destinatarios == ["a@x.com", "b@x.com"]
