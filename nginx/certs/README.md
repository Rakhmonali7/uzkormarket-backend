# TLS certificates

Production Nginx (`docker-compose.prod.yml`) expects these files to exist in
this directory before bringing the stack up:

- `fullchain.pem` — server cert + intermediate chain
- `privkey.pem`   — private key (mode 0600)

## Let's Encrypt (recommended)

```bash
sudo certbot certonly --standalone -d api.koruzmall.com
sudo cp /etc/letsencrypt/live/api.koruzmall.com/fullchain.pem ./fullchain.pem
sudo cp /etc/letsencrypt/live/api.koruzmall.com/privkey.pem   ./privkey.pem
sudo chown $USER:$USER fullchain.pem privkey.pem
sudo chmod 600 privkey.pem
docker compose -f docker-compose.prod.yml exec nginx nginx -s reload
```

## Local self-signed (only if you really need TLS in dev)

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout privkey.pem -out fullchain.pem \
    -subj "/CN=localhost"
```

Both files are git-ignored; only this README and `.gitkeep` are tracked.
