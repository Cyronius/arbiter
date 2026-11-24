import {useState, type ReactNode} from 'react';
import {ActionIcon} from './action-icon';
import {groupings, type Grouping} from './grouping';
import {clock, confirmationResponse, describe, number, terminal, type Approval, type BoardState, type Message, type Reservation, type Session} from './model';

type Run = (operation: string, data: object, key: string) => void;
type Name = (id?: string) => string;
export type Decision = NonNullable<BoardState['operator_decisions']>[number];

export function Panel({id, title, count, tools, children}: {
  id: string; title: string; count?: number; tools?: ReactNode; children: ReactNode;
}) {
  return <section className={`panel panel-${id}`} id={id} aria-labelledby={`${id}-heading`}>
    <h2 className="visually-hidden" id={`${id}-heading`}>{title}{count !== undefined && ` (${count})`}</h2>
    {tools}
    {children}
  </section>;
}

const stateLabels: Readonly<Record<string, string>> = {
  approval: 'Needs approval',
  waiting: 'Resource wait',
  running: 'Running',
  draining: 'Pausing',
  paused: 'Paused',
  stale: 'Stale',
  finished: 'Finished',
  failed: 'Failed',
};

function stateLabel(state: string) {
  return stateLabels[state] ?? state.charAt(0).toUpperCase() + state.slice(1);
}

function SessionRow({session, name, reservations, conversation, compose, inspect, run, busy, states, latest, queueReason}: {
  latest?: Session; queueReason?: string; session: Session; name: Name; reservations: Reservation[]; conversation: boolean;
  compose: (id: string) => void; inspect: (session: Session) => void; run: Run; busy: boolean; states: string[];
}) {
  const resume = ['paused', 'draining'].includes(session.state);
  const canInspect = conversation && session.external_id && ['claude', 'codex'].includes(session.provider ?? '');
  const recoveryLabel = session.kind === 'job' ? 'Rerun command' : 'Resume conversation';
  const canRecover = conversation && session.recovery_reason === null && !session.successor;
  function recover() {
    if (session.confirm_unknown && !window.confirm('The previous outcome is unknown. Check surviving work for side effects. Start another attempt?')) return;
    run('recover', {session: session.id, confirm_unknown: session.confirm_unknown}, session.id);
  }
  return <article className="session-row" id={session.id}>
    <div className="session-name">
      <h3>{session.name}</h3>
      <p title={session.id}>{[session.provider || session.kind, session.parent && `under ${name(session.parent)}`, session.pid && `PID ${session.pid}`].filter(Boolean).join(' · ')}</p>
    </div>
    <div className="session-state">
      {states.map(state => <span key={state} className={`status status-${state}`}>{stateLabel(state)}</span>)}
    </div>
    <div className="session-report">
      {session.status && <p>{session.status}</p>}
      {session.predecessor && <p>Recovery of <a href={`#${session.predecessor}`}>{session.predecessor}</a></p>}
      {latest && <p>Latest attempt <a href={`#${latest.id}`}>{latest.id}</a> · {stateLabel(latest.state)} · {queueReason || latest.status}</p>}
      {session.checkpoint && <p>Checkpoint · {session.checkpoint}</p>}
      {session.outcome && <p>Outcome · {session.outcome}</p>}
      {session.recovery_reason && <p>{session.recovery_reason}</p>}
      {Boolean(session.retry_limit) && <p>Automatic retries · {session.retries_used} of {session.retry_limit} used
        {session.retry_at && ` · next eligible at ${clock(session.retry_at)}`}
        {session.retry_cancelled && ' · cancelled'}{session.retry_blocker && ` · ${session.retry_blocker}`}</p>}
      {session.context_usage && <p className="reservation-note" title="Input tokens in the last measured request, including cached input">
        Context · {number(session.context_usage.input_tokens)}
        {session.context_usage.window_tokens
          ? ` / ${number(session.context_usage.window_tokens)} tokens (${number(100 * session.context_usage.input_tokens / session.context_usage.window_tokens)}%)`
          : ' tokens'}</p>}
      {session.state === 'stale' && session.stale_reason && <p>{session.stale_reason}</p>}
      {reservations.map(reservation => <p className="reservation-note" key={reservation.id}>
        {describe(reservation)} · {reservation.state === 'requested' ? reservation.reason : reservation.state}
        {reservation.eta ? ` · until ~${clock(reservation.eta)}` : ''}
        {reservation.stale_at && !session.pid ? ` · stale at ${clock(reservation.stale_at)}` : ''}
      </p>)}
    </div>
    {(canInspect || canRecover || !terminal(session) || session.retry_at) && <div className="session-actions" role="group" aria-label={`Actions for ${session.name}`}>
      {canRecover && <button disabled={busy} onClick={recover}>{recoveryLabel}</button>}
      {conversation && session.retry_at && <button disabled={busy} onClick={() => run('cancel_retry', {session: session.id}, session.id)}>Cancel retry</button>}
      {canInspect && <ActionIcon icon="conversation" label="View conversation" onClick={() => inspect(session)}/>}
      {!terminal(session) && <>
        <ActionIcon icon="message" label="Send message" onClick={() => compose(session.id)}/>
        {session.kind !== 'hold' && <>
          <ActionIcon icon={resume ? 'resume' : 'pause'} label={resume ? 'Resume' : 'Request pause'} disabled={busy}
            onClick={() => run('control', {session: session.id, action: resume ? 'resume' : 'pause', reason: 'Requested from message board'}, session.id)}/>
          <ActionIcon icon="update" label="Ask for update" disabled={busy}
            onClick={() => run('control', {session: session.id, action: 'status', reason: 'Short update requested from message board'}, session.id)}/>
        </>}
        {(session.kind === 'hold' || session.state === 'stale' && !session.worker_survived) && <ActionIcon icon="release" label="Release" disabled={busy}
          onClick={() => run('finish', {session: session.id, safe: true, status: 'Released from the message board'}, session.id)}/>}
      </>}
    </div>}
  </article>;
}

