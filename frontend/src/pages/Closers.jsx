import { useState } from 'react';
import { api } from '../api';
import { Badge, Empty, Field, Modal, Notice, Panel, Table, fmtDateTime, fmtPhone, useAction, useLoad } from '../components/ui';

export default function Closers() {
  const { data, error, reload } = useLoad(() => api('/api/closers'), [], 4000);
  const [busy, run] = useAction();
  const [editing, setEditing] = useState(null);

  const patch = (c, body, msg) => run(c.id, async () => { await api(`/api/closers/${c.id}`, { method: 'PATCH', body }); await reload(); }, msg);
  const release = (c) => {
    if (!window.confirm(`Mark ${c.name} as free? Only do this if they're definitely not on a call.`)) return;
    run(c.id, async () => { await api(`/api/closers/${c.id}/release`, { method: 'POST' }); await reload(); }, `${c.name} is free again.`);
  };
  const remove = (c) => {
    if (!window.confirm(`Remove ${c.name}?`)) return;
    run(c.id, async () => { await api(`/api/closers/${c.id}`, { method: 'DELETE' }); await reload(); }, 'Closer removed.');
  };

  const items = data?.items || [];
  return (
    <>
      <Notice>
        When a lead qualifies, the AI rings the <b>first free closer</b> (lowest priority number first; ties go to whoever
        had a transfer least recently). If that person is <b>busy on another call, marked Away, doesn't answer, or doesn't press 1</b>,
        the next closer is tried automatically. If nobody can take it, the AI books a callback.
      </Notice>
      <Panel title="Closers (licensed agents)" flush action={<button className="primary" onClick={() => setEditing({ name: '', destination: '', priority: items.length + 1, enabled: true })}>Add closer</button>}>
        {error ? <Empty text={error} /> : (
          <Table
            columns={['Closer', 'Phone / SIP', 'Priority', 'Availability', 'Right now', 'Transfers (24h)', '']}
            empty="No closers yet."
            rows={items.map((c) => ({
              key: c.id,
              cells: [
                <div key="n"><b>{c.name}</b><div className="sub">{!c.enabled ? 'Disabled' : c.last_assigned_at ? `Last transfer ${fmtDateTime(c.last_assigned_at)}` : 'No transfers yet'}</div></div>,
                c.destination ? fmtPhone(c.destination) : <span className="bad">Not set</span>,
                c.priority,
                <div key="a" className="segmented">
                  <button className={c.availability === 'available' ? 'on' : ''} disabled={busy === c.id} onClick={() => patch(c, { availability: 'available' }, `${c.name} is available.`)}>Available</button>
                  <button className={c.availability === 'away' ? 'on away' : ''} disabled={busy === c.id} onClick={() => patch(c, { availability: 'away' }, `${c.name} is away.`)}>Away</button>
                </div>,
                c.status === 'FREE' ? <Badge key="s" value="FREE" /> : (
                  <span key="s" className="rowActions"><Badge value={c.status === 'RINGING' ? 'ringing' : 'ON_CALL'} /><button className="small ghost" onClick={() => release(c)}>Set free</button></span>
                ),
                c.transfers_24h,
                <div key="x" className="rowActions">
                  <button className="small ghost" onClick={() => setEditing(c)}>Edit</button>
                  <button className="small ghost" disabled={busy === c.id} onClick={() => patch(c, { enabled: !c.enabled }, c.enabled ? 'Disabled.' : 'Enabled.')}>{c.enabled ? 'Disable' : 'Enable'}</button>
                </div>,
              ],
            }))}
          />
        )}
      </Panel>
      {editing && <CloserModal closer={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload(); }}
        onRemove={editing.id ? () => { setEditing(null); remove(editing); } : null} />}
    </>
  );
}

function CloserModal({ closer, onClose, onSaved, onRemove }) {
  const [form, setForm] = useState({ name: closer.name, destination: closer.destination || '', priority: closer.priority || 1, enabled: closer.enabled ?? true });
  const [busy, run] = useAction();
  const save = (e) => {
    e.preventDefault();
    const body = { ...form, priority: Number(form.priority) || 1 };
    run('save', async () => {
      if (closer.id) await api(`/api/closers/${closer.id}`, { method: 'PATCH', body });
      else await api('/api/closers', { method: 'POST', body });
      onSaved();
    }, 'Closer saved.');
  };
  return (
    <Modal title={closer.id ? `Edit ${closer.name}` : 'Add closer'} onClose={onClose}>
      <form className="form" onSubmit={save}>
        <Field label="Name"><input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
        <Field label="Phone number or SIP address" hint="Their direct line, e.g. (305) 555-0199, or sip:agent1@yourpbx.com. Make sure voicemail doesn't pick up within 20 seconds.">
          <input value={form.destination} onChange={(e) => setForm({ ...form, destination: e.target.value })} />
        </Field>
        <Field label="Priority" hint="1 is tried first. Give several closers the same number to rotate transfers fairly between them.">
          <input type="number" min={1} value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} />
        </Field>
        <label className="check"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> Enabled</label>
        <div className="formActions">
          {onRemove && <button type="button" className="ghost danger" onClick={onRemove}>Remove closer</button>}
          <span className="grow" />
          <button className="primary" disabled={busy === 'save'}>Save</button>
        </div>
      </form>
    </Modal>
  );
}
