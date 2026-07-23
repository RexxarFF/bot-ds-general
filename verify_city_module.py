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
tree = ast.parse(source, filename=str(CITY_MODULE))

for forbidden in ("set_thumbnail", ".set_thumbnail(", "http://", "https://"):
    if forbidden in source:
        raise SystemExit(f"ERROR: в city-модуле найден запрещённый фрагмент: {forbidden}")

required_fragments = (
    "attachment://",
    "discord.ui.LayoutView",
    "discord.ui.Container",
    "discord.ui.MediaGallery",
    "discord.ui.TextDisplay",
    "discord.ui.ActionRow",
    "CityCardLayoutView",
    "_legacy_view_action_rows",
    "controls=controls",
    "discord.ui.UserSelect",
    "discord.ui.FileUpload",
    '"mayorId"',
    '"deputyId"',
    '"reviewMessageId"',
    '"reviewScreenshotsMessageId"',
    '"registryThreadId"',
    '"registryMessageId"',
    '"registryScreenshotsMessageId"',
    "on_raw_thread_delete",
    "on_raw_message_delete",
    "on_member_remove",
    "on_member_update",
    "WARNING_COOLDOWN_SECONDS",
    "city_management_banner_path",
    "edit_original_response",
    "MAX_CITY_CITIZENS",
    '"citizenIds"',
    "CityCitizenAddSelect",
    "CityCitizenRemoveView",
    "CityCitizenListView",
    "_panel_publish_lock",
    "only_kind=self.target",
    "CityTransientView",
)
for fragment in required_fragments:
    if fragment not in source:
        raise SystemExit(f"ERROR: отсутствует обязательная часть city-модуля: {fragment}")

# Components V2 нельзя смешивать с обычным содержимым/Embed. Допускаются лишь
# пустые embeds=[] при миграции/редактировании старого сообщения.
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    # Проверяем только прямые вызовы Discord API. Внутренние helper-функции
    # законно принимают исходный Embed как данные и преобразуют его в TextDisplay.
    method_name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
    if method_name not in {"send", "send_message", "edit", "edit_message", "edit_original_response", "create_thread"}:
        continue
    keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    if "view" not in keywords:
        continue
    if "embed" in keywords:
        raise SystemExit("ERROR: найден прямой Discord-вызов с одновременными view= и embed=.")
    embeds_node = keywords.get("embeds")
    if embeds_node is not None:
        if not isinstance(embeds_node, (ast.List, ast.Tuple)) or embeds_node.elts:
            raise SystemExit("ERROR: найден непустой embeds= вместе с Components V2 view=.")
    content_node = keywords.get("content")
    if content_node is not None and not (
        isinstance(content_node, ast.Constant) and content_node.value is None
    ):
        raise SystemExit("ERROR: найден непустой content= вместе с Components V2 view=.")

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
    raise SystemExit("ERROR: требуется discord.py==2.7.1 для FileUpload и Components V2.")

print("OK: city-модуль использует одну полноширинную Components V2-карточку без смешивания с Embed.")
