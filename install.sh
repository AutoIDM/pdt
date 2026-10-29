#!/bin/sh
set -eu

if command -v uv >/dev/null 2>&1; then
    uv=$(command -v uv)
else
    echo "uv is not installed; installing uv..."
    curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh
    if [ -n "${UV_INSTALL_DIR:-}" ]; then
        uv="$UV_INSTALL_DIR/uv"
    elif [ -n "${XDG_BIN_HOME:-}" ]; then
        uv="$XDG_BIN_HOME/uv"
    elif [ -n "${XDG_DATA_HOME:-}" ]; then
        uv="$XDG_DATA_HOME/../bin/uv"
    else
        uv="$HOME/.local/bin/uv"
    fi
fi

"$uv" tool install --upgrade --python 3.12 pdt-cli
"$uv" tool update-shell >/dev/null 2>&1 || true

pdt="$("$uv" tool dir --bin)/pdt"
echo
echo "Installed pdt $("$pdt" --version) at $pdt"
case ":$PATH:" in
    *":$(dirname -- "$pdt"):"*) echo "Next, run: pdt init my-jobs" ;;
    *) echo "Next, open a new terminal window and run: pdt init my-jobs" ;;
esac
