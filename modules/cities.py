from __future__ import annotations

import asyncio
import copy
import logging
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import discord
from discord import app_commands
from discord.ext import commands

from .components import build_framed_view
from .unified_store import AssetRef, UnifiedDiscordStore, UnifiedState

log = logging.getLogger("funfernus-cities")

CITY_ID_RE = re.compile(r"CITY-\d{4,}")
DISCORD_ID_RE = re.compile(r"\d{15,22}")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_SCREENSHOTS = 10
MAX_CITY_CITIZENS = 100
WARNING_COOLDOWN_SECONDS = 45.0

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_BANNER_DIR = PROJECT_ROOT / "assets" / "banners" / "cities"
CITY_UPLOAD_DIR = PROJECT_ROOT / "assets" / "city_uploads"

STATIC_BANNERS: dict[str, str] = {
    "application": "city_application.png",
    "moderation": "city_moderation.png",
    "registry": "city_registry.png",
    "management": "city_management.png",
    "notification": "city_notification.png",
    "warning": "city_warning.png",
    "leadership": "city_leadership.png",
    "logs": "city_logs.png",
    "setup": "city_setup.png",
}

_city_locks: dict[tuple[int, str], asyncio.Lock] = {}
_panel_publish_locks: dict[int, asyncio.Lock] = {}
_warning_cooldowns: dict[tuple[int, int], float] = {}
_bot_deleted_messages: set[int] = set()
_missing_asset_log_cache: set[tuple[int, str, str]] = set()


def _lock(guild_id: int, city_id: str) -> asyncio.Lock:
    return _city_locks.setdefault((guild_id, city_id), asyncio.Lock())


def _panel_publish_lock(guild_id: int) -> asyncio.Lock:
    # Сериализует публикацию панелей одного сервера. Это не даёт двум
    # одновременным кликам отправлять несколько PATCH-запросов к одним и тем
    # же сообщениям и заметно снижает вероятность Discord rate limit (429).
    return _panel_publish_locks.setdefault(guild_id, asyncio.Lock())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, fallback: str = "—") -> str:
    result = str(value or "").strip()
    return result or fallback


def _trim(value: Any, limit: int = 1024, fallback: str = "—") -> str:
    result = _text(value, fallback)
    if len(result) <= limit:
        return result
    return result[: max(1, limit - 1)].rstrip() + "…"


def _city_id(message: discord.Message | None) -> str:
    if message is None:
        return ""
    values: list[str] = []
    if message.content:
        values.append(message.content)
    for embed in message.embeds:
        if embed.title:
            values.append(embed.title)
        if embed.footer and embed.footer.text:
            values.append(embed.footer.text)
    for value in values:
        match = CITY_ID_RE.search(value)
        if match:
            return match.group()
    return ""


def _discord_ids(value: str) -> list[int]:
    result: list[int] = []
    for item in DISCORD_ID_RE.findall(value or ""):
        number = int(item)
        if number not in result:
            result.append(number)
    return result


def _mayor_id(city: dict[str, Any]) -> int:
    return int(city.get("mayorId", city.get("mayor_id", 0)) or 0)


def _deputy_id(city: dict[str, Any]) -> int:
    return int(city.get("deputyId", city.get("deputy_id", 0)) or 0)


def _citizen_ids(city: dict[str, Any]) -> list[int]:
    raw = city.get("citizenIds", city.get("citizen_ids", []))
    if not isinstance(raw, list):
        return []
    leaders = {_mayor_id(city), _deputy_id(city)}
    result: list[int] = []
    for item in raw:
        try:
            user_id = int(item)
        except (TypeError, ValueError):
            continue
        if user_id <= 0 or user_id in leaders or user_id in result:
            continue
        result.append(user_id)
    return result[:MAX_CITY_CITIZENS]


def _set_citizen_ids(city: dict[str, Any], values: Iterable[int]) -> None:
    leaders = {_mayor_id(city), _deputy_id(city)}
    clean: list[int] = []
    for item in values:
        try:
            user_id = int(item)
        except (TypeError, ValueError):
            continue
        if user_id <= 0 or user_id in leaders or user_id in clean:
            continue
        clean.append(user_id)
        if len(clean) >= MAX_CITY_CITIZENS:
            break
    city["citizenIds"] = clean
    city["citizen_ids"] = list(clean)


def _citizen_absent_ids(city: dict[str, Any]) -> set[int]:
    raw = city.get("citizenAbsentIds", city.get("citizen_absent_ids", []))
    if not isinstance(raw, list):
        return set()
    valid = set(_citizen_ids(city))
    result: set[int] = set()
    for item in raw:
        try:
            user_id = int(item)
        except (TypeError, ValueError):
            continue
        if user_id in valid:
            result.add(user_id)
    return result


def _set_citizen_absent_ids(city: dict[str, Any], values: Iterable[int]) -> None:
    valid = set(_citizen_ids(city))
    clean = sorted({int(item) for item in values if str(item).isdigit() and int(item) in valid})
    city["citizenAbsentIds"] = clean
    city["citizen_absent_ids"] = list(clean)


def _citizen_preview(city: dict[str, Any], *, limit: int = 15) -> str:
    citizens = _citizen_ids(city)
    if not citizens:
        return "Горожане пока не добавлены."
    absent = _citizen_absent_ids(city)
    lines = [
        f"{index}. <@{user_id}> — `{user_id}`" + (" • ⚠️ покинул сервер" if user_id in absent else "")
        for index, user_id in enumerate(citizens[:limit], 1)
    ]
    if len(citizens) > limit:
        lines.append(f"…и ещё **{len(citizens) - limit}**")
    return _trim("\n".join(lines), 1024)


def _find_person_city(
    state: UnifiedState,
    user_id: int,
    *,
    exclude: str = "",
) -> tuple[str, dict[str, Any]] | None:
    for city_id, city in state.cities.items():
        if city_id == exclude or city.get("status") not in {"pending", "approved"}:
            continue
        _normalize_city(city)
        if user_id in {_mayor_id(city), _deputy_id(city)} or user_id in _citizen_ids(city):
            return city_id, city
    return None


def _set_leaders(city: dict[str, Any], mayor_id: int, deputy_id: int) -> None:
    city["mayorId"] = int(mayor_id)
    city["deputyId"] = int(deputy_id)
    # Старые ключи оставлены для безопасного обновления уже созданных данных.
    city["mayor_id"] = int(mayor_id)
    city["deputy_id"] = int(deputy_id)


def _get_message_id(city: dict[str, Any], camel: str, snake: str) -> int:
    return int(city.get(camel, city.get(snake, 0)) or 0)


def _set_message_id(city: dict[str, Any], camel: str, snake: str, value: int) -> None:
    city[camel] = int(value)
    city[snake] = int(value)


def _get_paths(city: dict[str, Any], camel: str, snake: str) -> list[str]:
    raw = city.get(camel, city.get(snake, []))
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def _set_paths(city: dict[str, Any], camel: str, snake: str, values: Iterable[str]) -> None:
    clean = [str(item) for item in values if str(item).strip()]
    city[camel] = clean
    city[snake] = list(clean)


def _project_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    candidate = path if path.is_absolute() else PROJECT_ROOT / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        # JSON не должен позволять читать или удалять файлы за пределами проекта.
        return PROJECT_ROOT / ".invalid_city_asset_path"
    return resolved


def _safe_extension(filename: str, content_type: str = "") -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return mapping.get((content_type or "").lower(), ".png")


def _validate_attachment(attachment: discord.Attachment) -> None:
    name = attachment.filename.lower()
    content_type = (attachment.content_type or "").lower()
    suffix = Path(name).suffix.lower()
    if not content_type.startswith("image/") and suffix not in IMAGE_EXTENSIONS:
        raise ValueError("поддерживаются только PNG, JPG, JPEG, WEBP и GIF")
    if attachment.size > MAX_IMAGE_SIZE:
        raise ValueError("максимальный размер одного изображения — 10 МБ")


async def _save_attachment_local(attachment: discord.Attachment, destination: Path) -> str:
    _validate_attachment(attachment)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = await attachment.read()
    if len(data) > MAX_IMAGE_SIZE:
        raise ValueError("изображение превышает 10 МБ")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return _project_relative(destination)


def _delete_local_paths(paths: Iterable[str]) -> None:
    for value in paths:
        try:
            path = _resolve_project_path(value)
            if path.is_file():
                path.unlink()
        except OSError:
            log.exception("Не удалось удалить локальный файл города: %s", value)


def _city_upload_folder(guild_id: int, city_id: str) -> Path:
    return CITY_UPLOAD_DIR / str(guild_id) / city_id


def _existing_local_paths(values: Iterable[str]) -> list[Path]:
    result: list[Path] = []
    for value in values:
        path = _resolve_project_path(value)
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            result.append(path)
    return result


def _static_banner_path(kind: str) -> Path:
    return STATIC_BANNER_DIR / STATIC_BANNERS.get(kind, STATIC_BANNERS["notification"])


def _city_custom_banner_path(city: dict[str, Any]) -> Path | None:
    raw = str(city.get("bannerPath", city.get("banner_path", "")) or "").strip()
    if not raw:
        return None
    path = _resolve_project_path(raw)
    return path if path.is_file() else None


def _banner_path(kind: str, city: dict[str, Any] | None = None, state: UnifiedState | None = None) -> Path:
    if state is not None and city is None and kind in {"application", "management"}:
        option_key = (
            "city_application_banner_path"
            if kind == "application"
            else "city_management_banner_path"
        )
        custom = str(state.options.get(option_key, "") or "").strip()
        if custom:
            path = _resolve_project_path(custom)
            if path.is_file():
                return path
    if city is not None and kind in {"registry", "management"}:
        custom = _city_custom_banner_path(city)
        if custom is not None:
            return custom
    path = _static_banner_path(kind)
    if path.is_file():
        return path
    fallback = _static_banner_path("notification")
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(f"Не найден локальный баннер системы городов: {path}")


def _banner_file_and_embed(
    kind: str,
    *,
    city: dict[str, Any] | None = None,
    state: UnifiedState | None = None,
) -> tuple[discord.Embed, discord.File]:
    path = _banner_path(kind, city, state)
    suffix = path.suffix.lower() if path.suffix.lower() in IMAGE_EXTENSIONS else ".png"
    filename = f"funfernus_city_{kind}_banner{suffix}"
    banner_embed = discord.Embed(color=int((state.options if state else {}).get("accent_color", 0x19B9D1)))
    # В Python-версии discord.py это прямой эквивалент EmbedBuilder#setImage().
    banner_embed.set_image(url=f"attachment://{filename}")
    return banner_embed, discord.File(path, filename=filename)


def _message_payload(
    kind: str,
    content_embed: discord.Embed,
    *,
    city: dict[str, Any] | None = None,
    state: UnifiedState | None = None,
) -> tuple[list[discord.Embed], discord.File]:
    banner_embed, banner_file = _banner_file_and_embed(kind, city=city, state=state)
    # Два Embed в одном сообщении нужны, чтобы полноразмерный setImage был над заголовком карточки.
    return [banner_embed, content_embed], banner_file


