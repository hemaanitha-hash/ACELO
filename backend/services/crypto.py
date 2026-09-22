"""
Encrypts connection secrets (PATs, client secrets) before they touch the database.

This is a minimum-bar implementation for a self-hosted deployment: symmetric
encryption at rest with a key from the environment. It keeps secrets out of
plaintext in the DB and fully out of the frontend/React bundle.

For a multi-customer production deployment, swap this for a managed secrets
store (AWS Secrets Manager, Azure Key Vault, GCP Secret Manager) and store only
a *reference* (secret_metadata) in the `connections` table instead of the
encrypted blob itself — the Connection model's `secret_encrypted` column is
named generically so that swap doesn't require a scACELO change.
"""

from cryptography.fernet import Fernet, InvalidToken

from config import get_settings


class SecretCipher:
    def __init__(self) -> None:
        key = get_settings().SECRET_ENCRYPTION_KEY
        if not key:
            raise RuntimeError(
                "SECRET_ENCRYPTION_KEY is not set. Generate one with:\n"
                "  python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"\n"
                "and set it as an environment variable before starting the backend."
            )
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Could not decrypt stored secret — SECRET_ENCRYPTION_KEY may have changed.") from exc


_cipher: SecretCipher | None = None


def get_cipher() -> SecretCipher:
    global _cipher
    if _cipher is None:
        _cipher = SecretCipher()
    return _cipher
