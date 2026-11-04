"""The plate-clear question as the last message of a print, with its Telegram button.

The Bot API is stood in for at the httpx layer and the database is the real test
engine. The services open their own sessions, so they get the test session factory.
"""

import asyncio
import inspect
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.notification import PlateClearPrompt
from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate
from backend.app.models.settings import Settings
from backend.app.services import plate_clear_prompt as module
from backend.app.services.notification_service import notification_service
from backend.app.services.plate_clear_prompt import plate_clear_prompts
from backend.app.services.printer_manager import printer_manager
from backend.app.services.telegram_reactions import TelegramReactionPoller, _provider_reaction_token
from backend.app.utils.local_time import utcnow_naive

BOT_TOKEN = "123456:AAbbCC"
CHAT_ID = "-1002520100736"
OTHER_CHAT_ID = "-1009999999999"
TELEGRAM_CONFIG = {"bot_token": BOT_TOKEN, "chat_id": CHAT_ID}


class _FakeBotApi:
    """Answers every call as Telegram would, handing out rising message ids."""

    def __init__(self):
        self.is_closed = False
        self.calls: list[dict] = []
        self.updates: list[dict] = []
        self._next_message_id = 500

    async def post(self, url, json=None, data=None, files=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append({"method": method, "json": json})
        if method == "getUpdates":
            updates, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": updates})
        if method == "sendMessage":
            self._next_message_id += 1
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"message_id": self._next_message_id, "chat": {"id": int(json["chat_id"])}},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": True})

    async def aclose(self):
        self.is_closed = True

    def of(self, method: str) -> list[dict]:
        return [c["json"] for c in self.calls if c["method"] == method]


@pytest.fixture
async def bot(test_engine, db_session):
    """Wire the services to the test database and a fake Bot API, with the gate setting on."""
    db_session.add(Settings(key="require_plate_clear", value="true"))
    for template in DEFAULT_TEMPLATES:
        if template["event_type"].startswith("plate_clear"):
            db_session.add(NotificationTemplate(**template, is_default=True))
    await db_session.commit()
    notification_service.clear_template_cache()

    api = _FakeBotApi()
    previous = plate_clear_prompts._session_factory
    plate_clear_prompts._session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(notification_service, "_get_client", AsyncMock(return_value=api)):
        yield api
    plate_clear_prompts._session_factory = previous
    notification_service.clear_template_cache()
    for printer_id in list(plate_clear_prompts._armed):
        plate_clear_prompts.disarm(printer_id)
    printer_manager._awaiting_plate_clear.clear()


@pytest.fixture
def poller(test_engine, bot):
    p = TelegramReactionPoller()
    p._session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    p._http_client = bot
    return p


@pytest.fixture
def telegram_provider(notification_provider_factory):
    async def _create(**kwargs):
        defaults = {
            "name": "Telegram",
            "provider_type": "telegram",
            "config": TELEGRAM_CONFIG,
            "on_plate_clear_required": True,
            "plate_clear_button": True,
        }
        defaults.update(kwargs)
        return await notification_provider_factory(**defaults)

    return _create


def _raise_gate(printer_id: int) -> None:
    printer_manager._awaiting_plate_clear.add(printer_id)
    plate_clear_prompts.arm(printer_id)


async def _prompts(db_session, printer_id: int | None = None) -> list[PlateClearPrompt]:
    query = select(PlateClearPrompt).execution_options(populate_existing=True)
    if printer_id is not None:
        query = query.where(PlateClearPrompt.printer_id == printer_id)
    return list((await db_session.execute(query)).scalars().all())


def _tap(update_id: int, token: str, message_id: int, chat_id: str = CHAT_ID, data: str | None = None) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"query-{update_id}",
            "from": {"id": 42},
            "data": data if data is not None else f"plate_clear:{token}",
            "message": {"message_id": message_id, "chat": {"id": int(chat_id)}},
        },
    }


async def _sent_button(db_session, printer, provider, bot) -> PlateClearPrompt:
    _raise_gate(printer.id)
    await plate_clear_prompts.send_after_print_end(printer.id)
    rows = await _prompts(db_session, printer.id)
    assert [r.provider_id for r in rows] == [provider.id]
    return rows[0]


