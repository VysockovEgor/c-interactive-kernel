#!/usr/bin/env bash
# Installs the "C (интерактивный)" Jupyter kernel for the current user.
# Linux or WSL on Windows. Run again after git pull to update.
# ./install.sh --uninstall removes it.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV="${XDG_DATA_HOME:-$HOME/.local/share}/c-interactive-kernel"

if [ "$1" = "--uninstall" ]; then
    if [ -x "$ENV/bin/jupyter-kernelspec" ]; then
        "$ENV/bin/jupyter-kernelspec" remove -f c-interactive || true
    fi
    rm -rf "$ENV"
    echo "ядро удалено"
    exit 0
fi

if [ "$(uname)" != "Linux" ]; then
    echo "нужен Linux; на Windows запустите этот скрипт внутри WSL (см. README.md)"
    exit 1
fi

missing=0
for tool in gcc gdb python3; do
    command -v "$tool" > /dev/null || missing=1
done
python3 -c 'import venv, ensurepip' 2> /dev/null || missing=1

if [ "$missing" = 1 ]; then
    echo "ставлю gcc, gdb и python (попросит пароль)..."
    if command -v apt-get > /dev/null; then
        sudo apt-get update -qq
        sudo apt-get install -y gcc gdb python3 python3-venv
    elif command -v dnf > /dev/null; then
        sudo dnf install -y gcc gdb python3
    elif command -v pacman > /dev/null; then
        sudo pacman -S --needed --noconfirm gcc gdb python
    elif command -v zypper > /dev/null; then
        sudo zypper install -y gcc gdb python3
    else
        echo "установите вручную gcc, gdb и python3 (с модулем venv), потом запустите скрипт снова"
        exit 1
    fi
fi

echo "ставлю ядро в $ENV ..."
python3 -m venv "$ENV"
"$ENV/bin/python" -m pip install --quiet --upgrade pip
"$ENV/bin/python" -m pip install --quiet --upgrade "$HERE"
"$ENV/bin/python" -m pip install --quiet --force-reinstall --no-deps "$HERE"  # the same version number: take the new code anyway
"$ENV/bin/python" -m c_interactive_kernel.install
echo "готово: в VS Code выберите ядро Select Kernel -> Jupyter Kernel... -> C (интерактивный)"
