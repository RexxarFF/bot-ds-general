from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .unified_store import UnifiedDiscordStore, UnifiedState

log = logging.getLogger("funfernus-government")


def _discord_id(value: str) -> int | None:
    match = re.search(r"\d{15,22}", value or "")
    return int(match.group()) if match else None


def _case_id(message: discord.Message | None) -> str:
    if not message or not message.embeds or not message.embeds[0].title:
        return ""
    match = re.search(r"CASE-\d{4}", message.embeds[0].title)
    return match.group() if match else ""


def _is_admin(interaction: discord.Interaction, admin_ids: set[int]) -> bool:
    return bool(
        interaction.guild
        and (
            interaction.guild.owner_id == interaction.user.id
            or interaction.user.id in admin_ids
            or (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator)
        )
    )


def _is_judge(user: discord.Member | discord.User, state: UnifiedState, admin_ids: set[int]) -> bool:
    if user.id in admin_ids:
        return True
    if isinstance(user, discord.Member):
        if user.guild.owner_id == user.id or user.guild_permissions.administrator:
            return True
        role_ids = set(state.roles.get("government_judges", []))
        return any(role.id in role_ids for role in user.roles)
    return False


async def _channel(bot: commands.Bot, channel_id: int) -> discord.TextChannel | None:
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    try:
        channel = await bot.fetch_channel(channel_id)
    except discord.HTTPException:
        return None
    return channel if isinstance(channel, discord.TextChannel) else None


def _banner_from_label(label: discord.ui.Label) -> discord.Attachment | None:
    component = label.component
    if isinstance(component, discord.ui.FileUpload) and component.values:
        return component.values[0]
    return None


