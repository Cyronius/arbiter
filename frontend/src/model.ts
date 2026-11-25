export interface Session {
  id: string;
  name: string;
  kind: string;
  state: string;
  status?: string;
  stale_reason?: string;
  worker_survived?: boolean;
  terminal_mode?: string;
  terminal_action?: 'resume-terminal' | 'attach-terminal';
  recovery_reason?: string | null;
  confirm_unknown?: boolean;
  successor?: string;
  predecessor?: string;
  original?: string;
  outcome?: string;
  checkpoint?: string;
  retry_at?: number;
  retry_blocker?: string;
  retry_cancelled?: boolean;
  retries_used?: number;
  retry_limit?: number;
  stale_check?: {unanswered_since?: number};
  parent?: string;
  pid?: number;
  provider?: string;
  external_id?: string;
  context_usage?: {input_tokens: number; window_tokens?: number};
  title?: string;
  attention?: {key: string; kind: 'permission' | 'input'; detail: string; since: number}[];
}

export interface Reservation {
  queue_position?: number;
  id: string;
  session: string;
  label?: string;
  exclusive: string[];
  state: string;
  reason?: string;
  eta?: number;
  stale_at?: number;
  measure?: boolean;
  disk_gb?: number;
  disk_path?: string;
  cpu: number;
  memory_mb: number;
}

export interface Message {
  id: string;
  created: number;
  from?: string;
  to?: string;
  text: string;
  topic?: string;
  reply_to?: string;
  acks?: string[];
}

export interface Approval {
  id: string;
  session: string;
  method: string;
  state: string;
  reason?: string;
  closed?: number;
  params?: {
    input?: Record<string, unknown>;
    command?: unknown;
    cwd?: string;
    reason?: string;
    message?: string;
    mode?: string;
    summary?: string;
    requestedSchema?: {type?: string; properties?: Record<string, unknown>; required?: string[]};
  };
}

export interface OperatorSettings {
  instructions: string;
  provider: string;
  model: string;
  approve_inbox: boolean;
  manage_resources: boolean;
  reply_messages: boolean;
  review_status: boolean;
}

export interface OperatorPreset {
  name: string;
  settings: OperatorSettings;
}

export interface SettingsValues extends OperatorSettings {
  preferred_phase_minutes: number;
  auto_approve_inbox: boolean;
  preserve_interactivity: boolean;
  presets: OperatorPreset[];
}

export interface BoardSettings extends SettingsValues {
  custom_command: boolean;
  overridden: boolean;
}

export interface BoardState {
  sessions: Record<string, Session>;
  reservations: Record<string, Reservation>;
  approvals: Record<string, Approval>;
  messages: Record<string, Message>;
  capacity: {cpu: number; memory_mb: number};
  telemetry?: {available_mb?: number; cpu_busy?: number; load?: number[]};
  pressure?: {ok: boolean; reason: string};
  resources?: Record<string, {capacity: number}>;
  config_error?: string;
  operator?: {provider: string; model: string};
  settings?: BoardSettings;
  operator_decisions?: {action: string; session: string; created: number; reason: string}[];
  cursor: number;
  conversation_viewer?: boolean;
}

export const terminal = (session: Session) => ['finished', 'failed'].includes(session.state);
export const clock = (time: number) => new Date(time * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
export const number = (value: number) => value.toLocaleString(undefined, {maximumFractionDigits: 1});

export function describe(reservation: Reservation) {
  return [
    reservation.label,
    reservation.exclusive.join(', '),
    reservation.measure && 'whole machine measurement',
    reservation.disk_gb && `${reservation.disk_gb} GB disk · ${reservation.disk_path}`,
    reservation.cpu && `${reservation.cpu} CPU`,
    reservation.memory_mb && `${reservation.memory_mb} MiB`,
  ].filter(Boolean).join(' · ') || 'Resource bundle';
}

export function confirmationResponse(approval: Pick<Approval, 'method' | 'params'>, allow: boolean) {
  const {method, params = {}} = approval;
  if (['item/commandExecution/requestApproval', 'item/fileChange/requestApproval'].includes(method)) {
    return {decision: allow ? 'accept' : 'decline'};
  }
  if (method === 'gaia/needs_confirmation') return {decision: allow ? 'allow' : 'deny'};
  if (method === 'claude/can_use_tool') {
    return allow ? {behavior: 'allow', updatedInput: params.input || {}} : {behavior: 'deny', message: 'Denied from Arbiter'};
  }
  if (method === 'mcpServer/elicitation/request' && ['form', 'openai/form'].includes(params.mode ?? '')) {
    if (!allow) return {action: 'decline', content: null};
    const schema = params.requestedSchema;
    if (schema?.type === 'object' && schema.properties && !Object.keys(schema.properties).length && !schema.required?.length) {
      return {action: 'accept', content: {}};
    }
  }
  return null;
}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

export async function api<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    signal,
    ...(body === undefined ? {} : {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    }),
  });
  const result = await response.json();
  if (!response.ok) throw new ApiError(result.error || response.statusText, response.status);
  return result;
}

export const action = (name: string, data: object) => api('/api/action', {action: name, data});
export const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);
