import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Plus, Search, Trash, UserLock } from '@openai/apps-sdk-ui/components/Icon'

// The owner's list of Slack people the bot answers. The bridge job reads this
// file for every message, so a change applies to the very next request.
const ACCESS = 'access.json'
const SLACK_ID = /^[UW][A-Z0-9]{6,}$/

export const PEOPLE_CSS = `
  .sbp-list { display: flex; flex-direction: column; }
  .sbp-person { display: flex; align-items: center; gap: 12px; padding: 8px 0; border-top: 1px solid var(--border); }
  .sbp-person:first-child { border-top: 0; }
  .sbp-avatar { width: 32px; height: 32px; border-radius: 50%; flex: none; display: grid; place-items: center;
    background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent-text);
    font-size: 13px; font-weight: 650; }
  .sbp-main { flex: 1; min-width: 0; }
  .sbp-name { font-size: 14px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sbp-meta { font-size: 12.5px; color: var(--muted); }
  .sbp-icon-btn { width: 44px; height: 44px; border-radius: 10px; border: 0; background: transparent;
    color: var(--muted); cursor: pointer; display: grid; place-items: center; flex: none; }
  .sbp-icon-btn:hover { background: var(--surface-2); color: var(--danger-text); }
  .sbp-icon-btn:focus-visible, .sbp-add:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .sbp-search { display: flex; align-items: center; gap: 8px; margin-top: 12px; padding: 0 12px;
    border: 1px solid var(--border); border-radius: 12px; background: var(--bg); min-height: 44px; }
  .sbp-search:focus-within { border-color: var(--accent); }
  .sbp-search input { flex: 1; border: 0; background: transparent; color: var(--text); font: inherit;
    font-size: 16px; outline: none; min-width: 0; }
  .sbp-results { margin-top: 6px; display: flex; flex-direction: column; }
  .sbp-add { display: flex; align-items: center; gap: 12px; padding: 6px 8px; min-height: 44px; border: 0;
    border-radius: 10px; background: transparent; color: var(--text); font: inherit; text-align: left; cursor: pointer; }
  .sbp-add:hover { background: var(--surface-2); }
  .sbp-add .sbp-plus { margin-left: auto; color: var(--accent-text); display: flex; align-items: center; gap: 4px;
    font-size: 13px; font-weight: 600; }
  .sbp-empty { display: flex; gap: 10px; align-items: flex-start; padding: 12px; border-radius: 12px;
    background: color-mix(in srgb, var(--warning) 10%, transparent); font-size: 14px; line-height: 1.45; }
  .sbp-error { color: var(--danger-text); font-size: 13px; margin-top: 8px; }
`

function initials(name) {
  return (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase()
}

export function usePeople() {
  const [access, setAccess] = useState({ people: [] })
  const [members, setMembers] = useState([])
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    try {
      const [saved, known] = await Promise.all([
        window.mobius.storage.get(ACCESS),
        window.mobius.storage.get('members.json'),
      ])
      setAccess(saved && Array.isArray(saved.people) ? saved : { people: [] })
      setMembers(known?.people || [])
    } catch {
      setError('Couldn’t load the list. Try again in a moment.')
    }
  }, [])

  useEffect(() => { reload() }, [reload])

  const save = useCallback(async (people) => {
    setError('')
    const next = { people, updated_at: Date.now() }
    const previous = access
    setAccess(next)
    try {
      await window.mobius.storage.set(ACCESS, next)
    } catch {
      setAccess(previous)
      setError('Couldn’t save the change. Nothing was changed.')
    }
  }, [access])

  const allowedIds = useMemo(() => new Set(access.people.map((p) => p.id)), [access])
  const add = useCallback((person) => {
    if (allowedIds.has(person.id)) return
    save([...access.people, { id: person.id, name: person.name || person.id, handle: person.handle || '' }])
  }, [access, allowedIds, save])
  const remove = useCallback((id) => save(access.people.filter((p) => p.id !== id)), [access, save])

  return { people: access.people, members, allowedIds, add, remove, reload, error }
}

export function People({ state }) {
  const { people, members, allowedIds, add, remove, error } = state
  const [query, setQuery] = useState('')

  const q = query.trim().toLowerCase()
  const matches = q
    ? members.filter((m) => !allowedIds.has(m.id)
        && (m.name.toLowerCase().includes(q) || (m.handle || '').toLowerCase().includes(q)))
        .slice(0, 6)
    : []
  const byId = SLACK_ID.test(query.trim().toUpperCase()) && !allowedIds.has(query.trim().toUpperCase())
    && !matches.some((m) => m.id === query.trim().toUpperCase())

  return (
    <>
      {people.length === 0 ? (
        <div className="sbp-empty">
          <UserLock width={20} height={20} aria-hidden="true" />
          <span>Nobody can use the bot yet. Add yourself first, then anyone else you trust with full access to this Möbius.</span>
        </div>
      ) : (
        <div className="sbp-list" aria-label="People who can use the bot">
          {people.map((p) => (
            <div className="sbp-person" key={p.id}>
              <span className="sbp-avatar" aria-hidden="true">{initials(p.name)}</span>
              <div className="sbp-main">
                <div className="sbp-name">{p.name}</div>
                <div className="sbp-meta">{p.handle ? `@${p.handle} · ` : ''}{p.id}</div>
              </div>
              <button type="button" className="sbp-icon-btn" onClick={() => remove(p.id)}
                aria-label={`Remove ${p.name}`} title="Remove">
                <Trash width={18} height={18} />
              </button>
            </div>
          ))}
        </div>
      )}

      <label className="sbp-search">
        <Search width={18} height={18} aria-hidden="true" />
        <input
          type="search" inputMode="search" enterKeyHint="search" autoComplete="off"
          placeholder={members.length ? 'Add a person by name' : 'Add by Slack member ID (U…)'}
          value={query} onChange={(e) => setQuery(e.target.value)}
          aria-label="Find a Slack person to add"
        />
      </label>
      {(matches.length > 0 || byId) && (
        <div className="sbp-results">
          {matches.map((m) => (
            <button type="button" className="sbp-add" key={m.id} onClick={() => { add(m); setQuery('') }}>
              <span className="sbp-avatar" aria-hidden="true">{initials(m.name)}</span>
              <span className="sbp-main">
                <span className="sbp-name" style={{ display: 'block' }}>{m.name}</span>
                <span className="sbp-meta">{m.handle ? `@${m.handle}` : m.id}</span>
              </span>
              <span className="sbp-plus"><Plus width={16} height={16} />Add</span>
            </button>
          ))}
          {byId && (
            <button type="button" className="sbp-add"
              onClick={() => { const id = query.trim().toUpperCase(); add({ id, name: id }); setQuery('') }}>
              <span className="sbp-avatar" aria-hidden="true">ID</span>
              <span className="sbp-main"><span className="sbp-name">Slack member {query.trim().toUpperCase()}</span></span>
              <span className="sbp-plus"><Plus width={16} height={16} />Add</span>
            </button>
          )}
        </div>
      )}
      {q && !matches.length && !byId && (
        <p className="sbp-meta" style={{ marginTop: 8 }}>
          No match. In Slack, open the person’s profile → ⋮ → Copy member ID, and paste it here.
        </p>
      )}
      {error && <p className="sbp-error" role="alert">{error}</p>}
    </>
  )
}
