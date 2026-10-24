from datetime import datetime, timedelta, timezone

import pytest

from radar_ia.armazenamento import Armazenamento, chave_traducao
from radar_ia.coleta import dentro_da_janela, extrair_data, interpretar_feed, limpar_html
from radar_ia.dedup import deduplicar, limitar_itens, normalizar_url
from radar_ia.fontes import Fonte
from radar_ia.modelos import ItemColetado


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("http://www.Exemplo.com/a/?utm_source=x&utm_medium=rss&id=3#topo", "https://exemplo.com/a?id=3"),
        ("https://exemplo.com/a?b=2&a=1&fbclid=zzz", "https://exemplo.com/a?a=1&b=2"),
        ("https://exemplo.com:443/", "https://exemplo.com"),
        ("https://exemplo.com/post/", "https://exemplo.com/post"),
    ],
)
def test_normalizar_url(entrada, esperado):
    assert normalizar_url(entrada) == esperado


def _item(titulo: str, url: str, peso: int = 3, fonte: str = "F") -> ItemColetado:
    return ItemColetado(fonte=fonte, peso=peso, titulo=titulo, url=url, url_normalizada=normalizar_url(url))


def test_deduplica_por_url_e_titulo_mantendo_maior_peso():
    itens = [
        _item("OpenAI lança novo modelo", "https://a.com/1?utm_source=x", peso=2, fonte="Imprensa"),
        _item("Outra notícia", "https://a.com/1"),  # mesma URL normalizada
        _item("OpenAI lança novo modelo!", "https://b.com/9", peso=5, fonte="Oficial"),  # título quase igual
        _item("Assunto totalmente diferente", "https://c.com/2"),
    ]
    resultado = deduplicar(itens, limiar_titulo=90)
    # Ordem original preservada; entre duplicados vence o maior peso.
    assert [(i.fonte, i.titulo) for i in resultado] == [
        ("F", "Outra notícia"),
        ("Oficial", "OpenAI lança novo modelo!"),
        ("F", "Assunto totalmente diferente"),
    ]


def test_limiar_de_titulo_configuravel():
    itens = [_item("Modelo X supera GPT em testes", "https://a.com/1"),
             _item("Modelo X supera GPT em vários testes", "https://b.com/2")]
    assert len(deduplicar(itens, limiar_titulo=99)) == 2
    assert len(deduplicar(itens, limiar_titulo=80)) == 1


def test_limitar_itens_alterna_entre_fontes():
    base = datetime(2026, 10, 23, tzinfo=timezone.utc)
    arxiv = [_item(f"Paper {n}", f"https://arxiv.org/{n}", peso=4, fonte="arXiv").model_copy(
        update={"data": base + timedelta(hours=n)}) for n in range(10)]
    imprensa = [_item(f"Notícia {n}", f"https://im.com/{n}", peso=3, fonte="Imprensa") for n in range(2)]
    escolhidos = limitar_itens(arxiv + imprensa, 5)
    assert [i.fonte for i in escolhidos] == ["arXiv", "Imprensa", "arXiv", "Imprensa", "arXiv"]
    assert escolhidos[0].titulo == "Paper 9"  # mais recente primeiro
    assert len(limitar_itens(imprensa, 10)) == 2


def test_extrair_data_formatos():
    assert extrair_data({"published_parsed": (2026, 10, 23, 12, 0, 0, 0, 0, 0)}) == datetime(
        2026, 10, 23, 12, tzinfo=timezone.utc)
    assert extrair_data({"published": "Thu, 23 Oct 2026 09:00:00 -0300"}) == datetime(
        2026, 10, 23, 12, tzinfo=timezone.utc)
    assert extrair_data({"updated": "2026-10-23T12:00:00"}) == datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    assert extrair_data({"published": "ontem à tarde"}) is None
    assert extrair_data({}) is None


def test_filtro_de_janela():
    fim = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    inicio = fim - timedelta(hours=24)
    assert dentro_da_janela(fim - timedelta(hours=1), inicio, fim)
    assert not dentro_da_janela(fim - timedelta(hours=30), inicio, fim)
    assert dentro_da_janela(None, inicio, fim)  # sem data: mantém
    assert not dentro_da_janela(fim + timedelta(days=2), inicio, fim)


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title><language>en-us</language>
<item><title>Item &lt;b&gt;um&lt;/b&gt;</title><link>https://x.com/1?utm_campaign=a</link>
<description>&lt;p&gt;Resumo   com HTML&lt;/p&gt;</description><pubDate>Thu, 23 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Sem data</title><link>https://x.com/2</link></item>
<item><title></title><link>https://x.com/3</link></item>
</channel></rss>"""


def test_interpretar_feed():
    fonte = Fonte(nome="X", url="https://x.com/feed", peso=4)
    itens, erro = interpretar_feed(RSS, fonte)
    assert erro is None
    assert [i.titulo for i in itens] == ["Item um", "Sem data"]  # sem título é descartado
    assert itens[0].resumo == "Resumo com HTML"
    assert itens[0].url_normalizada == "https://x.com/1"
    assert itens[0].idioma_feed == "en" and itens[0].peso == 4
    assert itens[1].data is None


def test_feed_ilegivel():
    itens, erro = interpretar_feed(b"isto nao e xml", Fonte(nome="X", url="https://x.com/feed"))
    assert itens == [] and erro


def test_limpar_html():
    assert limpar_html("<p>Olá&nbsp;<b>mundo</b> &amp; cia</p>") == "Olá mundo & cia"


def test_historico_sqlite(tmp_path):
    banco = Armazenamento(tmp_path / "radar.sqlite3")
    banco.marcar_reportados([("https://a.com/1", "A")])
    assert banco.ja_reportados(["https://a.com/1", "https://a.com/2"]) == {"https://a.com/1"}
    banco.fechar()
    # persiste entre execuções
    banco2 = Armazenamento(tmp_path / "radar.sqlite3")
    assert banco2.ja_reportados(["https://a.com/1"]) == {"https://a.com/1"}


def test_chave_de_cache_muda_com_conteudo_e_glossario():
    base = chave_traducao("https://a.com/1", "texto", 1, "m")
    assert base == chave_traducao("https://a.com/1", "texto", 1, "m")
    assert base != chave_traducao("https://a.com/1", "texto alterado", 1, "m")
    assert base != chave_traducao("https://a.com/1", "texto", 2, "m")
