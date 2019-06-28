#!/bin/bash

sudo snap install yq
enable_lb=$(cat bundle.yaml | yq r - services.kubernetes-master.options.enable-lb)
start_ip=$(cat bundle.yaml | yq r - services.kubernetes-master.options.lb-start-ip)
end_ip=$(cat bundle.yaml | yq r - services.kubernetes-master.options.lb-end-ip)
echo "enable-lb: $enable_lb"
echo "start-ip: $start_ip"
echo "end-ip: $end_ip"

if [ "$enable_lb" == "null" ] || [ "$enable_lb" == "false" ]
then
    echo "error: loadbalancer is not enabled"
    exit
elif [ "$start_ip" == "null" ] 
then
    echo "error: need to specify a starting ip range for loadbalancer"
    exit
elif [ "$end_ip" == "null" ] 
then
    echo "error: need to specify a ending ip range for loadbalancer"
    exit
fi

echo "info: reserving ip on maas"
maas admin ipranges create type=reserved start_ip=$start_ip end_ip=$end_ip comment='Metal Loadbalancer Reserved Range'
if ! [ $? -eq 0 ]
then 
    echo "error: could not set ip range"
    exit
fi

juju deploy bundle.yaml
