# give master nodes ability to mount
if [ $# -eq 0 ]
then
	echo 'Please supply a public key file to add'
	exit
fi

if ! [[ -f $1 ]]
then
	echo "File specified does not exist!"
	exit
fi

mach=$(juju status | grep 'started' | awk '{ print $1 }')
echo $mach
echo $1

for x in $mach
do
	cat $1 | juju ssh $x "cat >> ~/.ssh/authorized_keys"
done


