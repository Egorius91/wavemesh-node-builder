#!/usr/bin/env bash

wm_generate_web_identity() {
  wm_info "Generating Web Identity cover site"
  mkdir -p "$WM_SITE_DIR"

  DOMAIN="$DOMAIN" WEB_IDENTITY_NAME="$WEB_IDENTITY_NAME" WM_SITE_DIR="$WM_SITE_DIR" WM_CONFIG_JSON="$WM_CONFIG_JSON" python3 - <<'PY'
import hashlib
import html
import json
import os
import random
import re
import shutil
from pathlib import Path
from urllib.parse import quote

domain = os.environ["DOMAIN"]
brand = os.environ["WEB_IDENTITY_NAME"]
site_dir = Path(os.environ["WM_SITE_DIR"])
config_path = Path(os.environ["WM_CONFIG_JSON"])
seed_text = f"{domain}|{brand}"
seed = int(hashlib.sha256(seed_text.encode()).hexdigest()[:12], 16)
rng = random.Random(seed)

niches = [
    {
        "key": "logistics",
        "tagline": "Operational clarity for moving teams",
        "description": "A practical operations studio helping regional companies plan routes, maintain vendor records and keep daily work visible.",
        "hero": "Everyday logistics made easier to review, schedule and improve.",
        "accent": "#2f6f73",
        "warm": "#f2b66d",
        "dark": "#18282a",
        "light": "#f6f0e8",
        "services": [
            ("Route planning reviews", "Weekly route checks, capacity notes and handover-ready documentation."),
            ("Supplier desk support", "Clean records for carriers, service partners and recurring operating tasks."),
            ("Operations reporting", "Simple dashboards and monthly notes that help teams spot delays early."),
        ],
        "about": "The team works with small transport, field-service and wholesale businesses that need tidy operating routines without a heavy enterprise system.",
        "keywords": ["dispatch boards", "warehouse notes", "regional routes"],
    },
    {
        "key": "architecture",
        "tagline": "Measured design for compact commercial spaces",
        "description": "A small studio focused on adaptive interiors, site notes and practical design packs for offices, clinics and hospitality rooms.",
        "hero": "Quiet commercial spaces with better flow, light and maintenance.",
        "accent": "#8b5e3c",
        "warm": "#d7a86e",
        "dark": "#29231f",
        "light": "#f5efe7",
        "services": [
            ("Concept layouts", "Measured plans and material direction for first-round decisions."),
            ("Fit-out coordination", "Notes for contractors, procurement lists and visit summaries."),
            ("Refresh packages", "Focused updates for receptions, meeting rooms and guest areas."),
        ],
        "about": "Projects are intentionally modest: useful rooms, durable finishes and documentation that helps owners make decisions quickly.",
        "keywords": ["site sketches", "material boards", "interior planning"],
    },
    {
        "key": "coffee",
        "tagline": "Small-batch coffee supply for offices and local counters",
        "description": "A low-key roastery partner offering seasonal blends, tasting notes and dependable monthly delivery for teams.",
        "hero": "Coffee programs that feel personal without becoming complicated.",
        "accent": "#6f4b35",
        "warm": "#c89b62",
        "dark": "#241b16",
        "light": "#f4ede4",
        "services": [
            ("Office coffee plans", "Flexible monthly quantities, grind guidance and simple reorder reminders."),
            ("Seasonal blends", "Small lots with readable tasting notes and practical brew recipes."),
            ("Counter training", "Short sessions for service teams using filter, batch brew and espresso."),
        ],
        "about": "The work is intentionally hands-on: fewer products, clearer notes and coffee that can be brewed consistently by busy teams.",
        "keywords": ["roast notes", "coffee counter", "office supply"],
    },
    {
        "key": "energy",
        "tagline": "Readable energy notes for property teams",
        "description": "An advisory practice that turns utility data, maintenance notes and efficiency ideas into practical next steps.",
        "hero": "Energy improvements explained in plain language and small steps.",
        "accent": "#37785c",
        "warm": "#f0c857",
        "dark": "#17251f",
        "light": "#eff6ef",
        "services": [
            ("Usage reviews", "Monthly summaries that translate meter data into useful operating observations."),
            ("Efficiency checklists", "Prioritized maintenance and upgrade lists for smaller properties."),
            ("Vendor comparisons", "Readable scopes for lighting, controls and equipment proposals."),
        ],
        "about": "The practice is built for owners who want lower waste and better records, but do not need a large consulting engagement.",
        "keywords": ["meter readings", "solar notes", "building efficiency"],
    },
    {
        "key": "legalops",
        "tagline": "Organized records for growing professional teams",
        "description": "A legal operations support desk for contract tracking, document routines and internal process notes.",
        "hero": "Less chasing, cleaner files and calmer weekly reviews.",
        "accent": "#475569",
        "warm": "#b9935a",
        "dark": "#18202b",
        "light": "#f4f1ea",
        "services": [
            ("Contract registers", "Renewal dates, owners, status notes and tidy reference folders."),
            ("Policy libraries", "Readable internal pages for recurring operating questions."),
            ("Process cleanups", "Small workflow fixes for approvals, filing and handover points."),
        ],
        "about": "The service sits between administration and counsel: practical enough for daily use, careful enough for regulated teams.",
        "keywords": ["document review", "records desk", "office process"],
    },
    {
        "key": "studio",
        "tagline": "Visual systems for useful local brands",
        "description": "A design studio creating identity systems, simple websites and campaign materials for service businesses.",
        "hero": "Design work that is polished, usable and easy to keep alive.",
        "accent": "#7c5cff",
        "warm": "#ffb86b",
        "dark": "#201c32",
        "light": "#f5f2ff",
        "services": [
            ("Identity refresh", "Logos, color systems and layout rules for consistent everyday use."),
            ("Campaign kits", "Launch pages, social tiles and printed pieces with one clear message."),
            ("Website care", "Small content updates, audits and page improvements after launch."),
        ],
        "about": "The studio keeps projects compact and useful: enough system to look consistent, without turning every update into a production.",
        "keywords": ["brand boards", "design desk", "campaign planning"],
    },
]

niche = niches[seed % len(niches)]
year = os.popen("date +%Y").read().strip() or "2026"
slug = re.sub(r"[^a-z0-9]+", "-", brand.lower()).strip("-") or "site"
email = f"hello@{domain}"
city_options = ["Copenhagen", "Tallinn", "Prague", "Lisbon", "Warsaw", "Vilnius", "Riga", "Helsinki"]
city = city_options[(seed // 7) % len(city_options)]
hours = rng.choice(["Mon-Fri 09:00-17:30", "Tue-Fri 10:00-18:00", "Mon-Thu 09:30-17:00", "Weekdays by appointment"])
street = rng.choice(["Harbor Lane", "Northline Yard", "Market Street", "Foundry Walk", "Station Road", "Cedar Court"])
address = f"{rng.randrange(12, 88)} {street}, {city}"

site_dir.mkdir(parents=True, exist_ok=True)
for child in site_dir.iterdir():
    if child.name in {".", ".."}:
        continue
    if child.is_dir():
        shutil.rmtree(child)
    else:
        child.unlink()

assets = site_dir / "assets"
img_dir = assets / "img"
img_dir.mkdir(parents=True, exist_ok=True)

pages = [
    ("index.html", "Home"),
    ("about.html", "About"),
    ("services.html", "Services"),
    ("contact.html", "Contact"),
]

def esc(value):
    return html.escape(str(value), quote=True)

def nav(active):
    items = []
    for href, label in pages:
        cls = ' class="active"' if href == active else ""
        items.append(f'<a{cls} href="{href}">{label}</a>')
    return "\n        ".join(items)

def logo_letters():
    parts = re.findall(r"[A-Za-z0-9]+", brand)
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return (brand[:2] or "WM").upper()

def head(title, description, page):
    og_img = "assets/img/hero.svg"
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} | {esc(brand)}</title>
  <meta name="description" content="{esc(description)}">
  <meta property="og:title" content="{esc(title)} | {esc(brand)}">
  <meta property="og:description" content="{esc(description)}">
  <meta property="og:type" content="website">
  <meta property="og:image" content="{og_img}">
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="assets/style.css">
</head>
<body data-page="{esc(page)}">
  <header class="site-header">
    <a class="brand" href="index.html" aria-label="{esc(brand)} home"><span>{esc(logo_letters())}</span><strong>{esc(brand)}</strong></a>
    <button class="nav-toggle" type="button" aria-label="Toggle navigation">Menu</button>
    <nav class="site-nav">
        {nav(page)}
    </nav>
  </header>
'''

def footer():
    return f'''  <footer class="site-footer">
    <div>
      <strong>{esc(brand)}</strong>
      <p>{esc(niche["tagline"])}</p>
    </div>
    <div>
      <span>{esc(address)}</span>
      <span>{esc(hours)}</span>
      <a href="mailto:{esc(email)}">{esc(email)}</a>
    </div>
    <div>
      <a href="#">LinkedIn</a>
      <a href="#">Notes</a>
      <span>Copyright {esc(year)}</span>
    </div>
  </footer>
  <script src="assets/site.js"></script>
</body>
</html>
'''

def page(filename, title, description, body):
    (site_dir / filename).write_text(head(title, description, filename) + body + footer(), encoding="utf-8")

service_cards = "\n".join(
    f'''      <article class="service-card">
        <span>{i:02d}</span>
        <h3>{esc(name)}</h3>
        <p>{esc(text)}</p>
      </article>'''
    for i, (name, text) in enumerate(niche["services"], 1)
)

keywords = niche["keywords"]
testimonial_names = rng.sample(["Marta", "Lukas", "Nadia", "Jonas", "Elena", "Victor", "Sofia", "Noah"], 3)
testimonials = [
    f"{brand} gave us a clearer weekly rhythm and materials we could actually keep using.",
    "The work felt practical from the first review. No theatre, just useful documents and decisions.",
    "Their notes made it easier for our team to hand over tasks without losing context.",
]

home_body = f'''  <main>
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">{esc(niche["tagline"])}</p>
        <h1>{esc(niche["hero"])}</h1>
        <p>{esc(niche["description"])}</p>
        <div class="hero-actions">
          <a class="button primary" href="services.html">View services</a>
          <a class="button ghost" href="contact.html">Start a conversation</a>
        </div>
      </div>
      <div class="hero-media">
        <img src="assets/img/hero.svg" alt="{esc(keywords[0])}">
      </div>
    </section>
    <section class="section intro">
      <div>
        <p class="eyebrow">Working style</p>
        <h2>Small teams need systems that are easy to understand on a busy Tuesday.</h2>
      </div>
      <p>We keep the work grounded: clear notes, short review loops and documents that can be opened by the next person without a handover meeting.</p>
    </section>
    <section class="service-grid">
{service_cards}
    </section>
    <section class="split-feature">
      <img src="assets/img/detail.svg" alt="{esc(keywords[1])}">
      <div>
        <p class="eyebrow">Recent note</p>
        <h2>A practical update cycle, not a permanent project.</h2>
        <p>{esc(niche["about"])}</p>
        <ul class="checks">
          <li>Documented scope before work starts</li>
          <li>Readable weekly progress notes</li>
          <li>Careful handover at the end</li>
        </ul>
      </div>
    </section>
  </main>
'''

about_body = f'''  <main>
    <section class="page-hero">
      <p class="eyebrow">About</p>
      <h1>A compact practice with a bias toward useful details.</h1>
      <p>{esc(niche["about"])}</p>
    </section>
    <section class="two-column">
      <div>
        <h2>How we work</h2>
        <p>Most engagements begin with a short audit, a shared list of priorities and a simple calendar. The goal is to leave teams with habits they can keep after the project ends.</p>
      </div>
      <div class="note-card">
        <strong>Founded locally, working remotely</strong>
        <p>Based around {esc(city)}, with project work organized for distributed teams and remote reviews.</p>
      </div>
    </section>
    <section class="quote-grid">
      {''.join(f'<blockquote><p>{esc(text)}</p><cite>{esc(name)}, client lead</cite></blockquote>' for name, text in zip(testimonial_names, testimonials))}
    </section>
  </main>
'''

services_body = f'''  <main>
    <section class="page-hero">
      <p class="eyebrow">Services</p>
      <h1>Focused work packages for teams that want fewer loose ends.</h1>
      <p>Every package is sized to produce decisions, documentation and a clean next step.</p>
    </section>
    <section class="service-list">
{service_cards}
    </section>
    <section class="process">
      <h2>Typical project rhythm</h2>
      <ol>
        <li><strong>Listen:</strong> map the current situation, owners and recurring friction.</li>
        <li><strong>Draft:</strong> create a small first version that the team can react to.</li>
        <li><strong>Refine:</strong> tighten documents, handover notes and next actions.</li>
      </ol>
    </section>
  </main>
'''

contact_body = f'''  <main>
    <section class="page-hero">
      <p class="eyebrow">Contact</p>
      <h1>Tell us what needs to become clearer.</h1>
      <p>Send a short note about the team, the current situation and what you would like to improve.</p>
    </section>
    <section class="contact-panel">
      <div>
        <h2>Contact details</h2>
        <p><strong>Email:</strong> <a href="mailto:{esc(email)}">{esc(email)}</a></p>
        <p><strong>Address:</strong> {esc(address)}</p>
        <p><strong>Hours:</strong> {esc(hours)}</p>
      </div>
      <form class="contact-form" action="#" method="post">
        <label>Name <input name="name" autocomplete="name"></label>
        <label>Email <input name="email" type="email" autocomplete="email"></label>
        <label>Message <textarea name="message" rows="5"></textarea></label>
        <button class="button primary" type="submit">Send note</button>
        <p class="form-status" aria-live="polite"></p>
      </form>
    </section>
  </main>
'''

page("index.html", "Home", niche["description"], home_body)
page("about.html", "About", f"About {brand}.", about_body)
page("services.html", "Services", f"Services from {brand}.", services_body)
page("contact.html", "Contact", f"Contact {brand}.", contact_body)

css = f''':root {{
  --accent: {niche["accent"]};
  --warm: {niche["warm"]};
  --dark: {niche["dark"]};
  --light: {niche["light"]};
  --ink: #1f2933;
  --muted: #68727d;
  --line: rgba(31,41,51,.14);
  --white: #fff;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--light); }}
a {{ color: inherit; }}
.site-header {{ display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 22px min(6vw, 72px); border-bottom: 1px solid var(--line); background: rgba(255,255,255,.82); backdrop-filter: blur(16px); position: sticky; top: 0; z-index: 10; }}
.brand {{ display: inline-flex; align-items: center; gap: 12px; text-decoration: none; }}
.brand span {{ width: 42px; height: 42px; display: grid; place-items: center; border-radius: 10px; color: var(--white); background: var(--accent); font-weight: 800; }}
.brand strong {{ letter-spacing: .02em; }}
.site-nav {{ display: flex; gap: 22px; color: var(--muted); }}
.site-nav a {{ text-decoration: none; font-size: 15px; }}
.site-nav a.active {{ color: var(--accent); font-weight: 700; }}
.nav-toggle {{ display: none; border: 1px solid var(--line); background: var(--white); padding: 9px 12px; border-radius: 8px; }}
.hero {{ display: grid; grid-template-columns: minmax(0,1fr) minmax(320px,.8fr); gap: 56px; align-items: center; padding: 76px min(6vw, 72px) 54px; }}
.hero-copy h1, .page-hero h1 {{ margin: 8px 0 20px; font-size: clamp(42px, 6vw, 78px); line-height: .96; letter-spacing: 0; max-width: 980px; }}
.hero-copy p, .page-hero p, .intro p, .two-column p, .process li, .contact-panel p {{ color: var(--muted); font-size: 18px; line-height: 1.65; }}
.eyebrow {{ margin: 0 0 10px; text-transform: uppercase; letter-spacing: .14em; color: var(--accent); font-size: 12px; font-weight: 800; }}
.hero-actions {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 28px; }}
.button {{ display: inline-flex; align-items: center; justify-content: center; min-height: 44px; padding: 12px 18px; border-radius: 8px; text-decoration: none; border: 1px solid var(--line); font-weight: 700; }}
.button.primary {{ background: var(--accent); color: var(--white); border-color: var(--accent); }}
.button.ghost {{ background: rgba(255,255,255,.7); }}
.hero-media, .split-feature img {{ border-radius: 8px; overflow: hidden; box-shadow: 0 24px 80px rgba(20,24,30,.18); background: var(--white); }}
img {{ max-width: 100%; display: block; }}
.section, .page-hero, .two-column, .service-grid, .service-list, .split-feature, .quote-grid, .process, .contact-panel {{ padding-left: min(6vw, 72px); padding-right: min(6vw, 72px); }}
.intro {{ display: grid; grid-template-columns: .8fr 1fr; gap: 44px; padding-top: 34px; padding-bottom: 34px; }}
.intro h2, .split-feature h2, .process h2, .contact-panel h2, .two-column h2 {{ font-size: clamp(30px, 4vw, 48px); line-height: 1.04; margin: 0 0 16px; }}
.service-grid, .service-list {{ display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 18px; padding-top: 28px; padding-bottom: 42px; }}
.service-card, .note-card, blockquote, .process, .contact-panel {{ background: rgba(255,255,255,.74); border: 1px solid var(--line); border-radius: 8px; padding: 28px; }}
.service-card span {{ color: var(--warm); font-weight: 800; }}
.service-card h3 {{ margin: 16px 0 10px; font-size: 22px; }}
.service-card p, blockquote p {{ color: var(--muted); line-height: 1.6; }}
.split-feature {{ display: grid; grid-template-columns: .9fr 1fr; gap: 44px; align-items: center; padding-top: 34px; padding-bottom: 70px; }}
.checks {{ padding-left: 20px; color: var(--muted); line-height: 1.8; }}
.page-hero {{ padding-top: 64px; padding-bottom: 28px; }}
.two-column {{ display: grid; grid-template-columns: 1fr .7fr; gap: 26px; padding-top: 20px; padding-bottom: 36px; }}
.quote-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 18px; padding-top: 20px; padding-bottom: 62px; }}
blockquote {{ margin: 0; }}
cite {{ color: var(--accent); font-style: normal; font-weight: 700; }}
.process {{ margin: 18px min(6vw, 72px) 64px; }}
.process ol {{ margin: 0; padding-left: 22px; }}
.contact-panel {{ display: grid; grid-template-columns: .8fr 1fr; gap: 30px; margin: 18px min(6vw, 72px) 64px; padding: 28px; }}
.contact-form {{ display: grid; gap: 14px; }}
label {{ display: grid; gap: 6px; color: var(--muted); }}
input, textarea {{ width: 100%; border: 1px solid var(--line); border-radius: 8px; padding: 12px; font: inherit; background: var(--white); }}
.form-status {{ min-height: 24px; color: var(--accent); }}
.site-footer {{ display: grid; grid-template-columns: 1.1fr 1fr .8fr; gap: 28px; padding: 34px min(6vw, 72px); background: var(--dark); color: rgba(255,255,255,.82); }}
.site-footer p, .site-footer span, .site-footer a {{ display: block; margin: 6px 0; color: rgba(255,255,255,.68); text-decoration: none; }}
@media (max-width: 850px) {{
  .site-header {{ align-items: flex-start; flex-wrap: wrap; }}
  .nav-toggle {{ display: inline-flex; }}
  .site-nav {{ display: none; flex-basis: 100%; flex-direction: column; gap: 12px; }}
  .site-nav.open {{ display: flex; }}
  .hero, .intro, .split-feature, .two-column, .contact-panel, .site-footer {{ grid-template-columns: 1fr; }}
  .service-grid, .service-list, .quote-grid {{ grid-template-columns: 1fr; }}
  .hero {{ padding-top: 44px; }}
}}
'''
(assets / "style.css").write_text(css, encoding="utf-8")

js = '''document.querySelector(".nav-toggle")?.addEventListener("click", () => {
  document.querySelector(".site-nav")?.classList.toggle("open");
});
document.querySelector(".contact-form")?.addEventListener("submit", event => {
  event.preventDefault();
  const status = event.currentTarget.querySelector(".form-status");
  if (status) status.textContent = "Thanks. Your note has been prepared for review.";
});
'''
(assets / "site.js").write_text(js, encoding="utf-8")

def svg(path, title, bg, fg, label):
    safe_title = esc(title)
    safe_label = esc(label)
    content = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800" role="img" aria-labelledby="title">
  <title id="title">{safe_title}</title>
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{bg}"/>
      <stop offset="1" stop-color="{fg}"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="800" fill="url(#g)"/>
  <circle cx="960" cy="130" r="170" fill="rgba(255,255,255,.18)"/>
  <rect x="110" y="110" width="470" height="300" rx="18" fill="rgba(255,255,255,.78)"/>
  <rect x="150" y="160" width="310" height="20" rx="10" fill="{bg}"/>
  <rect x="150" y="215" width="380" height="16" rx="8" fill="rgba(31,41,51,.30)"/>
  <rect x="150" y="260" width="250" height="16" rx="8" fill="rgba(31,41,51,.22)"/>
  <path d="M720 520 C800 390 930 390 1010 520 L1070 620 H660 Z" fill="rgba(255,255,255,.56)"/>
  <path d="M120 650 C260 590 360 700 500 630 C680 540 770 680 930 620 C1040 575 1110 620 1160 650 L1160 800 L120 800 Z" fill="rgba(255,255,255,.28)"/>
  <text x="110" y="520" fill="rgba(255,255,255,.92)" font-family="Arial, sans-serif" font-size="54" font-weight="700">{safe_label}</text>
</svg>'''
    (img_dir / path).write_text(content, encoding="utf-8")

svg("hero.svg", f"{brand} workspace", niche["accent"], niche["warm"], keywords[0].title())
svg("detail.svg", f"{brand} detail", niche["dark"], niche["accent"], keywords[1].title())

favicon = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="{niche["accent"]}"/>
  <text x="32" y="39" text-anchor="middle" font-family="Arial, sans-serif" font-size="22" font-weight="800" fill="#fff">{esc(logo_letters())}</text>
</svg>'''
(site_dir / "favicon.svg").write_text(favicon, encoding="utf-8")
(site_dir / "robots.txt").write_text("User-agent: *\nAllow: /\nSitemap: https://%s/sitemap.xml\n" % domain, encoding="utf-8")
sitemap_urls = "\n".join(f"  <url><loc>https://{domain}/{quote(href)}</loc></url>" for href, _ in pages)
(site_dir / "sitemap.xml").write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{sitemap_urls}
</urlset>
''', encoding="utf-8")

