"""CredentialStore: reference-only credential storage (SPEC §7.5, T12b).

Model:
- Kernel and business code see ONLY ``credential_ref`` strings (e.g.
  ``Integration.credential_ref``). Secret VALUES are resolved through
  backends at the executor / plugin boundary and never enter the
  kernel's observable surfaces (Journal / audit / DB snapshot /
  prompt — SPEC §12.4 不变量 4).
- ``CredentialStore`` routes a ref ``<kind>:<name>`` to a registered
  backend (``env`` reads an environment variable, ``file`` decrypts an
  entry from an encrypted file). Unprefixed refs resolve through the
  configured default backend.
- ``resolve(ref)`` returns the secret — intended for the side that
  actually performs the outbound call (out-of-process executor /
  plugin / harness), which must not record it anywhere.
- ``has(ref)`` is the kernel-safe existence gate: it never returns the
  value, so kernel dispatch can verify a declared ``credential_ref``
  resolves without ever holding the secret.
- ``snapshot()`` is metadata-only (backend kinds + entry NAMES, never
  values), so the store itself is safe to expose for introspection.

The encrypted-file backend is stdlib-only (scrypt key derivation +
HMAC-SHA256 keystream) and is SIMULATOR-GRADE: it proves the
"secrets never stored in plaintext" property and keeps secrets out of
the kernel, but production deployments should point ``file:`` at a
real KMS / secret manager instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Protocol, Sequence

_KDF_N = 2**14
_KDF_R = 8
_KDF_P = 1
_KEY_LEN = 32
_FORMAT_VERSION = 1


class CredentialStoreError(Exception):
    """Base class for all CredentialStore failures."""


class MissingCredentialRefError(CredentialStoreError):
    """A credential_ref is required but empty/missing.

    Per the T12b acceptance: resolve() with no reference must fail
    with an explicit, unambiguous error.
    """


class CredentialNotFoundError(CredentialStoreError):
    """A credential_ref cannot be resolved (unknown backend or entry)."""


class CredentialDecryptError(CredentialStoreError):
    """The encrypted credential file cannot be decrypted (wrong
    passphrase or tampered payload)."""


class CredentialBackend(Protocol):
    """A resolvable credential source behind the store.

    Backends hold NO secrets in memory as state — ``resolve`` fetches
    them on demand (env lookup / file decrypt). ``contains`` is a
    value-free existence check; ``snapshot`` exposes names only.
    """

    kind: str

    def contains(self, name: str) -> bool: ...

    def resolve(self, name: str) -> str: ...

    def list_entries(self) -> list[str]: ...

    def snapshot(self) -> dict[str, Any]: ...


class EnvCredentialBackend:
    """Backend ``env``: resolves ``env:VAR_NAME`` from the environment.

    Secrets arrive via environment injection (CI / host / test), so
    they never touch the kernel, the journal, or disk.
    """

    kind = "env"

    def __init__(self, prefix: str | None = None) -> None:
        # Optional prefix narrows list_entries() to look like
        # credential vars; resolution is unaffected.
        self._prefix = prefix

    def contains(self, name: str) -> bool:
        return name in os.environ

    def resolve(self, name: str) -> str:
        try:
            return os.environ[name]
        except KeyError:
            raise CredentialNotFoundError(
                f"credential env var '{name}' is not set",
            ) from None

    def list_entries(self) -> list[str]:
        if self._prefix is None:
            return []
        return sorted(
            k for k in os.environ if k.startswith(self._prefix)
        )

    def snapshot(self) -> dict[str, Any]:
        return {"kind": self.kind, "entries": self.list_entries()}


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """scrypt key derivation (stdlib-only, simulator-grade KDF)."""
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=_KDF_N,
        r=_KDF_R,
        p=_KDF_P,
        dklen=_KEY_LEN,
    )


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 counter keystream (CTR-like stream cipher).

    Simulator-grade construction; NOT for production secrets.
    """
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            key,
            nonce + counter.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _encrypt(plaintext: bytes, key: bytes) -> tuple[bytes, bytes, bytes]:
    nonce = os.urandom(16)
    stream = _keystream(key, nonce, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))
    tag = hmac.new(
        key, b"credential-store-tag" + nonce + ciphertext, hashlib.sha256,
    ).digest()
    return nonce, ciphertext, tag


def _decrypt(nonce: bytes, ciphertext: bytes, tag: bytes, key: bytes) -> bytes:
    expected = hmac.new(
        key, b"credential-store-tag" + nonce + ciphertext, hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, tag):
        raise CredentialDecryptError(
            "credential file failed integrity check (wrong passphrase "
            "or tampered payload)",
        )
    stream = _keystream(key, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))


