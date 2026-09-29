// استبدل هذا بالرابط الذي يمنحك إياه cloudflared في Termux
const TUNNEL_DOMAIN = "drove-counts-meter-undertaken.trycloudflare.com";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // توجيه الطلب داخلياً إلى النفق
    url.hostname = TUNNEL_DOMAIN;
    url.protocol = "https:";
    url.port = "443";

    const newHeaders = new Headers(request.headers);
    newHeaders.set("Host", TUNNEL_DOMAIN);

    const newRequest = new Request(url.toString(), {
      method: request.method,
      headers: newHeaders,
      body: request.body,
      redirect: "follow"
    });

    return fetch(newRequest);
  }
};

