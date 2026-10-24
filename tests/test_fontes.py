from pathlib import Path

import pytest

from radar_ia.fontes import (
    ErroFontes,
    adicionar_fonte,
    carregar_fontes,
    definir_ativa,
    fontes_ativas,
    remover_fonte,
)

VALIDO = """\
# comentário que deve sobreviver às edições
fontes:
  # oficiais
  - nome: Blog A
    url: https://a.exemplo.com/feed.xml
    categoria: oficial
    ativa: true
    peso: 5
  - nome: Blog B
    url: https://b.exemplo.com/rss
    ativa: false
"""


def escrever(tmp_path: Path, conteudo: str) -> Path:
    caminho = tmp_path / "fontes.yaml"
    caminho.write_text(conteudo, encoding="utf-8")
    return caminho


def test_carrega_fontes_validas_com_padroes(tmp_path):
    fontes = carregar_fontes(escrever(tmp_path, VALIDO))
    assert [f.nome for f in fontes] == ["Blog A", "Blog B"]
    assert fontes[1].peso == 3  # padrão
    assert [f.nome for f in fontes_ativas(escrever(tmp_path, VALIDO))] == ["Blog A"]


@pytest.mark.parametrize(
    ("trecho", "esperado"),
    [
        ("    peso: 9\n", "deve ser no máximo 5"),
        ("    url: nao-e-url\n", "URL inválida"),
        ("    ativa: talvez\n", "use true ou false"),
    ],
)
def test_erro_aponta_fonte_e_campo_em_portugues(tmp_path, trecho, esperado):
    conteudo = "fontes:\n  - nome: Problemática\n" + (
        trecho if "url" in trecho else "    url: https://x.com/feed\n" + trecho
    )
    with pytest.raises(ErroFontes) as exc:
        carregar_fontes(escrever(tmp_path, conteudo))
    msg = str(exc.value)
    assert "fonte #1 ('Problemática')" in msg
    assert esperado in msg


def test_nome_ausente_e_nome_repetido(tmp_path):
    with pytest.raises(ErroFontes, match="campo obrigatório ausente"):
        carregar_fontes(escrever(tmp_path, "fontes:\n  - url: https://x.com/feed\n"))
    repetido = "fontes:\n  - nome: X\n    url: https://x.com/a\n  - nome: x\n    url: https://x.com/b\n"
    with pytest.raises(ErroFontes, match="nome de fonte repetido"):
        carregar_fontes(escrever(tmp_path, repetido))


def test_yaml_malformado_e_arquivo_ausente(tmp_path):
    with pytest.raises(ErroFontes, match="YAML malformado"):
        carregar_fontes(escrever(tmp_path, "fontes: [\n  - nome: x"))
    with pytest.raises(ErroFontes, match="não encontrado"):
        carregar_fontes(tmp_path / "nao_existe.yaml")


def test_crud_preserva_comentarios(tmp_path):
    caminho = escrever(tmp_path, VALIDO)
    adicionar_fonte("Blog C", "https://c.exemplo.com/feed", "imprensa", 2, caminho)
    definir_ativa("blog b", True, caminho)  # sem diferenciar maiúsculas
    remover_fonte("Blog A", caminho)
    fontes = carregar_fontes(caminho)
    assert [(f.nome, f.ativa) for f in fontes] == [("Blog B", True), ("Blog C", True)]
    texto = caminho.read_text(encoding="utf-8")
    assert "# comentário que deve sobreviver às edições" in texto


def test_crud_rejeita_invalidos_sem_corromper(tmp_path):
    caminho = escrever(tmp_path, VALIDO)
    original = caminho.read_text(encoding="utf-8")
    with pytest.raises(ErroFontes):
        adicionar_fonte("Blog A", "https://outra.com/feed", caminho=caminho)  # nome repetido
    with pytest.raises(ErroFontes, match="não encontrada"):
        remover_fonte("Inexistente", caminho)
    assert caminho.read_text(encoding="utf-8") == original
