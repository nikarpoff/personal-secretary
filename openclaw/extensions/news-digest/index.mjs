import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { isAbsolute, join } from 'node:path';
import { NewsDigest } from './runtime.mjs';
import { createScheduler } from './scheduler.mjs';

const exec = promisify(execFile);
const schemas = {
  news_settings: {
    action: { type: 'string', enum: ['get', 'update', 'feedback'] },
    ...Object.fromEntries(['topics', 'exclude', 'preferences'].map((key) => [key, { type: 'array', maxItems: 30, items: { type: 'string', maxLength: 300 } }])),
    language: { type: 'string', maxLength: 40 }, timezone: { type: 'string' },
    morning: { type: 'string', pattern: '^([01]\\d|2[0-3]):[0-5]\\d$' },
    evening: { type: 'string', pattern: '^([01]\\d|2[0-3]):[0-5]\\d$' },
    maxItems: { type: 'integer', minimum: 1, maximum: 10 }, enabled: { type: 'boolean' },
    text: { type: 'string', maxLength: 2000 }, issueId: { type: 'string' }, batchId: { type: 'string' }, articleNumber: { type: 'integer', minimum: 1 },
  },
  news_sources: {
    action: { type: 'string', enum: ['list', 'add', 'remove'] }, name: { type: 'string', maxLength: 100 },
    url: { type: 'string', maxLength: 2000 }, feedUrl: { type: 'string', maxLength: 2000 }, sourceId: { type: 'integer', minimum: 1 },
  },
  news_collect: { slot: { type: 'string', enum: ['morning', 'evening', 'manual'] } },
  news_publish: {
    batchId: { type: 'string' }, preview: { type: 'boolean', default: false },
    stories: { type: 'array', minItems: 1, maxItems: 10, items: { type: 'object', additionalProperties: false,
      properties: { articleId: { type: 'integer', minimum: 1 }, summary: { type: 'string', minLength: 1, maxLength: 800 } },
      required: ['articleId', 'summary'] } },
  },
  news_history: {
    action: { type: 'string', enum: ['get', 'resolve'] }, limit: { type: 'integer', minimum: 1, maximum: 20 },
    issueId: { type: 'string' }, delivered: { type: 'boolean' },
  },
};
const descriptions = {
  news_settings: 'Read/update the owner news profile, schedule or pause; record feedback. Arrays replace old values. Requires explicit owner intent to enable.',
  news_sources: 'List/add/remove public RSS sources using blogwatcher; no shell commands. At most 30 sources.',
  news_collect: 'Collect a bounded set of fresh candidates, current preferences, feedback and sent-issue context. Read the news-digest skill first.',
  news_publish: 'Preview or send one numbered digest to the fixed Telegram owner. Only supplied candidate IDs are accepted. Owns delivery/history; after success return NO_REPLY.',
  news_history: 'Read recent numbered issues for feedback references. Resolve uncertain delivery only after the owner explicitly confirms whether the message arrived.',
};

function cliJson(result) {
  try { return JSON.parse(String(result.stdout ?? result).trim()); }
  catch { throw new Error('OpenClaw returned invalid JSON; delivery confirmation is unavailable'); }
}

function messageReceipt(value) {
  if (!value || typeof value !== 'object') return null;
  if (value.dryRun === true || value.ok === false || value.error) return null;
  const id = value.messageId ?? value.message_id;
  if (/^[1-9]\d*$/.test(String(id ?? ''))) return {
    messageId: String(id),
    ...(value.chatId !== undefined ? { chatId: String(value.chatId) } : {}),
  };
  for (const key of ['payload', 'result', 'data']) { const found = messageReceipt(value[key]); if (found) return found; }
  return null;
}

