import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, getToken, qs } from '../api';
import { Empty, Notice, Panel, Table, fmtDateTime, useAction, useLoad } from '../components/ui';

/**
 * Leads the AI couldn't hand over because every closer was busy. A licensed agent rings
 * these people back themselves — the dialer never calls them again.
 */
export default function Followups() {
  const nav = useNavigate();
  const [status, setStatus] = useState('pending');
  const { data, error, reload } = useLoad(() => api(`/api/followups${qs({ status })}`), [status], 15000);
  const [busy, run] = useAction();

  const mark = (row, next) => run(row.id, async () => {
    await api(`/api/followups/${row.id}`, { method: 'PATCH', body: { status: next } });
    await reload();
  }, next === 'done' ? 'Marked as called back.' : 'Put back on the list.');

  const download = async () => {
    const res = await fetch(`/api/followups.csv${qs({ status })}`, { headers: { Authorization: `Bearer ${getToken()}` } });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'closer-callbacks.csv';
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <Panel
        title="Leads waiting for a closer to call back"
        flush
        action={(
          <div className="toolbar">
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="pending">Still to call ({data?.pending ?? 0})</option>
              <option value="done">Already called ({data?.done ?? 0})</option>
            </select>
            <button className="ghost" onClick={download}>Download CSV</button>
          </div>
        )}
      >
        {error ? <Empty text={error} /> : (
          <Table
            columns={['Lead', 'Where', 'Their local time', 'AI agent', 'When the AI called', 'What the AI learned', '']}
            empty={data ? 'Nobody is waiting — every lead reached a closer.' : 'Loading…'}
            onRowClick={(r) => nav(`/calls/${r.key}`)}
            rows={(data?.items || []).map((c) => {
              const q = c.qualification || {};
              const learned = [
                q.has_medicare_parts_a_and_b ? 'Has Part A & B' : null,
                q.current_coverage || null,
                q.wants_licensed_agent ? 'Wants an agent' : null,
              ].filter(Boolean).join(' · ');
              return {
                key: c.id,
                cells: [
                  <div key="l"><b>{c.lead_name || '—'}</b><div className="sub">{c.phone_pretty}</div></div>,
                  `${c.lead_zip || '—'}${c.lead_state ? `, ${c.lead_state}` : ''}`,
                  <span key="t" className="sub">{c.local_time}</span>,
                  c.agent_name || '—',
                  fmtDateTime(c.started_at),
                  learned || '—',
                  <div key="a" className="rowActions" onClick={(e) => e.stopPropagation()}>
                    {status === 'pending'
                      ? <button className="small" disabled={busy === c.id} onClick={() => mark(c, 'done')}>A closer called them</button>
                      : <button className="small ghost" disabled={busy === c.id} onClick={() => mark(c, 'pending')}>Put back</button>}
                  </div>,
                ],
              };
            })}
          />
        )}
      </Panel>
      <Notice>
        These people were qualified and wanted to talk to a licensed agent, but nobody was free at that moment.
        The AI told them an agent would call back — work through this list and mark each one once you have.
      </Notice>
    </>
  );
}
