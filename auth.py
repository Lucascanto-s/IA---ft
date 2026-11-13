"""Local accounts, permissions, sessions and encrypted settings (SQLite in dados/).

Nothing here depends on Streamlit, so it can be tested and reused by the future hosted version.
- Passwords: scrypt with a random salt per user (never stored or logged in plain text).
- Settings (BaseLinker token etc.): encrypted with Fernet; the key lives in dados/chave.key.
- Sessions: random token kept in a browser cookie; only its SHA-256 is stored here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet, InvalidToken

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("LMR_DATA_DIR", PROJECT_DIR / "dados"))

SESSION_HOURS = 12
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 5
MIN_PASSWORD = 8

ROLES = {"master": "Administrador máximo", "admin": "Administrador", "operador": "Operador"}
ADMIN_ROLES = {"master", "admin"}
# Permission id -> label shown in the "Usuários" page. Administrators always have all of them.
PERMISSIONS = {
    "processar": "Processar SKUs (gerar e ajustar imagens)",
    "publicar": "Publicar no BaseLinker",
    "catalogo": "Catálogo de veículos",
    "configuracoes": "Configurações (token do BaseLinker e opções)",
    "usuarios": "Criar logins e dar permissões",
}
DEFAULT_OPERATOR_PERMISSIONS = ["processar", "catalogo"]

# Settings editable in the app. They override .env and are encrypted at rest.
SETTINGS = {
    "BASELINKER_TOKEN": "Token da API do BaseLinker",
    "BASELINKER_INVENTORY_ID": "ID do catálogo (inventory) do BaseLinker",
    "BASELINKER_API_URL": "URL da API do BaseLinker",
    "PUBLIC_IMAGE_BASE_URL": "URL pública das imagens (opcional)",
    "BASELINKER_STAR_TODO": "Estrela da fila (SKUs que precisam de imagem)",
    "BASELINKER_STAR_DONE": "Estrela aplicada ao publicar",
}
SECRET_SETTINGS = {"BASELINKER_TOKEN"}


class AuthError(ValueError):
    """Message safe to show to the user."""


# --------------------------------------------------------------------------- storage

def _db_path() -> Path:
    return DATA_DIR / "lmr.db"


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(_db_path())
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db() -> None:
    with _db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                nome TEXT NOT NULL,
                senha_hash TEXT NOT NULL,
                papel TEXT NOT NULL DEFAULT 'operador',
                permissoes TEXT NOT NULL DEFAULT '[]',
                ativo INTEGER NOT NULL DEFAULT 1,
                trocar_senha INTEGER NOT NULL DEFAULT 0,
                tentativas INTEGER NOT NULL DEFAULT 0,
                bloqueado_ate TEXT,
                criado_em TEXT NOT NULL,
                ultimo_acesso TEXT
            );
            CREATE TABLE IF NOT EXISTS sessoes (
                token_hash TEXT PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                expira TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS configuracoes (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            );
            """
        )
        # Older databases had no "master": the first administrator becomes the top administrator.
        if db.execute("SELECT 1 FROM usuarios WHERE papel = 'master'").fetchone() is None:
            db.execute("UPDATE usuarios SET papel = 'master' WHERE id = "
                       "(SELECT MIN(id) FROM usuarios WHERE papel = 'admin')")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


# --------------------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"A senha precisa ter pelo menos {MIN_PASSWORD} caracteres.")


# --------------------------------------------------------------------------- users

