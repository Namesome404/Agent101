from pathlib import Path


MUSE_DIR = Path(__file__).resolve().parents[1]
MAIN_DIR = MUSE_DIR.parent
SERVER_DIR = MAIN_DIR / "server"
DIGITAL_HUMAN_DIR = MAIN_DIR / "digital-human"

DATA_DIR = MUSE_DIR / "data"
TMP_DIR = MUSE_DIR / "tmp"
UI_DIR = MUSE_DIR / "ui"
VENDOR_DIR = MUSE_DIR / "vendor"
DB_PATH = MUSE_DIR / "muse.db"
ENV_PATH = MUSE_DIR / ".env"
