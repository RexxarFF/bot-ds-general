from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import config
from rcon_client import RCONError, execute_rcon_command
from settings_store import DiscordSettingsStore, GuildSettings, PanelDraft
from modules.unified_store import UnifiedDiscordStore
from modules.components import build_framed_container
from modules.community import (
    setup_community,
    publish_support_panel,
    CommunitySetupView,
)
from modules.government import (
    setup_government,
    publish_government_panel,
    GovernmentSetupView,
)
from modules.cities import (
    setup_cities,
    publish_city_panels,
    CitySetupView,
)


# ============================================================
# ENV: ТОЛЬКО СЕКРЕТЫ И СТАРТОВЫЙ ДОСТУП
# ============================================================

load_dotenv()


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Переменная {name} должна содержать число. Сейчас: {raw!r}"
        ) from exc


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def parse_user_ids(raw: str) -> set[int]:
    return {
        int(value)
        for value in re.findall(r"\d{15,25}", raw or "")
    }


DISCORD_TOKEN = env_str("DISCORD_TOKEN")
GUILD_ID = env_int("GUILD_ID")

# Единственный канал, который указывается вручную.
# Все остальные каналы и роли выбираются через панель в Discord.
CONFIG_CHANNEL_ID = env_int("CONFIG_CHANNEL_ID")

ADMIN_USER_IDS = parse_user_ids(env_str("ADMIN_USER_IDS"))

RCON_TEST_MODE = env_bool("RCON_TEST_MODE", True)
RCON_ENABLED = env_bool("RCON_ENABLED", False)
RCON_HOST = env_str("RCON_HOST")
RCON_PORT = env_int("RCON_PORT", 25575)
RCON_PASSWORD = env_str("RCON_PASSWORD")

MINECRAFT_NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
HEX_COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("funfernus-discord")

_review_locks: dict[int, asyncio.Lock] = {}


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================


def get_review_lock(message_id: int) -> asyncio.Lock:
    lock = _review_locks.get(message_id)
    if lock is None:
        lock = asyncio.Lock()
        _review_locks[message_id] = lock
    return lock


