import http from "node:http";
import next from "next";

const args = process.argv.slice(2);
const valueAfter = (name, fallback) => {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const dev = args.includes("--dev");
const hostname = valueAfter("--hostname", "127.0.0.1");
const port = Number(valueAfter("--port", "3000"));
const apiOrigin = process.env.PAPERMINE_API_ORIGIN || "http://127.0.0.1:8000";

const app = next({ dev, hostname, port });
const handle = app.getRequestHandler();
await app.prepare();

const server = http.createServer((req, res) => {
  if (req.url === "/api" || req.url?.startsWith("/api/")) {
    const target = new URL(req.url.slice(4) || "/", apiOrigin);
    const headers = { ...req.headers, host: target.host };
    delete headers.connection;
    const proxy = http.request(target, { method: req.method, headers }, (upstream) => {
      res.writeHead(upstream.statusCode || 502, upstream.headers);
      upstream.pipe(res);
    });
    proxy.on("error", (error) => {
      if (!res.headersSent) res.writeHead(502, { "content-type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ detail: `本地 API 服务不可用：${error.message}` }));
    });
    req.pipe(proxy);
    return;
  }
  void handle(req, res);
});

server.listen(port, hostname, () => {
  console.log(`PaperMine Web ready on http://${hostname}:${port} (API → ${apiOrigin})`);
});

const stop = () => server.close(() => process.exit(0));
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
