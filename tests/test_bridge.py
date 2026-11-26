"""Slack Bridge behaviour against a simulated Slack and Möbius.

Run: python3 -m pytest -q tests  (from the app folder)
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge  # noqa: E402
import bridge_core as core  # noqa: E402

BOT = "UBOT"
MEMBER = {"id": "UANA", "real_name": "Ana Kovač", "profile": {"display_name": "ana", "real_name": "Ana Kovač"}}
GUEST = {"id": "UGST", "is_restricted": True, "profile": {"display_name": "guest"}}
OUTSIDER = {"id": "UOUT", "profile": {"display_name": "out"}}


def ev(**event):
  return {"team_id": "T1", "event": event}


class FakeApi:
  """Slack (through the vault) and Möbius, recorded."""

  def __init__(self):
    self.app_id = "7"
    self.calls = []          # (slack method, params)
    self.mobius = []         # (method, path, body)
    self.chats = {}          # chat_id -> chat dict served by GET
    self.users = {"UANA": MEMBER, "UGST": GUEST, "UOUT": OUTSIDER}
    self.denied = set()

  vault_attempts = 0

  def slack(self, method, params=None, *, key=bridge.BOT_KEY, as_json=False):
    self.vault_attempts += 1
    if key in self.denied:
      raise bridge.VaultDenied(key)
    self.calls.append((method, params or {}))
    if method == "auth.test":
      return {"ok": True, "user_id": BOT, "user": "mobius", "team": "Möbius"}
    if method == "users.info":
      return {"ok": True, "user": self.users[params["user"]]}
    if method == "conversations.info":
      return {"ok": True, "channel": {"name": "dev"}}
    if method == "apps.connections.open":
      return {"ok": True, "url": self.ws_url}
    return {"ok": True}

  def json(self, method, path, body=None):
    self.mobius.append((method, path, body))
    if path == "/api/app-chats/start":
      chat_id = f"chat-{len(self.chats) + 1}"
      self.chats[chat_id] = {"running": True, "messages": []}
      return {"chat_id": chat_id, "outcome": "started"}
    if path.endswith("/messages"):
      return {}
    if method == "GET" and path.startswith("/api/chats/"):
      # Like Möbius: the chat route refuses an app's own token.
      raise RuntimeError(f"GET {path} -> 403: App token is not valid here.")
    raise AssertionError(path)

  read_refused = False

  def read_chat(self, chat_id, limit=6):
    if self.read_refused:
      raise RuntimeError("POST /api/app-chats/x/embed-capability -> 403")
    return self.chats[chat_id]

  # The owner's decisions in the app's storage; None means never saved (404),
  # "broken" means storage answered with an error.
  access = {"allowed": [{"id": "UANA", "name": "Ana Kovač"}], "blocked": []}

  def request(self, method, path, body=None, **_):
    self.mobius.append((method, path, body))
    if method == "GET" and path.endswith("/access.json"):
      if self.access is None:
        return 404, {}, b'{"detail":"Not found"}'
      if self.access == "broken":
        return 503, {}, b'{"detail":"unavailable"}'
      return 200, {}, json.dumps(self.access).encode()
    return 200, {}, b"{}"

  def pushes(self):
    return [body for m, p, body in self.mobius if p == "/api/notifications/send"]

  def saved_requests(self):
    saved = [body for m, p, body in self.mobius if m == "PUT" and p.endswith("/requests.json")]
    return saved[-1]["requests"] if saved else []

  def answer(self, chat_id, text, *, mid="a1", question=False, tool_first=True):
    blocks = []
    if tool_first:
      blocks += [{"type": "text", "content": "Checking."}, {"type": "tool", "tool": "Bash"}]
    blocks.append({"type": "text", "content": text})
    if question:
      blocks.append({"type": "question", "question_id": "q1"})
    chat = self.chats[chat_id]
    chat["messages"].append({"role": "assistant", "id": mid, "ts": str(int(time.time() * 1000) + 5), "blocks": blocks})
    chat["running"] = False
    chat["pending_question_id"] = "q1" if question else None

  def posts(self):
    return [p for m, p in self.calls if m == "chat.postMessage"]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
  monkeypatch.setattr(bridge, "POLL_SECONDS", 0.01)
  monkeypatch.setattr(bridge, "NO_ANSWER_SECONDS", 0.3)


def make(tmp_path):
  api = FakeApi()
  b = bridge.Bridge(api, bridge.State(tmp_path / "s.json"), origin="https://m.example")
  return api, b


async def settle(b):
  for task in list(b.watchers.values()):
    await asyncio.wait_for(task, 3)


# Parsing --------------------------------------------------------------------

def test_channel_mention_is_taken_once():
  mention = ev(type="app_mention", user="UANA", text=f"<@{BOT}> hi", ts="1.1", channel="CDEV")
  twin = ev(type="message", channel_type="channel", user="UANA", text=f"<@{BOT}> hi", ts="1.1", channel="CDEV")
  assert core.parse_event(mention, BOT).mentioned
  assert core.parse_event(twin, BOT) is None


def test_bots_edits_and_own_posts_are_ignored():
  assert core.parse_event(ev(type="message", channel_type="im", bot_id="B1", user="UX", text="x", ts="1"), BOT) is None
  assert core.parse_event(ev(type="message", channel_type="im", subtype="message_changed", ts="1"), BOT) is None
  assert core.parse_event(ev(type="message", channel_type="im", user=BOT, text="x", ts="1"), BOT) is None


def test_plain_channel_message_only_continues_a_known_thread():
  msg = core.parse_event(ev(type="message", channel_type="channel", user="UANA", text="more", ts="2.0", thread_ts="1.0", channel="CDEV"), BOT)
  assert msg.scope == "slack:T1:CDEV:1.0"
  assert not core.wants_turn(msg, known_thread=False)
  assert core.wants_turn(msg, known_thread=True)


def test_reply_is_the_text_after_the_last_tool_step():
  chat = {"messages": [{"role": "assistant", "id": "a", "ts": "200", "blocks": [
    {"type": "text", "content": "Let me look."}, {"type": "tool"}, {"type": "text", "content": "Done: 3 PRs."}]}]}
  assert core.finished_reply(chat, 100, set()).text == "Done: 3 PRs."
  assert core.finished_reply(chat, 300, set()) is None     # older than the request
  assert core.finished_reply(chat, 100, {"a"}) is None     # already posted


def test_long_answers_split_under_slacks_limit():
  parts = core.split_markdown(("para " * 300 + "\n\n") * 20)
  assert len(parts) > 1 and all(len(p) <= core.SLACK_MARKDOWN_LIMIT for p in parts)


def test_mentions_are_named_and_the_bot_mention_dropped():
  assert core.clean_text(f"<@{BOT}> ask <@UANA> &amp; me", BOT, {"UANA": "ana"}) == "ask @ana & me"


# Access ---------------------------------------------------------------------

def said_anything(api):
  """Whether the bot posted any message (public or private) to Slack."""
  return any(m in ("chat.postMessage", "chat.postEphemeral") for m, _ in api.calls)


def test_guests_are_ignored_silently_and_never_become_requests(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  asyncio.run(b.on_event(ev(type="app_mention", user="UGST", text=f"<@{BOT}> rm -rf", ts="1.1", channel="CDEV")))
  assert not any(p == "/api/app-chats/start" for _, p, _ in api.mobius)
  assert not said_anything(api) and not api.saved_requests()
  assert b.state.data["activity"][0]["detail"] == "guest"


def test_someone_new_becomes_an_access_request_and_the_owner_hears_once(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  asyncio.run(b.on_event(ev(type="app_mention", user="UOUT", text=f"<@{BOT}> hi", ts="1.1", channel="CDEV")))
  asyncio.run(b.on_event(ev(type="message", channel_type="im", user="UOUT", text="hi again", ts="1.2", channel="DOUT")))
  assert not any(p == "/api/app-chats/start" for _, p, _ in api.mobius)
  assert not said_anything(api)   # pending requests get no reply, only the waiting mark
  [request] = api.saved_requests()
  assert request["id"] == "UOUT" and request["count"] == 2 and request["where"] == "a direct message to the Möbius bot"
  [push] = api.pushes()
  assert "asked to use the Möbius bot in #dev" in push["body"] and push["target"] == "/shell/?app=7"
  waiting = [p for m, p in api.calls if m == "reactions.add" and p["name"] == "hourglass_flowing_sand"]
  assert [p["timestamp"] for p in waiting] == ["1.1", "1.2"]


def test_allowing_a_requester_tells_them_once_where_they_asked(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  asyncio.run(b.on_event(ev(type="app_mention", user="UOUT", text=f"<@{BOT}> hi", ts="3.0", channel="CDEV")))
  asyncio.run(b.on_event(ev(type="app_mention", user="UOUT", text=f"<@{BOT}> hello?", ts="3.4", channel="CDEV")))
  api.access = {"allowed": [{"id": "UANA"}, {"id": "UOUT"}], "blocked": []}
  asyncio.run(b.settle_requests())
  asyncio.run(b.settle_requests())
  notices = [p for m, p in api.calls if m == "chat.postMessage" and "gave you access" in p.get("text", "")]
  assert notices == [{"channel": "CDEV", "thread_ts": "3.4", "text": f"<@UOUT> {bridge.core.ACCESS_GRANTED}"}]
  cleared = [p["timestamp"] for m, p in api.calls if m == "reactions.remove" and p["name"] == "hourglass_flowing_sand"]
  assert cleared == ["3.0", "3.4"]
  assert api.saved_requests() == []          # no longer shown as a request
  assert b.state.data["activity"][0]["outcome"] == "allowed"


def test_a_request_saved_by_0_3_without_its_place_is_answered_by_direct_message(tmp_path):
  api, b = make(tmp_path)
  b.state.data["requests"] = {"UOUT": {"id": "UOUT", "name": "out", "count": 1, "first_at": 1, "last_at": 1}}
  api.access = {"allowed": [{"id": "UOUT"}], "blocked": []}
  asyncio.run(b.settle_requests())
  [notice] = [p for m, p in api.calls if m == "chat.postMessage"]
  assert notice == {"channel": "UOUT", "text": bridge.core.ACCESS_GRANTED}


def test_blocking_a_requester_says_nothing_and_clears_their_waiting_mark(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  asyncio.run(b.on_event(ev(type="message", channel_type="im", user="UOUT", text="hi", ts="4.0", channel="DOUT")))
  api.access = {"allowed": [], "blocked": [{"id": "UOUT"}]}
  asyncio.run(b.settle_requests())
  asyncio.run(b.settle_requests())
  assert not said_anything(api)
  assert any(m == "reactions.remove" and p["timestamp"] == "4.0" for m, p in api.calls)


def test_someone_blocked_then_allowed_later_is_let_in(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  asyncio.run(b.on_event(ev(type="message", channel_type="im", user="UOUT", text="hi", ts="5.0", channel="DOUT")))
  api.access = {"allowed": [], "blocked": [{"id": "UOUT"}]}
  asyncio.run(b.settle_requests())
  api.access = {"allowed": [{"id": "UOUT"}], "blocked": []}

  async def flow():
    await b.on_event(ev(type="message", channel_type="im", user="UOUT", text="hi again", ts="5.5", channel="DOUT"))
    api.answer("chat-1", "Hello.")
    await settle(b)

  asyncio.run(flow())
  assert sum(p == "/api/app-chats/start" for _, p, _ in api.mobius) == 1


def test_a_blocked_person_is_turned_away_without_a_new_request(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  api.access = {"allowed": [], "blocked": [{"id": "UOUT"}]}
  asyncio.run(b.on_event(ev(type="app_mention", user="UOUT", text=f"<@{BOT}> hi", ts="1.1", channel="CDEV")))
  assert not said_anything(api)
  assert not api.saved_requests() and not api.pushes()


def test_allowing_a_requester_lets_their_next_message_through(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT

  async def flow():
    await b.on_event(ev(type="message", channel_type="im", user="UOUT", text="hi", ts="1.1", channel="DOUT"))
    api.access = {"allowed": [{"id": "UOUT"}], "blocked": []}
    await b.on_event(ev(type="message", channel_type="im", user="UOUT", text="hi", ts="1.2", channel="DOUT"))
    api.answer("chat-1", "Hello.")
    await settle(b)

  asyncio.run(flow())
  assert sum(p == "/api/app-chats/start" for _, p, _ in api.mobius) == 1


def test_the_list_saved_by_version_0_2_still_counts(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  api.access = {"people": [{"id": "UANA"}]}
  asyncio.run(b.on_event(ev(type="message", channel_type="im", user="UANA", text="hi", ts="1.1", channel="DANA")))
  assert any(p == "/api/app-chats/start" for _, p, _ in api.mobius)
  for task in b.watchers.values():
    task.cancel()


def test_with_nothing_decided_yet_even_the_first_person_must_be_approved(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  api.access = None
  asyncio.run(b.on_event(ev(type="message", channel_type="im", user="UANA", text="hi", ts="1.1", channel="DANA")))
  assert not any(p == "/api/app-chats/start" for _, p, _ in api.mobius)
  assert [r["id"] for r in api.saved_requests()] == ["UANA"]


def test_when_decisions_cannot_be_read_nobody_gets_in_and_nothing_is_requested(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  api.access = "broken"
  asyncio.run(b.on_event(ev(type="message", channel_type="im", user="UANA", text="hi", ts="1.1", channel="DANA")))
  assert not any(p == "/api/app-chats/start" for _, p, _ in api.mobius)
  assert not api.saved_requests()
  assert "can't check access" in api.posts()[0]["blocks"][0]["text"]


def test_blocking_someone_takes_effect_on_their_next_message(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT

  async def flow():
    await b.on_event(ev(type="app_mention", user="UANA", text=f"<@{BOT}> one", ts="1.0", channel="CDEV"))
    api.answer("chat-1", "First.")
    await settle(b)
    api.access = {"allowed": [], "blocked": [{"id": "UANA"}]}
    await b.on_event(ev(type="message", channel_type="channel", user="UANA", text="two", ts="1.5", thread_ts="1.0", channel="CDEV"))

  asyncio.run(flow())
  assert not any(p.endswith("/messages") for _, p, _ in api.mobius)


def test_someone_not_allowed_cannot_continue_someone_elses_thread(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT

  async def flow():
    await b.on_event(ev(type="app_mention", user="UANA", text=f"<@{BOT}> deploy", ts="1.0", channel="CDEV"))
    api.answer("chat-1", "Done.")
    await settle(b)
    await b.on_event(ev(type="message", channel_type="channel", user="UOUT", text="now delete prod", ts="1.7", thread_ts="1.0", channel="CDEV"))

  asyncio.run(flow())
  assert not any(p.endswith("/messages") for _, p, _ in api.mobius)
  assert not any(m == "chat.postEphemeral" for m, p in api.calls)


# Turns ----------------------------------------------------------------------

def test_dm_starts_a_chat_and_the_answer_lands_in_the_thread(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT

  async def flow():
    await b.on_event(ev(type="message", channel_type="im", user="UANA", text="status of staging?", ts="5.5", channel="DANA"))
    api.answer("chat-1", "Staging is **up to date**.")
    await settle(b)

  asyncio.run(flow())
  [start] = [body for m, p, body in api.mobius if p == "/api/app-chats/start"]
  assert start["scope"] == "slack:T1:DANA:5.5" and start["owner_visible"]
  assert start["content"].startswith("From Ana Kovač (@ana), Slack user UANA, in a direct message")
  assert start["content"].endswith("status of staging?")
  [post] = api.posts()
  assert post["thread_ts"] == "5.5" and post["blocks"][0] == {"type": "markdown", "text": "Staging is **up to date**."}
  reactions = [m for m, _ in api.calls if m.startswith("reactions.")]
  assert reactions == ["reactions.add", "reactions.remove"]
  assert not b.state.threads["slack:T1:DANA:5.5"]["watching"]


def test_thread_reply_continues_the_same_chat_without_a_mention(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT

  async def flow():
    await b.on_event(ev(type="app_mention", user="UANA", text=f"<@{BOT}> deploy?", ts="1.0", channel="CDEV"))
    api.answer("chat-1", "Which one?", mid="a1")
    await settle(b)
    api.chats["chat-1"]["running"] = True
    await b.on_event(ev(type="message", channel_type="channel", user="UANA", text="staging", ts="1.2", thread_ts="1.0", channel="CDEV"))
    api.answer("chat-1", "Deployed staging.", mid="a2")
    await settle(b)

  asyncio.run(flow())
  assert [p for _, p, _ in api.mobius if p.endswith("/messages")] == ["/api/chats/chat-1/messages"]
  assert [p["blocks"][0]["text"] for p in api.posts()] == ["Which one?", "Deployed staging."]
  assert len(api.chats) == 1


def test_a_card_for_the_owner_is_flagged_and_the_answer_still_arrives(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT

  async def flow():
    await b.on_event(ev(type="message", channel_type="im", user="UANA", text="restart it", ts="9.0", channel="DANA"))
    api.answer("chat-1", "This needs the owner's restart.", mid="a1", question=True)
    for _ in range(100):
      if api.posts():
        break
      await asyncio.sleep(0.01)
    assert "Waiting for the Möbius owner" in api.posts()[0]["blocks"][0]["text"]
    assert "https://m.example/shell/?chat=chat-1" in api.posts()[0]["blocks"][0]["text"]
    api.chats["chat-1"]["running"] = True
    await asyncio.sleep(0.05)
    api.answer("chat-1", "Restarted and healthy.", mid="a2")
    await settle(b)

  asyncio.run(flow())
  assert api.posts()[-1]["blocks"][0]["text"] == "Restarted and healthy."


def test_an_unreadable_answer_is_reported_instead_of_waiting_silently(tmp_path, monkeypatch):
  monkeypatch.setattr(bridge, "READ_FAILURES_BEFORE_GIVING_UP", 3)
  api, b = make(tmp_path)
  b.bot_user = BOT
  api.read_refused = True

  async def flow():
    await b.on_event(ev(type="message", channel_type="im", user="UANA", text="hi", ts="6.0", channel="DANA"))
    await settle(b)

  asyncio.run(flow())
  [post] = api.posts()
  assert "can't read the answer back" in post["blocks"][0]["text"]
  assert "https://m.example/shell/?chat=chat-1" in post["blocks"][0]["text"]
  assert b.state.data["activity"][0]["outcome"] == "read failed"


def test_slack_redelivery_never_starts_a_second_turn(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  event = ev(type="message", channel_type="im", user="UANA", text="hi", ts="3.0", channel="DANA")

  async def flow():
    await b.on_event(event)
    await b.on_event(event)
    api.answer("chat-1", "Hello.")
    await settle(b)

  asyncio.run(flow())
  assert sum(p == "/api/app-chats/start" for _, p, _ in api.mobius) == 1


def test_owed_replies_survive_a_new_run(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT

  async def first_run():
    await b.on_event(ev(type="message", channel_type="im", user="UANA", text="long job", ts="4.0", channel="DANA"))
    for task in b.watchers.values():
      task.cancel()

  asyncio.run(first_run())
  b2 = bridge.Bridge(api, bridge.State(tmp_path / "s.json"))

  async def second_run():
    b2.resume()
    api.answer("chat-1", "Finished the long job.")
    await settle(b2)

  asyncio.run(second_run())
  assert api.posts()[-1]["blocks"][0]["text"] == "Finished the long job."


# Socket Mode ------------------------------------------------------------------

def test_socket_mode_acks_every_envelope_and_dispatches_events(tmp_path):
  from websockets.asyncio.server import serve

  api, b = make(tmp_path)
  b.bot_user = BOT
  acks = []

  async def slack_side(ws):
    await ws.send(json.dumps({"type": "hello"}))
    await ws.send(json.dumps({"type": "events_api", "envelope_id": "e1", "payload": ev(
      type="message", channel_type="im", user="UANA", text="ping", ts="8.0", channel="DANA")}))
    acks.append(json.loads(await ws.recv()))
    await ws.send(json.dumps({"type": "disconnect", "reason": "refresh_requested"}))
    await asyncio.sleep(0.2)

  async def flow():
    async with serve(slack_side, "127.0.0.1", 0) as server:
      port = server.sockets[0].getsockname()[1]
      api.ws_url = f"ws://127.0.0.1:{port}/"
      await bridge.socket_loop(b, time.time() + 0.6)
    await asyncio.sleep(0.05)

  asyncio.run(flow())
  assert acks[0] == {"envelope_id": "e1"}
  assert any(p == "/api/app-chats/start" for _, p, _ in api.mobius)
  assert any(p.endswith("/status.json") and (body or {}).get("state") == "connected" for _, p, body in api.mobius)


def test_the_window_is_told_which_version_is_running(tmp_path):
  import json as _json
  api = FakeApi()
  bridge.write_status(api, None, state="connecting")
  [(_, _, status)] = api.mobius
  expected = _json.loads((Path(bridge.__file__).parent / "mobius.json").read_text())["version"]
  assert status["version"] == expected and expected


def test_missing_keys_wait_quietly_and_retry_later(tmp_path):
  api = FakeApi()
  api.denied = {bridge.BOT_KEY}
  state = bridge.State(tmp_path / "s.json")
  assert asyncio.run(bridge.run(api, state, "")) == 0
  assert any((body or {}).get("state") == "needs_keys" for _, _, body in api.mobius)
  attempts = api.vault_attempts
  api.mobius.clear()
  assert asyncio.run(bridge.run(api, bridge.State(tmp_path / "s.json"), "")) == 0
  assert api.vault_attempts == attempts   # inside the retry window: the vault is left alone
  [(_, _, status)] = api.mobius           # but the window still hears it is waiting
  assert status["state"] == "needs_keys"
