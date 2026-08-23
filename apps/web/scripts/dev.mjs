import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const incoming = process.argv.slice(2);
const nextArguments = [];
let apiUrl = process.env.HZCU_API_INTERNAL_URL;

for (let index = 0; index < incoming.length; index += 1) {
  if (incoming[index] === "--api-url") {
    apiUrl = incoming[index + 1];
    index += 1;
    continue;
  }
  nextArguments.push(incoming[index]);
}

if (apiUrl) {
  process.env.HZCU_API_INTERNAL_URL = apiUrl;
}

const nextCli = fileURLToPath(new URL("../node_modules/next/dist/bin/next", import.meta.url));
const child = spawn(process.execPath, [nextCli, "dev", ...nextArguments], {
  env: process.env,
  stdio: "inherit",
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
