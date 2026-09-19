import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api';
import { Badge, Empty, Notice, Panel, fmtDateTime, fmtDuration, fmtPhone, fullName, label, useAction, useLoad } from '../components/ui';

function Info({ k, v }) {
  return <div className="info"><span>{k}</span><b>{v ?? '—'}</b></div>;
}

export default function CallDetails() {
  const { id } = useParams();
  const { data, error, reload } = useLoad(() => api(`/api/calls/${id}`), [id], 5000);
  const [recording, setRecording] = useState(null);
  const [busy, run] = useAction();

  if (error) return <Panel title="Call details"><Empty text={error} /></Panel>;
  if (!data) return <Panel title="Call details"><Empty text="Loading…" /></Panel>;

  const q = data.qualification || {};
  const loadRecording = () => run('rec', async () => setRecording((await api(`/api/calls/${id}/recording`)).url));
  const live = data.status !== 'ENDED';
  const hangup = () => {
    if (!window.confirm('Hang up this call now?')) return;
    run('hangup', async () => { await api(`/api/calls/${id}/hangup`, { method: 'POST' }); await reload(); }, 'Call ended.');
  };

  return (
    <>
      <div className="backRow"><Link to="/calls">← Call history</Link></div>
      <div className="grid2">
        <Panel title="Call summary">
          <Info k="Lead" v={data.lead ? <Link to={`/leads?q=${encodeURIComponent(data.phone)}`}>{fullName(data.lead)}</Link> : data.lead_name} />
          <Info k="Phone" v={data.phone_pretty} />
          <Info k="Where they are" v={[data.lead_zip, data.lead_state].filter(Boolean).join(', ') || '—'} />
          <Info k="Lead came from" v={data.lead_source || '—'} />
          <Info k="AI agent" v={data.agent_name || '—'} />
          <Info k="What happened" v={data.what_happened} />
          <Info k="Status" v={<Badge value={data.status} />} />
          <Info k="Outcome" v={data.outcome ? <Badge value={data.outcome} /> : '—'} />
          <Info k="Started" v={fmtDateTime(data.started_at)} />
          <Info k="Answered" v={fmtDateTime(data.answered_at)} />
          <Info k="Transferred" v={fmtDateTime(data.transferred_at)} />
          <Info k="Ended" v={fmtDateTime(data.ended_at)} />
          <Info k="Talk time" v={fmtDuration(data.talk_seconds ?? data.elapsed_seconds)} />
          <Info k="Whole call" v={fmtDuration(data.duration_seconds)} />
          <Info k="Closer" v={data.closer_name} />
          <Info k="Voicemail check" v={data.amd_result ? label(data.amd_result) : '—'} />
          <Info k="Hang-up reason" v={data.hangup_cause ? label(data.hangup_cause) : '—'} />
          {(data.callbacks || []).length > 0 && (
            <Info k="Callback booked" v={data.callbacks.map((cb) => `${fmtDateTime(cb.scheduled_for)} (${cb.local_time})`).join(', ')} />
          )}
          {data.needs_closer_followup && (
            <Notice tone="warn">
              Every closer was busy on this call. The lead is on the <b>Closer Call-Backs</b> list
              {data.followup_status === 'done' ? ' and has been called back.' : ' and is still waiting.'}
            </Notice>
          )}
          {live && (
            <div className="formActions">
              <button className="ghost danger" disabled={busy === 'hangup'} onClick={hangup}>Hang up this call</button>
            </div>
          )}
          <div className="recording">
            {recording ? <audio controls src={recording} /> : (
              <button className="small" disabled={busy === 'rec' || !(data.recording_id || data.recording_url)} onClick={loadRecording}>
                {data.recording_id || data.recording_url ? 'Play recording' : 'Recording not available yet'}
              </button>
            )}
          </div>
        </Panel>
        <Panel title="What the AI learned">
          <Info k="Has Medicare Part A & B" v={q.has_medicare_parts_a_and_b === undefined ? '—' : q.has_medicare_parts_a_and_b ? 'Yes' : 'No'} />
          <Info k="Turning 65 within 3 months" v={q.turning_65_within_3_months === undefined ? '—' : q.turning_65_within_3_months ? 'Yes' : 'No'} />
          <Info k="ZIP code" v={q.zip_code} />
          <Info k="Current coverage" v={q.current_coverage} />
          <Info k="Wants licensed agent" v={q.wants_licensed_agent === undefined ? '—' : q.wants_licensed_agent ? 'Yes' : 'No'} />
          <Info k="Notes" v={q.notes} />
          <h3 className="subhead">Transfer attempts</h3>
          {(data.transfer_attempts || []).length === 0 ? <p className="muted">No transfer on this call.</p> : (
            <ol className="attempts">
              {data.transfer_attempts.map((a) => (
                <li key={a.id}>
                  <div><b>{a.closer_name}</b> <span className="sub">{fmtPhone(a.destination)}</span></div>
                  <div><Badge value={a.status} /> {a.reason && <span className="sub">{label(a.reason)}</span>}</div>
                </li>
              ))}
            </ol>
          )}
        </Panel>
      </div>
      <div className="grid2">
        <Panel title="Transcript">
          {(data.transcript || []).length === 0 ? <Empty text={data.status === 'ENDED' ? 'No conversation was recorded for this call.' : 'The transcript appears when the AI part of the call ends.'} /> : (
            <div className="transcript">
              {data.transcript.map((line, i) => (
                <div key={i} className={`line ${line.role}`}><span>{line.role === 'agent' ? 'AI' : 'Lead'}</span><p>{line.text}</p></div>
              ))}
            </div>
          )}
        </Panel>
        <Panel title="Telephony timeline">
          <div className="timeline">
            {(data.events || []).map((e, i) => (
              <div className="event" key={i}><i /><div><b>{e.event_type}</b><span>{fmtDateTime(e.created_at)}</span></div></div>
            ))}
            {!(data.events || []).length && <Empty text="No events yet." />}
          </div>
        </Panel>
      </div>
    </>
  );
}
