import hashlib
from cryptography.fernet import Fernet, InvalidToken
from src.config import MASTER_ENCRYPTION_KEY

# Initialize Fernet cipher once at module load
try:
    _cipher = Fernet(MASTER_ENCRYPTION_KEY.encode("utf-8"))
except Exception as e:
    raise RuntimeError(
        "Invalid MASTER_ENCRYPTION_KEY. Generate one using: "
        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    ) from e


def encrypt_api_key(raw_key: str) -> str:
    """Encrypts a plaintext API key into a URL-safe base64 ciphertext string."""
    clean_key = raw_key.strip()
    return _cipher.encrypt(clean_key.encode("utf-8")).decode("utf-8")


def decrypt_api_key(encrypted_key: str) -> str:
    """Decrypts a Fernet ciphertext string back into the raw API key in memory."""
    try:
        return _cipher.decrypt(encrypted_key.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Failed to decrypt API key. Master key may have changed or data is corrupted.") from e


def hash_passcode(raw_passcode: str) -> str:
    """Computes the SHA-256 hex digest of a passcode to match PostgreSQL encode(digest(..., 'sha256'), 'hex')."""
    clean_code = raw_passcode.strip()
    return hashlib.sha256(clean_code.encode("utf-8")).hexdigest()