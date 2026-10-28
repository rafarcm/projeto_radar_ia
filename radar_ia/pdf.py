"""Geração do relatório em PDF com reportlab.

Escolha: reportlab instala só com pip (wheels prontos), sem as bibliotecas de
sistema (Pango/Cairo) que o weasyprint exige."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from radar_ia.idioma import nome_idioma
from radar_ia.modelos import TEMAS, ItemProcessado, Relatorio
from radar_ia.processamento import ErroIdiomaRelatorio, textos_fora_do_portugues

log = logging.getLogger(__name__)
FUSO = ZoneInfo("America/Sao_Paulo")
AVISO_IA = (
    "As traduções e os resumos deste relatório foram gerados por IA e podem conter erros. "
    "Confira sempre a fonte original."
)

_CANDIDATAS_FONTE = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]


def _registrar_fontes() -> tuple[str, str]:
    """Usa uma TTF Unicode se houver; senão Helvetica (cobre acentos do português)."""
    for normal, negrito in _CANDIDATAS_FONTE:
        if Path(normal).exists() and Path(negrito).exists():
            if "RadarNormal" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("RadarNormal", normal))
                pdfmetrics.registerFont(TTFont("RadarNegrito", negrito))
                pdfmetrics.registerFontFamily("RadarNormal", normal="RadarNormal", bold="RadarNegrito",
                                              italic="RadarNormal", boldItalic="RadarNegrito")
            return "RadarNormal", "RadarNegrito"
    return "Helvetica", "Helvetica-Bold"


def _local(dt: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    return dt.astimezone(FUSO).strftime(fmt) if dt else "data não informada"


def _estilos() -> dict[str, ParagraphStyle]:
    normal, negrito = _registrar_fontes()
    base = getSampleStyleSheet()
    azul = colors.HexColor("#1f4e79")
    cinza = colors.HexColor("#555555")
    return {
        "capa_titulo": ParagraphStyle("capa_titulo", parent=base["Title"], fontName=negrito,
                                      fontSize=34, leading=40, textColor=azul, alignment=TA_CENTER),
        "capa_sub": ParagraphStyle("capa_sub", parent=base["Normal"], fontName=normal,
                                   fontSize=14, leading=20, alignment=TA_CENTER, textColor=cinza),
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontName=negrito, fontSize=18,
                             leading=22, textColor=azul, spaceBefore=6, spaceAfter=10),
        "tema": ParagraphStyle("tema", parent=base["Heading2"], fontName=negrito, fontSize=15,
                               leading=19, textColor=azul, spaceBefore=10, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName=negrito, fontSize=13,
                             leading=17, spaceBefore=8, spaceAfter=2),
        "texto": ParagraphStyle("texto", parent=base["BodyText"], fontName=normal, fontSize=10.5,
                                leading=15),
        "original": ParagraphStyle("original", parent=base["BodyText"], fontName=normal,
                                   fontSize=8.5, leading=11, textColor=cinza),
        "meta": ParagraphStyle("meta", parent=base["BodyText"], fontName=normal, fontSize=8.5,
                               leading=11, textColor=cinza, spaceAfter=3),
        "aviso": ParagraphStyle("aviso", parent=base["BodyText"], fontName=normal, fontSize=9,
                                leading=12, textColor=colors.HexColor("#8a4b00")),
    }


def _link(url: str, texto: str) -> str:
    return f'<a href="{escape(url, {chr(34): "&quot;"})}" color="#1f4e79"><u>{escape(texto)}</u></a>'


def _bloco_item(item: ItemProcessado, e: dict[str, ParagraphStyle], destaque: bool = False) -> KeepTogether:
    partes = []
    if item.traducao_indisponivel:
        partes.append(Paragraph(_link(item.url, "Tradução indisponível"), e["h2"]))
        partes.append(Paragraph(f"Título original: {escape(item.titulo_original)}", e["original"]))
    else:
        partes.append(Paragraph(_link(item.url, item.titulo_pt), e["h2"]))
        if item.foi_traduzido:
            partes.append(Paragraph(escape(item.titulo_original), e["original"]))
    meta = [escape(item.fonte), _local(item.data)]
    if destaque:
        meta.append(f"tema: {item.tema}")
    if item.foi_traduzido and not item.traducao_indisponivel:
        meta.append(f"Traduzido do {nome_idioma(item.idioma_original)}")
    partes.append(Paragraph(" · ".join(meta), e["meta"]))
    partes.append(Paragraph(escape(item.resumo_pt), e["texto"]))
    if item.sinalizado and item.observacao:
        partes.append(Paragraph("⚠ " + escape(item.observacao), e["aviso"]))
    partes.append(Spacer(1, 8))
    return KeepTogether(partes)


def _rodape(gerado_em: datetime, fonte: str):
    def desenhar(canvas, doc) -> None:  # noqa: ANN001 - assinatura do reportlab
        canvas.saveState()
        canvas.setFont(fonte, 7.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(2 * cm, 1.4 * cm, f"Gerado em {_local(gerado_em)} (horário de Brasília)")
        canvas.drawString(2 * cm, 1.0 * cm, "Traduções e resumos gerados por IA: confira sempre a fonte original.")
        canvas.drawRightString(A4[0] - 2 * cm, 1.0 * cm, f"Página {doc.page}")
        canvas.restoreState()

    return desenhar


def nome_arquivo(pasta: Path, data: datetime) -> Path:
    # Com a hora, uma segunda execução no mesmo dia não sobrescreve o relatório anterior.
    return pasta / f"radar-ia_{data.astimezone(FUSO):%Y-%m-%d_%H%M}.pdf"


def aviso_historico(ja_reportados: int) -> str:
    return (f"{ja_reportados} notícias coletadas já saíram em relatórios anteriores e não foram repetidas "
            "(use --ignorar-historico para incluí-las).")


def gerar_pdf(rel: Relatorio, destino: Path) -> Path:
    """Gera o PDF. Bloqueia (ErroIdiomaRelatorio) se algum texto não estiver em português."""
    if problemas := textos_fora_do_portugues(rel):
        raise ErroIdiomaRelatorio(
            "Geração do PDF bloqueada: textos fora do português em: " + "; ".join(problemas)
        )
    e = _estilos()
    destino.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(destino), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2.2 * cm,
        title=f"Radar IA - {_local(rel.fim, '%d/%m/%Y')}", author="radar-ia", lang="pt-BR",
    )
    h: list = []

    # Capa
    h += [Spacer(1, 7 * cm), Paragraph("Radar IA", e["capa_titulo"]), Spacer(1, 0.6 * cm),
          Paragraph("Notícias de Inteligência Artificial", e["capa_sub"]), Spacer(1, 0.4 * cm),
          Paragraph(f"Período: {_local(rel.inicio)} a {_local(rel.fim)}", e["capa_sub"]),
          Paragraph(f"{len(rel.itens)} notícias", e["capa_sub"])]
    if rel.ja_reportados:
        h += [Spacer(1, 0.4 * cm), Paragraph(escape(aviso_historico(rel.ja_reportados)), e["aviso"])]
    h.append(PageBreak())

    # Visão geral e destaques
    h += [Paragraph("Visão geral", e["h1"]), Paragraph(escape(rel.visao_geral), e["texto"]), Spacer(1, 12)]
    if rel.destaques:
        h.append(Paragraph("Destaques do dia", e["h1"]))
        h += [_bloco_item(i, e, destaque=True) for i in rel.destaques]

    # Notícias por tema
    if rel.itens:
        h.append(PageBreak())
        h.append(Paragraph("Notícias por tema", e["h1"]))
        por_tema: dict[str, list[ItemProcessado]] = defaultdict(list)
        for i in rel.itens:
            por_tema["Tradução indisponível" if i.traducao_indisponivel else i.tema].append(i)
        for tema in [*TEMAS, "Tradução indisponível"]:
            if por_tema.get(tema):
                h.append(Paragraph(f"{tema} ({len(por_tema[tema])})", e["tema"]))
                h += [_bloco_item(i, e) for i in por_tema[tema]]
    else:
        h.append(Paragraph("Nenhuma notícia nova no período.", e["texto"]))

    # Falhas
    if rel.falhas:
        h.append(Paragraph("Fontes que falharam", e["h1"]))
        for f in rel.falhas:
            h.append(Paragraph(f"<b>{escape(f.fonte)}</b>: {escape(f.erro)}<br/>"
                               f"{_link(f.url, f.url)}", e["texto"]))
            h.append(Spacer(1, 4))

    h += [Spacer(1, 14), Paragraph(escape(AVISO_IA), e["aviso"])]
    rodape = _rodape(rel.gerado_em, e["texto"].fontName)
    doc.build(h, onFirstPage=rodape, onLaterPages=rodape)
    log.info("pdf_gerado arquivo=%s itens=%d", destino, len(rel.itens))
    return destino
