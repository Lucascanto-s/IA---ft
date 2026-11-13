"""Streamlit screens for the local login: first admin, sign-in, users, settings and own account."""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

import auth
from baselinker import BaseLinkerClient, BaseLinkerError
from ui.theme import page_header

COOKIE = "lmr_sessao"
STAR_LABELS = {"": "Não mudar", "0": "0 · nenhuma", "1": "1 · azul", "2": "2 · vinho", "3": "3 · verde",
               "4": "4 · amarela", "5": "5 · vermelha"}
DEFAULT_API_URL = "https://api.baselinker.com/connector.php"


# --------------------------------------------------------------------------- session cookie

def _cookie_script() -> None:
    """Write/clear the session cookie in the browser (runs on the run after login/logout)."""
    action = st.session_state.pop("_auth_cookie", None)
    if action is None:
        return
    if action == "clear":
        value, max_age = "", 0
    else:
        value, max_age = action, auth.SESSION_HOURS * 3600
    st.html(
        f"<script>document.cookie = '{COOKIE}={value}; path=/; max-age={max_age}; SameSite=Strict';</script>",
        unsafe_allow_javascript=True,
    )


def _start_session(user: dict) -> None:
    token = auth.create_session(user["id"])
    st.session_state["auth_token"] = token
    st.session_state["_auth_cookie"] = token
    st.rerun()


def logout() -> None:
    auth.end_session(st.session_state.get("auth_token"))
    st.session_state.clear()
    st.session_state["_auth_cookie"] = "clear"
    st.session_state["_auth_logged_out"] = True
    st.rerun()


def current_user() -> dict | None:
    return st.session_state.get("auth_user")


def can(permission: str) -> bool:
    return auth.can(current_user(), permission)


# --------------------------------------------------------------------------- gate

def _centered():
    _, middle, _ = st.columns([1, 1.4, 1])
    return middle


def _first_admin_form() -> None:
    with _centered():
        page_header("Primeiro acesso", "Crie o login de administrador. Ele controla os outros logins e as "
                    "configurações.", icon="shield-lock")
        with st.form("first_admin", border=True):
            nome = st.text_input("Nome", autocomplete="name")
            email = st.text_input("E-mail", autocomplete="email")
            senha = st.text_input("Senha", type="password", autocomplete="new-password",
                                  help=f"Mínimo de {auth.MIN_PASSWORD} caracteres.")
            confirmar = st.text_input("Confirmar senha", type="password", autocomplete="new-password")
            if st.form_submit_button("Criar administrador", type="primary", icon=":material/admin_panel_settings:",
                                     width="stretch"):
                if senha != confirmar:
                    st.error("As senhas não conferem.", icon=":material/error:")
                    return
                try:
                    user = auth.create_user(email, nome, senha, papel="master")
                except auth.AuthError as error:
                    st.error(str(error), icon=":material/error:")
                    return
                auth.import_env_settings()  # token and options already in .env move to the encrypted store
                _start_session(user)


def _login_form() -> None:
    with _centered():
        page_header("LMR Edit", "Entre com seu login ou crie sua conta.", icon="shield-lock")
        if st.session_state.pop("_auth_logged_out", False):
            st.success("Você saiu da sua conta.", icon=":material/logout:")
        mode = st.segmented_control("Acesso", ["Entrar", "Criar conta"], default="Entrar",
                                    label_visibility="collapsed", key="auth_mode", width="stretch")
        if mode == "Criar conta":
            _signup_form()
            return
        with st.form("login", border=True):
            email = st.text_input("E-mail", autocomplete="email")
            senha = st.text_input("Senha", type="password", autocomplete="current-password")
            if st.form_submit_button("Entrar", type="primary", icon=":material/login:", width="stretch"):
                try:
                    user = auth.authenticate(email, senha)
                except auth.AuthError as error:
                    st.error(str(error), icon=":material/error:")
                    return
                _start_session(user)


def _signup_form() -> None:
    with st.form("signup", border=True):
        nome = st.text_input("Nome", autocomplete="name")
        email = st.text_input("E-mail", autocomplete="email")
        senha = st.text_input("Senha", type="password", autocomplete="new-password",
                              help=f"Mínimo de {auth.MIN_PASSWORD} caracteres.")
        confirmar = st.text_input("Confirmar senha", type="password", autocomplete="new-password")
        st.caption(":material/info: Depois de criar, o administrador libera o que você pode usar.")
        if st.form_submit_button("Criar conta", type="primary", icon=":material/person_add:", width="stretch"):
            if senha != confirmar:
                st.error("As senhas não conferem.", icon=":material/error:")
                return
            try:
                user = auth.register_self(email, nome, senha)
            except auth.AuthError as error:
                st.error(str(error), icon=":material/error:")
                return
            _start_session(user)