if config_path.exists():
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg.setdefault("web_identity", {})
    cfg["web_identity"].update({
        "company_name": brand,
        "theme": niche["key"],
        "site_path": str(site_dir),
        "pages": [href for href, _ in pages],
        "marker": brand,
    })
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY

  wm_validate_web_identity_site
  wm_success "Web Identity cover site generated: $WM_SITE_DIR"
}

wm_validate_web_identity_site() {
  local required=(
    "$WM_SITE_DIR/index.html"
    "$WM_SITE_DIR/about.html"
    "$WM_SITE_DIR/services.html"
    "$WM_SITE_DIR/contact.html"
    "$WM_SITE_DIR/assets/style.css"
    "$WM_SITE_DIR/assets/site.js"
    "$WM_SITE_DIR/assets/img/hero.svg"
    "$WM_SITE_DIR/assets/img/detail.svg"
    "$WM_SITE_DIR/favicon.svg"
    "$WM_SITE_DIR/robots.txt"
    "$WM_SITE_DIR/sitemap.xml"
  )
  local file
  for file in "${required[@]}"; do
    [[ -f "$file" ]] || wm_fail "Web Identity generation missing file: $file"
  done
  if grep -R -E '(src|href)="(/assets|https://[^"]+\.(css|js|jpg|jpeg|png|webp|svg))' "$WM_SITE_DIR"/*.html >/dev/null 2>&1; then
    wm_fail "Web Identity contains non-portable asset references"
  fi
}
