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
    if method == "users.conversations":
      return {"ok": True, "channels": [{"id": "CDEV"}]}
    if method == "conversations.members":
      return {"ok": True, "members": ["UANA", BOT]}
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
      return self.chats[path.split("/")[3].split("?")[0]]
    raise AssertionError(path)

  def request(self, method, path, body=None, **_):
    self.mobius.append((method, path, body))
    return 200, {}, b"{}"

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

def test_guests_are_refused_privately(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  asyncio.run(b.on_event(ev(type="app_mention", user="UGST", text=f"<@{BOT}> rm -rf", ts="1.1", channel="CDEV")))
  assert not any(p == "/api/app-chats/start" for _, p, _ in api.mobius)
  [(method, params)] = [c for c in api.calls if c[0] == "chat.postEphemeral"]
  assert params["user"] == "UGST" and "guests" in params["text"]


def test_dm_from_someone_outside_the_bots_channels_is_refused(tmp_path):
  api, b = make(tmp_path)
  b.bot_user = BOT
  asyncio.run(b.on_event(ev(type="message", channel_type="im", user="UOUT", text="hi", ts="1.1", channel="DOUT")))
  assert not any(p == "/api/app-chats/start" for _, p, _ in api.mobius)
  assert "members of a channel" in api.posts()[0]["blocks"][0]["text"]


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