def _simple_embed(
    title: str,
    description: str,
    *,
    color: int = 0x19B9D1,
    footer: str = "FunFernus • Система городов",
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    if footer:
        embed.set_footer(text=footer)
    return embed


async def _send_interaction_card(
    interaction: discord.Interaction,
    *,
    kind: str,
    title: str,
    description: str,
    state: UnifiedState | None = None,
    city: dict[str, Any] | None = None,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
    ephemeral: bool = True,
    followup: bool = False,
    color: int | None = None,
) -> None:
    accent = color if color is not None else int((state.options if state else {}).get("accent_color", 0x19B9D1))
    content = _simple_embed(title, description, color=accent)

    def build_send_kwargs() -> dict[str, Any]:
        embeds, banner_file = _message_payload(kind, content, city=city, state=state)
        result: dict[str, Any] = {
            "embeds": embeds,
            "file": banner_file,
            "ephemeral": ephemeral,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        # discord.py 2.7 не принимает явное view=None в send_message/followup.send.
        # Параметр необходимо полностью убрать из вызова, когда компонентов нет.
        if view is not None:
            result["view"] = view
        return result

    if followup:
        # После defer(thinking=True) завершаем именно исходный ответ. Иначе
        # Discord продолжает показывать «бот думает», даже если панель обновлена.
        embeds, banner_file = _message_payload(kind, content, city=city, state=state)
        edit_kwargs: dict[str, Any] = {
            "content": None,
            "embeds": embeds,
            "attachments": [banner_file],
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if view is not None:
            edit_kwargs["view"] = view
        try:
            await interaction.edit_original_response(**edit_kwargs)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # Файл мог быть закрыт после неудачного edit, поэтому payload
            # создаётся заново перед резервной отправкой followup-сообщения.
            await interaction.followup.send(**build_send_kwargs())
            return

    send_kwargs = build_send_kwargs()
    if interaction.response.is_done():
        await interaction.followup.send(**send_kwargs)
    else:
        await interaction.response.send_message(**send_kwargs)


async def _notify_component_error(
    interaction: discord.Interaction,
    error: Exception,
    *,
    context: str,
) -> None:
    log.error(
        "Ошибка Discord-компонента системы городов (%s): %s",
        context,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    text = "❌ Не удалось выполнить действие. Ошибка записана в лог. Повторите попытку один раз."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.exception("Не удалось показать пользователю уведомление об ошибке city-компонента")


class CityTransientView(discord.ui.View):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        await _notify_component_error(
            interaction,
            error,
            context=f"{item.__class__.__name__}",
        )


async def _send_user_card(
    user: discord.abc.Messageable,
    *,
    kind: str,
    embed: discord.Embed,
    state: UnifiedState | None = None,
    city: dict[str, Any] | None = None,
) -> discord.Message:
    embeds, file = _message_payload(kind, embed, city=city, state=state)
    return await user.send(embeds=embeds, file=file, allowed_mentions=discord.AllowedMentions.none())


async def _send_channel_card(
    channel: discord.abc.Messageable,
    *,
    kind: str,
    embed: discord.Embed,
    state: UnifiedState,
    city: dict[str, Any] | None = None,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
    content: str | None = None,
) -> discord.Message:
    embeds, file = _message_payload(kind, embed, city=city, state=state)
    kwargs: dict[str, Any] = {
        "content": content,
        "embeds": embeds,
        "file": file,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if view is not None:
        kwargs["view"] = view
    return await channel.send(**kwargs)


async def _edit_message_card(
    message: discord.Message,
    *,
    kind: str,
    embed: discord.Embed,
    state: UnifiedState,
    city: dict[str, Any] | None = None,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
    content: str | None = None,
) -> discord.Message:
    embeds, file = _message_payload(kind, embed, city=city, state=state)
    kwargs: dict[str, Any] = {
        "content": content,
        "embeds": embeds,
        "attachments": [file],
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if view is not None:
        kwargs["view"] = view
    return await message.edit(**kwargs)


def _is_core_admin(user: discord.abc.User, guild: discord.Guild, admin_ids: set[int]) -> bool:
    if guild.owner_id == user.id or user.id in admin_ids:
        return True
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


def _is_city_staff_member(
    user: discord.abc.User,
    guild: discord.Guild,
    state: UnifiedState,
    admin_ids: set[int],
) -> bool:
    if _is_core_admin(user, guild, admin_ids):
        return True
    if not isinstance(user, discord.Member):
        return False
    staff_roles = set(int(item) for item in state.roles.get("city_staff", []) if str(item).isdigit())
    return bool(staff_roles.intersection(role.id for role in user.roles))


def _allowed_bot_ids(state: UnifiedState) -> set[int]:
    raw = state.options.get("city_allowed_bot_ids", [])
    if not isinstance(raw, list):
        return set()
    return {int(item) for item in raw if str(item).isdigit()}


def _refresh_allowed_writers(
    guild: discord.Guild,
    state: UnifiedState,
    city: dict[str, Any],
    admin_ids: set[int],
    bot_user_id: int = 0,
) -> list[int]:
    allowed = {_mayor_id(city), _deputy_id(city), guild.owner_id, *admin_ids, *_allowed_bot_ids(state)}
    if bot_user_id:
        allowed.add(bot_user_id)
    staff_roles = set(int(item) for item in state.roles.get("city_staff", []) if str(item).isdigit())
    for member in guild.members:
        if member.guild_permissions.administrator or staff_roles.intersection(role.id for role in member.roles):
            allowed.add(member.id)
    result = sorted(item for item in allowed if item > 0)
    city["allowedWriterIds"] = result
    city["allowed_writer_ids"] = list(result)
    city["allowedRoleIds"] = sorted(staff_roles)
    city["allowed_role_ids"] = sorted(staff_roles)
    return result


def _can_write_city_thread(
    message: discord.Message,
    guild: discord.Guild,
    state: UnifiedState,
    city: dict[str, Any],
    admin_ids: set[int],
    bot_user_id: int,
) -> bool:
    if message.author.id == bot_user_id:
        return True
    if message.author.bot:
        return message.author.id in _allowed_bot_ids(state)
    if message.author.id in {_mayor_id(city), _deputy_id(city)}:
        return True
    # allowedWriterIds хранится в JSON как снимок разрешённых ID для аудита и восстановления,
    # но не используется как самостоятельный источник прав. Иначе пользователь, у которого
    # сняли роль модератора, мог бы оставаться разрешённым до следующей синхронизации.
    return _is_city_staff_member(message.author, guild, state, admin_ids)


def _status_text(status: str) -> str:
    return {
        "pending": "Ожидает рассмотрения",
        "approved": "Город зарегистрирован",
        "rejected": "Заявка отклонена",
    }.get(status, status or "Неизвестно")


def _status_color(status: str, accent: int) -> int:
    return {
        "pending": 0xF2B84B,
        "approved": 0x59B77A,
        "rejected": 0xD85C5C,
    }.get(status, accent)


def _leader_text(city: dict[str, Any], leader: str) -> str:
    user_id = _mayor_id(city) if leader == "mayor" else _deputy_id(city)
    present = bool(city.get(f"{leader}Present", city.get(f"{leader}_present", True)))
    if not user_id:
        return "Не назначен"
    return f"<@{user_id}>\n`ID: {user_id}`" + ("" if present else "\n⚠️ Покинул сервер")


def _registry_status(city: dict[str, Any]) -> str:
    status = str(city.get("registryStatus", city.get("registry_status", "not_created")) or "not_created")
    return {
        "active": "✅ Публикация доступна",
        "deleted": "❌ Публикация удалена",
        "message_deleted": "⚠️ Основная карточка удалена",
        "screenshots_deleted": "⚠️ Сообщение со скриншотами удалено",
        "not_created": "Не создана",
        "unavailable": "⚠️ Публикация недоступна",
    }.get(status, status)


def _find_city_for_mayor(state: UnifiedState, mayor_id: int) -> tuple[str, dict[str, Any]] | None:
    for city_id, city in state.cities.items():
        _normalize_city(city)
        if city.get("status") == "approved" and _mayor_id(city) == mayor_id:
            return city_id, city
    return None


def _find_city_by_thread(state: UnifiedState, thread_id: int) -> tuple[str, dict[str, Any]] | None:
    for city_id, city in state.cities.items():
        if _get_message_id(city, "registryThreadId", "registry_thread_id") == thread_id:
            return city_id, city
    return None


def _has_active_city(state: UnifiedState, mayor_id: int, *, exclude: str = "") -> bool:
    for city_id, city in state.cities.items():
        if city_id == exclude:
            continue
        if _mayor_id(city) == mayor_id and city.get("status") in {"pending", "approved"}:
            return True
    return False


def _name_taken(state: UnifiedState, name: str, *, exclude: str = "") -> bool:
    normalized = name.casefold().strip()
    for city_id, city in state.cities.items():
        if city_id == exclude or city.get("status") == "rejected":
            continue
        if str(city.get("name", "")).casefold().strip() == normalized:
            return True
    return False


def _normalize_city(city: dict[str, Any]) -> None:
    _set_leaders(city, _mayor_id(city), _deputy_id(city))
    for camel, snake in (
        ("reviewMessageId", "review_message_id"),
        ("reviewScreenshotsMessageId", "review_screenshots_message_id"),
        ("registryThreadId", "registry_thread_id"),
        ("registryMessageId", "registry_message_id"),
        ("registryScreenshotsMessageId", "registry_screenshots_message_id"),
    ):
        _set_message_id(city, camel, snake, _get_message_id(city, camel, snake))
    _set_paths(city, "screenshotPaths", "screenshot_paths", _get_paths(city, "screenshotPaths", "screenshot_paths"))
    banner = str(city.get("bannerPath", city.get("banner_path", "")) or "")
    city["bannerPath"] = banner
    city["banner_path"] = banner
    city.setdefault("allowedWriterIds", list(city.get("allowed_writer_ids", [])))
    city.setdefault("allowed_writer_ids", list(city.get("allowedWriterIds", [])))
    city.setdefault("allowedRoleIds", list(city.get("allowed_role_ids", [])))
    city.setdefault("allowed_role_ids", list(city.get("allowedRoleIds", [])))
    _set_citizen_ids(city, _citizen_ids(city))
    _set_citizen_absent_ids(city, _citizen_absent_ids(city))
    history = city.get("citizenHistory", city.get("citizen_history", []))
    if not isinstance(history, list):
        history = []
    city["citizenHistory"] = history
    city["citizen_history"] = history
    city.setdefault("mayorPresent", bool(city.get("mayor_present", True)))
    city.setdefault("deputyPresent", bool(city.get("deputy_present", True)))
    city["mayor_present"] = bool(city["mayorPresent"])
    city["deputy_present"] = bool(city["deputyPresent"])
    city.setdefault("registryStatus", str(city.get("registry_status", "not_created")))
    city["registry_status"] = str(city["registryStatus"])
    city.setdefault("question_history", [])
    city.setdefault("active_question", {})
    # Устаревшие URL намеренно не используются для отображения изображений.
    city["screenshots"] = []
    city["banner_url"] = ""


async def _text_channel(bot: commands.Bot, channel_id: int) -> discord.TextChannel | None:
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    try:
        fetched = await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return fetched if isinstance(fetched, discord.TextChannel) else None


async def _forum_channel(bot: commands.Bot, channel_id: int) -> discord.ForumChannel | None:
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.ForumChannel):
        return channel
    try:
        fetched = await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return fetched if isinstance(fetched, discord.ForumChannel) else None


async def _thread_channel(bot: commands.Bot, channel_id: int) -> discord.Thread | None:
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.Thread):
        return channel
    try:
        fetched = await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return fetched if isinstance(fetched, discord.Thread) else None


async def _member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    if not user_id:
        return None
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _user(bot: commands.Bot, user_id: int) -> discord.User | None:
    if not user_id:
        return None
    user = bot.get_user(user_id)
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _send_city_log(
    bot: commands.Bot,
    state: UnifiedState,
    *,
    title: str,
    description: str,
    city_id: str = "",
    city: dict[str, Any] | None = None,
    color: int = 0x5865F2,
) -> None:
    channel = await _text_channel(bot, int(state.channels.get("city_logs", 0)))
    footer = "FunFernus • Логи городов" + (f" • {city_id}" if city_id else "")
    embed = _simple_embed(title, description, color=color, footer=footer)
    if city_id:
        embed.add_field(name="ID города", value=f"`{city_id}`", inline=True)
    try:
        if channel is None:
            log.warning("Канал логов городов недоступен: %s — %s", title, description)
            return
        await _send_channel_card(channel, kind="logs", embed=embed, state=state, city=city)
    except Exception:
        log.exception("Не удалось отправить лог системы городов: %s", title)


async def _log_missing_local_asset_once(
    bot: commands.Bot,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
    asset_type: str,
) -> None:
    key = (state.guild_id, city_id, asset_type)
    if key in _missing_asset_log_cache:
        return
    _missing_asset_log_cache.add(key)
    await _send_city_log(
        bot,
        state,
        title="⚠️ Локальный файл города отсутствует",
        description=(
            f"Не найден локальный файл типа **{asset_type}** для города **{city.get('name', city_id)}**. "
            "Бот продолжил работу и использовал безопасный резервный визуал либо пропустил вложение."
        ),
        city_id=city_id,
        city=city,
        color=0xF2B84B,
    )


def city_review_embed(city_id: str, city: dict[str, Any], state: UnifiedState) -> discord.Embed:
    _normalize_city(city)
    status = str(city.get("status", "pending"))
    accent = int(state.options.get("accent_color", 0x19B9D1))
    embed = discord.Embed(
        title=f"🏰 Заявка на регистрацию города • {city_id}",
        description=f"**Статус:** {_status_text(status)}",
        color=_status_color(status, accent),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Название", value=_trim(city.get("name")), inline=True)
    embed.add_field(name="Мэр", value=_leader_text(city, "mayor"), inline=True)
    embed.add_field(name="Заместитель", value=_leader_text(city, "deputy"), inline=True)
    embed.add_field(name="Архитектурный стиль", value=_trim(city.get("style")), inline=False)
    embed.add_field(name="Координаты • Верхний мир", value=_trim(city.get("overworld_coords")), inline=True)
    embed.add_field(name="Координаты • Нижний мир", value=_trim(city.get("nether_coords")), inline=True)
    embed.add_field(name="Описание города", value=_trim(city.get("description")), inline=False)
    paths = _get_paths(city, "screenshotPaths", "screenshot_paths")
    embed.add_field(
        name="Скриншоты первых построек",
        value=f"📎 Отправлены отдельным сообщением: **{len(paths)} шт.**" if paths else "Не приложены.",
        inline=False,
    )
    embed.add_field(name="Заявку отправил", value=f"<@{int(city.get('applicant_id', 0))}>\n`ID: {int(city.get('applicant_id', 0))}`", inline=False)

    history = city.get("question_history", [])
    if history:
        latest = history[-1]
        embed.add_field(name="Последний вопрос администрации", value=_trim(latest.get("question"), 700), inline=False)
        embed.add_field(name="Ответ мэра", value=_trim(latest.get("answer"), 700, "Ответ ещё не получен."), inline=False)

    if status == "approved":
        embed.add_field(name=f"Горожане • {len(_citizen_ids(city))}", value=_citizen_preview(city), inline=False)
        embed.add_field(name="Одобрил", value=f"<@{int(city.get('reviewer_id', 0))}>", inline=True)
        thread_id = _get_message_id(city, "registryThreadId", "registry_thread_id")
        if thread_id:
            embed.add_field(name="Публикация реестра", value=f"<#{thread_id}>\n{_registry_status(city)}", inline=True)
    elif status == "rejected":
        embed.add_field(name="Причина отказа", value=_trim(city.get("rejection_reason")), inline=False)
        embed.add_field(name="Отклонил", value=f"<@{int(city.get('reviewer_id', 0))}>", inline=True)

    embed.set_footer(text=f"FunFernus • {city_id} • {_status_text(status)}")
    return embed


def city_registry_embed(city_id: str, city: dict[str, Any], state: UnifiedState) -> discord.Embed:
    _normalize_city(city)
    accent = int(state.options.get("accent_color", 0x19B9D1))
    embed = discord.Embed(
        title=f"🏰 {_trim(city.get('name'), 200)}",
        description=_trim(city.get("description"), 4096),
        color=accent,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Мэр", value=_leader_text(city, "mayor"), inline=True)
    embed.add_field(name="Заместитель мэра", value=_leader_text(city, "deputy"), inline=True)
    embed.add_field(name="Архитектурный стиль", value=_trim(city.get("style")), inline=False)
    embed.add_field(name="Верхний мир", value=_trim(city.get("overworld_coords")), inline=True)
    embed.add_field(name="Нижний мир и метро", value=_trim(city.get("nether_coords")), inline=True)
    paths = _get_paths(city, "screenshotPaths", "screenshot_paths")
    embed.add_field(
        name="Скриншоты города",
        value=f"📎 Находятся в следующем сообщении: **{len(paths)} шт.**" if paths else "Не приложены.",
        inline=False,
    )
    embed.add_field(
        name=f"Горожане • {len(_citizen_ids(city))}",
        value=_citizen_preview(city),
        inline=False,
    )
    embed.add_field(
        name="Кто может писать в этой публикации",
        value="Мэр, заместитель мэра, настроенная администрация и разрешённые боты. Остальные сообщения удаляются автоматически.",
        inline=False,
    )
    embed.set_footer(text=f"Официальный реестр FunFernus • ID города: {city_id}")
    return embed


def city_management_embed(city_id: str, city: dict[str, Any], state: UnifiedState) -> discord.Embed:
    _normalize_city(city)
    accent = int(state.options.get("accent_color", 0x19B9D1))
    embed = discord.Embed(
        title=f"⚙️ Управление городом • {_trim(city.get('name'), 180)}",
        description=(
            "Изменения сохраняются по Discord ID и сразу синхронизируются с официальной публикацией. "
            "Смена мэра и заместителя доступна только администрации через отдельную панель."
        ),
        color=accent,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="ID города", value=f"`{city_id}`", inline=True)
    embed.add_field(name="Мэр", value=_leader_text(city, "mayor"), inline=True)
    embed.add_field(name="Заместитель", value=_leader_text(city, "deputy"), inline=True)
    embed.add_field(name="Стиль", value=_trim(city.get("style"), 500), inline=False)
    embed.add_field(name="Верхний мир", value=_trim(city.get("overworld_coords"), 500), inline=True)
    embed.add_field(name="Нижний мир", value=_trim(city.get("nether_coords"), 500), inline=True)
    embed.add_field(name="Главный баннер", value="Установлен локальным файлом" if city.get("bannerPath") else "Используется системный баннер", inline=False)
    embed.add_field(
        name=f"Горожане • {len(_citizen_ids(city))}",
        value=_citizen_preview(city),
        inline=False,
    )
    embed.add_field(name="Публикация реестра", value=_registry_status(city), inline=False)
    embed.set_footer(text="FunFernus • Панель мэра")
    return embed


def _local_screenshot_files(city: dict[str, Any]) -> list[discord.File]:
    paths = _existing_local_paths(_get_paths(city, "screenshotPaths", "screenshot_paths"))[:MAX_SCREENSHOTS]
    files: list[discord.File] = []
    for index, path in enumerate(paths, 1):
        suffix = path.suffix.lower() if path.suffix.lower() in IMAGE_EXTENSIONS else ".png"
        files.append(discord.File(path, filename=f"city_screenshot_{index:02d}{suffix}"))
    return files


async def _send_screenshot_message(
    channel: discord.abc.Messageable,
    city_id: str,
    city: dict[str, Any],
    *,
    context: str,
) -> discord.Message | None:
    files = _local_screenshot_files(city)
    if not files:
        return None
    return await channel.send(
        content=f"📸 **Скриншоты города `{city_id}` • {context}**",
        files=files,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _edit_screenshot_message(
    channel: discord.abc.Messageable,
    message_id: int,
    city_id: str,
    city: dict[str, Any],
    *,
    context: str,
) -> tuple[discord.Message | None, str]:
    files = _local_screenshot_files(city)
    if not files:
        if message_id and hasattr(channel, "fetch_message"):
            try:
                old = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                _bot_deleted_messages.add(old.id)
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        return None, "Скриншотов нет."
    if message_id and hasattr(channel, "fetch_message"):
        try:
            message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
            edited = await message.edit(
                content=f"📸 **Скриншоты города `{city_id}` • {context}**",
                attachments=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return edited, "Сообщение со скриншотами обновлено."
        except discord.NotFound:
            pass
        except discord.Forbidden:
            return None, "Боту не хватает прав для обновления скриншотов."
        except discord.HTTPException as exc:
            return None, f"Discord не обновил скриншоты: {exc}"
    try:
        message = await channel.send(
            content=f"📸 **Скриншоты города `{city_id}` • {context}**",
            files=files,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return message, "Сообщение со скриншотами создано заново."
    except discord.HTTPException as exc:
        return None, f"Discord не отправил скриншоты: {exc}"


async def _migrate_legacy_city_assets(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    guild: discord.Guild,
    state: UnifiedState,
) -> bool:
    """Переносит старые Discord-вложения в локальную папку без использования URL в карточках."""
    changed = False
    try:
        config_channel = await store.config_channel(guild)
    except Exception:
        config_channel = None

    for city_id, city in state.cities.items():
        _normalize_city(city)
        folder = _city_upload_folder(guild.id, city_id)
        if not _get_paths(city, "screenshotPaths", "screenshot_paths") and config_channel is not None:
            raw_assets = city.get("screenshot_assets", [])
            local_paths: list[str] = []
            if isinstance(raw_assets, list):
                for index, raw in enumerate(raw_assets[:MAX_SCREENSHOTS], 1):
                    asset = AssetRef.from_dict(raw)
                    if not asset.message_id:
                        continue
                    try:
                        message = await config_channel.fetch_message(asset.message_id)
                        attachment = next(
                            (item for item in message.attachments if item.filename == asset.filename),
                            message.attachments[0] if message.attachments else None,
                        )
                        if attachment is None:
                            continue
                        suffix = _safe_extension(attachment.filename, attachment.content_type or "")
                        destination = folder / "screenshots" / f"screenshot_{index:02d}{suffix}"
                        local_paths.append(await _save_attachment_local(attachment, destination))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, OSError):
                        log.exception("Не удалось перенести старый скриншот города %s", city_id)
            if local_paths:
                _set_paths(city, "screenshotPaths", "screenshot_paths", local_paths)
                changed = True

        if not city.get("bannerPath") and config_channel is not None:
            asset = AssetRef.from_dict(city.get("banner_asset", {}))
            if asset.message_id:
                try:
                    message = await config_channel.fetch_message(asset.message_id)
                    attachment = next(
                        (item for item in message.attachments if item.filename == asset.filename),
                        message.attachments[0] if message.attachments else None,
                    )
                    if attachment is not None:
                        suffix = _safe_extension(attachment.filename, attachment.content_type or "")
                        destination = folder / f"main_banner{suffix}"
                        relative = await _save_attachment_local(attachment, destination)
                        city["bannerPath"] = relative
                        city["banner_path"] = relative
                        changed = True
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, OSError):
                    log.exception("Не удалось перенести старый баннер города %s", city_id)

        # Старые внешние ссылки больше никогда не выводятся.
        if city.get("screenshots") or city.get("banner_url"):
            city["screenshots"] = []
            city["banner_url"] = ""
            changed = True

    panel_asset = state.asset("city_application_panel")
    if not state.options.get("city_application_banner_path") and panel_asset.message_id and config_channel is not None:
        try:
            message = await config_channel.fetch_message(panel_asset.message_id)
            attachment = next(
                (item for item in message.attachments if item.filename == panel_asset.filename),
                message.attachments[0] if message.attachments else None,
            )
            if attachment is not None:
                suffix = _safe_extension(attachment.filename, attachment.content_type or "")
                destination = STATIC_BANNER_DIR / f"custom_application{suffix}"
                state.options["city_application_banner_path"] = await _save_attachment_local(attachment, destination)
                changed = True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, OSError):
            log.exception("Не удалось перенести старый баннер панели городов")

    if changed:
        await store.save(state)
    return changed


async def _audit_city_state(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    guild: discord.Guild,
    state: UnifiedState,
    admin_ids: set[int],
) -> None:
    changed = False
    notices: list[tuple[str, str, str, dict[str, Any], int]] = []

    for city_id, city in state.cities.items():
        _normalize_city(city)

        mayor = await _member(guild, _mayor_id(city))
        deputy = await _member(guild, _deputy_id(city))
        presence_checks = (
            ("mayor", mayor is not None, "мэр"),
            ("deputy", deputy is not None, "заместитель мэра"),
        )
        for role_key, present, role_label in presence_checks:
            previous_present = bool(city.get(f"{role_key}Present", city.get(f"{role_key}_present", True)))
            if previous_present == present:
                continue
            city[f"{role_key}Present"] = present
            city[f"{role_key}_present"] = present
            stamp_key = f"{role_key}{'JoinedAt' if present else 'LeftAt'}"
            city[stamp_key] = _now_iso()
            city[f"{role_key}_{'joined_at' if present else 'left_at'}"] = city[stamp_key]
            changed = True
            leader_id = _mayor_id(city) if role_key == "mayor" else _deputy_id(city)
            notices.append(
                (
                    "👤 Руководитель снова найден на сервере" if present else "⚠️ Руководитель отсутствует на сервере",
                    (
                        f"Discord ID `{leader_id}` ({role_label}) города **{city.get('name', city_id)}** "
                        + (
                            "снова принадлежит участнику сервера. Доступ по ID восстановлен автоматически."
                            if present
                            else "не найден среди участников. Система продолжает работать, но администрации необходимо назначить нового руководителя."
                        )
                    ),
                    city_id,
                    city,
                    0x59B77A if present else 0xF2B84B,
                )
            )

        before = list(city.get("allowedWriterIds", []))
        _refresh_allowed_writers(guild, state, city, admin_ids, bot.user.id if bot.user else 0)
        if before != city.get("allowedWriterIds", []):
            changed = True

        if city.get("status") == "approved":
            thread_id = _get_message_id(city, "registryThreadId", "registry_thread_id")
            previous_status = str(city.get("registryStatus", city.get("registry_status", "not_created")))
            new_status = previous_status
            if not thread_id:
                new_status = "not_created"
            else:
                thread = await _thread_channel(bot, thread_id)
                if thread is None:
                    new_status = "deleted"
                else:
                    registry_message_id = _get_message_id(city, "registryMessageId", "registry_message_id")
                    if registry_message_id:
                        try:
                            await thread.fetch_message(registry_message_id)
                        except discord.NotFound:
                            new_status = "message_deleted"
                        except (discord.Forbidden, discord.HTTPException):
                            new_status = "unavailable"
                        else:
                            screenshot_message_id = _get_message_id(
                                city,
                                "registryScreenshotsMessageId",
                                "registry_screenshots_message_id",
                            )
                            if screenshot_message_id:
                                try:
                                    await thread.fetch_message(screenshot_message_id)
                                except discord.NotFound:
                                    new_status = "screenshots_deleted"
                                except (discord.Forbidden, discord.HTTPException):
                                    new_status = "unavailable"
                                else:
                                    new_status = "active"
                            else:
                                new_status = "active"
                    else:
                        new_status = "message_deleted"

            if previous_status != new_status:
                city["registryStatus"] = new_status
                city["registry_status"] = new_status
                changed = True
                if new_status in {"deleted", "message_deleted", "screenshots_deleted", "not_created", "unavailable"}:
                    city["registryDeletedAt"] = _now_iso()
                    city["registry_deleted_at"] = city["registryDeletedAt"]
                    notices.append(
                        (
                            "🗑️ Нарушена связь с публикацией города",
                            (
                                f"Проверка после запуска обнаружила проблему у города **{city.get('name', city_id)}**. "
                                f"Статус публикации: **{_registry_status(city)}**. "
                                "Панель управления продолжит работать и покажет этот статус без падения бота."
                            ),
                            city_id,
                            city,
                            0xD85C5C,
                        )
                    )
                elif new_status == "active":
                    notices.append(
                        (
                            "✅ Связь с публикацией города восстановлена",
                            f"Публикация города **{city.get('name', city_id)}** снова доступна и связана с JSON-данными.",
                            city_id,
                            city,
                            0x59B77A,
                        )
                    )

        for local_path in _get_paths(city, "screenshotPaths", "screenshot_paths"):
            if not _resolve_project_path(local_path).is_file():
                await _log_missing_local_asset_once(bot, state, city_id, city, "скриншот")
                break
        banner_raw = str(city.get("bannerPath", "") or "")
        if banner_raw and not _resolve_project_path(banner_raw).is_file():
            if city.get("bannerFileStatus") != "missing":
                changed = True
            city["bannerFileStatus"] = "missing"
            await _log_missing_local_asset_once(bot, state, city_id, city, "главный баннер")
        elif banner_raw:
            if city.get("bannerFileStatus") != "active":
                changed = True
            city["bannerFileStatus"] = "active"

    if changed:
        await store.save(state)

    for title, description, city_id, city, color in notices:
        await _send_city_log(
            bot,
            state,
            title=title,
            description=description,
            city_id=city_id,
            city=city,
            color=color,
        )


async def _edit_review_message(
    bot: commands.Bot,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
) -> None:
    channel = await _text_channel(bot, int(city.get("review_channel_id", 0)))
    if channel is None:
        return
    message_id = _get_message_id(city, "reviewMessageId", "review_message_id")
    if not message_id:
        return
    try:
        message = await channel.fetch_message(message_id)
        view: discord.ui.View = (
            CityReviewView(bot, bot.unified_store, city_id)  # type: ignore[attr-defined]
            if city.get("status") == "pending"
            else discord.ui.View(timeout=None)
        )
        await _edit_message_card(
            message,
            kind="moderation",
            embed=city_review_embed(city_id, city, state),
            state=state,
            city=city,
            view=view,
        )
    except discord.NotFound:
        city["reviewMessageStatus"] = "deleted"
        city["review_message_status"] = "deleted"
        log.warning("Модерационная карточка города %s удалена", city_id)
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Не удалось обновить модерационную карточку города %s", city_id)


async def sync_registry_post(
    bot: commands.Bot,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
    *,
    rename_thread: bool = False,
) -> tuple[bool, str]:
    thread_id = _get_message_id(city, "registryThreadId", "registry_thread_id")
    thread = await _thread_channel(bot, thread_id)
    if thread is None:
        city["registryStatus"] = "deleted"
        city["registry_status"] = "deleted"
        city.setdefault("registryDeletedAt", _now_iso())
        city["registry_deleted_at"] = city["registryDeletedAt"]
        return False, "Связанная публикация реестра удалена или недоступна."

    try:
        if thread.archived:
            await thread.edit(archived=False, reason=f"Обновление карточки города {city_id}")
        message_id = _get_message_id(city, "registryMessageId", "registry_message_id")
        message = await thread.fetch_message(message_id)
        await _edit_message_card(
            message,
            kind="registry",
            embed=city_registry_embed(city_id, city, state),
            state=state,
            city=city,
            content=f"`{city_id}` • Официальная карточка города FunFernus",
        )
        screenshot_message, screenshot_text = await _edit_screenshot_message(
            thread,
            _get_message_id(city, "registryScreenshotsMessageId", "registry_screenshots_message_id"),
            city_id,
            city,
            context="официальный реестр",
        )
        _set_message_id(
            city,
            "registryScreenshotsMessageId",
            "registry_screenshots_message_id",
            screenshot_message.id if screenshot_message else 0,
        )
        if rename_thread:
            await thread.edit(name=str(city.get("name", city_id))[:100], reason=f"Переименование города {city_id}")
        city["registryStatus"] = "active"
        city["registry_status"] = "active"
        return True, f"Карточка реестра обновлена. {screenshot_text}"
    except discord.NotFound:
        city["registryStatus"] = "message_deleted"
        city["registry_status"] = "message_deleted"
        return False, "Основная карточка публикации была удалена вручную."
    except discord.Forbidden:
        city["registryStatus"] = "unavailable"
        city["registry_status"] = "unavailable"
        return False, "Боту не хватает прав для изменения публикации реестра."
    except discord.HTTPException as exc:
        city["registryStatus"] = "unavailable"
        city["registry_status"] = "unavailable"
        return False, f"Discord не обновил карточку реестра: {exc}"


async def create_registry_post(
    bot: commands.Bot,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
) -> tuple[discord.Thread | None, discord.Message | None, discord.Message | None, str]:
    forum = await _forum_channel(bot, state.channels.get("city_registry", 0))
    if forum is None:
        return None, None, None, "Форум-канал реестра не настроен или недоступен."

    kwargs: dict[str, Any] = {}
    if bool(getattr(forum.flags, "require_tag", False)):
        available = [tag for tag in forum.available_tags if not tag.moderated] or list(forum.available_tags)
        if not available:
            return None, None, None, "В форуме обязателен тег, но доступных тегов нет."
        kwargs["applied_tags"] = [available[0]]

    embeds, banner_file = _message_payload("registry", city_registry_embed(city_id, city, state), city=city, state=state)
    try:
        created = await forum.create_thread(
            name=str(city.get("name", city_id))[:100],
            content=f"`{city_id}` • Официальная карточка города FunFernus",
            embeds=embeds,
            file=banner_file,
            allowed_mentions=discord.AllowedMentions.none(),
            reason=f"Одобрена регистрация города {city_id}",
            **kwargs,
        )
        screenshot_message = await _send_screenshot_message(
            created.thread,
            city_id,
            city,
            context="официальный реестр",
        )
    except discord.Forbidden:
        return None, None, None, "Боту не хватает прав для создания публикаций в форуме реестра."
    except discord.HTTPException as exc:
        return None, None, None, f"Discord не создал карточку реестра: {exc}"
    return created.thread, created.message, screenshot_message, "Карточка реестра создана."


class MayorSelect(discord.ui.UserSelect):
    def __init__(self, owner: "MayorDeputyView") -> None:
        super().__init__(placeholder="Выберите мэра", min_values=1, max_values=1, row=0)
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        self.owner.mayor_id = self.values[0].id
        await interaction.response.defer()


class DeputySelect(discord.ui.UserSelect):
    def __init__(self, owner: "MayorDeputyView") -> None:
        super().__init__(placeholder="Выберите заместителя мэра", min_values=1, max_values=1, row=1)
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        self.owner.deputy_id = self.values[0].id
        await interaction.response.defer()


class MayorDeputyView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, applicant_id: int) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.store = store
        self.applicant_id = applicant_id
        self.mayor_id = 0
        self.deputy_id = 0
        self.add_item(MayorSelect(self))
        self.add_item(DeputySelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.applicant_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Форма недоступна",
                description="Эта форма выбора руководства открыта другим пользователем.",
            )
            return False
        return True

    @discord.ui.button(label="Продолжить", emoji="➡️", style=discord.ButtonStyle.primary, row=2)
    async def continue_form(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Хранилище недоступно",
                description="Служебные данные бота ещё не загружены.",
            )
            return
        if not self.mayor_id or not self.deputy_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Руководство не выбрано",
                description="Отдельно выберите мэра и заместителя через два меню пользователей Discord.",
                state=state,
            )
            return
        if self.mayor_id == self.deputy_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нельзя выбрать одного человека",
                description="Мэр и заместитель должны быть разными участниками сервера.",
                state=state,
            )
            return
        mayor = await _member(interaction.guild, self.mayor_id)
        deputy = await _member(interaction.guild, self.deputy_id)
        if mayor is None or deputy is None or mayor.bot or deputy.bot:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Некорректный выбор",
                description="Оба руководителя должны быть обычными участниками именно этого Discord-сервера.",
                state=state,
            )
            return
        if self.mayor_id != interaction.user.id and not _is_city_staff_member(
            interaction.user,
            interaction.guild,
            state,
            getattr(self.bot, "admin_user_ids", set()),
        ):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Отправитель не является мэром",
                description="Обычную заявку должен отправлять выбранный мэр. Администрация может подать её от имени игрока.",
                state=state,
            )
            return
        mayor_city = _find_person_city(state, self.mayor_id)
        deputy_city = _find_person_city(state, self.deputy_id)
        if mayor_city is not None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Мэр уже состоит в городе",
                description=f"Выбранный мэр уже связан с городом `{mayor_city[0]}` как руководитель или горожанин.",
                state=state,
            )
            return
        if deputy_city is not None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Заместитель уже состоит в городе",
                description=f"Выбранный заместитель уже связан с городом `{deputy_city[0]}` как руководитель или горожанин.",
                state=state,
            )
            return
        await interaction.response.send_modal(
            CityDetailsModal(self.bot, self.store, self.applicant_id, self.mayor_id, self.deputy_id)
        )


