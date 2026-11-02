// OpenClaw's audio CLI: upload the attachment to Whisper, print only the transcript.
import { readFile, stat } from "node:fs/promises";
import { basename, extname } from "node:path";

const MAX_AUDIO_BYTES = 20 * 1024 * 1024;
const TIMEOUT_MS = 120_000;
const MIME_TYPES = {
  ".ogg": "audio/ogg",
  ".oga": "audio/ogg",
  ".opus": "audio/ogg",
  ".mp3": "audio/mpeg",
  ".mpga": "audio/mpeg",
  ".mp4": "audio/mp4",
  ".m4a": "audio/mp4",
  ".wav": "audio/wav",
  ".webm": "audio/webm",
  ".flac": "audio/flac",
};

class TranscriptionError extends Error {}

async function transcribe() {
  const filePath = process.argv[2];
  if (!filePath || process.argv.length !== 3) {
    throw new TranscriptionError("Expected one local audio attachment path");
  }

  let endpoint;
  try {
    endpoint = new URL(process.env.WHISPER_URL);
  } catch {
    throw new TranscriptionError("WHISPER_URL must be an absolute HTTP(S) transcription URL");
  }
  if (!["https:", "http:"].includes(endpoint.protocol) || endpoint.username || endpoint.password) {
    throw new TranscriptionError("Use an HTTP(S) WHISPER_URL without embedded credentials");
  }

  const file = await stat(filePath);
  if (!file.isFile() || file.size === 0 || file.size > MAX_AUDIO_BYTES) {
    throw new TranscriptionError("Audio attachment must be a nonempty file up to 20 MiB");
  }
  const audio = await readFile(filePath);
  if (audio.length === 0 || audio.length > MAX_AUDIO_BYTES) {
    throw new TranscriptionError("Audio attachment size changed or exceeds 20 MiB");
  }

  const form = new FormData();
  const mimeType = MIME_TYPES[extname(filePath).toLowerCase()] ?? "application/octet-stream";
  form.append("file", new Blob([audio], { type: mimeType }), basename(filePath));
  form.append("model", process.env.WHISPER_MODEL?.trim() || "whisper-1");
  form.append("priority", "0");
  form.append("response_format", "json");
  form.append("language", process.env.WHISPER_LANGUAGE?.trim() || "ru");
  form.append("batch_size", "4");
  form.append("vad_enabled", "true");
  form.append("word_timestamps", "false");

  const headers = { Accept: "application/json" };
  // Use only the transcription service's own optional credential.
  const apiKey = process.env.WHISPER_API_KEY?.trim();
  if (apiKey) {
    headers.Authorization = `Bearer ${apiKey}`;
  }
  const response = await fetch(endpoint, {
    method: "POST",
    headers,
    body: form,
    signal: AbortSignal.timeout(TIMEOUT_MS),
    redirect: "error",
  });
  if (!response.ok) {
    // Upstream response bodies may contain private speech or request credentials.
    throw new TranscriptionError(`Whisper returned HTTP ${response.status}`);
  }

  let result;
  try {
    result = await response.json();
  } catch (error) {
    if (error.name === "TimeoutError" || error.name === "AbortError") throw error;
    throw new TranscriptionError("Whisper returned invalid JSON");
  }
  if (typeof result?.text !== "string" || !result.text.trim()) {
    throw new TranscriptionError("Whisper returned no recognized text");
  }
  process.stdout.write(`${result.text.trim()}\n`);
}

transcribe().catch((error) => {
  const message = error instanceof TranscriptionError
    ? error.message
    : error.name === "TimeoutError" || error.name === "AbortError"
      ? `Whisper request timed out after ${TIMEOUT_MS / 1000} seconds`
      : "Could not read the audio attachment or connect to Whisper";
  console.error(`Whisper transcription failed: ${message}`);
  process.exitCode = 1;
});
