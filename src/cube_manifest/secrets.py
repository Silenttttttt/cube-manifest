"""Decrypts the OLD Fernet `ENC[...]` scheme (see the sibling
app-generator/secrets_tool.py this replaces) in memory only - never writes
plaintext to disk anywhere. This is a compatibility bridge to the format
real secrets in this cluster already use, not the planned long-term
replacement (sops+age, not installed on this machine yet). Most real apps'
`secrets:` blocks still use this format, so decrypting it is what's needed
to actually build/apply them today.

This closes a real gap the old system had: terraform_generator.py had zero
awareness of `ENC[...]` and would silently emit the literal ciphertext as
a Kubernetes Secret's value if you ever generated Terraform without going
through deploy.sh's separate decrypt-then-restore shell dance first. Here,
decryption happens once, in schema/loader.py, immediately after parsing -
every other module (generators, deploy.py) only ever sees a plaintext
`AppConfig.secrets` dict and never needs to know this format exists.
"""

from __future__ import annotations

import os
import pwd
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ENC_PREFIX = "ENC["
ENC_SUFFIX = "]"


def _resolve_home() -> Path:
    # Matches secrets_tool.py's own resolution exactly, so both tools agree
    # on the key path even when one of them runs under sudo.
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def key_path() -> Path:
    return _resolve_home() / ".config" / "cubernetes" / "secrets.key"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    path = key_path()
    if not path.exists():
        raise FileNotFoundError(
            f"No secrets key at {path} - can't decrypt ENC[...] values. "
            "(Run the old secrets_tool.py init-key if this is a fresh machine.)"
        )
    return Fernet(path.read_bytes().strip())


def is_encrypted(value: str) -> bool:
    return value.startswith(ENC_PREFIX) and value.endswith(ENC_SUFFIX)


def decrypt_value(value: str) -> str:
    """Returns the plaintext for an ENC[...]-wrapped value, or the value
    unchanged if it isn't wrapped - a plain, never-encrypted secret is
    valid input too, and this must be a no-op for it."""
    if not is_encrypted(value):
        return value
    token = value[len(ENC_PREFIX) : -len(ENC_SUFFIX)]
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(f"Failed to decrypt secret ({value[:20]}...) - wrong key or corrupted value") from exc


def decrypt_secrets(secrets: dict[str, str]) -> dict[str, str]:
    """Decrypts every ENC[...] value in a secrets dict, in memory only.
    Only touches the Fernet key at all if at least one value is actually
    wrapped - an app.yml with plaintext-only secrets (or none) never needs
    the key to exist."""
    if not any(isinstance(v, str) and is_encrypted(v) for v in secrets.values()):
        return secrets
    return {k: decrypt_value(v) for k, v in secrets.items()}
