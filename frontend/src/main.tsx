import {useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {ActionIcon} from './action-icon';
import {BoardTabs, tabFromUrl, type BoardTab} from './board-tabs';
import {Settings} from './settings';
import {groupingFromUrl, type Grouping} from './grouping';
import {action, api, ApiError, confirmationResponse, errorText, terminal, type BoardState, type BoardSettings, type Session} from './model';
import {Approvals, Capacity, Feed, Reservations, Resources, Sessions} from './panels';

function useBoard() {
  const [state, setState] = useState<BoardState>();
  const [connection, setConnection] = useState('Connecting');
  const [error, setError] = useState('');
  const refresh = useRef<() => void>(() => {});
  useEffect(() => {
    let stream: EventSource | undefined;
    let busy = false;
    let repeat = false;
    const controller = new AbortController();
    const refreshState = async () => {
      if (busy) {
        repeat = true;
        return;
      }
      busy = true;
      try {
        const snapshot = await api<BoardState>('/api/state', undefined, controller.signal);
        if (controller.signal.aborted) return;
        setState(snapshot);
        setError('');
        if (stream?.readyState === EventSource.OPEN) setConnection('Live');
        if (!stream) {
          stream = new EventSource(`/events?after=${snapshot.cursor}`);
          stream.onopen = () => setConnection('Live');
          stream.onerror = () => setConnection('Reconnecting');
          stream.onmessage = refreshState;
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          stream?.close();
          stream = undefined;
          setConnection('Signed out');
          setError('Your session expired. Sign in to reconnect.');
        } else {
          setConnection('Reconnecting');
          setError(errorText(error));
        }
      } finally {
        busy = false;
        if (repeat && !controller.signal.aborted) {
          repeat = false;
          void refreshState();
        }
      }
    };
    refresh.current = refreshState;
    void refreshState();
    const interval = window.setInterval(refreshState, 5000);
    return () => {
      controller.abort();
      stream?.close();
      window.clearInterval(interval);
    };
  }, []);
  function updateSettings(settings: BoardSettings) {
    setState(current => current && {...current, settings, operator: {provider: settings.provider, model: settings.model}});
    refresh.current();
  }
  return {state, connection, error, refresh, updateSettings};
}

function Dialog({title, close, actions, children}: {
  title: string; close: () => void; actions?: ReactNode; children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const element = dialog.current!;
    element.showModal();
    element.querySelector<HTMLElement>('[data-initial-focus]')?.focus();
    return () => element.close();
  }, []);
  return <dialog ref={dialog} aria-labelledby="dialog-title" onCancel={event => {event.preventDefault(); close();}}>
    <header className="dialog-heading">
      <h2 id="dialog-title">{title}</h2>
      <div className="dialog-actions">{actions}<ActionIcon icon="close" label="Close" onClick={close}/></div>
    </header>
    {children}
  </dialog>;
}

function Compose({name, recipient, close, refresh}: {name: string; recipient: string | null; close: () => void; refresh: () => void}) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function send(event: FormEvent) {
    event.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true);
    setError('');
    try {
      await action('operator_message', {to: recipient, text: text.trim()});
      refresh();
      close();
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  return <Dialog title={recipient ? `Message ${name}` : 'Message all sessions'} close={close}>
    <form className="compose" onSubmit={send}>
      <label htmlFor="message-text">Message</label>
      <textarea id="message-text" data-initial-focus maxLength={8000} required rows={5} value={text} onChange={event => setText(event.target.value)}/>
      {error && <p className="notice" role="alert">{error}</p>}
      <div className="button-row">
        <ActionIcon icon="send" label={busy ? 'Sending message…' : 'Send message'} className="primary"
          type="submit" disabled={busy || !text.trim()}/>
      </div>
    </form>
  </Dialog>;
}

type ConversationPage = {before: string | number | null; messages: {role: string; text: string}[]};

