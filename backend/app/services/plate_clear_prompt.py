"""The "is the plate clear?" question as the last message of a finished print.

The plate-clear gate goes up the moment a print ends, well before the finish
photo is ready and the print-complete and outcome notifications are out. So the
rising edge only arms the question here; the completion task sends it once its
own notifications are done, and a timer sends it anyway if that task never gets
there (a print ending with no name, an internal printer job).

It does not wait for the outcome verdict: the two messages are independent, and
either can be answered first or not at all.

On Telegram the message can carry a callback button (``plate_clear_button``),
answered through the existing getUpdates poller so it needs no external_url.
Sent buttons live in ``plate_clear_prompts`` and survive a restart. An armed
question does not: it covers seconds to two minutes, and the gate itself is
persisted, so the plate can still be released from the UI.
"""

import asyncio
import json
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.notification import NotificationProvider, PlateClearPrompt
from backend.app.models.printer import Printer
from backend.app.services.notification_service import notification_service, telegram_markdown_escape
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.local_time import utcnow_naive

logger = logging.getLogger(__name__)

# How long after the gate went up the question goes out regardless. Above the
# longest finish-photo wait in on_print_complete (95 seconds).
SEND_FALLBACK_SECONDS = 120


async def release_plate(db: AsyncSession, printer_id: int) -> None:
    """Release the gate, as the clear-plate route and the Telegram button both do."""
    printer_manager.set_awaiting_plate_clear(printer_id, False)

    # #1898: releasing the plate without answering the outcome prompt can
    # count as "good" (opt-in setting) — this is the moment the operator
    # moves on, so an unanswered prompt would otherwise linger unconfirmed.
    from backend.app.api.routes.settings import get_setting, setting_is_true

    # setting_is_true rather than a comparison of our own: one reader deciding
    # for itself what "on" spells is how two parts of the app end up
    # disagreeing about the same row.
    if setting_is_true(await get_setting(db, "confirm_default_good_on_plate_clear")):
        from backend.app.services.print_confirmation import resolve_pending_confirmation_as_good

        resolved = await resolve_pending_confirmation_as_good(db, printer_id)
        if resolved is not None:
            await db.commit()


def _provider_config(provider: NotificationProvider) -> dict:
    return json.loads(provider.config) if isinstance(provider.config, str) else (provider.config or {})


def _chat_is_configured(provider: NotificationProvider, chat_id: str) -> bool:
    """Whether a chat is the one the provider sends to.

    A provider can name a public chat as ``@username``, while updates always
    carry its numeric id. That form can't be compared, so it passes here and
    the prompt row's chat id, recorded from Telegram's reply, is the check.
    """
    configured = str(_provider_config(provider).get("chat_id") or "").strip()
    return configured == chat_id or configured.startswith("@")


