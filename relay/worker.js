// MusicRPC 中継サーバー (Cloudflare Workers + Durable Objects)
//
//   POST /np                 スマホが再生情報を送る          Authorization: Bearer <RELAY_TOKEN>
//   GET  /ws                 PC が WebSocket で受け取る      Authorization: Bearer <RELAY_TOKEN>
//   PUT  /art                ジャケット / サムネイル (JPEG) を置く  Authorization: Bearer <RELAY_TOKEN>
//   GET  /art/<hash>.jpg     置いた画像を返す (Discord が読みに来るので認証なし)
//
// 最後の再生情報だけを保持し、接続中の PC にそのまま転送する。
// 画像は内容のハッシュを名前にして、新しいものから MAX_ART 枚だけ残す。

const MAX_BODY = 8 * 1024;
const MAX_ART_BYTES = 512 * 1024;
const MAX_ART = 300;
const ART_PATH = /^\/art\/([0-9a-f]{32})\.jpg$/;

async function sha256hex(data) {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

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

    if (url.pathname === "/art" && request.method === "PUT") {
      const data = await request.arrayBuffer();
      if (data.byteLength === 0 || data.byteLength > MAX_ART_BYTES) {
        return new Response("bad size", { status: 413 });
      }
      const head = new Uint8Array(data, 0, 3);
      if (head[0] !== 0xff || head[1] !== 0xd8 || head[2] !== 0xff) {
        return new Response("jpeg only", { status: 415 });
      }
      const hash = (await sha256hex(data)).slice(0, 32);
      const index = (await this.ctx.storage.get("art-index")) || [];
      if (!index.includes(hash)) {
        await this.ctx.storage.put("art:" + hash, data);
        index.push(hash);
        while (index.length > MAX_ART) {
          await this.ctx.storage.delete("art:" + index.shift());
        }
        await this.ctx.storage.put("art-index", index);
      }
      return Response.json({ url: `${url.origin}/art/${hash}.jpg` });
    }

    const art = url.pathname.match(ART_PATH);
    if (art && request.method === "GET") {
      const data = await this.ctx.storage.get("art:" + art[1]);
      if (!data) {
        return new Response("not found", { status: 404 });
      }
      return new Response(data, {
        headers: { "Content-Type": "image/jpeg", "Cache-Control": "public, max-age=31536000, immutable" },
      });
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
    const stub = env.CHANNEL.get(env.CHANNEL.idFromName("default"));
    if (request.method === "GET" && ART_PATH.test(new URL(request.url).pathname)) {
      return stub.fetch(request);  // 画像は Discord が取りに来るので公開 (名前はハッシュなので推測できない)
    }
    const auth = request.headers.get("Authorization") || "";
    const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (!env.RELAY_TOKEN || !sameToken(token, env.RELAY_TOKEN)) {
      return new Response("unauthorized", { status: 401 });
    }
    return stub.fetch(request);
  },
};
