# give master nodes ability to mount
mon=$(juju status | grep ceph-mon/0 | awk '{ print $4 }')
masters=$(juju status | grep kubernetes-master/ | awk '{ print $4}')
ips=$(juju status | grep kubernetes-master/ | awk '{ print $5}')

if [ -z $mon ] ||  [ -z $masters ] || [ -z $ips ] 
then
	echo "Required nodes aren't found"
	exit
fi

echo $mon
echo $masters
echo $ips

juju ssh $mon <<EOF
sudo apt-get install -y ceph-deploy
yes y | ssh-keygen -t rsa -N '' -f ~/.ssh/id_rsa
sudo rm ~/.ssh/config
echo "Host *" > ~/.ssh/config
echo "     StrictHostKeyChecking no" >> ~/.ssh/config
sudo chmod 400 ~/.ssh/config
EOF

# x in master node number
for x in $masters
do
juju ssh $x <<EOF 
sudo mkdir /etc/ceph
sudo chmod -R 777 /etc/ceph
sudo apt-get install -y ceph-fuse
EOF
juju ssh $mon "cat ~/.ssh/id_rsa.pub" | juju ssh $x "cat >> ~/.ssh/authorized_keys"
done

for x in $ips
do 
juju ssh $mon <<EOF
cd /etc/ceph
sudo chmod -R 777 /etc/ceph/
ceph-deploy install $x
ceph-deploy --overwrite-conf admin $x
EOF
done

