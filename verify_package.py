from __future__ import annotations

import ast
from pathlib import Path

root = Path(__file__).resolve().parent
required = [
    "main.py",
    "config.py",
    "settings_store.py",
    "rcon_client.py",
    "modules/unified_store.py",
    "modules/components.py",
    "modules/community.py",
    "modules/government.py",
    "requirements.txt",
    ".env.example",
]

missing = [name for name in required if not (root / name).exists()]
if missing:
    raise SystemExit("Отсутствуют файлы: " + ", ".join(missing))

for path in root.rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

print("OK: структура и синтаксис Python проверены.")
