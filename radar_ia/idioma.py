"""Detecção de idioma, glossário e verificações automáticas da tradução."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml
from langdetect import DetectorFactory, LangDetectException, detect_langs
from pydantic import BaseModel, Field, ValidationError, model_validator

DetectorFactory.seed = 0  # langdetect é aleatório sem semente fixa
# langdetect não é seguro entre threads (em paralelo erra metade das detecções),
# e o LangGraph processa os itens em threads: uma detecção por vez.
_TRAVA_DETECCAO = threading.Lock()


def _candidatos(texto: str) -> list:
    with _TRAVA_DETECCAO:
        return detect_langs(texto)

NOMES_IDIOMAS = {
    "en": "inglês", "pt": "português", "es": "espanhol", "fr": "francês",
    "de": "alemão", "it": "italiano", "zh-cn": "chinês", "zh-tw": "chinês",
    "zh": "chinês", "ja": "japonês", "ko": "coreano", "ru": "russo", "nl": "holandês",
}
MIN_CARACTERES_DETECCAO = 25
CONFIANCA_MINIMA = 0.80
CONFIANCA_PORTUGUES = 0.60

_URL = re.compile(r"https?://[^\s)\]>\"']+")
_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")
_DATA_BR = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_LETRAS = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass
class Deteccao:
    idioma: str | None  # código ISO ("en", "pt"...) ou None = ambíguo, o LLM decide
    confianca: float
    origem: str  # "texto" | "feed" | "indefinido"


def nome_idioma(codigo: str | None) -> str:
    if not codigo:
        return "idioma desconhecido"
    return NOMES_IDIOMAS.get(codigo.lower(), codigo)


def _detectar_texto(texto: str) -> tuple[str | None, float]:
    try:
        candidatos = _candidatos(texto)
    except LangDetectException:
        return None, 0.0
    melhor = candidatos[0]
    return melhor.lang, melhor.prob


def detectar_idioma(texto: str, idioma_feed: str | None = None) -> Deteccao:
    """Detecta o idioma; com texto curto ou pouca confiança, usa o idioma do feed."""
    texto = _URL.sub(" ", texto or "")
    if len(_LETRAS.findall(texto)) >= MIN_CARACTERES_DETECCAO:
        idioma, conf = _detectar_texto(texto)
        if idioma and conf >= CONFIANCA_MINIMA:
            return Deteccao(idioma, conf, "texto")
    if idioma_feed:
        return Deteccao(idioma_feed.split("-")[0].lower(), 0.5, "feed")
    return Deteccao(None, 0.0, "indefinido")


def eh_portugues(texto: str) -> bool:
    """True se o texto parece português. Textos curtos demais para detectar
    (ex.: "GPT-5 e Claude") passam; a checagem é feita no título+resumo juntos."""
    limpo = _URL.sub(" ", texto or "")
    if len(_LETRAS.findall(limpo)) < MIN_CARACTERES_DETECCAO:
        return True
    try:
        candidatos = _candidatos(limpo)
    except LangDetectException:
        return True
    return any(c.lang == "pt" and c.prob >= CONFIANCA_PORTUGUES for c in candidatos)


# ---------------------------------------------------------------- glossário


class ErroGlossario(Exception):
    """glossario.yaml malformado."""


class TermoGlossario(BaseModel):
    termo: str = Field(min_length=1)
    manter: bool = False
    traduzir_como: str | None = None

    @model_validator(mode="after")
    def _uma_regra(self) -> "TermoGlossario":
        if self.manter == bool(self.traduzir_como):
            raise ValueError("defina exatamente uma regra: 'manter: true' ou 'traduzir_como'")
        return self


class Glossario(BaseModel):
    versao: int = 1
    termos: list[TermoGlossario] = []

    def para_prompt(self) -> str:
        if not self.termos:
            return "(glossário vazio)"
        linhas = []
        for t in self.termos:
            if t.manter:
                linhas.append(f'- "{t.termo}": manter em inglês, sem traduzir')
            else:
                linhas.append(f'- "{t.termo}": traduzir sempre como "{t.traduzir_como}"')
        return "\n".join(linhas)


def carregar_glossario(caminho: Path = Path("glossario.yaml")) -> Glossario:
    if not caminho.exists():
        return Glossario()
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
        return Glossario.model_validate(dados)
    except yaml.YAMLError as e:
        raise ErroGlossario(f"{caminho}: YAML inválido: {e}") from e
    except ValidationError as e:
        termos = (dados or {}).get("termos") or []
        linhas = []
        for err in e.errors():
            loc = err["loc"]
            if len(loc) >= 2 and loc[0] == "termos" and isinstance(loc[1], int) and loc[1] < len(termos):
                alvo = f"termo '{(termos[loc[1]] or {}).get('termo', loc[1] + 1)}'"
            else:
                alvo = ".".join(str(p) for p in loc)
            linhas.append(f"  - {alvo}: {err['msg']}")
        raise ErroGlossario(f"{caminho} inválido:\n" + "\n".join(linhas)) from e


def _contem_termo(texto: str, termo: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(termo)}(?!\w)", texto, re.IGNORECASE) is not None


def violacoes_glossario(original: str, traduzido: str, glossario: Glossario) -> list[str]:
    """Termos com 'traduzir_como' que ficaram no original em vez da tradução definida."""
    problemas = []
    for t in glossario.termos:
        if not t.traduzir_como or not _contem_termo(original, t.termo):
            continue
        # O termo em inglês continua em algum ponto do texto traduzido.
        if _contem_termo(traduzido, t.termo):
            problemas.append(f'"{t.termo}" deveria ser traduzido como "{t.traduzir_como}"')
    return problemas


# ------------------------------------------------------ números e URLs


def _numeros(texto: str) -> set[str]:
    """Números sem separadores ("1.5" e "1,5" viram "15"; zeros à esquerda caem)."""
    achados = set()
    for n in _NUMERO.findall(_URL.sub(" ", texto)):
        so_digitos = re.sub(r"[.,]", "", n).lstrip("0") or "0"
        achados.add(so_digitos)
    return achados


def numeros_inventados(original: str, gerado: str) -> set[str]:
    """Números no texto gerado que não existem no original. Datas reformatadas
    para dd/mm/aaaa são ignoradas, pois o mês pode vir de um nome ("October"), e
    dígitos isolados também ("three" -> "3" é uma conversão legítima)."""
    sem_datas = _DATA_BR.sub(" ", gerado)
    return {n for n in _numeros(sem_datas) - _numeros(original) if len(n) > 1}


def numeros_perdidos_no_titulo(titulo_original: str, titulo_pt: str) -> set[str]:
    """Números do título original que sumiram do título traduzido."""
    return _numeros(titulo_original) - _numeros(titulo_pt)


def urls_alteradas(original: str, gerado: str) -> set[str]:
    """URLs no texto gerado que não existem no original (alteradas ou inventadas)."""
    return set(_URL.findall(gerado)) - set(_URL.findall(original))
