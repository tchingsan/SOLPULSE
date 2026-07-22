
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from tkinter import Tk, filedialog, messagebox

BASE_DIR = Path(__file__).resolve().parent
TARGET = BASE_DIR / "data" / "trading.db"


def main() -> None:
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    selected = filedialog.askopenfilename(
        title="Sélectionne le fichier trading.db d’une version précédente",
        filetypes=[
            ("Base SQLite SOLPULSE", "*.db"),
            ("Tous les fichiers", "*.*"),
        ],
    )

    if not selected:
        return

    source = Path(selected).resolve()
    if source == TARGET.resolve():
        messagebox.showinfo(
            "SOLPULSE V12.2",
            "Cette base est déjà celle du dossier V12.",
        )
        return

    TARGET.parent.mkdir(parents=True, exist_ok=True)

    # The previous SOLPULSE version must be closed. Checkpoint its WAL before copying the main file.
    try:
        with closing(sqlite3.connect(source, timeout=20)) as connection:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA wal_checkpoint(FULL)")
            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(
                    f"Intégrité de la base source : {integrity}"
                )
    except Exception as error:
        messagebox.showerror(
            "Base V9 indisponible",
            "Ferme toutes les fenêtres SOLPULSE V9 puis réessaie.\n\n"
            + str(error),
        )
        raise SystemExit(1)

    if TARGET.exists():
        backup = TARGET.with_name(
            "trading-before-import-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + ".db"
        )
        shutil.copy2(TARGET, backup)

    for suffix in ("", "-wal", "-shm"):
        Path(str(TARGET) + suffix).unlink(missing_ok=True)

    shutil.copy2(source, TARGET)

    result = subprocess.run(
        [
            sys.executable,
            str(BASE_DIR / "migrate_database.py"),
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        messagebox.showerror(
            "Import échoué",
            result.stderr or result.stdout,
        )
        raise SystemExit(result.returncode)

    messagebox.showinfo(
        "Import terminé",
        "La base précédente a été copiée et migrée vers V12.2.\n\n"
        "Tu peux maintenant lancer "
        "01_START_SOLPULSE_STABLE_V12_2.bat.",
    )


if __name__ == "__main__":
    main()
