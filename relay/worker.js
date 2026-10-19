// MusicRPC 中継サーバー (Cloudflare Workers + Durable Objects)
//
//   POST /np   スマホが再生情報を送る        Authorization: Bearer <RELAY_TOKEN>
//   GET  /ws   PC が WebSocket で受け取る    Authorization: Bearer <RELAY_TOKEN>
//
// 最後の再生情報だけを保持し、接続中の PC にそのまま転送する。

const MAX_BODY = 8 * 1024;

export class Channel {
  constructor(ctx, env) {
    this.ctx = ctx;
  }

  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === "/ws") {
      if (request.headers.get("Upgrade") !== "websocket") {
        return new Response("expected websocket", { status: 426 });
      }
      const [client, server] = Object.values(new WebSocketPair());
      // Hibernation API: 待機中は課金されない
      this.ctx.acceptWebSocket(server);
      const last = await this.ctx.storage.get("last");
      if (last) {
        server.send(JSON.stringify({ ...last, age_ms: Date.now() - last.received_at }));
      }
      return new Response(null, { status: 101, webSocket: client });
    }

    if (url.pathname === "/np" && request.method === "POST") {
      const text = await request.text();
      if (text.length > MAX_BODY) {
        return new Response("too large", { status: 413 });
      }
      let body;
      try {
        body = JSON.parse(text);
      } catch {
        return new Response("bad json", { status: 400 });
      }
      const message = { ...body, received_at: Date.now() };
      await this.ctx.storage.put("last", message);
      const payload = JSON.stringify({ ...message, age_ms: 0 });
      for (const ws of this.ctx.getWebSockets()) {
        try {
          ws.send(payload);
        } catch {
          // 切断済みの接続は無視
        }
      }
      return new Response("ok");
    }

    return new Response("not found", { status: 404 });
  }

  webSocketMessage(ws, message) {
    if (message === "ping") {
      ws.send("pong");
    }
  }

  webSocketClose(ws, code) {
    ws.close(code, "closing");
  }
}

function sameToken(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const auth = request.headers.get("Authorization") || "";
    const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (!env.RELAY_TOKEN || !sameToken(token, env.RELAY_TOKEN)) {
      return new Response("unauthorized", { status: 401 });
    }
    const stub = env.CHANNEL.get(env.CHANNEL.idFromName("default"));
    return stub.fetch(request);
  },
};
