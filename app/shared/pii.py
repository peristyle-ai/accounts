"""
PII encryption helpers for the payroll module.

Phase 1a (current — 2026-08-27, see payroll_expansion.md §12):

    - `tfn`, `bank_bsb`, `bank_account_number` columns are stored as Fernet
      ciphertext (URL-safe base64 in a VARCHAR(500) column).
    - Key lives in `.env` as `PAYROLL_PII_KEY` (env var). Never committed.
    - Plaintext is only returned via the `*_plain` accessors on the SQLAlchemy
      model. Default `to_dict()` returns masked values.
    - `cryptography` package is already installed (pulled in by `bcrypt`).

Phase 5 (KMS upgrade):

    - Replace `Fernet(key).encrypt/decrypt` with a call to a separate-user
      decryption daemon over an authenticated Unix domain socket.
    - Key bytes never enter the app process; every decrypt is logged.
    - See `coding_agents/encrpytion_server_implementation/implementation_plan.md`.
    - The encryption call sites (encrypt_pii / decrypt_pii) won't change; only
      the body of the helpers will. Models and accessors stay the same.

Thread safety: Fernet objects are not thread-safe for key rotation, but the
process-wide key is read once at module import. If rotation happens at runtime
(reload of the key file), restart the app.
"""

import os
import threading

from cryptography.fernet import Fernet, InvalidToken


# Resolve the key from the environment once at import. Fail fast if missing
# — there's no sane default, and a missing key would silently downgrade PII
# to plaintext in the DB.
_KEY = os.environ.get("PAYROLL_PII_KEY", "").strip()
if not _KEY:
    raise RuntimeError(
        "PAYROLL_PII_KEY is not set in the environment. PII columns cannot be "
        "encrypted/decrypted safely. Set it in .env (chmod 600) and restart."
    )

try:
    _FERNET = Fernet(_KEY.encode("ascii"))
except (ValueError, TypeError) as e:
    raise RuntimeError(
        f"PAYROLL_PII_KEY is not a valid Fernet key: {e}. Generate one with "
        f"`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`."
    ) from e

# Single-thread in-process lock — Python GIL makes this overkill in CPython
# but it's free correctness if anyone uses a non-CPython interpreter.
_LOCK = threading.Lock()


def is_configured() -> bool:
    """True iff PAYROLL_PII_KEY is set and a Fernet object was constructed.

    Useful for templates / status endpoints to display the encryption state
    without leaking the key.
    """
    return bool(_KEY) and _FERNET is not None


def encrypt_pii(plaintext):
    """Encrypt plaintext (str) and return URL-safe base64 ciphertext (str).

    Returns None when plaintext is None / empty — the column is nullable and
    we don't want to write the literal ciphertext-of-empty-string to the DB
    (it would be ambiguous with a real ciphertext that decrypts to "").
    """
    if plaintext is None:
        return None
    s = str(plaintext)
    if s == "":
        return None
    with _LOCK:
        return _FERNET.encrypt(s.encode("utf-8")).decode("ascii")


def decrypt_pii(ciphertext):
    """Decrypt Fernet ciphertext and return plaintext (str).

    Returns None when ciphertext is None / empty. Raises InvalidToken if the
    ciphertext is unreadable (e.g. plaintext that was never encrypted, or
    ciphertext encrypted with a different key).
    """
    if ciphertext is None:
        return None
    s = str(ciphertext)
    if s == "":
        return None
    with _LOCK:
        return _FERNET.decrypt(s.encode("ascii")).decode("utf-8")


def is_encrypted_pii(value):
    """True if `value` is a Fernet ciphertext that decrypts cleanly.

    False for None / empty / anything that is not a valid ciphertext.
    Used to decide whether a row still needs migrating to encrypted
    storage.
    """
    if value is None or str(value) == "":
        return False
    try:
        decrypt_pii(str(value))
    except (InvalidToken, ValueError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------
# Masking helpers — used by default in `to_dict()` so API/HMI responses
# never leak full PII.
# ---------------------------------------------------------------------------

def mask_tfn(plain):
    """Return `*** *** 250` style (last 3 digits visible).

    Tolerates the common Australian TFN input formats (with spaces, hyphens,
    dots — see ATO convention for TFN formatting).
    """
    if plain is None:
        return None
    digits = "".join(ch for ch in str(plain) if ch.isdigit())
    if not digits:
        return "***"
    last3 = digits[-3:]
    return f"*** *** {last3}"


def mask_bsb(plain):
    """Return `***-456` style (last 3 digits visible).

    BSB is 6 digits; tolerates `123-456` / `123456` / `123 456`.
    """
    if plain is None:
        return None
    digits = "".join(ch for ch in str(plain) if ch.isdigit())
    if len(digits) < 3:
        return "***"
    last3 = digits[-3:]
    return f"***-{last3}"


def mask_account(plain):
    """Return `*****678` style (last 4 digits visible).

    Australian account numbers are 4-10 digits.
    """
    if plain is None:
        return None
    digits = "".join(ch for ch in str(plain) if ch.isdigit())
    if len(digits) <= 4:
        return f"****{digits}" if digits else "*****"
    last4 = digits[-4:]
    return f"*****{last4}"
