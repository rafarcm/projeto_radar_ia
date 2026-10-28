"""Pipeline completo em LangGraph com LLM, feeds e embeddings falsos (nenhuma API real)."""

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
from conftest import ModeloFalso, resultado_pt
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from radar_ia import fluxo
from radar_ia.armazenamento import Armazenamento
from radar_ia.coleta import ResultadoColeta
from radar_ia.config import Config
from radar_ia.dedup import deduplicar_semantico, normalizar_url
from radar_ia.fontes import Fonte
from radar_ia.modelos import FalhaFonte, ItemColetado
from radar_ia.processamento import Contexto

FIM = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def _item(n: int, titulo: str, fonte: str = "Fonte A", peso: int = 3) -> ItemColetado:
    url = f"https://exemplo.com/{n}"
    return ItemColetado(
        fonte=fonte, peso=peso, titulo=titulo, url=url, url_normalizada=normalizar_url(url),
        resumo=f"{titulo}. The company published the details on its official blog today.",
        data=FIM - timedelta(hours=n), idioma_feed="en",
    )


ITENS = [
    _item(1, "Acme releases open source model with 70 billion parameters"),
    _item(2, "Regulators publish draft rules for AI systems in Europe", fonte="Fonte B"),
    _item(3, "Startup unveils assistant that books travel automatically", fonte="Fonte B"),
]


def _resposta(mensagens):
    """Devolve um resultado em português coerente com o título da mensagem."""
    usuario = mensagens[1][1]
    if "Regulators" in usuario:
        return resultado_pt(titulo_pt="Reguladores publicam rascunho de regras para sistemas de IA na Europa",
                            resumo_pt="Os reguladores divulgaram um rascunho de regras para sistemas de IA na Europa, "
                                      "segundo publicação oficial da empresa.", tema="Regulação", relevancia=5)
    if "Startup" in usuario:
        return resultado_pt(titulo_pt="Startup apresenta assistente que reserva viagens automaticamente",
                            resumo_pt="Uma startup apresentou um assistente capaz de reservar viagens de forma "
                                      "automática, segundo o blog oficial.", tema="Produtos", relevancia=3)
    return resultado_pt()


@pytest.fixture
def ambiente(tmp_path, monkeypatch, glossario, cfg_proc):
    monkeypatch.setattr(fluxo, "coletar", lambda fontes, cfg, inicio, fim: ResultadoColeta(
        itens=[i.model_copy() for i in ITENS],
        falhas=[FalhaFonte(fonte="Quebrada", url="https://q.com/feed", erro="HTTP 500")]))
    cfg = Config.model_validate({"saida": {"pasta_relatorios": str(tmp_path / "rel"),
                                           "banco_sqlite": str(tmp_path / "radar.sqlite3")},
                                 "processamento": cfg_proc.model_dump()})
    banco = Armazenamento(cfg.saida.banco_sqlite)
    modelo = ModeloFalso({
        "ResultadoItem": [_resposta],
        "TextoVisaoGeral": [{"visao_geral": "O período trouxe lançamentos de modelos, novas regras para IA na "
                                            "Europa e assistentes de viagem."}],
    })
    ctx = fluxo.ContextoRadar(config=cfg, fontes=[Fonte(nome="Fonte A", url="https://a.com/feed")], banco=banco,
                              proc=Contexto(modelo, glossario, cfg_proc, armazenamento=banco, identificador_modelo="m"))
    yield ctx, modelo, tmp_path
    banco.fechar()


def _opcoes(**extra) -> fluxo.Opcoes:
    base = dict(horas=24, max_itens=40, simular=False, sem_enviar=True, texto_completo=False,
                ignorar_historico=False, verificar_traducao=False)
    return fluxo.Opcoes(**(base | extra))


def _entrada(**extra):
    return {"opcoes": _opcoes(**extra), "inicio": FIM - timedelta(hours=24), "fim": FIM}


def test_pipeline_completo_gera_pdf_e_historico(ambiente):
    ctx, modelo, tmp_path = ambiente
    estado = asyncio.run(fluxo.construir_grafo_radar().ainvoke(_entrada(), context=ctx))
    rel = estado["relatorio"]
    assert estado["pdf"].endswith("radar-ia_2026-10-23_0900.pdf")
    assert (tmp_path / "rel" / "radar-ia_2026-10-23_0900.pdf").exists()
    assert rel.ja_reportados == 0
    assert len(rel.itens) == 3 and rel.falhas[0].fonte == "Quebrada"
    assert rel.destaques[0].tema == "Regulação"  # maior relevância
    assert "entrega" not in estado  # --sem-enviar
    # Itens viram histórico: a próxima execução não os repete nem chama o LLM.
    assert ctx.banco.ja_reportados(i.url_normalizada for i in ITENS) == {i.url_normalizada for i in ITENS}
    chamadas = len(modelo.chamadas_de("ResultadoItem"))
    estado2 = asyncio.run(fluxo.construir_grafo_radar().ainvoke(_entrada(), context=ctx))
    assert estado2["selecionados"] == [] and len(modelo.chamadas_de("ResultadoItem")) == chamadas
    assert estado2["relatorio"].ja_reportados == 3
    assert estado2["pdf"] == estado["pdf"]  # mesma janela, mesmo arquivo