def page_waiting() -> None:
    user = current_user() or {}
    page_header("Aguardando liberação", f"Olá, {user.get('nome', '')}. Sua conta foi criada.", icon="shield-lock")
    st.info("O administrador ainda não liberou nenhuma função para você. Peça para ele entrar com o login "
            "dele neste computador e liberar suas permissões em **Usuários**. Depois disso, atualize a página.",
            icon=":material/hourglass_top:", title="Conta sem permissões")
    if st.button("Sair da conta", icon=":material/logout:"):
        logout()


def _forced_password_change(user: dict) -> None:
    with _centered():
        page_header("Defina sua senha", f"Olá, {user['nome']}. Troque a senha provisória para continuar.",
                    icon="shield-lock")
        with st.form("forced_password", border=True):
            atual = st.text_input("Senha provisória", type="password", autocomplete="current-password")
            nova = st.text_input("Nova senha", type="password", autocomplete="new-password",
                                 help=f"Mínimo de {auth.MIN_PASSWORD} caracteres.")
            confirmar = st.text_input("Confirmar nova senha", type="password", autocomplete="new-password")
            if st.form_submit_button("Salvar nova senha", type="primary", width="stretch"):
                if nova != confirmar:
                    st.error("As senhas não conferem.", icon=":material/error:")
                    return
                try:
                    auth.change_own_password(user["id"], atual, nova)
                except auth.AuthError as error:
                    st.error(str(error), icon=":material/error:")
                    return
                _start_session(user)  # changing the password ends the old sessions
        if st.button("Sair", icon=":material/logout:"):
            logout()


def login_gate() -> dict:
    """Return the signed-in user, or show the right screen and stop the run."""
    _cookie_script()
    token = st.session_state.get("auth_token")
    if not token and not st.session_state.get("_auth_logged_out"):
        token = st.context.cookies.get(COOKIE)  # F5 / new tab: reuse the browser's session
        if token:
            st.session_state["auth_token"] = token
    user = auth.user_from_session(token)
    if user is None:
        auth.seed_admins_from_package()
        st.session_state.pop("auth_token", None)
        st.session_state.pop("auth_user", None)
        if auth.has_users():
            _login_form()
        else:
            _first_admin_form()
        st.stop()
    st.session_state["auth_user"] = user
    if user["trocar_senha"]:
        _forced_password_change(user)
        st.stop()
    return user


# --------------------------------------------------------------------------- pages

def _permission_labels(user: dict) -> str:
    if user["papel"] in auth.ADMIN_ROLES:
        return "Todas"
    return ", ".join(auth.PERMISSIONS[item] for item in user["permissoes"] if item in auth.PERMISSIONS) or "—"


def _create_user_form(me: dict) -> None:
    with st.container(border=True, key="lmr-card-create-user"):
        st.subheader("Criar login", icon=":material/person_add:")
        with st.form("create_user", clear_on_submit=True, border=False):
            col_a, col_b = st.columns(2)
            nome = col_a.text_input("Nome")
            email = col_b.text_input("E-mail")
            col_c, col_d = st.columns(2)
            senha = col_c.text_input("Senha provisória", type="password", autocomplete="new-password",
                                     help=f"Mínimo de {auth.MIN_PASSWORD} caracteres. Passe para a pessoa.")
            papel = col_d.segmented_control("Perfil", auth.assignable_roles(me), format_func=auth.ROLES.get,
                                            default="operador")
            permissoes = st.pills("Permissões (o administrador tem todas)", list(auth.PERMISSIONS),
                                  format_func=auth.PERMISSIONS.get, selection_mode="multi",
                                  default=auth.DEFAULT_OPERATOR_PERMISSIONS)
            trocar = st.checkbox("Pedir para trocar a senha no primeiro acesso", value=True)
            if st.form_submit_button("Criar login", type="primary", icon=":material/person_add:"):
                try:
                    created = auth.create_user(email, nome, senha, papel or "operador", permissoes, trocar, actor=me)
                except auth.AuthError as error:
                    st.error(str(error), icon=":material/error:")
                else:
                    st.success(f"Login de {created['nome']} criado.", icon=":material/check_circle:")