class CityDetailsModal(discord.ui.Modal):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        applicant_id: int,
        mayor_id: int,
        deputy_id: int,
    ) -> None:
        super().__init__(title="Регистрация города • данные", timeout=600)
        self.bot = bot
        self.store = store
        self.applicant_id = applicant_id
        self.mayor_id = mayor_id
        self.deputy_id = deputy_id
        self.name_input = discord.ui.TextInput(
            label="Название города",
            placeholder="От 1 до 20 символов",
            min_length=1,
            max_length=20,
        )
        self.style_input = discord.ui.TextInput(
            label="Архитектурный стиль",
            placeholder="Например: северный модерн",
            max_length=300,
        )
        self.overworld_input = discord.ui.TextInput(
            label="Координаты в Верхнем мире",
            placeholder="1000, 100, 1500",
            max_length=100,
        )
        self.nether_input = discord.ui.TextInput(
            label="Нижний мир и ветка метро",
            placeholder="Красная, 220",
            max_length=150,
        )
        self.description_input = discord.ui.TextInput(
            label="Описание города",
            placeholder="Задумка, стиль и особенности расположения",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=2000,
        )
        for item in (
            self.name_input,
            self.style_input,
            self.overworld_input,
            self.nether_input,
            self.description_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user.id != self.applicant_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Форма устарела",
                description="Начните регистрацию города заново через официальную панель.",
            )
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Хранилище недоступно",
                description="Служебные данные бота ещё не загружены.",
            )
            return
        name = str(self.name_input).strip()
        if _name_taken(state, name):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Название занято",
                description="Город с таким названием уже зарегистрирован или находится на рассмотрении.",
                state=state,
            )
            return
        occupied = _find_person_city(state, self.mayor_id) or _find_person_city(state, self.deputy_id)
        if occupied is not None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Руководитель уже состоит в городе",
                description=f"Пока форма была открыта, один из выбранных руководителей оказался связан с городом `{occupied[0]}`.",
                state=state,
            )
            return

        token = secrets.token_hex(8)
        draft = {
            "token": token,
            "applicant_id": interaction.user.id,
            "name": name,
            "style": str(self.style_input).strip(),
            "overworld_coords": str(self.overworld_input).strip(),
            "nether_coords": str(self.nether_input).strip(),
            "description": str(self.description_input).strip(),
            "created_at": _now_iso(),
        }
        _set_leaders(draft, self.mayor_id, self.deputy_id)
        state.city_drafts[str(interaction.user.id)] = draft
        await self.store.save(state)
        await _send_interaction_card(
            interaction,
            kind="application",
            title="📸 Последний этап регистрации",
            description=(
                "Прикрепите настоящие файлы скриншотов первых построек. Внешние ссылки не принимаются: "
                "бот сохранит изображения локально и отправит администрации отдельным сообщением-вложением."
            ),
            state=state,
            view=CityScreenshotsView(self.bot, self.store, interaction.user.id, token),
        )


class CityScreenshotsView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, applicant_id: int, token: str) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.store = store
        self.applicant_id = applicant_id
        self.token = token

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.applicant_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Форма недоступна",
                description="Эта форма открыта другим пользователем.",
            )
            return False
        return True

    @discord.ui.button(label="Прикрепить скриншоты и отправить", emoji="📸", style=discord.ButtonStyle.success)
    async def screenshots(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            CityScreenshotsModal(self.bot, self.store, self.applicant_id, self.token)
        )


class CityScreenshotsModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, applicant_id: int, token: str) -> None:
        super().__init__(title="Регистрация города • скриншоты", timeout=600)
        self.bot = bot
        self.store = store
        self.applicant_id = applicant_id
        self.token = token
        self.files_label = discord.ui.Label(
            text="Файлы изображений",
            description="От 1 до 10 изображений PNG/JPG/WEBP/GIF, каждое до 10 МБ.",
            component=discord.ui.FileUpload(
                custom_id="city_application_screenshots",
                required=True,
                min_values=1,
                max_values=MAX_SCREENSHOTS,
            ),
        )
        self.add_item(self.files_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user.id != self.applicant_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Форма устарела",
                description="Начните регистрацию города заново.",
            )
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Хранилище недоступно",
                description="Служебные данные бота ещё не загружены.",
            )
            return
        draft = state.city_drafts.get(str(interaction.user.id))
        if not draft or draft.get("token") != self.token:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Черновик не найден",
                description="Начните регистрацию заново через официальную панель.",
                state=state,
            )
            return
        review = await _text_channel(self.bot, state.channels.get("city_review", 0))
        if review is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Канал модерации недоступен",
                description="Администрация ещё не настроила канал рассмотрения городов.",
                state=state,
            )
            return
        leadership_occupied = (
            _find_person_city(state, _mayor_id(draft))
            or _find_person_city(state, _deputy_id(draft))
        )
        if leadership_occupied is not None or _name_taken(state, str(draft.get("name", ""))):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Данные уже заняты",
                description=(
                    "Пока форма была открыта, название стало занято либо один из руководителей "
                    "оказался связан с другим городом."
                ),
                state=state,
            )
            return

        file_component = self.files_label.component
        attachments = list(file_component.values) if isinstance(file_component, discord.ui.FileUpload) else []
        if not attachments:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет скриншотов",
                description="Прикрепите хотя бы один настоящий файл изображения.",
                state=state,
            )
            return
        for attachment in attachments:
            try:
                _validate_attachment(attachment)
            except ValueError as exc:
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Некорректный файл",
                    description=f"`{attachment.filename}`: {exc}.",
                    state=state,
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        city_id = state.next_id("city", "CITY")
        folder = _city_upload_folder(interaction.guild.id, city_id) / "screenshots"
        saved_paths: list[str] = []
        try:
            for index, attachment in enumerate(attachments[:MAX_SCREENSHOTS], 1):
                suffix = _safe_extension(attachment.filename, attachment.content_type or "")
                saved_paths.append(
                    await _save_attachment_local(attachment, folder / f"screenshot_{index:02d}{suffix}")
                )
        except (ValueError, OSError, discord.HTTPException) as exc:
            shutil.rmtree(_city_upload_folder(interaction.guild.id, city_id), ignore_errors=True)
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Скриншоты не сохранены",
                description=f"Локальное сохранение завершилось ошибкой: `{exc}`",
                state=state,
                followup=True,
            )
            return

        city: dict[str, Any] = {
            **draft,
            "id": city_id,
            "status": "pending",
            "question_history": [],
            "active_question": {},
            "review_channel_id": review.id,
            "submitted_at": _now_iso(),
            "bannerPath": "",
            "banner_path": "",
            "banner_url": "",
            "screenshots": [],
            "screenshot_assets": [],
            "mayorPresent": True,
            "deputyPresent": True,
            "registryStatus": "not_created",
            "registry_status": "not_created",
            "citizenIds": [],
            "citizen_ids": [],
            "citizenHistory": [],
            "citizen_history": [],
            "citizenAbsentIds": [],
            "citizen_absent_ids": [],
        }
        city.pop("token", None)
        _set_leaders(city, _mayor_id(draft), _deputy_id(draft))
        _set_paths(city, "screenshotPaths", "screenshot_paths", saved_paths)
        for camel, snake in (
            ("reviewMessageId", "review_message_id"),
            ("reviewScreenshotsMessageId", "review_screenshots_message_id"),
            ("registryThreadId", "registry_thread_id"),
            ("registryMessageId", "registry_message_id"),
            ("registryScreenshotsMessageId", "registry_screenshots_message_id"),
        ):
            _set_message_id(city, camel, snake, 0)
        _refresh_allowed_writers(
            interaction.guild,
            state,
            city,
            getattr(self.bot, "admin_user_ids", set()),
            self.bot.user.id if self.bot.user else 0,
        )
        state.cities[city_id] = city
        state.city_drafts.pop(str(interaction.user.id), None)

        main_message: discord.Message | None = None
        screenshots_message: discord.Message | None = None
        try:
            main_message = await _send_channel_card(
                review,
                kind="moderation",
                embed=city_review_embed(city_id, city, state),
                state=state,
                city=city,
                view=CityReviewView(self.bot, self.store, city_id),
            )
            screenshots_message = await _send_screenshot_message(
                review,
                city_id,
                city,
                context="материалы заявки",
            )
            _set_message_id(city, "reviewMessageId", "review_message_id", main_message.id)
            _set_message_id(
                city,
                "reviewScreenshotsMessageId",
                "review_screenshots_message_id",
                screenshots_message.id if screenshots_message else 0,
            )
            await self.store.save(state)
        except Exception as exc:
            if main_message is not None:
                try:
                    _bot_deleted_messages.add(main_message.id)
                    await main_message.delete()
                except discord.HTTPException:
                    pass
            if screenshots_message is not None:
                try:
                    _bot_deleted_messages.add(screenshots_message.id)
                    await screenshots_message.delete()
                except discord.HTTPException:
                    pass
            state.cities.pop(city_id, None)
            state.city_drafts[str(interaction.user.id)] = draft
            shutil.rmtree(_city_upload_folder(interaction.guild.id, city_id), ignore_errors=True)
            try:
                await self.store.save(state)
            except Exception:
                log.exception("Не удалось откатить заявку города %s", city_id)
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Заявка не отправлена",
                description=f"Discord или хранилище вернули ошибку: `{exc}`",
                state=state,
                followup=True,
            )
            return

        await _send_city_log(
            self.bot,
            state,
            title="📨 Подана заявка на регистрацию города",
            description=(
                f"Заявку отправил <@{interaction.user.id}> (`{interaction.user.id}`).\n"
                f"Мэр: <@{_mayor_id(city)}> (`{_mayor_id(city)}`).\n"
                f"Заместитель: <@{_deputy_id(city)}> (`{_deputy_id(city)}`).\n"
                f"Локально сохранено скриншотов: **{len(saved_paths)}**."
            ),
            city_id=city_id,
            city=city,
            color=0xF2B84B,
        )
        await _send_interaction_card(
            interaction,
            kind="notification",
            title="✅ Заявка отправлена",
            description=(
                f"Заявка `{city_id}` передана администрации. Основная карточка и скриншоты отправлены "
                "двумя отдельными сообщениями без URL-адресов изображений."
            ),
            state=state,
            city=city,
            followup=True,
            color=0x59B77A,
        )


class CityApplicationPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store

    @discord.ui.button(
        label="Зарегистрировать город",
        emoji="🏰",
        style=discord.ButtonStyle.success,
        custom_id="unified:cities:register",
    )
    async def register(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Недоступно в личных сообщениях",
                description="Регистрация города выполняется только на сервере FunFernus.",
            )
            return
        state = self.store.get(interaction.guild.id) or await self.store.load_or_create(interaction.guild)
        if interaction.channel_id != state.channels.get("city_application", 0):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Неофициальная панель",
                description="Используйте настроенный канал подачи заявок городов.",
                state=state,
            )
            return
        if _has_active_city(state, interaction.user.id):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Активный город уже существует",
                description="У вас уже есть зарегистрированный город или заявка на рассмотрении.",
                state=state,
            )
            return
        await _send_interaction_card(
            interaction,
            kind="application",
            title="🏰 Выберите руководство города",
            description=(
                "Сначала отдельно выберите мэра и заместителя через User Select Menu. "
                "Все права будут навсегда связаны с их Discord ID, а не с ником."
            ),
            state=state,
            view=MayorDeputyView(self.bot, self.store, interaction.user.id),
        )


class CityReviewView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, city_id: str = "") -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store
        self.city_id = city_id

    async def guard(self, interaction: discord.Interaction) -> tuple[UnifiedState | None, str, dict[str, Any] | None]:
        if interaction.guild is None:
            return None, "", None
        state = self.store.get(interaction.guild.id)
        if state is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Хранилище недоступно",
                description="Служебные данные системы городов не загружены.",
            )
            return None, "", None
        if not _is_city_staff_member(
            interaction.user,
            interaction.guild,
            state,
            getattr(self.bot, "admin_user_ids", set()),
        ):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Модерировать города могут только настроенные администраторы и модераторы.",
                state=state,
            )
            return None, "", None
        if interaction.channel_id != state.channels.get("city_review", 0):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Неверный канал",
                description="Используйте официальный канал рассмотрения заявок городов.",
                state=state,
            )
            return None, "", None
        city_id = self.city_id or _city_id(interaction.message)
        city = state.cities.get(city_id) if city_id else None
        if city is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Заявка не найдена",
                description="Карточка не связана с городом в JSON-хранилище.",
                state=state,
            )
            return None, "", None
        _normalize_city(city)
        if city.get("status") != "pending":
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Заявка уже рассмотрена",
                description=f"Текущий статус: **{_status_text(str(city.get('status')))}**.",
                state=state,
                city=city,
            )
            return None, "", None
        return state, city_id, city

    @discord.ui.button(
        label="Одобрить",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="unified:cities:review:approve",
    )
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state, city_id, city = await self.guard(interaction)
        if state is None or city is None or interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock(interaction.guild.id, city_id):
            city = state.cities.get(city_id)
            if city is None or city.get("status") != "pending":
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Заявка уже рассмотрена",
                    description="Другой модератор успел обработать её раньше.",
                    state=state,
                    followup=True,
                )
                return
            mayor_id = _mayor_id(city)
            deputy_id = _deputy_id(city)
            if not mayor_id or not deputy_id or mayor_id == deputy_id:
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Ошибка руководства",
                    description="В заявке должны быть два разных Discord ID: мэр и заместитель.",
                    state=state,
                    city=city,
                    followup=True,
                )
                return
            mayor = await _member(interaction.guild, mayor_id)
            deputy = await _member(interaction.guild, deputy_id)
            if mayor is None or deputy is None:
                missing = "мэр" if mayor is None else "заместитель"
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Руководитель покинул сервер",
                    description=f"Нельзя одобрить заявку: {missing} больше не находится на Discord-сервере.",
                    state=state,
                    city=city,
                    followup=True,
                )
                return
            role_ids = state.roles.get("city_mayor", [])
            mayor_role = interaction.guild.get_role(role_ids[0]) if role_ids else None
            if mayor_role is None:
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Роль мэра не настроена",
                    description="Выберите существующую роль мэра в панели настройки городов.",
                    state=state,
                    city=city,
                    followup=True,
                )
                return

            previous = copy.deepcopy(city)
            city.update(
                {
                    "status": "approved",
                    "reviewer_id": interaction.user.id,
                    "approved_at": _now_iso(),
                    "active_question": {},
                    "mayorPresent": True,
                    "mayor_present": True,
                    "deputyPresent": True,
                    "deputy_present": True,
                    "registryStatus": "active",
                    "registry_status": "active",
                }
            )
            _refresh_allowed_writers(
                interaction.guild,
                state,
                city,
                getattr(self.bot, "admin_user_ids", set()),
                self.bot.user.id if self.bot.user else 0,
            )

            thread: discord.Thread | None = None
            registry_message: discord.Message | None = None
            registry_screenshots: discord.Message | None = None
            role_already_present = mayor_role in mayor.roles
            try:
                thread, registry_message, registry_screenshots, error = await create_registry_post(
                    self.bot, state, city_id, city
                )
                if thread is None or registry_message is None:
                    raise RuntimeError(error)
                if not role_already_present:
                    await mayor.add_roles(mayor_role, reason=f"Мэр зарегистрированного города {city_id}")
                _set_message_id(city, "registryThreadId", "registry_thread_id", thread.id)
                _set_message_id(city, "registryMessageId", "registry_message_id", registry_message.id)
                _set_message_id(
                    city,
                    "registryScreenshotsMessageId",
                    "registry_screenshots_message_id",
                    registry_screenshots.id if registry_screenshots else 0,
                )
                await self.store.save(state)
            except Exception as exc:
                state.cities[city_id] = previous
                if not role_already_present and mayor_role in mayor.roles:
                    try:
                        await mayor.remove_roles(mayor_role, reason=f"Откат регистрации города {city_id}")
                    except discord.HTTPException:
                        log.exception("Не удалось убрать роль мэра при откате %s", city_id)
                if thread is not None:
                    try:
                        await thread.delete(reason=f"Откат регистрации города {city_id}")
                    except discord.HTTPException:
                        log.exception("Не удалось удалить публикацию при откате %s", city_id)
                await _send_city_log(
                    self.bot,
                    state,
                    title="❌ Ошибка одобрения города",
                    description=f"Модератор <@{interaction.user.id}> не смог одобрить город: `{exc}`",
                    city_id=city_id,
                    city=previous,
                    color=0xD85C5C,
                )
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Город не одобрен",
                    description=f"Операция полностью отменена: `{exc}`",
                    state=state,
                    city=previous,
                    followup=True,
                )
                return

            await _edit_review_message(self.bot, state, city_id, city)
            await _send_city_log(
                self.bot,
                state,
                title="✅ Город одобрен и опубликован",
                description=(
                    f"Модератор: <@{interaction.user.id}> (`{interaction.user.id}`).\n"
                    f"Публикация: <#{thread.id}> (`{thread.id}`).\n"
                    f"Мэр: <@{mayor_id}>. Заместитель: <@{deputy_id}>."
                ),
                city_id=city_id,
                city=city,
                color=0x59B77A,
            )

            mayor_dm = _simple_embed(
                "✅ Город успешно зарегистрирован",
                (
                    f"Город **{city.get('name')}** одобрен администрацией FunFernus.\n\n"
                    f"Вам выдана роль **{mayor_role.name}**, а публикация создана в <#{thread.id}>."
                ),
                color=0x59B77A,
                footer=f"FunFernus • {city_id}",
            )
            deputy_dm = _simple_embed(
                "✅ Вы назначены заместителем мэра",
                (
                    f"Город **{city.get('name')}** зарегистрирован. Вы можете писать в его официальной "
                    f"публикации <#{thread.id}> по своему Discord ID."
                ),
                color=0x59B77A,
                footer=f"FunFernus • {city_id}",
            )
            dm_failures: list[str] = []
            try:
                await _send_user_card(mayor, kind="notification", embed=mayor_dm, state=state, city=city)
            except discord.HTTPException:
                dm_failures.append("мэру")
            try:
                await _send_user_card(deputy, kind="notification", embed=deputy_dm, state=state, city=city)
            except discord.HTTPException:
                dm_failures.append("заместителю")

            tail = f" ЛС не доставлены: {', '.join(dm_failures)}." if dm_failures else ""
            await _send_interaction_card(
                interaction,
                kind="notification",
                title="✅ Город одобрен",
                description=f"Город `{city_id}` опубликован в <#{thread.id}>.{tail}",
                state=state,
                city=city,
                followup=True,
                color=0x59B77A,
            )

    @discord.ui.button(
        label="Отклонить с причиной",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="unified:cities:review:reject",
    )
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state, city_id, city = await self.guard(interaction)
        if state is not None and city is not None:
            await interaction.response.send_modal(CityRejectModal(self.bot, self.store, city_id))

    @discord.ui.button(
        label="Задать вопрос",
        emoji="❓",
        style=discord.ButtonStyle.primary,
        custom_id="unified:cities:review:question",
    )
    async def question(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state, city_id, city = await self.guard(interaction)
        if state is None or city is None:
            return
        if city.get("active_question"):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Вопрос уже ожидает ответа",
                description="Мэр ещё не ответил на предыдущий вопрос администрации.",
                state=state,
                city=city,
            )
            return
        await interaction.response.send_modal(CityQuestionModal(self.bot, self.store, city_id))


class CityRejectModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, city_id: str) -> None:
        super().__init__(title="Отклонение заявки города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.reason = discord.ui.TextInput(
            label="Причина отказа",
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=1500,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None or not _is_city_staff_member(
            interaction.user,
            interaction.guild,
            state,
            getattr(self.bot, "admin_user_ids", set()),
        ):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Заявка не найдена либо у вас нет прав модерации городов.",
                state=state,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock(interaction.guild.id, self.city_id):
            city = state.cities.get(self.city_id)
            if city is None or city.get("status") != "pending":
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Заявка уже рассмотрена",
                    description="Другой модератор обработал её раньше.",
                    state=state,
                    followup=True,
                )
                return
            previous = copy.deepcopy(city)
            city.update(
                {
                    "status": "rejected",
                    "reviewer_id": interaction.user.id,
                    "rejection_reason": str(self.reason).strip(),
                    "rejected_at": _now_iso(),
                    "active_question": {},
                }
            )
            try:
                await self.store.save(state)
            except Exception as exc:
                state.cities[self.city_id] = previous
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Отказ не сохранён",
                    description=f"Ошибка хранилища: `{exc}`",
                    state=state,
                    city=previous,
                    followup=True,
                )
                return
            await _edit_review_message(self.bot, state, self.city_id, city)
            await _send_city_log(
                self.bot,
                state,
                title="❌ Заявка города отклонена",
                description=(
                    f"Модератор: <@{interaction.user.id}> (`{interaction.user.id}`).\n"
                    f"Причина: {_trim(self.reason, 1500)}"
                ),
                city_id=self.city_id,
                city=city,
                color=0xD85C5C,
            )
            mayor = await _user(self.bot, _mayor_id(city))
            delivered = True
            if mayor is None:
                delivered = False
            else:
                dm = _simple_embed(
                    "❌ Регистрация города отклонена",
                    f"Заявка города **{city.get('name')}** отклонена.\n\n**Причина:**\n{str(self.reason).strip()}",
                    color=0xD85C5C,
                    footer=f"FunFernus • {self.city_id}",
                )
                try:
                    await _send_user_card(mayor, kind="warning", embed=dm, state=state, city=city)
                except discord.HTTPException:
                    delivered = False
            await _send_interaction_card(
                interaction,
                kind="notification",
                title="✅ Отказ сохранён",
                description="Причина отправлена мэру." if delivered else "Причина сохранена, но личные сообщения мэра недоступны.",
                state=state,
                city=city,
                followup=True,
                color=0x59B77A,
            )


class CityQuestionModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, city_id: str) -> None:
        super().__init__(title="Вопрос мэру города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.question = discord.ui.TextInput(
            label="Вопрос",
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=1500,
        )
        self.add_item(self.question)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None or not _is_city_staff_member(
            interaction.user,
            interaction.guild,
            state,
            getattr(self.bot, "admin_user_ids", set()),
        ):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Активная заявка не найдена или у вас нет прав.",
                state=state,
            )
            return
        if city.get("status") != "pending" or city.get("active_question"):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нельзя задать вопрос",
                description="Заявка уже рассмотрена либо предыдущий вопрос ещё ожидает ответа.",
                state=state,
                city=city,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        question = str(self.question).strip()
        async with _lock(interaction.guild.id, self.city_id):
            city = state.cities.get(self.city_id)
            if city is None or city.get("status") != "pending" or city.get("active_question"):
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Состояние заявки изменилось",
                    description="Обновите канал модерации и повторите действие.",
                    state=state,
                    followup=True,
                )
                return
            mayor = await _user(self.bot, _mayor_id(city))
            if mayor is None:
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Мэр не найден",
                    description="Пользователь больше недоступен в Discord.",
                    state=state,
                    city=city,
                    followup=True,
                )
                return
            item = {
                "token": secrets.token_hex(6),
                "question": question,
                "answer": "",
                "asked_by": interaction.user.id,
                "asked_at": _now_iso(),
                "answerAttachmentPaths": [],
                "reviewAnswerMessageId": 0,
                "reviewAnswerAttachmentsMessageId": 0,
            }
            previous = copy.deepcopy(city)
            city["active_question"] = dict(item)
            city.setdefault("question_history", []).append(item)
            try:
                await self.store.save(state)
                dm = _simple_embed(
                    "❓ Вопрос по заявке города",
                    (
                        f"Администрация задала вопрос по заявке **{city.get('name')}** (`{self.city_id}`).\n\n"
                        f"**Вопрос:**\n{question}\n\n"
                        "Ответьте следующим сообщением в этом личном чате. Файлы можно приложить — бот сохранит их локально."
                    ),
                    color=0x5865F2,
                    footer=f"FunFernus • {self.city_id}",
                )
                await _send_user_card(mayor, kind="notification", embed=dm, state=state, city=city)
            except Exception as exc:
                state.cities[self.city_id] = previous
                try:
                    await self.store.save(state)
                except Exception:
                    log.exception("Не удалось откатить вопрос по заявке %s", self.city_id)
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Вопрос не отправлен",
                    description=f"Личные сообщения мэра недоступны или возникла ошибка: `{exc}`",
                    state=state,
                    city=previous,
                    followup=True,
                )
                return
            await _edit_review_message(self.bot, state, self.city_id, city)
            await _send_city_log(
                self.bot,
                state,
                title="❓ Администрация задала вопрос мэру",
                description=f"Автор: <@{interaction.user.id}> (`{interaction.user.id}`).\nВопрос: {_trim(question, 1500)}",
                city_id=self.city_id,
                city=city,
            )
        await _send_interaction_card(
            interaction,
            kind="notification",
            title="✅ Вопрос отправлен",
            description="Заявка остаётся на рассмотрении до ответа мэра.",
            state=state,
            city=city,
            followup=True,
            color=0x59B77A,
        )


