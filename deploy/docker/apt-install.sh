#!/bin/sh
# apt-get install for image builds: HTTPS package sources, an optional extra CA
# (BuildKit secret "ca", for TLS-intercepting proxies) and no leftover lists.
set -eu
sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
if [ -s /run/secrets/ca ]; then
  echo 'Acquire::https::CaInfo "/run/secrets/ca";' > /etc/apt/apt.conf.d/99build-ca
fi
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99build-ca
