from __future__ import annotations

import discord


def parse_color(raw: str, fallback: int = 0x19B9D1) -> int:
    value = (raw or "").strip().lstrip("#")
    if len(value) == 6:
        try:
            return int(value, 16)
        except ValueError:
            pass
    return fallback


def _chunks(value: str, limit: int = 3900) -> list[str]:
    value = value.strip()
    if not value:
        return []
    result: list[str] = []
    while len(value) > limit:
        cut = value.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = value.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        result.append(value[:cut].rstrip())
        value = value[cut:].lstrip()
    if value:
        result.append(value)
    return result


def build_framed_container(
    *,
    title: str,
    body: str,
    banner_url: str = "",
    color: int = 0x19B9D1,
    footer: str = "",
    action_row: discord.ui.ActionRow | None = None,
) -> discord.ui.Container:
    """Собрать одну Components V2-рамку с крупным баннером сверху."""

    container = discord.ui.Container(accent_color=color)

    if banner_url:
        gallery = discord.ui.MediaGallery()
        gallery.add_item(
            media=banner_url,
            description=title.strip() or "FunFernus",
        )
        container.add_item(gallery)

    if title:
        container.add_item(discord.ui.TextDisplay(f"# {title.strip()}"))

    for part in _chunks(body):
        container.add_item(discord.ui.TextDisplay(part))

    if footer:
        container.add_item(discord.ui.TextDisplay(f"-# {footer.strip()}"))

    if not title and not body and not footer:
        container.add_item(discord.ui.TextDisplay("\u200b"))

    if action_row is not None:
        container.add_item(discord.ui.Separator())
        container.add_item(action_row)

    return container


def build_framed_view(
    *,
    title: str,
    body: str,
    banner_url: str = "",
    color: int = 0x19B9D1,
    footer: str = "",
    action_row: discord.ui.ActionRow | None = None,
    timeout: float | None = None,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    view.add_item(
        build_framed_container(
            title=title,
            body=body,
            banner_url=banner_url,
            color=color,
            footer=footer,
            action_row=action_row,
        )
    )
    return view
