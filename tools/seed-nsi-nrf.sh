#!/bin/bash
# Seed an NSI's NRF with the shared NF profiles (UDM, UDR, AUSF, PCF, NSSF, AMF).
#
# WHY: SD-Core has no NRF federation/hierarchy. Each NF registers into exactly
# one NRF (its `nrfUri`). So a second NRF belonging to a Network Slice Instance
# knows only the NFs that registered with it -- typically just that NSI's SMF.
# The SMF then fails PDU session creation with:
#     PDUSessionSMContextCreate, send NF Discovery Serving UDM Error
#         [UDM discovery returned no NF instances]
#
# This copies the shared NF profiles from the primary NRF's database into the
# NSI NRF's database, rewriting bare service hostnames to be namespace-qualified
# (`udm` -> `udm.aether-5gc`) because bare names do not resolve across
# namespaces. Cert SANs already include `<name>.<namespace>`, so TLS still
# validates.
#
# Usage: tools/seed-nsi-nrf.sh [src-db] [dst-db] [shared-ns] [mongo-pod] [mongo-ns]
set -euo pipefail
SRC_DB="${1:-aether}"
DST_DB="${2:-aether_nsi2}"
SHARED_NS="${3:-aether-5gc}"
MONGO_POD="${4:-mongodb-0}"
MONGO_NS="${5:-aether-5gc}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

kubectl -n "$MONGO_NS" exec "$MONGO_POD" -- mongosh --quiet --eval "
  var src = db.getSiblingDB('$SRC_DB').NfProfile;
  var dst = db.getSiblingDB('$DST_DB').NfProfile;
  // keep this NSI's own SMF, replace everything else
  dst.deleteMany({nftype:{\$ne:'SMF'}});
  var hosts = ['udm','udr','ausf','pcf','nssf','amf','nrf','webui'];
  var n = 0;
  src.find({nftype:{\$ne:'SMF'}}).forEach(function(p){
      delete p._id;
      var s = JSON.stringify(p);
      hosts.forEach(function(h){
          s = s.split('\"' + h + '\"').join('\"' + h + '.$SHARED_NS\"');
          s = s.split('://' + h + ':').join('://' + h + '.$SHARED_NS:');
      });
      dst.insertOne(JSON.parse(s)); n++;
  });
  print('seeded ' + n + ' shared NF profiles into $DST_DB');
  dst.find({},{nftype:1,ipv4addresses:1,_id:0}).forEach(x=>print('   ' + JSON.stringify(x)));
"
