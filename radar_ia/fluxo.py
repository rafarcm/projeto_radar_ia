"""Orquestração com LangGraph (biblioteca open source, sem serviço pago).

Dois grafos:

* grafo do item: preparar -> gerar -> verificar -> (refazer: gerar | concluir)
  A revisão da tradução é um laço condicional explícito.
* grafo do radar: coletar -> selecionar -> [texto_completo] -> traduzir_item (um por
  notícia, em paralelo via Send) -> montar_relatorio -> checagem_final -> gerar_pdf
  -> registrar_historico -> [entregar]

Com um checkpointer SQLite, cada etapa concluída fica salva e uma execução
interrompida pode ser retomada (`radar gerar --retomar`) sem refazer o que já foi feito.
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from radar_ia import entrega
from radar_ia.armazenamento import Armazenamento, chave_traducao
from radar_ia.coleta import baixar_texto_completo, coletar
from radar_ia.config import Config
from radar_ia.dedup import Embedder, deduplicar, deduplicar_semantico, limitar_itens, normalizar_url
from radar_ia.fontes import Fonte
from radar_ia.idioma import detectar_idioma
from radar_ia.modelos import FalhaFonte, ItemColetado, ItemProcessado, Relatorio
from radar_ia.pdf import gerar_pdf, nome_arquivo
from radar_ia.processamento import (
    Contexto,
    ResultadoItem,
    aplicar_fallback_idioma,
    conteudo_original,
    escolher_destaques,
    estruturado,
    gerar_visao_geral,
    item_indisponivel,
    mensagens_item,
    montar_item,
    problemas_da_traducao,
    verificar_com_llm,
)

log = logging.getLogger(__name__)

# Só estes tipos podem ser lidos de volta do banco de checkpoints.
SERIALIZADOR = JsonPlusSerializer(
    allowed_msgpack_modules=[ItemColetado, ItemProcessado, FalhaFonte, Relatorio, ResultadoItem]
)

# ================================================================ grafo do item


class EstadoItem(TypedDict, total=False):
    item: ItemColetado
    idioma: str | None
    idioma_confiavel: bool
    chave: str
    tentativa: int
    max_retentativas: int
    correcao: str
    resultado: ResultadoItem | None
    pendencias: list[str]
    idioma_ok: bool
    processado: ItemProcessado | None


def _preparar(estado: EstadoItem, runtime: Runtime[Contexto]) -> dict[str, Any]:
    """Detecta o idioma e consulta o cache de traduções."""
    ctx, item = runtime.context, estado["item"]
    deteccao = detectar_idioma(f"{item.titulo}. {item.resumo}", item.idioma_feed)
    chave = chave_traducao(
        item.url_normalizada, conteudo_original(item), ctx.glossario.versao, ctx.identificador_modelo
    )
    saida: dict[str, Any] = {
        "idioma": deteccao.idioma, "idioma_confiavel": deteccao.origem == "texto",
        "chave": chave, "tentativa": 0, "max_retentativas": ctx.cfg.max_tentativas_traducao,
        "correcao": "", "processado": None,
    }
    if ctx.armazenamento and (cache := ctx.armazenamento.obter_traducao(chave)):
        log.debug("cache_traducao_hit url=%s", item.url_normalizada)
        r = ResultadoItem.model_validate(cache["resultado"])
        saida["processado"] = montar_item(item, r, cache["idioma"], sinalizado=cache.get("sinalizado", False),
                                          observacao=cache.get("observacao"))
    return saida


async def _gerar(estado: EstadoItem, runtime: Runtime[Contexto]) -> dict[str, Any]:
    """Uma chamada estruturada ao LLM: tradução + resumo + tema + relevância."""
    ctx = runtime.context
    gerar = estruturado(ctx.modelo, ResultadoItem, ctx.cfg.tentativas)
    r = await gerar.ainvoke(mensagens_item(estado["item"], ctx, estado["idioma"], estado["correcao"]))
    return {"resultado": r, "tentativa": estado["tentativa"] + 1}


async def _verificar(estado: EstadoItem, runtime: Runtime[Contexto]) -> dict[str, Any]:
    """Checagens locais (idioma, números, URLs, glossário) e, opcionalmente, revisão por LLM."""
    ctx, item, r = runtime.context, estado["item"], estado["resultado"]
    prob_idioma, prob_fidelidade = problemas_da_traducao(item, r, ctx.glossario)
    if not prob_idioma and not prob_fidelidade and ctx.verificar_traducao:
        prob_fidelidade = await verificar_com_llm(item, r, ctx)
    pendencias = prob_idioma + prob_fidelidade
    if pendencias:
        log.info("traducao_rejeitada url=%s tentativa=%d problemas=%r", item.url, estado["tentativa"], pendencias)
    correcao = (
        "\n\nATENÇÃO: a tentativa anterior foi rejeitada pelos seguintes motivos; corrija:\n- "
        + "\n- ".join(pendencias)
    ) if pendencias else ""
    return {"pendencias": pendencias, "idioma_ok": not prob_idioma, "correcao": correcao}


def _concluir(estado: EstadoItem, runtime: Runtime[Contexto]) -> dict[str, Any]:
    """Publica, publica com aviso ou cai no fallback "tradução indisponível"."""
    ctx, item, r = runtime.context, estado["item"], estado["resultado"]
    pendencias = estado.get("pendencias", [])
    idioma = (estado["idioma"] if estado["idioma_confiavel"] else None) or r.idioma_original.lower()
    if not estado["idioma_ok"]:
        motivo = "; ".join(pendencias) or "falha na tradução"
        log.warning("traducao_indisponivel url=%s motivo=%r", item.url, motivo)
        return {"processado": item_indisponivel(item, idioma, motivo)}
    observacao = None
    if pendencias:
        observacao = "A verificação automática apontou possíveis divergências: " + "; ".join(pendencias)
        log.warning("traducao_sinalizada url=%s problemas=%r", item.url, pendencias)
    if ctx.armazenamento:
        ctx.armazenamento.salvar_traducao(
            estado["chave"],
            {"resultado": r.model_dump(), "idioma": idioma, "sinalizado": bool(pendencias), "observacao": observacao},
        )
    return {"processado": montar_item(item, r, idioma, sinalizado=bool(pendencias), observacao=observacao)}


def _depois_de_preparar(estado: EstadoItem) -> str:
    return END if estado.get("processado") else "gerar"


def _depois_de_verificar(estado: EstadoItem) -> str:
    if not estado["pendencias"]:
        return "concluir"
    if estado["tentativa"] <= estado["max_retentativas"]:
        return "gerar"  # laço de revisão
    return "concluir"


def construir_grafo_item():
    g = StateGraph(EstadoItem, context_schema=Contexto)
    g.add_node("preparar", _preparar)
    g.add_node("gerar", _gerar)
    g.add_node("verificar", _verificar)
    g.add_node("concluir", _concluir)
    g.add_edge(START, "preparar")
    g.add_conditional_edges("preparar", _depois_de_preparar, ["gerar", END])
    g.add_edge("gerar", "verificar")
    g.add_conditional_edges("verificar", _depois_de_verificar, ["gerar", "concluir"])
    g.add_edge("concluir", END)
    # Sem checkpoint próprio: o grafo do radar já salva o resultado de cada item.
    return g.compile(checkpointer=False)


GRAFO_ITEM = construir_grafo_item()


async def processar_item(item: ItemColetado, ctx: Contexto) -> ItemProcessado:
    """Roda o grafo do item. Exceções (ex.: API fora do ar) são propagadas."""
    saida = await GRAFO_ITEM.ainvoke({"item": item}, context=ctx)
    return saida["processado"]


async def processar_item_seguro(item: ItemColetado, ctx: Contexto) -> ItemProcessado:
    """Como processar_item, mas um erro vira "tradução indisponível" em vez de derrubar o relatório."""
    try:
        return await processar_item(item, ctx)
    except Exception as e:  # noqa: BLE001
        log.error("item_falhou url=%s erro=%s: %s", item.url, type(e).__name__, e)
        idioma = detectar_idioma(item.titulo, item.idioma_feed).idioma or "desconhecido"
        return item_indisponivel(item, idioma, f"erro ao chamar o modelo: {type(e).__name__}")


# ================================================================ grafo do radar


class Opcoes(TypedDict):
    """Opções da execução. Ficam no estado para que --retomar use as mesmas."""

    horas: int
    max_itens: int
    simular: bool
    sem_enviar: bool
    texto_completo: bool
    ignorar_historico: bool
    verificar_traducao: bool


class EstadoRadar(TypedDict, total=False):
    opcoes: Opcoes
    inicio: datetime
    fim: datetime
    coletados: list[ItemColetado]
    falhas: list[FalhaFonte]
    selecionados: list[ItemColetado]
    processados: Annotated[list[ItemProcessado], operator.add]  # junta os resultados paralelos
    relatorio: Relatorio
    pdf: str
    entrega: dict[str, str | None]


class EntradaItem(TypedDict):
    item: ItemColetado


@dataclass
class ContextoRadar:
    """Recursos da execução (não vão para o checkpoint; são recriados ao retomar)."""

    config: Config
    fontes: list[Fonte]
    banco: Armazenamento
    proc: Contexto | None = None  # None em --simular: nada de LLM
    embedder: Embedder | None = None


def _coletar(estado: EstadoRadar, runtime: Runtime[ContextoRadar]) -> dict[str, Any]:
    ctx = runtime.context
    log.info("coleta_inicio fontes=%d horas=%d", len(ctx.fontes), estado["opcoes"]["horas"])
    c = coletar(ctx.fontes, ctx.config.coleta, estado["inicio"], estado["fim"])
    if c.sem_data:
        log.info("itens_sem_data quantidade=%d (mantidos; o histórico evita repetição)", c.sem_data)
    return {"coletados": c.itens, "falhas": c.falhas}


def _selecionar(estado: EstadoRadar, runtime: Runtime[ContextoRadar]) -> dict[str, Any]:
    ctx, opcoes = runtime.context, estado["opcoes"]
    itens = deduplicar(estado["coletados"], ctx.config.deduplicacao.limiar_titulo)
    log.info("deduplicacao_titulo antes=%d depois=%d", len(estado["coletados"]), len(itens))
    if not opcoes["ignorar_historico"]:
        vistos = ctx.banco.ja_reportados(i.url_normalizada for i in itens)
        itens = [i for i in itens if i.url_normalizada not in vistos]
        log.info("historico ja_reportados=%d restantes=%d", len(vistos), len(itens))
    if ctx.embedder:
        antes = len(itens)
        itens = deduplicar_semantico(itens, ctx.config.deduplicacao.limiar_semantico, ctx.embedder)
        log.info("deduplicacao_semantica antes=%d depois=%d", antes, len(itens))
    if len(itens) > opcoes["max_itens"]:
        log.info("limite_itens descartados=%d max_itens=%d", len(itens) - opcoes["max_itens"], opcoes["max_itens"])
    return {"selecionados": limitar_itens(itens, opcoes["max_itens"])}


def _texto_completo(estado: EstadoRadar, runtime: Runtime[ContextoRadar]) -> dict[str, Any]:
    itens = [i.model_copy() for i in estado["selecionados"]]
    baixar_texto_completo(itens, runtime.context.config.coleta)
    return {"selecionados": itens}


def _distribuir(estado: EstadoRadar) -> list[Send] | str:
    """Uma tarefa `traduzir_item` por notícia; o LangGraph as roda em paralelo."""
    if not estado["selecionados"]:
        return "montar_relatorio"
    return [Send("traduzir_item", {"item": i}) for i in estado["selecionados"]]


def _depois_de_selecionar(estado: EstadoRadar) -> list[Send] | str:
    if estado["opcoes"]["simular"]:
        return END
    if estado["opcoes"]["texto_completo"]:
        return "texto_completo"
    return _distribuir(estado)


async def _traduzir_item(entrada: EntradaItem, runtime: Runtime[ContextoRadar]) -> dict[str, Any]:
    return {"processados": [await processar_item_seguro(entrada["item"], runtime.context.proc)]}


async def _montar_relatorio(estado: EstadoRadar, runtime: Runtime[ContextoRadar]) -> dict[str, Any]:
    ctx = runtime.context.proc
    # Ordem estável (o paralelismo devolve os itens em qualquer ordem).
    processados = sorted(estado.get("processados", []),
                         key=lambda i: (i.pontuacao, i.data or estado["inicio"], i.url), reverse=True)
    rel = Relatorio(
        gerado_em=datetime.now(timezone.utc),
        inicio=estado["inicio"],
        fim=estado["fim"],
        visao_geral=await gerar_visao_geral(processados, ctx),
        destaques=escolher_destaques(processados, ctx.cfg.num_destaques),
        itens=processados,
        falhas=estado.get("falhas", []),
    )
    return {"relatorio": rel}


def _checagem_final(estado: EstadoRadar) -> dict[str, Any]:
    return {"relatorio": aplicar_fallback_idioma(estado["relatorio"])}


def _gerar_pdf(estado: EstadoRadar, runtime: Runtime[ContextoRadar]) -> dict[str, Any]:
    destino = nome_arquivo(runtime.context.config.saida.pasta_relatorios, estado["fim"])
    return {"pdf": str(gerar_pdf(estado["relatorio"], destino))}


def _registrar_historico(estado: EstadoRadar, runtime: Runtime[ContextoRadar]) -> dict[str, Any]:
    # Itens com "tradução indisponível" ficam fora do histórico para nova tentativa.
    runtime.context.banco.marcar_reportados(
        (normalizar_url(i.url), i.titulo_original)
        for i in estado["relatorio"].itens if not i.traducao_indisponivel
    )
    return {}


def _depois_do_historico(estado: EstadoRadar) -> str:
    return END if estado["opcoes"]["sem_enviar"] else "entregar"


def _entregar(estado: EstadoRadar, runtime: Runtime[ContextoRadar]) -> dict[str, Any]:
    if not entrega.canais_ativos(runtime.context.config.entrega):
        return {"entrega": {}}
    return {"entrega": entrega.entregar(estado["relatorio"], Path(estado["pdf"]), runtime.context.config.entrega)}


def construir_grafo_radar(checkpointer: Any = None):
    g = StateGraph(EstadoRadar, context_schema=ContextoRadar)
    g.add_node("coletar", _coletar)
    g.add_node("selecionar", _selecionar)
    g.add_node("texto_completo", _texto_completo)
    g.add_node("traduzir_item", _traduzir_item)
    g.add_node("montar_relatorio", _montar_relatorio)
    g.add_node("checagem_final", _checagem_final)
    g.add_node("gerar_pdf", _gerar_pdf)
    g.add_node("registrar_historico", _registrar_historico)
    g.add_node("entregar", _entregar)

    g.add_edge(START, "coletar")
    g.add_edge("coletar", "selecionar")
    g.add_conditional_edges("selecionar", _depois_de_selecionar,
                            ["texto_completo", "traduzir_item", "montar_relatorio", END])
    g.add_conditional_edges("texto_completo", _distribuir, ["traduzir_item", "montar_relatorio"])
    g.add_edge("traduzir_item", "montar_relatorio")
    g.add_edge("montar_relatorio", "checagem_final")
    g.add_edge("checagem_final", "gerar_pdf")
    g.add_edge("gerar_pdf", "registrar_historico")
    g.add_conditional_edges("registrar_historico", _depois_do_historico, ["entregar", END])
    g.add_edge("entregar", END)
    return g.compile(checkpointer=checkpointer)
