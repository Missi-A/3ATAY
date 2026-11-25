"""Chiffrement authentifié (AES-256-GCM) des tâches et résultats échangés avec GitHub.

Le chiffrement cache le contenu dans les journaux publics ; l'authentification refuse tout résultat modifié ou forgé."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES, NONCE_BYTES = 32, 12


class SealError(ValueError):
    pass


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode()


def fingerprint(key: str) -> str:
    """Empreinte courte (8 hexadécimaux) pour comparer deux clés sans les montrer ; clé vide : « e3b0c442 »."""
    return hashlib.sha256((key or "").strip().encode()).hexdigest()[:8]


def _cipher(key: str) -> AESGCM:
    try:
        raw = base64.urlsafe_b64decode(key.strip().encode())
    except (binascii.Error, ValueError, AttributeError):
        raw = b""
    if len(raw) != KEY_BYTES:
        raise SealError("Clé de chiffrement invalide.")
    return AESGCM(raw)


def seal(key: str, data: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + _cipher(key).encrypt(nonce, data, None)


def unseal(key: str, blob: bytes) -> bytes:
    cipher = _cipher(key)
    if len(blob) < NONCE_BYTES + 16:
        raise SealError("Données chiffrées incomplètes.")
    try:
        return cipher.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], None)
    except InvalidTag:
        raise SealError("Données altérées ou clé incorrecte.") from None


def main(argv=None) -> int:
    """`python -m flemme.shared.sealed_box seal|unseal ENTREE SORTIE` ; la clé vient de la variable JOB_KEY (jamais d'un argument)."""
    import sys
    from pathlib import Path

    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments == ["fingerprint"]:
        print(fingerprint(os.environ.get("JOB_KEY", "")))
        return 0
    if len(arguments) != 3 or arguments[0] not in ("seal", "unseal"):
        print("Usage : sealed_box seal|unseal ENTREE SORTIE | fingerprint (clé dans JOB_KEY)", file=sys.stderr)
        return 1
    action, source, target = arguments
    try:
        data = Path(source).read_bytes()
        key = os.environ.get("JOB_KEY", "")
        result = seal(key, data) if action == "seal" else unseal(key, data)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(result)
    except (SealError, OSError) as error:
        hint = " : clé incorrecte ou données altérées" if isinstance(error, SealError) else ""
        print(f"Échec : {type(error).__name__}{hint}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
