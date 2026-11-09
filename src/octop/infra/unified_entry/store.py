"""Durable bindings and encrypted backend credentials for the entry candidate.

The encryption key is injected, never written to this database. Atomic
monotonic session states keep stale concurrent renewals from reviving access.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass, field

from cryptography.fernet import Fernet, InvalidToken

from octop.infra.db.services import SharedServices


@dataclass(frozen=True)
class Binding:
    identity_id: str
    instance: str
    username: str
    user_id: int

    @property
    def storage_scope(self) -> str:
        """An identity may own accounts in multiple instances; UI state may not mix them."""
        encoded = json.dumps([self.instance, self.user_id, self.identity_id], separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class Session:
    alias: str
    binding: Binding
    backend_token: str = field(repr=False)
    expires_at: float


class EntryStore:
    def __init__(self, services: SharedServices, encryption_key: bytes):
        self._settings = services.settings_repo
        self._sessions = services.repos.entry_session_repo
        self.operations = services.repos.entry_admin_operation_repo
        self._cipher = Fernet(encryption_key)

    @staticmethod
    def _key(kind: str, value: str) -> str:
        return "entry." + kind + "." + hashlib.sha256(value.encode()).hexdigest()

    def register(self, alias: str, binding: Binding) -> None:
        encoded = json.dumps(asdict(binding), sort_keys=True)
        stored = self._settings.set_if_absent(self._key("binding", alias), encoded)
        if stored != encoded:
            raise ValueError("Identity binding conflict; existing mapping preserved")

    def binding(self, alias: str) -> Binding | None:
        raw = self._settings.get(self._key("binding", alias))
        if raw is None:
            return None
        binding = Binding(**json.loads(raw))
        if self._settings.get(self._key("disabled", binding.identity_id)) is not None:
            return None
        return binding

    def disable(self, identity_id: str) -> None:
        self._settings.set(self._key("disabled", identity_id), "1")

    def grant_platform_admin(self, identity_id: str) -> None:
        """Platform administration is an entry fact, never inferred from an instance role."""
        self._settings.set(self._key("platform_admin", identity_id), "1")

    def is_platform_admin(self, identity_id: str) -> bool:
        return self._settings.get(self._key("platform_admin", identity_id)) is not None

    def create(self, alias: str, backend_token: str, ttl: int) -> str:
        binding = self.binding(alias)
        if binding is None:
            raise ValueError("Identity unavailable")
        token = secrets.token_urlsafe(32)
        session = Session(alias, binding, backend_token, time.time() + ttl)
        self._sessions.create(self._key("session", token), self._encrypt(token, session))
        return token

    def _encrypt(self, token: str, session: Session) -> str:
        key = self._key("session", token)
        payload = {**asdict(session), "session_key": key}
        return self._cipher.encrypt(json.dumps(payload).encode()).decode()

    def _save(self, token: str, session: Session) -> None:
        self._sessions.renew(self._key("session", token), self._encrypt(token, session))

    def read(self, token: str, pending: bool = False) -> Session | None:
        return self._read_key(self._key("session", token), pending)

    def _read_key(self, session_key: str, pending: bool) -> Session | None:
        record = self._sessions.get(session_key)
        if record is None:
            return None
        state, raw = record
        if state == "done" or (state != "active" and not pending) or raw is None:
            return None
        try:
            data = json.loads(self._cipher.decrypt(raw.encode()))
            if data["session_key"] != session_key:
                return None
            session = Session(
                data["alias"], Binding(**data["binding"]), data["backend_token"], data["expires_at"]
            )
        except (InvalidToken, ValueError, TypeError, KeyError):
            return None
        if pending:
            latest = self._sessions.get(session_key)
            return session if latest is not None and latest[0] != "done" else None
        binding_key = self._key("binding", session.alias)
        disabled_key = self._key("disabled", session.binding.identity_id)
        current = self._settings.get_many([binding_key, disabled_key])
        latest = self._sessions.get(session_key)
        # Recheck after decryption; do not cache across network awaits/requests.
        if (
            session.expires_at <= time.time()
            or disabled_key in current
            or latest is None
            or latest[0] != "active"
            or current.get(binding_key) != json.dumps(asdict(session.binding), sort_keys=True)
        ):
            return None
        return session

    def renew_backend(self, token: str, backend_token: str) -> None:
        session = self.read(token)
        if session is not None:
            self._save(
                token, Session(session.alias, session.binding, backend_token, session.expires_at)
            )
        # The repository only updates active rows; a concurrent revoke cannot be cleared.

    def begin_revoke(self, token: str) -> Session | None:
        key = self._key("session", token)
        self._sessions.revoke(key)
        return self._read_key(key, pending=True)

    def finish_revoke(self, token: str) -> None:
        self._sessions.complete(self._key("session", token))

    def pending_revocations(
        self, after: str = "", limit: int = 50
    ) -> list[tuple[str, Session | None]]:
        keys = self._sessions.pending(after, max(1, min(limit, 100)))
        return [(key, self._read_key(key, True)) for key in keys]

    def finish_pending(self, key: str) -> None:
        if not key.startswith("entry.session."):
            raise ValueError("Invalid pending session key")
        self._sessions.complete(key)
