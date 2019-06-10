# give master nodes ability to mount
mach=$(juju status | grep 'started' | awk '{ print $1 }')

echo $mach
echo $1

for x in $mach
do
cat $1 | juju ssh $x "cat >> ~/.ssh/authorized_keys"
done


