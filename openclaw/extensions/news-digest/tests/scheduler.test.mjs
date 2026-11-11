import assert from "node:assert/strict";
import test from "node:test";
import { createScheduler, newsJobName, scheduledNewsMessage, SCHEDULED_TOOLS } from "../scheduler.mjs";

const profile = (changes = {}) => ({ configured: true, enabled: true, schedule: { morning: "08:00", evening: "20:00", timezone: "Europe/Moscow" }, ...changes });

function harness(initial = [], options = {}) {
  const jobs = structuredClone(initial);
  const calls = [];
  let serial = 0;
  const runner = async (file, argv, execOptions) => {
    calls.push({ file, argv, execOptions });
    const index = argv.indexOf("automations");
    const args = argv.slice(index + 1);
    const value = (flag) => args[args.indexOf(flag) + 1];
    if (args[0] === "list") return { stdout: JSON.stringify({ jobs }) };
    const job = {
      id: args[0] === "edit" ? args[1] : `news-${++serial}`,
      name: value("--name"),
      agentId: value("--agent"),
      enabled: !args.includes("--disabled") && !args.includes("--disable"),
      schedule: { kind: "cron", expr: value("--cron"), tz: value("--tz") },
      payload: { kind: "agentTurn", message: value("--message"), toolsAllow: value("--tools").split(",") },
      delivery: { mode: "none", channel: value("--channel"), to: value("--to") },
    };
    if (args[0] === "add") {
      job.declarationKey = value("--declaration-key");
      jobs.push(job);
      return { stdout: JSON.stringify({ created: true, job }) };
    }
    const oldIndex = jobs.findIndex((item) => item.id === job.id);
    jobs[oldIndex] = { ...jobs[oldIndex], ...job };
    return { stdout: JSON.stringify(jobs[oldIndex]) };
  };
  const scheduler = createScheduler({ runCommand: runner, to: "123456", command: process.execPath, commandArgs: ["/app/dist/index.js"], ...options });
  return { jobs, calls, scheduler };
}

test("first setup creates two capped isolated jobs using native timers and fixed owner route", async () => {
  const { scheduler, jobs, calls } = harness();
  const result = await scheduler.reconcile(profile());
  assert.deepEqual(result.map((item) => item.action), ["created", "created"]);
  assert.equal(jobs.length, 2);
  assert.deepEqual(jobs.map((job) => job.schedule.expr), ["0 8 * * *", "0 20 * * *"]);
  for (const job of jobs) {
    assert.equal(job.enabled, true);
    assert.deepEqual(job.payload.toolsAllow, SCHEDULED_TOOLS);
    assert.equal(job.delivery.mode, "none");
    assert.equal(job.delivery.to, "123456");
    assert.match(job.payload.message, /news_collect/);
    assert.match(job.payload.message, /news_publish/);
    assert.match(job.payload.message, /NO_REPLY/);
  }
  assert.deepEqual(calls[0].argv, ["/app/dist/index.js", "automations", "list", "--all", "--json"]);
  assert.equal(calls[1].file, process.execPath);
  assert.equal(calls[1].execOptions.timeout, 30_000);
  assert.ok(calls[1].argv.includes("--session"));
  assert.ok(calls[1].argv.includes("--no-deliver"));
  assert.ok(!calls[1].argv.includes("--announce"));
});

test("unconfigured or explicitly paused profile leaves both automations disabled", async () => {
  for (const settings of [profile({ configured: false }), profile({ enabled: false }), {}]) {
    const { scheduler, jobs } = harness();
    await scheduler.reconcile(settings);
    assert.equal(jobs.length, 2);
    assert.ok(jobs.every((job) => job.enabled === false));
  }
});

test("changing time or pausing updates existing jobs without touching calendar or another agent", async () => {
  const unrelated = [
    { id: "calendar", name: "Утренняя сводка", agentId: "main", enabled: true },
    { id: "other-agent", name: newsJobName("morning"), declarationKey: newsJobName("morning"), agentId: "other", enabled: true },
  ];
  const { scheduler, jobs, calls } = harness(unrelated);
  await scheduler.reconcile(profile());
  const ids = jobs.slice(2).map((job) => job.id);
  await scheduler.reconcile(profile({ enabled: false, schedule: { morning: "07:30", evening: "21:05", timezone: "Asia/Tbilisi" } }));
  assert.equal(jobs.length, 4);
  assert.deepEqual(jobs.slice(0, 2), unrelated);
  assert.deepEqual(jobs.slice(2).map((job) => job.id), ids);
  assert.deepEqual(jobs.slice(2).map((job) => job.schedule.expr), ["30 7 * * *", "5 21 * * *"]);
  assert.ok(jobs.slice(2).every((job) => !job.enabled && job.schedule.tz === "Asia/Tbilisi"));
  assert.equal(calls.filter(({ argv }) => argv.includes("add")).length, 2);
  assert.equal(calls.filter(({ argv }) => argv.includes("edit")).length, 2);
});

test("recognizes an existing declaration after a display/name edit and legacy stable names", async () => {
  const { scheduler, jobs } = harness([
    { id: "a", name: "My morning", declarationKey: newsJobName("morning"), agentId: "main" },
    { id: "b", name: newsJobName("evening"), agentId: "main" },
  ]);
  await scheduler.reconcile(profile());
  assert.deepEqual(jobs.map((job) => job.id), ["a", "b"]);
  assert.ok(jobs.every((job) => job.enabled));
});

test("ambiguous managed declarations fail before any mutation", async () => {
  const { scheduler, calls } = harness([
    { id: "a", name: newsJobName("morning"), agentId: "main" },
    { id: "b", name: newsJobName("morning"), agentId: "main" },
  ]);
  await assert.rejects(scheduler.reconcile(profile()), /Multiple managed automations/);
  assert.equal(calls.length, 1);
});

test("invalid settings fail before command execution", () => {
  for (const schedule of [
    { morning: "24:00", evening: "20:00" },
    { morning: "08:00; curl evil", evening: "20:00" },
    { morning: "08:00", evening: "08:00" },
    { morning: "08:00", evening: "20:00", timezone: "not/a/timezone" },
  ]) {
    const { scheduler, calls } = harness();
    assert.throws(() => scheduler.reconcile(profile({ schedule })));
    assert.equal(calls.length, 0);
  }
});

test("failed inventory cannot silently create duplicate jobs", async () => {
  for (const output of ["warning: failed", "{}", "null"]) {
    let calls = 0;
    const scheduler = createScheduler({ to: "123456", runCommand: async () => { calls++; return output; } });
    await assert.rejects(scheduler.reconcile(profile()), /JSON|jobs array/);
    assert.equal(calls, 1);
  }
});

test("serialized concurrent updates do not duplicate automations", async () => {
  const { scheduler, jobs } = harness();
  await Promise.all([scheduler.reconcile(profile()), scheduler.reconcile(profile({ enabled: false }))]);
  assert.equal(jobs.length, 2);
  assert.ok(jobs.every((job) => !job.enabled));
});

test("status only reports managed automations and handles absent jobs", async () => {
  const { scheduler } = harness([{ id: "calendar", name: "Утренняя сводка" }]);
  assert.deepEqual(await scheduler.status(), [{ slot: "morning", job: null }, { slot: "evening", job: null }]);
});

test("scheduled messages reject arbitrary edition names", () => {
  assert.throws(() => scheduledNewsMessage("other"), /Unknown news edition/);
});