class TestWhenTheQuestionGoesOut:
    @pytest.mark.asyncio
    async def test_goes_out_once_with_a_callback_button(self, bot, db_session, printer_factory, telegram_provider):
        printer = await printer_factory(name="Workshop")
        provider = await telegram_provider()
        _raise_gate(printer.id)

        await plate_clear_prompts.send_after_print_end(printer.id)
        await plate_clear_prompts.send_after_print_end(printer.id)

        sends = bot.of("sendMessage")
        assert len(sends) == 1
        assert "Workshop" in sends[0]["text"]
        button = sends[0]["reply_markup"]["inline_keyboard"][0][0]
        assert "url" not in button
        rows = await _prompts(db_session, printer.id)
        assert [r.provider_id for r in rows] == [provider.id]
        assert button["callback_data"] == f"plate_clear:{rows[0].token}"
        assert len(button["callback_data"].encode()) <= 64
        assert rows[0].chat_id == CHAT_ID
        assert rows[0].message_id == 501

    @pytest.mark.asyncio
    async def test_not_armed_asks_nothing(self, bot, printer_factory, telegram_provider):
        """A print ending while the gate is still up raised no edge: no new question."""
        printer = await printer_factory()
        await telegram_provider()
        printer_manager._awaiting_plate_clear.add(printer.id)

        await plate_clear_prompts.send_after_print_end(printer.id)

        assert bot.of("sendMessage") == []

    @pytest.mark.asyncio
    async def test_released_before_sending_is_cancelled(self, bot, db_session, printer_factory, telegram_provider):
        printer = await printer_factory()
        await telegram_provider()
        _raise_gate(printer.id)

        printer_manager._awaiting_plate_clear.discard(printer.id)
        plate_clear_prompts.disarm(printer.id)
        await plate_clear_prompts.send_after_print_end(printer.id)

        assert bot.of("sendMessage") == []
        assert await _prompts(db_session) == []

    @pytest.mark.asyncio
    async def test_released_while_sending_loses_its_button(self, bot, db_session, printer_factory, telegram_provider):
        printer = await printer_factory()
        await telegram_provider()
        _raise_gate(printer.id)
        real_send = notification_service.on_plate_clear_required

        async def send_then_release(*args):
            await real_send(*args)
            printer_manager._awaiting_plate_clear.discard(printer.id)

        with patch.object(notification_service, "on_plate_clear_required", send_then_release):
            await plate_clear_prompts.send_after_print_end(printer.id)

        assert len(bot.of("editMessageText")) == 1
        assert await _prompts(db_session) == []

    @pytest.mark.asyncio
    async def test_setting_off_sends_nothing(self, bot, db_session, printer_factory, telegram_provider):
        printer = await printer_factory()
        await telegram_provider()
        row = await db_session.scalar(select(Settings).where(Settings.key == "require_plate_clear"))
        row.value = "false"
        await db_session.commit()
        _raise_gate(printer.id)

        await plate_clear_prompts.send_after_print_end(printer.id)

        assert bot.of("sendMessage") == []

    @pytest.mark.asyncio
    async def test_no_button_when_turned_off(self, bot, db_session, printer_factory, telegram_provider):
        printer = await printer_factory()
        await telegram_provider(plate_clear_button=False)
        _raise_gate(printer.id)

        await plate_clear_prompts.send_after_print_end(printer.id)

        sends = bot.of("sendMessage")
        assert len(sends) == 1
        assert "reply_markup" not in sends[0]
        assert await _prompts(db_session) == []

    @pytest.mark.asyncio
    async def test_other_provider_types_get_it_without_a_row(
        self, bot, db_session, printer_factory, notification_provider_factory
    ):
        printer = await printer_factory()
        await notification_provider_factory(on_plate_clear_required=True, plate_clear_button=True)
        _raise_gate(printer.id)

        with patch.object(notification_service, "_send_ntfy", AsyncMock(return_value=(True, "ok"))) as send_ntfy:
            await plate_clear_prompts.send_after_print_end(printer.id)

        assert send_ntfy.await_count == 1
        assert await _prompts(db_session) == []

    @pytest.mark.asyncio
    async def test_quiet_hours_skip_it_like_any_other(self, bot, db_session, printer_factory, telegram_provider):
        printer = await printer_factory()
        await telegram_provider()
        _raise_gate(printer.id)

        with patch.object(notification_service, "_is_in_quiet_hours", return_value=True):
            await plate_clear_prompts.send_after_print_end(printer.id)

        assert bot.of("sendMessage") == []
        assert await _prompts(db_session) == []

    @pytest.mark.asyncio
    async def test_fallback_sends_when_the_completion_task_never_does(self, bot, printer_factory, telegram_provider):
        printer = await printer_factory()
        await telegram_provider()
        _raise_gate(printer.id)

        with patch.object(module, "SEND_FALLBACK_SECONDS", 0):
            plate_clear_prompts.start_fallback(printer.id)
            await plate_clear_prompts._fallbacks[printer.id]

        assert len(bot.of("sendMessage")) == 1

    @pytest.mark.asyncio
    async def test_completion_cancels_the_fallback(self, bot, printer_factory, telegram_provider):
        printer = await printer_factory()
        await telegram_provider()
        _raise_gate(printer.id)
        plate_clear_prompts.start_fallback(printer.id)
        fallback = plate_clear_prompts._fallbacks[printer.id]

        await plate_clear_prompts.send_after_print_end(printer.id)
        await asyncio.gather(fallback, return_exceptions=True)

        assert fallback.cancelled()
        assert len(bot.of("sendMessage")) == 1

    def test_fallback_outlasts_the_longest_finish_photo_wait(self):
        from backend.app.main import _FINISH_PHOTO_PRODUCER_WAIT_SECONDS, _FINISH_PHOTO_TIMELAPSE_POLL_TIMEOUT_SECONDS

        assert module.SEND_FALLBACK_SECONDS > (
            _FINISH_PHOTO_PRODUCER_WAIT_SECONDS + _FINISH_PHOTO_TIMELAPSE_POLL_TIMEOUT_SECONDS
        )

    def test_print_end_sends_it_after_the_other_notifications(self):
        """on_print_complete is too large to drive here, so its order is pinned on the source."""
        from backend.app.main import on_print_complete

        source = inspect.getsource(on_print_complete)
        task = source[source.index("async def _photo_then_notify") :]
        assert task.index("await _background_notifications(") < task.index("plate_clear_prompts.send_after_print_end(")
        body = source[source.index("async def _background_notifications") :]
        assert body.index("on_print_complete(") < body.index("_dispatch_outcome_confirmation_safely(")


