# MimiClaw Smart Store Handoff

Updated: 2026-07-23 (Asia/Shanghai)

## Goal

Build MimiClaw as a two-audience smart-store agent:

- Merchant: inventory, expiry pricing, and service-ticket closed loops.
- Customer: QR/PWA entry, shopping assistant, cart/order, and human service.

Every privileged operation must require confirmation, be idempotent, verify the result, and leave an audit record.

## Devices and Network

- Work7_20 store controller: `192.168.43.44`
- MimiClaw: intended static IP `192.168.43.100`
- Windows gateway PC: `192.168.43.146`
- MimiClaw serial port: `COM36`
- Customer WebSocket: `ws://192.168.43.100:18789/`
- Store alert ingress: `http://192.168.43.100:18791/alert`

## Completed

### MimiClaw (`firmware/mimiclaw`)

- Added `store_query` for store summary, products, alerts, trend, and weight.
- Added restock proposal/status/apply flow.
- Added expiry-pricing proposal/status/apply flow.
- Added service-ticket status/update flow.
- Added merchant confirmation prompts and post-action verification guidance.
- Added customer role enforcement for every WebSocket session.
- WebSocket customer sessions cannot use pricing, restocking, refund, admin, file, cron, or GPIO mutations.
- Restored alert HTTP server startup.
- Latest MimiClaw firmware compiled and flashed successfully to `COM36`.
- App partition had about 43% free after build.

Important: at the last boot check MimiClaw failed to join Wi-Fi and entered onboarding mode:

- AP: `MimiClaw-9B15`
- Portal: `http://192.168.4.1`

Reconnect it to the same Wi-Fi as Work7_20 before end-to-end testing.

### Work7_20 (`firmware/store-controller`)

- Customer page is available at `/customer` (redirects to `/`).
- Store QR page is available at `/customer/qr`.
- Fixed route ordering so `/customer/qr` shows the QR page instead of redirecting.
- Added PWA manifest, service-worker shell, and customer session endpoint.
- Customer assistant now prefers MimiClaw WebSocket and falls back to Work7_20 `AI_Ask` if unavailable.
- Removed merchant-only quick actions from the customer assistant.
- Added restock proposal/apply endpoints and service-ticket timing/state checks.

The user compiles and flashes the Arduino/Work7_20 project. Codex must only compile and flash MimiClaw unless explicitly redirected.

## Customer URLs

- App: `http://192.168.43.44/customer`
- QR page: `http://192.168.43.44/customer/qr`
- Manifest: `http://192.168.43.44/manifest.webmanifest`
- Customer session: `http://192.168.43.44/api/customer/session`
- Service ticket: `http://192.168.43.44/api/service-request`

## Domain and PWA Publishing

- Selected domain: `mimistore.icu`
- Registrar: Alibaba Cloud
- Order shown: CNY 8 for 12 months, domain only, no add-ons.
- Domain purchase/real-name review is pending and likely completes tomorrow.
- Do not enable auto-renewal until the renewal price is checked.

Cloudflare Tunnel client is installed on the Windows gateway:

- Version: `2026.7.2`
- Path: `C:\Program Files (x86)\cloudflared\cloudflared.exe`

No Cloudflare login, tunnel, DNS route, or public exposure has been created yet.

## Next Steps

1. Confirm Alibaba Cloud domain purchase and real-name verification completed.
2. Add `mimistore.icu` to Cloudflare Free and wait for `Active` status.
3. Change Alibaba Cloud Nameservers to the two Cloudflare Nameservers.
4. Run `cloudflared tunnel login` on the Windows gateway.
5. Create a named tunnel.
6. Route an app hostname to `http://192.168.43.44`.
7. Route a MimiClaw hostname to `http://192.168.43.100:18789` with WebSocket support.
8. Change customer JavaScript from local `ws://` to public `wss://`.
9. Add 192x192 and 512x512 PNG app icons to the manifest.
10. Add authentication/rate limiting before public launch.

## Customer Analytics Added

- Work7_20 aggregates privacy-preserving intent counts, hourly question counts, resolved questions, cart additions, and orders.
- Work7_20 endpoint: `POST /api/customer/analytics-event` (customer-side event collector).
- Merchant endpoint: `GET /api/admin/customer-analytics` with `X-MimiClaw-Key`.
- MimiClaw tool action: `store_query` with `action=customer_analytics`.
- Merchant prompt example: `客户问得最多的问题是什么？哪些需求没有被满足？`
- Work7_20 must be recompiled/flashed by the user after the source update. MimiClaw customer-analytics firmware was compiled and app-flashed to COM36.

## Security Rules

- Customer traffic must never expose merchant analytics or mutation tools.
- Merchant mutations require explicit confirmation.
- Do not expose Work7_20 admin endpoints directly to the public Internet.
- Public WebSocket needs an issued session token and rate limiting before production use.
- Keep all secrets in existing secret configuration files; do not copy them into this handoff.

## Evening Update

### Domain and Tunnel

- Domain: `mimistore.icu` is registered, real-name verified, and Cloudflare status is Active.
- Cloudflare Nameservers: `amit.ns.cloudflare.com`, `cloe.ns.cloudflare.com`.
- Named tunnel: `mimistore`, status Healthy, cloudflared version `2026.7.2`.
- Public hostname route is intended to use `www.mimistore.icu` -> `http://192.168.43.44`.
- A Cloudflare 502 was observed because the gateway could not reach `192.168.43.44` at that time. Verify the ESP32 web server and IP before testing externally.
- If a replacement ESP32 gets another IP, either configure it with static `192.168.43.44` (old board must be powered off) or update the Tunnel Service URL.

### PWA and APK

- Work7_20 customer page already contains the customer UI, manifest route, and service worker route.
- Added PWA icon, improved manifest metadata, and offline shell caching in `firmware/store-controller\WebServer.cpp`.
- Removed the large bottom ticker/scrolling banner from the main page in the same file.
- Android WebView project created at `firmware/mimiclaw\android-app`; it loads `<你的域名>/customer` and supports JavaScript, storage, WebView navigation, and HTTPS only.
- Android Studio is installed at `<本地 Android Studio 安装目录>`, but Android SDK is not yet installed and APK has not been built.

### Vision Kiosk

- `<本地工程目录>\Vision_Kiosk` now has `WeightSensor.h/.cpp`, HX711 initialization, and visual/weight fusion scaffolding for three demo products.
- Arduino HX711 library, pins, calibration factor, product labels, and Edge Impulse three-class model still need final adjustment by the user before compiling.
- Codex has not compiled or flashed the Arduino projects.

### Disk Cleanup

- Cleaned only user Temp and Arduino15 `staging`/`tmp` caches on C: drive, freeing approximately 4.7GB.
- Arduino board packages, libraries, and project files were preserved.
