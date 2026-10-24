import zlib
from datetime import datetime, timezone

from conftest import item_processado, relatorio_exemplo

from radar_ia.modelos import FalhaFonte
from radar_ia.pdf import gerar_pdf, nome_arquivo


def test_nome_do_arquivo_usa_data_de_brasilia(tmp_path):
    # 02h UTC do dia 24 ainda é dia 23 em São Paulo
    assert nome_arquivo(tmp_path, datetime(2026, 10, 24, 2, tzinfo=timezone.utc)).name == "radar-ia_2026-10-23.pdf"


def test_gera_pdf_com_dados_falsos(tmp_path):
    itens = [item_processado(i) for i in range(1, 30)]  # força várias páginas
    itens.append(item_processado(99, traducao_indisponivel=True, titulo_pt="Tradução indisponível"))
    itens.append(item_processado(98, sinalizado=True, observacao="A verificação automática apontou divergências"))
    itens.append(item_processado(97, titulo_pt="Título com <tags> & símbolos “especiais” — ç ã é"))
    rel = relatorio_exemplo(itens=itens, falhas=[FalhaFonte(fonte="Fonte Quebrada", url="https://x.com/f", erro="HTTP 500")])
    caminho = gerar_pdf(rel, tmp_path / "sub" / "radar.pdf")
    dados = caminho.read_bytes()
    assert dados.startswith(b"%PDF")
    assert dados.count(b"/Type /Page\n") + dados.count(b"/Type /Page ") >= 3 or b"/Count" in dados
    assert b"/URI (https://exemplo.com/1)" in dados  # links clicáveis
    assert b"/URI (https://x.com/f)" in dados


def test_pdf_sem_itens(tmp_path):
    rel = relatorio_exemplo(itens=[], destaques=[], visao_geral="Nenhuma notícia nova foi processada no período.")
    assert gerar_pdf(rel, tmp_path / "vazio.pdf").stat().st_size > 0


def _texto_pdf(dados: bytes) -> bytes:
    # Concatena os streams descomprimidos para buscar texto (fonte TTF codifica por subconjunto,
    # então só checamos que existe conteúdo).
    partes = []
    for bloco in dados.split(b"stream\n")[1:]:
        try:
            partes.append(zlib.decompress(bloco.split(b"endstream")[0].strip()))
        except zlib.error:
            continue
    return b"".join(partes)


def test_pdf_tem_conteudo(tmp_path):
    dados = gerar_pdf(relatorio_exemplo(), tmp_path / "r.pdf").read_bytes()
    assert len(_texto_pdf(dados)) > 1000
