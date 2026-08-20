# Deploying SD-Core with Multiple Network Slices

How to run Aether OnRamp's SD-Core with **N network slices, each backed by its own
BESS-UPF**, and how to verify the slicing is real rather than cosmetic.

Verified end-to-end on a single-node RKE2 cluster with SD-Core 4.1.0 and UERANSIM
v3.3.0 (2 slices, 2 UPFs, 2 UEs, separate datapaths confirmed on the wire).

---

## Contents

1. [What you get](#1-what-you-get)
2. [How slice → UPF binding actually works](#2-how-slice--upf-binding-actually-works)
3. [Prerequisites](#3-prerequisites)
4. [Step 1 — plan the address space](#step-1--plan-the-address-space)
5. [Step 2 — declare the extra UPFs in `vars/main.yml`](#step-2--declare-the-extra-upfs-in-varsmainyml)
6. [Step 3 — declare device groups and slices](#step-3--declare-device-groups-and-slices)
7. [Step 4 — deploy the core](#step-4--deploy-the-core)
8. [Step 5 — deploy the additional UPFs](#step-5--deploy-the-additional-upfs)
9. [Step 6 — verify the control plane](#step-6--verify-the-control-plane)
10. [Step 7 — test with UERANSIM](#step-7--test-with-ueransim)
11. [Scaling to 3, 4, 5 … N slices](#scaling-to-3-4-5--n-slices)
12. [Troubleshooting](#troubleshooting)
13. [Teardown](#teardown)
14. [Appendix — files changed](#appendix--files-changed)

---

## 1. What you get

Each slice is an **S-NSSAI** (`sst` + `sd`) that acts as a routing key, enforced at
four independent points:

| Enforced at | Mechanism |
|---|---|
| Subscription | Device group → slice membership, stored in UDR/UDM |
| Session routing | SMF selects a UPF by `(DNN, S-NSSAI)` |
| User plane | A **separate BESS-UPF process**, own N3 tunnel endpoint |
| Addressing | Own UE IP pool, own N6 exit and NAT rule |

Concretely, with two slices you get two UPF pods in two namespaces, two UE address
pools, and two independent PFCP sessions. A UE on slice 1 has no path into slice 2's
datapath.

**What this is not:** there is no per-slice admission control (no NSACF), no
control-plane isolation (one AMF/SMF/NRF serves all slices), and no RAN-side
resource partitioning. This is *user-plane* slicing.

---

## 2. How slice → UPF binding actually works

Worth understanding before you change config, because the obvious guess is wrong.

```
  simapp (standalone)  or  ROC/AMP (managed)
                    │
                    ▼
               webconsole  ──── MongoDB
                    │
        HTTP GET /nfconfig/*   (each NF polls every 5s)
       ┌────────────┼─────────────┐
       ▼            ▼             ▼
  session-      access-       plmn-snssai
  management    mobility
       │            │             │
      SMF          AMF          NSSF
```

At PDU session establishment:

```
UE ──PDU Session Req (S-NSSAI, DNN)──► AMF ──NRF discovery (snssai+dnn)──► SMF
                                                                            │
                                        UPFSelectionParams{Dnn, SNssai}     │
                                        → selectMatchUPF()  ◄── THE decision│
                                                                            ▼
                                                        N4/PFCP to that slice's UPF
```

**The SMF makes the decision**, from config it polls out of the webconsole
(`context/user_plane_information.go`). Selection is first-match — `destinations[0]`,
no load balancing.

### About the NSSF

SD-Core ships an NSSF and it **is** called — once per PDU session
(`NSSelectionGetForPduSession`). It **cannot influence anything** in a stock
deployment:

* It is **never** called at registration, because the AMF only consults it when the
  requested S-NSSAI is absent from the subscribed NSSAI — and yours is provisioned.
* The AMF's query carries **no TAI and no home-plmn-id**, so every check inside the
  NSSF (`CheckSupportedTa`, `CheckSupportedSnssaiInPlmn`, `CheckSupportedSnssaiInTa`)
  is skipped — they are all guarded by `if param.Tai != nil`.
* Its only possible contribution is an `NsiInformation` pointing at a *different NRF*
  for a different network slice instance. The shipped `nsiList` has one entry
  (`sst 1 / sd 010203`) pointing at the same NRF you already use.

So a slice not listed in the NSSF's `nsiList` works fine. You will see this in the
AMF log and it is harmless:

```
WARN consumer/sm_context.go:125  nsiInformation is still nil, use default NRF[https://nrf:29510]
```

**You do not need to touch the NSSF to add slices.**

---

## 3. Prerequisites

* Aether OnRamp cloned, Ansible installed, `hosts.ini` matching your host.
* A running Kubernetes cluster (`make aether-k8s-install`) with **Multus** — the UPF
  needs macvlan interfaces.
* Enough headroom: each extra UPF is a 5-container pod. Budget ~1 GB RAM and
  ~1.5 GB disk per UPF.

Check before you start:

```bash
export KUBECONFIG=$HOME/.kube/config
kubectl get nodes
free -g ; df -h /
```

---

## Step 1 — plan the address space

Every slice needs four unique values. Collisions here are the most common cause of
a broken deployment.

| Slice | S-NSSAI | UPF access IP | UPF core IP | UE pool | Namespace | `upf-name` |
|---|---|---|---|---|---|---|
| 1 | sst 1 / sd 010203 | 192.168.252.3 | 192.168.250.3 | 192.168.100.0/24 | `aether-5gc` | `upf` |
| 2 | sst 2 / sd 010205 | 192.168.252.6 | 192.168.250.6 | 192.168.101.0/24 | `aether-upf-1` | `upf.aether-upf-1` |
| 3 | sst 3 / sd 010207 | 192.168.252.7 | 192.168.250.7 | 192.168.102.0/24 | `aether-upf-2` | `upf.aether-upf-2` |
| 4 | sst 4 / sd 010209 | 192.168.252.8 | 192.168.250.8 | 192.168.103.0/24 | `aether-upf-3` | `upf.aether-upf-3` |

Notes:

* **Slice 1 is special** — it uses the UPF built into the `sd-core` chart, in the
  `aether-5gc` namespace, addressed simply as `upf`. It is configured under
  `core.upf.default_upf`, not `additional_upfs`.
* Access/core IPs must sit inside `core.upf.access_subnet` (192.168.252.0/24) and
  `core.upf.core_subnet` (192.168.250.0/24). `.1` is the host gateway — do not use it.
* `sst`/`sd` values are arbitrary; they only need to be unique and to match what the
  gNB advertises and the UE requests.
* **IMSI ranges must be partitioned** — one device group per slice, no overlap.

Use the generator to produce a consistent plan:

```bash
python3 tools/gen-slices.py --slices 4 --imsis-per-slice 25
```

---

## Step 2 — declare the extra UPFs in `vars/main.yml`

The Quick Start `vars/main.yml` has **no** `core.upf.helm`, `core.upf.values_file`,
or `core.upf.additional_upfs` keys, so `make aether-add-upfs` has nothing to deploy.
Add them:

```yaml
core:
  # ... existing keys ...
  upf:
    access_subnet: "192.168.252.1/24"
    core_subnet: "192.168.250.1/24"
    mode: af_packet
    multihop_gnb: false

    # --- ADD: chart used for the additional UPFs ---
    helm:
      local_charts: false
      chart_ref: oci://ghcr.io/omec-project/bess-upf
      chart_version: 1.7.2
    values_file: "deps/5gc/roles/upf/templates/upf-5g-values.yaml"

    default_upf:             # slice 1 — built into the sd-core chart
      ip:
        access: "192.168.252.3"
        core:   "192.168.250.3"
      ue_ip_pool: "192.168.100.0/24"

    # --- ADD: one entry per extra slice. Key becomes namespace aether-upf-<key> ---
    additional_upfs:
      "1":                   # slice 2 -> ns aether-upf-1
        ip:
          access: "192.168.252.6"
          core:   "192.168.250.6"
        ue_ip_pool: "192.168.101.0/24"
```

Also confirm these, which the rest of the guide assumes:

```yaml
core:
  standalone: true          # true = simapp provisions slices (no ROC needed)
  data_iface: eth0          # your data-plane interface
  amf:
    ip: "192.168.6.90"      # host IP the RAN will use for N2
```

> `standalone: true` uses **simapp** to provision device groups and slices from the
> Helm values. The alternative (`standalone: false`) puts provisioning under ROC/AMP
> and is what `vars/main-upf.yml` demonstrates. Standalone has fewer moving parts.

---

## Step 3 — declare device groups and slices

Edit the **simapp** section of
`deps/5gc/roles/core/templates/sdcore-5g-values.yaml`, under
`omec-sub-provision.config.simapp.cfgFiles.simapp.yaml.configuration`.

### 3a. Subscribers

The `subscribers:` ranges hold auth keys. They must **cover every IMSI** you place
in any device group. The stock file already covers `...7500`–`...7599`:

```yaml
            subscribers:
            - ueId-start: "208930100007500"
              ueId-end: "208930100007509"
              plmnId: "20893"
              opc: "981d464c7c52eb6e5036234984ad0bcf"
              op: ""
              key: "5122250214c33e723a5dd523fc145fc0"
              sequenceNumber: "16f3b3f70fc2"
            - ueId-start: "208930100007510"
              ueId-end: "208930100007599"
              # ... same key material ...
```

### 3b. Device groups — one per slice, non-overlapping IMSIs

```yaml
            device-groups:
            - name:  "user-group1"
              imsis:
                - "208930100007500"
                # ... through ...
                - "208930100007524"
              msisdns:
                - "msisdn-9000000001"
                # ... one per IMSI ...
              ip-domain-name: "pool1"
              ip-domains:
                - dnn: internet
                  dns-primary: "8.8.8.8"
                  mtu: 1460
                  ue-ip-pool: {{ core.upf.default_upf.ue_ip_pool }}
                  ue-dnn-qos:
                    dnn-mbr-downlink: 1000
                    dnn-mbr-uplink:   1000
                    bitrate-unit: Mbps
                    traffic-class:
                      name: "platinum"
                      qci: 9
                      arp: 6
                      pdb: 300
                      pelr: 6
              site-info: "enterprise"

            - name:  "user-group2"
              imsis:
                - "208930100007525"
                # ... through ...
                - "208930100007599"
              msisdns:
                # ... one per IMSI ...
              ip-domain-name: "pool2"
              ip-domains:
                - dnn: internet
                  dns-primary: "8.8.8.8"
                  mtu: 1460
                  ue-ip-pool: {{ core.upf.additional_upfs['1'].ue_ip_pool }}
                  # ... same ue-dnn-qos block ...
              site-info: "enterprise"
```

Note the Jinja reference `{{ core.upf.additional_upfs['1'].ue_ip_pool }}` — this
keeps the pool in one place (`vars/main.yml`) so the UPF and the device group can
never disagree.

### 3c. Network slices — one per device group

```yaml
            network-slices:
            - name: "default"
              slice-id:
                sd: "010203"
                sst: 1
              site-device-group:
              - "user-group1"
              application-filtering-rules:
              - rule-name: "ALLOW-ALL"
                priority: 250
                action: "permit"
                endpoint: "0.0.0.0/0"
                traffic-class:
                  qci: 9
                  arp: 6
              site-info:
                gNodeBs:
                - name: "gnb1"
                  tac: 1
                - name: "gnb2"
                  tac: 2
                plmn:
                  mcc: "208"
                  mnc: "93"
                site-name: "enterprise"
                upf:
                  upf-name: "upf"                # slice 1 -> built-in UPF
                  upf-port: 8805

            - name: "slice2"
              slice-id:
                sd: "010205"
                sst: 2
              site-device-group:
              - "user-group2"
              # ... same application-filtering-rules ...
              site-info:
                # ... same gNodeBs / plmn / site-name ...
                upf:
                  upf-name: "upf.aether-upf-1"   # slice 2 -> UPF in ns aether-upf-1
                  upf-port: 8805
```

**`upf-name` is the critical field.** The `bess-upf` chart creates a Service named
`upf` in whatever namespace it is installed into, so `upf.<namespace>` resolves from
the SMF via cluster DNS.

### 3d. Validate before deploying

The values file is a Jinja template, so a plain YAML parse will not work. Render it
first:

```bash
python3 - <<'PY'
import yaml, jinja2
env = jinja2.Environment()
env.filters["ternary"] = lambda v,a,b: a if v else b
env.filters["string"]  = str
env.filters["lower"]   = lambda v: str(v).lower()
core = yaml.safe_load(open("vars/main.yml"))
t = env.from_string(open("deps/5gc/roles/core/templates/sdcore-5g-values.yaml").read())
d = yaml.safe_load(t.render(core=core["core"], ran_subnet="172.20.0.0/16",
        ansible_default_ipv4={"address":"127.0.0.1"}, access_gw="", core_gw="",
        access_ip="", core_ip=""))
sim = d["omec-sub-provision"]["config"]["simapp"]["cfgFiles"]["simapp.yaml"]["configuration"]
for g in sim["device-groups"]:
    print(f"group {g['name']:12s} imsis={len(g['imsis']):3d} pool={g['ip-domains'][0]['ue-ip-pool']}")
for s in sim["network-slices"]:
    print(f"slice {s['name']:9s} sst={s['slice-id']['sst']} sd={s['slice-id']['sd']} "
          f"upf={s['site-info']['upf']['upf-name']}")
print("RENDER OK")
PY
```

---

## Step 4 — deploy the core

```bash
make aether-5gc-install        # = 5gc-router-install + 5gc-core-install
```

This configures host networking (macvlan `access`/`core` interfaces, IP forwarding,
NAT) and installs the `sd-core` Helm chart into `aether-5gc`.

```bash
kubectl get pods -n aether-5gc
```

Wait for all pods `Running`. Expect ~16 pods.

> **`helm list` may show the release as `failed`.** Helm's `--wait` has a 2m30s
> timeout that image pulls routinely exceed on a first install. The Ansible role has
> a recovery path (`rescued=1`) and the playbook still exits 0. If all pods are
> `Running`, ignore it — see [Troubleshooting](#troubleshooting).

---

## Step 5 — deploy the additional UPFs

```bash
make aether-add-upfs
```

For **each** entry in `core.upf.additional_upfs` this:

1. renders `deps/5gc/roles/upf/templates/upf-5g-values.yaml` to `/tmp/upf-<key>.yaml`
2. installs the `bess-upf` chart as release `bess-upf` into namespace `aether-upf-<key>`
3. adds a host route: `<ue_ip_pool> via <core IP>`
4. adds a host MASQUERADE rule for `<ue_ip_pool>`

Verify — **note the namespace**, this is why `kubectl get pods -n aether-5gc` shows
only one UPF:

```bash
kubectl get pods -A | grep upf-0
#   aether-5gc     upf-0   5/5  Running     <- slice 1
#   aether-upf-1   upf-0   5/5  Running     <- slice 2

helm list -A | grep -E "sd-core|bess-upf"
ip route | grep 192.168.10          # one route per UE pool
sudo iptables-save -t nat | grep MASQUERADE | grep 192.168.10
```

---

## Step 6 — verify the control plane

Do these **before** connecting a RAN. Each one catches a different class of mistake.

### 6a. Did the SMF build both UPF nodes?

```bash
kubectl -n aether-5gc logs deploy/smf | grep "creating UPF node"
```

Expect one line per slice with the right S-NSSAI:

```
creating UPF node: upf,               ... SNSSAI: {Sst:1 ...}
creating UPF node: upf.aether-upf-1,  ... SNSSAI: {Sst:2 ...}
```

### 6b. Is PFCP associated with every UPF?

```bash
kubectl -n aether-5gc logs deploy/smf | grep -i "AssociatedSetUpSuccess"
```

One line per UPF ClusterIP. Transient `invalid NodeId: upf.aether-upf-N` errors before
the UPF exists are normal — the SMF re-polls every 5s and self-heals.

### 6c. What config does the webconsole actually serve?

```bash
kubectl -n aether-5gc port-forward svc/webui 15001:5001 &
curl -s -H "Accept: application/json" \
     http://127.0.0.1:15001/nfconfig/session-management | python3 -m json.tool
```

The `Accept` header is mandatory — without it you get
`{"error":"Accept header must be 'application/json'"}`. Confirm one object per slice
with the right `snssai`, `upf.hostname`, and `ipDomain[].ueSubnet`.

### 6d. Are subscribers split across slices?

```bash
kubectl -n aether-5gc exec mongodb-0 -- mongosh --quiet aether --eval '
  db.getCollection("subscriptionData.provisionedData.amData").aggregate([
    {$unwind:"$nssai.defaultSingleNssais"},
    {$group:{_id:{sst:"$nssai.defaultSingleNssais.sst",sd:"$nssai.defaultSingleNssais.sd"},
             count:{$sum:1}}}
  ]).forEach(d => print("sst=" + d._id.sst + " sd=" + d._id.sd + " -> " + d.count));'
```

Auth keys live in a **separate database**:

```bash
kubectl -n aether-5gc exec mongodb-0 -- mongosh --quiet --eval '
  print(db.getSiblingDB("authentication")
          .getCollection("subscriptionData.authenticationData.authenticationSubscription")
          .countDocuments({}));'
```

### 6e. Is N2 reachable for your RAN?

```bash
kubectl -n aether-5gc get svc amf -o wide      # EXTERNAL-IP should be core.amf.ip
sudo iptables-save -t nat | grep 38412
```

---

## Step 7 — test with UERANSIM

### 7a. The N3 rule that will bite you

The gNB's **`gtpIp` must be inside the UPF's `enb.subnet`** (`core.ran_subnet`,
default `172.20.0.0/16`).

Each UPF carries exactly one route back to the RAN:

```
172.20.0.0/16 via 192.168.252.1 dev access
```

BESS installs a next-hop only for that subnet. If you set `gtpIp` to the access
gateway itself (`192.168.252.1`), **PDU sessions establish normally and no user data
flows** — uplink GTP-U reaches the UPF and nothing comes back. Add the gNB address to
the host's access interface:

```bash
sudo ip addr add 172.20.0.2/16 dev access
```

> This is **runtime-only** and disappears on reboot. To persist it, add the address
> to the systemd-networkd file the router role manages
> (`/etc/systemd/network/20-aether-access.network`).

### 7b. gNB config — advertise every slice

`/home/ubuntu/UERANSIM/config/sdcore-2slice-gnb.yaml`:

```yaml
mcc: '208'
mnc: '93'
nci: '0x000000010'
idLength: 32
tac: 1

linkIp: 127.0.0.1          # radio-link sim to the UEs
ngapIp: 192.168.6.90       # N2 to AMF  (== core.amf.ip)
gtpIp: 172.20.0.2          # N3 source  — MUST be inside 172.20.0.0/16

amfConfigs:
  - address: 192.168.6.90
    port: 38412

slices:                    # every slice this gNB serves
  - sst: 1
    sd: 0x010203
  - sst: 2
    sd: 0x010205

ignoreStreamIds: true
```

### 7c. UE config — one per slice

Copy the stock `sdcore-ue.yaml` and change **three** things: `supi` (must be in that
slice's device group), `sessions[].slice`, and `configured-nssai`.

```yaml
supi: 'imsi-208930100007530'      # in user-group2 -> slice 2
# ...
sessions:
  - type: 'IPv4'
    apn: 'internet'
    slice:
      sst: 2
      sd: 0x010205
configured-nssai:
  - sst: 2
    sd: 0x010205
```

### 7d. Run

```bash
cd /home/ubuntu/UERANSIM
sudo ./build/nr-gnb -c config/sdcore-2slice-gnb.yaml    # terminal 1
sudo ./build/nr-ue  -c config/sdcore-slice1-ue.yaml     # terminal 2
sudo ./build/nr-ue  -c config/sdcore-slice2-ue.yaml     # terminal 3
```

Or all at once: `sudo ./run-2slice.sh` (logs in `~/UERANSIM/logs/`).

### 7e. Test each slice

UERANSIM v3.3.0 places each PDU session in **its own network namespace**, so
`ip addr` on the host shows nothing:

```bash
sudo ip netns list | grep uesimtun

NS1=uesimtun-208930100007510-internet-psi1
NS2=uesimtun-208930100007530-internet-psi1

sudo ip netns exec $NS1 ip -br -4 addr show uesimtun0    # 192.168.100.x
sudo ip netns exec $NS2 ip -br -4 addr show uesimtun0    # 192.168.101.x

sudo ip netns exec $NS1 ping -c3 -I uesimtun0 8.8.8.8
sudo ip netns exec $NS2 ping -c3 -I uesimtun0 8.8.8.8
```

### 7f. Prove the slices really diverge

The single most convincing check — same gNB source, different UPF per slice:

```bash
sudo tcpdump -ni access 'udp port 2152'
```

```
172.20.0.2.2152 > 192.168.252.3    <- slice 1
172.20.0.2.2152 > 192.168.252.6    <- slice 2
```

And on the control plane:

```bash
kubectl -n aether-5gc logs deploy/smf | grep "Session Establish Request"
#   ... NodeID[10.43.233.120] ... "id": "imsi-208930100007510"   <- UPF1
#   ... NodeID[10.43.188.100] ... "id": "imsi-208930100007530"   <- UPF2
```

Per-UPF PDR state, showing fully separate datapaths:

```bash
kubectl -n aether-5gc   logs upf-0 -c pfcp-agent | grep "PDRs:" | tail -1
kubectl -n aether-upf-1 logs upf-0 -c pfcp-agent | grep "PDRs:" | tail -1
```

Different `F-SEID`, different `ueAddress`, different `tunnelIPv4Dst`.

---

## Scaling to 3, 4, 5 … N slices

The pattern is fully general. For **each** slice beyond the first, add one entry in
each of five places.

### The five places

| # | File | What to add |
|---|---|---|
| 1 | `vars/main.yml` | `core.upf.additional_upfs["<key>"]` — access IP, core IP, UE pool |
| 2 | `sdcore-5g-values.yaml` | `subscribers:` range covering the new IMSIs |
| 3 | `sdcore-5g-values.yaml` | `device-groups:` entry — IMSIs, `ip-domain-name`, `ue-ip-pool` |
| 4 | `sdcore-5g-values.yaml` | `network-slices:` entry — `sst`/`sd`, device group, `upf-name` |
| 5 | gNB config | one more entry under `slices:` |

The key in `additional_upfs` becomes the namespace suffix: key `"3"` →
namespace `aether-upf-3` → `upf-name: "upf.aether-upf-3"`.

### Generate it

```bash
python3 tools/gen-slices.py --slices 5 --imsis-per-slice 20
```

Prints a plan plus all three config blocks, ready to paste:

```
#  slice 1: default   sst=1 sd=010203  IMSI ...7500..7519  pool=192.168.100.0/24  upf=upf (aether-5gc)
#  slice 2: slice2    sst=2 sd=010205  IMSI ...7520..7539  pool=192.168.101.0/24  upf=upf.aether-upf-1
#  slice 3: slice3    sst=3 sd=010207  IMSI ...7540..7559  pool=192.168.102.0/24  upf=upf.aether-upf-2
#  slice 4: slice4    sst=4 sd=010209  IMSI ...7560..7579  pool=192.168.103.0/24  upf=upf.aether-upf-3
#  slice 5: slice5    sst=5 sd=01020b  IMSI ...7580..7599  pool=192.168.104.0/24  upf=upf.aether-upf-4
```

### Apply

```bash
# 1. paste the three blocks into vars/main.yml, sdcore-5g-values.yaml, gNB config
# 2. re-render the core so the new slices reach the webconsole
make aether-5gc-reset          # uninstall + install (keeps the router config)
# 3. deploy every additional UPF (idempotent — existing ones are upgraded in place)
make aether-add-upfs
```

`make aether-add-upfs` loops over the whole `additional_upfs` dict, so a single run
brings up all new UPFs.

### Rules that must hold

* **Unique per slice:** `sst`/`sd`, device-group name, `ip-domain-name`, UE pool,
  UPF access IP, UPF core IP, `additional_upfs` key.
* **IMSI ranges must not overlap** between device groups. An IMSI in two groups gets
  non-deterministic slice assignment.
* **`subscribers:` must cover every IMSI** used in any device group, or the UE fails
  authentication (not slice selection — the symptom looks unrelated).
* **The gNB must advertise every slice** it serves, and the UE's `configured-nssai`
  must match the slice its IMSI belongs to. Mismatch → registration succeeds but PDU
  session establishment fails.
* **One UPF per slice is a config convention, not a hard limit.** The SMF's
  `getOrCreateUpfNode()` merges `SNssaiInfos` when the same `upf-name` appears in two
  slices, so a shared UPF is possible. But each BESS-UPF pod is configured with a
  single DNN, a single UE pool and a single slice rate-limit, so 1:1 is the sane
  default.

### Practical ceiling

Not a protocol limit — resources. Each UPF is a 5-container pod needing ~1 GB RAM and
~1.5 GB disk. The access/core subnets are `/24`, so addressing allows ~250 UPFs. On a
single node, 4–6 slices is comfortable at 19 GB RAM; beyond that, spread UPFs across
worker nodes with `nodeSelectors`.

---

## Troubleshooting

### `kubectl get pods -n aether-5gc` shows only one UPF

Correct behaviour. Additional UPFs live in their own namespaces:
`kubectl get pods -A | grep upf-0`.

### Helm release shows `failed` but everything runs

```
Error: context deadline exceeded
```

Helm's `--wait --timeout 2m30s` expired while images were still pulling. The Ansible
role recovers (`rescued=1`, playbook exits 0). Cosmetic; clears on the next
`helm upgrade`. Confirm with `kubectl get pods -n aether-5gc`.

### PDU session establishes but no traffic flows

The classic one. Check `gtpIp` is inside `core.ran_subnet` (`172.20.0.0/16`) and that
the address exists on the host:

```bash
ip -br addr show access                       # expect 172.20.0.2/16 present
sudo tcpdump -ni access 'udp port 2152'       # uplink present, downlink absent?
kubectl -n aether-5gc exec upf-0 -c routectl -- ip route | grep 172.20
```

If uplink GTP-U reaches the UPF and nothing returns, `gtpIp` is outside the RAN subnet.

### `invalid NodeId: upf.aether-upf-N` in SMF logs

The UPF's Service does not exist yet. Harmless if it stops after `make aether-add-upfs`
— the SMF re-polls every 5s. If it persists, check DNS:

```bash
kubectl -n aether-upf-1 get svc upf
kubectl -n aether-5gc exec deploy/smf -- nslookup upf.aether-upf-1
```

### `make aether-ueransim-install` fails with "Unable to start service docker"

systemd rate-limiting, not a config problem:

```bash
sudo systemctl reset-failed docker.service docker.socket
sudo systemctl start docker
```

### UE registers but PDU session is rejected

Slice mismatch. The UE's `sessions[].slice` must match the S-NSSAI of the slice whose
device group contains its IMSI:

```bash
kubectl -n aether-5gc logs deploy/smf | grep -i "can not find UPF"
```

### `{"error":"Accept header must be 'application/json'"}`

The `/nfconfig/*` endpoints require it: `curl -H "Accept: application/json" ...`.

---

## Teardown

```bash
make aether-remove-upfs        # remove additional UPFs
make aether-5gc-uninstall      # remove core + host router config
make aether-k8s-uninstall      # remove the cluster

sudo pkill -x nr-ue; sudo pkill -x nr-gnb
sudo ip addr del 172.20.0.2/16 dev access
```

---

## Appendix — files changed

Relative to a stock Aether OnRamp checkout, a two-slice deployment touches:

| File | Change |
|---|---|
| `vars/main.yml` | added `core.upf.helm`, `core.upf.values_file`, `core.upf.additional_upfs`; added a `ueransim:` section |
| `deps/5gc/roles/core/templates/sdcore-5g-values.yaml` | split `device-groups` in two; added a second `network-slices` entry |
| `hosts.ini` | added `node1` to `[ueransim_nodes]` (empty by default) |
| `deps/ueransim/config/custom-gnb.yaml` | added the second S-NSSAI to `slices:` |
| `deps/ueransim/config/custom-ue-slice2.yaml` | new — slice-2 UE |
| `tools/gen-slices.py` | new — N-slice config generator |

Host state that is **not** in git and does not survive a reboot:

* `172.20.0.2/16` on the `access` interface (gNB N3 source)
* UE-pool routes and MASQUERADE rules — these *are* recreated by
  `make aether-add-upfs`
