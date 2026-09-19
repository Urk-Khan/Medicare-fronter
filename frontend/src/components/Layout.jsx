import { useCallback, useEffect, useState } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { api, clearSession, getUsername, isSuperAdmin } from '../api';
import { Modal, Field, useAction, useInterval, useToast } from './ui';

const NAV = [
  { section: 'Operations' },
  { to: '/', label: 'Dashboard', end: true },
  { to: '/calls', label: 'Call History' },
  { to: '/followups', label: 'Closer Call-Backs' },
  { section: 'People' },
  { to: '/leads', label: 'Leads' },
  { to: '/closers', label: 'Closers' },
  { to: '/dnc', label: 'Do-Not-Call List' },
  { section: 'Setup' },
  { to: '/import', label: 'Import Leads' },
  { to: '/agents', label: 'AI Agents', superOnly: true },
  { to: '/users', label: 'Users', superOnly: true },
  { to: '/audit', label: 'Audit Log', superOnly: true },
  { to: '/settings', label: 'Settings' },
];

const TITLES = {
  '/': 'Dashboard', '/calls': 'Call History', '/followups': 'Closer Call-Backs', '/leads': 'Leads',
  '/closers': 'Closers', '/dnc': 'Do-Not-Call List', '/import': 'Import Leads', '/agents': 'AI Agents',
  '/users': 'Users', '/audit': 'Audit Log', '/settings': 'Settings',
};

