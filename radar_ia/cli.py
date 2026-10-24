"""Linha de comando: `radar gerar`, `radar fontes ...`, `radar testar-entrega`."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import typer

from radar_ia import entrega
from radar_ia.armazenamento import Armazenamento
from radar_ia.coleta import baixar, interpretar_feed
from radar_ia.config import Config, ErroConfiguracao, carregar_config, modelo_configurado
from radar_ia.fontes import (
    ARQUIVO_PADRAO,
    ErroFontes,
    Fonte,
    adicionar_fonte,
    carregar_fontes,
    definir_ativa,
    fontes_ativas,
    remover_fonte,
)
from radar_ia.idioma import ErroGlossario, Glossario, carregar_glossario, detectar_idioma
from radar_ia.registro import configurar_logging

log = logging.getLogger("radar_ia")

# Códigos de saída
OK, ERRO, ENTREGA_PARCIAL = 0, 1, 3

app = typer.Typer(help="Radar de notícias de IA em português do Brasil.", no_args_is_help=True,
                  add_completion=False)
fontes_app = typer.Typer(help="Gerencia as fontes cadastradas em fontes.yaml.", no_args_is_help=True)
app.add_typer(fontes_app, name="fontes")

ArquivoFontesOpt = Annotated[Path, typer.Option("--arquivo", help="Arquivo de fontes.")]


@app.callback()
def principal(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Logs detalhados (DEBUG).")] = False,
) -> None:
    configurar_logging(verbose)


def _sair_com_erro(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(ERRO)


# ------------------------------------------------------------------ gerar


@app.command()
def gerar(
    horas: Annotated[int | None, typer.Option(help="Janela de tempo em horas (padrão: config.yaml).")] = None,
    max_itens: Annotated[int | None, typer.Option(help="Máximo de itens enviados ao LLM.")] = None,
    simular: Annotated[bool, typer.Option(help="Coleta e mostra o que seria processado, sem chamar o LLM.")] = False,
    sem_enviar: Annotated[bool, typer.Option(help="Só gera o PDF, sem e-mail/Telegram.")] = False,
    verificar_traducao: Annotated[bool, typer.Option(help="Segunda chamada ao LLM para revisar cada tradução.")] = False,
    texto_completo: Annotated[bool, typer.Option(help="Baixa o texto do artigo (respeita robots.txt).")] = False,
    ignorar_historico: Annotated[bool, typer.Option(help="Inclui notícias já reportadas antes.")] = False,
    retomar: Annotated[bool, typer.Option(help="Continua a última execução interrompida de onde parou.")] = False,
    config: Annotated[Path, typer.Option(help="Arquivo de configuração.")] = Path("config.yaml"),
    arquivo_fontes: Annotated[Path, typer.Option(help="Arquivo de fontes.")] = ARQUIVO_PADRAO,
    glossario: Annotated[Path, typer.Option(help="Arquivo de glossário.")] = Path("glossario.yaml"),
) -> None:
    """Coleta, traduz, resume e gera o PDF (e entrega pelos canais ativos)."""
    try:
        cfg = carregar_config(config)
        fontes = fontes_ativas(arquivo_fontes)
        gloss = carregar_glossario(glossario)
    except (ErroConfiguracao, ErroFontes, ErroGlossario) as e:
        _sair_com_erro(str(e))
    if not fontes:
        _sair_com_erro("Nenhuma fonte ativa em fontes.yaml.")
    from radar_ia.fluxo import Opcoes

    opcoes = Opcoes(
        horas=horas or cfg.coleta.horas,
        max_itens=max_itens or cfg.coleta.max_itens,
        simular=simular,
        sem_enviar=sem_enviar,
        texto_completo=texto_completo,
        ignorar_historico=ignorar_historico,
        verificar_traducao=verificar_traducao,
    )
    raise typer.Exit(asyncio.run(_executar(cfg, fontes, gloss, opcoes, retomar)))


# Uma única linha de execução no banco de checkpoints: cada "radar gerar" novo
# descarta a anterior; "--retomar" continua a que ficou pela metade.
THREAD = "radar"


def _criar_contexto(cfg: Config, fontes: list[Fonte], gloss: Glossario, opcoes, banco: Armazenamento):  # noqa: ANN001
    from radar_ia.dedup import embedder_local
    from radar_ia.fluxo import ContextoRadar
    from radar_ia.processamento import Contexto, criar_modelo

    embedder = None
    if cfg.deduplicacao.semantica:
        try:
            embedder = embedder_local(cfg.deduplicacao.modelo_embeddings, cfg.deduplicacao.pasta_modelos)
        except Exception as e:  # noqa: BLE001 - sem rede no 1º download, por exemplo
            log.warning("deduplicacao_semantica_desativada erro=%s: %s", type(e).__name__, e)
    proc = None
    if not opcoes["simular"]:
        identificador = modelo_configurado()
        try:
            modelo = criar_modelo(identificador)
        except Exception as e:  # noqa: BLE001 - provedor/pacote ausente, chave faltando
            _sair_com_erro(f"Não foi possível iniciar o modelo '{identificador}': {e}")
        proc = Contexto(modelo=modelo, glossario=gloss, cfg=cfg.processamento, armazenamento=banco,
                        identificador_modelo=identificador, verificar_traducao=opcoes["verificar_traducao"])
    return ContextoRadar(config=cfg, fontes=fontes, banco=banco, proc=proc, embedder=embedder)


async def _executar(cfg: Config, fontes: list[Fonte], gloss: Glossario, opcoes, retomar: bool) -> int:  # noqa: ANN001
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from radar_ia.fluxo import SERIALIZADOR, construir_grafo_radar
    from radar_ia.processamento import ErroIdiomaRelatorio

    banco = Armazenamento(cfg.saida.banco_sqlite)
    try:
        if opcoes["simular"] and not retomar:
            fim = datetime.now(timezone.utc)
            inicio = fim - timedelta(hours=opcoes["horas"])
            ctx = _criar_contexto(cfg, fontes, gloss, opcoes, banco)
            estado = await construir_grafo_radar().ainvoke(
                {"opcoes": opcoes, "inicio": inicio, "fim": fim}, context=ctx)
            _mostrar_simulacao(estado["selecionados"], estado["falhas"], inicio, fim)
            return OK

        cfg.saida.banco_checkpoints.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(cfg.saida.banco_checkpoints)) as conexao:
            saver = AsyncSqliteSaver(conexao, serde=SERIALIZADOR)
            grafo = construir_grafo_radar(saver)
            execucao = {"configurable": {"thread_id": THREAD}, "max_concurrency": cfg.processamento.concorrencia}
            if retomar:
                foto = await grafo.aget_state(execucao)
                if not foto.next:
                    typer.echo("Nenhuma execução interrompida para retomar.")
                    return OK
                opcoes = foto.values["opcoes"]
                log.info("retomando etapas=%s", ",".join(sorted(set(foto.next))))
                entrada = None
            else:
                await saver.adelete_thread(THREAD)
                fim = datetime.now(timezone.utc)
                entrada = {"opcoes": opcoes, "inicio": fim - timedelta(hours=opcoes["horas"]), "fim": fim}
            ctx = _criar_contexto(cfg, fontes, gloss, opcoes, banco)
            try:
                estado = await grafo.ainvoke(entrada, execucao, context=ctx)
            except ErroIdiomaRelatorio as e:
                typer.secho(str(e), fg=typer.colors.RED, err=True)
                return ERRO
            except Exception as e:  # noqa: BLE001 - etapas concluídas ficam salvas
                log.error("execucao_interrompida erro=%s: %s", type(e).__name__, e)
                typer.secho("Execução interrompida. As etapas concluídas foram salvas; "
                            "rode 'radar gerar --retomar' para continuar de onde parou.",
                            fg=typer.colors.RED, err=True)
                return ERRO
    finally:
        banco.fechar()

    typer.secho(f"PDF gerado: {estado['pdf']}", fg=typer.colors.GREEN)
    resultado = estado.get("entrega") or {}
    for canal, erro in resultado.items():
        if erro:
            typer.secho(f"{canal}: falhou ({erro})", fg=typer.colors.RED, err=True)
        else:
            typer.secho(f"{canal}: enviado", fg=typer.colors.GREEN)
    return ENTREGA_PARCIAL if any(resultado.values()) else OK


def _mostrar_simulacao(itens, falhas, inicio: datetime, fim: datetime) -> None:  # noqa: ANN001
    typer.secho(
        f"\nSIMULAÇÃO: {len(itens)} itens seriam enviados ao LLM "
        f"(janela {inicio:%d/%m %H:%M} a {fim:%d/%m %H:%M} UTC). Nenhuma chamada ao LLM foi feita.\n",
        bold=True,
    )
    for n, i in enumerate(itens, 1):
        det = detectar_idioma(f"{i.titulo}. {i.resumo}", i.idioma_feed)
        data = i.data.strftime("%d/%m %H:%M") if i.data else "sem data"
        typer.echo(f"{n:>3}. [{i.fonte}] ({det.idioma or '?'}, {data}) {i.titulo[:100]}")
    if falhas:
        typer.secho("\nFontes que falharam:", fg=typer.colors.YELLOW)
        for f in falhas:
            typer.echo(f"  - {f.fonte}: {f.erro}")


# ------------------------------------------------------------------ fontes


@fontes_app.command("listar")
def fontes_listar(arquivo: ArquivoFontesOpt = ARQUIVO_PADRAO) -> None:
    """Lista as fontes cadastradas."""
    try:
        fontes = carregar_fontes(arquivo)
    except ErroFontes as e:
        _sair_com_erro(str(e))
    for f in fontes:
        estado = typer.style("ativa  ", fg=typer.colors.GREEN) if f.ativa else typer.style("inativa", fg=typer.colors.YELLOW)
        typer.echo(f"{estado}  peso {f.peso}  {f.nome:<32} {f.categoria or '-':<11} {f.url}")
    typer.echo(f"\n{sum(f.ativa for f in fontes)} ativas de {len(fontes)}.")


@fontes_app.command("adicionar")
def fontes_adicionar(
    nome: str,
    url: str,
    categoria: Annotated[str | None, typer.Option(help="oficial | pesquisa | imprensa | comunidade")] = None,
    peso: Annotated[int, typer.Option(min=1, max=5)] = 3,
    arquivo: ArquivoFontesOpt = ARQUIVO_PADRAO,
) -> None:
    """Cadastra uma fonte nova (ativa)."""
    try:
        adicionar_fonte(nome, url, categoria, peso, arquivo)
    except ErroFontes as e:
        _sair_com_erro(str(e))
    typer.secho(f"Fonte '{nome}' adicionada. Rode 'radar fontes testar' para validar o feed.", fg=typer.colors.GREEN)


@fontes_app.command("remover")
def fontes_remover(nome: str, arquivo: ArquivoFontesOpt = ARQUIVO_PADRAO) -> None:
    """Remove uma fonte."""
    try:
        remover_fonte(nome, arquivo)
    except ErroFontes as e:
        _sair_com_erro(str(e))
    typer.secho(f"Fonte '{nome}' removida.", fg=typer.colors.GREEN)


@fontes_app.command("ativar")
def fontes_ativar(nome: str, arquivo: ArquivoFontesOpt = ARQUIVO_PADRAO) -> None:
    """Ativa uma fonte."""
    try:
        definir_ativa(nome, True, arquivo)
    except ErroFontes as e:
        _sair_com_erro(str(e))
    typer.secho(f"Fonte '{nome}' ativada.", fg=typer.colors.GREEN)


@fontes_app.command("desativar")
def fontes_desativar(nome: str, arquivo: ArquivoFontesOpt = ARQUIVO_PADRAO) -> None:
    """Desativa uma fonte sem apagá-la."""
    try:
        definir_ativa(nome, False, arquivo)
    except ErroFontes as e:
        _sair_com_erro(str(e))
    typer.secho(f"Fonte '{nome}' desativada.", fg=typer.colors.GREEN)


@fontes_app.command("testar")
def fontes_testar(
    todas: Annotated[bool, typer.Option(help="Testa também as inativas.")] = False,
    arquivo: ArquivoFontesOpt = ARQUIVO_PADRAO,
) -> None:
    """Verifica se cada feed responde e quantos itens tem."""
    try:
        fontes = carregar_fontes(arquivo)
        cfg = carregar_config()
    except (ErroFontes, ErroConfiguracao) as e:
        _sair_com_erro(str(e))
    falhas = 0
    for f in fontes:
        if not f.ativa and not todas:
            continue
        try:
            itens, erro = interpretar_feed(baixar(str(f.url), cfg.coleta).content, f)
        except Exception as e:  # noqa: BLE001
            itens, erro = [], f"{type(e).__name__}: {e}"
        if erro or not itens:
            falhas += 1
            typer.secho(f"FALHOU  {f.nome:<32} {erro or 'feed sem itens'}", fg=typer.colors.RED)
        else:
            mais_recente = max((i.data for i in itens if i.data), default=None)
            quando = mais_recente.strftime("%d/%m/%Y") if mais_recente else "sem datas"
            typer.secho(f"OK      {f.nome:<32} {len(itens):>3} itens (mais recente: {quando})", fg=typer.colors.GREEN)
    raise typer.Exit(ERRO if falhas else OK)


# --------------------------------------------------------- testar-entrega


@app.command("testar-entrega")
def testar_entrega(config: Annotated[Path, typer.Option(help="Arquivo de configuração.")] = Path("config.yaml")) -> None:
    """Envia uma mensagem de teste por cada canal ativo."""
    try:
        cfg = carregar_config(config)
    except ErroConfiguracao as e:
        _sair_com_erro(str(e))
    if not entrega.canais_ativos(cfg.entrega):
        typer.echo("Nenhum canal ativo em config.yaml (entrega.email.ativo / entrega.telegram.ativo).")
        raise typer.Exit(OK)
    resultado = entrega.testar(cfg.entrega)
    for canal, erro in resultado.items():
        if erro:
            typer.secho(f"{canal}: falhou ({erro})", fg=typer.colors.RED)
        else:
            typer.secho(f"{canal}: mensagem de teste enviada", fg=typer.colors.GREEN)
    raise typer.Exit(ENTREGA_PARCIAL if any(resultado.values()) else OK)


if __name__ == "__main__":
    app()
