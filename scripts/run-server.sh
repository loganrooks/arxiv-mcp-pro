#!/bin/sh
# Launch the arxiv MCP server from THIS checkout, injecting an optional
# Semantic Scholar API key read from the macOS keychain at runtime.
#
# The key lives ONLY in the macOS keychain (service: semantic-scholar-api-key)
# — never in this file, never in git. With no key present the server runs
# unauthenticated, exactly as before (the key is additive, per C3).
#
# Store a key (in your own terminal, so it never lands in shell history):
#   security add-generic-password -a "$USER" -s "semantic-scholar-api-key" -U -w

# Run from the repo root (this script lives in <repo>/scripts/).
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1

# Optional API key from the keychain. 2>/dev/null + '|| true' => missing key is non-fatal.
KEY="$(security find-generic-password -s 'semantic-scholar-api-key' -w 2>/dev/null || true)"
if [ -n "$KEY" ]; then
  export SEMANTIC_SCHOLAR_API_KEY="$KEY"
  # The keyed S2 tier allows 1 request/second cumulative across endpoints.
  # Engage proactive pacing (B11) so bursts don't earn 429s. An explicit
  # value from the environment always wins.
  export SEMANTIC_SCHOLAR_MIN_REQUEST_INTERVAL="${SEMANTIC_SCHOLAR_MIN_REQUEST_INTERVAL:-1.1}"
fi
unset KEY

# Resolve uv (Claude Desktop launches MCP servers with a restricted PATH).
if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
elif [ -x "/opt/homebrew/bin/uv" ]; then
  UV="/opt/homebrew/bin/uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
else
  UV="uv"
fi

exec "$UV" run arxiv-mcp-pro "$@"
