import { useState } from 'react';
import { api } from '../api';
import { Empty, Field, Notice, Panel, Table, fmtDateTime, useAction, useLoad } from '../components/ui';

const FIELD_LABELS = {
  phone: 'Phone number *', first_name: 'First name', last_name: 'Last name', full_name: 'Full name (if no first/last)',
  zip_code: 'ZIP code', state: 'State', email: 'Email', source: 'Lead source', notes: 'Notes for the AI',
  consent_at: 'Consent date', consent_source: 'Consent source',
};

export default function ImportLeads() {
  const [tab, setTab] = useState('file');
  const [preview, setPreview] = useState(null);
  const [result, setResult] = useState(null);
  const history = useLoad(() => api('/api/import/history'), []);
  const sheet = useLoad(() => api('/api/sheets/status'), []);

  const onPreview = (p) => { setResult(null); setPreview(p); };
  const onDone = (r) => { setPreview(null); setResult(r); history.reload(); };

  return (
    <>
      <div className="tabs">
        <button className={tab === 'file' ? 'on' : ''} onClick={() => setTab('file')}>Excel / CSV file</button>
        <button className={tab === 'sheet' ? 'on' : ''} onClick={() => setTab('sheet')}>Google Sheet</button>
      </div>

      {!preview && tab === 'file' && <FileStep onPreview={onPreview} />}
      {!preview && tab === 'sheet' && <SheetStep status={sheet.data} onPreview={onPreview} />}
      {preview && <MappingStep preview={preview} onCancel={() => setPreview(null)} onDone={onDone} />}

      {result && (
        <Notice tone="good">
          Imported <b>{result.imported}</b> of {result.total_rows} rows.
          {result.duplicates ? ` ${result.duplicates} duplicates skipped.` : ''}
          {result.invalid_phone ? ` ${result.invalid_phone} invalid phone numbers skipped.` : ''}
          {result.missing_consent ? ` ${result.missing_consent} rows without a consent date skipped.` : ''}
          {result.dnc_skipped ? ` ${result.dnc_skipped} numbers on the do-not-call list skipped.` : ''}
        </Notice>
      )}

      <Panel title="Recent imports" flush>
        <Table
          columns={['When', 'Source', 'Rows', 'Imported', 'Duplicates', 'Skipped', 'By']}
          empty="No imports yet."
          rows={(history.data?.items || []).map((r) => ({
            key: r.id,
            cells: [fmtDateTime(r.created_at), <span key="s" className="truncate">{r.name}</span>, r.total_rows, r.imported, r.duplicates,
              r.invalid + r.dnc_skipped, r.created_by || '—'],
          }))}
        />
      </Panel>
    </>
  );
}

function FileStep({ onPreview }) {
  const [file, setFile] = useState(null);
  const [busy, run] = useAction();
  const go = () => run('preview', async () => {
    const form = new FormData();
    form.append('file', file);
    onPreview(await api('/api/import/preview', { method: 'POST', form }));
  });
  return (
    <Panel title="1. Choose a file">
      <div className="drop">
        <input type="file" accept=".csv,.xlsx,.xls" onChange={(e) => setFile(e.target.files?.[0] || null)} />
        <p className="muted">Needs a phone number column. Consent date/source columns are recommended.</p>
        <button className="primary" disabled={!file || busy === 'preview'} onClick={go}>{busy === 'preview' ? 'Reading…' : 'Preview'}</button>
      </div>
    </Panel>
  );
}

