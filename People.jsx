import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Check, MinusCircle, UserLock } from '@openai/apps-sdk-ui/components/Icon'

// Access is decided per person. The bridge job writes requests.json (people
// who tried the bot); this window writes only access.json (the owner's
// decisions), so the two never overwrite each other. The job reads
// access.json for every message, so a decision applies to the very next one.
const ACCESS = 'access.json'
const REQUESTS = 'requests.json'

export const PEOPLE_CSS = `
  .sbp-group + .sbp-group { margin-top: 18px; }
  .sbp-label { font-size: 12.5px; font-weight: 650; color: var(--muted); text-transform: uppercase;
    letter-spacing: .04em; margin: 0 0 4px; display: flex; align-items: center; gap: 8px; }
  .sbp-count { min-width: 20px; height: 20px; padding: 0 6px; border-radius: 10px; font-size: 12px;
    display: inline-grid; place-items: center; background: var(--accent-fill); color: var(--accent-fg);
    letter-spacing: 0; }
  .sbp-person { display: flex; align-items: center; gap: 12px; padding: 8px 0; border-top: 1px solid var(--border); }
  .sbp-person:first-of-type { border-top: 0; }
  .sbp-avatar { width: 32px; height: 32px; border-radius: 50%; flex: none; display: grid; place-items: center;
    background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent-text);
    font-size: 13px; font-weight: 650; }
  .sbp-request .sbp-avatar { background: color-mix(in srgb, var(--warning) 18%, transparent); color: var(--warning-text); }
  .sbp-blocked .sbp-avatar { background: var(--surface-2); color: var(--muted); }
  .sbp-main { flex: 1; min-width: 0; }
  .sbp-name { font-size: 14px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sbp-meta { font-size: 12.5px; color: var(--muted); }
  .sbp-actions { display: flex; gap: 6px; flex: none; }
  .sbp-btn { min-height: 44px; padding: 0 12px; border-radius: 10px; border: 1px solid var(--border);
    background: transparent; color: var(--text); font: inherit; font-size: 13.5px; font-weight: 600;
    cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
  .sbp-btn.allow { background: var(--accent-fill); border-color: transparent; color: var(--accent-fg); }
  .sbp-btn.allow:hover { background: var(--accent-fill-hover); }
  .sbp-btn.block:hover { color: var(--danger-text); border-color: color-mix(in srgb, var(--danger) 40%, var(--border)); }
  .sbp-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .sbp-btn:disabled { opacity: .5; cursor: default; }
  .sbp-empty { display: flex; gap: 10px; align-items: flex-start; padding: 12px; border-radius: 12px;
    background: color-mix(in srgb, var(--warning) 10%, transparent); font-size: 14px; line-height: 1.45; }
  .sbp-quiet { font-size: 13.5px; color: var(--muted); margin: 4px 0 0; }
  .sbp-error { color: var(--danger-text); font-size: 13px; margin-top: 8px; }
  @media (max-width: 420px) { .sbp-btn span { display: none; } .sbp-btn { padding: 0 12px; } }
`

