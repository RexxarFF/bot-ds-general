from __future__ import annotations

import asyncio
import copy
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from .unified_store import AssetRef, UnifiedDiscordStore, UnifiedState

log = logging.getLogger("funfernus-cities")

CITY_ID_RE = re.compile(r"CITY-\d{4,}")
DISCORD_ID_RE = re.compile(r"\d{15,22}")
_city_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _lock(guild_id: int, city_id: str) -> asyncio.Lock:
    return _city_locks.setdefault((guild_id, city_id), asyncio.Lock())


def _is_admin(interaction: discord.Interaction, admin_ids: set[int]) -> bool:
    return bool(
        interaction.guild
        and (
            interaction.guild.owner_id == interaction.user.id
            or interaction.user.id in admin_ids
            or (
                isinstance(interaction.user, discord.Member)
                and interaction.user.guild_permissions.administrator
            )
        )
    )


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


def _discord_id(value: str) -> int | None:
    match = DISCORD_ID_RE.search(value or "")
    return int(match.group()) if match else None


def _valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _split_urls(value: str, limit: int = 10) -> list[str]:
    result: list[str] = []
    for item in re.split(r"[\s]+", value.strip()):
        clean = item.strip().strip("<>")
        if clean and len(clean) <= 1000 and _valid_url(clean) and clean not in result:
            result.append(clean)
        if len(result) >= limit:
            break
    return result


def _screenshot_lines(urls: list[str]) -> str:
    if not urls:
        return "Скриншоты не приложены."
    lines: list[str] = []
    used = 0
    for index, url in enumerate(urls[:10], 1):
        line = f"[{index}. Открыть изображение]({url})"
        extra = len(line) + (1 if lines else 0)
        if used + extra > 1024:
            break
        lines.append(line)
        used += extra
    return "\n".join(lines) or "Скриншоты не приложены."


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


def _find_city_for_mayor(state: UnifiedState, mayor_id: int) -> tuple[str, dict[str, Any]] | None:
    for city_id, city in state.cities.items():
        if city.get("status") == "approved" and int(city.get("mayor_id", 0)) == mayor_id:
            return city_id, city
    return None