function Conversation({session, close}: {session: Session; close: () => void}) {
  const [messages, setMessages] = useState<ConversationPage['messages']>([]);
  const [before, setBefore] = useState<ConversationPage['before']>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const log = useRef<HTMLDivElement>(null);
  const scroll = useRef<{top: number; height: number} | undefined>(undefined);
  const request = useRef<AbortController | undefined>(undefined);

  async function load(older: boolean) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError('');
    const query = new URLSearchParams({session: session.id});
    if (older && before !== null) query.set('before', String(before));
    try {
      const page = await api<ConversationPage>(`/api/conversation?${query}`, undefined, controller.signal);
      if (controller.signal.aborted) return;
      scroll.current = older ? {top: log.current!.scrollTop, height: log.current!.scrollHeight} : undefined;
      setMessages(existing => older ? [...page.messages, ...existing] : page.messages);
      setBefore(page.before);
    } catch (error) {
      if (!controller.signal.aborted) setError(errorText(error));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    void load(false);
    return () => request.current?.abort();
  }, [session.id]);

  useLayoutEffect(() => {
    const element = log.current;
    if (element) element.scrollTop = scroll.current ? scroll.current.top + element.scrollHeight - scroll.current.height : element.scrollHeight;
  }, [messages]);

  return <Dialog title={session.name} close={close} actions={<>
      <ActionIcon icon="update" label="Refresh conversation" disabled={busy} onClick={() => void load(false)}/>
      {before !== null && <ActionIcon icon="older" label="Load older messages" disabled={busy} onClick={() => void load(true)}/>}
    </>}>
    <p role="status" className="muted">{busy ? 'Loading conversation…' : !error && !messages.length ? 'No conversation messages in this page.' : ''}</p>
    {error && <p role="alert" className="notice">{error}</p>}
    <div className="conversation-log" ref={log} tabIndex={0} aria-label="Conversation messages">
      {messages.map((message, index) => <article key={index}><h3>{message.role === 'user' ? 'User' : session.provider === 'claude' ? 'Claude' : 'Codex'}</h3><p>{message.text}</p></article>)}
    </div>
  </Dialog>;
}