function SheetStep({ status, onPreview }) {
  const [url, setUrl] = useState(status?.url || '');
  const [busy, run] = useAction();
  const go = (e) => {
    e.preventDefault();
    run('sheet', async () => onPreview(await api('/api/sheets/preview', { method: 'POST', body: { url } })));
  };
  return (
    <Panel title="1. Connect a Google Sheet">
      <form className="form" onSubmit={go}>
        <Field label="Google Sheet link"><input required placeholder="https://docs.google.com/spreadsheets/d/..." value={url} onChange={(e) => setUrl(e.target.value)} /></Field>
        {status?.service_account_email ? (
          <Notice>For a private sheet, click <b>Share</b> in Google Sheets and add <code>{status.service_account_email}</code> as a Viewer.</Notice>
        ) : (
          <Notice tone="warn">No Google service account is configured, so only sheets shared as "Anyone with the link can view" will work.</Notice>
        )}
        {status?.last_sync && <p className="muted">Last read: {fmtDateTime(status.last_sync)}</p>}
        <div className="formActions"><button className="primary" disabled={busy === 'sheet'}>{busy === 'sheet' ? 'Reading sheet…' : 'Preview sheet'}</button></div>
      </form>
    </Panel>
  );
}

function MappingStep({ preview, onCancel, onDone }) {
  const [mapping, setMapping] = useState(() => ({ ...preview.detected_mapping }));
  const hasConsentCol = Boolean(preview.detected_mapping.consent_at);
  const [consentMode, setConsentMode] = useState(hasConsentCol ? 'column' : 'attest');
  const [attest, setAttest] = useState(false);
  const [attestSource, setAttestSource] = useState('');
  const [busy, run] = useAction();

  const commit = () => run('commit', async () => {
    onDone(await api('/api/import/commit', {
      method: 'POST',
      body: { token: preview.token, mapping, consent_mode: consentMode, attest, attest_source: attestSource },
    }));
  });

  const canImport = mapping.phone && (consentMode === 'column' ? mapping.consent_at : attest && attestSource.trim().length >= 3);
  const mappedCols = preview.headers;

  return (
    <>
      <Panel title={`2. Match the columns · ${preview.total_rows} rows in ${preview.name}`}>
        <div className="mappingGrid">
          {Object.keys(FIELD_LABELS).map((field) => (
            <Field key={field} label={FIELD_LABELS[field]}>
              <select value={mapping[field] || ''} onChange={(e) => setMapping({ ...mapping, [field]: e.target.value || null })}>
                <option value="">— not in this file —</option>
                {mappedCols.map((h) => <option key={h} value={h}>{h}</option>)}
              </select>
            </Field>
          ))}
        </div>
      </Panel>

      <Panel title="3. Consent to be contacted">
        <div className="form">
          <label className="radio">
            <input type="radio" checked={consentMode === 'column'} onChange={() => setConsentMode('column')} />
            <span><b>Each row has a consent date column.</b> Rows without a date are skipped.</span>
          </label>
          <label className="radio">
            <input type="radio" checked={consentMode === 'attest'} onChange={() => setConsentMode('attest')} />
            <span><b>Every lead in this list opted in</b>, but there's no consent column.</span>
          </label>
          {consentMode === 'attest' && (
            <div className="attestBox">
              <Field label="Where did the consent come from?" hint="e.g. 'Medicare info web form on example.com with TCPA/AI-call disclosure, Sept 2026'.">
                <input value={attestSource} onChange={(e) => setAttestSource(e.target.value)} />
              </Field>
              <label className="check">
                <input type="checkbox" checked={attest} onChange={(e) => setAttest(e.target.checked)} />
                I confirm every person in this list gave written permission to be contacted about Medicare options by phone,
                including automated/AI-voice calls, and the list has been checked against the National Do Not Call Registry.
              </label>
            </div>
          )}
        </div>
      </Panel>

      <Panel title="Preview (first 10 rows)" flush>
        <Table columns={preview.headers} rows={preview.sample_rows.map((r, i) => ({ key: i, cells: preview.headers.map((h) => r[h] ?? '') }))} />
      </Panel>

      <div className="stickyActions">
        <button className="ghost" onClick={onCancel}>Cancel</button>
        {!mapping.phone && <span className="bad">Choose the phone number column.</span>}
        <button className="primary" disabled={!canImport || busy === 'commit'} onClick={commit}>
          {busy === 'commit' ? 'Importing…' : `Import ${preview.total_rows} rows`}
        </button>
      </div>
      {preview.total_rows === 0 && <Empty text="No rows to import." />}
    </>
  );
}
