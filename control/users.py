"""Comptes d'OmniConf : identifiant, nom, rôle, mot de passe, noms de l'assistant.

Fichier DATA_DIR/users.json (mode 600). Mots de passe hachés (scrypt, sel propre à chaque compte).
Rôles :
- « admin » : voit toutes les réunions et tous les assistants, gère les comptes et le lexique ;
- « user »  : voit ses réunions (et celles qu'on partage avec lui), utilise les assistants libres.

Premier démarrage : un compte administrateur est créé avec ADMIN_PASSWORD (identifiant ADMIN_LOGIN, « admin »
par défaut ; nom ADMIN_NAME). Les réunions déjà enregistrées, sans propriétaire, ne sont visibles que des
administrateurs.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path

LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
ROLES = ("admin", "user")
PLATFORMS = ("teams", "zoom", "web")
MIN_PASSWORD = 10


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def new_password() -> str:
    """Mot de passe provisoire lisible (sans caractères ambigus)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(14))


def person_name(display: str) -> str:
    """« Prénom NOM - Société » ou « PRÉNOM NOM (Société) » -> « Prénom Nom »."""
    n = re.sub(r"\s*(\(.*\)|[-–|].*)$", "", (display or "").strip()).strip()
    return " ".join("-".join(x.capitalize() for x in w.split("-")) if w.isupper() and len(w) > 1 else w
                    for w in n.split())


