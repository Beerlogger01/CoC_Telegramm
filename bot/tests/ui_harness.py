"""Offline Telegram transport + real PTB Application and production handlers."""

import copy
import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from types import SimpleNamespace

import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder
from telegram.request import BaseRequest

from app import bot as handlers
from app.bindings_storage import Binding, BindingsStorage
from app.navigation import navigator, plain_text
from app.operations import Operations, WarPlanStore
from app.polls import PollController, PollStore
from app.reminders import ReminderStore

GROUP = -10012345
OWNER = 101
TOKEN = "999:offline-test-token"


class HtmlCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "code", "a", "s", "u", "pre", "blockquote"}, (
            f"Unsafe HTML tag: {tag}"
        )
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, f"Unbalanced HTML: {tag}"


def validate_message(params):
    text = params.get("text", "")
    if params.get("parse_mode") == "HTML":
        parser = HtmlCheck()
        parser.feed(text)
        assert not parser.stack
        text = plain_text(text)
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    for row in (params.get("reply_markup") or {}).get("inline_keyboard", []):
        for button in row:
            data = button.get("callback_data")
            if data:
                assert len(data.encode()) <= 64, data


class TelegramTransport(BaseRequest):
    def __init__(self):
        self.messages = {}
        self.calls = []
        self.counter = 1000
        self.answers = []
        self.failures = {}

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        params = copy.deepcopy(request_data.parameters if request_data else {})
        self.calls.append((name, params))
        if name in self.failures:
            failure = self.failures.pop(name)
            if isinstance(failure, Exception):
                raise failure
            return 400, json.dumps(
                {"ok": False, "error_code": 400, "description": failure}
            ).encode()
        if name == "getMe":
            result = {
                "id": 999,
                "is_bot": True,
                "first_name": "Trobley",
                "username": "OfflineClanBot",
            }
        elif name in {"sendMessage", "editMessageText", "editMessageReplyMarkup"}:
            validate_message(params)
            chat_id = int(params["chat_id"])
            if name == "sendMessage":
                self.counter += 1
                mid = self.counter
                result = {
                    "message_id": mid,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {
                        "id": chat_id,
                        "type": "private" if chat_id > 0 else "supergroup",
                    },
                    "from": {"id": 999, "is_bot": True, "first_name": "Trobley"},
                }
            else:
                mid = params["message_id"]
                result = copy.deepcopy(self.messages.get((chat_id, mid)))
                if result is None:
                    return 400, json.dumps(
                        {
                            "ok": False,
                            "error_code": 400,
                            "description": "Bad Request: message to edit not found",
                        }
                    ).encode()
                if params.get("text", result.get("text")) == result.get(
                    "text"
                ) and params.get("reply_markup") == result.get("reply_markup"):
                    return 400, json.dumps(
                        {
                            "ok": False,
                            "error_code": 400,
                            "description": "Bad Request: message is not modified",
                        }
                    ).encode()
            if "text" in params:
                result["text"] = params["text"]
            result["reply_markup"] = params.get("reply_markup")
            self.messages[chat_id, mid] = result
        elif name == "answerCallbackQuery":
            self.answers.append(params)
            result = True
        elif name == "getChatMember":
            result = {
                "status": "member",
                "user": {
                    "id": params["user_id"],
                    "is_bot": False,
                    "first_name": "Player",
                },
            }
        elif name == "createChatInviteLink":
            result = {
                "invite_link": "https://t.me/+offline",
                "creator": {"id": 999, "is_bot": True, "first_name": "Trobley"},
                "creates_join_request": False,
                "is_primary": False,
                "is_revoked": False,
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


def tag_for(n):
    alphabet = "0289PYLQGRJCUV"
    return "#P" + alphabet[n // len(alphabet)] + alphabet[n % len(alphabet)] + "PP"


def fixtures(count=50):
    players = []
    for i in range(count):
        p = {
            "tag": tag_for(i),
            "name": f"{i:02} Lex<&>_*[]😀",
            "townHallLevel": 18 if i < 20 else 16,
            "expLevel": 280 - i,
            "role": "coLeader" if i == 0 else "member",
            "warPreference": "in",
            "donations": i * 10,
            "league": {"name": "Electro League 32"},
            "leagueName": "Electro League 32",
            "heroes": [
                {"name": f"Hero<{n}>", "level": n + 1, "village": "home"}
                for n in range(14)
            ],
            "heroEquipment": [
                {"name": f"Equipment & {n}", "level": n + 1} for n in range(29)
            ],
            "eligibility": "candidate",
            "selectionScore": 90 - i / 2,
            "clanRank": i + 1,
            "confidence": "medium",
            "scoreComponents": {
                "strength": 80,
                "discipline": 70,
                "quality": 60,
                "role": 50,
            },
            "scoreWeights": {
                "strength": 55,
                "discipline": 20,
                "quality": 20,
                "role": 5,
            },
            "history": {"wars": 3, "attacksUsed": 5, "attacksPossible": 6},
            "cwlHistory": {"wars": 3, "attacksUsed": 3, "attacksPossible": 3},
        }
        players.append(p)
    members = [
        dict(
            p,
            townhallLevel=p["townHallLevel"],
            attacks=[{"stars": 3, "defenderTag": players[-1]["tag"]}] if i % 2 else [],
        )
        for i, p in enumerate(players)
    ]
    war = {
        "state": "warEnded",
        "teamSize": len(players),
        "attacksPerMember": 2,
        "startTime": "20261104T180000Z",
        "endTime": "20261105T180000.000Z",
        "clan": {
            "tag": "#PPP",
            "name": "Clan<&>",
            "stars": 35,
            "destructionPercentage": 90,
            "members": members,
        },
        "opponent": {
            "tag": "#QQQ",
            "name": "Enemy<&>",
            "stars": 30,
            "destructionPercentage": 80,
            "members": members,
        },
    }
    return players, war


class Harness:
    def __init__(self, monkeypatch, tmp_path, count=50, own=24):
        self.players, self.war = fixtures(count)
        self.request = TelegramTransport()
        self.errors = []
        self.update_id = 0
        self.user = OWNER
        self.chat = OWNER
        self.token_valid = True
        self.fail_paths = set()
        self.empty = False
        self.db = str(tmp_path / "integration.db")
        self.storage = BindingsStorage(self.db)
        for p in self.players[:own]:
            self.storage.upsert_binding(
                Binding(OWNER, GROUP, p["tag"], None, "Captain<&>", "2026-11-05")
            )
        monkeypatch.setattr(handlers.settings, "clan_group_id", GROUP)
        monkeypatch.setattr(handlers.settings, "coc_clan_tag", "#PPP")
        monkeypatch.setattr(handlers.settings, "backend_url", "https://offline.invalid")
        monkeypatch.setattr(handlers.settings, "war_reminder_enabled", True)
        monkeypatch.setattr(handlers.settings, "bindings_db_path", self.db)
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            handlers.httpx,
            "AsyncClient",
            lambda *a, **kw: real_client(
                transport=httpx.MockTransport(self.backend), **kw
            ),
        )
        self.app = (
            ApplicationBuilder()
            .token(TOKEN)
            .request(self.request)
            .get_updates_request(self.request)
            .job_queue(None)
            .build()
        )
        self.polls = PollController(
            PollStore(self.db),
            GROUP,
            handlers.has_captain_access,
            handlers.send_or_edit_message,
        )
        self.ops = Operations(
            WarPlanStore(self.db),
            self.storage,
            "#PPP",
            GROUP,
            handlers.has_captain_access,
            handlers.send_or_edit_message,
            handlers.fetch_for_operations,
        )
        self.app.bot_data.update(
            storage=self.storage,
            polls=self.polls,
            operations=self.ops,
            reminders=ReminderStore(self.db),
        )
        handlers.register_handlers(self.app)

        async def record_error(update, context):
            self.errors.append(context.error)

        self.app.add_error_handler(record_error)
        navigator.reset()

    def backend(self, request):
        path = request.url.path
        if path in self.fail_paths:
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        players = [] if self.empty else self.players
        if path == "/clan":
            result = {
                "name": "Clan<&>",
                "tag": "#PPP",
                "memberList": players,
                "members": len(players),
            }
        elif path.startswith("/player/"):
            tag = path.rsplit("/", 1)[-1]
            result = next(
                (p for p in players if p["tag"] == tag), {"tag": tag, "name": "Unknown"}
            )
        elif path == "/next-war":
            mode = request.url.params.get("mode", "war")
            size = 15 if mode == "cwl" else int(request.url.params.get("size", "15"))
            result = {
                "mode": mode,
                "clanName": "Clan<&>",
                "observedWarCount": 3,
                "lineupSize": size,
                "recommendedSize": min(size, len(players)),
                "allPlayers": players,
                "recommendedLineup": players,
                "clanRanking": players,
                "weights": {"strength": 55, "discipline": 20, "quality": 20, "role": 5},
                "categories": {
                    "main": players[:size],
                    "bench": players[size:],
                    "cwl_review": players[:1],
                },
            }
        elif path == "/war":
            result = {"state": "notInWar"} if self.empty else self.war
        elif path == "/war-results":
            result = {"wars": [] if self.empty else [self.war] * 9}
        elif path == "/cwl":
            result = (
                {"state": "notInWar"}
                if self.empty
                else {
                    "state": "inWar",
                    "scheduledRounds": 7,
                    "observedRounds": 2,
                    "totalStars": 75,
                    "currentPlace": 2,
                    "rounds": [
                        {
                            "roundNumber": 1,
                            "enemy": "First<&>",
                            "stars": 35,
                            "enemyStars": 30,
                            "result": "win",
                        },
                        {
                            "roundNumber": 3,
                            "enemy": "Third<&>",
                            "stars": 30,
                            "enemyStars": 35,
                            "result": "loss",
                        },
                    ],
                }
            )
        elif path == "/hall":
            result = {}
        elif path == "/top-players":
            result = {"items": players, "memberList": players}
        elif path in {"/raids", "/games"}:
            result = {}
        elif path == "/activity-report":
            result = {
                "inactivePlayers": [],
                "kickCandidates": [],
                "promotionCandidates": [],
            }
        elif path.startswith("/progress/"):
            result = {
                "historyAvailable": True,
                "snapshotCount": 30,
                "snapshotDay": "2026-11-05",
                "changes": {"expLevel": {"from": 250, "to": 280, "delta": 30}},
            }
        elif path == "/selection/weights":
            result = {
                "war": {"strength": 20, "quality": 35, "discipline": 35, "role": 10},
                "cwl": {"strength": 55, "quality": 20, "discipline": 20, "role": 5},
            }
        elif path.startswith("/verify-player/"):
            result = {"verified": self.token_valid}
        else:
            raise AssertionError(f"Unstubbed backend path: {path}")
        return httpx.Response(200, json=copy.deepcopy(result))

    async def initialize(self):
        await self.app.initialize()

    async def shutdown(self):
        await self.app.shutdown()
        navigator.reset()

    @property
    def screen(self):
        state = navigator.screens.get(self.user)
        assert state and state.message, (
            f"No screen for {self.user}; errors={self.errors}"
        )
        return self.request.messages[state.chat_id, state.message.message_id]

    @property
    def actions(self):
        return {
            b["callback_data"].split(":", 2)[-1]: b["callback_data"]
            for row in (self.screen.get("reply_markup") or {}).get(
                "inline_keyboard", []
            )
            for b in row
            if "callback_data" in b
        }

    async def process(self, data):
        self.update_id += 1
        update = Update.de_json({"update_id": self.update_id, **data}, self.app.bot)
        await self.app.process_update(update)
        assert not self.errors, self.errors
        return update

    async def text(self, text):
        message = {
            "message_id": self.update_id + 1,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {
                "id": self.chat,
                "type": "private" if self.chat > 0 else "supergroup",
            },
            "from": {"id": self.user, "is_bot": False, "first_name": "Captain<&>"},
            "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        return await self.process({"message": message})

    async def click(self, action, *, raw=None, message=None):
        raw = raw or self.actions[action]
        message = message or copy.deepcopy(self.screen)
        return await self.process(
            {
                "callback_query": {
                    "id": str(self.update_id + 1),
                    "from": {
                        "id": self.user,
                        "is_bot": False,
                        "first_name": "Captain<&>",
                    },
                    "chat_instance": "offline",
                    "data": raw,
                    "message": message,
                }
            }
        )

    def sends(self, chat=None):
        return [
            p
            for name, p in self.request.calls
            if name == "sendMessage" and (chat is None or p["chat_id"] == chat)
        ]

    @property
    def context(self):
        return SimpleNamespace(
            bot=self.app.bot,
            application=self.app,
            user_data=self.app.user_data[self.user],
        )
