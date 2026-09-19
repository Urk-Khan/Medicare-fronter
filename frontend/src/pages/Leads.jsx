import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, qs } from '../api';
import { Badge, Empty, Field, Modal, Pager, Panel, Table, fmtDateTime, fmtDuration, fullName, label, useAction, useLoad } from '../components/ui';

const STATUSES = ['new', 'calling', 'retry_scheduled', 'callback_scheduled', 'transferred', 'contacted', 'not_interested',
  'not_qualified', 'no_answer_final', 'wrong_number', 'do_not_call', 'failed'];
const MANUAL_STATUSES = ['new', 'not_interested', 'not_qualified', 'contacted', 'failed', 'no_answer_final'];

export default function Leads() {
  const [params, setParams] = useSearchParams();
  const [page, setPage] = useState(1);
  const [q, setQ] = useState(params.get('q') || '');
  const search = params.get('q') || '';
  const status = params.get('status') || '';
  const { data, error, reload } = useLoad(() => api(`/api/leads${qs({ q: search, status, page, page_size: 50 })}`), [search, status, page]);
  const [busy, run] = useAction();
  const [adding, setAdding] = useState(false);
  const [openId, setOpenId] = useState(null);

  useEffect(() => setPage(1), [search, status]);

  const setFilter = (key, value) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    setParams(next);
  };

  const callNow = (lead) => run(`call-${lead.id}`, async () => {
    await api(`/api/leads/${lead.id}/call`, { method: 'POST' });
    await reload();
  }, `Calling ${fullName(lead)}…`);

  return (
    <>
      <Panel
        title={`Leads${data ? ` · ${data.total}` : ''}`}
        flush
        action={(
          <div className="toolbar">
            <form onSubmit={(e) => { e.preventDefault(); setFilter('q', q.trim()); }}>
              <input className="search" placeholder="Search name, phone, ZIP" value={q} onChange={(e) => setQ(e.target.value)} />
            </form>
            <select value={status} onChange={(e) => setFilter('status', e.target.value)}>
              <option value="">All statuses</option>
              {STATUSES.map((s) => <option key={s} value={s}>{label(s)}</option>)}
            </select>
            <button className="primary" onClick={() => setAdding(true)}>Add lead</button>
            <Link className="button ghost" to="/import">Import</Link>
          </div>
        )}
      >
        {error ? <Empty text={error} /> : (
          <>
            <Table
              columns={['Lead', 'Location', 'Status', 'Attempts', 'Last call', 'Consent', '']}
              empty={data ? 'No leads match.' : 'Loading…'}
              onRowClick={(r) => setOpenId(r.key)}
              rows={(data?.items || []).map((x) => ({
                key: x.id,
                cells: [
                  <div key="n"><b>{fullName(x)}</b><div className="sub">{x.phone_pretty}</div></div>,
                  <div key="l">{[x.state, x.zip_code].filter(Boolean).join(' ') || '—'}<div className="sub">{x.timezone ? x.timezone.replace('America/', '').replace('_', ' ') : 'Default time zone'}</div></div>,
                  <Badge key="s" value={x.status} />,
                  x.retry_count || 0,
                  <div key="lc">{fmtDateTime(x.last_call_at)}<div className="sub">{x.last_outcome ? label(x.last_outcome) : ''}</div></div>,
                  x.has_consent ? <span key="c" className="ok">✓</span> : <span key="c" className="bad" title="Won't be called">Missing</span>,
                  <button key="b" className="small" disabled={busy === `call-${x.id}` || !x.has_consent || ['do_not_call', 'calling', 'wrong_number'].includes(x.status)}
                    title={!x.callable_now ? "Outside this lead's calling hours" : ''}
                    onClick={(e) => { e.stopPropagation(); callNow(x); }}>
                    {busy === `call-${x.id}` ? 'Dialing…' : 'Call now'}
                  </button>,
                ],
              }))}
            />
            <Pager page={page} pageSize={50} total={data?.total} onPage={setPage} />
          </>
        )}
      </Panel>
      {adding && <AddLead onClose={() => setAdding(false)} onSaved={() => { setAdding(false); reload(); }} />}
      {openId && <LeadModal id={openId} onClose={() => setOpenId(null)} onChanged={reload} />}
    </>
  );
}

function AddLead({ onClose, onSaved }) {
  const [form, setForm] = useState({ first_name: '', last_name: '', phone: '', zip_code: '', state: '', source: '', notes: '', consent_source: '', consent_confirmed: false });
  const [busy, run] = useAction();
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value });
  const save = (e) => {
    e.preventDefault();
    run('save', async () => { await api('/api/leads', { method: 'POST', body: form }); onSaved(); }, 'Lead added.');
  };
  return (
    <Modal title="Add a lead" onClose={onClose}>
      <form className="form" onSubmit={save}>
        <div className="row2">
          <Field label="First name"><input value={form.first_name} onChange={set('first_name')} /></Field>
          <Field label="Last name"><input value={form.last_name} onChange={set('last_name')} /></Field>
        </div>
        <Field label="Phone"><input required value={form.phone} onChange={set('phone')} placeholder="(305) 555-0123" /></Field>
        <div className="row2">
          <Field label="ZIP code"><input value={form.zip_code} onChange={set('zip_code')} /></Field>
          <Field label="State"><input value={form.state} onChange={set('state')} maxLength={2} placeholder="FL" /></Field>
        </div>
        <Field label="Lead source" hint="Shown to the AI, e.g. 'requested Medicare info on our website'."><input value={form.source} onChange={set('source')} /></Field>
        <Field label="Notes for the AI"><textarea rows={2} value={form.notes} onChange={set('notes')} /></Field>
        <Field label="Where did consent come from?" hint="Required. e.g. 'Web form opt-in 9/12, TCPA disclosure accepted'."><input required value={form.consent_source} onChange={set('consent_source')} /></Field>
        <label className="check"><input type="checkbox" checked={form.consent_confirmed} onChange={set('consent_confirmed')} /> This person gave permission to be contacted about Medicare options, including by an automated/AI voice.</label>
        <div className="formActions"><button className="primary" disabled={busy === 'save' || !form.consent_confirmed}>Add lead</button></div>
      </form>
    </Modal>
  );
}

