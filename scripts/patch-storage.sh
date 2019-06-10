# create rbd pools
mon=$(juju status | grep ceph-mon/0 | awk '{ print $4 }')
if [ -z $mon ]
then
	echo "Can't find Ceph monitor nodes"
	exit
fi
juju ssh $mon << EOF
sudo ceph osd pool delete rbd rbd --yes-i-really-really-mean-it
sudo ceph osd pool delete ext4-pool ext4-pool --yes-i-really-really-mean-it
sudo ceph osd pool delete xfs-pool xfs-pool --yes-i-really-really-mean-it
sudo ceph osd pool create ext4-pool 64 64
sudo ceph osd pool set ext4-pool size 2
sudo ceph osd pool create xfs-pool 64 64
sudo ceph osd pool set xfs-pool size 2
sudo ceph osd pool set ceph-fs_data pg_num 64
sudo ceph osd pool set ceph-fs_data pgp_num 64
sudo ceph osd pool set ceph-fs_data size 2
sudo ceph osd pool set ceph-fs_metadata pg_num 64
sudo ceph osd pool set ceph-fs_metadata pgp_num 64
sudo ceph osd pool set ceph-fs_metadata size 2
EOF

