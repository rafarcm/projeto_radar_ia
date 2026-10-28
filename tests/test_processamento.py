import asyncio

import pytest
from conftest import ModeloFalso, item_processado, relatorio_exemplo, resultado_en, resultado_pt

from radar_ia.armazenamento import Armazenamento
from radar_ia.pdf import gerar_pdf
from radar_ia.fluxo import processar_item, processar_item_seguro
from radar_ia.processamento import (
    Contexto,
    ErroIdiomaRelatorio,
    aplicar_fallback_idioma,
    escolher_destaques,
    gerar_visao_geral,
    textos_fora_do_portugues,
)


def rodar(coro):
    return asyncio.run(coro)


def test_traducao_ok(item_en, glossario, cfg_proc):
    modelo = ModeloFalso({"ResultadoItem": [resultado_pt()]})
    r = rodar(processar_item(item_en, Contexto(modelo, glossario, cfg_proc)))
    assert r.titulo_pt.startswith("Acme lança")
    assert r.foi_traduzido and r.idioma_original == "en"
    assert r.url == item_en.url  # link original preservado
    assert r.titulo_original == item_en.titulo
    assert not r.traducao_indisponivel and not r.sinalizado
    sistema = modelo.chamadas_de("ResultadoItem")[0][0][1]
    assert '"open source": traduzir sempre como "código aberto"' in sistema  # glossário injetado


def test_refaz_quando_volta_em_ingles(item_en, glossario, cfg_proc):
    modelo = ModeloFalso({"ResultadoItem": [resultado_en(), resultado_pt()]})
    r = rodar(processar_item(item_en, Contexto(modelo, glossario, cfg_proc)))
    assert not r.traducao_indisponivel
    chamadas = modelo.chamadas_de("ResultadoItem")
    assert len(chamadas) == 2
    assert "não está em português" in chamadas[1][1][1]  # feedback na nova tentativa


def test_fallback_apos_tentativas_em_ingles(item_en, glossario, cfg_proc):
    modelo = ModeloFalso({"ResultadoItem": [resultado_en()]})
    r = rodar(processar_item(item_en, Contexto(modelo, glossario, cfg_proc)))
    assert len(modelo.chamadas_de("ResultadoItem")) == 1 + cfg_proc.max_tentativas_traducao
    assert r.traducao_indisponivel
    assert r.titulo_pt == "Tradução indisponível"
    assert r.titulo_original == item_en.titulo and r.url == item_en.url


def test_glossario_violado_gera_nova_tentativa(item_en, glossario, cfg_proc):
    com_violacao = resultado_pt(titulo_pt="Acme lança modelo open source com 70 bilhões de parâmetros")
    modelo = ModeloFalso({"ResultadoItem": [com_violacao, resultado_pt()]})
    r = rodar(processar_item(item_en, Contexto(modelo, glossario, cfg_proc)))
    assert "código aberto" in r.titulo_pt and not r.sinalizado
    assert 'deveria ser traduzido como "código aberto"' in modelo.chamadas_de("ResultadoItem")[1][1][1]


def test_numero_inventado_persistente_sinaliza(item_en, glossario, cfg_proc):
    inventado = resultado_pt(resumo_pt=resultado_pt()["resumo_pt"] + " O investimento foi de 300 milhões.")
    modelo = ModeloFalso({"ResultadoItem": [inventado]})
    r = rodar(processar_item(item_en, Contexto(modelo, glossario, cfg_proc)))
    assert r.sinalizado and "300" in (r.observacao or "")
    assert not r.traducao_indisponivel  # texto em português é publicado, com aviso


def test_verificar_traducao_opcional(item_en, glossario, cfg_proc):
    modelo = ModeloFalso({
        "ResultadoItem": [resultado_pt()],
        "Verificacao": [{"fiel": False, "problemas": ["a empresa Beta não está no original"]},
                        {"fiel": True, "problemas": []}],
    })
    ctx = Contexto(modelo, glossario, cfg_proc, verificar_traducao=True)
    r = rodar(processar_item(item_en, ctx))
    assert len(modelo.chamadas_de("Verificacao")) == 2
    assert not r.sinalizado


def test_cache_de_traducao(item_en, glossario, cfg_proc, tmp_path):
    banco = Armazenamento(tmp_path / "c.sqlite3")
    modelo = ModeloFalso({"ResultadoItem": [resultado_pt()]})
    ctx = Contexto(modelo, glossario, cfg_proc, armazenamento=banco, identificador_modelo="m")
    r1 = rodar(processar_item(item_en, ctx))
    r2 = rodar(processar_item(item_en, ctx))
    assert len(modelo.chamadas) == 1  # segunda vez veio do cache
    assert r1 == r2
    # glossário novo invalida o cache
    ctx.glossario = glossario.model_copy(update={"versao": 2})
    rodar(processar_item(item_en, ctx))
    assert len(modelo.chamadas) == 2


ARTIGO = ("Acme announced on Monday a new open source language model with 70 billion parameters. "
          "The model is available today under the Apache 2.0 license and scored 88 on the MMLU benchmark.")
RESUMO_ARTIGO = ("A Acme lançou um modelo de código aberto com 70 bilhões de parâmetros, disponível sob a "
                 "licença Apache 2.0, que obteve 88 pontos no MMLU.")


