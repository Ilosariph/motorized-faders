/*
 * Background script: keeps a WebSocket to each host that controls media speed
 * and tells them the speed of the page they act on: the frame playing that
 * started or changed most recently, else the one with media that changed most
 * recently. Speed requests from any host go to that frame.
 *
 * Hosts: the faders host (faders/browser_bridge.py) and the AgentDeck daemon
 * (bridge/src/browser-media-bridge.ts). Both speak the same protocol; a host
 * that is not running is retried quietly.
 */
"use strict";

const HOSTS = ["ws://127.0.0.1:47391", "ws://127.0.0.1:47392"];
const RETRY_MS = 3000;

// "tabId:frameId" -> { tabId, frameId, playing, rate, since }
const frames = new Map();
// url -> { socket, lastSent } for the hosts connected now
const hosts = new Map();

function current() {
  let best = null;
  for (const f of frames.values()) {
    if (f.rate === null) continue;
    if (!best || (f.playing && !best.playing)
        || (f.playing === best.playing && f.since > best.since)) {
      best = f;
    }
  }
  return best;
}

async function report() {
  if (hosts.size === 0) return;
  const f = current();
  let title = "";
  if (f) {
    try {
      title = (await browser.tabs.get(f.tabId)).title || "";
    } catch (e) {
      // Tab closed in the meantime; its frames go with onRemoved.
    }
  }
  const message = JSON.stringify({
    type: "state",
    rate: f ? f.rate : null,
    playing: f ? f.playing : false,
    title,
  });
  for (const host of hosts.values()) {
    if (message === host.lastSent) continue;
    host.lastSent = message;
    host.socket.send(message);
  }
}

browser.runtime.onMessage.addListener((message, sender) => {
  if (message.type !== "media" || !sender.tab) return;
  const key = `${sender.tab.id}:${sender.frameId}`;
  if (!message.hasMedia) {
    frames.delete(key);
  } else {
    const old = frames.get(key);
    const changed = !old || old.playing !== message.playing || old.rate !== message.rate;
    frames.set(key, {
      tabId: sender.tab.id,
      frameId: sender.frameId,
      playing: message.playing,
      rate: message.rate,
      since: changed ? Date.now() : old.since,
    });
  }
  report();
});

browser.tabs.onRemoved.addListener((tabId) => {
  for (const [key, f] of frames) {
    if (f.tabId === tabId) frames.delete(key);
  }
  report();
});

function setRate(rate) {
  const f = current();
  if (!f) return;
  browser.tabs.sendMessage(f.tabId, { type: "set_rate", rate },
                           { frameId: f.frameId }).catch(() => {});
}

function connect(url) {
  const ws = new WebSocket(url);
  ws.onopen = () => {
    hosts.set(url, { socket: ws, lastSent: "" });
    report();
  };
  ws.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (e) {
      return;
    }
    if (message.type === "set_rate" && typeof message.rate === "number") {
      setRate(message.rate);
    }
  };
  ws.onclose = () => {
    if (hosts.get(url)?.socket === ws) hosts.delete(url);
    setTimeout(() => connect(url), RETRY_MS);
  };
}

// Pages open before the extension was installed or updated get the content
// script now; the guard in it keeps it from running twice.
browser.runtime.onInstalled.addListener(async () => {
  for (const tab of await browser.tabs.query({})) {
    browser.tabs.executeScript(tab.id, {
      file: "content.js", allFrames: true, matchAboutBlank: true,
    }).catch(() => {});
  }
});

for (const url of HOSTS) connect(url);
