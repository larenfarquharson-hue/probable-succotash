"""Authentication for the web interface.

This app holds bank statements. For most of its life it binds to 127.0.0.1 and
is reachable only by the person sitting at the machine, which is why it shipped
with no login at all — a password prompt guarding a port nobody else can open
is theatre.

That stops being true the moment you bind to anything else. So the rule here is
that authentication is tied to the *bind address*, not to a setting someone has
to remember to turn on:

    127.0.0.1 / ::1  ->  no passphrase needed, nothing changes
    anything else    ->  a passphrase is required, and `serve` refuses without one

The dangerous configuration is therefore not reachable by accident. A warning in
a README would have been easier, and would have been ignored.

Design notes, because several choices look arbitrary:

* **One passphrase, no usernames.** Single-user tool. A users table would add a
  registration flow, a password-reset path and more attack surface, in exchange
  for no security whatsoever.

* **scrypt, from the standard library.** The CLI has no dependencies and that is
  a load-bearing property (see tests/test_no_dependencies.py), so bcrypt and
  argon2 are out. ``hashlib.scrypt`` is memory-hard, unlike a bare SHA-256, and
  costs an attacker roughly 16 MB per guess at the parameters below.

* **The secret key is generated, not defaulted.** Flask signs session cookies
  with it. A default value published in a public repository means anyone can
  forge a valid session cookie without ever seeing the passphrase, so a real key
  is generated on first use and persisted with restrictive permissions.

* **Rate limiting is in-process and in-memory.** This is one Flask process
  serving one person; a shared store would be pure ceremony. Restarting the
  server clears the counters, which is an accepted limit — an attacker cannot
  restart your server.

What this deliberately does NOT protect against, documented so nobody assumes
otherwise:

* **Traffic is unencrypted.** There is no TLS, so anyone positioned to read
  packets on your network can see the passphrase and the statement data. On home
  Wi-Fi with WPA2/WPA3 that means other devices already on the network, not
  passers-by. Do not put this on a public or guest network, and do not expose it
  to the internet — a tunnel to a public URL gives an unauthenticated-looking
  bank-data endpoint to anyone who finds it.
* **No protection from someone with filesystem access.** The SQLite database is
  not encrypted at rest. Someone who can read your disk does not need the login.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

# scrypt cost parameters. n=2**14 with r=8 needs 128 * n * r = 16 MiB per
# attempt, which is a fraction of a second for the one person logging in and a
# serious tax on anyone guessing. Raise n if you want; existing hashes record
# the parameters they were made with, so old ones keep verifying.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32
SALT_BYTES = 16

MIN_PASSPHRASE_LENGTH = 8

AUTH_FILENAME = "auth.json"

# Rate limiting. After this many consecutive failures from one address, that
# address waits, with the delay doubling each time up to the ceiling.
FREE_ATTEMPTS = 3
BASE_LOCKOUT_SECONDS = 2
MAX_LOCKOUT_SECONDS = 300

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "127.0.0.0/8"}


class AuthError(Exception):
    """Raised when authentication cannot be configured as asked."""


# ---------------------------------------------------------------------------
# Is this exposed?
# ---------------------------------------------------------------------------


def is_loopback(host: str | None) -> bool:
    """True when binding to ``host`` exposes nothing beyond this machine.

    Anything unrecognised is treated as exposed. Guessing wrong in that
    direction costs a passphrase prompt; guessing wrong in the other direction
    publishes bank statements.
    """
    if not host:
        return True
    host = host.strip().lower()
    if host in LOOPBACK_HOSTS:
        return True
    # 127.0.0.0/8 is all loopback.
    if host.startswith("127."):
        parts = host.split(".")
        return len(parts) == 4 and all(p.isdigit() for p in parts)
    return False


# ---------------------------------------------------------------------------
# Stored credentials
# ---------------------------------------------------------------------------


@dataclass
class AuthState:
    """What is stored on disk, and where."""

    path: Path
    secret_key: str
    passphrase_hash: str | None = None
    salt: str | None = None
    scrypt_n: int = SCRYPT_N
    scrypt_r: int = SCRYPT_R
    scrypt_p: int = SCRYPT_P
    updated_at: str | None = None

    @property
    def has_passphrase(self) -> bool:
        return bool(self.passphrase_hash and self.salt)


def auth_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / AUTH_FILENAME


def _restrict(path: Path) -> None:
    """Owner-only permissions, where the platform supports it.

    Windows ignores POSIX mode bits, so this is best-effort. The file holds a
    password hash and a signing key rather than the passphrase itself, so the
    consequence of a readable file is offline guessing, not instant access.
    """
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def load_auth(data_dir: Path | str) -> AuthState:
    """Read stored credentials, creating a signing key on first use.

    Always returns usable state: a brand new install gets a fresh random secret
    key and no passphrase.
    """
    path = auth_path(data_dir)
    if not path.exists():
        state = AuthState(path=path, secret_key=secrets.token_urlsafe(48))
        save_auth(state)
        return state

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthError(
            f"{path} is unreadable ({exc}). Delete it to start again — you will "
            "need to set the passphrase once more, but no statement data is "
            "stored in it."
        ) from exc

    secret = raw.get("secret_key")
    if not secret:
        secret = secrets.token_urlsafe(48)

    state = AuthState(
        path=path,
        secret_key=secret,
        passphrase_hash=raw.get("passphrase_hash"),
        salt=raw.get("salt"),
        scrypt_n=int(raw.get("scrypt_n", SCRYPT_N)),
        scrypt_r=int(raw.get("scrypt_r", SCRYPT_R)),
        scrypt_p=int(raw.get("scrypt_p", SCRYPT_P)),
        updated_at=raw.get("updated_at"),
    )
    if not raw.get("secret_key"):
        save_auth(state)
    return state


def save_auth(state: AuthState) -> None:
    payload = {
        "secret_key": state.secret_key,
        "passphrase_hash": state.passphrase_hash,
        "salt": state.salt,
        "scrypt_n": state.scrypt_n,
        "scrypt_r": state.scrypt_r,
        "scrypt_p": state.scrypt_p,
        "updated_at": state.updated_at,
    }
    state.path.parent.mkdir(parents=True, exist_ok=True)
    # Write then move, so an interrupted write cannot leave a truncated file
    # that locks you out.
    tmp = state.path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _restrict(tmp)
    tmp.replace(state.path)
    _restrict(state.path)


# ---------------------------------------------------------------------------
# Hashing and verification
# ---------------------------------------------------------------------------


def _derive(passphrase: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=DK_LEN,
        maxmem=256 * 1024 * 1024,
    )


def set_passphrase(state: AuthState, passphrase: str) -> AuthState:
    """Hash and store a new passphrase. Returns the updated state."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise AuthError(
            f"passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters"
        )

    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(passphrase, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)

    state.salt = salt.hex()
    state.passphrase_hash = digest.hex()
    state.scrypt_n, state.scrypt_r, state.scrypt_p = SCRYPT_N, SCRYPT_R, SCRYPT_P
    state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_auth(state)
    return state


