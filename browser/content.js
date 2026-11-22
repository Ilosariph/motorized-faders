/*
 * Content script, every frame: finds the page's <video> / <audio> elements,
 * reports their state (speed, position, length) to the background script and
 * sets their speed or position when a host asks.
 *
 * Elements are found three ways: those in the DOM by the media events that
 * pass through `document` on capture; detached ones (Spotify plays through an
 * element never added to the page) by wrapping HTMLMediaElement.play in the
 * page itself, on the sites listed in LOCK_HOSTS.
 *
 * Those sites also reset the speed on every track. There the page's own
 * writes to playbackRate are replaced with the speed the faders asked for;
 * writes from here use the browser's own setter and are not affected.
 */
"use strict";

(() => {
  if (window.__fadersMediaSpeed) return;
  window.__fadersMediaSpeed = true;

  const LOCK_HOSTS = ["open.spotify.com"];
  const EVENTS = ["play", "playing", "pause", "ratechange", "ended", "emptied",
                  "loadedmetadata", "seeked", "durationchange", "waiting"];
  // Firefox's limits for playbackRate.
  const RATE_MIN = 0.0625;
  const RATE_MAX = 16;

  const media = new Set();
  const locks = LOCK_HOSTS.includes(location.hostname);
  // Speed asked for by the faders, null until then.
  let wanted = null;
  let timer = null;
  let lastSent = "";

  function track(el) {
    if (media.has(el)) return;
    media.add(el);
    for (const type of EVENTS) el.addEventListener(type, schedule);
    schedule();
  }

  function isMedia(target) {
    return target instanceof HTMLMediaElement;
  }

  // Media events do not bubble, but they do pass `document` on capture.
  for (const type of EVENTS) {
    document.addEventListener(type, (event) => {
      if (isMedia(event.target)) track(event.target);
    }, true);
  }

  function scan() {
    for (const el of document.querySelectorAll("video, audio")) track(el);
  }

  function candidates() {
    for (const el of media) {
      // Gone from the page with nothing loaded: nothing to control.
      if (!el.isConnected && !el.currentSrc) media.delete(el);
    }
    const all = [...media].filter((el) => el.currentSrc || el.srcObject);
    const playing = all.filter((el) => !el.paused && !el.ended);
    // Muted previews (YouTube's hover thumbnails) only when nothing else plays.
    const audible = playing.filter((el) => !el.muted);
    return { all, playing: audible.length ? audible : playing };
  }

  function state() {
    const { all, playing } = candidates();
    const pick = playing[0] || all[all.length - 1];
    return {
      type: "media",
      hasMedia: Boolean(pick),
      playing: playing.length > 0,
      rate: pick ? pick.playbackRate : null,
      // Seconds at `at` (ms epoch); hosts advance it by rate while playing.
      // Firefox's MPRIS reports neither position nor length for pages.
      position: pick ? pick.currentTime : null,
      duration: pick && Number.isFinite(pick.duration) ? pick.duration : null,
      at: Date.now(),
    };
  }

  function send(message) {
    // `at` changes every time; only the rest decides whether to send.
    const text = JSON.stringify({ ...message, at: 0 });
    if (text === lastSent) return;
    lastSent = text;
    browser.runtime.sendMessage(message).catch(() => {});
  }

  function schedule() {
    if (timer !== null) return;
    timer = setTimeout(() => {
      timer = null;
      send(state());
    }, 50);
  }

  function setRate(rate) {
    rate = Math.min(RATE_MAX, Math.max(RATE_MIN, rate));
    wanted = rate;
    const { all, playing } = candidates();
    for (const el of playing.length ? playing : all) {
      // The default too: loading the next item resets to it.
      el.defaultPlaybackRate = rate;
      el.playbackRate = rate;
    }
    schedule();
  }

  function seek(position) {
    const { all, playing } = candidates();
    const el = playing[0] || all[all.length - 1];
    if (!el) return;
    const end = Number.isFinite(el.duration) ? el.duration : Infinity;
    el.currentTime = Math.min(end, Math.max(0, position));
  }

  function patchPage() {
    // Firefox only: reach into the page's own objects.
    const page = window.wrappedJSObject;
    if (!page || typeof exportFunction !== "function") return;
    const proto = page.HTMLMediaElement.prototype;
    const native = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,
                                                   "playbackRate");
    const pagePlay = proto.play;
    proto.play = exportFunction(function () {
      track(this);
      // play() takes no arguments; this script's own `arguments` object is
      // not readable by the page ("Permission denied to access property").
      return pagePlay.call(this);
    }, page);
    Object.defineProperty(proto, "playbackRate", {
      configurable: true,
      enumerable: true,
      get: exportFunction(function () {
        return native.get.call(this);
      }, page),
      set: exportFunction(function (value) {
        native.set.call(this, wanted === null ? value : wanted);
      }, page),
    });
  }

  if (locks) {
    try {
      patchPage();
    } catch (e) {
      console.warn("motorized faders: cannot hook the page's media", e);
    }
  }

  browser.runtime.onMessage.addListener((message) => {
    if (message.type === "set_rate" && typeof message.rate === "number") {
      setRate(message.rate);
    } else if (message.type === "seek" && typeof message.position === "number") {
      seek(message.position);
    } else if (message.type === "report") {
      lastSent = "";
      schedule();
    }
  });

  window.addEventListener("pagehide", () => {
    send({ type: "media", hasMedia: false, playing: false, rate: null });
  });
  window.addEventListener("pageshow", () => {
    lastSent = "";
    schedule();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan);
  } else {
    scan();
  }
})();
