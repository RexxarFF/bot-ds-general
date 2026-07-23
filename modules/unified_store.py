from __future__ import annotations

import asyncio
import io
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Any

import discord

log = logging.getLogger("funfernus-unified-store")

STATE_MARKER = "FUNFERNUS_UNIFIED_STATE_V1"
STATE_FILENAME = "funfernus_unified_state.json"
ASSET_MARKER = "FUNFERNUS_UNIFIED_ASSET_V1"


@dataclass
class AssetRef:
    url: str = ""
    message_id: int = 0
    filename: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> "AssetRef":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            url=str(raw.get("url", "")),
            message_id=int(raw.get("message_id", 0) or 0),
            filename=str(raw.get("filename", "")),
        )


@dataclass
class UnifiedState:
    schema: int = 1
    guild_id: int = 0
    config_channel_id: int = 0
    state_message_id: int = 0

    channels: dict[str, int] = field(default_factory=lambda: {
        "support_panel": 0,
        "support_review": 0,
        "support_logs": 0,
        "government_panel": 0,
        "government_review": 0,
        "government_logs": 0,
        "city_application": 0,
        "city_review": 0,
        "city_registry": 0,
        "city_management": 0,
        "city_logs": 0,
        "welcome_application": 0,
        "welcome_rules": 0,
        "welcome_news": 0,
    })
    roles: dict[str, list[int]] = field(default_factory=lambda: {
        "support_staff": [],
        "government_judges": [],
        "city_mayor": [],
        "city_staff": [],
    })
    messages: dict[str, int] = field(default_factory=lambda: {
        "support_panel": 0,
        "government_panel": 0,
        "city_application": 0,
        "city_management": 0,
    })
    assets: dict[str, dict[str, Any]] = field(default_factory=lambda: {
        "support_panel": asdict(AssetRef()),
        "welcome": asdict(AssetRef()),
        "government_panel": asdict(AssetRef()),
        "city_application_panel": asdict(AssetRef()),
    })
    texts: dict[str, str] = field(default_factory=lambda: {
        "support_title": "Поддержка FunFernus",
        "support_description": "Выберите подходящее действие ниже. Администрация рассмотрит обращение и ответит вам.",
        "support_footer": "FunFernus • Поддержка",
        "government_title": "Подача судебного иска",
        "government_description": "Нажмите кнопку ниже, заполните форму и при необходимости отправьте доказательства боту в личные сообщения.",
        "government_footer": "FunFernus • Правительство",
        "welcome_title": "Добро пожаловать на FunFernus!",
        "welcome_text": "Рады видеть вас на сервере. Ознакомьтесь с правилами и подайте заявку через панель сервера.",
        "city_application_title": "Регистрация города FunFernus",
        "city_application_description": "Нажмите кнопку ниже, выберите руководство и заполните данные будущего города.",
        "city_application_footer": "FunFernus • Реестр городов",
    })
    options: dict[str, Any] = field(default_factory=lambda: {
        "accent_color": 0x19B9D1,
        "welcome_enabled": True,
        "welcome_delay": 2,
        "city_allowed_bot_ids": [],
        "city_application_banner_path": "",
        "city_moderation_banner_path": "",
        "city_registry_banner_path": "",
        "city_management_banner_path": "",
        "city_notification_banner_path": "",
        "city_warning_banner_path": "",
        "city_leadership_banner_path": "",
        "city_logs_banner_path": "",
        "city_setup_banner_path": "",
    })
    counters: dict[str, int] = field(default_factory=lambda: {
        "ticket": 0,
        "suggestion": 0,
        "complaint": 0,
        "case": 0,
        "city": 0,
    })
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    cases: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_drafts: dict[str, str] = field(default_factory=dict)
    cities: dict[str, dict[str, Any]] = field(default_factory=dict)
    city_drafts: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "UnifiedState":
        state = cls()
        if not isinstance(raw, dict):
            return state
        for name in ("schema", "guild_id", "config_channel_id", "state_message_id"):
            if name in raw:
                setattr(state, name, int(raw[name] or 0))
        for name in ("channels", "roles", "messages", "texts", "options", "counters", "tickets", "cases", "active_drafts", "cities", "city_drafts"):
            value = raw.get(name)
            if isinstance(value, dict):
                current = getattr(state, name)
                current.update(value)
        assets = raw.get("assets")
        if isinstance(assets, dict):
            for key, value in assets.items():
                state.assets[key] = asdict(AssetRef.from_dict(value))
        state.channels = {k: int(v or 0) for k, v in state.channels.items()}
        state.messages = {k: int(v or 0) for k, v in state.messages.items()}
        state.roles = {
            k: [int(x) for x in values if str(x).isdigit()][:25]
            for k, values in state.roles.items()
            if isinstance(values, list)
        }
        state.counters = {k: int(v or 0) for k, v in state.counters.items()}
        state.schema = 1
        return state

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def next_id(self, kind: str, prefix: str) -> str:
        self.counters[kind] = int(self.counters.get(kind, 0)) + 1
        return f"{prefix}-{self.counters[kind]:04d}"

    def asset(self, key: str) -> AssetRef:
        return AssetRef.from_dict(self.assets.get(key, {}))

    def set_asset(self, key: str, value: AssetRef) -> None:
        self.assets[key] = asdict(value)