def _edit_user(me: dict, users: list[dict]) -> None:
    with st.container(border=True, key="lmr-card-edit-user"):
        st.subheader("Editar login", icon=":material/manage_accounts:")
        manageable = [user for user in users if auth.can_manage(me, user)]
        if not manageable:
            st.caption(":material/info: Nenhum login que você possa editar. Administradores só são "
                       "alterados pelo administrador máximo.")
            return
        selected = st.selectbox("Login", manageable, key="edit_user_pick",
                                format_func=lambda item: f"{item['nome']} · {item['email']} · "
                                                         f"{auth.ROLES.get(item['papel'], '')}")
        if not selected:
            return
        roles = auth.assignable_roles(me)
        with st.form(f"edit_user_{selected['id']}", border=False):
            col_a, col_b = st.columns([1.4, 2], vertical_alignment="bottom")
            nome = col_a.text_input("Nome", value=selected["nome"])
            papel = col_b.segmented_control("Perfil", roles, format_func=auth.ROLES.get,
                                            default=selected["papel"] if selected["papel"] in roles else None)
            ativo = st.toggle("Ativo (desligado = não consegue entrar)", value=selected["ativo"])
            permissoes = st.pills("Permissões (administradores têm todas)", list(auth.PERMISSIONS),
                                  format_func=auth.PERMISSIONS.get, selection_mode="multi",
                                  default=selected["permissoes"])
            if st.form_submit_button("Salvar alterações", type="primary", icon=":material/save:"):
                try:
                    auth.update_user(selected["id"], nome=nome, papel=papel or selected["papel"],
                                     permissoes=permissoes, ativo=ativo, actor=me)
                except auth.AuthError as error:
                    st.error(str(error), icon=":material/error:")
                else:
                    st.success("Login atualizado.", icon=":material/check_circle:")
                    st.rerun()
        with st.expander("Redefinir senha ou excluir", icon=":material/lock_reset:"):
            with st.form(f"reset_{selected['id']}", clear_on_submit=True, border=False):
                nova = st.text_input("Nova senha provisória", type="password", autocomplete="new-password")
                if st.form_submit_button("Redefinir senha", icon=":material/lock_reset:"):
                    try:
                        auth.set_password(selected["id"], nova, must_change=True, actor=me)
                    except auth.AuthError as error:
                        st.error(str(error), icon=":material/error:")
                    else:
                        st.success("Senha redefinida. A pessoa vai trocá-la no próximo acesso.",
                                   icon=":material/check_circle:")
            confirm = st.checkbox(f"Quero excluir o login de {selected['nome']}", key=f"del_ok_{selected['id']}")
            if st.button("Excluir login", disabled=not confirm, icon=":material/delete:",
                         key=f"del_{selected['id']}"):
                try:
                    auth.delete_user(selected["id"], actor=me)
                except auth.AuthError as error:
                    st.error(str(error), icon=":material/error:")
                else:
                    st.rerun()
        st.caption(":material/info: Sua própria senha fica em **Minha conta**.")


def page_users() -> None:
    page_header("Usuários", "Crie logins e escolha o que cada pessoa pode fazer.", icon="shield-lock")
    me = current_user()
    if not can("usuarios") or me is None:
        st.error("Você não tem permissão para esta página.", icon=":material/block:")
        return
    users = auth.list_users()
    table = pd.DataFrame([{
        "Nome": user["nome"],
        "E-mail": user["email"],
        "Perfil": auth.ROLES.get(user["papel"], user["papel"]),
        "Permissões": _permission_labels(user),
        "Ativo": user["ativo"],
        "Último acesso": (user["ultimo_acesso"] or "").replace("T", " "),
    } for user in users])
    st.dataframe(table, hide_index=True, alt="Logins cadastrados")
    left, right = st.columns(2, gap="large")
    with left:
        _create_user_form(me)
    with right:
        _edit_user(me, users)


