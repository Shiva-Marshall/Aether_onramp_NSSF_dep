#!/usr/bin/env python3
"""
Generate the config blocks needed to run SD-Core with N slices, each backed by
its own BESS-UPF.

Emits three blocks to stdout:
  1. core.upf.additional_upfs   -> paste into vars/main.yml
  2. device-groups + network-slices -> paste into
     deps/5gc/roles/core/templates/sdcore-5g-values.yaml (simapp configuration)
  3. gNB `slices:` list         -> paste into your UERANSIM/gNB config

Usage:
    python3 tools/gen-slices.py --slices 3
    python3 tools/gen-slices.py --slices 5 --imsis-per-slice 20

Blocks are printed for you to paste in; nothing is edited in place.

Address plan (slice 1 is the built-in UPF in namespace aether-5gc):
    slice i   access IP           core IP            UE pool            namespace
    1         192.168.252.3       192.168.250.3      192.168.100.0/24   aether-5gc
    2         192.168.252.6       192.168.250.6      192.168.101.0/24   aether-upf-1
    3         192.168.252.7       192.168.250.7      192.168.102.0/24   aether-upf-2
    4         192.168.252.8       192.168.250.8      192.168.103.0/24   aether-upf-3
"""
import argparse, sys

IMSI_PREFIX = "20893010000"      # 11 digits; + 4 more = 15-digit IMSI
DEFAULT_SD = ["010203", "010205", "010207", "010209", "01020b",
              "01020d", "01020f", "010211"]


def slice_params(i, imsis_per_slice, imsi_start):
    """i is 1-based. Slice 1 is the built-in UPF."""
    lo = imsi_start + (i - 1) * imsis_per_slice
    hi = lo + imsis_per_slice - 1
    return {
        "index":   i,
        "name":    "default" if i == 1 else f"slice{i}",
        "group":   f"user-group{i}",
        "pool_nm": f"pool{i}",
        "sst":     i,
        "sd":      DEFAULT_SD[i - 1] if i <= len(DEFAULT_SD) else f"0102{i:02x}",
        "ue_pool": f"192.168.{99 + i}.0/24",
        "access":  f"192.168.252.{3 if i == 1 else 4 + i}",
        "core":    f"192.168.250.{3 if i == 1 else 4 + i}",
        "ns":      "aether-5gc" if i == 1 else f"aether-upf-{i - 1}",
        "upf_nm":  "upf" if i == 1 else f"upf.aether-upf-{i - 1}",
        "key":     None if i == 1 else str(i - 1),
        "imsi_lo": lo,
        "imsi_hi": hi,
    }


def block_additional_upfs(slices):
    L = ["    additional_upfs:"]
    for s in slices[1:]:
        L += [f'      "{s["key"]}":                # slice "{s["name"]}"'
              f' (sst {s["sst"]} / sd {s["sd"]}) -> ns {s["ns"]}',
              "        ip:",
              f'          access: "{s["access"]}"',
              f'          core:   "{s["core"]}"',
              f'        ue_ip_pool: "{s["ue_pool"]}"']
    return "\n".join(L)


def block_device_groups(slices):
    L = ["            device-groups:"]
    for s in slices:
        pool_expr = ("{{ core.upf.default_upf.ue_ip_pool }}" if s["index"] == 1
                     else "{{ core.upf.additional_upfs['%s'].ue_ip_pool }}" % s["key"])
        L.append(f'            - name:  "{s["group"]}"')
        L.append("              imsis:")
        for n in range(s["imsi_lo"], s["imsi_hi"] + 1):
            L.append(f'                - "{IMSI_PREFIX}{n:04d}"')
        L.append("              msisdns:")
        for k in range(s["imsi_hi"] - s["imsi_lo"] + 1):
            L.append(f'                - "msisdn-{9000000000 + s["imsi_lo"] + k}"')
        L += [f'              ip-domain-name: "{s["pool_nm"]}"',
              "              ip-domains:",
              "                - dnn: internet",
              '                  dns-primary: "8.8.8.8"',
              "                  mtu: 1460",
              f"                  ue-ip-pool: {pool_expr}",
              "                  ue-dnn-qos:",
              "                    dnn-mbr-downlink: 1000",
              "                    dnn-mbr-uplink:   1000",
              "                    bitrate-unit: Mbps",
              "                    traffic-class:",
              '                      name: "platinum"',
              "                      qci: 9",
              "                      arp: 6",
              "                      pdb: 300",
              "                      pelr: 6",
              '              site-info: "enterprise"']
    return "\n".join(L)


def block_network_slices(slices):
    L = ["            network-slices:"]
    for s in slices:
        L += [f'            - name: "{s["name"]}"',
              "              slice-id:",
              f'                sd: "{s["sd"]}"',
              f'                sst: {s["sst"]}',
              "              site-device-group:",
              f'              - "{s["group"]}"',
              "              application-filtering-rules:",
              '              - rule-name: "ALLOW-ALL"',
              "                priority: 250",
              '                action: "permit"',
              '                endpoint: "0.0.0.0/0"',
              "                traffic-class:",
              "                  qci: 9",
              "                  arp: 6",
              "              site-info:",
              "                gNodeBs:",
              '                - name: "gnb1"',
              "                  tac: 1",
              '                - name: "gnb2"',
              "                  tac: 2",
              "                plmn:",
              '                  mcc: "208"',
              '                  mnc: "93"',
              '                site-name: "enterprise"',
              "                upf:",
              f'                  upf-name: "{s["upf_nm"]}"',
              "                  upf-port: 8805"]
    return "\n".join(L)


def block_gnb_slices(slices):
    L = ["slices:"]
    for s in slices:
        L += [f'  - sst: {s["sst"]}', f'    sd: 0x{s["sd"]}']
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slices", type=int, required=True, help="total slices (>=1)")
    ap.add_argument("--imsis-per-slice", type=int, default=25)
    ap.add_argument("--imsi-start", type=int, default=7500)
    args = ap.parse_args()
    if args.slices < 1:
        sys.exit("--slices must be >= 1")

    sl = [slice_params(i, args.imsis_per_slice, args.imsi_start)
          for i in range(1, args.slices + 1)]

    print("# ============ PLAN ============")
    for s in sl:
        print(f"#  slice {s['index']}: {s['name']:9s} sst={s['sst']} sd={s['sd']}  "
              f"IMSI {IMSI_PREFIX}{s['imsi_lo']:04d}..{IMSI_PREFIX}{s['imsi_hi']:04d}  "
              f"pool={s['ue_pool']:18s} upf={s['upf_nm']} ({s['ns']})")
    hi = sl[-1]["imsi_hi"]
    print(f"#  subscribers range must cover: {IMSI_PREFIX}{args.imsi_start:04d} .. {IMSI_PREFIX}{hi:04d}")
    print()
    print("# ===== 1. vars/main.yml : replace core.upf.additional_upfs =====")
    print(block_additional_upfs(sl))
    print()
    print("# ===== 2. sdcore-5g-values.yaml : replace device-groups + network-slices =====")
    print(block_device_groups(sl))
    print()
    print(block_network_slices(sl))
    print()
    print("# ===== 3. gNB config : replace slices: =====")
    print(block_gnb_slices(sl))


if __name__ == "__main__":
    main()
