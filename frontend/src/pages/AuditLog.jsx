import { useState } from 'react';
import { api, qs } from '../api';
import { Empty, Pager, Panel, Table, fmtDateTime, useLoad } from '../components/ui';

const ACTION_LABELS = {
  login: 'Signed in', password_changed: 'Changed their password', dialer_started: 'Started automatic calling',
  dialer_stopped: 'Stopped automatic calling', dialer_paused_by_safety_brake: 'Calling paused by the safety brake',
  settings_updated: 'Changed settings', agent_created: 'Added an AI agent', agent_updated: 'Edited an AI agent',
  agent_deleted: 'Removed an AI agent', agent_script_reset: 'Reset an agent script',
  user_created: 'Added a login', user_updated: 'Edited a login', user_deleted: 'Deleted a login',
  closer_created: 'Added a closer', closer_updated: 'Edited a closer', closer_deleted: 'Removed a closer',
  closer_force_released: 'Freed a closer by hand', lead_created: 'Added a lead', lead_updated: 'Edited a lead',
  lead_deleted: 'Deleted a lead', lead_dnc: 'Marked a lead do-not-call', manual_call: 'Called a lead by hand',
  manual_hangup: 'Hung up a call', leads_imported: 'Imported leads', dnc_added: 'Added do-not-call numbers',
  dnc_uploaded: 'Uploaded do-not-call numbers', dnc_removed: 'Removed a do-not-call number',
  callback_rescheduled: 'Moved a callback', callback_completed: 'Marked a callback done',
  callback_cancelled: 'Cancelled a callback', followup_done: 'Marked a closer call-back done',
  followup_pending: 'Put a closer call-back back on the list', followups_exported: 'Downloaded the closer call-back list',
};

const PAGE_SIZE = 100;

export default function AuditLog() {
  const [page, setPage] = useState(1);
  const [period, setPeriod] = useState('');
  const [q, setQ] = useState('');
  const [search, setSearch] = useState('');
  const { data, error } = useLoad(() => api(`/api/audit${qs({ page, q: search, period, page_size: PAGE_SIZE })}`), [page, search, period]);

  return (
    <Panel
      title="Everything people did"
      flush
      action={(
        <form className="toolbar" onSubmit={(e) => { e.preventDefault(); setPage(1); setSearch(q); }}>
          <input className="search" placeholder="Search person or action" value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={period} onChange={(e) => { setPage(1); setPeriod(e.target.value); }}>
            <option value="">All time</option>
            <option value="today">Today</option>
            <option value="month">This month</option>
          </select>
        </form>
      )}
    >
      {error ? <Empty text={error} /> : (
        <>
          <Table
            columns={['When', 'Who', 'What they did', 'Details']}
            empty={data ? 'Nothing recorded yet.' : 'Loading…'}
            rows={(data?.items || []).map((row) => ({
              key: row.id,
              cells: [
                fmtDateTime(row.created_at),
                row.username || 'system',
                ACTION_LABELS[row.action] || row.action.replaceAll('_', ' '),
                <code key="d" className="mini">{Object.keys(row.details || {}).length ? JSON.stringify(row.details) : '—'}</code>,
              ],
            }))}
          />
          <Pager page={page} pageSize={PAGE_SIZE} total={data?.total} onPage={setPage} />
        </>
      )}
    </Panel>
  );
}
