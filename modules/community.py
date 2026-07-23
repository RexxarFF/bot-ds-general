from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

from .components import build_framed_container, build_framed_view, parse_color
from .unified_store import UnifiedDiscordStore, UnifiedState

log = logging.getLogger("funfernus-community")


def _admin(interaction: discord.Interaction, admin_ids: set[int]) -> bool:
    return bool(
        interaction.guild
        and (
            interaction.guild.owner_id == interaction.user.id
            or interaction.user.id in admin_ids
            or (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator)
        )
    )


def _staff(member: discord.Member | discord.User, state: UnifiedState, admin_ids: set[int]) -> bool:
    if member.id in admin_ids:
        return True
    if isinstance(member, discord.Member):
        if member.guild.owner_id == member.id or member.guild_permissions.administrator:
            return True
        allowed = set(state.roles.get("support_staff", []))
        return any(role.id in allowed for role in member.roles)
    return False


async def _text_channel(
    bot: commands.Bot,
    channel_id: int,
) -> discord.TextChannel | discord.Thread | None:
    """Return a channel where the bot can send a normal message.

    Forum posts are represented by ``discord.Thread``.  The previous version
    accepted only ``discord.TextChannel``, so commands used inside a forum post
    were incorrectly reported as unavailable.
    """
    if not channel_id:
        return None

    channel = bot.get_channel(channel_id)
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel

    try:
        channel = await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None

    return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None


def _image_from_label(label: discord.ui.Label) -> discord.Attachment | None:
    component = label.component
    if isinstance(component, discord.ui.FileUpload) and component.values:
        return component.values[0]
    return None


async def _send_support_log(
    bot: commands.Bot,
    state: UnifiedState,
    embed: discord.Embed,
) -> None:
    """Отправить служебный лог поддержки, если канал настроен."""

    channel = await _text_channel(bot, state.channels.get("support_logs", 0))
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        log.exception("Не удалось отправить лог поддержки")


class PublicationModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, admin_ids: set[int], channel_id: int) -> None:
        super().__init__(title="Создать красивую публикацию", timeout=600)
        self.bot = bot
        self.store = store
        self.admin_ids = admin_ids
        self.channel_id = channel_id
        self.title_field = discord.ui.Label(
            text="Заголовок",
            description="Будет показан крупным текстом внутри рамки.",
            component=discord.ui.TextInput(custom_id="publication_title", max_length=200, required=True),
        )
        self.body_field = discord.ui.Label(
            text="Текст публикации",
            description="Поддерживается Discord Markdown: **жирный**, списки, ссылки и эмодзи.",
            component=discord.ui.TextInput(custom_id="publication_body", style=discord.TextStyle.paragraph, max_length=4000, required=True),
        )
        self.color_field = discord.ui.Label(
            text="Цвет рамки",
            description="HEX, например #19B9D1. Можно оставить пустым.",
            component=discord.ui.TextInput(custom_id="publication_color", max_length=7, required=False, placeholder="#19B9D1"),
        )
        self.footer_field = discord.ui.Label(
            text="Подпись внизу",
            description="Необязательно.",
            component=discord.ui.TextInput(custom_id="publication_footer", max_length=300, required=False),
        )
        self.banner_field = discord.ui.Label(
            text="Баннер",
            description="Широкий баннер PNG/JPG/WEBP/GIF до 10 МБ. Рекомендуется 1200×630 или 1600×840.",
            component=discord.ui.FileUpload(custom_id="publication_banner", required=True, min_values=1, max_values=1),
        )
        for item in (self.title_field, self.body_field, self.color_field, self.footer_field, self.banner_field):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _admin(interaction, self.admin_ids):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        image = _image_from_label(self.banner_field)
        if image is None:
            await interaction.response.send_message("❌ Выберите файл баннера.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = await _text_channel(self.bot, self.channel_id)
        if channel is None:
            await interaction.followup.send("❌ Канал, в котором была введена команда, больше недоступен.", ephemeral=True)
            return
        try:
            asset = await self.store.persist_asset(interaction.guild, image, "Публикация")
        except Exception as exc:
            await interaction.followup.send(f"❌ Не удалось сохранить баннер: `{exc}`", ephemeral=True)
            return
        title = str(self.title_field.component.value).strip()  # type: ignore[attr-defined]
        body = str(self.body_field.component.value).strip()  # type: ignore[attr-defined]
        color_raw = str(self.color_field.component.value).strip()  # type: ignore[attr-defined]
        footer = str(self.footer_field.component.value).strip()  # type: ignore[attr-defined]
        state = self.store.get(interaction.guild.id)
        fallback = int(state.options.get("accent_color", 0x19B9D1)) if state else 0x19B9D1
        view = build_framed_view(
            title=title,
            body=body,
            banner_url=asset.url,
            color=parse_color(color_raw, fallback),
            footer=footer,
            timeout=None,
        )
        try:
            message = await channel.send(view=view)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ Discord не принял публикацию: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Публикация отправлена: {message.jump_url}", ephemeral=True)


async def _forum_channel(
    bot: commands.Bot,
    channel_id: int,
) -> discord.ForumChannel | None:
    """Вернуть форум-канал по ID, включая получение через API."""
    if not channel_id:
        return None

    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.ForumChannel):
        return channel

    try:
        channel = await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None

    return channel if isinstance(channel, discord.ForumChannel) else None


