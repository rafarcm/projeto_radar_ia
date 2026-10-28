"""Leitura do config.yaml e das variáveis de ambiente (.env)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

MODELO_PADRAO = "anthropic:claude-opus-5-5"


class ErroConfiguracao(Exception):
    """Configuração inválida ou ausente."""


class ConfigColeta(BaseModel):
    horas: int = Field(default=24, gt=0)
    max_itens: int = Field(default=40, gt=0)
    max_itens_por_fonte: int = Field(default=15, gt=0)
    timeout_segundos: float = Field(default=20, gt=0)
    tentativas: int = Field(default=3, ge=1)
    user_agent: str = "radar-ia/0.1"
    max_caracteres_artigo: int = Field(default=20000, gt=0)


class ConfigDeduplicacao(BaseModel):
    limiar_titulo: int = Field(default=90, ge=0, le=100)
    semantica: bool = True
    limiar_semantico: float = Field(default=0.70, gt=0, le=1)
    modelo_embeddings: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    pasta_modelos: Path = Path("~/.cache/radar-ia/modelos")

    @field_validator("pasta_modelos")
    @classmethod
    def _expandir_usuario(cls, v: Path) -> Path:
        return v.expanduser()


class ConfigProcessamento(BaseModel):
    concorrencia: int = Field(default=4, ge=1)
    tentativas: int = Field(default=3, ge=1)
    max_tentativas_traducao: int = Field(default=2, ge=0)
    num_destaques: int = Field(default=5, ge=3, le=5)
    # Se o feed não traz detalhes suficientes, baixa o artigo e refaz o resumo.
    buscar_artigo_se_insuficiente: bool = True


class ConfigSaida(BaseModel):
    pasta_relatorios: Path = Path("relatorios")
    banco_sqlite: Path = Path("dados/radar.sqlite3")
    banco_checkpoints: Path = Path("dados/checkpoints.sqlite3")


class ConfigEmail(BaseModel):
    ativo: bool = False
    destinatarios: list[str] = []
    assunto: str = "Radar IA - {data}"


class ConfigTelegram(BaseModel):
    ativo: bool = False


class ConfigEntrega(BaseModel):
    email: ConfigEmail = ConfigEmail()
    telegram: ConfigTelegram = ConfigTelegram()


class Config(BaseModel):
    coleta: ConfigColeta = ConfigColeta()
    deduplicacao: ConfigDeduplicacao = ConfigDeduplicacao()
    processamento: ConfigProcessamento = ConfigProcessamento()
    saida: ConfigSaida = ConfigSaida()
    entrega: ConfigEntrega = ConfigEntrega()


def carregar_config(caminho: Path = Path("config.yaml")) -> Config:
    """Carrega o .env e o config.yaml; usa padrões se o arquivo não existir.

    Os destinatários do e-mail vêm de EMAIL_DESTINATARIOS (separados por vírgula,
    ponto e vírgula ou espaço),
    para que endereços pessoais não fiquem no config.yaml, que vai para o Git."""
    load_dotenv()
    cfg = _ler_yaml(caminho)
    if destinatarios := os.getenv("EMAIL_DESTINATARIOS"):
        cfg.entrega.email.destinatarios = [d for d in re.split(r"[,;\s]+", destinatarios) if d]
    return cfg


def _ler_yaml(caminho: Path) -> Config:
    if not caminho.exists():
        return Config()
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
        return Config.model_validate(dados)
    except yaml.YAMLError as e:
        raise ErroConfiguracao(f"{caminho}: YAML inválido: {e}") from e
    except ValidationError as e:
        linhas = [
            f"  - {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        ]
        raise ErroConfiguracao(f"{caminho} inválido:\n" + "\n".join(linhas)) from e


def modelo_configurado() -> str:
    """Identificador do modelo LangChain ("provedor:modelo")."""
    return os.getenv("RADAR_MODELO") or MODELO_PADRAO
