#!/usr/bin/env bash
# One-time Nota setup for macOS and Linux.
set -euo pipefail

cd "$(cd -- "$(dirname -- "$0")" && pwd)"

python_ok() {
  "$1" -c 'import sys; raise SystemExit(not (sys.version_info.major == 3 and sys.version_info.minor >= 11))' >/dev/null 2>&1
}

find_python() {
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$(command -v "$candidate")"; then
      printf '%s\n' "$(command -v "$candidate")"
      return 0
    fi
  done
  return 1
}

install_python() {
  case "$(uname -s)" in
    Darwin)
      if ! command -v brew >/dev/null 2>&1; then
        echo "Python 3.11+ is required. Install Homebrew from https://brew.sh, then run this script again." >&2
        return 1
      fi
      brew install python@3.12
      ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y python3 python3-venv python3-pip
      elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 python3-pip
      elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -Sy --needed python python-pip
      else
        echo "Python 3.11+ is required. Install it with your Linux distribution's package manager, then run this script again." >&2
        return 1
      fi
      ;;
    *)
      echo "Unsupported operating system. Install Python 3.11+ and run this script again." >&2
      return 1
      ;;
  esac
}

venv_python=".venv/bin/python"
if [[ -x "$venv_python" ]] && ! "$venv_python" -V >/dev/null 2>&1; then
  echo "Existing virtual environment is invalid. Rebuilding it..."
  rm -rf .venv
fi

if [[ ! -x "$venv_python" ]]; then
  if ! python_exe="$(find_python)"; then
    echo "Python 3.11+ was not found. Installing a supported version..."
    install_python
    python_exe="$(find_python)" || {
      echo "Python installation finished but Python 3.11+ could not be found. Open a new terminal and run setup.sh again." >&2
      exit 1
    }
  fi
  echo "[1/3] Creating Python virtual environment..."
  "$python_exe" -m venv .venv
fi

echo "[2/3] Updating pip and installing Nota dependencies..."
"$venv_python" -m pip install --disable-pip-version-check --upgrade pip
"$venv_python" -m pip install --disable-pip-version-check -r requirements.txt

if [[ ! -f object/.env ]]; then
  cp object/.env.example object/.env
  echo "[3/3] Created object/.env. Configure models in Nota before generating notes."
else
  echo "[3/3] Existing object/.env was kept."
fi

echo
echo "Setup complete. Run ./Nota.sh to launch Nota."
