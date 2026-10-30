// Append immutable runtime snapshots/updates. Never rewrite an earlier model input.
import { randomUUID } from 'node:crypto';

function runtimeValue(event) {
  if (event.type !== 'user/message' || event.data.source?.kind !== 'astcho-world') return null;
  try {
    const value = JSON.parse(event.data.content.filter(b => b.type === 'text').map(b => b.text).join('\n'));
    return value.type === 'world.runtime' ? value : null;
  } catch { return null; }
}

export function visibleRuntime(session, runId) {
  let state = null, id = null;
  for (const seq of session.surface.nodes) {
    const item = runtimeValue(session.eventAt(seq));
    if (!item) continue;
    const { runtime_state: meta, ...fields } = item;
    if (!meta || meta.run_id !== runId) { state = null; id = null; continue; }
    if (meta.kind === 'snapshot') { state = fields; id = meta.id; }
    else if (meta.kind === 'update' && state && meta.previous_id === id) {
      state = { ...state, ...fields }; id = meta.id;
    } else { state = null; id = null; }
  }
  return { state, id };
}

export function runtimeMessage(session, context) {
  const runId = context.activity.run_id;
  const previous = visibleRuntime(session, runId);
  const fields = previous.state ? Object.fromEntries(Object.entries(context).filter(([key, value]) =>
    ['type', 'source'].includes(key) || JSON.stringify(previous.state[key]) !== JSON.stringify(value)
  )) : context;
  if (previous.state) for (const key of Object.keys(previous.state)) {
    if (!(key in context) && previous.state[key] !== null) fields[key] = null;
  }
  return {
    role: 'user', id: randomUUID(), source: { kind: 'astcho-world' },
    content: [{ type: 'text', text: JSON.stringify({ ...fields, runtime_state: {
      kind: previous.state ? 'update' : 'snapshot', id: randomUUID(), run_id: runId,
      ...(previous.state ? { previous_id: previous.id } : {}),
    } }) }],
  };
}