def _user(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    user = dict(row)
    user.pop("senha_hash", None)
    user["permissoes"] = json.loads(user["permissoes"] or "[]")
    user["ativo"] = bool(user["ativo"])
    user["trocar_senha"] = bool(user["trocar_senha"])
    return user


def has_users() -> bool:
    init_db()
    with _db() as db:
        return db.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is not None


def can(user: dict | None, permission: str) -> bool:
    if not user or not user.get("ativo"):
        return False
    return user["papel"] in ADMIN_ROLES or permission in user["permissoes"]


def assignable_roles(actor: dict | None) -> list[str]:
    """Roles the actor may give: only the top administrator creates administrators."""
    if actor and actor.get("papel") == "master":
        return list(ROLES)
    return ["operador"]


def can_manage(actor: dict | None, target: dict | None) -> bool:
    """Top administrator manages everyone else; admins and 'usuarios' operators manage operators only."""
    if not actor or not target or actor["id"] == target["id"] or not can(actor, "usuarios"):
        return False
    return actor["papel"] == "master" or target["papel"] == "operador"


def _require_manage(actor: dict | None, user_id: int) -> None:
    if actor is not None and not can_manage(actor, get_user(user_id)):
        raise AuthError("Você não tem permissão para alterar este login.")


def list_users() -> list[dict]:
    init_db()
    with _db() as db:
        rows = db.execute("SELECT * FROM usuarios ORDER BY papel, nome").fetchall()
    return [_user(row) for row in rows]


def get_user(user_id: int) -> dict | None:
    with _db() as db:
        return _user(db.execute("SELECT * FROM usuarios WHERE id = ?", (user_id,)).fetchone())


def create_user(email: str, nome: str, password: str, papel: str = "operador",
                permissoes: list[str] | None = None, trocar_senha: bool = False,
                actor: dict | None = None) -> dict:
    init_db()
    if actor is not None and papel not in assignable_roles(actor):
        raise AuthError("Só o administrador máximo cria administradores.")
    email = email.strip().lower()
    nome = nome.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise AuthError("Informe um e-mail válido.")
    if not nome:
        raise AuthError("Informe o nome.")
    if papel not in ROLES:
        raise AuthError("Perfil inválido.")
    _check_password_rules(password)
    allowed = [item for item in (permissoes or []) if item in PERMISSIONS]
    try:
        with _db() as db:
            cursor = db.execute(
                "INSERT INTO usuarios (email, nome, senha_hash, papel, permissoes, trocar_senha, criado_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, nome, hash_password(password), papel, json.dumps(allowed), int(trocar_senha),
                 _now().isoformat()),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError as error:
        raise AuthError("Já existe um login com esse e-mail.") from error
    return get_user(int(user_id))  # type: ignore[return-value]


def register_self(email: str, nome: str, password: str) -> dict:
    """'Criar conta' on the login screen: an operator with no permissions until an admin releases them."""
    return create_user(email, nome, password, papel="operador", permissoes=[])


# Top administrators shipped inside the install zip (email, name and scrypt hash only, never the password).
SEED_FILE = PROJECT_DIR / "dados_iniciais" / "admins.json"


def export_admins() -> list[dict]:
    init_db()
    with _db() as db:
        rows = db.execute("SELECT email, nome, senha_hash FROM usuarios WHERE papel = 'master' AND ativo = 1")
        return [dict(row) for row in rows.fetchall()]


def seed_admins_from_package() -> None:
    """On a new computer, create the admins that came in the zip (only while there are no users)."""
    if has_users() or not SEED_FILE.exists():
        return
    admins = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    with _db() as db:
        for admin in admins:
            if not str(admin.get("senha_hash", "")).startswith("scrypt$"):
                continue
            db.execute(
                "INSERT OR IGNORE INTO usuarios (email, nome, senha_hash, papel, permissoes, criado_em) "
                "VALUES (?, ?, ?, 'master', '[]', ?)",
                (admin["email"].strip().lower(), admin["nome"], admin["senha_hash"], _now().isoformat()),
            )


def _active_masters(db: sqlite3.Connection, excluding: int | None = None) -> int:
    row = db.execute(
        "SELECT COUNT(*) FROM usuarios WHERE papel = 'master' AND ativo = 1 AND id != ?",
        (excluding if excluding is not None else -1,),
    ).fetchone()
    return int(row[0])


def update_user(user_id: int, *, nome: str | None = None, papel: str | None = None,
                permissoes: list[str] | None = None, ativo: bool | None = None,
                actor: dict | None = None) -> dict:
    _require_manage(actor, user_id)
    if actor is not None and papel is not None and papel not in assignable_roles(actor):
        raise AuthError("Só o administrador máximo define administradores.")
    with _db() as db:
        current = db.execute("SELECT * FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        if current is None:
            raise AuthError("Login não encontrado.")
        losing_master = current["papel"] == "master" and (
            (papel is not None and papel != "master") or ativo is False
        )
        if losing_master and _active_masters(db, excluding=user_id) == 0:
            raise AuthError("É preciso manter pelo menos um administrador máximo ativo.")
        if papel is not None and papel not in ROLES:
            raise AuthError("Perfil inválido.")
        fields: dict[str, object] = {}
        if nome is not None:
            if not nome.strip():
                raise AuthError("Informe o nome.")
            fields["nome"] = nome.strip()
        if papel is not None:
            fields["papel"] = papel
        if permissoes is not None:
            fields["permissoes"] = json.dumps([item for item in permissoes if item in PERMISSIONS])
        if ativo is not None:
            fields["ativo"] = int(ativo)
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            db.execute(f"UPDATE usuarios SET {assignments} WHERE id = ?", (*fields.values(), user_id))
        if ativo is False:
            db.execute("DELETE FROM sessoes WHERE usuario_id = ?", (user_id,))
    return get_user(user_id)  # type: ignore[return-value]


def set_password(user_id: int, password: str, must_change: bool = False,
                 actor: dict | None = None) -> None:
    _require_manage(actor, user_id)
    _check_password_rules(password)
    with _db() as db:
        db.execute(
            "UPDATE usuarios SET senha_hash = ?, trocar_senha = ?, tentativas = 0, bloqueado_ate = NULL "
            "WHERE id = ?",
            (hash_password(password), int(must_change), user_id),
        )
        db.execute("DELETE FROM sessoes WHERE usuario_id = ?", (user_id,))


def change_own_password(user_id: int, current: str, new: str) -> None:
    with _db() as db:
        row = db.execute("SELECT senha_hash FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    if row is None or not verify_password(current, row["senha_hash"]):
        raise AuthError("Senha atual incorreta.")
    set_password(user_id, new)


def delete_user(user_id: int, actor: dict | None = None) -> None:
    _require_manage(actor, user_id)
    with _db() as db:
        current = db.execute("SELECT papel FROM usuarios WHERE id = ?", (user_id,)).fetchone()
        if current is None:
            return
        if current["papel"] == "master" and _active_masters(db, excluding=user_id) == 0:
            raise AuthError("É preciso manter pelo menos um administrador máximo ativo.")
        db.execute("DELETE FROM sessoes WHERE usuario_id = ?", (user_id,))
        db.execute("DELETE FROM usuarios WHERE id = ?", (user_id,))


def authenticate(email: str, password: str) -> dict:
    """Check email + password. Locks the login for a few minutes after repeated failures."""
    init_db()
    # Errors are raised only after the `with` block: raising inside it would roll back the attempt counter.
    error: AuthError | None = None
    with _db() as db:
        row = db.execute("SELECT * FROM usuarios WHERE email = ?", (email.strip().lower(),)).fetchone()
        locked_until = row["bloqueado_ate"] if row else None
        if row is None:
            hash_password(password)  # same timing as a real check
            error = AuthError("E-mail ou senha incorretos.")
        elif locked_until and datetime.fromisoformat(locked_until) > _now():
            error = AuthError("Muitas tentativas erradas. Tente de novo em alguns minutos.")
        elif not verify_password(password, row["senha_hash"]):
            attempts = row["tentativas"] + 1
            lock = (_now() + timedelta(minutes=LOCK_MINUTES)).isoformat() if attempts >= MAX_FAILED_LOGINS else None
            db.execute("UPDATE usuarios SET tentativas = ?, bloqueado_ate = ? WHERE id = ?",
                       (0 if lock else attempts, lock, row["id"]))
            error = AuthError("E-mail ou senha incorretos.")
        elif not row["ativo"]:
            error = AuthError("Este login está desativado. Fale com o administrador.")
        else:
            db.execute("UPDATE usuarios SET tentativas = 0, bloqueado_ate = NULL, ultimo_acesso = ? WHERE id = ?",
                       (_now().isoformat(), row["id"]))
    if error is not None:
        raise error
    return get_user(row["id"])  # type: ignore[return-value]


# --------------------------------------------------------------------------- sessions

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with _db() as db:
        db.execute("DELETE FROM sessoes WHERE expira < ?", (_now().isoformat(),))
        db.execute("INSERT INTO sessoes (token_hash, usuario_id, expira) VALUES (?, ?, ?)",
                   (_token_hash(token), user_id, (_now() + timedelta(hours=SESSION_HOURS)).isoformat()))
    return token


def user_from_session(token: str | None) -> dict | None:
    if not token:
        return None
    init_db()
    with _db() as db:
        row = db.execute(
            "SELECT usuarios.* FROM sessoes JOIN usuarios ON usuarios.id = sessoes.usuario_id "
            "WHERE sessoes.token_hash = ? AND sessoes.expira > ? AND usuarios.ativo = 1",
            (_token_hash(token), _now().isoformat()),
        ).fetchone()
    return _user(row)


def end_session(token: str | None) -> None:
    if token:
        with _db() as db:
            db.execute("DELETE FROM sessoes WHERE token_hash = ?", (_token_hash(token),))


# --------------------------------------------------------------------------- encrypted settings

def _fernet() -> Fernet:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file = DATA_DIR / "chave.key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
    return Fernet(key_file.read_bytes().strip())


def load_settings() -> dict[str, str]:
    init_db()
    fernet = _fernet()
    values: dict[str, str] = {}
    with _db() as db:
        rows = db.execute("SELECT chave, valor FROM configuracoes").fetchall()
    for row in rows:
        try:
            values[row["chave"]] = fernet.decrypt(row["valor"].encode()).decode()
        except InvalidToken:
            continue  # key file replaced: the value has to be typed again
    return values


def save_settings(values: dict[str, str]) -> None:
    """Save the given settings (only known keys). An empty value removes the setting."""
    init_db()
    fernet = _fernet()
    with _db() as db:
        for key, value in values.items():
            if key not in SETTINGS:
                continue
            value = value.strip()
            if value:
                db.execute(
                    "INSERT INTO configuracoes (chave, valor) VALUES (?, ?) "
                    "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
                    (key, fernet.encrypt(value.encode()).decode()),
                )
            else:
                db.execute("DELETE FROM configuracoes WHERE chave = ?", (key,))


def apply_settings_to_env() -> None:
    """Saved settings win over .env, so the rest of the app keeps reading os.environ."""
    for key, value in load_settings().items():
        os.environ[key] = value


def import_env_settings() -> None:
    """First run: copy the settings that already exist in .env into the encrypted store."""
    if load_settings():
        return
    save_settings({key: os.getenv(key, "") for key in SETTINGS})
