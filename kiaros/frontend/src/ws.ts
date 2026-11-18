/**
 * WebSocket client for KIAROS server communication, with auto-reconnect.
 */

export type MessageHandler = (msg: Record<string, unknown>) => void;

export interface KiarosSocket {
  send(data: Record<string, unknown>): void;
  onMessage(handler: MessageHandler): void;
  onStatus(handler: (status: "connected" | "disconnected" | "unauthorized") => void): void;
  close(): void;
  reconnect(): void;
  isConnected(): boolean;
}

export function createSocket(urlFn: () => string, onOpen: (send: (d: Record<string, unknown>) => void) => void): KiarosSocket {
  let ws: WebSocket | null = null;
  const handlers: MessageHandler[] = [];
  const statusHandlers: ((s: "connected" | "disconnected" | "unauthorized") => void)[] = [];
  let reconnectDelay = 1000;
  let closed = false;
  let connected = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const emit = (s: "connected" | "disconnected" | "unauthorized") => statusHandlers.forEach((h) => h(s));
  const send = (data: Record<string, unknown>) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data));
  };

  function connect() {
    if (closed) return;
    let url: string;
    try {
      url = urlFn();
      ws = new WebSocket(url);
    } catch (e) {
      console.error("[ws] bad url", e);
      emit("disconnected");
      return;
    }

    ws.onopen = () => {
      connected = true;
      reconnectDelay = 1000;
      onOpen(send);
      emit("connected");
    };
    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        for (const h of handlers) h(msg);
      } catch {
        console.warn("[ws] bad message", event.data);
      }
    };
    ws.onclose = (event) => {
      connected = false;
      if (event.code === 4401) {
        emit("unauthorized");
        return; // wrong token — wait for the user to re-pair
      }
      emit("disconnected");
      if (!closed) {
        timer = setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 15000);
      }
    };
    ws.onerror = () => ws?.close();
  }

  connect();

  // Phones suspend sockets in the background; reconnect as soon as we're visible again.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !connected && !closed) {
      if (timer) clearTimeout(timer);
      reconnectDelay = 1000;
      connect();
    }
  });

  return {
    send,
    onMessage(handler) { handlers.push(handler); },
    onStatus(handler) { statusHandlers.push(handler); },
    close() { closed = true; ws?.close(); },
    reconnect() {
      if (timer) clearTimeout(timer);
      closed = false;
      reconnectDelay = 1000;
      if (ws) {
        ws.onclose = null; // don't let the old socket schedule its own reconnect
        try { ws.close(); } catch { /* ignore */ }
      }
      connect();
    },
    isConnected: () => connected,
  };
}
