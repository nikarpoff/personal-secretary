// OpenClaw's ${ENV} substitution preserves strings. Render only numeric web settings.
import { spawn } from "node:child_process";

const settings = {
  WEB_SEARCH_MAX_RESULTS: [5, 1, 10],
  WEB_SEARCH_TIMEOUT_SECONDS: [20, 1],
  WEB_SEARCH_CACHE_TTL_MINUTES: [15, 0, Infinity, false],
  WEB_FETCH_MAX_CHARS: [6000, 1],
  WEB_FETCH_MAX_CHARS_CAP: [8000, 1],
  WEB_FETCH_MAX_RESPONSE_BYTES: [250000, 1],
  WEB_FETCH_TIMEOUT_SECONDS: [20, 1],
  WEB_FETCH_CACHE_TTL_MINUTES: [15, 0, Infinity, false],
  WEB_FETCH_MAX_REDIRECTS: [3, 0],
};

try {
  process.stdin.setEncoding("utf8");
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const rendered = input.replace(/"\$\{(WEB_[A-Z_]+)\}"/g, (placeholder, name) => {
    const rule = settings[name];
    if (!rule) return placeholder;
    const [fallback, min, max = Infinity, integer = true] = rule;
    const raw = process.env[name]?.trim() || String(fallback);
    const value = Number(raw);
    if (!/^\d+(?:\.\d+)?$/.test(raw) || !Number.isFinite(value) || value < min || value > max
        || (integer && !Number.isSafeInteger(value))) {
      throw new Error(`${name} must be ${integer ? "an integer" : "a number"} >= ${min}${Number.isFinite(max) ? ` and <= ${max}` : ""}`);
    }
    return JSON.stringify(value);
  });
  const child = spawn(process.execPath, ["dist/index.js", ...process.argv.slice(2)], {
    stdio: ["pipe", "inherit", "inherit"],
  });
  child.on("error", () => {
    console.error("Could not start the OpenClaw configuration command");
    process.exitCode = 1;
  });
  child.stdin.on("error", () => {}); // A rejected patch may close stdin before it is drained.
  child.stdin.end(rendered);
  for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill(signal));
  child.on("exit", (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
} catch (error) {
  console.error(`Could not render OpenClaw web settings: ${error.message}`);
  process.exitCode = 1;
}