class CityManagementLauncherView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store

    @discord.ui.button(
        label="Открыть управление городом",
        emoji="⚙️",
        style=discord.ButtonStyle.primary,
        custom_id="unified:cities:management:open",
    )
    async def open_management(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Хранилище недоступно",
                description="Система городов ещё не загружена.",
            )
            return
        if interaction.channel_id != state.channels.get("city_management", 0):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Неверный канал",
                description="Используйте официальный канал управления городом.",
                state=state,
            )
            return
        found = _find_city_for_mayor(state, interaction.user.id)
        if found is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Город не найден",
                description="К вашему Discord ID не привязан зарегистрированный город, в котором вы являетесь мэром.",
                state=state,
            )
            return
        city_id, city = found
        role_ids = state.roles.get("city_mayor", [])
        if isinstance(interaction.user, discord.Member) and role_ids and not any(role.id in role_ids for role in interaction.user.roles):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет роли мэра",
                description="Связь с городом найдена, но роль мэра отсутствует. Обратитесь к администрации.",
                state=state,
                city=city,
            )
            return
        await _send_interaction_card(
            interaction,
            kind="management",
            title=city_management_embed(city_id, city, state).title or "Управление городом",
            description=(
                "Ниже доступны кнопки редактирования. Текущие сведения показаны в дополнительной карточке."
            ),
            state=state,
            city=city,
            view=CityManagementView(self.bot, self.store, city_id, interaction.user.id),
        )
        # Ephemeral follow-up с полными полями панели, также с большим локальным баннером.
        content_embed = city_management_embed(city_id, city, state)
        embeds, file = _message_payload("management", content_embed, city=city, state=state)
        await interaction.followup.send(embeds=embeds, file=file, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class CityManagementView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, city_id: str, mayor_id: int) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.user.id != self.mayor_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Эта панель открыта другому мэру и проверяет доступ только по Discord ID.",
            )
            return False
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if city is None or city.get("status") != "approved" or _mayor_id(city) != interaction.user.id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Доступ изменился",
                description="Вы больше не являетесь мэром этого города либо город недоступен.",
                state=state,
            )
            return False
        return True

    @discord.ui.button(label="Изменить название", emoji="✏️", style=discord.ButtonStyle.primary)
    async def rename(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        city = state.cities.get(self.city_id) if state else {}
        await interaction.response.send_modal(
            CityRenameModal(
                self.bot,
                self.store,
                self.city_id,
                self.mayor_id,
                current_name=str(city.get("name", "")),
            )
        )

    @discord.ui.button(label="Изменить описание", emoji="📝", style=discord.ButtonStyle.primary)
    async def description(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        city = state.cities.get(self.city_id) if state else {}
        await interaction.response.send_modal(
            CityDescriptionModal(
                self.bot,
                self.store,
                self.city_id,
                self.mayor_id,
                current_description=str(city.get("description", "")),
            )
        )

    @discord.ui.button(label="Главный баннер", emoji="🖼️", style=discord.ButtonStyle.success)
    async def banner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CityBannerModal(self.bot, self.store, self.city_id, self.mayor_id))

    @discord.ui.button(label="Координаты и данные", emoji="🧭", style=discord.ButtonStyle.secondary)
    async def data(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        city = state.cities.get(self.city_id) if state else {}
        await interaction.response.send_modal(
            CityEditDataModal(
                self.bot,
                self.store,
                self.city_id,
                self.mayor_id,
                current_city=city,
            )
        )

    @discord.ui.button(label="Добавить горожан", emoji="➕", style=discord.ButtonStyle.success)
    async def add_citizens(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None:
            return
        if len(_citizen_ids(city)) >= MAX_CITY_CITIZENS:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Достигнут лимит горожан",
                description=f"В одном городе можно хранить не более **{MAX_CITY_CITIZENS}** горожан.",
                state=state,
                city=city,
            )
            return
        await _send_interaction_card(
            interaction,
            kind="management",
            title="➕ Добавление горожан",
            description=(
                "Выберите участников Discord-сервера, которые состоят в вашем городе. "
                "Мэр, заместитель, боты и участники другого города добавлены не будут."
            ),
            state=state,
            city=city,
            view=CityCitizenAddView(self.bot, self.store, self.city_id, self.mayor_id),
        )

    @discord.ui.button(label="Удалить горожан", emoji="➖", style=discord.ButtonStyle.danger)
    async def remove_citizens(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None:
            return
        if not _citizen_ids(city):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="ℹ️ Список горожан пуст",
                description="В городе пока нет добавленных горожан.",
                state=state,
                city=city,
            )
            return
        await _send_interaction_card(
            interaction,
            kind="management",
            title="➖ Удаление горожан",
            description="Выберите одного или нескольких участников, которых нужно исключить из списка города.",
            state=state,
            city=city,
            view=CityCitizenRemoveView(self.bot, self.store, self.city_id, self.mayor_id, page=0, guild=interaction.guild),
        )

    @discord.ui.button(label="Список горожан", emoji="👥", style=discord.ButtonStyle.secondary)
    async def citizens_list(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None:
            return
        embed = city_citizens_embed(self.city_id, city, interaction.guild, state, page=0)
        embeds, file = _message_payload("management", embed, city=city, state=state)
        await interaction.response.send_message(
            embeds=embeds,
            file=file,
            view=CityCitizenListView(self.bot, self.store, self.city_id, self.mayor_id, page=0, total=len(_citizen_ids(city))),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Обновить статус", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None:
            return
        await _audit_city_state(
            self.bot,
            self.store,
            interaction.guild,
            state,
            getattr(self.bot, "admin_user_ids", set()),
        )
        embeds, file = _message_payload("management", city_management_embed(self.city_id, city, state), city=city, state=state)
        await interaction.response.send_message(
            embeds=embeds,
            file=file,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )



def city_citizens_embed(
    city_id: str,
    city: dict[str, Any],
    guild: discord.Guild,
    state: UnifiedState,
    *,
    page: int = 0,
) -> discord.Embed:
    _normalize_city(city)
    citizens = _citizen_ids(city)
    page_size = 20
    page_count = max(1, (len(citizens) + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    page_citizens = citizens[page * page_size : (page + 1) * page_size]
    embed = discord.Embed(
        title=f"👥 Горожане • {_trim(city.get('name'), 180)}",
        description=(
            "Список хранится исключительно по Discord ID. Смена ника или отображаемого имени не влияет "
            "на принадлежность к городу. Мэр и заместитель показаны отдельно и не занимают места в списке горожан."
        ),
        color=int(state.options.get("accent_color", 0x19B9D1)),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Мэр", value=_leader_text(city, "mayor"), inline=True)
    embed.add_field(name="Заместитель", value=_leader_text(city, "deputy"), inline=True)
    embed.add_field(
        name="Население",
        value=(
            f"**{len(citizens) + len({user_id for user_id in (_mayor_id(city), _deputy_id(city)) if user_id})}** "
            f"с учётом руководства\n**{len(citizens)}** обычных горожан"
        ),
        inline=True,
    )
    if not page_citizens:
        embed.add_field(name="Список", value="Горожане пока не добавлены.", inline=False)
    else:
        absent = _citizen_absent_ids(city)
        lines: list[str] = []
        for index, user_id in enumerate(page_citizens, page * page_size + 1):
            member = guild.get_member(user_id)
            is_absent = user_id in absent
            status = "⚠️ Покинул сервер" if is_absent else "✅ На сервере"
            display = member.display_name if member is not None else f"Discord ID {user_id}"
            lines.append(f"{index}. **{_trim(display, 55)}** • <@{user_id}> — `{user_id}` • {status}")
        embed.add_field(name="Список горожан", value=_trim("\n".join(lines), 4000), inline=False)
    embed.set_footer(
        text=f"FunFernus • {city_id} • Страница {page + 1}/{page_count} • Лимит: {MAX_CITY_CITIZENS}"
    )
    return embed


class CityCitizenListView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
        *,
        page: int,
        total: int,
    ) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        self.page_count = max(1, (max(total, 0) + 19) // 20)
        self.page = min(max(page, 0), self.page_count - 1)
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.page_count - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        state, city = await _management_guard(interaction, self.store, self.city_id, self.mayor_id)
        return state is not None and city is not None

    async def _show_page(self, interaction: discord.Interaction, target_page: int) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None:
            return
        citizens = _citizen_ids(city)
        page_count = max(1, (len(citizens) + 19) // 20)
        target_page = min(max(target_page, 0), page_count - 1)
        view = CityCitizenListView(
            self.bot,
            self.store,
            self.city_id,
            self.mayor_id,
            page=target_page,
            total=len(citizens),
        )
        content_embed = city_citizens_embed(
            self.city_id,
            city,
            interaction.guild,
            state,
            page=target_page,
        )
        if interaction.message is None or not interaction.message.embeds:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Панель недоступна",
                description="Сообщение со списком горожан больше недоступно. Откройте список заново.",
                state=state,
                city=city,
            )
            return
        banner_embed = interaction.message.embeds[0]
        await interaction.response.edit_message(embeds=[banner_embed, content_embed], view=view)

    @discord.ui.button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show_page(interaction, self.page - 1)

    @discord.ui.button(label="Далее", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show_page(interaction, self.page + 1)


class CityCitizenAddSelect(discord.ui.UserSelect):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
    ) -> None:
        super().__init__(
            placeholder="Выберите новых горожан",
            min_values=1,
            max_values=10,
        )
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id

    async def callback(self, interaction: discord.Interaction) -> None:
        state, city = await _management_guard(interaction, self.store, self.city_id, self.mayor_id)
        if state is None or city is None or interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        current = _citizen_ids(city)
        added: list[int] = []
        skipped: list[str] = []

        for selected in self.values:
            member = await _member(interaction.guild, selected.id)
            if member is None:
                skipped.append(f"`{selected.id}` — не найден на сервере")
                continue
            if member.bot:
                skipped.append(f"<@{member.id}> — ботов нельзя добавлять")
                continue
            if member.id in {_mayor_id(city), _deputy_id(city)}:
                skipped.append(f"<@{member.id}> — уже входит в руководство")
                continue
            if member.id in current or member.id in added:
                skipped.append(f"<@{member.id}> — уже состоит в городе")
                continue
            other = _find_person_city(state, member.id, exclude=self.city_id)
            if other is not None:
                skipped.append(f"<@{member.id}> — уже состоит в городе `{other[0]}`")
                continue
            if len(current) + len(added) >= MAX_CITY_CITIZENS:
                skipped.append(f"<@{member.id}> — достигнут лимит города")
                continue
            added.append(member.id)

        if not added:
            details = "\n".join(skipped[:10]) or "Подходящие участники не выбраны."
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Никого не удалось добавить",
                description=_trim(details, 1800),
                state=state,
                city=city,
                followup=True,
            )
            return

        previous = copy.deepcopy(city)
        _set_citizen_ids(city, [*current, *added])
        _set_citizen_absent_ids(city, _citizen_absent_ids(city))
        history = city.setdefault("citizenHistory", city.get("citizen_history", []))
        history.append(
            {
                "action": "add",
                "userIds": list(added),
                "actorId": interaction.user.id,
                "changedAt": _now_iso(),
            }
        )
        city["citizen_history"] = history
        async with _lock(interaction.guild.id, self.city_id):
            ok, sync_text = await _save_and_sync(
                interaction,
                self.bot,
                self.store,
                state,
                self.city_id,
                city,
                previous,
            )
        if ok:
            await _send_city_log(
                self.bot,
                state,
                title="➕ Добавлены горожане",
                description=(
                    f"Мэр: <@{interaction.user.id}> (`{interaction.user.id}`).\n"
                    f"Добавлены: {', '.join(f'<@{user_id}> (`{user_id}`)' for user_id in added)}.\n"
                    f"Теперь в списке: **{len(_citizen_ids(city))}**.\n"
                    f"Синхронизация: {sync_text}"
                ),
                city_id=self.city_id,
                city=city,
                color=0x59B77A,
            )
        skipped_text = f"\n\nНе добавлены:\n{_trim(chr(10).join(skipped[:10]), 900)}" if skipped else ""
        await _send_interaction_card(
            interaction,
            kind="notification" if ok else "warning",
            title="✅ Горожане добавлены" if ok else "❌ Изменения не сохранены",
            description=(
                f"Добавлено: **{len(added)}**. Всего горожан: **{len(_citizen_ids(city))}**.\n{sync_text}"
                f"{skipped_text}"
            ),
            state=state,
            city=city,
            followup=True,
            color=0x59B77A if ok else 0xD85C5C,
        )


class CityCitizenAddView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
    ) -> None:
        super().__init__(timeout=600)
        self.add_item(CityCitizenAddSelect(bot, store, city_id, mayor_id))


class CityCitizenRemoveSelect(discord.ui.Select):
    def __init__(self, owner: "CityCitizenRemoveView", citizen_ids: list[int]) -> None:
        options: list[discord.SelectOption] = []
        guild = owner.guild
        for user_id in citizen_ids:
            member = guild.get_member(user_id) if guild is not None else None
            label = _trim(member.display_name if member else f"Пользователь {user_id}", 90)
            description = f"Discord ID: {user_id}" if member else f"Покинул сервер • ID: {user_id}"
            options.append(discord.SelectOption(label=label, value=str(user_id), description=_trim(description, 100)))
        super().__init__(
            placeholder="Выберите горожан для удаления",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        state, city = await _management_guard(
            interaction,
            self.owner.store,
            self.owner.city_id,
            self.owner.mayor_id,
        )
        if state is None or city is None or interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        remove_ids = {int(item) for item in self.values if str(item).isdigit()}
        current = _citizen_ids(city)
        removed = [user_id for user_id in current if user_id in remove_ids]
        if not removed:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Горожане не выбраны",
                description="Выбранные Discord ID уже отсутствуют в списке города.",
                state=state,
                city=city,
                followup=True,
            )
            return
        previous = copy.deepcopy(city)
        _set_citizen_ids(city, [user_id for user_id in current if user_id not in remove_ids])
        _set_citizen_absent_ids(city, _citizen_absent_ids(city))
        history = city.setdefault("citizenHistory", city.get("citizen_history", []))
        history.append(
            {
                "action": "remove",
                "userIds": list(removed),
                "actorId": interaction.user.id,
                "changedAt": _now_iso(),
            }
        )
        city["citizen_history"] = history
        async with _lock(interaction.guild.id, self.owner.city_id):
            ok, sync_text = await _save_and_sync(
                interaction,
                self.owner.bot,
                self.owner.store,
                state,
                self.owner.city_id,
                city,
                previous,
            )
        if ok:
            await _send_city_log(
                self.owner.bot,
                state,
                title="➖ Удалены горожане",
                description=(
                    f"Мэр: <@{interaction.user.id}> (`{interaction.user.id}`).\n"
                    f"Удалены: {', '.join(f'<@{user_id}> (`{user_id}`)' for user_id in removed)}.\n"
                    f"Осталось в списке: **{len(_citizen_ids(city))}**.\n"
                    f"Синхронизация: {sync_text}"
                ),
                city_id=self.owner.city_id,
                city=city,
                color=0xF2B84B,
            )
        await _send_interaction_card(
            interaction,
            kind="notification" if ok else "warning",
            title="✅ Список горожан обновлён" if ok else "❌ Изменения не сохранены",
            description=(
                f"Удалено: **{len(removed)}**. Осталось: **{len(_citizen_ids(city))}**.\n{sync_text}"
            ),
            state=state,
            city=city,
            followup=True,
            color=0x59B77A if ok else 0xD85C5C,
        )


class CityCitizenRemoveView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
        *,
        page: int,
        guild: discord.Guild | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        self.guild = guild
        state = store.get(guild.id) if guild is not None else None
        city = state.cities.get(city_id) if state else None
        citizens = _citizen_ids(city or {})
        self.page_count = max(1, (len(citizens) + 24) // 25)
        self.page = min(max(page, 0), self.page_count - 1)
        page_ids = citizens[self.page * 25 : (self.page + 1) * 25]
        if page_ids:
            self.add_item(CityCitizenRemoveSelect(self, page_ids))
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.page_count - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.user.id != self.mayor_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Эта панель списка горожан открыта другому мэру.",
            )
            return False
        self.guild = interaction.guild
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if city is None or _mayor_id(city) != interaction.user.id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Доступ изменился",
                description="Вы больше не управляете этим городом.",
                state=state,
            )
            return False
        return True

    @discord.ui.button(label="Назад", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = CityCitizenRemoveView(
            self.bot,
            self.store,
            self.city_id,
            self.mayor_id,
            page=self.page - 1,
            guild=interaction.guild,
        )
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="Далее", emoji="➡️", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = CityCitizenRemoveView(
            self.bot,
            self.store,
            self.city_id,
            self.mayor_id,
            page=self.page + 1,
            guild=interaction.guild,
        )
        await interaction.response.edit_message(view=view)


async def _management_guard(
    interaction: discord.Interaction,
    store: UnifiedDiscordStore,
    city_id: str,
    mayor_id: int,
) -> tuple[UnifiedState | None, dict[str, Any] | None]:
    if interaction.guild is None or interaction.user.id != mayor_id:
        await _send_interaction_card(
            interaction,
            kind="warning",
            title="❌ Нет доступа",
            description="Панель привязана к другому Discord ID.",
        )
        return None, None
    state = store.get(interaction.guild.id)
    city = state.cities.get(city_id) if state else None
    if state is None or city is None or city.get("status") != "approved" or _mayor_id(city) != mayor_id:
        await _send_interaction_card(
            interaction,
            kind="warning",
            title="❌ Город недоступен",
            description="Город не найден или руководство было изменено администрацией.",
            state=state,
        )
        return None, None
    return state, city


async def _save_and_sync(
    interaction: discord.Interaction,
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
    previous: dict[str, Any],
    *,
    rename_thread: bool = False,
) -> tuple[bool, str]:
    city["updated_at"] = _now_iso()
    try:
        await store.save(state)
    except Exception as exc:
        state.cities[city_id] = previous
        return False, f"Изменения не сохранены: {exc}"

    ok, text = await sync_registry_post(bot, state, city_id, city, rename_thread=rename_thread)
    try:
        await store.save(state)
    except Exception:
        log.exception("Не удалось сохранить статус синхронизации города %s", city_id)
    await _edit_review_message(bot, state, city_id, city)
    if not ok:
        await _send_city_log(
            bot,
            state,
            title="⚠️ Данные города сохранены без синхронизации",
            description=f"Изменения внесены, но публикация не обновилась: {text}",
            city_id=city_id,
            city=city,
            color=0xF2B84B,
        )
        return True, f"Данные сохранены. ⚠️ {text}"
    return True, text


class CityRenameModal(discord.ui.Modal):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
        *,
        current_name: str,
    ) -> None:
        super().__init__(title="Изменение названия города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        self.name_input = discord.ui.TextInput(
            label="Новое название",
            min_length=1,
            max_length=20,
            default=current_name[:20],
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state, city = await _management_guard(interaction, self.store, self.city_id, self.mayor_id)
        if state is None or city is None:
            return
        name = str(self.name_input).strip()
        if _name_taken(state, name, exclude=self.city_id):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Название занято",
                description="Другой действующий город уже использует это название.",
                state=state,
                city=city,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock(state.guild_id, self.city_id):
            previous = copy.deepcopy(city)
            city["name"] = name
            ok, text = await _save_and_sync(
                interaction, self.bot, self.store, state, self.city_id, city, previous, rename_thread=True
            )
        await _send_interaction_card(
            interaction,
            kind="notification" if ok else "warning",
            title="✅ Название изменено" if ok else "❌ Ошибка изменения",
            description=text,
            state=state,
            city=city if ok else previous,
            followup=True,
            color=0x59B77A if ok else 0xD85C5C,
        )


class CityDescriptionModal(discord.ui.Modal):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
        *,
        current_description: str,
    ) -> None:
        super().__init__(title="Изменение описания города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        self.description_input = discord.ui.TextInput(
            label="Описание и концепция",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=2000,
            default=current_description[:2000],
        )
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state, city = await _management_guard(interaction, self.store, self.city_id, self.mayor_id)
        if state is None or city is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock(state.guild_id, self.city_id):
            previous = copy.deepcopy(city)
            city["description"] = str(self.description_input).strip()
            ok, text = await _save_and_sync(interaction, self.bot, self.store, state, self.city_id, city, previous)
        await _send_interaction_card(
            interaction,
            kind="notification" if ok else "warning",
            title="✅ Описание изменено" if ok else "❌ Ошибка изменения",
            description=text,
            state=state,
            city=city if ok else previous,
            followup=True,
            color=0x59B77A if ok else 0xD85C5C,
        )


class CityBannerModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, city_id: str, mayor_id: int) -> None:
        super().__init__(title="Главный баннер города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        self.file_label = discord.ui.Label(
            text="Локальный файл баннера",
            description="Один большой PNG/JPG/WEBP/GIF до 10 МБ. Внешние ссылки не используются.",
            component=discord.ui.FileUpload(
                custom_id="city_main_banner_file",
                required=True,
                min_values=1,
                max_values=1,
            ),
        )
        self.add_item(self.file_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state, city = await _management_guard(interaction, self.store, self.city_id, self.mayor_id)
        if state is None or city is None or interaction.guild is None:
            return
        component = self.file_label.component
        attachments = list(component.values) if isinstance(component, discord.ui.FileUpload) else []
        if not attachments:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Баннер не выбран",
                description="Прикрепите настоящий файл изображения.",
                state=state,
                city=city,
            )
            return
        attachment = attachments[0]
        try:
            _validate_attachment(attachment)
        except ValueError as exc:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Некорректный баннер",
                description=str(exc),
                state=state,
                city=city,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        suffix = _safe_extension(attachment.filename, attachment.content_type or "")
        destination = _city_upload_folder(interaction.guild.id, self.city_id) / f"main_banner{suffix}"
        previous = copy.deepcopy(city)
        old_path = str(city.get("bannerPath", "") or "")
        try:
            relative = await _save_attachment_local(attachment, destination)
        except Exception as exc:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Баннер не сохранён",
                description=f"Ошибка локального файла: `{exc}`",
                state=state,
                city=city,
                followup=True,
            )
            return
        async with _lock(state.guild_id, self.city_id):
            city["bannerPath"] = relative
            city["banner_path"] = relative
            city["banner_url"] = ""
            city["bannerFileStatus"] = "active"
            ok, text = await _save_and_sync(interaction, self.bot, self.store, state, self.city_id, city, previous)
        if not ok:
            _delete_local_paths([relative])
            if old_path:
                city["bannerPath"] = old_path
                city["banner_path"] = old_path
        elif old_path and old_path != relative:
            old_resolved = _resolve_project_path(old_path)
            if old_resolved.is_file() and old_resolved != destination:
                try:
                    old_resolved.unlink()
                except OSError:
                    pass
        await _send_interaction_card(
            interaction,
            kind="notification" if ok else "warning",
            title="✅ Баннер обновлён" if ok else "❌ Ошибка баннера",
            description=text,
            state=state,
            city=city if ok else previous,
            followup=True,
            color=0x59B77A if ok else 0xD85C5C,
        )


class CityEditDataModal(discord.ui.Modal):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
        *,
        current_city: dict[str, Any],
    ) -> None:
        super().__init__(title="Координаты и данные города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        self.style_input = discord.ui.TextInput(
            label="Архитектурный стиль",
            max_length=300,
            default=str(current_city.get("style", ""))[:300],
        )
        self.overworld_input = discord.ui.TextInput(
            label="Координаты в Верхнем мире",
            max_length=100,
            default=str(current_city.get("overworld_coords", ""))[:100],
        )
        self.nether_input = discord.ui.TextInput(
            label="Нижний мир и ветка метро",
            max_length=150,
            default=str(current_city.get("nether_coords", ""))[:150],
        )
        self.add_item(self.style_input)
        self.add_item(self.overworld_input)
        self.add_item(self.nether_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state, city = await _management_guard(interaction, self.store, self.city_id, self.mayor_id)
        if state is None or city is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock(state.guild_id, self.city_id):
            previous = copy.deepcopy(city)
            city["style"] = str(self.style_input).strip()
            city["overworld_coords"] = str(self.overworld_input).strip()
            city["nether_coords"] = str(self.nether_input).strip()
            ok, text = await _save_and_sync(interaction, self.bot, self.store, state, self.city_id, city, previous)
        await _send_interaction_card(
            interaction,
            kind="notification" if ok else "warning",
            title="✅ Данные изменены" if ok else "❌ Ошибка изменения",
            description=text,
            state=state,
            city=city if ok else previous,
            followup=True,
            color=0x59B77A if ok else 0xD85C5C,
        )


async def _send_leadership_service_message(
    bot: commands.Bot,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
    *,
    leader_type: str,
    old_id: int,
    new_id: int,
    moderator_id: int,
) -> None:
    thread = await _thread_channel(bot, _get_message_id(city, "registryThreadId", "registry_thread_id"))
    if thread is None:
        return
    label = "мэра" if leader_type == "mayor" else "заместителя мэра"
    embed = _simple_embed(
        "🔄 Изменение руководства города",
        (
            f"Администрация изменила {label} города **{city.get('name', city_id)}**.\n\n"
            f"**Предыдущий руководитель:** <@{old_id}> (`{old_id}`)\n"
            f"**Новый руководитель:** <@{new_id}> (`{new_id}`)\n"
            f"**Изменение выполнил:** <@{moderator_id}> (`{moderator_id}`)"
        ),
        color=0x5865F2,
        footer=f"FunFernus • {city_id} • Служебное сообщение",
    )
    try:
        await _send_channel_card(thread, kind="leadership", embed=embed, state=state, city=city)
    except discord.HTTPException:
        log.exception("Не удалось отправить служебное сообщение о руководстве %s", city_id)


async def _replace_city_leader(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    interaction: discord.Interaction,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
    *,
    leader_type: str,
    new_member: discord.Member,
) -> tuple[bool, str]:
    guild = interaction.guild
    if guild is None:
        return False, "Сервер недоступен."
    if new_member.bot:
        return False, "Руководителем нельзя назначить бота."
    old_mayor = _mayor_id(city)
    old_deputy = _deputy_id(city)
    old_id = old_mayor if leader_type == "mayor" else old_deputy
    if new_member.id == old_id:
        return False, "Этот пользователь уже занимает выбранную должность."
    if leader_type == "mayor" and new_member.id == old_deputy:
        return False, "Заместитель не может одновременно стать мэром без отдельной замены заместителя."
    if leader_type == "deputy" and new_member.id == old_mayor:
        return False, "Мэр и заместитель не могут быть одним человеком."
    other_city = _find_person_city(state, new_member.id, exclude=city_id)
    if other_city is not None:
        return False, f"Выбранный пользователь уже связан с другим городом `{other_city[0]}`."

    previous = copy.deepcopy(city)
    new_mayor = new_member.id if leader_type == "mayor" else old_mayor
    new_deputy = new_member.id if leader_type == "deputy" else old_deputy
    _set_leaders(city, new_mayor, new_deputy)
    _set_citizen_ids(city, _citizen_ids(city))
    _set_citizen_absent_ids(city, _citizen_absent_ids(city))
    city[f"{leader_type}Present"] = True
    city[f"{leader_type}_present"] = True
    city[f"{leader_type}ChangedAt"] = _now_iso()
    city[f"{leader_type}_changed_at"] = city[f"{leader_type}ChangedAt"]
    history = city.setdefault("leadershipHistory", city.get("leadership_history", []))
    history.append(
        {
            "type": leader_type,
            "oldId": old_id,
            "newId": new_member.id,
            "moderatorId": interaction.user.id,
            "changedAt": _now_iso(),
        }
    )
    city["leadership_history"] = history
    _refresh_allowed_writers(
        guild,
        state,
        city,
        getattr(bot, "admin_user_ids", set()),
        bot.user.id if bot.user else 0,
    )

    mayor_role: discord.Role | None = None
    old_mayor_member: discord.Member | None = None
    if leader_type == "mayor":
        role_ids = state.roles.get("city_mayor", [])
        mayor_role = guild.get_role(role_ids[0]) if role_ids else None
        if mayor_role is None:
            state.cities[city_id] = previous
            return False, "Роль мэра не настроена или удалена."
        old_mayor_member = await _member(guild, old_id)
        try:
            if mayor_role not in new_member.roles:
                await new_member.add_roles(mayor_role, reason=f"Назначен мэром города {city_id}")
            if old_mayor_member is not None and mayor_role in old_mayor_member.roles:
                await old_mayor_member.remove_roles(mayor_role, reason=f"Снят с должности мэра города {city_id}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            state.cities[city_id] = previous
            if mayor_role in new_member.roles:
                try:
                    await new_member.remove_roles(mayor_role, reason=f"Откат смены мэра {city_id}")
                except discord.HTTPException:
                    pass
            if old_mayor_member is not None and mayor_role not in old_mayor_member.roles:
                try:
                    await old_mayor_member.add_roles(mayor_role, reason=f"Откат смены мэра {city_id}")
                except discord.HTTPException:
                    pass
            return False, f"Не удалось изменить роль мэра: {exc}"

    try:
        await store.save(state)
    except Exception as exc:
        state.cities[city_id] = previous
        if leader_type == "mayor" and mayor_role is not None:
            try:
                if mayor_role in new_member.roles:
                    await new_member.remove_roles(mayor_role, reason=f"Откат смены мэра {city_id}")
                if old_mayor_member is not None and mayor_role not in old_mayor_member.roles:
                    await old_mayor_member.add_roles(mayor_role, reason=f"Откат смены мэра {city_id}")
            except discord.HTTPException:
                log.exception("Не удалось откатить роли после ошибки хранилища %s", city_id)
        return False, f"Изменение не сохранено: {exc}"

    sync_ok, sync_text = await sync_registry_post(bot, state, city_id, city)
    await _edit_review_message(bot, state, city_id, city)
    await _send_leadership_service_message(
        bot,
        state,
        city_id,
        city,
        leader_type=leader_type,
        old_id=old_id,
        new_id=new_member.id,
        moderator_id=interaction.user.id,
    )
    await _send_city_log(
        bot,
        state,
        title="🔄 Изменено руководство города",
        description=(
            f"Должность: **{'Мэр' if leader_type == 'mayor' else 'Заместитель мэра'}**.\n"
            f"Старый руководитель: <@{old_id}> (`{old_id}`).\n"
            f"Новый руководитель: <@{new_member.id}> (`{new_member.id}`).\n"
            f"Модератор: <@{interaction.user.id}> (`{interaction.user.id}`).\n"
            f"Синхронизация публикации: {sync_text}"
        ),
        city_id=city_id,
        city=city,
        color=0x5865F2,
    )
    try:
        await store.save(state)
    except Exception:
        log.exception("Не удалось сохранить статус после смены руководства %s", city_id)
    if sync_ok:
        return True, "Руководство изменено, список разрешённых авторов и публикация обновлены."
    return True, f"Руководство изменено. ⚠️ {sync_text}"


class LeadershipUserSelect(discord.ui.UserSelect):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        leader_type: str,
        moderator_id: int,
    ) -> None:
        label = "нового мэра" if leader_type == "mayor" else "нового заместителя"
        super().__init__(placeholder=f"Выберите {label}", min_values=1, max_values=1)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.leader_type = leader_type
        self.moderator_id = moderator_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user.id != self.moderator_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Эта панель открыта другому модератору.",
            )
            return
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None or not _is_city_staff_member(
            interaction.user,
            interaction.guild,
            state,
            getattr(self.bot, "admin_user_ids", set()),
        ):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Город не найден или права модерации были изменены.",
                state=state,
            )
            return
        selected = self.values[0]
        member = await _member(interaction.guild, selected.id)
        if member is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Пользователь не на сервере",
                description="Выбранный Discord ID не принадлежит текущему участнику сервера.",
                state=state,
                city=city,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock(interaction.guild.id, self.city_id):
            ok, text = await _replace_city_leader(
                self.bot,
                self.store,
                interaction,
                state,
                self.city_id,
                city,
                leader_type=self.leader_type,
                new_member=member,
            )
        await _send_interaction_card(
            interaction,
            kind="notification" if ok else "warning",
            title="✅ Руководство изменено" if ok else "❌ Смена не выполнена",
            description=text,
            state=state,
            city=city,
            followup=True,
            color=0x59B77A if ok else 0xD85C5C,
        )


