"""Leitura dos feeds RSS/Atom cadastrados, com retentativas e janela de tempo."""

from __future__ import annotations

import calendar
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from radar_ia.config import ConfigColeta
from radar_ia.dedup import normalizar_url
from radar_ia.fontes import Fonte
from radar_ia.modelos import FalhaFonte, ItemColetado

log = logging.getLogger(__name__)

_TAGS = re.compile(r"<[^>]+>")
_ESPACOS = re.compile(r"\s+")
MAX_RESUMO = 1500
MAX_TEXTO_COMPLETO = 6000


@dataclass
class ResultadoColeta:
    itens: list[ItemColetado] = field(default_factory=list)
    falhas: list[FalhaFonte] = field(default_factory=list)
    sem_data: int = 0


def _erro_transitorio(e: BaseException) -> bool:
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in (408, 425, 429) or e.response.status_code >= 500
    return isinstance(e, httpx.TransportError)


def baixar(url: str, cfg: ConfigColeta) -> httpx.Response:
    """GET com timeout, User-Agent identificável e retentativas com backoff."""

    @retry(
        stop=stop_after_attempt(cfg.tentativas),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_erro_transitorio),
        reraise=True,
    )
    def _get() -> httpx.Response:
        resp = httpx.get(
            url,
            timeout=cfg.timeout_segundos,
            follow_redirects=True,
            headers={"User-Agent": cfg.user_agent},
        )
        resp.raise_for_status()
        return resp

    return _get()


def limpar_html(texto: str) -> str:
    """Remove tags e espaços extras de títulos/resumos de feed."""
    return _ESPACOS.sub(" ", html.unescape(_TAGS.sub(" ", texto or ""))).strip()


def extrair_data(entrada: dict) -> datetime | None:
    """Data de publicação em UTC; None se ausente ou impossível de interpretar."""
    for chave in ("published_parsed", "updated_parsed", "created_parsed"):
        tupla = entrada.get(chave)
        if tupla:
            try:
                return datetime.fromtimestamp(calendar.timegm(tupla), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    for chave in ("published", "updated", "created", "date"):
        bruto = entrada.get(chave)
        if not bruto:
            continue
        for interpretar in (parsedate_to_datetime, datetime.fromisoformat):
            try:
                dt = interpretar(str(bruto).strip())
            except (TypeError, ValueError, IndexError):
                continue
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return None


def dentro_da_janela(data: datetime | None, inicio: datetime, fim: datetime) -> bool:
    """Itens sem data entram (o histórico SQLite evita repeti-los); datas futuras
    com folga de 1h são aceitas por causa de relógios/fusos mal configurados."""
    if data is None:
        return True
    return inicio <= data <= fim + timedelta(hours=1)


def interpretar_feed(conteudo: bytes, fonte: Fonte) -> tuple[list[ItemColetado], str | None]:
    """Converte o XML do feed em itens. Retorna (itens, erro_de_parse)."""
    feed = feedparser.parse(conteudo)
    if feed.bozo and not feed.entries:
        return [], f"feed ilegível: {feed.get('bozo_exception', 'formato desconhecido')}"
    idioma = (feed.feed.get("language") or "").split("-")[0].lower() or None
    itens: list[ItemColetado] = []
    for e in feed.entries:
        link = e.get("link") or ""
        titulo = limpar_html(e.get("title", ""))
        if not link or not titulo:
            continue
        resumo = limpar_html(e.get("summary") or e.get("description") or "")
        itens.append(
            ItemColetado(
                fonte=fonte.nome,
                peso=fonte.peso,
                categoria=fonte.categoria,
                titulo=titulo,
                resumo=resumo[:MAX_RESUMO],
                url=link,
                url_normalizada=normalizar_url(link),
                data=extrair_data(e),
                idioma_feed=idioma,
            )
        )
    return itens, None


def coletar_fonte(fonte: Fonte, cfg: ConfigColeta) -> list[ItemColetado]:
    resp = baixar(str(fonte.url), cfg)
    itens, erro = interpretar_feed(resp.content, fonte)
    if erro:
        raise ValueError(erro)
    return itens


def coletar(
    fontes: list[Fonte],
    cfg: ConfigColeta,
    inicio: datetime,
    fim: datetime,
) -> ResultadoColeta:
    """Lê todas as fontes em paralelo; falhas são registradas e não interrompem."""
    resultado = ResultadoColeta()
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(fontes)))) as pool:
        futuros = {f.nome: (f, pool.submit(coletar_fonte, f, cfg)) for f in fontes}
        for nome, (fonte, futuro) in futuros.items():
            try:
                itens = futuro.result()
            except Exception as e:  # noqa: BLE001 - qualquer falha da fonte vira relatório
                msg = _descrever_erro(e)
                log.warning("fonte_falhou fonte=%r erro=%r", nome, msg)
                resultado.falhas.append(FalhaFonte(fonte=nome, url=str(fonte.url), erro=msg))
                continue
            na_janela = [
                i for i in itens
                if dentro_da_janela(i.data, inicio, fim) and fonte.aceita(f"{i.titulo} {i.resumo}")
            ]
            resultado.sem_data += sum(1 for i in na_janela if i.data is None)
            na_janela.sort(key=lambda i: i.data or fim, reverse=True)
            na_janela = na_janela[: cfg.max_itens_por_fonte]
            log.info("fonte_lida fonte=%r itens_feed=%d na_janela=%d", nome, len(itens), len(na_janela))
            resultado.itens.extend(na_janela)
    return resultado


def _descrever_erro(e: BaseException) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, httpx.TimeoutException):
        return "tempo esgotado"
    if isinstance(e, httpx.TransportError):
        return f"erro de rede ({type(e).__name__})"
    return str(e) or type(e).__name__


# ---------------------------------------------------------------- texto completo

_robots_cache: dict[str, RobotFileParser | None] = {}


def permitido_por_robots(url: str, cfg: ConfigColeta) -> bool:
    """Consulta o robots.txt do site (com cache). Sem robots.txt = permitido."""
    partes = urlsplit(url)
    base = f"{partes.scheme}://{partes.netloc}"
    if base not in _robots_cache:
        parser: RobotFileParser | None = RobotFileParser()
        try:
            resp = httpx.get(
                f"{base}/robots.txt",
                timeout=cfg.timeout_segundos,
                follow_redirects=True,
                headers={"User-Agent": cfg.user_agent},
            )
            if resp.status_code >= 400:
                parser = None  # sem robots.txt
            else:
                parser.parse(resp.text.splitlines())  # type: ignore[union-attr]
        except httpx.HTTPError:
            # Na dúvida, não baixa.
            _robots_cache[base] = RobotFileParser()
            _robots_cache[base].disallow_all = True  # type: ignore[union-attr]
            return False
        _robots_cache[base] = parser
    parser = _robots_cache[base]
    return parser is None or parser.can_fetch(cfg.user_agent, url)


def baixar_texto_completo(itens: list[ItemColetado], cfg: ConfigColeta) -> None:
    """Preenche item.texto_completo com trafilatura, respeitando robots.txt."""
    import trafilatura

    def _um(item: ItemColetado) -> None:
        if not permitido_por_robots(item.url, cfg):
            log.info("robots_bloqueou url=%s", item.url)
            return
        try:
            resp = baixar(item.url, cfg)
            texto = trafilatura.extract(resp.text, include_comments=False, include_tables=False)
        except Exception as e:  # noqa: BLE001
            log.warning("texto_completo_falhou url=%s erro=%r", item.url, _descrever_erro(e))
            return
        if texto:
            item.texto_completo = texto[:MAX_TEXTO_COMPLETO]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_um, itens))
