import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, qs } from '../api';
import { Badge, Empty, Panel, Stat, Table, fmtDuration, fmtPhone, label, useLoad } from '../components/ui';

const PERIODS = [['today', 'Today'], ['month', 'This month']];

export default function Dashboard() {
  const nav = useNavigate();
  const [period, setPeriod] = useState('today');
  const [agentId, setAgentId] = useState('');
  const { data, error } = useLoad(() => api(`/api/dashboard${qs({ period, agent_id: agentId })}`), [period, agentId], 5000);
  const cards = useLoad(() => api(`/api/scorecards${qs({ period })}`), [period], 20000);

  if (error) return <Panel title="Dashboard"><Empty text={error} /></Panel>;
  if (!data) return <Panel title="Dashboard"><Empty text="Loading…" /></Panel>;

  const leads = data.leads || {};
  const t = data.totals || {};
  const outcomes = Object.entries(data.by_outcome || {}).sort((a, b) => b[1] - a[1]);
  const maxOutcome = Math.max(1, ...outcomes.map(([, n]) => n));
  const closers = data.closers || [];
  const periodWord = period === 'month' ? 'this month' : 'today';
  const agents = data.agents || [];

  return (
    <>
      <div className="filterBar">
        <label>
          <span>Show</span>
          <select value={period} onChange={(e) => setPeriod(e.target.value)}>
            {PERIODS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
        <label>
          <span>AI agent</span>
          <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            <option value="">All agents</option>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.name}{a.label ? ` · ${a.label}` : ''}</option>)}
          </select>
        </label>
      </div>

      <section className="stats">
        <Stat label="Live calls" value={data.active_calls.length} tone="accent" />
        <Stat label={`Calls ${periodWord}`} value={t.calls} hint={`${t.answered} answered · ${t.answer_rate}%`} />
        <Stat label={`Transfers ${periodWord}`} value={t.transferred} hint={`${t.transfer_rate}% of answered`} />
        <Stat label="Average call time" value={fmtDuration(t.avg_talk_seconds)} hint="answered calls" />
        <Stat label="Waiting for a closer" value={data.followups_pending} hint="closers call these back" />
        <Stat label="Leads waiting" value={(leads.new || 0) + (leads.retry_scheduled || 0)} hint={`${leads.total} total leads`} />
        <Stat label="Closers free" value={closers.filter((c) => c.enabled && c.availability === 'available' && c.status === 'FREE' && c.destination).length} hint={`of ${closers.filter((c) => c.enabled && c.destination).length} ready`} />
        <Stat label="Do not call" value={leads.do_not_call} />
      </section>

      <div className="grid2">
        <Panel title="Calls happening now" action={<Link to="/calls?outcome=live">Open →</Link>} flush>
          <Table
            columns={['Lead', 'Agent', 'What’s happening', 'Time']}
            empty="No calls right now."
            onRowClick={(r) => nav(`/calls/${r.key}`)}
            rows={data.active_calls.map((c) => ({
              key: c.id,
              cells: [
                <div key="n"><b>{c.lead_name || '—'}</b><div className="sub">{c.phone_pretty}</div></div>,
                c.agent_name || '—',
                <Badge key="s" value={c.status} />,
                fmtDuration(c.elapsed_seconds),
              ],
            }))}
          />
        </Panel>

        <Panel title="Closers" action={<Link to="/closers">Manage →</Link>} flush>
          <div className="closerList">
            {closers.map((c) => (
              <div className="closerRow" key={c.id}>
                <span className={`dot ${!c.enabled || !c.destination ? 'off' : c.status !== 'FREE' ? 'busy' : c.availability === 'away' ? 'away' : 'free'}`} />
                <div className="grow"><b>{c.name}</b><div className="sub">{c.destination ? fmtPhone(c.destination) : 'No number set'}</div></div>
                {!c.enabled ? <Badge value="cancelled" text="Disabled" /> : !c.destination ? <Badge value="none" text="Needs number" /> : c.status !== 'FREE' ? <Badge value={c.status} /> : <Badge value={c.availability} />}
              </div>
            ))}
            {!closers.length && <Empty text="No closers yet." />}
          </div>
        </Panel>
      </div>

      <div className="grid2">
        <Panel title={`How each AI agent did ${periodWord}`} flush>
          <Table
            columns={['Agent', 'Calls', 'Answered', 'Transfers', 'Transfer rate', 'Average call']}
            empty={cards.data ? 'No agents yet.' : 'Loading…'}
            rows={(cards.data?.agents || []).map((a) => ({
              key: a.id || a.name,
              cells: [
                <div key="n"><b>{a.name}</b>{a.label && <div className="sub">{a.label}</div>}</div>,
                a.calls, `${a.answered} (${a.answer_rate}%)`, a.transferred, `${a.transfer_rate}%`,
                fmtDuration(a.avg_talk_seconds),
              ],
            }))}
          />
        </Panel>
        <Panel title={`How each closer did ${periodWord}`} flush>
          <Table
            columns={['Closer', 'Offered', 'Accepted', 'Missed', 'Picks up in', 'Average call']}
            empty={cards.data ? 'No closers yet.' : 'Loading…'}
            rows={(cards.data?.closers || []).map((c) => ({
              key: c.id,
              cells: [
                <div key="n"><b>{c.name}</b><div className="sub">{c.accept_rate}% accepted</div></div>,
                c.offered, c.accepted, c.missed,
                c.avg_pickup_seconds ? `${c.avg_pickup_seconds}s` : '—',
                fmtDuration(c.avg_talk_seconds),
              ],
            }))}
          />
        </Panel>
      </div>

      <div className="grid2">
        <Panel title={`What happened on calls ${periodWord}`}>
          {outcomes.length === 0 ? <Empty text={`No finished calls ${periodWord}.`} /> : (
            <div className="bars">
              {outcomes.map(([k, n]) => (
                <div className="barRow" key={k}>
                  <span className="barLabel">{label(k)}</span>
                  <div className="barTrack"><div className="barFill" style={{ width: `${(n / maxOutcome) * 100}%` }} /></div>
                  <span className="barValue">{n}</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
        <Panel title="Lead pipeline">
          <div className="pipeline">
            {[['new', 'New'], ['retry_scheduled', 'Retry scheduled'], ['callback_scheduled', 'Callback booked'],
              ['awaiting_closer', 'Waiting for a closer'], ['transferred', 'Transferred'], ['not_interested', 'Not interested'],
              ['not_qualified', 'No Medicare'], ['no_answer_final', 'Unreachable'], ['do_not_call', 'Do not call']].map(([k, l]) => (
              <Link key={k} to={`/leads?status=${k}`} className="pipeItem"><span>{l}</span><b>{leads[k] || 0}</b></Link>
            ))}
            {leads.missing_consent > 0 && (
              <div className="pipeItem warn"><span>Missing consent (won&apos;t be called)</span><b>{leads.missing_consent}</b></div>
            )}
          </div>
        </Panel>
      </div>
    </>
  );
}