def is_staff(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if interaction.guild.owner_id == interaction.user.id:
        return True
    return interaction.user.id in ADMIN_USER_IDS


def no_access_text(interaction: discord.Interaction) -> str:
    owner_id = (
        interaction.guild.owner_id
        if interaction.guild is not None
        else "не определён"
    )
    loaded_ids = ", ".join(
        str(user_id)
        for user_id in sorted(ADMIN_USER_IDS)
    ) or "список пуст"
    return (
        "❌ **У тебя нет доступа к управлению ботом.**\n\n"
        f"Твой Discord ID: `{interaction.user.id}`\n"
        f"ID владельца сервера: `{owner_id}`\n"
        f"ADMIN_USER_IDS: `{loaded_ids}`"
    )


async def require_staff(interaction: discord.Interaction) -> bool:
    if is_staff(interaction):
        return True
    if interaction.response.is_done():
        await interaction.followup.send(
            no_access_text(interaction),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            no_access_text(interaction),
            ephemeral=True,
        )
    return False


async def safe_dm(
    user: discord.User | discord.Member,
    embed: discord.Embed,
) -> bool:
    try:
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def get_text_channel(
    bot: commands.Bot,
    channel_id: int,
) -> discord.TextChannel | None:
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    try:
        fetched = await bot.fetch_channel(channel_id)
    except (discord.Forbidden, discord.HTTPException):
        return None
    return fetched if isinstance(fetched, discord.TextChannel) else None


def get_embed_field(
    embed: discord.Embed,
    field_name: str,
) -> str | None:
    for field in embed.fields:
        if field.name == field_name:
            return field.value
    return None


def extract_application_data(
    message: discord.Message,
) -> tuple[int, str]:
    if not message.embeds:
        raise ValueError("В сообщении нет карточки заявки.")

    embed = message.embeds[0]
    user_id_raw = get_embed_field(embed, config.FIELD_USER_ID)
    nickname_raw = get_embed_field(embed, config.FIELD_NICKNAME)

    if not user_id_raw:
        raise ValueError("В заявке не найден Discord ID.")
    user_id_match = re.search(r"\d{15,25}", user_id_raw)
    if not user_id_match:
        raise ValueError("Discord ID в заявке имеет неверный формат.")

    if not nickname_raw:
        raise ValueError("В заявке не найден Minecraft-ник.")
    nickname = nickname_raw.strip().strip("`").strip()
    if not MINECRAFT_NICK_RE.fullmatch(nickname):
        raise ValueError("Minecraft-ник в заявке имеет неверный формат.")

    return int(user_id_match.group()), nickname


def reviewer_text(user: discord.abc.User) -> str:
    return f"{user.mention}\nID: `{user.id}`"


def button_style_from_name(name: str) -> discord.ButtonStyle:
    mapping = {
        "green": discord.ButtonStyle.success,
        "blue": discord.ButtonStyle.primary,
        "red": discord.ButtonStyle.danger,
        "gray": discord.ButtonStyle.secondary,
        "grey": discord.ButtonStyle.secondary,
    }
    return mapping.get(name.lower(), discord.ButtonStyle.success)


def format_roles(guild: discord.Guild, role_ids: list[int]) -> str:
    if not role_ids:
        return "Не выбраны"
    lines: list[str] = []
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is None:
            lines.append(f"Удалённая роль (`{role_id}`)")
        else:
            lines.append(role.mention)
    return "\n".join(lines)


def format_channel(channel_id: int) -> str:
    return f"<#{channel_id}>" if channel_id else "❌ Не настроен"


def build_public_panel_embed(draft: PanelDraft) -> discord.Embed:
    return draft.to_embed()


def build_control_embed(
    guild: discord.Guild,
    settings: GuildSettings,
) -> discord.Embed:
    embed = discord.Embed(
        title="⚙️ Управление FunFernus Applications",
        description=(
            "Все каналы, роли и оформление меняются кнопками ниже.\n"
            "Секреты RCON и токен остаются только в `.env`."
        ),
        color=config.COLOR_CONTROL,
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="📍 Каналы",
        value=(
            f"Публичная панель: {format_channel(settings.panel_channel_id)}\n"
            f"Рассмотрение: {format_channel(settings.review_channel_id)}\n"
            f"Логи: {format_channel(settings.log_channel_id)}\n"
            f"RCON-консоль: {format_channel(settings.rcon_channel_id)}\n"
            f"Управление: {format_channel(settings.control_channel_id)}"
        ),
        inline=False,
    )

    embed.add_field(
        name="🎭 Роли при принятии",
        value=format_roles(guild, settings.accept_role_ids),
        inline=False,
    )

    panel_status = (
        "✅ Опубликована и актуальна"
        if settings.panel_message_id
        and settings.published_revision == settings.draft_revision
        else (
            "🟡 Есть неопубликованные изменения"
            if settings.panel_message_id
            else "❌ Ещё не опубликована"
        )
    )
    embed.add_field(
        name="📝 Публичная панель",
        value=(
            f"Статус: **{panel_status}**\n"
            f"Черновик: `{settings.draft_revision}`\n"
            f"Опубликовано: `{settings.published_revision}`"
        ),
        inline=False,
    )

    rcon_status = (
        "🧪 Тестовый режим"
        if RCON_TEST_MODE
        else ("✅ Включён" if RCON_ENABLED else "❌ Отключён")
    )
    embed.add_field(
        name="🖥 RCON",
        value=f"{rcon_status}\n`{RCON_HOST or 'не указан'}:{RCON_PORT}`",
        inline=False,
    )

    embed.set_footer(
        text="FunFernus • Настройки хранятся в закрытом канале bot-config"
    )
    return embed


async def run_rcon(command: str) -> tuple[bool, str]:
    command = command.strip()
    if not command:
        return False, "Команда RCON пустая."
    if RCON_TEST_MODE:
        log.info("[RCON TEST MODE] %s", command)
        return True, f"Тестовый режим: `{command}`"
    if not RCON_ENABLED:
        return False, "RCON отключён: RCON_ENABLED=false."
    try:
        response = await execute_rcon_command(
            RCON_HOST,
            RCON_PORT,
            RCON_PASSWORD,
            command,
        )
        return True, response
    except RCONError as exc:
        return False, str(exc)
    except Exception as exc:
        log.exception("Неожиданная ошибка RCON")
        return False, f"{type(exc).__name__}: {exc}"


async def send_log(
    bot: "FunFernusBot",
    guild: discord.Guild,
    embed: discord.Embed,
) -> bool:
    settings = bot.settings_store.get_settings(guild.id)
    if settings is None or not settings.log_channel_id:
        return False
    channel = await get_text_channel(bot, settings.log_channel_id)
    if channel is None:
        return False
    try:
        await channel.send(embed=embed)
        return True
    except discord.HTTPException:
        log.exception("Не удалось отправить лог")
        return False


async def send_settings_log(
    bot: "FunFernusBot",
    guild: discord.Guild,
    user: discord.abc.User,
    action: str,
    details: str,
) -> None:
    embed = discord.Embed(
        title="⚙️ Настройки бота изменены",
        color=config.COLOR_CONTROL,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Изменение",
        value=action[:256],
        inline=False,
    )
    embed.add_field(
        name="Подробности",
        value=details[:1024],
        inline=False,
    )
    embed.add_field(
        name="Изменил",
        value=reviewer_text(user),
        inline=False,
    )
    await send_log(bot, guild, embed)


async def update_control_panel(
    bot: "FunFernusBot",
    guild: discord.Guild,
) -> discord.Message | None:
    settings = bot.settings_store.get_settings(guild.id)
    if settings is None or not settings.control_channel_id:
        return None

    channel = await get_text_channel(bot, settings.control_channel_id)
    if channel is None:
        return None

    message: discord.Message | None = None
    if (
        settings.control_message_id
        and settings.control_message_channel_id == channel.id
    ):
        try:
            message = await channel.fetch_message(settings.control_message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None
    elif settings.control_message_id and settings.control_message_channel_id:
        old_channel = await get_text_channel(
            bot,
            settings.control_message_channel_id,
        )
        if old_channel is not None:
            try:
                old_message = await old_channel.fetch_message(
                    settings.control_message_id
                )
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    embed = build_control_embed(guild, settings)

    if message is None:
        message = await channel.send(
            embed=embed,
            view=ControlPanelView(bot),
        )
        settings.control_message_id = message.id
        settings.control_message_channel_id = channel.id
        await bot.settings_store.save(settings)
    else:
        await message.edit(
            embed=embed,
            view=ControlPanelView(bot),
        )

    return message


async def publish_panel(
    bot: "FunFernusBot",
    guild: discord.Guild,
) -> tuple[bool, str]:
    settings = bot.settings_store.get_settings(guild.id)
    draft = bot.settings_store.get_draft(guild.id)
    if settings is None or draft is None:
        return False, "Бот ещё не настроен. Проверь CONFIG_CHANNEL_ID."
    if not settings.panel_channel_id:
        return False, "Сначала выбери канал публичной панели."

    channel = await get_text_channel(bot, settings.panel_channel_id)
    if channel is None:
        return False, "Канал публичной панели не найден."

    old_message: discord.Message | None = None
    if settings.panel_message_id and settings.panel_message_channel_id:
        old_channel = await get_text_channel(
            bot,
            settings.panel_message_channel_id,
        )
        if old_channel is not None:
            try:
                old_message = await old_channel.fetch_message(
                    settings.panel_message_id
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                old_message = None

    view = ApplicationPanelView(
        bot,
        settings.button_label,
        settings.button_emoji,
        settings.button_style,
        draft=draft,
    )

    if old_message is not None and old_message.channel.id == channel.id:
        # Components V2 нельзя смешивать с обычным embed/content.
        # При обновлении старой панели полностью очищаем прежнее оформление.
        await old_message.edit(
            content=None,
            embeds=[],
            attachments=[],
            view=view,
        )
        panel_message = old_message
    else:
        panel_message = await channel.send(view=view)
        if old_message is not None:
            try:
                await old_message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

    settings.panel_message_id = panel_message.id
    settings.panel_message_channel_id = channel.id
    settings.published_revision = settings.draft_revision
    await bot.settings_store.save(settings)
    await update_control_panel(bot, guild)
    return True, f"Панель опубликована в {channel.mention}."


# ============================================================
# ПУБЛИЧНАЯ ФОРМА
# ============================================================


class ApplicationModal(discord.ui.Modal):
    def __init__(self, bot: "FunFernusBot") -> None:
        super().__init__(title=config.MODAL_TITLE, timeout=900)
        self.bot = bot
        self.inputs: list[discord.ui.TextInput] = []

        for question in config.QUESTIONS:
            text_input = discord.ui.TextInput(
                label=question["label"],
                placeholder=question.get("placeholder"),
                style=(
                    discord.TextStyle.paragraph
                    if question.get("paragraph")
                    else discord.TextStyle.short
                ),
                min_length=question.get("min_length"),
                max_length=question.get("max_length"),
                required=question.get("required", True),
            )
            self.inputs.append(text_input)
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Заявку можно подать только на сервере.",
                ephemeral=True,
            )
            return

        settings = self.bot.settings_store.get_settings(interaction.guild.id)
        if settings is None or not settings.review_channel_id:
            await interaction.response.send_message(
                "❌ Канал рассмотрения заявок ещё не настроен.",
                ephemeral=True,
            )
            return

        values = [str(item.value).strip() for item in self.inputs]
        nickname = values[0]
        if not MINECRAFT_NICK_RE.fullmatch(nickname):
            await interaction.response.send_message(
                "❌ Minecraft-ник должен содержать только латинские "
                "буквы, цифры и `_`. Длина: от 3 до 16 символов.",
                ephemeral=True,
            )
            return

        age_match = re.search(r"\d{1,3}", values[1])
        if not age_match or int(age_match.group()) < 14:
            await interaction.response.send_message(
                "❌ Минимальный возраст для подачи заявки — 14 лет.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        review_channel = await get_text_channel(
            self.bot,
            settings.review_channel_id,
        )
        if review_channel is None:
            await interaction.followup.send(
                "❌ Канал рассмотрения заявок не найден.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=config.REVIEW_EMBED_TITLE,
            description=config.REVIEW_EMBED_DESCRIPTION,
            color=config.COLOR_PENDING,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url,
        )
        embed.add_field(
            name=config.FIELD_APPLICANT,
            value=(
                f"{interaction.user.mention}\n"
                f"Имя: **{interaction.user.display_name}**"
            ),
            inline=True,
        )
        embed.add_field(
            name=config.FIELD_NICKNAME,
            value=f"`{nickname}`",
            inline=True,
        )
        embed.add_field(
            name=config.FIELD_USER_ID,
            value=f"`{interaction.user.id}`",
            inline=False,
        )

        for question, value in zip(config.QUESTIONS[1:], values[1:]):
            embed.add_field(
                name=question["embed_name"],
                value=value[:1024],
                inline=False,
            )

        embed.set_footer(text=config.REVIEW_FOOTER_PENDING)

        try:
            review_message = await review_channel.send(
                embed=embed,
                view=ReviewView(self.bot),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ У бота нет доступа к каналу рассмотрения.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Ошибка Discord: `{exc}`",
                ephemeral=True,
            )
            return

        confirmation = discord.Embed(
            title="✅ Заявка отправлена",
            description=(
                "Она передана администрации. Результат придёт "
                "тебе в личные сообщения."
            ),
            color=config.COLOR_ACCEPTED,
        )
        confirmation.add_field(
            name="Minecraft-ник",
            value=f"`{nickname}`",
            inline=False,
        )
        confirmation.set_footer(text=f"ID заявки: {review_message.id}")
        await interaction.followup.send(
            embed=confirmation,
            ephemeral=True,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        log.exception("Ошибка формы заявки", exc_info=error)
        text = "❌ Произошла ошибка при отправке заявки."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class ApplicationPanelView(discord.ui.LayoutView):
    """Публичная панель анкеты в Components V2.

    Баннер отображается отдельной широкой галереей в самом верху рамки,
    а не маленькой картинкой внутри обычного embed.
    """

    def __init__(
        self,
        bot: "FunFernusBot",
        label: str = config.DEFAULT_BUTTON_LABEL,
        emoji: str = config.DEFAULT_BUTTON_EMOJI,
        style_name: str = config.DEFAULT_BUTTON_STYLE,
        *,
        draft: PanelDraft | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot

        panel = draft or PanelDraft()
        action_row = discord.ui.ActionRow()
        open_button = discord.ui.Button(
            label=(label[:80] or "Подать заявку"),
            emoji=(emoji or None),
            style=button_style_from_name(style_name),
            custom_id="funfernus:application:open:v2",
        )
        open_button.callback = self._open_application
        action_row.add_item(open_button)

        # Старое поле thumbnail не теряется: если большой баннер не задан,
        # картинка из thumbnail используется как основной баннер.
        banner_url = panel.image_url or panel.thumbnail_url
        self.add_item(
            build_framed_container(
                title=panel.title,
                body=panel.description,
                banner_url=banner_url,
                color=panel.color,
                footer=panel.footer,
                action_row=action_row,
            )
        )

    async def _open_application(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(ApplicationModal(self.bot))


# ============================================================
# ВЫДАЧА РОЛЕЙ
# ============================================================


async def assign_accept_roles(
    guild: discord.Guild,
    user_id: int,
    role_ids: list[int],
    reviewer: discord.abc.User,
) -> tuple[list[discord.Role], list[str]]:
    if not role_ids:
        return [], []

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return [], ["Пользователь не найден на сервере Discord."]

    bot_member = guild.me
    if bot_member is None:
        return [], ["Не удалось определить роль бота."]

    assigned: list[discord.Role] = []
    errors: list[str] = []

    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is None:
            errors.append(f"Роль `{role_id}` больше не существует.")
            continue
        if role.is_default():
            errors.append(f"{role.name}: роль @everyone выдавать нельзя.")
            continue
        if role.managed:
            errors.append(f"{role.name}: роль управляется интеграцией.")
            continue
        if role >= bot_member.top_role:
            errors.append(
                f"{role.name}: роль находится выше или на уровне роли бота."
            )
            continue
        if role in member.roles:
            assigned.append(role)
            continue

        try:
            await member.add_roles(
                role,
                reason=(
                    "Заявка FunFernus принята пользователем "
                    f"{reviewer} ({reviewer.id})"
                ),
            )
            assigned.append(role)
        except discord.Forbidden:
            errors.append(f"{role.name}: у бота недостаточно прав.")
        except discord.HTTPException as exc:
            errors.append(f"{role.name}: ошибка Discord — {exc}")

    return assigned, errors


def role_result_text(
    assigned: list[discord.Role],
    errors: list[str],
) -> str:
    lines: list[str] = []
    if assigned:
        lines.append(
            "**Выданы:**\n"
            + "\n".join(role.mention for role in assigned)
        )
    elif not errors:
        lines.append("Роли для автоматической выдачи не выбраны.")

    if errors:
        lines.append(
            "**Не удалось выдать:**\n"
            + "\n".join(f"• {item}" for item in errors)
        )

    return "\n\n".join(lines)[:1024]


# ============================================================
# ОТКЛОНЕНИЕ ЗАЯВКИ
# ============================================================


class RejectModal(discord.ui.Modal):
    def __init__(
        self,
        bot: "FunFernusBot",
        message: discord.Message,
        reviewer: discord.Member,
    ) -> None:
        super().__init__(title="Отклонение заявки", timeout=600)
        self.bot = bot
        self.message = message
        self.reviewer = reviewer
        self.reason = discord.ui.TextInput(
            label="Причина отклонения",
            placeholder="Например: недостаточно развёрнутые ответы",
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=500,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            user_id, nickname = extract_application_data(self.message)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        reason = str(self.reason.value).strip()
        embed = self.message.embeds[0]
        embed.title = config.REJECTED_EMBED_TITLE
        embed.color = config.COLOR_REJECTED
        embed.set_footer(text=config.REVIEW_FOOTER_REJECTED)
        embed.add_field(
            name="🚫 Причина",
            value=reason,
            inline=False,
        )
        embed.add_field(
            name="🛡 Рассмотрел",
            value=reviewer_text(self.reviewer),
            inline=False,
        )

        try:
            await self.message.edit(embed=embed, view=None)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Не удалось обновить заявку: `{exc}`",
                ephemeral=True,
            )
            return

        try:
            applicant = await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            applicant = None

        dm_sent = False
        if applicant is not None:
            dm_embed = discord.Embed(
                title=config.REJECT_DM_TITLE,
                description=config.REJECT_DM_TEXT,
                color=config.COLOR_REJECTED,
            )
            dm_embed.add_field(
                name="Minecraft-ник",
                value=f"`{nickname}`",
                inline=False,
            )
            dm_embed.add_field(
                name="Причина",
                value=reason,
                inline=False,
            )
            dm_embed.set_footer(text=config.DM_FOOTER)
            dm_sent = await safe_dm(applicant, dm_embed)

        log_embed = discord.Embed(
            title="🚫 Заявка отклонена",
            color=config.COLOR_REJECTED,
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.add_field(
            name="Заявитель",
            value=f"<@{user_id}>\nDiscord ID: `{user_id}`",
            inline=False,
        )
        log_embed.add_field(
            name="Minecraft",
            value=f"`{nickname}`",
            inline=False,
        )
        log_embed.add_field(
            name="Рассмотрел",
            value=reviewer_text(self.reviewer),
            inline=False,
        )
        log_embed.add_field(name="Причина", value=reason, inline=False)
        log_embed.add_field(
            name="Личное сообщение",
            value="✅ Доставлено" if dm_sent else "❌ Не доставлено",
            inline=False,
        )
        await send_log(self.bot, interaction.guild, log_embed)

        await interaction.followup.send(
            "✅ Заявка отклонена. "
            + ("ЛС отправлено." if dm_sent else "ЛС отправить не удалось."),
            ephemeral=True,
        )


# ============================================================
# ПРИНЯТИЕ / ОТКЛОНЕНИЕ
# ============================================================


class ReviewView(discord.ui.View):
    def __init__(self, bot: "FunFernusBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    @discord.ui.button(
        label="Принять",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="funfernus:review:accept:v2",
    )
    async def accept(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or interaction.message is None:
            await interaction.response.send_message(
                "❌ Не удалось получить заявку.",
                ephemeral=True,
            )
            return

        lock = get_review_lock(interaction.message.id)
        if lock.locked():
            await interaction.response.send_message(
                "⏳ Эта заявка уже обрабатывается.",
                ephemeral=True,
            )
            return

        async with lock:
            await interaction.response.defer(ephemeral=True)

            try:
                user_id, nickname = extract_application_data(
                    interaction.message
                )
            except ValueError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return

            settings = self.bot.settings_store.get_settings(
                interaction.guild.id
            )
            if settings is None:
                await interaction.followup.send(
                    "❌ Настройки сервера не найдены.",
                    ephemeral=True,
                )
                return

            try:
                await interaction.message.edit(
                    view=discord.ui.View(timeout=None)
                )
            except discord.HTTPException:
                pass

            ok, rcon_response = await run_rcon(
                f"whitelist add {nickname}"
            )
            if not ok:
                try:
                    await interaction.message.edit(view=ReviewView(self.bot))
                except discord.HTTPException:
                    pass
                await interaction.followup.send(
                    "❌ Игрок не был добавлен в whitelist.\n\n"
                    f"**Ошибка:** `{rcon_response}`\n\n"
                    "Заявка осталась активной.",
                    ephemeral=True,
                )
                return

            assigned_roles, role_errors = await assign_accept_roles(
                interaction.guild,
                user_id,
                settings.accept_role_ids,
                interaction.user,
            )
            roles_text = role_result_text(assigned_roles, role_errors)

            embed = interaction.message.embeds[0]
            embed.title = config.ACCEPTED_EMBED_TITLE
            embed.color = config.COLOR_ACCEPTED
            embed.set_footer(text=config.REVIEW_FOOTER_ACCEPTED)
            embed.add_field(
                name="🛡 Рассмотрел",
                value=reviewer_text(interaction.user),
                inline=False,
            )
            embed.add_field(
                name="🎭 Роли",
                value=roles_text,
                inline=False,
            )
            embed.add_field(
                name="🖥 Whitelist",
                value=rcon_response[:1024],
                inline=False,
            )

            try:
                await interaction.message.edit(embed=embed, view=None)
            except discord.HTTPException as exc:
                await interaction.followup.send(
                    "⚠️ Whitelist выполнен, но карточку обновить не удалось: "
                    f"`{exc}`",
                    ephemeral=True,
                )
                return

            try:
                applicant = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                applicant = None

            dm_sent = False
            if applicant is not None:
                dm_embed = discord.Embed(
                    title=config.ACCEPT_DM_TITLE,
                    description=config.ACCEPT_DM_TEXT,
                    color=config.COLOR_ACCEPTED,
                )
                dm_embed.add_field(
                    name="Minecraft-ник",
                    value=f"`{nickname}`",
                    inline=False,
                )
                dm_embed.add_field(
                    name="Адрес сервера",
                    value=f"`{config.SERVER_ADDRESS}`",
                    inline=True,
                )
                dm_embed.add_field(
                    name="Версия",
                    value=f"`{config.SERVER_VERSION}`",
                    inline=True,
                )
                dm_embed.set_footer(text=config.DM_FOOTER)
                dm_sent = await safe_dm(applicant, dm_embed)

            log_embed = discord.Embed(
                title="✅ Заявка принята",
                color=config.COLOR_ACCEPTED,
                timestamp=datetime.now(timezone.utc),
            )
            log_embed.add_field(
                name="Заявитель",
                value=f"<@{user_id}>\nDiscord ID: `{user_id}`",
                inline=False,
            )
            log_embed.add_field(
                name="Minecraft",
                value=f"`{nickname}`",
                inline=False,
            )
            log_embed.add_field(
                name="Рассмотрел",
                value=reviewer_text(interaction.user),
                inline=False,
            )
            log_embed.add_field(name="Роли", value=roles_text, inline=False)
            log_embed.add_field(
                name="RCON",
                value=rcon_response[:1024],
                inline=False,
            )
            log_embed.add_field(
                name="Личное сообщение",
                value="✅ Доставлено" if dm_sent else "❌ Не доставлено",
                inline=False,
            )
            await send_log(self.bot, interaction.guild, log_embed)

            result_lines = ["✅ Заявка принята."]
            if assigned_roles:
                result_lines.append(
                    "Выданы роли: "
                    + ", ".join(role.name for role in assigned_roles)
                )
            if role_errors:
                result_lines.append(
                    "Некоторые роли не выданы — подробности записаны в заявке."
                )
            result_lines.append(
                "ЛС отправлено." if dm_sent else "ЛС отправить не удалось."
            )
            await interaction.followup.send(
                "\n".join(result_lines),
                ephemeral=True,
            )

    @discord.ui.button(
        label="Отклонить",
        emoji="🚫",
        style=discord.ButtonStyle.danger,
        custom_id="funfernus:review:reject:v2",
    )
    async def reject(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.message is None:
            await interaction.response.send_message(
                "❌ Не удалось получить заявку.",
                ephemeral=True,
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ Не удалось определить администратора.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RejectModal(
                self.bot,
                interaction.message,
                interaction.user,
            )
        )


# ============================================================
# ПАНЕЛЬ УПРАВЛЕНИЯ: ВЫБОР КАНАЛОВ
# ============================================================


CHANNEL_SETTING_LABELS = {
    "panel_channel_id": "Публичная панель",
    "review_channel_id": "Рассмотрение заявок",
    "log_channel_id": "Логи заявок",
    "rcon_channel_id": "RCON-консоль",
}


class ChannelPicker(discord.ui.ChannelSelect):
    def __init__(
        self,
        bot: "FunFernusBot",
        setting_name: str,
        row: int,
    ) -> None:
        self.bot = bot
        self.setting_name = setting_name
        label = CHANNEL_SETTING_LABELS[setting_name]
        super().__init__(
            custom_id=f"funfernus:settings:channel:{setting_name}:v2",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
            ],
            placeholder=f"{label}: выбрать канал",
            min_values=1,
            max_values=1,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await require_staff(interaction):
            return
        if interaction.guild is None:
            return

        settings = self.bot.settings_store.get_settings(interaction.guild.id)
        if settings is None:
            await interaction.response.send_message(
                "❌ Панель ещё не инициализирована. Проверь `CONFIG_CHANNEL_ID` и перезапусти бота.",
                ephemeral=True,
            )
            return

        selected = self.values[0]
        setattr(settings, self.setting_name, int(selected.id))
        await self.bot.settings_store.save(settings)
        await update_control_panel(self.bot, interaction.guild)

        label = CHANNEL_SETTING_LABELS[self.setting_name]
        await send_settings_log(
            self.bot,
            interaction.guild,
            interaction.user,
            "Изменён канал",
            f"{label}: <#{selected.id}>",
        )
        await interaction.response.edit_message(
            content=(
                "📍 **Настройка каналов**\n\n"
                f"✅ {label}: <#{selected.id}>\n\n"
                "Можно продолжить и выбрать остальные каналы."
            ),
            view=self.view,
        )


class ChannelSettingsView(discord.ui.View):
    def __init__(self, bot: "FunFernusBot") -> None:
        super().__init__(timeout=600)
        for row, setting_name in enumerate(CHANNEL_SETTING_LABELS):
            self.add_item(ChannelPicker(bot, setting_name, row))


# ============================================================
# ПАНЕЛЬ УПРАВЛЕНИЯ: ВЫБОР НЕСКОЛЬКИХ РОЛЕЙ
# ============================================================


class AcceptRolePicker(discord.ui.RoleSelect):
    def __init__(self, bot: "FunFernusBot") -> None:
        self.bot = bot
        super().__init__(
            custom_id="funfernus:settings:roles:select:v2",
            placeholder="Выбрать роли, выдаваемые после принятия",
            min_values=0,
            max_values=25,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await require_staff(interaction):
            return
        if interaction.guild is None:
            return

        settings = self.bot.settings_store.get_settings(interaction.guild.id)
        if settings is None:
            await interaction.response.send_message(
                "❌ Панель ещё не инициализирована. Проверь `CONFIG_CHANNEL_ID` и перезапусти бота.",
                ephemeral=True,
            )
            return

        selected_roles = [
            role
            for role in self.values
            if isinstance(role, discord.Role)
            and not role.is_default()
        ]
        settings.accept_role_ids = [role.id for role in selected_roles][:25]
        await self.bot.settings_store.save(settings)
        await update_control_panel(self.bot, interaction.guild)
        await send_settings_log(
            self.bot,
            interaction.guild,
            interaction.user,
            "Изменены роли при принятии",
            (
                "\n".join(role.mention for role in selected_roles)
                if selected_roles
                else "Список ролей очищен"
            ),
        )

        text = (
            "✅ Роли сохранены:\n"
            + (
                "\n".join(role.mention for role in selected_roles)
                if selected_roles
                else "Роли очищены."
            )
        )
        await interaction.response.edit_message(content=text, view=self.view)


class RoleSettingsView(discord.ui.View):
    def __init__(self, bot: "FunFernusBot") -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.add_item(AcceptRolePicker(bot))

    @discord.ui.button(
        label="Очистить роли",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="funfernus:settings:roles:clear:v2",
        row=1,
    )
    async def clear_roles(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await require_staff(interaction):
            return
        if interaction.guild is None:
            return
        settings = self.bot.settings_store.get_settings(interaction.guild.id)
        if settings is None:
            return
        settings.accept_role_ids = []
        await self.bot.settings_store.save(settings)
        await update_control_panel(self.bot, interaction.guild)
        await send_settings_log(
            self.bot,
            interaction.guild,
            interaction.user,
            "Очищены роли при принятии",
            "Автоматическая выдача ролей отключена.",
        )
        await interaction.response.edit_message(
            content="✅ Список ролей очищен.",
            view=self,
        )


# ============================================================
# ПАНЕЛЬ УПРАВЛЕНИЯ: ТЕКСТ И ОФОРМЛЕНИЕ
# ============================================================


class PanelTextModal(discord.ui.Modal):
    def __init__(self, bot: "FunFernusBot", guild_id: int) -> None:
        super().__init__(title="Текст публичной панели", timeout=600)
        self.bot = bot
        self.guild_id = guild_id
        draft = bot.settings_store.get_draft(guild_id) or PanelDraft()
        settings = bot.settings_store.get_settings(guild_id) or GuildSettings()

        self.panel_title = discord.ui.TextInput(
            label="Заголовок",
            default=draft.title[:256],
            max_length=256,
            required=True,
        )
        self.panel_description = discord.ui.TextInput(
            label="Основной текст",
            default=draft.description[:1000],
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.panel_footer = discord.ui.TextInput(
            label="Нижняя подпись",
            default=draft.footer[:200],
            max_length=200,
            required=False,
        )
        self.button_label = discord.ui.TextInput(
            label="Текст кнопки",
            default=settings.button_label[:80],
            max_length=80,
            required=True,
        )
        for item in (
            self.panel_title,
            self.panel_description,
            self.panel_footer,
            self.button_label,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_staff(interaction):
            return
        if interaction.guild is None:
            return

        draft = self.bot.settings_store.get_draft(self.guild_id) or PanelDraft()
        settings = self.bot.settings_store.get_settings(self.guild_id)
        if settings is None:
            await interaction.response.send_message(
                "❌ Настройки не найдены.",
                ephemeral=True,
            )
            return

        draft.title = str(self.panel_title.value).strip()
        draft.description = str(self.panel_description.value).strip()
        draft.footer = str(self.panel_footer.value).strip()
        settings.button_label = str(self.button_label.value).strip()

        await self.bot.settings_store.save_draft(self.guild_id, draft)
        await self.bot.settings_store.save(settings)
        await update_control_panel(self.bot, interaction.guild)
        await send_settings_log(
            self.bot,
            interaction.guild,
            interaction.user,
            "Изменён текст публичной панели",
            f"Новый заголовок: **{draft.title}**",
        )
        await interaction.response.send_message(
            "✅ Текст сохранён в черновик. Нажми **«Опубликовать»**, "
            "чтобы обновить публичную панель.",
            ephemeral=True,
        )


class AppearanceModal(discord.ui.Modal):
    def __init__(self, bot: "FunFernusBot", guild_id: int) -> None:
        super().__init__(title="Оформление панели", timeout=600)
        self.bot = bot
        self.guild_id = guild_id
        draft = bot.settings_store.get_draft(guild_id) or PanelDraft()
        settings = bot.settings_store.get_settings(guild_id) or GuildSettings()

        self.color_hex = discord.ui.TextInput(
            label="Цвет рамки HEX",
            default=f"#{draft.color:06X}",
            placeholder="#19B9D1",
            max_length=7,
            required=True,
        )
        self.button_emoji = discord.ui.TextInput(
            label="Эмодзи кнопки",
            default=settings.button_emoji[:50],
            placeholder="📋",
            max_length=50,
            required=False,
        )
        self.button_style = discord.ui.TextInput(
            label="Цвет кнопки: green / blue / red / gray",
            default=settings.button_style,
            max_length=10,
            required=True,
        )
        self.thumbnail_url = discord.ui.TextInput(
            label="URL маленькой картинки (необязательно)",
            default=draft.thumbnail_url[:1000],
            placeholder="https://...",
            max_length=1000,
            required=False,
        )
        for item in (
            self.color_hex,
            self.button_emoji,
            self.button_style,
            self.thumbnail_url,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_staff(interaction):
            return
        if interaction.guild is None:
            return

        color_match = HEX_COLOR_RE.fullmatch(
            str(self.color_hex.value).strip()
        )
        if color_match is None:
            await interaction.response.send_message(
                "❌ Цвет должен быть в формате `#19B9D1`.",
                ephemeral=True,
            )
            return

        style_name = str(self.button_style.value).strip().lower()
        if style_name not in {"green", "blue", "red", "gray", "grey"}:
            await interaction.response.send_message(
                "❌ Цвет кнопки: `green`, `blue`, `red` или `gray`.",
                ephemeral=True,
            )
            return
        if style_name == "grey":
            style_name = "gray"

        draft = self.bot.settings_store.get_draft(self.guild_id) or PanelDraft()
        settings = self.bot.settings_store.get_settings(self.guild_id)
        if settings is None:
            return

        draft.color = int(color_match.group(1), 16)
        draft.thumbnail_url = str(self.thumbnail_url.value).strip()
        settings.button_emoji = str(self.button_emoji.value).strip()
        settings.button_style = style_name

        await self.bot.settings_store.save_draft(self.guild_id, draft)
        await self.bot.settings_store.save(settings)
        await update_control_panel(self.bot, interaction.guild)
        await send_settings_log(
            self.bot,
            interaction.guild,
            interaction.user,
            "Изменено оформление публичной панели",
            f"Цвет: `#{draft.color:06X}`, стиль кнопки: `{settings.button_style}`",
        )
        await interaction.response.send_message(
            "✅ Оформление сохранено в черновик.",
            ephemeral=True,
        )


# ============================================================
# ГЛАВНАЯ ПАНЕЛЬ УПРАВЛЕНИЯ
# ============================================================




class PanelBannerUploadModal(discord.ui.Modal):
    def __init__(self, bot: "FunFernusBot", guild_id: int) -> None:
        super().__init__(title="Загрузить баннер панели", timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.file_field = discord.ui.Label(
            text="Выберите файл баннера",
            description="Широкий баннер PNG/JPG/WEBP/GIF до 10 МБ. Рекомендуется 1600×600.",
            component=discord.ui.FileUpload(
                custom_id="funfernus_panel_banner_file",
                required=True,
                min_values=1,
                max_values=1,
            ),
        )
        self.add_item(self.file_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        settings = self.bot.settings_store.get_settings(self.guild_id)
        draft = self.bot.settings_store.get_draft(self.guild_id)
        if settings is None or draft is None:
            await interaction.response.send_message("Панель ещё не инициализирована.", ephemeral=True)
            return

        component = self.file_field.component
        image = component.values[0] if isinstance(component, discord.ui.FileUpload) and component.values else None
        if image is None:
            await interaction.response.send_message("Файл не выбран.", ephemeral=True)
            return

        filename = image.filename.lower()
        if not filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            await interaction.response.send_message("Поддерживаются PNG, JPG, JPEG, WEBP и GIF.", ephemeral=True)
            return
        if image.size > 10 * 1024 * 1024:
            await interaction.response.send_message("Файл слишком большой. Максимум 10 МБ.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        config_channel = await get_text_channel(self.bot, settings.config_channel_id)
        if config_channel is None:
            await interaction.followup.send("Канал bot-config не найден.", ephemeral=True)
            return

        try:
            data = await image.read()
            asset_message = await config_channel.send(
                content="FUNFERNUS_BANNER_ASSET_V3 — не удалять, файл используется ботом",
                file=discord.File(io.BytesIO(data), filename=image.filename),
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Не удалось сохранить баннер: `{exc}`", ephemeral=True)
            return

        if not asset_message.attachments:
            await interaction.followup.send("Discord не вернул сохранённое вложение.", ephemeral=True)
            return

        old_id = int(settings.banner_asset_message_id or 0)
        settings.banner_asset_message_id = asset_message.id
        draft.image_url = asset_message.attachments[0].url
        await self.bot.settings_store.save_draft(self.guild_id, draft)
        await self.bot.settings_store.save(settings)
        await update_control_panel(self.bot, interaction.guild)

        if old_id:
            try:
                old = await config_channel.fetch_message(old_id)
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await interaction.followup.send("Баннер загружен из файла и сохранён в config-канале.", ephemeral=True)


class PanelBannerActionView(discord.ui.View):
    def __init__(self, bot: "FunFernusBot", guild_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.button(label="Загрузить / заменить", emoji="📎", style=discord.ButtonStyle.primary)
    async def upload(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PanelBannerUploadModal(self.bot, self.guild_id))

    @discord.ui.button(label="Удалить баннер", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        settings = self.bot.settings_store.get_settings(self.guild_id)
        draft = self.bot.settings_store.get_draft(self.guild_id)
        if settings is None or draft is None:
            await interaction.response.send_message("Панель ещё не инициализирована.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        config_channel = await get_text_channel(self.bot, settings.config_channel_id)
        old_id = int(settings.banner_asset_message_id or 0)
        draft.image_url = ""
        settings.banner_asset_message_id = 0
        await self.bot.settings_store.save_draft(self.guild_id, draft)
        await self.bot.settings_store.save(settings)
        await update_control_panel(self.bot, interaction.guild)
        if old_id and config_channel is not None:
            try:
                old = await config_channel.fetch_message(old_id)
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await interaction.followup.send("Баннер удалён.", ephemeral=True)


class ControlPanelView(discord.ui.View):
    def __init__(self, bot: "FunFernusBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    @discord.ui.button(
        label="Каналы",
        emoji="📍",
        style=discord.ButtonStyle.primary,
        custom_id="funfernus:control:channels:v2",
        row=0,
    )
    async def channels(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "📍 **Выбери каналы для каждой функции.**\n"
            "Изменения сохраняются сразу и не требуют перезапуска.",
            view=ChannelSettingsView(self.bot),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Роли",
        emoji="🎭",
        style=discord.ButtonStyle.primary,
        custom_id="funfernus:control:roles:v2",
        row=0,
    )
    async def roles(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            return
        settings = self.bot.settings_store.get_settings(interaction.guild.id)
        current = (
            format_roles(interaction.guild, settings.accept_role_ids)
            if settings is not None
            else "Не выбраны"
        )
        await interaction.response.send_message(
            "🎭 **Роли при принятии заявки**\n\n"
            f"Сейчас:\n{current}\n\n"
            "Можно выбрать до 25 ролей одновременно.",
            view=RoleSettingsView(self.bot),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Текст",
        emoji="📝",
        style=discord.ButtonStyle.secondary,
        custom_id="funfernus:control:text:v2",
        row=0,
    )
    async def text_settings(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_modal(
            PanelTextModal(self.bot, interaction.guild.id)
        )

    @discord.ui.button(
        label="Оформление",
        emoji="🎨",
        style=discord.ButtonStyle.secondary,
        custom_id="funfernus:control:appearance:v2",
        row=0,
    )
    async def appearance(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_modal(
            AppearanceModal(self.bot, interaction.guild.id)
        )

    @discord.ui.button(
        label="Баннер",
        emoji="🖼️",
        style=discord.ButtonStyle.secondary,
        custom_id="funfernus:control:banner:v2",
        row=0,
    )
    async def banner(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(
            "🖼️ **Большой баннер панели заявки**\nЗагрузите файл — он появится широкой картинкой сверху внутри одной рамки.",
            view=PanelBannerActionView(self.bot, interaction.guild.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Предпросмотр",
        emoji="👁️",
        style=discord.ButtonStyle.secondary,
        custom_id="funfernus:control:preview:v2",
        row=1,
    )
    async def preview(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            return
        settings = self.bot.settings_store.get_settings(interaction.guild.id)
        draft = self.bot.settings_store.get_draft(interaction.guild.id)
        if settings is None or draft is None:
            await interaction.response.send_message(
                "❌ Черновик не найден.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=ApplicationPanelView(
                self.bot,
                settings.button_label,
                settings.button_emoji,
                settings.button_style,
                draft=draft,
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Опубликовать",
        emoji="🚀",
        style=discord.ButtonStyle.success,
        custom_id="funfernus:control:publish:v2",
        row=1,
    )
    async def publish(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True)
        ok, text = await publish_panel(self.bot, interaction.guild)
        if ok:
            await send_settings_log(
                self.bot,
                interaction.guild,
                interaction.user,
                "Опубликована публичная панель",
                text,
            )
        await interaction.followup.send(
            ("✅ " if ok else "❌ ") + text,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Проверить RCON",
        emoji="🧪",
        style=discord.ButtonStyle.secondary,
        custom_id="funfernus:control:rcon-test:v2",
        row=1,
    )
    async def rcon_test_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        ok, response = await run_rcon("list")
        await interaction.followup.send(
            ("✅" if ok else "❌")
            + " **RCON**\n\n"
            + f"```{response[:1800]}```",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Сбросить оформление",
        emoji="♻️",
        style=discord.ButtonStyle.danger,
        custom_id="funfernus:control:reset-panel:v2",
        row=1,
    )
    async def reset_panel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            return
        settings = self.bot.settings_store.get_settings(interaction.guild.id)
        if settings is None:
            return
        draft = PanelDraft()
        settings.button_label = config.DEFAULT_BUTTON_LABEL
        settings.button_emoji = config.DEFAULT_BUTTON_EMOJI
        settings.button_style = config.DEFAULT_BUTTON_STYLE
        await self.bot.settings_store.save_draft(interaction.guild.id, draft)
        await self.bot.settings_store.save(settings)
        await update_control_panel(self.bot, interaction.guild)
        await send_settings_log(
            self.bot,
            interaction.guild,
            interaction.user,
            "Сброшено оформление панели",
            "Черновик возвращён к стандартному тексту и оформлению.",
        )
        await interaction.response.send_message(
            "✅ Текст и оформление черновика сброшены к стандартным.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Обновить",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="funfernus:control:refresh:v2",
        row=1,
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            return
        await update_control_panel(self.bot, interaction.guild)
        await interaction.response.send_message(
            "✅ Панель управления обновлена.",
            ephemeral=True,
        )


# ============================================================
# ПЕРВИЧНАЯ ИНИЦИАЛИЗАЦИЯ В ГОТОВОМ CONFIG-КАНАЛЕ
# ============================================================


async def get_env_config_channel(
    guild: discord.Guild,
) -> discord.TextChannel | None:
    """
    Возвращает заранее созданный пользователем config-канал.
    Бот никогда не создаёт категории или каналы автоматически.
    """

    if not CONFIG_CHANNEL_ID:
        return None

    channel = guild.get_channel(CONFIG_CHANNEL_ID)

    if isinstance(channel, discord.TextChannel):
        return channel

    try:
        fetched = await bot.fetch_channel(CONFIG_CHANNEL_ID)
    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
    ):
        return None

    if (
        isinstance(fetched, discord.TextChannel)
        and fetched.guild.id == guild.id
    ):
        return fetched

    return None


async def initialize_from_config_channel(
    current_bot: "FunFernusBot",
    guild: discord.Guild,
) -> tuple[GuildSettings, PanelDraft]:
    """
    Инициализирует панель в CONFIG_CHANNEL_ID.

    Канал config пользователь создаёт самостоятельно.
    Остальные каналы выбираются через ChannelSelect в панели.
    """

    config_channel = await get_env_config_channel(guild)

    if config_channel is None:
        raise RuntimeError(
            "CONFIG_CHANNEL_ID не указан, канал не найден "
            "или бот не имеет к нему доступа."
        )

    loaded = await current_bot.settings_store.load(guild)

    if loaded is None:
        settings = GuildSettings(
            guild_id=guild.id,
            config_channel_id=config_channel.id,
            control_channel_id=config_channel.id,
        )

        settings, draft = await current_bot.settings_store.create(
            guild,
            config_channel,
            settings,
            PanelDraft(),
        )
    else:
        settings, draft = loaded

        # Config-канал из ENV всегда является каналом управления.
        settings.config_channel_id = config_channel.id
        settings.control_channel_id = config_channel.id

        await current_bot.settings_store.save(settings)

    current_bot._loaded_guilds.add(guild.id)
    await update_control_panel(current_bot, guild)

    return settings, draft


# ============================================================
# RCON-МОДАЛЬ
# ============================================================


class RconConsoleModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="Minecraft RCON", timeout=600)
        self.command = discord.ui.TextInput(
            label="Команда без символа /",
            placeholder="list или whitelist list",
            max_length=500,
            required=True,
        )
        self.add_item(self.command)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await require_staff(interaction):
            return
        if interaction.guild is None:
            return
        bot = interaction.client
        if not isinstance(bot, FunFernusBot):
            return

        settings = bot.settings_store.get_settings(interaction.guild.id)
        if settings is None or interaction.channel_id != settings.rcon_channel_id:
            await interaction.response.send_message(
                "❌ RCON-команды доступны только в настроенном канале RCON.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        command = str(self.command.value).strip().lstrip("/")
        ok, response = await run_rcon(command)
        embed = discord.Embed(
            title="✅ RCON: выполнено" if ok else "❌ RCON: ошибка",
            color=config.COLOR_ACCEPTED if ok else config.COLOR_REJECTED,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Команда", value=f"`{command}`", inline=False)
        embed.add_field(
            name="Ответ",
            value=f"```{response[:3500]}```",
            inline=False,
        )
        embed.add_field(
            name="Выполнил",
            value=reviewer_text(interaction.user),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=False)

        log_embed = embed.copy()
        log_embed.title = "🖥 Выполнена RCON-команда"
        log_embed.color = config.COLOR_LOG
        await send_log(bot, interaction.guild, log_embed)


# ============================================================
# ЕДИНАЯ ПАНЕЛЬ НАСТРОЙКИ
# ============================================================


class UnifiedSetupView(discord.ui.View):
    def __init__(self, bot: "FunFernusBot") -> None:
        super().__init__(timeout=900)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    @discord.ui.button(label="Заявки", emoji="📝", style=discord.ButtonStyle.primary)
    async def applications(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        settings, _ = await initialize_from_config_channel(self.bot, interaction.guild)
        await interaction.response.send_message(
            embed=build_control_embed(interaction.guild, settings),
            view=ControlPanelView(self.bot),
            ephemeral=True,
        )

    @discord.ui.button(label="Сообщество", emoji="🎫", style=discord.ButtonStyle.secondary)
    async def community(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.bot.unified_store.get(interaction.guild.id) or await self.bot.unified_store.load_or_create(interaction.guild)
        embed = discord.Embed(
            title="⚙️ Настройка сообщества",
            description="Поддержка, предложения, приветствие и их постоянные баннеры.",
            color=int(state.options.get("accent_color", 0x19B9D1)),
        )
        await interaction.response.send_message(
            embed=embed,
            view=CommunitySetupView(self.bot, self.bot.unified_store),
            ephemeral=True,
        )

    @discord.ui.button(label="Правительство", emoji="⚖️", style=discord.ButtonStyle.secondary)
    async def government(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.bot.unified_store.get(interaction.guild.id) or await self.bot.unified_store.load_or_create(interaction.guild)
        embed = discord.Embed(
            title="⚙️ Настройка правительства",
            description="Судебные иски, судьи, каналы и постоянный баннер панели.",
            color=int(state.options.get("accent_color", 0x19B9D1)),
        )
        await interaction.response.send_message(
            embed=embed,
            view=GovernmentSetupView(self.bot, self.bot.unified_store),
            ephemeral=True,
        )

    @discord.ui.button(label="Города", emoji="🏰", style=discord.ButtonStyle.secondary)
    async def cities(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.bot.unified_store.get(interaction.guild.id) or await self.bot.unified_store.load_or_create(interaction.guild)
        embed = discord.Embed(
            title="⚙️ Настройка системы городов",
            description="Регистрация, модерация, форум-реестр, роль мэра и синхронное управление карточками.",
            color=int(state.options.get("accent_color", 0x19B9D1)),
        )
        await interaction.response.send_message(
            embed=embed,
            view=CitySetupView(self.bot, self.bot.unified_store),
            ephemeral=True,
        )


# ============================================================
# БОТ
# ============================================================


class FunFernusBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.messages = True
        intents.dm_messages = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.admin_user_ids = ADMIN_USER_IDS
        self.settings_store = DiscordSettingsStore(
            self,
            preferred_config_channel_id=CONFIG_CHANNEL_ID,
        )
        self._loaded_guilds: set[int] = set()
        self.unified_store = UnifiedDiscordStore(self, CONFIG_CHANNEL_ID)

    async def setup_hook(self) -> None:
        self.add_view(ApplicationPanelView(self))
        self.add_view(ReviewView(self))
        self.add_view(ControlPanelView(self))
        await setup_community(self, self.unified_store, ADMIN_USER_IDS)
        await setup_government(self, self.unified_store, ADMIN_USER_IDS)
        await setup_cities(self, self.unified_store, ADMIN_USER_IDS)

        if GUILD_ID:
            guild_object = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild_object)
            synced = await self.tree.sync(guild=guild_object)
            log.info("Команды синхронизированы с сервером: %s", len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Глобально синхронизировано команд: %s", len(synced))


bot = FunFernusBot()


@bot.tree.command(name="настроить_бота", description="Открыть единую панель настройки FunFernus Bot")
@app_commands.guild_only()
async def unified_setup(interaction: discord.Interaction) -> None:
    if not await require_staff(interaction):
        return
    embed = discord.Embed(
        title="⚙️ Единая панель FunFernus Bot",
        description=(
            "Выберите нужный раздел. Банковская система в этот бот не входит.\n\n"
            "• **Заявки** — анкета, рассмотрение, роли и RCON.\n"
            "• **Сообщество** — поддержка, предложения и приветствие.\n"
            "• **Правительство** — судебные иски и судьи.\n"
            "• **Города** — заявки, модерация, реестр и управление карточками.\n"
            "• `/публикация` — красивое сообщение с баннером в текущем канале."
        ),
        color=config.COLOR_CONTROL,
    )
    await interaction.response.send_message(embed=embed, view=UnifiedSetupView(bot), ephemeral=True)


# ============================================================
# СОБЫТИЯ
# ============================================================


@bot.event
async def on_ready() -> None:
    log.info("============================================")
    log.info("FUNFERNUS DISCORD BOT ЗАПУЩЕН")
    log.info("Аккаунт: %s", bot.user)
    log.info("Bot ID: %s", bot.user.id if bot.user else "?")
    log.info("GUILD_ID: %s", GUILD_ID or "не указан")
    log.info(
        "CONFIG_CHANNEL_ID: %s",
        CONFIG_CHANNEL_ID or "не указан",
    )
    log.info("ADMIN_USER_IDS: %s", sorted(ADMIN_USER_IDS))
    log.info("RCON_TEST_MODE: %s", RCON_TEST_MODE)
    log.info("RCON_ENABLED: %s", RCON_ENABLED)
    log.info("============================================")

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=config.BOT_ACTIVITY,
        ),
    )

    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue

        try:
            unified_state = await bot.unified_store.load_or_create(guild)
            if unified_state.channels.get("support_panel"):
                await publish_support_panel(bot, bot.unified_store, guild, unified_state)
            if unified_state.channels.get("government_panel") and unified_state.channels.get("government_review"):
                await publish_government_panel(bot, bot.unified_store, guild, unified_state)
            if unified_state.channels.get("city_application") and unified_state.channels.get("city_management"):
                await publish_city_panels(bot, bot.unified_store, guild, unified_state)
        except Exception:
            log.exception("Не удалось загрузить объединённое хранилище для сервера %s", guild.id)

        if guild.id in bot._loaded_guilds:
            continue

        try:
            settings, _ = await initialize_from_config_channel(
                bot,
                guild,
            )

            log.info(
                "Панель управления готова в config-канале %s.",
                settings.config_channel_id,
            )
        except Exception:
            log.exception(
                "Не удалось инициализировать панель управления "
                "для сервера %s. Проверь CONFIG_CHANNEL_ID и права бота.",
                guild.id,
            )


# ============================================================
# SLASH-КОМАНДЫ
# ============================================================


@bot.tree.command(
    name="funfernus_setup",
    description="Восстановить панель управления в config-канале",
)
@app_commands.guild_only()
async def funfernus_setup(
    interaction: discord.Interaction,
) -> None:
    if not await require_staff(interaction):
        return

    if interaction.guild is None:
        return

    await interaction.response.defer(ephemeral=True)

    try:
        settings, _ = await initialize_from_config_channel(
            bot,
            interaction.guild,
        )
    except Exception as exc:
        log.exception("Ошибка инициализации config-канала")

        await interaction.followup.send(
            "❌ Не удалось открыть панель управления.\n\n"
            "Проверь:\n"
            "• `CONFIG_CHANNEL_ID` в переменных окружения;\n"
            "• что канал создан вручную;\n"
            "• что бот видит канал и может отправлять сообщения.\n\n"
            f"Ошибка: `{type(exc).__name__}: {exc}`",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        "✅ Панель управления восстановлена в "
        f"{format_channel(settings.config_channel_id)}.\n\n"
        "Бот не создаёт никаких каналов. "
        "Открой config-канал и выбери там публичную панель, "
        "рассмотрение заявок, логи, RCON и роли.",
        ephemeral=True,
    )


@bot.tree.command(
    name="setup_applications",
    description="Опубликовать или обновить публичную панель заявок",
)
@app_commands.guild_only()
async def setup_applications(interaction: discord.Interaction) -> None:
    if not await require_staff(interaction):
        return
    if interaction.guild is None:
        return
    await interaction.response.defer(ephemeral=True)
    ok, text = await publish_panel(bot, interaction.guild)
    if ok:
        await send_settings_log(
            bot,
            interaction.guild,
            interaction.user,
            "Опубликована публичная панель",
            text,
        )
    await interaction.followup.send(
        ("✅ " if ok else "❌ ") + text,
        ephemeral=True,
    )


@bot.tree.command(
    name="panel_banner",
    description="Открыть файловую загрузку баннера публичной панели",
)
@app_commands.guild_only()
async def panel_banner(interaction: discord.Interaction) -> None:
    if not await require_staff(interaction):
        return
    if interaction.guild is None:
        return
    await interaction.response.send_modal(
        PanelBannerUploadModal(bot, interaction.guild.id)
    )


@bot.tree.command(
    name="rcon",
    description="Выполнить команду Minecraft RCON",
)
@app_commands.describe(command="Команда без символа /")
@app_commands.guild_only()
async def rcon_command(
    interaction: discord.Interaction,
    command: str,
) -> None:
    if not await require_staff(interaction):
        return
    if interaction.guild is None:
        return

    settings = bot.settings_store.get_settings(interaction.guild.id)
    if settings is None or interaction.channel_id != settings.rcon_channel_id:
        await interaction.response.send_message(
            "❌ Эта команда работает только в настроенном канале RCON.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    clean_command = command.strip().lstrip("/")
    ok, response = await run_rcon(clean_command)
    embed = discord.Embed(
        title="✅ RCON: выполнено" if ok else "❌ RCON: ошибка",
        color=config.COLOR_ACCEPTED if ok else config.COLOR_REJECTED,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Команда", value=f"`{clean_command}`", inline=False)
    embed.add_field(
        name="Ответ",
        value=f"```{response[:3500]}```",
        inline=False,
    )
    embed.add_field(
        name="Выполнил",
        value=reviewer_text(interaction.user),
        inline=False,
    )
    await interaction.followup.send(embed=embed)

    log_embed = embed.copy()
    log_embed.title = "🖥 Выполнена RCON-команда"
    log_embed.color = config.COLOR_LOG
    await send_log(bot, interaction.guild, log_embed)


@bot.tree.command(
    name="rcon_console",
    description="Открыть окно ввода команды RCON",
)
@app_commands.guild_only()
async def rcon_console(interaction: discord.Interaction) -> None:
    if not await require_staff(interaction):
        return
    if interaction.guild is None:
        return
    settings = bot.settings_store.get_settings(interaction.guild.id)
    if settings is None or interaction.channel_id != settings.rcon_channel_id:
        await interaction.response.send_message(
            "❌ RCON-консоль открывается только в настроенном канале RCON.",
            ephemeral=True,
        )
        return
    await interaction.response.send_modal(RconConsoleModal())


@bot.tree.command(
    name="rcon_test",
    description="Проверить подключение к Minecraft RCON",
)
@app_commands.guild_only()
async def rcon_test(interaction: discord.Interaction) -> None:
    if not await require_staff(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    ok, response = await run_rcon("list")
    await interaction.followup.send(
        ("✅" if ok else "❌") + f" **RCON**\n```{response[:1800]}```",
        ephemeral=True,
    )


@bot.tree.command(
    name="whoami",
    description="Показать Discord ID и статус доступа",
)
@app_commands.guild_only()
async def whoami(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="🔎 Проверка доступа", color=config.COLOR_PANEL)
    embed.add_field(
        name="Твой Discord ID",
        value=f"`{interaction.user.id}`",
        inline=False,
    )
    embed.add_field(
        name="Доступ администратора",
        value="✅ Да" if is_staff(interaction) else "❌ Нет",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="bot_status",
    description="Показать текущие настройки бота без секретов",
)
@app_commands.guild_only()
async def bot_status(interaction: discord.Interaction) -> None:
    if not await require_staff(interaction):
        return
    if interaction.guild is None:
        return
    settings = bot.settings_store.get_settings(interaction.guild.id)
    if settings is None:
        await interaction.response.send_message(
            "❌ Бот ещё не настроен. Проверь CONFIG_CHANNEL_ID.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=build_control_embed(interaction.guild, settings),
        ephemeral=True,
    )


# ============================================================
# ПРОВЕРКА И ЗАПУСК
# ============================================================


def validate_settings() -> None:
    errors: list[str] = []
    if not CONFIG_CHANNEL_ID:
        errors.append(
            "Не указан CONFIG_CHANNEL_ID. "
            "Создай config-канал вручную и укажи его ID."
        )

    if not DISCORD_TOKEN:
        errors.append("Не указан DISCORD_TOKEN.")
    if not GUILD_ID:
        log.warning(
            "GUILD_ID не указан. Глобальные slash-команды могут появиться не сразу."
        )
    if RCON_ENABLED and not RCON_TEST_MODE and not RCON_HOST:
        errors.append("RCON включён, но не указан RCON_HOST.")
    if RCON_ENABLED and not RCON_TEST_MODE and not RCON_PASSWORD:
        errors.append("RCON включён, но не указан RCON_PASSWORD.")
    if errors:
        raise RuntimeError("\n".join(f"- {error}" for error in errors))


if __name__ == "__main__":
    validate_settings()
    bot.run(DISCORD_TOKEN, log_handler=None)
