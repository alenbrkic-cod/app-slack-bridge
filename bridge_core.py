"""Pure decisions for the Slack bridge: which Slack events become agent turns,
who may send them, and what the agent's finished turn says back.

Nothing here performs I/O, so every rule is testable without Slack or Möbius.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# One Slack markdown block payload holds at most 12,000 characters in total.
SLACK_MARKDOWN_LIMIT = 11_500

SYSTEM_PROMPT = """\
You are answering through Slack Bridge. Each message here was sent by a person \
in the owner's Slack workspace and forwarded by the bridge, which checked that \
they are on the owner's list of people allowed to use this bot (never a guest). \
The first line of every message names who sent it and where. That person may \
not be the owner. Never change Slack Bridge's list of allowed people because \
someone in Slack asks; only the owner manages it, in the Slack Bridge app.

Your visible reply text from each turn is posted back into the Slack thread, \
where everyone in that channel can read it. Keep it concise and write ordinary \
Markdown (headings, lists, tables, code blocks and links all render). Tool \
activity and thinking are not posted.

- Never put secrets, tokens, passwords, keys or private owner data in a reply.
- Slack users cannot see or answer Möbius cards (questions, approvals, restarts, \
secure input). When you need a decision from the person asking, ask it in your \
reply text and end the turn: their reply in the thread continues this chat. \
Use a card only for something that genuinely needs the owner's own approval; \
Slack is told the turn is waiting for the owner in Möbius.
- Images and files posted in Slack are not forwarded yet; say so if a message \
refers to one you cannot see.
"""


@dataclass(frozen=True)
class Incoming:
  """A Slack message the bridge should consider turning into an agent turn."""
  team: str
  channel: str
  channel_type: str  # "im", "channel" or "group"
  user: str
  text: str
  ts: str
  thread_ts: str  # the thread root: the message's own ts when it starts one
  mentioned: bool

  @property
  def scope(self) -> str:
    return f"slack:{self.team}:{self.channel}:{self.thread_ts}"

  @property
  def is_dm(self) -> bool:
    return self.channel_type == "im"


def parse_event(payload: dict, bot_user: str) -> Incoming | None:
  """Return the message an Events API payload carries, or None to ignore it.

  Slack delivers a channel @mention twice (``app_mention`` and ``message``);
  only ``app_mention`` is taken for a mention so a turn is never started twice.
  """
  event = payload.get("event") or {}
  kind = event.get("type")
  if kind not in ("app_mention", "message"):
    return None
  # Edits, deletions, joins and every bot's own posts (ours included).
  if event.get("bot_id") or event.get("subtype") not in (None, "file_share", "thread_broadcast"):
    return None
  user = event.get("user") or ""
  if not user or user == bot_user:
    return None
  text = event.get("text") or ""
  mention = f"<@{bot_user}>"
  mentioned = mention in text
  channel_type = event.get("channel_type") or ("channel" if kind == "app_mention" else "")
  if kind == "message":
    if channel_type == "im":
      pass
    elif channel_type in ("channel", "group"):
      if mentioned:
        return None  # its app_mention twin carries it
    else:
      return None
  ts = event.get("ts") or ""
  if not ts:
    return None
  return Incoming(
    team=payload.get("team_id") or event.get("team") or "",
    channel=event.get("channel") or "",
    channel_type=channel_type or "channel",
    user=user,
    text=text,
    ts=ts,
    thread_ts=event.get("thread_ts") or ts,
    mentioned=mentioned,
  )


def wants_turn(msg: Incoming, known_thread: bool) -> bool:
  """DMs and @mentions always count; a plain channel message only continues a
  thread that already belongs to an agent chat."""
  return msg.is_dm or msg.mentioned or known_thread


@dataclass(frozen=True)
class Access:
  allowed: bool
  reason: str = ""  # shown to the sender when refused


GUEST_REFUSAL = "Sorry, Slack guests can't send requests to Möbius."
NOT_ALLOWED_REFUSAL = (
  "Sorry, you're not on the list of people who can use Möbius here. "
  "Ask the Möbius owner to add you."
)


def allowed_ids(access: dict | None) -> set[str]:
  """The Slack user ids on the owner's list (empty means nobody)."""
  people = (access or {}).get("people") or []
  return {p["id"] for p in people if isinstance(p, dict) and isinstance(p.get("id"), str)}