class PlateClearPrompts:
    def __init__(self):
        self._armed: set[int] = set()
        self._fallbacks: dict[int, asyncio.Task] = {}
        self._session_factory = None

    def _sessions(self):
        if self._session_factory is not None:
            return self._session_factory
        from backend.app.core.database import async_session

        return async_session

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def arm(self, printer_id: int) -> None:
        """The gate just went up: the next end of notifications sends the question.

        Only the rising edge arms, so a print ending while the gate is still up
        asks nothing new, as it always has.
        """
        self._armed.add(printer_id)

    def disarm(self, printer_id: int) -> None:
        self._armed.discard(printer_id)
        fallback = self._fallbacks.pop(printer_id, None)
        if fallback is not None and fallback is not asyncio.current_task():
            fallback.cancel()

    def start_fallback(self, printer_id: int) -> None:
        if printer_id in self._armed and printer_id not in self._fallbacks:
            self._fallbacks[printer_id] = asyncio.create_task(
                self._send_after_fallback(printer_id), name=f"plate-clear-fallback-{printer_id}"
            )

    async def _send_after_fallback(self, printer_id: int) -> None:
        await asyncio.sleep(SEND_FALLBACK_SECONDS)
        await self.send_after_print_end(printer_id)

    async def send_after_print_end(self, printer_id: int) -> None:
        """Send the question if one is armed. Never raises: the caller is a print's completion."""
        if printer_id not in self._armed:
            return
        self.disarm(printer_id)
        if not printer_manager.is_awaiting_plate_clear(printer_id):
            return

        try:
            from backend.app.api.routes.settings import get_setting, setting_is_true

            async with self._sessions()() as db:
                # With the setting off the queue does not wait for anybody, and
                # the question would claim that it does.
                if not setting_is_true(await get_setting(db, "require_plate_clear")):
                    return
                printer_name = await db.scalar(select(Printer.name).where(Printer.id == printer_id))
                if printer_name is None:
                    return
                await notification_service.on_plate_clear_required(printer_id, printer_name, db)

            # Released while the message was on its way: the gate's own falling
            # edge may have looked for this row before it existed.
            if not printer_manager.is_awaiting_plate_clear(printer_id):
                await self.retire(printer_id)
        except Exception as e:
            logger.warning("Failed to send the plate-clear question for printer %d: %s", printer_id, e)

    # ------------------------------------------------------------------
    # Retiring sent buttons
    # ------------------------------------------------------------------

    async def retire(self, printer_id: int) -> None:
        """The gate came down, whichever way: forget the question and take its buttons away."""
        self.disarm(printer_id)
        async with self._sessions()() as db:
            prompts = list(
                (await db.execute(select(PlateClearPrompt).where(PlateClearPrompt.printer_id == printer_id)))
                .scalars()
                .all()
            )
            if not prompts:
                return
            printer_name = await db.scalar(select(Printer.name).where(Printer.id == printer_id))
            _, line = await notification_service._build_message_from_template(
                db, "plate_clear_done", {"printer": printer_name or ""}
            )
            await self._delete_and_edit(db, prompts, line)

    async def expire_stale(self) -> None:
        """Take away buttons past their expiry. The gate stays as it is."""
        async with self._sessions()() as db:
            prompts = list(
                (await db.execute(select(PlateClearPrompt).where(PlateClearPrompt.expires_at <= utcnow_naive())))
                .scalars()
                .all()
            )
            if not prompts:
                return
            _, line = await notification_service._build_message_from_template(db, "plate_clear_expired", {})
            await self._delete_and_edit(db, prompts, line)

    async def _delete_and_edit(self, db: AsyncSession, prompts: list[PlateClearPrompt], line: str) -> None:
        """Delete first, so no transaction stays open across the network and a
        failed edit cannot bring a spent button back."""
        edits = []
        for prompt in prompts:
            provider = await db.get(NotificationProvider, prompt.provider_id)
            token = str(_provider_config(provider).get("bot_token") or "").strip() if provider else ""
            if token:
                edits.append((token, prompt.chat_id, prompt.message_id, f"{prompt.message_text}\n\n{line}"))
        await db.execute(delete(PlateClearPrompt).where(PlateClearPrompt.id.in_([p.id for p in prompts])))
        await db.commit()
        for token, chat_id, message_id, text in edits:
            await self._edit_message(token, chat_id, message_id, text)

    async def _edit_message(self, bot_token: str, chat_id: str, message_id: int, text: str | None) -> None:
        """Replace the text, or with None only drop the keyboard. Editing without
        ``reply_markup`` is what removes the button. Best effort."""
        if text is None:
            method = "editMessageReplyMarkup"
            payload = {"chat_id": chat_id, "message_id": message_id}
        else:
            method = "editMessageText"
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": telegram_markdown_escape(text),
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
        try:
            client = await notification_service._get_client()
            response = await client.post(f"https://api.telegram.org/bot{bot_token}/{method}", json=payload)
            if response.status_code != 200:
                logger.warning("Telegram %s failed for the plate-clear message: HTTP %s", method, response.status_code)
        except Exception as e:
            # httpx errors can carry the request URL, which holds the bot token.
            logger.warning("Telegram %s failed for the plate-clear message: %s", method, type(e).__name__)

    # ------------------------------------------------------------------
    # The button
    # ------------------------------------------------------------------

    async def handle_tap(
        self, bot_token: str, provider_ids: set[int], token: str, chat_id: str, message_id: int | None
    ) -> str:
        """Act on a tap and return the text for answerCallbackQuery.

        There is no Bambuddy user behind a tap, so PRINTERS_CLEAR_PLATE cannot be
        asked. As with the outcome reactions, being in the provider's chat is the
        authorisation, on top of a token that is random, single-use and expiring.
        A chat that fails the check learns nothing beyond "not valid".
        """
        async with self._sessions()() as db:
            not_valid, _ = await notification_service._build_message_from_template(db, "plate_clear_expired", {})
            prompt = await db.scalar(select(PlateClearPrompt).where(PlateClearPrompt.token == token))

            if prompt is None:
                # Used or expired, and the edit that should have removed the
                # button failed. Only this bot's own configured chats get the cleanup.
                providers = (
                    await db.execute(select(NotificationProvider).where(NotificationProvider.id.in_(provider_ids)))
                ).scalars()
                own_chat = any(_chat_is_configured(p, chat_id) for p in providers)
                if own_chat and message_id is not None:
                    await self._edit_message(bot_token, chat_id, message_id, None)
                return not_valid

            provider = await db.get(NotificationProvider, prompt.provider_id)
            if (
                provider is None
                or prompt.provider_id not in provider_ids
                or prompt.chat_id != chat_id
                or prompt.message_id != message_id
                or not _chat_is_configured(provider, chat_id)
            ):
                logger.warning("Plate-clear button tapped from unexpected chat %s: ignored", chat_id)
                return not_valid

            if prompt.expires_at <= utcnow_naive():
                await self.expire_stale()
                return not_valid

            printer_id = prompt.printer_id
            if not printer_manager.is_awaiting_plate_clear(printer_id):
                printer_name = await db.scalar(select(Printer.name).where(Printer.id == printer_id))
                _, toast = await notification_service._build_message_from_template(
                    db, "plate_clear_button", {"printer": printer_name or ""}
                )
            else:
                await release_plate(db, printer_id)
                toast, _ = await notification_service._build_message_from_template(db, "plate_clear_done", {})
                logger.info("Plate cleared for printer %d from Telegram chat %s", printer_id, chat_id)

        await self.retire(printer_id)
        return toast


plate_clear_prompts = PlateClearPrompts()