export function Sessions({state, grouping, onGroupingChange, approvals, composeAll, name, reservations, compose, inspect, run, pending}: {
  state: BoardState; grouping: Grouping; onGroupingChange: (group: Grouping) => void; approvals: Approval[]; composeAll: () => void; name: Name; reservations: Reservation[];
  compose: (id: string) => void; inspect: (session: Session) => void; run: Run; pending: ReadonlySet<string>;
}) {
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const all = Object.values(state.sessions).reverse();
  const bySession = new Map<string, Reservation[]>();
  for (const reservation of reservations) {
    const list = bySession.get(reservation.session) ?? [];
    list.push(reservation);
    bySession.set(reservation.session, list);
  }

  const latestByOriginal = new Map<string, Session>();
  for (const session of all) {
    if (session.original && !session.successor) latestByOriginal.set(session.original, session);
  }
  const latest = (session: Session) => session.successor ? latestByOriginal.get(session.original ?? session.id) : undefined;
  const resourceWaits = new Set(reservations.filter(reservation => reservation.state === 'requested').map(reservation => reservation.session));
  const approvalWaits = new Set(approvals.map(approval => approval.session));
  const stateBySession = new Map(all.map(session => {
    const states: string[] = [];
    if (resourceWaits.has(session.id) || session.state === 'waiting') states.push('waiting');
    if (approvalWaits.has(session.id)) states.push('approval');
    if (!states.length || !['running', 'waiting'].includes(session.state)) states.push(session.state);
    return [session.id, states];
  }));
  const statesFor = (session: Session) => stateBySession.get(session.id) ?? [];
  const stateOrder = [...new Set([...Object.keys(stateLabels), ...all.map(session => session.state), filter])]
    .filter(id => id !== 'all' && id !== 'active');
  const filters = [
    {id: 'all', label: 'All', always: true, matches: () => true},
    {id: 'active', label: 'Active', always: true, matches: (session: Session) => !terminal(session)},
    ...stateOrder.map(id => ({
      id,
      label: stateLabel(id),
      always: ['approval', 'waiting'].includes(id),
      matches: (session: Session) => statesFor(session).includes(id),
    })),
  ];
  const matchFilter = filters.find(option => option.id === filter)!.matches;
  const keywords = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const visible = all.filter(session => {
    if (!matchFilter(session)) return false;
    const text = [
      session.name, session.id, session.kind, session.provider, session.state, session.status, session.stale_reason,
      session.parent && name(session.parent), session.pid,
      ...statesFor(session).map(stateLabel),
      ...(bySession.get(session.id) ?? []).flatMap(reservation => [describe(reservation), reservation.reason]),
    ].join(' ').toLowerCase();
    return keywords.every(keyword => text.includes(keyword));
  });
  const grouped = (title: string, matches: (session: Session) => boolean) => ({title, sessions: visible.filter(matches)});
  const groups = grouping === 'flow' ? stateOrder.map(id =>
    grouped(stateLabel(id), session => (filter === 'all' || filter === 'active' ? stateOrder.find(state => statesFor(session).includes(state)) : filter) === id)
  ) : grouping === 'roster' ? [
    grouped('Claude', session => session.provider === 'claude' && session.kind !== 'job'),
    grouped('Codex', session => session.provider === 'codex' && session.kind !== 'job'),
    grouped('Jobs', session => session.kind === 'job'),
    grouped('Other sessions', session => session.kind !== 'job' && !['claude', 'codex'].includes(session.provider ?? '')),
  ] : [{title: '', sessions: visible}];
  const shownGroups = groups.filter(group => group.sessions.length);

  return <Panel id="sessions" title="Sessions" count={visible.length} tools={
    <div className="session-tools">
      <input aria-label="Search sessions" type="search" placeholder="Search sessions…" value={query} onChange={event => setQuery(event.target.value)}/>
      <label className="grouping-select"><span>Grouping</span>
        <select aria-label="Grouping" value={grouping} onChange={event => {
          const group = groupings.find(option => option.id === event.target.value);
          if (group) onGroupingChange(group.id);
        }}>
          {groupings.map(group => <option key={group.id} value={group.id}>{group.label}</option>)}
        </select>
      </label>
      <ActionIcon icon="broadcast" label="Message all sessions" className="primary message-all" onClick={composeAll}/>
    </div>
  }>
    <div className="session-filters" role="group" aria-label="Quick filters">
      {filters.map(option => {
        const count = all.filter(option.matches).length;
        return (option.always || option.id === filter || count > 0) &&
          <button key={option.id} type="button" aria-pressed={option.id === filter} onClick={() => setFilter(option.id)}>
            {option.label} <span className="filter-count">{count}</span>
          </button>;
      })}
    </div>
    {!visible.length ? <p className="empty">{all.length ? 'No sessions match these filters.' : 'No sessions yet. Launch an agent or register a session to populate the board.'}</p> :
      <div className="session-list">
        <div className="row-labels" aria-hidden="true"><span>Session</span><span>State</span><span>Latest report</span><span>Actions</span></div>
        {shownGroups.map(group => <section className="session-group" key={group.title} aria-label={group.title || 'Session list'}>
          {group.title && <h3 className="group-heading">{group.title} <span className="count">{group.sessions.length}</span></h3>}
          {group.sessions.map(session => <SessionRow key={session.id} session={session} name={name}
            reservations={bySession.get(session.id) ?? []} states={statesFor(session)}
            latest={latest(session)} queueReason={bySession.get(latest(session)?.id ?? '')?.find(r => r.state === 'requested')?.reason}
            conversation={Boolean(state.conversation_viewer)} compose={compose} inspect={inspect} run={run} busy={pending.has(session.id)}/>)}
        </section>)}
      </div>}
  </Panel>;
}