class TestTheButton:
    @pytest.mark.asyncio
    async def test_valid_tap_releases_edits_and_answers(
        self, bot, poller, db_session, printer_factory, telegram_provider
    ):
        printer = await printer_factory()
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, prompt.token, prompt.message_id)]

        assert await poller.poll_once(BOT_TOKEN) == "ok"

        assert not printer_manager.is_awaiting_plate_clear(printer.id)
        edit = bot.of("editMessageText")[0]
        assert edit["message_id"] == prompt.message_id
        assert "Plate cleared at" in edit["text"]
        assert "reply_markup" not in edit
        answer = bot.of("answerCallbackQuery")[0]
        assert answer == {"callback_query_id": "query-1", "text": "Plate marked as clear"}
        assert await _prompts(db_session) == []

    @pytest.mark.asyncio
    async def test_used_button_does_nothing_more(self, bot, poller, db_session, printer_factory, telegram_provider):
        printer = await printer_factory()
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        token, message_id = prompt.token, prompt.message_id
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, token, message_id)]
        await poller.poll_once(BOT_TOKEN)

        # The next print raised the gate again: the old button must not release it.
        printer_manager._awaiting_plate_clear.add(printer.id)
        bot.updates = [_tap(2, token, message_id)]
        await poller.poll_once(BOT_TOKEN)

        assert printer_manager.is_awaiting_plate_clear(printer.id)
        assert bot.of("answerCallbackQuery")[1]["text"] == "This button is no longer valid"
        assert bot.of("editMessageReplyMarkup")[0]["message_id"] == message_id

    @pytest.mark.asyncio
    async def test_expired_button_leaves_the_gate_up(self, bot, poller, db_session, printer_factory, telegram_provider):
        printer = await printer_factory()
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        prompt.expires_at = utcnow_naive() - timedelta(seconds=1)
        await db_session.commit()
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, prompt.token, prompt.message_id)]

        await poller.poll_once(BOT_TOKEN)

        assert printer_manager.is_awaiting_plate_clear(printer.id)
        assert "expired" in bot.of("editMessageText")[0]["text"]
        assert bot.of("answerCallbackQuery")[0]["text"] == "This button is no longer valid"
        assert await _prompts(db_session) == []

    @pytest.mark.asyncio
    async def test_prune_expires_untouched_buttons(self, bot, poller, db_session, printer_factory, telegram_provider):
        printer = await printer_factory()
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        prompt.expires_at = utcnow_naive() - timedelta(seconds=1)
        await db_session.commit()

        await poller.prune_stale()

        assert "expired" in bot.of("editMessageText")[0]["text"]
        assert await _prompts(db_session) == []
        assert printer_manager.is_awaiting_plate_clear(printer.id)

    @pytest.mark.asyncio
    async def test_other_chat_is_refused(self, bot, poller, db_session, printer_factory, telegram_provider):
        printer = await printer_factory()
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, prompt.token, prompt.message_id, chat_id=OTHER_CHAT_ID)]

        await poller.poll_once(BOT_TOKEN)

        assert printer_manager.is_awaiting_plate_clear(printer.id)
        assert bot.of("editMessageText") == []
        assert bot.of("answerCallbackQuery")[0]["text"] == "This button is no longer valid"
        assert len(await _prompts(db_session)) == 1

    @pytest.mark.asyncio
    async def test_chat_changed_on_the_provider_is_refused(
        self, bot, poller, db_session, printer_factory, telegram_provider
    ):
        printer = await printer_factory()
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        provider.config = json.dumps({"bot_token": BOT_TOKEN, "chat_id": OTHER_CHAT_ID})
        await db_session.commit()
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, prompt.token, prompt.message_id)]

        await poller.poll_once(BOT_TOKEN)

        assert printer_manager.is_awaiting_plate_clear(printer.id)

    @pytest.mark.asyncio
    async def test_provider_of_another_bot_is_refused(
        self, bot, poller, db_session, printer_factory, telegram_provider
    ):
        printer = await printer_factory()
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        poller._providers[BOT_TOKEN] = {provider.id + 1}
        bot.updates = [_tap(1, prompt.token, prompt.message_id)]

        await poller.poll_once(BOT_TOKEN)

        assert printer_manager.is_awaiting_plate_clear(printer.id)

    @pytest.mark.asyncio
    async def test_already_released_says_so_and_does_nothing(
        self, bot, poller, db_session, printer_factory, telegram_provider
    ):
        printer = await printer_factory(name="Workshop")
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        printer_manager._awaiting_plate_clear.discard(printer.id)
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, prompt.token, prompt.message_id)]

        with patch.object(module, "release_plate", AsyncMock()) as release:
            await poller.poll_once(BOT_TOKEN)

        release.assert_not_awaited()
        assert bot.of("answerCallbackQuery")[0]["text"] == "Workshop: the plate was already clear."
        assert await _prompts(db_session) == []

    @pytest.mark.asyncio
    async def test_callbacks_of_other_buttons_are_left_alone(self, bot, poller, telegram_provider):
        provider = await telegram_provider()
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, "", 1, data="someone_else:42")]

        await poller.poll_once(BOT_TOKEN)

        assert bot.of("answerCallbackQuery") == []

    @pytest.mark.asyncio
    async def test_a_failure_still_answers_so_the_button_stops_spinning(self, bot, poller, telegram_provider):
        provider = await telegram_provider()
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, "whatever", 1)]

        with patch.object(plate_clear_prompts, "handle_tap", AsyncMock(side_effect=RuntimeError("boom"))):
            await poller.poll_once(BOT_TOKEN)

        assert bot.of("answerCallbackQuery") == [{"callback_query_id": "query-1"}]

    @pytest.mark.asyncio
    async def test_button_still_works_after_a_restart(
        self, bot, poller, db_session, printer_factory, telegram_provider
    ):
        printer = await printer_factory()
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)

        # Nothing in memory describes the prompt: it is all in the row.
        plate_clear_prompts._armed.clear()
        restarted = TelegramReactionPoller()
        restarted._session_factory = poller._session_factory
        restarted._http_client = bot
        restarted._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, prompt.token, prompt.message_id)]

        await restarted.poll_once(BOT_TOKEN)

        assert not printer_manager.is_awaiting_plate_clear(printer.id)

    @pytest.mark.asyncio
    async def test_each_printer_keeps_its_own_button(self, bot, poller, db_session, printer_factory, telegram_provider):
        first = await printer_factory(name="First")
        second = await printer_factory(name="Second")
        provider = await telegram_provider()
        first_prompt = await _sent_button(db_session, first, provider, bot)
        second_prompt = await _sent_button(db_session, second, provider, bot)
        assert first_prompt.token != second_prompt.token
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, first_prompt.token, first_prompt.message_id)]

        await poller.poll_once(BOT_TOKEN)

        assert not printer_manager.is_awaiting_plate_clear(first.id)
        assert printer_manager.is_awaiting_plate_clear(second.id)
        assert [p.printer_id for p in await _prompts(db_session)] == [second.id]