def _has_active_city(state: UnifiedState, mayor_id: int, *, exclude: str = "") -> bool:
    for city_id, city in state.cities.items():
        if city_id == exclude:
            continue
        if int(city.get("mayor_id", 0)) != mayor_id:
            continue
        if city.get("status") in {"pending", "approved"}:
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
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _user(bot: commands.Bot, user_id: int) -> discord.User | None:
    user = bot.get_user(user_id)
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def _delete_asset_message(store: UnifiedDiscordStore, guild: discord.Guild, message_id: int) -> None:
    if not message_id:
        return
    try:
        channel = await store.config_channel(guild)
        message = await channel.fetch_message(message_id)
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def city_review_embed(city_id: str, city: dict[str, Any], state: UnifiedState) -> discord.Embed:
    status = str(city.get("status", "pending"))
    accent = int(state.options.get("accent_color", 0x19B9D1))
    embed = discord.Embed(
        title=f"🏰 Заявка на регистрацию города • {city_id}",
        description=f"**Статус:** {_status_text(status)}",
        color=_status_color(status, accent),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Название", value=_trim(city.get("name"), 1024), inline=True)
    embed.add_field(name="Мэр", value=f"<@{int(city.get('mayor_id', 0))}>", inline=True)
    embed.add_field(name="Заместитель", value=f"<@{int(city.get('deputy_id', 0))}>", inline=True)
    embed.add_field(name="Архитектурный стиль", value=_trim(city.get("style")), inline=False)
    embed.add_field(name="Координаты • Верхний мир", value=_trim(city.get("overworld_coords")), inline=True)
    embed.add_field(name="Координаты • Нижний мир", value=_trim(city.get("nether_coords")), inline=True)
    embed.add_field(name="Описание города", value=_trim(city.get("description")), inline=False)
    embed.add_field(name="Скриншоты первых построек", value=_screenshot_lines(list(city.get("screenshots", []))), inline=False)
    embed.add_field(name="Заявку отправил", value=f"<@{int(city.get('applicant_id', 0))}>", inline=False)

    history = city.get("question_history", [])
    if history:
        latest = history[-1]
        question = _trim(latest.get("question"), 500)
        answer = _trim(latest.get("answer"), 500, "Ответ ещё не получен.")
        embed.add_field(name="Последний вопрос администрации", value=question, inline=False)
        embed.add_field(name="Ответ мэра", value=answer, inline=False)

    if status == "approved":
        embed.add_field(name="Одобрил", value=f"<@{int(city.get('reviewer_id', 0))}>", inline=True)
        if city.get("registry_thread_id"):
            embed.add_field(name="Карточка реестра", value=f"<#{int(city['registry_thread_id'])}>", inline=True)
    elif status == "rejected":
        embed.add_field(name="Причина отказа", value=_trim(city.get("rejection_reason")), inline=False)
        embed.add_field(name="Отклонил", value=f"<@{int(city.get('reviewer_id', 0))}>", inline=True)

    screenshots = list(city.get("screenshots", []))
    if screenshots:
        embed.set_image(url=screenshots[0])
    embed.set_footer(text=f"FunFernus • {city_id} • {_status_text(status)}")
    return embed


def city_registry_embed(city_id: str, city: dict[str, Any], state: UnifiedState) -> discord.Embed:
    accent = int(state.options.get("accent_color", 0x19B9D1))
    embed = discord.Embed(
        title=f"🏰 {_trim(city.get('name'), 200)}",
        description=_trim(city.get("description"), 4096),
        color=accent,
    )
    embed.add_field(name="Мэр", value=f"<@{int(city.get('mayor_id', 0))}>", inline=True)
    embed.add_field(name="Заместитель мэра", value=f"<@{int(city.get('deputy_id', 0))}>", inline=True)
    embed.add_field(name="Архитектурный стиль", value=_trim(city.get("style")), inline=False)
    embed.add_field(name="Верхний мир", value=_trim(city.get("overworld_coords")), inline=True)
    embed.add_field(name="Нижний мир и метро", value=_trim(city.get("nether_coords")), inline=True)
    embed.add_field(name="Скриншоты города", value=_screenshot_lines(list(city.get("screenshots", []))), inline=False)
    banner_url = str(city.get("banner_url", "")).strip()
    if banner_url:
        embed.set_image(url=banner_url)
    embed.set_footer(text=f"Официальный реестр FunFernus • ID города: {city_id}")
    return embed


def city_management_embed(city_id: str, city: dict[str, Any], state: UnifiedState) -> discord.Embed:
    accent = int(state.options.get("accent_color", 0x19B9D1))
    embed = discord.Embed(
        title=f"⚙️ Управление городом • {_trim(city.get('name'), 180)}",
        description=(
            "Изменения применяются к базе и сразу синхронизируются с карточкой "
            "в публичном реестре городов."
        ),
        color=accent,
    )
    embed.add_field(name="ID города", value=f"`{city_id}`", inline=True)
    embed.add_field(name="Мэр", value=f"<@{int(city.get('mayor_id', 0))}>", inline=True)
    embed.add_field(name="Заместитель", value=f"<@{int(city.get('deputy_id', 0))}>", inline=True)
    embed.add_field(name="Стиль", value=_trim(city.get("style"), 500), inline=False)
    embed.add_field(name="Верхний мир", value=_trim(city.get("overworld_coords"), 500), inline=True)
    embed.add_field(name="Нижний мир", value=_trim(city.get("nether_coords"), 500), inline=True)
    embed.add_field(name="Главный баннер", value="Установлен" if city.get("banner_url") else "Не установлен", inline=False)
    if city.get("banner_url"):
        embed.set_image(url=str(city["banner_url"]))
    embed.set_footer(text="FunFernus • Панель мэра")
    return embed


async def _edit_review_message(
    bot: commands.Bot,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
) -> None:
    channel = await _text_channel(bot, int(city.get("review_channel_id", 0)))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(city.get("review_message_id", 0)))
        view = CityReviewView(bot, bot.unified_store, city_id) if city.get("status") == "pending" else None  # type: ignore[attr-defined]
        await message.edit(embed=city_review_embed(city_id, city, state), view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        log.exception("Не удалось обновить модерационную карточку города %s", city_id)


async def sync_registry_post(
    bot: commands.Bot,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
    *,
    rename_thread: bool = False,
) -> tuple[bool, str]:
    thread = await _thread_channel(bot, int(city.get("registry_thread_id", 0)))
    if thread is None:
        return False, "Связанная тема реестра не найдена."
    try:
        if thread.archived:
            await thread.edit(archived=False, reason=f"Обновление карточки города {city_id}")
        message = await thread.fetch_message(int(city.get("registry_message_id", 0)))
        await message.edit(
            content=f"`{city_id}` • Официальная карточка города FunFernus",
            embed=city_registry_embed(city_id, city, state),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if rename_thread:
            await thread.edit(name=str(city.get("name", city_id))[:100], reason=f"Переименование города {city_id}")
    except discord.Forbidden:
        return False, "Боту не хватает прав для изменения темы реестра."
    except discord.HTTPException as exc:
        return False, f"Discord не обновил карточку реестра: {exc}"
    return True, "Карточка реестра обновлена."


async def create_registry_post(
    bot: commands.Bot,
    state: UnifiedState,
    city_id: str,
    city: dict[str, Any],
) -> tuple[discord.Thread | None, discord.Message | None, str]:
    forum = await _forum_channel(bot, state.channels.get("city_registry", 0))
    if forum is None:
        return None, None, "Форум-канал реестра не настроен или недоступен."

    kwargs: dict[str, Any] = {}
    if bool(getattr(forum.flags, "require_tag", False)):
        available = [tag for tag in forum.available_tags if not tag.moderated] or list(forum.available_tags)
        if not available:
            return None, None, "В форуме обязателен тег, но доступных тегов нет."
        kwargs["applied_tags"] = [available[0]]

    try:
        created = await forum.create_thread(
            name=str(city.get("name", city_id))[:100],
            content=f"`{city_id}` • Официальная карточка города FunFernus",
            embed=city_registry_embed(city_id, city, state),
            allowed_mentions=discord.AllowedMentions.none(),
            reason=f"Одобрена регистрация города {city_id}",
            **kwargs,
        )
    except discord.Forbidden:
        return None, None, "Боту не хватает прав для создания публикаций в форуме реестра."
    except discord.HTTPException as exc:
        return None, None, f"Discord не создал карточку реестра: {exc}"
    return created.thread, created.message, "Карточка реестра создана."


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
            await interaction.response.send_message("❌ Эта форма открыта другим пользователем.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Продолжить", emoji="➡️", style=discord.ButtonStyle.primary, row=2)
    async def continue_form(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        if not self.mayor_id or not self.deputy_id:
            await interaction.response.send_message("❌ Выберите мэра и заместителя.", ephemeral=True)
            return
        if self.mayor_id == self.deputy_id:
            await interaction.response.send_message("❌ Мэр и заместитель должны быть разными пользователями.", ephemeral=True)
            return
        mayor = await _member(interaction.guild, self.mayor_id)
        deputy = await _member(interaction.guild, self.deputy_id)
        if mayor is None or deputy is None or mayor.bot or deputy.bot:
            await interaction.response.send_message("❌ Выберите двух обычных участников сервера.", ephemeral=True)
            return
        if self.mayor_id != interaction.user.id and not _is_admin(interaction, getattr(self.bot, "admin_user_ids", set())):
            await interaction.response.send_message("❌ Заявку должен отправлять выбранный мэр.", ephemeral=True)
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message("❌ Хранилище бота не загружено.", ephemeral=True)
            return
        if _has_active_city(state, self.mayor_id):
            await interaction.response.send_message("❌ У выбранного мэра уже есть город или заявка на рассмотрении.", ephemeral=True)
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
            await interaction.response.send_message("❌ Эта форма больше недоступна.", ephemeral=True)
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message("❌ Хранилище бота не загружено.", ephemeral=True)
            return
        name = str(self.name_input).strip()
        if _name_taken(state, name):
            await interaction.response.send_message("❌ Город с таким названием уже зарегистрирован или рассматривается.", ephemeral=True)
            return
        if _has_active_city(state, self.mayor_id):
            await interaction.response.send_message("❌ У выбранного мэра уже есть город или активная заявка.", ephemeral=True)
            return

        token = secrets.token_hex(8)
        state.city_drafts[str(interaction.user.id)] = {
            "token": token,
            "applicant_id": interaction.user.id,
            "mayor_id": self.mayor_id,
            "deputy_id": self.deputy_id,
            "name": name,
            "style": str(self.style_input).strip(),
            "overworld_coords": str(self.overworld_input).strip(),
            "nether_coords": str(self.nether_input).strip(),
            "description": str(self.description_input).strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.store.save(state)
        embed = discord.Embed(
            title="📸 Последний этап регистрации",
            description=(
                "Прикрепите изображения первых построек или вставьте прямые ссылки. "
                "После отправки заявка сразу попадёт администрации."
            ),
            color=int(state.options.get("accent_color", 0x19B9D1)),
        )
        await interaction.response.send_message(
            embed=embed,
            view=CityScreenshotsView(self.bot, self.store, interaction.user.id, token),
            ephemeral=True,
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
            await interaction.response.send_message("❌ Эта форма открыта другим пользователем.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Добавить скриншоты и отправить", emoji="📸", style=discord.ButtonStyle.success)
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
            description="До 5 изображений PNG/JPG/WEBP/GIF, каждое до 10 МБ.",
            component=discord.ui.FileUpload(
                custom_id="city_application_screenshots",
                required=False,
                min_values=0,
                max_values=5,
            ),
        )
        self.links_label = discord.ui.Label(
            text="Ссылки на изображения",
            description="Необязательно. Каждую ссылку укажите с новой строки.",
            component=discord.ui.TextInput(
                custom_id="city_application_screenshot_links",
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=1800,
                placeholder="https://...",
            ),
        )
        self.add_item(self.files_label)
        self.add_item(self.links_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.user.id != self.applicant_id:
            await interaction.response.send_message("❌ Эта форма больше недоступна.", ephemeral=True)
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message("❌ Хранилище бота не загружено.", ephemeral=True)
            return
        draft = state.city_drafts.get(str(interaction.user.id))
        if not draft or draft.get("token") != self.token:
            await interaction.response.send_message("❌ Черновик заявки не найден. Начните регистрацию заново.", ephemeral=True)
            return
        review = await _text_channel(self.bot, state.channels.get("city_review", 0))
        if review is None:
            await interaction.response.send_message("❌ Канал рассмотрения городов ещё не настроен.", ephemeral=True)
            return
        if _has_active_city(state, int(draft.get("mayor_id", 0))) or _name_taken(state, str(draft.get("name", ""))):
            await interaction.response.send_message("❌ Пока вы заполняли форму, такая заявка уже появилась.", ephemeral=True)
            return

        file_component = self.files_label.component
        attachments = list(file_component.values) if isinstance(file_component, discord.ui.FileUpload) else []
        links_component = self.links_label.component
        raw_links = str(links_component.value).strip() if isinstance(links_component, discord.ui.TextInput) else ""
        external_links = _split_urls(raw_links)
        if not attachments and not external_links:
            await interaction.response.send_message("❌ Прикрепите хотя бы один скриншот или укажите ссылку.", ephemeral=True)
            return

        for attachment in attachments:
            try:
                self.store.validate_image(attachment)
            except ValueError as exc:
                await interaction.response.send_message(f"❌ `{attachment.filename}`: {exc}", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        saved_assets: list[AssetRef] = []
        try:
            for index, attachment in enumerate(attachments, 1):
                saved_assets.append(
                    await self.store.persist_asset(
                        interaction.guild,
                        attachment,
                        f"Заявка города {draft.get('name', '')} • скриншот {index}",
                    )
                )
        except Exception as exc:
            for asset in saved_assets:
                await _delete_asset_message(self.store, interaction.guild, asset.message_id)
            await interaction.followup.send(f"❌ Не удалось сохранить скриншоты: `{exc}`", ephemeral=True)
            return

        screenshots = [asset.url for asset in saved_assets] + external_links
        city_id = state.next_id("city", "CITY")
        city: dict[str, Any] = {
            **draft,
            "id": city_id,
            "status": "pending",
            "screenshots": screenshots[:10],
            "screenshot_assets": [asset.__dict__ for asset in saved_assets],
            "banner_url": "",
            "banner_asset": {},
            "question_history": [],
            "active_question": {},
            "review_channel_id": review.id,
            "review_message_id": 0,
            "registry_thread_id": 0,
            "registry_message_id": 0,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        city.pop("token", None)
        state.cities[city_id] = city
        state.city_drafts.pop(str(interaction.user.id), None)
        try:
            await self.store.save(state)
        except Exception as exc:
            state.cities.pop(city_id, None)
            state.city_drafts[str(interaction.user.id)] = draft
            for asset in saved_assets:
                await _delete_asset_message(self.store, interaction.guild, asset.message_id)
            await interaction.followup.send(f"❌ Не удалось сохранить заявку: `{exc}`", ephemeral=True)
            return

        try:
            message = await review.send(
                embed=city_review_embed(city_id, city, state),
                view=CityReviewView(self.bot, self.store, city_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            state.cities.pop(city_id, None)
            state.city_drafts[str(interaction.user.id)] = draft
            try:
                await self.store.save(state)
            except Exception:
                log.exception("Не удалось откатить заявку города %s после ошибки отправки", city_id)
            for asset in saved_assets:
                await _delete_asset_message(self.store, interaction.guild, asset.message_id)
            await interaction.followup.send(f"❌ Не удалось отправить заявку администрации: `{exc}`", ephemeral=True)
            return

        city["review_message_id"] = message.id
        try:
            await self.store.save(state)
        except Exception as exc:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            state.cities.pop(city_id, None)
            state.city_drafts[str(interaction.user.id)] = draft
            try:
                await self.store.save(state)
            except Exception:
                log.exception("Не удалось откатить заявку города %s после ошибки финального сохранения", city_id)
            for asset in saved_assets:
                await _delete_asset_message(self.store, interaction.guild, asset.message_id)
            await interaction.followup.send(f"❌ Не удалось завершить регистрацию заявки: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Заявка `{city_id}` отправлена администрации. Решение и вопросы придут мэру в личные сообщения.",
            ephemeral=True,
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
            await interaction.response.send_message("❌ Регистрация доступна только на сервере.", ephemeral=True)
            return
        state = self.store.get(interaction.guild.id) or await self.store.load_or_create(interaction.guild)
        if interaction.channel_id != state.channels.get("city_application", 0):
            await interaction.response.send_message("❌ Используйте официальную панель регистрации городов.", ephemeral=True)
            return
        if _has_active_city(state, interaction.user.id):
            await interaction.response.send_message("❌ У вас уже есть город или заявка на рассмотрении.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🏰 Выберите руководство города",
            description="Сначала укажите мэра и заместителя. Заявку должен отправлять выбранный мэр.",
            color=int(state.options.get("accent_color", 0x19B9D1)),
        )
        await interaction.response.send_message(
            embed=embed,
            view=MayorDeputyView(self.bot, self.store, interaction.user.id),
            ephemeral=True,
        )


class CityReviewView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, city_id: str = "") -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store
        self.city_id = city_id

    async def guard(self, interaction: discord.Interaction) -> tuple[UnifiedState | None, str, dict[str, Any] | None]:
        if interaction.guild is None or not _is_admin(interaction, getattr(self.bot, "admin_user_ids", set())):
            await interaction.response.send_message("❌ Нет доступа к модерации городов.", ephemeral=True)
            return None, "", None
        state = self.store.get(interaction.guild.id)
        if state is not None and interaction.channel_id != state.channels.get("city_review", 0):
            await interaction.response.send_message("❌ Используйте официальный канал рассмотрения городов.", ephemeral=True)
            return None, "", None
        city_id = self.city_id or _city_id(interaction.message)
        city = state.cities.get(city_id) if state and city_id else None
        if state is None or city is None:
            await interaction.response.send_message("❌ Заявка города не найдена в хранилище.", ephemeral=True)
            return None, "", None
        if city.get("status") != "pending":
            await interaction.response.send_message("❌ Эта заявка уже рассмотрена.", ephemeral=True)
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
            if city.get("status") != "pending":
                await interaction.followup.send("❌ Заявка уже рассмотрена.", ephemeral=True)
                return
            role_ids = state.roles.get("city_mayor", [])
            mayor_role = interaction.guild.get_role(role_ids[0]) if role_ids else None
            if mayor_role is None:
                await interaction.followup.send("❌ Роль мэра не настроена или удалена.", ephemeral=True)
                return
            mayor = await _member(interaction.guild, int(city.get("mayor_id", 0)))
            if mayor is None:
                await interaction.followup.send("❌ Пользователь-мэр больше не найден на сервере.", ephemeral=True)
                return

            thread, registry_message, error = await create_registry_post(self.bot, state, city_id, city)
            if thread is None or registry_message is None:
                await interaction.followup.send(f"❌ {error}", ephemeral=True)
                return
            role_already_present = mayor_role in mayor.roles
            try:
                if not role_already_present:
                    await mayor.add_roles(mayor_role, reason=f"Мэр зарегистрированного города {city_id}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                try:
                    await thread.delete(reason=f"Откат регистрации {city_id}: не выдана роль мэра")
                except discord.HTTPException:
                    pass
                await interaction.followup.send(f"❌ Не удалось выдать роль мэра: `{exc}`", ephemeral=True)
                return

            previous = copy.deepcopy(city)
            city.update(
                {
                    "status": "approved",
                    "reviewer_id": interaction.user.id,
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                    "registry_thread_id": thread.id,
                    "registry_message_id": registry_message.id,
                    "active_question": {},
                }
            )
            try:
                await self.store.save(state)
            except Exception as exc:
                state.cities[city_id] = previous
                if not role_already_present:
                    try:
                        await mayor.remove_roles(mayor_role, reason=f"Откат регистрации города {city_id}")
                    except discord.HTTPException:
                        log.exception("Не удалось убрать роль мэра при откате города %s", city_id)
                try:
                    await thread.delete(reason=f"Откат регистрации {city_id}: данные не сохранены")
                except discord.HTTPException:
                    log.exception("Не удалось удалить тему реестра при откате города %s", city_id)
                await interaction.followup.send(f"❌ Не удалось сохранить регистрацию города: `{exc}`", ephemeral=True)
                return
            if interaction.message:
                try:
                    await interaction.message.edit(embed=city_review_embed(city_id, city, state), view=None)
                except discord.HTTPException:
                    pass

            dm_delivered = True
            user = await _user(self.bot, int(city.get("mayor_id", 0)))
            if user is None:
                dm_delivered = False
            else:
                dm = discord.Embed(
                    title="✅ Город успешно зарегистрирован",
                    description=(
                        f"Город **{city.get('name')}** одобрен администрацией FunFernus.\n\n"
                        f"Вам выдана роль **{mayor_role.name}**, а карточка добавлена в <#{thread.id}>."
                    ),
                    color=0x59B77A,
                )
                dm.set_footer(text=f"FunFernus • {city_id}")
                try:
                    await user.send(embed=dm)
                except discord.HTTPException:
                    dm_delivered = False
            await interaction.followup.send(
                f"✅ Город `{city_id}` одобрен и опубликован в <#{thread.id}>."
                + ("" if dm_delivered else " ⚠️ ЛС мэру закрыты."),
                ephemeral=True,
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
        if state is not None and city is not None:
            if city.get("active_question"):
                await interaction.response.send_message("❌ Мэр ещё не ответил на предыдущий вопрос.", ephemeral=True)
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
        if interaction.guild is None or not _is_admin(interaction, getattr(self.bot, "admin_user_ids", set())):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None:
            await interaction.response.send_message("❌ Заявка не найдена.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock(interaction.guild.id, self.city_id):
            if city.get("status") != "pending":
                await interaction.followup.send("❌ Заявка уже рассмотрена.", ephemeral=True)
                return
            previous = copy.deepcopy(city)
            city.update(
                {
                    "status": "rejected",
                    "reviewer_id": interaction.user.id,
                    "rejection_reason": str(self.reason).strip(),
                    "rejected_at": datetime.now(timezone.utc).isoformat(),
                    "active_question": {},
                }
            )
            try:
                await self.store.save(state)
            except Exception as exc:
                state.cities[self.city_id] = previous
                await interaction.followup.send(f"❌ Не удалось сохранить отказ: `{exc}`", ephemeral=True)
                return
            await _edit_review_message(self.bot, state, self.city_id, city)
            dm_delivered = True
            user = await _user(self.bot, int(city.get("mayor_id", 0)))
            if user is None:
                dm_delivered = False
            else:
                embed = discord.Embed(
                    title="❌ Регистрация города отклонена",
                    description=f"Заявка города **{city.get('name')}** отклонена.\n\n**Причина:**\n{str(self.reason).strip()}",
                    color=0xD85C5C,
                )
                embed.set_footer(text=f"FunFernus • {self.city_id}")
                try:
                    await user.send(embed=embed)
                except discord.HTTPException:
                    dm_delivered = False
            await interaction.followup.send(
                "✅ Отказ сохранён и отправлен мэру." if dm_delivered else "✅ Отказ сохранён. ⚠️ ЛС мэру закрыты.",
                ephemeral=True,
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
        if interaction.guild is None or not _is_admin(interaction, getattr(self.bot, "admin_user_ids", set())):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if state is None or city is None or city.get("status") != "pending":
            await interaction.response.send_message("❌ Активная заявка не найдена.", ephemeral=True)
            return
        if city.get("active_question"):
            await interaction.response.send_message("❌ Мэр ещё не ответил на предыдущий вопрос.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        question = str(self.question).strip()
        async with _lock(interaction.guild.id, self.city_id):
            city = state.cities.get(self.city_id)
            if city is None or city.get("status") != "pending":
                await interaction.followup.send("❌ Заявка уже рассмотрена или удалена.", ephemeral=True)
                return
            if city.get("active_question"):
                await interaction.followup.send("❌ Мэр ещё не ответил на предыдущий вопрос.", ephemeral=True)
                return
            user = await _user(self.bot, int(city.get("mayor_id", 0)))
            if user is None:
                await interaction.followup.send("❌ Не удалось найти пользователя-мэра.", ephemeral=True)
                return
            embed = discord.Embed(
                title="❓ Вопрос по заявке города",
                description=(
                    f"Администрация задала вопрос по заявке **{city.get('name')}** (`{self.city_id}`).\n\n"
                    f"**Вопрос:**\n{question}\n\n"
                    "Ответьте следующим сообщением в этом личном чате. Можно приложить файлы или ссылки."
                ),
                color=0x5865F2,
            )
            item = {
                "token": secrets.token_hex(6),
                "question": question,
                "answer": "",
                "asked_by": interaction.user.id,
                "asked_at": datetime.now(timezone.utc).isoformat(),
            }
            previous_active = copy.deepcopy(city.get("active_question", {}))
            history = city.setdefault("question_history", [])
            city["active_question"] = dict(item)
            history.append(item)
            try:
                await self.store.save(state)
            except Exception as exc:
                city["active_question"] = previous_active
                if history and history[-1].get("token") == item["token"]:
                    history.pop()
                await interaction.followup.send(f"❌ Не удалось сохранить вопрос: `{exc}`", ephemeral=True)
                return

            try:
                await user.send(embed=embed)
            except discord.HTTPException:
                city["active_question"] = previous_active
                if history and history[-1].get("token") == item["token"]:
                    history.pop()
                try:
                    await self.store.save(state)
                except Exception:
                    log.exception("Не удалось откатить вопрос по заявке города %s", self.city_id)
                await interaction.followup.send("❌ Личные сообщения мэра закрыты. Вопрос не сохранён.", ephemeral=True)
                return

            await _edit_review_message(self.bot, state, self.city_id, city)
        await interaction.followup.send("✅ Вопрос отправлен мэру. Заявка остаётся на рассмотрении.", ephemeral=True)


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
        state = self.store.get(interaction.guild.id) or await self.store.load_or_create(interaction.guild)
        if interaction.channel_id != state.channels.get("city_management", 0):
            await interaction.response.send_message("❌ Используйте настроенный канал управления городом.", ephemeral=True)
            return
        found = _find_city_for_mayor(state, interaction.user.id)
        if found is None:
            await interaction.response.send_message("❌ За вашим Discord-аккаунтом не найден зарегистрированный город.", ephemeral=True)
            return
        role_ids = state.roles.get("city_mayor", [])
        if isinstance(interaction.user, discord.Member) and role_ids:
            if not any(role.id in set(role_ids) for role in interaction.user.roles):
                await interaction.response.send_message("❌ У вас нет настроенной роли мэра.", ephemeral=True)
                return
        city_id, city = found
        await interaction.response.send_message(
            embed=city_management_embed(city_id, city, state),
            view=CityManagementView(self.bot, self.store, city_id, interaction.user.id),
            ephemeral=True,
        )


class CityManagementView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, city_id: str, mayor_id: int) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.user.id != self.mayor_id:
            await interaction.response.send_message("❌ Эта панель принадлежит другому мэру.", ephemeral=True)
            return False
        state = self.store.get(interaction.guild.id)
        city = state.cities.get(self.city_id) if state else None
        if city is None or city.get("status") != "approved" or int(city.get("mayor_id", 0)) != interaction.user.id:
            await interaction.response.send_message("❌ Связь с городом больше не найдена.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Изменить название города", emoji="✏️", style=discord.ButtonStyle.secondary, row=0)
    async def rename(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild_id or 0)
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

    @discord.ui.button(label="Изменить описание", emoji="📝", style=discord.ButtonStyle.secondary, row=0)
    async def description(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild_id or 0)
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

    @discord.ui.button(label="Установить / заменить баннер", emoji="🖼️", style=discord.ButtonStyle.primary, row=1)
    async def banner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CityBannerModal(self.bot, self.store, self.city_id, self.mayor_id))

    @discord.ui.button(label="Координаты и прочие данные", emoji="🧭", style=discord.ButtonStyle.primary, row=1)
    async def details(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = self.store.get(interaction.guild_id or 0)
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


async def _management_guard(
    interaction: discord.Interaction,
    store: UnifiedDiscordStore,
    city_id: str,
    mayor_id: int,
) -> tuple[UnifiedState | None, dict[str, Any] | None]:
    if interaction.guild is None or interaction.user.id != mayor_id:
        await interaction.response.send_message("❌ Нет доступа к этому городу.", ephemeral=True)
        return None, None
    state = store.get(interaction.guild.id)
    city = state.cities.get(city_id) if state else None
    if state is None or city is None or city.get("status") != "approved" or int(city.get("mayor_id", 0)) != mayor_id:
        await interaction.response.send_message("❌ Город не найден.", ephemeral=True)
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
    city["updated_at"] = datetime.now(timezone.utc).isoformat()
    await store.save(state)
    ok, text = await sync_registry_post(bot, state, city_id, city, rename_thread=rename_thread)
    if not ok:
        state.cities[city_id] = previous
        await store.save(state)
        restored, restore_text = await sync_registry_post(
            bot,
            state,
            city_id,
            previous,
            rename_thread=rename_thread,
        )
        if not restored:
            log.error(
                "Не удалось восстановить публичную карточку города %s после ошибки обновления: %s",
                city_id,
                restore_text,
            )
        return False, text
    return True, text


class CityRenameModal(discord.ui.Modal):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
        *,
        current_name: str = "",
    ) -> None:
        super().__init__(title="Изменить название города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        self.name_input = discord.ui.TextInput(
            label="Новое название",
            min_length=1,
            max_length=20,
            default=current_name[:20] or None,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state, city = await _management_guard(interaction, self.store, self.city_id, self.mayor_id)
        if state is None or city is None:
            return
        name = str(self.name_input).strip()
        if _name_taken(state, name, exclude=self.city_id):
            await interaction.response.send_message("❌ Такое название уже используется.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock(state.guild_id, self.city_id):
            previous = copy.deepcopy(city)
            city["name"] = name
            ok, text = await _save_and_sync(interaction, self.bot, self.store, state, self.city_id, city, previous, rename_thread=True)
        await interaction.followup.send(("✅ " if ok else "❌ ") + text, ephemeral=True)


class CityDescriptionModal(discord.ui.Modal):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
        *,
        current_description: str = "",
    ) -> None:
        super().__init__(title="Изменить описание города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        self.description_input = discord.ui.TextInput(
            label="Новое описание",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=2000,
            default=current_description[:2000] or None,
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
        await interaction.followup.send(("✅ " if ok else "❌ ") + text, ephemeral=True)


class CityBannerModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, city_id: str, mayor_id: int) -> None:
        super().__init__(title="Главный баннер города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        self.file_label = discord.ui.Label(
            text="Файл баннера",
            description="PNG/JPG/WEBP/GIF до 10 МБ. Рекомендуется 1600×600.",
            component=discord.ui.FileUpload(
                custom_id="city_main_banner_file",
                required=False,
                min_values=0,
                max_values=1,
            ),
        )
        self.link_label = discord.ui.Label(
            text="Или прямая ссылка",
            description="Используется, если файл не прикреплён.",
            component=discord.ui.TextInput(
                custom_id="city_main_banner_url",
                required=False,
                max_length=1000,
                placeholder="https://...",
            ),
        )
        self.add_item(self.file_label)
        self.add_item(self.link_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state, city = await _management_guard(interaction, self.store, self.city_id, self.mayor_id)
        if state is None or city is None or interaction.guild is None:
            return
        file_component = self.file_label.component
        attachment = file_component.values[0] if isinstance(file_component, discord.ui.FileUpload) and file_component.values else None
        link_component = self.link_label.component
        link = str(link_component.value).strip() if isinstance(link_component, discord.ui.TextInput) else ""
        if attachment is None and not _valid_url(link):
            await interaction.response.send_message("❌ Прикрепите изображение или укажите корректную ссылку http/https.", ephemeral=True)
            return
        if attachment is not None:
            try:
                self.store.validate_image(attachment)
            except ValueError as exc:
                await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        new_asset = AssetRef()
        if attachment is not None:
            try:
                new_asset = await self.store.persist_asset(
                    interaction.guild,
                    attachment,
                    f"Главный баннер города {city.get('name', self.city_id)}",
                )
                link = new_asset.url
            except Exception as exc:
                await interaction.followup.send(f"❌ Не удалось сохранить баннер: `{exc}`", ephemeral=True)
                return

        async with _lock(state.guild_id, self.city_id):
            previous = copy.deepcopy(city)
            old_asset = AssetRef.from_dict(city.get("banner_asset", {}))
            city["banner_url"] = link
            city["banner_asset"] = new_asset.__dict__ if new_asset.url else {}
            ok, text = await _save_and_sync(interaction, self.bot, self.store, state, self.city_id, city, previous)
            if not ok and new_asset.message_id:
                await _delete_asset_message(self.store, interaction.guild, new_asset.message_id)
            elif ok and old_asset.message_id:
                await _delete_asset_message(self.store, interaction.guild, old_asset.message_id)
        await interaction.followup.send(("✅ Баннер сохранён. " if ok else "❌ ") + text, ephemeral=True)


class CityEditDataModal(discord.ui.Modal):
    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        city_id: str,
        mayor_id: int,
        *,
        current_city: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(title="Координаты и данные города", timeout=600)
        self.bot = bot
        self.store = store
        self.city_id = city_id
        self.mayor_id = mayor_id
        current = current_city or {}
        self.style_input = discord.ui.TextInput(
            label="Архитектурный стиль",
            max_length=300,
            default=str(current.get("style", ""))[:300] or None,
        )
        self.overworld_input = discord.ui.TextInput(
            label="Координаты в Верхнем мире",
            max_length=100,
            default=str(current.get("overworld_coords", ""))[:100] or None,
        )
        self.nether_input = discord.ui.TextInput(
            label="Нижний мир и ветка метро",
            max_length=150,
            default=str(current.get("nether_coords", ""))[:150] or None,
        )
        deputy_id = int(current.get("deputy_id", 0) or 0)
        self.deputy_input = discord.ui.TextInput(
            label="Discord заместителя",
            placeholder="Упоминание или цифровой ID",
            max_length=64,
            default=(f"<@{deputy_id}>" if deputy_id else None),
        )
        for item in (self.style_input, self.overworld_input, self.nether_input, self.deputy_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state, city = await _management_guard(interaction, self.store, self.city_id, self.mayor_id)
        if state is None or city is None or interaction.guild is None:
            return
        deputy_id = _discord_id(str(self.deputy_input))
        if deputy_id is None:
            await interaction.response.send_message("❌ Укажите упоминание или цифровой Discord ID заместителя.", ephemeral=True)
            return
        if deputy_id == self.mayor_id:
            await interaction.response.send_message("❌ Мэр не может быть собственным заместителем.", ephemeral=True)
            return
        deputy = await _member(interaction.guild, deputy_id)
        if deputy is None or deputy.bot:
            await interaction.response.send_message("❌ Заместитель должен быть участником сервера и не может быть ботом.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock(state.guild_id, self.city_id):
            previous = copy.deepcopy(city)
            city.update(
                {
                    "style": str(self.style_input).strip(),
                    "overworld_coords": str(self.overworld_input).strip(),
                    "nether_coords": str(self.nether_input).strip(),
                    "deputy_id": deputy_id,
                }
            )
            ok, text = await _save_and_sync(interaction, self.bot, self.store, state, self.city_id, city, previous)
        await interaction.followup.send(("✅ " if ok else "❌ ") + text, ephemeral=True)


async def publish_city_panels(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    guild: discord.Guild,
    state: UnifiedState,
) -> tuple[bool, str]:
    application_channel = await _text_channel(bot, state.channels.get("city_application", 0))
    review_channel = await _text_channel(bot, state.channels.get("city_review", 0))
    registry_channel = await _forum_channel(bot, state.channels.get("city_registry", 0))
    management_channel = await _text_channel(bot, state.channels.get("city_management", 0))
    role_ids = state.roles.get("city_mayor", [])
    mayor_role = guild.get_role(role_ids[0]) if role_ids else None
    if application_channel is None or review_channel is None or registry_channel is None or management_channel is None:
        return False, "Выберите канал подачи, рассмотрения, форум реестра и канал управления."
    selected_channels = {
        application_channel.id,
        review_channel.id,
        registry_channel.id,
        management_channel.id,
    }
    if len(selected_channels) != 4:
        return False, "Для подачи, рассмотрения, реестра и управления выберите четыре разных канала."
    if mayor_role is None:
        return False, "Выберите роль мэра."
    if mayor_role.is_default() or mayor_role.managed:
        return False, "Роль мэра должна быть обычной отдельной ролью Discord."
    bot_member = guild.me
    if bot_member is not None and mayor_role >= bot_member.top_role:
        return False, "Переместите роль бота выше роли мэра, иначе бот не сможет её выдавать."

    application_asset = state.asset("city_application_panel")
    if not application_asset.url:
        return False, "Сначала загрузите большой баннер панели регистрации городов."

    accent = int(state.options.get("accent_color", 0x19B9D1))
    application_embed = discord.Embed(
        title=state.texts.get("city_application_title", "Регистрация города FunFernus"),
        description=state.texts.get(
            "city_application_description",
            "Нажмите кнопку ниже, выберите руководство и заполните данные будущего города.",
        ),
        color=accent,
    )
    application_embed.add_field(
        name="Что потребуется",
        value=(
            "• название города до 20 символов\n"
            "• мэр и заместитель\n"
            "• архитектурный стиль и описание\n"
            "• координаты Верхнего и Нижнего мира\n"
            "• скриншоты первых построек"
        ),
        inline=False,
    )
    application_embed.set_footer(text=state.texts.get("city_application_footer", "FunFernus • Реестр городов"))
    application_embed.set_image(url=application_asset.url)

    application_view = CityApplicationPanelView(bot, store)
    old_application = state.messages.get("city_application", 0)
    application_message: discord.Message | None = None
    try:
        if old_application:
            try:
                application_message = await application_channel.fetch_message(old_application)
                await application_message.edit(content=None, embed=application_embed, attachments=[], view=application_view)
            except discord.HTTPException:
                application_message = None
        if application_message is None:
            application_message = await application_channel.send(embed=application_embed, view=application_view)
    except discord.Forbidden:
        return False, "Боту не хватает прав для публикации панели регистрации."
    except discord.HTTPException as exc:
        return False, f"Discord не опубликовал панель регистрации: {exc}"
    state.messages["city_application"] = application_message.id
    try:
        await store.save(state)
    except Exception as exc:
        return False, f"Панель опубликована, но её ID не удалось сохранить: {exc}"

    management_embed = discord.Embed(
        title="⚙️ Управление зарегистрированным городом",
        description=(
            "Бот определит ваш Discord ID, найдёт связанный город и откроет личную панель мэра. "
            "Все изменения сразу появятся в публичном реестре."
        ),
        color=accent,
    )
    management_embed.add_field(
        name="Доступные действия",
        value="Изменение названия, описания, главного баннера, координат, стиля, метро и заместителя.",
        inline=False,
    )
    management_embed.set_footer(text=f"FunFernus • Доступ для роли {mayor_role.name}")
    management_view = CityManagementLauncherView(bot, store)
    old_management = state.messages.get("city_management", 0)
    management_message: discord.Message | None = None
    try:
        if old_management:
            try:
                management_message = await management_channel.fetch_message(old_management)
                await management_message.edit(content=None, embed=management_embed, attachments=[], view=management_view)
            except discord.HTTPException:
                management_message = None
        if management_message is None:
            management_message = await management_channel.send(embed=management_embed, view=management_view)
    except discord.Forbidden:
        return False, "Боту не хватает прав для публикации панели управления городом."
    except discord.HTTPException as exc:
        return False, f"Discord не опубликовал панель управления: {exc}"
    state.messages["city_management"] = management_message.id
    try:
        await store.save(state)
    except Exception as exc:
        return False, f"Панели опубликованы, но ID панели управления не удалось сохранить: {exc}"
    return True, "Панель регистрации и панель управления опубликованы."


class CityPanelBannerModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(title="Баннер регистрации городов", timeout=600)
        self.bot = bot
        self.store = store
        self.file_label = discord.ui.Label(
            text="Файл большого баннера",
            description="PNG/JPG/WEBP/GIF до 10 МБ. Рекомендуется 1600×600.",
            component=discord.ui.FileUpload(
                custom_id="city_application_panel_banner",
                required=True,
                min_values=1,
                max_values=1,
            ),
        )
        self.add_item(self.file_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _is_admin(interaction, getattr(self.bot, "admin_user_ids", set())):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        component = self.file_label.component
        attachment = component.values[0] if isinstance(component, discord.ui.FileUpload) and component.values else None
        if attachment is None:
            await interaction.response.send_message("❌ Файл не выбран.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.followup.send("❌ Хранилище не загружено.", ephemeral=True)
            return
        try:
            await self.store.replace_asset(
                interaction.guild,
                state,
                "city_application_panel",
                attachment,
                "Панель регистрации городов",
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Не удалось сохранить баннер: `{exc}`", ephemeral=True)
            return
        ok, text = await publish_city_panels(self.bot, self.store, interaction.guild, state)
        await interaction.followup.send(("✅ " if ok else "⚠️ ") + text, ephemeral=True)


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
            await interaction.response.send_message("❌ Хранилище не загружено.", ephemeral=True)
            return
        state.channels[self.key] = self.values[0].id
        await self.store.save(state)
        await interaction.response.send_message(f"✅ Выбран канал: {self.values[0].mention}", ephemeral=True)


class CityMayorRoleSelect(discord.ui.RoleSelect):
    def __init__(self, store: UnifiedDiscordStore) -> None:
        super().__init__(placeholder="Выберите роль Мэр", min_values=1, max_values=1)
        self.store = store

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message("❌ Хранилище не загружено.", ephemeral=True)
            return
        role = self.values[0]
        if role.is_default() or role.managed:
            await interaction.response.send_message("❌ Выберите обычную отдельную роль, а не @everyone или интеграционную роль.", ephemeral=True)
            return
        bot_member = interaction.guild.me
        if bot_member is not None and role >= bot_member.top_role:
            await interaction.response.send_message("❌ Роль бота должна находиться выше роли мэра.", ephemeral=True)
            return
        state.roles["city_mayor"] = [role.id]
        await self.store.save(state)
        await interaction.response.send_message(f"✅ Роль мэра: {role.mention}", ephemeral=True)


class CitySetupView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.store = store

    @discord.ui.button(label="Каналы", emoji="📍", style=discord.ButtonStyle.secondary)
    async def channels(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = discord.ui.View(timeout=600)
        view.add_item(CityChannelSelect(self.store, "city_application", "Канал подачи заявок", discord.ChannelType.text))
        view.add_item(CityChannelSelect(self.store, "city_review", "Канал рассмотрения заявок", discord.ChannelType.text))
        view.add_item(CityChannelSelect(self.store, "city_registry", "Форум реестра городов", discord.ChannelType.forum))
        view.add_item(CityChannelSelect(self.store, "city_management", "Канал управления городом", discord.ChannelType.text))
        await interaction.response.send_message("Выберите четыре канала по очереди:", view=view, ephemeral=True)

    @discord.ui.button(label="Роль Мэр", emoji="👑", style=discord.ButtonStyle.secondary)
    async def mayor_role(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = discord.ui.View(timeout=600)
        view.add_item(CityMayorRoleSelect(self.store))
        await interaction.response.send_message("Выберите роль, которая выдаётся после одобрения города:", view=view, ephemeral=True)

    @discord.ui.button(label="Большой баннер", emoji="🖼️", style=discord.ButtonStyle.primary)
    async def banner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CityPanelBannerModal(self.bot, self.store))

    @discord.ui.button(label="Опубликовать панели", emoji="🚀", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.followup.send("❌ Хранилище не загружено.", ephemeral=True)
            return
        ok, text = await publish_city_panels(self.bot, self.store, interaction.guild, state)
        await interaction.followup.send(("✅ " if ok else "❌ ") + text, ephemeral=True)


async def setup_cities(bot: commands.Bot, store: UnifiedDiscordStore, admin_ids: set[int]) -> None:
    bot.add_view(CityApplicationPanelView(bot, store))
    bot.add_view(CityReviewView(bot, store))
    bot.add_view(CityManagementLauncherView(bot, store))

    @bot.tree.command(name="настроить_города", description="Настроить регистрацию, реестр и управление городами")
    @app_commands.guild_only()
    async def cities_setup(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _is_admin(interaction, admin_ids):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        state = store.get(interaction.guild.id) or await store.load_or_create(interaction.guild)
        role_ids = state.roles.get("city_mayor", [])
        role_text = f"<@&{role_ids[0]}>" if role_ids else "не выбрана"
        embed = discord.Embed(
            title="⚙️ Настройка системы городов",
            description=(
                "Выберите канал подачи, закрытый канал рассмотрения, форум реестра, "
                "канал управления и роль мэра. Затем загрузите большой баннер и опубликуйте панели."
            ),
            color=int(state.options.get("accent_color", 0x19B9D1)),
        )
        embed.add_field(name="Роль мэра", value=role_text, inline=False)
        await interaction.response.send_message(embed=embed, view=CitySetupView(bot, store), ephemeral=True)

    @bot.listen("on_message")
    async def city_question_dm_listener(message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        parts: list[str] = []
        if message.content.strip():
            parts.append(message.content.strip())
        parts.extend(f"[{attachment.filename}]({attachment.url})" for attachment in message.attachments)
        if not parts:
            return
        answer = "\n".join(parts)

        for state in list(store._states.values()):
            target: tuple[str, dict[str, Any]] | None = None
            for city_id, city in state.cities.items():
                if city.get("status") != "pending" or int(city.get("mayor_id", 0)) != message.author.id:
                    continue
                if city.get("active_question"):
                    target = (city_id, city)
                    break
            if target is None:
                continue
            city_id, city = target
            async with _lock(state.guild_id, city_id):
                city = state.cities.get(city_id)
                if (
                    city is None
                    or city.get("status") != "pending"
                    or int(city.get("mayor_id", 0)) != message.author.id
                    or not city.get("active_question")
                ):
                    continue
                previous = copy.deepcopy(city)
                active = city.get("active_question", {})
                token = active.get("token")
                for item in reversed(city.get("question_history", [])):
                    if item.get("token") == token:
                        item["answer"] = answer
                        item["answered_at"] = datetime.now(timezone.utc).isoformat()
                        break
                city["active_question"] = {}
                try:
                    await store.save(state)
                except Exception:
                    state.cities[city_id] = previous
                    log.exception("Не удалось сохранить ответ мэра по заявке %s", city_id)
                    try:
                        await message.reply("❌ Ответ не удалось сохранить. Повторите отправку чуть позже.")
                    except discord.HTTPException:
                        pass
                    return
                await _edit_review_message(bot, state, city_id, city)

                review_channel = await _text_channel(bot, int(city.get("review_channel_id", 0)))
                if review_channel is not None:
                    try:
                        review_message = await review_channel.fetch_message(int(city.get("review_message_id", 0)))
                        embed = discord.Embed(
                            title=f"💬 Ответ мэра • {city_id}",
                            description=_trim(answer, 4000),
                            color=0x5865F2,
                            timestamp=datetime.now(timezone.utc),
                        )
                        embed.add_field(name="Мэр", value=f"<@{message.author.id}>", inline=False)
                        await review_message.reply(
                            embed=embed,
                            mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        log.exception("Не удалось переслать ответ мэра по %s", city_id)
            try:
                await message.reply(f"✅ Ответ по заявке `{city_id}` передан администрации.")
            except discord.HTTPException:
                pass
            return
