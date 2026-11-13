import json

import pytest

import auth


@pytest.fixture(autouse=True)
def temp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path / "dados")
    monkeypatch.setattr(auth, "SEED_FILE", tmp_path / "admins.json")


def test_password_is_hashed_and_checked():
    user = auth.create_user("Ana@Teste.com", "Ana", "senha-boa-1", papel="admin")
    assert user["email"] == "ana@teste.com"
    assert auth.authenticate("ana@teste.com", "senha-boa-1")["id"] == user["id"]
    with pytest.raises(auth.AuthError):
        auth.authenticate("ana@teste.com", "errada123")


def test_login_locks_after_repeated_failures():
    auth.create_user("b@teste.com", "B", "senha-boa-1")
    for _ in range(auth.MAX_FAILED_LOGINS):
        with pytest.raises(auth.AuthError):
            auth.authenticate("b@teste.com", "errada123")
    with pytest.raises(auth.AuthError, match="Muitas tentativas"):
        auth.authenticate("b@teste.com", "senha-boa-1")


def test_self_signup_has_no_permissions_until_admin_releases():
    user = auth.register_self("c@teste.com", "C", "senha-boa-1")
    assert not auth.can(user, "processar")
    user = auth.update_user(user["id"], permissoes=["processar"])
    assert auth.can(user, "processar") and not auth.can(user, "publicar")


def test_last_admin_cannot_be_removed():
    admin = auth.create_user("d@teste.com", "D", "senha-boa-1", papel="master")
    with pytest.raises(auth.AuthError):
        auth.update_user(admin["id"], ativo=False)
    with pytest.raises(auth.AuthError):
        auth.delete_user(admin["id"])


def test_sessions_and_disabled_user():
    user = auth.create_user("e@teste.com", "E", "senha-boa-1")
    token = auth.create_session(user["id"])
    assert auth.user_from_session(token)["id"] == user["id"]
    auth.create_user("adm@teste.com", "Adm", "senha-boa-1", papel="master")
    auth.update_user(user["id"], ativo=False)
    assert auth.user_from_session(token) is None


def test_settings_are_encrypted_at_rest():
    auth.save_settings({"BASELINKER_TOKEN": "segredo-123"})
    raw = (auth.DATA_DIR / "lmr.db").read_bytes()
    assert b"segredo-123" not in raw
    assert auth.load_settings()["BASELINKER_TOKEN"] == "segredo-123"


def test_admins_seeded_from_install_package():
    auth.SEED_FILE.write_text(json.dumps([{"email": "f@teste.com", "nome": "F",
                                           "senha_hash": auth.hash_password("senha-boa-1")}]))
    auth.seed_admins_from_package()
    assert auth.authenticate("f@teste.com", "senha-boa-1")["papel"] == "master"


def test_only_master_manages_admins():
    master = auth.create_user("m@teste.com", "M", "senha-boa-1", papel="master")
    admin = auth.create_user("a@teste.com", "A", "senha-boa-1", papel="admin", actor=master)
    oper = auth.create_user("o@teste.com", "O", "senha-boa-1")
    assert auth.can_manage(master, admin) and auth.can_manage(admin, oper)
    assert not auth.can_manage(admin, master) and not auth.can_manage(admin, admin)
    with pytest.raises(auth.AuthError):
        auth.delete_user(master["id"], actor=admin)
    with pytest.raises(auth.AuthError):
        auth.create_user("x@teste.com", "X", "senha-boa-1", papel="admin", actor=admin)
    with pytest.raises(auth.AuthError):
        auth.update_user(oper["id"], papel="admin", actor=admin)
    auth.delete_user(admin["id"], actor=master)
    assert [u["email"] for u in auth.list_users()] == ["m@teste.com", "o@teste.com"]


def test_oldest_admin_becomes_master_on_upgrade():
    auth.init_db()
    with auth._db() as db:
        db.execute("INSERT INTO usuarios (email, nome, senha_hash, papel, criado_em) VALUES "
                   "('velho@teste.com', 'V', 'x', 'admin', '2026-01-01')")
    auth.init_db()
    assert auth.list_users()[0]["papel"] == "master"
