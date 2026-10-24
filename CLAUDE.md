# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Visão geral

`radar-ia` coleta notícias de IA **apenas** das fontes de `fontes.yaml` (RSS/Atom), deduplica, traduz e resume em pt-BR com LangChain (saída estruturada), gera um PDF (reportlab) e entrega por e-mail/Telegram. O código, os identificadores, os logs e as mensagens ao usuário são em **português**: mantenha esse padrão.

## Comandos

O ambiente é um `.venv` gerenciado com `uv` (Python 3.14 local; o projeto exige >=3.11 e o CI usa 3.12).

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"   # ou: ./radar.sh instalar
.venv/bin/pytest -q                                   # todos os testes (sem APIs reais)
.venv/bin/pytest tests/test_fluxo.py::test_retoma_execucao_interrompida_sem_refazer_traducoes
.venv/bin/radar gerar --simular                       # coleta + seleção, sem LLM (custo zero)
.venv/bin/radar -v gerar --sem-enviar --max-itens 3   # execução real e barata, só o PDF
.venv/bin/radar fontes testar                         # confere os feeds
```

Não há linter configurado. Comandos que chamam o LLM (`radar gerar` sem `--simular`, `./radar.sh teste|pdf|gerar`) **custam dinheiro**, e `gerar` sem `--sem-enviar` envia e-mail de verdade (o `config.yaml` tem o e-mail ativo). Prefira `--simular` e os testes.

Códigos de saída do CLI: `0` sucesso, `1` erro, `3` PDF gerado mas algum canal de entrega falhou.

## Arquitetura

O pipeline é orquestrado com **dois grafos LangGraph** em [radar_ia/fluxo.py](radar_ia/fluxo.py):

- **Grafo do radar**: `coletar → selecionar → [texto_completo] → traduzir_item × N (fan-out via Send) → montar_relatorio → checagem_final → gerar_pdf → registrar_historico → [entregar]`. Os resultados paralelos se juntam em `EstadoRadar.processados` (reducer `operator.add`). `--simular` termina depois de `selecionar`.
- **Grafo do item** (compilado com `checkpointer=False`): `preparar (idioma + cache) → gerar (LLM) → verificar → gerar | concluir`. É o laço de revisão: se a verificação reprovar, o motivo volta ao prompt, até `max_tentativas_traducao`. `processar_item_seguro` converte exceções em item "Tradução indisponível" em vez de derrubar o relatório.

Pontos que exigem ler vários arquivos:

- **Checkpoint/retomada**: o CLI ([radar_ia/cli.py](radar_ia/cli.py)) roda o grafo com `AsyncSqliteSaver` em `dados/checkpoints.sqlite3` e um `thread_id` fixo. `--retomar` continua essa thread; um `gerar` novo a descarta. As `Opcoes` ficam no estado para que a retomada use as mesmas. Recursos não serializáveis (config, banco, modelo, embedder) ficam em `ContextoRadar`/`Contexto`, passados via `Runtime.context`, e são recriados ao retomar. **Todo tipo novo guardado no estado precisa entrar em `SERIALIZADOR.allowed_msgpack_modules`**, senão não é lido de volta do checkpoint.
- **LLM**: `processamento.criar_modelo` usa `init_chat_model(RADAR_MODELO)` (formato `provedor:modelo`, padrão `anthropic:claude-opus-5-5`); toda chamada passa por `estruturado()` → `with_structured_output(schema, method="json_schema").with_retry(...)`. Os schemas (`ResultadoItem`, `Verificacao`, `TextoVisaoGeral`) ficam em [radar_ia/processamento.py](radar_ia/processamento.py); os modelos de dados do pipeline, em [radar_ia/modelos.py](radar_ia/modelos.py) (incluindo a lista fixa de `Tema`).
- **Garantia de pt-BR**: [radar_ia/idioma.py](radar_ia/idioma.py) detecta o idioma (langdetect → idioma do feed → LLM) e confere números, URLs e glossário. `checagem_final` (`aplicar_fallback_idioma`) bloqueia o PDF com `ErroIdiomaRelatorio` se sobrar texto fora do português. O link de cada notícia vem sempre do feed, nunca do LLM.
- **Cache e histórico** ([radar_ia/armazenamento.py](radar_ia/armazenamento.py), `dados/radar.sqlite3`): a chave do cache é URL normalizada + hash do conteúdo + `versao` do glossário + modelo. Ao mudar `glossario.yaml`, aumente `versao`.
- **Deduplicação** ([radar_ia/dedup.py](radar_ia/dedup.py)): URL normalizada (sem `utm_*`), título (rapidfuzz) e semântica (fastembed local, só entre fontes diferentes; se o modelo não baixar, a etapa é pulada com aviso). Entre duplicados, fica a fonte de maior `peso`; `limitar_itens` alterna entre as fontes por peso.
- **Edição de `fontes.yaml`** pelo CLI usa ruamel.yaml para preservar comentários.
- **Segredos**: [radar_ia/registro.py](radar_ia/registro.py) mascara nos logs os valores das variáveis em `VARIAVEIS_SECRETAS`. Se surgir uma variável secreta nova, adicione-a lá.
- **CI** ([.github/workflows/radar.yml](.github/workflows/radar.yml)): roda diariamente às 11:00 UTC; o SQLite persiste entre execuções no branch `radar-dados` via [scripts/historico_git.sh](scripts/historico_git.sh).

## Testes

Os testes nunca chamam APIs reais. [tests/conftest.py](tests/conftest.py) define `ModeloFalso`, que imita `with_structured_output`: `respostas` mapeia o **nome do schema** para uma fila de respostas (dict, Exception ou função `mensagens -> dict`; a última se repete), e `chamadas_de(schema)` permite verificar os prompts enviados. Os testes do fluxo trocam `fluxo.coletar`, `fluxo.gerar_pdf` etc. via `monkeypatch`. Também há fábricas prontas (`item_processado`, `relatorio_exemplo`, `resultado_pt`).
