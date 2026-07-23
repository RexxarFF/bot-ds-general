from __future__ import annotations

import ast
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CITY_MODULE = ROOT / "modules" / "cities.py"
BANNER_DIR = ROOT / "assets" / "banners" / "cities"

required_banners = {
    "city_application.png",
    "city_moderation.png",
    "city_registry.png",
    "city_management.png",
    "city_notification.png",
    "city_warning.png",
    "city_leadership.png",
    "city_logs.png",
    "city_setup.png",
}

if not CITY_MODULE.is_file():
    raise SystemExit("ERROR: modules/cities.py не найден.")

source = CITY_MODULE.read_text(encoding="utf-8")
ast.parse(source, filename=str(CITY_MODULE))

for forbidden in ("set_thumbnail", ".set_thumbnail(", "http://", "https://"):
    if forbidden in source:
        raise SystemExit(f"ERROR: в city-модуле найден запрещённый фрагмент: {forbidden}")

required_fragments = (
    "attachment://",
    "discord.ui.UserSelect",
    "discord.ui.FileUpload",
    '"mayorId"',
    '"deputyId"',
    '"reviewMessageId"',
    '"reviewScreenshotsMessageId"',
    '"registryThreadId"',
    '"registryMessageId"',
    '"registryScreenshotsMessageId"',
    'on_raw_thread_delete',
    'on_raw_message_delete',
    'on_member_remove',
    'on_member_update',
    'WARNING_COOLDOWN_SECONDS',
    'city_management_banner_path',
    'edit_original_response',
    'MAX_CITY_CITIZENS',
    '"citizenIds"',
    'CityCitizenAddSelect',
    'CityCitizenRemoveView',
    'CityCitizenListView',
    '_panel_publish_lock',
    'only_kind=self.target',
    'CityTransientView',
    'if view is not None',
    'IS_COMPONENTS_V2',
    'replace_incompatible_panel',
    'message.flags.value',
)
for fragment in required_fragments:
    if fragment not in source:
        raise SystemExit(f"ERROR: отсутствует обязательная часть city-модуля: {fragment}")

missing = sorted(name for name in required_banners if not (BANNER_DIR / name).is_file())
if missing:
    raise SystemExit("ERROR: отсутствуют баннеры: " + ", ".join(missing))

for name in sorted(required_banners):
    path = BANNER_DIR / name
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise SystemExit(f"ERROR: {name} не является корректным PNG.")
    width, height = struct.unpack(">II", data[16:24])
    if width < 1000 or height < 600:
        raise SystemExit(f"ERROR: {name} слишком маленький: {width}x{height}.")

requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
if "discord.py==2.7.1" not in requirements:
    raise SystemExit("ERROR: требуется discord.py==2.7.1 для FileUpload в Modal.")

print("OK: city-модуль, локальные баннеры, ID-связи и ограничения проверены.")
