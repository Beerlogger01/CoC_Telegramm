import asyncio
import logging
from typing import Any

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from app.backend_client import fetch_json
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def format_clan(payload: dict[str, Any]) -> str:
    return (
        f"*{payload.get('name', 'Clan')}*\n"
        f"Tag: `{payload.get('tag', 'N/A')}`\n"
        f"Level: {payload.get('clanLevel', 'N/A')}\n"
        f"Members: {payload.get('members', 'N/A')}\n"
        f"War League: {payload.get('warLeague', {}).get('name', 'N/A')}\n"
    )


def format_player(payload: dict[str, Any]) -> str:
    return (
        f"*{payload.get('name', 'Player')}*\n"
        f"Tag: `{payload.get('tag', 'N/A')}`\n"
        f"Town Hall: {payload.get('townHallLevel', 'N/A')}\n"
        f"Trophies: {payload.get('trophies', 'N/A')}\n"
        f"Best Trophies: {payload.get('bestTrophies', 'N/A')}\n"
        f"Clan: {payload.get('clan', {}).get('name', 'No clan')}\n"
    )


def format_war(payload: dict[str, Any]) -> str:
    return (
        f"*Current War*\n"
        f"State: {payload.get('state', 'N/A')}\n"
        f"Team Size: {payload.get('teamSize', 'N/A')}\n"
        f"Start: {payload.get('startTime', 'N/A')}\n"
        f"End: {payload.get('endTime', 'N/A')}\n"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Welcome! Use /clan, /player <tag>, or /war to get Clash of Clans info."
        )


async def clan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/clan")
            await update.message.reply_text(format_clan(payload), parse_mode=ParseMode.MARKDOWN)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error: %s", exc)
            if status == 429:
                message = "Rate limit reached. Please try again later."
            elif status == 400:
                message = "Invalid clan tag configured."
            elif status == 504:
                message = "Backend timed out contacting Clash of Clans."
            else:
                message = "Backend error while fetching clan data."
            await update.message.reply_text(message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")


async def player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /player <tag>")
        return
    tag = context.args[0]
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, f"/player/{tag}")
            await update.message.reply_text(format_player(payload), parse_mode=ParseMode.MARKDOWN)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error: %s", exc)
            if status == 400:
                message = "Invalid player tag format."
            elif status == 404:
                message = "Player not found."
            elif status == 429:
                message = "Rate limit reached. Please try again later."
            elif status == 504:
                message = "Backend timed out contacting Clash of Clans."
            else:
                message = "Backend error while fetching player data."
            await update.message.reply_text(message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")


async def war(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            payload = await fetch_json(client, "/war")
            await update.message.reply_text(format_war(payload), parse_mode=ParseMode.MARKDOWN)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Backend error: %s", exc)
            if status == 429:
                message = "Rate limit reached. Please try again later."
            elif status == 400:
                message = "Invalid clan tag configured."
            elif status == 504:
                message = "Backend timed out contacting Clash of Clans."
            else:
                message = "Backend error while fetching war data."
            await update.message.reply_text(message)
        except httpx.RequestError as exc:
            logger.warning("Backend unreachable: %s", exc)
            await update.message.reply_text("Backend is unreachable. Please try again later.")


async def main() -> None:
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clan", clan))
    application.add_handler(CommandHandler("player", player))
    application.add_handler(CommandHandler("war", war))

    logger.info("Telegram bot starting")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Telegram bot shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
