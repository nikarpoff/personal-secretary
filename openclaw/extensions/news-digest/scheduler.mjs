export const NEWS_SLOTS = Object.freeze(["morning", "evening"]);
export const NEWS_JOB_PREFIX = "news-digest:";
export const SCHEDULED_TOOLS = Object.freeze(["read", "web_fetch", "news_collect", "news_publish"]);

export function newsJobName(slot) {
  if (!NEWS_SLOTS.includes(slot)) throw new Error("Unknown news edition");
  return `${NEWS_JOB_PREFIX}${slot}`;
}

export function scheduledNewsMessage(slot) {
  newsJobName(slot);
  return [
    `Run the news-digest skill for the scheduled ${slot} edition.`,
    `Call news_collect with slot="${slot}" and use its current profile and returned candidates.`,
    "Select relevant new stories, combine overlapping coverage, and read full articles with web_fetch only when needed.",
    "Publish the final edition once with news_publish, preserving the candidate ids and edition identity returned by news_collect.",
    "news_publish owns delivery and the sent history; do not send via another tool.",
    "If the profile is disabled or unconfigured, no suitable new stories exist, or this edition is already delivered, send nothing.",
    "Never change preferences, sources, feedback or schedules during a scheduled edition.",
    "After completion return exactly NO_REPLY. Surface tool failures as errors; never invent a successful delivery.",
  ].join(" ");
}

function string(value, label) {
  if (typeof value !== "string" || !value.trim() || value.includes("\0")) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value.trim();
}

function scheduleFor(profile, fallbackTimezone) {
  if (!profile || typeof profile !== "object" || Array.isArray(profile)) {
    throw new Error("A news profile is required");
  }
  const schedule = profile.schedule ?? {};
  const timezone = string(schedule.timezone ?? profile.timezone ?? fallbackTimezone, "Timezone");
  try {
    new Intl.DateTimeFormat("en", { timeZone: timezone }).format(0);
  } catch {
    throw new Error("Timezone must be a valid IANA timezone");
  }
  const times = {};
  for (const slot of NEWS_SLOTS) {
    const value = schedule[slot] ?? profile[slot] ?? (slot === "morning" ? "08:00" : "20:00");
    if (typeof value !== "string" || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value)) {
      throw new Error(`${slot} time must use HH:MM`);
    }
    const [hour, minute] = value.split(":").map(Number);
    times[slot] = { time: value, cron: `${minute} ${hour} * * *` };
  }
  if (times.morning.time === times.evening.time) {
    throw new Error("Morning and evening editions must use different times");
  }
  return { times, timezone, enabled: profile.configured === true && profile.enabled === true };
}

function parseJson(result, label) {
  const output = typeof result === "string" ? result : result?.stdout;
  if (typeof output !== "string") throw new Error(`${label} returned no JSON output`);
  try {
    return JSON.parse(output.trim());
  } catch {
    throw new Error(`${label} returned invalid JSON`);
  }
}

function jobsFrom(result) {
  const payload = parseJson(result, "OpenClaw automations list");
  const jobs = Array.isArray(payload) ? payload : payload?.jobs;
  if (!Array.isArray(jobs)) throw new Error("OpenClaw automations list returned no jobs array");
  return jobs;
}

function isManaged(job, slot, agentId) {
  if (!job || typeof job !== "object") return false;
  if (job.agentId && job.agentId !== agentId) return false;
  const name = newsJobName(slot);
  return job.declarationKey ? job.declarationKey === name : job.name === name;
}

export function createScheduler({
  runCommand,
  agentId = "main",
  channel = "telegram",
  to,
  timezone = "Europe/Moscow",
  command = "openclaw",
  commandArgs = [],
}) {
  if (typeof runCommand !== "function") throw new Error("runCommand is required");
  agentId = string(agentId, "Agent id");
  channel = string(channel, "Channel");
  to = string(String(to ?? ""), "Delivery destination");
  command = string(command, "OpenClaw executable");
  if (!Array.isArray(commandArgs) || commandArgs.some((arg) => typeof arg !== "string" || arg.includes("\0"))) {
    throw new Error("commandArgs must be an array of literal strings");
  }
  commandArgs = [...commandArgs];
  let queue = Promise.resolve();

  const invoke = (args) => runCommand(command, [...commandArgs, "automations", ...args], {
    timeout: 30_000,
    maxBuffer: 1024 * 1024,
  });
  const inventory = async () => jobsFrom(await invoke(["list", "--all", "--json"]));

  function ownJobs(jobs) {
    const result = new Map();
    for (const slot of NEWS_SLOTS) {
      const matches = jobs.filter((job) => isManaged(job, slot, agentId));
      if (matches.length > 1) throw new Error(`Multiple managed automations match ${newsJobName(slot)}; resolve the duplicate jobs first`);
      if (matches[0] && (typeof matches[0].id !== "string" || !matches[0].id.trim())) {
        throw new Error(`Managed automation ${newsJobName(slot)} has no id`);
      }
      result.set(slot, matches[0]);
    }
    return result;
  }

  function commonArgs(slot, settings) {
    return [
      "--name", newsJobName(slot),
      "--agent", agentId,
      "--cron", settings.times[slot].cron,
      "--tz", settings.timezone,
      "--exact",
      "--session", "isolated",
      "--message", scheduledNewsMessage(slot),
      "--tools", SCHEDULED_TOOLS.join(","),
      "--timeout-seconds", "300",
      "--no-deliver",
      "--channel", channel,
      "--to", to,
    ];
  }

  async function reconcileOnce(profile) {
    const settings = scheduleFor(profile, timezone);
    const jobs = ownJobs(await inventory());
    const result = [];
    for (const slot of NEWS_SLOTS) {
      const existing = jobs.get(slot);
      const args = existing
        ? ["edit", existing.id, ...commonArgs(slot, settings), settings.enabled ? "--enable" : "--disable"]
        : ["add", "--declaration-key", newsJobName(slot), ...commonArgs(slot, settings), ...(settings.enabled ? [] : ["--disabled"])];
      const response = parseJson(await invoke(args), `OpenClaw automations ${existing ? "edit" : "add"}`);
      result.push({ slot, action: existing ? "updated" : "created", enabled: settings.enabled, time: settings.times[slot].time, timezone: settings.timezone, job: response?.job ?? response });
    }
    return result;
  }

  return {
    reconcile(profile) {
      const captured = JSON.parse(JSON.stringify(profile));
      scheduleFor(captured, timezone);
      const task = queue.then(() => reconcileOnce(captured));
      queue = task.catch(() => {});
      return task;
    },
    async status() {
      const managed = ownJobs(await inventory());
      return NEWS_SLOTS.map((slot) => ({ slot, job: managed.get(slot) ?? null }));
    },
  };
}