class LeadershipUserSelectView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        leader_type: str,
        moderator_id: int,
    ) -> None:
        super().__init__(timeout=600)
        self.add_item(LeadershipUserSelect(bot, store, city_id, leader_type, moderator_id))


class CityLeadershipAdminView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        moderator_id: int,
    ) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.moderator_id = moderator_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.user.id != self.moderator_id:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Эта административная панель открыта другому пользователю.",
            )
            return False
        state = self.store.get(interaction.guild.id)
        if state is None or not _is_city_staff_member(
            interaction.user,
            interaction.guild,
            state,
            getattr(self.bot, "admin_user_ids", set()),
        ):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет прав модерации",
                description="Ваш доступ к управлению руководством был отозван.",
                state=state,
            )
            return False
        return True

    @discord.ui.button(label="Сменить мэра", emoji="👑", style=discord.ButtonStyle.danger)
    async def change_mayor(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        city = state.cities.get(self.city_id) if state else None
        await _send_interaction_card(
            interaction,
            kind="leadership",
            title="👑 Выберите нового мэра",
            description="Проверка и назначение выполняются исключительно по Discord ID.",
            state=state,
            city=city,
            view=LeadershipUserSelectView(self.bot, self.store, self.city_id, "mayor", interaction.user.id),
        )

    @discord.ui.button(label="Сменить заместителя", emoji="🛡️", style=discord.ButtonStyle.danger)
    async def change_deputy(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        city = state.cities.get(self.city_id) if state else None
        await _send_interaction_card(
            interaction,
            kind="leadership",
            title="🛡️ Выберите нового заместителя",
            description="Новый пользователь должен находиться на сервере и отличаться от мэра.",
            state=state,
            city=city,
            view=LeadershipUserSelectView(self.bot, self.store, self.city_id, "deputy", interaction.user.id),
        )

    @discord.ui.button(label="Проверить публикацию", emoji="🔎", style=discord.ButtonStyle.secondary)
    async def check_registry(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None:
            return
        thread = await _thread_channel(self.bot, _get_message_id(city, "registryThreadId", "registry_thread_id"))
        if thread is None:
            city["registryStatus"] = "deleted"
            city["registry_status"] = "deleted"
            city["registryDeletedAt"] = _now_iso()
            city["registry_deleted_at"] = city["registryDeletedAt"]
            await self.store.save(state)
            text = "Публикация удалена или недоступна. Статус сохранён в JSON и показан в панели."
            kind = "warning"
        else:
            city["registryStatus"] = "active"
            city["registry_status"] = "active"
            await self.store.save(state)
            text = f"Публикация доступна: <#{thread.id}>."
            kind = "notification"
        await _send_interaction_card(
            interaction,
            kind=kind,
            title="🔎 Проверка публикации",
            description=text,
            state=state,
            city=city,
            color=0x59B77A if thread else 0xF2B84B,
        )


async def publish_city_panels(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    guild: discord.Guild,
    state: UnifiedState,
    *,
    only_kind: str | None = None,
    run_audit: bool = True,
) -> tuple[bool, str]:
    if only_kind not in {None, "application", "management"}:
        return False, "Неизвестный тип панели городов."

    async with _panel_publish_lock(guild.id):
        admin_ids = getattr(bot, "admin_user_ids", set())
        if run_audit:
            await _migrate_legacy_city_assets(bot, store, guild, state)
            await _audit_city_state(bot, store, guild, state, admin_ids)

        return await _publish_city_panels_locked(
            bot,
            store,
            guild,
            state,
            only_kind=only_kind,
        )


async def _publish_city_panels_locked(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    guild: discord.Guild,
    state: UnifiedState,
    *,
    only_kind: str | None,
) -> tuple[bool, str]:
    application_channel = await _text_channel(bot, state.channels.get("city_application", 0))
    management_channel = await _text_channel(bot, state.channels.get("city_management", 0))
    review_channel = await _text_channel(bot, state.channels.get("city_review", 0))
    registry_channel = await _forum_channel(bot, state.channels.get("city_registry", 0))
    logs_channel = await _text_channel(bot, state.channels.get("city_logs", 0))
    if only_kind == "application":
        if application_channel is None:
            return False, "Канал подачи заявок не настроен или удалён."
    elif only_kind == "management":
        if management_channel is None:
            return False, "Канал управления городом не настроен или удалён."
    else:
        missing: list[str] = []
        if application_channel is None:
            missing.append("канал подачи")
        if review_channel is None:
            missing.append("канал рассмотрения")
        if registry_channel is None:
            missing.append("форум реестра")
        if management_channel is None:
            missing.append("канал управления")
        if logs_channel is None:
            missing.append("канал логов")
        if missing:
            return False, "Не настроены или удалены: " + ", ".join(missing) + "."
        if not state.roles.get("city_mayor"):
            return False, "Не выбрана роль мэра."

    accent = int(state.options.get("accent_color", 0x19B9D1))

    def panel_payload(kind: str) -> tuple[discord.ui.LayoutView, discord.File]:
        if kind == "application":
            title = str(state.texts.get("city_application_title", "Регистрация города FunFernus"))
            body = (
                str(
                    state.texts.get(
                        "city_application_description",
                        "Нажмите кнопку ниже, выберите мэра и заместителя, затем заполните данные города.",
                    )
                )
                + "\n\n**Как проходит регистрация**\n"
                + "• Выбор мэра и заместителя через меню пользователей Discord.\n"
                + "• Заполнение названия, стиля, координат и описания.\n"
                + "• Загрузка настоящих файлов скриншотов.\n"
                + "• Рассмотрение администрацией и публикация в реестре."
            )
            footer = str(state.texts.get("city_application_footer", "FunFernus • Реестр городов"))
            source_view = CityApplicationPanelView(bot, store)
        else:
            title = "⚙️ Управление зарегистрированным городом"
            body = (
                "Бот определяет город по вашему Discord ID. Через панель можно менять название, описание, "
                "главный баннер, координаты и архитектурный стиль.\n\n"
                "**Безопасность**\n"
                "• Права не зависят от ника.\n"
                "• Руководство меняет только настроенная администрация.\n"
                "• Все изменения и ошибки записываются в отдельный канал логов."
            )
            footer = "FunFernus • Управление городом"
            source_view = CityManagementLauncherView(bot, store)

        source_button = next(
            item for item in source_view.children if isinstance(item, discord.ui.Button)
        )
        button = discord.ui.Button(
            label=source_button.label,
            emoji=source_button.emoji,
            style=source_button.style,
            custom_id=source_button.custom_id,
        )
        button.callback = source_button.callback
        action_row = discord.ui.ActionRow()
        action_row.add_item(button)

        path = _banner_path(kind, state=state)
        suffix = path.suffix.lower() if path.suffix.lower() in IMAGE_EXTENSIONS else ".png"
        filename = f"funfernus_city_{kind}_panel{suffix}"
        layout = build_framed_view(
            title=title,
            body=body,
            banner_url=f"attachment://{filename}",
            color=accent,
            footer=footer,
            action_row=action_row,
            timeout=None,
        )
        return layout, discord.File(path, filename=filename)

    async def upsert(
        channel: discord.TextChannel,
        key: str,
        *,
        kind: str,
    ) -> discord.Message:
        layout, banner_file = panel_payload(kind)
        message_id = int(state.messages.get(key, 0) or 0)
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                return await message.edit(
                    content=None,
                    embeds=[],
                    attachments=[banner_file],
                    view=layout,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                # Исходное сообщение действительно удалено — создаём его заново.
                layout, banner_file = panel_payload(kind)
            except (discord.Forbidden, discord.HTTPException):
                # При 429/5xx нельзя сразу создавать дубликат панели. Ошибка
                # передаётся наружу, а discord.py сам соблюдает Retry-After.
                raise
        return await channel.send(
            file=banner_file,
            view=layout,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    updated: dict[str, discord.Message] = {}
    try:
        if only_kind in {None, "application"}:
            assert application_channel is not None
            updated["application"] = await upsert(
                application_channel,
                "city_application",
                kind="application",
            )
        if only_kind in {None, "management"}:
            assert management_channel is not None
            updated["management"] = await upsert(
                management_channel,
                "city_management",
                kind="management",
            )
    except Exception as exc:
        await _send_city_log(
            bot,
            state,
            title="❌ Ошибка публикации панелей городов",
            description=f"Discord вернул ошибку: `{exc}`",
            color=0xD85C5C,
        )
        return False, f"Discord не опубликовал панель: {exc}"

    if "application" in updated:
        state.messages["city_application"] = updated["application"].id
    if "management" in updated:
        state.messages["city_management"] = updated["management"].id
    try:
        await store.save(state)
    except Exception as exc:
        return False, f"Панель отправлена, но её ID не сохранён: {exc}"

    if only_kind is None:
        application_message = updated["application"]
        management_message = updated["management"]
        assert application_channel is not None and management_channel is not None
        await _send_city_log(
            bot,
            state,
            title="🚀 Панели системы городов опубликованы",
            description=(
                f"Панель заявок: <#{application_channel.id}> (`{application_message.id}`).\n"
                f"Панель управления: <#{management_channel.id}> (`{management_message.id}`)."
            ),
            color=0x59B77A,
        )
        return True, "Панели регистрации и управления городами опубликованы с локальными полноразмерными баннерами."

    panel_name = "подачи заявок" if only_kind == "application" else "управления городом"
    return True, f"Панель {panel_name} обновлена."


class CityPanelBannerModal(discord.ui.Modal):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        target: str,
    ) -> None:
        self.target = "management" if target == "management" else "application"
        label = "управления городом" if self.target == "management" else "регистрации городов"
        super().__init__(title=f"Баннер панели {label}", timeout=600)
        self.bot = bot
        self.store = store
        self.file_label = discord.ui.Label(
            text="Большой полноразмерный баннер",
            description="PNG/JPG/WEBP/GIF до 10 МБ. Рекомендуемый размер: 1200×630 или 1600×840.",
            component=discord.ui.FileUpload(
                custom_id=f"city_{self.target}_panel_banner_file",
                required=True,
                min_values=1,
                max_values=1,
            ),
        )
        self.add_item(self.file_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None or not _is_city_staff_member(
            interaction.user,
            interaction.guild,
            state,
            getattr(self.bot, "admin_user_ids", set()),
        ):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Изменять баннеры системы городов может только администрация.",
                state=state,
            )
            return
        component = self.file_label.component
        attachments = list(component.values) if isinstance(component, discord.ui.FileUpload) else []
        if not attachments:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Файл не выбран",
                description="Прикрепите большой баннер.",
                state=state,
            )
            return
        attachment = attachments[0]
        try:
            _validate_attachment(attachment)
        except ValueError as exc:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Некорректный файл",
                description=str(exc),
                state=state,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        suffix = _safe_extension(attachment.filename, attachment.content_type or "")
        option_key = (
            "city_management_banner_path"
            if self.target == "management"
            else "city_application_banner_path"
        )
        destination = STATIC_BANNER_DIR / f"custom_{self.target}{suffix}"
        previous = str(state.options.get(option_key, "") or "")
        try:
            relative = await _save_attachment_local(attachment, destination)
            state.options[option_key] = relative
        except Exception as exc:
            state.options[option_key] = previous
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Баннер не сохранён",
                description=f"Ошибка локального сохранения: `{exc}`",
                state=state,
                followup=True,
            )
            return

        # publish_city_panels сохраняет state после обновления нужной панели.
        # Раньше здесь был отдельный store.save(), поэтому одна загрузка
        # баннера дважды PATCH-ила служебное JSON-сообщение и провоцировала 429.
        try:
            ok, text = await publish_city_panels(
                self.bot,
                self.store,
                interaction.guild,
                state,
                only_kind=self.target,
                run_audit=False,
            )
        except Exception:
            # Даже если Discord не смог обновить панель, выбранный локальный
            # баннер не теряется после перезапуска. Сохраняем состояние один раз.
            await self.store.save(state)
            raise
        if not ok:
            # Целевой канал может быть ещё не настроен. В этом случае панель
            # обновить нельзя, но путь к баннеру всё равно сохраняется.
            await self.store.save(state)
        panel_name = "управления городом" if self.target == "management" else "подачи заявок"
        await _send_interaction_card(
            interaction,
            kind="notification" if ok else "warning",
            title="✅ Баннер установлен" if ok else "⚠️ Баннер сохранён",
            description=f"Баннер панели **{panel_name}** обновлён. {text}",
            state=state,
            followup=True,
            color=0x59B77A if ok else 0xF2B84B,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _notify_component_error(interaction, error, context="CityPanelBannerModal")


class CityChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, store: UnifiedDiscordStore, key: str, placeholder: str, channel_type: discord.ChannelType) -> None:
        super().__init__(
            placeholder=placeholder,
            channel_types=[channel_type],
            min_values=1,
            max_values=1,
        )
        self.store = store
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Хранилище недоступно",
                description="Перезапустите панель настройки.",
            )
            return
        state.channels[self.key] = self.values[0].id
        await self.store.save(state)
        await _send_interaction_card(
            interaction,
            kind="notification",
            title="✅ Канал сохранён",
            description=f"Выбран канал {self.values[0].mention} (`{self.values[0].id}`).",
            state=state,
            color=0x59B77A,
        )


class CityMayorRoleSelect(discord.ui.RoleSelect):
    def __init__(self, store: UnifiedDiscordStore) -> None:
        super().__init__(placeholder="Выберите роль Мэр", min_values=1, max_values=1)
        self.store = store

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            return
        role = self.values[0]
        if role.is_default() or role.managed:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Неподходящая роль",
                description="Выберите обычную отдельную роль, а не @everyone или роль интеграции.",
                state=state,
            )
            return
        bot_member = interaction.guild.me
        if bot_member is not None and role >= bot_member.top_role:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Неверная иерархия ролей",
                description="Роль бота должна находиться выше роли мэра.",
                state=state,
            )
            return
        state.roles["city_mayor"] = [role.id]
        await self.store.save(state)
        await _send_interaction_card(
            interaction,
            kind="notification",
            title="✅ Роль мэра сохранена",
            description=f"После одобрения будет выдаваться роль {role.mention} (`{role.id}`).",
            state=state,
            color=0x59B77A,
        )


