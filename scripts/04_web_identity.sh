#!/usr/bin/env bash

wm_generate_web_identity() {
  wm_info "Generating Web Identity site"
  mkdir -p "$WM_SITE_DIR/assets"
  local logo_letters
  logo_letters="$(echo "$WEB_IDENTITY_NAME" | awk '{print substr($1,1,1) substr($2,1,1)}' | tr '[:lower:]' '[:upper:]')"

  cat > "$WM_SITE_DIR/index.html" <<EOF
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${WEB_IDENTITY_NAME} — Digital Infrastructure</title>
  <meta name="description" content="${WEB_IDENTITY_NAME} provides secure digital infrastructure, cloud operations and managed technology services.">
  <link rel="stylesheet" href="/styles.css">
</head>
<body>
  <header class="header">
    <div class="brand"><span class="logo">${logo_letters}</span><span>${WEB_IDENTITY_NAME}</span></div>
    <nav><a href="#services">Services</a><a href="#infrastructure">Infrastructure</a><a href="#security">Security</a><a href="#contacts">Contacts</a></nav>
  </header>
  <main>
    <section class="hero">
      <div><p class="eyebrow">Cloud operations · Managed systems · Digital continuity</p><h1>Reliable infrastructure for distributed teams.</h1><p>We design, maintain and monitor resilient digital environments for modern businesses that depend on always-on connectivity and secure operations.</p><a class="button" href="#contacts">Contact us</a></div>
      <div class="card"><h2>Operational focus</h2><ul><li>Managed cloud environments</li><li>Secure remote operations</li><li>Infrastructure monitoring</li><li>Business continuity planning</li></ul></div>
    </section>
    <section id="services" class="grid"><article><h3>Cloud Engineering</h3><p>Architecture, deployment and lifecycle support for scalable business systems.</p></article><article><h3>Infrastructure Care</h3><p>Routine maintenance, observability and operational diagnostics.</p></article><article><h3>Secure Access</h3><p>Private connectivity patterns for teams, devices and internal resources.</p></article></section>
    <section id="infrastructure" class="split"><div><h2>Built for predictable operations</h2><p>Our work is centered around stable service delivery, clean documentation and measurable availability.</p></div><div class="metric"><strong>24/7</strong><span>monitoring-ready infrastructure</span></div></section>
    <section id="security" class="panel"><h2>Security by default</h2><p>We follow a conservative approach: minimal public surface, encrypted traffic, least-privilege access and regular configuration reviews.</p></section>
  </main>
  <footer id="contacts"><strong>${WEB_IDENTITY_NAME}</strong><span>contact@${DOMAIN}</span><span>© $(date +%Y)</span></footer>
</body>
</html>
EOF

  cat > "$WM_SITE_DIR/styles.css" <<'EOF'
:root{--bg:#0f172a;--panel:#111827;--text:#e5e7eb;--muted:#94a3b8;--brand:#38bdf8;--white:#fff}*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:linear-gradient(135deg,#020617,#0f172a 45%,#1e293b);color:var(--text)}a{color:inherit;text-decoration:none}.header{display:flex;justify-content:space-between;align-items:center;padding:28px 7vw}.brand{display:flex;align-items:center;gap:12px;font-weight:700}.logo{display:grid;place-items:center;width:42px;height:42px;border-radius:14px;background:var(--brand);color:#02111f}nav{display:flex;gap:24px;color:var(--muted)}.hero{display:grid;grid-template-columns:1.4fr .8fr;gap:48px;padding:86px 7vw 60px;align-items:center}.eyebrow{color:var(--brand);text-transform:uppercase;letter-spacing:.12em;font-size:12px}h1{font-size:clamp(42px,7vw,76px);line-height:.95;margin:12px 0 24px}h2{font-size:32px;margin:0 0 18px}p{color:var(--muted);font-size:18px;line-height:1.65}.button{display:inline-flex;margin-top:18px;background:var(--white);color:#0f172a;padding:14px 20px;border-radius:999px;font-weight:700}.card,.panel,article,.metric{background:rgba(15,23,42,.72);border:1px solid rgba(148,163,184,.24);border-radius:28px;padding:32px;box-shadow:0 20px 70px rgba(0,0,0,.25)}li{margin:12px 0;color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;padding:30px 7vw}.split{display:grid;grid-template-columns:1fr .6fr;gap:24px;padding:30px 7vw}.metric strong{display:block;font-size:64px;color:var(--brand)}.metric span{color:var(--muted)}.panel{margin:30px 7vw 70px}footer{display:flex;gap:24px;justify-content:space-between;padding:28px 7vw;color:var(--muted);border-top:1px solid rgba(148,163,184,.2)}@media(max-width:850px){.hero,.grid,.split{grid-template-columns:1fr}.header,footer{flex-direction:column;align-items:flex-start}nav{flex-wrap:wrap}}
EOF
  wm_success "Web Identity site generated: $WM_SITE_DIR"
}