export function Capacity({state, held}: {state: BoardState; held: Reservation[]}) {
  const {telemetry} = state;
  return <section className="machine-stats" aria-label="Machine statistics">
    <div className="capacity-grid">
      {([['cpu', 'CPU budget', 'CPU'], ['memory_mb', 'Job memory budget', 'MiB']] as const).map(([key, label, unit]) => {
        const used = held.reduce((sum, reservation) => sum + reservation[key], 0);
        const limit = state.capacity[key];
        return <div className="meter" key={key}>
          <div><span>{label}</span><strong>{number(used)} / {number(limit)} {unit}</strong></div>
          <progress aria-label={label} max={limit || 1} value={used}/>
        </div>;
      })}
    </div>
    <dl className="host-stats">
      {telemetry?.available_mb != null && <div><dt>Host memory free</dt><dd>{number(telemetry.available_mb)} MiB</dd></div>}
      {telemetry?.cpu_busy != null && <div><dt>CPU activity</dt><dd>{number(telemetry.cpu_busy)}%</dd></div>}
      {telemetry?.load?.[0] != null && <div><dt>Load average</dt><dd>{telemetry.load[0].toFixed(2)}</dd></div>}
    </dl>
  </section>;
}

function ReservationList({entries, name, details = false, run, pending}: {
  entries: Reservation[]; name: Name; details?: boolean; run?: Run; pending?: ReadonlySet<string>;
}) {
  return <ul className="reservations">{entries.map(reservation => <li key={reservation.id}>
    <div className="reservation-heading"><strong>{name(reservation.session)}</strong>{reservation.eta ? <time>~{clock(reservation.eta)}</time> : null}</div>
    {details && <p>{describe(reservation)}</p>}
    {reservation.state === 'requested' && reservation.reason && <p>{reservation.reason}</p>}
    {run && reservation.queue_position && <button disabled={pending?.has(reservation.id)}
      onClick={() => run('promote', {reservation: reservation.id}, reservation.id)}>Run next</button>}
  </li>)}</ul>;
}

