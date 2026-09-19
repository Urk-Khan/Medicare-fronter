import { useEffect, useState } from 'react';
import { api, isSuperAdmin } from '../api';
import { Empty, Field, Notice, Panel, useAction, useLoad } from '../components/ui';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const ZONES = ['America/New_York', 'America/Chicago', 'America/Denver', 'America/Phoenix', 'America/Los_Angeles',
  'America/Anchorage', 'Pacific/Honolulu', 'America/Puerto_Rico'];

export default function Settings() {
  const { data, error, reload } = useLoad(() => api('/api/settings'), []);
  const sys = useLoad(() => api('/api/system/status'), []);
  const [v, setV] = useState(null);
  const [busy, run] = useAction();
  const superAdmin = isSuperAdmin();

  useEffect(() => { if (data) setV(data.values); }, [data]);
  if (error) return <Panel title="Settings"><Empty text={error} /></Panel>;
  if (!v) return <Panel title="Settings"><Empty text="Loading…" /></Panel>;

  const set = (k) => (e) => setV({ ...v, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value });
  const toggleDay = (i) => setV({ ...v, calling_days: v.calling_days.includes(i) ? v.calling_days.filter((d) => d !== i) : [...v.calling_days, i].sort() });
  const save = () => run('save', async () => {
    const keys = superAdmin ? Object.keys(v) : Object.keys(v).filter((k) => !(data.super_admin_keys || []).includes(k));
    const body = Object.fromEntries(keys.map((k) => [k, v[k]]));
    await api('/api/settings', { method: 'PUT', body });
    await reload();
  }, 'Settings saved. They take effect within a few seconds.');
  const s = sys.data;

  return (
    <>
      <div className="grid2">
        <Panel title="Company & what the AI says about you">
          <div className="form">
            <Field label="Company name" hint="Spoken in the opening line of every call."><input value={v.company_name} onChange={set('company_name')} /></Field>
            <Field label="Call-back number" hint="Given out if a lead wants to call you back. Write it the way it should be spoken, e.g. 8 8 8, 5 5 5, 0 1 9 9.">
              <input value={v.company_callback_number} onChange={set('company_callback_number')} />
            </Field>
            <div className="row2">
              <Field label="Organizations you represent" hint="Used in the Medicare disclaimer."><input value={v.tpmo_org_count} onChange={set('tpmo_org_count')} /></Field>
              <Field label="Products offered" hint="Used in the Medicare disclaimer."><input value={v.tpmo_product_count} onChange={set('tpmo_product_count')} /></Field>
            </div>
            <p className="fieldHint">Each AI agent&apos;s name, voice and script live on the AI Agents page.</p>
          </div>
        </Panel>

        <Panel title="Calling hours (in each lead's own time zone)">
          <div className="form">
            <div className="row2">
              <Field label="Start"><input type="time" value={v.calling_start_local} onChange={set('calling_start_local')} /></Field>
              <Field label="End"><input type="time" value={v.calling_end_local} onChange={set('calling_end_local')} /></Field>
            </div>
            <p className="fieldHint">The system never calls before 8:00 AM or after 9:00 PM local time, whatever is set here. Some states are stricter (Florida: 8 PM).</p>
            <Field label="Days">
              <div className="dayPicker">
                {DAYS.map((d, i) => <button key={d} type="button" className={v.calling_days.includes(i) ? 'on' : ''} onClick={() => toggleDay(i)}>{d}</button>)}
              </div>
            </Field>
            <Field label="Time zone when a number's area code doesn't tell us">
              <select value={v.default_timezone} onChange={set('default_timezone')}>
                {[...new Set([v.default_timezone, ...ZONES])].map((z) => <option key={z} value={z}>{z}</option>)}
              </select>
            </Field>
          </div>
        </Panel>
      </div>

      {superAdmin ? (
        <>
          <div className="grid2">
            <Panel title="Dialer">
              <div className="form">
                <div className="row2">
                  <Field label="Calls at the same time" hint="Across all AI agents. Each agent also has its own limit."><input type="number" min={1} max={50} value={v.max_concurrent_calls} onChange={set('max_concurrent_calls')} /></Field>
                  <Field label="Ring before giving up (seconds)"><input type="number" min={10} max={60} value={v.ring_timeout_seconds} onChange={set('ring_timeout_seconds')} /></Field>
                </div>
                <div className="row2">
                  <Field label="Voicemail detection" hint="Detects and hangs up on answering machines instead of talking to them.">
                    <select value={v.amd_mode} onChange={set('amd_mode')}>
                      <option value="detect">Standard (recommended)</option>
                      <option value="premium">Premium (more accurate, costs more)</option>
                      <option value="disabled">Off</option>
                    </select>
                  </Field>
                  <Field label="Max AI call length (minutes)"><input type="number" min={2} max={30} value={v.max_ai_call_minutes} onChange={set('max_ai_call_minutes')} /></Field>
                </div>
              </div>
            </Panel>

            <Panel title="Transfers to closers">
              <div className="form">
                <Field label="Ring each closer for (seconds)" hint="Then the next free closer is tried. Keep it shorter than their voicemail pickup.">
                  <input type="number" min={8} max={60} value={v.closer_ring_seconds} onChange={set('closer_ring_seconds')} />
                </Field>
                <label className="check">
                  <input type="checkbox" checked={v.closer_require_accept} onChange={set('closer_require_accept')} />
                  Closer hears who&apos;s waiting and must press 1 to accept (recommended — stops voicemail from taking the transfer)
                </label>
                <Notice>
                  When every closer is busy, the AI tells the lead a licensed agent will call back and puts them on the
                  <b> Closer Call-Backs</b> list. It never books the callback itself and never calls them again.
                </Notice>
              </div>
            </Panel>
          </div>

          <Panel title="Trying again when a call doesn't reach anybody">
            <div className="form">
              <div className="retryGrid">
                <div className="retryHead"><span /><span>Wait (hours)</span><span>Times to try again</span></div>
                <RetryRow label="Nobody answered" v={v} set={set} hours="retry_no_answer_hours" max="retry_no_answer_max" />
                <RetryRow label="Line was busy" v={v} set={set} hours="retry_busy_hours" max="retry_busy_max" />
                <RetryRow label="Voicemail picked up" v={v} set={set} hours="retry_voicemail_hours" max="retry_voicemail_max" />
                <RetryRow label="They hung up on the AI" v={v} set={set} hours="retry_hangup_hours" max="retry_hangup_max" />
              </div>
              <div className="row2">
                <Field label="Never try again after this many attempts in total"><input type="number" min={0} max={20} value={v.max_retries} onChange={set('max_retries')} /></Field>
                <Field label="Try again only after this time of day" hint="Lead's own local time. Leave empty to call again as soon as the wait is up.">
                  <input type="time" value={v.retry_time_of_day || ''} onChange={set('retry_time_of_day')} />
                </Field>
              </div>
            </div>
          </Panel>

          <Panel title="Safety brake">
            <div className="form">
              <label className="check">
                <input type="checkbox" checked={v.brake_enabled} onChange={set('brake_enabled')} />
                Pause automatic calling by itself when calls keep failing
              </label>
              <div className="row2">
                <Field label="Stop after this many failed calls in a row"><input type="number" min={2} max={50} value={v.brake_consecutive_failures} onChange={set('brake_consecutive_failures')} /></Field>
                <Field label="…or when this share of recent calls failed (%)"><input type="number" min={10} max={100} value={v.brake_window_failure_percent} onChange={set('brake_window_failure_percent')} /></Field>
              </div>
              <Field label="How many recent calls count as 'recent'"><input type="number" min={5} max={200} value={v.brake_window_calls} onChange={set('brake_window_calls')} /></Field>
            </div>
          </Panel>
        </>
      ) : (
        <Notice>
          The dialer, retry, transfer and safety settings are managed by your super admin.
        </Notice>
      )}

      <div className="stickyActions">
        <button className="primary" disabled={busy === 'save'} onClick={save}>Save settings</button>
      </div>

      {s && (
        <Panel title="System check">
          <div className="checks">
            <Check ok={s.database === 'ok'} text="Database tables" fix="Run backend/app/db/schema.sql in the Supabase SQL Editor." />
            <Check ok={s.telnyx === 'ok'} text="Telnyx connection details" fix="Set TELNYX_API_KEY, TELNYX_CONNECTION_ID and TELNYX_FROM_NUMBER in backend/.env." />
            <Check ok={s.webhook_signature_check} text="Telnyx webhook signature check" fix="Set TELNYX_WEBHOOK_PUBLIC_KEY in backend/.env." />
            <Check ok={s.public_url_ok} text={`Public URL: ${s.public_base_url}`} fix="Start the backend with start.py (it opens a secure tunnel)." />
            <Check ok={s.llm_key} text={`AI model: ${s.llm}`} fix="Set the LLM API key in backend/.env." />
            <Check ok={s.cartesia_key} text="Cartesia voice + speech recognition" fix="Set CARTESIA_API_KEY in backend/.env." />
            <Check ok={s.agents_ready > 0} text={`${s.agents_ready} AI agent(s) switched on`} fix="Set one up on the AI Agents page." />
            <Check ok={s.closers_ready > 0} text={`${s.closers_ready} closer(s) with a phone number`} fix="Add numbers on the Closers page." />
            <Check ok={Boolean(s.google_service_account_email)} optional text={s.google_service_account_email ? `Google service account: ${s.google_service_account_email}` : 'Google service account (optional)'} fix="Put credentials.json in the backend folder to import private sheets." />
          </div>
          <Notice>API keys and connection details live in <code>backend/.env</code> and are never shown in the browser.</Notice>
        </Panel>
      )}
    </>
  );
}

function RetryRow({ label: l, v, set, hours, max }) {
  return (
    <div className="retryRow">
      <span>{l}</span>
      <input type="number" step="0.5" min={0.25} value={v[hours]} onChange={set(hours)} aria-label={`${l} wait in hours`} />
      <input type="number" min={0} max={10} value={v[max]} onChange={set(max)} aria-label={`${l} number of tries`} />
    </div>
  );
}

function Check({ ok, text, fix, optional }) {
  return (
    <div className={`check-row ${ok ? 'ok' : optional ? 'opt' : 'bad'}`}>
      <i>{ok ? '✓' : optional ? '–' : '!'}</i>
      <div><b>{text}</b>{!ok && <div className="sub">{fix}</div>}</div>
    </div>
  );
}