function initials(name) {
  return (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase()
}

function ago(seconds) {
  if (!seconds) return ''
  const diff = Math.max(0, Date.now() / 1000 - seconds)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  if (diff < 3600) return rtf.format(-Math.max(1, Math.round(diff / 60)), 'minute')
  if (diff < 86400) return rtf.format(-Math.round(diff / 3600), 'hour')
  return rtf.format(-Math.round(diff / 86400), 'day')
}

function normalize(saved) {
  // Version 0.2.0 kept the allowed people under "people".
  const allowed = saved?.allowed || saved?.people || []
  return { allowed: Array.isArray(allowed) ? allowed : [], blocked: Array.isArray(saved?.blocked) ? saved.blocked : [] }
}

function entry(p) {
  return { id: p.id, name: p.name || p.id, handle: p.handle || '' }
}

export function usePeople() {
  const [access, setAccess] = useState({ allowed: [], blocked: [] })
  const [requests, setRequests] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    try {
      const [saved, asked] = await Promise.all([
        window.mobius.storage.get(ACCESS),
        window.mobius.storage.get(REQUESTS),
      ])
      setAccess(normalize(saved))
      setRequests(Array.isArray(asked?.requests) ? asked.requests : [])
    } catch {
      setError('Couldn’t load who has access. Try again in a moment.')
    }
  }, [])

  useEffect(() => { reload() }, [reload])

  const decide = useCallback(async (person, verdict) => {
    setBusy(true)
    setError('')
    const previous = access
    const others = (list) => list.filter((p) => p.id !== person.id)
    const next = {
      allowed: verdict === 'allow' ? [...others(access.allowed), entry(person)] : others(access.allowed),
      blocked: verdict === 'block' ? [...others(access.blocked), entry(person)] : others(access.blocked),
    }
    setAccess(next)
    try {
      await window.mobius.storage.set(ACCESS, { ...next, updated_at: Date.now() })
    } catch {
      setAccess(previous)
      setError('Couldn’t save that. Nothing was changed.')
    }
    setBusy(false)
  }, [access])

  const decided = useMemo(
    () => new Set([...access.allowed, ...access.blocked].map((p) => p.id)), [access])
  const pending = useMemo(
    () => requests.filter((r) => !decided.has(r.id)).sort((a, b) => (b.last_at || 0) - (a.last_at || 0)),
    [requests, decided])

  return { ...access, pending, decide, busy, reload, error }
}

function Row({ person, kind, children, meta }) {
  return (
    <div className={`sbp-person sbp-${kind}`}>
      <span className="sbp-avatar" aria-hidden="true">{initials(person.name)}</span>
      <div className="sbp-main">
        <div className="sbp-name">{person.name}</div>
        <div className="sbp-meta">{meta}</div>
      </div>
      <div className="sbp-actions">{children}</div>
    </div>
  )
}

export function People({ state }) {
  const { allowed, blocked, pending, decide, busy, error } = state
  const handle = (p) => (p.handle ? `@${p.handle}` : p.id)

  const allowBtn = (p) => (
    <button type="button" className="sbp-btn allow" disabled={busy} onClick={() => decide(p, 'allow')}
      aria-label={`Allow ${p.name}`}>
      <Check width={16} height={16} /><span>Allow</span>
    </button>
  )
  const blockBtn = (p) => (
    <button type="button" className="sbp-btn block" disabled={busy} onClick={() => decide(p, 'block')}
      aria-label={`Block ${p.name}`}>
      <MinusCircle width={16} height={16} /><span>Block</span>
    </button>
  )

  return (
    <>
      {pending.length > 0 && (
        <div className="sbp-group" aria-label="Access requests">
          <p className="sbp-label">Requests <span className="sbp-count">{pending.length}</span></p>
          {pending.map((r) => (
            <Row key={r.id} person={r} kind="request"
              meta={`${handle(r)} · tried ${r.count > 1 ? `${r.count} times, last ` : ''}${ago(r.last_at)} in ${r.where || 'Slack'}`}>
              {allowBtn(r)}{blockBtn(r)}
            </Row>
          ))}
        </div>
      )}

      <div className="sbp-group" aria-label="Allowed people">
        <p className="sbp-label">Allowed</p>
        {allowed.length === 0 ? (
          pending.length === 0 ? (
            <div className="sbp-empty">
              <UserLock width={20} height={20} aria-hidden="true" />
              <span>Nobody can use the bot yet. @mention or DM it once in Slack yourself, and your request appears here to allow.</span>
            </div>
          ) : <p className="sbp-quiet">Nobody yet.</p>
        ) : allowed.map((p) => (
          <Row key={p.id} person={p} kind="allowed" meta={handle(p)}>{blockBtn(p)}</Row>
        ))}
      </div>

      {blocked.length > 0 && (
        <div className="sbp-group" aria-label="Blocked people">
          <p className="sbp-label">Blocked</p>
          {blocked.map((p) => (
            <Row key={p.id} person={p} kind="blocked" meta={`${handle(p)} · gets no access and no new requests`}>
              {allowBtn(p)}
            </Row>
          ))}
        </div>
      )}
      {error && <p className="sbp-error" role="alert">{error}</p>}
    </>
  )
}