def page_settings() -> None:
    page_header("Configurações", "Token do BaseLinker e opções. Tudo fica salvo com criptografia neste "
                "computador.", icon="cpu")
    if not can("configuracoes"):
        st.error("Você não tem permissão para esta página.", icon=":material/block:")
        return
    saved = auth.load_settings()
    stars = list(STAR_LABELS)
    with st.container(border=True, key="lmr-card-settings"):
        with st.form("settings", border=False):
            st.subheader("BaseLinker", icon=":material/key:")
            token = st.text_input(
                auth.SETTINGS["BASELINKER_TOKEN"], type="password", autocomplete="off",
                placeholder="•••••••• configurado · deixe vazio para manter" if saved.get("BASELINKER_TOKEN")
                else "Cole aqui o token (BaseLinker › Minha conta › API)",
            )
            col_a, col_b = st.columns(2)
            inventory = col_a.text_input(auth.SETTINGS["BASELINKER_INVENTORY_ID"],
                                         value=saved.get("BASELINKER_INVENTORY_ID", ""))
            api_url = col_b.text_input(auth.SETTINGS["BASELINKER_API_URL"],
                                       value=saved.get("BASELINKER_API_URL", DEFAULT_API_URL))
            st.subheader("Publicação", icon=":material/cloud_upload:")
            public_url = st.text_input(auth.SETTINGS["PUBLIC_IMAGE_BASE_URL"],
                                       value=saved.get("PUBLIC_IMAGE_BASE_URL", ""),
                                       help="Vazio = as imagens vão direto do computador (base64).")
            col_c, col_d = st.columns(2)
            todo = col_c.selectbox(auth.SETTINGS["BASELINKER_STAR_TODO"], stars[1:], format_func=STAR_LABELS.get,
                                   index=stars[1:].index(saved.get("BASELINKER_STAR_TODO", "4"))
                                   if saved.get("BASELINKER_STAR_TODO", "4") in stars[1:] else 4)
            done = col_d.selectbox(auth.SETTINGS["BASELINKER_STAR_DONE"], stars, format_func=STAR_LABELS.get,
                                   index=stars.index(saved.get("BASELINKER_STAR_DONE", "5"))
                                   if saved.get("BASELINKER_STAR_DONE", "5") in stars else 6)
            remove_token = bool(saved.get("BASELINKER_TOKEN")) and st.checkbox("Apagar o token salvo")
            if st.form_submit_button("Salvar configurações", type="primary", icon=":material/save:"):
                values = {
                    "BASELINKER_INVENTORY_ID": inventory,
                    "BASELINKER_API_URL": api_url or DEFAULT_API_URL,
                    "PUBLIC_IMAGE_BASE_URL": public_url,
                    "BASELINKER_STAR_TODO": todo or "4",
                    "BASELINKER_STAR_DONE": done or "",
                }
                if token.strip() or remove_token:
                    values["BASELINKER_TOKEN"] = "" if remove_token else token
                auth.save_settings(values)
                for key, value in values.items():
                    if value.strip():
                        os.environ[key] = value.strip()
                    else:
                        os.environ.pop(key, None)
                st.success("Configurações salvas.", icon=":material/check_circle:")
    with st.container(border=True, key="lmr-card-settings-test"):
        st.markdown("**:material/network_check: Testar conexão com o BaseLinker**")
        if st.button("Testar conexão", icon=":material/play_arrow:"):
            _test_connection()


def _test_connection() -> None:
    token = os.getenv("BASELINKER_TOKEN", "")
    inventory = os.getenv("BASELINKER_INVENTORY_ID", "")
    if not token:
        st.warning("Salve o token antes de testar.", icon=":material/warning:")
        return
    client = BaseLinkerClient(token, inventory, os.getenv("BASELINKER_API_URL", DEFAULT_API_URL))
    try:
        result = client.call("getInventories", {})
    except BaseLinkerError as error:
        st.error(f"Não conectou: {error}", icon=":material/error:")
        return
    inventories = {str(item.get("inventory_id")): item.get("name", "") for item in result.get("inventories", [])}
    if inventory and inventory not in inventories:
        st.warning(f"Token válido, mas o catálogo {inventory} não existe nessa conta. "
                   f"Catálogos: {', '.join(f'{key} ({name})' for key, name in inventories.items())}",
                   icon=":material/warning:")
    else:
        name = inventories.get(inventory, "")
        st.success(f"Conectado ao BaseLinker{f' · catálogo {inventory} ({name})' if inventory else ''}.",
                   icon=":material/check_circle:")


def page_account() -> None:
    user = current_user()
    if user is None:
        return
    page_header("Minha conta", f"{user['nome']} · {user['email']} · {auth.ROLES.get(user['papel'], '')}",
                icon="shield-lock")
    left, right = st.columns(2, gap="large")
    with left, st.container(border=True, key="lmr-card-password"):
        st.subheader("Trocar senha", icon=":material/password:")
        with st.form("own_password", clear_on_submit=True, border=False):
            atual = st.text_input("Senha atual", type="password", autocomplete="current-password")
            nova = st.text_input("Nova senha", type="password", autocomplete="new-password",
                                 help=f"Mínimo de {auth.MIN_PASSWORD} caracteres.")
            confirmar = st.text_input("Confirmar nova senha", type="password", autocomplete="new-password")
            if st.form_submit_button("Salvar nova senha", type="primary", icon=":material/save:"):
                if nova != confirmar:
                    st.error("As senhas não conferem.", icon=":material/error:")
                else:
                    try:
                        auth.change_own_password(user["id"], atual, nova)
                    except auth.AuthError as error:
                        st.error(str(error), icon=":material/error:")
                    else:
                        _start_session(user)
    with right, st.container(border=True, key="lmr-card-logout"):
        st.subheader("Sessão", icon=":material/badge:")
        st.markdown(f"Permissões: {_permission_labels(user)}")
        if st.button("Sair da conta", icon=":material/logout:", type="primary"):
            logout()
