import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export const LABELS = {
  new: 'New', calling: 'Calling', retry_scheduled: 'Retry scheduled', callback_scheduled: 'Callback scheduled',
  transferred: 'Transferred', not_interested: 'Not interested', not_qualified: 'No Medicare',
  no_closer_available: 'Closer to call back', awaiting_closer: 'Waiting for a closer',
  do_not_call: 'Do not call', no_answer_final: 'Unreachable', wrong_number: 'Wrong number', contacted: 'Contacted',
  failed: 'Failed', no_answer: 'No answer', busy: 'Busy', voicemail: 'Voicemail', caller_hangup: 'Hung up',
  caller_hangup_early: 'Hung up early', bad_number: 'Bad number', completed: 'Completed', no_conversation: 'No conversation',
  DIALING: 'Dialing', RINGING: 'Ringing', ANSWERED: 'Answered', AI_CONVERSATION: 'AI talking', TRANSFERRING: 'Transferring',
  TRANSFERRED: 'With closer', ENDED: 'Ended', FREE: 'Free', ON_CALL: 'On a call', available: 'Available', away: 'Away',
  pending: 'Pending', dialing: 'Dialing', cancelled: 'Cancelled', ringing: 'Ringing', answered: 'Answered',
  connecting: 'Connecting', connected: 'Connected', declined: 'Declined', greeting: 'Greeting', qualifying: 'Qualifying',
  transferring: 'Transferring', closing: 'Wrapping up',
};

export const label = (v) => LABELS[v] || (v ? String(v).replaceAll('_', ' ') : '—');

const TONES = {
  good: ['transferred', 'connected', 'completed', 'FREE', 'available', 'contacted', 'TRANSFERRED', 'AI_CONVERSATION'],
  info: ['new', 'calling', 'DIALING', 'RINGING', 'ANSWERED', 'TRANSFERRING', 'ringing', 'answered', 'connecting', 'dialing', 'pending', 'RINGING'],
  warn: ['retry_scheduled', 'callback_scheduled', 'no_closer_available', 'awaiting_closer', 'no_answer', 'busy', 'voicemail', 'caller_hangup', 'caller_hangup_early', 'ON_CALL', 'away', 'declined', 'no_conversation'],
  bad: ['do_not_call', 'failed', 'wrong_number', 'bad_number', 'not_qualified', 'not_interested', 'no_answer_final', 'cancelled'],
};

export function Badge({ value, text }) {
  const tone = Object.keys(TONES).find((t) => TONES[t].includes(value)) || 'muted';
  return <span className={`badge ${tone}`}>{text || label(value)}</span>;
}

export function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

export function fmtDuration(sec) {
  if (sec === null || sec === undefined) return '—';
  const s = Math.max(0, Math.round(sec));
  const m = Math.floor(s / 60);
  return m ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`;
}

export function fmtPhone(value) {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(value || '');
  return m ? `(${m[1]}) ${m[2]}-${m[3]}` : (value || '');
}

export function fullName(x) {
  return [x?.first_name, x?.last_name].filter(Boolean).join(' ') || x?.lead_name || '—';
}

// ---------------------------------------------------------------------------
// Layout pieces
// ---------------------------------------------------------------------------

export function Panel({ title, action, children, className = '', flush = false }) {
  return (
    <section className={`panel ${className}`}>
      {(title || action) && (
        <div className="panelHead">
          <h2>{title}</h2>
          {action && <div className="panelAction">{action}</div>}
        </div>
      )}
      <div className={flush ? '' : 'panelBody'}>{children}</div>
    </section>
  );
}

export function Stat({ label: l, value, hint, tone }) {
  return (
    <div className={`stat ${tone || ''}`}>
      <span>{l}</span>
      <strong>{value ?? 0}</strong>
      {hint && <small>{hint}</small>}
    </div>
  );
}

export function Empty({ text, children }) {
  return <div className="empty">{text}{children}</div>;
}

export function Table({ columns, rows, empty = 'Nothing here yet.', onRowClick }) {
  return (
    <div className="tableWrap">
      <table>
        <thead>
          <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr><td colSpan={columns.length}><Empty text={empty} /></td></tr>
          ) : rows.map((r, i) => (
            <tr key={r.key ?? i} className={onRowClick ? 'clickable' : ''} onClick={onRowClick ? () => onRowClick(r) : undefined}>
              {r.cells.map((cell, j) => <td key={j}>{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Pager({ page, pageSize, total, onPage }) {
  const pages = Math.max(1, Math.ceil((total || 0) / pageSize));
  if (pages <= 1) return null;
  return (
    <div className="pager">
      <button className="ghost" disabled={page <= 1} onClick={() => onPage(page - 1)}>← Previous</button>
      <span>Page {page} of {pages} · {total} total</span>
      <button className="ghost" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next →</button>
    </div>
  );
}

export function Field({ label: l, hint, children }) {
  return (
    <label className="field">
      <span className="fieldLabel">{l}</span>
      {children}
      {hint && <span className="fieldHint">{hint}</span>}
    </label>
  );
}

// Escape closes only the modal on top, so a preview opened from an editor
// doesn't take the editor (and your unsaved edits) with it.
const modalStack = [];

export function Modal({ title, onClose, children, footer, wide }) {
  useEffect(() => {
    const token = {};
    modalStack.push(token);
    const onKey = (e) => {
      if (e.key === 'Escape' && modalStack[modalStack.length - 1] === token) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      const i = modalStack.indexOf(token);
      if (i >= 0) modalStack.splice(i, 1);
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);
  return (
    <div className="modalBackdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true">
        <div className="modalHead">
          <h3>{title}</h3>
          <button className="iconBtn" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="modalBody">{children}</div>
        {footer && <div className="modalFoot">{footer}</div>}
      </div>
    </div>
  );
}

export function Notice({ tone = 'info', children }) {
  return <div className={`notice tone-${tone}`}>{children}</div>;
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

export function useInterval(fn, ms) {
  const saved = useRef(fn);
  useEffect(() => { saved.current = fn; }, [fn]);
  useEffect(() => {
    if (!ms) return undefined;
    const tick = () => { if (!document.hidden) saved.current(); };
    const id = setInterval(tick, ms);
    return () => clearInterval(id);
  }, [ms]);
}

export function useLoad(loader, deps = [], pollMs = 0) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const run = useCallback(async () => {
    try {
      setData(await loader());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => { setLoading(true); run(); }, [run]);
  useInterval(run, pollMs);
  return { data, error, loading, reload: run };
}

// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------

const ToastContext = createContext(() => {});

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const push = useCallback((text, tone = 'good') => {
    const id = Math.random().toString(36).slice(2);
    setToasts((t) => [...t, { id, text, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === 'bad' ? 7000 : 3500);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => <div key={t.id} className={`toast ${t.tone}`}>{t.text}</div>)}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);

// Run an async action with a busy flag and success/error toasts.
export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState(null);
  const run = useCallback(async (key, fn, success) => {
    setBusy(key);
    try {
      const result = await fn();
      if (success) toast(typeof success === 'function' ? success(result) : success);
      return result;
    } catch (e) {
      toast(e.message, 'bad');
      return undefined;
    } finally {
      setBusy(null);
    }
  }, [toast]);
  return [busy, run];
}