export function Resources({state, reservations, name}: {state: BoardState; reservations: Reservation[]; name: Name}) {
  const names = [...new Set([...Object.keys(state.resources ?? {}), ...reservations.flatMap(reservation => reservation.exclusive)])].sort();
  const rows = names.map(resource => ({resource, entries: reservations.filter(reservation => reservation.exclusive.includes(resource)), capacity: state.resources?.[resource]?.capacity ?? 1}));
  if (reservations.some(reservation => reservation.measure)) rows.unshift({resource: 'Whole machine', entries: reservations.filter(reservation => reservation.measure), capacity: 1});
  return <Panel id="resources" title="Resource allocation">
    {!rows.length && <p className="empty">No named resources configured or in use.</p>}
    <div className="resource-list">{rows.map(({resource, entries, capacity}) => {
      const holders = entries.filter(reservation => reservation.state === 'granted');
      const waiting = entries.filter(reservation => reservation.state === 'requested');
      return <article className="resource-row" key={resource}>
        <header><h3>{resource}</h3><span className="resource-count">{holders.length} / {capacity} in use</span></header>
        <div><h4>Holding</h4>{holders.length ? <ReservationList entries={holders} name={name}/> : <p className="muted">Free</p>}</div>
        <div><h4>Waiting</h4>{waiting.length ? <ReservationList entries={waiting} name={name}/> : <p className="muted">Queue clear</p>}</div>
      </article>;
    })}</div>
  </Panel>;
}

export function Reservations({reservations, name, run, pending}: {
  reservations: Reservation[]; name: Name; run: Run; pending: ReadonlySet<string>;
}) {
  const groups = [
    {state: 'requested', title: 'Waiting for resources'},
    {state: 'granted', title: 'Holding'},
    {state: 'suspended', title: 'Suspended'},
  ];
  return <Panel id="reservations" title="Reservations" count={reservations.length}>
    {!reservations.length && <p className="empty">No active reservations.</p>}
    {groups.map(group => {
      const entries = reservations.filter(reservation => reservation.state === group.state);
      return entries.length > 0 && <section className="reservation-group" key={group.state} aria-label={group.title}>
        <h3 className="group-heading">{group.title} <span className="count">{entries.length}</span></h3>
        <ReservationList entries={entries} name={name} details run={run} pending={pending}/>
      </section>;
    })}
  </Panel>;
}

