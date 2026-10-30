// Native Cordis extension: DSH owns the loop, schema admission and durable session.
// World owns app operations and information boundaries. No model SDK lives here.
import { installContext, reconcile, maintain, projectVideoResults } from './context.mjs';
import { installCapabilities } from './capabilities.mjs';
import { projectActivities } from './activities.mjs';
import { runtimeMessage } from './runtime-state.mjs';
import { createConnection } from 'node:net';
import { StringDecoder } from 'node:string_decoder';

export const name = 'astcho-world';
export const inject = ['tools', 'compaction', 'tokenMeter', 'llm', 'schedule', 'skills', 'web', 'sessions'];

function rpc(payload, signal) {
  return new Promise((resolve, reject) => {
    const socket = createConnection(process.env.ASTCHO_DSH_SOCKET);
    let buffer = '';
    const decoder = new StringDecoder('utf8');
    const abort = () => socket.destroy(new Error('World call cancelled'));
    signal?.addEventListener('abort', abort, { once: true });
    socket.setTimeout(150000, () => socket.destroy(new Error('World bridge timeout')));
    socket.on('connect', () => socket.write(JSON.stringify({
      ...payload, token: process.env.ASTCHO_DSH_BRIDGE_TOKEN,
    }) + '\n'));
    socket.on('data', chunk => {
      buffer += decoder.write(chunk);
      if (Buffer.byteLength(buffer) > 16 * 1024 * 1024) {
        socket.destroy(new Error('World reply too large'));
      }
      const end = buffer.indexOf('\n');
      if (end < 0) return;
      try {
        const reply = JSON.parse(buffer.slice(0, end));
        if (reply.error) reject(new Error(reply.error));
        else resolve(reply.result);
      } catch (error) { reject(error); }
      socket.end();
    });
    socket.on('error', reject);
    socket.on('end', () => { if (!buffer.includes('\n')) reject(new Error('World disconnected')); });
    socket.on('close', () => signal?.removeEventListener('abort', abort));
    if (signal?.aborted) abort();
  });
}

export async function apply(ctx) {
  const registered = new Map();
  async function sync() {
    const { tools } = await rpc({ method: 'describe' });
    const names = new Set(tools.map(tool => tool.function.name));
    for (const [name, dispose] of registered) {
      if (!names.has(name)) { dispose(); registered.delete(name); }
    }
    for (const { function: tool } of tools) {
      if (registered.has(tool.name)) continue;
      registered.set(tool.name, ctx.tools.register({
        name: tool.name, description: tool.description, parameters: tool.parameters,
        output: {
          schema: { type: 'object' },
          render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
        },
        async execute(args, exec) {
          const result = await rpc({
            method: 'execute', name: tool.name, arguments: args, call_id: String(exec.callId),
          }, exec.signal);
          if (tool.name === 'world_tools') await sync();
          if (tool.name === 'world_finish' && result.status === 'idle') exec.concludeTurn();
          if (tool.name === 'world_pause' && result.status === 'waiting') exec.concludeTurn();
          return result;
        },
      }));
    }
  }
  const description = await rpc({ method: 'describe' });
  const context = installContext(ctx, rpc, description.compaction_instruction);
  await sync();
  const routeSchedule = await installCapabilities(ctx, rpc);

  // The loop probes pre-step once more after plain completion. Preserve that empty
  // decision; adding context there would accidentally create another activity.
  let toolContinuation = false;
  ctx.on('session/event', (_session, event) => {
    if (event.type === 'step/start') toolContinuation = false;
    if (event.type === 'tool/call') toolContinuation = true;
  });
  ctx.on('agent/request', async (payload, next) => {
    const config = await next();
    reconcile(payload.agent.session, payload.turn, payload.step);
    return config;
  });
  ctx.on('agent/pre-step', async (payload, next) => {
    if (payload.messages.length === 0 && !toolContinuation) {
      return { kind: 'enter', messages: [] };
    }
    // Maintenance must be intercepted before the stock pressure listener;
    // initialization and archive indexing must never trigger a paid compaction.
    await projectVideoResults(payload.agent.session, rpc, payload.signal);
    reconcile(payload.agent.session, payload.turn, payload.step, true);
    if (payload.messages.some(m => m.content?.some(b => {
      if (b.type !== 'text') return false;
      try { const v = JSON.parse(b.text); return v.type === 'world.maintenance' && v.action === 'initialize'; }
      catch { return false; }
    }))) return { kind: 'reject' };
    if (await maintain(payload, { messages: payload.messages }, ctx, rpc)) return { kind: 'reject' };
    const projection = payload.messages.flatMap(m => m.content || []).find(b => {
      if (b.type !== 'text') return false;
      try { return JSON.parse(b.text).type === 'world.project_activities'; } catch { return false; }
    });
    if (projection) {
      try {
        await projectActivities(payload, ctx, rpc, JSON.parse(projection.text));
        await rpc({ method: 'activity_projection_result', error: null });
      } catch (error) {
        await rpc({ method: 'activity_projection_result', error: String(error) });
      }
      return { kind: 'reject' };
    }
    let decision = await next();
    if (decision.kind !== 'enter') return decision;
    decision = await routeSchedule(payload, decision);
    if (decision.kind !== 'enter') return decision;
    if (decision.messages.length === 0 && !toolContinuation) return decision;
    const result = await rpc({ method: 'step' }, payload.signal);
    if (result.stop) return { kind: 'reject' };
    await context.compactForDecision(payload.agent, payload.signal);
    return {
      ...decision,
      messages: [...decision.messages, runtimeMessage(payload.agent.session, result.context)],
    };
  }, { prepend: true });
  await rpc({ method: 'ready' });
}
