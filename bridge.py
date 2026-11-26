#!/usr/bin/env python3
"""Slack Bridge job: keeps one Slack Socket Mode connection open and turns
Slack messages into agent turns on this Möbius, posting each answer back into
its Slack thread.

The platform runs this every minute and never runs two copies at once, so a
live connection simply makes later ticks skip. The run exits before its
two-hour app token expires; replies still owed are saved and finished by the
next run. Slack tokens stay in the owner's key vault: every Slack call is a
vault borrow, and the WebSocket address Slack returns needs no token.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge_core as core  # noqa: E402

BOT_KEY = "slack-bot-token"
APP_KEY = "slack-app-token"
RUN_SECONDS = 100 * 60          # the job's app token lasts 2 hours
POLL_SECONDS = 4
NO_ANSWER_SECONDS = 180         # idle with no answer this long: tell Slack
GIVE_UP_SECONDS = 12 * 3600
KEY_RETRY_SECONDS = 10 * 60
CACHE_SECONDS = 300
HEARTBEAT_SECONDS = 60
READ_FAILURES_BEFORE_GIVING_UP = 15     # about a minute of refused reads
ACTIVITY_KEEP = 30
SEEN_KEEP = 500


class VaultDenied(Exception):
  """The key is missing or not approved for this app on that path."""


class SlackError(Exception):
  pass


class Mobius:
  """This Möbius's API with the job's app token."""

  def __init__(self, base: str, token: str, app_id: str):
    self.base = base.rstrip("/")
    self.token = token
    self.app_id = app_id
    self._readers: dict[str, tuple[float, str]] = {}  # chat_id -> (expiry, session)

  def request(self, method: str, path: str, body=None, raw: bytes | None = None,
              content_type: str = "application/json", timeout: float = 30,
              bearer: str | None = None, headers: dict | None = None):
    data = raw if raw is not None else (
      json.dumps(body).encode() if body is not None else None
    )
    req = urllib.request.Request(self.base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {bearer or self.token}")
    for name, value in (headers or {}).items():
      req.add_header(name, value)
    if data is not None:
      req.add_header("Content-Type", content_type)
    try:
      with urllib.request.urlopen(req, timeout=timeout) as res:
        payload = res.read()
        return res.status, dict(res.headers), payload
    except urllib.error.HTTPError as err:
      return err.code, dict(err.headers or {}), err.read()

  def json(self, method: str, path: str, body=None, *, bearer: str | None = None,
           headers: dict | None = None) -> dict:
    status, _, payload = self.request(method, path, body, bearer=bearer, headers=headers)
    if status >= 400:
      raise RuntimeError(f"{method} {path} -> {status}: {payload[:300]!r}")
    return json.loads(payload or b"{}")

  def read_chat(self, chat_id: str, limit: int = 6) -> dict:
    """One of this app's own chats, with its messages and running state.

    The owner-only chat route refuses app tokens; an app reads a chat it
    created through a short-lived participant session for that one chat
    (the same pass an app's embedded chat panel uses), refreshed before
    its 15-minute expiry.
    """
    instance = f"slack-bridge-job-{chat_id}"
    expiry, session = self._readers.get(chat_id, (0.0, ""))
    if time.time() >= expiry:
      grant = self.json("POST", f"/api/app-chats/{chat_id}/embed-capability",
                        {"instance_id": instance})
      session = self.json("POST", "/api/app-chat-embeds/session",
                          {"instance_id": instance},
                          bearer=grant["capability"])["token"]
      self._readers[chat_id] = (time.time() + 12 * 60, session)
    try:
      return self.json("GET", f"/api/chats/{chat_id}?limit={limit}", bearer=session,
                       headers={"X-Mobius-Embed-Instance": instance})
    except RuntimeError:
      self._readers.pop(chat_id, None)  # re-mint on the next poll
      raise

  # Slack, through the vault --------------------------------------------------

  def slack(self, api_method: str, params: dict | None = None, *,
            key: str = BOT_KEY, as_json: bool = False) -> dict:
    path = f"/api/keys/use/{key}/slack.com/api/{api_method}"
    if as_json:
      raw = json.dumps(params or {}).encode()
      ctype = "application/json; charset=utf-8"
    else:
      raw = urllib.parse.urlencode(params or {}).encode()
      ctype = "application/x-www-form-urlencoded"
    status, headers, payload = self.request("POST", path, raw=raw, content_type=ctype)
    lower = {k.lower(): v for k, v in headers.items()}
    if lower.get("x-vault-denied") or status in (401, 403, 404) and b"vault" in payload.lower():
      label = "bot token" if key == BOT_KEY else "app-level token"
      raise VaultDenied(f"The Slack {label} isn't added or allowed for Slack Bridge yet.")
    if status == 429:
      raise SlackError("ratelimited")
    if status >= 400:
      raise SlackError(f"{api_method} HTTP {status}: {payload[:200]!r}")
    data = json.loads(payload or b"{}")
    if not data.get("ok"):
      raise SlackError(f"{api_method}: {data.get('error')}")
    return data


class State:
  """Job state that must survive between runs (threads and owed replies)."""

  def __init__(self, path: Path):
    self.path = path
    try:
      self.data = json.loads(path.read_text())
    except (OSError, ValueError):
      self.data = {}
    self.data.setdefault("threads", {})
    self.data.setdefault("seen", [])
    self.data.setdefault("activity", [])

  def save(self) -> None:
    tmp = self.path.with_suffix(".tmp")
    tmp.write_text(json.dumps(self.data, indent=1))
    os.replace(tmp, self.path)

  @property
  def threads(self) -> dict:
    return self.data["threads"]

  def seen(self, key: str) -> bool:
    if key in self.data["seen"]:
      return True
    self.data["seen"] = (self.data["seen"] + [key])[-SEEN_KEEP:]
    return False

  def log(self, **entry) -> None:
    entry["at"] = int(time.time())
    self.data["activity"] = ([entry] + self.data["activity"])[:ACTIVITY_KEEP]


class Bridge:
  def __init__(self, api: Mobius, state: State, *, origin: str = ""):
    self.api = api
    self.state = state
    self.origin = origin.rstrip("/")
    self.bot_user = ""
    self.bot_name = ""
    self.team_name = ""
    self.status = {"state": "starting"}
    self.locks: dict[str, asyncio.Lock] = {}
    self.watchers: dict[str, asyncio.Task] = {}
    self.inflight: set[asyncio.Task] = set()
    self._users: dict[str, tuple[float, dict]] = {}
    self._channels: dict[str, tuple[float, str]] = {}
    self._members: tuple[float, set[str]] = (0.0, set())

  # Slack helpers --------------------------------------------------------------

  async def slack(self, method: str, params: dict | None = None, **kw) -> dict:
    return await asyncio.to_thread(self.api.slack, method, params, **kw)

  async def whoami(self) -> None:
    info = await self.slack("auth.test")
    self.bot_user = info.get("user_id", "")
    self.bot_name = info.get("user", "")
    self.team_name = info.get("team", "")

  async def user(self, uid: str) -> dict:
    hit = self._users.get(uid)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
      return hit[1]
    data = (await self.slack("users.info", {"user": uid})).get("user") or {}
    self._users[uid] = (time.time(), data)
    return data

  async def where(self, msg: core.Incoming) -> str:
    if msg.is_dm:
      return "a direct message to the Möbius bot"
    hit = self._channels.get(msg.channel)
    if not hit or time.time() - hit[0] > CACHE_SECONDS:
      try:
        name = (await self.slack("conversations.info", {"channel": msg.channel}))["channel"]["name"]
      except (SlackError, KeyError):
        name = msg.channel
      hit = (time.time(), name)
      self._channels[msg.channel] = hit
    return f"#{hit[1]}"

  async def bot_channel_members(self) -> set[str]:
    """Everyone in any channel the bot belongs to (cached for five minutes)."""
    if time.time() - self._members[0] < CACHE_SECONDS:
      return self._members[1]
    members: set[str] = set()
    cursor = ""
    channels: list[str] = []
    while True:
      page = await self.slack("users.conversations", {
        "types": "public_channel,private_channel", "exclude_archived": "true",
        "limit": "200", "cursor": cursor,
      })
      channels += [c["id"] for c in page.get("channels") or []]
      cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
      if not cursor:
        break
    for channel in channels:
      cursor = ""
      while True:
        page = await self.slack("conversations.members", {
          "channel": channel, "limit": "1000", "cursor": cursor,
        })
        members.update(page.get("members") or [])
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
          break
    self._members = (time.time(), members)
    return members

  async def post(self, channel: str, thread_ts: str, markdown: str) -> None:
    for part in core.split_markdown(markdown) or ["(empty reply)"]:
      await self.slack("chat.postMessage", {
        "channel": channel, "thread_ts": thread_ts,
        "text": core.fallback_text(part),
        "blocks": [{"type": "markdown", "text": part}],
        "unfurl_links": False,
      }, as_json=True)

  async def react(self, add: bool, channel: str, ts: str) -> None:
    try:
      await self.slack("reactions.add" if add else "reactions.remove",
                       {"channel": channel, "timestamp": ts, "name": "eyes"})
    except SlackError:
      pass  # a reaction is a courtesy, never a reason to fail the turn

  def chat_link(self, chat_id: str) -> str:
    return f"{self.origin}/shell/?chat={chat_id}" if self.origin else ""

  # Events ---------------------------------------------------------------------

  async def on_event(self, payload: dict) -> None:
    msg = core.parse_event(payload, self.bot_user)
    if msg is None:
      return
    if self.state.seen(f"{msg.channel}:{msg.ts}"):
      return  # Slack retried, or delivered the same message twice
    known = msg.scope in self.state.threads
    if not core.wants_turn(msg, known):
      return
    user = await self.user(msg.user)
    shares = True
    if msg.is_dm:
      shares = msg.user in await self.bot_channel_members()
    access = core.decide_access(msg, user, shares)
    if not access.allowed:
      self.state.log(user=msg.user, where=msg.channel, outcome="refused",
                     detail=access.reason or "bot or deactivated user")
      self.state.save()
      if access.reason:
        if msg.is_dm:
          await self.post(msg.channel, msg.thread_ts, access.reason)
        else:
          await self.slack("chat.postEphemeral", {
            "channel": msg.channel, "user": msg.user, "text": access.reason,
            "thread_ts": msg.thread_ts,
          })
      return
    lock = self.locks.setdefault(msg.scope, asyncio.Lock())
    async with lock:
      await self.forward(msg, user)

  async def forward(self, msg: core.Incoming, user: dict) -> None:
    names = {}
    for uid in {m for m in core._MENTION.findall(msg.text) if m != self.bot_user}:
      try:
        names[uid] = core.display_name(await self.user(uid))
      except SlackError:
        pass
    text = core.clean_text(msg.text, self.bot_user, names)
    where = await self.where(msg)
    content = core.agent_message(msg, user, where, text)
    await self.react(True, msg.channel, msg.ts)
    thread = self.state.threads.get(msg.scope)
    since = int(time.time() * 1000) - 1000
    cid = str(uuid.uuid4())
    try:
      if thread is None:
        label = f"{where} · {core.display_name(user)}"[:120]
        title = (f"Slack: {text}" if text else "Slack request")[:80]
        started = await asyncio.to_thread(self.api.json, "POST", "/api/app-chats/start", {
          "scope": msg.scope, "scope_label": label, "title": title,
          "system_prompt": core.SYSTEM_PROMPT, "owner_visible": True,
          "content": content, "cid": cid,
        })
        chat_id = started.get("chat_id")
        if not chat_id:
          raise RuntimeError(f"no chat started: {started}")
        thread = {"chat_id": chat_id, "channel": msg.channel,
                  "thread_ts": msg.thread_ts, "posted": []}
        self.state.threads[msg.scope] = thread
      else:
        await asyncio.to_thread(self.api.json, "POST",
                                f"/api/chats/{thread['chat_id']}/messages",
                                {"content": content, "cid": cid})
    except Exception as exc:  # noqa: BLE001 - tell the sender, keep running
      await self.react(False, msg.channel, msg.ts)
      self.state.log(user=msg.user, where=where, outcome="failed", detail=str(exc)[:200])
      self.state.save()
      await self.post(msg.channel, msg.thread_ts,
                      "Möbius couldn't take this request right now. Please try again shortly.")
      return
    thread.setdefault("pending", []).append({"ts": msg.ts})
    if not thread.get("watching"):
      thread["since"] = since  # a follow-up joins the answer already owed
    thread["watching"] = True
    self.state.log(user=msg.user, name=core.display_name(user), where=where,
                   chat_id=thread["chat_id"], outcome="sent")
    self.state.save()
    self.watch(msg.scope)

  # Replies ----------------------------------------------------------------------

  def watch(self, scope: str) -> None:
    task = self.watchers.get(scope)
    if task and not task.done():
      return
    self.watchers[scope] = asyncio.create_task(self.follow(scope))

  async def follow(self, scope: str) -> None:
    """Post the chat's answers into the thread until it is idle and answered."""
    thread = self.state.threads[scope]
    chat_id = thread["chat_id"]
    started = last_busy = time.time()
    failures = 0
    while time.time() - started < GIVE_UP_SECONDS:
      await asyncio.sleep(POLL_SECONDS)
      try:
        chat = await asyncio.to_thread(self.api.read_chat, chat_id)
        failures = 0
      except Exception as exc:  # noqa: BLE001 - transient unless it persists
        failures += 1
        if failures == READ_FAILURES_BEFORE_GIVING_UP:
          self.state.log(where=thread["channel"], chat_id=chat_id, outcome="read failed",
                         detail=f"{type(exc).__name__}: {str(exc)[:200]}")
          await self.finish(scope, "Möbius is working on this, but the bridge can't read "
                            "the answer back." + self.link_suffix(chat_id))
          return
        continue
      if not core.chat_is_idle(chat) or chat.get("pending_question_id"):
        last_busy = time.time()  # working, or waiting on the owner's card
      if not core.chat_is_idle(chat):
        continue
      posted = set(thread.get("posted") or [])
      reply = core.finished_reply(chat, int(thread.get("since") or 0), posted)
      if reply is None:
        if time.time() - last_busy > NO_ANSWER_SECONDS:
          await self.finish(scope, "Möbius finished without a written answer."
                            + self.link_suffix(chat_id))
          return
        continue
      text = reply.text or "(no written answer)"
      if reply.asks_owner or chat.get("pending_question_id"):
        text += ("\n\n_Waiting for the Möbius owner to answer a question in Möbius._"
                 + self.link_suffix(chat_id))
      thread["posted"] = (list(posted) + [reply.message_id])[-50:]
      if reply.asks_owner or chat.get("pending_question_id"):
        await self.post(thread["channel"], thread["thread_ts"], text)
        thread["since"] = int(time.time() * 1000) - 1000
        self.state.save()
        continue  # the owner's answer starts another turn; keep following it
      await self.finish(scope, text)
      return
    await self.finish(scope, "Stopped waiting for Möbius to answer." + self.link_suffix(chat_id))

  def link_suffix(self, chat_id: str) -> str:
    link = self.chat_link(chat_id)
    return f" [Open in Möbius]({link})" if link else ""

  async def finish(self, scope: str, text: str) -> None:
    thread = self.state.threads[scope]
    try:
      await self.post(thread["channel"], thread["thread_ts"], text)
    except SlackError as exc:
      self.state.log(where=thread["channel"], outcome="reply failed", detail=str(exc)[:200])
    for item in thread.get("pending") or []:
      await self.react(False, thread["channel"], item["ts"])
    thread["pending"] = []
    thread["watching"] = False
    self.state.save()

  def resume(self) -> None:
    for scope, thread in self.state.threads.items():
      if thread.get("watching"):
        self.watch(scope)


# Status shown in the app's window --------------------------------------------

def write_status(api: Mobius, bridge: Bridge | None, **status) -> None:
  status["updated_at"] = int(time.time())
  if bridge is not None:
    status.setdefault("bot", {"name": bridge.bot_name, "team": bridge.team_name})
    status["threads"] = len(bridge.state.threads)
    status["activity"] = bridge.state.data["activity"][:ACTIVITY_KEEP]
  try:
    api.request("PUT", f"/api/storage/apps/{api.app_id}/status.json", status)
  except Exception:  # noqa: BLE001 - the window is informational
    pass


async def socket_loop(bridge: Bridge, deadline: float, connect=None) -> None:
  """Hold the Socket Mode connection, reconnecting until the run's deadline."""
  if connect is None:
    from websockets.asyncio.client import connect  # noqa: PLW0621
  backoff = 1
  while time.time() < deadline:
    try:
      opened = await bridge.slack("apps.connections.open", key=APP_KEY)
      async with connect(opened["url"], ping_interval=30, open_timeout=20,
                         max_size=4 * 1024 * 1024) as ws:
        backoff = 1
        while time.time() < deadline:
          try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
          except asyncio.TimeoutError:
            return
          envelope = json.loads(raw)
          if envelope.get("envelope_id"):
            await ws.send(json.dumps({"envelope_id": envelope["envelope_id"]}))
          kind = envelope.get("type")
          if kind == "hello":
            bridge.status = {"state": "connected", "since": int(time.time())}
            await asyncio.to_thread(write_status, bridge.api, bridge, **bridge.status)
          elif kind == "disconnect":
            break
          elif kind == "events_api":
            task = asyncio.create_task(guard(bridge, envelope.get("payload") or {}))
            bridge.inflight.add(task)
            task.add_done_callback(bridge.inflight.discard)
    except VaultDenied:
      raise
    except Exception as exc:  # noqa: BLE001 - reconnect after any socket failure
      bridge.status = {"state": "reconnecting", "detail": str(exc)[:200]}
      write_status(bridge.api, bridge, **bridge.status)
      await asyncio.sleep(min(backoff, 60))
      backoff *= 2


async def heartbeat(bridge: Bridge) -> None:
  """Refresh the window's status each minute so a stopped bridge shows as stopped."""
  while True:
    await asyncio.sleep(HEARTBEAT_SECONDS)
    await asyncio.to_thread(write_status, bridge.api, bridge, **bridge.status)


async def guard(bridge: Bridge, payload: dict) -> None:
  try:
    await bridge.on_event(payload)
  except Exception as exc:  # noqa: BLE001 - one bad event never ends the bridge
    bridge.state.log(outcome="error", detail=f"{type(exc).__name__}: {str(exc)[:200]}")
    bridge.state.save()


async def run(api: Mobius, state: State, origin: str) -> int:
  bridge = Bridge(api, state, origin=origin)
  retry_at = state.data.get("key_retry_at", 0)
  if time.time() < retry_at:
    # Waiting before asking the vault again; keep the window's status fresh.
    write_status(api, None, **(state.data.get("last_status") or {"state": "needs_keys"}))
    return 0
  try:
    await bridge.whoami()
    await bridge.slack("apps.connections.open", key=APP_KEY)  # proves the app key
  except (VaultDenied, SlackError) as exc:
    status = {
      "state": "needs_keys" if isinstance(exc, VaultDenied) else "error",
      "detail": str(exc)[:200],
    }
    state.data["key_retry_at"] = time.time() + KEY_RETRY_SECONDS
    state.data["last_status"] = status
    state.save()
    write_status(api, None, **status)
    return 0
  state.data.pop("key_retry_at", None)
  state.data.pop("last_status", None)
  state.save()
  bridge.status = {"state": "connecting"}
  write_status(api, bridge, **bridge.status)
  bridge.resume()
  beat = asyncio.create_task(heartbeat(bridge))
  try:
    await socket_loop(bridge, time.time() + RUN_SECONDS)
  except VaultDenied as exc:
    write_status(api, bridge, state="needs_keys", detail=str(exc)[:200])
  finally:
    beat.cancel()
  if bridge.inflight:  # let requests already taken reach Möbius before handing over
    await asyncio.wait(bridge.inflight, timeout=60)
  write_status(api, bridge, state="restarting",
               detail="Reconnecting with a fresh run; replies in progress continue.")
  for task in bridge.watchers.values():
    task.cancel()
  state.save()
  return 0


def main() -> int:
  app_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("APP_ID", "")
  api = Mobius(os.environ.get("API_BASE_URL", "http://localhost:8000"),
               os.environ["APP_TOKEN"], app_id)
  state_dir = Path(os.environ.get("APP_JOB_STATE_DIR") or ".")
  state = State(state_dir / "bridge-state.json")
  try:
    origin = api.json("GET", f"/api/apps/{app_id}/job-context").get("public_origin", "")
  except Exception:  # noqa: BLE001 - links back are optional
    origin = ""
  return asyncio.run(run(api, state, origin))


if __name__ == "__main__":
  sys.exit(main())