def case_embed(case_id: str, case: dict, state: UnifiedState) -> discord.Embed:
    status = case.get("status", "pending")
    colors = {
        "pending": discord.Color.gold(),
        "clarification": discord.Color.orange(),
        "scheduled": discord.Color.green(),
        "rejected": discord.Color.red(),
    }
    status_text = {
        "pending": "Ожидает рассмотрения",
        "clarification": "Запрошено уточнение",
        "scheduled": "Суд назначен",
        "rejected": "Иск отклонён",
    }.get(status, status)
    embed = discord.Embed(title=f"⚖️ Судебное дело • {case_id}", color=colors.get(status, discord.Color.blurple()), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Истец", value=f"<@{case.get('plaintiff_id')}>\nMinecraft: `{case.get('plaintiff_nick', '—')}`", inline=True)
    embed.add_field(name="Ответчик", value=f"<@{case.get('defendant_id')}>\nMinecraft: `{case.get('defendant_nick', '—')}`", inline=True)
    embed.add_field(name="Статус", value=f"**{status_text}**", inline=False)
    embed.add_field(name="Суть иска", value=str(case.get("claim", "—"))[:1024], inline=False)
    embed.add_field(name="Требования", value=str(case.get("demands", "—"))[:1024], inline=False)
    evidence = case.get("evidence", [])
    if evidence:
        lines = []
        for index, item in enumerate(evidence[:12], 1):
            if item.get("type") == "file":
                lines.append(f"{index}. [{item.get('name', 'файл')}]({item.get('url', '')})")
            else:
                lines.append(f"{index}. {str(item.get('text', ''))[:180]}")
        embed.add_field(name=f"Доказательства: {len(evidence)}", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="Доказательства", value="Не приложены", inline=False)
    if status == "clarification":
        embed.add_field(name="Запрос судьи", value=str(case.get("clarification_question", "—"))[:1024], inline=False)
    if case.get("clarifications"):
        embed.add_field(name="Последнее уточнение", value=str(case["clarifications"][-1])[:1024], inline=False)
    if status == "scheduled":
        schedule = case.get("schedule", {})
        value = f"**Дата:** {schedule.get('date', '—')}\n**Время:** {schedule.get('time', '—')}\n**Место:** {schedule.get('place', '—')}"
        if schedule.get("comment"):
            value += f"\n**Комментарий:** {schedule['comment']}"
        embed.add_field(name="Судебное заседание", value=value[:1024], inline=False)
    if status == "rejected":
        embed.add_field(name="Причина отклонения", value=str(case.get("rejection_reason", "—"))[:1024], inline=False)
    embed.set_footer(text=state.texts.get("government_footer", "FunFernus • Правительство"))
    return embed


class ClaimModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(title="Подача судебного иска", timeout=600)
        self.bot = bot
        self.store = store
        self.plaintiff_nick = discord.ui.TextInput(label="Ваш Minecraft-ник", max_length=32)
        self.defendant_nick = discord.ui.TextInput(label="Minecraft-ник ответчика", max_length=32)
        self.defendant_discord = discord.ui.TextInput(label="Discord ответчика", max_length=64, placeholder="Упоминание или цифровой ID")
        self.claim = discord.ui.TextInput(label="Подробно опишите ситуацию", style=discord.TextStyle.paragraph, max_length=4000)
        self.demands = discord.ui.TextInput(label="Ваши требования", style=discord.TextStyle.paragraph, max_length=1500)
        for item in (self.plaintiff_nick, self.defendant_nick, self.defendant_discord, self.claim, self.demands):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        defendant_id = _discord_id(str(self.defendant_discord))
        if not defendant_id:
            await interaction.response.send_message("❌ Укажите упоминание или цифровой Discord ID ответчика.", ephemeral=True)
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            await interaction.response.send_message("Бот не инициализирован.", ephemeral=True)
            return
        if str(interaction.user.id) in state.active_drafts:
            await interaction.response.send_message("У вас уже есть незавершённый иск.", ephemeral=True)
            return
        draft_id = secrets.token_hex(6)
        state.active_drafts[str(interaction.user.id)] = draft_id
        state.cases[f"DRAFT:{draft_id}"] = {
            "draft_id": draft_id,
            "status": "collecting",
            "guild_id": interaction.guild.id,
            "plaintiff_id": interaction.user.id,
            "plaintiff_nick": str(self.plaintiff_nick),
            "defendant_id": defendant_id,
            "defendant_nick": str(self.defendant_nick),
            "claim": str(self.claim),
            "demands": str(self.demands),
            "evidence": [],
        }
        await self.store.save(state)
        embed = discord.Embed(
            title="Черновик судебного иска",
            description="Отправляйте боту в личные сообщения изображения, файлы, ссылки или текст. Когда закончите — нажмите **«Отправить иск»**.",
            color=discord.Color.blurple(),
        )
        try:
            await interaction.user.send(embed=embed, view=DraftView(self.bot, self.store, draft_id))
        except discord.HTTPException:
            state.active_drafts.pop(str(interaction.user.id), None)
            state.cases.pop(f"DRAFT:{draft_id}", None)
            await self.store.save(state)
            await interaction.response.send_message("❌ Откройте личные сообщения от участников сервера и повторите попытку.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Черновик создан. Продолжайте в личных сообщениях с ботом.", ephemeral=True)


class DraftView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, draft_id: str = "") -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store
        self.draft_id = draft_id

    @discord.ui.button(label="Отправить иск", emoji="⚖️", style=discord.ButtonStyle.success, custom_id="unified:government:draft:submit")
    async def submit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        draft_id = self.draft_id
        state = None
        guild = None
        for candidate in self.store._states.values():
            active = candidate.active_drafts.get(str(interaction.user.id))
            if active:
                draft_id = active
                state = candidate
                guild = self.bot.get_guild(candidate.guild_id)
                break
        if not draft_id or state is None or guild is None:
            await interaction.response.send_message("Черновик не найден.", ephemeral=True)
            return
        draft = state.cases.get(f"DRAFT:{draft_id}")
        if not draft:
            await interaction.response.send_message("Черновик не найден.", ephemeral=True)
            return
        review = await _channel(self.bot, state.channels.get("government_review", 0))
        if review is None:
            await interaction.response.send_message("Канал рассмотрения суда не настроен.", ephemeral=True)
            return
        case_id = state.next_id("case", "CASE")
        case = dict(draft)
        case.update({"id": case_id, "status": "pending", "review_channel_id": review.id, "created_at": datetime.now(timezone.utc).isoformat()})
        message = await review.send(embed=case_embed(case_id, case, state), view=CaseReviewView(self.bot, self.store, case_id))
        case["review_message_id"] = message.id
        state.cases[case_id] = case
        state.cases.pop(f"DRAFT:{draft_id}", None)
        state.active_drafts.pop(str(interaction.user.id), None)
        await self.store.save(state)
        await interaction.response.edit_message(content=f"✅ Иск отправлен. Номер дела: `{case_id}`.", embed=None, view=None)

    @discord.ui.button(label="Отменить", emoji="✖️", style=discord.ButtonStyle.danger, custom_id="unified:government:draft:cancel")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        for state in self.store._states.values():
            draft_id = state.active_drafts.pop(str(interaction.user.id), None)
            if draft_id:
                state.cases.pop(f"DRAFT:{draft_id}", None)
                await self.store.save(state)
                await interaction.response.edit_message(content="Иск отменён.", embed=None, view=None)
                return
        await interaction.response.send_message("Черновик не найден.", ephemeral=True)


class ScheduleModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, case_id: str) -> None:
        super().__init__(title="Назначить судебное заседание", timeout=600)
        self.bot = bot
        self.store = store
        self.case_id = case_id
        self.date = discord.ui.TextInput(label="Дата", placeholder="25.07.2026", max_length=30)
        self.time = discord.ui.TextInput(label="Время", placeholder="19:00 МСК", max_length=30)
        self.place = discord.ui.TextInput(label="Место", placeholder="Зал судебных заседаний", max_length=200)
        self.comment = discord.ui.TextInput(label="Комментарий", style=discord.TextStyle.paragraph, max_length=1000, required=False)
        for item in (self.date, self.time, self.place, self.comment):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await update_case_status(interaction, self.bot, self.store, self.case_id, "scheduled", schedule={"date": str(self.date), "time": str(self.time), "place": str(self.place), "comment": str(self.comment)})


class RejectModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, case_id: str) -> None:
        super().__init__(title="Отклонить судебный иск", timeout=600)
        self.bot = bot
        self.store = store
        self.case_id = case_id
        self.reason = discord.ui.TextInput(label="Причина", style=discord.TextStyle.paragraph, max_length=2000)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await update_case_status(interaction, self.bot, self.store, self.case_id, "rejected", rejection_reason=str(self.reason))


class ClarificationModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, case_id: str) -> None:
        super().__init__(title="Запросить уточнение", timeout=600)
        self.bot = bot
        self.store = store
        self.case_id = case_id
        self.question = discord.ui.TextInput(label="Что нужно уточнить?", style=discord.TextStyle.paragraph, max_length=2000)
        self.add_item(self.question)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await update_case_status(interaction, self.bot, self.store, self.case_id, "clarification", clarification_question=str(self.question))


async def update_case_status(interaction: discord.Interaction, bot: commands.Bot, store: UnifiedDiscordStore, case_id: str, status: str, **changes) -> None:
    if interaction.guild is None:
        return
    state = store.get(interaction.guild.id)
    if state is None:
        await interaction.response.send_message("Состояние не загружено.", ephemeral=True)
        return
    case = state.cases.get(case_id)
    if not case:
        await interaction.response.send_message("Дело не найдено.", ephemeral=True)
        return
    case["status"] = status
    case.update(changes)
    await store.save(state)
    if interaction.message:
        view = CaseReviewView(bot, store, case_id) if status in {"pending", "clarification"} else None
        await interaction.response.edit_message(embed=case_embed(case_id, case, state), view=view)
    else:
        await interaction.response.send_message("✅ Изменения сохранены.", ephemeral=True)
    user = bot.get_user(int(case.get("plaintiff_id", 0)))
    if user:
        try:
            if status == "clarification":
                await user.send(f"По делу `{case_id}` судья запросил уточнение:\n{changes.get('clarification_question')}\n\nОтветьте на это сообщение текстом или файлами.")
            elif status == "scheduled":
                schedule = changes.get("schedule", {})
                await user.send(f"По делу `{case_id}` назначен суд: {schedule.get('date')} в {schedule.get('time')}, место: {schedule.get('place')}.")
            elif status == "rejected":
                await user.send(f"Иск `{case_id}` отклонён. Причина: {changes.get('rejection_reason')}")
        except discord.HTTPException:
            pass


