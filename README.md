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

Only the people on the owner's list, managed in the app window. They can use
the bot from any channel or by DM. Everyone else is privately turned away and
shows up under Recent requests with an **Allow** button. Guests and other bots
are always refused. With no list saved, nobody is answered.

The list, not channel membership, is the control: on standard Slack plans any
workspace member can invite an installed app to their channels. The bridge
reads the list for every message, so removing someone takes effect on their
next message. Each request runs with the owner's full access to this Möbius,
so list only people you trust with that.

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