export function createNewsPlugin({ execute = (file, args, options) => exec(file, args, { ...options, windowsHide: true }) } = {}) {
 return {
  id: 'news-digest',
  name: 'Personal news digest',
  register(api) {
    const config = api.pluginConfig ?? {};
    const agentId = config.agentId ?? 'main';
    const ownerId = String(config.ownerId ?? process.env.TELEGRAM_CHAT_ID ?? '');
    const ready = /^[0-9]+$/.test(ownerId);
    const timezone = config.timezone ?? api.config?.agents?.defaults?.userTimezone ?? 'Europe/Moscow';
    if (config.cliPath && !isAbsolute(config.cliPath)) throw new Error('news-digest cliPath must be absolute');
    const command = config.cliPath ? process.execPath : 'openclaw';
    const commandArgs = config.cliPath ? [config.cliPath] : [];
    const runCommand = execute;
    const scheduler = createScheduler({ runCommand, agentId, channel: 'telegram', to: ownerId || 'unconfigured', timezone, command, commandArgs });

    api.registerTool((ctx) => {
      if (!ctx.workspaceDir || ctx.agentId !== agentId) return null;
      const owner = ctx.senderIsOwner === true || (ctx.messageChannel === 'telegram' && ready && String(ctx.requesterSenderId ?? '') === ownerId);
      const match = /^agent:([^:]+):cron:([^:]+):run:[^:]+$/.exec(ctx.sessionKey ?? '');
      const scheduled = Boolean(match?.[1] === agentId);
      if (!owner && !scheduled) return null;
      const guard = () => { ctx.assertInvocationCurrent?.(); };
      async function authorize() {
        guard();
        if (scheduled) {
          const jobs = await scheduler.status();
          if (!jobs.some(({ job }) => job?.id === match[2] && job.enabled === true)) throw new Error('Not an enabled news automation');
          guard();
        }
      }
      const service = new NewsDigest({ workspaceDir: ctx.workspaceDir, scheduler, timezone, guard, deliveryReady: ready,
        runBlogwatcher: (args, home) => runCommand(config.blogwatcherPath ?? 'blogwatcher', args, {
          cwd: home, env: { ...process.env, HOME: home, USERPROFILE: home, BLOGWATCHER_DB: join(home, '.blogwatcher', 'blogwatcher.db') }, timeout: 90_000, maxBuffer: 256 * 1024,
        }),
        send: async (message) => {
          await authorize();
          const result = await runCommand(command, [...commandArgs, 'message', 'send', '--channel', 'telegram', '--target', ownerId, '--message', message, '--json'],
            { timeout: 60_000, maxBuffer: 256 * 1024 });
          const receipt = messageReceipt(cliJson(result));
          if (!receipt) throw new Error('Telegram returned no message ID; check the chat before retrying');
          return receipt;
        },
      });
      const names = scheduled ? ['news_collect', 'news_publish'] : Object.keys(schemas);
      return names.map((name) => ({
        name, label: name.replaceAll('_', ' '), description: descriptions[name],
        parameters: { type: 'object', additionalProperties: false, properties: schemas[name],
          ...(name === 'news_publish' ? { required: ['batchId', 'stories'] } : name === 'news_sources' ? { required: ['action'] } : {}) },
        async execute(_toolCallId, params) {
          try {
            await authorize();
            let result;
            if (name === 'news_settings') result = await service.settings(params);
            else if (name === 'news_sources') result = await service.manageSources(params);
            else if (name === 'news_collect') result = await service.collect({ slot: params.slot ?? 'manual', scheduled: Boolean(scheduled) });
            else if (name === 'news_publish') result = await service.publish(params);
            else result = await service.history(params);
            return { content: [{ type: 'text', text: JSON.stringify(result) }], details: result };
          } catch (error) {
            const message = error.cmd ? 'News command failed or timed out; check the gateway log and delivery history before retrying' : error.message;
            return { content: [{ type: 'text', text: message }], isError: true };
          }
        },
      }));
    }, { optional: true, names: Object.keys(schemas) });
  },
 };
}

export default createNewsPlugin();
