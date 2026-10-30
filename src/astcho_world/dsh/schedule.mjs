// Embedding adapter for the pinned rc2 Schedule service's activation dependency.
// Resolve real SDK Agents, never construct a second model loop or a Web controller.
export const name = 'astcho-world-schedule-host';
export const inject = ['agents'];

export function apply(ctx) {
  const waiting = new Map();
  ctx.on('agent/created', ({ agent }) => {
    const id = agent.session.id;
    for (const resolve of waiting.get(id) ?? []) resolve({ agent });
    waiting.delete(id);
  });
  ctx.provide('sessionController', {
    async resolveAgent(sessionId) {
      if (sessionId !== process.env.ASTCHO_DSH_SESSION_ID) {
        return { error: new Error('Schedule target is outside this World') };
      }
      const agent = ctx.agents.get(sessionId);
      if (agent) return { agent };
      return new Promise(resolve => {
        const listeners = waiting.get(sessionId) ?? [];
        listeners.push(resolve);
        waiting.set(sessionId, listeners);
      });
    },
  });
  ctx.effect(() => () => {
    for (const listeners of waiting.values()) {
      for (const resolve of listeners) resolve({ error: new Error('World is shutting down') });
    }
    waiting.clear();
  });
}
