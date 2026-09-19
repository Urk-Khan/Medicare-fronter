import { useEffect, useState } from 'react';
import { api } from '../api';
import { Badge, Empty, Field, Modal, Notice, Panel, Table, fmtPhone, useAction, useLoad } from '../components/ui';

/**
 * One row per AI agent. Each agent has its own name, voice, phone number, call limit and
 * script — so several of them can work the same lead list at the same time.
 */
export default function Agents() {
  const { data, error, reload } = useLoad(() => api('/api/agents'), [], 8000);
  const defaults = useLoad(() => api('/api/script/defaults'), []);
  const [editing, setEditing] = useState(null);
  const [busy, run] = useAction();

  const items = data?.items || [];

  const patch = (agent, body, msg) => run(agent.id, async () => {
    await api(`/api/agents/${agent.id}`, { method: 'PATCH', body });
    await reload();
  }, msg);

  const remove = (agent) => {
    if (!window.confirm(`Remove ${agent.name}? Calls already made by this agent are kept.`)) return;
    run(agent.id, async () => { await api(`/api/agents/${agent.id}`, { method: 'DELETE' }); await reload(); }, 'Agent removed.');
  };

  return (
    <>
      <Panel
        title="AI agents"
        flush
        action={(
          <button className="primary" onClick={() => setEditing({
            name: '', label: `Agent ${items.length + 1}`, enabled: true, voice_id: '', from_number: '',
            max_concurrent_calls: 3, priority: items.length + 1,
            opening_line: defaults.data?.opening_line || '', system_prompt: defaults.data?.system_prompt || '',
            disclaimer_text: defaults.data?.disclaimer_text || '',
          })}
          >
            Add agent
          </button>
        )}
      >
        {error ? <Empty text={error} /> : (
          <Table
            columns={['Agent', 'Phone number it calls from', 'Voice', 'At once', 'Live now', 'Order', 'Status', '']}
            empty={data ? 'No AI agents yet — add one to start calling.' : 'Loading…'}
            rows={items.map((a) => ({
              key: a.id,
              cells: [
                <div key="n"><b>{a.name}</b>{a.label && <div className="sub">{a.label}</div>}</div>,
                a.from_number ? fmtPhone(a.from_number) : <span className="sub">Main number</span>,
                a.voice_id ? <code className="mini">{a.voice_id.slice(0, 8)}…</code> : <span className="sub">Default voice</span>,
                a.max_concurrent_calls,
                a.live_calls || 0,
                a.priority,
                a.enabled ? <Badge key="s" value="available" text="On" /> : <Badge key="s" value="cancelled" text="Off" />,
                <div key="a" className="rowActions">
                  <button className="small ghost" onClick={() => setEditing(a)}>Edit</button>
                  <button className="small ghost" disabled={busy === a.id} onClick={() => patch(a, { enabled: !a.enabled }, a.enabled ? 'Agent switched off.' : 'Agent switched on.')}>
                    {a.enabled ? 'Switch off' : 'Switch on'}
                  </button>
                  <button className="small ghost danger" disabled={busy === a.id} onClick={() => remove(a)}>Remove</button>
                </div>,
              ],
            }))}
          />
        )}
      </Panel>

      <Notice>
        Every agent works the same lead list, and a lead is only ever called by one of them. Leave the phone number
        blank to use your main Telnyx number, and the voice blank to use the default voice.
      </Notice>

      {editing && (
        <AgentModal
          agent={editing}
          placeholders={defaults.data?.placeholders || {}}
          onClose={() => setEditing(null)}
          onSaved={async () => { setEditing(null); await reload(); }}
        />
      )}
    </>
  );
}

