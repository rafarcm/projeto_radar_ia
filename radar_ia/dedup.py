"""Normalização de URLs e remoção de notícias duplicadas."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np
from rapidfuzz import fuzz

from radar_ia.modelos import ItemColetado

log = logging.getLogger(__name__)

_PARAMETROS_RASTREIO = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid",
    "ref", "ref_src", "ref_url", "source", "cmpid", "_hsenc", "_hsmi", "mkt_tok",
    "guccounter", "guce_referrer", "guce_referrer_sig", "s_cid", "spm",
}
_NAO_ALFANUM = re.compile(r"[^\w\s]")


def normalizar_url(url: str) -> str:
    """Forma canônica: https, host minúsculo sem www, sem utm_*/rastreio, sem #fragmento
    e sem barra final. Usada como chave de deduplicação e de cache."""
    partes = urlsplit(url.strip())
    esquema = "https" if partes.scheme in ("http", "https", "") else partes.scheme
    host = partes.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    host = host.removesuffix(":443").removesuffix(":80")
    query = [
        (k, v)
        for k, v in parse_qsl(partes.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _PARAMETROS_RASTREIO
    ]
    caminho = partes.path.rstrip("/") or ""
    return urlunsplit((esquema, host, caminho, urlencode(sorted(query)), ""))


def _titulo_comparavel(titulo: str) -> str:
    return _NAO_ALFANUM.sub(" ", titulo.casefold()).strip()


def deduplicar(itens: list[ItemColetado], limiar_titulo: int = 90) -> list[ItemColetado]:
    """Remove duplicados por URL normalizada e por similaridade de título.

    Entre duplicados, fica o da fonte de maior peso (e, empatando, o primeiro)."""
    ordenados = sorted(enumerate(itens), key=lambda p: (-p[1].peso, p[0]))
    mantidos: list[tuple[int, ItemColetado]] = []
    urls: set[str] = set()
    titulos: list[str] = []
    for pos, item in ordenados:
        if item.url_normalizada in urls:
            continue
        t = _titulo_comparavel(item.titulo)
        if any(fuzz.token_sort_ratio(t, outro) >= limiar_titulo for outro in titulos):
            continue
        urls.add(item.url_normalizada)
        titulos.append(t)
        mantidos.append((pos, item))
    mantidos.sort(key=lambda p: p[0])
    return [item for _, item in mantidos]


Embedder = Callable[[list[str]], Sequence[Sequence[float]]]


def embedder_local(modelo: str, pasta: Path) -> Embedder:
    """Embeddings calculados na própria máquina (fastembed/ONNX), sem API paga.
    O modelo é baixado do Hugging Face na primeira vez e fica em `pasta`."""
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    from fastembed import TextEmbedding

    pasta.mkdir(parents=True, exist_ok=True)
    m = TextEmbedding(model_name=modelo, cache_dir=str(pasta))
    return lambda textos: list(m.embed(textos))


def deduplicar_semantico(itens: list[ItemColetado], limiar: float, embed: Embedder) -> list[ItemColetado]:
    """Remove a mesma notícia contada com outras palavras (similaridade de cosseno
    entre embeddings de título + início do resumo). Fica o item de maior peso.

    Só compara itens de fontes diferentes: uma fonte não publica a mesma notícia
    duas vezes, e itens da mesma fonte (ex.: artigos do arXiv sobre o mesmo tema)
    eram os falsos positivos na calibração com dados reais."""
    if len(itens) < 2:
        return itens
    vetores = np.asarray(embed([f"{i.titulo}. {i.resumo[:300]}" for i in itens]), dtype=float)
    vetores /= np.clip(np.linalg.norm(vetores, axis=1, keepdims=True), 1e-12, None)
    similaridade = vetores @ vetores.T
    mantidos: list[int] = []
    for k in sorted(range(len(itens)), key=lambda k: (-itens[k].peso, k)):
        parecido = next((j for j in mantidos
                         if itens[j].fonte != itens[k].fonte and similaridade[k, j] >= limiar), None)
        if parecido is not None:
            log.info("duplicado_semantico descartado=%r mantido=%r similaridade=%.2f",
                     itens[k].titulo[:70], itens[parecido].titulo[:70], similaridade[k, parecido])
            continue
        mantidos.append(k)
    return [itens[k] for k in sorted(mantidos)]


def limitar_itens(itens: list[ItemColetado], limite: int) -> list[ItemColetado]:
    """Escolhe até `limite` itens alternando entre fontes (rodízio), das de maior peso
    para as de menor e, em cada fonte, dos mais recentes para os mais antigos.
    Evita que uma fonte volumosa (ex.: arXiv) ocupe todo o limite."""
    por_fonte: dict[str, list[ItemColetado]] = {}
    for item in itens:
        por_fonte.setdefault(item.fonte, []).append(item)
    for lista in por_fonte.values():
        lista.sort(key=lambda i: i.data.timestamp() if i.data else 0.0, reverse=True)
    filas = sorted(por_fonte.values(), key=lambda lista: -lista[0].peso)
    escolhidos: list[ItemColetado] = []
    rodada = 0
    while len(escolhidos) < limite and any(rodada < len(f) for f in filas):
        for fila in filas:
            if rodada < len(fila) and len(escolhidos) < limite:
                escolhidos.append(fila[rodada])
        rodada += 1
    return escolhidos