class CaseReviewView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore, case_id: str = "") -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store
        self.case_id = case_id

    async def guard(self, interaction: discord.Interaction) -> tuple[UnifiedState | None, str]:
        if interaction.guild is None:
            return None, ""
        state = self.store.get(interaction.guild.id)
        if state is None or not _is_judge(interaction.user, state, getattr(self.bot, "admin_user_ids", set())):
            await interaction.response.send_message("❌ Нет доступа судьи.", ephemeral=True)
            return None, ""
        return state, self.case_id or _case_id(interaction.message)

    @discord.ui.button(label="Назначить суд", emoji="📅", style=discord.ButtonStyle.success, custom_id="unified:government:schedule")
    async def schedule(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state, case_id = await self.guard(interaction)
        if state:
            await interaction.response.send_modal(ScheduleModal(self.bot, self.store, case_id))

    @discord.ui.button(label="Запросить уточнение", emoji="❓", style=discord.ButtonStyle.primary, custom_id="unified:government:clarify")
    async def clarify(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state, case_id = await self.guard(interaction)
        if state:
            await interaction.response.send_modal(ClarificationModal(self.bot, self.store, case_id))

    @discord.ui.button(label="Отклонить", emoji="✖️", style=discord.ButtonStyle.danger, custom_id="unified:government:reject")
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state, case_id = await self.guard(interaction)
        if state:
            await interaction.response.send_modal(RejectModal(self.bot, self.store, case_id))


class ClaimPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store

    @discord.ui.button(label="Подать иск", emoji="⚖️", style=discord.ButtonStyle.primary, custom_id="unified:government:claim")
    async def claim(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ClaimModal(self.bot, self.store))


async def publish_government_panel(bot: commands.Bot, store: UnifiedDiscordStore, guild: discord.Guild, state: UnifiedState) -> tuple[bool, str]:
    channel = await _channel(bot, state.channels.get("government_panel", 0))
    review = await _channel(bot, state.channels.get("government_review", 0))
    if channel is None or review is None:
        return False, "Сначала выберите канал подачи и канал рассмотрения."
    embed = discord.Embed(
        title=state.texts.get("government_title", "Подача судебного иска"),
        description=state.texts.get("government_description", "Нажмите кнопку ниже."),
        color=int(state.options.get("accent_color", 0x19B9D1)),
    )
    asset = state.asset("government_panel")
    if asset.url:
        embed.set_image(url=asset.url)
    embed.add_field(name="Подготовьте", value="• Minecraft-ник ответчика\n• Discord ответчика\n• описание ситуации\n• требования\n• доказательства", inline=False)
    embed.set_footer(text=state.texts.get("government_footer", "FunFernus • Правительство"))
    old = state.messages.get("government_panel", 0)
    if old:
        try:
            message = await channel.fetch_message(old)
            await message.edit(embed=embed, view=ClaimPanelView(bot, store))
            return True, "Панель правительства обновлена."
        except discord.HTTPException:
            pass
    message = await channel.send(embed=embed, view=ClaimPanelView(bot, store))
    state.messages["government_panel"] = message.id
    await store.save(state)
    return True, "Панель правительства опубликована."


class GovernmentBannerModal(discord.ui.Modal):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(title="Баннер правительства", timeout=600)
        self.bot = bot
        self.store = store
        self.file_label = discord.ui.Label(
            text="Файл баннера",
            description="PNG, JPG, JPEG, WEBP или GIF, до 10 МБ.",
            component=discord.ui.FileUpload(custom_id="government_banner", required=True, min_values=1, max_values=1),
        )
        self.add_item(self.file_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        image = _banner_from_label(self.file_label)
        if image is None:
            await interaction.response.send_message("Файл не выбран.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = self.store.get(interaction.guild.id)
        if state is None:
            return
        await self.store.replace_asset(interaction.guild, state, "government_panel", image, "Панель правительства")
        await publish_government_panel(self.bot, self.store, interaction.guild, state)
        await interaction.followup.send("✅ Баннер сохранён и панель обновлена.", ephemeral=True)


class GovernmentChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, store: UnifiedDiscordStore, key: str, placeholder: str) -> None:
        super().__init__(placeholder=placeholder, channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.store = store
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            return
        state.channels[self.key] = self.values[0].id
        await self.store.save(state)
        await interaction.response.send_message(f"✅ Выбран канал: {self.values[0].mention}", ephemeral=True)


class JudgeRoleSelect(discord.ui.RoleSelect):
    def __init__(self, store: UnifiedDiscordStore) -> None:
        super().__init__(placeholder="Выберите роли судей", min_values=0, max_values=25)
        self.store = store

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        state = self.store.get(interaction.guild.id)
        if state is None:
            return
        state.roles["government_judges"] = [role.id for role in self.values]
        await self.store.save(state)
        await interaction.response.send_message("✅ Роли судей сохранены.", ephemeral=True)


class GovernmentSetupView(discord.ui.View):
    def __init__(self, bot: commands.Bot, store: UnifiedDiscordStore) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.store = store

    @discord.ui.button(label="Каналы", emoji="📍", style=discord.ButtonStyle.secondary)
    async def channels(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = discord.ui.View(timeout=600)
        view.add_item(GovernmentChannelSelect(self.store, "government_panel", "Канал подачи исков"))
        view.add_item(GovernmentChannelSelect(self.store, "government_review", "Канал рассмотрения"))
        view.add_item(GovernmentChannelSelect(self.store, "government_logs", "Канал логов"))
        await interaction.response.send_message("Выберите каналы по очереди:", view=view, ephemeral=True)

    @discord.ui.button(label="Роли судей", emoji="⚖️", style=discord.ButtonStyle.secondary)
    async def roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = discord.ui.View(timeout=600)
        view.add_item(JudgeRoleSelect(self.store))
        await interaction.response.send_message("Выберите роли судей:", view=view, ephemeral=True)

    @discord.ui.button(label="Загрузить баннер", emoji="🖼️", style=discord.ButtonStyle.primary)
    async def banner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(GovernmentBannerModal(self.bot, self.store))

    @discord.ui.button(label="Опубликовать панель", emoji="🚀", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = self.store.get(interaction.guild.id)
        if state is None:
            return
        ok, text = await publish_government_panel(self.bot, self.store, interaction.guild, state)
        await interaction.followup.send(("✅ " if ok else "❌ ") + text, ephemeral=True)


async def setup_government(bot: commands.Bot, store: UnifiedDiscordStore, admin_ids: set[int]) -> None:
    bot.add_view(ClaimPanelView(bot, store))
    bot.add_view(DraftView(bot, store))
    bot.add_view(CaseReviewView(bot, store))

    @bot.tree.command(name="настроить_правительство", description="Настроить судебные иски и панель правительства")
    @app_commands.guild_only()
    async def government_setup(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _is_admin(interaction, admin_ids):
            await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
            return
        state = store.get(interaction.guild.id) or await store.load_or_create(interaction.guild)
        embed = discord.Embed(
            title="⚙️ Настройка правительства",
            description="Выберите каналы и роли судей, загрузите баннер файлом, затем опубликуйте панель.",
            color=int(state.options.get("accent_color", 0x19B9D1)),
        )
        await interaction.response.send_message(embed=embed, view=GovernmentSetupView(bot, store), ephemeral=True)

    @bot.listen("on_message")
    async def government_dm_listener(message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        for state in list(store._states.values()):
            draft_id = state.active_drafts.get(str(message.author.id))
            if draft_id:
                draft = state.cases.get(f"DRAFT:{draft_id}")
                if not draft:
                    continue
                items = draft.setdefault("evidence", [])
                if message.content.strip():
                    items.append({"type": "text", "text": message.content.strip()})
                for attachment in message.attachments:
                    items.append({"type": "file", "name": attachment.filename, "url": attachment.url})
                await store.save(state)
                await message.reply(f"Материалы добавлены. Всего: **{len(items)}**. Когда закончите, нажмите **«Отправить иск»**.")
                return
            for case_id, case in state.cases.items():
                if not case_id.startswith("CASE-") or case.get("status") != "clarification" or int(case.get("plaintiff_id", 0)) != message.author.id:
                    continue
                parts = [message.content.strip()] if message.content.strip() else []
                parts.extend(f"{a.filename}: {a.url}" for a in message.attachments)
                if not parts:
                    return
                case.setdefault("clarifications", []).append("\n".join(parts))
                case["status"] = "pending"
                await store.save(state)
                guild = bot.get_guild(state.guild_id)
                channel = await _channel(bot, int(case.get("review_channel_id", 0)))
                if guild and channel:
                    try:
                        review_message = await channel.fetch_message(int(case.get("review_message_id", 0)))
                        await review_message.edit(embed=case_embed(case_id, case, state), view=CaseReviewView(bot, store, case_id))
                    except discord.HTTPException:
                        pass
                await message.reply(f"Уточнение по делу `{case_id}` передано судье.")
                return