class Users:
    def __init__(self, path: Path, *, admin_password: str | None = None, admin_login: str | None = None,
                 admin_name: str | None = None, bot_names: dict | None = None):
        self.path = Path(path)
        self.lock = threading.Lock()
        self._cache: tuple[float, list[dict]] | None = None
        if not self.path.exists() and admin_password:
            login = (admin_login or "admin").strip().lower()
            name = admin_name or person_name((bot_names or {}).get("teams") or "") or "Administrateur"
            self._save([{"login": login, "name": name, "role": "admin", "hash": hash_password(admin_password),
                         "must_change": False, "bot_names": {k: v for k, v in (bot_names or {}).items() if v},
                         "created": _now(), "last_login": None, "disabled": False}])

    # ------------------------------------------------------------------ fichier
    def _load(self) -> list[dict]:
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return []
        if self._cache and self._cache[0] == mt:
            return self._cache[1]
        items = json.loads(self.path.read_text())
        self._cache = (mt, items)
        return items

    def _save(self, items: list[dict]):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._cache = None

    # ------------------------------------------------------------------ lecture
    def all(self) -> list[dict]:
        return [dict(u) for u in self._load()]

    def get(self, login: str | None) -> dict | None:
        login = (login or "").strip().lower()
        return next((dict(u) for u in self._load() if u["login"] == login), None)

    def admins(self) -> list[str]:
        return [u["login"] for u in self._load() if u["role"] == "admin" and not u.get("disabled")]

    def first_admin(self) -> str | None:
        a = self.admins()
        return a[0] if a else None

    @staticmethod
    def public(u: dict) -> dict:
        return {k: v for k, v in u.items() if k != "hash"}

    def names(self) -> dict[str, str]:
        return {u["login"]: u["name"] for u in self._load()}

    # ------------------------------------------------------------------ connexion
    def authenticate(self, login: str, password: str) -> dict | None:
        u = self.get(login)
        # même coût de calcul que le compte existe ou non (pas d'indice sur les identifiants)
        ok = check_password(password, u["hash"] if u else hash_password("x"))
        if not u or not ok or u.get("disabled"):
            return None
        self.update(u["login"], last_login=_now())
        return self.get(u["login"])

    # ------------------------------------------------------------------ écriture
    def create(self, login: str, name: str, role: str = "user", bot_names: dict | None = None) -> tuple[dict, str]:
        login = (login or "").strip().lower()
        if not LOGIN_RE.match(login):
            raise ValueError("identifiant : 2 à 32 caractères, minuscules, chiffres, « . », « _ » ou « - »")
        if role not in ROLES:
            raise ValueError("rôle inconnu")
        if not (name or "").strip():
            raise ValueError("nom obligatoire")
        pw = new_password()
        with self.lock:
            items = self._load()
            if any(u["login"] == login for u in items):
                raise ValueError("cet identifiant existe déjà")
            u = {"login": login, "name": name.strip()[:80], "role": role, "hash": hash_password(pw),
                 "must_change": True, "bot_names": _clean_bots(bot_names), "created": _now(),
                 "last_login": None, "disabled": False}
            self._save(items + [u])
        return self.public(u), pw

    def update(self, login: str, **fields) -> dict:
        with self.lock:
            items = [dict(u) for u in self._load()]
            u = next((x for x in items if x["login"] == login), None)
            if not u:
                raise KeyError(login)
            if "name" in fields:
                if not (fields["name"] or "").strip():
                    raise ValueError("nom obligatoire")
                fields["name"] = fields["name"].strip()[:80]
            if "bot_names" in fields:
                fields["bot_names"] = _clean_bots(fields["bot_names"])
            if "role" in fields and fields["role"] not in ROLES:
                raise ValueError("rôle inconnu")
            demote = (fields.get("role", u["role"]) != "admin" or fields.get("disabled", u.get("disabled"))) and u["role"] == "admin"
            if demote and [x["login"] for x in items if x["role"] == "admin" and not x.get("disabled")] == [login]:
                raise ValueError("il doit rester au moins un administrateur actif")
            u.update({k: v for k, v in fields.items() if k in ("name", "role", "bot_names", "disabled", "last_login", "must_change")})
            self._save(items)
            return self.public(u)

    def set_password(self, login: str, password: str | None = None, must_change: bool = False) -> str:
        """Nouveau mot de passe (provisoire et à changer s'il n'est pas fourni). Renvoie le mot de passe."""
        if password is not None and len(password) < MIN_PASSWORD:
            raise ValueError(f"mot de passe : {MIN_PASSWORD} caractères au moins")
        pw = password or new_password()
        with self.lock:
            items = [dict(u) for u in self._load()]
            u = next((x for x in items if x["login"] == login), None)
            if not u:
                raise KeyError(login)
            u["hash"], u["must_change"] = hash_password(pw), must_change or password is None
            u["pw_changed"] = _now()
            self._save(items)
        return pw

    def delete(self, login: str):
        with self.lock:
            items = self._load()
            u = next((x for x in items if x["login"] == login), None)
            if not u:
                raise KeyError(login)
            if u["role"] == "admin" and [x["login"] for x in items if x["role"] == "admin" and not x.get("disabled")] == [login]:
                raise ValueError("il doit rester au moins un administrateur actif")
            self._save([x for x in items if x["login"] != login])


def _clean_bots(b: dict | None) -> dict:
    return {k: str(v).strip()[:80] for k, v in (b or {}).items() if k in PLATFORMS and str(v or "").strip()}


# ============================================================================ droits
def is_admin(u: dict | None) -> bool:
    return bool(u) and u.get("role") == "admin"


def can_see(u: dict | None, meta: dict | None) -> bool:
    """Réunion visible : administrateur, propriétaire, ou partagée avec lui. Sans propriétaire : administrateurs."""
    if not u:
        return False
    if is_admin(u):
        return True
    meta = meta or {}
    return meta.get("owner") == u["login"] or u["login"] in (meta.get("shared_with") or [])


def can_edit(u: dict | None, meta: dict | None) -> bool:
    """Renommer, supprimer, partager, régénérer : administrateur ou propriétaire."""
    return bool(u) and (is_admin(u) or (meta or {}).get("owner") == u["login"])


def bot_name(u: dict, platform: str, defaults: dict | None = None) -> str:
    """Nom sous lequel l'assistant entre dans la réunion pour cet utilisateur."""
    b = u.get("bot_names") or {}
    return b.get(platform) or b.get("web") or b.get("teams") or u.get("name") or (defaults or {}).get(platform) or "Assistant"