class CityStaffRoleSelect(discord.ui.RoleSelect):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(placeholder="Выберите роли администраторов и модераторов", min_values=1, max_values=10)
        self.bot = bot
        self.store = store

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            return
        roles = [role for role in self.values if not role.is_default() and not role.managed]
        if not roles:
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Роли не выбраны",
                description="Выберите хотя бы одну обычную роль администрации.",
                state=state,
            )
            return
        state.roles["city_staff"] = [role.id for role in roles]
        for city in state.cities.values():
            _refresh_allowed_writers(
                interaction.guild,
                state,
                city,
                getattr(self.bot, "admin_user_ids", set()),
                self.bot.user.id if self.bot.user else 0,
            )
        await self.store.save(state)
        await _send_interaction_card(
            interaction,
            kind="notification",
            title="✅ Роли модерации сохранены",
            description="Выбраны: " + ", ".join(role.mention for role in roles),
            state=state,
            color=0x59B77A,
        )


class CityAllowedBotsSelect(discord.ui.UserSelect):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(placeholder="Выберите разрешённых ботов", min_values=1, max_values=10)
        self.bot = bot
        self.store = store

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            return
        members: list[discord.Member] = []
        for selected in self.values:
            member = await _member(interaction.guild, selected.id)
            if member is None or not member.bot:
                await _send_interaction_card(
                    interaction,
                    kind="warning",
                    title="❌ Выбран обычный пользователь",
                    description="В этом меню разрешено выбирать только ботов. Сам FunFernus Bot разрешён автоматически.",
                    state=state,
                )
                return
            members.append(member)
        state.options["city_allowed_bot_ids"] = [member.id for member in members]
        for city in state.cities.values():
            _refresh_allowed_writers(
                interaction.guild,
                state,
                city,
                getattr(self.bot, "admin_user_ids", set()),
                self.bot.user.id if self.bot.user else 0,
            )
        await self.store.save(state)
        await _send_interaction_card(
            interaction,
            kind="notification",
            title="✅ Разрешённые боты сохранены",
            description="Discord ID: " + ", ".join(f"`{member.id}`" for member in members),
            state=state,
            color=0x59B77A,
        )


