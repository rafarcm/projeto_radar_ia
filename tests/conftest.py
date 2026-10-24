"""Fixtures compartilhadas: modelo LLM falso e dados de exemplo."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import pytest

from radar_ia.config import ConfigProcessamento
from radar_ia.idioma import Glossario, TermoGlossario
from radar_ia.modelos import ItemColetado, ItemProcessado, Relatorio


class RunnableFalso:
    def __init__(self, modelo: "ModeloFalso", schema: type):
        self.modelo, self.schema = modelo, schema

    def with_retry(self, **_: Any) -> "RunnableFalso":
        return self

    async def ainvoke(self, mensagens: list) -> Any:
        self.modelo.chamadas.append((self.schema.__name__, mensagens))
        fila = self.modelo.respostas[self.schema.__name__]
        resposta = fila.pop(0) if len(fila) > 1 else fila[0]
        if isinstance(resposta, Exception):
            raise resposta
        if callable(resposta):
            resposta = resposta(mensagens)
        return self.schema.model_validate(resposta)


class ModeloFalso:
    """Imita BaseChatModel.with_structured_output sem chamar nenhuma API.

    `respostas` mapeia o nome do schema para uma fila de respostas (dict, Exception
    ou função mensagens->dict); a última resposta se repete."""

    def __init__(self, respostas: dict[str, list[dict | Exception | Callable]]):
        self.respostas = respostas
        self.chamadas: list[tuple[str, list]] = []

    def with_structured_output(self, schema: type, **_: Any) -> RunnableFalso:
        return RunnableFalso(self, schema)

    def chamadas_de(self, schema: str) -> list[list]:
        return [m for s, m in self.chamadas if s == schema]


@pytest.fixture
def cfg_proc() -> ConfigProcessamento:
    return ConfigProcessamento(concorrencia=2, tentativas=1, max_tentativas_traducao=2, num_destaques=3)


@pytest.fixture
def glossario() -> Glossario:
    return Glossario(versao=1, termos=[
        TermoGlossario(termo="fine-tuning", manter=True),
        TermoGlossario(termo="open source", traduzir_como="código aberto"),
    ])


@pytest.fixture
def item_en() -> ItemColetado:
    return ItemColetado(
        fonte="Exemplo",
        peso=4,
        titulo="Acme releases open source model with 70 billion parameters",
        resumo="Acme announced on Monday a new open source language model with 70 billion "
               "parameters that beats previous models on several reasoning benchmarks.",
        url="https://exemplo.com/noticia?utm_source=rss",
        url_normalizada="https://exemplo.com/noticia",
        data=datetime(2026, 10, 23, 12, tzinfo=timezone.utc),
        idioma_feed="en",
    )


def resultado_pt(**extra: Any) -> dict:
    base = {
        "titulo_pt": "Acme lança modelo de código aberto com 70 bilhões de parâmetros",
        "resumo_pt": "A Acme anunciou na segunda-feira um novo modelo de linguagem de código aberto "
                     "com 70 bilhões de parâmetros, que supera modelos anteriores em vários testes de raciocínio.",
        "idioma_original": "en",
        "foi_traduzido": True,
        "tema": "Modelos",
        "relevancia": 4,
    }
    return base | extra


def resultado_en() -> dict:
    return resultado_pt(
        titulo_pt="Acme releases open source model with 70 billion parameters",
        resumo_pt="Acme announced on Monday a new open source language model with 70 billion parameters "
                  "that beats previous models on several reasoning benchmarks.",
    )


def item_processado(n: int = 1, **extra: Any) -> ItemProcessado:
    base = dict(
        titulo_pt=f"Notícia número {n} sobre inteligência artificial em português",
        resumo_pt="Este é um resumo de exemplo escrito em português do Brasil, com acentuação: "
                  "ação, informação, coração, pão e são. Ele descreve uma novidade fictícia.",
        idioma_original="en",
        foi_traduzido=True,
        tema="Modelos",
        relevancia=3,
        fonte="Fonte Exemplo",
        url=f"https://exemplo.com/{n}",
        data=datetime(2026, 10, 23, 10, tzinfo=timezone.utc),
        titulo_original=f"Example news number {n}",
        peso=3,
    )
    return ItemProcessado(**(base | extra))


def relatorio_exemplo(**extra: Any) -> Relatorio:
    itens = [item_processado(i, tema=t) for i, t in enumerate(["Modelos", "Pesquisa", "Regulação", "Produtos"], 1)]
    base = dict(
        gerado_em=datetime(2026, 10, 23, 12, tzinfo=timezone.utc),
        inicio=datetime(2026, 10, 22, 12, tzinfo=timezone.utc),
        fim=datetime(2026, 10, 23, 12, tzinfo=timezone.utc),
        visao_geral="O período foi marcado por lançamentos de modelos e novidades em regulação, "
                    "com destaque para pesquisas sobre raciocínio e segurança.",
        destaques=itens[:3],
        itens=itens,
        falhas=[],
    )
    return Relatorio(**(base | extra))
