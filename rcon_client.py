from __future__ import annotations

import asyncio
import logging
import socket
import struct


RCON_TYPE_COMMAND = 2
RCON_TYPE_AUTH = 3

log = logging.getLogger("funfernus-rcon")


class RCONError(Exception):
    """Понятная ошибка Minecraft RCON."""


def build_rcon_packet(
    request_id: int,
    packet_type: int,
    payload: str,
) -> bytes:
    body = (
        struct.pack("<ii", request_id, packet_type)
        + payload.encode("utf-8")
        + b"\x00\x00"
    )
    return struct.pack("<i", len(body)) + body


async def read_exactly_with_timeout(
    reader: asyncio.StreamReader,
    size: int,
    timeout: float = 10.0,
) -> bytes:
    try:
        return await asyncio.wait_for(
            reader.readexactly(size),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise RCONError("RCON-сервер не ответил вовремя.") from exc
    except asyncio.IncompleteReadError as exc:
        raise RCONError("RCON-соединение неожиданно закрылось.") from exc


async def read_rcon_packet(
    reader: asyncio.StreamReader,
) -> tuple[int, int, str]:
    length_raw = await read_exactly_with_timeout(reader, 4)
    packet_length = struct.unpack("<i", length_raw)[0]

    if packet_length < 10 or packet_length > 4_194_304:
        raise RCONError(
            f"Получен некорректный размер RCON-пакета: {packet_length}."
        )

    body = await read_exactly_with_timeout(reader, packet_length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    payload = body[8:-2].decode("utf-8", errors="replace")
    return request_id, packet_type, payload


async def open_rcon_connection(
    host: str,
    port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            log.info(
                "RCON: попытка подключения %s/3 к %s:%s",
                attempt,
                host,
                port,
            )
            return await asyncio.wait_for(
                asyncio.open_connection(
                    host=host,
                    port=port,
                    family=socket.AF_INET,
                ),
                timeout=10,
            )
        except Exception as exc:
            last_error = exc
            log.warning(
                "RCON: попытка %s/3 неудачна: %s: %r",
                attempt,
                type(exc).__name__,
                exc,
            )
            if attempt < 3:
                await asyncio.sleep(2)

    error_name = (
        type(last_error).__name__
        if last_error is not None
        else "UnknownError"
    )
    raise RCONError(
        "Не удалось подключиться к RCON после 3 попыток.\n"
        f"Адрес: {host}:{port}\n"
        f"Ошибка: {error_name}: {last_error!r}"
    )


async def execute_rcon_command(
    host: str,
    port: int,
    password: str,
    command: str,
) -> str:
    if not host:
        raise RCONError("Не указан RCON_HOST.")
    if not password:
        raise RCONError("Не указан RCON_PASSWORD.")

    reader, writer = await open_rcon_connection(host, port)

    try:
        auth_request_id = 1001
        writer.write(
            build_rcon_packet(
                auth_request_id,
                RCON_TYPE_AUTH,
                password,
            )
        )
        await writer.drain()

        authenticated = False
        for _ in range(2):
            response_id, _, _ = await read_rcon_packet(reader)
            if response_id == -1:
                raise RCONError("RCON отклонил пароль.")
            if response_id == auth_request_id:
                authenticated = True
                break

        if not authenticated:
            raise RCONError(
                "RCON вернул неожиданный ответ на авторизацию."
            )

        command_request_id = 1002
        writer.write(
            build_rcon_packet(
                command_request_id,
                RCON_TYPE_COMMAND,
                command,
            )
        )
        await writer.drain()

        response_id, _, response_text = await read_rcon_packet(reader)
        if response_id == -1:
            raise RCONError("RCON отклонил команду.")
        if response_id != command_request_id:
            raise RCONError("RCON вернул неожиданный ID ответа.")

        return response_text.strip() or "Команда выполнена."
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
