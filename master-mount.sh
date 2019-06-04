# give master nodes ability to mount
mon=$(juju status | grep ceph-mon/0 | awk '{ print $4 }')
masters=$(juju status | grep kubernetes-master/ | awk '{ print $4}')
ips=$(juju status | grep kubernetes-master/ | awk '{ print $5}')

echo $mon
echo $masters
echo $ips

juju ssh $mon "sudo apt-get install -y ceph-deploy"

# x in master node number
for x in $masters
do
	juju ssh $x "yes y | ssh-keygen -t rsa -N '' -f ~/.ssh/id_rsa"
	juju ssh $x "sudo mkdir /etc/ceph; sudo chmod -R 777 /etc/ceph"
	juju ssh $mon "cat ~/.ssh/id_rsa.pub" | juju ssh $x "cat >> ~/.ssh/authorized_keys"
	juju ssh $x "sudo apt-get install -y ceph-fuse"
done

for x in $ips
do 
	juju ssh $mon "cd /etc/ceph;ceph-deploy install $x"
	juju ssh $mon "cd /etc/ceph;ceph-deploy --overwrite-conf admin $x"
done