async def _delete_stored_asset(
    store: UnifiedDiscordStore,
    guild: discord.Guild,
    message_id: int,
) -> None:
    if not message_id:
        return
    try:
        channel = await store.config_channel(guild)
        message = await channel.fetch_message(message_id)
        await message.delete()
    except discord.HTTPException:
        pass


class ForumPublicationModal(discord.ui.Modal):
    """Создаёт новую публикацию форума с баннером в стартовом сообщении."""

    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        admin_ids: set[int],
        forum_channel_id: int,
    ) -> None:
        super().__init__(title="Создать публикацию форума", timeout=600)
        self.bot = bot
        self.store = store
        self.admin_ids = admin_ids
        self.forum_channel_id = forum_channel_id

        self.title_field = discord.ui.Label(
            text="Название публикации",
            description="Используется как название карточки форума и заголовок сообщения.",
            component=discord.ui.TextInput(
                custom_id="forum_publication_title",
                max_length=100,
                required=True,
            ),
        )
        self.body_field = discord.ui.Label(
            text="Текст публикации",
            description="Поддерживается Discord Markdown: **жирный**, списки, ссылки и эмодзи.",
            component=discord.ui.TextInput(
                custom_id="forum_publication_body",
                style=discord.TextStyle.paragraph,
                max_length=4000,
                required=True,
            ),
        )
        self.color_field = discord.ui.Label(
            text="Цвет рамки",
            description="HEX, например #19B9D1. Можно оставить пустым.",
            component=discord.ui.TextInput(
                custom_id="forum_publication_color",
                max_length=7,
                required=False,
                placeholder="#19B9D1",
            ),
        )
        self.footer_field = discord.ui.Label(
            text="Подпись внизу",
            description="Необязательно.",
            component=discord.ui.TextInput(
                custom_id="forum_publication_footer",
                max_length=300,
                required=False,
            ),
        )
        self.banner_field = discord.ui.Label(
            text="Баннер",
            description="PNG/JPG/WEBP/GIF до 10 МБ.",
            component=discord.ui.FileUpload(
                custom_id="forum_publication_banner",
                required=True,
                min_values=1,
                max_values=1,
            ),
        )

        for item in (
            self.title_field,
            self.body_field,
            self.color_field,
            self.footer_field,
            self.banner_field,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _admin(interaction, self.admin_ids):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return

        image = _image_from_label(self.banner_field)
        if image is None:
            await interaction.response.send_message("❌ Выберите файл баннера.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        forum = await _forum_channel(self.bot, self.forum_channel_id)
        if forum is None or forum.guild.id != interaction.guild.id:
            await interaction.followup.send("❌ Выбранный форум больше недоступен.", ephemeral=True)
            return

        try:
            self.store.validate_image(image)
            banner_bytes = await image.read()
        except Exception as exc:
            await interaction.followup.send(f"❌ Не удалось прочитать баннер: `{exc}`", ephemeral=True)
            return

        safe_name = image.filename.replace("/", "_").replace("\\", "_").strip() or "banner.png"
        title = str(self.title_field.component.value).strip()  # type: ignore[attr-defined]
        body = str(self.body_field.component.value).strip()  # type: ignore[attr-defined]
        color_raw = str(self.color_field.component.value).strip()  # type: ignore[attr-defined]
        footer = str(self.footer_field.component.value).strip()  # type: ignore[attr-defined]

        state = self.store.get(interaction.guild.id)
        fallback = int(state.options.get("accent_color", 0x19B9D1)) if state else 0x19B9D1
        view = build_framed_view(
            title=title,
            body=body,
            banner_url=f"attachment://{safe_name}",
            color=parse_color(color_raw, fallback),
            footer=footer,
            timeout=None,
        )

        # Сохраняем копию баннера тем же способом, что и остальные ресурсы бота.
        # При этом оригинал прикрепляется к стартовому сообщению форума, поэтому
        # карточка в режиме «Галерея» всегда получает изображение.
        stored_asset = None
        try:
            stored_asset = await self.store.persist_asset_bytes(
                interaction.guild,
                banner_bytes,
                safe_name,
                f"Форум • {title}",
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Не удалось сохранить баннер: `{exc}`", ephemeral=True)
            return

        thread_kwargs: dict[str, object] = {}
        require_tag = bool(getattr(forum.flags, "require_tag", False))
        if require_tag:
            available = [tag for tag in forum.available_tags if not tag.moderated]
            if not available:
                available = list(forum.available_tags)
            if available:
                thread_kwargs["applied_tags"] = [available[0]]
            else:
                await _delete_stored_asset(
                    self.store, interaction.guild, stored_asset.message_id if stored_asset else 0
                )
                await interaction.followup.send(
                    "❌ В этом форуме обязательно нужен тег, но доступных тегов нет. "
                    "Добавьте тег в настройках форума или отключите обязательный выбор тега.",
                    ephemeral=True,
                )
                return

        try:
            created = await forum.create_thread(
                name=title[:100],
                file=discord.File(io.BytesIO(banner_bytes), filename=safe_name),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                reason=f"Публикация форума от {interaction.user} ({interaction.user.id})",
                **thread_kwargs,
            )
        except discord.Forbidden:
            await _delete_stored_asset(
                self.store, interaction.guild, stored_asset.message_id if stored_asset else 0
            )
            await interaction.followup.send(
                "❌ Боту не хватает прав для создания публикаций в этом форуме. "
                "Выдайте права **Просмотр канала**, **Отправка сообщений** и **Создание публичных веток**.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await _delete_stored_asset(
                self.store, interaction.guild, stored_asset.message_id if stored_asset else 0
            )
            await interaction.followup.send(f"❌ Discord не создал публикацию: `{exc}`", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ Публикация форума создана: {created.thread.jump_url}",
            ephemeral=True,
        )


class TicketCreateModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, kind: str) -> None:
        titles = {
            "question": "Задать вопрос",
            "problem": "Сообщить о проблеме",
            "suggestion": "Предложить улучшение",
        }
        super().__init__(title=titles.get(kind, "Новое обращение"), timeout=600)
        self.bot = bot
        self.store = store
        self.kind = kind
        self.subject = discord.ui.TextInput(label="Краткая тема", max_length=120, required=True)
        self.details = discord.ui.TextInput(label="Подробное описание", style=discord.TextStyle.paragraph, max_length=4000, required=True)
        self.contacts = discord.ui.TextInput(label="Дополнительная информация", style=discord.TextStyle.paragraph, max_length=1000, required=False, placeholder="Ник Minecraft, ссылки, время события и т. п.")
        self.add_item(self.subject)
        self.add_item(self.details)
        self.add_item(self.contacts)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message("Бот ещё не инициализирован.", ephemeral=True)
            return
        review = await _text_channel(self.bot, state.channels.get("support_review", 0))
        if review is None:
            await interaction.response.send_message("Канал рассмотрения обращений не настроен.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        prefix = "SUG" if self.kind == "suggestion" else "TICKET"
        counter = "suggestion" if self.kind == "suggestion" else "ticket"
        record_id = state.next_id(counter, prefix)
        labels = {"question": "Вопрос", "problem": "Проблема", "suggestion": "Предложение"}
        embed = discord.Embed(
            title=f"{labels.get(self.kind, 'Обращение')} • {record_id}",
            color=int(state.options.get("accent_color", 0x19B9D1)),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Автор", value=f"{interaction.user.mention}\nID: `{interaction.user.id}`", inline=False)
        embed.add_field(name="Тема", value=str(self.subject)[:1024], inline=False)
        embed.add_field(name="Описание", value=str(self.details)[:1024], inline=False)
        if str(self.contacts).strip():
            embed.add_field(name="Дополнительно", value=str(self.contacts)[:1024], inline=False)
        embed.set_footer(text="Статус: ожидает рассмотрения")
        message = await review.send(embed=embed, view=TicketReviewView(self.bot, self.store, record_id))
        state.tickets[record_id] = {
            "id": record_id,
            "kind": self.kind,
            "user_id": interaction.user.id,
            "review_channel_id": review.id,
            "review_message_id": message.id,
            "status": "open",
            "subject": str(self.subject),
            "details": str(self.details),
            "contacts": str(self.contacts),
        }
        await self.store.save(state)

        log_embed = discord.Embed(
            title=f"📨 Новое обращение • {record_id}",
            description=f"Тип: **{labels.get(self.kind, 'Обращение')}**",
            color=int(state.options.get("accent_color", 0x19B9D1)),
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.add_field(
            name="Автор",
            value=f"{interaction.user.mention}\nID: `{interaction.user.id}`",
            inline=False,
        )
        log_embed.add_field(name="Тема", value=str(self.subject)[:1024], inline=False)
        await _send_support_log(self.bot, state, log_embed)

        await interaction.followup.send(f"✅ Обращение отправлено. Номер: `{record_id}`.", ephemeral=True)


class ComplaintCreateModal(discord.ui.Modal):
    """Форма жалобы на игрока либо на другую ситуацию."""

    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        complaint_kind: str,
    ) -> None:
        self.complaint_kind = complaint_kind
        title = "Жалоба на игрока" if complaint_kind == "player" else "Другая жалоба"
        super().__init__(title=title, timeout=600)
        self.bot = bot
        self.store = store

        target_label = "Ник / Discord нарушителя" if complaint_kind == "player" else "На кого или на что жалоба"
        target_placeholder = (
            "Minecraft-ник, упоминание или Discord ID"
            if complaint_kind == "player"
            else "Например: правило, система, ситуация, организация"
        )
        self.target = discord.ui.TextInput(
            label=target_label,
            placeholder=target_placeholder,
            max_length=200,
            required=True,
        )
        self.reason = discord.ui.TextInput(
            label="Краткая причина",
            placeholder="Коротко укажите суть нарушения",
            max_length=150,
            required=True,
        )
        self.details = discord.ui.TextInput(
            label="Подробное описание",
            placeholder="Что произошло, когда, где и при каких обстоятельствах",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.evidence = discord.ui.TextInput(
            label="Доказательства и дополнительная информация",
            placeholder="Ссылки на скриншоты/видео, время события, свидетели. Можно оставить пустым.",
            style=discord.TextStyle.paragraph,
            max_length=1200,
            required=False,
        )
        for item in (self.target, self.reason, self.details, self.evidence):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Жалобу можно отправить только на сервере.",
                ephemeral=True,
            )
            return

        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message(
                "Бот ещё не инициализирован.",
                ephemeral=True,
            )
            return

        review = await _text_channel(self.bot, state.channels.get("support_review", 0))
        if review is None:
            await interaction.response.send_message(
                "Канал рассмотрения обращений не настроен.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        record_id = state.next_id("complaint", "REPORT")
        type_label = "Жалоба на игрока" if self.complaint_kind == "player" else "Другая жалоба"
        embed = discord.Embed(
            title=f"⚠️ {type_label} • {record_id}",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Автор жалобы",
            value=f"{interaction.user.mention}\nID: `{interaction.user.id}`",
            inline=False,
        )
        embed.add_field(
            name="Объект жалобы",
            value=str(self.target)[:1024],
            inline=False,
        )
        embed.add_field(
            name="Причина",
            value=str(self.reason)[:1024],
            inline=False,
        )
        embed.add_field(
            name="Описание",
            value=str(self.details)[:1024],
            inline=False,
        )
        if str(self.evidence).strip():
            embed.add_field(
                name="Доказательства / дополнительно",
                value=str(self.evidence)[:1024],
                inline=False,
            )
        embed.set_footer(text="Статус: ожидает рассмотрения")

        try:
            message = await review.send(
                embed=embed,
                view=TicketReviewView(self.bot, self.store, record_id),
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Discord не принял жалобу: `{exc}`",
                ephemeral=True,
            )
            return

        state.tickets[record_id] = {
            "id": record_id,
            "kind": f"complaint_{self.complaint_kind}",
            "user_id": interaction.user.id,
            "review_channel_id": review.id,
            "review_message_id": message.id,
            "status": "open",
            "subject": str(self.reason),
            "target": str(self.target),
            "details": str(self.details),
            "evidence": str(self.evidence),
        }
        await self.store.save(state)

        log_embed = discord.Embed(
            title=f"⚠️ Новая жалоба • {record_id}",
            description=type_label,
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.add_field(
            name="Автор",
            value=f"{interaction.user.mention}\nID: `{interaction.user.id}`",
            inline=False,
        )
        log_embed.add_field(
            name="Объект жалобы",
            value=str(self.target)[:1024],
            inline=False,
        )
        log_embed.add_field(
            name="Причина",
            value=str(self.reason)[:1024],
            inline=False,
        )
        await _send_support_log(self.bot, state, log_embed)

        await interaction.followup.send(
            f"✅ Жалоба отправлена. Номер: `{record_id}`.",
            ephemeral=True,
        )


class ComplaintKindView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.store = store

    @discord.ui.button(
        label="На игрока",
        emoji="👤",
        style=discord.ButtonStyle.danger,
    )
    async def player(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            ComplaintCreateModal(self.bot, self.store, "player")
        )

    @discord.ui.button(
        label="На другое",
        emoji="⚠️",
        style=discord.ButtonStyle.secondary,
    )
    async def other(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            ComplaintCreateModal(self.bot, self.store, "other")
        )


class StaffReplyModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, record_id: str) -> None:
        super().__init__(title="Ответить пользователю", timeout=600)
        self.bot = bot
        self.store = store
        self.record_id = record_id
        self.text = discord.ui.TextInput(label="Ответ", style=discord.TextStyle.paragraph, max_length=3500)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message("Состояние не загружено.", ephemeral=True)
            return
        record = state.tickets.get(self.record_id)
        if not record:
            await interaction.response.send_message("Обращение не найдено.", ephemeral=True)
            return
        user = self.bot.get_user(int(record["user_id"]))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(record["user_id"]))
            except discord.HTTPException:
                user = None
        delivered = False
        if user is not None:
            try:
                await user.send(f"## Ответ по обращению `{self.record_id}`\n{self.text}")
                delivered = True
            except discord.HTTPException:
                pass
        record["last_reply"] = str(self.text)
        record["last_staff_id"] = interaction.user.id
        await self.store.save(state)
        await interaction.response.send_message(f"✅ Ответ сохранён. ЛС доставлено: **{'да' if delivered else 'нет'}**.", ephemeral=True)


class TicketReviewView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, record_id: str = "") -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store
        self.record_id = record_id

    @discord.ui.button(label="Ответить", emoji="💬", style=discord.ButtonStyle.primary, custom_id="unified:support:reply")
    async def reply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None or not _staff(interaction.user, state, getattr(self.bot, "admin_user_ids", set())):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        record_id = self.record_id or _ticket_id_from_message(interaction.message)
        if not record_id:
            await interaction.response.send_message("Не удалось определить номер обращения.", ephemeral=True)
            return
        await interaction.response.send_modal(StaffReplyModal(self.bot, self.store, record_id))

    @discord.ui.button(label="Закрыть", emoji="✅", style=discord.ButtonStyle.success, custom_id="unified:support:close")
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None or not _staff(interaction.user, state, getattr(self.bot, "admin_user_ids", set())):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        record_id = self.record_id or _ticket_id_from_message(interaction.message)
        record = state.tickets.get(record_id)
        if record:
            record["status"] = "closed"
            record["closed_by"] = interaction.user.id
            await self.store.save(state)
        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0].copy()
            embed.color = discord.Color.green()
            embed.set_footer(text=f"Закрыто: {interaction.user}")
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.send_message("✅ Закрыто.", ephemeral=True)


def _ticket_id_from_message(message: discord.Message | None) -> str:
    if not message or not message.embeds or not message.embeds[0].title:
        return ""
    return message.embeds[0].title.rsplit("•", 1)[-1].strip()


class SupportPanelView(discord.ui.LayoutView):
    """Публичная панель техподдержки с широким баннером Components V2."""

    def __init__(
        self,
        bot: commands.Bot,
        store: UnifiedDiscordStore,
        state: UnifiedState | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store

        panel_state = state or UnifiedState()
        action_row = discord.ui.ActionRow()

        question_button = discord.ui.Button(
            label="Задать вопрос",
            emoji="❓",
            style=discord.ButtonStyle.primary,
            custom_id="unified:support:question",
        )
        problem_button = discord.ui.Button(
            label="Сообщить о проблеме",
            emoji="🛠️",
            style=discord.ButtonStyle.secondary,
            custom_id="unified:support:problem",
        )
        complaint_button = discord.ui.Button(
            label="Жалоба",
            emoji="⚠️",
            style=discord.ButtonStyle.danger,
            custom_id="unified:support:complaint",
        )
        suggestion_button = discord.ui.Button(
            label="Предложить улучшение",
            emoji="💡",
            style=discord.ButtonStyle.success,
            custom_id="unified:support:suggestion",
        )

        question_button.callback = self._question
        problem_button.callback = self._problem
        complaint_button.callback = self._complaint
        suggestion_button.callback = self._suggestion

        for button in (
            question_button,
            problem_button,
            complaint_button,
            suggestion_button,
        ):
            action_row.add_item(button)

        asset = panel_state.asset("support_panel")
        self.add_item(
            build_framed_container(
                title=panel_state.texts.get(
                    "support_title",
                    "Поддержка FunFernus",
                ),
                body=panel_state.texts.get(
                    "support_description",
                    "Выберите подходящее действие ниже. Администрация рассмотрит обращение и ответит вам.",
                ),
                banner_url=asset.url,
                color=int(panel_state.options.get("accent_color", 0x19B9D1)),
                footer=panel_state.texts.get(
                    "support_footer",
                    "FunFernus • Поддержка",
                ),
                action_row=action_row,
            )
        )

    async def _question(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            TicketCreateModal(self.bot, self.store, "question")
        )

    async def _problem(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            TicketCreateModal(self.bot, self.store, "problem")
        )

    async def _complaint(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Выберите тип жалобы:",
            view=ComplaintKindView(self.bot, self.store),
            ephemeral=True,
        )

    async def _suggestion(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            TicketCreateModal(self.bot, self.store, "suggestion")
        )


async def publish_support_panel(
    bot: commands.Bot,
    store: UnifiedDiscordStore,
    guild: discord.Guild,
    state: UnifiedState,
) -> tuple[bool, str]:
    channel = await _text_channel(bot, state.channels.get("support_panel", 0))
    if channel is None:
        return False, "Канал панели поддержки не выбран."

    panel_view = SupportPanelView(bot, store, state)
    old_id = state.messages.get("support_panel", 0)

    if old_id:
        try:
            message = await channel.fetch_message(old_id)
            await message.edit(
                content=None,
                embeds=[],
                attachments=[],
                view=panel_view,
            )
            return True, "Панель поддержки обновлена."
        except discord.HTTPException:
            pass

    message = await channel.send(view=panel_view)
    state.messages["support_panel"] = message.id
    await store.save(state)
    return True, "Панель поддержки опубликована."


class BannerUploadModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, key: str, label: str) -> None:
        super().__init__(title=f"Загрузить: {label}", timeout=600)
        self.bot = bot
        self.store = store
        self.key = key
        self.label = label
        self.file_label = discord.ui.Label(
            text="Файл баннера",
            description="Широкий баннер PNG/JPG/WEBP/GIF до 10 МБ. Рекомендуется 1200×630 или 1600×840.",
            component=discord.ui.FileUpload(custom_id=f"banner_{key}", required=True, min_values=1, max_values=1),
        )
        self.add_item(self.file_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        image = _image_from_label(self.file_label)
        if image is None:
            await interaction.response.send_message("Файл не выбран.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.edit_original_response(content="❌ Состояние не загружено.")
            return
        try:
            await self.store.replace_asset(interaction.guild, state, self.key, image, self.label)
        except Exception as exc:
            await interaction.edit_original_response(content=f"❌ Ошибка сохранения: `{exc}`")
            return
        if self.key == "support_panel":
            await publish_support_panel(self.bot, self.store, interaction.guild, state)
        await interaction.edit_original_response(
            content="✅ Баннер сохранён. Индикатор загрузки закрыт, изменения применены."
        )


class CommunityChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, key: str, label: str) -> None:
        super().__init__(placeholder=label, channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.bot = bot
        self.store = store
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message("Состояние не загружено.", ephemeral=True)
            return
        state.channels[self.key] = self.values[0].id
        await self.store.save(state)
        await interaction.response.send_message(f"✅ Выбран канал: {self.values[0].mention}", ephemeral=True)


class CommunityRoleSelect(discord.ui.RoleSelect):
    def __init__(self, store: UnifiedDiscordStore) -> None:
        super().__init__(placeholder="Выберите роли сотрудников поддержки", min_values=0, max_values=25)
        self.store = store

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            return
        state.roles["support_staff"] = [role.id for role in self.values]
        await self.store.save(state)
        await interaction.response.send_message("✅ Роли поддержки сохранены.", ephemeral=True)


class CommunityPanelTextModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, state: UnifiedState) -> None:
        super().__init__(title="Текст панели техподдержки", timeout=600)
        self.bot = bot
        self.store = store
        self.title_field = discord.ui.TextInput(
            label="Заголовок панели",
            default=state.texts.get("support_title", "Поддержка FunFernus")[:256],
            max_length=256,
            required=True,
        )
        self.description_field = discord.ui.TextInput(
            label="Основной текст",
            default=state.texts.get(
                "support_description",
                "Выберите подходящее действие ниже.",
            )[:2000],
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=True,
        )
        self.footer_field = discord.ui.TextInput(
            label="Подпись снизу",
            default=state.texts.get(
                "support_footer",
                "FunFernus • Поддержка",
            )[:300],
            max_length=300,
            required=False,
        )
        for item in (
            self.title_field,
            self.description_field,
            self.footer_field,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message(
                "Состояние не загружено.",
                ephemeral=True,
            )
            return

        state.texts["support_title"] = str(self.title_field).strip()
        state.texts["support_description"] = str(self.description_field).strip()
        state.texts["support_footer"] = str(self.footer_field).strip()
        await self.store.save(state)

        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, result = await publish_support_panel(
            self.bot,
            self.store,
            interaction.guild,
            state,
        )
        await interaction.followup.send(
            ("✅ " if ok else "⚠️ ") + result,
            ephemeral=True,
        )


class WelcomeLinkChannelSelect(discord.ui.ChannelSelect):
    def __init__(
        self,
        store: UnifiedDiscordStore,
        key: str,
        placeholder: str,
        channel_types: list[discord.ChannelType],
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            channel_types=channel_types,
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
            await interaction.response.send_message("❌ Состояние не загружено.", ephemeral=True)
            return
        channel = self.values[0]
        state.channels[self.key] = channel.id
        await self.store.save(state)
        await interaction.response.send_message(
            f"✅ Ссылка приветствия сохранена: {channel.mention}",
            ephemeral=True,
        )


class CommunitySetupView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.store = store

    @discord.ui.button(label="Каналы", emoji="📍", style=discord.ButtonStyle.secondary)
    async def channels(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = discord.ui.View(timeout=600)
        view.add_item(CommunityChannelSelect(self.bot, self.store, "support_panel", "Канал публичной панели"))
        view.add_item(CommunityChannelSelect(self.bot, self.store, "support_review", "Канал рассмотрения"))
        view.add_item(CommunityChannelSelect(self.bot, self.store, "support_logs", "Канал логов"))
        await interaction.response.send_message("Выберите каналы по очереди:", view=view, ephemeral=True)

    @discord.ui.button(label="Роли поддержки", emoji="👥", style=discord.ButtonStyle.secondary)
    async def roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = discord.ui.View(timeout=600)
        view.add_item(CommunityRoleSelect(self.store))
        await interaction.response.send_message("Выберите роли:", view=view, ephemeral=True)

    @discord.ui.button(label="Текст панели", emoji="📝", style=discord.ButtonStyle.secondary)
    async def panel_text(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message(
                "Состояние не загружено.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            CommunityPanelTextModal(self.bot, self.store, state)
        )

    @discord.ui.button(label="Баннер панели", emoji="🖼️", style=discord.ButtonStyle.primary)
    async def panel_banner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(BannerUploadModal(self.bot, self.store, "support_panel", "Панель поддержки"))

    @discord.ui.button(label="Баннер приветствия", emoji="👋", style=discord.ButtonStyle.primary)
    async def welcome_banner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(BannerUploadModal(self.bot, self.store, "welcome", "Приветствие"))

    @discord.ui.button(label="Кнопки приветствия", emoji="🔗", style=discord.ButtonStyle.secondary)
    async def welcome_links(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = discord.ui.View(timeout=600)
        view.add_item(
            WelcomeLinkChannelSelect(
                self.store,
                "welcome_application",
                "Канал подачи анкеты",
                [discord.ChannelType.text, discord.ChannelType.news],
            )
        )
        view.add_item(
            WelcomeLinkChannelSelect(
                self.store,
                "welcome_rules",
                "Форум с правилами",
                [discord.ChannelType.forum],
            )
        )
        view.add_item(
            WelcomeLinkChannelSelect(
                self.store,
                "welcome_news",
                "Канал или форум новостей",
                [discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum],
            )
        )
        await interaction.response.send_message(
            "Выберите три места, на которые будут вести кнопки в приветственном письме:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Опубликовать панель", emoji="🚀", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.followup.send("Состояние не загружено.", ephemeral=True)
            return
        ok, text = await publish_support_panel(self.bot, self.store, interaction.guild, state)
        await interaction.followup.send(("✅ " if ok else "❌ ") + text, ephemeral=True)


async def setup_community(bot: commands.Bot, store: UnifiedDiscordStore, admin_ids: set[int]) -> None:
    bot.admin_user_ids = admin_ids  # type: ignore[attr-defined]
    bot.add_view(SupportPanelView(bot, store))
    bot.add_view(TicketReviewView(bot, store))

    @bot.tree.command(name="публикация", description="Создать красивую публикацию с баннером в текущем канале")
    @app_commands.guild_only()
    async def publication(interaction: discord.Interaction) -> None:
        if not _admin(interaction, admin_ids):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        await interaction.response.send_modal(PublicationModal(bot, store, admin_ids, interaction.channel_id))

    @bot.tree.command(
        name="публикация_форум",
        description="Создать новую публикацию форума с баннером в первом сообщении",
    )
    @app_commands.describe(forum="Форум, в котором нужно создать публикацию")
    @app_commands.rename(forum="форум")
    @app_commands.guild_only()
    async def forum_publication(
        interaction: discord.Interaction,
        forum: discord.ForumChannel | None = None,
    ) -> None:
        if not _admin(interaction, admin_ids):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return

        target_forum = forum
        if target_forum is None:
            current = interaction.channel
            if isinstance(current, discord.ForumChannel):
                target_forum = current
            elif isinstance(current, discord.Thread) and isinstance(current.parent, discord.ForumChannel):
                target_forum = current.parent

        if target_forum is None:
            await interaction.response.send_message(
                "❌ Укажите форум в параметре **форум** или запустите команду внутри публикации нужного форума.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            ForumPublicationModal(bot, store, admin_ids, target_forum.id)
        )

    @bot.tree.command(name="настроить_сообщество", description="Настроить поддержку, предложения и приветствие")
    @app_commands.guild_only()
    async def community_setup(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _admin(interaction, admin_ids):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        state = store.get(interaction.guild.id) or await store.load_or_create(interaction.guild)
        support_panel_id = state.channels.get("support_panel")
        support_review_id = state.channels.get("support_review")
        support_panel_label = f"<#{support_panel_id}>" if support_panel_id else "не выбрана"
        support_review_label = f"<#{support_review_id}>" if support_review_id else "не выбрано"

        welcome_application = state.channels.get("welcome_application", 0)
        welcome_rules = state.channels.get("welcome_rules", 0)
        welcome_news = state.channels.get("welcome_news", 0)
        embed = discord.Embed(
            title="⚙️ Настройка сообщества",
            description=(
                f"Панель: {support_panel_label}\n"
                f"Рассмотрение: {support_review_label}\n"
                "Баннеры загружаются файлами и сохраняются служебными сообщениями в config-канале."
            ),
            color=int(state.options.get("accent_color", 0x19B9D1)),
        )
        embed.add_field(
            name="Кнопки приветственного письма",
            value=(
                f"Подать анкету: {f'<#{welcome_application}>' if welcome_application else 'автоматически из основной панели'}\n"
                f"Правила: {f'<#{welcome_rules}>' if welcome_rules else 'не выбраны'}\n"
                f"Новости: {f'<#{welcome_news}>' if welcome_news else 'не выбраны'}"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, view=CommunitySetupView(bot, store), ephemeral=True)

    @bot.listen("on_member_join")
    async def welcome_listener(member: discord.Member) -> None:
        if member.bot:
            return
        state = store.get(member.guild.id)
        if state is None or not bool(state.options.get("welcome_enabled", True)):
            return
        delay = min(int(state.options.get("welcome_delay", 2) or 0), 60)
        if delay:
            await asyncio.sleep(delay)
        application_id = int(state.channels.get("welcome_application", 0) or 0)
        if not application_id:
            settings_store = getattr(bot, "settings_store", None)
            settings = settings_store.get_settings(member.guild.id) if settings_store else None
            application_id = int(getattr(settings, "panel_channel_id", 0) or 0)

        links = [
            ("📋 Подать анкету", application_id),
            ("📖 Правила", int(state.channels.get("welcome_rules", 0) or 0)),
            ("📰 Новости сервера", int(state.channels.get("welcome_news", 0) or 0)),
        ]
        action_row = discord.ui.ActionRow()
        has_links = False
        for label, channel_id in links:
            if channel_id:
                has_links = True
                action_row.add_item(
                    discord.ui.Button(
                        label=label,
                        style=discord.ButtonStyle.link,
                        url=f"https://discord.com/channels/{member.guild.id}/{channel_id}",
                    )
                )

        asset = state.asset("welcome")
        view = build_framed_view(
            title=state.texts.get("welcome_title", "Добро пожаловать!"),
            body=state.texts.get("welcome_text", "Рады видеть вас на сервере."),
            banner_url=asset.url,
            color=int(state.options.get("accent_color", 0x19B9D1)),
            footer="FunFernus • Приветствие",
            action_row=action_row if has_links else None,
            timeout=None,
        )
        try:
            await member.send(view=view)
        except discord.HTTPException:
            pass