function App() {
  const {state, connection, error, refresh, updateSettings} = useBoard();
  const [grouping, setGrouping] = useState(groupingFromUrl);
  const [tab, setTab] = useState(tabFromUrl);
  const [compose, setCompose] = useState<string | null>();
  const [conversation, setConversation] = useState<Session>();
  const [feedback, setFeedback] = useState<{text: string; error: boolean}>();
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const inFlight = useRef(new Set<string>());

  useEffect(() => {
    const update = () => {
      setGrouping(groupingFromUrl());
      setTab(tabFromUrl());
    };
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);

  function chooseGrouping(group: Grouping) {
    if (group === grouping) return;
    const url = new URL(location.href);
    url.hash = '';
    url.searchParams.delete('k');
    url.searchParams.delete('concept');
    if (group === 'flat') url.searchParams.delete('group');
    else url.searchParams.set('group', group);
    history.pushState(null, '', url);
    setGrouping(group);
  }

  function chooseTab(next: BoardTab) {
    if (tab === next) return;
    const url = new URL(location.href);
    url.hash = '';
    url.searchParams.delete('k');
    if (next === 'sessions') url.searchParams.delete('tab');
    else url.searchParams.set('tab', next);
    history.pushState(null, '', url);
    setTab(next);
  }

  useEffect(() => {
    const url = new URL(location.href);
    if (url.searchParams.has('k')) {
      url.searchParams.delete('k');
      history.replaceState(null, '', url);
    }
  }, []);

  async function run(operation: string, data: object, key: string) {
    if (inFlight.current.has(key)) return;
    inFlight.current.add(key);
    setPending(new Set(inFlight.current));
    setFeedback(undefined);
    try {
      await action(operation, data);
      const feedback: Record<string, string> = {approval_answer: 'Approval response recorded.', finish: 'Session released.', promote: 'Moved reservation to the front.'};
      setFeedback({text: feedback[operation] ?? 'Session request recorded.', error: false});
      refresh.current();
    } catch (error) {
      setFeedback({text: errorText(error), error: true});
    } finally {
      inFlight.current.delete(key);
      setPending(new Set(inFlight.current));
    }
  }

  const name = (id?: string) => id === 'arbiter' ? 'Arbiter' : id ? state?.sessions[id]?.name || id : 'Arbiter';
  const reservations = Object.values(state?.reservations ?? {})
    .filter(reservation => ['granted', 'requested', 'suspended'].includes(reservation.state))
    .sort((a, b) => (a.queue_position ?? 0) - (b.queue_position ?? 0));
  const held = reservations.filter(reservation => reservation.state === 'granted');
  const approvals = Object.values(state?.approvals ?? {}).filter(approval => {
    const session = state?.sessions[approval.session];
    return approval.state === 'pending' && session && !terminal(session);
  });
  const messages = Object.values(state?.messages ?? {}).sort((first, second) => second.created - first.created).slice(0, 250);
  const decisions = state?.operator_decisions ?? [];
  const provider = [state?.operator?.provider, state?.operator?.model].filter(Boolean).join(' · ');
  const staleBlockers = Object.values(state?.sessions ?? {}).filter(session => session.stale_check?.unanswered_since != null);

  return <div className="app">
    <a className="skip-link" href="#main">Skip to board</a>
    <header className="masthead">
      <div className="mast-inner">
        <h1><a className="brand" href="/" aria-label="Arbiter home"><span className="brand-mark" aria-hidden="true"><i/><i/><i/></span>Arbiter</a></h1>
        {state && <Capacity state={state} held={held}/>}
        <div className="mast-actions">
          <span className={`connection ${connection === 'Live' ? 'is-live' : ''}`} role="status">{connection}</span>
        </div>
      </div>
    </header>
    <main id="main" className="shell">
      {state?.pressure && !state.pressure.ok && <p className="notice">Machine pressure · {state.pressure.reason}</p>}
      {state?.config_error && <p className="notice">Config error · {state.config_error}</p>}
      {staleBlockers.map(session => <p key={session.id} className="notice">
        Stale reservation blocking work · <strong>{session.name}</strong> has not answered its status check.
      </p>)}
      {error && <p className="notice" role="alert">{error} {connection === 'Signed out' && <a href={location.pathname + location.search}>Sign in</a>}</p>}
      {!state && !error && <p className="loading" role="status">Loading board…</p>}
      {feedback && <div className={feedback.error ? 'notice feedback' : 'feedback'} role={feedback.error ? 'alert' : 'status'}>{feedback.text}<button onClick={() => setFeedback(undefined)}>Dismiss</button></div>}
      {state && <BoardTabs selected={tab} onSelect={chooseTab}>{{
        sessions: <Sessions state={state} grouping={grouping} onGroupingChange={chooseGrouping} approvals={approvals}
          composeAll={() => setCompose(null)} name={name} reservations={reservations} compose={setCompose}
          inspect={setConversation} run={run} pending={pending}/>,
        reservations: <Reservations reservations={reservations} name={name} run={run} pending={pending}/>,
        approvals: <Approvals entries={approvals} name={name} run={run} pending={pending}/>,
        resources: <Resources state={state} reservations={reservations} name={name}/>,
        messages: <Feed kind="messages" messages={messages} decisions={[]} name={name}/>,
        operator: <Feed kind="operator" messages={[]} decisions={decisions} name={name} provider={provider || undefined}/>,
        settings: state.settings ? <Settings current={state.settings} onSaved={updateSettings}/> :
          <p className="empty">Settings are unavailable on this daemon version.</p>,
      }}</BoardTabs>}
    </main>
    {compose !== undefined && <Compose recipient={compose} name={name(compose ?? undefined)} close={() => setCompose(undefined)} refresh={() => {refresh.current(); setFeedback({text: 'Message sent.', error: false});}}/>}
    {conversation && <Conversation session={conversation} close={() => setConversation(undefined)}/>}
  </div>;
}

// Keep the browser and CLI approval response parity test pointed at the shipped code.
Object.assign(globalThis, {ArbiterApprovals: {response: confirmationResponse}});
if (typeof document !== 'undefined') createRoot(document.getElementById('root')!).render(<App/>);