const approvalTitles: Record<string, string> = {
  'item/commandExecution/requestApproval': 'Command approval',
  'item/fileChange/requestApproval': 'File change approval',
  'claude/can_use_tool': 'Tool approval',
  'mcpServer/elicitation/request': 'Input needed',
};

export function Approvals({entries, name, run, pending}: {entries: Approval[]; name: Name; run: Run; pending: ReadonlySet<string>}) {
  return <Panel id="approvals" title="Approvals" count={entries.length}>
    {!entries.length && <p className="empty">No approvals pending.</p>}
    {entries.map(approval => {
      const params = approval.params ?? {};
      const command = params.command || params.input?.command;
      const allow = confirmationResponse(approval, true);
      const decline = confirmationResponse(approval, false);
      const answer = (response: object) => run('approval_answer', {session: approval.session, approval: approval.id, response}, approval.id);
      return <article className="approval" key={approval.id}>
        <h3>{approvalTitles[approval.method] ?? 'Input needed'}</h3><p className="muted">{name(approval.session)}</p>
        {(params.reason || params.message) && <p>{params.reason || params.message}</p>}
        {typeof command === 'string' && command && <pre><code>{command}</code></pre>}
        {params.cwd && <p className="muted">Working directory · {params.cwd}</p>}
        <details><summary>Request details</summary><pre>{approval.method}{'\n'}{JSON.stringify(params, null, 2)}</pre></details>
        <div className="button-row">
          {allow && <button className="primary" disabled={pending.has(approval.id)} onClick={() => answer(allow)}>Allow this call</button>}
          {decline && <button disabled={pending.has(approval.id)} onClick={() => answer(decline)}>Decline</button>}
        </div>
        {!allow && <p>Respond in the CLI with <code>arbiter answer {approval.id} '&lt;provider response JSON&gt;'</code>.</p>}
      </article>;
    })}
  </Panel>;
}

function MessageBody({message, name}: {message: Message; name: Name}) {
  return <><strong>{message.from === 'arbiter' ? 'Arbiter operator' : name(message.from)}</strong><span className="destination"> to {message.to ? name(message.to) : 'all sessions'}</span>
    <p>{message.text}</p>
    {(message.topic && message.topic !== 'general' || message.reply_to) && <small>{[message.topic && message.topic !== 'general' && `#${message.topic}`, message.reply_to && 'Reply'].filter(Boolean).join(' · ')}</small>}</>;
}

const decisionLabels: Record<string, string> = {approve_inbox: 'Approved inbox read', promote: 'Raised queue priority', request_update: 'Requested update', reply_message: 'Replied to message', flag_status: 'Flagged status change'};
function DecisionBody({decision, name}: {decision: Decision; name: Name}) {
  return <><strong>{decisionLabels[decision.action] ?? decision.action}</strong><span className="destination"> · {name(decision.session)}</span><p>{decision.reason}</p></>;
}

export function Feed({messages, decisions, kind, name, provider}: {
  messages: Message[]; decisions: Decision[]; kind: 'messages' | 'operator'; name: Name; provider?: string;
}) {
  const entries = [
    ...messages.map(message => ({id: message.id, at: message.created, body: <MessageBody message={message} name={name}/>})),
    ...decisions.map((decision, index) => ({id: `decision-${index}`, at: decision.created, body: <DecisionBody decision={decision} name={name}/>})),
  ].sort((left, right) => right.at - left.at);
  const title = {messages: 'Messages', operator: 'Operator decisions'}[kind];
  return <Panel id={kind} title={title}>
    {provider && <p className="panel-meta">{provider}</p>}
    {!entries.length && <p className="empty">{kind === 'messages' ? 'No messages yet.' : 'No operator decisions yet.'}</p>}
    {entries.length > 0 && <ol className="feed">{entries.map(entry => <li key={entry.id}><time dateTime={new Date(entry.at * 1000).toISOString()}>{clock(entry.at)}</time><div>{entry.body}</div></li>)}</ol>}
  </Panel>;
}
