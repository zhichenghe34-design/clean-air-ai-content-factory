"use strict";

const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");

const logPath = process.env.SHIYI_EXTERNAL_NETWORK_LOG || "";
const loopback = new Set(["", "localhost", "127.0.0.1", "::1", "[::1]"]);

function targetHost(target) {
  if (target instanceof URL) return target.hostname.toLowerCase();
  if (typeof target === "string") {
    try {
      return new URL(target).hostname.toLowerCase();
    } catch {
      return "";
    }
  }
  if (target && typeof target === "object") {
    const value = target.hostname || target.host || "";
    return String(value).replace(/^\[|\]$/g, "").split(":", 1)[0].toLowerCase();
  }
  return "";
}

function rejectExternal(kind, target) {
  const host = targetHost(target);
  if (loopback.has(host)) return;
  const record = JSON.stringify({ kind, host }) + "\n";
  if (logPath) fs.appendFileSync(logPath, record, "utf8");
  throw new Error(`[offline-contract] blocked external ${kind} request to ${host}`);
}

const originalFetch = globalThis.fetch;
globalThis.fetch = function guardedFetch(input, init) {
  rejectExternal("fetch", input);
  return originalFetch.call(this, input, init);
};

for (const [name, client] of [["http", http], ["https", https]]) {
  const originalRequest = client.request;
  const originalGet = client.get;
  client.request = function guardedRequest(...args) {
    rejectExternal(`${name}.request`, args[0]);
    return originalRequest.apply(this, args);
  };
  client.get = function guardedGet(...args) {
    rejectExternal(`${name}.get`, args[0]);
    return originalGet.apply(this, args);
  };
}
