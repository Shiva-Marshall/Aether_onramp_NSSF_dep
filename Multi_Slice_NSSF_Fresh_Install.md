# SD-Core Multi-Slice with NSSF-Based Slice Selection — Complete Fresh Install

A **from-zero** guide: bare Ubuntu host → Kubernetes → SD-Core → two network
slices, each served by its **own NRF and SMF and UPF**, with the **NSSF actually
selecting** between them. Ends with a working two-slice RAN test.

Nothing is assumed to be installed. Every command is here.

Verified end to end on Ubuntu 22.04, single node, SD-Core 4.1.0, UERANSIM v3.3.0.

---

## Contents

- [0. What you are building](#0-what-you-are-building)
- [1. Host prerequisites](#1-host-prerequisites)
- [2. Get the code](#2-get-the-code)
- [3. Plan your addresses](#3-plan-your-addresses)
- [4. Configure `hosts.ini`](#4-configure-hostsini)
- [5. Configure `vars/main.yml`](#5-configure-varsmainyml)
- [6. Configure slices, NSSF and AMF](#6-configure-slices-nssf-and-amf)
- [7. Install Kubernetes](#7-install-kubernetes)
- [8. Install SD-Core (NSI-1)](#8-install-sd-core-nsi-1)
- [9. Deploy UPF-2](#9-deploy-upf-2)
- [10. Deploy NSI-2 (NRF-2 + SMF-2)](#10-deploy-nsi-2-nrf-2--smf-2)
- [11. Seed NRF-2 with the shared NFs](#11-seed-nrf-2-with-the-shared-nfs)
- [12. Build and configure UERANSIM](#12-build-and-configure-ueransim)
- [13. Run and verify](#13-run-and-verify)
- [14. Prove the isolation](#14-prove-the-isolation)
- [15. Day-2 operations](#15-day-2-operations)
- [16. Troubleshooting — every error and its fix](#16-troubleshooting--every-error-and-its-fix)
- [17. Adding a third slice](#17-adding-a-third-slice)
- [18. Teardown](#18-teardown)

---

## 0. What you are building

```
                    ┌──────────── shared (serves every slice) ────────────┐
 gNB ──N2──►  AMF        NSSF      UDM  UDR  AUSF  PCF  webconsole  mongo
               │          ▲                                      [aether-5gc]
               │  (1) "which NSI serves sst=2/sd=010205?"
               └──────────┘
               │  (2) {nrfId: https://nrf.aether-nsi2:29510, nsiId: 2}
               │  (3) discover the SMF from THAT NRF
        ┌──────┴──────────────────────────┐
        ▼                                 ▼
  ┌─ NSI-1 ─────────────┐          ┌─ NSI-2 ─────────────────┐
  │ NRF-1  [aether-5gc] │          │ NRF-2  [aether-nsi2]    │
  │ SMF-1 ──N4──► UPF-1 │          │ SMF-2 ──N4──► UPF-2     │
  │        [aether-5gc] │          │        [aether-upf-1]   │
  └─────────────────────┘          └─────────────────────────┘
```

Final result: **19 pods across 3 namespaces**, two slices with fully separate
control and user planes, and an NSSF whose answer actually changes the outcome.

**Why the NSSF matters here.** Its only real output is
`nsiInformation{nsiId, nrfId}` — "use *this* NRF". With a single NRF that
sentence carries no information, which is why a stock SD-Core calls the NSSF and
discards the answer. Deploying a second NRF is what gives it meaning. SD-Core's
AMF already implements the redirect (`consumer/sm_context.go` overwrites `nrfUri`
from the NSSF response); it is simply dormant out of the box.

**Time:** ~60–90 minutes including image pulls and a UERANSIM build.

---

## 1. Host prerequisites

Ubuntu 22.04 or 24.04. Root/sudo. One network interface with a default route.

**Minimum:** 8 vCPU, 16 GB RAM, 40 GB free disk. (Reference host: 12 vCPU,
19 GB RAM.)

```bash
sudo apt update
sudo apt install -y sshpass ansible make git curl python3-pip
pip3 install --user jinja2 pyyaml          # only for the optional config validator
```

Note your interface name and IP — you need both later:

```bash
ip -br addr
ip route | grep default
```

Example used throughout: interface **`eth0`**, host IP **`192.168.6.90`**.
Substitute yours everywhere.

> Passwordless sudo helps. If your user needs a sudo password, put it in
> `hosts.ini` (step 4).

---

## 2. Get the code

```bash
cd ~
git clone https://github.com/opennetworkinglab/aether-onramp.git
cd aether-onramp
```

Everything below runs from `~/aether-onramp` unless stated otherwise.

> **The three helper scripts this guide uses are additions, not part of upstream
> Aether OnRamp.** Copy them into `tools/` before you start:
>
> | Script | Purpose | Needed at |
> |---|---|---|
> | `tools/gen-slices.py` | generate slice/device-group/UPF config for N slices | step 6a |
> | `tools/fix-ca-secret.sh` | work around the chart's broken CA upgrade path | day-2, Error 1 |
> | `tools/seed-nsi-nrf.sh` | seed an NSI's NRF with the shared NF profiles | step 11 |
>
> ```bash
> mkdir -p tools && chmod +x tools/*.sh tools/*.py
> ```

---

## 3. Plan your addresses

Decide these before editing anything. Collisions here cause failures that look
unrelated later.

| Slice | S-NSSAI | UPF access | UPF core | UE pool | Namespace | `upf-name` | IMSIs |
|---|---|---|---|---|---|---|---|
| 1 | sst 1 / sd 010203 | 192.168.252.3 | 192.168.250.3 | 192.168.100.0/24 | `aether-5gc` | `upf` | …7500–7524 |
| 2 | sst 2 / sd 010205 | 192.168.252.6 | 192.168.250.6 | 192.168.101.0/24 | `aether-upf-1` | `upf.aether-upf-1` | …7525–7599 |

Fixed values used below:

| Item | Value |
|---|---|
| PLMN | MCC **208**, MNC **93** |
| Access subnet / gateway | 192.168.252.0/24, `.1` on the host |
| Core subnet / gateway | 192.168.250.0/24, `.1` on the host |
| RAN subnet (`ran_subnet`) | 172.20.0.0/16 |
| gNB N3 source IP | 172.20.0.2 (added to the host `access` interface) |
| AMF N2 | `<your host IP>:38412` SCTP |
| Ki / OPc | `5122250214c33e723a5dd523fc145fc0` / `981d464c7c52eb6e5036234984ad0bcf` |

`.1` in each subnet is the host gateway — never assign it to a UPF.

The IMSI split at **7525** is deliberate: it matches the shipped gNBsim profiles
(7510–7514 on slice 1, 7530–7534 on slice 2) so the built-in simulator also works.

---

## 4. Configure `hosts.ini`

```bash
nano hosts.ini
```

Set the `node1` line to your user, and **uncomment `node1` under
`[ueransim_nodes]`** — it ships entirely commented out, which silently makes the
UERANSIM playbooks no-ops:

```ini
[all]
node1 ansible_host=127.0.0.1 ansible_user=<your-user> ansible_password=<your-password> ansible_sudo_pass=<your-sudo-password>

[master_nodes]
node1

[worker_nodes]

[gnbsim_nodes]
node1

[ueransim_nodes]
node1
```

Drop `ansible_password`/`ansible_sudo_pass` if you have passwordless sudo.

Verify Ansible can reach the host:

```bash
make aether-pingall
```

---

## 5. Configure `vars/main.yml`

```bash
nano vars/main.yml
```

**5a. Set your interface and AMF IP** in the `core:` block:

```yaml
core:
  standalone: true          # simapp provisions slices; no ROC/AMP needed
  data_iface: eth0          # <-- YOUR interface
  amf:
    ip: "192.168.6.90"      # <-- YOUR host IP (the RAN connects here)
```

**5b. Add the UPF chart reference and the second UPF.** The Quick Start file has
none of these keys, so `make aether-add-upfs` would have nothing to deploy.
Replace the whole `core.upf:` block with:

```yaml
  upf:
    access_subnet: "192.168.252.1/24"
    core_subnet: "192.168.250.1/24"
    mode: af_packet
    multihop_gnb: false
    helm:                    # chart used for the additional UPFs
      local_charts: false
      chart_ref: oci://ghcr.io/omec-project/bess-upf
      chart_version: 1.7.2
    values_file: "deps/5gc/roles/upf/templates/upf-5g-values.yaml"
    default_upf:             # slice 1, lives inside the sd-core chart
      ip:
        access: "192.168.252.3"
        core:   "192.168.250.3"
      ue_ip_pool: "192.168.100.0/24"
    additional_upfs:         # slice 2 -> namespace aether-upf-1
      "1":
        ip:
          access: "192.168.252.6"
          core:   "192.168.250.6"
        ue_ip_pool: "192.168.101.0/24"
```

The dict key (`"1"`) becomes the namespace suffix: `aether-upf-1`.

**5c. Add a `ueransim:` section** at the top level (same indentation as `core:`):

```yaml
ueransim:
  docker:
    container:
      image: aetherproject/ueransim:rel-0.8.0
      name: ueransim
    network:
      name: host
  gnb:
    ip: "172.20.0.2"        # must be inside ran_subnet and exist on the host
  servers:
    0:
      gnb: "deps/ueransim/config/custom-gnb.yaml"
      ue:  "deps/ueransim/config/custom-ue.yaml"
```

---

## 6. Configure slices, NSSF and AMF

All of this goes into
`deps/5gc/roles/core/templates/sdcore-5g-values.yaml` **before the first
install**. Doing it now avoids the Helm upgrade bug entirely
([Error 1](#error-1--buildcustomcert-unable-to-decode-base64-certificate)) —
a fresh install never hits it.

### 6a. Generate the slice blocks

```bash
python3 tools/gen-slices.py --slices 2 --imsis-per-slice 25 > /tmp/slices.txt
head -8 /tmp/slices.txt
```

If `tools/gen-slices.py` is not in your checkout, copy it from this bundle. It
prints a plan plus three ready-to-paste blocks.

### 6b. Paste the device groups and slices

```bash
nano deps/5gc/roles/core/templates/sdcore-5g-values.yaml
```

Under `omec-sub-provision.config.simapp.cfgFiles.simapp.yaml.configuration`,
replace the existing `device-groups:` and `network-slices:` blocks with blocks
2 and 3 from `/tmp/slices.txt`.

Leave `subscribers:` as shipped — it already covers …7500–7599, which must cover
every IMSI you place in a device group.

The result must contain two slices ending like this:

```yaml
            network-slices:
            - name: "default"
              slice-id:
                sd: "010203"
                sst: 1
              site-device-group:
              - "user-group1"
              ...
                upf:
                  upf-name: "upf"                 # slice 1
                  upf-port: 8805
            - name: "slice2"
              slice-id:
                sd: "010205"
                sst: 2
              site-device-group:
              - "user-group2"
              ...
                upf:
                  upf-name: "upf.aether-upf-1"    # slice 2
                  upf-port: 8805
```

### 6c. Add the NSSF `nsiList` — this is what makes the NSSF decide

In the **same file**, under `5g-control-plane.config:` (sibling of the existing
`nrf:` block), add:

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

### 6d. Disable AMF NRF caching

Still in the same file, inside the existing `amf.cfgFiles.amfcfg.yaml.configuration`:

```yaml
    amf:
      ngapp:
        externalIp: {{ core.amf.ip }}
      cfgFiles:
        amfcfg.yaml:
          configuration:
            enableDBStore: true
            enableNrfCaching: false      # <-- ADD: cache is not keyed by NRF URI
            networkFeatureSupport5GS:
              imsVoPS: 1
```

With two NRFs, a cached result from NRF-2 can be served for a query that should
have gone to NRF-1, putting both slices on one SMF.

### 6e. Validate before installing (optional but cheap)

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
cp  = d["5g-control-plane"]["config"]
sim = d["omec-sub-provision"]["config"]["simapp"]["cfgFiles"]["simapp.yaml"]["configuration"]
for g in sim["device-groups"]:
    print(f"group {g['name']:12s} imsis={len(g['imsis']):3d} pool={g['ip-domains'][0]['ue-ip-pool']}")
for s in sim["network-slices"]:
    print(f"slice {s['name']:9s} sst={s['slice-id']['sst']} sd={s['slice-id']['sd']} upf={s['site-info']['upf']['upf-name']}")
for n in cp["nssf"]["cfgFiles"]["nssfcfg.yaml"]["configuration"]["nsiList"]:
    print(f"nsi   sst={n['snssai']['sst']} sd={n['snssai']['sd']} -> {n['nsiInformationList'][0]['nrfId']}")
print("amf enableNrfCaching:", cp["amf"]["cfgFiles"]["amfcfg.yaml"]["configuration"]["enableNrfCaching"])
print("RENDER OK")
PY
```

Expect two groups, two slices, two `nsi` lines with **different** NRFs, and
`enableNrfCaching: False`.

---

## 7. Install Kubernetes

```bash
make aether-k8s-install
```

Installs RKE2 (with Multus, needed for UPF macvlan) and Helm.

```bash
export KUBECONFIG=$HOME/.kube/config     # add to ~/.bashrc
kubectl get nodes
# node1   Ready   control-plane,etcd   ...
```

If `~/.kube/config` is missing:

```bash
mkdir -p ~/.kube
sudo cp /etc/rancher/rke2/rke2.yaml ~/.kube/config
sudo chown $USER ~/.kube/config
```

---

## 8. Install SD-Core (NSI-1)

```bash
make aether-5gc-install
```

This configures host networking (macvlan `access`/`core` interfaces, IP
forwarding, UE NAT) and installs the `sd-core` chart into `aether-5gc`.

```bash
kubectl get pods -n aether-5gc
```

Wait for all ~16 pods `Running`.

> **`rescued=1` on this first run is normal.** Helm's `--wait` has a 150 s
> timeout that first-time image pulls routinely exceed; the Ansible role
> recovers and the playbook exits 0. On *later* runs `rescued=1` means something
> genuinely failed — see [Error 9](#error-9--playbook-reports-rescued1).

Verify the host side:

```bash
ip -br addr show access      # 192.168.252.1/24
ip -br addr show core        # 192.168.250.1/24
kubectl -n aether-5gc get svc amf -o wide     # EXTERNAL-IP == your host IP
```

---

## 9. Deploy UPF-2

```bash
make aether-add-upfs
```

For each `additional_upfs` entry this installs the `bess-upf` chart into
`aether-upf-<key>`, adds a host route for the UE pool and a MASQUERADE rule.

```bash
kubectl get pods -A | grep upf-0
#   aether-5gc     upf-0   5/5  Running     <- slice 1
#   aether-upf-1   upf-0   5/5  Running     <- slice 2

ip route | grep 192.168.10
#   192.168.100.0/24 via 192.168.250.3 dev core
#   192.168.101.0/24 via 192.168.250.6 dev core
```

> `kubectl get pods -n aether-5gc` shows only **one** UPF. The second is in its
> own namespace. Use `-A`.

Confirm both UPFs are associated:

```bash
kubectl -n aether-5gc logs deploy/smf | grep -i AssociatedSetUpSuccess
# ... for NodeID[10.43.x.x]   (one line per UPF)
```

---

## 10. Deploy NSI-2 (NRF-2 + SMF-2)

### 10a. Namespace and shared CA

The AMF in `aether-5gc` makes HTTPS calls to SMF-2 in `aether-nsi2`. The chart
generates a **per-release CA**, so NSI-2's certs would be untrusted. Share the CA
— **double-encoded**, because of a chart bug
([Error 1](#error-1--buildcustomcert-unable-to-decode-base64-certificate)):

```bash
kubectl create namespace aether-nsi2

kubectl -n aether-5gc get secret 5g-control-plane-ca-private -o json | python3 -c "
import json,sys,base64
s=json.load(sys.stdin); d=s['data']
out={k: base64.b64encode(d[k].encode()).decode() for k in ('ca.crt','ca.key')}
print(json.dumps({'apiVersion':'v1','kind':'Secret','type':'Opaque',
  'metadata':{'name':'5g-control-plane-ca-private','namespace':'aether-nsi2'},
  'data':out}))" | kubectl apply -f -

# decoding TWICE must yield PEM
kubectl -n aether-nsi2 get secret 5g-control-plane-ca-private \
  -o jsonpath='{.data.ca\.crt}' | base64 -d | base64 -d | head -1
# -----BEGIN CERTIFICATE-----
```

Cert SANs are `<name>`, `<name>.<namespace>`, `<name>.<namespace>.svc`,
`<name>.<namespace>.svc.cluster.local`, so `smf.aether-nsi2` validates.

### 10b. Create the NSI-2 values file

```bash
nano deps/5gc/roles/core/templates/nsi2-values.yaml
```

```yaml
omec-control-plane:
  enable4G: false
omec-sub-provision:
  enable: false          # subscribers come from the aether-5gc simapp
omec-user-plane:
  enable: false          # UPF-2 already deployed by `make aether-add-upfs`
5g-ran-sim:
  enable: false

5g-control-plane:
  enable5G: true
  nodeSelectors:
    enabled: false
  resources:
    enabled: false
  kafka:
    deploy: false        # reuse kafka in aether-5gc
  mongodb:
    deploy: false        # reuse mongodb in aether-5gc

  config:
    certs:
      sharedCA:
        existingPrivateSecret: "5g-control-plane-ca-private"

    # SEPARATE database. Both NRFs persist NF profiles under this name; sharing
    # `aether` makes NRF-2 see NRF-1's registrations and NSI separation
    # silently collapses.
    mongodb:
      name: aether_nsi2
      url: mongodb://mongodb-headless.aether-5gc:27017/?replicaSet=rs0
      authKeysDbName: authentication
      authUrl: mongodb://mongodb-headless.aether-5gc:27017/?replicaSet=rs0

    managedByConfigPod:
      enabled: true

    # ---------- everything except NRF and SMF is off ----------
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

    # ---------- NRF-2 ----------
    nrf:
      deploy: true
      cfgFiles:
        nrfcfg.yaml:
          configuration:
            mongoDBStreamEnable: false
            nfProfileExpiryEnable: false          # keep the seeded shared NFs
            nfKeepAliveTime: 60
            webuiUri: http://webui.aether-5gc:5001
            sbi:
              registerIPv4: nrf.aether-nsi2       # MUST be namespace-qualified

    # ---------- SMF-2 ----------
    smf:
      deploy: true
      cfgFiles:
        smfcfg.yaml:
          configuration:
            smfName: SMF-NSI2
            enableDBStore: true
            enableNrfCaching: true
            nrfUri: https://nrf.aether-nsi2:29510      # register into NRF-2
            webuiUri: http://webui.aether-5gc:5001
            kafkaInfo:
              brokerUri: kafka.aether-5gc
              brokerPort: 9092
              topicName: sdcore-data-source-smf
            sbi:
              registerIPv4: smf.aether-nsi2            # MUST be qualified
```

Both `registerIPv4` values must be namespace-qualified. Left as bare `smf`, the
AMF discovers NSI-2's profile and then dials `smf`, which resolves to **SMF-1** —
the experiment silently does nothing.

### 10c. Install

```bash
helm upgrade --install nsi2 oci://ghcr.io/omec-project/sd-core --version 4.1.0 \
  -n aether-nsi2 --values deps/5gc/roles/core/templates/nsi2-values.yaml \
  --wait --timeout 5m

kubectl -n aether-nsi2 get pods
#   nrf-...   1/1  Running
#   smf-...   1/1  Running
```

> Do **not** use `helm template` to preview this — it renders client-side and its
> `lookup` of the CA secret returns empty, failing with "existing private CA
> Secret ... not found". Use `--dry-run=server` if you want a preview.

Confirm SMF-2 registered with the correct address:

```bash
kubectl -n aether-nsi2 port-forward svc/nrf 29510:29510 >/dev/null 2>&1 &
sleep 3
curl -sk "https://127.0.0.1:29510/nnrf-disc/v1/nf-instances?target-nf-type=SMF&requester-nf-type=AMF" \
  | python3 -m json.tool | grep -E "ipv4Addresses|apiPrefix"
pkill -f "port-forward svc/nrf"
```

Must show `["smf.aether-nsi2"]`. If it shows `["smf"]`, fix
[Error 3](#error-3--slice-2-is-handled-by-smf-1).

---

## 11. Seed NRF-2 with the shared NFs

SD-Core has **no NRF federation**. Each NF registers into exactly one NRF, so
NRF-2 currently knows only SMF-2 — no UDM, UDR, AUSF or PCF. Without this step
every slice-2 session fails with `UDM discovery returned no NF instances`.

```bash
./tools/seed-nsi-nrf.sh
# seeded 6 shared NF profiles into aether_nsi2
#    {"nftype":"SMF","ipv4addresses":["smf.aether-nsi2"]}
#    {"nftype":"UDM","ipv4addresses":["udm.aether-5gc"]}
#    ...
```

The script rewrites bare hostnames (`udm` → `udm.aether-5gc`) because bare names
do not resolve across namespaces. Signature:

```
tools/seed-nsi-nrf.sh [src-db] [dst-db] [shared-ns] [mongo-pod] [mongo-ns]
defaults:              aether   aether_nsi2 aether-5gc mongodb-0  aether-5gc
```

Then restart SMF-2 so it re-discovers:

```bash
kubectl -n aether-nsi2 rollout restart deploy/smf
kubectl -n aether-nsi2 rollout status  deploy/smf
```

**Re-run this script** whenever the shared NFs re-register — i.e. after any
`make 5gc-core-install`.

---

## 12. Build and configure UERANSIM

### 12a. Build

```bash
sudo apt install -y make gcc g++ libsctp-dev lksctp-tools iproute2 cmake
cd ~
git clone https://github.com/aligungr/UERANSIM.git
cd UERANSIM
make          # ~10 minutes
ls build/     # nr-gnb  nr-ue  nr-cli  nr-binder
```

### 12b. The N3 rule that will bite you

The gNB's `gtpIp` **must be inside the UPF's `enb.subnet`** (`172.20.0.0/16`).
Each UPF carries exactly one route back to the RAN:

```
172.20.0.0/16 via 192.168.252.1 dev access
```

BESS installs a next-hop only for that subnet. Point `gtpIp` at the gateway
itself (`192.168.252.1`) and **sessions establish but no user data flows**.

```bash
sudo ip addr add 172.20.0.2/16 dev access
ip -br addr show access      # 192.168.252.1/24 172.20.0.2/16
```

This is runtime-only; re-add after a reboot, or add it to
`/etc/systemd/network/20-aether-access.network`.

### 12c. gNB config — advertise both slices

```bash
nano ~/UERANSIM/config/sdcore-2slice-gnb.yaml
```

```yaml
mcc: '208'
mnc: '93'
nci: '0x000000010'
idLength: 32
tac: 1

linkIp: 127.0.0.1          # radio-link sim to the UEs
ngapIp: 192.168.6.90       # N2 to AMF  == core.amf.ip   <-- YOUR host IP
gtpIp: 172.20.0.2          # N3 source  -- inside 172.20.0.0/16

amfConfigs:
  - address: 192.168.6.90  # <-- YOUR host IP
    port: 38412

slices:
  - sst: 1
    sd: 0x010203
  - sst: 2
    sd: 0x010205

ignoreStreamIds: true
```

### 12d. UE configs — one per slice

Create the slice-1 UE from scratch (upstream UERANSIM ships no plain `ue.yaml`):

```bash
cat > ~/UERANSIM/config/sdcore-slice1-ue.yaml <<'EOF'
supi: 'imsi-208930100007510'        # must be inside user-group1 (7500-7524)
mcc: '208'
mnc: '93'
protectionScheme: 0
homeNetworkPublicKey: '5a8d38864820197c3394b92613b20b91633cbd897119273bf8e4a6f4eec0a650'
homeNetworkPublicKeyId: 1
routingIndicator: '0000'

key: '5122250214c33e723a5dd523fc145fc0'
op: '981d464c7c52eb6e5036234984ad0bcf'
opType: 'OPC'
amf: '8000'
imei: '356938035643803'
imeiSv: '4370816125816151'

gnbSearchList:
  - 127.0.0.1

uacAic:
  mps: false
  mcs: false
uacAcc:
  normalClass: 0
  class11: false
  class12: false
  class13: false
  class14: false
  class15: false

sessions:
  - type: 'IPv4'
    apn: 'internet'
    slice:
      sst: 1
      sd: 0x010203

configured-nssai:
  - sst: 1
    sd: 0x010203
default-nssai:
  - sst: 1
    sd: 1

integrity:
  IA1: true
  IA2: false
  IA3: false
ciphering:
  EA1: false
  EA2: false
  EA3: false
integrityMaxRate:
  uplink: 'full'
  downlink: 'full'
EOF
```

Then derive the slice-2 UE:

```bash
sed -e "s/^supi: .*/supi: 'imsi-208930100007530'/" \
    -e "s/sst: 1/sst: 2/g" -e "s/sd: 0x010203/sd: 0x010205/g" \
    sdcore-slice1-ue.yaml > sdcore-slice2-ue.yaml

grep -E "^supi|sst:|sd:" sdcore-slice2-ue.yaml
```

The IMSI must belong to the device group whose slice the UE requests, or the PDU
session is rejected.

### 12e. Runner script

```bash
mkdir -p ~/UERANSIM/logs
cat > ~/UERANSIM/run-2slice.sh <<'EOF'
#!/bin/bash
cd /home/ubuntu/UERANSIM
L=/home/ubuntu/UERANSIM/logs
pkill -x nr-gnb 2>/dev/null; pkill -x nr-ue 2>/dev/null
sleep 2
rm -f $L/gnb.log $L/ue1.log $L/ue2.log
setsid ./build/nr-gnb -c config/sdcore-2slice-gnb.yaml > $L/gnb.log 2>&1 < /dev/null &
sleep 5
setsid ./build/nr-ue -c config/sdcore-slice1-ue.yaml > $L/ue1.log 2>&1 < /dev/null &
setsid ./build/nr-ue -c config/sdcore-slice2-ue.yaml > $L/ue2.log 2>&1 < /dev/null &
sleep 12
echo "gNB NG-Setup : $(grep -c 'NG Setup procedure is successful' $L/gnb.log)"
echo "UE1 slice1   : $(grep -o 'uesimtun0, [0-9.]*' $L/ue1.log | tail -1)"
echo "UE2 slice2   : $(grep -o 'uesimtun0, [0-9.]*' $L/ue2.log | tail -1)"
EOF
chmod +x ~/UERANSIM/run-2slice.sh
```

---

## 13. Run and verify

### 13a. Does the NSSF return different NRFs?

```bash
kubectl -n aether-5gc port-forward svc/nssf 29531:29531 >/dev/null 2>&1 &
sleep 3
B="https://127.0.0.1:29531/nnssf-nsselection/v2/network-slice-information"
P="nf-type=AMF&nf-id=t&slice-info-request-for-pdu-session%5BroamingIndication%5D=NON_ROAMING"
curl -sk "$B?$P&slice-info-request-for-pdu-session%5BsNssai%5D%5Bsst%5D=1&slice-info-request-for-pdu-session%5BsNssai%5D%5Bsd%5D=010203"; echo
curl -sk "$B?$P&slice-info-request-for-pdu-session%5BsNssai%5D%5Bsst%5D=2&slice-info-request-for-pdu-session%5BsNssai%5D%5Bsd%5D=010205"; echo
pkill -f "port-forward svc/nssf"
```

Expected — **different NRFs**:

```json
{"nsiInformation":{"nrfId":"https://nrf:29510/nnrf-nfm/v1/nf-instances","nsiId":"1"}}
{"nsiInformation":{"nrfId":"https://nrf.aether-nsi2:29510/nnrf-nfm/v1/nf-instances","nsiId":"2"}}
```

If slice 2 returns `{}`, the NSSF pod is serving stale config —
[Error 8](#error-8--nssf-still-returns-the-old-nsilist).

### 13b. Are the two registries separate?

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

Exactly one SMF each. Both in one list means
[Error 2](#error-2--nrf-2-sees-nrf-1s-registrations).

### 13c. The real test

```bash
sudo ~/UERANSIM/run-2slice.sh
# gNB NG-Setup : 1
# UE1 slice1   : uesimtun0, 192.168.100.x
# UE2 slice2   : uesimtun0, 192.168.101.x
```

**Each SMF must handle exactly one IMSI** — this is the proof:

```bash
echo "SMF-1:"; kubectl -n aether-5gc  logs deploy/smf --since=3m | grep -oE 'imsi-[0-9]+' | sort -u
#   imsi-208930100007510
echo "SMF-2:"; kubectl -n aether-nsi2 logs deploy/smf --since=3m | grep -oE 'imsi-[0-9]+' | sort -u
#   imsi-208930100007530
```

PFCP shows the user plane split too:

```bash
kubectl -n aether-5gc  logs deploy/smf --since=3m | grep "Session Establish Request"
#   ... NodeID[10.43.233.120] ... "id": "imsi-208930100007510"   <- UPF-1
kubectl -n aether-nsi2 logs deploy/smf --since=3m | grep "Session Establish Request"
#   ... NodeID[10.43.188.100] ... "id": "imsi-208930100007530"   <- UPF-2
```

### 13d. Data path

Each PDU session lives in its own network namespace, so `ip addr` on the host
shows nothing:

```bash
sudo ip netns list | grep uesimtun
NS1=uesimtun-208930100007510-internet-psi1
NS2=uesimtun-208930100007530-internet-psi1

sudo ip netns exec $NS1 ip -br -4 addr show uesimtun0     # 192.168.100.x
sudo ip netns exec $NS2 ip -br -4 addr show uesimtun0     # 192.168.101.x

sudo ip netns exec $NS1 ping -c3 -I uesimtun0 8.8.8.8
sudo ip netns exec $NS2 ping -c3 -I uesimtun0 8.8.8.8
```

### 13e. Watch the split on the wire

```bash
sudo tcpdump -ni access 'udp port 2152'
# 172.20.0.2.2152 > 192.168.252.3    <- slice 1
# 172.20.0.2.2152 > 192.168.252.6    <- slice 2
```

---

## 14. Prove the isolation

The reason NSIs exist. Kill NSI-2's control plane; slice 1 must survive:

```bash
kubectl -n aether-nsi2 scale deploy/smf --replicas=0
sudo ~/UERANSIM/run-2slice.sh
# UE1 slice1   : uesimtun0, 192.168.100.x     <- still works
# UE2 slice2   :                              <- no session

kubectl -n aether-nsi2 scale deploy/smf --replicas=1
kubectl -n aether-nsi2 rollout status deploy/smf
```

With a shared control plane, killing the single SMF takes down **both** slices.
That difference is the entire value proposition.

---

## 15. Day-2 operations

**Any change to `sdcore-5g-values.yaml`** (slices, NSSF, AMF) requires:

```bash
./tools/fix-ca-secret.sh aether-5gc      # REQUIRED -- see Error 1
make 5gc-core-install                    # expect failed=0 rescued=0
kubectl -n aether-5gc rollout restart deploy/nssf   # NSSF does not hot-reload
./tools/seed-nsi-nrf.sh                  # shared NFs re-registered; re-seed NRF-2
kubectl -n aether-nsi2 rollout restart deploy/smf
```

**After a host reboot:**

```bash
sudo ip addr add 172.20.0.2/16 dev access
```

**Restart the RAN:** `sudo ~/UERANSIM/run-2slice.sh`
**Stop it:** `sudo pkill -x nr-ue; sudo pkill -x nr-gnb`

---

## 16. Troubleshooting — every error and its fix

Listed in the order you are most likely to meet them.

### Error 1 — `buildCustomCert: unable to decode base64 certificate`

```
Error: UPGRADE FAILED: template: sd-core/charts/5g-control-plane/templates/secret-certs.yaml:12:14:
  ... at <buildCustomCert $caCrt $caKey>: error calling buildCustomCert:
  unable to decode base64 certificate
```

**A chart bug that breaks every `helm upgrade` of SD-Core 4.1.0**, not just
multi-NSI work. `_helpers.tpl` does:

```gotemplate
{{- $caCrt = index $existingCaSecret.data "ca.crt" | b64dec -}}   # raw PEM
{{- $ca = buildCustomCert $caCrt $caKey -}}                       # wants BASE64 PEM
```

Fresh installs have no secret, so the chart calls `genCA` and works — which is
why only upgrades break, and why this guide front-loads all config into step 6.

**Symptom you may already see:** `helm list -n aether-5gc` showing `sd-core` as
`failed` while every pod is Running.

**Fix** — store the secret double-encoded so the chart's `b64dec` yields base64:

```bash
./tools/fix-ca-secret.sh aether-5gc
```

The chart rewrites it single-encoded after a successful upgrade, so run this
**before every upgrade**.

### Error 2 — NRF-2 sees NRF-1's registrations

**Symptom.** NRF-2 discovery lists two SMFs, one of them `['smf']`. Slice-2
sessions may land on SMF-1 while everything looks correct.

**Cause.** Both NRFs persist NF profiles into the `NfProfile` collection of the
database named by `config.mongodb.name`. Sharing `aether` merges the registries.

**Fix.** `config.mongodb.name: aether_nsi2` in the NSI values, then clean what
already leaked:

```bash
kubectl -n aether-5gc exec mongodb-0 -- mongosh --quiet --eval '
  print(db.getSiblingDB("aether").NfProfile.deleteMany({ipv4addresses:"smf.aether-nsi2"}).deletedCount);'
kubectl -n aether-5gc rollout restart deploy/nrf
```

### Error 3 — slice 2 is handled by SMF-1

**Symptom.** NRF-2 lists the SMF as `ipv4Addresses: ["smf"]`,
`apiPrefix: https://smf:29502`. Nothing errors; the wrong SMF just serves the slice.

**Cause.** `registerIPv4` defaults to the bare name `smf`, which resolves to
**SMF-1** from the AMF's namespace.

**Fix.** `registerIPv4: smf.aether-nsi2` (and `nrf.aether-nsi2`) in the NSI values,
then `helm upgrade` NSI-2.

### Error 4 — `UDM discovery returned no NF instances`

```
ERROR producer/pdu_session.go:188  PDUSessionSMContextCreate,
  send NF Discovery Serving UDM Error[UDM discovery returned no NF instances]
```

**Cause.** No NRF federation — NRF-2 contains only SMF-2.

**Fix.** Step 11: `./tools/seed-nsi-nrf.sh`, plus
`nfProfileExpiryEnable: false` on NRF-2 so the seeded profiles are not purged
(nothing heartbeats them).

**Sub-error:** seeding unmodified profiles gives
`lookup udm on 10.43.0.10:53: no such host` — bare names do not resolve across
namespaces. The script rewrites them.

### Error 5 — both slices land on the same SMF

**Symptom.** SMF-2's log shows *both* IMSIs; SMF-1's shows none.

**Cause.** The AMF caches NRF discovery results and the cache does not appear to
be keyed by NRF URI, so a result from NRF-2 gets served for an NRF-1 query.

**Fix.** `enableNrfCaching: false` on the AMF (step 6d), then restart it.

> The correlation was clear and disabling the cache resolved it, but the exact
> mechanism is *suspected*, not proven.

### Error 6 — `Error during Process: datapath down`

```
ERROR pfcpiface/messages.go:142  error handling PFCP message type
  Association Setup Request, ... error: Error during Process: datapath down
```
```
ERROR producer/pdu_session.go:366  UPF association recovery failed:
  UPF 10.43.188.100 not associated after PFCP association retry
```

**Cause.** The BESS datapath stopped serving while all five containers still
reported `Running`/`Ready` with 0 restarts. pfcpiface rejects every association
and never self-heals.

**Fix.**

```bash
kubectl -n aether-upf-1 delete pod upf-0
```

**Pod health tells you nothing here** — check
`kubectl -n <ns> logs upf-0 -c pfcp-agent | grep -i datapath`.

### Error 7 — `helm template` fails on the CA lookup

```
Error: existing private CA Secret "5g-control-plane-ca-private" was configured but not found
```

`helm template` renders client-side; its `lookup` returns empty. Harmless. Use
`helm install`/`upgrade` or `--dry-run=server`.

### Error 8 — NSSF still returns the old `nsiList`

**Symptom.** The `nssf` ConfigMap has your new list but the API returns
`nsiId: 22` and `{}` for slice 2.

**Cause.** The NSSF does not reload its ConfigMap at runtime.

**Fix.** `kubectl -n aether-5gc rollout restart deploy/nssf`

### Error 9 — playbook reports `rescued=1`

On the **first** install this is benign (Helm `--wait` timing out during image
pulls). On any **later** run it means the Helm task failed and **your config
changes were not applied**, even though the playbook exits 0.

```bash
grep -oE "Error: UPGRADE FAILED[^\"]{0,120}" <logfile>
```

Almost always Error 1 → run `./tools/fix-ca-secret.sh aether-5gc` and retry.

### Error 10 — PDU session establishes but no traffic flows

**Cause.** `gtpIp` outside `172.20.0.0/16`.

```bash
ip -br addr show access                    # 172.20.0.2/16 present?
sudo tcpdump -ni access 'udp port 2152'    # uplink present, downlink absent?
```

Uplink reaching the UPF with nothing returning confirms it. Fix `gtpIp` and
`sudo ip addr add 172.20.0.2/16 dev access`.

### Error 11 — UE registers but the PDU session is rejected

The UE's `sessions[].slice` must match the S-NSSAI of the slice whose device
group contains its IMSI. Slice 1 = 7500–7524, slice 2 = 7525–7599.

### Error 12 — `Accept header must be 'application/json'`

The webconsole `/nfconfig/*` endpoints require it:
`curl -H "Accept: application/json" ...`

### Error 13 — `make aether-ueransim-install` fails to start Docker

```
Unable to start service docker: Job for docker.service failed
```

systemd rate-limiting, not a config problem:

```bash
sudo systemctl reset-failed docker.service docker.socket
sudo systemctl start docker
```

### Known noise you can ignore

SMF-2 logs this forever:

```
WARN  host lookup failed: lookup upf on 10.43.0.10:53: no such host
ERROR send pfcp association setup request failed: ... invalid NodeId: upf
```

Both SMFs poll the same webconsole, so SMF-2 also learns about UPF-1 and keeps
trying to reach it by a name that does not resolve from its namespace. Harmless.
Fixing it properly needs a second webconsole with per-NSI slice config, which
SD-Core has no mechanism for.

---

## 17. Adding a third slice

**User plane** (from `MULTI-SLICE.md`):

1. `vars/main.yml` → `additional_upfs["2"]`: access `192.168.252.7`, core
   `192.168.250.7`, pool `192.168.102.0/24`
2. `sdcore-5g-values.yaml` → third device group + third slice
   (`sst 3 / sd 010207`, `upf-name: "upf.aether-upf-2"`)
3. gNB config → third entry under `slices:`

Generate all three with `python3 tools/gen-slices.py --slices 3`.

**Control plane:**

4. `kubectl create namespace aether-nsi3`; copy the double-encoded CA into it
5. `cp nsi2-values.yaml nsi3-values.yaml` and change: `mongodb.name:
   aether_nsi3`, `nrf...registerIPv4: nrf.aether-nsi3`, `smf...nrfUri:
   https://nrf.aether-nsi3:29510`, `smf...registerIPv4: smf.aether-nsi3`,
   `smfName: SMF-NSI3`
6. `helm upgrade --install nsi3 ... -n aether-nsi3 --values .../nsi3-values.yaml`
7. `./tools/seed-nsi-nrf.sh aether aether_nsi3 aether-5gc`
8. Add an `nsiList` entry: `sst 3 / sd 010207` → `https://nrf.aether-nsi3:29510/...`
9. `./tools/fix-ca-secret.sh aether-5gc && make 5gc-core-install && make aether-add-upfs`
10. `kubectl -n aether-5gc rollout restart deploy/nssf`

**Cost per NSI:** ~2 control-plane pods + 1 UPF pod, roughly 1.3 GB RAM.

---

## 18. Teardown

**Back to a shared control plane** (keeps user-plane slicing):

```bash
helm uninstall nsi2 -n aether-nsi2
kubectl delete namespace aether-nsi2
kubectl -n aether-5gc exec mongodb-0 -- mongosh --quiet --eval '
  db.getSiblingDB("aether_nsi2").dropDatabase();'
# remove nsiList + enableNrfCaching from sdcore-5g-values.yaml, then
./tools/fix-ca-secret.sh aether-5gc
make 5gc-core-install
kubectl -n aether-5gc rollout restart deploy/nssf
```

Slice 2 falls back to SMF-1 and keeps working — a neat confirmation that the
NSSF was load-bearing.

**Everything:**

```bash
sudo pkill -x nr-ue; sudo pkill -x nr-gnb
sudo ip addr del 172.20.0.2/16 dev access
helm uninstall nsi2 -n aether-nsi2 ; kubectl delete namespace aether-nsi2
make aether-remove-upfs
make aether-5gc-uninstall
make aether-k8s-uninstall
```

---

## Appendix — what a healthy deployment looks like

```
$ kubectl get pods -A | grep aether
aether-5gc     amf-...          1/1  Running
aether-5gc     ausf-...         1/1  Running
aether-5gc     kafka-controller-0  1/1  Running
aether-5gc     metricfunc-...   1/1  Running
aether-5gc     mongodb-0        1/1  Running
aether-5gc     mongodb-1        1/1  Running
aether-5gc     mongodb-arbiter-0   1/1  Running
aether-5gc     nrf-...          1/1  Running
aether-5gc     nssf-...         1/1  Running
aether-5gc     pcf-...          1/1  Running
aether-5gc     simapp-...       1/1  Running
aether-5gc     smf-...          1/1  Running
aether-5gc     udm-...          1/1  Running
aether-5gc     udr-...          1/1  Running
aether-5gc     upf-0            5/5  Running
aether-5gc     webui-...        1/1  Running
aether-nsi2    nrf-...          1/1  Running
aether-nsi2    smf-...          1/1  Running
aether-upf-1   upf-0            5/5  Running
```

19 pods. `helm list -A` should show `sd-core` (aether-5gc), `nsi2` (aether-nsi2)
and `bess-upf` (aether-upf-1), all `deployed`.
