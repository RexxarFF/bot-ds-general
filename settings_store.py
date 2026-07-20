from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

import discord

import config


STATE_MARKER = "FUNFERNUS_CONFIG_V2"
DRAFT_MARKER = "FUNFERNUS_DRAFT_V2"

log = logging.getLogger("funfernus-settings")


@dataclass
class GuildSettings:
    schema: int = 2
    guild_id: int = 0

    config_channel_id: int = 0
    state_message_id: int = 0
    draft_message_id: int = 0

    control_channel_id: int = 0
    control_message_id: int = 0
    control_message_channel_id: int = 0

    panel_channel_id: int = 0
    panel_message_id: int = 0
    panel_message_channel_id: int = 0

    review_channel_id: int = 0
    log_channel_id: int = 0
    rcon_channel_id: int = 0

    banner_asset_message_id: int = 0
    accept_role_ids: list[int] = field(default_factory=list)

    button_label: str = config.DEFAULT_BUTTON_LABEL
    button_emoji: str = config.DEFAULT_BUTTON_EMOJI
    button_style: str = config.DEFAULT_BUTTON_STYLE

    draft_revision: int = 1
    published_revision: int = 0

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GuildSettings":
        allowed = {
            item.name
            for item in cls.__dataclass_fields__.values()
        }
        cleaned = {
            key: item
            for key, item in value.items()
            if key in allowed
        }
        settings = cls(**cleaned)
        settings.accept_role_ids = [
            int(role_id)
            for role_id in settings.accept_role_ids
            if str(role_id).isdigit()
        ][:25]
        return settings

    def to_compact_json(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass
class PanelDraft:
    title: str = config.DEFAULT_PANEL_TITLE
    description: str = config.DEFAULT_PANEL_DESCRIPTION
    footer: str = config.DEFAULT_PANEL_FOOTER
    image_url: str = config.DEFAULT_PANEL_IMAGE_URL
    thumbnail_url: str = config.DEFAULT_PANEL_THUMBNAIL_URL
    color: int = config.DEFAULT_PANEL_COLOR

    @classmethod
    def from_embed(cls, embed: discord.Embed) -> "PanelDraft":
        footer_text = ""
        if embed.footer and embed.footer.text:
            footer_text = embed.footer.text

        image_url = ""
        if embed.image and embed.image.url:
            image_url = str(embed.image.url)

        thumbnail_url = ""
        if embed.thumbnail and embed.thumbnail.url:
            thumbnail_url = str(embed.thumbnail.url)

        return cls(
            title=embed.title or config.DEFAULT_PANEL_TITLE,
            description=(
                embed.description
                or config.DEFAULT_PANEL_DESCRIPTION
            ),
            footer=(
                footer_text
                or config.DEFAULT_PANEL_FOOTER
            ),
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            color=(
                embed.color.value
                if embed.color is not None
                else config.DEFAULT_PANEL_COLOR
            ),
        )

    def to_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.title,
            description=self.description,
            color=self.color,
        )

        if self.footer:
            embed.set_footer(text=self.footer)
        if self.image_url:
            embed.set_image(url=self.image_url)
        if self.thumbnail_url:
            embed.set_thumbnail(url=self.thumbnail_url)

        return embed


