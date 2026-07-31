"""Authentication, and the interlock that makes it non-optional.

The property under test is not "there is a login page" — it is that bank data
cannot end up on the network without one. So most of these assert that the
unsafe configuration is refused, and that the guard fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spendtracker import auth as auth_mod
from spendtracker.config import Config


# ---------------------------------------------------------------------------
# Loopback detection — the whole interlock hangs off this
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "::1", "localhost", "LOCALHOST", "127.1.2.3", "", None],
)
def test_loopback_addresses_are_recognised(host) -> None:
    assert auth_mod.is_loopback(host) is True


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.42", "10.0.0.5", "example.com", "1.2.3.4"],
)
def test_exposed_addresses_are_recognised(host) -> None:
    assert auth_mod.is_loopback(host) is False


def test_unknown_hosts_are_treated_as_exposed() -> None:
    """Fail closed: guessing wrong here publishes bank statements."""
    assert auth_mod.is_loopback("something-unparseable") is False
    assert auth_mod.is_loopback("127.0.0.1.evil.com") is False


# ---------------------------------------------------------------------------
# Stored credentials
# ---------------------------------------------------------------------------


def test_a_fresh_install_generates_a_real_signing_key(tmp_path: Path) -> None:
    """The default key is published in the repo, so it must never be used."""
    state = auth_mod.load_auth(tmp_path)

    assert state.secret_key
    assert state.secret_key != "dev-only-change-me"
    assert len(state.secret_key) >= 32
    assert state.has_passphrase is False


def test_the_signing_key_is_stable_across_loads(tmp_path: Path) -> None:
    """A key that changed per process would sign everyone out constantly."""
    first = auth_mod.load_auth(tmp_path).secret_key
    second = auth_mod.load_auth(tmp_path).secret_key
    assert first == second


def test_two_installs_get_different_keys(tmp_path: Path) -> None:
    a = auth_mod.load_auth(tmp_path / "a").secret_key
    b = auth_mod.load_auth(tmp_path / "b").secret_key
    assert a != b


def test_the_passphrase_is_never_stored(tmp_path: Path) -> None:
    state = auth_mod.load_auth(tmp_path)
    auth_mod.set_passphrase(state, "correct horse battery staple")

    raw = auth_mod.auth_path(tmp_path).read_text(encoding="utf-8")
    assert "correct horse battery staple" not in raw
    stored = json.loads(raw)
    assert stored["passphrase_hash"]
    assert stored["salt"]


def test_the_same_passphrase_hashes_differently_each_time(tmp_path: Path) -> None:
    """Distinct salts, so identical passphrases are not identifiable as such."""
    a = auth_mod.set_passphrase(auth_mod.load_auth(tmp_path / "a"), "shared-passphrase")
    b = auth_mod.set_passphrase(auth_mod.load_auth(tmp_path / "b"), "shared-passphrase")
    assert a.passphrase_hash != b.passphrase_hash
    assert a.salt != b.salt


def test_verification_accepts_the_right_one_and_rejects_the_rest(
    tmp_path: Path,
) -> None:
    state = auth_mod.set_passphrase(auth_mod.load_auth(tmp_path), "hunter2-and-more")

    assert auth_mod.verify_passphrase(state, "hunter2-and-more") is True
    assert auth_mod.verify_passphrase(state, "hunter2-and-mor") is False
    assert auth_mod.verify_passphrase(state, "HUNTER2-AND-MORE") is False
    assert auth_mod.verify_passphrase(state, "") is False


def test_verification_survives_a_restart(tmp_path: Path) -> None:
    auth_mod.set_passphrase(auth_mod.load_auth(tmp_path), "persisted-passphrase")
    reloaded = auth_mod.load_auth(tmp_path)
    assert auth_mod.verify_passphrase(reloaded, "persisted-passphrase") is True


def test_short_passphrases_are_refused(tmp_path: Path) -> None:
    state = auth_mod.load_auth(tmp_path)
    with pytest.raises(auth_mod.AuthError, match="at least"):
        auth_mod.set_passphrase(state, "short")
    assert state.has_passphrase is False


def test_nothing_verifies_when_no_passphrase_is_set(tmp_path: Path) -> None:
    state = auth_mod.load_auth(tmp_path)
    assert auth_mod.verify_passphrase(state, "") is False
    assert auth_mod.verify_passphrase(state, "anything at all") is False


def test_clearing_removes_it(tmp_path: Path) -> None:
    state = auth_mod.set_passphrase(auth_mod.load_auth(tmp_path), "to-be-removed")
    auth_mod.clear_passphrase(state)

    assert state.has_passphrase is False
    assert auth_mod.load_auth(tmp_path).has_passphrase is False


def test_a_corrupt_credentials_file_is_reported_not_ignored(tmp_path: Path) -> None:
    auth_mod.load_auth(tmp_path)
    auth_mod.auth_path(tmp_path).write_text("{not json", encoding="utf-8")

    with pytest.raises(auth_mod.AuthError, match="unreadable"):
        auth_mod.load_auth(tmp_path)


# ---------------------------------------------------------------------------
# The interlock
# ---------------------------------------------------------------------------


def test_loopback_needs_no_passphrase(tmp_path: Path) -> None:
    """The existing localhost workflow must keep working untouched."""
    state = auth_mod.load_auth(tmp_path)
    auth_mod.check_exposure(state, "127.0.0.1")  # must not raise


def test_exposing_without_a_passphrase_is_refused(tmp_path: Path) -> None:
    state = auth_mod.load_auth(tmp_path)
    with pytest.raises(auth_mod.AuthError) as exc:
        auth_mod.check_exposure(state, "0.0.0.0")

    message = str(exc.value)
    assert "spendtracker.cli passphrase" in message, "must say how to fix it"
    assert "would not be" in message and "encrypted" in message, (
        "must disclose that traffic is unencrypted"
    )


def test_the_warning_does_not_claim_cleartext_when_tls_is_ready(
    tmp_path: Path,
) -> None:
    """A warning that is visibly wrong teaches the reader to ignore all of them.

    Someone who has already run `tls setup` and is told their traffic will be
    unencrypted learns that these messages are boilerplate. That is a worse
    outcome than saying nothing.
    """
    state = auth_mod.load_auth(tmp_path)
    with pytest.raises(auth_mod.AuthError) as exc:
        auth_mod.check_exposure(state, "0.0.0.0", tls_ready=True)

    message = str(exc.value)
    assert "will be encrypted" in message
    assert "would not be encrypted" not in message
    assert "passphrase" in message, "the actual problem must still be stated"


def test_exposing_with_a_passphrase_is_allowed(tmp_path: Path) -> None:
    state = auth_mod.set_passphrase(auth_mod.load_auth(tmp_path), "network-passphrase")
    auth_mod.check_exposure(state, "0.0.0.0")  # must not raise


def test_exposing_with_the_default_signing_key_is_refused(tmp_path: Path) -> None:
    """A known key means forgeable sessions, passphrase or not."""
    state = auth_mod.set_passphrase(auth_mod.load_auth(tmp_path), "network-passphrase")
    state.secret_key = "dev-only-change-me"

    with pytest.raises(auth_mod.AuthError, match="forgeable"):
        auth_mod.check_exposure(state, "192.168.1.42")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_typos_are_forgiven_before_throttling_starts() -> None:
    clock = FakeClock()
    throttle = auth_mod.LoginThrottle(free_attempts=3, clock=clock)

    for _ in range(3):
        assert throttle.record_failure("1.2.3.4") == 0.0
    assert throttle.retry_after("1.2.3.4") == 0.0


def test_the_delay_grows_with_each_further_failure() -> None:
    clock = FakeClock()
    throttle = auth_mod.LoginThrottle(
        free_attempts=1, base_seconds=2, max_seconds=100, clock=clock
    )

    throttle.record_failure("1.2.3.4")
    delays = [throttle.record_failure("1.2.3.4") for _ in range(4)]

    assert delays == [2, 4, 8, 16]
    assert delays == sorted(delays), "backoff must never shrink"


def test_the_delay_is_capped() -> None:
    clock = FakeClock()
    throttle = auth_mod.LoginThrottle(
        free_attempts=0, base_seconds=2, max_seconds=10, clock=clock
    )
    for _ in range(20):
        delay = throttle.record_failure("1.2.3.4")
    assert delay == 10


def test_waiting_clears_the_block() -> None:
    clock = FakeClock()
    throttle = auth_mod.LoginThrottle(
        free_attempts=0, base_seconds=5, max_seconds=100, clock=clock
    )
    throttle.record_failure("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == pytest.approx(5)

    clock.advance(5)
    assert throttle.retry_after("1.2.3.4") == 0.0


def test_one_address_cannot_lock_out_another() -> None:
    clock = FakeClock()
    throttle = auth_mod.LoginThrottle(free_attempts=0, clock=clock)
    for _ in range(5):
        throttle.record_failure("1.2.3.4")

    assert throttle.retry_after("1.2.3.4") > 0
    assert throttle.retry_after("5.6.7.8") == 0.0


def test_a_successful_login_resets_the_counter() -> None:
    clock = FakeClock()
    throttle = auth_mod.LoginThrottle(free_attempts=1, base_seconds=2, clock=clock)
    throttle.record_failure("1.2.3.4")
    throttle.record_failure("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") > 0

    throttle.record_success("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 0.0
    assert throttle.record_failure("1.2.3.4") == 0.0