class TestReleasedAnotherWay:
    @pytest.mark.asyncio
    async def test_retire_takes_the_button_off_every_provider(
        self, bot, db_session, printer_factory, telegram_provider
    ):
        printer = await printer_factory()
        await telegram_provider(name="A")
        await telegram_provider(name="B")
        _raise_gate(printer.id)
        await plate_clear_prompts.send_after_print_end(printer.id)
        assert len(await _prompts(db_session, printer.id)) == 2

        await plate_clear_prompts.retire(printer.id)

        edits = bot.of("editMessageText")
        assert len(edits) == 2
        assert all("Plate cleared at" in e["text"] and "reply_markup" not in e for e in edits)
        assert await _prompts(db_session) == []

    @pytest.mark.asyncio
    async def test_failed_edit_does_not_bring_the_button_back(
        self, bot, db_session, printer_factory, telegram_provider
    ):
        printer = await printer_factory()
        provider = await telegram_provider()
        await _sent_button(db_session, printer, provider, bot)

        async def refuse(url, json=None, data=None, files=None):
            raise httpx.ConnectError("no route")

        with patch.object(bot, "post", refuse):
            await plate_clear_prompts.retire(printer.id)

        assert await _prompts(db_session) == []


class TestOutcomeStaysIndependent:
    @pytest.fixture
    async def unanswered_archive(self, db_session, archive_factory, printer_factory):
        printer = await printer_factory()
        archive = await archive_factory(printer.id, status="completed", confirm_requested=True)
        return printer, archive

    @pytest.mark.asyncio
    async def test_releasing_leaves_the_outcome_open(
        self, bot, poller, db_session, unanswered_archive, telegram_provider
    ):
        printer, archive = unanswered_archive
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, prompt.token, prompt.message_id)]

        await poller.poll_once(BOT_TOKEN)

        fresh = await db_session.get(PrintArchive, archive.id, populate_existing=True)
        assert not printer_manager.is_awaiting_plate_clear(printer.id)
        assert fresh.user_verdict is None

    @pytest.mark.asyncio
    async def test_default_good_setting_applies_as_on_the_web(
        self, bot, poller, db_session, unanswered_archive, telegram_provider
    ):
        printer, archive = unanswered_archive
        db_session.add(Settings(key="confirm_default_good_on_plate_clear", value="true"))
        await db_session.commit()
        provider = await telegram_provider()
        prompt = await _sent_button(db_session, printer, provider, bot)
        poller._providers[BOT_TOKEN] = {provider.id}
        bot.updates = [_tap(1, prompt.token, prompt.message_id)]

        await poller.poll_once(BOT_TOKEN)

        fresh = await db_session.get(PrintArchive, archive.id, populate_existing=True)
        assert fresh.user_verdict == "good"
        assert fresh.user_verdict_source == "plate_clear"


