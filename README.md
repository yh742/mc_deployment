# MC Deployment

This is a test deployment of Kubernetes, Elastic Stack, and Ceph
* Kubernetes 1.12 Cluster
* Ceph Cluster
* FileBeat
* TopBeat
* Kibana
* Elasticsearch

## Prerequisistes
* MaaS
* Juju
* 14 nodes (minimum for testing)
* Tag the machines in MaaS with the following:

| Charm Names | Nodes Deployed | Machine Tag Name |
|:-----------:|:--------------:|:------------:|
| kubernetes-master | 2 | kmaster |
| kubernetes-worker | 2 | kworker | 
| kubeapi-loadbalancer | 1 | kaux |
| ceph-mon | 3 | ceph-mon |
| ceph-osd | 3 | ceph-osd |
| ceph-fs | 1 | ceph-fs |
| kibana | 1 | kibana |
| elasticsearch | 1 | elasticsearch |
| topbeat | N/A (Subordinate Charm) | N/A |
| filebeat | N/A (Subordinate Charm) | N/A |
| easyrsa | N/A (Shared w/ klb) | N/A |
| etcd | N/A (Shared w/ kubernetes-master) | N/A |
| flannel | N/A (Subordinate Charm) | N/A |

## Instructions
All commands below are issued from where the juju client resides. Everything is included in the package, so when you are ready, issue the following commands in the command prompt:
```
juju deploy bundle.yaml
```
You should see the following when the cluster is up:

![alt text](https://github.com/yh742/mc_deployment/blob/master/success.png)

Once the cluster is up, you can patch the storage so that the correct number of PG and replications size is applied on the pools. You might want to verify the PG num, PGP num, and replication size after this gets applied.
```
./patch-storage.sh
./master-mount.sh
```

