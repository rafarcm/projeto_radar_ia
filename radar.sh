#!/usr/bin/env bash
# Atalhos para usar o radar-ia no Linux.
#
#   ./radar.sh              abre um menu
#   ./radar.sh <comando>    roda direto (veja ./radar.sh ajuda)
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
VENV=".venv"
RADAR="$VENV/bin/radar"

verde() { printf '\033[32m%s\033[0m\n' "$*"; }
amarelo() { printf '\033[33m%s\033[0m\n' "$*"; }
vermelho() { printf '\033[31m%s\033[0m\n' "$*" >&2; }

instalar() {
  if [ ! -x "$VENV/bin/python" ]; then
    amarelo "Criando ambiente virtual em $VENV..."
    if command -v uv >/dev/null 2>&1; then uv venv -q "$VENV"; else python3 -m venv "$VENV"; fi
  fi
  amarelo "Instalando dependências..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install -q --python "$VENV/bin/python" -e ".[dev]"
  else
    "$VENV/bin/pip" install -q -e ".[dev]"
  fi
  [ -f .env ] || { cp .env.example .env; amarelo "Criei o .env a partir do .env.example: preencha as chaves."; }
  verde "Instalação concluída."
}

exigir_instalacao() {
  [ -x "$RADAR" ] || { amarelo "Ambiente ainda não instalado; instalando agora."; instalar; }
}

conferir_env() {
  # Mostra só se cada variável está preenchida, nunca o valor.
  [ -f .env ] || { vermelho "Arquivo .env não encontrado. Rode: ./radar.sh instalar"; return 1; }
  local faltando=0
  for v in "$@"; do
    if ! grep -Eq "^${v}=.+" .env; then vermelho "  $v está vazio no .env"; faltando=1; fi
  done
  return "$faltando"
}

ajuda() {
  cat <<'TXT'
Uso: ./radar.sh [comando]

  instalar        cria o ambiente virtual e instala as dependências
  fontes          testa se os feeds cadastrados respondem
  simular         mostra o que seria processado (sem LLM, custo zero)
  testar-email    envia uma mensagem de teste pelos canais ativos (e-mail/Telegram)
  teste           relatório pequeno: 3 notícias, gera o PDF e envia (custo de centavos)
  pdf             relatório completo, só o PDF (sem enviar)
  gerar           relatório completo: gera o PDF e envia
  retomar         continua a última execução interrompida
  abrir           abre o PDF mais recente
  testes          roda os testes automatizados (pytest, sem APIs reais)
  menu            abre o menu (padrão sem argumentos)

Opções extras são repassadas ao "radar gerar". Exemplos:
  ./radar.sh gerar --horas 48
  ./radar.sh pdf --max-itens 10 --verificar-traducao
TXT
}

abrir_pdf() {
  local ultimo
  ultimo="$(ls -t relatorios/radar-ia_*.pdf 2>/dev/null | head -n1 || true)"
  [ -n "$ultimo" ] || { vermelho "Nenhum PDF em relatorios/. Gere um primeiro."; return 1; }
  verde "Abrindo $ultimo"
  xdg-open "$ultimo" >/dev/null 2>&1 &
}

executar() {
  local comando="$1"; shift || true
  case "$comando" in
    instalar) instalar ;;
    fontes) exigir_instalacao; "$RADAR" fontes testar ;;
    simular) exigir_instalacao; "$RADAR" gerar --simular "$@" ;;
    testar-email|testar-entrega)
      exigir_instalacao; conferir_env SMTP_HOST SMTP_USUARIO SMTP_SENHA || true
      "$RADAR" testar-entrega ;;
    teste)
      exigir_instalacao; conferir_env ANTHROPIC_API_KEY
      amarelo "Teste com 3 notícias (as 3 entram no histórico e não se repetem no próximo relatório)."
      "$RADAR" gerar --max-itens 3 "$@" ;;
    pdf) exigir_instalacao; conferir_env ANTHROPIC_API_KEY; "$RADAR" gerar --sem-enviar "$@" ;;
    gerar) exigir_instalacao; conferir_env ANTHROPIC_API_KEY; "$RADAR" gerar "$@" ;;
    retomar) exigir_instalacao; "$RADAR" gerar --retomar ;;
    abrir) abrir_pdf ;;
    testes) exigir_instalacao; "$VENV/bin/pytest" -q ;;
    menu) menu ;;
    ajuda|-h|--help) ajuda ;;
    *) vermelho "Comando desconhecido: $comando"; ajuda; return 2 ;;
  esac
}

menu() {
  local opcoes=("simular" "testar-email" "teste" "pdf" "gerar" "retomar" "abrir" "fontes" "testes" "instalar" "sair")
  echo "radar-ia - escolha uma opção:"
  PS3="Número: "
  select escolha in "${opcoes[@]}"; do
    [ "$escolha" = "sair" ] && break
    [ -n "${escolha:-}" ] || { vermelho "Opção inválida."; continue; }
    executar "$escolha" || true
    echo
    break
  done
}

if [ $# -eq 0 ]; then menu; else executar "$@"; fi
