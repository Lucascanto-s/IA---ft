"""Build the install zip for another computer: python tools/empacotar.py

The zip carries the code, the vehicle catalog and the administrators of THIS computer
(email, name and scrypt hash only). It never carries .env, dados/ (token, key, users of this
computer), downloads, outputs or backups. On the new computer: unzip, run instalar.bat,
the person creates her account and an admin signs in there to release permissions and
type that computer's BaseLinker token.
"""
from __future__ import annotations

import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import auth  # noqa: E402
from PIL import Image  # noqa: E402

INCLUDE = [
    "*.py", "requirements.txt", "README.md", ".streamlit/config.toml",
    "ui/**/*", "components/**/*", "static/**/*", "arquivos/mapa/**/*", "arquivos/veiculos/**/*",
]
EMPTY_DIRS = ["arquivos/saida", "arquivos/downloads", "arquivos/backup"]
SKIP_PARTS = {"__pycache__", ".git", ".venv", "dados", "dados_iniciais", "tests", ".pytest_cache", "dist"}


def _files() -> list[Path]:
    found: set[Path] = set()
    for pattern in INCLUDE:
        for path in PROJECT_DIR.glob(pattern):
            relative = path.relative_to(PROJECT_DIR)
            if path.is_file() and not SKIP_PARTS & set(relative.parts) and path.suffix not in {".pyc", ".env"}:
                found.add(relative)
    return sorted(found)


def main() -> int:
    admins = auth.export_admins()
    if not admins:
        print("Nenhum administrador neste computador. Abra o app, crie seu login no 'Primeiro acesso' "
              "e rode este script de novo.")
        return 1
    icon = PROJECT_DIR / "static" / "branding" / "lmr.ico"
    if not icon.exists():
        Image.open(PROJECT_DIR / "static" / "branding" / "lmr_icon.png").save(icon, sizes=[(256, 256), (48, 48),
                                                                                         (32, 32), (16, 16)])
    out_dir = PROJECT_DIR / "dist"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"LMR-Edit-{datetime.now():%Y%m%d-%H%M}.zip"
    files = _files()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        root = "LMR-Edit/"
        for relative in files:
            bundle.write(PROJECT_DIR / relative, root + relative.as_posix())
        for folder in EMPTY_DIRS:
            bundle.writestr(f"{root}{folder}/.gitkeep", "")
        for name in ("instalar.bat", "iniciar.bat"):
            bundle.write(PROJECT_DIR / "instalador" / name, root + name)
        bundle.writestr(root + "dados_iniciais/admins.json", json.dumps(admins, ensure_ascii=False, indent=2))
        bundle.writestr(root + "LEIA-ME.txt", (
            "LMR Edit - instalacao\r\n\r\n"
            "1. Extraia esta pasta em um lugar fixo (ex.: Documentos).\r\n"
            "2. Abra instalar.bat (instala o Python se precisar e cria o atalho 'LMR Edit').\r\n"
            "3. Abra o atalho LMR Edit, clique em 'Criar conta' e crie seu login.\r\n"
            "4. O administrador entra neste computador, libera suas permissoes e configura o token.\r\n"
        ))
    size = target.stat().st_size / 1024 / 1024
    print(f"Zip criado: {target} ({size:.0f} MB, {len(files)} arquivos, {len(admins)} admin(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
