"""Peças de LangChain usadas pelos grafos de radar_ia/fluxo.py: prompts, saída
estruturada, verificações, fallback, destaques e visão geral."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from radar_ia.armazenamento import Armazenamento
from radar_ia.config import ConfigProcessamento
from radar_ia.idioma import (
    Glossario,
    eh_portugues,
    nome_idioma,
    numeros_inventados,
    numeros_perdidos_no_titulo,
    urls_alteradas,
    violacoes_glossario,
)
from radar_ia.modelos import TEMAS, ItemColetado, ItemProcessado, Relatorio, Tema

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ schemas


class ResultadoItem(BaseModel):
    """Saída estruturada do LLM para uma notícia."""

    titulo_pt: str = Field(description="Título em português do Brasil.")
    resumo_pt: str = Field(description="Resumo fiel em português do Brasil, 2 a 3 frases.")
    idioma_original: str = Field(description="Código ISO 639-1 do idioma original, ex.: en, pt, es.")
    foi_traduzido: bool = Field(description="true se o original não estava em português.")
    tema: Tema = Field(description="Tema principal, um da lista fixa.")
    relevancia: int = Field(ge=1, le=5, description="Relevância para quem acompanha IA, de 1 a 5.")
    # Padrão True para que traduções antigas do cache continuem válidas.
    conteudo_suficiente: bool = Field(
        default=True,
        description="false se o conteúdo fornecido traz só o título ou um resumo curto, "
                    "sem os detalhes necessários para um bom resumo.",
    )


class Verificacao(BaseModel):
    """Saída estruturada da verificação opcional de fidelidade."""

    fiel: bool = Field(description="true se a tradução não perdeu nem inventou informação.")
    problemas: list[str] = Field(
        default_factory=list, description="Problemas encontrados, em português. Vazio se fiel."
    )


class TextoVisaoGeral(BaseModel):
    visao_geral: str = Field(description="Um parágrafo curto em português do Brasil.")


# ------------------------------------------------------------------ prompts

REGRAS_ITEM = """Você é um editor de tecnologia que escreve em português do Brasil.
Sua tarefa: a partir APENAS do conteúdo da notícia fornecida, produzir título e resumo em pt-BR.

Regras de conteúdo (obrigatórias):
- Use somente as informações do conteúdo fornecido. Não use conhecimento externo.
- Não invente números, nomes, datas, citações nem conclusões.
- Se o conteúdo for insuficiente para um resumo, diga isso no resumo (ex.: "O feed traz apenas o título; não há detalhes sobre ...").
- O resumo tem 2 a 3 frases.
- conteudo_suficiente: false se o conteúdo for só o título ou um resumo curto, sem os detalhes principais da notícia (o quê, quem, como, números, disponibilidade); true caso contrário.

Regras de idioma (obrigatórias):
- Se o original NÃO estiver em português: traduza e resuma de uma vez, com fidelidade, sem acrescentar, omitir ou inventar informações.
- Se o original estiver em português (Brasil ou Portugal): apenas padronize para ortografia e vocabulário do Brasil.
- Mantenha inalterados nomes próprios, empresas, produtos, modelos e bibliotecas (ex.: GPT, Claude, LangChain), siglas consagradas (LLM, RAG, API) e termos técnicos sem tradução estabelecida em português.
- Na primeira ocorrência de um termo técnico mantido em inglês, você pode explicá-lo brevemente entre parênteses, se ajudar.
- Preserve números, unidades e valores exatamente como estão. Datas: use o formato dd/mm/aaaa.
- Nunca traduza URLs, nomes de arquivos nem trechos de código.
- Todo o texto de saída (titulo_pt e resumo_pt) deve estar em português do Brasil.

Glossário (siga à risca):
{glossario}

Classificação:
- tema: um destes valores exatos: {temas}.
- relevancia: 1 (nota menor) a 5 (marco importante para a área de IA).
- idioma_original: código ISO 639-1 do idioma do conteúdo original."""

MODELO_ITEM = """Fonte: {fonte}
Idioma detectado automaticamente: {idioma}
URL (não altere): {url}

Título original:
{titulo}

