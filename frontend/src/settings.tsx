import {useState, type FormEvent} from 'react';
import {api, errorText, type BoardSettings, type OperatorSettings, type SettingsValues} from './model';

function operatorFields({provider, model, manage_resources, approve_inbox, reply_messages, review_status, instructions}: OperatorSettings): OperatorSettings {
  return {provider, model, manage_resources, approve_inbox, reply_messages, review_status, instructions};
}

function values(settings: BoardSettings): SettingsValues {
  const {preferred_phase_minutes, auto_approve_inbox, preserve_interactivity, presets} = settings;
  return {...operatorFields(settings), preferred_phase_minutes, auto_approve_inbox, preserve_interactivity, presets};
}

const operatorOptions = [
  ['manage_resources', 'Assist the queue', 'Ask holders for updates and promote waiting reservations.'],
  ['approve_inbox', 'Review inbox read requests', 'Let the model decide which routine reads to approve.'],
  ['reply_messages', 'Reply to session messages', 'Review direct and broadcast messages and reply when useful.'],
  ['review_status', 'Review status changes', 'Flag significant changes in Operator decisions.'],
] as const;

export function Settings({current, onSaved}: {current: BoardSettings; onSaved: (next: BoardSettings) => void}) {
  const [draft, setDraft] = useState<{base: BoardSettings; fields: SettingsValues}>();
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{message: string; error?: boolean}>();
  const [presetName, setPresetName] = useState('');
  const [selectedName, setSelectedName] = useState('');
  const fields = draft?.fields ?? values(current);
  const dirty = draft && JSON.stringify(fields) !== JSON.stringify(values(current));
  const stale = draft && JSON.stringify(draft.base) !== JSON.stringify(current);

  const selected = fields.presets?.find(p => p.name === selectedName);
  function savePreset() {
    const name = presetName.trim();
    if (!name || !fields.provider) return;
    if (fields.presets.some(p => p.name.toLowerCase() === name.toLowerCase())) {
      setFeedback({message: 'This name is already in use. Choose another name or update the existing preset.', error: true});
      return;
    }
    edit({presets: [...fields.presets, {name, settings: operatorFields(fields)}]});
    setSelectedName(name);
    setPresetName('');
  }

  function edit(patch: Partial<SettingsValues>) {
    setDraft({base: draft?.base ?? current, fields: {...fields, ...patch}});
    setFeedback(undefined);
  }

  async function save(settings: SettingsValues | null) {
    setBusy(true);
    setFeedback(undefined);
    try {
      const result = await api<BoardSettings>('/api/action', {
        action: 'save_settings', data: {expected: draft?.base ?? current, settings},
      });
      onSaved(result);
      setDraft(undefined);
      setFeedback({message: settings ? 'Settings saved.' : 'Configuration file settings restored.'});
    } catch (error) {
      setFeedback({message: errorText(error), error: true});
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!busy && dirty && !stale) void save(fields);
  }

  if (!Array.isArray(current.presets)) return <section className="panel settings"><p className="notice">
    Restart the Arbiter daemon to use the updated settings board.</p></section>;

  return <section className="panel settings" aria-labelledby="settings-heading">
    <h2 className="visually-hidden" id="settings-heading">Settings</h2>
    <div className="settings-layout">
      <form onSubmit={submit}>
        <fieldset className="settings-group" disabled={busy}>
          <legend>Scheduler rules · No model required</legend>
          <label className="phase-field">Preferred reservation length
            <select value={fields.preferred_phase_minutes} onChange={e => edit({preferred_phase_minutes: Number(e.target.value)})}>
              {Array.from(new Set([5, 10, 15, 30, 60, 120, fields.preferred_phase_minutes])).sort((a, b) => a - b).map(n =>
                <option key={n} value={n}>{n % 60 ? n === 1 ? '1 minute' : n + ' minutes' : n === 60 ? '1 hour' : n / 60 + ' hours'}</option>)}
            </select>
          </label>
          <label className="event-option"><input type="checkbox" checked={fields.auto_approve_inbox}
            onChange={e => edit({auto_approve_inbox: e.target.checked})}/>
            <span><strong>Automatically approve routine inbox reads</strong><small>Applies to new Arbiter inbox read requests.</small></span></label>
          <label className="event-option"><input type="checkbox" checked={fields.preserve_interactivity}
            onChange={e => edit({preserve_interactivity: e.target.checked})}/>
            <span><strong>Preserve machine interactivity</strong><small>Delay new reservations under CPU or memory pressure. GPU jobs also wait under graphics load or low VRAM on supported hardware. Running jobs keep their grants.</small></span></label>
        </fieldset>
        <fieldset className="settings-group" disabled={busy}>
          <legend>Operator · Requires a model</legend>
          <div className="model-fields">
            <label>Provider<select value={fields.provider} onChange={e => edit({provider: e.target.value})}>
              <option value="">Off</option><option value="codex">Codex</option><option value="claude">Claude</option><option value="pi">Pi</option>
              {current.custom_command && <option value="command">Configured command</option>}
            </select></label>
            <label>Model ID<input value={fields.model} maxLength={250} disabled={!fields.provider || fields.provider === 'command'}
              placeholder={fields.provider === 'command' ? 'Set by the configured command' : 'Provider default'}
              onChange={e => edit({model: e.target.value})}/></label>
          </div>
          {operatorOptions.map(([key, label, help]) =>
            <label className="event-option" key={key}><input type="checkbox" checked={fields[key]}
              disabled={!fields.provider || (key === 'approve_inbox' && fields.auto_approve_inbox)}
              onChange={e => edit({[key]: e.target.checked})}/>
              <span><strong>{label}</strong><small>{key === 'approve_inbox' && fields.auto_approve_inbox ?
                'New requests are handled by the automatic inbox rule.' : help}</small></span></label>)}
          <label className="instructions">Custom instructions
            <textarea rows={3} maxLength={4000} disabled={!fields.provider} value={fields.instructions}
              placeholder="For example, prioritize short builds when several sessions are waiting."
              onChange={e => edit({instructions: e.target.value})}/>
          </label>
          <p className="settings-help">Instructions guide the model when it chooses among enabled actions.</p>
        </fieldset>
        <fieldset className="settings-group" disabled={busy}>
          <legend>Saved operator presets</legend>
          <p className="settings-help">Store the provider, model, actions, and instructions together. Save settings to apply your edits.</p>
          {fields.presets.length > 0 && <div className="preset-controls">
            <label>Saved preset<select value={selected?.name ?? ''} onChange={e => setSelectedName(e.target.value)}>
              <option value="">Choose a preset</option>
              {fields.presets.map(p => <option key={p.name}>{p.name}</option>)}
            </select></label>
            <button type="button" disabled={!selected || (selected.settings.provider === 'command' && !current.custom_command)}
              onClick={() => selected && edit(selected.settings)}>Load</button>
            <button type="button" disabled={!selected || !fields.provider} onClick={() => edit({presets: fields.presets.map(p =>
              p === selected ? {...p, settings: operatorFields(fields)} : p)})}>Update preset</button>
            <button type="button" disabled={!selected} onClick={() => {
              edit({presets: fields.presets.filter(p => p !== selected)}); setSelectedName('');
            }}>Remove</button>
          </div>}
          <div className="preset-controls">
            <label>New preset name<input maxLength={64} value={presetName} onChange={e => setPresetName(e.target.value)}/></label>
            <button type="button" disabled={!fields.provider || !presetName.trim() || fields.presets.length >= 32}
              onClick={savePreset}>Add preset</button>
          </div>
        </fieldset>
        {stale && <p className="notice" role="alert">Settings changed elsewhere. <button type="button" onClick={() => {setDraft(undefined); setFeedback(undefined);}}>Load current settings</button></p>}
        {feedback && <p className={feedback.error ? 'notice' : 'settings-result'} role={feedback.error ? 'alert' : 'status'}>{feedback.message}</p>}
        <div className="settings-actions">
          <button type="submit" className="primary" disabled={busy || !dirty || !!stale}>{busy ? 'Saving…' : 'Save settings'}</button>
          {current.overridden && <button type="button" disabled={busy || !!stale} onClick={() => void save(null)}>Use configuration file</button>}
          <small>{dirty ? 'Unsaved changes' : current.overridden ? 'Saved on this machine' : 'Using configuration file defaults'}</small>
        </div>
      </form>
      <aside className="appearance">
        <h3>Appearance</h3>
        <label>Board theme<select disabled defaultValue="observatory"><option value="observatory">Observatory · Dark</option></select></label>
        <p className="settings-help">Additional themes are planned.</p>
      </aside>
    </div>
  </section>;
}