function LeadModal({ id, onClose, onChanged }) {
  const { data, error, reload } = useLoad(() => api(`/api/leads/${id}`), [id]);
  const [form, setForm] = useState(null);
  const [busy, run] = useAction();

  useEffect(() => {
    if (data) setForm({ first_name: data.first_name, last_name: data.last_name, zip_code: data.zip_code || '', state: data.state || '', source: data.source || '', notes: data.notes || '', status: data.status });
  }, [data]);

  const save = () => run('save', async () => {
    const body = { ...form };
    if (body.status === data.status || !MANUAL_STATUSES.includes(body.status)) delete body.status;
    await api(`/api/leads/${id}`, { method: 'PATCH', body });
    await reload();
    onChanged();
  }, 'Lead saved.');
  const markDnc = () => {
    if (!window.confirm('Add this number to the do-not-call list? It will never be called again.')) return;
    run('dnc', async () => { await api(`/api/leads/${id}/dnc`, { method: 'POST' }); await reload(); onChanged(); }, 'Added to do-not-call list.');
  };
  const remove = () => {
    if (!window.confirm('Delete this lead and its callbacks? Call history is kept.')) return;
    run('del', async () => { await api(`/api/leads/${id}`, { method: 'DELETE' }); onChanged(); onClose(); }, 'Lead deleted.');
  };

  return (
    <Modal wide title={data ? fullName(data) : 'Lead'} onClose={onClose}
      footer={data && form && (
        <>
          <button className="ghost danger" disabled={busy === 'del'} onClick={remove}>Delete</button>
          {data.status !== 'do_not_call' && <button className="ghost" disabled={busy === 'dnc'} onClick={markDnc}>Mark do-not-call</button>}
          <span className="grow" />
          <button className="primary" disabled={busy === 'save'} onClick={save}>Save changes</button>
        </>
      )}>
      {error && <Empty text={error} />}
      {data && form && (
        <div className="grid2 tight">
          <div className="form">
            <div className="row2">
              <Field label="First name"><input value={form.first_name} onChange={(e) => setForm({ ...form, first_name: e.target.value })} /></Field>
              <Field label="Last name"><input value={form.last_name} onChange={(e) => setForm({ ...form, last_name: e.target.value })} /></Field>
            </div>
            <div className="row2">
              <Field label="ZIP code"><input value={form.zip_code} onChange={(e) => setForm({ ...form, zip_code: e.target.value })} /></Field>
              <Field label="State"><input value={form.state} maxLength={2} onChange={(e) => setForm({ ...form, state: e.target.value })} /></Field>
            </div>
            <Field label="Lead source"><input value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} /></Field>
            <Field label="Notes for the AI"><textarea rows={3} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} /></Field>
            <Field label="Status" hint="Set back to New to put the lead in the queue again.">
              <select value={form.status} disabled={!MANUAL_STATUSES.includes(data.status) && data.status !== form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
                {[...new Set([data.status, ...MANUAL_STATUSES])].map((s) => <option key={s} value={s}>{label(s)}</option>)}
              </select>
            </Field>
          </div>
          <div>
            <div className="info"><span>Phone</span><b>{data.phone_pretty}</b></div>
            <div className="info"><span>Their local time</span><b>{data.local_time}</b></div>
            <div className="info"><span>Callable right now</span><b>{data.callable_now ? 'Yes' : 'No (outside calling hours)'}</b></div>
            <div className="info"><span>Consent</span><b>{data.consent_at ? `${fmtDateTime(data.consent_at)} — ${data.consent_source || ''}` : 'Missing'}</b></div>
            <div className="info"><span>Next attempt</span><b>{fmtDateTime(data.next_attempt_at)}</b></div>
            {Object.keys(data.custom_fields || {}).length > 0 && (
              <div className="info"><span>Other columns</span><b className="small">{Object.entries(data.custom_fields).map(([k, v]) => `${k}: ${v}`).join(' · ')}</b></div>
            )}
            <h3 className="subhead">Calls</h3>
            {(data.calls || []).length === 0 ? <p className="muted">Not called yet.</p> : (
              <ul className="miniList">
                {data.calls.map((c) => (
                  <li key={c.id}><Link to={`/calls/${c.id}`}>{fmtDateTime(c.started_at)}</Link> <Badge value={c.outcome || c.status} /> <span className="sub">{fmtDuration(c.duration_seconds)}{c.closer_name ? ` · ${c.closer_name}` : ''}</span></li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
