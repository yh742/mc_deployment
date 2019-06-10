#!/bin/bash

# create rbd pools
mon=$(juju status | grep ceph-mon/0 | awk '{ print $4 }')
all_mons=$(juju status | grep ceph-mon/ | awk '{ print $4 }')
if [ -z $mon ]
then
	echo "Can't find Ceph monitor nodes"
	exit
fi

# calculate pg/pgp number here
mon_num=0
repl_size=2
for x in $all_mons; do mon_num=$(($mon_num + 1)); done 
if [ ! $# == 0 ]; then repl_size=$1; fi
echo "number of monitors is $mon_num"
echo "replication size is $repl_size"
echo "default number of pool is 4 (ext4-pool, xfs-pool, ceph-fs_data, ceph-fs_metadata)"
let "pg_per_pool = $mon_num*100/$repl_size/4"
echo "number of PGs per pool calculated is $pg_per_pool"

exp=0 
while [ $((2**$exp)) -lt $pg_per_pool ]
do
	exp=$(($exp + 1))
done
pg_num=$((2**$exp))
echo "calculated pg_num and pgp_num is $pg_num"

juju ssh $mon << EOF
sudo ceph osd pool delete rbd rbd --yes-i-really-really-mean-it
sudo ceph osd pool delete ext4-pool ext4-pool --yes-i-really-really-mean-it
sudo ceph osd pool delete xfs-pool xfs-pool --yes-i-really-really-mean-it
sudo ceph osd pool create ext4-pool $pg_num $pg_num
sudo ceph osd pool set ext4-pool size $repl_size
sudo ceph osd pool create xfs-pool $pg_num $pg_num
sudo ceph osd pool set xfs-pool size $repl_size
sudo ceph osd pool set ceph-fs_data pg_num $pg_num
sudo ceph osd pool set ceph-fs_data pgp_num $pg_num
sudo ceph osd pool set ceph-fs_data size $repl_size
sudo ceph osd pool set ceph-fs_metadata pg_num $pg_num
sudo ceph osd pool set ceph-fs_metadata pgp_num $pg_num
sudo ceph osd pool set ceph-fs_metadata size $repl_size
EOF
