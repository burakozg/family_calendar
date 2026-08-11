# Caddy + the DuckDNS module.
#
# The stock caddy image can't do a DNS-01 challenge against DuckDNS — the DNS
# provider modules aren't compiled in — so build once with xcaddy. DNS-01 is the
# whole point: it proves domain ownership through a DNS TXT record, never through
# an inbound connection, so the NAS stays closed to the internet.
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/duckdns

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
