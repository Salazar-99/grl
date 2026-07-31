import { DataFrame, Field } from '@grafana/data';

export interface TrajectoryRow {
  index: number;
  time?: number | string;
  taskId?: string;
  groupId?: string;
  rolloutIndex?: number;
  reward?: number;
  numTurns?: number;
  doneReason?: string;
  promptTokens?: number;
  responseTokens?: number;
  policyVersionStart?: number;
  policyVersionCurrent?: number;
  body: string;
}

function fieldByName(frame: DataFrame, name: string): Field | undefined {
  return frame.fields.find((f) => f.name === name);
}

function valueAt<T>(field: Field | undefined, index: number): T | undefined {
  if (!field) {
    return undefined;
  }
  return field.values[index] as T | undefined;
}

export function rowsFromFrame(frame: DataFrame): TrajectoryRow[] {
  const time = fieldByName(frame, 'time') ?? fieldByName(frame, 'Time');
  const taskId = fieldByName(frame, 'TaskId');
  const groupId = fieldByName(frame, 'GroupId');
  const rolloutIndex = fieldByName(frame, 'RolloutIndex');
  const reward = fieldByName(frame, 'Reward');
  const numTurns = fieldByName(frame, 'NumTurns');
  const doneReason = fieldByName(frame, 'DoneReason');
  const promptTokens = fieldByName(frame, 'PromptTokens');
  const responseTokens = fieldByName(frame, 'ResponseTokens');
  const policyVersionStart = fieldByName(frame, 'PolicyVersionStart');
  const policyVersionCurrent = fieldByName(frame, 'PolicyVersionCurrent');
  const body = fieldByName(frame, 'Body');

  const length = frame.length;
  const rows: TrajectoryRow[] = [];
  for (let i = 0; i < length; i++) {
    const bodyValue = valueAt<string>(body, i);
    rows.push({
      index: i,
      time: valueAt<number | string>(time, i),
      taskId: valueAt<string>(taskId, i),
      groupId: valueAt<string>(groupId, i),
      rolloutIndex: valueAt<number>(rolloutIndex, i),
      reward: valueAt<number>(reward, i),
      numTurns: valueAt<number>(numTurns, i),
      doneReason: valueAt<string>(doneReason, i),
      promptTokens: valueAt<number>(promptTokens, i),
      responseTokens: valueAt<number>(responseTokens, i),
      policyVersionStart: valueAt<number>(policyVersionStart, i),
      policyVersionCurrent: valueAt<number>(policyVersionCurrent, i),
      body: typeof bodyValue === 'string' ? bodyValue : bodyValue != null ? String(bodyValue) : '',
    });
  }
  return rows;
}

export function formatTime(value: number | string | undefined): string {
  if (value == null || value === '') {
    return '—';
  }
  const ms = typeof value === 'number' ? value : Date.parse(value);
  if (Number.isNaN(ms)) {
    return String(value);
  }
  return new Date(ms).toISOString().replace('T', ' ').replace('Z', ' UTC');
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) {
    return text;
  }
  return `${text.slice(0, Math.max(0, max - 1))}…`;
}
