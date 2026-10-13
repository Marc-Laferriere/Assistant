"""Secret storage backed by the OS credential store (via ``keyring``).

On Windows this is Credential Manager. All secrets are stored under a single
service name (``[secrets] service_name`` in config.toml) so they show up grouped
in the credential manager UI.
"""

from __future__ import annotations

import keyring

from .config import service_name


class SecretNotFound(RuntimeError):
    pass


def get_secret(name: str) -> str:
    svc = service_name()
    value = keyring.get_password(svc, name)
    if not value:
        raise SecretNotFound(
            f"secret {name!r} not found in the credential store under "
            f"{svc!r}. Set it with:  python -m assistant set-secret {name}"
        )
    return value


def set_secret(name: str, value: str) -> None:
    keyring.set_password(service_name(), name, value)


def delete_secret(name: str) -> None:
    try:
        keyring.delete_password(service_name(), name)
    except keyring.errors.PasswordDeleteError:
        pass
