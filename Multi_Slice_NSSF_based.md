# NSSF-Based Multi-Slice SD-Core (Network Slice Instances)

Deploying SD-Core so that **the NSSF actually selects the slice** — each slice
backed by its own Network Slice Instance (NSI) with a **separate NRF and SMF**,
not just a separate UPF.

Every error in this guide was hit for real during the first working deployment,
and each one is documented with its root cause and fix. Several are chart or
code bugs, not misconfiguration.

> **Prerequisite.** This guide assumes you already have the *user-plane* multi-slice
> deployment from [MULTI-SLICE.md](MULTI-SLICE.md) running (2 slices, 2 UPFs,
> shared control plane). This guide adds control-plane separation on top of it.

---

## Contents

1. [Why bother — what NSSF-based slicing changes](#1-why-bother)
2. [Target architecture](#2-target-architecture)
3. [Step 1 — namespace and shared CA](#step-1--namespace-and-shared-ca)
4. [Step 2 — deploy NSI-2 (NRF-2 + SMF-2)](#step-2--deploy-nsi-2-nrf-2--smf-2)
5. [Step 3 — seed NRF-2 with the shared NFs](#step-3--seed-nrf-2-with-the-shared-nfs)
6. [Step 4 — point the NSSF at NRF-2](#step-4--point-the-nssf-at-nrf-2)
7. [Step 5 — disable AMF NRF caching](#step-5--disable-amf-nrf-caching)
8. [Step 6 — verify](#step-6--verify)
9. [Step 7 — the isolation demo](#step-7--the-isolation-demo)
10. [Errors encountered, with fixes](#errors-encountered-with-fixes)
11. [Known limitations of this setup](#known-limitations-of-this-setup)
12. [Scaling to a third NSI](#scaling-to-a-third-nsi)
13. [Teardown](#teardown)

---

## 1. Why bother

In the plain multi-slice deployment the **SMF** decides everything — it looks up
`(DNN, S-NSSAI)` and picks a UPF. The NSSF is called once per PDU session and its
answer is discarded. You get *user-plane* isolation only.

With NSIs, the **NSSF** decides which *copy of the control plane* serves a slice:

| | User-plane slicing | NSSF-based NSI slicing |
|---|---|---|
| Who selects | SMF (`selectMatchUPF`) | NSSF → picks NRF → that NRF picks SMF |
| NSSF | called, ignored | **load-bearing** |
| Isolation | UPF only | SMF + NRF + UPF |
| SMF crash | all slices down | **only that slice down** |
| Cost | 1 extra pod per slice | ~2 extra pods per slice |

The mechanism is already implemented in SD-Core's AMF —
`consumer/sm_context.go` parses the NSSF's `nsiInformation.nrfId` and
**overwrites the NRF it uses for SMF discovery**:

```go
} else {
    smContext.SetNsInstance(nsiInformation.GetNsiId())
    nrfApiUri, err := url.Parse(nsiInformation.NrfId)
    ...
    nrfUri = fmt.Sprintf("%s://%s", nrfApiUri.Scheme, nrfApiUri.Host)   // ← overwritten
}
...
result, err := SendSearchNFInstances(ctx, nrfUri, models.NFTYPE_SMF, ...)
```

It is dormant only because the shipped `nsiList` points back at the same NRF.

---

## 2. Target architecture

```
                    ┌──────────── shared (NSI-agnostic) ────────────┐
 gNB ──N2──►  AMF        NSSF      UDM  UDR  AUSF  PCF  webconsole  mongo
               │          ▲                                    [aether-5gc]
               │  (1) "which NSI for sst=2/sd=010205?"
               └──────────┘
               │  (2) {nrfId: https://nrf.aether-nsi2:29510, nsiId: 2}
               │
               │  (3) discover SMF from THAT NRF
        ┌──────┴──────────────────────────┐
        ▼                                 ▼
  ┌─ NSI-1 ─────────────┐          ┌─ NSI-2 ─────────────────┐
  │ NRF-1  [aether-5gc] │          │ NRF-2  [aether-nsi2]    │
  │ SMF-1 ──N4──► UPF-1 │          │ SMF-2 ──N4──► UPF-2     │
  │        [aether-5gc] │          │        [aether-upf-1]   │
  └─────────────────────┘          └─────────────────────────┘
```

**Shared** (one copy, serves every slice): AMF, NSSF, UDM, UDR, AUSF, PCF,
webconsole, MongoDB, Kafka. The AMF is shared by 3GPP design — one AMF serves a
UE across all of its slices.

**Per-NSI**: NRF, SMF, UPF.

Final layout — 19 pods across three namespaces:

| Namespace | Contents |
|---|---|
| `aether-5gc` | full control plane + UPF-1 (16 pods) |
| `aether-nsi2` | NRF-2 + SMF-2 (2 pods) |
| `aether-upf-1` | UPF-2 (1 pod) |

---

## Step 1 — namespace and shared CA

The AMF in `aether-5gc` will make HTTPS calls to SMF-2 in `aether-nsi2`. The chart
generates a **per-release CA**, so NSI-2's leaf certs would be signed by a CA the
AMF does not trust. Share the CA.

```bash
export KUBECONFIG=$HOME/.kube/config
kubectl create namespace aether-nsi2
```

Copy the CA secret — **double-encoded**, see
[Error 1](#error-1--buildcustomcert-unable-to-decode-base64-certificate):

```bash
kubectl -n aether-5gc get secret 5g-control-plane-ca-private -o json | python3 -c "
import json,sys,base64
s=json.load(sys.stdin); d=s['data']
out={k: base64.b64encode(d[k].encode()).decode() for k in ('ca.crt','ca.key')}
print(json.dumps({'apiVersion':'v1','kind':'Secret','type':'Opaque',
  'metadata':{'name':'5g-control-plane-ca-private','namespace':'aether-nsi2'},
  'data':out}))" | kubectl apply -f -
```

Verify — decoding **twice** must yield PEM:

```bash
kubectl -n aether-nsi2 get secret 5g-control-plane-ca-private \
  -o jsonpath='{.data.ca\.crt}' | base64 -d | base64 -d | head -1
# -----BEGIN CERTIFICATE-----
```

Cert SANs are `<name>`, `<name>.<namespace>`, `<name>.<namespace>.svc`,
`<name>.<namespace>.svc.cluster.local`, so `smf.aether-nsi2` validates.

---

## Step 2 — deploy NSI-2 (NRF-2 + SMF-2)

Every NF in the chart has an independent `config.<nf>.deploy` flag, so NSI-2
needs only 2 pods rather than 16.

Values file: [deps/5gc/roles/core/templates/nsi2-values.yaml](deps/5gc/roles/core/templates/nsi2-values.yaml).
The parts that matter:

```yaml
5g-control-plane:
  enable5G: true
  kafka:   {deploy: false}      # reuse kafka in aether-5gc
  mongodb: {deploy: false}      # reuse mongodb in aether-5gc

  config:
    certs:
      sharedCA:
        existingPrivateSecret: "5g-control-plane-ca-private"

    # SEPARATE database -- see Error 2
    mongodb:
      name: aether_nsi2
      url: mongodb://mongodb-headless.aether-5gc:27017/?replicaSet=rs0
      authKeysDbName: authentication
      authUrl: mongodb://mongodb-headless.aether-5gc:27017/?replicaSet=rs0

    # everything except NRF and SMF is off
    amf:        {deploy: false}
    ausf:       {deploy: false}
    pcf:        {deploy: false}
    nssf:       {deploy: false}
    udm:        {deploy: false}
    udr:        {deploy: false}
    webui:      {deploy: false}
    sctplb:     {deploy: false}
    upfadapter: {deploy: false}
    metricfunc: {deploy: false}

    nrf:
      deploy: true
      cfgFiles:
        nrfcfg.yaml:
          configuration:
            nfProfileExpiryEnable: false        # see Error 4
            webuiUri: http://webui.aether-5gc:5001
            sbi:
              registerIPv4: nrf.aether-nsi2     # MUST be namespace-qualified

    smf:
      deploy: true
      cfgFiles:
        smfcfg.yaml:
          configuration:
            smfName: SMF-NSI2
            enableDBStore: true
            nrfUri: https://nrf.aether-nsi2:29510      # register into NRF-2
            webuiUri: http://webui.aether-5gc:5001
            kafkaInfo:
              brokerUri: kafka.aether-5gc
              brokerPort: 9092
            sbi:
              registerIPv4: smf.aether-nsi2            # see Error 3
```

Also set at the top of the file: `omec-sub-provision.enable: false`,
`omec-user-plane.enable: false`, `5g-ran-sim.enable: false`,
`omec-control-plane.enable4G: false`.

Deploy:

```bash
helm upgrade --install nsi2 oci://ghcr.io/omec-project/sd-core --version 4.1.0 \
  -n aether-nsi2 --values deps/5gc/roles/core/templates/nsi2-values.yaml \
  --wait --timeout 5m

kubectl -n aether-nsi2 get pods
#   nrf-...   1/1  Running
#   smf-...   1/1  Running
```

> `helm template` will **fail** here with "existing private CA Secret ... not found".
> That is expected — `helm template` runs client-side and its `lookup` of cluster
> secrets returns empty. Use `helm install`/`upgrade`, or `--dry-run=server`.

Confirm SMF-2 registered into NRF-2 with the right address:

```bash
kubectl -n aether-nsi2 port-forward svc/nrf 29510:29510 &
curl -sk "https://127.0.0.1:29510/nnrf-disc/v1/nf-instances?target-nf-type=SMF&requester-nf-type=AMF" \
  | python3 -m json.tool | grep -E "ipv4Addresses|apiPrefix"
# "ipv4Addresses": ["smf.aether-nsi2"]
# "apiPrefix": "https://smf.aether-nsi2:29502"
```

If it says `["smf"]`, stop — see [Error 3](#error-3--amf-discovers-nsi-2-but-dials-smf-1).

---

## Step 3 — seed NRF-2 with the shared NFs

SD-Core has **no NRF federation**. NRF-2 knows only what registered with it —
just SMF-2. SMF-2 will therefore fail every PDU session with
`UDM discovery returned no NF instances`.

Copy the shared NF profiles into NRF-2's database, rewriting bare hostnames so
they resolve from `aether-nsi2`:

```bash
./tools/seed-nsi-nrf.sh
# seeded 6 shared NF profiles into aether_nsi2
#    {"nftype":"SMF","ipv4addresses":["smf.aether-nsi2"]}
#    {"nftype":"PCF","ipv4addresses":["pcf.aether-5gc"]}
#    {"nftype":"UDM","ipv4addresses":["udm.aether-5gc"]}
#    ...
```

Signature: `tools/seed-nsi-nrf.sh [src-db] [dst-db] [shared-ns] [mongo-pod] [mongo-ns]`
(defaults `aether aether_nsi2 aether-5gc mongodb-0 aether-5gc`).

Re-run it whenever a shared NF re-registers with a new address (for example after
`make 5gc-core-install`).

---

## Step 4 — point the NSSF at NRF-2

Add an `nsiList` to the NSSF in
[deps/5gc/roles/core/templates/sdcore-5g-values.yaml](deps/5gc/roles/core/templates/sdcore-5g-values.yaml),
under `5g-control-plane.config`:

```yaml
    nssf:
      cfgFiles:
        nssfcfg.yaml:
          configuration:
            nsiList:
              - snssai:
                  sst: 1
                  sd: "010203"
                nsiInformationList:
                  - nrfId: https://nrf:29510/nnrf-nfm/v1/nf-instances
                    nsiId: "1"
              - snssai:
                  sst: 2
                  sd: "010205"
                nsiInformationList:
                  - nrfId: https://nrf.aether-nsi2:29510/nnrf-nfm/v1/nf-instances
                    nsiId: "2"
```

The AMF uses only `scheme://host` from `nrfId`; the path is cosmetic.

---

## Step 5 — disable AMF NRF caching

With more than one NRF in play the AMF's discovery cache does not appear to be
keyed by NRF URI, so a result fetched from NRF-2 can be served for a query that
should have gone to NRF-1. In the same values file:

```yaml
    amf:
      cfgFiles:
        amfcfg.yaml:
          configuration:
            enableNrfCaching: false
```

Apply both changes:

```bash
./tools/fix-ca-secret.sh aether-5gc      # REQUIRED before every upgrade -- Error 1
make 5gc-core-install
```

Expect `failed=0 rescued=0`. If you see `rescued=1`, the Helm step failed and
your changes were **not applied** — check the log.

The NSSF pod does not reload its ConfigMap automatically:

```bash
kubectl -n aether-5gc rollout restart deploy/nssf
kubectl -n aether-5gc rollout status  deploy/nssf
```

---

## Step 6 — verify

### 6a. Does the NSSF return different NRFs?

```bash
kubectl -n aether-5gc port-forward svc/nssf 29531:29531 &
B="https://127.0.0.1:29531/nnssf-nsselection/v2/network-slice-information"
P="nf-type=AMF&nf-id=t&slice-info-request-for-pdu-session%5BroamingIndication%5D=NON_ROAMING"
curl -sk "$B?$P&slice-info-request-for-pdu-session%5BsNssai%5D%5Bsst%5D=1&slice-info-request-for-pdu-session%5BsNssai%5D%5Bsd%5D=010203"
curl -sk "$B?$P&slice-info-request-for-pdu-session%5BsNssai%5D%5Bsst%5D=2&slice-info-request-for-pdu-session%5BsNssai%5D%5Bsd%5D=010205"
```

Expected:

```json
{"nsiInformation":{"nrfId":"https://nrf:29510/nnrf-nfm/v1/nf-instances","nsiId":"1"}}
{"nsiInformation":{"nrfId":"https://nrf.aether-nsi2:29510/nnrf-nfm/v1/nf-instances","nsiId":"2"}}
```

### 6b. Are the two NRF registries separate?

```bash
for ns in aether-5gc aether-nsi2; do
  kubectl -n $ns port-forward svc/nrf 29599:29510 >/dev/null 2>&1 & sleep 3
  echo -n "$ns: "
  curl -sk "https://127.0.0.1:29599/nnrf-disc/v1/nf-instances?target-nf-type=SMF&requester-nf-type=AMF" \
    | python3 -c "import json,sys;print([i['ipv4Addresses'] for i in json.load(sys.stdin)['nfInstances']])"
  pkill -f "port-forward svc/nrf"
done
# aether-5gc:  [['smf']]
# aether-nsi2: [['smf.aether-nsi2']]
```

Exactly one SMF each. If either shows both, see
[Error 2](#error-2--nrf-2-sees-nrf-1s-registrations).

### 6c. The real test — does each slice land on its own SMF?

```bash
sudo /home/ubuntu/UERANSIM/run-2slice.sh
# UE1 slice1   : uesimtun0, 192.168.100.9
# UE2 slice2   : uesimtun0, 192.168.101.4

kubectl -n aether-5gc  logs deploy/smf --since=2m | grep -oE 'imsi-[0-9]+' | sort -u
#   imsi-208930100007510        <- slice 1 only
kubectl -n aether-nsi2 logs deploy/smf --since=2m | grep -oE 'imsi-[0-9]+' | sort -u
#   imsi-208930100007530        <- slice 2 only
```

**Each SMF handles exactly one IMSI.** That is the proof.

PFCP peers confirm the user plane also split:

```bash
kubectl -n aether-5gc  logs deploy/smf --since=2m | grep "Session Establish Request"
#   ... NodeID[10.43.233.120] ... "id": "imsi-208930100007510"    <- UPF-1
kubectl -n aether-nsi2 logs deploy/smf --since=2m | grep "Session Establish Request"
#   ... NodeID[10.43.188.100] ... "id": "imsi-208930100007530"    <- UPF-2
```

Data path:

```bash
NS1=uesimtun-208930100007510-internet-psi1
NS2=uesimtun-208930100007530-internet-psi1
sudo ip netns exec $NS1 ping -c3 -I uesimtun0 8.8.8.8
sudo ip netns exec $NS2 ping -c3 -I uesimtun0 8.8.8.8
```

---

## Step 7 — the isolation demo

The point of NSIs. Kill NSI-2's control plane and slice 1 must be unaffected:

```bash
kubectl -n aether-nsi2 scale deploy/smf --replicas=0
sudo /home/ubuntu/UERANSIM/run-2slice.sh
# UE1 slice1   : uesimtun0, 192.168.100.8      <- still works
# UE2 slice2   :                               <- no session

kubectl -n aether-nsi2 scale deploy/smf --replicas=1
```

In the shared-control-plane deployment, killing the single SMF takes down *both*
slices. That difference is the whole value proposition.

---

## Errors encountered, with fixes

Every one of these was hit in order during the first deployment.

### Error 1 — `buildCustomCert: unable to decode base64 certificate`

```
Error: UPGRADE FAILED: template: sd-core/charts/5g-control-plane/templates/secret-certs.yaml:12:14:
  ... executing "5g-control-plane.ensure-shared-ca" at <buildCustomCert $caCrt $caKey>:
  error calling buildCustomCert: unable to decode base64 certificate
```

**This is a chart bug, and it breaks every `helm upgrade` of SD-Core 4.1.0 —
not just multi-NSI work.** `templates/_helpers.tpl` does:

```gotemplate
{{- $caCrt = index $existingCaSecret.data "ca.crt" | default "" | b64dec -}}   # -> raw PEM
{{- $ca = buildCustomCert $caCrt $caKey -}}                                    # wants BASE64 PEM
```

sprig's `buildCustomCert` expects base64-encoded PEM. A fresh install has no
secret, so the chart calls `genCA` instead and works — which is why only
upgrades break.

**Symptom you may already have:** `helm list -n aether-5gc` showing the `sd-core`
release as `failed` even though every pod is Running.

**Fix** — store the secret double-encoded so the chart's `b64dec` yields base64:

```bash
./tools/fix-ca-secret.sh aether-5gc
```

The chart rewrites the secret single-encoded after a successful upgrade, so
**run this before every `helm upgrade` / `make 5gc-core-install`.**

### Error 2 — NRF-2 sees NRF-1's registrations

**Symptom.** NRF-2 discovery returns two SMFs, one of them `['smf']`. The AMF may
route slice-2 sessions to SMF-1 while everything looks correct.

**Cause.** Both NRFs persist NF profiles into the `NfProfile` collection of the
database named by `config.mongodb.name`. Sharing `name: aether` merges the two
registries and the NSI separation silently collapses.

**Fix.** Give NSI-2 its own database:

```yaml
    mongodb:
      name: aether_nsi2
```

Then clean any profiles that already leaked into the primary DB:

```bash
kubectl -n aether-5gc exec mongodb-0 -- mongosh --quiet --eval '
  print(db.getSiblingDB("aether").NfProfile.deleteMany({ipv4addresses:"smf.aether-nsi2"}).deletedCount);'
kubectl -n aether-5gc rollout restart deploy/nrf
```

### Error 3 — AMF discovers NSI-2 but dials SMF-1

**Symptom.** NRF-2 lists the SMF as `ipv4Addresses: ["smf"]`, `apiPrefix:
https://smf:29502`. Slice-2 sessions are handled by SMF-1. Nothing errors.

**Cause.** `registerIPv4` defaults to the bare service name `smf`. The AMF lives
in `aether-5gc`, where `smf` resolves to **SMF-1**.

**Fix.** Namespace-qualify it in the NSI values (both NRF and SMF):

```yaml
    smf:
      cfgFiles:
        smfcfg.yaml:
          configuration:
            sbi:
              registerIPv4: smf.aether-nsi2
```

Cert SANs already cover `<name>.<namespace>`, so TLS still validates.

### Error 4 — `UDM discovery returned no NF instances`

```
ERROR producer/pdu_session.go:188  PDUSessionSMContextCreate,
  send NF Discovery Serving UDM Error[UDM discovery returned no NF instances]
  {"id": "imsi-208930100007530"}
```

**Cause.** SD-Core has no NRF federation. Each NF registers into exactly one NRF,
so NRF-2 contains only SMF-2 — no UDM, UDR, AUSF or PCF.

**Fix.** Seed them (Step 3) and stop NRF-2 expiring the seeded profiles, since
nothing heartbeats them into NRF-2:

```yaml
    nrf:
      cfgFiles:
        nrfcfg.yaml:
          configuration:
            nfProfileExpiryEnable: false
```

```bash
./tools/seed-nsi-nrf.sh
```

**Sub-error:** if you seed the profiles unmodified, SMF-2 fails with
`lookup udm on 10.43.0.10:53: no such host` — bare names do not resolve across
namespaces. The script rewrites them to `udm.aether-5gc` etc.

### Error 5 — both slices land on the same SMF

**Symptom.** SMF-2's log shows *both* IMSIs; SMF-1's shows none.

**Cause.** The AMF caches NRF discovery results and the cache does not appear to
be keyed by NRF URI, so a result fetched from NRF-2 gets served for a query that
should have gone to NRF-1.

**Fix.** `enableNrfCaching: false` on the AMF (Step 5).

> Not rigorously isolated — the correlation was clear and disabling the cache
> resolved it, but treat the exact mechanism as suspected rather than proven.

### Error 6 — `Error during Process: datapath down`

```
ERROR pfcpiface/messages.go:142  error handling PFCP message type
  Association Setup Request, from: 10.42.0.101:8805, nodeID: ,
  error: Error during Process: datapath down
```

and on the SMF side:

```
ERROR producer/pdu_session.go:366  PDUSessionSMContextCreate,
  UPF association recovery failed: UPF 10.43.188.100 not associated after PFCP association retry
```

**Cause.** The BESS datapath inside the UPF pod had stopped serving, while all 5
containers still reported `Running`/`Ready` with 0 restarts. pfcpiface rejects
every association and never recovers on its own.

**Fix.**

```bash
kubectl -n aether-upf-1 delete pod upf-0
```

Worth knowing: **pod health tells you nothing here.** Check
`kubectl -n <ns> logs upf-0 -c pfcp-agent | grep -i datapath` when associations fail.

### Error 7 — `helm template` fails on the CA lookup

```
Error: execution error at (sd-core/charts/5g-control-plane/templates/secret-certs.yaml:12:14):
  existing private CA Secret "5g-control-plane-ca-private" was configured but not found
```

**Cause.** `helm template` renders client-side, so its `lookup` of cluster
secrets returns empty. Not a real problem.

**Fix.** Use `helm install` / `helm upgrade`, or `--dry-run=server`.

### Error 8 — NSSF still returns the old `nsiList`

**Symptom.** The `nssf` ConfigMap has your new `nsiList` but the API still
returns `nsiId: 22` and `{}` for slice 2.

**Cause.** The NSSF does not reload its ConfigMap at runtime.

**Fix.** `kubectl -n aether-5gc rollout restart deploy/nssf`

### Error 9 — playbook says `rescued=1`

The Helm task failed and Ansible recovered, so **your config changes were not
applied** even though the playbook exits 0. Distinct from the harmless
`rescued=1` on a *first* install (Helm `--wait` timing out during image pulls).

```bash
grep -oE "Error: UPGRADE FAILED[^\"]{0,120}" <logfile>
```

Almost always Error 1. Run `./tools/fix-ca-secret.sh aether-5gc` and retry.

---

## Known limitations of this setup

Be honest about what this is and is not.

* **The config plane cannot be scoped per NSI.** Both SMFs poll the same
  webconsole `/nfconfig/session-management`, so both learn *all* slices and *all*
  UPFs. SMF-2 endlessly retries an association with `upf` (UPF-1), which does not
  resolve from `aether-nsi2`:

  ```
  WARN  host lookup failed: lookup upf on 10.43.0.10:53: no such host
  ERROR send pfcp association setup request failed: ... invalid NodeId: upf
  ```

  Harmless noise here, but true per-NSI config isolation would need a second
  webconsole fed from a separate slice configuration.

* **Shared-NF discovery is seeded, not federated.** `tools/seed-nsi-nrf.sh` is a
  workaround for a missing feature. Re-run it after the shared NFs re-register.

* **Subscriber data is still shared** — one UDM/UDR over one `aether` database.
  Separating that means a full NF stack per NSI.

* **The AMF is shared**, correctly — 3GPP requires one AMF per UE across its
  slices. AMF re-allocation (a different AMF *set* per slice) is a separate NSSF
  job that SD-Core cannot exercise, because its AMF sends no TAI in the NSSF
  query.

* **No per-slice admission control** (no NSACF) and **no RAN slicing** — the gNB
  advertises both S-NSSAIs but does not partition radio resources.

---

## Scaling to a third NSI

Repeat with incremented names. For NSI-3 serving `sst 3 / sd 010207`:

1. `kubectl create namespace aether-nsi3`, copy the double-encoded CA secret into it.
2. Copy `nsi2-values.yaml` → `nsi3-values.yaml` and change:
   - `config.mongodb.name: aether_nsi3`
   - `nrf.…sbi.registerIPv4: nrf.aether-nsi3`
   - `smf.…nrfUri: https://nrf.aether-nsi3:29510`
   - `smf.…sbi.registerIPv4: smf.aether-nsi3`
   - `smf.…smfName: SMF-NSI3`
3. `helm upgrade --install nsi3 ... -n aether-nsi3 --values .../nsi3-values.yaml`
4. `./tools/seed-nsi-nrf.sh aether aether_nsi3 aether-5gc`
5. Add an `nsiList` entry mapping `sst 3 / sd 010207` →
   `https://nrf.aether-nsi3:29510/...`
6. `./tools/fix-ca-secret.sh aether-5gc && make 5gc-core-install`
7. `kubectl -n aether-5gc rollout restart deploy/nssf`

Each NSI also needs its own slice and UPF from
[MULTI-SLICE.md](MULTI-SLICE.md) — `tools/gen-slices.py --slices 3` generates those.

Cost per NSI: ~2 control-plane pods + 1 UPF pod, roughly 1.3 GB RAM.

---

## Teardown

Back to the shared control plane, keeping user-plane slicing:

```bash
helm uninstall nsi2 -n aether-nsi2
kubectl delete namespace aether-nsi2

kubectl -n aether-5gc exec mongodb-0 -- mongosh --quiet --eval '
  db.getSiblingDB("aether_nsi2").dropDatabase();'

# remove the nsiList and enableNrfCaching lines from sdcore-5g-values.yaml, then
./tools/fix-ca-secret.sh aether-5gc
make 5gc-core-install
kubectl -n aether-5gc rollout restart deploy/nssf
```

Slice 2 then falls back to SMF-1 and keeps working — the NSSF stops mattering
again, which is itself a neat confirmation that it was load-bearing.