def test_feed_insuficiente_baixa_artigo_e_refaz_resumo(item_en, glossario, cfg_proc):
    modelo = ModeloFalso({"ResultadoItem": [resultado_pt(conteudo_suficiente=False),
                                            resultado_pt(resumo_pt=RESUMO_ARTIGO)]})
    baixados = []
    ctx = Contexto(modelo, glossario, cfg_proc, buscar_artigo=lambda url: baixados.append(url) or ARTIGO)
    r = rodar(processar_item(item_en, ctx))
    assert baixados == [item_en.url]
    assert r.resumo_pt == RESUMO_ARTIGO and not r.sinalizado  # números do artigo não contam como inventados
    chamadas = modelo.chamadas_de("ResultadoItem")
    assert len(chamadas) == 2
    assert "Texto do artigo:" not in chamadas[0][1][1]
    assert ARTIGO in chamadas[1][1][1]


def test_feed_suficiente_nao_baixa_artigo(item_en, glossario, cfg_proc):
    modelo = ModeloFalso({"ResultadoItem": [resultado_pt()]})
    ctx = Contexto(modelo, glossario, cfg_proc, buscar_artigo=lambda url: pytest.fail("não devia baixar"))
    rodar(processar_item(item_en, ctx))
    assert len(modelo.chamadas) == 1


def test_artigo_indisponivel_mantem_resumo_do_feed(item_en, glossario, cfg_proc):
    modelo = ModeloFalso({"ResultadoItem": [resultado_pt(conteudo_suficiente=False)]})
    baixados = []
    ctx = Contexto(modelo, glossario, cfg_proc, buscar_artigo=lambda url: baixados.append(url))
    r = rodar(processar_item(item_en, ctx))
    assert len(baixados) == 1 and len(modelo.chamadas) == 1  # tenta uma vez, sem nova chamada ao LLM
    assert r.titulo_pt.startswith("Acme lança") and not r.traducao_indisponivel


def test_artigo_insuficiente_nao_baixa_de_novo(item_en, glossario, cfg_proc):
    modelo = ModeloFalso({"ResultadoItem": [resultado_pt(conteudo_suficiente=False)]})
    baixados = []
    ctx = Contexto(modelo, glossario, cfg_proc, buscar_artigo=lambda url: baixados.append(url) or ARTIGO)
    rodar(processar_item(item_en, ctx))
    assert len(baixados) == 1 and len(modelo.chamadas) == 2


def test_erro_em_um_item_nao_derruba_os_outros(item_en, glossario, cfg_proc):
    outro = item_en.model_copy(update={"url": "https://exemplo.com/2", "url_normalizada": "https://exemplo.com/2"})

    def responder(mensagens):
        if "exemplo.com/2" in mensagens[1][1]:
            raise RuntimeError("API fora do ar")
        return resultado_pt()

    modelo = ModeloFalso({"ResultadoItem": [responder]})
    ctx = Contexto(modelo, glossario, cfg_proc)
    r = [rodar(processar_item_seguro(i, ctx)) for i in (item_en, outro)]
    assert not r[0].traducao_indisponivel
    assert r[1].traducao_indisponivel and "RuntimeError" in (r[1].observacao or "")


def test_visao_geral_fora_do_portugues_usa_texto_padrao(glossario, cfg_proc):
    modelo = ModeloFalso({"TextoVisaoGeral": [{"visao_geral": "Today was a busy day for AI with many new model releases."}]})
    texto = rodar(gerar_visao_geral([item_processado(1)], Contexto(modelo, glossario, cfg_proc)))
    assert len(modelo.chamadas) == 1 + cfg_proc.max_tentativas_traducao
    assert texto.startswith("Foram selecionadas 1 notícias")


def test_destaques_ponderam_relevancia_e_peso():
    itens = [item_processado(1, relevancia=5, peso=1), item_processado(2, relevancia=4, peso=5),
             item_processado(3, relevancia=2, peso=2), item_processado(4, traducao_indisponivel=True, relevancia=5)]
    assert [i.url[-1] for i in escolher_destaques(itens, 3)] == ["2", "1", "3"]


def test_pdf_bloqueado_com_texto_fora_do_portugues(tmp_path):
    rel = relatorio_exemplo(visao_geral="This report covers the most important AI news of the day in detail.")
    with pytest.raises(ErroIdiomaRelatorio, match="visão geral"):
        gerar_pdf(rel, tmp_path / "r.pdf")
    assert not (tmp_path / "r.pdf").exists()

    ingles = item_processado(9, titulo_pt="New model beats every benchmark",
                             resumo_pt="The company said the new model is faster and cheaper than the previous one.")
    rel = relatorio_exemplo(itens=[ingles])
    with pytest.raises(ErroIdiomaRelatorio, match="Example news number 9"):
        gerar_pdf(rel, tmp_path / "r.pdf")


def test_fallback_final_libera_o_pdf(tmp_path):
    ingles = item_processado(9, titulo_pt="New model beats every benchmark",
                             resumo_pt="The company said the new model is faster and cheaper than the previous one.")
    rel = aplicar_fallback_idioma(relatorio_exemplo(
        itens=[ingles, item_processado(1)],
        visao_geral="This report covers the most important AI news of the day in detail."))
    assert rel.itens[0].traducao_indisponivel
    assert not textos_fora_do_portugues(rel)
    assert gerar_pdf(rel, tmp_path / "r.pdf").exists()