class CitySetupView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.store = store

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        await _notify_component_error(
            interaction,
            error,
            context=f"CitySetupView/{item.__class__.__name__}",
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        state = self.store.get(interaction.guild.id)
        if state is None:
            return False
        if not _is_core_admin(interaction.user, interaction.guild, getattr(self.bot, "admin_user_ids", set())):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Настраивать систему городов могут владелец сервера и основные администраторы.",
                state=state,
            )
            return False
        return True

    @discord.ui.button(label="Каналы", emoji="📍", style=discord.ButtonStyle.secondary)
    async def channels(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = CityTransientView(timeout=600)
        view.add_item(CityChannelSelect(self.store, "city_application", "Канал подачи заявок", discord.ChannelType.text))
        view.add_item(CityChannelSelect(self.store, "city_review", "Канал рассмотрения заявок", discord.ChannelType.text))
        view.add_item(CityChannelSelect(self.store, "city_registry", "Форум реестра городов", discord.ChannelType.forum))
        view.add_item(CityChannelSelect(self.store, "city_management", "Канал управления городом", discord.ChannelType.text))
        view.add_item(CityChannelSelect(self.store, "city_logs", "Отдельный канал логов городов", discord.ChannelType.text))
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        await _send_interaction_card(
            interaction,
            kind="setup",
            title="📍 Выбор каналов",
            description="Последовательно выберите пять каналов системы городов.",
            state=state,
            view=view,
        )

    @discord.ui.button(label="Роль Мэр", emoji="👑", style=discord.ButtonStyle.secondary)
    async def mayor_role(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = CityTransientView(timeout=600)
        view.add_item(CityMayorRoleSelect(self.store))
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        await _send_interaction_card(
            interaction,
            kind="setup",
            title="👑 Роль мэра",
            description="Выберите роль, которая выдаётся принятому мэру.",
            state=state,
            view=view,
        )

    @discord.ui.button(label="Роли администрации", emoji="🛡️", style=discord.ButtonStyle.secondary)
    async def staff_roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = CityTransientView(timeout=600)
        view.add_item(CityStaffRoleSelect(self.bot, self.store))
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        await _send_interaction_card(
            interaction,
            kind="setup",
            title="🛡️ Роли модерации городов",
            description="Эти роли смогут рассматривать заявки, менять руководство и писать во всех городских публикациях.",
            state=state,
            view=view,
        )

    @discord.ui.button(label="Разрешённые боты", emoji="🤖", style=discord.ButtonStyle.secondary)
    async def allowed_bots(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = CityTransientView(timeout=600)
        view.add_item(CityAllowedBotsSelect(self.bot, self.store))
        state = self.store.get(interaction.guild.id) if interaction.guild else None
        await _send_interaction_card(
            interaction,
            kind="setup",
            title="🤖 Разрешённые боты",
            description="Выберите ботов, сообщения которых не должны удаляться в публикациях городов.",
            state=state,
            view=view,
        )

    @discord.ui.button(label="Баннер заявок", emoji="🏰", style=discord.ButtonStyle.primary)
    async def application_banner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            CityPanelBannerModal(self.bot, self.store, "application")
        )

    @discord.ui.button(label="Баннер управления", emoji="⚙️", style=discord.ButtonStyle.primary)
    async def management_banner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            CityPanelBannerModal(self.bot, self.store, "management")
        )

    @discord.ui.button(label="Опубликовать панели", emoji="🚀", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.edit_original_response(content="❌ Состояние системы городов не загружено.")
            return
        ok, text = await publish_city_panels(self.bot, self.store, interaction.guild, state)
        await _send_interaction_card(
            interaction,
            kind="notification" if ok else "warning",
            title="✅ Панели опубликованы" if ok else "❌ Публикация не выполнена",
            description=text,
            state=state,
            followup=True,
            color=0x59B77A if ok else 0xD85C5C,
        )


async def send_city_setup_message(
    interaction: discord.Interaction,
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    *,
    followup: bool = False,
) -> None:
    if interaction.guild is None:
        return
    state = store.get(interaction.guild.id) or await store.load_or_create(interaction.guild)
    role_ids = state.roles.get("city_mayor", [])
    staff_roles = state.roles.get("city_staff", [])
    embed = discord.Embed(
        title="⚙️ Настройка системы городов",
        description=(
            "Настройте пять каналов, роль мэра, роли администрации, разрешённых ботов и два отдельных баннера: "
            "для подачи заявок и для управления городом. Все изображения системы городов отправляются "
            "настоящими файлами через `attachment://`, без внешних URL."
        ),
        color=int(state.options.get("accent_color", 0x19B9D1)),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Роль мэра", value=f"<@&{role_ids[0]}>" if role_ids else "Не выбрана", inline=False)
    embed.add_field(
        name="Роли администрации",
        value=" ".join(f"<@&{role_id}>" for role_id in staff_roles) if staff_roles else "Не выбраны",
        inline=False,
    )
    embed.add_field(
        name="Канал логов",
        value=f"<#{state.channels.get('city_logs', 0)}>" if state.channels.get("city_logs", 0) else "Не выбран",
        inline=False,
    )
    embed.add_field(
        name="Баннер панели заявок",
        value=(
            "Выбран собственный локальный файл"
            if state.options.get("city_application_banner_path")
            else "Используется встроенный большой баннер"
        ),
        inline=True,
    )
    embed.add_field(
        name="Баннер панели управления",
        value=(
            "Выбран собственный локальный файл"
            if state.options.get("city_management_banner_path")
            else "Используется встроенный большой баннер"
        ),
        inline=True,
    )
    embed.set_footer(text="FunFernus • Настройка городов")
    embeds, file = _message_payload("setup", embed, state=state)
    kwargs = {
        "embeds": embeds,
        "file": file,
        "view": CitySetupView(bot, store),
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if followup:
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def _handle_city_question_dm(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    message: discord.Message,
) -> bool:
    if message.guild is not None or message.author.bot:
        return False
    if not message.content.strip() and not message.attachments:
        return False

    for state in list(store._states.values()):
        target: tuple[str, dict[str, Any]] | None = None
        for city_id, city in state.cities.items():
            if city.get("status") == "pending" and _mayor_id(city) == message.author.id and city.get("active_question"):
                target = (city_id, city)
                break
        if target is None:
            continue
        city_id, city = target
        guild = bot.get_guild(state.guild_id)
        if guild is None:
            continue
        async with _lock(state.guild_id, city_id):
            city = state.cities.get(city_id)
            if city is None or city.get("status") != "pending" or _mayor_id(city) != message.author.id or not city.get("active_question"):
                continue
            previous = copy.deepcopy(city)
            active = city.get("active_question", {})
            token = active.get("token")
            saved_paths: list[str] = []
            try:
                question_folder = _city_upload_folder(state.guild_id, city_id) / "questions" / str(token or secrets.token_hex(4))
                for index, attachment in enumerate(message.attachments[:MAX_SCREENSHOTS], 1):
                    _validate_attachment(attachment)
                    suffix = _safe_extension(attachment.filename, attachment.content_type or "")
                    saved_paths.append(
                        await _save_attachment_local(attachment, question_folder / f"answer_{index:02d}{suffix}")
                    )
            except Exception as exc:
                _delete_local_paths(saved_paths)
                try:
                    embed = _simple_embed(
                        "❌ Файлы ответа не сохранены",
                        f"Проверьте формат и размер вложений, затем отправьте ответ повторно. Ошибка: `{exc}`",
                        color=0xD85C5C,
                    )
                    await _send_user_card(message.author, kind="warning", embed=embed, state=state, city=city)
                except discord.HTTPException:
                    pass
                return True

            answer_text = message.content.strip() or "Ответ отправлен только файлами."
            history_item: dict[str, Any] | None = None
            for item in reversed(city.get("question_history", [])):
                if item.get("token") == token:
                    item["answer"] = answer_text
                    item["answered_at"] = _now_iso()
                    item["answerAttachmentPaths"] = saved_paths
                    item["answer_attachment_paths"] = list(saved_paths)
                    history_item = item
                    break
            city["active_question"] = {}
            try:
                await store.save(state)
            except Exception:
                state.cities[city_id] = previous
                _delete_local_paths(saved_paths)
                log.exception("Не удалось сохранить ответ мэра по заявке %s", city_id)
                try:
                    embed = _simple_embed(
                        "❌ Ответ не сохранён",
                        "Повторите отправку чуть позже.",
                        color=0xD85C5C,
                    )
                    await _send_user_card(message.author, kind="warning", embed=embed, state=state, city=previous)
                except discord.HTTPException:
                    pass
                return True

            await _edit_review_message(bot, state, city_id, city)
            review_channel = await _text_channel(bot, int(city.get("review_channel_id", 0)))
            if review_channel is not None:
                try:
                    review_message = await review_channel.fetch_message(
                        _get_message_id(city, "reviewMessageId", "review_message_id")
                    )
                    answer_embed = _simple_embed(
                        f"💬 Ответ мэра • {city_id}",
                        _trim(answer_text, 4000),
                        color=0x5865F2,
                        footer=f"FunFernus • {city_id} • Ответ на вопрос",
                    )
                    answer_embed.add_field(name="Мэр", value=f"<@{message.author.id}>\n`ID: {message.author.id}`", inline=False)
                    answer_message = await _send_channel_card(
                        review_channel,
                        kind="moderation",
                        embed=answer_embed,
                        state=state,
                        city=city,
                        content=f"↪️ Ответ относится к заявке `{city_id}` и сообщению `{review_message.id}`.",
                    )
                    attachment_message: discord.Message | None = None
                    files: list[discord.File] = []
                    for index, path in enumerate(_existing_local_paths(saved_paths), 1):
                        suffix = path.suffix.lower()
                        files.append(discord.File(path, filename=f"answer_attachment_{index:02d}{suffix}"))
                    if files:
                        attachment_message = await review_channel.send(
                            content=f"📎 **Вложения к ответу мэра по заявке `{city_id}`**",
                            files=files,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    if history_item is not None:
                        history_item["reviewAnswerMessageId"] = answer_message.id
                        history_item["review_answer_message_id"] = answer_message.id
                        history_item["reviewAnswerAttachmentsMessageId"] = attachment_message.id if attachment_message else 0
                        history_item["review_answer_attachments_message_id"] = attachment_message.id if attachment_message else 0
                        await store.save(state)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log.exception("Не удалось переслать ответ мэра по %s", city_id)

            await _send_city_log(
                bot,
                state,
                title="💬 Получен ответ мэра",
                description=(
                    f"Мэр: <@{message.author.id}> (`{message.author.id}`).\n"
                    f"Текст: {_trim(answer_text, 1500)}\n"
                    f"Локальных вложений: **{len(saved_paths)}**."
                ),
                city_id=city_id,
                city=city,
            )
            try:
                confirmation = _simple_embed(
                    "✅ Ответ передан администрации",
                    f"Ваш ответ по заявке `{city_id}` сохранён. Вложения переданы отдельным сообщением без URL.",
                    color=0x59B77A,
                )
                await _send_user_card(message.author, kind="notification", embed=confirmation, state=state, city=city)
            except discord.HTTPException:
                pass
            return True
    return False


async def _handle_city_thread_message(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    message: discord.Message,
) -> None:
    if message.guild is None or not isinstance(message.channel, discord.Thread):
        return
    # Системные события Discord не модерируются и не удаляются.
    if message.type is not discord.MessageType.default:
        return
    state = store.get(message.guild.id)
    if state is None:
        return
    found = _find_city_by_thread(state, message.channel.id)
    if found is None:
        return
    city_id, city = found
    if city.get("status") != "approved":
        return
    if _can_write_city_thread(
        message,
        message.guild,
        state,
        city,
        getattr(bot, "admin_user_ids", set()),
        bot.user.id if bot.user else 0,
    ):
        return

    deleted = False
    try:
        _bot_deleted_messages.add(message.id)
        await message.delete()
        deleted = True
    except discord.NotFound:
        deleted = True
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Не удалось удалить запрещённое сообщение %s в городе %s", message.id, city_id)

    content = message.content.strip() or "Сообщение без текста."
    attachment_names = ", ".join(item.filename for item in message.attachments) or "нет"
    await _send_city_log(
        bot,
        state,
        title="🚫 Удалено сообщение постороннего пользователя",
        description=(
            f"Пользователь: <@{message.author.id}> (`{message.author.id}`).\n"
            f"Город: **{city.get('name', city_id)}**.\n"
            f"Публикация: <#{message.channel.id}> (`{message.channel.id}`).\n"
            f"Время: <t:{int(datetime.now(timezone.utc).timestamp())}:F>.\n"
            f"Удаление: {'успешно' if deleted else 'не выполнено'}.\n"
            f"Содержимое: {_trim(content, 1200)}\n"
            f"Вложения: {_trim(attachment_names, 500)}"
        ),
        city_id=city_id,
        city=city,
        color=0xD85C5C,
    )

    cooldown_key = (message.channel.id, message.author.id)
    now = time.monotonic()
    if now - _warning_cooldowns.get(cooldown_key, 0.0) < WARNING_COOLDOWN_SECONDS:
        return
    _warning_cooldowns[cooldown_key] = now
    warning = _simple_embed(
        "🚫 Сообщение удалено",
        (
            f"В публикации города **{city.get('name', city_id)}** могут писать только мэр, заместитель, "
            "настроенная администрация и разрешённые боты. Проверка выполняется по Discord ID."
        ),
        color=0xD85C5C,
        footer=f"FunFernus • {city_id}",
    )
    try:
        await _send_user_card(message.author, kind="warning", embed=warning, state=state, city=city)
    except discord.HTTPException:
        try:
            temporary = await _send_channel_card(
                message.channel,
                kind="warning",
                embed=warning,
                state=state,
                city=city,
            )
            await temporary.delete(delay=8)
        except discord.HTTPException:
            pass


async def _mark_registry_thread_deleted(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    guild_id: int,
    thread_id: int,
) -> None:
    state = store.get(guild_id)
    if state is None:
        return
    found = _find_city_by_thread(state, thread_id)
    if found is None:
        return
    city_id, city = found
    if city.get("registryStatus") == "deleted":
        return
    city["registryStatus"] = "deleted"
    city["registry_status"] = "deleted"
    city["registryDeletedAt"] = _now_iso()
    city["registry_deleted_at"] = city["registryDeletedAt"]
    try:
        await store.save(state)
    except Exception:
        log.exception("Не удалось сохранить удаление публикации города %s", city_id)
    await _send_city_log(
        bot,
        state,
        title="🗑️ Городская публикация удалена",
        description=(
            f"Публикация `{thread_id}` города **{city.get('name', city_id)}** больше не существует. "
            "Система продолжает работать, а панель показывает статус удаления."
        ),
        city_id=city_id,
        city=city,
        color=0xD85C5C,
    )


async def _handle_raw_city_message_delete(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    payload: Any,
) -> None:
    message_id = int(getattr(payload, "message_id", 0) or 0)
    guild_id = int(getattr(payload, "guild_id", 0) or 0)
    if not message_id or not guild_id:
        return
    if message_id in _bot_deleted_messages:
        _bot_deleted_messages.discard(message_id)
        return
    state = store.get(guild_id)
    if state is None:
        return
    changes: list[tuple[str, dict[str, Any], str]] = []
    for city_id, city in state.cities.items():
        if message_id == _get_message_id(city, "registryMessageId", "registry_message_id"):
            city["registryStatus"] = "message_deleted"
            city["registry_status"] = "message_deleted"
            changes.append((city_id, city, "основная карточка реестра"))
        elif message_id == _get_message_id(city, "registryScreenshotsMessageId", "registry_screenshots_message_id"):
            city["registryStatus"] = "screenshots_deleted"
            city["registry_status"] = "screenshots_deleted"
            changes.append((city_id, city, "сообщение со скриншотами реестра"))
        elif message_id == _get_message_id(city, "reviewMessageId", "review_message_id"):
            city["reviewMessageStatus"] = "deleted"
            city["review_message_status"] = "deleted"
            changes.append((city_id, city, "модерационная карточка"))
        elif message_id == _get_message_id(city, "reviewScreenshotsMessageId", "review_screenshots_message_id"):
            city["reviewScreenshotsStatus"] = "deleted"
            city["review_screenshots_status"] = "deleted"
            changes.append((city_id, city, "скриншоты заявки"))
    if not changes:
        return
    try:
        await store.save(state)
    except Exception:
        log.exception("Не удалось сохранить статус удалённого сообщения города")
    for city_id, city, label in changes:
        await _send_city_log(
            bot,
            state,
            title="🗑️ Вручную удалено сообщение системы городов",
            description=f"Удалено: **{label}**. ID сообщения: `{message_id}`.",
            city_id=city_id,
            city=city,
            color=0xD85C5C,
        )


async def _handle_member_presence_change(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    member: discord.Member,
    *,
    present: bool,
) -> None:
    state = store.get(member.guild.id)
    if state is None:
        return
    leader_changes: list[tuple[str, dict[str, Any], str]] = []
    citizen_changes: list[tuple[str, dict[str, Any]]] = []
    for city_id, city in state.cities.items():
        _normalize_city(city)
        role = ""
        if _mayor_id(city) == member.id:
            role = "mayor"
        elif _deputy_id(city) == member.id:
            role = "deputy"
        if role:
            city[f"{role}Present"] = present
            city[f"{role}_present"] = present
            city[f"{role}{'JoinedAt' if present else 'LeftAt'}"] = _now_iso()
            city[f"{role}_{'joined_at' if present else 'left_at'}"] = city[f"{role}{'JoinedAt' if present else 'LeftAt'}"]
            leader_changes.append((city_id, city, role))
            continue
        if member.id in _citizen_ids(city):
            absent = _citizen_absent_ids(city)
            if present:
                absent.discard(member.id)
            else:
                absent.add(member.id)
            _set_citizen_absent_ids(city, absent)
            citizen_changes.append((city_id, city))

    if not leader_changes and not citizen_changes:
        return
    try:
        await store.save(state)
    except Exception:
        log.exception("Не удалось сохранить изменение присутствия участника города")

    for city_id, city, role in leader_changes:
        label = "мэр" if role == "mayor" else "заместитель мэра"
        await _edit_review_message(bot, state, city_id, city)
        if city.get("status") == "approved":
            await sync_registry_post(bot, state, city_id, city)
        await _send_city_log(
            bot,
            state,
            title="👤 Руководитель вернулся на сервер" if present else "⚠️ Руководитель покинул сервер",
            description=(
                f"Пользователь <@{member.id}> (`{member.id}`), должность **{label}**, "
                f"{'снова находится на сервере' if present else 'покинул Discord-сервер'}. "
                + ("Доступ по ID снова действует." if present else "Администрации необходимо назначить нового руководителя.")
            ),
            city_id=city_id,
            city=city,
            color=0x59B77A if present else 0xF2B84B,
        )

    for city_id, city in citizen_changes:
        await _edit_review_message(bot, state, city_id, city)
        if city.get("status") == "approved":
            await sync_registry_post(bot, state, city_id, city)
        await _send_city_log(
            bot,
            state,
            title="👥 Горожанин вернулся на сервер" if present else "👥 Горожанин покинул сервер",
            description=(
                f"Пользователь <@{member.id}> (`{member.id}`) "
                f"{'снова доступен на Discord-сервере' if present else 'покинул Discord-сервер, но сохранён в списке города'}."
            ),
            city_id=city_id,
            city=city,
            color=0x59B77A if present else 0xF2B84B,
        )


async def _handle_city_staff_role_change(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    before: discord.Member,
    after: discord.Member,
) -> None:
    if before.guild.id != after.guild.id or {role.id for role in before.roles} == {role.id for role in after.roles}:
        return
    state = store.get(after.guild.id)
    if state is None:
        return
    admin_ids = getattr(bot, "admin_user_ids", set())
    before_access = _is_city_staff_member(before, before.guild, state, admin_ids)
    after_access = _is_city_staff_member(after, after.guild, state, admin_ids)
    changed = False
    for city in state.cities.values():
        previous_ids = list(city.get("allowedWriterIds", []))
        _refresh_allowed_writers(
            after.guild,
            state,
            city,
            admin_ids,
            bot.user.id if bot.user else 0,
        )
        if previous_ids != city.get("allowedWriterIds", []):
            changed = True
    if changed:
        try:
            await store.save(state)
        except Exception:
            log.exception("Не удалось сохранить обновление ролей модерации городов")
    if before_access != after_access:
        await _send_city_log(
            bot,
            state,
            title="🛡️ Изменён доступ к публикациям городов",
            description=(
                f"Пользователь <@{after.id}> (`{after.id}`) "
                + (
                    "получил право модерации и отправки сообщений во всех городских публикациях."
                    if after_access
                    else "потерял право модерации и отправки сообщений во всех городских публикациях."
                )
            ),
            color=0x59B77A if after_access else 0xF2B84B,
        )


async def setup_cities(bot: commands.Bot, store: UnifiedDiscordStore, admin_ids: set[int]) -> None:
    bot.add_view(CityApplicationPanelView(bot, store))
    bot.add_view(CityReviewView(bot, store))
    bot.add_view(CityManagementLauncherView(bot, store))

    @bot.tree.command(name="настроить_города", description="Настроить регистрацию, реестр и управление городами")
    @app_commands.guild_only()
    async def cities_setup(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _is_core_admin(interaction.user, interaction.guild, admin_ids):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Команда доступна владельцу сервера и основным администраторам.",
            )
            return
        await send_city_setup_message(interaction, bot, store)

    @bot.tree.command(name="город_руководство", description="Открыть административную панель смены мэра и заместителя")
    @app_commands.describe(city_id="ID города, например CITY-0001")
    @app_commands.guild_only()
    async def city_leadership(interaction: discord.Interaction, city_id: str) -> None:
        if interaction.guild is None:
            return
        state = store.get(interaction.guild.id) or await store.load_or_create(interaction.guild)
        if not _is_city_staff_member(interaction.user, interaction.guild, state, admin_ids):
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Нет доступа",
                description="Менять руководство могут только настроенные администраторы и модераторы.",
                state=state,
            )
            return
        normalized_id = city_id.strip().upper()
        city = state.cities.get(normalized_id)
        if city is None or city.get("status") != "approved":
            await _send_interaction_card(
                interaction,
                kind="warning",
                title="❌ Город не найден",
                description="Укажите ID принятого города в формате `CITY-0001`.",
                state=state,
            )
            return
        embed = city_management_embed(normalized_id, city, state)
        embed.title = f"🛡️ Административное управление • {city.get('name', normalized_id)}"
        embed.description = (
            "Выберите, кого заменить. После смены старый руководитель сразу потеряет право писать в публикации, "
            "а новый получит его без перезапуска бота."
        )
        embeds, file = _message_payload("leadership", embed, city=city, state=state)
        await interaction.response.send_message(
            embeds=embeds,
            file=file,
            view=CityLeadershipAdminView(bot, store, normalized_id, interaction.user.id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.listen("on_message")
    async def city_message_listener(message: discord.Message) -> None:
        if await _handle_city_question_dm(bot, store, message):
            return
        await _handle_city_thread_message(bot, store, message)

    @bot.listen("on_raw_message_delete")
    async def city_raw_message_delete_listener(payload: Any) -> None:
        await _handle_raw_city_message_delete(bot, store, payload)

    @bot.listen("on_raw_thread_delete")
    async def city_raw_thread_delete_listener(payload: Any) -> None:
        guild_id = int(getattr(payload, "guild_id", 0) or 0)
        thread_id = int(getattr(payload, "thread_id", 0) or 0)
        if guild_id and thread_id:
            await _mark_registry_thread_deleted(bot, store, guild_id, thread_id)

    @bot.listen("on_thread_delete")
    async def city_thread_delete_listener(thread: discord.Thread) -> None:
        await _mark_registry_thread_deleted(bot, store, thread.guild.id, thread.id)

    @bot.listen("on_member_remove")
    async def city_member_remove_listener(member: discord.Member) -> None:
        await _handle_member_presence_change(bot, store, member, present=False)

    @bot.listen("on_member_join")
    async def city_member_join_listener(member: discord.Member) -> None:
        await _handle_member_presence_change(bot, store, member, present=True)

    @bot.listen("on_member_update")
    async def city_member_update_listener(before: discord.Member, after: discord.Member) -> None:
        await _handle_city_staff_role_change(bot, store, before, after)

    @bot.listen("on_guild_channel_delete")
    async def city_channel_delete_listener(channel: discord.abc.GuildChannel) -> None:
        state = store.get(channel.guild.id)
        if state is None:
            return
        keys = [key for key, value in state.channels.items() if key.startswith("city_") and int(value or 0) == channel.id]
        if not keys:
            return
        state.options.setdefault("city_deleted_channels", {})[str(channel.id)] = {
            "keys": keys,
            "name": channel.name,
            "deletedAt": _now_iso(),
        }
        try:
            await store.save(state)
        except Exception:
            log.exception("Не удалось сохранить удаление канала системы городов")
        await _send_city_log(
            bot,
            state,
            title="🗑️ Удалён канал системы городов",
            description=f"Канал **{channel.name}** (`{channel.id}`), назначения: {', '.join(keys)}.",
            color=0xD85C5C,
        )
