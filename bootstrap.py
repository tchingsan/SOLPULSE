from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REQUIREMENTS = BASE_DIR / "requirements.txt"
MARKER = BASE_DIR / ".venv" / ".solpulse_requirements.sha256"


def main() -> int:
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if MARKER.exists() and MARKER.read_text(encoding="utf-8").strip() == digest:
        print("Dépendances déjà prêtes.")
        return 0

    print("Installation/mise à jour des dépendances SOLPULSE...")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "-r",
            str(REQUIREMENTS),
        ],
        cwd=BASE_DIR,
    )
    if result.returncode != 0:
        return result.returncode

    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(digest, encoding="utf-8")
    print("Dépendances validées.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