class UnifiedDiscordStore:
    def __init__(self, bot: discord.Client, config_channel_id: int) -> None:
        self.bot = bot
        self.config_channel_id = config_channel_id
        self._states: dict[int, UnifiedState] = {}
        self._messages: dict[int, discord.Message] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, guild_id: int) -> UnifiedState | None:
        return self._states.get(guild_id)

    def lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def config_channel(self, guild: discord.Guild) -> discord.TextChannel:
        channel = guild.get_channel(self.config_channel_id)
        if not isinstance(channel, discord.TextChannel):
            fetched = await self.bot.fetch_channel(self.config_channel_id)
            if not isinstance(fetched, discord.TextChannel) or fetched.guild.id != guild.id:
                raise RuntimeError("CONFIG_CHANNEL_ID должен вести в текстовый канал этого сервера.")
            channel = fetched
        return channel

    @staticmethod
    def _state_file(state: UnifiedState) -> discord.File:
        raw = json.dumps(state.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        return discord.File(io.BytesIO(raw), filename=STATE_FILENAME)

    async def _find_message(self, channel: discord.TextChannel) -> discord.Message | None:
        try:
            pins = await channel.pins()
        except discord.HTTPException:
            pins = []
        for message in pins:
            if self.bot.user and message.author.id == self.bot.user.id and STATE_MARKER in message.content:
                return message
        async for message in channel.history(limit=150):
            if self.bot.user and message.author.id == self.bot.user.id and STATE_MARKER in message.content:
                return message
        return None

    async def load_or_create(self, guild: discord.Guild) -> UnifiedState:
        channel = await self.config_channel(guild)
        message = await self._find_message(channel)
        state = UnifiedState(guild_id=guild.id, config_channel_id=channel.id)
        if message and message.attachments:
            try:
                attachment = next((x for x in message.attachments if x.filename == STATE_FILENAME), message.attachments[0])
                state = UnifiedState.from_dict(json.loads((await attachment.read()).decode("utf-8")))
            except Exception:
                log.exception("Не удалось прочитать unified state, создано новое состояние")
        if message is None:
            message = await channel.send(
                f"⚙️ **Служебное хранилище объединённого бота — не удалять**\n`{STATE_MARKER}`",
                file=self._state_file(state),
            )
            try:
                await message.pin(reason="Хранилище объединённого FunFernus Bot")
            except discord.HTTPException:
                pass
        state.guild_id = guild.id
        state.config_channel_id = channel.id
        state.state_message_id = message.id
        self._states[guild.id] = state
        self._messages[guild.id] = message
        await self.save(state)
        return state

    async def save(self, state: UnifiedState) -> None:
        async with self.lock(state.guild_id):
            guild = self.bot.get_guild(state.guild_id)
            if guild is None:
                raise RuntimeError("Сервер не найден.")
            channel = await self.config_channel(guild)
            message = self._messages.get(state.guild_id)
            if message is None and state.state_message_id:
                try:
                    message = await channel.fetch_message(state.state_message_id)
                except discord.HTTPException:
                    message = None
            content = f"⚙️ **Служебное хранилище объединённого бота — не удалять**\n`{STATE_MARKER}`"
            if message is None:
                message = await channel.send(content, file=self._state_file(state))
                state.state_message_id = message.id
            else:
                message = await message.edit(content=content, attachments=[self._state_file(state)])
            self._messages[state.guild_id] = message
            self._states[state.guild_id] = state

    @staticmethod
    def validate_image(attachment: discord.Attachment) -> None:
        name = attachment.filename.lower()
        content_type = (attachment.content_type or "").lower()
        if not content_type.startswith("image/") and not name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            raise ValueError("Поддерживаются PNG, JPG, JPEG, WEBP и GIF.")
        if attachment.size > 10 * 1024 * 1024:
            raise ValueError("Максимальный размер баннера — 10 МБ.")

    async def persist_asset_bytes(
        self,
        guild: discord.Guild,
        data: bytes,
        filename: str,
        label: str,
    ) -> AssetRef:
        """Сохранить уже загруженный файл в закрытом config-канале.

        Метод нужен публикациям форума: один и тот же набор байтов
        одновременно прикрепляется к стартовому сообщению форума и
        сохраняется в служебном хранилище Discord.
        """
        channel = await self.config_channel(guild)
        safe_name = filename.replace("/", "_").replace("\\", "_").strip() or "banner.png"
        message = await channel.send(
            f"🖼️ **Ресурс объединённого бота:** {label}\n`{ASSET_MARKER}`\nНе удаляйте это сообщение — файл используется ботом.",
            file=discord.File(io.BytesIO(data), filename=safe_name),
        )
        if not message.attachments:
            raise RuntimeError("Discord не вернул сохранённое вложение.")
        saved = message.attachments[0]
        return AssetRef(url=saved.url, message_id=message.id, filename=saved.filename)

    async def persist_asset(self, guild: discord.Guild, attachment: discord.Attachment, label: str) -> AssetRef:
        self.validate_image(attachment)
        data = await attachment.read()
        return await self.persist_asset_bytes(guild, data, attachment.filename, label)

    async def replace_asset(self, guild: discord.Guild, state: UnifiedState, key: str, attachment: discord.Attachment, label: str) -> AssetRef:
        old = state.asset(key)
        new = await self.persist_asset(guild, attachment, label)
        state.set_asset(key, new)
        await self.save(state)
        if old.message_id:
            try:
                channel = await self.config_channel(guild)
                await (await channel.fetch_message(old.message_id)).delete()
            except discord.HTTPException:
                pass
        return new
