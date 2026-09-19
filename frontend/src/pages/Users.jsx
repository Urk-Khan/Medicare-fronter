import { useState } from 'react';
import { api, getUsername } from '../api';
import { Badge, Empty, Field, Modal, Notice, Panel, Table, fmtDateTime, useAction, useLoad } from '../components/ui';

/** Who can sign in. Only the super admin sees this page. */
export default function Users() {
  const { data, error, reload } = useLoad(() => api('/api/users'), []);
  const [editing, setEditing] = useState(null);
  const [adding, setAdding] = useState(false);
  const [busy, run] = useAction();
  const me = getUsername();

  const patch = (user, body, msg) => run(user.id, async () => {
    await api(`/api/users/${user.id}`, { method: 'PATCH', body });
    await reload();
  }, msg);

  const remove = (user) => {
    if (!window.confirm(`Delete the login for ${user.username}?`)) return;
    run(user.id, async () => { await api(`/api/users/${user.id}`, { method: 'DELETE' }); await reload(); }, 'Login deleted.');
  };

  return (
    <>
      <Panel
        title="People who can sign in"
        flush
        action={<button className="primary" onClick={() => setAdding(true)}>Add a login</button>}
      >
        {error ? <Empty text={error} /> : (
          <Table
            columns={['Username', 'Name', 'Can do', 'Status', 'Last signed in', '']}
            empty={data ? 'No logins yet.' : 'Loading…'}
            rows={(data?.items || []).map((u) => ({
              key: u.id,
              cells: [
                <div key="u"><b>{u.username}</b>{u.username === me && <span className="sub">this is you</span>}</div>,
                u.full_name || '—',
                u.role === 'super_admin'
                  ? <Badge key="r" value="transferred" text="Everything (super admin)" />
                  : <Badge key="r" value="new" text="Day-to-day work" />,
                u.enabled ? <Badge key="s" value="available" text="Active" /> : <Badge key="s" value="cancelled" text="Disabled" />,
                fmtDateTime(u.last_login_at),
                <div key="a" className="rowActions">
                  <button className="small ghost" onClick={() => setEditing(u)}>Edit</button>
                  {u.username !== me && (
                    <button className="small ghost" disabled={busy === u.id} onClick={() => patch(u, { enabled: !u.enabled }, u.enabled ? 'Login disabled.' : 'Login enabled.')}>
                      {u.enabled ? 'Disable' : 'Enable'}
                    </button>
                  )}
                  {u.username !== me && <button className="small ghost danger" disabled={busy === u.id} onClick={() => remove(u)}>Delete</button>}
                </div>,
              ],
            }))}
          />
        )}
      </Panel>

      <Notice>
        A <b>super admin</b> can do everything, including the AI agents&apos; scripts, the dialer and transfer settings,
        the audit log and this page. Everyone else works the calls, leads, closers and imports.
      </Notice>

      {(adding || editing) && (
        <UserModal
          user={editing}
          onClose={() => { setAdding(false); setEditing(null); }}
          onSaved={async () => { setAdding(false); setEditing(null); await reload(); }}
        />
      )}
    </>
  );
}

function UserModal({ user, onClose, onSaved }) {
  const isNew = !user;
  const [form, setForm] = useState(user || { username: '', full_name: '', role: 'admin', password: '' });
  const [busy, run] = useAction();
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const submit = (e) => {
    e.preventDefault();
    run('save', async () => {
      if (isNew) {
        await api('/api/users', { method: 'POST', body: { username: form.username, password: form.password, full_name: form.full_name, role: form.role } });
      } else {
        const body = { full_name: form.full_name, role: form.role };
        if (form.password) body.new_password = form.password;
        await api(`/api/users/${user.id}`, { method: 'PATCH', body });
      }
      await onSaved();
    }, isNew ? 'Login created.' : 'Login updated.');
  };

  return (
    <Modal title={isNew ? 'Add a login' : `Edit ${user.username}`} onClose={onClose}>
      <form className="form" onSubmit={submit}>
        {isNew && <Field label="Username" hint="Letters, numbers, dots, dashes and underscores."><input required minLength={3} value={form.username} onChange={set('username')} /></Field>}
        <Field label="Full name"><input value={form.full_name || ''} onChange={set('full_name')} /></Field>
        <Field label="What they can do">
          <select value={form.role} onChange={set('role')}>
            <option value="admin">Day-to-day work (calls, leads, closers, imports)</option>
            <option value="super_admin">Everything, including scripts, dialer settings and users</option>
          </select>
        </Field>
        <Field label={isNew ? 'Password' : 'New password'} hint={isNew ? 'At least 8 characters.' : 'Leave empty to keep the current one.'}>
          <input type="password" minLength={isNew ? 8 : undefined} required={isNew} value={form.password || ''} onChange={set('password')} />
        </Field>
        <div className="formActions"><button className="primary" disabled={busy === 'save'}>{isNew ? 'Create login' : 'Save'}</button></div>
      </form>
    </Modal>
  );
}
