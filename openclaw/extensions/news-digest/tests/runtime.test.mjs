import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, dirname, basename } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { NewsDigest, canonicalUrl } from '../runtime.mjs';

async function fixture(t, options = {}) {
  const scratchRoot = resolve(process.env.NEWS_TEST_TMPDIR ?? tmpdir());
  const workspaceDir = await mkdtemp(join(scratchRoot, 'news-test-'));
  const root = join(workspaceDir, 'news-digest');
  await mkdir(join(root, '.blogwatcher'), { recursive: true });
  const db = new DatabaseSync(join(root, '.blogwatcher', 'blogwatcher.db'));
  db.exec(`CREATE TABLE blogs (id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT UNIQUE, feed_url TEXT);
    CREATE TABLE articles (id INTEGER PRIMARY KEY, blog_id INTEGER, title TEXT, url TEXT UNIQUE,
      published_date TEXT, discovered_date TEXT, is_read INTEGER DEFAULT 0);`);
  db.prepare('INSERT INTO blogs VALUES (1, ?, ?, ?)').run('Python', 'https://python.example/', 'https://python.example/feed');
  const now = new Date('2026-11-11T06:00:00Z');
  const calls = []; const sent = [];
  const scheduler = { reconcile: async (profile) => { calls.push(structuredClone(profile)); return []; } };
  const service = new NewsDigest({ workspaceDir, scheduler, now: () => now,
    runBlogwatcher: async (args) => { calls.push(args); return { stdout: 'Found 5 new articles' }; },
    send: async (message) => { sent.push(message); return { messageId: '42' }; }, ...options });
  t.after(async () => {
    db.close();
    assert.equal(dirname(workspaceDir), scratchRoot);
    assert.ok(basename(workspaceDir).startsWith('news-test-'));
    await rm(workspaceDir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
  });
  function article(id, title = `Python ${id}`, overrides = {}) {
    db.prepare('INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, 0)').run(id, overrides.sourceId ?? 1, title,
      overrides.url ?? `https://python.example/articles/${id}`, overrides.date ?? now.toISOString(), now.toISOString());
  }
  return { service, db, root, workspaceDir, now, calls, sent, article };
}

test('new profile is disabled and refuses activation without topics/sources', async (t) => {
  const f = await fixture(t);
  const initial = await f.service.settings();
  assert.equal(initial.profile.enabled, false);
  assert.deepEqual(initial.profile.topics, []);
  await assert.rejects(f.service.settings({ action: 'update', enabled: true }), /configure topics/);
  assert.equal(f.calls.length, 0);
});

test('preferences and custom times survive a new instance and get reconciled', async (t) => {
  const f = await fixture(t);
  await f.service.settings({ action: 'update', topics: ['Python'], exclude: ['crypto'], preferences: ['Practical tools'], morning: '09:30', evening: '21:00', enabled: true });
  const service = new NewsDigest({ workspaceDir: f.workspaceDir });
  const result = await service.settings();
  assert.equal(result.profile.schedule.morning, '09:30');
  assert.equal(result.profile.schedule.evening, '21:00');
  assert.equal(result.profile.enabled, true);
  assert.deepEqual(result.profile.exclude, ['crypto']);
  assert.equal(f.calls.at(-1).configured, true);
  assert.deepEqual(JSON.parse(await readFile(join(f.root, 'profile.json'), 'utf8')).preferences, ['Practical tools']);
});

test('invalid settings do not change profile or invoke scheduler', async (t) => {
  const f = await fixture(t);
  await assert.rejects(f.service.settings({ action: 'update', morning: '25:00' }), /HH:MM/);
  await assert.rejects(f.service.settings({ action: 'update', timezone: 'Unknown/Place' }), /time zone/i);
  await assert.rejects(f.service.settings({ action: 'update', maxItems: 100 }), /between/);
  assert.equal((await f.service.settings()).profile.schedule.morning, '08:00');
  assert.equal(f.calls.length, 0);
});

test('paused scheduled runs skip scanning while manual previews work', async (t) => {
  const f = await fixture(t);
  f.article(1);
  await f.service.settings({ action: 'update', topics: ['Python'] });
  f.calls.length = 0;
  assert.equal((await f.service.collect({ scheduled: true, slot: 'morning' })).skip, true);
  assert.equal(f.calls.length, 0);
  const batch = await f.service.collect();
  const preview = await f.service.publish({ batchId: batch.batchId, stories: [{ articleId: 1, summary: 'A practical tool.' }], preview: true });
  assert.match(preview.text, /1\. Python 1/);
  assert.equal(f.sent.length, 0);
  await assert.rejects(f.service.publish({ batchId: batch.batchId, stories: [{ articleId: 1, summary: 'A tool.' }] }), /paused/);
});

test('collection excludes old, excluded and duplicate URLs before reaching model', async (t) => {
  const f = await fixture(t);
  await f.service.settings({ action: 'update', topics: ['Python'], exclude: ['crypto'] });
  f.article(1);
  f.article(2, 'crypto launch');
  f.article(3, 'Old Python', { date: '2026-01-01T00:00:00Z' });
  f.article(4, 'duplicate', { url: 'https://python.example/articles/1?utm_source=mail' });
  const batch = await f.service.collect();
  assert.equal(batch.candidates.length, 1);
  assert.equal(batch.candidates[0].articleId, 1);
});

test('candidate limit fairly includes lower-volume sources', async (t) => {
  const f = await fixture(t);
  f.db.prepare('INSERT INTO blogs VALUES (2, ?, ?, ?)').run('Science', 'https://science.example', 'https://science.example/feed');
  for (let i = 1; i <= 100; i++) f.article(i);
  f.article(101, 'Science article', { sourceId: 2 });
  await f.service.settings({ action: 'update', topics: ['Python', 'Science'] });
  const batch = await f.service.collect();
  assert.equal(batch.candidates.length, 50);
  assert.ok(batch.candidates.slice(0, 2).some((c) => c.sourceId === 2));
});

test('confirmed publication persists receipt, rejects cross-batch IDs and suppresses replay', async (t) => {
  const f = await fixture(t); f.article(1); f.article(2);
  await f.service.settings({ action: 'update', topics: ['Python'], enabled: true });
  const batch = await f.service.collect({ scheduled: true, slot: 'morning' });
  await assert.rejects(f.service.publish({ batchId: batch.batchId, stories: [{ articleId: 999, summary: 'Fabricated.' }] }), /does not belong/);
  const params = { batchId: batch.batchId, stories: [{ articleId: 1, summary: 'A useful Python release.' }] };
  const result = await f.service.publish(params);
  assert.equal(result.sent, true);
  assert.equal(result.receipt.messageId, '42');
  assert.equal((await f.service.publish(params)).duplicate, true);
  assert.equal(f.sent.length, 1);
  assert.equal((await f.service.collect({ scheduled: true, slot: 'morning' })).skip, true);
  const evening = await f.service.collect({ scheduled: true, slot: 'evening' });
  assert.deepEqual(evening.candidates.map((a) => a.articleId), [2]);
});

test('concurrent publish calls cannot send the same batch twice', async (t) => {
  const f = await fixture(t); f.article(1);
  await f.service.settings({ action: 'update', topics: ['Python'], enabled: true });
  const batch = await f.service.collect();
  const params = { batchId: batch.batchId, stories: [{ articleId: 1, summary: 'Useful.' }] };
  await Promise.all([f.service.publish(params), f.service.publish(params)]);
  assert.equal(f.sent.length, 1);
});

test('uncertain delivery is recoverable only through explicit history resolution', async (t) => {
  let attempts = 0;
  const f = await fixture(t, { send: async () => { attempts++; throw new Error('timeout'); } });
  f.article(1);
  await f.service.settings({ action: 'update', topics: ['Python'], enabled: true });
  const batch = await f.service.collect();
  const params = { batchId: batch.batchId, stories: [{ articleId: 1, summary: 'Useful.' }] };
  await assert.rejects(f.service.publish(params), /delivery unknown/);
  const issue = (await f.service.history()).issues[0];
  assert.equal(issue.status, 'unknown');
  assert.equal((await f.service.publish(params)).status, 'unknown');
  assert.equal(attempts, 1);
  assert.equal((await f.service.collect()).candidates.length, 0);
  await f.service.history({ action: 'resolve', issueId: issue.id, delivered: false });
  assert.equal((await f.service.collect()).candidates.length, 1);
});

test('definitely rejected sends remain retryable', async (t) => {
  const f = await fixture(t, { send: async () => { throw Object.assign(new Error('rejected'), { definitelyNotSent: true }); } });
  f.article(1);
  await f.service.settings({ action: 'update', topics: ['Python'], enabled: true });
  const batch = await f.service.collect();
  await assert.rejects(f.service.publish({ batchId: batch.batchId, stories: [{ articleId: 1, summary: 'Useful.' }] }), /delivery failed/);
  assert.equal((await f.service.history()).issues[0].status, 'failed');
  assert.equal((await f.service.collect()).candidates.length, 1);
});

test('feedback binds a numbered story to its actual sent issue', async (t) => {
  const f = await fixture(t); f.article(1);
  await f.service.settings({ action: 'update', topics: ['Python'], enabled: true });
  const batch = await f.service.collect();
  const issue = await f.service.publish({ batchId: batch.batchId, stories: [{ articleId: 1, summary: 'Practical.' }] });
  const result = await f.service.settings({ action: 'feedback', issueId: issue.issueId, articleNumber: 1, text: 'More practical tools like this' });
  assert.equal(result.feedback[0].story.articleId, 1);
  await assert.rejects(f.service.settings({ action: 'feedback', articleNumber: 2, text: 'Bad' }), /Article number/);
});

test('preference changes invalidate previously collected batches', async (t) => {
  const f = await fixture(t); f.article(1);
  await f.service.settings({ action: 'update', topics: ['Python'], enabled: true });
  const batch = await f.service.collect();
  await f.service.settings({ action: 'update', preferences: ['Research only'] });
  await assert.rejects(f.service.publish({ batchId: batch.batchId, stories: [{ articleId: 1, summary: 'Useful.' }] }), /Preferences changed/);
  assert.equal(f.sent.length, 0);
});

test('source arguments are literal and local URLs are rejected', async (t) => {
  const f = await fixture(t);
  await assert.rejects(f.service.manageSources({ action: 'add', name: 'Local', url: 'http://127.0.0.1/feed' }), /public/);
  await assert.rejects(f.service.manageSources({ action: 'add', name: '--evil', url: 'https://news.example' }), /dash/);
  await f.service.manageSources({ action: 'add', name: 'News $(touch x)', url: 'https://news.example', feedUrl: 'https://news.example/rss' });
  assert.deepEqual(f.calls.at(-1), ['add', 'News $(touch x)', 'https://news.example/', '--feed-url', 'https://news.example/rss']);
});

test('pause persists even if the scheduler is temporarily unavailable', async (t) => {
  const f = await fixture(t);
  await f.service.settings({ action: 'update', topics: ['Python'], enabled: true });
  f.service.scheduler = { reconcile: async () => { throw new Error('gateway down'); } };
  await assert.rejects(f.service.settings({ action: 'update', enabled: false }), /gateway down/);
  assert.equal((await f.service.settings()).profile.enabled, false);
  assert.equal((await f.service.collect({ scheduled: true, slot: 'morning' })).skip, true);
});

test('URL normalization keeps identity parameters while stripping tracking', () => {
  assert.equal(canonicalUrl('https://example.org/watch?v=one&utm_source=x#section'), 'https://example.org/watch?v=one');
  assert.notEqual(canonicalUrl('https://example.org/watch?v=one'), canonicalUrl('https://example.org/watch?v=two'));
  assert.notEqual(canonicalUrl('https://example.org/?value=a/'), canonicalUrl('https://example.org/?value=a'));
});

test('feedback on a preview is linked to the preview, not an older sent issue', async (t) => {
  const f = await fixture(t); f.article(1);
  await f.service.settings({ action: 'update', topics: ['Python'] });
  const batch = await f.service.collect();
  const preview = await f.service.publish({ batchId: batch.batchId, stories: [{ articleId: 1, summary: 'Useful.' }], preview: true });
  const result = await f.service.settings({ action: 'feedback', batchId: preview.batchId, articleNumber: 1, text: 'More like this' });
  assert.equal(result.feedback[0].preview, true);
  assert.equal(result.feedback[0].story.articleId, 1);
  assert.equal(result.feedback[0].issueId, null);
  assert.equal((await f.service.history()).issues.length, 0);
});
