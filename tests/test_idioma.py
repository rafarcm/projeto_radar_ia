import pytest

from radar_ia.idioma import (
    ErroGlossario,
    carregar_glossario,
    detectar_idioma,
    eh_portugues,
    numeros_inventados,
    numeros_perdidos_no_titulo,
    urls_alteradas,
    violacoes_glossario,
)

EN = "OpenAI released a new reasoning model today that outperforms previous versions on math benchmarks."
PT = "A OpenAI lançou hoje um novo modelo de raciocínio que supera as versões anteriores em testes de matemática."


def test_detecta_ingles_e_portugues():
    assert detectar_idioma(EN).idioma == "en"
    assert detectar_idioma(PT).idioma == "pt"
    assert detectar_idioma(EN).origem == "texto"


def test_texto_curto_usa_idioma_do_feed_ou_fica_indefinido():
    d = detectar_idioma("GPT-6", idioma_feed="en-US")
    assert (d.idioma, d.origem) == ("en", "feed")
    assert detectar_idioma("GPT-6").idioma is None  # o LLM decide


def test_eh_portugues():
    assert eh_portugues(PT)
    assert not eh_portugues(EN)
    assert eh_portugues("Claude 5")  # curto demais para julgar


def test_glossario_traduzir_como(glossario):
    original = "The model is fully open source."
    assert violacoes_glossario(original, "O modelo é totalmente open source.", glossario)
    assert not violacoes_glossario(original, "O modelo é totalmente de código aberto.", glossario)
    # termo ausente do original: nada a cobrar
    assert not violacoes_glossario("A closed model.", "Um modelo fechado.", glossario)


def test_carregar_glossario(tmp_path):
    caminho = tmp_path / "g.yaml"
    caminho.write_text('versao: 3\ntermos:\n  - termo: RAG\n    manter: true\n'
                       '  - termo: agent\n    traduzir_como: "agente"\n', encoding="utf-8")
    g = carregar_glossario(caminho)
    assert g.versao == 3 and len(g.termos) == 2
    assert '"agent": traduzir sempre como "agente"' in g.para_prompt()
    caminho.write_text("termos:\n  - termo: RAG\n", encoding="utf-8")  # sem regra
    with pytest.raises(ErroGlossario, match="termo 'RAG'"):
        carregar_glossario(caminho)


def test_numeros_preservados_e_inventados():
    original = "Startup raises $1.5 billion; model has 70B parameters, released October 5, 2026"
    assert not numeros_inventados(original, "Startup capta US$ 1,5 bilhão; modelo com 70B parâmetros, lançado em 05/10/2026")
    assert numeros_inventados(original, "Startup capta US$ 2,5 bilhões") == {"25"}
    assert numeros_perdidos_no_titulo("GPT-6 scores 92% on test", "GPT-6 tem bom resultado") == {"92"}
    assert not numeros_perdidos_no_titulo("GPT-6 scores 92% on test", "GPT-6 atinge 92% no teste")


def test_urls_preservadas():
    original = "See https://exemplo.com/paper.pdf for details"
    assert not urls_alteradas(original, "Veja https://exemplo.com/paper.pdf para detalhes")
    assert urls_alteradas(original, "Veja https://exemplo.com.br/artigo.pdf") == {"https://exemplo.com.br/artigo.pdf"}


def test_deteccao_em_paralelo_nao_erra():
    # langdetect não é seguro entre threads; o LangGraph processa itens em paralelo.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        resultados = list(pool.map(lambda _: eh_portugues(PT) and not eh_portugues(EN), range(200)))
    assert all(resultados)