class EncryptedFileCredentialBackend:
    """Backend ``file``: ``file:ENTRY`` decrypts from an encrypted file.

    The file holds one encrypted JSON document ``{entry: secret}``:

    .. code-block:: json

        {
          "version": 1,
          "kdf": "scrypt",
          "salt": "<b64>",
          "nonce": "<b64>",
          "tag": "<b64>",
          "ciphertext": "<b64>"
        }

    The plaintext never appears on disk; ``put()`` re-encrypts the
    whole map and writes atomically. A missing file is an empty store.
    """

    kind = "file"

    def __init__(
        self,
        path: str | Path,
        passphrase: str,
    ) -> None:
        self._path = Path(path)
        self._passphrase = passphrase

    # -- private ------------------------------------------------------------

    def _load_entries(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CredentialDecryptError(
                f"credential file '{self._path}' is not readable JSON",
            ) from None
        if not isinstance(doc, dict) or doc.get("version") != _FORMAT_VERSION:
            raise CredentialDecryptError(
                f"credential file '{self._path}' has an unsupported format",
            )
        try:
            salt = base64.b64decode(doc["salt"])
            nonce = base64.b64decode(doc["nonce"])
            tag = base64.b64decode(doc["tag"])
            ciphertext = base64.b64decode(doc["ciphertext"])
        except (KeyError, ValueError):
            raise CredentialDecryptError(
                f"credential file '{self._path}' is malformed",
            ) from None
        key = _derive_key(self._passphrase, salt)
        payload = _decrypt(nonce, ciphertext, tag, key)
        try:
            entries = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise CredentialDecryptError(
                f"credential file '{self._path}' payload is corrupt",
            ) from None
        if not isinstance(entries, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in entries.items()
        ):
            raise CredentialDecryptError(
                f"credential file '{self._path}' payload is not a "
                "string-to-string map",
            )
        return entries

    def _save_entries(self, entries: dict[str, str]) -> None:
        salt = os.urandom(16)
        key = _derive_key(self._passphrase, salt)
        payload = json.dumps(entries, sort_keys=True).encode("utf-8")
        nonce, ciphertext, tag = _encrypt(payload, key)
        doc = {
            "version": _FORMAT_VERSION,
            "kdf": "scrypt",
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(doc, sort_keys=True), encoding="utf-8",
        )
        os.replace(tmp, self._path)

    # -- backend interface --------------------------------------------------

    def contains(self, name: str) -> bool:
        return name in self._load_entries()

    def resolve(self, name: str) -> str:
        entries = self._load_entries()
        try:
            return entries[name]
        except KeyError:
            raise CredentialNotFoundError(
                f"credential entry '{name}' not found in file "
                f"'{self._path}'",
            ) from None

    def list_entries(self) -> list[str]:
        return sorted(self._load_entries())

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": str(self._path),
            "entries": self.list_entries(),
        }

    # -- host-side mutation (never part of kernel state) -------------------

    def put(self, name: str, secret: str) -> None:
        """Write one entry (encrypted, atomic). Host-side setup only."""
        entries = self._load_entries()
        entries[name] = secret
        self._save_entries(entries)


class CredentialStore:
    """Reference-only credential resolution service (SPEC §7.5).

    Ref syntax: ``<kind>:<name>`` where ``kind`` names a registered
    backend; a ref without a recognized ``kind:`` prefix resolves
    through ``default_backend`` when configured.
    """

    def __init__(
        self,
        backends: Sequence[CredentialBackend] | None = None,
        default_backend: str | None = None,
    ) -> None:
        self._backends: dict[str, CredentialBackend] = {}
        self._default_backend = default_backend
        for backend in backends or []:
            self.register(backend)

    def register(self, backend: CredentialBackend) -> None:
        if backend.kind in self._backends:
            raise ValueError(
                f"credential backend kind '{backend.kind}' already "
                "registered",
            )
        self._backends[backend.kind] = backend

    @property
    def default_backend(self) -> str | None:
        return self._default_backend

    # -- ref routing ---------------------------------------------------------

    def _backend_for(self, ref: str) -> tuple[CredentialBackend, str]:
        kind, sep, name = ref.partition(":")
        if sep and kind and name:
            backend = self._backends.get(kind)
            if backend is None:
                raise CredentialNotFoundError(
                    f"credential_ref '{ref}' names unknown backend "
                    f"kind '{kind}'",
                )
            return backend, name
        if self._default_backend is not None:
            backend = self._backends.get(self._default_backend)
            if backend is None:
                raise CredentialStoreError(
                    f"default backend '{self._default_backend}' is not "
                    "registered",
                )
            return backend, ref
        raise CredentialNotFoundError(
            f"credential_ref '{ref}' has no backend kind and no default "
            "backend is configured",
        )

    # -- interface ------------------------------------------------------------

    def has(self, ref: str) -> bool:
        """Value-free existence check (kernel admission gate).

        Never returns the secret; unresolvable refs are simply False.
        """
        if not ref:
            return False
        try:
            backend, name = self._backend_for(ref)
        except CredentialNotFoundError:
            return False
        return backend.contains(name)

    def resolve(self, ref: str) -> str:
        """Resolve a credential_ref to its secret value.

        Intended for the executor / plugin / harness boundary that
        performs the outbound call. Raises:
        - ``MissingCredentialRefError`` when the ref is empty (无引用);
        - ``CredentialNotFoundError`` when the ref cannot be resolved;
        - ``CredentialStoreError`` on backend failures.
        """
        if not ref or not ref.strip():
            raise MissingCredentialRefError(
                "credential_ref is empty: a credential reference is "
                "required to resolve a credential",
            )
        backend, name = self._backend_for(ref)
        return backend.resolve(name)

    def snapshot(self) -> dict[str, Any]:
        """Metadata-only introspection: kinds + entry names, never values."""
        return {
            "default_backend": self._default_backend,
            "backends": [
                b.snapshot() for b in self._backends.values()
            ],
        }
