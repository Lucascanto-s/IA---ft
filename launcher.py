"""Inicia o LMR Edit numa porta livre e abre o navegador. Usado pelo iniciar.bat.

Fica preso ao próprio computador (127.0.0.1), então o Windows não pergunta nada de firewall
e ninguém de fora acessa. Se a porta preferida estiver ocupada, escolhe outra sozinho.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
PREFERRED_PORTS = (8501, 8502, 8503, 8504, 8505)


def _free_port() -> int:
    for port in PREFERRED_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:  # nada escutando nessa porta
                return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> int:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(PROJECT_DIR / "app.py"),
         "--server.headless=true", "--server.address=127.0.0.1", f"--server.port={port}",
         "--browser.gatherUsageStats=false"],
        cwd=str(PROJECT_DIR),
    )
    try:
        for _ in range(90):
            if process.poll() is not None:
                return process.returncode or 1
            try:
                with urllib.request.urlopen(f"{url}/_stcore/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(1)
        webbrowser.open(url)
        print(f"\n  LMR Edit aberto em {url}\n  Deixe esta janela aberta enquanto usa o app.\n")
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if process.poll() is None:
            process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