function AgentModal({ agent, placeholders, onClose, onSaved }) {
  const isNew = !agent.id;
  const [form, setForm] = useState(agent);
  const [preview, setPreview] = useState(null);
  const [busy, run] = useAction();
  useEffect(() => setForm(agent), [agent]);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value });

  const save = (e) => {
    e.preventDefault();
    const body = {
      name: form.name, label: form.label, enabled: form.enabled, voice_id: form.voice_id,
      from_number: form.from_number, max_concurrent_calls: Number(form.max_concurrent_calls),
      priority: Number(form.priority), opening_line: form.opening_line, system_prompt: form.system_prompt,
      disclaimer_text: form.disclaimer_text,
    };
    run('save', async () => {
      if (isNew) await api('/api/agents', { method: 'POST', body });
      else await api(`/api/agents/${agent.id}`, { method: 'PATCH', body });
      await onSaved();
    }, isNew ? 'Agent added.' : 'Agent saved.');
  };

  const showPreview = () => run('preview', async () => {
    setPreview(await api('/api/agents/preview', {
      method: 'POST',
      body: { name: form.name || 'Ava', opening_line: form.opening_line, system_prompt: form.system_prompt, disclaimer_text: form.disclaimer_text },
    }));
  });

  const resetScript = () => {
    if (!window.confirm('Replace this agent’s script with the default Medicare script?')) return;
    run('reset', async () => {
      await api(`/api/agents/${agent.id}/reset-script`, { method: 'POST' });
      await onSaved();
    }, 'Script reset to the default.');
  };

  return (
    <Modal title={isNew ? 'Add an AI agent' : `Edit ${agent.name}`} onClose={onClose} wide>
      <form className="form" onSubmit={save}>
        <div className="row2">
          <Field label="Name the AI uses on the phone" hint="e.g. Ava. The caller hears this name."><input required value={form.name} onChange={set('name')} /></Field>
          <Field label="Internal label" hint="Only you see this, e.g. 'Morning shift'."><input value={form.label} onChange={set('label')} /></Field>
        </div>
        <div className="row2">
          <Field label="Phone number it calls from" hint="Leave empty to use your main Telnyx number."><input value={form.from_number} onChange={set('from_number')} placeholder="(305) 555-0123" /></Field>
          <Field label="Cartesia voice ID" hint="Leave empty for the default voice."><input value={form.voice_id} onChange={set('voice_id')} /></Field>
        </div>
        <div className="row2">
          <Field label="Calls at the same time" hint="How many people this agent talks to at once."><input type="number" min={1} max={20} value={form.max_concurrent_calls} onChange={set('max_concurrent_calls')} /></Field>
          <Field label="Order" hint="1 is used first when several agents are free."><input type="number" min={1} value={form.priority} onChange={set('priority')} /></Field>
        </div>
        <label className="check">
          <input type="checkbox" checked={form.enabled} onChange={set('enabled')} /> This agent can make calls
        </label>

        <h3 className="subhead">What this agent says</h3>
        <Field label="Opening line" hint="Spoken the moment the lead answers.">
          <textarea className="mono" rows={3} value={form.opening_line} onChange={set('opening_line')} />
        </Field>
        <Field label="Medicare disclaimer" hint="Read word for word in the first minute. Required by CMS.">
          <textarea className="mono" rows={4} value={form.disclaimer_text} onChange={set('disclaimer_text')} />
        </Field>
        <Field label="Instructions the AI follows">
          <textarea className="mono tall" rows={16} value={form.system_prompt} onChange={set('system_prompt')} />
        </Field>
        <div className="placeholders">
          {Object.entries(placeholders).map(([k, desc]) => (
            <span key={k} title={desc} className="chip">{`{{${k}}}`}</span>
          ))}
        </div>

        <div className="formActions">
          {!isNew && <button type="button" className="ghost danger" disabled={busy === 'reset'} onClick={resetScript}>Reset to default script</button>}
          <button type="button" className="ghost" disabled={busy === 'preview'} onClick={showPreview}>Preview with a sample lead</button>
          <button className="primary" disabled={busy === 'save'}>{isNew ? 'Add agent' : 'Save agent'}</button>
        </div>
      </form>

      {preview && (
        <Modal title="Preview with a sample lead" onClose={() => setPreview(null)} wide>
          <Field label="Opening line"><div className="previewBox">{preview.opening_line}</div></Field>
          <Field label="Disclaimer"><div className="previewBox">{preview.disclaimer}</div></Field>
          <Field label="Instructions the AI receives"><pre className="previewBox pre">{preview.system_prompt}</pre></Field>
        </Modal>
      )}
    </Modal>
  );
}