export default function Layout({ children }) {
  const loc = useLocation();
  const nav = useNavigate();
  const toast = useToast();
  const [status, setStatus] = useState(null);
  const [calling, setCalling] = useState({ running: false, active: 0 });
  const [busy, run] = useAction();
  const [menuOpen, setMenuOpen] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);

  const loadCalling = useCallback(async () => {
    try { setCalling(await api('/api/calling/status')); } catch { /* handled globally */ }
  }, []);
  const loadStatus = useCallback(async () => {
    try { setStatus(await api('/api/system/status')); } catch { /* handled globally */ }
  }, []);

  useEffect(() => { loadCalling(); loadStatus(); }, [loadCalling, loadStatus]);
  useInterval(loadCalling, 3000);
  useInterval(loadStatus, 20000);
  useEffect(() => setMenuOpen(false), [loc.pathname]);

  const toggleCalling = () => run('dialer', async () => {
    await api(calling.running ? '/api/calling/stop' : '/api/calling/start', { method: 'POST' });
    await loadCalling();
  }, calling.running ? 'Automatic calling stopped.' : 'Automatic calling started.');

  const warnings = [];
  if (status) {
    if (status.database !== 'ok') warnings.push('Database tables are missing — run schema.sql in Supabase.');
    if (!status.public_url_ok) warnings.push('No public URL — start the backend with start.py so Telnyx can reach it.');
    if (status.closers_ready === 0) warnings.push('No closer phone numbers yet — add them on the Closers page.');
    if (status.agents_ready === 0) warnings.push('No AI agent is switched on — set one up on the AI Agents page.');
    if (!status.company_name_set) warnings.push('Set your company name in Settings — the AI says it on every call.');
    if (status.disclaimer_incomplete) warnings.push('Medicare disclaimer is incomplete — fill in the organization and product counts in Settings.');
    if (status.dialer_last_error && calling.running) warnings.push(`Dialer: ${status.dialer_last_error}`);
  }

  const title = loc.pathname.startsWith('/calls/') ? 'Call Details' : TITLES[loc.pathname] || 'VoiceOps';

  return (
    <div className={`shell ${menuOpen ? 'menuOpen' : ''}`}>
      <aside className="sidebar">
        <div className="brand">
          <div className="logo" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M7.2 3.5c.8 0 1.6 1.6 2.1 2.7.3.7-.2 1.3-.7 1.8.7 1.5 1.9 2.7 3.4 3.4.5-.5 1.1-1 1.8-.7 1.1.5 2.7 1.3 2.7 2.1 0 1.3-1.3 2.5-2.6 2.5C9.5 15.3 4.7 10.5 4.7 6.1c0-1.3 1.2-2.6 2.5-2.6z" transform="translate(1.5 2)"/></svg>
          </div>
          <div><b>VoiceOps</b><span>Medicare outbound</span></div>
        </div>
        <nav>
          {NAV.filter((item) => !item.superOnly || isSuperAdmin()).map((item, i) => item.section
            ? <div key={i} className="navSection">{item.section}</div>
            : (
              <NavLink key={item.to} to={item.to} end={item.end}>
                {item.label}
                {item.to === '/followups' && status?.followups_pending > 0 && <b className="navCount">{status.followups_pending}</b>}
              </NavLink>
            ))}
        </nav>
        <div className="sideBottom">
          {isSuperAdmin() && <div className="roleTag">Super admin</div>}
          <div className="healthRow"><i className={status?.database === 'ok' ? 'ok' : 'bad'} />Database</div>
          <div className="healthRow"><i className={status?.telnyx === 'ok' ? 'ok' : 'bad'} />Telnyx</div>
          <div className="healthRow"><i className={status?.llm_key && status?.cartesia_key ? 'ok' : 'bad'} />Voice AI</div>
          <div className="healthRow"><i className={status?.public_url_ok ? 'ok' : 'bad'} />Public URL</div>
          <div className="userRow">
            <span title={isSuperAdmin() ? 'Super admin' : 'Admin'}>{getUsername()}</span>
            <button className="linkBtn" onClick={() => setPwOpen(true)}>Password</button>
            <button className="linkBtn" onClick={() => { clearSession(); nav('/login'); }}>Sign out</button>
          </div>
        </div>
      </aside>

      <div className="mainCol">
        <header className="topbar">
          <button className="iconBtn menuBtn" onClick={() => setMenuOpen((v) => !v)} aria-label="Menu">☰</button>
          <div className="titleBlock">
            <div className="eyebrow">Medicare campaign</div>
            <h1>{title}</h1>
          </div>
          <div className="topActions">
            <span className={`runBadge ${calling.running ? 'live' : ''}`}>
              <i />{calling.running ? `Calling · ${calling.active} live` : `Stopped · ${calling.active} live`}
            </span>
            <button className={calling.running ? 'danger' : 'primary'} disabled={busy === 'dialer'} onClick={toggleCalling}>
              {calling.running ? 'Stop calling' : 'Start calling'}
            </button>
          </div>
        </header>
        {calling.paused_reason && !calling.running && (
          <div className="brakeBanner">
            <b>Calling paused automatically.</b> {calling.paused_reason} Press <b>Start calling</b> when you've checked it.
          </div>
        )}
        {warnings.length > 0 && (
          <div className="setupWarnings">
            {warnings.map((w) => <div key={w}>⚠ {w}</div>)}
          </div>
        )}
        <main className="main">{children}</main>
      </div>
      {menuOpen && <div className="scrim" onClick={() => setMenuOpen(false)} />}
      {pwOpen && <PasswordModal onClose={() => setPwOpen(false)} onDone={() => { setPwOpen(false); toast('Password changed.'); }} />}
    </div>
  );
}

function PasswordModal({ onClose, onDone }) {
  const [form, setForm] = useState({ current_password: '', new_password: '', confirm: '' });
  const [busy, run] = useAction();
  const submit = (e) => {
    e.preventDefault();
    if (form.new_password !== form.confirm) return;
    run('pw', async () => {
      await api('/api/auth/change-password', { method: 'POST', body: { current_password: form.current_password, new_password: form.new_password } });
      onDone();
    });
  };
  return (
    <Modal title="Change password" onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <Field label="Current password"><input type="password" required value={form.current_password} onChange={(e) => setForm({ ...form, current_password: e.target.value })} /></Field>
        <Field label="New password" hint="At least 8 characters."><input type="password" required minLength={8} value={form.new_password} onChange={(e) => setForm({ ...form, new_password: e.target.value })} /></Field>
        <Field label="Repeat new password">
          <input type="password" required value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} />
        </Field>
        {form.confirm && form.confirm !== form.new_password && <div className="formError">Passwords don't match.</div>}
        <div className="formActions"><button className="primary" disabled={busy === 'pw'}>Save password</button></div>
      </form>
    </Modal>
  );
}
