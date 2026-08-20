#!/bin/bash
# Workaround for an SD-Core 4.1.0 chart bug that makes `helm upgrade` fail with
#   buildCustomCert: unable to decode base64 certificate
#
# templates/_helpers.tpl reads the shared CA secret and does:
#     $caCrt = index .data "ca.crt" | b64dec        -> raw PEM
#     $ca    = buildCustomCert $caCrt $caKey        -> sprig wants BASE64 PEM
# so the existing-CA path always fails. On a fresh install there is no secret,
# the chart calls genCA instead, and it works -- which is why only upgrades break.
#
# Fix: store the secret double-encoded, so the chart's b64dec yields base64 PEM.
# The chart rewrites it single-encoded after a successful upgrade, so run this
# before EVERY `helm upgrade` / `make 5gc-core-install`.
#
# Usage: tools/fix-ca-secret.sh <namespace> [secret-name]
set -euo pipefail
NS="${1:?usage: fix-ca-secret.sh <namespace> [secret]}"
SEC="${2:-5g-control-plane-ca-private}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

kubectl -n "$NS" get secret "$SEC" -o json | python3 -c "
import json,sys,base64
s=json.load(sys.stdin); d=s['data']
if base64.b64decode(d['ca.crt']).decode(errors='ignore').startswith('-----BEGIN'):
    out={k: base64.b64encode(d[k].encode()).decode() for k in ('ca.crt','ca.key')}
    print(json.dumps({'apiVersion':'v1','kind':'Secret','type':'Opaque',
      'metadata':{'name':'$SEC','namespace':'$NS'},'data':out}))
else:
    sys.stderr.write('[$NS/$SEC] already double-encoded, nothing to do\n'); sys.exit(3)
" | kubectl apply -f - && echo "[$NS/$SEC] double-encoded for upgrade" || true
