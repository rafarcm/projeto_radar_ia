#!/usr/bin/env bash
# Guarda o histórico SQLite (dados/radar.sqlite3) num branch próprio do repositório,
# para que ele sobreviva entre execuções do GitHub Actions sem serviço pago.
#
#   scripts/historico_git.sh baixar   # antes de "radar gerar"
#   scripts/historico_git.sh enviar   # depois (rode mesmo se o radar falhar)
#
# O branch tem sempre um único commit (recriado e enviado com --force), então o
# repositório não cresce a cada dia com cópias antigas do banco.
set -euo pipefail

BRANCH="${RADAR_BRANCH_DADOS:-radar-dados}"
ARQUIVO="${RADAR_BANCO:-dados/radar.sqlite3}"
REMOTO="${RADAR_REMOTO:-origin}"

baixar() {
  if git ls-remote --exit-code --heads "$REMOTO" "$BRANCH" >/dev/null 2>&1; then
    git fetch --quiet --depth 1 "$REMOTO" "$BRANCH"
    mkdir -p "$(dirname "$ARQUIVO")"
    git show "FETCH_HEAD:radar.sqlite3" > "$ARQUIVO"
    echo "Histórico restaurado do branch '$BRANCH'."
  else
    echo "Branch '$BRANCH' ainda não existe: começando com histórico vazio."
  fi
}

enviar() {
  if [ ! -f "$ARQUIVO" ]; then
    echo "Nada a salvar: $ARQUIVO não existe."
    return 0
  fi
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  git -C "$tmp" init --quiet
  git -C "$tmp" checkout --quiet -b "$BRANCH"
  cp "$ARQUIVO" "$tmp/radar.sqlite3"
  cat > "$tmp/README.md" <<'TXT'
Branch gerado automaticamente pelo radar-ia: guarda o histórico de notícias já
reportadas e o cache de traduções (radar.sqlite3). Não edite à mão.
TXT
  git -C "$tmp" add radar.sqlite3 README.md
  git -C "$tmp" -c user.name="radar-ia" -c user.email="radar-ia@users.noreply.github.com" \
    commit --quiet -m "Histórico do radar-ia ($(date -u +%Y-%m-%d))"
  # Traz o commit para este repositório e envia daqui, usando a autenticação já
  # configurada (no Actions, a do actions/checkout).
  git fetch --quiet "$tmp" "$BRANCH"
  git push --quiet --force "$REMOTO" "FETCH_HEAD:refs/heads/$BRANCH"
  echo "Histórico salvo no branch '$BRANCH'."
}

case "${1:-}" in
  baixar) baixar ;;
  enviar) enviar ;;
  *) echo "uso: $0 baixar|enviar" >&2; exit 2 ;;
esac
