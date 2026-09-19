import { useState } from 'react';
import { api, qs } from '../api';
import { Empty, Field, Notice, Pager, Panel, Table, fmtDateTime, label, useAction, useLoad } from '../components/ui';

export default function DncList() {
  const [page, setPage] = useState(1);
  const [q, setQ] = useState('');
  const [search, setSearch] = useState('');
  const { data, error, reload } = useLoad(() => api(`/api/dnc${qs({ q: search, page, page_size: 100 })}`), [search, page]);
  const [busy, run] = useAction();
  const [phones, setPhones] = useState('');
  const [file, setFile] = useState(null);

  const add = (e) => {
    e.preventDefault();
    run('add', async () => {
      const res = await api('/api/dnc', { method: 'POST', body: { phones, reason: 'added manually' } });
      setPhones('');
      await reload();
      return res;
    }, (res) => `Added ${res.added} number(s)${res.invalid ? `, skipped ${res.invalid} invalid` : ''}.`);
  };
  const upload = () => run('upload', async () => {
    const form = new FormData();
    form.append('file', file);
    const res = await api('/api/dnc/upload', { method: 'POST', form });
    setFile(null);
    await reload();
    return res;
  }, (res) => `Added ${res.added} number(s) from the file.`);
  const remove = (x) => {
    if (!window.confirm(`Remove ${x.phone_pretty} from the do-not-call list?`)) return;
    run(x.phone, async () => { await api(`/api/dnc/${encodeURIComponent(x.phone)}`, { method: 'DELETE' }); await reload(); }, 'Removed.');
  };

  return (
    <>
      <Notice>
        Numbers here are never dialed — by the dialer or Call now. The AI adds numbers automatically whenever someone asks
        not to be called. Also scrub your lists against the National Do Not Call Registry before importing.
      </Notice>
      <div className="grid2">
        <Panel title="Add numbers">
          <form className="form" onSubmit={add}>
            <Field label="Phone numbers" hint="One per line, or separated by commas."><textarea rows={4} value={phones} onChange={(e) => setPhones(e.target.value)} /></Field>
            <div className="formActions"><button className="primary" disabled={!phones.trim() || busy === 'add'}>Add to list</button></div>
          </form>
        </Panel>
        <Panel title="Upload a list">
          <div className="form">
            <Field label="CSV or Excel file" hint="Uses the column that looks like a phone number (or the first column).">
              <input type="file" accept=".csv,.xlsx,.xls" onChange={(e) => setFile(e.target.files?.[0] || null)} />
            </Field>
            <div className="formActions"><button className="primary" disabled={!file || busy === 'upload'} onClick={upload}>Upload</button></div>
          </div>
        </Panel>
      </div>
      <Panel title={`Do-not-call numbers${data ? ` · ${data.total}` : ''}`} flush
        action={<form onSubmit={(e) => { e.preventDefault(); setPage(1); setSearch(q); }}><input className="search" placeholder="Search number" value={q} onChange={(e) => setQ(e.target.value)} /></form>}>
        {error ? <Empty text={error} /> : (
          <>
            <Table
              columns={['Number', 'Reason', 'Added by', 'Date', '']}
              empty="The list is empty."
              rows={(data?.items || []).map((x) => ({
                key: x.phone,
                cells: [x.phone_pretty, x.reason || '—', label(x.source), fmtDateTime(x.created_at),
                  <button key="r" className="small ghost" disabled={busy === x.phone} onClick={() => remove(x)}>Remove</button>],
              }))}
            />
            <Pager page={page} pageSize={100} total={data?.total} onPage={setPage} />
          </>
        )}
      </Panel>
    </>
  );
}
