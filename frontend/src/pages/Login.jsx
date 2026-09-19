import { useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { api, getToken, setSession } from '../api';

export default function Login() {
  const nav = useNavigate();
  const [form, setForm] = useState({ username: '', password: '' });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  if (getToken()) return <Navigate to="/" replace />;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const res = await api('/api/auth/login', { method: 'POST', body: form });
      setSession(res.access_token, res.username, res.role);
      nav('/', { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="loginPage">
      <form className="loginCard" onSubmit={submit}>
        <div className="logo big" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="26" height="26"><path fill="currentColor" d="M7.2 3.5c.8 0 1.6 1.6 2.1 2.7.3.7-.2 1.3-.7 1.8.7 1.5 1.9 2.7 3.4 3.4.5-.5 1.1-1 1.8-.7 1.1.5 2.7 1.3 2.7 2.1 0 1.3-1.3 2.5-2.6 2.5C9.5 15.3 4.7 10.5 4.7 6.1c0-1.3 1.2-2.6 2.5-2.6z" transform="translate(1.5 2)"/></svg>
        </div>
        <h1>VoiceOps</h1>
        <p className="muted">Medicare outbound calling console</p>
        <label className="field">
          <span className="fieldLabel">Username</span>
          <input autoFocus autoComplete="username" required value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        </label>
        <label className="field">
          <span className="fieldLabel">Password</span>
          <input type="password" autoComplete="current-password" required value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        </label>
        {error && <div className="formError">{error}</div>}
        <button className="primary full" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </div>
  );
}
