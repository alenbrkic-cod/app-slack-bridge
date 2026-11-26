# Slack Bridge

Use your Möbius from Slack. @mention the bot in a channel or send it a direct
message. The Möbius agent works on your server and answers in the Slack thread.
Replies in that thread continue the same conversation.

## How it works

- A background job keeps one Slack **Socket Mode** connection open. Your Möbius
  needs no public webhook address. Möbius starts the job every minute and only
  ever runs one copy, so the bridge comes back by itself after a restart.
- Each Slack thread becomes one agent chat on this Möbius, visible in your chat
  list. The person, channel and message are written at the top of each turn.
- The agent's closing answer is posted back to the thread as Slack Markdown. If
  the agent needs the owner's own decision (a card), Slack gets a link to open
  it in Möbius.

## Who can use it

Only people the owner has allowed. Anyone else who @mentions or DMs the bot
becomes an **access request** in the app window, and the owner gets one push
notification per new person. The bot doesn't reply to them. Their message just
gets a ⏳ reaction. The owner taps **Allow** or **Block**:

- Allowed people get a direct message from the bot saying they now have access,
  and can then use it from any channel or by DM, until blocked.
- Blocked people get no reply and create no new requests, until allowed. The ⏳
  is removed.
- Guests and other bots are always ignored. Until the owner allows someone,
  nobody is answered, the owner included: try the bot once and allow yourself.

The owner's per-person decision, not channel membership, is the control: on
standard Slack plans any workspace member can invite an installed app to their
channels. The bridge reads the decisions for every message, so a change takes
effect on the person's next message. If they can't be read, nobody gets in.
Each request runs with the owner's full access to this Möbius, so allow only
people you trust with that.

## Setup

1. In your Slack app: turn on Socket Mode, create an app-level token with
   `connections:write`, add the bot scopes and events listed in the app window,
   turn on the App Home Messages tab, and reinstall the app.
2. Open Slack Bridge in Möbius and tap **Add Slack tokens**. Paste the bot token
   (`xoxb-…`) and the app-level token (`xapp-…`) into the key vault prompts.
   The app itself never sees them.
3. Invite the bot to a channel (`/invite @YourApp`) and @mention it.

Only one Möbius should hold a given Slack app's tokens. Slack spreads events
across every open connection.

## Development

```bash
python3 -m pytest -q tests
```