def clear_passphrase(state: AuthState) -> AuthState:
    """Remove the passphrase. Only sensible for a localhost-only setup."""
    state.passphrase_hash = None
    state.salt = None
    state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_auth(state)
    return state


def verify_passphrase(state: AuthState, attempt: str) -> bool:
    """Constant-time check of an attempted passphrase."""
    if not state.has_passphrase:
        return False
    try:
        salt = bytes.fromhex(state.salt or "")
        expected = bytes.fromhex(state.passphrase_hash or "")
    except ValueError:
        return False

    candidate = _derive(
        attempt, salt, n=state.scrypt_n, r=state.scrypt_r, p=state.scrypt_p
    )
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class LoginThrottle:
    """Per-address backoff on failed logins.

    A short passphrase is only as strong as the number of guesses an attacker
    gets per second. Three free attempts absorb ordinary typos; after that the
    wait doubles each failure, so a thousand guesses takes days rather than
    seconds.
    """

    def __init__(
        self,
        *,
        free_attempts: int = FREE_ATTEMPTS,
        base_seconds: float = BASE_LOCKOUT_SECONDS,
        max_seconds: float = MAX_LOCKOUT_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.free_attempts = free_attempts
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def retry_after(self, key: str) -> float:
        """Seconds this address must wait. 0 means it may try now."""
        until = self._blocked_until.get(key)
        if until is None:
            return 0.0
        remaining = until - self._clock()
        if remaining <= 0:
            self._blocked_until.pop(key, None)
            return 0.0
        return remaining

    def record_failure(self, key: str) -> float:
        """Count a failure and return how long this address must now wait."""
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count

        if count <= self.free_attempts:
            return 0.0

        exponent = count - self.free_attempts - 1
        delay = min(self.base_seconds * (2**exponent), self.max_seconds)
        self._blocked_until[key] = self._clock() + delay
        return delay

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._blocked_until.pop(key, None)


# ---------------------------------------------------------------------------
# The interlock
# ---------------------------------------------------------------------------


def check_exposure(
    state: AuthState, host: str | None, *, tls_ready: bool = False
) -> None:
    """Raise unless it is safe to bind to ``host``.

    Called before the server starts. This is the whole point of the module: the
    unsafe configuration is unreachable rather than merely discouraged.

    ``tls_ready`` only changes what the message says. Telling someone their
    traffic will be unencrypted when they have already set up certificates
    teaches them to distrust these warnings, and a warning nobody believes is
    worse than none.
    """
    if is_loopback(host):
        return

    if not state.has_passphrase:
        if tls_ready:
            aside = (
                "Certificates are already set up, so once a passphrase exists "
                "the connection will be encrypted. This is still a single-user "
                "local tool — keep it to a network you trust and off the "
                "internet."
            )
        else:
            aside = (
                "Note that even with a passphrase the connection would not be "
                "encrypted, so anyone able to watch your network could read the "
                "traffic. Run `tls setup` to fix that. Either way, keep this to "
                "a home network you trust, never a public or guest one, and do "
                "not expose it to the internet."
            )
        raise AuthError(
            f"refusing to serve on {host}: that is reachable from your network, "
            "and no passphrase is set.\n\n"
            "    python3 -m spendtracker.cli passphrase\n\n"
            "sets one (or just `spendtracker passphrase` if that is on your "
            "PATH). Until then the web UI stays on 127.0.0.1, where only this "
            "machine can reach it.\n\n" + aside
        )

    if not state.secret_key or state.secret_key == "dev-only-change-me":
        raise AuthError(
            "refusing to serve off localhost with the default signing key: "
            "session cookies would be forgeable by anyone who has read the "
            "source. Delete auth.json to have a new key generated."
        )
