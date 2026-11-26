import React, { useCallback, useEffect, useState } from 'react'
import {
  CheckCircleFilled, Copy, ExclamationMarkCircle, Key, Reload, Chat,
} from '@openai/apps-sdk-ui/components/Icon'
import { People, PEOPLE_CSS, usePeople } from './People.jsx'

const BOT_KEY = 'slack-bot-token'
const APP_KEY = 'slack-app-token'
const KEY_REQUESTS = [
  {
    key: BOT_KEY,
    label: 'Bot token',
    hint: 'Starts with xoxb-. Slack › OAuth & Permissions › Bot User OAuth Token.',
    reason: 'Reads and answers Slack messages sent to the Möbius bot.',
    sites: [{ host: 'slack.com', methods: ['POST'], paths: ['/api/*'] }],
  },
  {
    key: APP_KEY,
    label: 'App-level token',
    hint: 'Starts with xapp-. Slack › Basic Information › App-Level Tokens (scope connections:write).',
    reason: 'Opens the live Slack connection, with no public address needed.',
    sites: [{ host: 'slack.com', methods: ['POST'], paths: ['/api/apps.connections.open'] }],
  },
]
const BOT_SCOPES = [
  'app_mentions:read', 'chat:write', 'im:history', 'channels:history',
  'groups:history', 'channels:read', 'groups:read', 'users:read', 'reactions:write',
]
const BOT_EVENTS = ['app_mention', 'message.im', 'message.channels', 'message.groups']
const STALE_SECONDS = 180

const STATES = {
  connected: { tone: 'ok', title: 'Connected to Slack' },
  connecting: { tone: 'busy', title: 'Connecting to Slack…' },
  reconnecting: { tone: 'busy', title: 'Reconnecting to Slack…' },
  restarting: { tone: 'busy', title: 'Refreshing the connection…' },
  starting: { tone: 'busy', title: 'Starting…' },
  needs_keys: { tone: 'warn', title: 'Waiting for Slack keys' },
  error: { tone: 'bad', title: 'Slack refused the connection' },
  offline: { tone: 'warn', title: 'Not running' },
}

const CSS = `
  * { box-sizing: border-box; }
  .sb-root { min-height: 100%; background: var(--bg); color: var(--text); font-family: var(--font);
    padding: 24px 16px 48px; }
  .sb-wrap { max-width: 640px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }
  .sb-hero { display: flex; gap: 16px; align-items: center; padding: 20px; border-radius: 16px;
    background: var(--surface); border: 1px solid var(--border); }
  .sb-orb { width: 48px; height: 48px; border-radius: 50%; flex: none; position: relative;
    background: color-mix(in srgb, var(--sb-tone) 18%, transparent); }
  .sb-orb::after { content: ''; position: absolute; inset: 15px; border-radius: 50%; background: var(--sb-tone); }
  .sb-orb.busy::before { content: ''; position: absolute; inset: 0; border-radius: 50%;
    border: 2px solid var(--sb-tone); opacity: .5; animation: sb-pulse 1.6s ease-out infinite; }
  @keyframes sb-pulse { from { transform: scale(.7); opacity: .7 } to { transform: scale(1.15); opacity: 0 } }
  .tone-ok { --sb-tone: var(--green); } .tone-busy { --sb-tone: var(--accent); }
  .tone-warn { --sb-tone: var(--warning); } .tone-bad { --sb-tone: var(--danger); }
  .sb-hero h1 { font-size: 19px; margin: 0 0 4px; font-weight: 650; }
  .sb-sub { color: var(--muted); font-size: 14px; line-height: 1.45; margin: 0; }
  .sb-version { color: var(--muted); font-size: 12.5px; margin: 6px 0 0; }
  .sb-card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 18px 20px; }
  .sb-card h2 { font-size: 15px; margin: 0 0 12px; font-weight: 650; }
  .sb-row { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-top: 1px solid var(--border); }
  .sb-row:first-of-type { border-top: 0; }
  .sb-row-main { flex: 1; min-width: 0; }
  .sb-row-title { font-size: 14px; font-weight: 600; }
  .sb-row-hint { font-size: 12.5px; color: var(--muted); margin-top: 2px; line-height: 1.4; }
  .sb-badge { font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 99px; white-space: nowrap;
    background: var(--surface-2); color: var(--muted); }
  .sb-badge.ok { color: var(--green-text); background: color-mix(in srgb, var(--green) 14%, transparent); }
  .sb-badge.warn { color: var(--warning-text); background: color-mix(in srgb, var(--warning) 14%, transparent); }
  .sb-badge.bad { color: var(--danger-text); background: color-mix(in srgb, var(--danger) 14%, transparent); }
  .sb-btn { min-height: 44px; padding: 0 18px; border-radius: 12px; border: 0; font: inherit; font-size: 15px;
    font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 8px;
    background: var(--accent-fill); color: var(--accent-fg); }
  .sb-btn:hover { background: var(--accent-fill-hover); }
  .sb-btn.quiet { background: var(--surface-2); color: var(--text); }
  .sb-btn:focus-visible, .sb-link:focus-visible, .sb-copy:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .sb-actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
  .sb-steps { margin: 0; padding-left: 20px; display: flex; flex-direction: column; gap: 10px;
    font-size: 14px; line-height: 1.5; }
  .sb-steps b { font-weight: 600; }
  .sb-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; align-items: center; }
  .sb-chip { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px;
    background: var(--surface-2); border-radius: 6px; padding: 2px 7px; }
  .sb-copy { min-height: 32px; border: 1px solid var(--border); background: transparent; color: var(--accent-text);
    border-radius: 8px; font: inherit; font-size: 12.5px; padding: 0 10px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 4px; }
  .sb-empty { color: var(--muted); font-size: 14px; margin: 0; }
  .sb-link { border: 0; background: none; color: var(--accent-text); font: inherit; font-size: 13px;
    cursor: pointer; padding: 6px 0; min-height: 44px; display: inline-flex; align-items: center; gap: 4px; }
  .sb-note { font-size: 13px; color: var(--muted); line-height: 1.5; margin: 12px 0 0; }
  @media (prefers-reduced-motion: reduce) { .sb-orb.busy::before { animation: none; opacity: .4; } }
`

