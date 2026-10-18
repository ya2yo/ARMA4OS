// SPDX-License-Identifier: GPL-2.0
// An allow-all ingress accounting probe. Requires clang -target bpf and libbpf headers.
#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

struct accounting {
    __u64 packets;
    __u64 bytes;
};

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct accounting);
} counters SEC(".maps");

SEC("cgroup_skb/ingress")
int count_ingress(struct __sk_buff *skb)
{
    __u32 key = 0;
    struct accounting *counter = bpf_map_lookup_elem(&counters, &key);
    if (counter) {
        counter->packets++;
        counter->bytes += skb->len;
    }
    return 1; // Always allow. This hook observes traffic; it does not filter it.
}

char LICENSE[] SEC("license") = "GPL";