class DiscordSettingsStore:
    """
    Настройки хранятся не в SQLite и не в локальном JSON-файле.

    Числовые параметры лежат в закреплённом служебном сообщении,
    а текст и оформление публичной панели — в отдельном Embed-сообщении
    закрытого канала bot-config.
    """

    def __init__(
        self,
        bot: discord.Client,
        preferred_config_channel_id: int = 0,
    ) -> None:
        self.bot = bot
        self.preferred_config_channel_id = preferred_config_channel_id
        self._settings: dict[int, GuildSettings] = {}
        self._drafts: dict[int, PanelDraft] = {}
        self._locks: dict[int, Any] = {}

    def get_settings(self, guild_id: int) -> GuildSettings | None:
        return self._settings.get(guild_id)

    def get_draft(self, guild_id: int) -> PanelDraft | None:
        return self._drafts.get(guild_id)

    def set_cached(
        self,
        settings: GuildSettings,
        draft: PanelDraft,
    ) -> None:
        self._settings[settings.guild_id] = settings
        self._drafts[settings.guild_id] = draft

    async def _find_config_channel(
        self,
        guild: discord.Guild,
    ) -> discord.TextChannel | None:
        # CONFIG_CHANNEL_ID из .env является главным источником.
        # Благодаря этому бот не ищет и не создаёт каналы сам.
        if self.preferred_config_channel_id:
            preferred = guild.get_channel(
                self.preferred_config_channel_id
            )

            if isinstance(preferred, discord.TextChannel):
                return preferred

            try:
                fetched = await self.bot.fetch_channel(
                    self.preferred_config_channel_id
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                fetched = None

            if (
                isinstance(fetched, discord.TextChannel)
                and fetched.guild.id == guild.id
            ):
                return fetched

        for channel in guild.text_channels:
            topic = channel.topic or ""
            if STATE_MARKER in topic:
                return channel

        for channel in guild.text_channels:
            try:
                pins = await channel.pins()
            except (discord.Forbidden, discord.HTTPException):
                continue

            for message in pins:
                if (
                    message.author.id == self.bot.user.id
                    and STATE_MARKER in message.content
                ):
                    return channel

        return None

    @staticmethod
    def _parse_state_content(content: str) -> dict[str, Any]:
        marker_position = content.find(STATE_MARKER)

        if marker_position < 0:
            raise ValueError("Это не служебное сообщение FunFernus.")

        raw = content[
            marker_position + len(STATE_MARKER):
        ].strip()
        if raw.startswith("```json"):
            raw = raw[7:]
        elif raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]

        return json.loads(raw.strip())

    @staticmethod
    def _state_content(settings: GuildSettings) -> str:
        content = (
            "⚙️ **Служебные настройки FunFernus — не удалять**\n"
            f"{STATE_MARKER}\n```json\n"
            f"{settings.to_compact_json()}\n```"
        )
        if len(content) > 2000:
            raise ValueError(
                "Служебное сообщение настроек стало слишком длинным."
            )
        return content

    async def load(
        self,
        guild: discord.Guild,
    ) -> tuple[GuildSettings, PanelDraft] | None:
        config_channel = await self._find_config_channel(guild)
        if config_channel is None:
            return None

        state_message: discord.Message | None = None

        try:
            pins = await config_channel.pins()
        except (discord.Forbidden, discord.HTTPException):
            pins = []

        for message in pins:
            if (
                self.bot.user is not None
                and message.author.id == self.bot.user.id
                and STATE_MARKER in message.content
            ):
                state_message = message
                break

        if state_message is None:
            try:
                async for message in config_channel.history(limit=50):
                    if (
                        self.bot.user is not None
                        and message.author.id == self.bot.user.id
                        and STATE_MARKER in message.content
                    ):
                        state_message = message
                        break
            except (discord.Forbidden, discord.HTTPException):
                return None

        if state_message is None:
            return None

        try:
            raw = self._parse_state_content(state_message.content)
            settings = GuildSettings.from_dict(raw)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            log.error("Не удалось прочитать настройки сервера %s: %s", guild.id, exc)
            return None

        settings.guild_id = guild.id
        settings.config_channel_id = config_channel.id
        settings.state_message_id = state_message.id

        draft_message: discord.Message | None = None
        if settings.draft_message_id:
            try:
                draft_message = await config_channel.fetch_message(
                    settings.draft_message_id
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                draft_message = None

        if draft_message is None or not draft_message.embeds:
            draft = PanelDraft()
            draft_message = await config_channel.send(
                content=DRAFT_MARKER,
                embed=draft.to_embed(),
            )
            settings.draft_message_id = draft_message.id
            try:
                await draft_message.pin(reason="Черновик панели FunFernus")
            except (discord.Forbidden, discord.HTTPException):
                pass
        else:
            draft = PanelDraft.from_embed(draft_message.embeds[0])

        self.set_cached(settings, draft)
        await self.save(settings)
        return settings, draft

    async def create(
        self,
        guild: discord.Guild,
        config_channel: discord.TextChannel,
        settings: GuildSettings,
        draft: PanelDraft | None = None,
    ) -> tuple[GuildSettings, PanelDraft]:
        draft = draft or PanelDraft()

        settings.guild_id = guild.id
        settings.config_channel_id = config_channel.id

        try:
            if STATE_MARKER not in (config_channel.topic or ""):
                await config_channel.edit(
                    topic=config.CONFIG_CHANNEL_TOPIC,
                    reason="Служебный канал FunFernus",
                )
        except (discord.Forbidden, discord.HTTPException):
            pass

        draft_message = await config_channel.send(
            content=DRAFT_MARKER,
            embed=draft.to_embed(),
        )
        settings.draft_message_id = draft_message.id

        state_message = await config_channel.send(
            self._state_content(settings)
        )
        settings.state_message_id = state_message.id

        for message in (draft_message, state_message):
            try:
                await message.pin(reason="Служебные настройки FunFernus")
            except (discord.Forbidden, discord.HTTPException):
                pass

        self.set_cached(settings, draft)
        await self.save(settings)
        return settings, draft

    async def save(
        self,
        settings: GuildSettings,
    ) -> None:
        guild = self.bot.get_guild(settings.guild_id)
        if guild is None:
            raise RuntimeError("Сервер не найден в кэше бота.")

        channel = guild.get_channel(settings.config_channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(
                    settings.config_channel_id
                )
            except (discord.Forbidden, discord.HTTPException):
                fetched = None
            channel = fetched if isinstance(fetched, discord.TextChannel) else None

        if channel is None:
            raise RuntimeError("Служебный канал bot-config не найден.")

        message: discord.Message | None = None
        if settings.state_message_id:
            try:
                message = await channel.fetch_message(
                    settings.state_message_id
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None

        if message is None:
            message = await channel.send(
                self._state_content(settings)
            )
            settings.state_message_id = message.id
            try:
                await message.pin(reason="Настройки FunFernus")
            except (discord.Forbidden, discord.HTTPException):
                pass
        else:
            await message.edit(
                content=self._state_content(settings)
            )

        self._settings[settings.guild_id] = settings

    async def save_draft(
        self,
        guild_id: int,
        draft: PanelDraft,
    ) -> None:
        settings = self.get_settings(guild_id)
        if settings is None:
            raise RuntimeError("Бот ещё не настроен на этом сервере.")

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise RuntimeError("Сервер не найден.")

        channel = guild.get_channel(settings.config_channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Канал bot-config не найден.")

        message: discord.Message | None = None
        if settings.draft_message_id:
            try:
                message = await channel.fetch_message(
                    settings.draft_message_id
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None

        if message is None:
            message = await channel.send(
                content=DRAFT_MARKER,
                embed=draft.to_embed(),
            )
            settings.draft_message_id = message.id
            try:
                await message.pin(reason="Черновик панели FunFernus")
            except (discord.Forbidden, discord.HTTPException):
                pass
        else:
            await message.edit(
                content=DRAFT_MARKER,
                embed=draft.to_embed(),
            )

        settings.draft_revision += 1
        self._drafts[guild_id] = draft
        await self.save(settings)
