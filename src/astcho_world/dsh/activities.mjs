// Supported, durable Session surface transactions; original events remain intact.
import { randomUUID } from 'node:crypto';

function body(message) {
  try { return JSON.parse(message.content.filter(b => b.type === 'text').map(b => b.text).join('\n')); }
  catch { return null; }
}

async function indexActivity(id, payload, rpc) {
  // Python archives and validates original records, then builds local facts or
  // reuses an existing brief. No model call, including maintenance and recovery.
  return await rpc({ method: 'activity_index', id }, payload.signal);
}

function hide(session, nodes, start, end, id) {
  session.append('user/message', { role: 'user', id: randomUUID(), content: [],
    source: { kind: 'astcho-world-archived', logical_activity_id: id } },
  { surfaceOp: { op: 'replace', startSeq: start, endSeq: end },
    sourceEventSeqs: nodes.slice(nodes.indexOf(start), nodes.indexOf(end) + 1) });
}

export async function projectActivities(payload, ctx, rpc, request) {
  const session = payload.agent.session;
  for (let i = 0; i < (request.refresh_ids || []).length; i += 3) {
    await Promise.all(request.refresh_ids.slice(i, i + 3).map(id => indexActivity(id, payload, rpc)));
  }
  let nodes = [...session.surface.nodes];
  if (request.legacy_ids?.length) {
    // Local archive/index maintenance; no subject or summary model turn.
    for (let i = 0; i < request.legacy_ids.length; i += 3) {
      await Promise.all(request.legacy_ids.slice(i, i + 3).map(id => indexActivity(id, payload, rpc)));
    }
    const first = nodes.findIndex(seq => session.eventAt(seq).type !== 'system/message');
    let end = nodes.length;
    for (let i = Math.max(0, first); i < nodes.length; i++) {
      const e = session.eventAt(nodes[i]);
      const v = e.type === 'user/message' && body(e.data);
      if (v?.type === 'world.wake' && v.logical_activity_id) { end = i; break; }
    }
    if (first >= 0 && end > first) hide(session, nodes, nodes[first], nodes[end-1], 'legacy-records');
    nodes = [...session.surface.nodes];
  }
  const spans = [];
  let current;
  for (const seq of nodes) {
    const event = session.eventAt(seq);
    if (event.type === 'system/message') continue;
    const item = event.type === 'user/message' && body(event.data);
    if (item?.type === 'world.activity_summary') current = null;
    else if (['world.wake', 'world.summary'].includes(item?.type) && item.logical_activity_id) {
      if (current?.id !== item.logical_activity_id) {
        current = { id: item.logical_activity_id, start: seq, end: seq };
        spans.push(current);
      }
    }
    if (current) current.end = seq;
  }
  for (const span of spans) {
    if (!request.finished_ids.includes(span.id)) continue;
    // Verify archives and summaries first. Failure keeps every original node.
    await indexActivity(span.id, payload, rpc);
    hide(session, nodes, span.start, span.end, span.id);
  }
  // Briefs are provided in a bounded World runtime snapshot, never folded into
  // another growing global summary. This also cleans earlier projection versions.
  for (const seq of [...session.surface.nodes]) {
    const event = session.eventAt(seq);
    if (event.type === 'user/message' && body(event.data)?.type === 'world.activity_summary') {
      hide(session, [seq], seq, seq, 'superseded-brief');
    }
  }
  if (ctx.sessions?.flush) await ctx.sessions.flush(session);
}