Resumo do feed:
{resumo}
{texto_extra}{correcao}"""

REGRAS_VERIFICACAO = """Você é um revisor de traduções. Compare o ORIGINAL com a TRADUÇÃO (que é um resumo em pt-BR).
Um resumo pode omitir detalhes secundários, mas NÃO pode:
- trazer números, datas ou valores diferentes do original;
- citar pessoas, empresas, produtos ou fatos que não estão no original;
- perder a informação principal do título.
Responda em português."""

REGRAS_VISAO_GERAL = """Você é um editor de tecnologia. Escreva UM parágrafo curto (3 a 5 frases) em português do Brasil
com a visão geral das notícias de IA do período, baseando-se SOMENTE nos itens listados.
Não invente fatos, números ou tendências que não estejam nos itens. Mantenha nomes de produtos e empresas como estão."""


# ------------------------------------------------------------------ modelo


class ModeloChat(Protocol):
    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable: ...


def criar_modelo(identificador: str) -> ModeloChat:
    """Modelo trocável por RADAR_MODELO (ex.: "anthropic:claude-opus-5-5")."""
    from langchain.chat_models import init_chat_model

    return init_chat_model(identificador, max_tokens=16000)


def estruturado(modelo: ModeloChat, schema: type[BaseModel], tentativas: int) -> Runnable:
    return modelo.with_structured_output(schema, method="json_schema").with_retry(
        stop_after_attempt=tentativas, wait_exponential_jitter=True
    )


# ------------------------------------------------------------------ itens


@dataclass
class Contexto:
    modelo: ModeloChat
    glossario: Glossario
    cfg: ConfigProcessamento
    armazenamento: Armazenamento | None = None
    identificador_modelo: str = ""
    verificar_traducao: bool = False
    # url -> texto do artigo (ou None); usado quando o feed não traz detalhes suficientes.
    buscar_artigo: Callable[[str], str | None] | None = None


def conteudo_original(item: ItemColetado) -> str:
    return "\n".join(p for p in (item.titulo, item.resumo, item.texto_completo or "") if p)


def problemas_da_traducao(
    item: ItemColetado, r: ResultadoItem, glossario: Glossario
) -> tuple[list[str], list[str]]:
    """Retorna (problemas_de_idioma, problemas_de_fidelidade)."""
    original = conteudo_original(item)
    gerado = f"{r.titulo_pt}\n{r.resumo_pt}"
    idioma: list[str] = []
    if not r.titulo_pt.strip() or not r.resumo_pt.strip():
        idioma.append("título ou resumo vazio")
    elif not eh_portugues(gerado):
        idioma.append("o texto gerado não está em português")
    fidelidade: list[str] = []
    if perdidos := numeros_perdidos_no_titulo(item.titulo, r.titulo_pt):
        fidelidade.append(f"números do título original ausentes no título traduzido: {sorted(perdidos)}")
    if inventados := numeros_inventados(original, gerado):
        fidelidade.append(f"números que não existem no original: {sorted(inventados)}")
    if urls := urls_alteradas(original, gerado):
        fidelidade.append(f"URLs que não existem no original: {sorted(urls)}")
    fidelidade.extend(violacoes_glossario(original, gerado, glossario))
    return idioma, fidelidade


def mensagens_item(item: ItemColetado, ctx: Contexto, idioma: str | None, correcao: str) -> list:
    extra = f"\nTexto do artigo:\n{item.texto_completo}\n" if item.texto_completo else ""
    sistema = REGRAS_ITEM.format(glossario=ctx.glossario.para_prompt(), temas=", ".join(TEMAS))
    usuario = MODELO_ITEM.format(
        fonte=item.fonte,
        idioma=idioma or "indefinido (identifique você)",
        url=item.url,
        titulo=item.titulo,
        resumo=item.resumo or "(o feed não trouxe resumo)",
        texto_extra=extra,
        correcao=correcao,
    )
    return [("system", sistema), ("user", usuario)]


async def verificar_com_llm(item: ItemColetado, r: ResultadoItem, ctx: Contexto) -> list[str]:
    verificador = estruturado(ctx.modelo, Verificacao, ctx.cfg.tentativas)
    mensagens = [
        ("system", REGRAS_VERIFICACAO),
        ("user", f"ORIGINAL:\n{conteudo_original(item)}\n\nTRADUÇÃO:\n{r.titulo_pt}\n{r.resumo_pt}"),
    ]
    v: Verificacao = await verificador.ainvoke(mensagens)
    return [] if v.fiel else (v.problemas or ["o revisor apontou divergência sem detalhar"])


def montar_item(item: ItemColetado, r: ResultadoItem, idioma: str, **extra: Any) -> ItemProcessado:
    return ItemProcessado(
        titulo_pt=r.titulo_pt.strip(),
        resumo_pt=r.resumo_pt.strip(),
        idioma_original=idioma,
        foi_traduzido=not idioma.startswith("pt"),
        tema=r.tema,
        relevancia=r.relevancia,
        fonte=item.fonte,
        url=item.url,  # o link vem sempre do feed, nunca do LLM
        data=item.data,
        titulo_original=item.titulo,
        peso=item.peso,
        **extra,
    )


def item_indisponivel(item: ItemColetado, idioma: str, motivo: str) -> ItemProcessado:
    """Fallback: sem texto em outro idioma no relatório, só o título original e o link."""
    return ItemProcessado(
        titulo_pt="Tradução indisponível",
        resumo_pt="Não foi possível gerar um resumo confiável em português. Consulte a fonte original.",
        idioma_original=idioma,
        foi_traduzido=False,
        tema="Outros",
        relevancia=1,
        fonte=item.fonte,
        url=item.url,
        data=item.data,
        titulo_original=item.titulo,
        peso=item.peso,
        traducao_indisponivel=True,
        observacao=motivo,
    )


# ------------------------------------------------------- destaques e visão geral


def escolher_destaques(itens: list[ItemProcessado], n: int) -> list[ItemProcessado]:
    candidatos = [i for i in itens if not i.traducao_indisponivel]
    return sorted(candidatos, key=lambda i: (i.pontuacao, i.data or datetime.min.replace(tzinfo=timezone.utc)),
                  reverse=True)[:n]


def visao_geral_padrao(itens: list[ItemProcessado]) -> str:
    """Texto determinístico, usado se o LLM falhar ou não gerar português."""
    validos = [i for i in itens if not i.traducao_indisponivel]
    if not validos:
        return "Nenhuma notícia nova foi processada no período."
    temas = Counter(i.tema for i in validos).most_common(3)
    fontes = len({i.fonte for i in validos})
    lista = ", ".join(f"{t} ({q})" for t, q in temas)
    return (
        f"Foram selecionadas {len(validos)} notícias de {fontes} fontes no período. "
        f"Os temas mais frequentes foram: {lista}."
    )


async def gerar_visao_geral(itens: list[ItemProcessado], ctx: Contexto) -> str:
    validos = [i for i in itens if not i.traducao_indisponivel]
    if not validos:
        return visao_geral_padrao(itens)
    lista = "\n".join(f"- [{i.tema}] {i.titulo_pt}: {i.resumo_pt}" for i in validos)
    gerar = estruturado(ctx.modelo, TextoVisaoGeral, ctx.cfg.tentativas)
    for _ in range(ctx.cfg.max_tentativas_traducao + 1):
        try:
            r: TextoVisaoGeral = await gerar.ainvoke(
                [("system", REGRAS_VISAO_GERAL), ("user", f"Itens do período:\n{lista}")]
            )
        except Exception as e:  # noqa: BLE001
            log.error("visao_geral_falhou erro=%s", type(e).__name__)
            break
        if r.visao_geral.strip() and eh_portugues(r.visao_geral) and not urls_alteradas(lista, r.visao_geral):
            return r.visao_geral.strip()
        log.info("visao_geral_refeita motivo=idioma_ou_conteudo")
    return visao_geral_padrao(itens)


# ---------------------------------------------------------- checagem final (5.7)


class ErroIdiomaRelatorio(Exception):
    """Algum texto que iria para o relatório não está em português."""


def textos_fora_do_portugues(rel: Relatorio) -> list[str]:
    """Lista legível de cada texto do relatório que não passou na checagem de idioma."""
    problemas = []
    if not eh_portugues(rel.visao_geral):
        problemas.append("visão geral")
    for i in rel.itens + rel.destaques:
        if i.traducao_indisponivel:
            continue  # só exibe título original + link, por definição
        if i.tema not in TEMAS:
            problemas.append(f"tema inválido em '{i.url}'")
        if not eh_portugues(f"{i.titulo_pt}\n{i.resumo_pt}"):
            problemas.append(f"item '{i.titulo_original}' ({i.url})")
    return list(dict.fromkeys(problemas))


def aplicar_fallback_idioma(rel: Relatorio) -> Relatorio:
    """Itens que não passaram viram 'tradução indisponível'; visão geral vira o texto padrão."""
    itens = []
    for i in rel.itens:
        if not i.traducao_indisponivel and not eh_portugues(f"{i.titulo_pt}\n{i.resumo_pt}"):
            log.warning("checagem_final_fallback url=%s", i.url)
            i = item_indisponivel(
                ItemColetado(fonte=i.fonte, peso=i.peso, titulo=i.titulo_original, url=i.url,
                             url_normalizada=i.url, data=i.data),
                i.idioma_original,
                "texto final não passou na checagem de idioma",
            )
        itens.append(i)
    visao = rel.visao_geral if eh_portugues(rel.visao_geral) else visao_geral_padrao(itens)
    n = len(rel.destaques) or 5
    return rel.model_copy(update={"itens": itens, "visao_geral": visao,
                                  "destaques": escolher_destaques(itens, max(3, min(5, n)))})
