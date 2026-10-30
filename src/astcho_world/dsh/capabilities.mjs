import { createHash } from 'node:crypto';

const nativeTools = new Set([
  'web_search', 'web_fetch', 'skill', 'schedule_create', 'schedule_list',
  'schedule_update', 'schedule_delete',
]);

export async function installCapabilities(ctx, rpc) {
  const sessionId = process.env.ASTCHO_DSH_SESSION_ID;
  const publish = async () => {
    const catalog = (await ctx.schedule.catalog()).filter(r => r.sessionId === sessionId);
    for (const record of catalog) {
      if (!record.lastDelivery) continue;
      let before;
      do {
        const page = await ctx.schedule.history({ sessionId, id: record.id, limit: 100, before });
        if (!page.records) throw new Error(`Native reminder history unavailable: ${page.code}`);
        await rpc({ method: 'reminder_delivery', reminders: page.records.map(receipt => {
          if (receipt.prompt === undefined) throw new Error('Native receipt has no original prompt');
          return {
            schedule_id: record.id, occurrence_at: receipt.scheduledAt,
            reminder_prompt: receipt.prompt,
          };
        }) });
        before = page.nextBefore;
      } while (before !== undefined);
    }
    await rpc({ method: 'reminder_catalog', reminders: catalog });
  };
  // Readers see a projection; DSH's storage domain remains the only timer authority.
  let publication = Promise.resolve();
  ctx.on('schedule/changed', () => {
    publication = publication.catch(() => {}).then(publish);
    publication.catch(error => ctx.logger.warn(`World reminder projection: ${error}`));
  });
  const { reminders: legacy } = await rpc({ method: 'reminder_imports' });
  for (const old of legacy) {
    // Stable marker permits recovery if the process dies between native commit and
    // World's migration receipt. Original row/text/time are retained unchanged.
    const title = `World迁移:${createHash('sha256').update(old.id).digest('hex')}`;
    let record = (await ctx.schedule.catalog()).find(r =>
      r.sessionId === sessionId && r.title === title && r.prompt === old.text.trim());
    if (!record) record = await ctx.schedule.create(sessionId, {
      title, prompt: old.text,
      ...(old.due * 1000 > Date.now()
        ? { at: new Date(old.due * 1000).toISOString() } : { after_seconds: 1 }),
    });
    await rpc({ method: 'reminder_imported', legacy_id: old.id, native_id: record.id });
  }
  await publish();
  await rpc({ method: 'capabilities' });

  const admitted = new Map();
  let receipts = Promise.resolve();
  ctx.on('tools/execute', async (exec, next) => {
    if (!nativeTools.has(exec.name)) return next();
    const identity = { name: exec.name, call_id: String(exec.callId) };
    await rpc({ method: 'native_start', ...identity, arguments: exec.arguments }, exec.signal);
    admitted.set(identity.call_id, identity);
    return next();
  });
  ctx.on('session/event', (_session, event) => {
    if (event.type !== 'tool/result') return;
    const result = event.data.message;
    const identity = admitted.get(String(result.toolCallId));
    if (!identity) return;
    admitted.delete(identity.call_id);
    // Observe the final durable model-facing result, after timeout and other
    // native policies. Never call an intermediate executor result a success.
    receipts = receipts.then(() => rpc({ method: 'native_result', ...identity, result: {
      content: result.content, isError: result.isError,
    } }));
    receipts.catch(error => ctx.logger.warn(`World native receipt: ${error}`));
  });

  return async function routeSchedule(payload, decision) {
    await receipts;
    const deliveries = decision.messages.filter(m => m.source?.kind === 'schedule');
    if (!deliveries.length) return decision;
    const messages = decision.messages.filter(m => m.source?.kind !== 'schedule');
    // The native inbox turn spends no LLM call. Its committed delivery history
    // above relays to World; replaying it repairs a crash between native commit
    // and notification commit without inventing a second timer authority.
    return payload.messages.every(m => m.source?.kind === 'schedule')
      ? { kind: 'reject' } : { ...decision, messages };
  };
}
