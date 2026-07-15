import os
from pathlib import Path


def _default_data_dir() -> Path:
    env_dir = os.environ.get("DATA_DIR")
    if env_dir:
        return Path(env_dir)
    if os.path.exists("/.dockerenv"):
        return Path("/app/data")
    return Path.home() / "Library" / "Application Support" / "ChildrensBookGenerator"


DATA_DIR = _default_data_dir()
UPLOADS_DIR = DATA_DIR / "uploads"
BOOKS_DIR = DATA_DIR / "books"
DB_PATH = DATA_DIR / "app.db"

for _d in (DATA_DIR, UPLOADS_DIR, BOOKS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("PORT", 9005))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
KIE_AI_API_KEY = os.environ.get("KIE_AI_API_KEY")

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY")
R2_BUCKET = os.environ.get("R2_BUCKET")
R2_PUBLIC_DOMAIN = os.environ.get("R2_PUBLIC_DOMAIN")
