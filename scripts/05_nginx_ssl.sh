#!/usr/bin/env bash

wm_configure_nginx_http() {
  wm_info "Configuring temporary nginx HTTP site"
  mkdir -p "$WM_CERTBOT_DIR"
  cat > /etc/nginx/sites-available/wavemesh-node.conf <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root ${WM_CERTBOT_DIR};
    }

    location / {
        root ${WM_SITE_DIR};
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }
}
EOF
  ln -sf /etc/nginx/sites-available/wavemesh-node.conf /etc/nginx/sites-enabled/wavemesh-node.conf
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl enable --now nginx
  systemctl reload nginx
  wm_success "Temporary HTTP site ready"
}

wm_obtain_ssl() {
  wm_info "Obtaining Let's Encrypt certificate for ${DOMAIN}"
  local email_args=()
  if [[ -n "${EMAIL}" ]]; then
    email_args=(--email "$EMAIL")
  else
    email_args=(--register-unsafely-without-email)
  fi
  if certbot certonly --webroot -w "$WM_CERTBOT_DIR" -d "$DOMAIN" --agree-tos --non-interactive "${email_args[@]}"; then
    wm_success "SSL certificate obtained"
  else
    wm_check_provider_ports_hint
    wm_fail "SSL certificate request failed"
  fi
}

wm_configure_nginx_https() {
  wm_info "Configuring nginx HTTPS reverse proxy"
  local panel_path_no_slash renderer
  panel_path_no_slash="${PANEL_PATH%/}"
  renderer="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/nginx_renderer.py"
  python3 "$renderer" --config "$WM_CONFIG_JSON" --output /etc/nginx/wavemesh-managed-locations.conf
  chmod 0644 /etc/nginx/wavemesh-managed-locations.conf
  cat > /etc/nginx/sites-available/wavemesh-node.conf <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root ${WM_CERTBOT_DIR};
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    root ${WM_SITE_DIR};
    index index.html;

    include /etc/nginx/wavemesh-managed-locations.conf;

    location = ${panel_path_no_slash} {
        return 301 https://\$host${PANEL_PATH};
    }

    location ${PANEL_PATH} {
        proxy_pass http://127.0.0.1:${PANEL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port 443;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_redirect off;
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    location ${XHTTP_PATH} {
        proxy_pass http://127.0.0.1:${XHTTP_LOCAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port 443;
        proxy_redirect off;
        proxy_connect_timeout 10s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
        send_timeout 300s;
        proxy_buffering off;
        proxy_request_buffering off;
    }

}
EOF
  nginx -t
  systemctl reload nginx
  wm_success "nginx HTTPS config ready"
}
