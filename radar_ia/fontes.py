"""Cadastro de fontes (fontes.yaml): validação e edição preservando comentários."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

ARQUIVO_PADRAO = Path("fontes.yaml")


class ErroFontes(Exception):
    """fontes.yaml ausente, malformado ou com fonte inválida."""


class Fonte(BaseModel):
    nome: str = Field(min_length=1)
    url: HttpUrl
    categoria: str | None = None
    ativa: bool = True
    peso: int = Field(default=3, ge=1, le=5)
    # Opcional: só entram itens cujo título/resumo contenha ao menos um destes termos.
    palavras_chave: list[str] = []

    def aceita(self, texto: str) -> bool:
        """Filtro gratuito (sem LLM) por palavras-chave; sem lista, aceita tudo."""
        if not self.palavras_chave:
            return True
        return any(
            re.search(rf"(?<!\w){re.escape(p)}(?!\w)", texto, re.IGNORECASE) for p in self.palavras_chave
        )

    @field_validator("nome")
    @classmethod
    def _sem_espacos_nas_bordas(cls, v: str) -> str:
        return v.strip()


class ArquivoFontes(BaseModel):
    fontes: list[Fonte]

    @field_validator("fontes")
    @classmethod
    def _nomes_unicos(cls, fontes: list[Fonte]) -> list[Fonte]:
        vistos: set[str] = set()
        for f in fontes:
            chave = f.nome.casefold()
            if chave in vistos:
                raise ValueError(f"nome de fonte repetido: '{f.nome}'")
            vistos.add(chave)
        return fontes


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def _ler_bruto(caminho: Path) -> CommentedMap:
    if not caminho.exists():
        raise ErroFontes(f"Arquivo de fontes não encontrado: {caminho}")
    try:
        dados = _yaml().load(caminho.read_text(encoding="utf-8"))
    except YAMLError as e:
        raise ErroFontes(f"{caminho}: YAML malformado.\n{e}") from e
    if not isinstance(dados, dict) or "fontes" not in dados:
        raise ErroFontes(f"{caminho}: o arquivo precisa ter uma lista 'fontes:' no topo.")
    return dados


def _mensagem_validacao(caminho: Path, bruto: CommentedMap, erro: ValidationError) -> str:
    lista = bruto.get("fontes") or []
    linhas = [f"{caminho}: há problemas no cadastro de fontes:"]
    for err in erro.errors():
        loc = err["loc"]
        if len(loc) >= 2 and loc[0] == "fontes" and isinstance(loc[1], int):
            idx = loc[1]
            item = lista[idx] if idx < len(lista) else {}
            nome = item.get("nome") if isinstance(item, dict) else None
            alvo = f"fonte #{idx + 1}" + (f" ('{nome}')" if nome else "")
            campo = ".".join(str(p) for p in loc[2:]) or "(item)"
            linhas.append(f"  - {alvo}, campo '{campo}': {_traduzir_erro(err)}")
        else:
            linhas.append(f"  - {'.'.join(str(p) for p in loc)}: {_traduzir_erro(err)}")
    return "\n".join(linhas)


def _traduzir_erro(err: dict) -> str:
    tipo = err["type"]
    ctx = err.get("ctx") or {}
    traducoes = {
        "missing": "campo obrigatório ausente",
        "url_parsing": "URL inválida",
        "url_scheme": "a URL deve começar com http:// ou https://",
        "bool_parsing": "use true ou false",
        "int_parsing": "deve ser um número inteiro",
        "greater_than_equal": f"deve ser no mínimo {ctx.get('ge')}",
        "less_than_equal": f"deve ser no máximo {ctx.get('le')}",
        "string_too_short": "não pode ficar vazio",
        "string_type": "deve ser texto",
        "model_type": "cada fonte deve ser um bloco com nome, url etc.",
        "list_type": "deve ser uma lista",
    }
    if tipo == "value_error":
        return str(ctx.get("error", err["msg"]))
    return traducoes.get(tipo, err["msg"])


def carregar_fontes(caminho: Path = ARQUIVO_PADRAO) -> list[Fonte]:
    """Carrega e valida todas as fontes cadastradas."""
    bruto = _ler_bruto(caminho)
    try:
        return ArquivoFontes.model_validate({"fontes": bruto.get("fontes") or []}).fontes
    except ValidationError as e:
        raise ErroFontes(_mensagem_validacao(caminho, bruto, e)) from e


def fontes_ativas(caminho: Path = ARQUIVO_PADRAO) -> list[Fonte]:
    """Somente as fontes com ativa: true. É a única lista que o sistema usa."""
    return [f for f in carregar_fontes(caminho) if f.ativa]


def _localizar(bruto: CommentedMap, nome: str) -> int:
    for i, item in enumerate(bruto["fontes"]):
        if str(item.get("nome", "")).strip().casefold() == nome.strip().casefold():
            return i
    raise ErroFontes(f"Fonte não encontrada: '{nome}'. Use 'radar fontes listar'.")


def _salvar(caminho: Path, bruto: CommentedMap) -> None:
    # Valida antes de gravar: nunca deixa o arquivo num estado inválido.
    try:
        ArquivoFontes.model_validate({"fontes": bruto.get("fontes") or []})
    except ValidationError as e:
        raise ErroFontes(_mensagem_validacao(caminho, bruto, e)) from e
    with caminho.open("w", encoding="utf-8") as fh:
        _yaml().dump(bruto, fh)


def adicionar_fonte(
    nome: str,
    url: str,
    categoria: str | None = None,
    peso: int = 3,
    caminho: Path = ARQUIVO_PADRAO,
) -> Fonte:
    bruto = _ler_bruto(caminho)
    if bruto["fontes"] is None:
        bruto["fontes"] = []
    nova = CommentedMap(nome=nome, url=url)
    if categoria:
        nova["categoria"] = categoria
    nova["ativa"] = True
    nova["peso"] = peso
    bruto["fontes"].append(nova)
    _salvar(caminho, bruto)
    return Fonte.model_validate(dict(nova))


def remover_fonte(nome: str, caminho: Path = ARQUIVO_PADRAO) -> None:
    bruto = _ler_bruto(caminho)
    del bruto["fontes"][_localizar(bruto, nome)]
    _salvar(caminho, bruto)


def definir_ativa(nome: str, ativa: bool, caminho: Path = ARQUIVO_PADRAO) -> None:
    bruto = _ler_bruto(caminho)
    bruto["fontes"][_localizar(bruto, nome)]["ativa"] = ativa
    _salvar(caminho, bruto)