function api(token, path, options = {}) {
  return fetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
}

function ago(seconds) {
  if (!seconds) return ''
  const diff = Math.max(0, Date.now() / 1000 - seconds)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  if (diff < 60) return rtf.format(-Math.round(diff), 'second')
  if (diff < 3600) return rtf.format(-Math.round(diff / 60), 'minute')
  if (diff < 86400) return rtf.format(-Math.round(diff / 3600), 'hour')
  return rtf.format(-Math.round(diff / 86400), 'day')
}

function CopyList({ items }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    const ok = await window.mobius.clipboard.writeText(items.join('\n'))
    setCopied(ok)
    if (ok) setTimeout(() => setCopied(false), 1600)
  }
  return (
    <div className="sb-chips">
      {items.map((s) => <span key={s} className="sb-chip">{s}</span>)}
      <button type="button" className="sb-copy" onClick={copy} aria-label="Copy list">
        <Copy width={14} height={14} />{copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}

export default function App({ token }) {
  const [status, setStatus] = useState(null)
  const [keys, setKeys] = useState({})
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState('')
  const people = usePeople()
  const reloadPeople = people.reload

  const load = useCallback(async () => {
    try {
      const value = await window.mobius.storage.get('status.json')
      setStatus(value || null)
      setError('')
    } catch {
      setError('Couldn’t read the bridge status. It will retry.')
    }
    reloadPeople()
  }, [reloadPeople])

  const askForKeys = useCallback(async () => {
    setAsking(true)
    setError('')
    const next = {}
    for (const request of KEY_REQUESTS) {
      try {
        const res = await api(token, '/api/keys/requests', {
          method: 'POST',
          body: JSON.stringify({
            key: request.key, reason: request.reason, sites: request.sites,
            inject: { type: 'header', name: 'Authorization', prefix: 'Bearer ' },
          }),
        })
        const body = await res.json().catch(() => ({}))
        next[request.key] = res.ok ? body.status : 'error'
      } catch {
        next[request.key] = 'error'
      }
    }
    setKeys(next)
    setAsking(false)
    try { await window.mobius.storage.set('keys-asked.json', { at: Date.now() }) } catch { /* only a hint */ }
  }, [token])

  useEffect(() => {
    load()
    window.mobius.storage.get('keys-asked.json').then((asked) => { if (asked) askForKeys() }).catch(() => {})
    const timer = setInterval(() => { if (window.mobius.visible !== false) load() }, 10000)
    const off = window.mobius.onVisibilityChange?.((visible) => { if (visible) load() })
    return () => { clearInterval(timer); off?.() }
  }, [load, askForKeys])

  const fresh = status && Date.now() / 1000 - (status.updated_at || 0) < STALE_SECONDS
  // The bridge rewrites its status at least once a minute; stale means stopped.
  let stateName = status?.state || 'offline'
  if (!fresh) stateName = 'offline'
  const view = STATES[stateName] || STATES.offline
  const keysReady = KEY_REQUESTS.every((r) => keys[r.key] === 'granted') || stateName === 'connected'
  const activity = status?.activity || []

  let sub = 'Möbius checks for the connection every minute.'
  if (stateName === 'connected') {
    sub = `Answering as @${status.bot?.name || 'bot'} in ${status.bot?.team || 'your workspace'}${status.since ? `, since ${ago(status.since)}` : ''}.`
  } else if (stateName === 'needs_keys') {
    sub = 'Add the two Slack tokens below. The bridge connects within a few minutes of approval.'
  } else if (stateName === 'error') {
    sub = status?.detail || 'Check the Slack app settings below.'
  } else if (stateName === 'offline') {
    sub = 'The bridge starts within a minute of installing. If this stays, check the steps below.'
  }

  const badge = (value) => {
    if (value === 'granted') return <span className="sb-badge ok">Allowed</span>
    if (value === 'pending') return <span className="sb-badge warn">Waiting for you</span>
    if (value === 'denied') return <span className="sb-badge bad">Declined</span>
    if (value === 'paused') return <span className="sb-badge warn">Paused</span>
    if (value === 'error') return <span className="sb-badge bad">Couldn’t ask</span>
    return <span className="sb-badge">Not added</span>
  }

  return (
    <div className="sb-root">
      <style>{CSS + PEOPLE_CSS}</style>
      <div className="sb-wrap">
        <section className={`sb-hero tone-${view.tone}`} aria-live="polite">
          <div className={`sb-orb ${view.tone === 'busy' ? 'busy' : ''}`} aria-hidden="true" />
          <div>
            <h1>{view.title}</h1>
            <p className="sb-sub">{sub}</p>
            {status?.version && <p className="sb-version">Running version {status.version}</p>}
          </div>
        </section>

        {error && <p className="sb-note" role="alert">{error}</p>}

        <section className="sb-card">
          <h2>Who can use it</h2>
          <p className="sb-sub" style={{ marginBottom: 12 }}>
            Anyone who tries the bot in Slack shows up here as a request. Allowed people can
            use it from any channel or by direct message, and each request runs as you with
            full access to this Möbius. Everyone else gets no answer; their message just
            shows ⏳ until you decide.
          </p>
          <People state={people} />
        </section>

        <section className="sb-card">
          <h2>Slack tokens</h2>
          {KEY_REQUESTS.map((r) => (
            <div className="sb-row" key={r.key}>
              <Key width={20} height={20} aria-hidden="true" />
              <div className="sb-row-main">
                <div className="sb-row-title">{r.label}</div>
                <div className="sb-row-hint">{r.hint}</div>
              </div>
              {badge(stateName === 'connected' ? 'granted' : keys[r.key])}
            </div>
          ))}
          {!keysReady && (
            <div className="sb-actions">
              <button type="button" className="sb-btn" onClick={askForKeys} disabled={asking}>
                <Key width={18} height={18} />{asking ? 'Asking…' : 'Add Slack tokens'}
              </button>
            </div>
          )}
          <p className="sb-note">
            Tokens go into your key vault and are only ever sent to slack.com. This app never sees them.
          </p>
        </section>

        <section className="sb-card">
          <h2>Slack app settings</h2>
          <ol className="sb-steps">
            <li><b>Socket Mode</b>: turn it on, and create an app-level token with <span className="sb-chip">connections:write</span>.</li>
            <li><b>OAuth &amp; Permissions</b>: add these bot token scopes.
              <CopyList items={BOT_SCOPES} />
            </li>
            <li><b>Event Subscriptions</b>: turn on, then under bot events add:
              <CopyList items={BOT_EVENTS} />
            </li>
            <li><b>App Home</b>: turn on the Messages tab and allow people to send messages to the app.</li>
            <li><b>Reinstall</b> the app to the workspace, then copy the Bot User OAuth Token.</li>
            <li>In Slack, <b>invite the bot</b> to the channels where you want to use it: <span className="sb-chip">/invite @Möbius</span>. Only people you allow above get answers there.</li>
          </ol>
        </section>

        <section className="sb-card">
          <h2>Recent requests</h2>
          {activity.length === 0 && <p className="sb-empty">Nothing yet. @mention the bot in Slack to try it.</p>}
          {activity.slice(0, 12).map((a, i) => (
            <div className="sb-row" key={`${a.at}-${i}`}>
              {a.outcome === 'sent'
                ? <CheckCircleFilled width={20} height={20} aria-hidden="true" />
                : <ExclamationMarkCircle width={20} height={20} aria-hidden="true" />}
              <div className="sb-row-main">
                <div className="sb-row-title">{a.name || a.user || 'Slack'} · {a.where || ''}</div>
                <div className="sb-row-hint">
                  {a.outcome === 'sent' ? 'Sent to Möbius' : a.outcome === 'refused' ? `Turned away (${a.detail || 'not allowed'})` : `${a.outcome}${a.detail ? `: ${a.detail}` : ''}`} · {ago(a.at)}
                </div>
              </div>
              {a.chat_id && (
                <button type="button" className="sb-link"
                  onClick={() => window.parent.postMessage({ type: 'moebius:open-chat', chatId: a.chat_id }, '*')}>
                  <Chat width={16} height={16} />Open
                </button>
              )}
            </div>
          ))}
          <div className="sb-actions">
            <button type="button" className="sb-btn quiet" onClick={load}><Reload width={16} height={16} />Refresh</button>
          </div>
        </section>
      </div>
    </div>
  )
}