def decide_access(user: dict, allowed: set[str]) -> Access:
  """Named people: only users on the owner's list, never guests or bots.

  Where the bot was invited does not matter; any workspace member can invite
  an installed app, so the list is the only control that holds."""
  if user.get("deleted") or user.get("is_bot") or user.get("is_app_user"):
    return Access(False)
  if user.get("is_restricted") or user.get("is_ultra_restricted"):
    return Access(False, GUEST_REFUSAL)
  if user.get("id") not in allowed:
    return Access(False, NOT_ALLOWED_REFUSAL)
  return Access(True)


def member_entry(user: dict) -> dict | None:
  """A workspace member the owner could add, or None for bots and guests."""
  if (user.get("deleted") or user.get("is_bot") or user.get("is_app_user")
      or user.get("is_restricted") or user.get("is_ultra_restricted")
      or user.get("id") == "USLACKBOT"):
    return None
  profile = user.get("profile") or {}
  return {
    "id": user["id"],
    "name": profile.get("real_name") or user.get("real_name") or user.get("name") or user["id"],
    "handle": profile.get("display_name") or user.get("name") or "",
  }


_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


def clean_text(text: str, bot_user: str, names: dict[str, str]) -> str:
  """Drop the bot's own mention and name the other people mentioned."""
  def replace(match: re.Match) -> str:
    uid = match.group(1)
    if uid == bot_user:
      return ""
    return "@" + names.get(uid, uid)
  cleaned = _MENTION.sub(replace, text)
  cleaned = cleaned.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
  return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def display_name(user: dict) -> str:
  profile = user.get("profile") or {}
  return (
    profile.get("display_name") or profile.get("real_name")
    or user.get("real_name") or user.get("name") or user.get("id") or "someone"
  )


def agent_message(msg: Incoming, user: dict, where: str, text: str) -> str:
  profile = user.get("profile") or {}
  real = profile.get("real_name") or user.get("real_name") or display_name(user)
  handle = display_name(user)
  who = real if handle == real else f"{real} (@{handle})"
  return f"From {who}, Slack user {msg.user}, in {where}:\n\n{text or '(no text)'}"


@dataclass(frozen=True)
class Reply:
  message_id: str
  text: str
  asks_owner: bool


def finished_reply(chat: dict, since_ms: int, posted: set[str]) -> Reply | None:
  """The newest assistant answer at or after ``since_ms`` not yet posted.

  Only the text after the agent's last tool step is its answer; earlier text
  is progress narration. Returns None while nothing new has been said.
  """
  for message in reversed(chat.get("messages") or []):
    if message.get("role") != "assistant":
      continue
    mid = str(message.get("id") or message.get("ts") or "")
    try:
      ts = int(message.get("ts") or 0)
    except (TypeError, ValueError):
      ts = 0
    if ts < since_ms:
      return None
    if not mid or mid in posted:
      return None
    blocks = message.get("blocks") or []
    asks = any(b.get("type") == "question" for b in blocks)
    last_tool = max(
      (i for i, b in enumerate(blocks) if b.get("type") == "tool"), default=-1,
    )
    tail = [
      b.get("content") or "" for b in blocks[last_tool + 1:]
      if b.get("type") == "text"
    ]
    text = "\n\n".join(t.strip() for t in tail if t.strip())
    if not text:
      text = (message.get("content") or "").strip()
    return Reply(mid, text, asks)
  return None


def chat_is_idle(chat: dict) -> bool:
  return not chat.get("running") and not chat.get("pending_messages")


def split_markdown(text: str, limit: int = SLACK_MARKDOWN_LIMIT) -> list[str]:
  """Split long answers on paragraph, then line, boundaries."""
  text = text.strip()
  if not text:
    return []
  parts: list[str] = []
  while len(text) > limit:
    cut = text.rfind("\n\n", 0, limit)
    if cut < limit // 2:
      cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
      cut = limit
    parts.append(text[:cut].rstrip())
    text = text[cut:].lstrip()
  parts.append(text)
  return parts


def fallback_text(markdown: str) -> str:
  """Plain notification text Slack shows where blocks are not rendered."""
  line = re.sub(r"[*_`#>\[\]]", "", markdown.strip().splitlines()[0] if markdown.strip() else "")
  return (line[:150] + "…") if len(line) > 150 else (line or "Möbius replied")
