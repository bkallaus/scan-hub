// Tag one document's text with topics using Desert Ant Labs' Gist, on this
// machine. ScanHub's Python side runs this once per scanned document:
//
//   echo '{"text": "...", "topK": 3}' | node gist/tag.mjs
//   -> [{"slug":"finance","name":"Personal Finance","score":0.91}, ...]
//
// The model (~74 MB) downloads from Hugging Face on first use into
// GIST_MODEL_DIR (or ~/.cache when unset) and is reused after that.
import { Gist } from "@desert-ant-labs/gist/native"; // server-side Node build

const LICENSE_FLUSH_MS = 5000; // don't let a slow network hold up the scan

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  return Buffer.concat(chunks).toString("utf8");
}

async function main() {
  const req = JSON.parse((await readStdin()) || "{}");
  const text = typeof req.text === "string" ? req.text : "";
  const topK = Number.isInteger(req.topK) && req.topK > 0 ? req.topK : 3;

  const directory = process.env.GIST_MODEL_DIR || undefined;
  const gist = await Gist.load(directory ? { directory } : {});
  try {
    const topics = await gist.classify(text, { topK });
    process.stdout.write(JSON.stringify(topics.map(({ slug, name, score }) => ({ slug, name, score }))));
    // The process exits right after this, so send the SDK's pending license
    // requests first (bounded, so being offline doesn't delay the scan).
    await Promise.race([
      gist.flushTelemetry().catch(() => false),
      new Promise((r) => setTimeout(r, LICENSE_FLUSH_MS).unref()),
    ]);
  } finally {
    gist.dispose();
  }
}

main().catch((e) => {
  process.stderr.write(`gist: ${e?.message || e}\n`);
  process.exit(1);
});
