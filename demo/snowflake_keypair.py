"""Create a key pair for signing in to Snowflake without a password (key-pair authentication).

    python demo/snowflake_keypair.py YOUR_SNOWFLAKE_USER

It writes the private key to ~/.l2c/snowflake_rsa_key.p8 (keep it private; it is the password)
and prints one SQL statement. Run that statement in a Snowflake worksheet; then put the key file's
path in the destination's "Private key file" box. Run it again to replace the key.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit("Usage: python demo/snowflake_keypair.py YOUR_SNOWFLAKE_USER")
    user = argv[1].strip().upper()
    folder = Path.home() / ".l2c"
    folder.mkdir(exist_ok=True)
    key_path = folder / "snowflake_rsa_key.p8"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with contextlib.suppress(OSError):  # Windows: the file is already private to your profile
        key_path.chmod(0o600)
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    body = "".join(line for line in public.splitlines() if "-----" not in line)

    print(f"Private key written to: {key_path}")
    print("\n1. Run this in a Snowflake worksheet (Projects > Worksheets > +):\n")
    print(f"ALTER USER {user} SET RSA_PUBLIC_KEY='{body}';")
    print("\n2. In the accelerator's Snowflake destination, leave Password empty and set")
    print(f"   Private key file = {key_path}")


if __name__ == "__main__":
    main(sys.argv)