def test_simular_nao_chama_llm(ambiente):
    ctx, modelo, _ = ambiente
    estado = asyncio.run(fluxo.construir_grafo_radar().ainvoke(_entrada(simular=True), context=ctx))
    assert len(estado["selecionados"]) == 3
    assert modelo.chamadas == [] and "pdf" not in estado


def test_texto_completo_vai_para_o_prompt_de_todos_os_itens(ambiente, monkeypatch):
    ctx, modelo, _ = ambiente
    artigos = {i.url: f"Full article text number {n} with more details." for n, i in enumerate(ITENS, 1)}

    def baixar(itens, cfg):
        for i in itens:
            i.texto_completo = artigos[i.url]

    monkeypatch.setattr(fluxo, "baixar_texto_completo", baixar)
    estado = asyncio.run(fluxo.construir_grafo_radar().ainvoke(_entrada(texto_completo=True), context=ctx))
    assert len(estado["relatorio"].itens) == 3
    prompts = [m[1][1] for m in modelo.chamadas_de("ResultadoItem")]
    assert len(prompts) == 3
    assert all(any(f"Texto do artigo:\n{t}" in p for p in prompts) for t in artigos.values())


def test_retoma_execucao_interrompida_sem_refazer_traducoes(ambiente, monkeypatch):
    ctx, modelo, tmp_path = ambiente
    gerar_pdf_original = fluxo.gerar_pdf
    falhas = {"restantes": 1}

    def gerar_pdf_instavel(rel, destino):
        if falhas["restantes"]:
            falhas["restantes"] -= 1
            raise OSError("disco cheio")
        return gerar_pdf_original(rel, destino)

    monkeypatch.setattr(fluxo, "gerar_pdf", gerar_pdf_instavel)
    execucao = {"configurable": {"thread_id": "teste"}}

    async def rodar():
        async with aiosqlite.connect(str(tmp_path / "cp.sqlite3")) as con:
            grafo = fluxo.construir_grafo_radar(AsyncSqliteSaver(con, serde=fluxo.SERIALIZADOR))
            with pytest.raises(OSError):
                await grafo.ainvoke(_entrada(), execucao, context=ctx)
            parado = await grafo.aget_state(execucao)
            chamadas_antes = len(modelo.chamadas)
            final = await grafo.ainvoke(None, execucao, context=ctx)  # retomar
            return parado, chamadas_antes, final

    parado, chamadas_antes, final = asyncio.run(rodar())
    assert parado.next == ("gerar_pdf",)
    assert len(modelo.chamadas) == chamadas_antes  # nada foi traduzido de novo
    assert final["pdf"].endswith(".pdf")


def test_deduplicacao_semantica_mantem_maior_peso():
    itens = [
        _item(1, "OpenAI now trains its frontier AI models with no internet access", peso=3),
        _item(2, "OpenAI says it will no longer train its AI models on the live internet", peso=5, fonte="Fonte B"),
        _item(3, "Canva launches Craft, its first in-house image model"),
    ]
    vetores = {1: [1.0, 0.1, 0.0], 2: [0.95, 0.2, 0.0], 3: [0.0, 0.1, 1.0]}  # 1 e 2 "parecidos"
    embed = lambda textos: [vetores[int(next(i.url[-1] for i in itens if t.startswith(i.titulo)))] for t in textos]
    resultado = deduplicar_semantico(itens, 0.9, embed)
    assert [i.url[-1] for i in resultado] == ["2", "3"]
    assert len(deduplicar_semantico(itens, 0.999, embed)) == 3
    # mesma fonte nunca é considerada duplicada
    mesma_fonte = [i.model_copy(update={"fonte": "arXiv"}) for i in itens]
    assert len(deduplicar_semantico(mesma_fonte, 0.9, embed)) == 3


def test_filtro_por_palavras_chave():
    fonte = Fonte(nome="arXiv", url="https://rss.arxiv.org/rss/cs.AI", palavras_chave=["LLM", "agent"])
    assert fonte.aceita("A new LLM benchmark")
    assert fonte.aceita("Multi-agent systems")  # "agent" como palavra inteira dentro de "multi-agent"
    assert not fonte.aceita("Belief-function layers for neural networks")
    assert not fonte.aceita("Agentic workflows")  # não casa pedaço de palavra
    assert Fonte(nome="x", url="https://x.com/f").aceita("qualquer coisa")