class TestPolling:
    @pytest.mark.asyncio
    async def test_polls_only_with_the_event_and_the_button_on(self, telegram_provider):
        on = await telegram_provider()
        no_button = await telegram_provider(plate_clear_button=False)
        no_event = await telegram_provider(on_plate_clear_required=False)
        reactions = await telegram_provider(
            on_plate_clear_required=False, plate_clear_button=False, telegram_verdict_mode="reactions"
        )

        assert _provider_reaction_token(on) == BOT_TOKEN
        assert _provider_reaction_token(no_button) is None
        assert _provider_reaction_token(no_event) is None
        assert _provider_reaction_token(reactions) == BOT_TOKEN


class TestProviderApi:
    @pytest.mark.asyncio
    async def test_button_setting_round_trips_and_defaults_off(self, async_client):
        with patch("backend.app.api.routes.notifications.telegram_reaction_poller.sync", AsyncMock()):
            created = await async_client.post(
                "/api/v1/notifications/",
                json={"name": "T", "provider_type": "telegram", "config": TELEGRAM_CONFIG},
            )
            assert created.status_code == 200
            assert created.json()["plate_clear_button"] is False

            updated = await async_client.patch(
                f"/api/v1/notifications/{created.json()['id']}", json={"plate_clear_button": True}
            )
        assert updated.status_code == 200
        assert updated.json()["plate_clear_button"] is True

    @pytest.mark.asyncio
    async def test_deleting_the_printer_removes_its_prompts(
        self, bot, async_client, db_session, printer_factory, telegram_provider
    ):
        printer = await printer_factory()
        provider = await telegram_provider()
        await _sent_button(db_session, printer, provider, bot)

        with patch("backend.app.api.routes.printers.printer_manager.disconnect_printer"):
            response = await async_client.delete(f"/api/v1/printers/{printer.id}")

        assert response.status_code == 200
        assert await _prompts(db_session) == []
