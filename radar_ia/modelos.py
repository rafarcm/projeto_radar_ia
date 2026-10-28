"""Modelos de dados compartilhados entre as etapas do pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Tema = Literal[
    "Modelos",
    "Pesquisa",
    "Produtos",
    "Regulação",
    "Negócios",
    "Segurança",
    "Open Source",
    "Outros",
]
TEMAS: tuple[str, ...] = Tema.__args__  # type: ignore[attr-defined]


class ItemColetado(BaseModel):
    """Notícia como veio do feed, já com URL normalizada."""

    fonte: str
    peso: int = 3
    categoria: str | None = None
    titulo: str
    resumo: str = ""
    url: str
    url_normalizada: str
    data: datetime | None = None
    idioma_feed: str | None = None
    texto_completo: str | None = None


class ItemProcessado(BaseModel):
    """Notícia traduzida/resumida em pt-BR, pronta para o relatório."""

    titulo_pt: str
    resumo_pt: str
    idioma_original: str
    foi_traduzido: bool
    tema: Tema = "Outros"
    relevancia: int = Field(default=1, ge=1, le=5)
    fonte: str
    url: str
    data: datetime | None = None
    titulo_original: str
    peso: int = 3
    traducao_indisponivel: bool = False
    sinalizado: bool = False
    observacao: str | None = None

    @property
    def pontuacao(self) -> float:
        """Pontuação para destaques: relevância do LLM ponderada pelo peso da fonte."""
        return self.relevancia * 0.7 + self.peso * 0.3


class FalhaFonte(BaseModel):
    """Fonte que não pôde ser lida nesta execução."""

    fonte: str
    url: str
    erro: str


class Relatorio(BaseModel):
    """Tudo o que vai para o PDF e para as entregas."""

    gerado_em: datetime
    inicio: datetime
    fim: datetime
    visao_geral: str
    destaques: list[ItemProcessado]
    itens: list[ItemProcessado]
    falhas: list[FalhaFonte] = []
    ja_reportados: int = 0  # coletados que já saíram em relatórios anteriores
