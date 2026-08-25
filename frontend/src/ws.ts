export type WsEvent = {
  type: string;
  channel?: string;
  event_type?: string;
  sequence?: number;
  last_sequence?: number;
  events?: WsEvent[];
  payload?: Record<string, unknown>;
  [k: string]: unknown;
};

type Handler = (event: WsEvent) => void;

const handlers = new Map<string, Set<Handler>>();
let ws: WebSocket | null = null;
let subscribed = new Set<string>();
let lastSeq = new Map<string, number>();
let retryTimer: ReturnType<typeof setTimeout> | null = null;

export function connectWs() {
  if (ws && ws.readyState <= WebSocket.OPEN) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    if (subscribed.size > 0) {
      ws!.send(JSON.stringify({ type: "SUBSCRIBE", channels: [...subscribed] }));
    }
  };

  ws.onmessage = (ev) => {
    try {
      const event = JSON.parse(ev.data) as WsEvent;
      if (event.type === "SNAPSHOT" && event.channel && typeof event.last_sequence === "number") {
        lastSeq.set(event.channel, event.last_sequence);
        for (const e of event.events ?? []) dispatch(event.channel!, e);
      } else if (event.type === "RESUMED" && event.channel) {
        for (const e of event.events ?? []) dispatch(event.channel, e);
      } else if (event.channel) {
        if (typeof event.sequence === "number") lastSeq.set(event.channel, event.sequence);
        dispatch(event.channel, event);
      }
    } catch {
      /* ignore malformed */
    }
  };

  ws.onclose = () => {
    ws = null;
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = setTimeout(() => connectWs(), 3000);
  };
  ws.onerror = () => ws?.close();
}

function dispatch(channel: string, event: WsEvent) {
  handlers.get(channel)?.forEach((h) => h(event));
}

export function subscribe(channel: string, handler: Handler): () => void {
  const set = handlers.get(channel) ?? new Set<Handler>();
  set.add(handler);
  handlers.set(channel, set);

  if (!subscribed.has(channel)) {
    subscribed.add(channel);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "SUBSCRIBE", channels: [channel] }));
    } else {
      connectWs();
    }
  } else if (ws && ws.readyState === WebSocket.OPEN) {
    const last = lastSeq.get(channel) ?? 0;
    ws.send(JSON.stringify({ type: "RESUME", channel, last_sequence: last }));
  }
  return () => {
    handlers.get(channel)?.delete(handler);
  };
}
