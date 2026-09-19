import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, qs } from '../api';
import { Badge, Empty, Field, Modal, Pager, Panel, Table, fmtDateTime, fmtDuration, useAction, useLoad } from '../components/ui';

const PAGE_SIZE = 50;

function toLocalInput(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function CallHistory() {
  const nav = useNavigate();
  const [page, setPage] = useState(1);
  const [outcome, setOutcome] = useState(new URLSearchParams(window.location.search).get('outcome') || '');
  const [agentId, setAgentId] = useState('');
  const [period, setPeriod] = useState('');
  const [q, setQ] = useState('');
  const [search, setSearch] = useState('');
  const [busy, run] = useAction();
  const [reschedule, setReschedule] = useState(null);
  const [when, setWhen] = useState('');

  const { data, error, reload } = useLoad(
    () => api(`/api/calls${qs({ page, outcome, agent_id: agentId, period, q: search, page_size: PAGE_SIZE })}`),
    [page, outcome, agentId, period, search],
    5000,
  );

  const callNow = (row) => run(row.id, async () => {
    await api(`/api/leads/${row.lead_id}/call`, { method: 'POST' });
    await reload();
  }, 'Calling them now.');

  const markDnc = (row) => {
    if (!window.confirm(`Add ${row.phone_pretty} to the do-not-call list? They will never be called again.`)) return;
    run(row.id, async () => { await api(`/api/leads/${row.lead_id}/dnc`, { method: 'POST' }); await reload(); }, 'Added to the do-not-call list.');
  };

  const cancelCallback = (row) => {
    if (!window.confirm('Cancel this callback?')) return;
    run(row.id, async () => {
      await api(`/api/callbacks/${row.callback.id}`, { method: 'PATCH', body: { status: 'cancelled' } });
      await reload();
    }, 'Callback cancelled.');
  };

  const saveReschedule = () => run('resched', async () => {
    await api(`/api/callbacks/${reschedule.callback.id}`, { method: 'PATCH', body: { scheduled_for: new Date(when).toISOString() } });
    setReschedule(null);
    await reload();
  }, 'Callback moved.');

  const rows = (data?.items || []).map((c) => ({
    key: c.id,
    cells: [
      <div key="w"><b>{fmtDateTime(c.started_at)}</b>{c.is_callback && <div className="sub">Callback</div>}</div>,
      <div key="l"><b>{c.lead_name || '—'}</b><div className="sub">{c.phone_pretty}</div></div>,
      <div key="p">{c.lead_zip || '—'}{c.lead_state ? `, ${c.lead_state}` : ''}<div className="sub">{c.lead_source || ''}</div></div>,
      c.agent_name || '—',
      <span key="h" className={c.live ? 'liveNow' : ''}>{c.live && <i className="pulse" />}{c.what_happened}</span>,
      c.closer_name || '—',
      fmtDuration(c.talk_seconds || c.elapsed_seconds),
      c.callback ? fmtDateTime(c.callback.scheduled_for) : '—',
      <div key="a" className="rowActions" onClick={(e) => e.stopPropagation()}>
        {!c.live && c.lead_id && (
          <button className="small" disabled={busy === c.id} onClick={() => callNow(c)}>Call now</button>
        )}
        {c.callback && (
          <>
            <button className="small ghost" onClick={() => { setReschedule(c); setWhen(toLocalInput(c.callback.scheduled_for)); }}>Move</button>
            <button className="small ghost" disabled={busy === c.id} onClick={() => cancelCallback(c)}>Cancel</button>
          </>
        )}
        {c.lead_id && <button className="small ghost danger" disabled={busy === c.id} onClick={() => markDnc(c)}>Do not call</button>}
      </div>,
    ],
  }));

  return (
    <>
      <div className="filterBar">
        <form onSubmit={(e) => { e.preventDefault(); setPage(1); setSearch(q); }}>
          <input className="search" placeholder="Search name, phone or ZIP" value={q} onChange={(e) => setQ(e.target.value)} />
        </form>
        <label>
          <span>What happened</span>
          <select value={outcome} onChange={(e) => { setPage(1); setOutcome(e.target.value); }}>
            <option value="">Everything</option>
            <option value="live">Happening now{data?.live_count ? ` (${data.live_count})` : ''}</option>
            {(data?.outcomes || []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </label>
        <label>
          <span>AI agent</span>
          <select value={agentId} onChange={(e) => { setPage(1); setAgentId(e.target.value); }}>
            <option value="">All agents</option>
            {(data?.agents || []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </label>
        <label>
          <span>When</span>
          <select value={period} onChange={(e) => { setPage(1); setPeriod(e.target.value); }}>
            <option value="">All time</option>
            <option value="today">Today</option>
            <option value="month">This month</option>
          </select>
        </label>
      </div>

      <Panel title="Every call" flush>
        {error ? <Empty text={error} /> : (
          <>
            <Table
              columns={['When', 'Lead', 'Where', 'Agent', 'What happened', 'Closer', 'Talk time', 'Call back at', '']}
              empty={data ? 'No calls match what you picked.' : 'Loading…'}
              onRowClick={(r) => nav(`/calls/${r.key}`)}
              rows={rows}
            />
            <Pager page={page} pageSize={PAGE_SIZE} total={data?.total} onPage={setPage} />
          </>
        )}
      </Panel>

      {reschedule && (
        <Modal
          title={`Move the callback for ${reschedule.lead_name || reschedule.phone_pretty}`}
          onClose={() => setReschedule(null)}
          footer={(
            <>
              <button className="ghost" onClick={() => setReschedule(null)}>Close</button>
              <button className="primary" disabled={!when || busy === 'resched'} onClick={saveReschedule}>Save</button>
            </>
          )}
        >
          <Field label="New date and time (your computer's time zone)" hint="If this falls outside the lead's calling hours, it moves to the next allowed time.">
            <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
          </Field>
        </Modal>
      )}
    </>
  );
}
