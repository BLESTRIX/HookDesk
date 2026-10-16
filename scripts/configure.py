"""Create local development configuration without printing any secrets."""

import base64
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    raise SystemExit(".env already exists; kept existing configuration")
password = secrets.token_hex(24)
content = (root / ".env.example").read_text()
content = content.replace("replace-with-generated-password", password)
content = content.replace("hookdesk:hookdesk@", f"hookdesk:{password}@")
content = content.replace(
    "generate-a-fernet-key-with-scripts-configure.py",
    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
)
target.write_text(content, encoding="utf-8")
print("Created .env with random database credentials and encryption key")
