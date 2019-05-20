# Deprecation Notice

This interface is deprecated. Kube-DNS info is now sent via
[interface-kube-control](https://github.com/juju-solutions/interface-kube-control).

# Kube-DNS

This interface provides the DNS details for a Kubernetes cluster.

The majority of kubernetes services will expect the following values:

```
--cluster-dns $IP_OF_DNS_SERVER
--cluster-domain $DOMAIN
```


# Provides

Kubernetes API credentials are sent in the following dict structure:

```python
{"private-address": "",
 "port": "53",
 "domain": "cluster.local",
 "sdn_ip": "10.1.0.10"
}

```

# Requires

```python
@when('kube-dns.available')
def save_dns_credentials(kube_dns):
    context = kube_dns.details()
    print(context['domain'])
    print(context['private-address'])
    print(context['sdn-ip'])
    print(context['port'])
```


